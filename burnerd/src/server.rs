//! JSON-lines socket server: bind + chmod the socket, accept one thread per
//! connection, gate on SO_PEERCRED, and route requests to `api`/`scan`.

use std::env;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::{FileTypeExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::thread;

use nix::sys::socket::{getsockopt, sockopt::PeerCredentials};
use serde_json::Value;

use crate::api::{self, StreamError};
use crate::paths::DAEMON_ALLOWED_UID_ENV;
use crate::scan;
use crate::supervisor::{ChildKind, Supervisor};

const UID_DENIED_LINE: &[u8] = b"{\"ok\":false,\"error\":\"daemon client uid is not allowed\"}\n";
/// Per-request cap on the world-connectable (0666) socket: the maximum content
/// bytes of one request line, EXCLUDING its trailing newline. A client that
/// streams bytes without a newline gets a bounded error, not an unbounded buffer
/// in the root daemon. Generous — real requests are < 4 KiB.
const MAX_REQUEST_BYTES: u64 = 1024 * 1024;

/// Bind and prepare the listening socket. Errors carry the exact Python message
/// for the "path exists and is not a socket" case.
pub fn bind(path: &Path) -> anyhow::Result<UnixListener> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    if path.exists() {
        let metadata = fs::metadata(path)?;
        if !metadata.file_type().is_socket() {
            anyhow::bail!(
                "daemon socket path exists and is not a socket: {}",
                path.display()
            );
        }
        fs::remove_file(path)?;
    }
    let listener = UnixListener::bind(path)?;
    // 0o666 so the desktop user can connect without a shared group; the real
    // access control is the SO_PEERCRED gate in peer_uid_allowed (which fails
    // closed when the daemon runs as root with no allowed UID configured).
    fs::set_permissions(path, fs::Permissions::from_mode(0o666))?;
    Ok(listener)
}

/// Accept loop. Each connection is handled on its own thread; a panic in a
/// connection is caught so it never takes the daemon down.
pub fn serve(listener: UnixListener, sup: Arc<Mutex<Supervisor>>) {
    for incoming in listener.incoming() {
        let stream = match incoming {
            Ok(stream) => stream,
            Err(err) => {
                crate::logging::warn(&format!("accept failed: {err}"));
                continue; // transient accept error
            }
        };
        let sup = sup.clone();
        let _ = thread::Builder::new()
            .name("penguin-burner-conn".to_string())
            .spawn(move || {
                let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    handle_connection(stream, &sup);
                }));
            });
    }
}

fn handle_connection(stream: UnixStream, sup: &Arc<Mutex<Supervisor>>) {
    if !peer_uid_allowed(&stream) {
        let mut writer = &stream;
        let _ = writer.write_all(UID_DENIED_LINE);
        let _ = writer.flush();
        return;
    }

    let reader = match stream.try_clone() {
        Ok(clone) => BufReader::new(clone),
        Err(_) => return,
    };
    let mut reader = reader;
    let mut writer = stream;
    let mut buffer = Vec::new();

    loop {
        buffer.clear();
        // Bounded read: `take` caps how much a single "line" may buffer. This
        // sits below every method (including the streaming and gpu_* ones), so
        // no request path can be fed an unbounded line. Read one byte past a
        // full-size line-plus-newline so an exactly-at-cap line is accepted and
        // a one-byte-over line is still detected as oversized.
        match (&mut reader)
            .take(MAX_REQUEST_BYTES + 2)
            .read_until(b'\n', &mut buffer)
        {
            Ok(0) => return, // EOF
            Ok(_) => {}
            Err(_) => return,
        }
        // Measure content excluding a single trailing newline, so a line whose
        // content is exactly the cap is accepted (the newline does not count).
        let content_len = buffer.len() - usize::from(buffer.last() == Some(&b'\n'));
        if content_len as u64 > MAX_REQUEST_BYTES {
            // Oversized request: answer with a bounded error and close (the
            // remainder of the line cannot be resynced).
            let _ = api::write_response(
                &mut writer,
                Err(format!("request line exceeds {MAX_REQUEST_BYTES} bytes")),
            );
            return;
        }
        let decoded = String::from_utf8_lossy(&buffer);
        let line = decoded.trim();
        if line.is_empty() {
            continue;
        }
        match serde_json::from_str::<Value>(line) {
            Ok(value) => {
                if let Some(kind) = streaming_kind(&value) {
                    handle_start_stream(kind, &value, sup, &mut writer);
                    return; // streaming consumes the connection
                }
                let response =
                    if value.get("method").and_then(Value::as_str) == Some("client_identity") {
                        client_identity(&writer, &value)
                    } else {
                        api::handle_request(sup, &value)
                    };
                if !api::write_response(&mut writer, response) {
                    return;
                }
            }
            Err(err) => {
                // Malformed JSON: same envelope shape as Python; the parser's
                // message text differs (serde_json vs Python json) — see STATUS.
                if !api::write_response(&mut writer, Err(err.to_string())) {
                    return;
                }
            }
        }
    }
}

/// Credentials are reported in the daemon's host PID namespace, including for
/// Flatpak clients. Never accept a caller-supplied PID for this read-only query.
fn client_identity(stream: &UnixStream, value: &Value) -> Result<api::MethodResult, String> {
    if value.as_object().is_none_or(|object| object.len() != 1) {
        return Err("client_identity accepts only method".to_string());
    }
    let credentials = getsockopt(stream, PeerCredentials).map_err(|error| error.to_string())?;
    if credentials.pid() <= 0 {
        return Err("client host PID is unavailable".to_string());
    }
    Ok(api::MethodResult::Value(serde_json::json!({
        "pid": credentials.pid(), "uid": credentials.uid()
    })))
}

fn streaming_kind(value: &Value) -> Option<ChildKind> {
    match value.get("method").and_then(Value::as_str) {
        Some("start_auto_uv_scan") => Some(ChildKind::Scan),
        Some("start_profile_verification") => Some(ChildKind::Verify),
        _ => None,
    }
}

fn handle_start_stream(
    kind: ChildKind,
    value: &Value,
    sup: &Arc<Mutex<Supervisor>>,
    writer: &mut UnixStream,
) {
    // `streaming_kind` already established this is an object with a string method.
    let object = value.as_object().expect("streaming request is an object");
    let mut unknown: Vec<&str> = object
        .keys()
        .filter(|key| key.as_str() != "method" && key.as_str() != "options")
        .map(String::as_str)
        .collect();
    unknown.sort_unstable();
    if !unknown.is_empty() {
        api::write_json_line(
            writer,
            &StreamError::new(format!("unknown request field: {}", unknown.join(", "))),
        );
        return;
    }
    let options = object.get("options").cloned().unwrap_or(Value::Null);
    match kind {
        ChildKind::Scan => scan::run_scan(sup, &options, writer),
        ChildKind::Verify => scan::run_verification(sup, &options, writer),
    }
}

fn peer_uid_allowed(stream: &UnixStream) -> bool {
    let allowed = env::var(DAEMON_ALLOWED_UID_ENV).unwrap_or_default();
    let allowed = allowed.trim().to_string();
    peer_uid_allowed_with(&allowed, crate::paths::geteuid_is_root(), || {
        peer_uid(stream)
    })
}

/// Split out for testing. The socket is world-connectable, so when the daemon
/// runs as root the allowed-UID gate is the ONLY access control — an empty
/// allowlist must therefore fail CLOSED (a root install that never resolved a
/// desktop UID must not accept commands from every local user). An unprivileged
/// daemon has nothing to protect, so it stays permissive.
fn peer_uid_allowed_with(
    allowed: &str,
    running_as_root: bool,
    peer_uid: impl FnOnce() -> Option<u32>,
) -> bool {
    if allowed.is_empty() {
        if running_as_root {
            log_gate_disabled_once();
            return false;
        }
        return true;
    }
    match peer_uid() {
        Some(uid) => uid == 0 || uid.to_string() == allowed,
        None => false,
    }
}

fn log_gate_disabled_once() {
    use std::sync::atomic::{AtomicBool, Ordering};
    static LOGGED: AtomicBool = AtomicBool::new(false);
    if !LOGGED.swap(true, Ordering::Relaxed) {
        crate::logging::error(
            "refusing client connections: running as root with no \
             PENGUIN_BURNER_DAEMON_ALLOWED_UID configured. Reinstall the \
             service from the PenguinBurner UI (or set the allowed UID in the \
             unit) so the peer-UID gate can enforce access.",
        );
    }
}

/// Read the connecting peer's uid via SO_PEERCRED (mirrors the Python daemon's
/// `struct.unpack("3i", ...)` on the same option).
fn peer_uid(stream: &UnixStream) -> Option<u32> {
    getsockopt(stream, PeerCredentials)
        .ok()
        .map(|credentials| credentials.uid())
}

#[cfg(test)]
mod tests {
    use super::peer_uid_allowed_with;

    #[test]
    fn client_identity_uses_socket_credentials() {
        let (client, _server) = std::os::unix::net::UnixStream::pair().unwrap();
        let result =
            super::client_identity(&client, &serde_json::json!({"method": "client_identity"}))
                .unwrap();
        let value = serde_json::to_value(result).unwrap();
        assert_eq!(value["pid"], std::process::id());
        assert!(super::client_identity(
            &client,
            &serde_json::json!({
                "method": "client_identity", "pid": 1
            })
        )
        .is_err());
    }

    #[test]
    fn empty_allowlist_fails_closed_as_root() {
        // The world-connectable socket + a root daemon means an empty gate
        // must reject everyone, not accept everyone (the pre-fix hole).
        assert!(!peer_uid_allowed_with("", true, || Some(1000)));
        assert!(!peer_uid_allowed_with("", true, || Some(0)));
    }

    #[test]
    fn empty_allowlist_is_permissive_when_unprivileged() {
        // A non-root daemon holds no privilege worth gating.
        assert!(peer_uid_allowed_with("", false, || Some(1000)));
    }

    #[test]
    fn configured_allowlist_matches_uid_or_root() {
        assert!(peer_uid_allowed_with("1000", true, || Some(1000)));
        assert!(peer_uid_allowed_with("1000", true, || Some(0)));
        assert!(!peer_uid_allowed_with("1000", true, || Some(1001)));
        assert!(!peer_uid_allowed_with("1000", true, || None));
    }
}
