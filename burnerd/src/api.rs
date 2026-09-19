//! Wire response shapes + non-streaming request dispatch (`handle_request`).
//!
//! Every struct here serializes with its fields in declaration order, so the
//! emitted JSON key order is byte-compatible with the Python daemon (whose dicts
//! preserve insertion order). serde_json's default compact form matches Python's
//! `separators=(",", ":")`.

use std::io::Write;
use std::sync::Mutex;

use serde::Serialize;
use serde_json::Value;

use crate::delete;
use crate::gpu_rpc;
use crate::profile::RuntimeSpec;
use crate::supervisor::{self, Supervisor};

pub const PROTOCOL_MAJOR: u32 = 2;
pub const PROTOCOL_MINOR: u32 = 0;
pub const DAEMON_CAPABILITIES: &[&str] = &[
    "client-identity-v1",
    "deep-sleep-status-v1",
    "energy-savings-v1",
    "game-runtime-v1",
    "gpu-capabilities-v1",
    "gpu-telemetry-v1",
    "gpu-vf-snapshot-v1",
    "gpu-writes-v1",
    "runtime-spec-v1",
    "scan-stream-v1",
    "verification-stream-v1",
];

#[derive(Debug, Serialize)]
pub struct ActiveJob {
    #[serde(rename = "type")]
    pub job_type: &'static str,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub argv: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub profile_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub runtime_mode: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub gpu_uuid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub silent_fan_curve: Option<bool>,
    pub pid: u32,
    pub returncode: Option<i32>,
}

#[derive(Debug, Serialize)]
pub struct StatusResult {
    pub state: String,
    pub active_job: Option<ActiveJob>,
    pub version: String,
    /// Short git commit of this build ("unknown" for non-git source trees).
    /// The package version alone cannot distinguish builds within a release
    /// cycle, which let a stale installed daemon pass for a fresh one.
    pub build: String,
    pub protocol_major: u32,
    pub protocol_minor: u32,
    pub capabilities: &'static [&'static str],
    #[serde(skip_serializing_if = "Option::is_none")]
    pub game_runtime: Option<GameRuntimeStatus>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub energy_savings: Option<EnergySavingsStatus>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub deep_sleep: Option<crate::rtd3::DeepSleepStatus>,
}

#[derive(Debug, Serialize)]
pub struct EnergySavingsStatus {
    /// Seconds a PenguinBurner profile spent under an active GPU workload.
    pub active_seconds: f64,
    /// Accumulated energy saved versus stock, in watt-seconds (joules).
    pub saved_watt_seconds: f64,
}

#[derive(Debug, Serialize)]
pub struct GameRuntimeStatus {
    pub active: bool,
    pub watched: Vec<GameWatchStatus>,
    pub standing_profile_id: Option<String>,
    pub standing_runtime_mode: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct GameWatchStatus {
    pub pid: u32,
    pub app_id: String,
}

impl StatusResult {
    pub fn new(state: impl Into<String>, active_job: Option<ActiveJob>) -> Self {
        Self {
            state: state.into(),
            active_job,
            version: env!("CARGO_PKG_VERSION").to_string(),
            build: env!("PENGUIN_BURNERD_BUILD_ID").to_string(),
            protocol_major: PROTOCOL_MAJOR,
            protocol_minor: PROTOCOL_MINOR,
            capabilities: DAEMON_CAPABILITIES,
            game_runtime: None,
            energy_savings: None,
            deep_sleep: None,
        }
    }

    pub fn with_game_runtime(mut self, game_runtime: Option<GameRuntimeStatus>) -> Self {
        self.game_runtime = game_runtime;
        self
    }

    pub fn with_energy_savings(mut self, energy_savings: Option<EnergySavingsStatus>) -> Self {
        self.energy_savings = energy_savings;
        self
    }

    pub fn with_deep_sleep(mut self, deep_sleep: Option<crate::rtd3::DeepSleepStatus>) -> Self {
        self.deep_sleep = deep_sleep;
        self
    }
}

#[derive(Debug, Serialize)]
pub struct StartResult {
    pub started: bool,
    pub pid: u32,
    pub profile_id: String,
    pub runtime_mode: String,
    pub gpu_uuid: String,
}

#[derive(Debug, Serialize)]
pub struct StopResult {
    pub stopped: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub state: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pid: Option<u32>,
}

impl StopResult {
    pub fn idle() -> Self {
        StopResult {
            stopped: false,
            state: Some("idle"),
            pid: None,
        }
    }

    pub fn stopped(pid: u32) -> Self {
        StopResult {
            stopped: true,
            state: None,
            pid: Some(pid),
        }
    }
}

#[derive(Debug, Serialize)]
#[serde(untagged)]
pub enum MethodResult {
    Status(Box<StatusResult>),
    Start(StartResult),
    Stop(StopResult),
    /// Dynamic result dicts (milestone-B `gpu_*` writes + profile deletion).
    Value(Value),
}

#[derive(Debug, Serialize)]
struct OkEnvelope {
    ok: bool,
    result: MethodResult,
}

#[derive(Debug, Serialize)]
struct ErrEnvelope {
    ok: bool,
    error: String,
}

// Streaming payloads for `start_auto_uv_scan`.

#[derive(Debug, Serialize)]
pub struct StreamStarted {
    pub ok: bool,
    pub control: &'static str,
    pub pid: u32,
}

#[derive(Debug, Serialize)]
pub struct StreamLine {
    pub ok: bool,
    pub line: String,
}

#[derive(Debug, Serialize)]
pub struct StreamFinished {
    pub ok: bool,
    pub control: &'static str,
    pub exit_code: i32,
}

#[derive(Debug, Serialize)]
pub struct StreamError {
    pub ok: bool,
    pub error: String,
}

impl StreamStarted {
    pub fn new(pid: u32) -> Self {
        StreamStarted {
            ok: true,
            control: "started",
            pid,
        }
    }
}

impl StreamLine {
    pub fn new(line: String) -> Self {
        StreamLine { ok: true, line }
    }
}

impl StreamFinished {
    pub fn new(exit_code: i32) -> Self {
        StreamFinished {
            ok: true,
            control: "finished",
            exit_code,
        }
    }
}

impl StreamError {
    pub fn new(error: String) -> Self {
        StreamError { ok: false, error }
    }
}

/// Serialize `payload` as one compact JSON line and write it. Returns `false` if
/// the write/flush failed (client disconnected) — parity with the Python
/// handler's `_write_stream_payload` returning `False` on a broken pipe.
pub fn write_json_line<W: Write, T: Serialize>(writer: &mut W, payload: &T) -> bool {
    let Ok(mut text) = serde_json::to_string(payload) else {
        return false;
    };
    text.push('\n');
    writer
        .write_all(text.as_bytes())
        .and_then(|()| writer.flush())
        .is_ok()
}

/// Write a non-streaming response envelope. Returns `false` on write failure.
pub fn write_response<W: Write>(writer: &mut W, response: Result<MethodResult, String>) -> bool {
    match response {
        Ok(result) => write_json_line(writer, &OkEnvelope { ok: true, result }),
        Err(error) => write_json_line(writer, &ErrEnvelope { ok: false, error }),
    }
}

/// Dispatch a non-streaming request (`handle_request`). Field validation and the
/// error strings are byte-exact with `runtime/daemon_api.py::handle_request`;
/// the milestone-B methods (`gpu_*`, `stop_profile_verification`,
/// `delete_auto_uv_profiles`) are additive and reuse the same validation style.
///
/// Engine/scan interplay: `gpu_*` writes carry NO supervisor arbitration — they
/// are legal in any state. During a scan or verification the profile engine is
/// stopped by design and the streaming child is the intended caller of these
/// writes; adding arbitration here would deadlock the very flow they serve.
pub fn handle_request(sup: &Mutex<Supervisor>, payload: &Value) -> Result<MethodResult, String> {
    let object = match payload.as_object() {
        Some(object) => object,
        None => return Err("request must be a JSON object".to_string()),
    };

    let method = object.get("method").and_then(Value::as_str);
    let allowed_fields: &[&str] = match method {
        Some("apply_runtime_spec") => &["spec"],
        Some("start_game_runtime_profile") => &["spec", "watch_pid", "app_id"],
        Some("set_boot_runtime_spec") => &["spec"],
        Some("set_boot_main_gpu") => &["gpu_uuid"],
        Some("clear_boot_runtime_spec") => &["gpu_uuid"],
        Some("delete_auto_uv_profiles") => &["paths"],
        Some(name) if gpu_rpc::is_gpu_method(name) => gpu_rpc::allowed_fields(name),
        _ => &[],
    };

    let mut unknown: Vec<&str> = object
        .keys()
        .filter(|key| {
            let key = key.as_str();
            key != "method" && !allowed_fields.contains(&key)
        })
        .map(String::as_str)
        .collect();
    unknown.sort_unstable();
    if !unknown.is_empty() {
        return Err(format!("unknown request field: {}", unknown.join(", ")));
    }

    match method {
        Some("status") => Ok(MethodResult::Status(Box::new(supervisor::status(sup)))),
        Some("stop_auto_uv_scan") => Ok(MethodResult::Stop(supervisor::stop_auto_uv_scan(sup))),
        Some("stop_profile_verification") => Ok(MethodResult::Stop(
            supervisor::stop_profile_verification(sup),
        )),
        Some("apply_runtime_spec") => {
            let spec = parse_runtime_spec(object.get("spec"))?;
            supervisor::apply_runtime_spec(sup, spec).map(MethodResult::Start)
        }
        Some("start_game_runtime_profile") => {
            let spec = parse_runtime_spec(object.get("spec"))?;
            let watch_pid = parse_watch_pid(object.get("watch_pid"))?;
            let app_id = match object.get("app_id") {
                None => String::new(),
                Some(Value::String(value)) => value.clone(),
                Some(_) => return Err("app_id must be a string".to_string()),
            };
            supervisor::start_game_runtime_profile(sup, spec, watch_pid, app_id)
                .map(MethodResult::Value)
        }
        Some("set_boot_runtime_spec") => {
            let spec = parse_runtime_spec(object.get("spec"))?;
            supervisor::set_boot_runtime_spec(spec).map(MethodResult::Value)
        }
        Some("set_boot_main_gpu") => {
            let gpu_uuid = match object.get("gpu_uuid") {
                None => "",
                Some(Value::String(value)) => value.as_str(),
                Some(_) => return Err("gpu_uuid must be a string".to_string()),
            };
            supervisor::set_boot_main_gpu(gpu_uuid).map(MethodResult::Value)
        }
        Some("clear_boot_runtime_spec") => {
            let gpu_uuid = match object.get("gpu_uuid") {
                None => "",
                Some(Value::String(value)) => value.as_str(),
                Some(_) => return Err("gpu_uuid must be a string".to_string()),
            };
            supervisor::clear_boot_runtime_spec(gpu_uuid).map(MethodResult::Value)
        }
        Some("boot_runtime_spec") => {
            supervisor::boot_runtime_spec_summary(sup).map(MethodResult::Value)
        }
        Some("stop_runtime_profile") => {
            supervisor::stop_runtime_profile(sup).map(MethodResult::Stop)
        }
        Some("delete_auto_uv_profiles") => {
            delete::delete_auto_uv_profiles(object.get("paths")).map(MethodResult::Value)
        }
        Some("probe_power_limit_support") => {
            let gpu_index = gpu_rpc::request_gpu_index(object)?;
            if matches!(
                supervisor::active_child_kind(sup),
                Some(supervisor::ChildKind::Scan)
            ) {
                return Ok(MethodResult::Value(serde_json::json!({
                    "gpu_index": gpu_index,
                    "supported": false,
                    "reason": "auto-uv-scan-running",
                })));
            }
            gpu_rpc::probe_power_limit_support(object).map(MethodResult::Value)
        }
        Some(name) if gpu_rpc::is_gpu_method(name) => {
            // Raw GPU WRITES must not race the in-process profile engine. It is
            // stopped during a scan/verification (the streaming child is then
            // the intended writer), and "Restore defaults" reaches stock via
            // the arbitrated runtime-spec path, not a raw write — so a raw
            // write while the engine runs has no legitimate caller. Reject it
            // with a clear reason instead of two writers fighting for the GPU.
            if !gpu_rpc::is_read_method(name) && supervisor::profile_engine_running(sup) {
                return Err(format!(
                    "{name} refused: a runtime profile is active; stop it \
                     (or reset to stock) before raw GPU writes"
                ));
            }
            gpu_rpc::handle(name, object).map(MethodResult::Value)
        }
        Some(name) if !name.is_empty() => Err(format!("unknown daemon method: {name}")),
        _ => Err("request method is required".to_string()),
    }
}

fn parse_runtime_spec(value: Option<&Value>) -> Result<RuntimeSpec, String> {
    let value = value.ok_or_else(|| "spec must be an object".to_string())?;
    if !value.is_object() {
        return Err("spec must be an object".to_string());
    }
    let spec: RuntimeSpec = serde_json::from_value(value.clone())
        .map_err(|error| format!("invalid runtime spec: {error}"))?;
    spec.validate()?;
    Ok(spec)
}

fn parse_watch_pid(value: Option<&Value>) -> Result<u32, String> {
    let pid = value
        .and_then(Value::as_u64)
        .and_then(|value| u32::try_from(value).ok())
        .ok_or_else(|| "watch_pid must be an integer".to_string())?;
    if pid <= 1 {
        return Err(format!("watch_pid {pid} is not a running process"));
    }
    Ok(pid)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn fresh() -> Mutex<Supervisor> {
        Mutex::new(Supervisor::new())
    }

    #[test]
    fn status_idle_shape() {
        // Byte-exact golden: pin the savings state to a nonexistent path so a
        // live /var/lib counter on the build host cannot leak into the shape.
        std::env::set_var(
            crate::profile::savings::SAVINGS_STATE_FILE_ENV,
            "/nonexistent/penguin-burnerd-test-savings.json",
        );
        let sup = fresh();
        let result = handle_request(&sup, &json!({"method": "status"})).unwrap();
        let text = serde_json::to_string(&OkEnvelope { ok: true, result }).unwrap();
        assert_eq!(
            text,
            format!(
                "{{\"ok\":true,\"result\":{{\"state\":\"idle\",\"active_job\":null,\"version\":\"{}\",\"build\":\"{}\",\"protocol_major\":2,\"protocol_minor\":0,\"capabilities\":[\"client-identity-v1\",\"deep-sleep-status-v1\",\"energy-savings-v1\",\"game-runtime-v1\",\"gpu-capabilities-v1\",\"gpu-telemetry-v1\",\"gpu-vf-snapshot-v1\",\"gpu-writes-v1\",\"runtime-spec-v1\",\"scan-stream-v1\",\"verification-stream-v1\"]}}}}",
                env!("CARGO_PKG_VERSION"),
                env!("PENGUIN_BURNERD_BUILD_ID"),
            )
        );
    }

    #[test]
    fn rejects_unknown_field() {
        let sup = fresh();
        let err = handle_request(&sup, &json!({"method": "status", "argv": ["--x"]})).unwrap_err();
        assert_eq!(err, "unknown request field: argv");
    }

    #[test]
    fn rejects_unknown_method() {
        let sup = fresh();
        let err = handle_request(&sup, &json!({"method": "run_cli"})).unwrap_err();
        assert_eq!(err, "unknown daemon method: run_cli");
    }

    #[test]
    fn requires_method() {
        let sup = fresh();
        assert_eq!(
            handle_request(&sup, &json!({})).unwrap_err(),
            "request method is required"
        );
        assert_eq!(
            handle_request(&sup, &json!({"method": ""})).unwrap_err(),
            "request method is required"
        );
        assert_eq!(
            handle_request(&sup, &json!({"method": 5})).unwrap_err(),
            "request method is required"
        );
    }

    #[test]
    fn rejects_non_object_request() {
        let sup = fresh();
        assert_eq!(
            handle_request(&sup, &json!(5)).unwrap_err(),
            "request must be a JSON object"
        );
    }

    #[test]
    fn legacy_runtime_argv_method_is_rejected() {
        let sup = fresh();
        let err = handle_request(
            &sup,
            &json!({"method": "start_runtime_profile", "argv": ["--daemon-api", "/tmp/x"]}),
        )
        .unwrap_err();
        assert_eq!(err, "unknown request field: argv");
    }

    #[test]
    fn apply_runtime_spec_requires_a_typed_object() {
        let sup = fresh();
        let error = handle_request(&sup, &json!({"method": "apply_runtime_spec"})).unwrap_err();
        assert_eq!(error, "spec must be an object");

        let error = handle_request(
            &sup,
            &json!({"method": "apply_runtime_spec", "spec": ["--gpu-index", "0"]}),
        )
        .unwrap_err();
        assert_eq!(error, "spec must be an object");
    }

    #[test]
    fn stop_runtime_profile_when_idle() {
        let sup = fresh();
        let result = handle_request(&sup, &json!({"method": "stop_runtime_profile"})).unwrap();
        let text = serde_json::to_string(&OkEnvelope { ok: true, result }).unwrap();
        assert_eq!(
            text,
            "{\"ok\":true,\"result\":{\"stopped\":false,\"state\":\"idle\"}}"
        );
    }

    #[test]
    fn stop_profile_verification_when_idle() {
        let sup = fresh();
        let result = handle_request(&sup, &json!({"method": "stop_profile_verification"})).unwrap();
        let text = serde_json::to_string(&OkEnvelope { ok: true, result }).unwrap();
        assert_eq!(
            text,
            "{\"ok\":true,\"result\":{\"stopped\":false,\"state\":\"idle\"}}"
        );
    }

    #[test]
    fn gpu_method_rejects_unknown_field_and_bad_gpu_index() {
        let sup = fresh();
        // Both failures happen before any backend is opened (no NVML in tests).
        let err = handle_request(
            &sup,
            &json!({"method": "gpu_apply_power_limit", "gpu_index": 0, "power_limit_w": 1, "bogus": 1}),
        )
        .unwrap_err();
        assert_eq!(err, "unknown request field: bogus");

        let err = handle_request(
            &sup,
            &json!({"method": "gpu_apply_power_limit", "gpu_index": "x", "power_limit_w": 1}),
        )
        .unwrap_err();
        assert_eq!(err, "gpu_index must be an integer");
    }

    #[test]
    fn probe_power_limit_support_validates_request_before_backend() {
        let sup = fresh();
        let err = handle_request(
            &sup,
            &json!({"method": "probe_power_limit_support", "gpu_index": 0, "bogus": true}),
        )
        .unwrap_err();
        assert_eq!(err, "unknown request field: bogus");

        let err = handle_request(
            &sup,
            &json!({"method": "probe_power_limit_support", "gpu_index": "0"}),
        )
        .unwrap_err();
        assert_eq!(err, "gpu_index must be an integer");
    }

    #[test]
    fn delete_auto_uv_profiles_requires_a_string_list() {
        let sup = fresh();
        let err = handle_request(&sup, &json!({"method": "delete_auto_uv_profiles"})).unwrap_err();
        assert_eq!(err, "profile paths must be a JSON string list");
        let err = handle_request(
            &sup,
            &json!({"method": "delete_auto_uv_profiles", "paths": [1]}),
        )
        .unwrap_err();
        assert_eq!(err, "profile paths must be a JSON string list");
        // Unknown fields are rejected before any filesystem work.
        let err = handle_request(
            &sup,
            &json!({"method": "delete_auto_uv_profiles", "paths": [], "force": true}),
        )
        .unwrap_err();
        assert_eq!(err, "unknown request field: force");
    }
}
