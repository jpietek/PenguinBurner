//! Streaming-child management for the Auto-UV scan and profile verification:
//! spawn the (unchanged, Python) CLI child, relay its merged stdout/stderr as
//! JSON-line frames, and drive the stop protocol + detached kill ladder. Scan
//! behavior is parity with `stream_auto_uv_scan` (spec 01 §4.4); verification
//! reuses the same machinery with its own argv whitelist and stop marker.
//!
//! Since milestone B every GPU write routes through the daemon socket, so the
//! Python child no longer needs root: when the daemon runs as root it drops the
//! child to the desktop user (resolved from the identity env the systemd unit
//! carries) before exec — Python never runs as root.

use std::env;
use std::ffi::{CString, OsString};
use std::fs::File;
use std::io::{self, BufRead, BufReader, Read};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use nix::fcntl::OFlag;
use nix::unistd::pipe2;
use nix::unistd::Gid;
use serde_json::Value;

use crate::api::{write_json_line, StreamError, StreamFinished, StreamLine, StreamStarted};
use crate::argvspec;
use crate::logging;
use crate::paths;
use crate::supervisor::{self, ChildJob, ChildKind, ChildStart, Supervisor};

/// Sandbox/elevation state must never leak into the de-rooted worker. In
/// particular, a Flatpak child launched from a system daemon must be a plain
/// host Python process, not something that can accidentally re-enter bwrap via
/// inherited Flatpak or dynamic-loader state.
const CHILD_ENV_REMOVE: &[&str] = &[
    "FLATPAK_ID",
    "FLATPAK_SANDBOX_DIR",
    "container",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    "NOTIFY_SOCKET",
    "LISTEN_FDS",
    "LISTEN_FDNAMES",
    "LISTEN_PID",
];

// Linux close_range(2): mark every non-stdio descriptor close-on-exec without
// closing Rust's exec-error pipe before `execve` has succeeded.
const CLOSE_RANGE_CLOEXEC_FLAG: libc::c_uint = 1 << 2;

/// Kill-ladder timings. Shrunk by the undocumented `PENGUIN_BURNERD_TEST_TIMINGS`
/// env (integration tests only) so the ladder can be exercised quickly.
struct Timings {
    kill_after: Duration,
    term_grace: Duration,
    kill_grace: Duration,
}

fn timings() -> Timings {
    if env::var_os("PENGUIN_BURNERD_TEST_TIMINGS").is_some_and(|v| !v.is_empty()) {
        Timings {
            kill_after: Duration::from_secs(1),
            term_grace: Duration::from_millis(500),
            kill_grace: Duration::from_millis(500),
        }
    } else {
        Timings {
            kill_after: Duration::from_secs(30),
            term_grace: Duration::from_secs(5),
            kill_grace: Duration::from_secs(5),
        }
    }
}

/// The launch-failure label for each child kind.
fn launch_label(kind: ChildKind) -> &'static str {
    match kind {
        ChildKind::Scan => "Auto-UV scan",
        ChildKind::Verify => "profile verification",
    }
}

/// Handle a `start_auto_uv_scan` request on `writer` (the connection stream).
pub fn run_scan(sup: &Arc<Mutex<Supervisor>>, options: &Value, writer: &mut UnixStream) {
    let option_args = match argvspec::auto_uv_option_args(options) {
        Ok(args) => args,
        Err(err) => {
            write_json_line(writer, &StreamError::new(err));
            return;
        }
    };
    let mut argv = vec![
        "--auto-uv-voltage-scan".to_string(),
        "--json-events".to_string(),
        "--auto-uv-require-final-choice".to_string(),
    ];
    argv.extend(option_args);
    run_child(sup, ChildKind::Scan, argv, writer);
}

/// Handle a `start_profile_verification` request on `writer`. The argv mirrors
/// `runtime/daemon_commands.py::profile_verify_command` (`--stability-test` prefix, then
/// the whitelisted options in its exact order); the stop-request file is owned
/// by the daemon and always appended, pointing at the effective user's config
/// dir like the Qt `VerifyController` did.
pub fn run_verification(sup: &Arc<Mutex<Supervisor>>, options: &Value, writer: &mut UnixStream) {
    let option_args = match argvspec::profile_verify_option_args(options) {
        Ok(args) => args,
        Err(err) => {
            write_json_line(writer, &StreamError::new(err));
            return;
        }
    };
    let mut argv = vec!["--stability-test".to_string()];
    argv.extend(option_args);
    argv.push("--stability-stop-request-file".to_string());
    argv.push(
        paths::profile_verify_stop_request_path()
            .display()
            .to_string(),
    );
    run_child(sup, ChildKind::Verify, argv, writer);
}

/// Start + stream one child. This consumes the connection: it streams frames
/// until the child finishes or the client disconnects, then returns.
fn run_child(
    sup: &Arc<Mutex<Supervisor>>,
    kind: ChildKind,
    argv: Vec<String>,
    writer: &mut UnixStream,
) {
    let program_file = supervisor::daemon_program_file();
    // Resolve the interpreter and the privilege-drop plan here, OUTSIDE
    // `begin_child`'s critical section: the spawn closure runs while the
    // supervisor mutex is held, so env reads, passwd/NSS lookups, and
    // filesystem walks must not happen under the lock.
    let python = child_python(&program_file);
    let drop_plan = match child_drop_plan() {
        Ok(plan) => plan,
        Err(err) => {
            // Identity env is present but unusable: refuse to launch rather
            // than silently running Python as root again (fail closed).
            write_json_line(
                writer,
                &StreamError::new(format!("failed to launch {}: {err}", launch_label(kind))),
            );
            return;
        }
    };
    if let Some(plan) = &drop_plan {
        logging::info(&format!(
            "spawning {} child as {} (uid {} gid {})",
            launch_label(kind),
            plan.user,
            plan.uid,
            plan.gid
        ));
        // The child owns its config. Reject symlinked/root-owned paths instead of
        // letting the root daemon repair or traverse user-controlled directories.
        if let Err(err) = paths::validate_config_tree_for(&plan.config_home, plan.uid) {
            write_json_line(
                writer,
                &StreamError::new(format!(
                    "failed to launch {}: unsafe desktop config directory: {err}",
                    launch_label(kind)
                )),
            );
            return;
        }
    }
    if let Err(err) = validate_child_program_file(&program_file) {
        write_json_line(
            writer,
            &StreamError::new(format!("failed to launch {}: {err}", launch_label(kind))),
        );
        return;
    }

    // The whole check-clear-stop-spawn-install runs atomically under the lock.
    let (job, reader) = match supervisor::begin_child(sup, kind, argv, |argv| {
        spawn_child(&python, &program_file, argv, drop_plan.as_ref())
    }) {
        ChildStart::Started(job, reader) => (job, reader),
        ChildStart::Refused(err) => {
            write_json_line(writer, &StreamError::new(err));
            return;
        }
        ChildStart::ClearFailed(err) => {
            // The scan text is byte-exact with the Python daemon ("Auto-UV",
            // not "Auto-UV scan"); the verification text is new.
            let label = match kind {
                ChildKind::Scan => "Auto-UV",
                ChildKind::Verify => "profile verification",
            };
            write_json_line(
                writer,
                &StreamError::new(format!("failed to clear stale {label} stop request: {err}")),
            );
            return;
        }
        ChildStart::SpawnFailed(err) => {
            // `begin_child` already stopped the active profile before spawn.
            // A permission/path failure must not leave the user's standing
            // runtime silently disabled (Mobile/Unknown: the watcher owns
            // the restart so a sleeping GPU is not force-attached).
            supervisor::resume_autostart_after_child(sup);
            write_json_line(
                writer,
                &StreamError::new(format!("failed to launch {}: {err}", launch_label(kind))),
            );
            return;
        }
    };

    // If the client is already gone by the time we announce "started", treat it
    // as a mid-stream disconnect (abort + monitor) rather than leaking the child.
    if !write_json_line(writer, &StreamStarted::new(job.proc.pid())) {
        disconnect_cleanup(sup, job, reader);
        return;
    }

    stream_stdout(sup, job, reader, writer);
}

/// Read the child's merged stdout, relaying each line as a `line` frame. On EOF,
/// emit `finished`; on client disconnect, run the abort + detached kill ladder.
fn stream_stdout(
    sup: &Arc<Mutex<Supervisor>>,
    job: Arc<ChildJob>,
    reader: File,
    writer: &mut UnixStream,
) {
    let mut reader = BufReader::new(reader);
    let mut client_gone = false;
    let mut buffer = Vec::new();
    loop {
        buffer.clear();
        match reader.read_until(b'\n', &mut buffer) {
            Ok(0) => break, // EOF
            Ok(_) => {
                // decode utf-8 with replacement (== Python errors="replace"),
                // keeping the trailing '\n' exactly as Python does.
                let line = String::from_utf8_lossy(&buffer).into_owned();
                if !write_json_line(writer, &StreamLine::new(line)) {
                    client_gone = true;
                    break;
                }
            }
            Err(_) => break, // read error → treat as EOF
        }
    }

    let mut completed = false;
    if !client_gone {
        let exit_code = job.proc.wait();
        completed = true;
        // A write failure here does not change `completed` (parity): finished has
        // already been decided.
        write_json_line(writer, &StreamFinished::new(exit_code));
    }

    // finally: `completed or poll() is not None` → finish; else disconnect.
    if completed || job.proc.poll().is_some() {
        drop(reader);
        supervisor::finish_child(sup, &job);
    } else {
        disconnect_cleanup(sup, job, reader.into_inner());
    }
}

/// Client-disconnect cleanup: stop-request marker + SIGINT, then detach a drain
/// thread and a kill-ladder monitor thread (parity `_start_detached_scan_monitor`).
fn disconnect_cleanup(sup: &Arc<Mutex<Supervisor>>, job: Arc<ChildJob>, reader: File) {
    // Disconnect is an abort (scan: abort-final-choice reason).
    job.kind.write_stop_request(true);
    job.proc.signal(libc::SIGINT);
    start_detached_monitor(sup, job, reader);
}

fn start_detached_monitor(sup: &Arc<Mutex<Supervisor>>, job: Arc<ChildJob>, reader: File) {
    // Drain thread: consume stdout to EOF so the child never blocks on a full pipe.
    let _ = thread::Builder::new()
        .name("penguin-burner-child-drain".to_string())
        .spawn(move || {
            let mut reader = reader;
            let mut sink = [0u8; 8192];
            while matches!(reader.read(&mut sink), Ok(n) if n > 0) {}
        });

    // Monitor thread: the kill ladder, then finish.
    let sup = sup.clone();
    let _ = thread::Builder::new()
        .name("penguin-burner-child-monitor".to_string())
        .spawn(move || {
            wait_for_detached_child(&sup, &job, &timings());
        });
}

fn wait_for_detached_child(sup: &Arc<Mutex<Supervisor>>, job: &Arc<ChildJob>, timings: &Timings) {
    if job.proc.wait_timeout(timings.kill_after).is_none() {
        job.proc.signal(libc::SIGTERM);
        if job.proc.wait_timeout(timings.term_grace).is_none() {
            job.proc.signal(libc::SIGKILL);
            let _ = job.proc.wait_timeout(timings.kill_grace);
        }
    }
    // Reap the child BEFORE `finish_child` restarts the autostart engine. Even
    // after SIGKILL the child can outlive the grace window (uninterruptible
    // sleep on a wedged GPU); restarting the runtime engine while its GPU ioctls
    // are still in flight would put two writers on one GPU — the exact thing the
    // supervisor's "refuse GPU work until the previous owner provably stopped"
    // rule exists to prevent. This `poll()` also reaps a zombie (`try_wait`), so
    // the child never lingers for the daemon's (unbounded) lifetime. Poll on a
    // coarse interval so the per-job lock is only held briefly — `status`/`poll`
    // from other threads must not block on this wait.
    while job.proc.poll().is_none() {
        std::thread::sleep(Duration::from_millis(200));
    }
    supervisor::finish_child(sup, job);
}

/// Installer-recorded Python interpreter for the scan/verification child (the
/// role `sys.executable` played for the Python daemon, which the Rust daemon
/// does not have).
const DAEMON_PYTHON_ENV: &str = "PENGUIN_BURNER_DAEMON_PYTHON";

/// Resolve the interpreter for the Python child:
/// 1. the `PENGUIN_BURNER_DAEMON_PYTHON` override, if the installer set one;
/// 2. else, if `program_file` lives inside a **virtualenv** (an ancestor with
///    both `pyvenv.cfg` and an executable `bin/python3`), that venv interpreter
///    — root's PATH `python3` cannot import a venv-installed package;
/// 3. else bare `python3` from PATH.
///
/// The venv anchor is deliberately conservative: a bare `bin/python3` under an
/// ancestor is NOT adopted without a `pyvenv.cfg`, because for a `pip --user`
/// layout that would wrongly pick an unrelated `~/.local/bin/python3` (a uv /
/// pipx / pyenv shim of a different minor version) over the PATH interpreter
/// that actually owns the user site-packages — breaking every scan.
fn child_python(program_file: &str) -> PathBuf {
    resolve_child_python(
        paths::nonempty_env(DAEMON_PYTHON_ENV).as_deref(),
        program_file,
    )
}

fn resolve_child_python(override_value: Option<&str>, program_file: &str) -> PathBuf {
    if let Some(value) = override_value {
        let value = value.trim();
        if !value.is_empty() {
            return PathBuf::from(value);
        }
    }
    let mut dir = Path::new(program_file).parent();
    while let Some(ancestor) = dir {
        if ancestor == Path::new("/") {
            break; // stop before the root.
        }
        let candidate = ancestor.join("bin").join("python3");
        if ancestor.join("pyvenv.cfg").is_file() && is_executable_file(&candidate) {
            return candidate;
        }
        dir = ancestor.parent();
    }
    PathBuf::from("python3")
}

fn is_executable_file(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path)
        .map(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

// --- child privilege drop (milestone B3) --------------------------------------
// Every GPU write now routes through the daemon socket, so the Python child no
// longer needs root. When the daemon runs as root it resolves the desktop-user
// identity from the env the systemd unit carries and drops the child to it
// before exec (Python never runs as root). All lookups that can allocate or hit
// NSS (getpw*, getgrouplist) run in the PARENT; the post-fork `pre_exec` hook
// only issues raw, async-signal-safe syscalls.

const Q2RTX_UID_ENV: &str = "PENGUIN_BURNER_Q2RTX_UID";
const Q2RTX_GID_ENV: &str = "PENGUIN_BURNER_Q2RTX_GID";
const SUDO_USER_ENV: &str = "SUDO_USER";

/// The desktop-user identity the child is dropped to.
struct ChildIdentity {
    uid: u32,
    gid: u32,
    user: String,
    home: PathBuf,
}

/// Everything the spawn needs, precomputed in the parent: the ids, the target
/// user's supplementary groups (applied via `setgroups` — the async-signal-safe
/// equivalent of `initgroups`), and the env overrides.
struct DropPlan {
    uid: libc::uid_t,
    gid: libc::gid_t,
    groups: Vec<libc::gid_t>,
    env: Vec<(&'static str, OsString)>,
    user: String,
    config_home: PathBuf,
    config_dirs: Vec<CString>,
}

/// Resolve the drop target from the daemon's environment. `Ok(None)` means
/// "spawn as-is" (daemon not root, no identity env, or an explicit-root
/// target); `Err` means identity env is present but unusable — the caller must
/// refuse to spawn rather than run the child as root.
fn resolve_child_identity(
    daemon_is_root: bool,
    q2rtx_uid: Option<&str>,
    q2rtx_gid: Option<&str>,
    allowed_uid: Option<&str>,
    sudo_user: Option<&str>,
    pw_uid: impl Fn(u32) -> Option<paths::PwEntry>,
    pw_name: impl Fn(&str) -> Option<paths::PwEntry>,
) -> Result<Option<ChildIdentity>, String> {
    if !daemon_is_root {
        // Nothing to drop; a non-root dev daemon spawns the child unchanged.
        return Ok(None);
    }
    fn parse_id(env_name: &str, text: &str) -> Result<u32, String> {
        text.trim()
            .parse::<u32>()
            .map_err(|_| format!("invalid {env_name} value: {text:?}"))
    }
    // 1. Explicit numeric identity (the systemd unit forwards the desktop ids).
    if let Some(uid_text) = q2rtx_uid {
        let uid = parse_id(Q2RTX_UID_ENV, uid_text)?;
        if uid == 0 {
            // An explicit-root target: dropping is a no-op, spawn as-is.
            return Ok(None);
        }
        let entry = pw_uid(uid).ok_or_else(|| format!("no passwd entry for uid {uid}"))?;
        let gid = match q2rtx_gid {
            Some(text) => parse_id(Q2RTX_GID_ENV, text)?,
            None => entry.gid,
        };
        return Ok(Some(ChildIdentity {
            uid,
            gid,
            user: entry.name,
            home: entry.dir,
        }));
    }
    // 2. The socket-gate uid, with that user's primary gid.
    if let Some(uid_text) = allowed_uid {
        let uid = parse_id(paths::DAEMON_ALLOWED_UID_ENV, uid_text)?;
        if uid == 0 {
            return Ok(None);
        }
        let entry = pw_uid(uid).ok_or_else(|| format!("no passwd entry for uid {uid}"))?;
        return Ok(Some(ChildIdentity {
            uid,
            gid: entry.gid,
            user: entry.name,
            home: entry.dir,
        }));
    }
    // 3. The sudo invoker.
    if let Some(user) = sudo_user {
        let entry =
            pw_name(user).ok_or_else(|| format!("no passwd entry for SUDO_USER {user:?}"))?;
        if entry.uid == 0 {
            return Ok(None);
        }
        return Ok(Some(ChildIdentity {
            uid: entry.uid,
            gid: entry.gid,
            user: entry.name,
            home: entry.dir,
        }));
    }
    Ok(None)
}

/// The env overrides for the de-rooted child, mirroring the block the Python
/// scan set for its own Q2RTX child (`stability/q2rtx/identity.py`): the child
/// must resolve the same effective home/config dir it did as root, and the
/// Q2RTX grandchild (which inherits this env now that the child no longer
/// re-drops) must see the same world. `PENGUIN_BURNER_*` passthrough is
/// untouched — the child inherits the rest of the daemon env.
fn child_env_overrides(
    identity: &ChildIdentity,
    runtime_dir: Option<&Path>,
    parent_has_xauthority: bool,
) -> Vec<(&'static str, OsString)> {
    let home = &identity.home;
    let mut env: Vec<(&'static str, OsString)> = vec![
        ("HOME", home.clone().into_os_string()),
        ("USER", OsString::from(&identity.user)),
        ("LOGNAME", OsString::from(&identity.user)),
        ("XDG_CONFIG_HOME", home.join(".config").into_os_string()),
        (
            "XDG_DATA_HOME",
            home.join(".local").join("share").into_os_string(),
        ),
        ("XDG_CACHE_HOME", home.join(".cache").into_os_string()),
    ];
    if let Some(dir) = runtime_dir {
        env.push(("XDG_RUNTIME_DIR", dir.as_os_str().to_os_string()));
    }
    if !parent_has_xauthority {
        let xauthority = home.join(".Xauthority");
        if xauthority.is_file() {
            env.push(("XAUTHORITY", xauthority.into_os_string()));
        }
    }
    env
}

/// `/run/user/<uid>` if it exists (only then is `XDG_RUNTIME_DIR` injected).
fn existing_runtime_dir(uid: u32) -> Option<PathBuf> {
    let dir = PathBuf::from(format!("/run/user/{uid}"));
    if dir.is_dir() {
        Some(dir)
    } else {
        None
    }
}

/// The target user's full supplementary group list via `getgrouplist`, resolved
/// in the parent so the post-fork hook only needs the raw `setgroups` syscall.
fn supplementary_groups(user: &CString, gid: libc::gid_t) -> Result<Vec<libc::gid_t>, String> {
    nix::unistd::getgrouplist(user.as_c_str(), Gid::from_raw(gid))
        .map(|groups| groups.into_iter().map(Gid::as_raw).collect())
        .map_err(|error| format!("getgrouplist failed for user {user:?}: {error}"))
}

/// Build the full drop plan from the daemon env, or `None` when no drop applies.
fn child_drop_plan() -> Result<Option<DropPlan>, String> {
    let identity = resolve_child_identity(
        paths::geteuid_is_root(),
        paths::nonempty_env(Q2RTX_UID_ENV).as_deref(),
        paths::nonempty_env(Q2RTX_GID_ENV).as_deref(),
        paths::nonempty_env(paths::DAEMON_ALLOWED_UID_ENV).as_deref(),
        paths::nonempty_env(SUDO_USER_ENV).as_deref(),
        paths::pw_by_uid,
        paths::pw_by_name,
    )?;
    let Some(identity) = identity else {
        return Ok(None);
    };
    let user_c = CString::new(identity.user.as_str())
        .map_err(|_| format!("child user name contains NUL: {:?}", identity.user))?;
    let groups = supplementary_groups(&user_c, identity.gid)?;
    let env = child_env_overrides(
        &identity,
        existing_runtime_dir(identity.uid).as_deref(),
        env::var_os("XAUTHORITY").is_some(),
    );
    let config_home = paths::effective_home();
    let config_dirs = [
        config_home.join(".config"),
        config_home.join(".config/PenguinBurner"),
    ]
    .into_iter()
    .map(|path| {
        CString::new(path.as_os_str().as_bytes())
            .map_err(|_| format!("child config path contains NUL: {}", path.display()))
    })
    .collect::<Result<Vec<_>, _>>()?;
    Ok(Some(DropPlan {
        uid: identity.uid,
        gid: identity.gid,
        groups,
        env,
        user: identity.user,
        config_home,
        config_dirs,
    }))
}

/// Create and validate the child's config directories using only raw,
/// async-signal-safe syscalls. Called after setuid in `pre_exec`, so newly
/// created directories belong to the desktop user without any root chown.
fn ensure_child_config_dirs(config_dirs: &[CString]) -> io::Result<()> {
    for dir in config_dirs {
        // SAFETY: `dir` is a stable NUL-terminated path built before fork.
        if unsafe { libc::mkdir(dir.as_ptr(), 0o700) } != 0 {
            let err = io::Error::last_os_error();
            if err.raw_os_error() != Some(libc::EEXIST) {
                return Err(err);
            }
        }
        // SAFETY: read-only validation of the same stable path; O_NOFOLLOW
        // rejects a final symlink and O_DIRECTORY rejects a regular file.
        let fd = unsafe {
            libc::open(
                dir.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: `fd` is the descriptor just returned by open.
        unsafe { libc::close(fd) };
    }
    Ok(())
}

fn validate_child_program_file(program_file: &str) -> io::Result<()> {
    let metadata = std::fs::metadata(program_file).map_err(|error| {
        io::Error::new(
            error.kind(),
            format!("daemon worker {program_file} is not accessible: {error}"),
        )
    })?;
    if !metadata.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("daemon worker {program_file} is not a regular file"),
        ));
    }
    Ok(())
}

/// Explicitly prevent inherited systemd/Flatpak descriptors (for example the
/// `/proc/self/fd/17` seen in the Bazzite failure) from surviving exec.
fn mark_inherited_fds_cloexec() -> io::Result<()> {
    // SAFETY: close_range receives scalar bounds/flags only. CLOEXEC does not
    // close descriptors in the pre-exec process; it applies on successful exec.
    let result = unsafe {
        libc::syscall(
            libc::SYS_close_range,
            3_u32,
            u32::MAX,
            CLOSE_RANGE_CLOEXEC_FLAG,
        )
    };
    if result == 0 {
        return Ok(());
    }
    let error = io::Error::last_os_error();
    if matches!(
        error.raw_os_error(),
        Some(libc::ENOSYS) | Some(libc::EINVAL)
    ) {
        // Old kernels lack close_range/CLOEXEC. Rust-created daemon descriptors
        // are already O_CLOEXEC, so retain that baseline instead of breaking
        // otherwise supported systems.
        return Ok(());
    }
    Err(error)
}

fn verify_child_program_readable(program_file: &CString) -> io::Result<()> {
    // SAFETY: `program_file` is a stable NUL-terminated path built before fork.
    let fd = unsafe { libc::open(program_file.as_ptr(), libc::O_RDONLY | libc::O_CLOEXEC) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: `fd` is the descriptor just returned by open.
    unsafe { libc::close(fd) };
    Ok(())
}

/// Spawn the child with stdout and stderr merged onto one pipe, `cwd="/"`.
/// Returns the child and the read end of the pipe. `python` is the pre-resolved
/// interpreter and `drop` the pre-resolved privilege-drop plan (both resolved
/// outside the supervisor lock by the caller). With a plan, the child is
/// dropped to the desktop user before exec; any failure aborts the spawn.
fn spawn_child(
    python: &Path,
    program_file: &str,
    argv: &[String],
    drop: Option<&DropPlan>,
) -> std::io::Result<(Child, File)> {
    let program_file_c =
        CString::new(Path::new(program_file).as_os_str().as_bytes()).map_err(|_| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "worker path contains a NUL byte",
            )
        })?;
    let (read_fd, write_fd) = pipe2(OFlag::O_CLOEXEC).map_err(io::Error::from)?;
    let reader = File::from(read_fd);
    let write_file = File::from(write_fd);
    let write_clone = write_file.try_clone()?;

    let mut command = Command::new(python);
    command
        .arg(program_file)
        .args(argv)
        .current_dir("/")
        .stdin(Stdio::null())
        .stdout(Stdio::from(write_file))
        .stderr(Stdio::from(write_clone));

    for key in CHILD_ENV_REMOVE {
        command.env_remove(key);
    }

    // De-rooted child: point HOME/USER/XDG at the desktop user so the child
    // resolves the same `~/.config/PenguinBurner` it did as root. The rest of
    // the daemon env (PENGUIN_BURNER_* passthrough) is inherited unchanged.
    if let Some(plan) = drop {
        for (key, value) in &plan.env {
            command.env(key, value);
        }
    }

    // The post-fork/pre-exec data must be owned by the closure and applied
    // without allocating (fork of a multithreaded process: only
    // async-signal-safe calls are legal until exec).
    let drop_spec: Option<(libc::uid_t, libc::gid_t, Vec<libc::gid_t>, Vec<CString>)> =
        drop.map(|plan| {
            (
                plan.uid,
                plan.gid,
                plan.groups.clone(),
                plan.config_dirs.clone(),
            )
        });

    // With a drop plan, privileges are dropped in the mandatory order
    // setsid → setgroups → setgid → setuid (gid before uid: after setuid the
    // process can no longer change its gid). Every step fails CLOSED: an
    // `Err` here aborts the spawn, so the child can never exec with root or
    // half-dropped privileges.
    // SAFETY: runs between fork and exec; every call below is a raw syscall
    // (async-signal-safe, no allocation — the groups Vec was built pre-fork).
    unsafe {
        use std::os::unix::process::CommandExt;
        command.pre_exec(move || {
            mark_inherited_fds_cloexec()?;
            if let Some((uid, gid, groups, config_dirs)) = drop_spec.as_ref() {
                if libc::setsid() < 0 {
                    return Err(io::Error::last_os_error());
                }
                if libc::setgroups(groups.len(), groups.as_ptr()) != 0 {
                    return Err(io::Error::last_os_error());
                }
                if libc::setgid(*gid) != 0 {
                    return Err(io::Error::last_os_error());
                }
                if libc::setuid(*uid) != 0 {
                    return Err(io::Error::last_os_error());
                }
                // Create the two config directories only after dropping uid/gid,
                // so a fresh first run can always receive an immediate stop
                // marker without the root daemon creating/chowning user files.
                ensure_child_config_dirs(config_dirs)?;
            }
            // Open after setuid so root cannot mask an unreadable Flatpak
            // deployment. Failure is reported as a spawn error before the
            // daemon emits a misleading `started` frame.
            verify_child_program_readable(&program_file_c)?;
            Ok(())
        });
    }

    let child = command.spawn().map_err(|error| {
        io::Error::new(
            error.kind(),
            format!("cannot execute daemon worker {program_file} as the desktop user: {error}"),
        )
    })?;
    // The parent's copies of the write end were moved into `command` and are now
    // closed; only the child holds it, so `reader` sees EOF when the child exits.
    Ok((child, reader))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::supervisor::ChildProc;
    use std::fs;

    // --- child interpreter resolution (F3) -----------------------------------

    #[test]
    fn child_python_env_override_wins() {
        assert_eq!(
            resolve_child_python(Some("/opt/pb/bin/python"), "/x/penguin_burner.py"),
            PathBuf::from("/opt/pb/bin/python")
        );
        // Blank override is ignored (falls through to the heuristic/fallback).
        assert_eq!(
            resolve_child_python(Some("   "), "/x/penguin_burner.py"),
            PathBuf::from("python3")
        );
    }

    /// Make an executable `bin/python3` under `venv_root`; optionally mark it as
    /// a virtualenv with a `pyvenv.cfg`. Returns the interpreter path.
    fn make_venv(venv_root: &Path, with_marker: bool) -> PathBuf {
        let bin = venv_root.join("bin");
        fs::create_dir_all(&bin).unwrap();
        let python = bin.join("python3");
        fs::write(&python, "#!/bin/sh\n").unwrap();
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&python, fs::Permissions::from_mode(0o755)).unwrap();
        }
        if with_marker {
            fs::write(venv_root.join("pyvenv.cfg"), "home = /usr/bin\n").unwrap();
        }
        python
    }

    #[test]
    fn child_python_finds_venv_interpreter_above_program_file() {
        let root = std::env::temp_dir().join(format!("pb-venv-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let venv = root.join("venv");
        let python = make_venv(&venv, true);
        let program = venv
            .join("lib")
            .join("python3.12")
            .join("site-packages")
            .join("penguin_burner.py");
        assert_eq!(
            resolve_child_python(None, program.to_str().unwrap()),
            python
        );
        let _ = fs::remove_dir_all(&root);
    }

    /// Regression guard for the pip `--user` layout: a bare `bin/python3` under
    /// an ancestor WITHOUT a `pyvenv.cfg` (e.g. a uv/pyenv shim in ~/.local/bin)
    /// must NOT be adopted — that would break scans where PATH `python3` worked.
    #[test]
    fn child_python_ignores_non_venv_bin_python() {
        let root = std::env::temp_dir().join(format!("pb-user-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let local = root.join(".local");
        make_venv(&local, false); // ~/.local/bin/python3 but no pyvenv.cfg
        let program = local
            .join("lib")
            .join("python3.14")
            .join("site-packages")
            .join("penguin_burner.py");
        assert_eq!(
            resolve_child_python(None, program.to_str().unwrap()),
            PathBuf::from("python3")
        );
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn child_python_falls_back_to_path_python3() {
        let root = std::env::temp_dir().join(format!("pb-noenv-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let program = root.join("penguin_burner.py");
        assert_eq!(
            resolve_child_python(None, program.to_str().unwrap()),
            PathBuf::from("python3")
        );
        let _ = fs::remove_dir_all(&root);
    }

    // --- child privilege-drop resolution (B3) ---------------------------------

    fn entry(name: &str, dir: &str, uid: u32, gid: u32) -> paths::PwEntry {
        paths::PwEntry {
            name: name.to_string(),
            dir: PathBuf::from(dir),
            uid,
            gid,
        }
    }

    /// Fake passwd tables: uid 1000 = jp (primary gid 1000), uid 2000 = other
    /// (primary gid 2500); by-name knows jp and root.
    fn fake_pw_uid(uid: u32) -> Option<paths::PwEntry> {
        match uid {
            1000 => Some(entry("jp", "/home/jp", 1000, 1000)),
            2000 => Some(entry("other", "/home/other", 2000, 2500)),
            _ => None,
        }
    }

    fn fake_pw_name(name: &str) -> Option<paths::PwEntry> {
        match name {
            "jp" => Some(entry("jp", "/home/jp", 1000, 1000)),
            "root" => Some(entry("root", "/root", 0, 0)),
            _ => None,
        }
    }

    fn resolve(
        daemon_is_root: bool,
        q2rtx_uid: Option<&str>,
        q2rtx_gid: Option<&str>,
        allowed_uid: Option<&str>,
        sudo_user: Option<&str>,
    ) -> Result<Option<ChildIdentity>, String> {
        resolve_child_identity(
            daemon_is_root,
            q2rtx_uid,
            q2rtx_gid,
            allowed_uid,
            sudo_user,
            fake_pw_uid,
            fake_pw_name,
        )
    }

    #[test]
    fn non_root_daemon_never_drops() {
        let identity =
            resolve(false, Some("1000"), Some("1000"), Some("1000"), Some("jp")).expect("no error");
        assert!(identity.is_none(), "a non-root daemon must spawn as-is");
    }

    #[test]
    fn no_identity_env_means_no_drop() {
        let identity = resolve(true, None, None, None, None).expect("no error");
        assert!(identity.is_none());
    }

    #[test]
    fn q2rtx_uid_and_gid_env_win() {
        let identity = resolve(true, Some("1000"), Some("985"), Some("2000"), Some("root"))
            .expect("no error")
            .expect("drops");
        assert_eq!(identity.uid, 1000);
        assert_eq!(identity.gid, 985, "explicit gid env beats the primary gid");
        assert_eq!(identity.user, "jp");
        assert_eq!(identity.home, PathBuf::from("/home/jp"));
    }

    #[test]
    fn q2rtx_uid_without_gid_uses_primary_gid() {
        let identity = resolve(true, Some("2000"), None, None, None)
            .expect("no error")
            .expect("drops");
        assert_eq!(identity.uid, 2000);
        assert_eq!(identity.gid, 2500);
        assert_eq!(identity.user, "other");
    }

    #[test]
    fn allowed_uid_fallback_uses_primary_gid() {
        let identity = resolve(true, None, None, Some("1000"), Some("root"))
            .expect("no error")
            .expect("drops");
        assert_eq!(identity.uid, 1000);
        assert_eq!(identity.gid, 1000);
        assert_eq!(identity.user, "jp");
    }

    #[test]
    fn sudo_user_is_the_last_fallback() {
        let identity = resolve(true, None, None, None, Some("jp"))
            .expect("no error")
            .expect("drops");
        assert_eq!(identity.uid, 1000);
        assert_eq!(identity.gid, 1000);
        assert_eq!(identity.home, PathBuf::from("/home/jp"));
    }

    #[test]
    fn root_targets_mean_no_drop() {
        // Explicit-root targets are a no-op drop, not an error (parity with a
        // root child today), for every source.
        assert!(resolve(true, Some("0"), None, None, None)
            .unwrap()
            .is_none());
        assert!(resolve(true, None, None, Some("0"), None)
            .unwrap()
            .is_none());
        assert!(resolve(true, None, None, None, Some("root"))
            .unwrap()
            .is_none());
    }

    #[test]
    fn unparseable_identity_env_fails_closed() {
        assert!(resolve(true, Some("abc"), None, None, None).is_err());
        assert!(resolve(true, Some("-1"), None, None, None).is_err());
        assert!(resolve(true, Some("1000"), Some("xyz"), None, None).is_err());
        assert!(resolve(true, None, None, Some("abc"), None).is_err());
    }

    #[test]
    fn unknown_identity_fails_closed() {
        // Identity env present but not resolvable in passwd: refuse to spawn
        // rather than silently running the child as root.
        assert!(resolve(true, Some("4242"), None, None, None).is_err());
        assert!(resolve(true, None, None, Some("4242"), None).is_err());
        assert!(resolve(true, None, None, None, Some("ghost")).is_err());
    }

    // --- child env overrides (B3) ----------------------------------------------

    fn env_value<'a>(env: &'a [(&'static str, OsString)], key: &str) -> Option<&'a OsString> {
        env.iter().find(|(k, _)| *k == key).map(|(_, v)| v)
    }

    #[test]
    fn env_overrides_point_child_at_desktop_home() {
        let identity = ChildIdentity {
            uid: 1234,
            gid: 1234,
            user: "jp".to_string(),
            home: PathBuf::from("/home/jp"),
        };
        let runtime = PathBuf::from("/run/user/1234");
        let env = child_env_overrides(&identity, Some(&runtime), true);
        assert_eq!(env_value(&env, "HOME").unwrap(), "/home/jp");
        assert_eq!(env_value(&env, "USER").unwrap(), "jp");
        assert_eq!(env_value(&env, "LOGNAME").unwrap(), "jp");
        assert_eq!(
            env_value(&env, "XDG_CONFIG_HOME").unwrap(),
            "/home/jp/.config"
        );
        assert_eq!(
            env_value(&env, "XDG_DATA_HOME").unwrap(),
            "/home/jp/.local/share"
        );
        assert_eq!(
            env_value(&env, "XDG_CACHE_HOME").unwrap(),
            "/home/jp/.cache"
        );
        assert_eq!(
            env_value(&env, "XDG_RUNTIME_DIR").unwrap(),
            "/run/user/1234"
        );
        // Daemon already carries XAUTHORITY: never overridden.
        assert!(env_value(&env, "XAUTHORITY").is_none());
    }

    #[test]
    fn env_overrides_skip_missing_runtime_dir_and_add_xauthority() {
        let home = std::env::temp_dir().join(format!("pb-xauth-{}", std::process::id()));
        let _ = fs::remove_dir_all(&home);
        fs::create_dir_all(&home).unwrap();
        fs::write(home.join(".Xauthority"), "").unwrap();
        let identity = ChildIdentity {
            uid: 1234,
            gid: 1234,
            user: "jp".to_string(),
            home: home.clone(),
        };
        let env = child_env_overrides(&identity, None, false);
        assert!(env_value(&env, "XDG_RUNTIME_DIR").is_none());
        assert_eq!(
            env_value(&env, "XAUTHORITY").unwrap(),
            home.join(".Xauthority").as_os_str()
        );
        let _ = fs::remove_dir_all(&home);
    }

    // --- the drop itself (B3) ---------------------------------------------------

    #[test]
    fn child_config_dirs_are_created_and_symlinks_rejected() {
        use std::os::unix::fs::symlink;

        let root = std::env::temp_dir().join(format!("pb-child-config-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir(&root).unwrap();
        let dot_config = root.join(".config");
        let app_config = dot_config.join("PenguinBurner");
        let dirs = [&dot_config, &app_config]
            .into_iter()
            .map(|path| CString::new(path.to_string_lossy().as_bytes()).unwrap())
            .collect::<Vec<_>>();

        ensure_child_config_dirs(&dirs).unwrap();
        assert!(dot_config.is_dir());
        assert!(app_config.is_dir());

        fs::remove_dir(&app_config).unwrap();
        let victim = root.join("victim");
        fs::create_dir(&victim).unwrap();
        symlink(&victim, &app_config).unwrap();
        assert!(ensure_child_config_dirs(&dirs).is_err());
        assert!(victim.is_dir());

        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn supplementary_groups_include_primary_gid() {
        // SAFETY: getuid is always safe.
        let uid = unsafe { libc::getuid() };
        let entry = paths::pw_by_uid(uid).expect("current user resolves");
        let user = CString::new(entry.name.clone()).unwrap();
        let groups = supplementary_groups(&user, entry.gid).expect("group list resolves");
        assert!(
            groups.contains(&entry.gid),
            "the primary gid must be in the supplementary list"
        );
    }

    /// Fail-closed: without the privilege to drop (non-root test run), a spawn
    /// WITH a drop plan must fail — the child must never exec half-dropped.
    #[test]
    fn spawn_with_drop_plan_fails_closed_without_privilege() {
        // SAFETY: geteuid is always safe.
        if unsafe { libc::geteuid() } == 0 {
            return; // as root the drop succeeds; covered by the root-gated test
        }
        let dir = std::env::temp_dir().join(format!("pb-dropfail-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let marker = dir.join("ran");
        let plan = DropPlan {
            uid: 1,
            gid: 1,
            groups: vec![1],
            env: Vec::new(),
            user: "daemon".to_string(),
            config_home: dir.clone(),
            config_dirs: Vec::new(),
        };
        let result = spawn_child(
            Path::new("/bin/sh"),
            "-c",
            &[format!("touch {}", marker.display())],
            Some(&plan),
        );
        assert!(result.is_err(), "a failed drop must abort the spawn");
        assert!(
            !marker.exists(),
            "the child must never run without the drop"
        );
        let _ = fs::remove_dir_all(&dir);
    }

    /// The real drop, asserted end-to-end when the test itself runs as root:
    /// the child must report the TARGET uid/gid, not 0.
    #[test]
    fn spawn_with_drop_plan_runs_child_as_target_when_root() {
        // SAFETY: geteuid is always safe.
        if unsafe { libc::geteuid() } != 0 {
            return; // needs real root to perform the drop
        }
        let Some(entry) = paths::pw_by_name("nobody") else {
            return;
        };
        let user = CString::new(entry.name.clone()).unwrap();
        let groups = supplementary_groups(&user, entry.gid).expect("group list resolves");
        let plan = DropPlan {
            uid: entry.uid,
            gid: entry.gid,
            groups,
            env: Vec::new(),
            user: entry.name.clone(),
            config_home: entry.dir.clone(),
            config_dirs: Vec::new(),
        };
        let script = std::env::temp_dir().join(format!(
            "pb-root-drop-{}-{}.sh",
            std::process::id(),
            entry.uid
        ));
        fs::write(&script, "#!/bin/sh\nid -u\nid -g\n").unwrap();
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
        }
        let (child, reader) = spawn_child(
            Path::new("/bin/sh"),
            script.to_str().unwrap(),
            &[],
            Some(&plan),
        )
        .expect("spawn with drop");
        let mut output = String::new();
        let mut reader = BufReader::new(reader);
        reader.read_to_string(&mut output).unwrap();
        let mut child = child;
        let _ = child.wait();
        let reported: Vec<&str> = output.split_whitespace().collect();
        assert_eq!(
            reported,
            vec![entry.uid.to_string(), entry.gid.to_string()],
            "the child must run as the drop target, not root"
        );
        let _ = fs::remove_file(script);
    }

    #[test]
    fn spawn_child_closes_inherited_non_stdio_descriptors_on_exec() {
        use std::os::fd::{AsRawFd, FromRawFd};

        let dir = std::env::temp_dir().join(format!("pb-fd-sanitize-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let script = dir.join("check_fd.py");
        fs::write(
            &script,
            concat!(
                "import os, sys\n",
                "try:\n    target = os.readlink('/proc/self/fd/' + sys.argv[1])\n",
                "except OSError:\n    target = ''\n",
                "print('inherited' if target == sys.argv[2] else 'closed')\n",
            ),
        )
        .unwrap();

        let marker = dir.join("inherited-marker");
        fs::write(&marker, "marker").unwrap();
        let base = File::open(&marker).unwrap();
        // SAFETY: fcntl duplicates a valid owned fd; on success the returned
        // descriptor is independent and intentionally lacks FD_CLOEXEC.
        let raw_fd = unsafe { libc::fcntl(base.as_raw_fd(), libc::F_DUPFD, 3) };
        assert!(raw_fd >= 3);
        // SAFETY: `raw_fd` is the new descriptor returned by F_DUPFD.
        let inherited = unsafe { File::from_raw_fd(raw_fd) };

        let (mut child, reader) = spawn_child(
            Path::new("python3"),
            script.to_str().unwrap(),
            &[raw_fd.to_string(), marker.display().to_string()],
            None,
        )
        .unwrap();
        let mut output = String::new();
        let mut reader = BufReader::new(reader);
        reader.read_to_string(&mut output).unwrap();
        assert!(child.wait().unwrap().success());
        assert_eq!(output.trim(), "closed");
        drop(inherited);
        let _ = fs::remove_dir_all(dir);
    }

    // --- kill-ladder reap (F6) ------------------------------------------------

    /// After the final SIGKILL the monitor must block until the child is reaped:
    /// with a zero kill-grace (simulating a child that outlives the grace
    /// window) the child must NOT be left as a permanent zombie.
    #[test]
    fn kill_ladder_reaps_child_after_final_sigkill() {
        // A child that survives the SIGTERM rung so the ladder reaches SIGKILL.
        let child = Command::new("sh")
            .arg("-c")
            .arg("trap '' TERM INT; while :; do sleep 0.05; done")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn stub child");
        let pid = child.id();
        let sup = Arc::new(Mutex::new(Supervisor::new()));
        let job = Arc::new(ChildJob {
            proc: ChildProc::new(child).unwrap(),
            argv: Vec::new(),
            generation: 1,
            kind: ChildKind::Scan,
        });
        let timings = Timings {
            kill_after: Duration::from_millis(50),
            term_grace: Duration::from_millis(50),
            kill_grace: Duration::ZERO,
        };
        wait_for_detached_child(&sup, &job, &timings);
        // Fully reaped: the pid is gone (a zombie would still answer kill(0)).
        // SAFETY: kill(pid, 0) only probes for existence.
        let alive = unsafe { libc::kill(pid as libc::pid_t, 0) == 0 };
        assert!(!alive, "child must be reaped, not left a zombie");
        assert_eq!(job.proc.poll(), Some(-libc::SIGKILL));
    }
}
