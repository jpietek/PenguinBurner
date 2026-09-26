//! Integration tests: build+spawn the real binary on a temp socket, drive it with
//! raw JSON-line requests, and assert wire-level parity with the Python daemon.
//!
//! A stub `python3` script stands in for the Auto-UV scan child. Its behavior is
//! selected by env the daemon inherits and forwards to the child.

use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};

use serde_json::Value;

static COUNTER: AtomicU32 = AtomicU32::new(0);

const STUB: &str = r#"import os, sys, signal, time

argv_file = os.environ.get("SCAN_STUB_ARGV_FILE")
if argv_file:
    with open(argv_file, "w") as handle:
        import json
        handle.write(json.dumps(sys.argv[1:]))

sys.stdout.write('{"event":"auto_uv_start"}\n')
sys.stdout.write('human line\n')
sys.stdout.flush()

if os.environ.get("SCAN_STUB_EXIT_AFTER_LINES") == "1":
    sys.exit(0)

if os.environ.get("SCAN_STUB_IGNORE_SIGINT") == "1":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
else:
    signal.signal(signal.SIGINT, lambda *_: os._exit(0))

initial_ppid = os.getppid()
while True:
    # Exit if the daemon (our parent) died and we were reparented.
    if os.getppid() != initial_ppid:
        os._exit(0)
    time.sleep(0.05)
"#;

struct Daemon {
    child: Child,
    socket: PathBuf,
    home: PathBuf,
    state_file: PathBuf,
    dir: PathBuf,
}

impl Daemon {
    fn start(extra_env: &[(&str, &str)]) -> Daemon {
        let n = COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("pb-int-{}-{}", std::process::id(), n));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let home = dir.join("home");
        std::fs::create_dir_all(&home).unwrap();
        let socket = dir.join("sock");
        let state_file = dir.join("state.json");
        let boot_state_file = dir.join("boot-state.json");
        let stub = dir.join("scan_stub.py");
        std::fs::write(&stub, STUB).unwrap();
        // Generic daemon tests exercise the established always-attached path.
        // Declare that fake machine as Desktop explicitly; no visible GPU now
        // correctly resolves to Unknown and must stay detached.
        let (default_rtd3_sys, default_rtd3_proc) =
            write_rtd3_tree(&dir, "Disabled by default", "auto", "active");

        let mut command = Command::new(env!("CARGO_BIN_EXE_penguin-burnerd"));
        command
            .arg("--socket")
            .arg(&socket)
            .env("PENGUIN_BURNER_DAEMON_PROGRAM_FILE", &stub)
            .env("PENGUIN_BURNER_HOME", &home)
            .env("PENGUIN_BURNERD_TEST_STATE_FILE", &state_file)
            .env("PENGUIN_BURNERD_TEST_BOOT_STATE_FILE", &boot_state_file)
            .env("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1")
            .env("PENGUIN_BURNERD_TEST_TIMINGS", "1")
            .env("PENGUIN_BURNERD_TEST_RTD3_SYSFS", default_rtd3_sys)
            .env("PENGUIN_BURNERD_TEST_RTD3_PROC", default_rtd3_proc)
            .env_remove("PENGUIN_BURNER_DAEMON_ALLOWED_UID")
            .env_remove("NOTIFY_SOCKET")
            .stdout(Stdio::null())
            // The daemon logs to stderr (journald's view); capture it so
            // tests can assert the journal narrative the docs promise.
            .stderr(Stdio::from(
                std::fs::File::create(dir.join("daemon.stderr.log")).unwrap(),
            ));
        for (key, value) in extra_env {
            command.env(key, value);
        }
        let child = command.spawn().expect("spawn daemon");

        let daemon = Daemon {
            child,
            socket,
            home,
            state_file,
            dir,
        };
        daemon.wait_for_socket();
        daemon
    }

    fn wait_for_socket(&self) {
        for _ in 0..200 {
            if self.socket.exists() {
                return;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        panic!("socket was not created: {}", self.socket.display());
    }

    fn connect(&self) -> UnixStream {
        let stream = UnixStream::connect(&self.socket).expect("connect");
        stream
            .set_read_timeout(Some(Duration::from_secs(10)))
            .unwrap();
        stream
    }

    /// Send one request, read exactly one response line, parse it.
    fn request(&self, request: &str) -> Value {
        let mut stream = self.connect();
        stream.write_all(request.as_bytes()).unwrap();
        stream.write_all(b"\n").unwrap();
        stream.flush().unwrap();
        let mut reader = BufReader::new(stream);
        let line = read_line(&mut reader).expect("a response line");
        serde_json::from_str(&line).expect("valid JSON response")
    }

    /// Everything the daemon has logged so far (its stderr — what journald
    /// would show).
    fn stderr_log(&self) -> String {
        std::fs::read_to_string(self.dir.join("daemon.stderr.log")).unwrap_or_default()
    }

    fn stop_request_file(&self) -> PathBuf {
        self.home
            .join(".config")
            .join("PenguinBurner")
            .join("auto-uv-stop-requested")
    }

    fn verify_stop_request_file(&self) -> PathBuf {
        self.home
            .join(".config")
            .join("PenguinBurner")
            .join("profile-verify-stop-requested")
    }

    fn profiles_dir(&self) -> PathBuf {
        self.home
            .join(".config")
            .join("PenguinBurner")
            .join("auto-uv-profiles")
    }

    /// Send a streaming start request, assert the `started` frame, and return
    /// the connection reader plus the child pid.
    fn start_streaming(&self, request: &str) -> (UnixStream, BufReader<UnixStream>, u32) {
        let mut stream = self.connect();
        stream.write_all(request.as_bytes()).unwrap();
        stream.write_all(b"\n").unwrap();
        stream.flush().unwrap();
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        let started: Value = serde_json::from_str(&read_line(&mut reader).unwrap()).unwrap();
        assert_eq!(started["control"], "started", "frame: {started}");
        let pid = started["pid"].as_u64().unwrap() as u32;
        (stream, reader, pid)
    }
}

impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        // Give reparented stub children a moment to notice and self-exit.
        std::thread::sleep(Duration::from_millis(150));
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn read_line<R: BufRead>(reader: &mut R) -> Option<String> {
    let mut line = String::new();
    match reader.read_line(&mut line) {
        Ok(0) => None,
        Ok(_) => Some(line),
        Err(_) => None,
    }
}

fn mock_gpu_lifetime(path: &std::path::Path) -> (u64, u64, u64) {
    let values: Vec<u64> = std::fs::read_to_string(path)
        .unwrap_or_default()
        .split_whitespace()
        .filter_map(|value| value.parse().ok())
        .collect();
    match values.as_slice() {
        [opens, closes, live] => (*opens, *closes, *live),
        _ => (0, 0, 0),
    }
}

fn test_runtime_spec() -> Value {
    serde_json::json!({
        "format_version": 1,
        "gpu": {
            "uuid": "GPU-inert-test",
            "index_at_resolution": 0,
            "pci_bus_id": "0000:01:00.0",
            "name": "Inert test GPU"
        },
        "mode": "stock",
        "static_profile": null,
        "adaptive": null,
        "fan": {
            "enabled": false,
            "config": {
                "poll_interval_s": 2.0,
                "curve": [[55.0, 30.0], [80.0, 45.0]],
                "hysteresis_c": 2.0,
                "mode": "linear",
                "min_fan_speed_pct": 20,
                "max_fan_speed_pct": 100,
                "max_step_up_pct_per_s": 25.0,
                "max_step_down_pct_per_s": 15.0,
                "manual_enable_temp_c": 55.0,
                "auto_restore_temp_c": 50.0,
                "emergency_auto_override_temp_c": 80.0,
                "emergency_auto_resume_temp_c": 75.0,
                "force_update_every_poll": false,
                "curve_source": null,
                "curve_source_path": null
            },
            "notice": ""
        },
        "policy": {"enable_persistence_mode": false},
        "overlay": {"enabled": false, "update_interval_s": 1}
    })
}

fn test_runtime_profile(profile_id: &str, tier: &str, target_mhz: i64) -> Value {
    serde_json::json!({
        "path": format!("/tmp/{profile_id}.json"),
        "profile_id": profile_id,
        "profile_tier": match tier {
            "efficiency" => "Efficiency",
            "performance" => "Performance",
            _ => "Balanced",
        },
        "profile_tier_key": tier,
        "plan": [{
            "index": 12,
            "voltage_mv": 900,
            "base_mhz": 2500,
            "target_mhz": target_mhz,
            "new_offset_mhz": target_mhz - 2500,
        }],
        "lock_clock_mhz": target_mhz,
        "candidate_voltage_mv": 900,
        "memory_offset_mhz": 1000,
        "power_limit_w": 319,
        "flatten_target": {
            "source": "auto-uv-final",
            "lock_clock_mhz": target_mhz,
            "lock_voltage_mv": 900,
            "end_voltage_mv": 1100,
            "tail_point_count": 6,
            "ceiling_clock_mhz": null,
            "tail_rise_bins": 0,
        },
    })
}

fn test_static_runtime_spec(profile_id: &str) -> Value {
    let mut spec = test_runtime_spec();
    spec["mode"] = Value::String("static".to_string());
    spec["static_profile"] = test_runtime_profile(profile_id, "balanced", 2722);
    spec
}

fn test_adaptive_runtime_spec() -> Value {
    let mut spec = test_runtime_spec();
    spec["mode"] = Value::String("adaptive".to_string());
    spec["adaptive"] = serde_json::json!({
        "initial_tier": "balanced",
        "profiles": {
            "efficiency": test_runtime_profile("adaptive-efficiency", "efficiency", 2550),
            "balanced": test_runtime_profile("adaptive-balanced", "balanced", 2722),
            "performance": test_runtime_profile("adaptive-performance", "performance", 2900),
        },
        "policy": {
            "target_fps": 90.0,
            "target_slow_windows": 3,
            "near_slow_windows": 3,
            "comfort_windows": 3,
            "performance_comfort_windows": 3,
            "demote_dwell_s": 5.0,
            "performance_demote_dwell_s": 5.0,
            "cpu_bound_gpu_util_max_pct": 80.0,
            "cpu_bound_peak_thread_min_pct": 80.0,
            "cpu_bound_process_util_min_pct": 50.0,
            // All six deliberately non-default: a default value is skipped
            // when the daemon re-serializes state, so only non-default values
            // exercise the full persist round-trip.
            "frame_cap_enter_gpu_pct": 55.0,
            "frame_cap_exit_gpu_pct": 85.0,
            "frame_cap_confirm_windows": 2,
            "frame_cap_exit_pacing_pct": 20.0,
            "desktop_idle_gpu_pct": 15.0,
            "desktop_idle_after_s": 45.0,
        },
    });
    spec
}

fn runtime_spec_request(method: &str, spec: &Value) -> String {
    serde_json::json!({"method": method, "spec": spec}).to_string()
}

fn apply_runtime_request() -> String {
    runtime_spec_request("apply_runtime_spec", &test_runtime_spec())
}

fn finish_successful_scan(daemon: &Daemon) {
    let (_stream, mut reader, _pid) =
        daemon.start_streaming(r#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#);
    let mut exit_code = None;
    while let Some(line) = read_line(&mut reader) {
        let frame: Value = serde_json::from_str(&line).unwrap();
        if frame["control"] == "finished" {
            exit_code = frame["exit_code"].as_i64();
        }
    }
    assert_eq!(exit_code, Some(0));
}

fn pid_alive(pid: u32) -> bool {
    // SAFETY: kill(pid, 0) only probes for existence.
    unsafe { libc::kill(pid as libc::pid_t, 0) == 0 }
}

// --- tests -------------------------------------------------------------------

#[test]
fn version_flag_prints_identity_and_exits_zero() {
    // The Flatpak install transaction pre-flights the staged binary with
    // --version before committing; exit 0 plus the version line is the
    // contract that probe relies on.
    let output = Command::new(env!("CARGO_BIN_EXE_penguin-burnerd"))
        .arg("--version")
        .output()
        .expect("run penguin-burnerd --version");
    assert!(output.status.success());
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.starts_with("penguin-burnerd "));
    assert!(stdout.contains(env!("CARGO_PKG_VERSION")));
}

#[test]
fn status_is_idle_when_nothing_running() {
    let daemon = Daemon::start(&[]);
    let response = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(response["ok"], Value::Bool(true));
    let result = &response["result"];
    assert_eq!(result["state"], "idle");
    assert_eq!(result["active_job"], Value::Null);
    assert!(result["version"].is_string());
    assert!(result["build"].is_string());
}

#[test]
fn scan_streams_started_lines_finished() {
    let argv_file = std::env::temp_dir().join(format!("pb-argv-{}.json", std::process::id()));
    let _ = std::fs::remove_file(&argv_file);
    let daemon = Daemon::start(&[
        ("SCAN_STUB_EXIT_AFTER_LINES", "1"),
        ("SCAN_STUB_ARGV_FILE", argv_file.to_str().unwrap()),
    ]);

    let mut stream = daemon.connect();
    stream
        .write_all(br#"{"method":"start_auto_uv_scan","options":{"gpu_index":0,"auto_uv_mode":"performance"}}"#)
        .unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();

    let mut reader = BufReader::new(stream);
    let mut frames = Vec::new();
    while let Some(line) = read_line(&mut reader) {
        frames.push(serde_json::from_str::<Value>(&line).unwrap());
    }

    assert_eq!(frames[0]["ok"], Value::Bool(true));
    assert_eq!(frames[0]["control"], "started");
    assert!(frames[0]["pid"].is_number());
    assert_eq!(frames[1]["line"], "{\"event\":\"auto_uv_start\"}\n");
    assert_eq!(frames[2]["line"], "human line\n");
    assert_eq!(frames[3]["control"], "finished");
    assert_eq!(frames[3]["exit_code"], 0);

    // The child was launched with the exact scan argv.
    let argv: Value = serde_json::from_str(&std::fs::read_to_string(&argv_file).unwrap()).unwrap();
    assert_eq!(
        argv,
        serde_json::json!([
            "--auto-uv-voltage-scan",
            "--json-events",
            "--auto-uv-require-final-choice",
            "--gpu-index",
            "0",
            "--auto-uv-mode",
            "performance"
        ])
    );
    let _ = std::fs::remove_file(&argv_file);
}

#[test]
fn second_scan_is_refused_while_one_runs() {
    let daemon = Daemon::start(&[]);

    // First scan keeps running (the default stub loops until signalled).
    let mut first = daemon.connect();
    first
        .write_all(br#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#)
        .unwrap();
    first.write_all(b"\n").unwrap();
    first.flush().unwrap();
    let mut first_reader = BufReader::new(first);
    let started = read_line(&mut first_reader).unwrap();
    assert!(started.contains("\"started\""));

    // Second scan on a fresh connection is refused.
    let second = daemon.request(r#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#);
    assert_eq!(second["ok"], Value::Bool(false));
    assert_eq!(second["error"], "Auto-UV scan is already running");
}

#[test]
fn stop_auto_uv_writes_offer_stop_file() {
    let daemon = Daemon::start(&[]);

    let mut stream = daemon.connect();
    stream
        .write_all(br#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#)
        .unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();
    let mut reader = BufReader::new(stream);
    let started: Value = serde_json::from_str(&read_line(&mut reader).unwrap()).unwrap();
    assert_eq!(started["control"], "started");
    let scan_pid = started["pid"].as_u64().unwrap() as u32;

    let stop = daemon.request(r#"{"method":"stop_auto_uv_scan"}"#);
    assert_eq!(stop["ok"], Value::Bool(true));
    assert_eq!(stop["result"]["stopped"], Value::Bool(true));

    // The stop-request marker exists with the offer reason.
    let path = daemon.stop_request_file();
    let mut content = String::new();
    for _ in 0..100 {
        if let Ok(text) = std::fs::read_to_string(&path) {
            content = text;
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(
        content,
        "stop requested by PenguinBurner daemon client\nreason=offer-final-choice\n"
    );

    // The SIGINT actually reaches the child (the child's signal mask is reset
    // at spawn; the daemon itself blocks SIGINT process-wide).
    wait_until(|| !pid_alive(scan_pid), Duration::from_secs(5));
    assert!(!pid_alive(scan_pid), "scan child did not receive SIGINT");
}

#[test]
fn disconnect_writes_abort_stop_file_and_ends_scan() {
    let daemon = Daemon::start(&[]);

    let mut stream = daemon.connect();
    stream
        .write_all(br#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#)
        .unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let started: Value = serde_json::from_str(&read_line(&mut reader).unwrap()).unwrap();
    assert_eq!(started["control"], "started");
    let scan_pid = started["pid"].as_u64().unwrap() as u32;

    // Disconnect mid-stream: drop both the reader clone and the stream.
    drop(reader);
    drop(stream);

    // The daemon writes an abort stop-request and SIGINTs the child (which exits).
    let path = daemon.stop_request_file();
    let mut content = String::new();
    for _ in 0..200 {
        if let Ok(text) = std::fs::read_to_string(&path) {
            content = text;
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(
        content,
        "stop requested by PenguinBurner daemon client\nreason=abort-final-choice\n"
    );

    // The scan child terminates and the daemon returns to idle.
    wait_until(|| !pid_alive(scan_pid), Duration::from_secs(5));
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "idle");
}

#[test]
fn kill_ladder_terminates_a_sigint_ignoring_scan() {
    let daemon = Daemon::start(&[("SCAN_STUB_IGNORE_SIGINT", "1")]);

    let mut stream = daemon.connect();
    stream
        .write_all(br#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#)
        .unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let started: Value = serde_json::from_str(&read_line(&mut reader).unwrap()).unwrap();
    let scan_pid = started["pid"].as_u64().unwrap() as u32;
    assert!(pid_alive(scan_pid));

    // Disconnect: SIGINT is ignored, so the ladder escalates to SIGTERM/SIGKILL.
    drop(reader);
    drop(stream);

    // Under PENGUIN_BURNERD_TEST_TIMINGS the ladder fires within ~2s.
    wait_until(|| !pid_alive(scan_pid), Duration::from_secs(8));
    assert!(
        !pid_alive(scan_pid),
        "kill ladder did not terminate the scan"
    );
}

#[test]
fn runtime_spec_apply_stop_transitions_and_state_file() {
    let daemon = Daemon::start(&[]);
    let daemon_pid = daemon.child.id();

    let start = daemon.request(&apply_runtime_request());
    assert_eq!(start["ok"], Value::Bool(true));
    let result = &start["result"];
    assert_eq!(result["started"], Value::Bool(true));
    assert_eq!(result["pid"], daemon_pid);
    assert_eq!(result["runtime_mode"], "stock");
    assert_eq!(result["gpu_uuid"], "GPU-inert-test");

    let status = daemon.request(r#"{"method":"status"}"#);
    let status_result = &status["result"];
    assert_eq!(status_result["state"], "runtime_profile_running");
    let job = &status_result["active_job"];
    assert_eq!(job["type"], "runtime_profile");
    assert_eq!(job["pid"], daemon_pid);
    assert_eq!(job["returncode"], Value::Null);
    assert_eq!(job["runtime_mode"], "stock");
    assert_eq!(job["gpu_uuid"], "GPU-inert-test");
    assert_eq!(job["silent_fan_curve"], Value::Bool(false));

    // State file persists the validated typed spec.
    let state: Value =
        serde_json::from_str(&std::fs::read_to_string(&daemon.state_file).unwrap()).unwrap();
    assert_eq!(state["mode"], "stock");
    assert_eq!(state["gpu"]["uuid"], "GPU-inert-test");

    // Explicit stop clears current-session recovery state.
    let stop = daemon.request(r#"{"method":"stop_runtime_profile"}"#);
    assert_eq!(stop["result"]["stopped"], Value::Bool(true));
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "idle");
    assert!(!daemon.state_file.exists());
}

#[test]
fn autostart_runs_the_persisted_runtime_profile() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-seed-{}-{}", std::process::id(), n));
    std::fs::create_dir_all(&dir).unwrap();
    let state_file = dir.join("state.json");
    std::fs::write(&state_file, test_runtime_spec().to_string()).unwrap();

    let daemon = Daemon::start(&[(
        "PENGUIN_BURNERD_TEST_STATE_FILE",
        state_file.to_str().unwrap(),
    )]);
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "runtime_profile_running");
    assert_eq!(status["result"]["active_job"]["runtime_mode"], "stock");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn completed_scan_restores_exact_selected_profile_instead_of_boot_profile() {
    let daemon = Daemon::start(&[("SCAN_STUB_EXIT_AFTER_LINES", "1")]);
    let boot = test_static_runtime_spec("boot-profile");
    let selected = test_static_runtime_spec("selected-custom-profile");

    assert_eq!(
        daemon.request(&runtime_spec_request("set_boot_runtime_spec", &boot))["ok"],
        Value::Bool(true)
    );
    assert_eq!(
        daemon.request(&runtime_spec_request("apply_runtime_spec", &selected))["ok"],
        Value::Bool(true)
    );

    finish_successful_scan(&daemon);

    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "runtime_profile_running");
    assert_eq!(status["result"]["active_job"]["runtime_mode"], "static");
    assert_eq!(
        status["result"]["active_job"]["profile_id"],
        "selected-custom-profile"
    );
    let restored: Value =
        serde_json::from_str(&std::fs::read_to_string(&daemon.state_file).unwrap()).unwrap();
    assert_eq!(restored, selected);
}

#[test]
fn completed_scan_restores_exact_adaptive_runtime_instead_of_boot_profile() {
    let daemon = Daemon::start(&[("SCAN_STUB_EXIT_AFTER_LINES", "1")]);
    let boot = test_static_runtime_spec("boot-profile");
    let adaptive = test_adaptive_runtime_spec();

    assert_eq!(
        daemon.request(&runtime_spec_request("set_boot_runtime_spec", &boot))["ok"],
        Value::Bool(true)
    );
    assert_eq!(
        daemon.request(&runtime_spec_request("apply_runtime_spec", &adaptive))["ok"],
        Value::Bool(true)
    );

    finish_successful_scan(&daemon);

    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "runtime_profile_running");
    assert_eq!(status["result"]["active_job"]["runtime_mode"], "adaptive");
    assert_eq!(
        status["result"]["active_job"]["profile_id"],
        "adaptive-balanced"
    );
    let restored: Value =
        serde_json::from_str(&std::fs::read_to_string(&daemon.state_file).unwrap()).unwrap();
    assert_eq!(restored, adaptive);
}

#[test]
fn boot_runtime_specs_are_saved_and_cleared_per_gpu() {
    let daemon = Daemon::start(&[]);
    let mut gpu_a = test_static_runtime_spec("profile-a");
    gpu_a["gpu"]["uuid"] = Value::String("GPU-A".to_string());
    gpu_a["gpu"]["index_at_resolution"] = Value::from(0);
    let mut gpu_b = test_static_runtime_spec("profile-b");
    gpu_b["gpu"]["uuid"] = Value::String("GPU-B".to_string());
    gpu_b["gpu"]["index_at_resolution"] = Value::from(1);

    let saved_a = daemon.request(&runtime_spec_request("set_boot_runtime_spec", &gpu_a));
    assert_eq!(saved_a["result"]["saved_gpu_count"], 1);
    assert_eq!(saved_a["result"]["active_gpu_uuid"], "GPU-A");

    let saved_b = daemon.request(&runtime_spec_request("set_boot_runtime_spec", &gpu_b));
    assert_eq!(saved_b["result"]["saved_gpu_count"], 2);
    assert_eq!(saved_b["result"]["active_gpu_uuid"], "GPU-B");

    let summary = daemon.request(r#"{"method":"boot_runtime_spec"}"#);
    assert_eq!(summary["result"]["configured"], true);
    assert_eq!(summary["result"]["active_gpu_uuid"], "GPU-B");
    assert_eq!(summary["result"]["gpus"].as_array().unwrap().len(), 2);
    assert_eq!(summary["result"]["gpus"][0]["gpu_uuid"], "GPU-A");
    assert_eq!(summary["result"]["gpus"][1]["gpu_uuid"], "GPU-B");

    let selected_a = daemon.request(r#"{"method":"set_boot_main_gpu","gpu_uuid":"GPU-A"}"#);
    assert_eq!(selected_a["result"]["selected"], true);
    assert_eq!(selected_a["result"]["main_gpu_uuid"], "GPU-A");
    assert_eq!(selected_a["result"]["active_gpu_uuid"], "GPU-A");

    // An explicit Main GPU survives later startup-profile saves for another
    // NVIDIA card instead of silently reverting to last-saved-wins.
    let resaved_b = daemon.request(&runtime_spec_request("set_boot_runtime_spec", &gpu_b));
    assert_eq!(resaved_b["result"]["main_gpu_uuid"], "GPU-A");
    assert_eq!(resaved_b["result"]["active_gpu_uuid"], "GPU-A");

    let rejected = daemon.request(r#"{"method":"set_boot_main_gpu","gpu_uuid":"GPU-MISSING"}"#);
    assert_eq!(rejected["ok"], Value::Bool(false));
    assert_eq!(rejected["error"], "main GPU has no saved boot runtime spec");
    let bad_type = daemon.request(r#"{"method":"set_boot_main_gpu","gpu_uuid":1}"#);
    assert_eq!(bad_type["ok"], Value::Bool(false));
    assert_eq!(bad_type["error"], "gpu_uuid must be a string");

    let cleared_main = daemon.request(r#"{"method":"set_boot_main_gpu","gpu_uuid":""}"#);
    assert_eq!(cleared_main["result"]["selected"], false);
    assert_eq!(cleared_main["result"]["main_gpu_uuid"], "");

    let cleared_a = daemon.request(r#"{"method":"clear_boot_runtime_spec","gpu_uuid":"GPU-A"}"#);
    assert_eq!(cleared_a["result"]["cleared"], true);
    assert_eq!(cleared_a["result"]["saved_gpu_count"], 1);
    let summary = daemon.request(r#"{"method":"boot_runtime_spec"}"#);
    assert_eq!(summary["result"]["gpus"].as_array().unwrap().len(), 1);
    assert_eq!(summary["result"]["gpus"][0]["gpu_uuid"], "GPU-B");

    let cleared_b = daemon.request(r#"{"method":"clear_boot_runtime_spec","gpu_uuid":"GPU-B"}"#);
    assert_eq!(cleared_b["result"]["saved_gpu_count"], 0);
    let summary = daemon.request(r#"{"method":"boot_runtime_spec"}"#);
    assert_eq!(summary["result"]["configured"], false);
}

#[test]
fn autostart_replays_available_gpus_by_uuid_and_reports_missing_targets() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-multi-boot-{}-{}", std::process::id(), n));
    std::fs::create_dir_all(&dir).unwrap();
    let boot_file = dir.join("boot.json");

    let mut gpu_a = test_static_runtime_spec("profile-a");
    gpu_a["gpu"]["uuid"] = Value::String("GPU-A".to_string());
    gpu_a["gpu"]["index_at_resolution"] = Value::from(0); // stale: now index 1
    let mut gpu_b = test_static_runtime_spec("profile-b");
    gpu_b["gpu"]["uuid"] = Value::String("GPU-B".to_string());
    gpu_b["gpu"]["index_at_resolution"] = Value::from(1); // stale: now index 0
                                                          // A missing selected stock target must not suppress the available static
                                                          // fallback that RTD3 is actually watching.
    let mut missing = test_runtime_spec();
    missing["gpu"]["uuid"] = Value::String("GPU-MISSING".to_string());
    missing["gpu"]["index_at_resolution"] = Value::from(2);
    std::fs::write(
        &boot_file,
        serde_json::json!({
            "format_version": 1,
            "active_gpu_uuid": "GPU-MISSING",
            "specs": [gpu_a, missing, gpu_b],
        })
        .to_string(),
    )
    .unwrap();

    let daemon = Daemon::start(&[
        (
            "PENGUIN_BURNERD_TEST_BOOT_STATE_FILE",
            boot_file.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_GPU_IDENTITIES",
            r#"[{"index":0,"uuid":"GPU-B"},{"index":1,"uuid":"GPU-A"}]"#,
        ),
    ]);

    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "runtime_profile_running");
    assert_eq!(status["result"]["active_job"]["gpu_uuid"], "GPU-B");

    let boot = daemon.request(r#"{"method":"boot_runtime_spec"}"#);
    assert_eq!(boot["result"]["active_gpu_uuid"], "GPU-MISSING");
    assert_eq!(boot["result"]["effective_active_gpu_uuid"], "GPU-B");
    assert_eq!(boot["result"]["gpus"].as_array().unwrap().len(), 3);
    assert_eq!(
        boot["result"]["replay"],
        serde_json::json!([
            {"gpu_uuid": "GPU-A", "gpu_index": 1, "outcome": "applied"},
            {
                "gpu_uuid": "GPU-MISSING",
                "outcome": "gpu-not-detected",
                "error": "runtime profile GPU GPU-MISSING is not currently detected",
            },
            {"gpu_uuid": "GPU-B", "gpu_index": 0, "outcome": "active"},
        ])
    );

    // A missing card remains configured for a future restart.
    assert_eq!(boot["result"]["configured"], true);
    let _ = std::fs::remove_dir_all(&dir);
}

/// F1 regression: the autostart engine thread spawns before the signal thread,
/// so it MUST inherit the SIGTERM/SIGINT block — otherwise the kernel delivers
/// a process-directed SIGTERM to it (the only thread with the signal unblocked)
/// and the daemon dies instantly with no cleanup. Clean shutdown is observable
/// as exit code 0 (`wait_and_shutdown` → `exit(0)`) + the socket unlinked;
/// signal death would report a signal, not a code, and leave the socket behind.
#[test]
fn sigterm_with_autostart_engine_runs_clean_shutdown() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-sigterm-{}-{}", std::process::id(), n));
    std::fs::create_dir_all(&dir).unwrap();
    let state_file = dir.join("state.json");
    std::fs::write(&state_file, test_runtime_spec().to_string()).unwrap();

    let mut daemon = Daemon::start(&[(
        "PENGUIN_BURNERD_TEST_STATE_FILE",
        state_file.to_str().unwrap(),
    )]);
    // The engine thread is live (spawned during autostart, before the socket).
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "runtime_profile_running");

    // SAFETY: SIGTERM to our own child daemon.
    unsafe {
        libc::kill(daemon.child.id() as libc::pid_t, libc::SIGTERM);
    }
    let deadline = Instant::now() + Duration::from_secs(10);
    let exit = loop {
        if let Some(exit) = daemon.child.try_wait().unwrap() {
            break exit;
        }
        assert!(
            Instant::now() < deadline,
            "daemon did not exit after SIGTERM"
        );
        std::thread::sleep(Duration::from_millis(25));
    };
    assert_eq!(
        exit.code(),
        Some(0),
        "daemon must exit through the clean-shutdown path, not signal death: {exit:?}"
    );
    assert!(
        !daemon.socket.exists(),
        "the shutdown path must unlink the socket"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn peercred_denies_non_matching_uid() {
    let daemon = Daemon::start(&[("PENGUIN_BURNER_DAEMON_ALLOWED_UID", "999999")]);
    let mut stream = daemon.connect();
    // The daemon writes the denial and closes before reading any request, so the
    // client just reads (writing to the closed socket would race into an RST).
    let mut text = String::new();
    stream.read_to_string(&mut text).unwrap();
    assert_eq!(
        text,
        "{\"ok\":false,\"error\":\"daemon client uid is not allowed\"}\n"
    );
}

#[test]
fn malformed_json_returns_error_envelope() {
    let daemon = Daemon::start(&[]);
    let mut stream = daemon.connect();
    stream.write_all(b"{bad json\n").unwrap();
    stream.flush().unwrap();
    let mut reader = BufReader::new(stream);
    let line = read_line(&mut reader).unwrap();
    let response: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(response["ok"], Value::Bool(false));
    assert!(response["error"].is_string());
}

#[test]
fn unknown_method_and_field_error_lines_are_byte_exact() {
    let daemon = Daemon::start(&[]);

    let mut stream = daemon.connect();
    stream.write_all(br#"{"method":"run_cli"}"#).unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let line = read_line(&mut reader).unwrap();
    assert_eq!(
        line,
        "{\"ok\":false,\"error\":\"unknown daemon method: run_cli\"}\n"
    );

    // Reuse the same connection: the non-streaming path loops.
    stream
        .write_all(br#"{"method":"status","argv":["--x"]}"#)
        .unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();
    let line = read_line(&mut reader).unwrap();
    assert_eq!(
        line,
        "{\"ok\":false,\"error\":\"unknown request field: argv\"}\n"
    );
}

#[test]
fn unknown_scan_option_is_rejected() {
    let daemon = Daemon::start(&[]);
    let mut stream = daemon.connect();
    stream
        .write_all(br#"{"method":"start_auto_uv_scan","options":{"bogus":1}}"#)
        .unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();
    let mut reader = BufReader::new(stream);
    let line = read_line(&mut reader).unwrap();
    assert_eq!(
        line,
        "{\"ok\":false,\"error\":\"unknown Auto-UV option: bogus\"}\n"
    );
}

fn wait_until<F: Fn() -> bool>(predicate: F, timeout: Duration) {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if predicate() {
            return;
        }
        std::thread::sleep(Duration::from_millis(25));
    }
}

// --- milestone B: GPU-write RPCs (mock-GPU seam) -------------------------------

const MOCK_GPU: (&str, &str) = ("PENGUIN_BURNERD_TEST_MOCK_GPU", "1");

#[test]
fn gpu_writes_run_against_the_mock_and_echo_ops() {
    let daemon = Daemon::start(&[MOCK_GPU]);

    let response = daemon.request(r#"{"method":"probe_power_limit_support","gpu_index":0}"#);
    assert_eq!(response["ok"], Value::Bool(true));
    assert_eq!(response["result"]["supported"], Value::Bool(true));
    assert_eq!(response["result"]["probe_power_limit_w"], 300);
    assert_eq!(
        response["result"]["mock_ops"],
        serde_json::json!(["ApplyPowerLimit { power_limit_w: 300 }"])
    );

    let response = daemon.request(
        r#"{"method":"gpu_apply_vf_offsets","gpu_index":0,"offsets":[[12,150000],[13,-15000]]}"#,
    );
    assert_eq!(response["ok"], Value::Bool(true));
    assert_eq!(response["result"]["applied"], 2);
    assert_eq!(
        response["result"]["mock_ops"],
        serde_json::json!(["ApplyVfOffsets { offsets: [(12, 150000), (13, -15000)] }"])
    );

    let response =
        daemon.request(r#"{"method":"gpu_apply_power_limit","gpu_index":0,"power_limit_w":300}"#);
    assert_eq!(response["result"]["applied_w"], 300);
    assert_eq!(
        response["result"]["mock_ops"],
        serde_json::json!(["ApplyPowerLimit { power_limit_w: 300 }"])
    );

    let response = daemon.request(
        r#"{"method":"gpu_apply_clock_offsets","gpu_index":0,"gpc_clk_vf_offset_mhz":30,"mem_clk_vf_offset_mhz":1500}"#,
    );
    assert_eq!(response["result"]["gpc_clk_vf_offset_mhz"], 30);
    assert_eq!(response["result"]["gpc_clk_vf_offset_readback_mhz"], 30);
    assert_eq!(response["result"]["mem_clk_vf_offset_mhz"], 1500);
    assert_eq!(response["result"]["mem_clk_vf_offset_readback_mhz"], 1500);

    // The mock's canned steps are 1800/1900/2000/2100: 1950 floors to 1900.
    let response = daemon
        .request(r#"{"method":"gpu_apply_locked_core_clock","gpu_index":0,"clock_mhz":1950}"#);
    assert_eq!(response["result"]["requested_clock_mhz"], 1950);
    assert_eq!(response["result"]["applied_clock_mhz"], 1900);
    assert_eq!(response["result"]["mode"], "floor");
    assert_eq!(
        response["result"]["mock_ops"],
        serde_json::json!([
            "ApplyLockedCoreClock { clock_mhz: 1950, prefer_not_above: true, snap: true }"
        ])
    );

    let response = daemon.request(
        r#"{"method":"gpu_apply_locked_core_clock_range","gpu_index":0,"min_mhz":1850,"max_mhz":2150}"#,
    );
    assert_eq!(response["result"]["applied_min_clock_mhz"], 1900);
    assert_eq!(response["result"]["min_mode"], "ceil");
    assert_eq!(response["result"]["applied_max_clock_mhz"], 2100);
    assert_eq!(response["result"]["max_mode"], "floor");

    for (method, key) in [
        ("gpu_reset_locked_core_clocks", "reset"),
        ("gpu_reset_locked_memory_clocks", "reset"),
        ("gpu_enable_persistence_mode", "enabled"),
    ] {
        let response = daemon.request(&format!(r#"{{"method":"{method}","gpu_index":0}}"#));
        assert_eq!(response["ok"], Value::Bool(true), "{method}");
        assert_eq!(response["result"][key], Value::Bool(true), "{method}");
        assert_eq!(
            response["result"]["mock_ops"].as_array().unwrap().len(),
            1,
            "{method}"
        );
    }
}

#[test]
fn gpu_write_validation_errors_over_the_wire() {
    let daemon = Daemon::start(&[MOCK_GPU]);

    let cases = [
        (
            r#"{"method":"gpu_apply_power_limit","gpu_index":0,"power_limit_w":300,"bogus":1}"#,
            "unknown request field: bogus",
        ),
        (
            r#"{"method":"gpu_apply_power_limit","power_limit_w":300}"#,
            "gpu_index must be an integer",
        ),
        (
            r#"{"method":"gpu_apply_power_limit","gpu_index":-1,"power_limit_w":300}"#,
            "gpu_index must be an integer",
        ),
        (
            r#"{"method":"gpu_apply_power_limit","gpu_index":0,"power_limit_w":"300"}"#,
            "power_limit_w must be an integer",
        ),
        (
            r#"{"method":"gpu_apply_vf_offsets","gpu_index":0,"offsets":[[1,2,3]]}"#,
            "offsets must be a list of [index, offset_khz] integer pairs",
        ),
        (
            r#"{"method":"gpu_apply_locked_core_clock","gpu_index":0,"clock_mhz":1950,"prefer_not_above":"yes"}"#,
            "prefer_not_above must be a boolean",
        ),
        // Unknown-field rejection precedes method dispatch (existing precedence),
        // so the not-a-method probe carries no fields.
        (
            r#"{"method":"gpu_frobnicate"}"#,
            "unknown daemon method: gpu_frobnicate",
        ),
    ];
    for (request, expected) in cases {
        let response = daemon.request(request);
        assert_eq!(response["ok"], Value::Bool(false), "request: {request}");
        assert_eq!(response["error"], expected, "request: {request}");
    }
}

#[test]
fn gpu_write_backend_error_text_is_relayed() {
    let daemon = Daemon::start(&[
        MOCK_GPU,
        ("PENGUIN_BURNERD_TEST_MOCK_GPU_FAIL", "apply_power_limit_w"),
    ]);
    let response =
        daemon.request(r#"{"method":"gpu_apply_power_limit","gpu_index":0,"power_limit_w":300}"#);
    assert_eq!(response["ok"], Value::Bool(false));
    assert_eq!(response["error"], "apply_power_limit_w mock failure");

    // Other methods on the same backend still work.
    let response = daemon.request(r#"{"method":"gpu_reset_locked_core_clocks","gpu_index":0}"#);
    assert_eq!(response["ok"], Value::Bool(true));
}

#[test]
fn gpu_writes_are_legal_while_a_scan_runs() {
    let daemon = Daemon::start(&[MOCK_GPU]);
    let (_stream, _reader, _pid) =
        daemon.start_streaming(r#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#);

    // The scan child is the intended caller of the write RPCs: no arbitration.
    let response =
        daemon.request(r#"{"method":"gpu_apply_power_limit","gpu_index":0,"power_limit_w":250}"#);
    assert_eq!(response["ok"], Value::Bool(true));
    assert_eq!(response["result"]["applied_w"], 250);
}

// --- milestone B: profile verification (streaming) -----------------------------

#[test]
fn verification_streams_frames_and_builds_the_exact_argv() {
    let argv_file = std::env::temp_dir().join(format!("pb-vargv-{}.json", std::process::id()));
    let _ = std::fs::remove_file(&argv_file);
    let daemon = Daemon::start(&[
        ("SCAN_STUB_EXIT_AFTER_LINES", "1"),
        ("SCAN_STUB_ARGV_FILE", argv_file.to_str().unwrap()),
    ]);

    let mut stream = daemon.connect();
    stream
        .write_all(br#"{"method":"start_profile_verification","options":{"gpu_index":0,"stability_seconds":120,"auto_uv_profile":"profile-a"}}"#)
        .unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();

    let mut reader = BufReader::new(stream);
    let mut frames = Vec::new();
    while let Some(line) = read_line(&mut reader) {
        frames.push(serde_json::from_str::<Value>(&line).unwrap());
    }
    assert_eq!(frames[0]["control"], "started");
    assert_eq!(frames[1]["line"], "{\"event\":\"auto_uv_start\"}\n");
    assert_eq!(frames[2]["line"], "human line\n");
    assert_eq!(frames[3]["control"], "finished");
    assert_eq!(frames[3]["exit_code"], 0);

    // The exact argv of `ui/commands.py::profile_verify_command`, with the
    // daemon-owned stop-request file appended last.
    let argv: Value = serde_json::from_str(&std::fs::read_to_string(&argv_file).unwrap()).unwrap();
    assert_eq!(
        argv,
        serde_json::json!([
            "--stability-test",
            "--stability-seconds",
            "120",
            "--gpu-index",
            "0",
            "--auto-uv-profile",
            "profile-a",
            "--stability-stop-request-file",
            daemon.verify_stop_request_file().to_str().unwrap(),
        ])
    );
    let _ = std::fs::remove_file(&argv_file);
}

#[test]
fn unknown_verification_option_is_rejected() {
    let daemon = Daemon::start(&[]);
    let mut stream = daemon.connect();
    stream
        .write_all(br#"{"method":"start_profile_verification","options":{"bogus":1}}"#)
        .unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();
    let mut reader = BufReader::new(stream);
    let line = read_line(&mut reader).unwrap();
    assert_eq!(
        line,
        "{\"ok\":false,\"error\":\"unknown profile verification option: bogus\"}\n"
    );
}

#[test]
fn stop_profile_verification_writes_marker_and_ends_child() {
    let daemon = Daemon::start(&[]);
    let (_stream, mut reader, pid) = daemon
        .start_streaming(r#"{"method":"start_profile_verification","options":{"gpu_index":0}}"#);

    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "profile_verification_running");
    assert_eq!(
        status["result"]["active_job"]["type"],
        "profile_verification"
    );

    let stop = daemon.request(r#"{"method":"stop_profile_verification"}"#);
    assert_eq!(stop["ok"], Value::Bool(true));
    assert_eq!(stop["result"]["stopped"], Value::Bool(true));
    assert_eq!(stop["result"]["pid"].as_u64().unwrap() as u32, pid);

    let marker = std::fs::read_to_string(daemon.verify_stop_request_file()).unwrap();
    assert_eq!(marker, "stop requested by PenguinBurner daemon client\n");

    // The child exits on SIGINT and the stream finishes.
    let mut finished = None;
    while let Some(line) = read_line(&mut reader) {
        let frame: Value = serde_json::from_str(&line).unwrap();
        if frame["control"] == "finished" {
            finished = Some(frame);
            break;
        }
    }
    assert!(finished.is_some(), "no finished frame after stop");
    wait_until(|| !pid_alive(pid), Duration::from_secs(5));
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "idle");

    // Stopping again reports idle (no verification running).
    let stop = daemon.request(r#"{"method":"stop_profile_verification"}"#);
    assert_eq!(stop["result"]["stopped"], Value::Bool(false));
    assert_eq!(stop["result"]["state"], "idle");
}

#[test]
fn stop_auto_uv_scan_does_not_stop_a_verification() {
    let daemon = Daemon::start(&[]);
    let (_stream, _reader, pid) = daemon
        .start_streaming(r#"{"method":"start_profile_verification","options":{"gpu_index":0}}"#);

    let stop = daemon.request(r#"{"method":"stop_auto_uv_scan"}"#);
    assert_eq!(stop["result"]["stopped"], Value::Bool(false));
    assert!(pid_alive(pid), "verification child was signalled");
}

#[test]
fn verification_disconnect_writes_marker_and_ends_child() {
    let daemon = Daemon::start(&[]);
    let (stream, reader, pid) = daemon
        .start_streaming(r#"{"method":"start_profile_verification","options":{"gpu_index":0}}"#);

    drop(reader);
    drop(stream);

    let path = daemon.verify_stop_request_file();
    wait_until(|| path.exists(), Duration::from_secs(5));
    assert_eq!(
        std::fs::read_to_string(&path).unwrap(),
        "stop requested by PenguinBurner daemon client\n"
    );
    wait_until(|| !pid_alive(pid), Duration::from_secs(8));
    assert!(!pid_alive(pid), "verification child survived disconnect");
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "idle");
}

#[test]
fn scan_and_verification_refuse_each_other() {
    let daemon = Daemon::start(&[]);

    // Scan running → verification, second scan, and runtime profile refused.
    let (_stream, _reader, _pid) =
        daemon.start_streaming(r#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#);
    let refused =
        daemon.request(r#"{"method":"start_profile_verification","options":{"gpu_index":0}}"#);
    assert_eq!(
        refused["error"],
        "cannot start profile verification while Auto-UV scan is running"
    );
    let refused = daemon.request(&apply_runtime_request());
    assert_eq!(
        refused["error"],
        "cannot apply a runtime spec while Auto-UV scan is running"
    );
}

#[test]
fn verification_refuses_scan_second_verification_and_profile() {
    let daemon = Daemon::start(&[]);
    let (_stream, _reader, _pid) = daemon
        .start_streaming(r#"{"method":"start_profile_verification","options":{"gpu_index":0}}"#);

    let refused = daemon.request(r#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#);
    assert_eq!(
        refused["error"],
        "cannot start an Auto-UV scan while profile verification is running"
    );
    let refused =
        daemon.request(r#"{"method":"start_profile_verification","options":{"gpu_index":0}}"#);
    assert_eq!(refused["error"], "profile verification is already running");
    let refused = daemon.request(&apply_runtime_request());
    assert_eq!(
        refused["error"],
        "cannot apply a runtime spec while profile verification is running"
    );
}

// --- milestone B: profile deletion ---------------------------------------------

#[test]
fn delete_auto_uv_profiles_happy_path_and_attacks() {
    let daemon = Daemon::start(&[]);
    let profiles_dir = daemon.profiles_dir();
    std::fs::create_dir_all(&profiles_dir).unwrap();
    std::fs::write(profiles_dir.join("auto-uv-profile-a.json"), "{}").unwrap();
    std::fs::write(profiles_dir.join("auto-uv-profile-b.json"), "{}").unwrap();
    let victim = daemon.home.join("victim.json");
    std::fs::write(&victim, "precious").unwrap();

    // Attack: ../ traversal out of the profiles dir.
    let attack = profiles_dir.join("../../../victim.json");
    let response = daemon.request(&format!(
        r#"{{"method":"delete_auto_uv_profiles","paths":["{}"]}}"#,
        attack.display()
    ));
    assert_eq!(response["ok"], Value::Bool(false));
    assert!(
        response["error"]
            .as_str()
            .unwrap()
            .starts_with("refusing to delete a path outside"),
        "{response}"
    );
    assert!(victim.exists());

    // Attack: absolute path outside the dir.
    let response = daemon.request(&format!(
        r#"{{"method":"delete_auto_uv_profiles","paths":["{}"]}}"#,
        victim.display()
    ));
    assert_eq!(response["ok"], Value::Bool(false));
    assert!(victim.exists());

    // Attack: symlink inside the dir escaping it.
    let link = profiles_dir.join("evil.json");
    std::os::unix::fs::symlink(&victim, &link).unwrap();
    let response = daemon.request(&format!(
        r#"{{"method":"delete_auto_uv_profiles","paths":["{}"]}}"#,
        link.display()
    ));
    assert_eq!(response["ok"], Value::Bool(false));
    assert!(victim.exists());
    assert!(std::fs::symlink_metadata(&link).is_ok());

    // Validation: paths must be a string list; unknown fields rejected.
    let response = daemon.request(r#"{"method":"delete_auto_uv_profiles"}"#);
    assert_eq!(
        response["error"],
        "profile paths must be a JSON string list"
    );
    let response = daemon.request(r#"{"method":"delete_auto_uv_profiles","paths":[],"x":1}"#);
    assert_eq!(response["error"], "unknown request field: x");

    // A failing batch deletes nothing, even when it contains a valid path.
    let response = daemon.request(&format!(
        r#"{{"method":"delete_auto_uv_profiles","paths":["{}","{}"]}}"#,
        profiles_dir.join("auto-uv-profile-a.json").display(),
        victim.display()
    ));
    assert_eq!(response["ok"], Value::Bool(false));
    assert!(profiles_dir.join("auto-uv-profile-a.json").exists());

    // Happy path: both profiles deleted, canonical paths reported.
    let response = daemon.request(&format!(
        r#"{{"method":"delete_auto_uv_profiles","paths":["{}","{}"]}}"#,
        profiles_dir.join("auto-uv-profile-a.json").display(),
        profiles_dir.join("auto-uv-profile-b.json").display()
    ));
    assert_eq!(response["ok"], Value::Bool(true), "{response}");
    let deleted = response["result"]["deleted"].as_array().unwrap();
    assert_eq!(deleted.len(), 2);
    assert!(deleted[0]
        .as_str()
        .unwrap()
        .ends_with("auto-uv-profile-a.json"));
    assert!(!profiles_dir.join("auto-uv-profile-a.json").exists());
    assert!(!profiles_dir.join("auto-uv-profile-b.json").exists());

    // Empty list is a successful no-op.
    let response = daemon.request(r#"{"method":"delete_auto_uv_profiles","paths":[]}"#);
    assert_eq!(response["result"]["deleted"], serde_json::json!([]));
}

// --- request-size cap on the 0666 socket ---------------------------------------

/// A client streaming an over-long "line" (no newline) must get a bounded error
/// and a closed connection — not an unbounded buffer in the root daemon — and
/// the daemon must keep serving other clients afterwards.
#[test]
fn oversized_request_line_is_rejected_and_daemon_survives() {
    let daemon = Daemon::start(&[]);

    let mut stream = daemon.connect();
    let chunk = vec![b'x'; 64 * 1024];
    let mut sent: u64 = 0;
    // 1 MiB cap + 2 bytes so the daemon's bounded read trips without us racing
    // its close (Unix sockets don't discard buffered data on close).
    while sent < 1024 * 1024 + 2 {
        if stream.write_all(&chunk).is_err() {
            break; // daemon already rejected and closed — fine
        }
        sent += chunk.len() as u64;
    }
    let _ = stream.flush();

    let mut reader = BufReader::new(stream);
    let line = read_line(&mut reader).expect("a bounded error line");
    let response: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(response["ok"], Value::Bool(false));
    assert!(
        response["error"]
            .as_str()
            .unwrap()
            .contains("request line exceeds"),
        "{response}"
    );
    // The connection is closed after the error (the line cannot be resynced).
    assert!(read_line(&mut reader).is_none());

    // The daemon is still healthy for new connections.
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["ok"], Value::Bool(true));
    assert_eq!(status["result"]["state"], "idle");
}

/// A request of a normal size (well under the cap) still round-trips — the cap
/// must not interfere with legitimate lines.
#[test]
fn large_but_legal_request_line_still_parses() {
    let daemon = Daemon::start(&[]);
    // ~64 KiB of padding inside an unknown field name: parsed as JSON, rejected
    // as an unknown field (proving it passed the size gate into the dispatcher).
    let padding = "p".repeat(64 * 1024);
    let response = daemon.request(&format!(r#"{{"method":"status","{padding}":1}}"#));
    assert_eq!(response["ok"], Value::Bool(false));
    assert!(
        response["error"]
            .as_str()
            .unwrap()
            .starts_with("unknown request field:"),
        "{response}"
    );
}

// --- deep-sleep (RTD3) gate ---------------------------------------------------

/// Fake PCI sysfs + NVIDIA procfs trees for the RTD3 probe seams, at the same
/// address `test_runtime_spec` persists so the hint path is exercised.
fn write_rtd3_tree(
    dir: &std::path::Path,
    d3_line: &str,
    control: &str,
    runtime_status: &str,
) -> (PathBuf, PathBuf) {
    let addr = "0000:01:00.0";
    let sys = dir.join("rtd3-sys");
    let proc_root = dir.join("rtd3-proc");
    write_rtd3_gpu(&sys, &proc_root, addr, d3_line, control, runtime_status);
    (sys, proc_root)
}

fn write_rtd3_gpu(
    sys: &std::path::Path,
    proc_root: &std::path::Path,
    addr: &str,
    d3_line: &str,
    control: &str,
    runtime_status: &str,
) {
    let device = sys.join(addr);
    let power = device.join("power");
    std::fs::create_dir_all(&power).unwrap();
    std::fs::write(device.join("vendor"), "0x10de\n").unwrap();
    std::fs::write(device.join("class"), "0x030000\n").unwrap();
    std::fs::write(power.join("control"), format!("{control}\n")).unwrap();
    std::fs::write(power.join("runtime_status"), format!("{runtime_status}\n")).unwrap();
    // Cumulative runtime-PM residency counters (ms), surfaced in the status
    // block so testers can quantify how much the GPU really sleeps.
    std::fs::write(power.join("runtime_active_time"), "5123\n").unwrap();
    std::fs::write(power.join("runtime_suspended_time"), "60789\n").unwrap();
    let proc_gpu = proc_root.join(addr);
    std::fs::create_dir_all(&proc_gpu).unwrap();
    std::fs::write(
        proc_gpu.join("power"),
        format!("Runtime D3 status:          {d3_line}\nVideo Memory:               Off\n"),
    )
    .unwrap();
}

fn write_fake_proc_identity(
    proc_root: &std::path::Path,
    pid: u32,
    name: &str,
    status: Option<&str>,
) {
    let process_dir = proc_root.join(pid.to_string());
    std::fs::create_dir_all(&process_dir).unwrap();
    std::fs::write(process_dir.join("comm"), format!("{name}\n")).unwrap();
    if let Some(status) = status {
        std::fs::write(process_dir.join("status"), status).unwrap();
    }
}

fn write_root_powerd_identity(proc_root: &std::path::Path, pid: u32) {
    write_fake_proc_identity(
        proc_root,
        pid,
        "nvidia-powerd",
        Some("Name:\tnvidia-powerd\nUid:\t0\t0\t0\t0\n"),
    );
}

#[test]
fn deep_sleep_defers_autostart_while_the_gpu_is_suspended() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let state_file = dir.join("state.json");
    // A static spec: deferral only applies to a runtime the watcher would
    // re-attach (a pending stock runtime enforces nothing and deliberately
    // never defers nor wakes).
    std::fs::write(
        &state_file,
        test_static_runtime_spec("deferred-profile").to_string(),
    )
    .unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (fine-grained)", "auto", "suspended");

    let daemon = Daemon::start(&[
        (
            "PENGUIN_BURNERD_TEST_STATE_FILE",
            state_file.to_str().unwrap(),
        ),
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
    ]);

    // The mode is evaluated synchronously before the socket binds: Mobile, and
    // the persisted runtime must NOT be running while the GPU sleeps.
    let status = daemon.request(r#"{"method":"status"}"#);
    let result = &status["result"];
    assert_eq!(result["state"], "idle", "{status}");
    assert_eq!(result["deep_sleep"]["state"], "mobile", "{status}");
    assert_eq!(result["deep_sleep"]["mode"], "fine-grained", "{status}");
    assert_eq!(result["deep_sleep"]["pci_addr"], "0000:01:00.0", "{status}");

    // The watcher marks the deferral within its first ticks.
    let mut deferred = false;
    for _ in 0..100 {
        let status = daemon.request(r#"{"method":"status"}"#);
        let deep_sleep = &status["result"]["deep_sleep"];
        if deep_sleep["autostart_deferred"] == Value::Bool(true) {
            assert_eq!(deep_sleep["runtime_status"], "suspended", "{status}");
            assert_eq!(
                deep_sleep["suspended_observed"],
                Value::Bool(true),
                "{status}"
            );
            // The kernel residency counters pass through from the fake sysfs.
            assert_eq!(deep_sleep["runtime_active_ms"], 5123, "{status}");
            assert_eq!(deep_sleep["runtime_suspended_ms"], 60789, "{status}");
            deferred = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(deferred, "autostart was never marked deferred");

    // Still no engine: the sleeping GPU was never attached.
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "idle", "{status}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn deep_sleep_defers_multi_gpu_boot_replay_until_the_active_gpu_is_used() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-multi-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let boot_file = dir.join("boot.json");
    let mut gpu_a = test_static_runtime_spec("profile-a");
    gpu_a["gpu"]["uuid"] = Value::String("GPU-A".to_string());
    gpu_a["gpu"]["index_at_resolution"] = Value::from(0);
    gpu_a["gpu"]["pci_bus_id"] = Value::String("0000:02:00.0".to_string());
    let mut gpu_b = test_static_runtime_spec("profile-b");
    gpu_b["gpu"]["uuid"] = Value::String("GPU-B".to_string());
    gpu_b["gpu"]["index_at_resolution"] = Value::from(1);
    let mut missing = test_static_runtime_spec("profile-missing");
    missing["gpu"]["uuid"] = Value::String("GPU-MISSING".to_string());
    missing["gpu"]["index_at_resolution"] = Value::from(2);
    missing["gpu"]["pci_bus_id"] = Value::String("0000:03:00.0".to_string());
    std::fs::write(
        &boot_file,
        serde_json::json!({
            "format_version": 1,
            "active_gpu_uuid": "GPU-MISSING",
            "specs": [gpu_a, missing, gpu_b],
        })
        .to_string(),
    )
    .unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (fine-grained)", "auto", "suspended");
    write_rtd3_gpu(
        &sys,
        &proc_root,
        "0000:02:00.0",
        "Enabled (fine-grained)",
        "auto",
        "suspended",
    );
    let clients = dir.join("clients");
    let contexts = dir.join("context-pids");

    let daemon = Daemon::start(&[
        (
            "PENGUIN_BURNERD_TEST_BOOT_STATE_FILE",
            boot_file.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_GPU_IDENTITIES",
            r#"[{"index":0,"uuid":"GPU-B"},{"index":1,"uuid":"GPU-A"}]"#,
        ),
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_CLIENT_PROC",
            clients.to_str().unwrap(),
        ),
        MOCK_GPU,
        (
            "PENGUIN_BURNERD_TEST_MOCK_GPU_CONTEXT_PIDS",
            contexts.to_str().unwrap(),
        ),
    ]);

    let mut deferred = false;
    for _ in 0..100 {
        let status = daemon.request(r#"{"method":"status"}"#);
        let result = &status["result"];
        if result["deep_sleep"]["autostart_deferred"] == Value::Bool(true) {
            assert_eq!(result["state"], "idle", "{status}");
            deferred = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(deferred, "multi-GPU boot replay was not deferred");

    write_fake_proc_identity(
        &clients,
        4242,
        "some-game",
        Some("Name:\tsome-game\nUid:\t1000\t1000\t1000\t1000\n"),
    );
    std::fs::write(&contexts, "graphics 4242\n").unwrap();
    std::fs::write(sys.join("0000:01:00.0/power/runtime_status"), "active\n").unwrap();

    let mut started = false;
    for _ in 0..200 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["state"] == "runtime_profile_running" {
            assert_eq!(status["result"]["active_job"]["gpu_uuid"], "GPU-B");
            started = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(started, "selected active GPU runtime was never started");

    let boot = daemon.request(r#"{"method":"boot_runtime_spec"}"#);
    assert_eq!(
        boot["result"]["replay"],
        serde_json::json!([
            {"gpu_uuid": "GPU-A", "gpu_index": 1, "outcome": "applied"},
            {
                "gpu_uuid": "GPU-MISSING",
                "outcome": "gpu-not-detected",
                "error": "runtime profile GPU GPU-MISSING is not currently detected",
            },
            {"gpu_uuid": "GPU-B", "gpu_index": 0, "outcome": "active"},
        ])
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn deep_sleep_coarse_grained_ignores_another_gpu_device_holder() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir =
        std::env::temp_dir().join(format!("pb-rtd3-coarse-multi-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let state_file = dir.join("state.json");
    std::fs::write(
        &state_file,
        test_static_runtime_spec("gpu-a-profile").to_string(),
    )
    .unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (coarse-grained)", "auto", "suspended");
    write_rtd3_gpu(
        &sys,
        &proc_root,
        "0000:02:00.0",
        "Enabled (coarse-grained)",
        "auto",
        "active",
    );
    std::fs::write(
        proc_root.join("0000:01:00.0/information"),
        "Model: Test GPU A\nDevice Minor: 0\n",
    )
    .unwrap();
    std::fs::write(
        proc_root.join("0000:02:00.0/information"),
        "Model: Test GPU B\nDevice Minor: 1\n",
    )
    .unwrap();

    let clients = dir.join("clients");
    write_fake_proc_identity(
        &clients,
        4242,
        "other-gpu-game",
        Some("Name:\tother-gpu-game\nUid:\t1000\t1000\t1000\t1000\n"),
    );
    let fd = clients.join("4242/fd/7");
    std::fs::create_dir_all(fd.parent().unwrap()).unwrap();
    std::os::unix::fs::symlink("/dev/nvidia1", &fd).unwrap();

    let daemon = Daemon::start(&[
        (
            "PENGUIN_BURNERD_TEST_STATE_FILE",
            state_file.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_GPU_IDENTITIES",
            r#"[{"index":0,"uuid":"GPU-inert-test"},{"index":1,"uuid":"GPU-other"}]"#,
        ),
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_CLIENT_PROC",
            clients.to_str().unwrap(),
        ),
        MOCK_GPU,
    ]);

    let mut deferred = false;
    for _ in 0..100 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["deep_sleep"]["autostart_deferred"] == Value::Bool(true) {
            deferred = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(deferred, "target GPU runtime was not deferred");
    std::fs::write(sys.join("0000:01:00.0/power/runtime_status"), "active\n").unwrap();

    for _ in 0..100 {
        let status = daemon.request(r#"{"method":"status"}"#);
        assert_eq!(
            status["result"]["state"], "idle",
            "another GPU's holder started the target runtime: {status}"
        );
        std::thread::sleep(Duration::from_millis(50));
    }

    std::fs::remove_file(&fd).unwrap();
    std::os::unix::fs::symlink("/dev/nvidia0", &fd).unwrap();
    let mut started = false;
    for _ in 0..200 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["state"] == "runtime_profile_running" {
            started = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(started, "the target GPU holder never started its runtime");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn deep_sleep_replays_non_stock_gpu_when_selected_gpu_is_stock() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!(
        "pb-rtd3-multi-stock-active-{}-{}",
        std::process::id(),
        n
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let boot_file = dir.join("boot.json");
    let mut gpu_a = test_static_runtime_spec("profile-a");
    gpu_a["gpu"]["uuid"] = Value::String("GPU-A".to_string());
    gpu_a["gpu"]["index_at_resolution"] = Value::from(0); // stale: now index 1
    gpu_a["gpu"]["pci_bus_id"] = Value::String("0000:02:00.0".to_string());
    let mut gpu_b = test_runtime_spec();
    gpu_b["gpu"]["uuid"] = Value::String("GPU-B".to_string());
    gpu_b["gpu"]["index_at_resolution"] = Value::from(1); // stale: now index 0
    std::fs::write(
        &boot_file,
        serde_json::json!({
            "format_version": 1,
            "active_gpu_uuid": "GPU-B",
            "specs": [gpu_a, gpu_b],
        })
        .to_string(),
    )
    .unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (fine-grained)", "auto", "suspended");
    write_rtd3_gpu(
        &sys,
        &proc_root,
        "0000:02:00.0",
        "Enabled (fine-grained)",
        "auto",
        "suspended",
    );
    let clients = dir.join("clients");
    let contexts = dir.join("context-pids");

    let daemon = Daemon::start(&[
        (
            "PENGUIN_BURNERD_TEST_BOOT_STATE_FILE",
            boot_file.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_GPU_IDENTITIES",
            r#"[{"index":0,"uuid":"GPU-B"},{"index":1,"uuid":"GPU-A"}]"#,
        ),
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_CLIENT_PROC",
            clients.to_str().unwrap(),
        ),
        MOCK_GPU,
        // An empty profile id matches stock. If the daemon tries to start the
        // selected stock engine instead of skipping it, replay reports a
        // failure and this test fails.
        ("PENGUIN_BURNERD_TEST_FAIL_PROFILE_ID", ""),
        (
            "PENGUIN_BURNERD_TEST_MOCK_GPU_CONTEXT_PIDS",
            contexts.to_str().unwrap(),
        ),
    ]);

    let mut deferred = false;
    for _ in 0..100 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["deep_sleep"]["autostart_deferred"] == Value::Bool(true) {
            assert_eq!(status["result"]["state"], "idle", "{status}");
            assert_eq!(
                status["result"]["deep_sleep"]["pci_addr"], "0000:02:00.0",
                "the watcher must target the non-stock GPU: {status}"
            );
            deferred = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(
        deferred,
        "the non-stock entry did not make the complete boot replay wakeable"
    );

    write_fake_proc_identity(
        &clients,
        4242,
        "some-game",
        Some("Name:\tsome-game\nUid:\t1000\t1000\t1000\t1000\n"),
    );
    std::fs::write(&contexts, "graphics 4242\n").unwrap();
    std::fs::write(sys.join("0000:02:00.0/power/runtime_status"), "active\n").unwrap();

    let mut started = false;
    for _ in 0..200 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["state"] == "runtime_profile_running" {
            assert_eq!(status["result"]["active_job"]["gpu_uuid"], "GPU-A");
            started = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(started, "the complete boot replay never started");
    assert_eq!(
        std::fs::read_to_string(sys.join("0000:01:00.0/power/runtime_status")).unwrap(),
        "suspended\n",
        "the selected stock GPU must not need to wake to trigger replay"
    );

    let boot = daemon.request(r#"{"method":"boot_runtime_spec"}"#);
    assert_eq!(
        boot["result"]["replay"],
        serde_json::json!([
            {"gpu_uuid": "GPU-A", "gpu_index": 1, "outcome": "active"},
            {"gpu_uuid": "GPU-B", "gpu_index": 0, "outcome": "stock-skipped"},
        ])
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn deep_sleep_replays_legacy_boot_spec_without_pci_id_on_single_gpu() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir =
        std::env::temp_dir().join(format!("pb-rtd3-legacy-boot-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let boot_file = dir.join("boot.json");
    let mut legacy = test_static_runtime_spec("legacy-profile");
    legacy["gpu"]["uuid"] = Value::String("GPU-LEGACY".to_string());
    legacy["gpu"].as_object_mut().unwrap().remove("pci_bus_id");
    // The old on-disk format was one RuntimeSpec rather than a BootRuntimeSet.
    std::fs::write(&boot_file, legacy.to_string()).unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (fine-grained)", "auto", "suspended");
    let clients = dir.join("clients");
    let contexts = dir.join("context-pids");

    let daemon = Daemon::start(&[
        (
            "PENGUIN_BURNERD_TEST_BOOT_STATE_FILE",
            boot_file.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_GPU_IDENTITIES",
            r#"[{"index":0,"uuid":"GPU-LEGACY"}]"#,
        ),
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_CLIENT_PROC",
            clients.to_str().unwrap(),
        ),
        MOCK_GPU,
        (
            "PENGUIN_BURNERD_TEST_MOCK_GPU_CONTEXT_PIDS",
            contexts.to_str().unwrap(),
        ),
    ]);

    let mut deferred = false;
    for _ in 0..100 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["deep_sleep"]["autostart_deferred"] == Value::Bool(true) {
            assert_eq!(status["result"]["deep_sleep"]["pci_addr"], "0000:01:00.0");
            deferred = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(deferred, "legacy boot intent was not deferred for wake");

    write_fake_proc_identity(
        &clients,
        4242,
        "some-game",
        Some("Name:\tsome-game\nUid:\t1000\t1000\t1000\t1000\n"),
    );
    std::fs::write(&contexts, "graphics 4242\n").unwrap();
    std::fs::write(sys.join("0000:01:00.0/power/runtime_status"), "active\n").unwrap();

    let mut started = false;
    for _ in 0..200 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["state"] == "runtime_profile_running" {
            assert_eq!(status["result"]["active_job"]["gpu_uuid"], "GPU-LEGACY");
            started = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(started, "legacy boot profile never started after GPU use");
    let boot = daemon.request(r#"{"method":"boot_runtime_spec"}"#);
    assert_eq!(
        boot["result"]["replay"],
        serde_json::json!([
            {"gpu_uuid": "GPU-LEGACY", "gpu_index": 0, "outcome": "active"},
        ])
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn deep_sleep_does_not_substitute_gpu_when_target_uuid_resolution_fails() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!(
        "pb-rtd3-unresolved-target-{}-{}",
        std::process::id(),
        n
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let boot_file = dir.join("boot.json");
    let mut gpu_a = test_static_runtime_spec("profile-a");
    gpu_a["gpu"]["uuid"] = Value::String("GPU-A".to_string());
    let mut gpu_b = test_static_runtime_spec("profile-b");
    gpu_b["gpu"]["uuid"] = Value::String("GPU-B".to_string());
    gpu_b["gpu"]["index_at_resolution"] = Value::from(1);
    gpu_b["gpu"]["pci_bus_id"] = Value::String("0000:02:00.0".to_string());
    std::fs::write(
        &boot_file,
        serde_json::json!({
            "format_version": 1,
            "active_gpu_uuid": "GPU-A",
            "specs": [gpu_b, gpu_a],
        })
        .to_string(),
    )
    .unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (fine-grained)", "auto", "suspended");
    write_rtd3_gpu(
        &sys,
        &proc_root,
        "0000:02:00.0",
        "Enabled (fine-grained)",
        "auto",
        "suspended",
    );
    let clients = dir.join("clients");
    let contexts = dir.join("context-pids");
    write_fake_proc_identity(
        &clients,
        4242,
        "some-game",
        Some("Name:\tsome-game\nUid:\t1000\t1000\t1000\t1000\n"),
    );
    let other_gpu_fd_dir = clients.join("4242/fd");
    std::fs::create_dir_all(&other_gpu_fd_dir).unwrap();
    std::os::unix::fs::symlink("/dev/nvidia0", other_gpu_fd_dir.join("7")).unwrap();
    std::fs::write(&contexts, "graphics 4242\n").unwrap();

    let daemon = Daemon::start(&[
        (
            "PENGUIN_BURNERD_TEST_BOOT_STATE_FILE",
            boot_file.to_str().unwrap(),
        ),
        // The watched GPU-A is physically present in sysfs, but its stable
        // UUID cannot be resolved. GPU-B must not be substituted for client
        // detection just because it is resolvable.
        (
            "PENGUIN_BURNERD_TEST_GPU_IDENTITIES",
            r#"[{"index":0,"uuid":"GPU-B"}]"#,
        ),
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_CLIENT_PROC",
            clients.to_str().unwrap(),
        ),
        MOCK_GPU,
        (
            "PENGUIN_BURNERD_TEST_MOCK_GPU_CONTEXT_PIDS",
            contexts.to_str().unwrap(),
        ),
    ]);

    let mut targeted = false;
    for _ in 0..100 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["deep_sleep"]["autostart_deferred"] == Value::Bool(true) {
            assert_eq!(status["result"]["deep_sleep"]["pci_addr"], "0000:01:00.0");
            targeted = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(targeted, "the selected target was not observed");
    std::fs::write(sys.join("0000:01:00.0/power/runtime_status"), "active\n").unwrap();

    for _ in 0..100 {
        let status = daemon.request(r#"{"method":"status"}"#);
        assert_eq!(
            status["result"]["state"], "idle",
            "another GPU was substituted for the unresolved target: {status}"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
    let boot = daemon.request(r#"{"method":"boot_runtime_spec"}"#);
    assert_eq!(boot["result"]["replay"], serde_json::json!([]));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn deep_sleep_rebinds_after_applying_a_profile_to_another_gpu() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-rebind-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (fine-grained)", "auto", "suspended");
    write_rtd3_gpu(
        &sys,
        &proc_root,
        "0000:02:00.0",
        "Enabled (fine-grained)",
        "auto",
        "active",
    );
    let clients = dir.join("clients");
    let contexts = dir.join("context-pids");
    write_fake_proc_identity(
        &clients,
        4242,
        "some-game",
        Some("Name:\tsome-game\nUid:\t1000\t1000\t1000\t1000\n"),
    );
    std::fs::write(&contexts, "graphics 4242\n").unwrap();

    let daemon = Daemon::start(&[
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_CLIENT_PROC",
            clients.to_str().unwrap(),
        ),
        MOCK_GPU,
        (
            "PENGUIN_BURNERD_TEST_MOCK_GPU_CONTEXT_PIDS",
            contexts.to_str().unwrap(),
        ),
    ]);
    let initial = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(initial["result"]["deep_sleep"]["pci_addr"], "0000:01:00.0");
    assert_eq!(
        initial["result"]["deep_sleep"]["suspended_observed"],
        Value::Bool(true)
    );

    let mut gpu_b = test_static_runtime_spec("profile-b");
    gpu_b["gpu"]["uuid"] = Value::String("GPU-B".to_string());
    gpu_b["gpu"]["index_at_resolution"] = Value::from(1);
    gpu_b["gpu"]["pci_bus_id"] = Value::String("0000:02:00.0".to_string());
    let applied = daemon.request(&runtime_spec_request("apply_runtime_spec", &gpu_b));
    assert_eq!(applied["ok"], Value::Bool(true), "{applied}");

    let mut rebound = false;
    for _ in 0..100 {
        let status = daemon.request(r#"{"method":"status"}"#);
        let result = &status["result"];
        if result["deep_sleep"]["pci_addr"] == "0000:02:00.0" {
            assert_eq!(result["active_job"]["gpu_uuid"], "GPU-B", "{status}");
            assert_eq!(
                result["deep_sleep"]["suspended_observed"],
                Value::Bool(false),
                "{status}"
            );
            rebound = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(rebound, "RTD3 watcher did not rebind from GPU A to GPU B");
    assert!(daemon
        .stderr_log()
        .contains("deep sleep: rebinding GPU target 0000:01:00.0 -> 0000:02:00.0"));
    let _ = std::fs::remove_dir_all(&dir);
}

/// NVIDIA's Dynamic Boost helper may retain an NVML context while no user
/// workload exists. On fine-grained RTD3 that infrastructure context must not
/// turn a daemon restart into a self-sustaining runtime attachment.
#[test]
fn deep_sleep_powerd_only_context_does_not_start_deferred_runtime() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let state_file = dir.join("state.json");
    std::fs::write(
        &state_file,
        test_static_runtime_spec("deferred-profile").to_string(),
    )
    .unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (fine-grained)", "auto", "active");
    // Keep the old timer-driven repro fast: its quiet interval was this
    // autosuspend delay plus one watcher tick, so the five-second observation
    // below spans more than two complete retry deadlines.
    std::fs::write(
        sys.join("0000:01:00.0/power/autosuspend_delay_ms"),
        "1000\n",
    )
    .unwrap();
    let clients = dir.join("clients");
    write_root_powerd_identity(&clients, 880);
    let contexts = dir.join("context-pids");
    let lifetime = dir.join("gpu-lifetime");
    std::fs::write(&contexts, "graphics 880\ncompute 880\n").unwrap();

    let daemon = Daemon::start(&[
        (
            "PENGUIN_BURNERD_TEST_STATE_FILE",
            state_file.to_str().unwrap(),
        ),
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_CLIENT_PROC",
            clients.to_str().unwrap(),
        ),
        MOCK_GPU,
        (
            "PENGUIN_BURNERD_TEST_MOCK_GPU_CONTEXT_PIDS",
            contexts.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_MOCK_GPU_LIFETIME_FILE",
            lifetime.to_str().unwrap(),
        ),
    ]);

    // Stay beyond the three-tick wake threshold. The helper is not a game,
    // so it must never materialize the persisted profile.
    for _ in 0..100 {
        let status = daemon.request(r#"{"method":"status"}"#);
        assert_eq!(
            status["result"]["state"], "idle",
            "powerd-only context started the runtime: {status}"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(
        status["result"]["deep_sleep"]["autostart_deferred"],
        Value::Bool(true),
        "{status}"
    );
    let gpu_clients = &status["result"]["deep_sleep"]["gpu_clients"];
    assert_eq!(gpu_clients["total_count"], 0, "{status}");
    assert_eq!(gpu_clients["graphics_count"], 0, "{status}");
    assert_eq!(gpu_clients["compute_count"], 0, "{status}");
    assert_eq!(gpu_clients["ignored_count"], 1, "{status}");
    assert_eq!(gpu_clients["ignored_clients"][0]["pid"], 880, "{status}");
    assert_eq!(
        gpu_clients["ignored_clients"][0]["name"], "nvidia-powerd",
        "{status}"
    );
    let (opens, closes, live) = mock_gpu_lifetime(&lifetime);
    assert!(opens > 0, "the watcher never exercised the context probe");
    assert_eq!(
        opens, 1,
        "powerd-only activity caused a repeated NVIDIA probe loop"
    );
    assert_eq!(
        (opens, closes, live),
        (opens, opens, 0),
        "the watcher retained its NVIDIA session while only nvidia-powerd remained"
    );

    // Dynamic Boost remains present when a real workload arrives. The game
    // context—not powerd—must still materialize the persisted profile.
    std::fs::create_dir_all(clients.join("4242")).unwrap();
    std::fs::write(clients.join("4242/comm"), "some-game\n").unwrap();
    std::fs::write(
        clients.join("4242/status"),
        "Name:\tsome-game\nUid:\t1000\t1000\t1000\t1000\n",
    )
    .unwrap();
    let game_fds = clients.join("4242/fd");
    std::fs::create_dir_all(&game_fds).unwrap();
    std::os::unix::fs::symlink("/dev/nvidia0", game_fds.join("7")).unwrap();
    std::fs::write(&contexts, "graphics 880 4242\ncompute 880\n").unwrap();

    let mut attached = false;
    for _ in 0..120 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["state"] == "runtime_profile_running" {
            let clients = &status["result"]["deep_sleep"]["gpu_clients"];
            assert_eq!(clients["total_count"], 1, "{status}");
            assert_eq!(clients["graphics"][0]["pid"], 4242, "{status}");
            assert_eq!(clients["ignored_count"], 1, "{status}");
            attached = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(attached, "a real game context did not start the runtime");
    let _ = std::fs::remove_dir_all(&dir);
}

/// An explicitly applied profile initially owns GPU handles. A powerd-only
/// context must still let the watcher park that runtime and release PB's hold.
#[test]
fn deep_sleep_powerd_only_context_allows_running_runtime_to_park() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (fine-grained)", "auto", "active");
    let clients = dir.join("clients");
    write_root_powerd_identity(&clients, 880);
    let contexts = dir.join("context-pids");
    std::fs::write(&contexts, "graphics 880\n").unwrap();

    let daemon = Daemon::start(&[
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_CLIENT_PROC",
            clients.to_str().unwrap(),
        ),
        MOCK_GPU,
        (
            "PENGUIN_BURNERD_TEST_MOCK_GPU_CONTEXT_PIDS",
            contexts.to_str().unwrap(),
        ),
    ]);

    let start = daemon.request(&runtime_spec_request(
        "apply_runtime_spec",
        &test_static_runtime_spec("powerd-only-profile"),
    ));
    assert_eq!(start["ok"], Value::Bool(true), "{start}");
    // While the engine is attached, the daemon must confess its own hold:
    // the client sample excludes the daemon's pid, so this flag is the only
    // status evidence that the daemon itself is the pin.
    let running = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(
        running["result"]["deep_sleep"]["daemon"]["engine_attached"],
        Value::Bool(true),
        "{running}"
    );

    let mut parked = false;
    for _ in 0..120 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["deep_sleep"]["parked"] == Value::Bool(true) {
            assert_eq!(status["result"]["state"], "idle", "{status}");
            let clients = &status["result"]["deep_sleep"]["gpu_clients"];
            assert_eq!(clients["total_count"], 0, "{status}");
            assert_eq!(clients["ignored_count"], 1, "{status}");
            assert_eq!(
                status["result"]["deep_sleep"]["daemon"]["engine_attached"],
                Value::Bool(false),
                "{status}"
            );
            parked = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(parked, "powerd-only context prevented runtime parking");
    let log = daemon.stderr_log();
    assert!(
        log.contains("ignored infrastructure helpers=1 [880 nvidia-powerd]"),
        "journal did not explain the powerd exclusion:\n{log}"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn deep_sleep_powerd_exclusion_requires_exact_root_identity() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (fine-grained)", "auto", "active");
    let clients = dir.join("clients");

    for (pid, comm, status) in [
        (880, "nvidia-powerd", Some("Uid:\t0\t0\t0\t0\n")),
        (881, "nvidia-powerd", Some("Uid:\t1000\t1000\t1000\t1000\n")),
        (882, "nvidia-powerd", None),
        (883, "nvidia-powerd", Some("Uid:\tnot-a-uid\n")),
        (884, "nvidia-powerd2", Some("Uid:\t0\t0\t0\t0\n")),
        (885, "nvidia-powerd ", Some("Uid:\t0\t0\t0\t0\n")),
        (886, "nvidia-powerd", Some("Uid:\t1000\t0\n")),
        (887, "nvidia-powerd", Some("Uid:\t1000\t0\tnot-a-uid\t0\n")),
        (
            888,
            "nvidia-powerd",
            Some("Uid:\t0\t0\t0\t0\nUid:\t1000\t1000\t1000\t1000\n"),
        ),
    ] {
        write_fake_proc_identity(&clients, pid, comm, status);
    }
    let contexts = dir.join("context-pids");
    std::fs::write(&contexts, "graphics 880 881 882 883 884 885 886 887 888\n").unwrap();

    let daemon = Daemon::start(&[
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_CLIENT_PROC",
            clients.to_str().unwrap(),
        ),
        MOCK_GPU,
        (
            "PENGUIN_BURNERD_TEST_MOCK_GPU_CONTEXT_PIDS",
            contexts.to_str().unwrap(),
        ),
    ]);
    let start = daemon.request(&runtime_spec_request(
        "apply_runtime_spec",
        &test_static_runtime_spec("identity-profile"),
    ));
    assert_eq!(start["ok"], Value::Bool(true), "{start}");

    let mut sampled = false;
    for _ in 0..100 {
        let status = daemon.request(r#"{"method":"status"}"#);
        let clients = &status["result"]["deep_sleep"]["gpu_clients"];
        if clients["ignored_count"] == 1 && clients["total_count"] == 8 {
            assert_eq!(clients["ignored_clients"][0]["pid"], 880, "{status}");
            assert_eq!(clients["graphics_count"], 8, "{status}");
            sampled = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(sampled, "identity classification was not reported");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn deep_sleep_desktop_mode_runs_autostart_immediately() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let state_file = dir.join("state.json");
    std::fs::write(&state_file, test_runtime_spec().to_string()).unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Disabled by default", "auto", "active");

    let daemon = Daemon::start(&[
        (
            "PENGUIN_BURNERD_TEST_STATE_FILE",
            state_file.to_str().unwrap(),
        ),
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
    ]);

    // Desktop mode preserves synchronous eager autostart.
    let status = daemon.request(r#"{"method":"status"}"#);
    let result = &status["result"];
    assert_eq!(result["state"], "runtime_profile_running", "{status}");
    assert_eq!(result["deep_sleep"]["state"], "desktop", "{status}");
    assert_eq!(
        result["deep_sleep"]["autostart_deferred"],
        Value::Bool(false),
        "{status}"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn suspended_observation_selects_mobile_before_desktop_autostart() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let state_file = dir.join("state.json");
    std::fs::write(&state_file, test_runtime_spec().to_string()).unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Disabled by default", "auto", "suspended");

    let daemon = Daemon::start(&[
        (
            "PENGUIN_BURNERD_TEST_STATE_FILE",
            state_file.to_str().unwrap(),
        ),
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
    ]);

    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "idle", "{status}");
    assert_eq!(
        status["result"]["deep_sleep"]["state"], "mobile",
        "{status}"
    );
    assert_eq!(
        status["result"]["deep_sleep"]["suspended_observed"],
        Value::Bool(true),
        "{status}"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn deep_sleep_scan_completion_does_not_force_start_the_deferred_runtime() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let state_file = dir.join("state.json");
    // A static spec: deferral only applies to a runtime the watcher would
    // re-attach (a pending stock runtime enforces nothing and deliberately
    // never defers nor wakes).
    std::fs::write(
        &state_file,
        test_static_runtime_spec("deferred-profile").to_string(),
    )
    .unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (fine-grained)", "auto", "suspended");

    let daemon = Daemon::start(&[
        (
            "PENGUIN_BURNERD_TEST_STATE_FILE",
            state_file.to_str().unwrap(),
        ),
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        ("SCAN_STUB_EXIT_AFTER_LINES", "1"),
    ]);

    // Mobile and deferred at boot; a completed scan must hand the restart to
    // the watcher (which sees a suspended GPU) instead of force-starting the
    // runtime and pinning the GPU — the desktop finish_child behavior.
    finish_successful_scan(&daemon);
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "idle", "{status}");

    // The deferral resumes: still pending, never started.
    let mut deferred = false;
    for _ in 0..100 {
        let status = daemon.request(r#"{"method":"status"}"#);
        let result = &status["result"];
        if result["deep_sleep"]["autostart_deferred"] == Value::Bool(true) {
            assert_eq!(result["state"], "idle", "{status}");
            deferred = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(deferred, "deferral did not resume after the scan");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn deep_sleep_unknown_scan_completion_stays_deferred() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let state_file = dir.join("state.json");
    std::fs::write(&state_file, test_runtime_spec().to_string()).unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "?", "auto", "active");

    let daemon = Daemon::start(&[
        (
            "PENGUIN_BURNERD_TEST_STATE_FILE",
            state_file.to_str().unwrap(),
        ),
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        ("SCAN_STUB_EXIT_AFTER_LINES", "1"),
    ]);

    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(
        status["result"]["deep_sleep"]["state"], "unknown",
        "{status}"
    );
    finish_successful_scan(&daemon);

    // Unknown follows conservative Mobile behavior: child completion must not
    // invoke the eager Desktop autostart path.
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "idle", "{status}");
    assert_eq!(
        status["result"]["deep_sleep"]["state"], "unknown",
        "{status}"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn deep_sleep_unknown_resolves_to_desktop_and_autostarts() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let state_file = dir.join("state.json");
    std::fs::write(&state_file, test_runtime_spec().to_string()).unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "?", "auto", "active");

    let daemon = Daemon::start(&[
        (
            "PENGUIN_BURNERD_TEST_STATE_FILE",
            state_file.to_str().unwrap(),
        ),
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
    ]);
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(
        status["result"]["deep_sleep"]["state"], "unknown",
        "{status}"
    );

    std::fs::write(
        proc_root.join("0000:01:00.0").join("power"),
        "Runtime D3 status:          Disabled by default\n",
    )
    .unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["state"] == "runtime_profile_running" {
            assert_eq!(
                status["result"]["deep_sleep"]["state"], "desktop",
                "{status}"
            );
            break;
        }
        assert!(
            Instant::now() < deadline,
            "Unknown never resolved: {status}"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn mobile_mode_rejects_persistence_enable_rpc() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (fine-grained)", "auto", "suspended");

    let daemon = Daemon::start(&[
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        ("PENGUIN_BURNERD_TEST_MOCK_GPU", "1"),
    ]);

    let response = daemon.request(r#"{"method":"gpu_enable_persistence_mode","gpu_index":0}"#);
    assert_eq!(response["ok"], Value::Bool(false), "{response}");
    assert!(
        response["error"]
            .as_str()
            .is_some_and(|error| error.contains("Mobile or Unknown")),
        "{response}"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn deep_sleep_parks_idle_runtime_and_reattaches_on_use() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (fine-grained)", "auto", "suspended");
    // Fake client-scan proc tree, initially empty: no GPU clients anywhere.
    let clients = dir.join("clients");
    std::fs::create_dir_all(&clients).unwrap();
    // Fine-grained mode reads GPU contexts from NVML (the mock's staged PID
    // file here), not the fd scan; no file yet means no contexts.
    let contexts = dir.join("context-pids");

    let daemon = Daemon::start(&[
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_CLIENT_PROC",
            clients.to_str().unwrap(),
        ),
        MOCK_GPU,
        (
            "PENGUIN_BURNERD_TEST_MOCK_GPU_CONTEXT_PIDS",
            contexts.to_str().unwrap(),
        ),
    ]);

    // Explicit apply attaches the engine (a user action may wake the GPU).
    let start = daemon.request(&runtime_spec_request(
        "apply_runtime_spec",
        &test_static_runtime_spec("parked-profile"),
    ));
    assert_eq!(start["ok"], Value::Bool(true), "{start}");
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(
        status["result"]["state"], "runtime_profile_running",
        "{status}"
    );

    // No clients for the (test-shrunk) idle window: the watcher parks the
    // runtime — engine gone, persisted state kept, profile pending again.
    let mut parked = false;
    for _ in 0..200 {
        let status = daemon.request(r#"{"method":"status"}"#);
        let result = &status["result"];
        if result["deep_sleep"]["parked"] == Value::Bool(true) {
            assert_eq!(result["state"], "idle", "{status}");
            assert_eq!(
                result["deep_sleep"]["autostart_deferred"],
                Value::Bool(true),
                "{status}"
            );
            // The client sample behind the park decision: no contexts, no
            // holders — nothing counted.
            let clients = &result["deep_sleep"]["gpu_clients"];
            assert_eq!(clients["source"], "nvml-contexts", "{status}");
            assert_eq!(clients["total_count"], 0, "{status}");
            assert_eq!(clients["graphics_count"], 0, "{status}");
            assert_eq!(clients["compute_count"], 0, "{status}");
            parked = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(parked, "runtime was never parked");
    assert!(
        daemon.state_file.exists(),
        "parking must keep the persisted runtime state"
    );

    // Simulate real use: GPU active + processes with live GPU contexts (one
    // graphics, one compute; comm files supply the names the status block
    // reports). The watcher must re-materialize the parked profile.
    std::fs::write(sys.join("0000:01:00.0/power/runtime_status"), "active\n").unwrap();
    std::fs::create_dir_all(clients.join("4242")).unwrap();
    std::fs::write(clients.join("4242/comm"), "some-game\n").unwrap();
    std::fs::create_dir_all(clients.join("555")).unwrap();
    std::fs::write(clients.join("555/comm"), "cuda-worker\n").unwrap();
    std::fs::write(&contexts, "graphics 4242\ncompute 555\n").unwrap();

    let mut reattached = false;
    for _ in 0..200 {
        let status = daemon.request(r#"{"method":"status"}"#);
        let result = &status["result"];
        if result["state"] == "runtime_profile_running" {
            assert_eq!(
                result["active_job"]["profile_id"], "parked-profile",
                "{status}"
            );
            assert_eq!(
                result["deep_sleep"]["parked"],
                Value::Bool(false),
                "{status}"
            );
            reattached = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(
        reattached,
        "parked runtime was never re-materialized on use"
    );

    // The per-kind stats name the processes that woke the runtime.
    let mut stats_seen = false;
    for _ in 0..100 {
        let status = daemon.request(r#"{"method":"status"}"#);
        let clients = &status["result"]["deep_sleep"]["gpu_clients"];
        if clients["total_count"] == 2 {
            assert_eq!(clients["source"], "nvml-contexts", "{status}");
            assert_eq!(clients["graphics_count"], 1, "{status}");
            assert_eq!(clients["compute_count"], 1, "{status}");
            assert_eq!(clients["graphics"][0]["pid"], 4242, "{status}");
            assert_eq!(clients["graphics"][0]["name"], "some-game", "{status}");
            assert_eq!(clients["compute"][0]["pid"], 555, "{status}");
            assert_eq!(clients["compute"][0]["name"], "cuda-worker", "{status}");
            stats_seen = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(stats_seen, "gpu_clients never reported the per-kind stats");

    // The journal narrative the docs promise, end to end: countdown start
    // (remaining time, threshold-1 under the 2-tick test window), park,
    // kernel transition, the wake sample with the counted verdict, and the
    // re-materialization. Substring checks: other lines may interleave.
    let log = daemon.stderr_log();
    for expected in [
        "deep sleep: runtime_status suspended (initial)",
        "deep sleep: gpu clients (nvml-contexts): counted=0 (idle), graphics=0, compute=0, ignored infrastructure helpers=0, device-node holders=0",
        "deep sleep: no GPU clients; parking the runtime in 1s unless one appears",
        "deep sleep: no GPU clients for the idle window; parking the runtime",
        "deep sleep: runtime parked; the GPU may suspend until its next real use",
        "deep sleep: runtime_status suspended -> active (suspended for ",
        "deep sleep: gpu clients (nvml-contexts): counted=2 (GPU in use), graphics=1 [4242 some-game], compute=1 [555 cuda-worker], ignored infrastructure helpers=0, device-node holders=0",
        "deep sleep: GPU is in use; starting the deferred persisted runtime",
        "deep sleep: runtime deferral cleared",
        "deep sleep: parked runtime re-materialized",
    ] {
        assert!(log.contains(expected), "journal missing {expected:?}:\n{log}");
    }
    // The park drain must NOT be mislabeled as a wake: the GPU never came
    // out of suspend without a client in this scenario.
    assert!(
        !log.contains("transient wake"),
        "park drain was mislabeled as a transient wake:\n{log}"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn deep_sleep_never_reattaches_a_parked_stock_runtime() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (fine-grained)", "auto", "suspended");
    let clients = dir.join("clients");
    std::fs::create_dir_all(&clients).unwrap();
    let contexts = dir.join("context-pids");

    let daemon = Daemon::start(&[
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_CLIENT_PROC",
            clients.to_str().unwrap(),
        ),
        MOCK_GPU,
        (
            "PENGUIN_BURNERD_TEST_MOCK_GPU_CONTEXT_PIDS",
            contexts.to_str().unwrap(),
        ),
    ]);

    // Apply stock (the GUI's "restore defaults"): the engine attaches for the
    // reset writes, then the park policy must release it like any runtime.
    let start = daemon.request(&apply_runtime_request());
    assert_eq!(start["ok"], Value::Bool(true), "{start}");

    let mut parked = false;
    for _ in 0..200 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["deep_sleep"]["parked"] == Value::Bool(true) {
            parked = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(parked, "stock runtime was never parked");

    // Real GPU use arrives: a stock runtime enforces nothing, so the watcher
    // must NOT re-attach (that would only pin the GPU) and must not report
    // the stock intent as deferred.
    std::fs::write(sys.join("0000:01:00.0/power/runtime_status"), "active\n").unwrap();
    std::fs::write(&contexts, "4242\n").unwrap();

    for _ in 0..100 {
        let status = daemon.request(r#"{"method":"status"}"#);
        let result = &status["result"];
        assert_eq!(
            result["state"], "idle",
            "stock must never re-attach: {status}"
        );
        assert_eq!(
            result["deep_sleep"]["autostart_deferred"],
            Value::Bool(false),
            "{status}"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
    let _ = std::fs::remove_dir_all(&dir);
}

/// The issue-#30 hardware finding: fine-grained RTD3 suspends the GPU under
/// idly-open `/dev/nvidia<N>` fds (desktop shells, monitoring agents), so an
/// fd holder must not starve the park policy, and after parking a bare fd
/// holder plus an active GPU must not re-attach either — only a live NVML
/// context is real use.
#[test]
fn deep_sleep_fine_grained_ignores_idle_device_node_holders() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (fine-grained)", "auto", "suspended");
    // A long-lived /dev/nvidia0 holder exists the whole time.
    let clients = dir.join("clients");
    let fd_dir = clients.join("4242").join("fd");
    std::fs::create_dir_all(&fd_dir).unwrap();
    std::os::unix::fs::symlink("/dev/nvidia0", fd_dir.join("7")).unwrap();
    std::fs::write(clients.join("4242/comm"), "plasmashell\n").unwrap();
    let contexts = dir.join("context-pids");

    let daemon = Daemon::start(&[
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_CLIENT_PROC",
            clients.to_str().unwrap(),
        ),
        MOCK_GPU,
        (
            "PENGUIN_BURNERD_TEST_MOCK_GPU_CONTEXT_PIDS",
            contexts.to_str().unwrap(),
        ),
    ]);

    let start = daemon.request(&runtime_spec_request(
        "apply_runtime_spec",
        &test_static_runtime_spec("holder-blind-profile"),
    ));
    assert_eq!(start["ok"], Value::Bool(true), "{start}");

    // No NVML contexts: the fd holder alone must not block parking.
    let mut parked = false;
    for _ in 0..200 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["deep_sleep"]["parked"] == Value::Bool(true) {
            // The sample shows the holder as informational, not counted:
            // exactly the evidence that explains this park to a debugger.
            let clients = &status["result"]["deep_sleep"]["gpu_clients"];
            assert_eq!(clients["source"], "nvml-contexts", "{status}");
            assert_eq!(clients["total_count"], 0, "{status}");
            assert_eq!(clients["device_node_count"], 1, "{status}");
            assert_eq!(clients["device_node_holders"][0]["pid"], 4242, "{status}");
            assert_eq!(
                clients["device_node_holders"][0]["name"], "plasmashell",
                "{status}"
            );
            parked = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(parked, "fd holder starved the fine-grained park policy");

    // GPU goes (and stays) active with the fd holder but still no context:
    // must not re-materialize on that alone.
    std::fs::write(sys.join("0000:01:00.0/power/runtime_status"), "active\n").unwrap();
    std::thread::sleep(Duration::from_secs(5));
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(
        status["result"]["state"], "idle",
        "an fd holder without a context re-attached the runtime: {status}"
    );

    // With no context, the kernel completes a runtime-PM cycle. A later real
    // context wakes the GPU; that external transition re-arms the one-shot
    // context probe even though the long-lived fd holder did not change.
    std::fs::write(sys.join("0000:01:00.0/power/runtime_status"), "suspended\n").unwrap();
    let mut suspension_observed = false;
    for _ in 0..80 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["deep_sleep"]["runtime_status"] == "suspended" {
            suspension_observed = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(
        suspension_observed,
        "runtime-PM suspension was not observed"
    );

    std::fs::write(&contexts, "4242\n").unwrap();
    std::fs::write(sys.join("0000:01:00.0/power/runtime_status"), "active\n").unwrap();
    let mut reattached = false;
    for _ in 0..200 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["state"] == "runtime_profile_running" {
            reattached = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(
        reattached,
        "a live context never re-materialized the runtime"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

/// Coarse-grained RTD3 keeps the GPU awake per open fd, so there the fd scan
/// stays the park signal: a device-node holder blocks parking, and its exit
/// releases the runtime.
#[test]
fn deep_sleep_coarse_grained_device_holder_blocks_park() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (coarse-grained)", "auto", "active");
    let clients = dir.join("clients");
    let fd_dir = clients.join("880").join("fd");
    std::fs::create_dir_all(&fd_dir).unwrap();
    std::os::unix::fs::symlink("/dev/nvidia0", fd_dir.join("7")).unwrap();
    write_root_powerd_identity(&clients, 880);

    let daemon = Daemon::start(&[
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_CLIENT_PROC",
            clients.to_str().unwrap(),
        ),
        MOCK_GPU,
    ]);

    let start = daemon.request(&runtime_spec_request(
        "apply_runtime_spec",
        &test_static_runtime_spec("coarse-profile"),
    ));
    assert_eq!(start["ok"], Value::Bool(true), "{start}");

    // Well past the (test-shrunk) park window: the fd holder must keep the
    // engine attached.
    std::thread::sleep(Duration::from_secs(5));
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(
        status["result"]["state"], "runtime_profile_running",
        "coarse-grained park ignored the fd holder: {status}"
    );
    assert_eq!(
        status["result"]["deep_sleep"]["parked"],
        Value::Bool(false),
        "{status}"
    );
    // Coarse-grained: the device-node scan decides, so the holder is counted.
    let stats = &status["result"]["deep_sleep"]["gpu_clients"];
    assert_eq!(stats["source"], "device-nodes", "{status}");
    assert_eq!(stats["device_node_count"], 1, "{status}");
    assert_eq!(stats["total_count"], 1, "{status}");
    assert_eq!(stats["ignored_count"], 0, "{status}");
    assert_eq!(stats["device_node_holders"][0]["pid"], 880, "{status}");

    // The holder exits: the park policy releases the runtime.
    std::fs::remove_dir_all(clients.join("880")).unwrap();
    let mut parked = false;
    for _ in 0..200 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["deep_sleep"]["parked"] == Value::Bool(true) {
            parked = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(parked, "runtime was never parked after the holder exited");
    let _ = std::fs::remove_dir_all(&dir);
}

/// When the NVML context query fails (driver without the _v3 process-list
/// symbols), fine-grained mode falls back to the conservative fd scan rather
/// than never parking (or parking under a real client).
#[test]
fn deep_sleep_fine_grained_falls_back_to_fd_scan_when_nvml_query_fails() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-rtd3-{}-{}", std::process::id(), n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let (sys, proc_root) = write_rtd3_tree(&dir, "Enabled (fine-grained)", "auto", "suspended");
    let clients = dir.join("clients");
    let powerd_fd_dir = clients.join("880/fd");
    std::fs::create_dir_all(&powerd_fd_dir).unwrap();
    std::os::unix::fs::symlink("/dev/nvidia0", powerd_fd_dir.join("14")).unwrap();
    write_root_powerd_identity(&clients, 880);
    let game_fd_dir = clients.join("4242/fd");
    std::fs::create_dir_all(&game_fd_dir).unwrap();
    std::os::unix::fs::symlink("/dev/nvidia0", game_fd_dir.join("7")).unwrap();
    std::fs::write(clients.join("4242/comm"), "some-game\n").unwrap();
    let lifetime = dir.join("gpu-lifetime");

    let daemon = Daemon::start(&[
        ("PENGUIN_BURNERD_TEST_RTD3_SYSFS", sys.to_str().unwrap()),
        (
            "PENGUIN_BURNERD_TEST_RTD3_PROC",
            proc_root.to_str().unwrap(),
        ),
        (
            "PENGUIN_BURNERD_TEST_CLIENT_PROC",
            clients.to_str().unwrap(),
        ),
        MOCK_GPU,
        ("PENGUIN_BURNERD_TEST_MOCK_GPU_FAIL", "gpu_context_pids"),
        (
            "PENGUIN_BURNERD_TEST_MOCK_GPU_LIFETIME_FILE",
            lifetime.to_str().unwrap(),
        ),
    ]);

    let start = daemon.request(&runtime_spec_request(
        "apply_runtime_spec",
        &test_static_runtime_spec("fallback-profile"),
    ));
    assert_eq!(start["ok"], Value::Bool(true), "{start}");

    // The fallback remains conservative for ordinary holders. Powerd is
    // visible but ignored; the game beside it is counted and blocks parking.
    std::thread::sleep(Duration::from_secs(4));
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(
        status["result"]["state"], "runtime_profile_running",
        "fallback ignored a real holder: {status}"
    );
    let gpu_clients = &status["result"]["deep_sleep"]["gpu_clients"];
    assert_eq!(gpu_clients["source"], "device-nodes", "{status}");
    assert_eq!(gpu_clients["total_count"], 1, "{status}");
    assert_eq!(gpu_clients["device_node_count"], 2, "{status}");
    assert_eq!(gpu_clients["ignored_count"], 1, "{status}");
    let (opens, closes, live) = mock_gpu_lifetime(&lifetime);
    assert!(opens > 0, "the failing context probe was never exercised");
    assert_eq!(
        (opens, closes, live),
        (opens, opens, 0),
        "a failed context query retained its NVIDIA session"
    );

    // Once the real holder exits, powerd alone must not starve parking.
    std::fs::remove_dir_all(clients.join("4242")).unwrap();
    let mut parked = false;
    for _ in 0..200 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["deep_sleep"]["parked"] == Value::Bool(true) {
            // The stats disclose that the decision fell back to the fd scan.
            let clients = &status["result"]["deep_sleep"]["gpu_clients"];
            assert_eq!(clients["source"], "device-nodes", "{status}");
            assert_eq!(clients["total_count"], 0, "{status}");
            assert_eq!(clients["device_node_count"], 1, "{status}");
            assert_eq!(clients["ignored_count"], 1, "{status}");
            assert_eq!(clients["ignored_clients"][0]["pid"], 880, "{status}");
            parked = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(parked, "fd-scan fallback never parked the runtime");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn launcher_session_events_do_not_require_a_gpu_profile() {
    for launcher in ["heroic", "faugus"] {
        check_launcher_session_events(launcher);
    }
}

fn check_launcher_session_events(launcher: &str) {
    let daemon = Daemon::start(&[]);
    let mut stream = daemon.connect();
    stream
        .write_all(b"{\"method\":\"subscribe_launcher_sessions\"}\n")
        .unwrap();
    let mut reader = BufReader::new(stream);
    let initial: Value = serde_json::from_str(&read_line(&mut reader).unwrap()).unwrap();
    assert_eq!(initial["ok"], true);
    let initial_sequence = initial["result"]["sequence"].as_u64().unwrap();
    let mut child = Command::new("/usr/bin/python3").args(["-c", r#"
import json, socket, sys
s = socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
s.sendall(json.dumps(dict(method="register_launcher_session", app_id=sys.argv[2] + ":event-test", session_id="independent")).encode() + b"\n")
r = json.loads(s.makefile().readline())
assert r['ok'], r
print('registered', flush=True)
sys.stdin.readline()
s = socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
s.sendall(b'{"method":"update_launcher_session","session_id":"independent","phase":"failed","profile":"unconfirmed"}\n')
assert json.loads(s.makefile().readline())['ok']
"#]).arg(&daemon.socket).arg(launcher).stdin(Stdio::piped()).stdout(Stdio::piped()).spawn().unwrap();
    let mut child_out = BufReader::new(child.stdout.take().unwrap());
    assert_eq!(read_line(&mut child_out).unwrap().trim(), "registered");
    let registered: Value = serde_json::from_str(&read_line(&mut reader).unwrap()).unwrap();
    let session = registered["result"]["sessions"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["session_id"] == "independent")
        .unwrap();
    assert_eq!(session["pid"], child.id()); // SO_PEERCRED, not user-supplied PID
    assert_eq!(session["profile"], "unconfirmed");
    assert_eq!(session["wrapped"], true);
    assert!(registered["result"]["sequence"].as_u64().unwrap() > initial_sequence);
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["active_job"], Value::Null);
    assert_eq!(daemon.request(r#"{"method":"update_launcher_session","session_id":"independent","phase":"running","profile":"applied"}"#)["ok"], false);
    child.stdin.take().unwrap().write_all(b"exit\n").unwrap();
    assert!(child.wait().unwrap().success());
    loop {
        let event: Value = serde_json::from_str(&read_line(&mut reader).unwrap()).unwrap();
        if let Some(ended) = event["result"]["ended"]
            .as_array()
            .unwrap()
            .iter()
            .find(|e| e["session"]["session_id"] == "independent")
        {
            assert_eq!(ended["session"]["phase"], "failed");
            break;
        }
    }
    // A new subscriber atomically receives the latest terminal state, even if
    // registration, failure and exit were coalesced by a slow consumer.
    let recovered = daemon.request(r#"{"method":"launcher_sessions"}"#);
    assert!(recovered["result"]["ended"]
        .as_array()
        .unwrap()
        .iter()
        .any(|e| e["session"]["session_id"] == "independent"));
}

#[test]
fn launcher_session_recovers_without_replaying_gpu_writes() {
    let mut game = Command::new("/usr/bin/python3")
        .args([
            "-c",
            "import sys; print('ready', flush=True); sys.stdin.readline()",
        ])
        .env("PENGUIN_BURNER_SESSION_ID", "restart-recovery-test")
        .env("PENGUIN_BURNER_GAME_KEY", "lutris:recovery")
        // A daemon-down Flatpak launch may only know its namespace-local PID.
        .env("PENGUIN_BURNER_TELEMETRY_SESSION", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut output = BufReader::new(game.stdout.take().unwrap());
    assert_eq!(read_line(&mut output).unwrap().trim(), "ready");
    let daemon = Daemon::start(&[]);
    let snapshot = daemon.request(r#"{"method":"launcher_sessions"}"#);
    let recovered = snapshot["result"]["sessions"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["session_id"] == "restart-recovery-test")
        .unwrap();
    assert_eq!(recovered["pid"], game.id());
    assert_eq!(recovered["phase"], "running");
    assert_eq!(recovered["profile"], "unconfirmed");
    assert_eq!(
        daemon.request(r#"{"method":"status"}"#)["result"]["active_job"],
        Value::Null
    );
    drop(game.stdin.take());
    assert!(game.wait().unwrap().success());
}

#[test]
fn launcher_session_follows_exec_child_handoff() {
    for launcher in ["steam", "faugus"] {
        check_launcher_handoff(launcher);
    }
}

fn check_launcher_handoff(launcher: &str) {
    let daemon = Daemon::start(&[]);
    let mut stream = daemon.connect();
    stream
        .write_all(b"{\"method\":\"subscribe_launcher_sessions\"}\n")
        .unwrap();
    let mut reader = BufReader::new(stream);
    read_line(&mut reader).unwrap();
    let mut parent = Command::new("/usr/bin/python3")
        .args([
            "-c",
            r#"
import json, os, socket, subprocess, sys
identity = 'handoff-event-test'
def request(**payload):
    s = socket.socket(socket.AF_UNIX)
    s.connect(sys.argv[1])
    s.sendall(json.dumps(payload).encode() + b'\n')
    assert json.loads(s.makefile().readline())['ok']
request(method='register_launcher_session', app_id=sys.argv[3] + ':handoff', session_id=identity)
request(method='start_game_runtime_profile', spec=json.loads(sys.argv[2]), watch_pid=os.getpid(), app_id=sys.argv[3] + ':handoff')
request(method='update_launcher_session', session_id=identity, phase='running', profile='applied')
env = dict(os.environ, PENGUIN_BURNER_SESSION_ID=identity, PENGUIN_BURNER_GAME_KEY=sys.argv[3] + ':handoff')
child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.readline()'], env=env)
print(child.pid, flush=True)
"#,
        ])
        .arg(&daemon.socket)
        .arg(test_runtime_spec().to_string())
        .arg(launcher)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut output = BufReader::new(parent.stdout.take().unwrap());
    let successor: u32 = read_line(&mut output).unwrap().trim().parse().unwrap();
    let input = parent.stdin.take().unwrap();
    assert!(parent.wait().unwrap().success());
    loop {
        let event: Value = serde_json::from_str(&read_line(&mut reader).unwrap()).unwrap();
        let sessions = event["result"]["sessions"].as_array().unwrap();
        assert!(sessions
            .iter()
            .any(|s| s["session_id"] == "handoff-event-test"));
        if let Some(child) = sessions.iter().find(|s| s["pid"] == successor) {
            assert_eq!(child["phase"], "running");
            assert_eq!(child["profile"], "applied");
            break;
        }
    }
    // Outlive the former test-only 50 ms restoration grace. A UI handoff
    // alone must not leave the game running without its GPU runtime owner.
    std::thread::sleep(Duration::from_millis(150));
    assert_eq!(
        daemon.request(r#"{"method":"status"}"#)["result"]["game_runtime"]["active"],
        true
    );
    drop(input); // EOF terminates the controlled child; pidfd delivers its exit.
    loop {
        let event: Value = serde_json::from_str(&read_line(&mut reader).unwrap()).unwrap();
        if !event["result"]["sessions"]
            .as_array()
            .unwrap()
            .iter()
            .any(|s| s["session_id"] == "handoff-event-test")
        {
            break;
        }
    }
    for _ in 0..100 {
        let status = daemon.request(r#"{"method":"status"}"#);
        if status["result"]["game_runtime"].is_null() && status["result"]["active_job"].is_null() {
            return;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    panic!("game runtime did not end after the final child exit");
}

#[test]
fn launcher_session_duplicate_and_regressive_updates_are_idempotent() {
    let daemon = Daemon::start(&[]);
    let register = r#"{"method":"register_launcher_session","app_id":"heroic:ordered","session_id":"ordered"}"#;
    assert_eq!(daemon.request(register)["ok"], true);
    let before = daemon.request(r#"{"method":"launcher_sessions"}"#);
    assert_eq!(daemon.request(register)["ok"], true);
    assert_eq!(daemon.request(r#"{"method":"launcher_sessions"}"#), before);
    assert_eq!(daemon.request(r#"{"method":"update_launcher_session","session_id":"ordered","phase":"running","profile":"applied"}"#)["ok"], true);
    let running = daemon.request(r#"{"method":"launcher_sessions"}"#);
    assert_eq!(daemon.request(r#"{"method":"update_launcher_session","session_id":"ordered","phase":"running","profile":"unconfirmed"}"#)["ok"], true);
    assert_eq!(daemon.request(r#"{"method":"launcher_sessions"}"#), running);
    assert_eq!(daemon.request(r#"{"method":"update_launcher_session","session_id":"ordered","phase":"starting","profile":"unconfirmed"}"#)["ok"], true);
    assert_eq!(daemon.request(r#"{"method":"launcher_sessions"}"#), running);
    assert_eq!(daemon.request(r#"{"method":"update_launcher_session","session_id":"ordered","phase":"failed","profile":"applied"}"#)["ok"], true);
    let failed = daemon.request(r#"{"method":"launcher_sessions"}"#);
    assert_eq!(daemon.request(r#"{"method":"update_launcher_session","session_id":"ordered","phase":"running","profile":"applied"}"#)["ok"], true);
    assert_eq!(daemon.request(r#"{"method":"launcher_sessions"}"#), failed);
}

#[test]
fn faugus_external_session_verifies_identity_and_reports_exit() {
    let daemon = Daemon::start(&[]);
    let mut game = Command::new("/usr/bin/python3")
        .args([
            "-c",
            "import sys; print('ready', flush=True); sys.stdin.readline()",
        ])
        .env("FAUGUSID", "external-test")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut output = BufReader::new(game.stdout.take().unwrap());
    assert_eq!(read_line(&mut output).unwrap().trim(), "ready");
    let observe = |app_id: &str| {
        daemon.request(
            &serde_json::json!({
                "method": "observe_launcher_session", "pid": game.id(), "app_id": app_id,
            })
            .to_string(),
        )
    };
    assert_eq!(observe("faugus:other")["ok"], false);
    assert_eq!(observe("faugus:")["ok"], false);
    assert_eq!(observe("faugus:external-test")["ok"], true);
    let snapshot = daemon.request(r#"{"method":"launcher_sessions"}"#);
    let session = snapshot["result"]["sessions"]
        .as_array()
        .unwrap()
        .iter()
        .find(|session| session["pid"] == game.id())
        .unwrap();
    assert_eq!(session["app_id"], "faugus:external-test");
    assert_eq!(session["wrapped"], false);
    assert_eq!(session["profile"], "unconfirmed");
    drop(game.stdin.take());
    assert!(game.wait().unwrap().success());
    for _ in 0..100 {
        let snapshot = daemon.request(r#"{"method":"launcher_sessions"}"#);
        if snapshot["result"]["ended"]
            .as_array()
            .unwrap()
            .iter()
            .any(|entry| {
                entry["session"]["pid"] == game.id() && entry["session"]["phase"] == "exited"
            })
        {
            assert_eq!(
                daemon.request(r#"{"method":"status"}"#)["result"]["active_job"],
                Value::Null
            );
            return;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    panic!("Faugus exit was not reported");
}

#[test]
fn faugus_store_handoff_reclassifies_only_the_matching_executable() {
    use std::os::unix::process::CommandExt;
    let daemon = Daemon::start(&[]);
    let prefix = tempfile::tempdir().unwrap();
    std::fs::create_dir(prefix.path().join("dosdevices")).unwrap();
    std::fs::create_dir(prefix.path().join("drive_c")).unwrap();
    std::os::unix::fs::symlink("../drive_c", prefix.path().join("dosdevices/c:")).unwrap();
    let executable = prefix.path().join("drive_c/NFS.exe");
    std::fs::write(&executable, "").unwrap();
    let mut game = Command::new("/usr/bin/python3")
        .arg0(r"C:\NFS.exe")
        .args([
            "-c",
            "import sys; print('ready', flush=True); sys.stdin.readline()",
        ])
        .env("FAUGUSID", "ea-app")
        .env("WINEPREFIX", prefix.path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    assert_eq!(
        read_line(&mut BufReader::new(game.stdout.take().unwrap()))
            .unwrap()
            .trim(),
        "ready"
    );
    let observe = |app_id: &str, path: &std::path::Path| {
        daemon.request(
            &serde_json::json!({
                "method": "observe_launcher_session", "pid": game.id(),
                "app_id": app_id, "executable": path,
            })
            .to_string(),
        )
    };
    assert_eq!(observe("faugus:ea-app", &executable)["ok"], true);
    assert_eq!(
        observe("faugus:nfs", &prefix.path().join("elsewhere/NFS.exe"))["ok"],
        false
    );
    assert_eq!(observe("faugus:nfs", &executable)["ok"], true);
    let snapshot = daemon.request(r#"{"method":"launcher_sessions"}"#);
    let session = snapshot["result"]["sessions"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["pid"] == game.id())
        .unwrap();
    assert_eq!(session["app_id"], "faugus:nfs");
    assert_eq!(session["wrapped"], false);
    assert_eq!(session["profile"], "unconfirmed");
    drop(game.stdin.take());
    assert!(game.wait().unwrap().success());
}
