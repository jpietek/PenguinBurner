//! Launcher sessions are independent of GPU policy. A registration proves only
//! that the wrapper entered; profile application has its own explicit result.
//! Subscribers receive atomic, sequenced snapshots, including on reconnect.

use std::collections::{BTreeMap, VecDeque};
use std::io::{Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::fs::MetadataExt;
use std::os::unix::net::UnixStream;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use serde_json::{json, Value};

use crate::api::{self, MethodResult};

const MAX_SESSIONS: usize = 128;
const MAX_SUBSCRIBERS: usize = 32;

#[derive(Clone, Serialize)]
struct Session {
    pid: u32,
    app_id: String,
    session_id: String,
    phase: String,
    profile: String,
    wrapped: bool,
}

struct Registry {
    epoch: String,
    sequence: u64,
    sessions: BTreeMap<u32, Session>,
    ended: VecDeque<Value>,
    subscribers: BTreeMap<u64, UnixStream>,
    next_subscriber: u64,
    lifetimes: BTreeMap<(String, String), (UnixStream, UnixStream)>,
}

impl Registry {
    fn new() -> Self {
        Self {
            epoch: format!(
                "{}-{}",
                std::process::id(),
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_nanos()
            ),
            sequence: 0,
            sessions: BTreeMap::new(),
            ended: VecDeque::new(),
            subscribers: BTreeMap::new(),
            next_subscriber: 0,
            lifetimes: BTreeMap::new(),
        }
    }

    fn snapshot(&self) -> Value {
        json!({"epoch": self.epoch, "sequence": self.sequence,
            "sessions": self.sessions.values().collect::<Vec<_>>(), "ended": self.ended})
    }

    fn changed(&mut self) {
        self.sequence += 1;
        // Wakeups may coalesce: readers fetch a COMPLETE snapshot. Slow readers
        // never block a wrapper or GPU operation and cannot lose final state.
        self.subscribers
            .retain(|_, stream| match stream.write(&[1]) {
                Ok(_) => true,
                Err(error) => error.kind() == std::io::ErrorKind::WouldBlock,
            });
    }

    fn remove(&mut self, pid: u32, session_id: &str) {
        if self
            .sessions
            .get(&pid)
            .is_some_and(|s| s.session_id == session_id)
        {
            if let Some(mut session) = self.sessions.remove(&pid) {
                if !self.sessions.values().any(|other| {
                    other.app_id == session.app_id && other.session_id == session.session_id
                }) {
                    // Closing the writer ends GPU ownership only after handoff
                    // discovery has attached every surviving process.
                    self.lifetimes
                        .remove(&(session.app_id.clone(), session.session_id.clone()));
                }
                if session.phase != "failed" {
                    session.phase = "exited".into();
                }
                self.ended
                    .push_back(json!({"session": session, "sequence": self.sequence + 1}));
                while self.ended.len() > MAX_SESSIONS {
                    self.ended.pop_front();
                }
            }
            self.changed();
        }
    }
}

fn registry() -> &'static Mutex<Registry> {
    static REGISTRY: OnceLock<Mutex<Registry>> = OnceLock::new();
    REGISTRY.get_or_init(|| Mutex::new(Registry::new()))
}

pub fn snapshot() -> Value {
    registry()
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .snapshot()
}

/// A readable/closed handle means the entire registered launch has ended.
/// The registry owns lifetime observation; consumers need no independent
/// process scan or grace period to guess whether a handoff is complete.
pub fn watch_lifetime(pid: u32) -> Result<Option<OwnedFd>, String> {
    let mut state = registry().lock().unwrap_or_else(|p| p.into_inner());
    let Some(session) = state.sessions.get(&pid).filter(|s| s.wrapped) else {
        return Ok(None);
    };
    let key = (session.app_id.clone(), session.session_id.clone());
    let handles = match state.lifetimes.entry(key) {
        std::collections::btree_map::Entry::Occupied(entry) => entry.into_mut(),
        std::collections::btree_map::Entry::Vacant(entry) => {
            entry.insert(UnixStream::pair().map_err(|e| e.to_string())?)
        }
    };
    handles
        .0
        .try_clone()
        .map(|fd| Some(fd.into()))
        .map_err(|e| e.to_string())
}

fn text<'a>(value: &'a Value, name: &str) -> Result<&'a str, String> {
    value
        .get(name)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty() && s.len() <= 256 && !s.chars().any(char::is_control))
        .ok_or_else(|| format!("invalid {name}"))
}

fn valid_app_id(app_id: &str) -> bool {
    app_id.split_once(':').is_some_and(|(launcher, id)| {
        matches!(launcher, "steam" | "lutris" | "heroic" | "faugus") && !id.is_empty()
    })
}

fn pidfd(pid: u32) -> Result<OwnedFd, String> {
    // SAFETY: pidfd_open takes scalar arguments and returns a new descriptor.
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid as libc::pid_t, 0) };
    if fd < 0 {
        return Err(format!(
            "cannot watch session: {}",
            std::io::Error::last_os_error()
        ));
    }
    // SAFETY: the successful syscall above transferred ownership of this fd.
    Ok(unsafe { OwnedFd::from_raw_fd(fd as i32) })
}

fn poll_fds(fds: &mut [libc::pollfd]) -> std::io::Result<()> {
    loop {
        // SAFETY: fds points to initialized entries, valid for the whole call.
        let result = unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as libc::nfds_t, -1) };
        if result >= 0 {
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

fn register(session: Session) -> Result<Value, String> {
    let fd = pidfd(session.pid)?;
    register_with_fd(session, fd)
}

fn register_with_fd(session: Session, fd: OwnedFd) -> Result<Value, String> {
    let mut readiness = libc::pollfd {
        fd: fd.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    // SAFETY: readiness and fd are valid for this nonblocking readiness check.
    if unsafe { libc::poll(&mut readiness, 1, 0) } != 0 {
        return Err("session process already exited".into());
    }
    let pid = session.pid;
    let session_id = session.session_id.clone();
    let mut state = registry().lock().unwrap_or_else(|p| p.into_inner());
    if let Some(existing) = state.sessions.get(&pid) {
        if existing.session_id == session_id && existing.app_id == session.app_id {
            return Ok(json!({"pid": pid, "session_id": session_id}));
        }
        if existing.wrapped {
            return Err("process already owns a different session".to_string());
        }
    }
    if state.sessions.len() >= MAX_SESSIONS {
        return Err("session limit reached".to_string());
    }
    state.sessions.insert(pid, session.clone());
    let watched_id = session_id.clone();
    if let Err(error) = std::thread::Builder::new()
        .name("pb-session-exit".into())
        .spawn(move || {
            let mut fds = [libc::pollfd {
                fd: fd.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            }];
            if poll_fds(&mut fds).is_ok() && fds[0].revents & libc::POLLIN != 0 {
                // exec preserves the pidfd. A forking launcher can instead hand
                // off to children carrying the same launch identity. Attach their
                // handles before publishing the original process exit.
                let current = registry()
                    .lock()
                    .unwrap_or_else(|p| p.into_inner())
                    .sessions
                    .get(&pid)
                    .filter(|s| s.session_id == watched_id)
                    .cloned();
                if let Some(current) = current {
                    recover_handoff(&current);
                }
                registry()
                    .lock()
                    .unwrap_or_else(|p| p.into_inner())
                    .remove(pid, &watched_id);
            }
        })
    {
        state.sessions.remove(&pid);
        return Err(error.to_string());
    }
    state.changed();
    Ok(json!({"pid": pid, "session_id": session_id}))
}

/// PID comes from SO_PEERCRED, not request data, including Flatpak clients.
pub fn request(pid: u32, value: &Value) -> Result<MethodResult, String> {
    let session_id = text(value, "session_id")?;
    match value.get("method").and_then(Value::as_str) {
        Some("register_launcher_session") => {
            let app_id = text(value, "app_id")?;
            if !valid_app_id(app_id) {
                return Err("invalid launcher app_id".into());
            }
            register(Session {
                pid,
                app_id: app_id.into(),
                session_id: session_id.into(),
                phase: "starting".into(),
                profile: "unconfirmed".into(),
                wrapped: true,
            })
            .map(MethodResult::Value)
        }
        Some("update_launcher_session") => {
            let phase = text(value, "phase")?;
            let profile = text(value, "profile")?;
            if !matches!(phase, "starting" | "running" | "failed")
                || !matches!(profile, "unconfirmed" | "applied" | "not-applied")
            {
                return Err("invalid session state".into());
            }
            let mut state = registry().lock().unwrap_or_else(|p| p.into_inner());
            let session = state
                .sessions
                .get_mut(&pid)
                .filter(|s| s.session_id == session_id)
                .ok_or("session is not owned by this process")?;
            // Positive profile evidence is sticky for this launch identity.
            let profile = if session.profile == "applied" {
                "applied"
            } else {
                profile
            };
            // Duplicate updates are harmless; terminal state cannot regress.
            if session.phase != "failed"
                && !(session.phase == "running" && phase == "starting")
                && (session.phase != phase || session.profile != profile)
            {
                session.phase = phase.into();
                session.profile = profile.into();
                state.changed();
            }
            Ok(MethodResult::Value(json!({"updated": true})))
        }
        _ => Err("unknown session method".into()),
    }
}

/// Observe a launcher-owned process without granting wrapper/Stop authority.
/// The UID and actual command/environment must agree with the requested game.
pub fn observe(uid: u32, value: &Value) -> Result<MethodResult, String> {
    let pid = value
        .get("pid")
        .and_then(Value::as_u64)
        .and_then(|p| u32::try_from(p).ok())
        .filter(|p| *p > 1)
        .ok_or("invalid pid")?;
    let app_id = text(value, "app_id")?;
    let fd = pidfd(pid)?;
    let path = std::path::PathBuf::from(format!("/proc/{pid}"));
    if !std::fs::metadata(&path).is_ok_and(|m| m.uid() == uid) {
        return Err("launcher process is not owned by this user".into());
    }
    let stat = std::fs::read_to_string(path.join("stat")).map_err(|e| e.to_string())?;
    let start = stat
        .rsplit_once(')')
        .and_then(|(_, fields)| fields.split_whitespace().nth(19))
        .ok_or("missing process identity")?;
    let bytes = std::fs::read(path.join("environ")).map_err(|e| e.to_string())?;
    let env: BTreeMap<&str, &str> = bytes
        .split(|b| *b == 0)
        .filter_map(|s| std::str::from_utf8(s).ok()?.split_once('='))
        .collect();
    let command = std::fs::read(path.join("cmdline")).map_err(|e| e.to_string())?;
    let args: Vec<&str> = command
        .split(|b| *b == 0)
        .filter_map(|s| std::str::from_utf8(s).ok())
        .collect();
    let (launcher, game) = app_id.split_once(':').ok_or("invalid app_id")?;
    let agrees = match launcher {
        "heroic" => env.get("HEROIC_APP_NAME") == Some(&game),
        "faugus" => {
            !game.is_empty()
                && (env.get("FAUGUSID") == Some(&game)
                    || (env.contains_key("FAUGUSID")
                        && executable_matches(
                            args.first().copied().unwrap_or_default(),
                            env.get("WINEPREFIX").copied().unwrap_or_default(),
                            value
                                .get("executable")
                                .and_then(Value::as_str)
                                .unwrap_or_default(),
                        )))
        }
        "steam" => args.iter().any(|arg| *arg == format!("AppId={game}")),
        "lutris" => {
            let title = text(value, "title")?;
            args.first()
                .is_some_and(|arg| *arg == format!("lutris-wrapper: {title}"))
                || (args.first().is_some_and(|arg| {
                    arg.ends_with("/lutris-wrapper") || *arg == "lutris-wrapper"
                }) && args.get(1) == Some(&title))
        }
        _ => false,
    };
    if !agrees {
        return Err("launcher process identity changed".into());
    }
    {
        let state = registry().lock().unwrap_or_else(|p| p.into_inner());
        if state.sessions.get(&pid).is_some_and(|s| s.app_id == app_id) {
            return Ok(MethodResult::Value(json!({"observed": true})));
        }
    }
    register_with_fd(
        Session {
            pid,
            app_id: app_id.into(),
            session_id: format!("observed-{pid}-{start}"),
            phase: "running".into(),
            profile: "unconfirmed".into(),
            wrapped: false,
        },
        fd,
    )
    .map(MethodResult::Value)
}

/// An already-running store client can spawn a game with its own inherited ID.
/// Verify the full executable and prefix, never a basename or client-supplied PID alone.
fn executable_matches(command: &str, prefix: &str, expected: &str) -> bool {
    use std::path::Path;
    if !Path::new(expected).is_absolute() {
        return false;
    }
    let command = command.replace('\\', "/");
    let path = if command.as_bytes().get(1..3) == Some(b":/") {
        if prefix.is_empty() {
            return false;
        }
        Path::new(prefix)
            .join("dosdevices")
            .join(command[..2].to_ascii_lowercase())
            .join(&command[3..])
    } else {
        Path::new(&command).to_path_buf()
    };
    path.is_absolute()
        && path.canonicalize().ok().is_some_and(|actual| {
            Path::new(expected)
                .canonicalize()
                .is_ok_and(|wanted| actual == wanted)
        })
}

pub fn subscribe(stream: &mut UnixStream) {
    let _ = stream.set_write_timeout(Some(Duration::from_secs(3)));
    let Ok((mut reader, writer)) = UnixStream::pair() else {
        return;
    };
    if reader.set_nonblocking(true).is_err() || writer.set_nonblocking(true).is_err() {
        return;
    }
    let (id, initial) = {
        let mut state = registry().lock().unwrap_or_else(|p| p.into_inner());
        if state.subscribers.len() >= MAX_SUBSCRIBERS {
            let _ = api::write_response(stream, Err("session subscriber limit reached".into()));
            return;
        }
        state.next_subscriber += 1;
        let id = state.next_subscriber;
        state.subscribers.insert(id, writer);
        (id, state.snapshot())
    };
    // An I/O bound protects the daemon from stalled readers; it never decides
    // that a game failed, ended or did not load PenguinBurner.
    let _ = stream.set_write_timeout(Some(Duration::from_secs(3)));
    let mut payload = initial;
    loop {
        if !api::write_response(stream, Ok(MethodResult::Value(payload))) {
            break;
        }
        let mut fds = [
            libc::pollfd {
                fd: reader.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            },
            libc::pollfd {
                fd: stream.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            },
        ];
        if poll_fds(&mut fds).is_err() || fds[1].revents != 0 {
            break;
        }
        let mut buffer = [0; 256];
        while reader.read(&mut buffer).is_ok_and(|n| n > 0) {}
        payload = snapshot();
    }
    registry()
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .subscribers
        .remove(&id);
}

/// Recover observation after a daemon restart. Never replay a GPU write.
pub fn recover(uid: u32) {
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|s| s.parse::<u32>().ok())
        else {
            continue;
        };
        let Ok(fd) = pidfd(pid) else {
            continue;
        };
        if !entry.metadata().is_ok_and(|m| m.uid() == uid) {
            continue;
        }
        let Ok(bytes) = std::fs::read(path.join("environ")) else {
            continue;
        };
        let env: BTreeMap<&str, &str> = bytes
            .split(|b| *b == 0)
            .filter_map(|s| std::str::from_utf8(s).ok()?.split_once('='))
            .collect();
        // New wrappers carry a random identity through exec/fork, even if
        // the daemon was down at launch and Flatpak only knew its local PID.
        // Legacy wrappers can be recovered only at their known host leader.
        if !env.contains_key("PENGUIN_BURNER_SESSION_ID")
            && env
                .get("PENGUIN_BURNER_TELEMETRY_SESSION")
                .and_then(|s| s.parse().ok())
                != Some(pid)
        {
            continue;
        }
        let steam_key = env
            .get("SteamAppId")
            .filter(|s| s.bytes().all(|b| b.is_ascii_digit()))
            .map(|s| format!("steam:{s}"));
        let Some(app_id) = env
            .get("PENGUIN_BURNER_GAME_KEY")
            .copied()
            .or(steam_key.as_deref())
            .filter(|s| valid_app_id(s))
        else {
            continue;
        };
        let legacy_id = format!("recovered-{pid}");
        let session_id = env
            .get("PENGUIN_BURNER_SESSION_ID")
            .copied()
            .unwrap_or(&legacy_id);
        if app_id.len() > 256
            || session_id.is_empty()
            || session_id.len() > 256
            || session_id.chars().any(char::is_control)
        {
            continue;
        }
        let _ = register_with_fd(
            Session {
                pid,
                app_id: app_id.into(),
                session_id: session_id.into(),
                phase: "running".into(),
                profile: "unconfirmed".into(),
                wrapped: true,
            },
            fd,
        );
    }
}

fn recover_handoff(session: &Session) {
    if !session.wrapped || session.phase == "failed" {
        return;
    }
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return;
    };
    for entry in entries.flatten() {
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|s| s.parse::<u32>().ok())
        else {
            continue;
        };
        if pid == session.pid {
            continue;
        }
        let Ok(fd) = pidfd(pid) else {
            continue;
        };
        let Ok(bytes) = std::fs::read(entry.path().join("environ")) else {
            continue;
        };
        let env: BTreeMap<&str, &str> = bytes
            .split(|b| *b == 0)
            .filter_map(|s| std::str::from_utf8(s).ok()?.split_once('='))
            .collect();
        if env.get("PENGUIN_BURNER_SESSION_ID") != Some(&session.session_id.as_str())
            || env.get("PENGUIN_BURNER_GAME_KEY") != Some(&session.app_id.as_str())
        {
            continue;
        }
        let mut successor = session.clone();
        successor.pid = pid;
        let _ = register_with_fd(successor, fd);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn handoff_requires_full_executable_in_the_same_wine_prefix() {
        let root = tempfile::tempdir().unwrap();
        let prefix = root.path();
        std::fs::create_dir(prefix.join("dosdevices")).unwrap();
        std::fs::create_dir(prefix.join("drive_c")).unwrap();
        std::os::unix::fs::symlink("../drive_c", prefix.join("dosdevices/c:")).unwrap();
        let exe = prefix.join("drive_c/NFS.exe");
        std::fs::write(&exe, "").unwrap();
        let expected = exe.to_str().unwrap();
        assert!(executable_matches(
            r"C:\NFS.exe",
            prefix.to_str().unwrap(),
            expected
        ));
        assert!(!executable_matches(
            r"C:\NFS.exe",
            "/other-prefix",
            expected
        ));
        assert!(!executable_matches(
            r"C:\EADesktop.exe",
            prefix.to_str().unwrap(),
            expected
        ));
        assert!(!executable_matches(
            "NFS.exe",
            prefix.to_str().unwrap(),
            expected
        ));
        assert!(!executable_matches(r"C:\NFS.exe", "", expected));
    }

    #[test]
    fn old_exit_cannot_remove_a_replacement_session() {
        let mut state = Registry::new();
        state.sessions.insert(
            42,
            Session {
                pid: 42,
                app_id: "heroic:test".into(),
                session_id: "new".into(),
                phase: "running".into(),
                profile: "applied".into(),
                wrapped: true,
            },
        );
        state.remove(42, "old");
        assert_eq!(state.sessions.len(), 1);
        state.remove(42, "new");
        assert_eq!(state.snapshot()["sessions"], json!([]));
        assert_eq!(state.sequence, 1);
    }

    #[test]
    fn snapshot_and_subscription_share_one_sequence() {
        let mut state = Registry::new();
        let (mut read, write) = UnixStream::pair().unwrap();
        write.set_nonblocking(true).unwrap();
        state.subscribers.insert(1, write);
        let before = state.snapshot();
        state.changed();
        let mut byte = [0];
        read.read_exact(&mut byte).unwrap();
        assert_eq!(before["sequence"], 0);
        assert_eq!(state.snapshot()["sequence"], 1);
    }
}
