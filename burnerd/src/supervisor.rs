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
use std::io::Write;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::path::PathBuf;
use std::process::{Child, ExitStatus};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

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
const BOOT_RUNTIME_STATE_PATH: &str = "/var/lib/penguin-burner/boot-runtime.json";
const ENGINE_STOP_TIMEOUT: Duration = Duration::from_secs(10);
const GAME_RESTORE_GRACE: Duration = Duration::from_secs(3);
const GAME_WATCH_INTERVAL: Duration = Duration::from_millis(250);
const TEST_GAME_RESTORE_GRACE: Duration = Duration::from_millis(50);
const TEST_GAME_WATCH_INTERVAL: Duration = Duration::from_millis(20);

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
    pidfd: Option<OwnedFd>,
    process_start_time: Option<u64>,
    exited_at: Option<Instant>,
}

#[derive(Default)]
struct GameRuntimeState {
    watches: BTreeMap<u32, GameWatch>,
    standing_spec: Option<RuntimeSpec>,
    override_active: bool,
}

pub struct Supervisor {
    profile: Option<ProfileJob>,
    child: Option<Arc<ChildJob>>,
    child_generation: u64,
    stop_timeout: Duration,
    game_runtime: GameRuntimeState,
}

impl Default for Supervisor {
    fn default() -> Self {
        Self::new()
    }
}

impl Supervisor {
    pub fn new() -> Self {
        Supervisor {
            profile: None,
            child: None,
            child_generation: 0,
            stop_timeout: ENGINE_STOP_TIMEOUT,
            game_runtime: GameRuntimeState::default(),
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

fn game_restore_grace() -> Duration {
    if test_timings_enabled() {
        TEST_GAME_RESTORE_GRACE
    } else {
        GAME_RESTORE_GRACE
    }
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
        let pidfd = if raw_fd >= 0 {
            // SAFETY: pidfd_open returned a new owned descriptor on success.
            Some(unsafe { OwnedFd::from_raw_fd(raw_fd as i32) })
        } else {
            None
        };
        let process_start_time = if pidfd.is_none() {
            process_start_time(pid)
        } else {
            None
        };
        if pidfd.is_none() && process_start_time.is_none() {
            return Err(format!("watch_pid {pid} is not a running process"));
        }
        Ok(Self {
            app_id,
            pidfd,
            process_start_time,
            exited_at: None,
        })
    }

    fn running(&self, pid: u32) -> bool {
        if let Some(pidfd) = &self.pidfd {
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
    let weak = Arc::downgrade(sup);
    thread::Builder::new()
        .name("penguin-burner-game-watch".to_string())
        .spawn(move || loop {
            thread::sleep(game_watch_interval());
            let Some(sup) = weak.upgrade() else {
                return;
            };
            reap_game_watches(&sup);
        })
        .expect("spawn game watch thread");
}

fn reap_game_watches(sup: &Mutex<Supervisor>) {
    let now = Instant::now();
    let grace = game_restore_grace();
    let mut supervisor = guard(sup);
    if supervisor.game_runtime.watches.is_empty() {
        return;
    }
    for (&pid, watch) in &mut supervisor.game_runtime.watches {
        if watch.exited_at.is_none() && !watch.running(pid) {
            watch.exited_at = Some(now);
            // Fires exactly once per pid: archive the game's telemetry ring.
            crate::frame_history::session_end(pid, &watch.app_id);
        }
    }
    supervisor.game_runtime.watches.retain(|_, watch| {
        watch
            .exited_at
            .is_none_or(|exited_at| now.duration_since(exited_at) < grace)
    });
    if !supervisor.game_runtime.watches.is_empty() {
        return;
    }

    let should_restore = supervisor.game_runtime.override_active;
    supervisor.game_runtime.override_active = false;
    let standing_spec = supervisor.game_runtime.standing_spec.take();
    if !should_restore || supervisor.child_running_kind().is_some() {
        return;
    }
    if let Err(error) = supervisor.stop_engine_for_child("the standing runtime after game exit") {
        logging::error(&error);
        return;
    }
    let Some(spec) = standing_spec else {
        return;
    };
    match profile::start(spec.clone()) {
        Ok(engine) => {
            supervisor.profile = Some(ProfileJob { engine, spec });
        }
        Err(error) => {
            let (message, failed_engine) = error.into_parts();
            logging::error(&format!(
                "failed to restore standing runtime after game exit: {message}"
            ));
            if let Some(engine) = failed_engine {
                supervisor.profile = Some(ProfileJob { engine, spec });
            }
        }
    }
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

fn boot_state_file_path() -> PathBuf {
    if let Some(path) = env::var_os(BOOT_STATE_FILE_ENV) {
        if !path.is_empty() {
            return PathBuf::from(path);
        }
    }
    PathBuf::from(BOOT_RUNTIME_STATE_PATH)
}

fn persist_runtime_spec(path: &PathBuf, spec: &RuntimeSpec) -> Result<(), String> {
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
    serde_json::to_writer(&mut temp, spec)
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

fn persist_active_runtime(spec: &RuntimeSpec) {
    if let Err(error) = persist_runtime_spec(&active_state_file_path(), spec) {
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
        .with_energy_savings(energy_savings);
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
        .with_energy_savings(energy_savings);
    }

    StatusResult::new("idle", None)
        .with_game_runtime(game_runtime)
        .with_energy_savings(energy_savings)
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
    spec: RuntimeSpec,
    watch_pid: u32,
    app_id: String,
) -> Result<Value, String> {
    spec.validate()?;
    let watch = GameWatch::open(watch_pid, app_id)?;
    let profile_id = spec.active_profile_id();
    let runtime_mode = spec.mode_name().to_string();
    let gpu_uuid = spec.gpu.uuid.clone();

    let mut supervisor = guard(sup);
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
    supervisor.stop_engine_for_child("a game runtime profile")?;

    match profile::start(spec.clone()) {
        Ok(engine) => {
            supervisor.profile = Some(ProfileJob {
                engine,
                spec: spec.clone(),
            });
            if first_watch {
                supervisor.game_runtime.standing_spec = standing_spec;
            }
            supervisor.game_runtime.watches.insert(watch_pid, watch);
            if spec.game_telemetry {
                if let Some(watch) = supervisor.game_runtime.watches.get(&watch_pid) {
                    crate::frame_history::session_start(watch_pid, &watch.app_id);
                }
            }
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
                "failed to apply game runtime spec: {apply_error}; {recovery}"
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
            drop(supervisor);
            persist_active_runtime(&spec);
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
            let mut recovered_spec = None;
            let mut recovery_engine_wedged = false;

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

            drop(supervisor);
            if !preserve_persisted_standing {
                if let Some(recovered) = recovered_spec {
                    persist_active_runtime(&recovered);
                } else if !recovery_engine_wedged {
                    clear_active_runtime();
                }
            }
            Err(format!(
                "failed to apply runtime spec: {apply_error}; {recovery}"
            ))
        }
    }
}

pub fn stop_runtime_profile(sup: &Mutex<Supervisor>) -> Result<StopResult, String> {
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
        start_autostart_if_configured(sup);
    }
}

pub fn set_boot_runtime_spec(spec: RuntimeSpec) -> Result<Value, String> {
    spec.validate()?;
    persist_runtime_spec(&boot_state_file_path(), &spec)?;
    Ok(serde_json::json!({
        "saved": true,
        "profile_id": spec.active_profile_id(),
        "runtime_mode": spec.mode_name(),
        "gpu_uuid": spec.gpu.uuid,
    }))
}

pub fn clear_boot_runtime_spec() -> Result<Value, String> {
    let path = boot_state_file_path();
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
    Ok(serde_json::json!({"cleared": cleared}))
}

pub fn boot_runtime_spec_summary() -> Result<Value, String> {
    let Some(spec) = load_runtime_spec(&boot_state_file_path())? else {
        return Ok(serde_json::json!({"configured": false}));
    };
    Ok(serde_json::json!({
        "configured": true,
        "profile_id": spec.active_profile_id(),
        "runtime_mode": spec.mode_name(),
        "gpu_uuid": spec.gpu.uuid,
        "silent_fan_curve": spec.fan.enabled,
    }))
}

/// Recover the current-session runtime first, then the explicit boot runtime.
/// The `/run` state naturally disappears at reboot; boot persistence is a
/// separate opt-in API instead of an accidental consequence of applying once.
/// Once a valid spec reaches the GPU, failure recovery stays on that same GPU:
/// start its stock fallback instead of trying an older spec that may identify a
/// different GPU. Persisting the stock fallback shadows the broken intent only
/// for the current boot; the explicit boot spec is retried after reboot.
pub fn start_autostart_if_configured(sup: &Arc<Mutex<Supervisor>>) {
    let mut supervisor = guard(sup);
    if supervisor.profile.is_some() {
        return;
    }

    let candidates = [
        ("active-session", active_state_file_path()),
        ("boot", boot_state_file_path()),
    ];
    for (source, path) in candidates {
        let spec = match load_runtime_spec(&path) {
            Ok(Some(spec)) => spec,
            Ok(None) => continue,
            Err(error) => {
                logging::error(&error);
                continue;
            }
        };
        match profile::start(spec.clone()) {
            Ok(engine) => {
                logging::info(&format!(
                    "recovered {source} runtime: mode={} profile={} gpu={}",
                    spec.mode_name(),
                    spec.active_profile_id(),
                    spec.gpu.uuid
                ));
                supervisor.profile = Some(ProfileJob {
                    engine,
                    spec: spec.clone(),
                });
                if !supervisor.game_runtime.watches.is_empty() {
                    supervisor.game_runtime.standing_spec = Some(spec.clone());
                    supervisor.game_runtime.override_active = false;
                }
                drop(supervisor);
                persist_active_runtime(&spec);
                return;
            }
            Err(error) => {
                let (message, failed_engine) = error.into_parts();
                logging::error(&format!(
                    "failed to recover {source} runtime from {}: {message}",
                    path.display()
                ));
                if let Some(engine) = failed_engine {
                    supervisor.profile = Some(ProfileJob { engine, spec });
                    return;
                }

                if spec.mode_name() == "stock" {
                    return;
                }
                let stock = spec.stock_fallback();
                match profile::start(stock.clone()) {
                    Ok(engine) => {
                        logging::warn(&format!(
                            "recovered failed {source} runtime with stock fallback for gpu={}",
                            stock.gpu.uuid
                        ));
                        supervisor.profile = Some(ProfileJob {
                            engine,
                            spec: stock.clone(),
                        });
                        if !supervisor.game_runtime.watches.is_empty() {
                            supervisor.game_runtime.standing_spec = Some(stock.clone());
                            supervisor.game_runtime.override_active = false;
                        }
                        drop(supervisor);
                        persist_active_runtime(&stock);
                    }
                    Err(stock_error) => {
                        let (stock_message, failed_engine) = stock_error.into_parts();
                        logging::error(&format!(
                            "failed to apply stock fallback after {source} recovery failure: {stock_message}"
                        ));
                        if let Some(engine) = failed_engine {
                            supervisor.profile = Some(ProfileJob {
                                engine,
                                spec: stock,
                            });
                        }
                    }
                }
                return;
            }
        }
    }
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
                    pidfd: None,
                    process_start_time: process_start_time(std::process::id()),
                    exited_at: None,
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
