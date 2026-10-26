//! `MockGpu`: a dumb in-memory [`GpuBackend`] for the wave-A3 engine tests and
//! the `PENGUIN_BURNERD_TEST_MOCK_GPU` RPC seam (`gpu_rpc`).
//!
//! No mocking framework — just settable telemetry (plain public fields), a
//! `Vec` of recorded write operations, and a per-method failure-injection map.
//! Snap/VF derivations reuse the same pure helpers as the real backend so the
//! engine sees identical shapes.

use std::cell::RefCell;
use std::collections::HashMap;
use std::path::PathBuf;

use super::{
    snap_core_clock_mhz, snap_core_clock_range, vf_editable_core_points, vf_find_nearest,
    vf_summary_of, AppliedOffsets, ClockOffsets, ClockType, GpuBackend, GpuContextPids, GpuError,
    GpuIdentity, GpuMemoryInfo, GpuResult, PowerLimits, RangeSnapResult, SnapResult, VfPoint,
    VfSummary,
};

/// One recorded privileged operation, in call order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MockOp {
    SetFanSpeed {
        fan_idx: u32,
        speed_pct: u32,
    },
    SetAllFansSpeed {
        fan_count: u32,
        speed_pct: u32,
    },
    SetDefaultFanSpeed {
        fan_idx: u32,
    },
    SetAllFansDefault {
        fan_count: u32,
    },
    ApplyPowerLimit {
        power_limit_w: i64,
    },
    EnablePersistence,
    ApplyLockedCoreClock {
        clock_mhz: i64,
        prefer_not_above: bool,
        snap: bool,
    },
    ApplyLockedCoreClockRange {
        min_mhz: i64,
        max_mhz: i64,
        prefer_max_not_above: bool,
        snap: bool,
    },
    ResetLockedCoreClocks,
    ResetLockedMemoryClocks,
    ApplyClockOffsets {
        gpc_mhz: Option<i32>,
        mem_mhz: Option<i32>,
    },
    ApplyVfOffsets {
        offsets: Vec<(u32, i32)>,
    },
}

/// In-memory backend. Telemetry fields are public and settable; writes append
/// to `ops`; `failures[method]` forces that method to return the given error.
pub struct MockGpu {
    pub gpu_index: u32,
    pub gpu_count: u32,
    pub identity: GpuIdentity,
    pub gpu_name: Option<String>,
    pub memory_info: Option<GpuMemoryInfo>,
    pub architecture: Option<u32>,

    pub temperature_c: f64,
    pub hotspot_temperature_c: Option<f64>,
    pub fan_count: u32,
    pub fan_limits: (Option<u32>, Option<u32>),
    pub reported_fan_speeds: Option<Vec<u32>>,
    pub power_draw_w: Option<f64>,
    pub utilization_pct: Option<u32>,
    pub graphics_clock_mhz: Option<u32>,
    pub sm_clock_mhz: Option<u32>,
    pub memory_clock_mhz: Option<u32>,
    pub video_clock_mhz: Option<u32>,
    pub throttle_mask: Option<u64>,

    pub power_limits: PowerLimits,
    pub supported_memory_clocks: Vec<u32>,
    pub supported_core_clocks: Vec<u32>,
    pub clock_offsets: ClockOffsets,
    pub mem_offset_range: Option<(i32, i32)>,
    /// Read-back returned by `apply_clock_offsets` (default: echoes the request).
    pub mem_offset_readback: Option<i32>,

    pub voltage_uv: Option<i64>,
    pub vf_available: bool,
    pub vf_points: Vec<VfPoint>,

    /// Canned `gpu_context_pids` answer.
    pub context_pids: GpuContextPids,
    /// Integration-test seam: when set, `gpu_context_pids` re-reads this file
    /// on every call (missing file → no contexts), so a test can stage and
    /// remove GPU clients while the daemon runs. Overrides `context_pids`.
    /// Format: whitespace-separated tokens where `graphics` / `compute`
    /// select the kind for subsequent PIDs (default `graphics`).
    pub context_pids_file: Option<PathBuf>,
    /// Integration-test seam: a three-field `opens closes live` file updated
    /// when a mock NVIDIA session is acquired and dropped. Production never
    /// sets it.
    lifetime_file: Option<PathBuf>,
    /// Management power limit applied through the trait; overrides only
    /// `power_limit_w` in the `query_power_limits` readback so
    /// verify-after-apply behaves like real hardware while the enforced
    /// limit stays independently settable (it can legitimately diverge —
    /// thermal/idle caps). Tests simulating a driver-side reset (e.g.
    /// suspend loss) call `clear_applied_power_limit` so later
    /// `power_limits` writes win.
    applied_power_limit_w: RefCell<Option<i64>>,

    ops: RefCell<Vec<MockOp>>,
    failures: RefCell<HashMap<&'static str, GpuError>>,
    /// Test hook run on every `temperature_c` read — lets engine-loop tests
    /// simulate an event (e.g. a stop request) landing while a blocking backend
    /// call is in flight. `Send` so `MockGpu` stays movable across threads.
    pub on_temperature: RefCell<Option<Box<dyn Fn() + Send>>>,
    /// Same, for the `editable_core_vf_points` read (the blocking NVML call at
    /// the head of the VF-reapply guard).
    pub on_editable_vf_points: RefCell<Option<Box<dyn Fn() + Send>>>,
}

impl Default for MockGpu {
    fn default() -> Self {
        Self {
            gpu_index: 0,
            gpu_count: 1,
            identity: GpuIdentity::default(),
            gpu_name: None,
            memory_info: None,
            architecture: None,
            temperature_c: 0.0,
            hotspot_temperature_c: None,
            fan_count: 0,
            fan_limits: (None, None),
            reported_fan_speeds: None,
            power_draw_w: None,
            utilization_pct: None,
            graphics_clock_mhz: None,
            sm_clock_mhz: None,
            memory_clock_mhz: None,
            video_clock_mhz: None,
            throttle_mask: None,
            power_limits: PowerLimits::default(),
            supported_memory_clocks: Vec::new(),
            supported_core_clocks: Vec::new(),
            clock_offsets: ClockOffsets::default(),
            mem_offset_range: None,
            mem_offset_readback: None,
            voltage_uv: None,
            vf_available: false,
            vf_points: Vec::new(),
            context_pids: GpuContextPids::default(),
            context_pids_file: None,
            lifetime_file: None,
            applied_power_limit_w: RefCell::new(None),
            ops: RefCell::new(Vec::new()),
            failures: RefCell::new(HashMap::new()),
            on_temperature: RefCell::new(None),
            on_editable_vf_points: RefCell::new(None),
        }
    }
}

impl MockGpu {
    pub fn new() -> Self {
        Self::default()
    }

    /// Force `method` (the trait method name, e.g. `"apply_power_limit_w"`) to
    /// return `err`.
    pub fn inject_failure(&self, method: &'static str, err: GpuError) {
        self.failures.borrow_mut().insert(method, err);
    }

    /// Clear an injected failure.
    #[allow(dead_code)] // used by unit tests only (the module is not test-gated)
    pub fn clear_failure(&self, method: &'static str) {
        self.failures.borrow_mut().remove(method);
    }

    /// Forget the trait-applied power limit so `query_power_limits` reports
    /// the `power_limits` field again — simulates the driver losing the
    /// limit (suspend/reset) after an apply.
    #[allow(dead_code)] // used by unit tests only (the module is not test-gated)
    pub fn clear_applied_power_limit(&self) {
        *self.applied_power_limit_w.borrow_mut() = None;
    }

    /// A snapshot of the recorded operations.
    pub fn recorded(&self) -> Vec<MockOp> {
        self.ops.borrow().clone()
    }

    /// Begin tracking this mock instance as one live NVIDIA session.
    pub(crate) fn track_lifetime(&mut self, path: PathBuf) {
        let (opens, closes, live) = read_lifetime_state(&path);
        write_lifetime_state(&path, opens + 1, closes, live + 1);
        self.lifetime_file = Some(path);
    }

    fn record(&self, op: MockOp) {
        self.ops.borrow_mut().push(op);
    }

    fn fail(&self, method: &'static str) -> GpuResult<()> {
        match self.failures.borrow().get(method) {
            Some(err) => Err(err.clone()),
            None => Ok(()),
        }
    }
}

impl Drop for MockGpu {
    fn drop(&mut self) {
        let Some(path) = &self.lifetime_file else {
            return;
        };
        let (opens, closes, live) = read_lifetime_state(path);
        write_lifetime_state(path, opens, closes + 1, live.saturating_sub(1));
    }
}

fn write_lifetime_state(path: &std::path::Path, opens: u64, closes: u64, live: u64) {
    let temporary = path.with_extension("tmp");
    if std::fs::write(&temporary, format!("{opens} {closes} {live}\n")).is_ok() {
        let _ = std::fs::rename(temporary, path);
    }
}

fn read_lifetime_state(path: &std::path::Path) -> (u64, u64, u64) {
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

#[cfg(test)]
impl MockGpu {
    /// Shared test fixture: sane constraints around explicit management and
    /// enforced limits (kept in one place so the resume/apply tests cannot
    /// drift apart).
    pub fn with_power_limits(limit_w: i64, enforced_w: i64) -> Self {
        let mut mock = Self::new();
        mock.power_limits = PowerLimits {
            power_management_enabled: Some(true),
            power_limit_w: Some(limit_w),
            enforced_power_limit_w: Some(enforced_w),
            power_limit_default_w: Some(320),
            power_limit_min_w: Some(150),
            power_limit_max_w: Some(450),
        };
        mock
    }
}

impl GpuBackend for MockGpu {
    fn gpu_index(&self) -> u32 {
        self.gpu_index
    }

    fn gpu_count(&self) -> GpuResult<u32> {
        self.fail("gpu_count")?;
        Ok(self.gpu_count)
    }

    fn identity(&self) -> GpuIdentity {
        self.identity.clone()
    }

    fn gpu_name(&self) -> Option<String> {
        self.gpu_name.clone()
    }

    fn memory_info(&self) -> Option<GpuMemoryInfo> {
        self.memory_info
    }

    fn architecture(&self) -> Option<u32> {
        self.architecture
    }

    fn temperature_c(&self) -> GpuResult<f64> {
        if let Some(hook) = self.on_temperature.borrow().as_ref() {
            hook();
        }
        self.fail("temperature_c")?;
        Ok(self.temperature_c)
    }

    fn hotspot_temperature_c(&self) -> Option<f64> {
        self.fail("hotspot_temperature_c").ok()?;
        self.hotspot_temperature_c
    }

    fn fan_count(&self) -> GpuResult<u32> {
        self.fail("fan_count")?;
        Ok(self.fan_count)
    }

    fn fan_speed_limits(&self) -> (Option<u32>, Option<u32>) {
        self.fan_limits
    }

    fn reported_fan_speeds(&self, _fan_count: u32) -> Option<Vec<u32>> {
        self.reported_fan_speeds.clone()
    }

    fn power_draw_w(&self) -> Option<f64> {
        self.power_draw_w
    }

    fn gpu_utilization_pct(&self) -> Option<u32> {
        self.utilization_pct
    }

    fn clock_info_mhz(&self, clock_type: ClockType) -> Option<u32> {
        match clock_type {
            ClockType::Graphics => self.graphics_clock_mhz,
            ClockType::Sm => self.sm_clock_mhz,
            ClockType::Memory => self.memory_clock_mhz,
            ClockType::Video => self.video_clock_mhz,
        }
    }

    fn throttle_reason_mask(&self) -> Option<u64> {
        self.throttle_mask
    }

    fn gpu_context_pids(&self) -> GpuResult<GpuContextPids> {
        self.fail("gpu_context_pids")?;
        match &self.context_pids_file {
            Some(path) => Ok(parse_context_pid_list(
                &std::fs::read_to_string(path).unwrap_or_default(),
            )),
            None => Ok(self.context_pids.clone()),
        }
    }

    fn query_power_limits(&self) -> PowerLimits {
        let mut limits = self.power_limits;
        if let Some(applied) = *self.applied_power_limit_w.borrow() {
            limits.power_limit_w = Some(applied);
        }
        limits
    }

    fn apply_power_limit_w(&self, power_limit_w: i64) -> GpuResult<i64> {
        self.record(MockOp::ApplyPowerLimit { power_limit_w });
        self.fail("apply_power_limit_w")?;
        *self.applied_power_limit_w.borrow_mut() = Some(power_limit_w);
        Ok(power_limit_w)
    }

    fn enable_persistence_mode(&self) -> GpuResult<()> {
        self.record(MockOp::EnablePersistence);
        self.fail("enable_persistence_mode")
    }

    fn supported_memory_clock_steps_mhz(&self) -> Vec<u32> {
        let mut steps = self.supported_memory_clocks.clone();
        steps.sort_unstable();
        steps.dedup();
        steps
    }

    fn supported_core_clock_steps_mhz(&self) -> Vec<u32> {
        let mut steps = self.supported_core_clocks.clone();
        steps.sort_unstable();
        steps.dedup();
        steps
    }

    fn apply_locked_core_clock_mhz(
        &self,
        clock_mhz: i64,
        prefer_not_above: bool,
        snap_to_supported: bool,
    ) -> GpuResult<SnapResult> {
        self.record(MockOp::ApplyLockedCoreClock {
            clock_mhz,
            prefer_not_above,
            snap: snap_to_supported,
        });
        self.fail("apply_locked_core_clock_mhz")?;
        let snap = if snap_to_supported {
            snap_core_clock_mhz(
                clock_mhz,
                &self.supported_core_clock_steps_mhz(),
                prefer_not_above,
            )
        } else {
            SnapResult {
                requested_clock_mhz: clock_mhz,
                applied_clock_mhz: clock_mhz,
                mode: "unsnapped".to_string(),
                supported_steps_mhz: Vec::new(),
            }
        };
        Ok(snap)
    }

    fn apply_locked_core_clock_range_mhz(
        &self,
        min_clock_mhz: i64,
        max_clock_mhz: i64,
        prefer_max_not_above: bool,
        snap_to_supported: bool,
    ) -> GpuResult<RangeSnapResult> {
        self.record(MockOp::ApplyLockedCoreClockRange {
            min_mhz: min_clock_mhz,
            max_mhz: max_clock_mhz,
            prefer_max_not_above,
            snap: snap_to_supported,
        });
        self.fail("apply_locked_core_clock_range_mhz")?;
        let range = if snap_to_supported {
            snap_core_clock_range(
                min_clock_mhz,
                max_clock_mhz,
                &self.supported_core_clock_steps_mhz(),
                prefer_max_not_above,
            )
        } else {
            RangeSnapResult {
                requested_min_clock_mhz: min_clock_mhz,
                requested_max_clock_mhz: max_clock_mhz,
                applied_min_clock_mhz: min_clock_mhz,
                applied_max_clock_mhz: max_clock_mhz,
                min_mode: "unsnapped".to_string(),
                max_mode: "unsnapped".to_string(),
                supported_steps_mhz: Vec::new(),
            }
        };
        Ok(range)
    }

    fn reset_locked_core_clocks(&self) -> GpuResult<()> {
        self.record(MockOp::ResetLockedCoreClocks);
        self.fail("reset_locked_core_clocks")
    }

    fn reset_locked_memory_clocks(&self) -> GpuResult<()> {
        self.record(MockOp::ResetLockedMemoryClocks);
        self.fail("reset_locked_memory_clocks")
    }

    fn clock_offsets(&self) -> ClockOffsets {
        self.clock_offsets
    }

    fn memory_clock_offset_range_mhz(&self) -> Option<(i32, i32)> {
        self.mem_offset_range
    }

    fn apply_clock_offsets(
        &self,
        gpc_clk_vf_offset_mhz: Option<i32>,
        mem_clk_vf_offset_mhz: Option<i32>,
    ) -> GpuResult<AppliedOffsets> {
        self.record(MockOp::ApplyClockOffsets {
            gpc_mhz: gpc_clk_vf_offset_mhz,
            mem_mhz: mem_clk_vf_offset_mhz,
        });
        self.fail("apply_clock_offsets")?;
        Ok(AppliedOffsets {
            gpc_clk_vf_offset_mhz,
            gpc_clk_vf_offset_readback_mhz: gpc_clk_vf_offset_mhz,
            mem_clk_vf_offset_mhz,
            mem_clk_vf_offset_readback_mhz: mem_clk_vf_offset_mhz
                .map(|req| self.mem_offset_readback.unwrap_or(req)),
        })
    }

    fn set_fan_speed(&self, fan_idx: u32, speed_pct: u32) -> GpuResult<()> {
        self.record(MockOp::SetFanSpeed { fan_idx, speed_pct });
        self.fail("set_fan_speed")
    }

    fn set_all_fans_speed(&self, fan_count: u32, speed_pct: u32) -> GpuResult<()> {
        self.record(MockOp::SetAllFansSpeed {
            fan_count,
            speed_pct,
        });
        self.fail("set_all_fans_speed")
    }

    fn set_default_fan_speed(&self, fan_idx: u32) -> GpuResult<()> {
        self.record(MockOp::SetDefaultFanSpeed { fan_idx });
        self.fail("set_default_fan_speed")
    }

    fn set_all_fans_default(&self, fan_count: u32) -> GpuResult<()> {
        self.record(MockOp::SetAllFansDefault { fan_count });
        self.fail("set_all_fans_default")
    }

    fn read_voltage_uv(&self) -> Option<i64> {
        self.voltage_uv
    }

    fn vf_curve_available(&self) -> bool {
        self.vf_available
    }

    fn vf_points(&self) -> Vec<VfPoint> {
        self.vf_points.clone()
    }

    fn refresh_vf_points(&self) -> GpuResult<()> {
        self.fail("refresh_vf_points")
    }

    fn editable_core_vf_points(&self) -> Vec<VfPoint> {
        if let Some(hook) = self.on_editable_vf_points.borrow().as_ref() {
            hook();
        }
        vf_editable_core_points(&self.vf_points)
    }

    fn find_nearest_vf_point(&self, core_clock_mhz: i64, voltage_uv: i64) -> Option<VfPoint> {
        vf_find_nearest(&self.vf_points, core_clock_mhz, voltage_uv)
    }

    fn vf_summary(&self) -> VfSummary {
        vf_summary_of(&self.vf_points)
    }

    fn apply_vf_offsets_khz(&self, offsets: &[(u32, i32)]) -> GpuResult<()> {
        self.record(MockOp::ApplyVfOffsets {
            offsets: offsets.to_vec(),
        });
        self.fail("apply_vf_offsets_khz")
    }
}

/// Whitespace-separated token list from the `context_pids_file` seam. The
/// tokens `graphics` and `compute` select the kind for the PIDs that follow
/// (default `graphics`, so a bare PID list stays valid); other non-PID
/// tokens are ignored.
fn parse_context_pid_list(text: &str) -> GpuContextPids {
    let mut pids = GpuContextPids::default();
    let mut compute = false;
    for token in text.split_whitespace() {
        match token {
            "graphics" => compute = false,
            "compute" => compute = true,
            token => {
                if let Ok(pid) = token.parse() {
                    if compute {
                        pids.compute.push(pid);
                    } else {
                        pids.graphics.push(pid);
                    }
                }
            }
        }
    }
    pids
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn context_pids_come_from_the_canned_field_or_the_file_seam() {
        let mut mock = MockGpu::new();
        assert_eq!(mock.gpu_context_pids().unwrap(), GpuContextPids::default());
        mock.context_pids.graphics = vec![4242];
        assert_eq!(mock.gpu_context_pids().unwrap().graphics, vec![4242]);

        let dir = tempfile::tempdir().expect("tempdir");
        let file = dir.path().join("pids");
        mock.context_pids_file = Some(file.clone());
        // Missing file: the seam reports no contexts (overriding the canned field).
        assert_eq!(mock.gpu_context_pids().unwrap(), GpuContextPids::default());
        // Bare PIDs default to graphics; kind tokens switch the target list.
        std::fs::write(&file, "123 456\nnot-a-pid\ncompute 789\ngraphics 321\n").unwrap();
        assert_eq!(
            mock.gpu_context_pids().unwrap(),
            GpuContextPids {
                graphics: vec![123, 456, 321],
                compute: vec![789],
            }
        );
    }

    #[test]
    fn records_writes_in_order() {
        let mock = MockGpu::new();
        mock.set_all_fans_speed(2, 55).unwrap();
        mock.apply_power_limit_w(320).unwrap();
        mock.apply_vf_offsets_khz(&[(12, 150_000)]).unwrap();
        assert_eq!(
            mock.recorded(),
            vec![
                MockOp::SetAllFansSpeed {
                    fan_count: 2,
                    speed_pct: 55
                },
                MockOp::ApplyPowerLimit { power_limit_w: 320 },
                MockOp::ApplyVfOffsets {
                    offsets: vec![(12, 150_000)]
                },
            ]
        );
    }

    #[test]
    fn injected_failure_is_returned() {
        let mock = MockGpu::new();
        mock.inject_failure(
            "apply_power_limit_w",
            GpuError::nvml_with_text("nvmlDeviceSetPowerManagementLimit", 3, "Not Supported"),
        );
        let err = mock.apply_power_limit_w(300).unwrap_err();
        assert_eq!(err.rc(), 3);
        // The op is still recorded even though it "failed".
        assert_eq!(mock.recorded().len(), 1);
        mock.clear_failure("apply_power_limit_w");
        assert!(mock.apply_power_limit_w(300).is_ok());
    }

    #[test]
    fn snap_uses_configured_steps() {
        let mut mock = MockGpu::new();
        mock.supported_core_clocks = vec![1800, 1900, 2000, 2100];
        let snap = mock.apply_locked_core_clock_mhz(1950, true, true).unwrap();
        assert_eq!(snap.applied_clock_mhz, 1900);
        assert_eq!(snap.mode, "floor");
    }

    #[test]
    fn mem_offset_readback_echoes_by_default() {
        let mock = MockGpu::new();
        let applied = mock.apply_clock_offsets(None, Some(1500)).unwrap();
        assert_eq!(applied.mem_clk_vf_offset_mhz, Some(1500));
        assert_eq!(applied.mem_clk_vf_offset_readback_mhz, Some(1500));
    }

    #[test]
    fn mem_offset_readback_can_diverge() {
        // Simulate the issue-#20 case: write succeeds, read-back differs.
        let mut mock = MockGpu::new();
        mock.mem_offset_readback = Some(0);
        let applied = mock.apply_clock_offsets(None, Some(1500)).unwrap();
        assert_eq!(applied.mem_clk_vf_offset_mhz, Some(1500));
        assert_eq!(applied.mem_clk_vf_offset_readback_mhz, Some(0));
    }
}
