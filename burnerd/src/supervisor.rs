//! The single supervisor: one `Mutex<Supervisor>` holding the profile-engine job,
//! the streaming child job (Auto-UV scan or profile verification — one slot, so
//! they are mutually exclusive), and a generation counter (the Python identity
//! guard).
//!
//! Free functions take `&Mutex<Supervisor>` / `&Arc<Mutex<Supervisor>>` and manage
//! locking themselves so lock scopes stay tiny (parity with `_ACTIVE_SCAN_LOCK`).

use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::fs::File;
use std::io::{Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, ExitStatus};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::Duration;
#[cfg(test)]
use std::time::Instant;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use shared_child::unix::SharedChildExt;
use shared_child::SharedChild;
use tempfile::NamedTempFile;

use crate::api::{
    ActiveJob, EnergySavingsStatus, GameRuntimeStatus, GameWatchStatus, StartResult, StatusResult,
    StopResult,
};
use crate::logging;
use crate::paths;
use crate::profile::{self, EngineHandle, RuntimeSpec, StopOutcome};

const PROGRAM_FILE_ENV: &str = "PENGUIN_BURNER_DAEMON_PROGRAM_FILE";
/// Test-only overrides for the typed runtime-state files.
const STATE_FILE_ENV: &str = "PENGUIN_BURNERD_TEST_STATE_FILE";
const BOOT_STATE_FILE_ENV: &str = "PENGUIN_BURNERD_TEST_BOOT_STATE_FILE";
const ACTIVE_RUNTIME_STATE_PATH: &str = "/run/penguin-burner/active-runtime.json";
const APPLIED_RUNTIME_HISTORY_PATH: &str = "/run/penguin-burner/applied-runtime-history.json";
const BOOT_RUNTIME_STATE_PATH: &str = "/var/lib/penguin-burner/boot-runtime.json";
const ENGINE_STOP_TIMEOUT: Duration = Duration::from_secs(10);
const GAME_WATCH_INTERVAL: Duration = Duration::from_millis(250);
const TEST_GAME_WATCH_INTERVAL: Duration = Duration::from_millis(20);
const BOOT_RUNTIME_SET_FORMAT_VERSION: u32 = 1;
const APPLIED_RUNTIME_HISTORY_FORMAT_VERSION: u32 = 1;
static BOOT_STATE_UPDATE_LOCK: Mutex<()> = Mutex::new(());

fn validate_unique_runtime_specs(specs: &[RuntimeSpec], state_label: &str) -> Result<(), String> {
    let mut seen = std::collections::BTreeSet::new();
    for spec in specs {
        spec.validate()?;
        let uuid = spec.gpu.uuid.trim();
        if !seen.insert(uuid.to_string()) {
            return Err(format!("duplicate {state_label} GPU uuid: {uuid}"));
        }
    }
    Ok(())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct AppliedRuntimeHistory {
    format_version: u32,
    specs: Vec<RuntimeSpec>,
}

impl AppliedRuntimeHistory {
    fn from_specs(specs: &BTreeMap<String, RuntimeSpec>) -> Self {
        Self {
            format_version: APPLIED_RUNTIME_HISTORY_FORMAT_VERSION,
            specs: specs.values().cloned().collect(),
        }
    }

    fn validate(&self) -> Result<(), String> {
        if self.format_version != APPLIED_RUNTIME_HISTORY_FORMAT_VERSION {
            return Err(format!(
                "unsupported applied runtime history format_version: {}",
                self.format_version
            ));
        }
        validate_unique_runtime_specs(&self.specs, "applied runtime")
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct BootRuntimeSet {
    format_version: u32,
    active_gpu_uuid: String,
    #[serde(default)]
    main_gpu_uuid: String,
    specs: Vec<RuntimeSpec>,
}

impl BootRuntimeSet {
    fn empty() -> Self {
        Self {
            format_version: BOOT_RUNTIME_SET_FORMAT_VERSION,
            active_gpu_uuid: String::new(),
            main_gpu_uuid: String::new(),
            specs: Vec::new(),
        }
    }

    fn from_legacy(spec: RuntimeSpec) -> Self {
        Self {
            format_version: BOOT_RUNTIME_SET_FORMAT_VERSION,
            active_gpu_uuid: spec.gpu.uuid.clone(),
            main_gpu_uuid: String::new(),
            specs: vec![spec],
        }
    }

    fn validate(&self) -> Result<(), String> {
        if self.format_version != BOOT_RUNTIME_SET_FORMAT_VERSION {
            return Err(format!(
                "unsupported boot runtime set format_version: {}",
                self.format_version
            ));
        }
        validate_unique_runtime_specs(&self.specs, "boot runtime")?;
        if !self.specs.is_empty()
            && !self
                .specs
                .iter()
                .any(|spec| spec.gpu.uuid == self.active_gpu_uuid)
        {
            return Err("boot runtime active_gpu_uuid has no saved spec".to_string());
        }
        if !self.main_gpu_uuid.is_empty()
            && !self
                .specs
                .iter()
                .any(|spec| spec.gpu.uuid == self.main_gpu_uuid)
        {
            return Err("boot runtime main_gpu_uuid has no saved spec".to_string());
        }
        if !self.main_gpu_uuid.is_empty() && self.active_gpu_uuid != self.main_gpu_uuid {
            return Err("boot runtime main_gpu_uuid is not active".to_string());
        }
        Ok(())
    }

    fn upsert(&mut self, spec: RuntimeSpec) {
        let uuid = spec.gpu.uuid.clone();
        self.specs.retain(|saved| saved.gpu.uuid != uuid);
        self.specs.push(spec);
        if self.main_gpu_uuid.is_empty() {
            self.active_gpu_uuid = uuid;
        }
    }

    fn set_main_gpu(&mut self, gpu_uuid: &str) -> Result<(), String> {
        let selected_uuid = gpu_uuid.trim();
        if selected_uuid.is_empty() {
            self.main_gpu_uuid.clear();
            return Ok(());
        }
        if !self.specs.iter().any(|spec| spec.gpu.uuid == selected_uuid) {
            return Err("main GPU has no saved boot runtime spec".to_string());
        }
        self.main_gpu_uuid = selected_uuid.to_string();
        self.active_gpu_uuid = selected_uuid.to_string();
        Ok(())
    }

    fn remove(&mut self, gpu_uuid: &str) -> bool {
        let before = self.specs.len();
        self.specs.retain(|spec| spec.gpu.uuid != gpu_uuid);
        let removed = self.specs.len() != before;
        if removed && self.main_gpu_uuid == gpu_uuid {
            self.main_gpu_uuid.clear();
        }
        if removed && self.active_gpu_uuid == gpu_uuid {
            self.active_gpu_uuid = self
                .specs
                .last()
                .map(|spec| spec.gpu.uuid.clone())
                .unwrap_or_default();
        }
        removed
    }

    fn active_spec(&self) -> Option<&RuntimeSpec> {
        self.specs
            .iter()
            .find(|spec| spec.gpu.uuid == self.active_gpu_uuid)
    }
}

/// Concurrent child handle with cached exit status and PID-reuse-safe signals.
pub struct ChildProc {
    child: SharedChild,
}

impl ChildProc {
    pub fn new(child: Child) -> std::io::Result<Self> {
        SharedChild::new(child).map(|child| ChildProc { child })
    }

    pub fn pid(&self) -> u32 {
        self.child.id()
    }

    /// Non-blocking `poll()`: `None` if still running, else the (cached) code.
    pub fn poll(&self) -> Option<i32> {
        self.child.try_wait().ok().flatten().map(exit_code)
    }

    /// Block until the child exits and return its code.
    pub fn wait(&self) -> i32 {
        self.child.wait().map(exit_code).unwrap_or(-1)
    }

    /// Wait up to `timeout`; `None` on timeout, else the exit code.
    pub fn wait_timeout(&self, timeout: Duration) -> Option<i32> {
        self.child
            .wait_timeout(timeout)
            .ok()
            .flatten()
            .map(exit_code)
    }

    /// Send a signal, guarded like `Popen.send_signal` so a reaped (possibly
    /// reused) pid is never signalled. Errors are ignored (parity).
    pub fn signal(&self, sig: i32) {
        let _ = self.child.send_signal(sig);
    }
}

fn exit_code(status: ExitStatus) -> i32 {
    use std::os::unix::process::ExitStatusExt;
    // Python's poll() returns -signum when the child is killed by a signal.
    status
        .code()
        .unwrap_or_else(|| -status.signal().unwrap_or(0))
}

/// Which streaming child occupies the (single) child-job slot. A scan and a
/// profile verification are mutually exclusive: both own the GPU.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChildKind {
    Scan,
    Verify,
}

impl ChildKind {
    /// Write this kind's cooperative stop marker. `abort_final_choice` selects
    /// the scan's abort-vs-offer reason (the verification marker has no reason).
    pub fn write_stop_request(self, abort_final_choice: bool) {
        match self {
            ChildKind::Scan => paths::write_auto_uv_stop_request(abort_final_choice),
            ChildKind::Verify => paths::write_profile_verify_stop_request(),
        }
    }

    /// Clear this kind's stale stop marker (`NotFound` is success).
    fn clear_stop_request(self) -> std::io::Result<()> {
        match self {
            ChildKind::Scan => paths::clear_auto_uv_stop_request(),
            ChildKind::Verify => paths::clear_profile_verify_stop_request(),
        }
    }
}

/// The active streaming child (Auto-UV scan or profile verification).
/// `generation` is the identity guard: a stale monitor only clears/restarts if
/// the supervisor still holds this exact job.
pub struct ChildJob {
    pub proc: ChildProc,
    pub argv: Vec<String>,
    pub generation: u64,
    pub kind: ChildKind,
}

struct ProfileJob {
    engine: EngineHandle,
    spec: RuntimeSpec,
}

struct GameWatch {
    app_id: String,
    lifetime_fd: Option<OwnedFd>,
    process_start_time: Option<u64>,
}

#[derive(Default)]
struct GameRuntimeState {
    watches: BTreeMap<u32, GameWatch>,
    standing_spec: Option<RuntimeSpec>,
    restore_spec: Option<RuntimeSpec>,
    override_active: bool,
}

pub struct Supervisor {
    profile: Option<ProfileJob>,
    child: Option<Arc<ChildJob>>,
    child_generation: u64,
    stop_timeout: Duration,
    game_runtime: GameRuntimeState,
    game_watch_wake: UnixStream,
    game_watch_receiver: Option<UnixStream>,
    last_applied_specs: BTreeMap<String, RuntimeSpec>,
    boot_replay: Vec<Value>,
}

impl Default for Supervisor {
    fn default() -> Self {
        Self::new()
    }
}

impl Supervisor {
    pub fn new() -> Self {
        let (receiver, wake) = UnixStream::pair().expect("game watch wake socket");
        receiver
            .set_nonblocking(true)
            .expect("nonblocking game watch receiver");
        wake.set_nonblocking(true)
            .expect("nonblocking game watch wakeup");
        Supervisor {
            profile: None,
            child: None,
            child_generation: 0,
            stop_timeout: ENGINE_STOP_TIMEOUT,
            game_runtime: GameRuntimeState::default(),
            game_watch_wake: wake,
            game_watch_receiver: Some(receiver),
            last_applied_specs: BTreeMap::new(),
            boot_replay: Vec::new(),
        }
    }

    /// Test-only constructor with a short engine-stop timeout so the
    /// wedged-engine (`StopOutcome::TimedOut`) paths run in milliseconds.
    #[cfg(test)]
    fn with_stop_timeout(stop_timeout: Duration) -> Self {
        Supervisor {
            stop_timeout,
            ..Self::new()
        }
    }

    fn child_running_kind(&self) -> Option<ChildKind> {
        self.child
            .as_ref()
            .filter(|job| job.proc.poll().is_none())
            .map(|job| job.kind)
    }

    fn next_child_generation(&mut self) -> u64 {
        self.child_generation += 1;
        self.child_generation
    }

    /// Stop the engine so the child can own the GPU. Mirrors
    /// `_stop_autostart_runtime_for_scan`.
    ///
    /// A `TimedOut` stop means the engine thread is wedged in a blocking GPU
    /// call and could revive at any moment, writing fans/mem/VF/locked clocks
    /// over whatever owns the GPU next — so the caller must NOT proceed. The
    /// wedged job is retained (stop flag already set) so status stays truthful
    /// and a later attempt re-checks it.
    ///
    /// An engine that has ALREADY exited (a clean stop, an error exit, or a
    /// previously-wedged thread that finally returned and honored the stop flag)
    /// is dead: free the slot so it cannot block the post-child autostart
    /// restart (`start_autostart_if_configured` early-returns while
    /// `profile.is_some()`).
    fn stop_engine_for_child(&mut self, what: &str) -> Result<(), String> {
        if let Some(job) = self.profile.as_mut() {
            if job.engine.is_running() {
                match job.engine.stop(self.stop_timeout) {
                    StopOutcome::Stopped => self.profile = None,
                    StopOutcome::TimedOut => {
                        let message = format!(
                            "runtime profile engine did not stop (wedged GPU call?); refusing to start {what}"
                        );
                        logging::error(&message);
                        return Err(message);
                    }
                }
            } else {
                self.profile = None;
            }
        }
        Ok(())
    }
}

/// The refusal error for starting `requested` while `running` occupies the
/// child slot. The scan-vs-scan text is byte-exact with the Python daemon; the
/// verification texts are new (the method is milestone-B additive).
fn child_refusal(running: ChildKind, requested: ChildKind) -> String {
    match (running, requested) {
        (ChildKind::Scan, ChildKind::Scan) => "Auto-UV scan is already running",
        (ChildKind::Verify, ChildKind::Verify) => "profile verification is already running",
        (ChildKind::Scan, ChildKind::Verify) => {
            "cannot start profile verification while Auto-UV scan is running"
        }
        (ChildKind::Verify, ChildKind::Scan) => {
            "cannot start an Auto-UV scan while profile verification is running"
        }
    }
    .to_string()
}

fn guard(sup: &Mutex<Supervisor>) -> MutexGuard<'_, Supervisor> {
    sup.lock().unwrap_or_else(|poison| poison.into_inner())
}

fn test_timings_enabled() -> bool {
    env::var_os("PENGUIN_BURNERD_TEST_TIMINGS").is_some()
}

fn game_watch_interval() -> Duration {
    if test_timings_enabled() {
        TEST_GAME_WATCH_INTERVAL
    } else {
        GAME_WATCH_INTERVAL
    }
}

fn process_start_time(pid: u32) -> Option<u64> {
    let pid = i32::try_from(pid).ok()?;
    procfs::process::Process::new(pid)
        .ok()?
        .stat()
        .ok()
        .map(|stat| stat.starttime)
}

impl GameWatch {
    fn open(pid: u32, app_id: String) -> Result<Self, String> {
        // SAFETY: pidfd_open has no pointer arguments; `pid` and flags are
        // validated scalar values, and a nonnegative result is a new fd.
        let raw_fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid as libc::pid_t, 0) };
        let process_fd = if raw_fd >= 0 {
            // SAFETY: pidfd_open returned a new owned descriptor on success.
            Some(unsafe { OwnedFd::from_raw_fd(raw_fd as i32) })
        } else {
            None
        };
        let lifetime_fd = crate::sessions::watch_lifetime(pid)?.or(process_fd);
        let process_start_time = if lifetime_fd.is_none() {
            process_start_time(pid)
        } else {
            None
        };
        if lifetime_fd.is_none() && process_start_time.is_none() {
            return Err(format!("watch_pid {pid} is not a running process"));
        }
        Ok(Self {
            app_id,
            lifetime_fd,
            process_start_time,
        })
    }

    fn running(&self, pid: u32) -> bool {
        if let Some(pidfd) = &self.lifetime_fd {
            let mut pollfd = libc::pollfd {
                fd: pidfd.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            };
            // SAFETY: `pollfd` points to one initialized entry for the full
            // duration of this nonblocking call.
            let result = unsafe { libc::poll(&mut pollfd, 1, 0) };
            return result == 0
                || (result < 0
                    && std::io::Error::last_os_error().raw_os_error() == Some(libc::EINTR));
        }
        process_start_time(pid) == self.process_start_time
    }
}

impl Supervisor {
    fn game_runtime_status(&self) -> Option<GameRuntimeStatus> {
        if self.game_runtime.watches.is_empty() {
            return None;
        }
        Some(GameRuntimeStatus {
            active: self.game_runtime.override_active,
            watched: self
                .game_runtime
                .watches
                .iter()
                .map(|(&pid, watch)| GameWatchStatus {
                    pid,
                    app_id: watch.app_id.clone(),
                })
                .collect(),
            standing_profile_id: self
                .game_runtime
                .standing_spec
                .as_ref()
                .map(RuntimeSpec::active_profile_id),
            standing_runtime_mode: self
                .game_runtime
                .standing_spec
                .as_ref()
                .map(|spec| spec.mode_name().to_string()),
        })
    }
}

/// Start the process-lifetime monitor used by Steam per-game profiles. A pidfd
/// guards against PID reuse where the kernel supports it; the `/proc` start time
/// is the fallback on older kernels.
pub fn start_game_watch_monitor(sup: &Arc<Mutex<Supervisor>>) {
    let Some(mut wake) = guard(sup).game_watch_receiver.take() else {
        return;
    };
    let weak = Arc::downgrade(sup);
    thread::Builder::new()
        .name("penguin-burner-game-watch".to_string())
        .spawn(move || loop {
            let (handles, timeout) = {
                let Some(sup) = weak.upgrade() else {
                    return;
                };
                let supervisor = guard(&sup);
                let mut handles = Vec::new();
                let mut timeout: Option<Duration> = None;
                for watch in supervisor.game_runtime.watches.values() {
                    if let Some(fd) = &watch.lifetime_fd {
                        match fd.try_clone() {
                            Ok(fd) => handles.push(fd),
                            Err(_) => {
                                let fallback = game_watch_interval();
                                timeout = Some(timeout.map_or(fallback, |old| old.min(fallback)));
                            }
                        }
                    } else {
                        // Older kernels lack pidfd: retain identity-checked
                        // reconciliation there, not on the normal event path.
                        let fallback = game_watch_interval();
                        timeout = Some(timeout.map_or(fallback, |old| old.min(fallback)));
                    }
                }
                (handles, timeout)
            };
            let mut fds = vec![libc::pollfd {
                fd: wake.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            }];
            fds.extend(handles.iter().map(|fd| libc::pollfd {
                fd: fd.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            }));
            let timeout_ms =
                timeout.map_or(-1, |d| d.as_millis().clamp(1, i32::MAX as u128) as i32);
            // SAFETY: all descriptors are owned above and fds remains valid.
            let result =
                unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as libc::nfds_t, timeout_ms) };
            if result < 0 {
                if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
                    continue;
                }
                logging::error("game-watch event wait failed");
                return;
            }
            if fds[0].revents & libc::POLLHUP != 0 {
                return;
            }
            let mut bytes = [0; 256];
            while wake.read(&mut bytes).is_ok_and(|n| n > 0) {}
            let Some(sup) = weak.upgrade() else {
                return;
            };
            reap_game_watches(&sup);
        })
        .expect("spawn game watch thread");
}

fn apply_inactive_spec_and_stop(
    supervisor: &mut Supervisor,
    spec: RuntimeSpec,
    context: &str,
) -> Result<(), String> {
    match profile::start(spec.clone()) {
        Ok(mut engine) => {
            if engine.stop(supervisor.stop_timeout) == StopOutcome::TimedOut {
                supervisor.profile = Some(ProfileJob { engine, spec });
                return Err(format!("{context} engine did not stop within timeout"));
            }
            supervisor
                .last_applied_specs
                .insert(spec.gpu.uuid.clone(), spec);
            Ok(())
        }
        Err(error) => {
            let (message, failed_engine) = error.into_parts();
            if let Some(engine) = failed_engine {
                supervisor.profile = Some(ProfileJob { engine, spec });
            }
            Err(format!("failed to {context}: {message}"))
        }
    }
}

fn reap_game_watches(sup: &Mutex<Supervisor>) {
    if let Err(error) = restore_finished_game(&mut guard(sup)) {
        logging::error(&error);
    }
}

fn restore_finished_game(supervisor: &mut Supervisor) -> Result<(), String> {
    if supervisor.game_runtime.watches.is_empty() {
        return Ok(());
    }
    supervisor.game_runtime.watches.retain(|&pid, watch| {
        let running = watch.running(pid);
        if !running {
            // Fires exactly once per pid: archive the game's telemetry ring.
            crate::frame_history::session_end(pid);
        }
        running
    });
    if !supervisor.game_runtime.watches.is_empty() {
        return Ok(());
    }

    let should_restore = supervisor.game_runtime.override_active;
    supervisor.game_runtime.override_active = false;
    let standing_spec = supervisor.game_runtime.standing_spec.take();
    let target_restore_spec = supervisor.game_runtime.restore_spec.take();
    if !should_restore || supervisor.child_running_kind().is_some() {
        return Ok(());
    }
    let game_spec = supervisor.profile.as_ref().map(|job| job.spec.clone());
    supervisor.stop_engine_for_child("the standing runtime after game exit")?;

    let restore_target =
        target_restore_spec.or_else(|| game_spec.as_ref().map(RuntimeSpec::stock_fallback));
    let standing_gpu_uuid = standing_spec
        .as_ref()
        .map(|spec| spec.gpu.uuid.as_str())
        .unwrap_or_default();
    if let Some(target_spec) = restore_target.filter(|spec| spec.gpu.uuid != standing_gpu_uuid) {
        apply_inactive_spec_and_stop(
            supervisor,
            target_spec,
            "restore game target GPU after game exit",
        )?;
    }

    let Some(spec) = standing_spec else {
        return Ok(());
    };
    match profile::start(spec.clone()) {
        Ok(engine) => {
            supervisor.profile = Some(ProfileJob { engine, spec });
        }
        Err(error) => {
            let (message, failed_engine) = error.into_parts();
            if let Some(engine) = failed_engine {
                supervisor.profile = Some(ProfileJob { engine, spec });
            }
            return Err(format!(
                "failed to restore standing runtime after game exit: {message}"
            ));
        }
    }
    Ok(())
}

/// `PENGUIN_BURNER_DAEMON_PROGRAM_FILE` resolved absolute, else this binary's path.
pub fn daemon_program_file() -> String {
    if let Ok(value) = env::var(PROGRAM_FILE_ENV) {
        let value = value.trim();
        if !value.is_empty() {
            return fs::canonicalize(value)
                .map(|path| path.display().to_string())
                .unwrap_or_else(|_| value.to_string());
        }
    }
    env::current_exe()
        .map(|path| path.display().to_string())
        .unwrap_or_default()
}

fn active_state_file_path() -> PathBuf {
    if let Some(path) = env::var_os(STATE_FILE_ENV) {
        if !path.is_empty() {
            return PathBuf::from(path);
        }
    }
    PathBuf::from(ACTIVE_RUNTIME_STATE_PATH)
}

fn applied_runtime_history_path() -> PathBuf {
    if env::var_os(STATE_FILE_ENV).is_some() {
        let mut path = active_state_file_path();
        path.set_extension("history.json");
        return path;
    }
    PathBuf::from(APPLIED_RUNTIME_HISTORY_PATH)
}

fn boot_state_file_path() -> PathBuf {
    if let Some(path) = env::var_os(BOOT_STATE_FILE_ENV) {
        if !path.is_empty() {
            return PathBuf::from(path);
        }
    }
    PathBuf::from(BOOT_RUNTIME_STATE_PATH)
}

fn persist_json(path: &PathBuf, value: &impl Serialize) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("runtime state path has no parent: {}", path.display()))?;
    fs::create_dir_all(parent).map_err(|error| {
        format!(
            "failed to create runtime state directory {}: {error}",
            parent.display()
        )
    })?;
    let mut temp = NamedTempFile::new_in(parent).map_err(|error| {
        format!(
            "failed to create temporary runtime state in {}: {error}",
            parent.display()
        )
    })?;
    serde_json::to_writer(&mut temp, value)
        .map_err(|error| format!("failed to serialize runtime state: {error}"))?;
    temp.write_all(b"\n")
        .map_err(|error| format!("failed to write runtime state: {error}"))?;
    temp.as_file()
        .sync_all()
        .map_err(|error| format!("failed to sync runtime state: {error}"))?;
    temp.persist(path).map_err(|error| {
        format!(
            "failed to replace runtime state {}: {}",
            path.display(),
            error.error
        )
    })?;
    Ok(())
}

fn persist_runtime_spec(path: &PathBuf, spec: &RuntimeSpec) -> Result<(), String> {
    persist_json(path, spec)
}

fn load_runtime_spec(path: &PathBuf) -> Result<Option<RuntimeSpec>, String> {
    let text = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(format!(
                "failed to read runtime state {}: {error}",
                path.display()
            ))
        }
    };
    let spec: RuntimeSpec = serde_json::from_str(&text)
        .map_err(|error| format!("invalid runtime state {}: {error}", path.display()))?;
    spec.validate()
        .map_err(|error| format!("invalid runtime state {}: {error}", path.display()))?;
    Ok(Some(spec))
}

fn load_applied_runtime_history(path: &PathBuf) -> Result<Option<AppliedRuntimeHistory>, String> {
    let text = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(format!(
                "failed to read applied runtime history {}: {error}",
                path.display()
            ))
        }
    };
    let history: AppliedRuntimeHistory = serde_json::from_str(&text).map_err(|error| {
        format!(
            "invalid applied runtime history {}: {error}",
            path.display()
        )
    })?;
    history.validate().map_err(|error| {
        format!(
            "invalid applied runtime history {}: {error}",
            path.display()
        )
    })?;
    Ok(Some(history))
}

fn load_boot_runtime_set(path: &PathBuf) -> Result<BootRuntimeSet, String> {
    let text = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(BootRuntimeSet::empty())
        }
        Err(error) => {
            return Err(format!(
                "failed to read runtime state {}: {error}",
                path.display()
            ))
        }
    };
    let value: Value = serde_json::from_str(&text)
        .map_err(|error| format!("invalid runtime state {}: {error}", path.display()))?;
    let set = if value.get("specs").is_some() {
        serde_json::from_value::<BootRuntimeSet>(value)
            .map_err(|error| format!("invalid runtime state {}: {error}", path.display()))?
    } else {
        let spec = serde_json::from_value::<RuntimeSpec>(value)
            .map_err(|error| format!("invalid runtime state {}: {error}", path.display()))?;
        BootRuntimeSet::from_legacy(spec)
    };
    set.validate()
        .map_err(|error| format!("invalid runtime state {}: {error}", path.display()))?;
    Ok(set)
}

/// PCI bus ids from persisted runtime intent. The current session keeps
/// precedence. Fresh-boot candidates then put the selected non-stock GPU
/// first, followed by other non-stock GPUs newest first; stock entries are
/// only fallbacks. RTD3 uses the first address still present in sysfs without
/// waking NVML, so a selected stock card cannot hide pending work on another
/// card.
pub fn persisted_pci_bus_id_hints() -> Vec<String> {
    let mut hints = Vec::new();
    let mut push = |spec: &RuntimeSpec| {
        let pci_bus_id = spec.gpu.pci_bus_id.trim();
        if !pci_bus_id.is_empty() && !hints.iter().any(|hint| hint == pci_bus_id) {
            hints.push(pci_bus_id.to_string());
        }
    };
    if let Ok(Some(spec)) = load_runtime_spec(&active_state_file_path()) {
        push(&spec);
    }
    if let Ok(set) = load_boot_runtime_set(&boot_state_file_path()) {
        let selected = set.active_spec();
        if let Some(spec) = selected.filter(|spec| spec.mode_name() != "stock") {
            push(spec);
        }
        for spec in set
            .specs
            .iter()
            .rev()
            .filter(|spec| spec.gpu.uuid != set.active_gpu_uuid && spec.mode_name() != "stock")
        {
            push(spec);
        }
        if let Some(spec) = selected.filter(|spec| spec.mode_name() == "stock") {
            push(spec);
        }
        for spec in set
            .specs
            .iter()
            .rev()
            .filter(|spec| spec.gpu.uuid != set.active_gpu_uuid && spec.mode_name() == "stock")
        {
            push(spec);
        }
    }
    hints
}

/// Live RTD3 target candidates. An occupied engine slot wins over disk state
/// so applying a profile to another GPU rebinds the watcher on its next tick.
pub fn runtime_pci_bus_id_hints(sup: &Mutex<Supervisor>) -> Vec<String> {
    let profile_hint = guard(sup)
        .profile
        .as_ref()
        .map(|job| job.spec.gpu.pci_bus_id.trim().to_string())
        .filter(|hint| !hint.is_empty());
    let mut hints = persisted_pci_bus_id_hints();
    if let Some(profile_hint) = profile_hint {
        hints.retain(|hint| hint != &profile_hint);
        hints.insert(0, profile_hint);
    }
    hints
}

/// Set once this session's boot intent has been acted on (an autostart ran,
/// or the user explicitly stopped the runtime). The boot set must apply at
/// most once per daemon lifetime — the deep-sleep watcher must not resurrect
/// runtimes the user stopped just because the boot file still exists.
static BOOT_RUNTIME_CONSUMED: AtomicBool = AtomicBool::new(false);

#[cfg(test)]
fn deferred_pending_from(active_exists: bool, boot_exists: bool, boot_consumed: bool) -> bool {
    active_exists || (boot_exists && !boot_consumed)
}

fn spec_matches_pci(spec: &RuntimeSpec, target_pci_addr: &str) -> bool {
    crate::rtd3::detect::sysfs_pci_addr(&spec.gpu.pci_bus_id).as_deref()
        == crate::rtd3::detect::sysfs_pci_addr(target_pci_addr).as_deref()
}

fn boot_spec_for_target<'a>(
    set: &'a BootRuntimeSet,
    target_pci_addr: &str,
    legacy_target_unambiguous: bool,
) -> Option<&'a RuntimeSpec> {
    set.specs
        .iter()
        .find(|spec| spec_matches_pci(spec, target_pci_addr))
        .or_else(|| {
            if !legacy_target_unambiguous {
                return None;
            }
            let mut legacy = set
                .specs
                .iter()
                .filter(|spec| spec.gpu.pci_bus_id.trim().is_empty());
            let spec = legacy.next()?;
            legacy.next().is_none().then_some(spec)
        })
}

/// True when the persisted spec for the RTD3 target contains work worth
/// replaying after a real wake. PCI target selection prefers a pending
/// non-stock boot entry, so a selected stock GPU cannot suppress another
/// card's saved settings and a missing non-stock entry cannot make an
/// unrelated stock card wakeable.
pub fn deferred_runtime_wakeable(
    target_pci_addr: Option<&str>,
    legacy_target_unambiguous: bool,
) -> bool {
    if let Ok(Some(spec)) = load_runtime_spec(&active_state_file_path()) {
        if target_pci_addr.is_none_or(|target| {
            (legacy_target_unambiguous && spec.gpu.pci_bus_id.trim().is_empty())
                || spec_matches_pci(&spec, target)
        }) {
            return spec.mode_name() != "stock";
        }
    }
    if !BOOT_RUNTIME_CONSUMED.load(Ordering::SeqCst) {
        if let Ok(set) = load_boot_runtime_set(&boot_state_file_path()) {
            if let Some(target) = target_pci_addr {
                return boot_spec_for_target(&set, target, legacy_target_unambiguous)
                    .is_some_and(|spec| spec.mode_name() != "stock");
            }
            return set.specs.iter().any(|spec| spec.mode_name() != "stock");
        }
    }
    false
}

/// Deep-sleep park: stop the running engine while deliberately keeping the
/// persisted runtime state and boot intent. Static state on inactive GPUs is
/// not monitored by this single active engine and is unaffected by parking.
pub fn park_runtime_for_deep_sleep(sup: &Mutex<Supervisor>) -> bool {
    let mut supervisor = guard(sup);
    if !supervisor.game_runtime.watches.is_empty() {
        return false;
    }
    let stop_timeout = supervisor.stop_timeout;
    match supervisor.profile.as_mut() {
        Some(job) if job.engine.is_running() => match job.engine.stop(stop_timeout) {
            StopOutcome::Stopped => {
                supervisor.profile = None;
                true
            }
            StopOutcome::TimedOut => {
                logging::error(
                    "deep sleep park: engine did not stop within timeout; staying attached",
                );
                false
            }
        },
        _ => false,
    }
}

/// True while the profile slot is occupied at all — running, or retained
/// after a failure/wedge (which deliberately blocks further starts).
pub fn profile_slot_occupied(sup: &Mutex<Supervisor>) -> bool {
    guard(sup).profile.is_some()
}

/// GPU index of the runtime in the profile slot, for the deep-sleep park
/// policy's client check.
pub fn profile_gpu_index(sup: &Mutex<Supervisor>) -> Option<u32> {
    guard(sup)
        .profile
        .as_ref()
        .map(|job| job.spec.gpu.index_at_resolution)
}

/// Current GPU index for the same persisted PCI target the deep-sleep watcher
/// is observing. Keeping target selection and client detection coherent is
/// essential when the selected boot GPU is stock and another GPU has work.
pub fn deferred_runtime_gpu_index(
    target_pci_addr: Option<&str>,
    legacy_target_unambiguous: bool,
) -> Option<u32> {
    if let Ok(Some(spec)) = load_runtime_spec(&active_state_file_path()) {
        if target_pci_addr.is_none_or(|target| {
            (legacy_target_unambiguous && spec.gpu.pci_bus_id.trim().is_empty())
                || spec_matches_pci(&spec, target)
        }) {
            return crate::gpu::NvmlBackend::resolve_gpu_index(
                &spec.gpu.uuid,
                spec.gpu.index_at_resolution,
            )
            .ok();
        }
    }
    if !BOOT_RUNTIME_CONSUMED.load(Ordering::SeqCst) {
        if let Ok(set) = load_boot_runtime_set(&boot_state_file_path()) {
            if let Some(target) = target_pci_addr {
                let spec = boot_spec_for_target(&set, target, legacy_target_unambiguous)?;
                return crate::gpu::NvmlBackend::resolve_gpu_index(
                    &spec.gpu.uuid,
                    spec.gpu.index_at_resolution,
                )
                .ok();
            }
            if let Some(index) = set.active_spec().and_then(|spec| {
                crate::gpu::NvmlBackend::resolve_gpu_index(
                    &spec.gpu.uuid,
                    spec.gpu.index_at_resolution,
                )
                .ok()
            }) {
                return Some(index);
            }
            return set.specs.iter().rev().find_map(|spec| {
                crate::gpu::NvmlBackend::resolve_gpu_index(
                    &spec.gpu.uuid,
                    spec.gpu.index_at_resolution,
                )
                .ok()
            });
        }
    }
    None
}

/// Post-child runtime recovery: Desktop mode restarts immediately; Mobile and
/// Unknown leave the start to the watcher so a scan exit cannot force-attach a
/// GPU that is about to idle.
pub fn resume_autostart_after_child(sup: &Arc<Mutex<Supervisor>>) {
    if !crate::rtd3::allows_desktop_autostart() {
        logging::info("deep sleep: leaving post-child runtime start to the watcher");
        return;
    }
    start_autostart_if_configured(sup);
}

fn persist_active_runtime(spec: &RuntimeSpec) {
    if let Err(error) = persist_runtime_spec(&active_state_file_path(), spec) {
        logging::error(&error);
    }
}

fn persist_applied_runtime_history(specs: &BTreeMap<String, RuntimeSpec>) {
    let history = AppliedRuntimeHistory::from_specs(specs);
    if let Err(error) = persist_json(&applied_runtime_history_path(), &history) {
        logging::error(&error);
    }
}

fn clear_active_runtime() {
    let path = active_state_file_path();
    if let Err(error) = fs::remove_file(&path) {
        if error.kind() != std::io::ErrorKind::NotFound {
            logging::error(&format!(
                "failed to clear active runtime state {}: {error}",
                path.display()
            ));
        }
    }
}

fn energy_savings_status() -> Option<EnergySavingsStatus> {
    let totals = profile::savings::load_freshest_totals(
        &profile::savings::live_savings_state_path(),
        &profile::savings::savings_state_path(),
    );
    if totals.active_seconds <= 0.0 {
        return None;
    }
    Some(EnergySavingsStatus {
        active_seconds: totals.active_seconds,
        saved_watt_seconds: totals.saved_watt_seconds,
    })
}

pub fn status(sup: &Mutex<Supervisor>) -> StatusResult {
    let supervisor = guard(sup);
    let game_runtime = supervisor.game_runtime_status();
    let energy_savings = energy_savings_status();
    let deep_sleep = crate::rtd3::status(crate::rtd3::DaemonGpuHolds {
        engine_attached: supervisor
            .profile
            .as_ref()
            .is_some_and(|job| job.engine.returncode().is_none()),
        rpc_backends: crate::gpu_rpc::open_backend_count(),
    });

    if let Some(job) = &supervisor.child {
        let returncode = job.proc.poll();
        let (running_state, stopped_state, job_type) = match job.kind {
            ChildKind::Scan => (
                "auto_uv_scan_running",
                "auto_uv_scan_stopped",
                "auto_uv_scan",
            ),
            ChildKind::Verify => (
                "profile_verification_running",
                "profile_verification_stopped",
                "profile_verification",
            ),
        };
        let state = if returncode.is_none() {
            running_state
        } else {
            stopped_state
        };
        return StatusResult::new(
            state,
            Some(ActiveJob {
                job_type,
                argv: job.argv.clone(),
                profile_id: None,
                runtime_mode: None,
                gpu_uuid: None,
                silent_fan_curve: None,
                pid: job.proc.pid(),
                returncode,
            }),
        )
        .with_game_runtime(game_runtime)
        .with_energy_savings(energy_savings)
        .with_deep_sleep(deep_sleep);
    }

    if let Some(job) = &supervisor.profile {
        let returncode = job.engine.returncode();
        let state = if returncode.is_none() {
            "runtime_profile_running"
        } else {
            "runtime_profile_stopped"
        };
        return StatusResult::new(
            state,
            Some(ActiveJob {
                job_type: "runtime_profile",
                argv: Vec::new(),
                profile_id: Some(job.spec.active_profile_id()),
                runtime_mode: Some(job.spec.mode_name().to_string()),
                gpu_uuid: Some(job.spec.gpu.uuid.clone()),
                silent_fan_curve: Some(job.spec.fan.enabled),
                // The engine is in-process, so the "job pid" is the daemon's pid.
                pid: std::process::id(),
                returncode,
            }),
        )
        .with_game_runtime(game_runtime)
        .with_energy_savings(energy_savings)
        .with_deep_sleep(deep_sleep);
    }

    StatusResult::new("idle", None)
        .with_game_runtime(game_runtime)
        .with_energy_savings(energy_savings)
        .with_deep_sleep(deep_sleep)
}

pub fn stop_auto_uv_scan(sup: &Mutex<Supervisor>) -> StopResult {
    stop_child(sup, ChildKind::Scan)
}

pub fn stop_profile_verification(sup: &Mutex<Supervisor>) -> StopResult {
    stop_child(sup, ChildKind::Verify)
}

pub fn active_child_kind(sup: &Mutex<Supervisor>) -> Option<ChildKind> {
    guard(sup).child_running_kind()
}

/// True while an in-process profile engine is actively applying GPU state.
/// During a scan/verification the engine is stopped, so this is false and the
/// streaming child's raw GPU writes are the intended, unarbitrated caller.
pub fn profile_engine_running(sup: &Mutex<Supervisor>) -> bool {
    guard(sup)
        .profile
        .as_ref()
        .is_some_and(|job| job.engine.returncode().is_none())
}

/// Cooperative stop: write the kind's stop-request marker FIRST, then SIGINT
/// (ordered protocol, parity with the Python daemon's scan stop).
fn stop_child(sup: &Mutex<Supervisor>, kind: ChildKind) -> StopResult {
    let supervisor = guard(sup);
    match &supervisor.child {
        Some(job) if job.kind == kind && job.proc.poll().is_none() => {
            let pid = job.proc.pid();
            kind.write_stop_request(false);
            job.proc.signal(libc::SIGINT);
            StopResult::stopped(pid)
        }
        _ => StopResult::idle(),
    }
}

/// Apply a resolved per-game RuntimeSpec without replacing the persisted
/// standing action. The watch is registered only after the GPU transition
/// succeeds; the monitor restores the original standing spec after the last
/// watched launcher/game process exits.
pub fn start_game_runtime_profile(
    sup: &Mutex<Supervisor>,
    mut spec: RuntimeSpec,
    watch_pid: u32,
    app_id: String,
) -> Result<Value, String> {
    spec.validate()?;
    let watch = GameWatch::open(watch_pid, app_id)?;
    let profile_id = spec.active_profile_id();
    let runtime_mode = spec.mode_name().to_string();
    let gpu_uuid = spec.gpu.uuid.clone();

    let mut supervisor = guard(sup);
    // A new launch can beat the monitor thread to the previous exit event.
    // Reconcile under the same lock before deciding which live game owns GPU.
    restore_finished_game(&mut supervisor)?;
    match supervisor.child_running_kind() {
        Some(ChildKind::Scan) => {
            return Err(
                "cannot start a game runtime profile while Auto-UV scan is running".to_string(),
            );
        }
        Some(ChildKind::Verify) => {
            return Err(
                "cannot start a game runtime profile while profile verification is running"
                    .to_string(),
            );
        }
        None => {}
    }

    // One GPU and one overlay telemetry stream can only have one game owner.
    // Keep the first live Steam session authoritative; a later game launch must
    // not replace its profile or make its FPS drive the adaptive controller.
    if !supervisor.game_runtime.watches.is_empty()
        && !supervisor.game_runtime.watches.contains_key(&watch_pid)
    {
        return Ok(serde_json::json!({
            "started": false,
            "ignored": true,
            "reason": "first-game-runtime-active",
            "watching_pid": watch_pid,
            "profile_id": profile_id,
            "runtime_mode": runtime_mode,
            "gpu_uuid": gpu_uuid,
        }));
    }

    let first_watch = supervisor.game_runtime.watches.is_empty();
    if !first_watch {
        let current_gpu_uuid = supervisor
            .profile
            .as_ref()
            .map(|job| job.spec.gpu.uuid.as_str())
            .unwrap_or_default();
        if current_gpu_uuid != spec.gpu.uuid {
            return Err(
                "cannot change a running game's target GPU; restart the game first".to_string(),
            );
        }
    }
    let standing_spec = if first_watch {
        supervisor
            .profile
            .as_ref()
            .filter(|job| job.engine.is_running())
            .map(|job| job.spec.clone())
    } else {
        None
    };
    let previous_spec = supervisor.profile.as_ref().map(|job| job.spec.clone());
    let restore_spec = if first_watch {
        standing_spec
            .as_ref()
            .filter(|standing| standing.gpu.uuid == spec.gpu.uuid)
            .cloned()
            .or_else(|| supervisor.last_applied_specs.get(&spec.gpu.uuid).cloned())
    } else {
        None
    };
    // Game presets select V/F policy, not a new fan policy. Their default
    // disabled fan payload must not drop the user's active silent curve.
    // Resolve this under the supervisor lock, including hot target updates,
    // and only inherit state belonging to the game's target GPU. Explicit
    // Stock remains a request for factory control, including automatic fans.
    if spec.mode_name() != "stock" && !spec.fan.enabled {
        let current = previous_spec
            .as_ref()
            .filter(|current| current.gpu.uuid == spec.gpu.uuid);
        // A temporary Stock game mode has automatic fans. When leaving it,
        // recover the target GPU's standing fan policy, not Stock's override.
        let fan_source = if supervisor.game_runtime.override_active
            && current.is_some_and(|current| current.mode_name() == "stock")
        {
            supervisor
                .game_runtime
                .standing_spec
                .as_ref()
                .filter(|standing| standing.gpu.uuid == spec.gpu.uuid)
                .or(supervisor.game_runtime.restore_spec.as_ref())
        } else {
            current.or(restore_spec.as_ref())
        };
        if let Some(source) = fan_source {
            spec.fan = source.fan.clone();
        }
    }
    let failed_target_restore_spec = previous_spec
        .as_ref()
        .filter(|previous| previous.gpu.uuid != spec.gpu.uuid)
        .map(|_| {
            restore_spec
                .clone()
                .unwrap_or_else(|| spec.stock_fallback())
        });
    supervisor.stop_engine_for_child("a game runtime profile")?;

    match profile::start(spec.clone()) {
        Ok(engine) => {
            supervisor.profile = Some(ProfileJob {
                engine,
                spec: spec.clone(),
            });
            if first_watch {
                supervisor.game_runtime.standing_spec = standing_spec;
                supervisor.game_runtime.restore_spec = restore_spec;
            }
            supervisor.game_runtime.watches.insert(watch_pid, watch);
            if spec.game_telemetry {
                if let Some(watch) = supervisor.game_runtime.watches.get(&watch_pid) {
                    crate::frame_history::session_start(watch_pid, &watch.app_id);
                }
            }
            let _ = supervisor.game_watch_wake.write(&[1]);
            supervisor.game_runtime.override_active = true;
            Ok(serde_json::json!({
                "started": true,
                "pid": std::process::id(),
                "watching_pid": watch_pid,
                "profile_id": profile_id,
                "runtime_mode": runtime_mode,
                "gpu_uuid": gpu_uuid,
            }))
        }
        Err(error) => {
            let (apply_error, failed_engine) = error.into_parts();
            if let Some(engine) = failed_engine {
                supervisor.profile = Some(ProfileJob {
                    engine,
                    spec: spec.clone(),
                });
                return Err(format!(
                    "failed to apply game runtime spec: {apply_error}; refusing rollback while the failed engine may still be running"
                ));
            }

            let mut recovery = "no fallback runtime could be started".to_string();
            let mut target_recovery = String::new();
            if let Some(target_spec) = failed_target_restore_spec {
                match apply_inactive_spec_and_stop(
                    &mut supervisor,
                    target_spec,
                    "restore the failed game target GPU",
                ) {
                    Ok(()) => {
                        target_recovery = "failed game target GPU restored".to_string();
                    }
                    Err(error) => {
                        logging::error(&error);
                        target_recovery = error;
                        if supervisor.profile.is_some() {
                            return Err(format!(
                                "failed to apply game runtime spec: {apply_error}; {target_recovery}; refusing to restart the previous engine while a failed restore engine may still be running"
                            ));
                        }
                    }
                }
            }
            if let Some(previous) = previous_spec {
                match profile::start(previous.clone()) {
                    Ok(engine) => {
                        supervisor.profile = Some(ProfileJob {
                            engine,
                            spec: previous,
                        });
                        recovery = "previous runtime restored".to_string();
                    }
                    Err(restore_error) => {
                        let (message, failed_engine) = restore_error.into_parts();
                        logging::error(&format!(
                            "failed to restore previous runtime after game apply failure: {message}"
                        ));
                        if let Some(engine) = failed_engine {
                            supervisor.profile = Some(ProfileJob {
                                engine,
                                spec: previous,
                            });
                            recovery = "previous runtime restore is still stopping".to_string();
                        }
                    }
                }
            } else {
                let stock = spec.stock_fallback();
                match profile::start(stock.clone()) {
                    Ok(engine) => {
                        supervisor.profile = Some(ProfileJob {
                            engine,
                            spec: stock,
                        });
                        recovery = "stock fallback applied".to_string();
                    }
                    Err(stock_error) => {
                        let (message, failed_engine) = stock_error.into_parts();
                        logging::error(&format!(
                            "failed to apply stock fallback after game runtime failure: {message}"
                        ));
                        if let Some(engine) = failed_engine {
                            supervisor.profile = Some(ProfileJob {
                                engine,
                                spec: stock,
                            });
                            recovery = "stock fallback is still stopping".to_string();
                        }
                    }
                }
            }
            Err(format!(
                "failed to apply game runtime spec: {apply_error}; {}{recovery}",
                if target_recovery.is_empty() {
                    String::new()
                } else {
                    format!("{target_recovery}; ")
                }
            ))
        }
    }
}

pub fn apply_runtime_spec(
    sup: &Mutex<Supervisor>,
    spec: RuntimeSpec,
) -> Result<StartResult, String> {
    spec.validate()?;
    let profile_id = spec.active_profile_id();
    let runtime_mode = spec.mode_name().to_string();
    let gpu_uuid = spec.gpu.uuid.clone();

    let mut supervisor = guard(sup);
    match supervisor.child_running_kind() {
        Some(ChildKind::Scan) => {
            return Err("cannot apply a runtime spec while Auto-UV scan is running".to_string());
        }
        Some(ChildKind::Verify) => {
            return Err(
                "cannot apply a runtime spec while profile verification is running".to_string(),
            );
        }
        None => {}
    }

    let preserve_persisted_standing =
        !supervisor.game_runtime.watches.is_empty() && supervisor.game_runtime.override_active;
    let previous_spec = supervisor.profile.as_ref().map(|job| job.spec.clone());
    let inactive_restore_spec = previous_spec
        .as_ref()
        .filter(|previous| previous.gpu.uuid != spec.gpu.uuid)
        .map(|_| {
            supervisor
                .last_applied_specs
                .get(&spec.gpu.uuid)
                .cloned()
                .unwrap_or_else(|| spec.stock_fallback())
        });
    supervisor.stop_engine_for_child("a new runtime spec")?;
    match profile::start(spec.clone()) {
        Ok(engine) => {
            supervisor.profile = Some(ProfileJob {
                engine,
                spec: spec.clone(),
            });
            if !supervisor.game_runtime.watches.is_empty() {
                // A Profiles-tab action while a game is watched becomes the
                // new standing action immediately. Keep the watch for Steam
                // hot re-apply/status, but do not tear this action down when
                // the game exits unless another game override replaces it.
                supervisor.game_runtime.standing_spec = Some(spec.clone());
                supervisor.game_runtime.override_active = false;
            }
            supervisor
                .last_applied_specs
                .insert(spec.gpu.uuid.clone(), spec.clone());
            persist_active_runtime(&spec);
            persist_applied_runtime_history(&supervisor.last_applied_specs);
            drop(supervisor);
            Ok(StartResult {
                started: true,
                pid: std::process::id(),
                profile_id,
                runtime_mode,
                gpu_uuid,
            })
        }
        Err(error) => {
            let (apply_error, failed_engine) = error.into_parts();
            if let Some(engine) = failed_engine {
                supervisor.profile = Some(ProfileJob {
                    engine,
                    spec: spec.clone(),
                });
                return Err(format!(
                    "failed to apply runtime spec: {apply_error}; refusing rollback while the failed engine may still be running"
                ));
            }
            let mut recovery = "no fallback runtime could be started".to_string();
            let mut target_recovery = String::new();
            let mut recovered_spec = None;
            let mut recovery_engine_wedged = false;

            if let Some(target_spec) = inactive_restore_spec {
                match apply_inactive_spec_and_stop(
                    &mut supervisor,
                    target_spec,
                    "restore the failed target GPU",
                ) {
                    Ok(()) => {
                        target_recovery = "failed target GPU restored".to_string();
                    }
                    Err(error) => {
                        logging::error(&error);
                        target_recovery = error;
                        if supervisor.profile.is_some() {
                            return Err(format!(
                                "failed to apply runtime spec: {apply_error}; {target_recovery}; refusing to restart the previous engine while a failed restore engine may still be running"
                            ));
                        }
                    }
                }
            }

            if let Some(previous) = previous_spec {
                match profile::start(previous.clone()) {
                    Ok(engine) => {
                        supervisor.profile = Some(ProfileJob {
                            engine,
                            spec: previous.clone(),
                        });
                        recovery = "previous runtime restored".to_string();
                        recovered_spec = Some(previous);
                    }
                    Err(restore_error) => {
                        let (message, failed_engine) = restore_error.into_parts();
                        logging::error(&format!(
                            "failed to restore previous runtime after apply failure: {message}"
                        ));
                        if let Some(engine) = failed_engine {
                            supervisor.profile = Some(ProfileJob {
                                engine,
                                spec: previous.clone(),
                            });
                            recovered_spec = Some(previous);
                            recovery = "previous runtime restore is still stopping".to_string();
                            recovery_engine_wedged = true;
                        }
                    }
                }
            }

            if recovered_spec.is_none() && !recovery_engine_wedged {
                let stock = spec.stock_fallback();
                match profile::start(stock.clone()) {
                    Ok(engine) => {
                        supervisor.profile = Some(ProfileJob {
                            engine,
                            spec: stock.clone(),
                        });
                        recovery = "stock fallback applied".to_string();
                        recovered_spec = Some(stock);
                    }
                    Err(stock_error) => {
                        let (message, failed_engine) = stock_error.into_parts();
                        logging::error(&format!(
                            "failed to apply stock fallback after runtime apply failure: {message}"
                        ));
                        if let Some(engine) = failed_engine {
                            supervisor.profile = Some(ProfileJob {
                                engine,
                                spec: stock.clone(),
                            });
                            recovery = "stock fallback is still stopping".to_string();
                            recovery_engine_wedged = true;
                        }
                    }
                }
            }

            if !preserve_persisted_standing {
                if let Some(recovered) = recovered_spec {
                    persist_active_runtime(&recovered);
                } else if !recovery_engine_wedged {
                    clear_active_runtime();
                }
            }
            persist_applied_runtime_history(&supervisor.last_applied_specs);
            drop(supervisor);
            Err(format!(
                "failed to apply runtime spec: {apply_error}; {}{recovery}",
                if target_recovery.is_empty() {
                    String::new()
                } else {
                    format!("{target_recovery}; ")
                }
            ))
        }
    }
}

pub fn stop_runtime_profile(sup: &Mutex<Supervisor>) -> Result<StopResult, String> {
    // An explicit stop settles this session's runtime intent: the saved boot
    // set must not be replayed by the deep-sleep watcher afterwards.
    BOOT_RUNTIME_CONSUMED.store(true, Ordering::SeqCst);
    crate::rtd3::set_parked(false);
    let mut supervisor = guard(sup);
    let stop_timeout = supervisor.stop_timeout;
    match supervisor.profile.as_mut() {
        Some(job) if job.engine.is_running() => {
            let pid = std::process::id();
            match job.engine.stop(stop_timeout) {
                StopOutcome::Stopped => {
                    supervisor.profile = None;
                    if !supervisor.game_runtime.watches.is_empty() {
                        supervisor.game_runtime.standing_spec = None;
                        supervisor.game_runtime.override_active = false;
                    }
                    drop(supervisor);
                    clear_active_runtime();
                    return Ok(StopResult::stopped(pid));
                }
                // Wedged engine: keep the job (so future scan/verification/
                // profile starts still see it and refuse) and tell the client
                // the truth. The stop flag is set, so the engine exits — and
                // runs its cleanup — as soon as the wedged call returns.
                StopOutcome::TimedOut => {
                    let message =
                        "runtime profile engine did not stop within timeout (wedged GPU call?)"
                            .to_string();
                    logging::error(&message);
                    return Err(message);
                }
            }
        }
        _ => {}
    }
    if !supervisor.game_runtime.watches.is_empty() {
        supervisor.game_runtime.standing_spec = None;
        supervisor.game_runtime.override_active = false;
    }
    drop(supervisor);
    clear_active_runtime();
    Ok(StopResult::idle())
}

/// Result of the atomic child-start critical section.
pub enum ChildStart {
    Refused(String),
    ClearFailed(String),
    SpawnFailed(String),
    Started(Arc<ChildJob>, File),
}

/// Atomically (under one lock, like Python's `_ACTIVE_SCAN_LOCK`): refuse a
/// concurrent scan/verification, clear the kind's stale stop-request, stop the
/// engine, spawn the child via `spawn`, and install the job. Holding the lock
/// across the spawn is what prevents two racing requests from both launching.
pub fn begin_child(
    sup: &Mutex<Supervisor>,
    kind: ChildKind,
    argv: Vec<String>,
    spawn: impl FnOnce(&[String]) -> std::io::Result<(Child, File)>,
) -> ChildStart {
    let mut supervisor = guard(sup);
    if let Some(running) = supervisor.child_running_kind() {
        return ChildStart::Refused(child_refusal(running, kind));
    }
    if let Err(err) = kind.clear_stop_request() {
        return ChildStart::ClearFailed(err.to_string());
    }
    let what = match kind {
        ChildKind::Scan => "an Auto-UV scan",
        ChildKind::Verify => "profile verification",
    };
    if let Err(err) = supervisor.stop_engine_for_child(what) {
        return ChildStart::Refused(err);
    }
    let (child, reader) = match spawn(&argv) {
        Ok(pair) => pair,
        Err(err) => return ChildStart::SpawnFailed(err.to_string()),
    };
    let generation = supervisor.next_child_generation();
    let proc = match ChildProc::new(child) {
        Ok(proc) => proc,
        Err(err) => return ChildStart::SpawnFailed(err.to_string()),
    };
    let job = Arc::new(ChildJob {
        proc,
        argv,
        generation,
        kind,
    });
    supervisor.child = Some(job.clone());
    ChildStart::Started(job, reader)
}

/// Clear the child slot if it still holds `job` (identity guard) and, if so,
/// restore the persisted current-session RuntimeSpec now that the GPU is free.
/// This is the exact fixed/adaptive runtime that was active before the child;
/// the boot RuntimeSpec is only a fallback when no current-session state exists.
pub fn finish_child(sup: &Arc<Mutex<Supervisor>>, job: &Arc<ChildJob>) {
    let restart = {
        let mut supervisor = guard(sup);
        match &supervisor.child {
            Some(current) if current.generation == job.generation => {
                supervisor.child = None;
                true
            }
            _ => false,
        }
    };
    if restart {
        resume_autostart_after_child(sup);
    }
}

pub fn set_boot_runtime_spec(spec: RuntimeSpec) -> Result<Value, String> {
    spec.validate()?;
    let _update_guard = BOOT_STATE_UPDATE_LOCK
        .lock()
        .unwrap_or_else(|poison| poison.into_inner());
    let path = boot_state_file_path();
    let mut set = load_boot_runtime_set(&path)?;
    set.upsert(spec.clone());
    persist_json(&path, &set)?;
    Ok(serde_json::json!({
        "saved": true,
        "profile_id": spec.active_profile_id(),
        "runtime_mode": spec.mode_name(),
        "gpu_uuid": spec.gpu.uuid,
        "active_gpu_uuid": set.active_gpu_uuid,
        "main_gpu_uuid": set.main_gpu_uuid,
        "saved_gpu_count": set.specs.len(),
    }))
}

pub fn set_boot_main_gpu(gpu_uuid: &str) -> Result<Value, String> {
    let _update_guard = BOOT_STATE_UPDATE_LOCK
        .lock()
        .unwrap_or_else(|poison| poison.into_inner());
    let path = boot_state_file_path();
    let mut set = load_boot_runtime_set(&path)?;
    set.set_main_gpu(gpu_uuid)?;
    if !set.specs.is_empty() {
        persist_json(&path, &set)?;
    }
    Ok(serde_json::json!({
        "selected": !set.main_gpu_uuid.is_empty(),
        "main_gpu_uuid": set.main_gpu_uuid,
        "active_gpu_uuid": set.active_gpu_uuid,
        "saved_gpu_count": set.specs.len(),
    }))
}

pub fn clear_boot_runtime_spec(gpu_uuid: &str) -> Result<Value, String> {
    let _update_guard = BOOT_STATE_UPDATE_LOCK
        .lock()
        .unwrap_or_else(|poison| poison.into_inner());
    let path = boot_state_file_path();
    let selected_uuid = gpu_uuid.trim();
    if selected_uuid.is_empty() {
        let cleared = match fs::remove_file(&path) {
            Ok(()) => true,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
            Err(error) => {
                return Err(format!(
                    "failed to clear boot runtime state {}: {error}",
                    path.display()
                ))
            }
        };
        return Ok(serde_json::json!({"cleared": cleared, "saved_gpu_count": 0}));
    }
    let mut set = load_boot_runtime_set(&path)?;
    let cleared = set.remove(selected_uuid);
    if set.specs.is_empty() {
        if let Err(error) = fs::remove_file(&path) {
            if error.kind() != std::io::ErrorKind::NotFound {
                return Err(format!(
                    "failed to clear boot runtime state {}: {error}",
                    path.display()
                ));
            }
        }
    } else if cleared {
        persist_json(&path, &set)?;
    }
    Ok(serde_json::json!({
        "cleared": cleared,
        "gpu_uuid": selected_uuid,
        "active_gpu_uuid": set.active_gpu_uuid,
        "main_gpu_uuid": set.main_gpu_uuid,
        "saved_gpu_count": set.specs.len(),
    }))
}

fn boot_spec_summary(spec: &RuntimeSpec) -> Value {
    serde_json::json!({
        "profile_id": spec.active_profile_id(),
        "runtime_mode": spec.mode_name(),
        "gpu_uuid": spec.gpu.uuid,
        "silent_fan_curve": spec.fan.enabled,
    })
}

pub fn boot_runtime_spec_summary(sup: &Mutex<Supervisor>) -> Result<Value, String> {
    let set = load_boot_runtime_set(&boot_state_file_path())?;
    let replay = guard(sup).boot_replay.clone();
    let Some(active) = set.active_spec() else {
        return Ok(serde_json::json!({
            "configured": false,
            "gpus": [],
            "replay": replay,
        }));
    };
    let effective_active_gpu_uuid = replay
        .iter()
        .find(|entry| {
            matches!(
                entry.get("outcome").and_then(Value::as_str),
                Some("active" | "stock-fallback-active")
            )
        })
        .and_then(|entry| entry.get("gpu_uuid"))
        .and_then(Value::as_str)
        .unwrap_or(&set.active_gpu_uuid);
    Ok(serde_json::json!({
        "configured": true,
        "profile_id": active.active_profile_id(),
        "runtime_mode": active.mode_name(),
        "gpu_uuid": active.gpu.uuid,
        "silent_fan_curve": active.fan.enabled,
        "active_gpu_uuid": set.active_gpu_uuid,
        "main_gpu_uuid": set.main_gpu_uuid,
        "effective_active_gpu_uuid": effective_active_gpu_uuid,
        "gpus": set.specs.iter().map(boot_spec_summary).collect::<Vec<_>>(),
        "replay": replay,
    }))
}

fn resolve_recovery_gpu(mut spec: RuntimeSpec) -> Result<RuntimeSpec, String> {
    let saved_index = spec.gpu.index_at_resolution;
    let current_index = crate::gpu::NvmlBackend::resolve_gpu_index(&spec.gpu.uuid, saved_index)
        .map_err(|error| error.to_string())?;
    if current_index != saved_index {
        logging::info(&format!(
            "resolved runtime GPU {} from saved index {} to current index {}",
            spec.gpu.uuid, saved_index, current_index
        ));
    }
    spec.gpu.index_at_resolution = current_index;
    Ok(spec)
}

#[derive(Clone, Copy)]
enum BootReplayOutcome {
    Active,
    Applied,
    ApplyFailed,
    EngineStopTimeout,
    GpuNotDetected,
    StockFallback,
    StockFallbackActive,
    StockFallbackFailed,
    StockSkipped,
}

impl BootReplayOutcome {
    fn as_str(self) -> &'static str {
        match self {
            Self::Active => "active",
            Self::Applied => "applied",
            Self::ApplyFailed => "apply-failed",
            Self::EngineStopTimeout => "engine-stop-timeout",
            Self::GpuNotDetected => "gpu-not-detected",
            Self::StockFallback => "stock-fallback",
            Self::StockFallbackActive => "stock-fallback-active",
            Self::StockFallbackFailed => "stock-fallback-failed",
            Self::StockSkipped => "stock-skipped",
        }
    }
}

fn boot_replay_result(
    gpu_uuid: &str,
    gpu_index: Option<u32>,
    outcome: BootReplayOutcome,
    error: Option<&str>,
) -> Value {
    let mut value = serde_json::json!({
        "gpu_uuid": gpu_uuid,
        "outcome": outcome.as_str(),
    });
    let object = value
        .as_object_mut()
        .expect("boot replay result is an object");
    if let Some(index) = gpu_index {
        object.insert("gpu_index".to_string(), Value::from(index));
    }
    if let Some(message) = error.filter(|message| !message.is_empty()) {
        object.insert("error".to_string(), Value::String(message.to_string()));
    }
    value
}

fn recover_active_session(supervisor: &mut Supervisor, path: &Path, saved: RuntimeSpec) -> bool {
    let spec = match resolve_recovery_gpu(saved) {
        Ok(spec) => spec,
        Err(error) => {
            logging::error(&format!(
                "failed to resolve active-session runtime from {}: {error}",
                path.display()
            ));
            return false;
        }
    };
    match profile::start(spec.clone()) {
        Ok(engine) => {
            logging::info(&format!(
                "recovered active-session runtime: mode={} profile={} gpu={}",
                spec.mode_name(),
                spec.active_profile_id(),
                spec.gpu.uuid
            ));
            supervisor.profile = Some(ProfileJob {
                engine,
                spec: spec.clone(),
            });
            supervisor
                .last_applied_specs
                .insert(spec.gpu.uuid.clone(), spec.clone());
            if !supervisor.game_runtime.watches.is_empty() {
                supervisor.game_runtime.standing_spec = Some(spec.clone());
                supervisor.game_runtime.override_active = false;
            }
            persist_active_runtime(&spec);
            true
        }
        Err(error) => {
            let (message, failed_engine) = error.into_parts();
            logging::error(&format!(
                "failed to recover active-session runtime from {}: {message}",
                path.display()
            ));
            if let Some(engine) = failed_engine {
                supervisor.profile = Some(ProfileJob { engine, spec });
                return true;
            }
            if spec.mode_name() == "stock" {
                return true;
            }
            let stock = spec.stock_fallback();
            match profile::start(stock.clone()) {
                Ok(engine) => {
                    logging::warn(&format!(
                        "recovered failed active-session runtime with stock fallback for gpu={}",
                        stock.gpu.uuid
                    ));
                    supervisor.profile = Some(ProfileJob {
                        engine,
                        spec: stock.clone(),
                    });
                    supervisor
                        .last_applied_specs
                        .insert(stock.gpu.uuid.clone(), stock.clone());
                    if !supervisor.game_runtime.watches.is_empty() {
                        supervisor.game_runtime.standing_spec = Some(stock.clone());
                        supervisor.game_runtime.override_active = false;
                    }
                    persist_active_runtime(&stock);
                    true
                }
                Err(stock_error) => {
                    let (stock_message, failed_engine) = stock_error.into_parts();
                    logging::error(&format!(
                        "failed to apply stock fallback after active-session recovery failure: {stock_message}"
                    ));
                    if let Some(engine) = failed_engine {
                        supervisor.profile = Some(ProfileJob {
                            engine,
                            spec: stock,
                        });
                        return true;
                    }
                    true
                }
            }
        }
    }
}

fn cache_saved_boot_specs(supervisor: &mut Supervisor) {
    let Ok(set) = load_boot_runtime_set(&boot_state_file_path()) else {
        return;
    };
    for saved in set.specs {
        if let Ok(spec) = resolve_recovery_gpu(saved) {
            supervisor
                .last_applied_specs
                .entry(spec.gpu.uuid.clone())
                .or_insert(spec);
        }
    }
}

fn cache_applied_runtime_history(supervisor: &mut Supervisor) {
    let path = applied_runtime_history_path();
    let history = match load_applied_runtime_history(&path) {
        Ok(Some(history)) => history,
        Ok(None) => return,
        Err(error) => {
            logging::error(&error);
            return;
        }
    };
    for saved in history.specs {
        let uuid = saved.gpu.uuid.clone();
        match resolve_recovery_gpu(saved) {
            Ok(spec) => {
                supervisor.last_applied_specs.entry(uuid).or_insert(spec);
            }
            Err(error) => logging::warn(&format!(
                "applied runtime history skipped for gpu={uuid}: {error}"
            )),
        }
    }
}

struct BootReplayStep {
    result: Value,
    abort: bool,
}

fn replay_inactive_boot_spec(supervisor: &mut Supervisor, spec: RuntimeSpec) -> BootReplayStep {
    let uuid = spec.gpu.uuid.clone();
    let index = spec.gpu.index_at_resolution;
    match profile::start(spec.clone()) {
        Ok(mut engine) => {
            if engine.stop(supervisor.stop_timeout) == StopOutcome::TimedOut {
                let message = "temporary boot engine did not stop within timeout";
                supervisor.profile = Some(ProfileJob { engine, spec });
                return BootReplayStep {
                    result: boot_replay_result(
                        &uuid,
                        Some(index),
                        BootReplayOutcome::EngineStopTimeout,
                        Some(message),
                    ),
                    abort: true,
                };
            }
            supervisor.last_applied_specs.insert(uuid.clone(), spec);
            BootReplayStep {
                result: boot_replay_result(&uuid, Some(index), BootReplayOutcome::Applied, None),
                abort: false,
            }
        }
        Err(error) => {
            let (message, failed_engine) = error.into_parts();
            if let Some(engine) = failed_engine {
                supervisor.profile = Some(ProfileJob { engine, spec });
                return BootReplayStep {
                    result: boot_replay_result(
                        &uuid,
                        Some(index),
                        BootReplayOutcome::ApplyFailed,
                        Some(&message),
                    ),
                    abort: true,
                };
            }
            if spec.mode_name() == "stock" {
                return BootReplayStep {
                    result: boot_replay_result(
                        &uuid,
                        Some(index),
                        BootReplayOutcome::ApplyFailed,
                        Some(&message),
                    ),
                    abort: false,
                };
            }

            let stock = spec.stock_fallback();
            match profile::start(stock.clone()) {
                Ok(mut engine) => {
                    if engine.stop(supervisor.stop_timeout) == StopOutcome::TimedOut {
                        let stop_message =
                            "temporary stock fallback engine did not stop within timeout";
                        supervisor.profile = Some(ProfileJob {
                            engine,
                            spec: stock,
                        });
                        return BootReplayStep {
                            result: boot_replay_result(
                                &uuid,
                                Some(index),
                                BootReplayOutcome::EngineStopTimeout,
                                Some(stop_message),
                            ),
                            abort: true,
                        };
                    }
                    supervisor
                        .last_applied_specs
                        .insert(stock.gpu.uuid.clone(), stock);
                    BootReplayStep {
                        result: boot_replay_result(
                            &uuid,
                            Some(index),
                            BootReplayOutcome::StockFallback,
                            Some(&message),
                        ),
                        abort: false,
                    }
                }
                Err(stock_error) => {
                    let (stock_message, failed_engine) = stock_error.into_parts();
                    let abort = failed_engine.is_some();
                    if let Some(engine) = failed_engine {
                        supervisor.profile = Some(ProfileJob {
                            engine,
                            spec: stock,
                        });
                    }
                    BootReplayStep {
                        result: boot_replay_result(
                            &uuid,
                            Some(index),
                            BootReplayOutcome::StockFallbackFailed,
                            Some(&stock_message),
                        ),
                        abort,
                    }
                }
            }
        }
    }
}

fn start_active_boot_spec(
    supervisor: &mut Supervisor,
    spec: RuntimeSpec,
) -> (Value, Option<RuntimeSpec>) {
    let uuid = spec.gpu.uuid.clone();
    let index = spec.gpu.index_at_resolution;
    match profile::start(spec.clone()) {
        Ok(engine) => {
            logging::info(&format!(
                "recovered boot runtime: mode={} profile={} gpu={}",
                spec.mode_name(),
                spec.active_profile_id(),
                spec.gpu.uuid
            ));
            supervisor.profile = Some(ProfileJob {
                engine,
                spec: spec.clone(),
            });
            supervisor
                .last_applied_specs
                .insert(spec.gpu.uuid.clone(), spec.clone());
            if !supervisor.game_runtime.watches.is_empty() {
                supervisor.game_runtime.standing_spec = Some(spec.clone());
                supervisor.game_runtime.override_active = false;
            }
            (
                boot_replay_result(&uuid, Some(index), BootReplayOutcome::Active, None),
                Some(spec),
            )
        }
        Err(error) => {
            let (message, failed_engine) = error.into_parts();
            if let Some(engine) = failed_engine {
                supervisor.profile = Some(ProfileJob { engine, spec });
                return (
                    boot_replay_result(
                        &uuid,
                        Some(index),
                        BootReplayOutcome::ApplyFailed,
                        Some(&message),
                    ),
                    None,
                );
            }
            if spec.mode_name() == "stock" {
                return (
                    boot_replay_result(
                        &uuid,
                        Some(index),
                        BootReplayOutcome::ApplyFailed,
                        Some(&message),
                    ),
                    None,
                );
            }

            let stock = spec.stock_fallback();
            match profile::start(stock.clone()) {
                Ok(engine) => {
                    supervisor.profile = Some(ProfileJob {
                        engine,
                        spec: stock.clone(),
                    });
                    supervisor
                        .last_applied_specs
                        .insert(stock.gpu.uuid.clone(), stock.clone());
                    if !supervisor.game_runtime.watches.is_empty() {
                        supervisor.game_runtime.standing_spec = Some(stock.clone());
                        supervisor.game_runtime.override_active = false;
                    }
                    (
                        boot_replay_result(
                            &uuid,
                            Some(index),
                            BootReplayOutcome::StockFallbackActive,
                            Some(&message),
                        ),
                        Some(stock),
                    )
                }
                Err(stock_error) => {
                    let (stock_message, failed_engine) = stock_error.into_parts();
                    if let Some(engine) = failed_engine {
                        supervisor.profile = Some(ProfileJob {
                            engine,
                            spec: stock,
                        });
                    }
                    (
                        boot_replay_result(
                            &uuid,
                            Some(index),
                            BootReplayOutcome::StockFallbackFailed,
                            Some(&stock_message),
                        ),
                        None,
                    )
                }
            }
        }
    }
}

fn ordered_boot_replay(
    ordered_uuids: &[String],
    replay_by_uuid: &BTreeMap<String, Value>,
) -> Vec<Value> {
    ordered_uuids
        .iter()
        .filter_map(|uuid| replay_by_uuid.get(uuid).cloned())
        .collect()
}

/// Recover the current-session runtime first. On a fresh boot, resolve every
/// saved GPU UUID, apply the available specs serially, and leave one selected
/// non-stock engine running. On RTD3 systems, stock entries are recorded but
/// never opened: a stock reset is unnecessary after boot and must not wake a
/// sleeping card. Stopping earlier non-stock engines deliberately leaves their
/// V/F curve, memory offset, and power limit applied while returning fans to
/// hardware auto and releasing clock locks.
pub fn start_autostart_if_configured(sup: &Arc<Mutex<Supervisor>>) -> bool {
    let mut supervisor = guard(sup);
    if supervisor.profile.is_some() || supervisor.child_running_kind().is_some() {
        return false;
    }
    supervisor.boot_replay.clear();
    cache_applied_runtime_history(&mut supervisor);

    let active_path = active_state_file_path();
    match load_runtime_spec(&active_path) {
        Ok(Some(spec)) => {
            BOOT_RUNTIME_CONSUMED.store(true, Ordering::SeqCst);
            if recover_active_session(&mut supervisor, &active_path, spec) {
                cache_saved_boot_specs(&mut supervisor);
                return true;
            }
        }
        Ok(None) => {}
        Err(error) => logging::error(&error),
    }

    let boot_path = boot_state_file_path();
    let set = match load_boot_runtime_set(&boot_path) {
        Ok(set) => set,
        Err(error) => {
            logging::error(&error);
            return false;
        }
    };
    if set.specs.is_empty() {
        return false;
    }
    BOOT_RUNTIME_CONSUMED.store(true, Ordering::SeqCst);

    let ordered_uuids: Vec<String> = set.specs.iter().map(|spec| spec.gpu.uuid.clone()).collect();
    let mut replay_by_uuid = BTreeMap::new();
    let mut available = Vec::new();
    for saved in set.specs {
        let uuid = saved.gpu.uuid.clone();
        match resolve_recovery_gpu(saved) {
            Ok(spec) => available.push(spec),
            Err(error) => {
                logging::warn(&format!("boot runtime skipped for gpu={uuid}: {error}"));
                replay_by_uuid.insert(
                    uuid.clone(),
                    boot_replay_result(
                        &uuid,
                        None,
                        BootReplayOutcome::GpuNotDetected,
                        Some(&error),
                    ),
                );
            }
        }
    }
    let default_active_uuid = if available
        .iter()
        .any(|spec| spec.gpu.uuid == set.active_gpu_uuid)
    {
        set.active_gpu_uuid
    } else {
        available
            .last()
            .map(|spec| spec.gpu.uuid.clone())
            .unwrap_or_default()
    };
    let protects_deep_sleep = crate::rtd3::protects_deep_sleep();
    let target_pci_addr = crate::rtd3::target_pci_addr();
    let active_uuid = if protects_deep_sleep {
        target_pci_addr
            .as_deref()
            .and_then(|target| {
                available
                    .iter()
                    .find(|spec| spec.mode_name() != "stock" && spec_matches_pci(spec, target))
            })
            .or_else(|| {
                available
                    .iter()
                    .rev()
                    .find(|spec| spec.mode_name() != "stock")
            })
            .map(|spec| spec.gpu.uuid.clone())
            .unwrap_or(default_active_uuid)
    } else {
        default_active_uuid
    };

    for spec in available.iter().filter(|spec| spec.gpu.uuid != active_uuid) {
        let uuid = spec.gpu.uuid.clone();
        if protects_deep_sleep && spec.mode_name() == "stock" {
            logging::info(&format!(
                "deep sleep: leaving boot stock target detached for gpu={uuid}"
            ));
            supervisor
                .last_applied_specs
                .insert(uuid.clone(), spec.clone());
            replay_by_uuid.insert(
                uuid.clone(),
                boot_replay_result(
                    &uuid,
                    Some(spec.gpu.index_at_resolution),
                    BootReplayOutcome::StockSkipped,
                    None,
                ),
            );
            continue;
        }
        let replay = replay_inactive_boot_spec(&mut supervisor, spec.clone());
        replay_by_uuid.insert(uuid, replay.result);
        if replay.abort {
            supervisor.boot_replay = ordered_boot_replay(&ordered_uuids, &replay_by_uuid);
            return true;
        }
    }

    let active_spec = available
        .into_iter()
        .find(|spec| spec.gpu.uuid == active_uuid);
    let persisted_active = active_spec.and_then(|spec| {
        let uuid = spec.gpu.uuid.clone();
        if protects_deep_sleep && spec.mode_name() == "stock" {
            logging::info(&format!(
                "deep sleep: leaving boot stock target detached for gpu={uuid}"
            ));
            supervisor
                .last_applied_specs
                .insert(uuid.clone(), spec.clone());
            replay_by_uuid.insert(
                uuid.clone(),
                boot_replay_result(
                    &uuid,
                    Some(spec.gpu.index_at_resolution),
                    BootReplayOutcome::StockSkipped,
                    None,
                ),
            );
            return None;
        }
        let (result, persisted) = start_active_boot_spec(&mut supervisor, spec);
        replay_by_uuid.insert(uuid, result);
        persisted
    });
    supervisor.boot_replay = ordered_boot_replay(&ordered_uuids, &replay_by_uuid);
    if let Some(spec) = persisted_active {
        persist_active_runtime(&spec);
    }
    drop(supervisor);
    true
}

/// Shutdown cleanup: stop the in-process engine (A3 releases fans + clock lock).
pub fn shutdown(sup: &Mutex<Supervisor>) {
    let mut supervisor = guard(sup);
    let stop_timeout = supervisor.stop_timeout;
    if let Some(mut job) = supervisor.profile.take() {
        if job.engine.stop(stop_timeout) == StopOutcome::TimedOut {
            // The process exits anyway; the wedged thread cannot run its
            // cleanup, so at least say so in the journal.
            logging::error(
                "engine thread did not stop during shutdown; exiting without its fan/clock cleanup",
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // `STATE_FILE_ENV` is process-global; serialize the tests that mutate it.
    static STATE_ENV_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn deferred_pending_honors_boot_consumption() {
        // Active-session intent always re-arms the watcher.
        assert!(deferred_pending_from(true, false, false));
        assert!(deferred_pending_from(true, true, true));
        // The boot set arms it at most once per daemon lifetime.
        assert!(deferred_pending_from(false, true, false));
        assert!(!deferred_pending_from(false, true, true));
        assert!(!deferred_pending_from(false, false, false));
    }

    struct StateEnvGuard {
        name: &'static str,
        previous: Option<std::ffi::OsString>,
    }
    impl StateEnvGuard {
        fn new(path: &std::path::Path) -> Self {
            Self::named(STATE_FILE_ENV, path)
        }

        fn named(name: &'static str, value: impl AsRef<std::ffi::OsStr>) -> Self {
            let previous = env::var_os(name);
            env::set_var(name, value);
            StateEnvGuard { name, previous }
        }
    }
    impl Drop for StateEnvGuard {
        fn drop(&mut self) {
            match &self.previous {
                Some(value) => env::set_var(self.name, value),
                None => env::remove_var(self.name),
            }
        }
    }

    // --- wedged-engine (StopOutcome::TimedOut) refusal paths ------------------

    use std::sync::atomic::{AtomicBool, Ordering};

    /// A supervisor holding a running engine that ignores its stop flag for
    /// `wedge` (longer than the supervisor's `stop_timeout`).
    fn wedged_supervisor(wedge: Duration, stop_timeout: Duration) -> Mutex<Supervisor> {
        let sup = Mutex::new(Supervisor::with_stop_timeout(stop_timeout));
        guard(&sup).profile = Some(ProfileJob {
            engine: profile::wedged_engine_for_test(wedge),
            spec: RuntimeSpec::test_stock("GPU-test"),
        });
        sup
    }

    #[test]
    fn begin_child_refuses_scan_when_engine_stop_times_out() {
        let sup = wedged_supervisor(Duration::from_secs(5), Duration::from_millis(50));
        let spawned = AtomicBool::new(false);
        let result = begin_child(&sup, ChildKind::Scan, vec![], |_| {
            spawned.store(true, Ordering::SeqCst);
            Err(std::io::Error::other("must not spawn"))
        });
        match result {
            ChildStart::Refused(err) => {
                assert!(err.contains("did not stop"), "{err}");
                assert!(err.contains("Auto-UV scan"), "{err}");
            }
            _ => panic!("expected Refused"),
        }
        assert!(
            !spawned.load(Ordering::SeqCst),
            "the child must not be spawned over a wedged engine"
        );
        assert!(
            guard(&sup).profile.is_some(),
            "the wedged job must be retained so later attempts re-check it"
        );
    }

    #[test]
    fn begin_child_refuses_verification_when_engine_stop_times_out() {
        let sup = wedged_supervisor(Duration::from_secs(5), Duration::from_millis(50));
        let result = begin_child(&sup, ChildKind::Verify, vec![], |_| {
            Err(std::io::Error::other("must not spawn"))
        });
        match result {
            ChildStart::Refused(err) => {
                assert!(err.contains("did not stop"), "{err}");
                assert!(err.contains("profile verification"), "{err}");
            }
            _ => panic!("expected Refused"),
        }
    }

    #[test]
    fn apply_runtime_spec_refuses_when_engine_stop_times_out() {
        let sup = wedged_supervisor(Duration::from_secs(5), Duration::from_millis(50));
        let err = apply_runtime_spec(&sup, RuntimeSpec::test_stock("GPU-other")).unwrap_err();
        assert!(err.contains("did not stop"), "{err}");
        assert!(guard(&sup).profile.is_some());
    }

    #[test]
    fn stop_runtime_profile_errors_when_engine_wedged() {
        let sup = wedged_supervisor(Duration::from_secs(5), Duration::from_millis(50));
        let err = stop_runtime_profile(&sup).unwrap_err();
        assert!(err.contains("did not stop"), "{err}");
        assert!(
            guard(&sup).profile.is_some(),
            "the wedged job must be retained (dropping it would let a scan start \
             while the engine can still write to the GPU)"
        );
    }

    #[test]
    fn begin_child_recovers_after_wedged_engine_exits() {
        let sup = wedged_supervisor(Duration::from_millis(300), Duration::from_millis(50));
        let result = begin_child(&sup, ChildKind::Scan, vec![], |_| {
            Err(std::io::Error::other("no spawn"))
        });
        assert!(matches!(result, ChildStart::Refused(_)));
        // The wedged engine eventually exits on its own; a retry must get past
        // the engine gate (reaching the spawn closure proves it).
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            std::thread::sleep(Duration::from_millis(50));
            let result = begin_child(&sup, ChildKind::Scan, vec![], |_| {
                Err(std::io::Error::other("no spawn"))
            });
            match result {
                ChildStart::SpawnFailed(_) => break,
                ChildStart::Refused(_) if Instant::now() < deadline => continue,
                ChildStart::Refused(err) => panic!("still refused after engine exit: {err}"),
                _ => panic!("expected SpawnFailed or Refused"),
            }
        }
    }

    /// After a wedged engine finally exits, the dead job must NOT linger in the
    /// slot — otherwise `start_autostart_if_configured` (early-returns while
    /// `profile.is_some()`) never re-applies the persisted profile after a scan.
    /// `stop_engine_for_child` frees the exited slot on the next start.
    #[test]
    fn stop_engine_for_child_frees_an_already_exited_job() {
        let sup = wedged_supervisor(Duration::from_millis(100), Duration::from_millis(50));
        // First attempt times out and retains the (still-wedged) job.
        assert!(guard(&sup).stop_engine_for_child("a scan").is_err());
        // Let the engine thread finish.
        std::thread::sleep(Duration::from_millis(300));
        assert!(!guard(&sup).profile.as_ref().unwrap().engine.is_running());
        // The next start sees a dead engine and frees the slot (Ok, not Err).
        assert!(guard(&sup).stop_engine_for_child("a scan").is_ok());
        assert!(
            guard(&sup).profile.is_none(),
            "an exited engine's slot must be freed so autostart can restart"
        );
    }

    /// A retry against a STILL-wedged engine must return quickly rather than
    /// re-paying the full stop timeout under the supervisor mutex (which would
    /// starve the watchdog and hang status). The second stop polls once.
    #[test]
    fn repeated_stop_of_wedged_engine_is_fast() {
        let sup = wedged_supervisor(Duration::from_secs(5), Duration::from_millis(400));
        // First stop pays the timeout.
        assert!(guard(&sup).stop_engine_for_child("a scan").is_err());
        // Second stop, still wedged: must be near-instant, not another 400ms.
        let start = Instant::now();
        assert!(guard(&sup).stop_engine_for_child("a scan").is_err());
        assert!(
            start.elapsed() < Duration::from_millis(200),
            "a retry against a still-wedged engine must not re-wait the timeout: {:?}",
            start.elapsed()
        );
    }

    #[test]
    fn active_runtime_round_trip() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-a-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("active-runtime.json");
        let _guard = StateEnvGuard::new(&path);

        let spec = RuntimeSpec::test_stock("GPU-round-trip");
        persist_runtime_spec(&active_state_file_path(), &spec).unwrap();
        let loaded = load_runtime_spec(&active_state_file_path())
            .unwrap()
            .unwrap();
        assert_eq!(loaded.gpu.uuid, "GPU-round-trip");
        assert_eq!(loaded.mode_name(), "stock");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn autostart_atomically_refuses_a_running_child() {
        use std::process::Command;

        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-child-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("active-runtime.json");
        let _state_guard = StateEnvGuard::new(&path);
        let _inert_guard = StateEnvGuard::named("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1");
        persist_runtime_spec(&path, &RuntimeSpec::test_stock("GPU-child-gate")).unwrap();

        let child = Command::new("sleep")
            .arg("10")
            .spawn()
            .expect("spawn sleep");
        let job = Arc::new(ChildJob {
            proc: ChildProc::new(child).expect("shared child"),
            argv: vec!["sleep".to_string(), "10".to_string()],
            generation: 1,
            kind: ChildKind::Scan,
        });
        let supervisor = Arc::new(Mutex::new(Supervisor::new()));
        guard(&supervisor).child = Some(job.clone());

        assert!(!start_autostart_if_configured(&supervisor));
        assert!(guard(&supervisor).profile.is_none());

        job.proc.signal(libc::SIGKILL);
        job.proc.wait();
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn legacy_single_boot_spec_is_preserved_when_second_gpu_is_saved() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-legacy-boot-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("boot-runtime.json");
        let _boot_guard = StateEnvGuard::named(BOOT_STATE_FILE_ENV, &path);
        let legacy = RuntimeSpec::test_static("GPU-A", "profile-a");
        persist_runtime_spec(&path, &legacy).unwrap();

        set_boot_runtime_spec(RuntimeSpec::test_static("GPU-B", "profile-b")).unwrap();

        let migrated = load_boot_runtime_set(&path).unwrap();
        assert_eq!(migrated.active_gpu_uuid, "GPU-B");
        assert_eq!(migrated.specs.len(), 2);
        assert_eq!(migrated.specs[0].gpu.uuid, "GPU-A");
        assert_eq!(migrated.specs[1].gpu.uuid, "GPU-B");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn explicit_main_gpu_survives_other_saves_and_old_sets_default_to_unset() {
        let mut set = BootRuntimeSet::empty();
        set.upsert(RuntimeSpec::test_static("GPU-A", "profile-a"));
        set.upsert(RuntimeSpec::test_static("GPU-B", "profile-b"));
        set.set_main_gpu("GPU-A").unwrap();
        set.upsert(RuntimeSpec::test_static("GPU-B", "profile-b-new"));
        assert_eq!(set.main_gpu_uuid, "GPU-A");
        assert_eq!(set.active_gpu_uuid, "GPU-A");

        let mut legacy_json = serde_json::to_value(&set).unwrap();
        legacy_json.as_object_mut().unwrap().remove("main_gpu_uuid");
        let legacy_set: BootRuntimeSet = serde_json::from_value(legacy_json).unwrap();
        assert!(legacy_set.main_gpu_uuid.is_empty());
        legacy_set.validate().unwrap();
    }

    #[test]
    fn concurrent_boot_saves_preserve_every_gpu_entry() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-concurrent-boot-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("boot-runtime.json");
        let _boot_guard = StateEnvGuard::named(BOOT_STATE_FILE_ENV, &path);
        let barrier = Arc::new(std::sync::Barrier::new(16));
        let mut workers = Vec::new();
        for index in 0..16 {
            let barrier = barrier.clone();
            workers.push(thread::spawn(move || {
                barrier.wait();
                set_boot_runtime_spec(RuntimeSpec::test_static(
                    &format!("GPU-{index}"),
                    &format!("profile-{index}"),
                ))
                .unwrap();
            }));
        }
        for worker in workers {
            worker.join().unwrap();
        }

        let saved = load_boot_runtime_set(&path).unwrap();
        assert_eq!(saved.specs.len(), 16);
        for index in 0..16 {
            assert!(saved
                .specs
                .iter()
                .any(|spec| spec.gpu.uuid == format!("GPU-{index}")));
        }
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn concurrent_runtime_applies_persist_the_final_active_gpu_and_history() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!(
            "pb-state-concurrent-runtime-{}",
            std::process::id()
        ));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("active-runtime.json");
        let _state_guard = StateEnvGuard::new(&path);
        let _inert_guard = StateEnvGuard::named("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1");
        let supervisor = Arc::new(Mutex::new(Supervisor::new()));
        let barrier = Arc::new(std::sync::Barrier::new(8));
        let mut workers = Vec::new();
        for index in 0..8 {
            let barrier = barrier.clone();
            let supervisor = supervisor.clone();
            workers.push(thread::spawn(move || {
                barrier.wait();
                apply_runtime_spec(
                    &supervisor,
                    RuntimeSpec::test_static(&format!("GPU-{index}"), &format!("profile-{index}")),
                )
                .unwrap();
            }));
        }
        for worker in workers {
            worker.join().unwrap();
        }

        let active_gpu_uuid = guard(&supervisor)
            .profile
            .as_ref()
            .unwrap()
            .spec
            .gpu
            .uuid
            .clone();
        assert_eq!(
            load_runtime_spec(&path).unwrap().unwrap().gpu.uuid,
            active_gpu_uuid
        );
        let history = load_applied_runtime_history(&applied_runtime_history_path())
            .unwrap()
            .unwrap();
        assert_eq!(history.specs.len(), 8);
        shutdown(&supervisor);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn active_session_state_wins_over_older_boot_spec_in_restore_cache() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-cache-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let active_path = dir.join("active-runtime.json");
        let boot_path = dir.join("boot-runtime.json");
        let _active_guard = StateEnvGuard::new(&active_path);
        let _boot_guard = StateEnvGuard::named(BOOT_STATE_FILE_ENV, &boot_path);
        let _inert_guard = StateEnvGuard::named("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1");
        let active = RuntimeSpec::test_static("GPU-A", "session-profile-a");
        persist_runtime_spec(&active_path, &active).unwrap();
        persist_json(
            &boot_path,
            &BootRuntimeSet {
                format_version: BOOT_RUNTIME_SET_FORMAT_VERSION,
                active_gpu_uuid: "GPU-B".to_string(),
                main_gpu_uuid: String::new(),
                specs: vec![
                    RuntimeSpec::test_static("GPU-A", "boot-profile-a"),
                    RuntimeSpec::test_static("GPU-B", "boot-profile-b"),
                ],
            },
        )
        .unwrap();
        let supervisor = Arc::new(Mutex::new(Supervisor::new()));

        start_autostart_if_configured(&supervisor);

        let running = guard(&supervisor);
        assert_eq!(
            running
                .last_applied_specs
                .get("GPU-A")
                .unwrap()
                .active_profile_id(),
            "session-profile-a"
        );
        assert_eq!(
            running
                .last_applied_specs
                .get("GPU-B")
                .unwrap()
                .active_profile_id(),
            "boot-profile-b"
        );
        drop(running);
        shutdown(&supervisor);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn serial_multi_gpu_applies_remember_each_gpu_state() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-multi-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("active-runtime.json");
        let _state_guard = StateEnvGuard::new(&path);
        let _inert_guard = StateEnvGuard::named("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1");
        let sup = Mutex::new(Supervisor::new());

        apply_runtime_spec(&sup, RuntimeSpec::test_static("GPU-A", "profile-a")).unwrap();
        apply_runtime_spec(&sup, RuntimeSpec::test_static("GPU-B", "profile-b")).unwrap();

        let running = guard(&sup);
        assert_eq!(running.profile.as_ref().unwrap().spec.gpu.uuid, "GPU-B");
        assert_eq!(
            running
                .last_applied_specs
                .get("GPU-A")
                .unwrap()
                .active_profile_id(),
            "profile-a"
        );
        assert_eq!(
            running
                .last_applied_specs
                .get("GPU-B")
                .unwrap()
                .active_profile_id(),
            "profile-b"
        );
        drop(running);
        shutdown(&sup);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn daemon_restart_preserves_inactive_gpu_session_restore_state() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-session-history-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("active-runtime.json");
        let _state_guard = StateEnvGuard::new(&path);
        let _inert_guard = StateEnvGuard::named("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1");
        let _identity_guard = StateEnvGuard::named(
            "PENGUIN_BURNERD_TEST_GPU_IDENTITIES",
            r#"[{"index":0,"uuid":"GPU-A"},{"index":1,"uuid":"GPU-B"}]"#,
        );
        let first = Mutex::new(Supervisor::new());

        apply_runtime_spec(&first, RuntimeSpec::test_static("GPU-A", "profile-a")).unwrap();
        apply_runtime_spec(&first, RuntimeSpec::test_static("GPU-B", "profile-b")).unwrap();
        shutdown(&first);

        let restarted = Arc::new(Mutex::new(Supervisor::new()));
        start_autostart_if_configured(&restarted);
        start_game_runtime_profile(
            &restarted,
            RuntimeSpec::test_static("GPU-A", "game-profile-a"),
            std::process::id(),
            "42".to_string(),
        )
        .unwrap();

        let running = guard(&restarted);
        assert_eq!(
            running
                .game_runtime
                .restore_spec
                .as_ref()
                .expect("inactive GPU session history")
                .active_profile_id(),
            "profile-a"
        );
        drop(running);
        shutdown(&restarted);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn failed_cross_gpu_apply_restores_target_to_stock_and_previous_engine() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-multi-fail-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("active-runtime.json");
        let _state_guard = StateEnvGuard::new(&path);
        let _inert_guard = StateEnvGuard::named("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1");
        let _failure_guard =
            StateEnvGuard::named("PENGUIN_BURNERD_TEST_FAIL_PROFILE_ID", "profile-that-fails");
        let sup = Mutex::new(Supervisor::new());
        apply_runtime_spec(&sup, RuntimeSpec::test_static("GPU-A", "profile-a")).unwrap();

        let error = apply_runtime_spec(
            &sup,
            RuntimeSpec::test_static("GPU-B", "profile-that-fails"),
        )
        .unwrap_err();

        assert!(error.contains("injected runtime profile initial apply failure"));
        let running = guard(&sup);
        assert_eq!(running.profile.as_ref().unwrap().spec.gpu.uuid, "GPU-A");
        assert_eq!(
            running.last_applied_specs.get("GPU-B").unwrap().mode_name(),
            "stock"
        );
        drop(running);
        shutdown(&sup);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn relaunch_reclaims_exited_owner_before_monitor_runs() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = tempfile::tempdir().unwrap();
        let _state_guard = StateEnvGuard::new(&dir.path().join("active-runtime.json"));
        let _inert_guard = StateEnvGuard::named("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1");
        let sup = Mutex::new(Supervisor::new());
        apply_runtime_spec(&sup, RuntimeSpec::test_static("GPU-A", "standing")).unwrap();
        let mut first = std::process::Command::new("sleep")
            .arg("60")
            .spawn()
            .unwrap();
        start_game_runtime_profile(
            &sup,
            RuntimeSpec::test_stock("GPU-B"),
            first.id(),
            "heroic:first".into(),
        )
        .unwrap();
        let refused = start_game_runtime_profile(
            &sup,
            RuntimeSpec::test_stock("GPU-C"),
            std::process::id(),
            "lutris:second".into(),
        )
        .unwrap();
        assert_eq!(refused["reason"], "first-game-runtime-active");
        first.kill().unwrap();
        first.wait().unwrap();
        // Deliberately no monitor and no grace-period sleep: launch admission
        // must consume the already-ready exit handle under its own lock.
        let started = start_game_runtime_profile(
            &sup,
            RuntimeSpec::test_stock("GPU-C"),
            std::process::id(),
            "lutris:second".into(),
        )
        .unwrap();
        assert_eq!(started["started"], true);
        let running = guard(&sup);
        assert_eq!(running.game_runtime.watches.len(), 1);
        assert_eq!(
            running
                .game_runtime
                .standing_spec
                .as_ref()
                .unwrap()
                .active_profile_id(),
            "standing"
        );
        assert_eq!(
            running.last_applied_specs.get("GPU-B").unwrap().mode_name(),
            "stock"
        );
        drop(running);
        shutdown(&sup);
    }

    #[test]
    fn game_profiles_preserve_target_gpu_fan_policy_on_launch_and_reapply() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        // Standing fan, target GPU, remembered target, Stock, explicit fan,
        // expected game fan. Never copy another GPU's curve or enable a fan
        // policy the user has not applied.
        for (enabled, target, remembered, stock, explicit, expected) in [
            (true, "GPU-A", false, false, false, true),
            (false, "GPU-A", false, false, false, false),
            (true, "GPU-B", false, false, false, false),
            (false, "GPU-B", true, false, false, true),
            (true, "GPU-A", false, true, false, false),
            (false, "GPU-A", false, false, true, true),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let state_path = dir.path().join("active-runtime.json");
            let _state_guard = StateEnvGuard::new(&state_path);
            let _inert_guard = StateEnvGuard::named("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1");
            let sup = Mutex::new(Supervisor::new());
            let mut standing = RuntimeSpec::test_static("GPU-A", "standing");
            standing.fan.enabled = enabled;
            standing.fan.config.hysteresis_c = 4.0;
            if remembered {
                let mut other = RuntimeSpec::test_static("GPU-B", "other");
                other.fan = standing.fan.clone();
                other.fan.enabled = true;
                apply_runtime_spec(&sup, other).unwrap();
            }
            apply_runtime_spec(&sup, standing.clone()).unwrap();
            let persisted = fs::read(&state_path).unwrap();
            for reapply in [false, true] {
                let mut game = if stock {
                    RuntimeSpec::test_stock(target)
                } else {
                    RuntimeSpec::test_static(target, if reapply { "updated" } else { "game" })
                };
                // Only the first request explicitly enables a fan; the live
                // update must retain its complete config without another flag.
                game.fan.enabled = explicit && !reapply;
                if explicit {
                    game.fan.config.hysteresis_c = 4.0;
                }
                start_game_runtime_profile(
                    &sup,
                    game,
                    std::process::id(),
                    "heroic:fan-test".into(),
                )
                .unwrap();
                let running = guard(&sup);
                let fan = &running.profile.as_ref().unwrap().spec.fan;
                assert_eq!(fan.enabled, expected, "target={target}, reapply={reapply}");
                if expected {
                    assert_eq!(fan.config.hysteresis_c, 4.0);
                }
                assert_eq!(fs::read(&state_path).unwrap(), persisted);
            }
            if !stock && !explicit {
                start_game_runtime_profile(
                    &sup,
                    RuntimeSpec::test_stock(target),
                    std::process::id(),
                    "heroic:fan-test".into(),
                )
                .unwrap();
                assert!(!guard(&sup).profile.as_ref().unwrap().spec.fan.enabled);
                start_game_runtime_profile(
                    &sup,
                    RuntimeSpec::test_static(target, "back-from-stock"),
                    std::process::id(),
                    "heroic:fan-test".into(),
                )
                .unwrap();
                let running = guard(&sup);
                let fan = &running.profile.as_ref().unwrap().spec.fan;
                assert_eq!(fan.enabled, expected, "Stock round trip, target={target}");
                if expected {
                    assert_eq!(fan.config.hysteresis_c, 4.0);
                }
            }
            {
                let mut running = guard(&sup);
                let watch = running
                    .game_runtime
                    .watches
                    .get_mut(&std::process::id())
                    .unwrap();
                watch.lifetime_fd = None;
                watch.process_start_time = Some(0);
            }
            reap_game_watches(&sup);
            {
                let running = guard(&sup);
                let restored = &running.profile.as_ref().unwrap().spec;
                assert_eq!(
                    serde_json::to_value(restored).unwrap(),
                    serde_json::to_value(&standing).unwrap()
                );
            }
            shutdown(&sup);
        }
    }

    #[test]
    fn explicit_fan_disable_during_game_survives_next_game_update() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = tempfile::tempdir().unwrap();
        let _state_guard = StateEnvGuard::new(&dir.path().join("active-runtime.json"));
        let _inert_guard = StateEnvGuard::named("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1");
        let sup = Mutex::new(Supervisor::new());
        let mut standing = RuntimeSpec::test_static("GPU-A", "standing");
        standing.fan.enabled = true;
        apply_runtime_spec(&sup, standing.clone()).unwrap();
        start_game_runtime_profile(
            &sup,
            RuntimeSpec::test_static("GPU-A", "game"),
            std::process::id(),
            "42".into(),
        )
        .unwrap();
        standing.fan.enabled = false;
        apply_runtime_spec(&sup, standing).unwrap();
        start_game_runtime_profile(
            &sup,
            RuntimeSpec::test_static("GPU-A", "updated"),
            std::process::id(),
            "42".into(),
        )
        .unwrap();
        assert!(!guard(&sup).profile.as_ref().unwrap().spec.fan.enabled);
        shutdown(&sup);
    }

    #[test]
    fn cross_gpu_game_override_remembers_target_and_restores_standing_gpu() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-game-gpu-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("active-runtime.json");
        let _state_guard = StateEnvGuard::new(&path);
        let _inert_guard = StateEnvGuard::named("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1");
        let sup = Mutex::new(Supervisor::new());

        apply_runtime_spec(&sup, RuntimeSpec::test_static("GPU-B", "profile-b")).unwrap();
        apply_runtime_spec(&sup, RuntimeSpec::test_static("GPU-A", "profile-a")).unwrap();
        start_game_runtime_profile(
            &sup,
            RuntimeSpec::test_static("GPU-B", "game-profile-b"),
            std::process::id(),
            "42".to_string(),
        )
        .unwrap();

        {
            let mut running = guard(&sup);
            assert_eq!(
                running
                    .game_runtime
                    .restore_spec
                    .as_ref()
                    .unwrap()
                    .active_profile_id(),
                "profile-b"
            );
            let watch = running
                .game_runtime
                .watches
                .get_mut(&std::process::id())
                .unwrap();
            watch.lifetime_fd = None;
            watch.process_start_time = Some(0);
        }
        reap_game_watches(&sup);

        let running = guard(&sup);
        assert_eq!(running.profile.as_ref().unwrap().spec.gpu.uuid, "GPU-A");
        assert_eq!(
            running.profile.as_ref().unwrap().spec.active_profile_id(),
            "profile-a"
        );
        assert!(running.game_runtime.watches.is_empty());
        assert!(running.game_runtime.restore_spec.is_none());
        drop(running);
        shutdown(&sup);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn failed_cross_gpu_game_apply_restores_game_gpu_before_standing() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-game-fail-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("active-runtime.json");
        let _state_guard = StateEnvGuard::new(&path);
        let _inert_guard = StateEnvGuard::named("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1");
        let _failure_guard =
            StateEnvGuard::named("PENGUIN_BURNERD_TEST_FAIL_PROFILE_ID", "game-that-fails");
        let sup = Mutex::new(Supervisor::new());
        apply_runtime_spec(&sup, RuntimeSpec::test_static("GPU-A", "profile-a")).unwrap();

        let error = start_game_runtime_profile(
            &sup,
            RuntimeSpec::test_static("GPU-B", "game-that-fails"),
            std::process::id(),
            "42".to_string(),
        )
        .unwrap_err();

        assert!(error.contains("injected runtime profile initial apply failure"));
        let running = guard(&sup);
        assert_eq!(running.profile.as_ref().unwrap().spec.gpu.uuid, "GPU-A");
        assert_eq!(
            running.last_applied_specs.get("GPU-B").unwrap().mode_name(),
            "stock"
        );
        drop(running);
        shutdown(&sup);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn boot_replay_stock_recovers_failed_gpu_then_continues() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-boot-fail-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let active_path = dir.join("active-runtime.json");
        let boot_path = dir.join("boot-runtime.json");
        let _active_guard = StateEnvGuard::new(&active_path);
        let _boot_guard = StateEnvGuard::named(BOOT_STATE_FILE_ENV, &boot_path);
        let _inert_guard = StateEnvGuard::named("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1");
        let _identity_guard = StateEnvGuard::named(
            "PENGUIN_BURNERD_TEST_GPU_IDENTITIES",
            r#"[{"index":0,"uuid":"GPU-A"},{"index":1,"uuid":"GPU-B"}]"#,
        );
        let _failure_guard =
            StateEnvGuard::named("PENGUIN_BURNERD_TEST_FAIL_PROFILE_ID", "profile-that-fails");
        let mut gpu_a = RuntimeSpec::test_static("GPU-A", "profile-that-fails");
        gpu_a.gpu.index_at_resolution = 0;
        let mut gpu_b = RuntimeSpec::test_static("GPU-B", "profile-b");
        gpu_b.gpu.index_at_resolution = 1;
        persist_json(
            &boot_path,
            &BootRuntimeSet {
                format_version: BOOT_RUNTIME_SET_FORMAT_VERSION,
                active_gpu_uuid: "GPU-B".to_string(),
                main_gpu_uuid: String::new(),
                specs: vec![gpu_a, gpu_b],
            },
        )
        .unwrap();
        let supervisor = Arc::new(Mutex::new(Supervisor::new()));

        start_autostart_if_configured(&supervisor);

        let running = guard(&supervisor);
        assert_eq!(running.profile.as_ref().unwrap().spec.gpu.uuid, "GPU-B");
        assert_eq!(
            running.boot_replay,
            vec![
                serde_json::json!({
                    "gpu_uuid": "GPU-A",
                    "gpu_index": 0,
                    "outcome": "stock-fallback",
                    "error": "injected runtime profile initial apply failure",
                }),
                serde_json::json!({
                    "gpu_uuid": "GPU-B",
                    "gpu_index": 1,
                    "outcome": "active",
                }),
            ]
        );
        drop(running);
        shutdown(&supervisor);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn failed_standing_apply_during_game_preserves_persisted_standing() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-game-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("active-runtime.json");
        let _state_guard = StateEnvGuard::new(&path);
        let _inert_guard = StateEnvGuard::named("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1");
        let _failure_guard =
            StateEnvGuard::named("PENGUIN_BURNERD_TEST_FAIL_PROFILE_ID", "profile-that-fails");

        let standing = RuntimeSpec::test_stock("GPU-standing");
        persist_runtime_spec(&path, &standing).unwrap();
        let game = RuntimeSpec::test_stock("GPU-game");
        let game_engine = profile::start(game.clone()).unwrap();
        let sup = Mutex::new(Supervisor::new());
        {
            let mut running = guard(&sup);
            running.profile = Some(ProfileJob {
                engine: game_engine,
                spec: game.clone(),
            });
            running.game_runtime.standing_spec = Some(standing.clone());
            running.game_runtime.override_active = true;
            running.game_runtime.watches.insert(
                std::process::id(),
                GameWatch {
                    app_id: "42".to_string(),
                    lifetime_fd: None,
                    process_start_time: process_start_time(std::process::id()),
                },
            );
        }

        let error = apply_runtime_spec(
            &sup,
            RuntimeSpec::test_static("GPU-failed-standing", "profile-that-fails"),
        )
        .unwrap_err();
        assert!(
            error.contains("injected runtime profile initial apply failure"),
            "{error}"
        );
        let persisted = load_runtime_spec(&path).unwrap().unwrap();
        assert_eq!(persisted.gpu.uuid, "GPU-standing");
        {
            let running = guard(&sup);
            assert_eq!(running.profile.as_ref().unwrap().spec.gpu.uuid, "GPU-game");
            assert!(running.game_runtime.override_active);
            assert_eq!(
                running
                    .game_runtime
                    .standing_spec
                    .as_ref()
                    .unwrap()
                    .gpu
                    .uuid,
                "GPU-standing"
            );
        }
        shutdown(&sup);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn legacy_argv_runtime_state_is_rejected() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-b-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("active-runtime.json");
        let _guard = StateEnvGuard::new(&path);

        fs::write(&path, r#"{"argv":["--gpu-index","0"]}"#).unwrap();
        let error = load_runtime_spec(&active_state_file_path()).unwrap_err();
        assert!(error.contains("invalid runtime state"), "{error}");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn malformed_runtime_state_is_rejected() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-c-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("active-runtime.json");
        let _guard = StateEnvGuard::new(&path);

        fs::write(&path, "{not json").unwrap();
        assert!(load_runtime_spec(&active_state_file_path()).is_err());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn autostart_apply_failure_recovers_to_stock_for_current_boot() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-d-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let active_path = dir.join("active-runtime.json");
        let boot_path = dir.join("boot-runtime.json");
        let _active_guard = StateEnvGuard::new(&active_path);
        let _boot_guard = StateEnvGuard::named(BOOT_STATE_FILE_ENV, &boot_path);
        let _inert_guard = StateEnvGuard::named("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1");
        let _failure_guard =
            StateEnvGuard::named("PENGUIN_BURNERD_TEST_FAIL_PROFILE_ID", "profile-that-fails");

        let requested = RuntimeSpec::test_static("GPU-recovery", "profile-that-fails");
        persist_runtime_spec(&active_path, &requested).unwrap();
        let supervisor = Arc::new(Mutex::new(Supervisor::new()));

        start_autostart_if_configured(&supervisor);

        let running = guard(&supervisor);
        let recovered = &running.profile.as_ref().expect("stock fallback").spec;
        assert_eq!(recovered.mode_name(), "stock");
        assert_eq!(recovered.gpu.uuid, "GPU-recovery");
        drop(running);
        let persisted = load_runtime_spec(&active_path).unwrap().unwrap();
        assert_eq!(persisted.mode_name(), "stock");
        shutdown(&supervisor);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn raw_gpu_writes_are_refused_while_a_profile_engine_runs() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let _inert_guard = StateEnvGuard::named("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1");

        let sup = Mutex::new(Supervisor::new());
        // Idle: no engine -> not running, and the gate does not fire.
        assert!(!profile_engine_running(&sup));

        // Install a running (inert) profile engine.
        let spec = RuntimeSpec::test_stock("GPU-gate");
        let engine = profile::start(spec.clone()).unwrap();
        guard(&sup).profile = Some(ProfileJob { engine, spec });
        assert!(profile_engine_running(&sup));

        // A raw GPU WRITE is refused with a clear reason (not two writers
        // racing the GPU); the message names the method.
        let err = crate::api::handle_request(
            &sup,
            &serde_json::json!({"method": "gpu_reset_fans", "gpu_index": 0}),
        )
        .unwrap_err();
        assert!(err.contains("gpu_reset_fans"), "message: {err}");
        assert!(err.contains("runtime profile is active"), "message: {err}");

        shutdown(&sup);
    }
}
