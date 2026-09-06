//! GPU-write RPCs (milestone B1): thin socket exposure of the existing
//! [`GpuBackend`] write methods, so the Python Auto-UV sweep keeps its logic
//! byte-identical while every GPU mutation is implemented once, in Rust.
//!
//! Backend: one lazy shared `NvmlBackend` per `gpu_index`, opened on first use
//! and kept for the daemon's lifetime (NVML is refcounted/thread-safe; the
//! hidden-NVAPI get-mutate-set stays per-call inside the backend). All access
//! is serialized under one registry mutex — writes are slow NVML/NVAPI ops and
//! the scan child is the only intended caller, so contention is irrelevant.
//!
//! Failures relay the backend's exact `Display` text in the standard
//! `{"ok":false,"error":…}` envelope: the Python initial-check pattern-matches
//! these strings (`_looks_like_permission_error`), so message fidelity is
//! load-bearing.
//!
//! Test seam (never active in production): `PENGUIN_BURNERD_TEST_MOCK_GPU=1`
//! swaps the lazy backend for a canned [`MockGpu`] (supported core-clock steps
//! 1800/1900/2000/2100 MHz) and echoes the ops recorded during the call as a
//! `mock_ops` list in the result so integration tests can assert the exact
//! backend calls; `PENGUIN_BURNERD_TEST_MOCK_GPU_FAIL=<trait method>` injects a
//! `"<method> mock failure"` error to exercise the error-relay path.

use std::env;
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use crate::gpu::{
    mock::MockGpu, ClockType, GpuBackend, GpuError, GpuIdentity, GpuMemoryInfo, NvmlBackend,
    VfPoint,
};

const MOCK_ENV: &str = "PENGUIN_BURNERD_TEST_MOCK_GPU";
const MOCK_FAIL_ENV: &str = "PENGUIN_BURNERD_TEST_MOCK_GPU_FAIL";
/// Path of a PID-list file the mock re-reads on every `gpu_context_pids` call,
/// so integration tests can stage and remove GPU contexts while the daemon runs.
const MOCK_CONTEXT_PIDS_ENV: &str = "PENGUIN_BURNERD_TEST_MOCK_GPU_CONTEXT_PIDS";

/// method → request fields allowed besides `method`. Also the method registry:
/// a name missing here is "unknown daemon method".
const METHODS: &[(&str, &[&str])] = &[
    ("gpu_capabilities", &["gpu_index"]),
    ("gpu_telemetry", &["gpu_index"]),
    ("gpu_vf_snapshot", &["gpu_index"]),
    ("probe_power_limit_support", &["gpu_index"]),
    ("gpu_apply_vf_offsets", &["gpu_index", "offsets"]),
    ("gpu_apply_power_limit", &["gpu_index", "power_limit_w"]),
    (
        "gpu_apply_clock_offsets",
        &[
            "gpu_index",
            "gpc_clk_vf_offset_mhz",
            "mem_clk_vf_offset_mhz",
        ],
    ),
    (
        "gpu_apply_locked_core_clock",
        &[
            "gpu_index",
            "clock_mhz",
            "prefer_not_above",
            "snap_to_supported",
        ],
    ),
    (
        "gpu_apply_locked_core_clock_range",
        &[
            "gpu_index",
            "min_mhz",
            "max_mhz",
            "prefer_max_not_above",
            "snap_to_supported",
        ],
    ),
    ("gpu_reset_locked_core_clocks", &["gpu_index"]),
    ("gpu_reset_locked_memory_clocks", &["gpu_index"]),
    ("gpu_reset_defaults", &["gpu_index"]),
    ("gpu_reset_fans", &["gpu_index"]),
    ("gpu_enable_persistence_mode", &["gpu_index"]),
];

pub fn is_gpu_method(method: &str) -> bool {
    METHODS.iter().any(|(name, _)| *name == method)
}

pub fn allowed_fields(method: &str) -> &'static [&'static str] {
    METHODS
        .iter()
        .find(|(name, _)| *name == method)
        .map(|(_, fields)| *fields)
        .unwrap_or(&[])
}

pub fn request_gpu_index(request: &Map<String, Value>) -> Result<u32, String> {
    require_u32(request, "gpu_index")
}

enum RpcBackend {
    Real(Box<NvmlBackend>),
    Mock(Box<MockGpu>),
}

// SAFETY: the registry `Mutex` serializes every access, so the backend is only
// ever touched by one thread at a time — no data race on its interior
// `Cell`/`RefCell` caches. Cross-thread *reuse* (opened on one connection
// thread, called from another) is sound because the state it holds is not
// thread-affine: the NVML library init is refcounted process-global and NVML
// documents its API as thread-safe, and the hidden-NVAPI sessions hold only a
// process-global `dlopen` handle, process-global function pointers, and a
// process-stable physical-GPU handle (the same properties that let the former
// threaded Python daemon call these drivers from arbitrary handler threads).
unsafe impl Send for RpcBackend {}

struct RegistryEntry {
    gpu_index: u32,
    backend: RpcBackend,
    last_used: Instant,
}

/// Lazy per-`gpu_index` backends. On desktops they live for the daemon's
/// lifetime; in Mobile or Unknown mode the RTD3 watcher drops idle
/// entries (`release_idle_backends`) so a one-off telemetry query cannot pin
/// the GPU in D0 forever. Dropping a real entry closes NVML and the hidden
/// NVAPI sessions via their `Drop` impls; the next request simply reopens.
static REGISTRY: Mutex<Vec<RegistryEntry>> = Mutex::new(Vec::new());

/// Drop backends that have not served a request within `ttl`. Returns how
/// many were released. Called only while deep-sleep protection is active.
pub fn release_idle_backends(ttl: Duration) -> usize {
    let mut registry = REGISTRY.lock().unwrap_or_else(|poison| poison.into_inner());
    let before = registry.len();
    registry.retain(|entry| entry.last_used.elapsed() < ttl);
    before - registry.len()
}

/// Backends open right now — the daemon's own GPU hold count. Surfaced in the
/// deep-sleep status because the watcher's client sample excludes the daemon's
/// pid: without this number a daemon-held NVML session (a polling GUI's
/// telemetry backend) reads as "no clients" while it pins the GPU.
pub fn open_backend_count() -> usize {
    REGISTRY
        .lock()
        .unwrap_or_else(|poison| poison.into_inner())
        .len()
}

fn open_backend(gpu_index: u32) -> Result<RpcBackend, String> {
    if env::var_os(MOCK_ENV).is_some_and(|v| !v.is_empty()) {
        let mut mock = MockGpu::new();
        mock.gpu_index = gpu_index;
        mock.power_limits = crate::gpu::PowerLimits {
            power_management_enabled: Some(true),
            power_limit_w: Some(300),
            enforced_power_limit_w: Some(300),
            power_limit_default_w: Some(300),
            power_limit_min_w: Some(150),
            power_limit_max_w: Some(450),
        };
        mock.identity = GpuIdentity {
            index: gpu_index,
            name: "Mock NVIDIA GPU".to_string(),
            driver_version: "999.0".to_string(),
            pci_bus_id: format!("00000000:{:02X}:00.0", gpu_index + 1),
            pci_device_id: "0x000010DE".to_string(),
            uuid: format!("GPU-mock-{gpu_index}"),
        };
        mock.gpu_name = Some(mock.identity.name.clone());
        mock.memory_info = Some(GpuMemoryInfo {
            index: gpu_index,
            total_bytes: 16 * 1024 * 1024 * 1024,
            free_bytes: 12 * 1024 * 1024 * 1024,
            used_bytes: 4 * 1024 * 1024 * 1024,
        });
        mock.architecture = Some(10);
        mock.temperature_c = 55.0;
        mock.fan_count = 2;
        mock.fan_limits = (Some(30), Some(100));
        mock.reported_fan_speeds = Some(vec![42, 43]);
        mock.power_draw_w = Some(200.5);
        mock.utilization_pct = Some(97);
        mock.graphics_clock_mhz = Some(2_800);
        mock.sm_clock_mhz = Some(2_800);
        mock.memory_clock_mhz = Some(12_000);
        mock.video_clock_mhz = Some(1_500);
        mock.throttle_mask = Some(0);
        mock.supported_core_clocks = vec![1800, 1900, 2000, 2100];
        mock.supported_memory_clocks = vec![10_000, 12_000];
        mock.clock_offsets = crate::gpu::ClockOffsets {
            gpc_clk_vf_offset_mhz: Some(120),
            mem_clk_vf_offset_mhz: Some(1_500),
        };
        mock.mem_offset_range = Some((-2_000, 3_000));
        mock.voltage_uv = Some(900_000);
        mock.vf_available = true;
        mock.vf_points = vec![VfPoint {
            index: 12,
            type_: 0,
            voltage_based: 1,
            freq_khz: 2_800_000,
            voltage_uv: 900_000,
            base_freq_khz: 2_680_000,
            base_voltage_uv: 900_000,
            current_offset_khz: 120_000,
        }];
        mock.context_pids_file = env::var_os(MOCK_CONTEXT_PIDS_ENV)
            .filter(|v| !v.is_empty())
            .map(std::path::PathBuf::from);
        if let Ok(method) = env::var(MOCK_FAIL_ENV) {
            if !method.is_empty() {
                let message = format!("{method} mock failure");
                mock.inject_failure(
                    Box::leak(method.into_boxed_str()),
                    GpuError::other(message, 0),
                );
            }
        }
        return Ok(RpcBackend::Mock(Box::new(mock)));
    }
    NvmlBackend::open(gpu_index)
        .map(|backend| RpcBackend::Real(Box::new(backend)))
        .map_err(|err| err.to_string())
}

/// Find (or lazily open) the backend for `gpu_index`, stamping its last-use
/// time so `release_idle_backends` measures idleness from the newest request.
fn registry_position(registry: &mut Vec<RegistryEntry>, gpu_index: u32) -> Result<usize, String> {
    match registry
        .iter()
        .position(|entry| entry.gpu_index == gpu_index)
    {
        Some(position) => {
            registry[position].last_used = Instant::now();
            Ok(position)
        }
        None => {
            registry.push(RegistryEntry {
                gpu_index,
                backend: open_backend(gpu_index)?,
                last_used: Instant::now(),
            });
            Ok(registry.len() - 1)
        }
    }
}

/// A fully-parsed, validated GPU write. Parsing is pure (no backend), so every
/// field-validation error is returned BEFORE any NVML/NVAPI init.
enum GpuWrite {
    VfOffsets(Vec<(u32, i32)>),
    PowerLimit(i64),
    ClockOffsets {
        gpc: Option<i32>,
        mem: Option<i32>,
    },
    LockedCoreClock {
        clock_mhz: i64,
        prefer_not_above: bool,
        snap: bool,
    },
    LockedCoreClockRange {
        min_mhz: i64,
        max_mhz: i64,
        prefer_max_not_above: bool,
        snap: bool,
    },
    ResetLockedCore,
    ResetLockedMemory,
    ResetDefaults,
    ResetFans,
    EnablePersistence,
}

/// Dispatch one `gpu_*` request. `request` is the full request object (with
/// `method`); unknown-field rejection already happened in `api::handle_request`.
///
/// Supervisor interplay: GPU writes are deliberately legal in ANY supervisor
/// state — while a scan or verification runs the engine is stopped by design
/// and the child is the intended caller; there is no further arbitration.
pub fn handle(method: &str, request: &Map<String, Value>) -> Result<Value, String> {
    // Validate everything (gpu_index + method fields) before opening a backend,
    // so a bad request never pays an NVML init nor masks the field error with
    // an NVML-open error on a GPU-less machine.
    let gpu_index = require_u32(request, "gpu_index")?;
    if is_read_method(method) {
        return read(gpu_index, method);
    }
    let write = parse(method, request)?;
    if matches!(&write, GpuWrite::EnablePersistence) && crate::rtd3::protects_deep_sleep() {
        return Err(
            "GPU persistence mode is unavailable in Mobile or Unknown deep-sleep mode".to_string(),
        );
    }

    let mut registry = REGISTRY.lock().unwrap_or_else(|poison| poison.into_inner());
    let position = registry_position(&mut registry, gpu_index)?;

    match &registry[position].backend {
        RpcBackend::Real(backend) => execute(backend.as_ref(), write),
        // Test seam only: echo the ops recorded during this call back to the
        // integration test (its sole cross-process channel). Never runs in prod.
        RpcBackend::Mock(mock) => {
            let before = mock.recorded().len();
            let mut result = execute(mock.as_ref(), write)?;
            let ops: Vec<Value> = mock.recorded()[before..]
                .iter()
                .map(|op| Value::String(format!("{op:?}")))
                .collect();
            if let Some(object) = result.as_object_mut() {
                object.insert("mock_ops".to_string(), Value::Array(ops));
            }
            Ok(result)
        }
    }
}

pub fn is_read_method(method: &str) -> bool {
    matches!(
        method,
        "gpu_capabilities" | "gpu_telemetry" | "gpu_vf_snapshot"
    )
}

fn read(gpu_index: u32, method: &str) -> Result<Value, String> {
    let mut registry = REGISTRY.lock().unwrap_or_else(|poison| poison.into_inner());
    let position = registry_position(&mut registry, gpu_index)?;
    let backend: &dyn GpuBackend = match &registry[position].backend {
        RpcBackend::Real(backend) => backend.as_ref(),
        RpcBackend::Mock(backend) => backend.as_ref(),
    };
    match method {
        "gpu_capabilities" => capabilities(backend),
        "gpu_telemetry" => telemetry(backend),
        "gpu_vf_snapshot" => vf_snapshot(backend),
        other => Err(format!("unknown daemon method: {other}")),
    }
}

fn capabilities(backend: &dyn GpuBackend) -> Result<Value, String> {
    let gpu_count = backend.gpu_count().map_err(|err| err.to_string())?;
    let identity = backend.identity();
    let memory = backend.memory_info();
    let fan_count = backend.fan_count().ok();
    let (fan_min_speed_pct, fan_max_speed_pct) = backend.fan_speed_limits();
    let power_limits = backend.query_power_limits();
    let clock_offsets = backend.clock_offsets();
    let memory_offset_range = backend.memory_clock_offset_range_mhz();
    let summary = backend.vf_summary();
    Ok(json!({
        "gpu_index": backend.gpu_index(),
        "gpu_count": gpu_count,
        "identity": identity_json(identity),
        "memory": memory.map(memory_json),
        "architecture": backend.architecture(),
        "power_limits": power_limits_json(power_limits),
        "supported_memory_clock_steps_mhz": backend.supported_memory_clock_steps_mhz(),
        "supported_core_clock_steps_mhz": backend.supported_core_clock_steps_mhz(),
        "clock_offset_ranges_mhz": {
            "memory": memory_offset_range.map(|(min, max)| [min, max]),
        },
        "clock_offsets_mhz": {
            "gpc": clock_offsets.gpc_clk_vf_offset_mhz,
            "memory": clock_offsets.mem_clk_vf_offset_mhz,
        },
        "fan": {
            "count": fan_count,
            "min_speed_pct": fan_min_speed_pct,
            "max_speed_pct": fan_max_speed_pct,
        },
        "features": {
            "vf_curve": backend.vf_curve_available(),
            "voltage": backend.read_voltage_uv().is_some(),
        },
        "vf_summary": {
            "active_points": summary.active_points,
            "editable_core_points": summary.editable_core_points,
        },
    }))
}

fn telemetry(backend: &dyn GpuBackend) -> Result<Value, String> {
    let temperature_c = backend.temperature_c().map_err(|err| err.to_string())?;
    let fan_count = backend.fan_count().ok();
    let fan_speeds_pct = fan_count.and_then(|count| backend.reported_fan_speeds(count));
    let voltage_uv = backend.read_voltage_uv();
    let offsets = backend.clock_offsets();
    Ok(json!({
        "gpu_index": backend.gpu_index(),
        "updated_unix_ns": unix_time_ns(),
        "temperature_c": temperature_c,
        "fan_speeds_pct": fan_speeds_pct,
        "power_draw_w": backend.power_draw_w(),
        "gpu_utilization_pct": backend.gpu_utilization_pct(),
        "clocks_mhz": {
            "graphics": backend.clock_info_mhz(ClockType::Graphics),
            "sm": backend.clock_info_mhz(ClockType::Sm),
            "memory": backend.clock_info_mhz(ClockType::Memory),
            "video": backend.clock_info_mhz(ClockType::Video),
        },
        "voltage_uv": voltage_uv,
        "voltage_mv": voltage_uv.map(|value| value / 1_000),
        "throttle_reason_mask": backend.throttle_reason_mask(),
        "clock_offsets_mhz": {
            "gpc": offsets.gpc_clk_vf_offset_mhz,
            "memory": offsets.mem_clk_vf_offset_mhz,
        },
    }))
}

fn vf_snapshot(backend: &dyn GpuBackend) -> Result<Value, String> {
    if !backend.vf_curve_available() {
        return Err("hidden NVIDIA V/F curve is unavailable for this GPU".to_string());
    }
    backend.refresh_vf_points().map_err(|err| err.to_string())?;
    let points = backend.vf_points();
    let summary = backend.vf_summary();
    Ok(json!({
        "gpu_index": backend.gpu_index(),
        "updated_unix_ns": unix_time_ns(),
        "points": points.into_iter().map(vf_point_json).collect::<Vec<_>>(),
        "summary": {
            "active_points": summary.active_points,
            "editable_core_points": summary.editable_core_points,
        },
    }))
}

fn identity_json(identity: GpuIdentity) -> Value {
    json!({
        "index": identity.index,
        "name": identity.name,
        "driver_version": identity.driver_version,
        "pci_bus_id": identity.pci_bus_id,
        "pci_device_id": identity.pci_device_id,
        "uuid": identity.uuid,
    })
}

fn memory_json(memory: GpuMemoryInfo) -> Value {
    json!({
        "index": memory.index,
        "total_bytes": memory.total_bytes,
        "free_bytes": memory.free_bytes,
        "used_bytes": memory.used_bytes,
    })
}

fn vf_point_json(point: VfPoint) -> Value {
    json!({
        "index": point.index,
        "type": point.type_,
        "voltage_based": point.voltage_based,
        "freq_khz": point.freq_khz,
        "voltage_uv": point.voltage_uv,
        "base_freq_khz": point.base_freq_khz,
        "base_voltage_uv": point.base_voltage_uv,
        "current_offset_khz": point.current_offset_khz,
    })
}

fn unix_time_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| u64::try_from(duration.as_nanos()).unwrap_or(u64::MAX))
        .unwrap_or(0)
}

/// Probe whether the driver accepts power-limit writes by re-applying the
/// current limit. Unlike `gpu_apply_power_limit`, this is a capability probe:
/// driver/open/setter failures are reported as `supported:false` instead of a
/// failing daemon request so the UI can disable the control.
pub fn probe_power_limit_support(request: &Map<String, Value>) -> Result<Value, String> {
    let gpu_index = request_gpu_index(request)?;
    let mut registry = REGISTRY.lock().unwrap_or_else(|poison| poison.into_inner());
    let position = match registry_position(&mut registry, gpu_index) {
        Ok(position) => position,
        Err(err) => {
            return Ok(json!({
                "gpu_index": gpu_index,
                "supported": false,
                "reason": err,
            }));
        }
    };

    match &registry[position].backend {
        RpcBackend::Real(backend) => probe_power_limit_backend(gpu_index, backend.as_ref()),
        RpcBackend::Mock(mock) => {
            let before = mock.recorded().len();
            let mut result = probe_power_limit_backend(gpu_index, mock.as_ref())?;
            let ops: Vec<Value> = mock.recorded()[before..]
                .iter()
                .map(|op| Value::String(format!("{op:?}")))
                .collect();
            if let Some(object) = result.as_object_mut() {
                object.insert("mock_ops".to_string(), Value::Array(ops));
            }
            Ok(result)
        }
    }
}

fn probe_power_limit_backend(gpu_index: u32, backend: &dyn GpuBackend) -> Result<Value, String> {
    let power_limits = backend.query_power_limits();
    let Some(current_w) = power_limits.power_limit_w.filter(|w| *w > 0) else {
        return Ok(json!({
            "gpu_index": gpu_index,
            "supported": false,
            "reason": "current-power-limit-unavailable",
            "power_limits": power_limits_json(power_limits),
        }));
    };
    match backend.apply_power_limit_w(current_w) {
        Ok(applied_w) => {
            let readback = backend.query_power_limits();
            Ok(json!({
                "gpu_index": gpu_index,
                "supported": true,
                "probe_power_limit_w": applied_w,
                "power_limits": power_limits_json(readback),
            }))
        }
        Err(err) => Ok(json!({
            "gpu_index": gpu_index,
            "supported": false,
            "reason": err.to_string(),
            "probe_power_limit_w": current_w,
            "power_limits": power_limits_json(power_limits),
        })),
    }
}

fn power_limits_json(power_limits: crate::gpu::PowerLimits) -> Value {
    json!({
        "power_management_enabled": power_limits.power_management_enabled,
        "power_limit_w": power_limits.power_limit_w,
        "enforced_power_limit_w": power_limits.enforced_power_limit_w,
        "power_limit_default_w": power_limits.power_limit_default_w,
        "power_limit_min_w": power_limits.power_limit_min_w,
        "power_limit_max_w": power_limits.power_limit_max_w,
    })
}

/// Parse + validate the method's request fields into a [`GpuWrite`]. Pure.
fn parse(method: &str, request: &Map<String, Value>) -> Result<GpuWrite, String> {
    Ok(match method {
        "gpu_apply_vf_offsets" => GpuWrite::VfOffsets(require_offsets(request)?),
        "gpu_apply_power_limit" => GpuWrite::PowerLimit(require_i64(request, "power_limit_w")?),
        "gpu_apply_clock_offsets" => GpuWrite::ClockOffsets {
            gpc: optional_i32(request, "gpc_clk_vf_offset_mhz")?,
            mem: optional_i32(request, "mem_clk_vf_offset_mhz")?,
        },
        "gpu_apply_locked_core_clock" => GpuWrite::LockedCoreClock {
            clock_mhz: require_i64(request, "clock_mhz")?,
            prefer_not_above: optional_bool(request, "prefer_not_above")?.unwrap_or(true),
            snap: optional_bool(request, "snap_to_supported")?.unwrap_or(true),
        },
        "gpu_apply_locked_core_clock_range" => GpuWrite::LockedCoreClockRange {
            min_mhz: require_i64(request, "min_mhz")?,
            max_mhz: require_i64(request, "max_mhz")?,
            prefer_max_not_above: optional_bool(request, "prefer_max_not_above")?.unwrap_or(true),
            snap: optional_bool(request, "snap_to_supported")?.unwrap_or(true),
        },
        "gpu_reset_locked_core_clocks" => GpuWrite::ResetLockedCore,
        "gpu_reset_locked_memory_clocks" => GpuWrite::ResetLockedMemory,
        "gpu_reset_defaults" => GpuWrite::ResetDefaults,
        "gpu_reset_fans" => GpuWrite::ResetFans,
        "gpu_enable_persistence_mode" => GpuWrite::EnablePersistence,
        other => return Err(format!("unknown daemon method: {other}")),
    })
}

/// Run a parsed write against `backend`, relaying the backend's exact error
/// text and returning the method's result dict.
fn execute(backend: &dyn GpuBackend, write: GpuWrite) -> Result<Value, String> {
    match write {
        GpuWrite::VfOffsets(offsets) => {
            backend
                .apply_vf_offsets_khz(&offsets)
                .map_err(|err| err.to_string())?;
            Ok(json!({ "applied": offsets.len() }))
        }
        GpuWrite::PowerLimit(watts) => {
            let applied = backend
                .apply_power_limit_w(watts)
                .map_err(|err| err.to_string())?;
            Ok(json!({ "applied_w": applied }))
        }
        GpuWrite::ClockOffsets { gpc, mem } => {
            let applied = backend
                .apply_clock_offsets(gpc, mem)
                .map_err(|err| err.to_string())?;
            // Mirror the Python dict: only the requested sides carry keys, the
            // mandatory read-back may be null (issue #20: it can differ).
            let mut object = Map::new();
            if let Some(value) = applied.gpc_clk_vf_offset_mhz {
                object.insert("gpc_clk_vf_offset_mhz".to_string(), json!(value));
                object.insert(
                    "gpc_clk_vf_offset_readback_mhz".to_string(),
                    json!(applied.gpc_clk_vf_offset_readback_mhz),
                );
            }
            if let Some(value) = applied.mem_clk_vf_offset_mhz {
                object.insert("mem_clk_vf_offset_mhz".to_string(), json!(value));
                object.insert(
                    "mem_clk_vf_offset_readback_mhz".to_string(),
                    json!(applied.mem_clk_vf_offset_readback_mhz),
                );
            }
            Ok(Value::Object(object))
        }
        GpuWrite::LockedCoreClock {
            clock_mhz,
            prefer_not_above,
            snap,
        } => {
            let result = backend
                .apply_locked_core_clock_mhz(clock_mhz, prefer_not_above, snap)
                .map_err(|err| err.to_string())?;
            // The snap struct's field names ARE the wire keys (Serialize derive).
            Ok(serde_json::to_value(result).expect("SnapResult serializes"))
        }
        GpuWrite::LockedCoreClockRange {
            min_mhz,
            max_mhz,
            prefer_max_not_above,
            snap,
        } => {
            let result = backend
                .apply_locked_core_clock_range_mhz(min_mhz, max_mhz, prefer_max_not_above, snap)
                .map_err(|err| err.to_string())?;
            Ok(serde_json::to_value(result).expect("RangeSnapResult serializes"))
        }
        GpuWrite::ResetLockedCore => {
            backend
                .reset_locked_core_clocks()
                .map_err(|err| err.to_string())?;
            Ok(json!({ "reset": true }))
        }
        GpuWrite::ResetLockedMemory => {
            backend
                .reset_locked_memory_clocks()
                .map_err(|err| err.to_string())?;
            Ok(json!({ "reset": true }))
        }
        GpuWrite::ResetDefaults => {
            let power_limit_set_supported = crate::profile::reset_gpu_to_stock(backend)?;
            let identity = backend.identity();
            let power_limits = backend.query_power_limits();
            let clock_offsets = backend.clock_offsets();
            Ok(json!({
                "reset": true,
                "power_limit_set_supported": power_limit_set_supported,
                "gpu_name": identity.name,
                "pci_device_id": identity.pci_device_id,
                "power_limits": power_limits_json(power_limits),
                "clock_offsets_mhz": {
                    "gpc": clock_offsets.gpc_clk_vf_offset_mhz,
                    "memory": clock_offsets.mem_clk_vf_offset_mhz,
                },
                "points": backend
                    .editable_core_vf_points()
                    .into_iter()
                    .map(vf_point_json)
                    .collect::<Vec<_>>(),
            }))
        }
        GpuWrite::ResetFans => {
            let fan_count = backend.fan_count().map_err(|err| err.to_string())?;
            backend
                .set_all_fans_default(fan_count)
                .map_err(|err| err.to_string())?;
            Ok(json!({ "reset": true, "fan_count": fan_count }))
        }
        GpuWrite::EnablePersistence => {
            backend
                .enable_persistence_mode()
                .map_err(|err| err.to_string())?;
            Ok(json!({ "enabled": true }))
        }
    }
}

// --- request field validation -------------------------------------------
// Type errors use one fixed message per field so clients get a stable string;
// missing and mistyped are deliberately the same error.

fn require_u32(request: &Map<String, Value>, field: &str) -> Result<u32, String> {
    request
        .get(field)
        .and_then(Value::as_u64)
        .and_then(|value| u32::try_from(value).ok())
        .ok_or_else(|| format!("{field} must be an integer"))
}

fn require_i64(request: &Map<String, Value>, field: &str) -> Result<i64, String> {
    request
        .get(field)
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("{field} must be an integer"))
}

/// Optional signed-MHz offset: absent or `null` → `None` (Python kwarg `None`).
fn optional_i32(request: &Map<String, Value>, field: &str) -> Result<Option<i32>, String> {
    match request.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => value
            .as_i64()
            .and_then(|v| i32::try_from(v).ok())
            .map(Some)
            .ok_or_else(|| format!("{field} must be an integer")),
    }
}

fn optional_bool(request: &Map<String, Value>, field: &str) -> Result<Option<bool>, String> {
    match request.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Bool(flag)) => Ok(Some(*flag)),
        Some(_) => Err(format!("{field} must be a boolean")),
    }
}

/// `offsets`: a list of `[index, offset_khz]` integer pairs (the VF plan). The
/// backend owns the NVAPI get-mutate-set and preserves non-listed points.
fn require_offsets(request: &Map<String, Value>) -> Result<Vec<(u32, i32)>, String> {
    const ERR: &str = "offsets must be a list of [index, offset_khz] integer pairs";
    let items = request
        .get("offsets")
        .and_then(Value::as_array)
        .ok_or_else(|| ERR.to_string())?;
    let mut offsets = Vec::with_capacity(items.len());
    for item in items {
        let pair = item.as_array().filter(|p| p.len() == 2);
        let index = pair
            .and_then(|p| p[0].as_u64())
            .and_then(|v| u32::try_from(v).ok());
        let offset = pair
            .and_then(|p| p[1].as_i64())
            .and_then(|v| i32::try_from(v).ok());
        match (index, offset) {
            (Some(index), Some(offset)) => offsets.push((index, offset)),
            _ => return Err(ERR.to_string()),
        }
    }
    Ok(offsets)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gpu::mock::MockGpu;

    fn request(json_text: &str) -> Map<String, Value> {
        serde_json::from_str::<Value>(json_text)
            .unwrap()
            .as_object()
            .unwrap()
            .clone()
    }

    fn mock() -> MockGpu {
        let mut mock = MockGpu::new();
        mock.supported_memory_clocks = vec![10_000, 12_000];
        mock.supported_core_clocks = vec![1800, 1900, 2000, 2100];
        mock
    }

    fn readable_mock() -> MockGpu {
        let mut mock = mock();
        mock.gpu_index = 1;
        mock.gpu_count = 2;
        mock.identity = GpuIdentity {
            index: 1,
            name: "Mock RTX".into(),
            driver_version: "999.1".into(),
            pci_bus_id: "00000000:02:00.0".into(),
            pci_device_id: "0x123410DE".into(),
            uuid: "GPU-readable".into(),
        };
        mock.memory_info = Some(GpuMemoryInfo {
            index: 1,
            total_bytes: 16_000,
            free_bytes: 12_000,
            used_bytes: 4_000,
        });
        mock.architecture = Some(10);
        mock.temperature_c = 61.5;
        mock.fan_count = 2;
        mock.fan_limits = (Some(30), Some(100));
        mock.reported_fan_speeds = Some(vec![40, 42]);
        mock.power_draw_w = Some(250.25);
        mock.utilization_pct = Some(98);
        mock.graphics_clock_mhz = Some(2_800);
        mock.sm_clock_mhz = Some(2_790);
        mock.memory_clock_mhz = Some(12_000);
        mock.video_clock_mhz = Some(1_500);
        mock.throttle_mask = Some(8);
        mock.power_limits = crate::gpu::PowerLimits {
            power_management_enabled: Some(true),
            power_limit_w: Some(300),
            enforced_power_limit_w: Some(295),
            power_limit_default_w: Some(360),
            power_limit_min_w: Some(180),
            power_limit_max_w: Some(450),
        };
        mock.clock_offsets = crate::gpu::ClockOffsets {
            gpc_clk_vf_offset_mhz: Some(120),
            mem_clk_vf_offset_mhz: Some(1_500),
        };
        mock.mem_offset_range = Some((-2_000, 3_000));
        mock.voltage_uv = Some(900_000);
        mock.vf_available = true;
        mock.vf_points = vec![VfPoint {
            index: 12,
            type_: 0,
            voltage_based: 1,
            freq_khz: 2_800_000,
            voltage_uv: 900_000,
            base_freq_khz: 2_680_000,
            base_voltage_uv: 900_000,
            current_offset_khz: 120_000,
        }];
        mock
    }

    /// Test shim: parse + execute in one call. Production splits them so field
    /// validation runs before any backend opens.
    fn dispatch(
        backend: &dyn GpuBackend,
        method: &str,
        request: &Map<String, Value>,
    ) -> Result<Value, String> {
        execute(backend, parse(method, request)?)
    }

    #[test]
    fn method_registry_and_fields() {
        assert!(is_gpu_method("gpu_capabilities"));
        assert!(is_gpu_method("gpu_telemetry"));
        assert!(is_gpu_method("gpu_vf_snapshot"));
        assert!(is_gpu_method("gpu_reset_defaults"));
        assert!(is_gpu_method("gpu_reset_fans"));
        assert!(is_gpu_method("probe_power_limit_support"));
        assert!(is_gpu_method("gpu_apply_vf_offsets"));
        assert!(is_gpu_method("gpu_enable_persistence_mode"));
        assert!(!is_gpu_method("gpu_frobnicate"));
        assert!(!is_gpu_method("status"));
        assert_eq!(
            allowed_fields("gpu_apply_power_limit"),
            &["gpu_index", "power_limit_w"]
        );
        assert!(allowed_fields("nope").is_empty());
    }

    #[test]
    fn capability_snapshot_is_coarse_and_typed() {
        let mock = readable_mock();
        let result = capabilities(&mock).unwrap();

        assert_eq!(result["gpu_index"], 1);
        assert_eq!(result["gpu_count"], 2);
        assert_eq!(result["identity"]["uuid"], "GPU-readable");
        assert_eq!(result["identity"]["pci_bus_id"], "00000000:02:00.0");
        assert_eq!(result["memory"]["total_bytes"], 16_000);
        assert_eq!(result["architecture"], 10);
        assert_eq!(result["fan"]["count"], 2);
        assert_eq!(result["power_limits"]["power_limit_max_w"], 450);
        assert_eq!(result["power_limits"]["power_management_enabled"], true);
        assert_eq!(result["power_limits"]["enforced_power_limit_w"], 295);
        assert_eq!(
            result["supported_memory_clock_steps_mhz"],
            json!([10_000, 12_000])
        );
        assert_eq!(
            result["supported_core_clock_steps_mhz"],
            json!([1800, 1900, 2000, 2100])
        );
        assert_eq!(
            result["clock_offset_ranges_mhz"]["memory"],
            json!([-2000, 3000])
        );
        assert_eq!(result["features"]["vf_curve"], true);
        assert_eq!(result["vf_summary"]["editable_core_points"], 1);
    }

    #[test]
    fn telemetry_snapshot_uses_one_backend_sample() {
        let mock = readable_mock();
        let result = telemetry(&mock).unwrap();

        assert_eq!(result["gpu_index"], 1);
        assert!(result["updated_unix_ns"].as_u64().unwrap() > 0);
        assert_eq!(result["temperature_c"], 61.5);
        assert_eq!(result["fan_speeds_pct"], json!([40, 42]));
        assert_eq!(result["power_draw_w"], 250.25);
        assert_eq!(result["clocks_mhz"]["graphics"], 2_800);
        assert_eq!(result["voltage_uv"], 900_000);
        assert_eq!(result["voltage_mv"], 900);
        assert_eq!(result["clock_offsets_mhz"]["memory"], 1_500);
    }

    #[test]
    fn vf_snapshot_refreshes_and_preserves_raw_units() {
        let mock = readable_mock();
        let result = vf_snapshot(&mock).unwrap();

        assert_eq!(result["summary"]["active_points"], 1);
        assert_eq!(result["points"][0]["index"], 12);
        assert_eq!(result["points"][0]["type"], 0);
        assert_eq!(result["points"][0]["voltage_uv"], 900_000);
        assert_eq!(result["points"][0]["current_offset_khz"], 120_000);

        let unavailable = MockGpu::new();
        assert_eq!(
            vf_snapshot(&unavailable).unwrap_err(),
            "hidden NVIDIA V/F curve is unavailable for this GPU"
        );
    }

    #[test]
    fn probe_power_limit_support_reapplies_current_limit() {
        let mut mock = mock();
        mock.power_limits = crate::gpu::PowerLimits {
            power_management_enabled: Some(true),
            power_limit_w: Some(320),
            enforced_power_limit_w: Some(320),
            power_limit_default_w: Some(360),
            power_limit_min_w: Some(200),
            power_limit_max_w: Some(450),
        };
        let result = probe_power_limit_backend(2, &mock).unwrap();

        assert_eq!(
            result,
            json!({
                "gpu_index": 2,
                "supported": true,
                "probe_power_limit_w": 320,
                "power_limits": {
                    "power_management_enabled": true,
                    "power_limit_w": 320,
                    "enforced_power_limit_w": 320,
                    "power_limit_default_w": 360,
                    "power_limit_min_w": 200,
                    "power_limit_max_w": 450,
                },
            })
        );
        assert_eq!(
            mock.recorded(),
            vec![crate::gpu::mock::MockOp::ApplyPowerLimit { power_limit_w: 320 }]
        );
    }

    #[test]
    fn probe_power_limit_support_reports_setter_rejection() {
        let mut mock = mock();
        mock.power_limits.power_limit_w = Some(320);
        mock.inject_failure(
            "apply_power_limit_w",
            GpuError::nvml_with_text("nvmlDeviceSetPowerManagementLimit", 3, "Not Supported"),
        );

        let result = probe_power_limit_backend(0, &mock).unwrap();

        assert_eq!(result["gpu_index"], 0);
        assert_eq!(result["supported"], false);
        assert_eq!(result["probe_power_limit_w"], 320);
        assert_eq!(
            result["reason"],
            "nvmlDeviceSetPowerManagementLimit failed with NVML error 3: Not Supported"
        );
        assert_eq!(
            mock.recorded(),
            vec![crate::gpu::mock::MockOp::ApplyPowerLimit { power_limit_w: 320 }]
        );
    }

    #[test]
    fn probe_power_limit_support_reports_missing_current_limit() {
        let mock = mock();
        let result = probe_power_limit_backend(0, &mock).unwrap();

        assert_eq!(
            result,
            json!({
                "gpu_index": 0,
                "supported": false,
                "reason": "current-power-limit-unavailable",
                "power_limits": {
                    "power_management_enabled": null,
                    "power_limit_w": null,
                    "enforced_power_limit_w": null,
                    "power_limit_default_w": null,
                    "power_limit_min_w": null,
                    "power_limit_max_w": null,
                },
            })
        );
        assert!(mock.recorded().is_empty());
    }

    #[test]
    fn vf_offsets_apply_and_count() {
        let mock = mock();
        let req = request(r#"{"gpu_index":0,"offsets":[[12,150000],[13,-15000]]}"#);
        let result = dispatch(&mock, "gpu_apply_vf_offsets", &req).unwrap();
        assert_eq!(result, json!({"applied": 2}));
        assert_eq!(
            mock.recorded(),
            vec![crate::gpu::mock::MockOp::ApplyVfOffsets {
                offsets: vec![(12, 150_000), (13, -15_000)]
            }]
        );
    }

    #[test]
    fn vf_offsets_validation_rejects_bad_shapes() {
        let mock = mock();
        const ERR: &str = "offsets must be a list of [index, offset_khz] integer pairs";
        for bad in [
            r#"{"gpu_index":0}"#,                            // missing
            r#"{"gpu_index":0,"offsets":5}"#,                // not a list
            r#"{"gpu_index":0,"offsets":[[1]]}"#,            // not a pair
            r#"{"gpu_index":0,"offsets":[[1,2,3]]}"#,        // too long
            r#"{"gpu_index":0,"offsets":[[1,"x"]]}"#,        // non-int offset
            r#"{"gpu_index":0,"offsets":[[-1,0]]}"#,         // negative index
            r#"{"gpu_index":0,"offsets":[[1,1.5]]}"#,        // float offset
            r#"{"gpu_index":0,"offsets":[[1,2147483648]]}"#, // offset > i32
            r#"{"gpu_index":0,"offsets":["x"]}"#,            // non-array item
        ] {
            let err = dispatch(&mock, "gpu_apply_vf_offsets", &request(bad)).unwrap_err();
            assert_eq!(err, ERR, "case: {bad}");
        }
        // Nothing reached the backend on validation failure.
        assert!(mock.recorded().is_empty());
    }

    #[test]
    fn empty_offsets_list_is_a_no_op_apply() {
        let mock = mock();
        let req = request(r#"{"gpu_index":0,"offsets":[]}"#);
        let result = dispatch(&mock, "gpu_apply_vf_offsets", &req).unwrap();
        assert_eq!(result, json!({"applied": 0}));
    }

    #[test]
    fn semantic_defaults_reset_reuses_profile_stock_pipeline() {
        let mut mock = readable_mock();
        mock.power_limits.power_limit_w = Some(360);
        mock.power_limits.enforced_power_limit_w = Some(360);
        mock.clock_offsets = crate::gpu::ClockOffsets {
            gpc_clk_vf_offset_mhz: Some(0),
            mem_clk_vf_offset_mhz: Some(0),
        };
        mock.vf_points[0].current_offset_khz = 0;

        let result = dispatch(&mock, "gpu_reset_defaults", &request(r#"{"gpu_index":1}"#)).unwrap();

        assert_eq!(result["reset"], true);
        assert_eq!(result["power_limit_set_supported"], true);
        assert_eq!(result["gpu_name"], "Mock RTX");
        assert_eq!(result["pci_device_id"], "0x123410DE");
        assert_eq!(result["power_limits"]["power_limit_default_w"], 360);
        assert_eq!(result["points"][0]["index"], 12);
        assert_eq!(
            mock.recorded(),
            vec![
                crate::gpu::mock::MockOp::ResetLockedCoreClocks,
                crate::gpu::mock::MockOp::ResetLockedMemoryClocks,
                crate::gpu::mock::MockOp::ApplyClockOffsets {
                    gpc_mhz: Some(0),
                    mem_mhz: Some(0),
                },
                crate::gpu::mock::MockOp::ApplyPowerLimit { power_limit_w: 360 },
                crate::gpu::mock::MockOp::ApplyVfOffsets {
                    offsets: vec![(12, 0)],
                },
            ]
        );
    }

    #[test]
    fn semantic_reset_distinguishes_fixed_mobile_power_from_unknown_support() {
        let mut mock = readable_mock();
        mock.clock_offsets = crate::gpu::ClockOffsets {
            gpc_clk_vf_offset_mhz: Some(0),
            mem_clk_vf_offset_mhz: Some(0),
        };
        mock.vf_points[0].current_offset_khz = 0;
        mock.inject_failure(
            "apply_power_limit_w",
            crate::gpu::GpuError::nvml_with_text(
                "nvmlDeviceSetPowerManagementLimit",
                3,
                "Not Supported",
            ),
        );
        let result = dispatch(&mock, "gpu_reset_defaults", &request(r#"{"gpu_index":1}"#)).unwrap();
        assert_eq!(result["power_limit_set_supported"], false);

        mock.power_limits.power_limit_default_w = None;
        let result = dispatch(&mock, "gpu_reset_defaults", &request(r#"{"gpu_index":1}"#)).unwrap();
        assert!(result["power_limit_set_supported"].is_null());
    }

    #[test]
    fn power_limit_applies_and_validates() {
        let mock = mock();
        let req = request(r#"{"gpu_index":0,"power_limit_w":300}"#);
        let result = dispatch(&mock, "gpu_apply_power_limit", &req).unwrap();
        assert_eq!(result, json!({"applied_w": 300}));

        for bad in [
            r#"{"gpu_index":0}"#,
            r#"{"gpu_index":0,"power_limit_w":"300"}"#,
            r#"{"gpu_index":0,"power_limit_w":300.5}"#,
            r#"{"gpu_index":0,"power_limit_w":true}"#,
            r#"{"gpu_index":0,"power_limit_w":null}"#,
        ] {
            let err = dispatch(&mock, "gpu_apply_power_limit", &request(bad)).unwrap_err();
            assert_eq!(err, "power_limit_w must be an integer", "case: {bad}");
        }
    }

    #[test]
    fn clock_offsets_mirror_requested_sides_only() {
        let mock = mock();
        let both = dispatch(
            &mock,
            "gpu_apply_clock_offsets",
            &request(r#"{"gpu_index":0,"gpc_clk_vf_offset_mhz":30,"mem_clk_vf_offset_mhz":1500}"#),
        )
        .unwrap();
        assert_eq!(
            both,
            json!({
                "gpc_clk_vf_offset_mhz": 30,
                "gpc_clk_vf_offset_readback_mhz": 30,
                "mem_clk_vf_offset_mhz": 1500,
                "mem_clk_vf_offset_readback_mhz": 1500,
            })
        );

        let gpc_only = dispatch(
            &mock,
            "gpu_apply_clock_offsets",
            &request(r#"{"gpu_index":0,"gpc_clk_vf_offset_mhz":0,"mem_clk_vf_offset_mhz":null}"#),
        )
        .unwrap();
        assert_eq!(
            gpc_only,
            json!({"gpc_clk_vf_offset_mhz": 0, "gpc_clk_vf_offset_readback_mhz": 0})
        );

        // No sides requested → empty dict (Python parity).
        let none = dispatch(
            &mock,
            "gpu_apply_clock_offsets",
            &request(r#"{"gpu_index":0}"#),
        )
        .unwrap();
        assert_eq!(none, json!({}));

        let err = dispatch(
            &mock,
            "gpu_apply_clock_offsets",
            &request(r#"{"gpu_index":0,"gpc_clk_vf_offset_mhz":"x"}"#),
        )
        .unwrap_err();
        assert_eq!(err, "gpc_clk_vf_offset_mhz must be an integer");
    }

    #[test]
    fn mem_readback_divergence_is_relayed() {
        let mut mock = mock();
        mock.mem_offset_readback = Some(0); // issue #20: write "succeeds", does not stick
        let result = dispatch(
            &mock,
            "gpu_apply_clock_offsets",
            &request(r#"{"gpu_index":0,"mem_clk_vf_offset_mhz":1500}"#),
        )
        .unwrap();
        assert_eq!(
            result,
            json!({"mem_clk_vf_offset_mhz": 1500, "mem_clk_vf_offset_readback_mhz": 0})
        );
    }

    #[test]
    fn locked_core_clock_returns_snap_dict_and_defaults() {
        let mock = mock();
        let snap = dispatch(
            &mock,
            "gpu_apply_locked_core_clock",
            &request(r#"{"gpu_index":0,"clock_mhz":1950}"#),
        )
        .unwrap();
        // Defaults: prefer_not_above=true, snap_to_supported=true → floor.
        assert_eq!(
            snap,
            json!({
                "requested_clock_mhz": 1950,
                "applied_clock_mhz": 1900,
                "mode": "floor",
                "supported_steps_mhz": [1800, 1900, 2000, 2100],
            })
        );

        let unsnapped = dispatch(
            &mock,
            "gpu_apply_locked_core_clock",
            &request(r#"{"gpu_index":0,"clock_mhz":1950,"snap_to_supported":false}"#),
        )
        .unwrap();
        assert_eq!(unsnapped["mode"], "unsnapped");
        assert_eq!(unsnapped["applied_clock_mhz"], 1950);

        let err = dispatch(
            &mock,
            "gpu_apply_locked_core_clock",
            &request(r#"{"gpu_index":0,"clock_mhz":1950,"prefer_not_above":1}"#),
        )
        .unwrap_err();
        assert_eq!(err, "prefer_not_above must be a boolean");
        let err = dispatch(
            &mock,
            "gpu_apply_locked_core_clock",
            &request(r#"{"gpu_index":0,"clock_mhz":"fast"}"#),
        )
        .unwrap_err();
        assert_eq!(err, "clock_mhz must be an integer");
    }

    #[test]
    fn locked_core_clock_range_returns_range_snap_dict() {
        let mock = mock();
        let range = dispatch(
            &mock,
            "gpu_apply_locked_core_clock_range",
            &request(
                r#"{"gpu_index":0,"min_mhz":1850,"max_mhz":2150,"prefer_max_not_above":true}"#,
            ),
        )
        .unwrap();
        assert_eq!(
            range,
            json!({
                "requested_min_clock_mhz": 1850,
                "requested_max_clock_mhz": 2150,
                "applied_min_clock_mhz": 1900,
                "applied_max_clock_mhz": 2100,
                "min_mode": "ceil",
                "max_mode": "floor",
                "supported_steps_mhz": [1800, 1900, 2000, 2100],
            })
        );

        let err = dispatch(
            &mock,
            "gpu_apply_locked_core_clock_range",
            &request(r#"{"gpu_index":0,"min_mhz":1850}"#),
        )
        .unwrap_err();
        assert_eq!(err, "max_mhz must be an integer");
    }

    #[test]
    fn resets_and_persistence() {
        let mock = mock();
        assert_eq!(
            dispatch(
                &mock,
                "gpu_reset_locked_core_clocks",
                &request(r#"{"gpu_index":0}"#)
            )
            .unwrap(),
            json!({"reset": true})
        );
        assert_eq!(
            dispatch(
                &mock,
                "gpu_reset_locked_memory_clocks",
                &request(r#"{"gpu_index":0}"#)
            )
            .unwrap(),
            json!({"reset": true})
        );
        assert_eq!(
            dispatch(
                &mock,
                "gpu_enable_persistence_mode",
                &request(r#"{"gpu_index":0}"#)
            )
            .unwrap(),
            json!({"enabled": true})
        );
        assert_eq!(
            mock.recorded(),
            vec![
                crate::gpu::mock::MockOp::ResetLockedCoreClocks,
                crate::gpu::mock::MockOp::ResetLockedMemoryClocks,
                crate::gpu::mock::MockOp::EnablePersistence,
            ]
        );
    }

    #[test]
    fn fan_reset_uses_detected_fan_count() {
        let mut mock = mock();
        mock.fan_count = 2;
        let result = dispatch(&mock, "gpu_reset_fans", json!({}).as_object().unwrap()).unwrap();

        assert_eq!(result, json!({ "reset": true, "fan_count": 2 }));
        assert_eq!(
            mock.recorded(),
            vec![crate::gpu::mock::MockOp::SetAllFansDefault { fan_count: 2 }]
        );
    }

    #[test]
    fn backend_error_text_is_relayed_verbatim() {
        let mock = mock();
        mock.inject_failure(
            "apply_power_limit_w",
            GpuError::nvml_with_text(
                "nvmlDeviceSetPowerManagementLimit",
                4,
                "Insufficient Permissions",
            ),
        );
        let err = dispatch(
            &mock,
            "gpu_apply_power_limit",
            &request(r#"{"gpu_index":0,"power_limit_w":300}"#),
        )
        .unwrap_err();
        // The exact text `_looks_like_permission_error` matches ("nvml error 4").
        assert_eq!(
            err,
            "nvmlDeviceSetPowerManagementLimit failed with NVML error 4: Insufficient Permissions"
        );

        mock.inject_failure(
            "apply_vf_offsets_khz",
            GpuError::nvapi(
                "ClockClientClkVfPointsSetControl",
                -104,
                "invalid_user_privilege",
            ),
        );
        let err = dispatch(
            &mock,
            "gpu_apply_vf_offsets",
            &request(r#"{"gpu_index":0,"offsets":[[1,0]]}"#),
        )
        .unwrap_err();
        assert_eq!(
            err,
            "ClockClientClkVfPointsSetControl failed with status -104: invalid_user_privilege"
        );
    }

    #[test]
    fn gpu_index_validation() {
        for bad in [
            r#"{"power_limit_w":300}"#,
            r#"{"gpu_index":-1,"power_limit_w":300}"#,
            r#"{"gpu_index":0.5,"power_limit_w":300}"#,
            r#"{"gpu_index":"0","power_limit_w":300}"#,
            r#"{"gpu_index":4294967296,"power_limit_w":300}"#,
        ] {
            let err = require_u32(&request(bad), "gpu_index").unwrap_err();
            assert_eq!(err, "gpu_index must be an integer", "case: {bad}");
        }
        assert_eq!(request(r#"{"gpu_index":1}"#).len(), 1);
        assert_eq!(
            require_u32(&request(r#"{"gpu_index":1}"#), "gpu_index"),
            Ok(1)
        );
    }
}
