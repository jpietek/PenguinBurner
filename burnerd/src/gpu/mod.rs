//! GPU backend: a narrow hardware-facts-and-operations trait plus its real
//! (NVML + hidden NVAPI) and in-memory (test) implementations.
//!
//! The trait exposes only device facts and privileged operations — never
//! product policy (tier choice, guard cooldowns, the reset-twice quirk, …),
//! which lives in the engine. Method shapes map ~1:1 onto the Python drivers in
//! `drivers/nvidia/*.py` and `runtime/gpu_control/*.py`; unit conventions and
//! error-message text are preserved verbatim (see `port-notes/03-nvml-ffi.md`).

mod backend;
pub(crate) mod context;
mod nvapi;

// Compiled into the binary (not test-gated) because it also backs the
// `PENGUIN_BURNERD_TEST_MOCK_GPU` RPC seam in `gpu_rpc` — production code never
// constructs it outside that explicitly test-only env knob.
pub mod mock;

pub use backend::NvmlBackend;

use std::fmt;

use serde::Serialize;

pub const NVML_ERROR_NOT_SUPPORTED: i64 = 3;

// The A2 backend is a stable 39-method contract; the wave-A3 engine consumes
// most of it. The remainder tagged below (device identity/memory/throttle reads,
// single-fan writes, the exact-lock path, driver/uuid strings, VfSummary) is
// retained for milestone-B write RPCs and telemetry completeness — not dead by
// accident. Narrowly allowed per-item so real future dead code still surfaces.

/// NVML clock domain (matches `nvmlClockType_t`).
#[allow(dead_code)] // Sm/Video domains unused by A3.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClockType {
    Graphics = 0,
    Sm = 1,
    Memory = 2,
    Video = 3,
}

/// Public NVML identity of the bound device (`nvml_identity.py`).
///
/// String fields degrade to `""` on read failure, exactly like the Python
/// session — they never fail the whole identity read.
#[allow(dead_code)] // milestone-B identity read
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct GpuIdentity {
    pub index: u32,
    pub name: String,
    pub driver_version: String,
    pub pci_bus_id: String,
    pub pci_device_id: String,
    pub uuid: String,
}

/// `nvmlMemory_t` view (bytes).
#[allow(dead_code)] // milestone-B memory read
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct GpuMemoryInfo {
    pub index: u32,
    pub total_bytes: u64,
    pub free_bytes: u64,
    pub used_bytes: u64,
}

/// Power-limit facts in **integer watts** (rounded), matching the policy
/// controller's `query_power_limits` (§8). Each field is `None` when the
/// corresponding getter is missing or fails.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct PowerLimits {
    pub power_management_enabled: Option<bool>,
    pub power_limit_w: Option<i64>,
    pub enforced_power_limit_w: Option<i64>,
    pub power_limit_default_w: Option<i64>,
    pub power_limit_min_w: Option<i64>,
    pub power_limit_max_w: Option<i64>,
}

/// NVML VF clock offsets in **signed MHz** (`get_clock_offsets`, §8).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct ClockOffsets {
    pub gpc_clk_vf_offset_mhz: Option<i32>,
    pub mem_clk_vf_offset_mhz: Option<i32>,
}

/// Result of `apply_clock_offsets`: only the sides that were requested carry a
/// value; each set records the mandatory read-back (issue #20 for memory).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct AppliedOffsets {
    pub gpc_clk_vf_offset_mhz: Option<i32>,
    pub gpc_clk_vf_offset_readback_mhz: Option<i32>,
    pub mem_clk_vf_offset_mhz: Option<i32>,
    pub mem_clk_vf_offset_readback_mhz: Option<i32>,
}

/// A single locked-clock snap decision (`snap_core_clock_mhz`, §8). Field names
/// are the `gpu_apply_locked_core_clock` RPC's wire keys (serialized directly).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SnapResult {
    pub requested_clock_mhz: i64,
    pub applied_clock_mhz: i64,
    pub mode: String,
    pub supported_steps_mhz: Vec<u32>,
}

/// A locked-clock **range** snap decision (`apply_locked_core_clock_range_mhz`).
/// Field names are the `gpu_apply_locked_core_clock_range` RPC's wire keys.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RangeSnapResult {
    pub requested_min_clock_mhz: i64,
    pub requested_max_clock_mhz: i64,
    pub applied_min_clock_mhz: i64,
    pub applied_max_clock_mhz: i64,
    pub min_mode: String,
    pub max_mode: String,
    pub supported_steps_mhz: Vec<u32>,
}

/// One hidden-NVAPI VF-curve point (`hidden_nvapi_vf.py::_read_points`).
///
/// Frequencies are **kHz**, voltages **µV**, `current_offset_khz` a signed kHz
/// delta — the hidden-NVAPI unit convention, distinct from NVML's signed-MHz.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct VfPoint {
    pub index: u32,
    pub type_: u32,
    pub voltage_based: u32,
    pub freq_khz: i64,
    pub voltage_uv: i64,
    pub base_freq_khz: i64,
    pub base_voltage_uv: i64,
    pub current_offset_khz: i64,
}

/// `summary()` of the VF curve.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct VfSummary {
    pub active_points: usize,
    pub editable_core_points: usize,
}

/// A typed backend error carrying the native return code and a pre-formatted,
/// Python-shaped message.
///
/// The Python distinctions are preserved by *which* constructor built the
/// message: the policy write-path appends `nvmlErrorString` text
/// (`nvml_with_text`), while the runtime-session read/fan path embeds only the
/// numeric code (`nvml`). Best-effort reads never build a `GpuError` at all —
/// they return `Option`/empty. `rc` is `0` for non-NVML failures (missing
/// symbol, NULL query interface).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GpuError {
    rc: i64,
    message: String,
}

impl GpuError {
    /// The native return code (NVML `nvmlReturn_t` / NVAPI `NvAPI_Status`), or
    /// `0` when the failure was not a native call (e.g. a missing symbol).
    pub fn rc(&self) -> i64 {
        self.rc
    }

    /// Runtime-session / typed-`NvmlError` shape: `"{name} failed with NVML
    /// error {rc}"` (no `nvmlErrorString`). Used by temperature/fan/clock reads
    /// and fan writes.
    pub(crate) fn nvml(name: &str, rc: i64) -> Self {
        Self {
            rc,
            message: format!("{name} failed with NVML error {rc}"),
        }
    }

    /// Policy-controller shape: `"{name} failed with NVML error {rc}: {text}"`
    /// where `text` is `nvmlErrorString(rc)`. Used by every privileged write.
    pub(crate) fn nvml_with_text(name: &str, rc: i64, text: &str) -> Self {
        Self {
            rc,
            message: format!("{name} failed with NVML error {rc}: {text}"),
        }
    }

    /// Missing optional setter/getter: `"{symbol} is not available on this
    /// system"`.
    pub(crate) fn unavailable(symbol: &str) -> Self {
        Self {
            rc: 0,
            message: format!("{symbol} is not available on this system"),
        }
    }

    /// Hidden-NVAPI call failure shape: `"{context} failed with status {rc}:
    /// {text}"` where `text` is `NvAPI_GetErrorMessage(rc)`.
    pub(crate) fn nvapi(context: &str, rc: i64, text: &str) -> Self {
        Self {
            rc,
            message: format!("{context} failed with status {rc}: {text}"),
        }
    }

    /// Free-form message (device-count queries, NULL query interface, index out
    /// of range).
    pub(crate) fn other(message: impl Into<String>, rc: i64) -> Self {
        Self {
            rc,
            message: message.into(),
        }
    }
}

impl fmt::Display for GpuError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for GpuError {}

/// Convenience alias for backend results.
pub type GpuResult<T> = Result<T, GpuError>;

/// PIDs holding a live GPU context, split by context kind. Each list is
/// sorted and deduplicated; a process with both context kinds appears in
/// both lists.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct GpuContextPids {
    pub graphics: Vec<u32>,
    pub compute: Vec<u32>,
}

/// The narrow GPU backend contract: hardware facts and privileged operations.
///
/// Every fallible operation returns [`GpuError`]; best-effort reads that the
/// Python code lets degrade return `Option`/empty instead of erroring. The
/// engine chooses handling (log-and-continue vs abort vs the double-reset
/// quirk) — none of that policy lives here.
#[allow(dead_code)] // identity/memory/throttle reads, single-fan writes, exact-lock: milestone-B
pub trait GpuBackend {
    // --- device identity / info -------------------------------------------
    fn gpu_index(&self) -> u32;
    fn gpu_count(&self) -> GpuResult<u32>;
    fn identity(&self) -> GpuIdentity;
    fn gpu_name(&self) -> Option<String>;
    fn memory_info(&self) -> Option<GpuMemoryInfo>;
    fn architecture(&self) -> Option<u32>;

    // --- telemetry reads (best-effort → Option, unless the Python raises) --
    fn temperature_c(&self) -> GpuResult<f64>;
    fn hotspot_temperature_c(&self) -> Option<f64>;
    fn fan_count(&self) -> GpuResult<u32>;
    fn fan_speed_limits(&self) -> (Option<u32>, Option<u32>);
    fn reported_fan_speeds(&self, fan_count: u32) -> Option<Vec<u32>>;
    fn power_draw_w(&self) -> Option<f64>;
    fn gpu_utilization_pct(&self) -> Option<u32>;
    fn clock_info_mhz(&self, clock_type: ClockType) -> Option<u32>;
    fn throttle_reason_mask(&self) -> Option<u64>;
    /// PIDs holding a live graphics or compute context on this GPU, split by
    /// kind. The accurate "GPU in use" signal on fine-grained RTD3 laptops,
    /// where an idly open `/dev/nvidia<N>` fd does not keep the GPU awake;
    /// the split feeds the `deep_sleep.gpu_clients` debug stats.
    fn gpu_context_pids(&self) -> GpuResult<GpuContextPids>;

    // --- power / persistence ----------------------------------------------
    fn query_power_limits(&self) -> PowerLimits;
    fn apply_power_limit_w(&self, power_limit_w: i64) -> GpuResult<i64>;
    fn enable_persistence_mode(&self) -> GpuResult<()>;

    // --- supported clocks + locked-clock control --------------------------
    fn supported_memory_clock_steps_mhz(&self) -> Vec<u32>;
    fn supported_core_clock_steps_mhz(&self) -> Vec<u32>;
    fn apply_locked_core_clock_mhz(
        &self,
        clock_mhz: i64,
        prefer_not_above: bool,
        snap_to_supported: bool,
    ) -> GpuResult<SnapResult>;
    fn apply_locked_core_clock_range_mhz(
        &self,
        min_clock_mhz: i64,
        max_clock_mhz: i64,
        prefer_max_not_above: bool,
        snap_to_supported: bool,
    ) -> GpuResult<RangeSnapResult>;
    fn reset_locked_core_clocks(&self) -> GpuResult<()>;
    fn reset_locked_memory_clocks(&self) -> GpuResult<()>;

    // --- NVML VF clock offsets (signed MHz) -------------------------------
    fn clock_offsets(&self) -> ClockOffsets;
    fn memory_clock_offset_range_mhz(&self) -> Option<(i32, i32)>;
    fn apply_clock_offsets(
        &self,
        gpc_clk_vf_offset_mhz: Option<i32>,
        mem_clk_vf_offset_mhz: Option<i32>,
    ) -> GpuResult<AppliedOffsets>;

    // --- fan writes -------------------------------------------------------
    fn set_fan_speed(&self, fan_idx: u32, speed_pct: u32) -> GpuResult<()>;
    fn set_all_fans_speed(&self, fan_count: u32, speed_pct: u32) -> GpuResult<()>;
    fn set_default_fan_speed(&self, fan_idx: u32) -> GpuResult<()>;
    fn set_all_fans_default(&self, fan_count: u32) -> GpuResult<()>;

    // --- hidden NVAPI: core voltage ---------------------------------------
    /// Current core voltage in µV, clamped to the validity window
    /// `[300_000, 1_500_000]`; `None` if unavailable or implausible.
    fn read_voltage_uv(&self) -> Option<i64>;

    // --- hidden NVAPI: VF curve -------------------------------------------
    fn vf_curve_available(&self) -> bool;
    fn vf_points(&self) -> Vec<VfPoint>;
    /// Re-read the whole VF curve into the backend's cache; consumers that need
    /// the fresh points read them back with [`GpuBackend::vf_points`].
    fn refresh_vf_points(&self) -> GpuResult<()>;
    fn editable_core_vf_points(&self) -> Vec<VfPoint>;
    fn find_nearest_vf_point(&self, core_clock_mhz: i64, voltage_uv: i64) -> Option<VfPoint>;
    fn vf_summary(&self) -> VfSummary;
    /// Per-point frequency-offset write (the `apply_plan` path): reads the live
    /// control struct, sets `freq_offset_khz` for each `(index, offset_khz)`,
    /// and issues a single `SetControl`. Because a mem-offset-style write can
    /// silently not stick, callers verify with [`GpuBackend::refresh_vf_points`].
    fn apply_vf_offsets_khz(&self, offsets: &[(u32, i32)]) -> GpuResult<()>;
}

// ------------------------------------------------------------------------
// Pure helpers (unit conversions, snap math, VF derivations) — shared by the
// real backend and the mock, and unit-tested here without a GPU.
// ------------------------------------------------------------------------

/// Decode a fixed-size C buffer: bytes up to the first NUL, lossy UTF-8, trimmed.
/// Shared by the NVML (`backend`) and NVAPI (`nvapi`) string/struct readers.
pub(crate) fn decode_cstr(buf: &[u8]) -> String {
    let end = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
    String::from_utf8_lossy(&buf[..end]).trim().to_string()
}

/// Round half-to-even (banker's rounding), matching Python's `round()`.
pub(crate) fn round_half_even(x: f64) -> f64 {
    x.round_ties_even()
}

/// milliwatts → **integer** watts, rounded (policy controller convention, §8).
pub(crate) fn mw_to_w_round(mw: u32) -> i64 {
    round_half_even(f64::from(mw) / 1000.0) as i64
}

/// milliwatts → **float** watts (telemetry convention, §7/§10).
pub(crate) fn mw_to_w_float(mw: u32) -> f64 {
    f64::from(mw) / 1000.0
}

/// watts → milliwatts, rounded (the `apply_power_limit_w` target, §8).
pub(crate) fn w_to_mw_round(w: f64) -> i64 {
    round_half_even(w * 1000.0) as i64
}

/// The core-voltage validity window (`read_microvolts`).
pub(crate) fn voltage_uv_in_window(uv: i64) -> bool {
    (300_000..=1_500_000).contains(&uv)
}

/// Snap a single requested core clock to the supported step list (§8).
pub(crate) fn snap_core_clock_mhz(
    target_clock_mhz: i64,
    supported_steps: &[u32],
    prefer_not_above: bool,
) -> SnapResult {
    if supported_steps.is_empty() {
        return SnapResult {
            requested_clock_mhz: target_clock_mhz,
            applied_clock_mhz: target_clock_mhz,
            mode: "unsupported-list-unavailable".to_string(),
            supported_steps_mhz: Vec::new(),
        };
    }

    let requested = target_clock_mhz;
    let (applied, mode) = if supported_steps.iter().any(|&s| i64::from(s) == requested) {
        (requested, "exact")
    } else {
        let lower_max = supported_steps
            .iter()
            .map(|&s| i64::from(s))
            .filter(|&s| s <= requested)
            .max();
        let upper_min = supported_steps
            .iter()
            .map(|&s| i64::from(s))
            .filter(|&s| s >= requested)
            .min();
        // Exact Python branch order: (prefer_not_above AND a lower step) → floor;
        // else an upper step → ceil; else a lower step → floor; else nearest.
        // (`nearest` is unreachable for a non-empty list — every step is on one
        // side of `requested` — but kept for parity.)
        if let (true, Some(l)) = (prefer_not_above, lower_max) {
            (l, "floor")
        } else if let Some(u) = upper_min {
            (u, "ceil")
        } else if let Some(l) = lower_max {
            (l, "floor")
        } else {
            let nearest = supported_steps
                .iter()
                .map(|&s| i64::from(s))
                .min_by_key(|&s| (s - requested).abs())
                .unwrap_or(requested);
            (nearest, "nearest")
        }
    };

    SnapResult {
        requested_clock_mhz: requested,
        applied_clock_mhz: applied,
        mode: mode.to_string(),
        supported_steps_mhz: supported_steps.to_vec(),
    }
}

/// Snap a locked-clock **range** (min snaps ceil-preference, max snaps
/// floor-preference, then clamp min ≤ max), §8.
pub(crate) fn snap_core_clock_range(
    min_clock_mhz: i64,
    max_clock_mhz: i64,
    supported_steps: &[u32],
    prefer_max_not_above: bool,
) -> RangeSnapResult {
    let min_snap = snap_core_clock_mhz(min_clock_mhz, supported_steps, false);
    let max_snap = snap_core_clock_mhz(max_clock_mhz, supported_steps, prefer_max_not_above);

    let mut applied_min = min_snap.applied_clock_mhz;
    let applied_max = max_snap.applied_clock_mhz;
    let mut min_mode = min_snap.mode;
    let max_mode = max_snap.mode;
    let supported = if max_snap.supported_steps_mhz.is_empty() {
        min_snap.supported_steps_mhz
    } else {
        max_snap.supported_steps_mhz
    };

    if applied_min > applied_max {
        applied_min = applied_max;
        min_mode = format!("{min_mode}-clamped-to-max");
    }

    RangeSnapResult {
        requested_min_clock_mhz: min_clock_mhz,
        requested_max_clock_mhz: max_clock_mhz,
        applied_min_clock_mhz: applied_min,
        applied_max_clock_mhz: applied_max,
        min_mode,
        max_mode,
        supported_steps_mhz: supported,
    }
}

/// Editable core VF points: `type == 0 && voltage_based == 1`.
pub(crate) fn vf_editable_core_points(points: &[VfPoint]) -> Vec<VfPoint> {
    points
        .iter()
        .copied()
        .filter(|p| p.type_ == 0 && p.voltage_based == 1)
        .collect()
}

/// Nearest editable point by L1 distance in mixed kHz+µV units (no
/// normalization — matches `find_nearest_point`).
pub(crate) fn vf_find_nearest(
    points: &[VfPoint],
    core_clock_mhz: i64,
    voltage_uv: i64,
) -> Option<VfPoint> {
    let target_freq_khz = core_clock_mhz * 1000;
    vf_editable_core_points(points)
        .into_iter()
        .min_by_key(|p| (p.freq_khz - target_freq_khz).abs() + (p.voltage_uv - voltage_uv).abs())
}

/// VF curve `summary()`.
pub(crate) fn vf_summary_of(points: &[VfPoint]) -> VfSummary {
    VfSummary {
        active_points: points.len(),
        editable_core_points: vf_editable_core_points(points).len(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn banker_rounding_matches_python() {
        assert_eq!(round_half_even(210.5) as i64, 210);
        assert_eq!(round_half_even(211.5) as i64, 212);
        assert_eq!(round_half_even(2.5) as i64, 2);
        assert_eq!(round_half_even(3.5) as i64, 4);
        assert_eq!(round_half_even(-0.5) as i64, 0);
        assert_eq!(round_half_even(-1.5) as i64, -2);
        assert_eq!(round_half_even(210.734) as i64, 211);
        assert_eq!(round_half_even(210.4) as i64, 210);
    }

    #[test]
    fn power_unit_conversions() {
        // Float watts (telemetry) vs rounded int watts (policy).
        assert_eq!(mw_to_w_float(210_500), 210.5);
        assert_eq!(mw_to_w_round(210_500), 210); // banker's: 210.5 -> even 210
        assert_eq!(mw_to_w_round(211_500), 212);
        assert_eq!(mw_to_w_round(210_734), 211);
        assert_eq!(w_to_mw_round(300.0), 300_000);
        assert_eq!(w_to_mw_round(0.5), 500); // 0.5 W = 500 mW exactly
        assert_eq!(w_to_mw_round(210.5), 210_500);
    }

    #[test]
    fn voltage_window() {
        assert!(!voltage_uv_in_window(299_999));
        assert!(voltage_uv_in_window(300_000));
        assert!(voltage_uv_in_window(805_000));
        assert!(voltage_uv_in_window(1_500_000));
        assert!(!voltage_uv_in_window(1_500_001));
    }

    #[test]
    fn snap_exact_and_floor_and_ceil() {
        let steps = [1800u32, 1900, 2000, 2100, 2200];

        let exact = snap_core_clock_mhz(2000, &steps, true);
        assert_eq!(exact.applied_clock_mhz, 2000);
        assert_eq!(exact.mode, "exact");

        // prefer_not_above with a lower step available -> floor.
        let floor = snap_core_clock_mhz(1950, &steps, true);
        assert_eq!(floor.applied_clock_mhz, 1900);
        assert_eq!(floor.mode, "floor");

        // prefer_not_above=false with both sides -> ceil.
        let ceil = snap_core_clock_mhz(1950, &steps, false);
        assert_eq!(ceil.applied_clock_mhz, 2000);
        assert_eq!(ceil.mode, "ceil");

        // Above the top with prefer_not_above -> floor to max step.
        let above = snap_core_clock_mhz(2500, &steps, true);
        assert_eq!(above.applied_clock_mhz, 2200);
        assert_eq!(above.mode, "floor");

        // Below the bottom with prefer_not_above -> no lower, ceil to min.
        let below = snap_core_clock_mhz(1500, &steps, true);
        assert_eq!(below.applied_clock_mhz, 1800);
        assert_eq!(below.mode, "ceil");
    }

    #[test]
    fn snap_empty_list_passes_through() {
        let snap = snap_core_clock_mhz(2345, &[], true);
        assert_eq!(snap.applied_clock_mhz, 2345);
        assert_eq!(snap.mode, "unsupported-list-unavailable");
        assert!(snap.supported_steps_mhz.is_empty());
    }

    #[test]
    fn range_snap_min_ceil_max_floor() {
        let steps = [1800u32, 1900, 2000, 2100, 2200];
        // min snaps up (ceil-preference), max snaps down (floor-preference).
        let r = snap_core_clock_range(1850, 2150, &steps, true);
        assert_eq!(r.applied_min_clock_mhz, 1900);
        assert_eq!(r.min_mode, "ceil");
        assert_eq!(r.applied_max_clock_mhz, 2100);
        assert_eq!(r.max_mode, "floor");
    }

    #[test]
    fn range_snap_clamps_min_to_max() {
        let steps = [1800u32, 1900, 2000, 2100, 2200];
        // Requested min above requested max: min gets clamped down to max.
        let r = snap_core_clock_range(2200, 1900, &steps, true);
        assert_eq!(r.applied_max_clock_mhz, 1900);
        assert_eq!(r.applied_min_clock_mhz, 1900);
        assert!(r.min_mode.ends_with("-clamped-to-max"));
    }

    #[test]
    fn error_display_shapes_match_python() {
        assert_eq!(
            GpuError::nvml("nvmlDeviceGetTemperature", 15).to_string(),
            "nvmlDeviceGetTemperature failed with NVML error 15"
        );
        assert_eq!(
            GpuError::nvml_with_text("nvmlDeviceSetPowerManagementLimit", 3, "Not Supported")
                .to_string(),
            "nvmlDeviceSetPowerManagementLimit failed with NVML error 3: Not Supported"
        );
        assert_eq!(
            GpuError::unavailable("nvmlDeviceSetGpuLockedClocks").to_string(),
            "nvmlDeviceSetGpuLockedClocks is not available on this system"
        );
        assert_eq!(
            GpuError::nvapi("ClockClientClkVfPointsGetInfo", 5, "some text").to_string(),
            "ClockClientClkVfPointsGetInfo failed with status 5: some text"
        );
        assert_eq!(
            GpuError::nvapi("NvAPI voltage query", 1, "Error").to_string(),
            "NvAPI voltage query failed with status 1: Error"
        );
        // rc is preserved for callers that switch on it.
        assert_eq!(GpuError::nvml("x", 42).rc(), 42);
        assert_eq!(GpuError::unavailable("x").rc(), 0);
    }

    #[test]
    fn vf_derivations() {
        let points = vec![
            VfPoint {
                index: 0,
                type_: 0,
                voltage_based: 1,
                freq_khz: 1_800_000,
                voltage_uv: 800_000,
                base_freq_khz: 1_800_000,
                base_voltage_uv: 800_000,
                current_offset_khz: 0,
            },
            VfPoint {
                index: 1,
                type_: 0,
                voltage_based: 1,
                freq_khz: 2_600_000,
                voltage_uv: 900_000,
                base_freq_khz: 2_450_000,
                base_voltage_uv: 900_000,
                current_offset_khz: 150_000,
            },
            // Non-editable (type != 0) — excluded from editable set.
            VfPoint {
                index: 2,
                type_: 1,
                voltage_based: 1,
                freq_khz: 3_000_000,
                voltage_uv: 950_000,
                ..VfPoint::default()
            },
        ];
        assert_eq!(vf_editable_core_points(&points).len(), 2);
        let summary = vf_summary_of(&points);
        assert_eq!(summary.active_points, 3);
        assert_eq!(summary.editable_core_points, 2);
        // Nearest to 2600 MHz @ 900 mV is index 1.
        let nearest = vf_find_nearest(&points, 2600, 900_000).unwrap();
        assert_eq!(nearest.index, 1);
    }
}
