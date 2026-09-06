//! GPU mutation pipeline — port of
//! `runtime/gpu_control/vf_curve_runtime_policy.py`, `vf_curve_plan.py::apply_plan`,
//! and the profile power/memory apply in `runtime_auto_uv_profile.py`.
//!
//! Load-bearing ordering: persistence mode → memory offset (with read-back) →
//! per-point VF frequency offsets → power limit → locked clock ceiling.
//!
//! WHY the mem offset must precede the VF writes (still load-bearing): a coarse
//! mem-offset write WIPES the entire core per-point VF offset table — on BOTH
//! NVML API families (the modern `nvmlDeviceSetClockOffsets` and the deprecated
//! `nvmlDeviceSetMemClkVfOffset` fallback; each proven 2026-07-07 on the live
//! GPU, driver 610.43.02). The coupling is one-directional — VF writes leave the
//! mem offset untouched, and locked clocks / the power limit touch neither.
//! Applying the mem offset first, then the per-point offsets, leaves both live;
//! the reverse order silently erases the fresh curve.

use crate::gpu::{GpuBackend, NVML_ERROR_NOT_SUPPORTED};

use super::ceiling::FlattenedClockCeilingController;
use super::guard::select_expected_vf_samples;
use super::runtime_spec::{LoadedCurve, PlanItem, RuntimeMode};

/// `apply_plan`: MHz→kHz ×1000, then a single `SetControl` write. The engine
/// does the ×1000 here (coordinator answer, matches Python `apply_plan`).
pub fn apply_plan(backend: &dyn GpuBackend, plan: &[PlanItem]) -> crate::gpu::GpuResult<()> {
    let offsets: Vec<(u32, i32)> = plan
        .iter()
        .map(|p| (p.index, (p.new_offset_mhz * 1000) as i32))
        .collect();
    backend.apply_vf_offsets_khz(&offsets)
}

/// The applied base policy (`power_limit_w` / `mem_clk_vf_offset_mhz`).
#[derive(Debug, Clone, Copy, Default)]
pub struct GpuPolicyApplied {
    pub power_limit_w: Option<i64>,
    pub mem_clk_vf_offset_mhz: Option<i64>,
}

impl GpuPolicyApplied {
    /// `describe_translated_gpu_policy` for the two-key applied policy.
    pub fn describe(&self) -> String {
        let mut parts = Vec::new();
        if let Some(w) = self.power_limit_w {
            parts.push(format!("power-limit={w}W"));
        }
        if let Some(m) = self.mem_clk_vf_offset_mhz {
            parts.push(format!("mem-vf-offset={m:+}MHz"));
        }
        if parts.is_empty() {
            "none".to_string()
        } else {
            parts.join(", ")
        }
    }
}

#[derive(Default)]
pub struct VfPolicyResult<'a> {
    pub auto_uv_final_curve: Option<LoadedCurve>,
    pub vf_apply_plan: Option<Vec<PlanItem>>,
    pub active_vf_curve_source: Option<String>,
    pub auto_uv_profile_gpu_policy: GpuPolicyApplied,
    pub active_profile_id: String,
    pub active_profile_tier: String,
    pub active_profile_tier_key: String,
    pub clock_ceiling_controller: Option<FlattenedClockCeilingController<'a>>,
    pub vf_expected_samples: Vec<PlanItem>,
    /// Profile target (`lock_clock_mhz`, `candidate_voltage_mv`) for status lines.
    pub profile_clock_mhz: Option<f64>,
    pub profile_voltage_mv: Option<f64>,
}

pub(super) fn apply_gpu_base_policy(
    backend: &dyn GpuBackend,
    enable_persistence_mode: bool,
    log: &mut dyn FnMut(&str),
) {
    if enable_persistence_mode {
        match backend.enable_persistence_mode() {
            Ok(()) => log("Enabled GPU persistence mode"),
            Err(exc) => log(&format!("Skipping GPU persistence mode: error={exc}")),
        }
    }
}

pub(super) fn reset_gpu_to_stock(
    backend: &dyn GpuBackend,
    log: &mut dyn FnMut(&str),
) -> Result<Option<bool>, String> {
    let mut failures = Vec::new();
    let mut power_limit_set_supported = None;
    if let Err(exc) = backend.reset_locked_core_clocks() {
        failures.push(format!("locked core clocks: {exc}"));
    }
    // The runtime does not create a memory-clock lock, so this legacy cleanup
    // remains best-effort for drivers that do not expose the reset call.
    if let Err(exc) = backend.reset_locked_memory_clocks() {
        log(&format!(
            "keep-stock: locked memory clocks reset skipped: {exc}"
        ));
    }
    match backend.apply_clock_offsets(Some(0), Some(0)) {
        Ok(applied)
            if applied.gpc_clk_vf_offset_readback_mhz == Some(0)
                && applied.mem_clk_vf_offset_readback_mhz == Some(0) => {}
        Ok(applied) => failures.push(format!(
            "clock offsets readback: gpc={:?} memory={:?}",
            applied.gpc_clk_vf_offset_readback_mhz, applied.mem_clk_vf_offset_readback_mhz
        )),
        Err(exc) => failures.push(format!("clock offsets: {exc}")),
    }
    // Re-applying the readable default is also the capability check. Fixed
    // mobile limits remain unchanged and return NVML_ERROR_NOT_SUPPORTED;
    // that rejection is best-effort because no power state was changed.
    let default_w = backend.query_power_limits().power_limit_default_w;
    if let Some(default_w) = default_w {
        if default_w != 0 {
            match backend.apply_power_limit_w(default_w) {
                Err(exc) if exc.rc() == NVML_ERROR_NOT_SUPPORTED => {
                    power_limit_set_supported = Some(false);
                    log(&format!(
                        "keep-stock: default power limit restore skipped: {exc}"
                    ));
                }
                Err(exc) => failures.push(format!("power limit restore: {exc}")),
                Ok(_) => {
                    power_limit_set_supported = Some(true);
                    let readback = backend.query_power_limits();
                    if readback.power_limit_w.or(readback.enforced_power_limit_w) != Some(default_w)
                    {
                        failures.push(format!(
                            "power limit readback: current={:?} enforced={:?} expected={default_w}",
                            readback.power_limit_w, readback.enforced_power_limit_w
                        ));
                    }
                }
            }
        }
    }
    if backend.vf_curve_available() {
        if let Err(exc) = backend.refresh_vf_points() {
            failures.push(format!("V/F refresh before reset: {exc}"));
        } else {
            let zero_offsets: Vec<(u32, i32)> = backend
                .editable_core_vf_points()
                .iter()
                .map(|point| (point.index, 0))
                .collect();
            if let Err(exc) = backend.apply_vf_offsets_khz(&zero_offsets) {
                failures.push(format!("V/F reset: {exc}"));
            } else if let Err(exc) = backend.refresh_vf_points() {
                failures.push(format!("V/F refresh after reset: {exc}"));
            } else if let Some(point) = backend
                .editable_core_vf_points()
                .into_iter()
                .find(|point| point.current_offset_khz != 0)
            {
                failures.push(format!(
                    "V/F point {} read back {:+} kHz after reset",
                    point.index, point.current_offset_khz
                ));
            }
        }
    }
    if !failures.is_empty() {
        return Err(format!(
            "stock GPU reset incomplete: {}",
            failures.join("; ")
        ));
    }
    log("keep-stock: GPU reset to factory V/F; no undervolt applied");
    Ok(power_limit_set_supported)
}

pub(super) fn apply_power_limit(
    backend: &dyn GpuBackend,
    label: &str,
    power_limit_w: Option<i64>,
    log: &mut dyn FnMut(&str),
) -> Result<Option<i64>, String> {
    let Some(power) = power_limit_w.filter(|&p| p > 0) else {
        return Ok(None);
    };
    let applied = match backend.apply_power_limit_w(power) {
        Ok(applied) => applied,
        Err(exc) if exc.rc() == NVML_ERROR_NOT_SUPPORTED => {
            log(&format!(
                "Skipped saved profile fixed power limit {power} W for {label}: driver reports that power-limit control is unavailable"
            ));
            return Ok(None);
        }
        Err(exc) => {
            return Err(format!(
                "failed to apply saved profile power limit {power} W for {label}: driver rejected nvmlDeviceSetPowerManagementLimit: {exc}"
            ));
        }
    };
    let readback = backend.query_power_limits();
    if applied != power
        || (readback.power_limit_w != Some(power) && readback.enforced_power_limit_w != Some(power))
    {
        return Err(format!(
            "failed to verify saved profile power limit {power} W for {label}: current={:?} enforced={:?}",
            readback.power_limit_w, readback.enforced_power_limit_w
        ));
    }
    Ok(Some(power))
}

fn apply_memory_offset(
    backend: &dyn GpuBackend,
    label: &str,
    memory_offset_mhz: Option<i64>,
) -> Result<Option<i64>, String> {
    let Some(raw) = memory_offset_mhz else {
        return Ok(None);
    };
    let driver_max = backend
        .memory_clock_offset_range_mhz()
        .map(|(_, max)| i64::from(max));
    let offset = clamp_memory_offset_to_driver(raw, driver_max);
    let applied = backend
        .apply_clock_offsets(None, Some(offset as i32))
        .map_err(|exc| {
            format!(
                "failed to apply saved profile memory offset {offset:+} MHz for {label}: driver rejected nvmlDeviceSetMemClkVfOffset: {exc}"
            )
        })?;
    if applied.mem_clk_vf_offset_readback_mhz != Some(offset as i32) {
        return Err(format!(
            "failed to verify saved profile memory offset {offset:+} MHz for {label}: readback={:?}",
            applied.mem_clk_vf_offset_readback_mhz
        ));
    }
    Ok(Some(offset))
}

/// Apply the memory offset (when `memory_offset_mhz` is `Some`) and then the
/// per-point VF plan, re-reading the curve afterward. Returns the applied,
/// driver-clamped memory offset (`None` when `memory_offset_mhz` was `None`).
///
/// WHY the mem offset must precede the VF writes (load-bearing invariant): a
/// coarse mem-offset write WIPES the entire core per-point VF offset table — on
/// BOTH NVML API families (`nvmlDeviceSetClockOffsets` and the deprecated
/// `nvmlDeviceSetMemClkVfOffset` fallback; each proven 2026-07-07 on the live
/// GPU, driver 610.43.02). The coupling is one-directional — VF writes leave
/// the mem offset untouched, and locked clocks / the power limit touch
/// neither. Applying the mem offset first, then the per-point offsets, leaves
/// both live; the reverse order silently erases the fresh curve. Callers that
/// pass `None` (the startup and VF-reapply guards) have already
/// written/verified the mem offset just above the call, so the ordering still
/// holds.
pub fn apply_memory_offset_then_vf_plan(
    backend: &dyn GpuBackend,
    label: &str,
    memory_offset_mhz: Option<i64>,
    plan: &[PlanItem],
) -> Result<Option<i64>, String> {
    let memory = apply_memory_offset(backend, label, memory_offset_mhz)?;
    apply_plan(backend, plan).map_err(|e| e.to_string())?;
    backend.refresh_vf_points().map_err(|e| e.to_string())?;
    verify_vf_plan(backend, label, plan)?;
    Ok(memory)
}

fn verify_vf_plan(backend: &dyn GpuBackend, label: &str, plan: &[PlanItem]) -> Result<(), String> {
    let points = backend.vf_points();
    for item in plan {
        let expected_khz = item.new_offset_mhz * 1000;
        let Some(point) = points.iter().find(|point| point.index == item.index) else {
            return Err(format!(
                "failed to verify V/F curve for {label}: point {} is missing after apply",
                item.index
            ));
        };
        if point.current_offset_khz != expected_khz {
            return Err(format!(
                "failed to verify V/F curve for {label}: point {} expected {expected_khz:+} kHz read back {:+} kHz",
                item.index, point.current_offset_khz
            ));
        }
    }
    Ok(())
}

/// Apply the fully resolved profile embedded in a RuntimeSpec.
pub fn configure_runtime_spec_policy<'a>(
    backend: &'a dyn GpuBackend,
    enable_persistence_mode: bool,
    mode: RuntimeMode,
    curve: Option<&LoadedCurve>,
    log: &mut dyn FnMut(&str),
) -> Result<VfPolicyResult<'a>, String> {
    let mut result = VfPolicyResult::default();

    if mode == RuntimeMode::Stock {
        apply_gpu_base_policy(backend, enable_persistence_mode, log);
        reset_gpu_to_stock(backend, log)?;
        result.active_vf_curve_source = Some("stock".to_string());
        return Ok(result);
    }
    result.auto_uv_final_curve = curve.cloned();

    apply_gpu_base_policy(backend, enable_persistence_mode, log);

    if !backend.vf_curve_available() {
        return Ok(result);
    }

    apply_auto_uv_final_curve(&mut result, backend, log)?;
    Ok(result)
}

fn clamp_memory_offset_to_driver(offset_mhz: i64, driver_range_max: Option<i64>) -> i64 {
    let upper = driver_range_max.unwrap_or(2000).max(0);
    offset_mhz.clamp(0, upper)
}

fn apply_auto_uv_final_curve<'a>(
    result: &mut VfPolicyResult<'a>,
    backend: &'a dyn GpuBackend,
    log: &mut dyn FnMut(&str),
) -> Result<(), String> {
    let Some(curve) = result.auto_uv_final_curve.clone() else {
        return Ok(());
    };

    // Memory offset FIRST — before the per-point VF writes (mem-wipes-VF; see
    // `apply_memory_offset_then_vf_plan`). Fatal on failure (spec 02 §6.4).
    // Recorded into the result immediately so the applied policy reflects hardware
    // even if the VF write below then fails and bails.
    let memory = apply_memory_offset(backend, "auto-UV final curve", curve.memory_offset_mhz)?;
    if let Some(m) = memory {
        log(&format!("Applied auto-UV profile memory offset: {m:+}MHz."));
    }
    result.auto_uv_profile_gpu_policy.mem_clk_vf_offset_mhz = memory;

    // Mem already applied above → pass None; the helper does the VF plan + readback.
    apply_memory_offset_then_vf_plan(backend, "auto-UV final curve", None, &curve.plan).map_err(
        |exc| {
            format!(
                "failed to apply auto-UV final curve {}: {exc}",
                curve.path.display()
            )
        },
    )?;

    result.vf_apply_plan = Some(curve.plan.clone());
    result.active_vf_curve_source = Some("auto-uv-final".to_string());
    result.active_profile_id = curve.profile_id.clone();
    result.active_profile_tier = curve.profile_tier.clone();
    result.active_profile_tier_key = curve.profile_tier_key.clone();
    result.vf_expected_samples = select_expected_vf_samples(&curve.plan, 8);
    result.profile_clock_mhz = Some(curve.lock_clock_mhz as f64);
    result.profile_voltage_mv = Some(curve.candidate_voltage_mv as f64);
    log(&format!(
        "Applied auto-UV final curve: path={} target={}MHz@{}mV points={}.",
        curve.path.display(),
        curve.lock_clock_mhz,
        curve.candidate_voltage_mv,
        curve.plan.len()
    ));

    let power = apply_power_limit(backend, "auto-UV final curve", curve.power_limit_w, log)?;
    if let Some(w) = power {
        log(&format!("Applied auto-UV profile power limit: {w}W."));
    }
    result.auto_uv_profile_gpu_policy.power_limit_w = power;

    let mut controller =
        FlattenedClockCeilingController::new(curve.flatten_target.clone(), backend);
    match controller.apply() {
        Ok(()) => {
            log(&format!(
                "Configured auto-UV clock ceiling: {}.",
                controller.describe()
            ));
            result.clock_ceiling_controller = Some(controller);
        }
        Err(exc) => {
            return Err(format!(
                "failed to configure auto-UV clock ceiling {}: {exc}",
                curve.path.display()
            ));
        }
    }
    // keep the loaded curve on the result
    result.auto_uv_final_curve = Some(curve);
    Ok(())
}

/// The adaptive tier switch's GPU ops: memory offset → VF plan → power →
/// ceiling retarget. Returns the applied power/mem for the switch result. The
/// mem-before-VF ordering (mem write wipes the VF table) is owned by
/// `apply_memory_offset_then_vf_plan`; power stays after the VF writes.
pub fn apply_adaptive_curve(
    backend: &dyn GpuBackend,
    curve: &LoadedCurve,
    ceiling: &mut Option<FlattenedClockCeilingController<'_>>,
    tier_label: &str,
    log: &mut dyn FnMut(&str),
) -> Result<(Option<i64>, Option<i64>), String> {
    let label = format!("adaptive {tier_label} profile");
    let memory =
        apply_memory_offset_then_vf_plan(backend, &label, curve.memory_offset_mhz, &curve.plan)?;
    let power = apply_power_limit(backend, &label, curve.power_limit_w, log)?;
    if let Some(controller) = ceiling {
        controller
            .retarget(curve.flatten_target.clone())
            .map_err(|e| e.to_string())?;
    }
    Ok((power, memory))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gpu::mock::{MockGpu, MockOp};

    fn item(index: u32, offset: i64) -> PlanItem {
        PlanItem {
            index,
            voltage_mv: 800 + offset,
            base_mhz: 2000,
            target_mhz: 2000 + offset,
            new_offset_mhz: offset,
        }
    }

    #[test]
    fn apply_plan_converts_mhz_to_khz_single_write() {
        let mock = MockGpu::new();
        let plan = vec![item(3, 120), item(7, -50)];
        apply_plan(&mock, &plan).unwrap();
        assert_eq!(
            mock.recorded(),
            vec![MockOp::ApplyVfOffsets {
                offsets: vec![(3, 120_000), (7, -50_000)]
            }]
        );
    }

    #[test]
    fn stock_reset_sequence_single_locked_core_reset() {
        let mut mock = MockGpu::new();
        mock.vf_available = true;
        mock.power_limits.power_limit_default_w = Some(300);
        mock.power_limits.power_limit_w = Some(300);
        mock.vf_points = vec![crate::gpu::VfPoint {
            index: 5,
            type_: 0,
            voltage_based: 1,
            ..Default::default()
        }];
        let mut log = |_: &str| {};
        let result =
            configure_runtime_spec_policy(&mock, true, RuntimeMode::Stock, None, &mut log).unwrap();
        assert_eq!(result.active_vf_curve_source.as_deref(), Some("stock"));
        let ops = mock.recorded();
        // persistence → reset core (ONCE) → reset mem → offsets 0/0 → power default
        // → refresh (not recorded) → vf zero plan.
        assert_eq!(
            ops,
            vec![
                MockOp::EnablePersistence,
                MockOp::ResetLockedCoreClocks,
                MockOp::ResetLockedMemoryClocks,
                MockOp::ApplyClockOffsets {
                    gpc_mhz: Some(0),
                    mem_mhz: Some(0)
                },
                MockOp::ApplyPowerLimit { power_limit_w: 300 },
                MockOp::ApplyVfOffsets {
                    offsets: vec![(5, 0)]
                },
            ]
        );
    }

    /// An unrecognized mobile board can still reject the manual setter; the
    /// stock reset must remain tolerant and zero the V/F curve.
    #[test]
    fn stock_reset_tolerates_rejected_power_limit_setter_for_unknown_identity() {
        let mut mock = MockGpu::new();
        mock.vf_available = true;
        mock.power_limits.power_limit_default_w = Some(115);
        mock.power_limits.power_limit_w = Some(115);
        mock.vf_points = vec![crate::gpu::VfPoint {
            index: 5,
            type_: 0,
            voltage_based: 1,
            ..Default::default()
        }];
        mock.inject_failure(
            "apply_power_limit_w",
            crate::gpu::GpuError::nvml_with_text(
                "nvmlDeviceSetPowerManagementLimit",
                3,
                "Not Supported",
            ),
        );
        let mut messages = Vec::new();
        let mut log = |msg: &str| messages.push(msg.to_string());
        let result =
            configure_runtime_spec_policy(&mock, true, RuntimeMode::Stock, None, &mut log).unwrap();
        assert_eq!(result.active_vf_curve_source.as_deref(), Some("stock"));
        let ops = mock.recorded();
        assert!(
            ops.contains(&MockOp::ApplyVfOffsets {
                offsets: vec![(5, 0)]
            }),
            "V/F zero plan must still apply when the power limit setter is rejected: {ops:?}"
        );
        assert!(
            messages
                .iter()
                .any(|msg| msg.contains("default power limit restore skipped")),
            "the skipped power limit restore must be logged: {messages:?}"
        );
    }

    #[test]
    fn stock_reset_keeps_other_power_limit_failures_fatal() {
        let mut mock = MockGpu::new();
        mock.power_limits.power_limit_default_w = Some(115);
        mock.inject_failure(
            "apply_power_limit_w",
            crate::gpu::GpuError::nvml_with_text(
                "nvmlDeviceSetPowerManagementLimit",
                4,
                "Insufficient Permissions",
            ),
        );
        let mut log = |_: &str| {};

        let error = reset_gpu_to_stock(&mock, &mut log).unwrap_err();

        assert!(error.contains("NVML error 4: Insufficient Permissions"));
    }

    #[test]
    fn stock_reset_uses_driver_result_instead_of_mobile_identity() {
        let mut mock = MockGpu::new();
        mock.identity.name = "NVIDIA GeForce RTX 4090 Laptop GPU".to_string();
        mock.vf_available = true;
        mock.power_limits.power_limit_default_w = Some(150);
        mock.power_limits.power_limit_w = Some(150);
        mock.vf_points = vec![crate::gpu::VfPoint {
            index: 5,
            type_: 0,
            voltage_based: 1,
            ..Default::default()
        }];
        mock.inject_failure(
            "apply_power_limit_w",
            crate::gpu::GpuError::nvml_with_text(
                "nvmlDeviceSetPowerManagementLimit",
                3,
                "Not Supported",
            ),
        );
        let mut messages = Vec::new();
        let mut log = |message: &str| messages.push(message.to_string());

        let result =
            configure_runtime_spec_policy(&mock, true, RuntimeMode::Stock, None, &mut log).unwrap();

        assert_eq!(result.active_vf_curve_source.as_deref(), Some("stock"));
        assert!(mock
            .recorded()
            .contains(&MockOp::ApplyPowerLimit { power_limit_w: 150 }));
        assert!(mock.recorded().contains(&MockOp::ApplyVfOffsets {
            offsets: vec![(5, 0)]
        }));
        assert!(messages
            .iter()
            .any(|message| message.contains("keep-stock: default power limit restore skipped")));
    }

    #[test]
    fn power_limit_failure_is_fatal() {
        let mock = MockGpu::new();
        mock.inject_failure(
            "apply_power_limit_w",
            crate::gpu::GpuError::nvml_with_text(
                "nvmlDeviceSetPowerManagementLimit",
                4,
                "Insufficient Permissions",
            ),
        );
        let mut log = |_: &str| {};
        let err = apply_power_limit(&mock, "auto-UV final curve", Some(320), &mut log).unwrap_err();
        assert_eq!(
            err,
            "failed to apply saved profile power limit 320 W for auto-UV final curve: driver rejected nvmlDeviceSetPowerManagementLimit: nvmlDeviceSetPowerManagementLimit failed with NVML error 4: Insufficient Permissions"
        );
    }

    #[test]
    fn unavailable_power_limit_setter_is_skipped_without_identity_matching() {
        let mock = MockGpu::new();
        mock.inject_failure(
            "apply_power_limit_w",
            crate::gpu::GpuError::nvml_with_text(
                "nvmlDeviceSetPowerManagementLimit",
                3,
                "the requested operation is not available on the target device",
            ),
        );
        let mut messages = Vec::new();
        let mut log = |message: &str| messages.push(message.to_string());

        let applied = apply_power_limit(&mock, "auto-UV final curve", Some(35), &mut log).unwrap();

        assert_eq!(applied, None);
        assert_eq!(
            messages,
            ["Skipped saved profile fixed power limit 35 W for auto-UV final curve: driver reports that power-limit control is unavailable"]
        );
    }

    #[test]
    fn unsupported_static_power_limit_is_skipped_after_driver_probe() {
        let mut mock = MockGpu::new();
        mock.identity.name = "NVIDIA GeForce RTX 2050".to_string();
        mock.inject_failure(
            "apply_power_limit_w",
            crate::gpu::GpuError::nvml_with_text(
                "nvmlDeviceSetPowerManagementLimit",
                3,
                "Not Supported",
            ),
        );
        let mut messages = Vec::new();
        let mut log = |message: &str| messages.push(message.to_string());

        let applied = apply_power_limit(&mock, "auto-UV final curve", Some(150), &mut log).unwrap();

        assert_eq!(applied, None);
        assert_eq!(
            mock.recorded(),
            [MockOp::ApplyPowerLimit { power_limit_w: 150 }]
        );
        assert_eq!(
            messages,
            ["Skipped saved profile fixed power limit 150 W for auto-UV final curve: driver reports that power-limit control is unavailable"]
        );
    }

    #[test]
    fn memory_offset_clamped_to_driver_and_applied() {
        let mut mock = MockGpu::new();
        mock.mem_offset_range = Some((-2000, 3000));
        let applied = apply_memory_offset(&mock, "auto-UV final curve", Some(5000)).unwrap();
        assert_eq!(applied, Some(3000)); // clamped to driver max
        assert_eq!(
            mock.recorded(),
            vec![MockOp::ApplyClockOffsets {
                gpc_mhz: None,
                mem_mhz: Some(3000)
            }]
        );
    }

    /// The adaptive tier switch pins the same mem-before-VF ordering as the
    /// startup pipeline: a coarse mem-offset write (either NVML API family)
    /// wipes the whole core per-point VF offset table (mem-wipes-VF coupling),
    /// so the memory offset must land BEFORE the VF plan or the switch erases
    /// its own curve.
    /// Power limit stays after the VF writes.
    #[test]
    fn adaptive_curve_applies_mem_before_vf_then_power() {
        use super::super::runtime_spec::FlattenTarget;

        let mut mock = MockGpu::new();
        mock.mem_offset_range = Some((-2000, 6000));
        mock.power_limits.power_limit_w = Some(320);
        mock.vf_points = vec![crate::gpu::VfPoint {
            index: 12,
            type_: 0,
            voltage_based: 1,
            current_offset_khz: 240_000,
            ..Default::default()
        }];
        let curve = LoadedCurve {
            path: std::path::PathBuf::new(),
            profile_id: "adaptive-test".to_string(),
            profile_tier: "Balanced".to_string(),
            profile_tier_key: "balanced".to_string(),
            plan: vec![item(12, 240)],
            lock_clock_mhz: 2640,
            candidate_voltage_mv: 875,
            memory_offset_mhz: Some(1500),
            power_limit_w: Some(320),
            avg_power_w: None,
            base_avg_power_w: None,
            flatten_target: FlattenTarget {
                source: "auto-uv-final".to_string(),
                lock_clock_mhz: 2640,
                lock_voltage_mv: Some(875),
                end_voltage_mv: Some(875),
                tail_point_count: Some(1),
                ceiling_clock_mhz: None,
                tail_rise_bins: None,
            },
        };

        let mut ceiling = None;
        let mut log = |_: &str| {};
        let (power, memory) =
            apply_adaptive_curve(&mock, &curve, &mut ceiling, "balanced", &mut log).unwrap();
        assert_eq!(power, Some(320));
        assert_eq!(memory, Some(1500));
        // Exact op order: mem offset → VF plan → power limit (no ceiling here).
        assert_eq!(
            mock.recorded(),
            vec![
                MockOp::ApplyClockOffsets {
                    gpc_mhz: None,
                    mem_mhz: Some(1500)
                },
                MockOp::ApplyVfOffsets {
                    offsets: vec![(12, 240_000)]
                },
                MockOp::ApplyPowerLimit { power_limit_w: 320 },
            ]
        );
    }

    #[test]
    fn unsupported_adaptive_power_limit_is_skipped_after_driver_probe() {
        use super::super::runtime_spec::FlattenTarget;

        let mut mock = MockGpu::new();
        mock.identity.name = "NVIDIA GeForce RTX 2050".to_string();
        mock.mem_offset_range = Some((-2000, 6000));
        mock.vf_points = vec![crate::gpu::VfPoint {
            index: 12,
            type_: 0,
            voltage_based: 1,
            current_offset_khz: 240_000,
            ..Default::default()
        }];
        mock.inject_failure(
            "apply_power_limit_w",
            crate::gpu::GpuError::nvml_with_text(
                "nvmlDeviceSetPowerManagementLimit",
                3,
                "Not Supported",
            ),
        );
        let curve = LoadedCurve {
            path: std::path::PathBuf::new(),
            profile_id: "legacy-adaptive-mobile".to_string(),
            profile_tier: "Balanced".to_string(),
            profile_tier_key: "balanced".to_string(),
            plan: vec![item(12, 240)],
            lock_clock_mhz: 2640,
            candidate_voltage_mv: 875,
            memory_offset_mhz: Some(1500),
            power_limit_w: Some(150),
            avg_power_w: None,
            base_avg_power_w: None,
            flatten_target: FlattenTarget {
                source: "auto-uv-final".to_string(),
                lock_clock_mhz: 2640,
                lock_voltage_mv: Some(875),
                end_voltage_mv: Some(875),
                tail_point_count: Some(1),
                ceiling_clock_mhz: None,
                tail_rise_bins: None,
            },
        };
        let mut ceiling = None;
        let mut messages = Vec::new();
        let mut log = |message: &str| messages.push(message.to_string());

        let (power, memory) =
            apply_adaptive_curve(&mock, &curve, &mut ceiling, "balanced", &mut log).unwrap();

        assert_eq!(power, None);
        assert_eq!(memory, Some(1500));
        assert!(mock
            .recorded()
            .contains(&MockOp::ApplyPowerLimit { power_limit_w: 150 }));
        assert_eq!(
            messages,
            ["Skipped saved profile fixed power limit 150 W for adaptive balanced profile: driver reports that power-limit control is unavailable"]
        );
    }

    #[test]
    fn describe_policy() {
        let policy = GpuPolicyApplied {
            power_limit_w: Some(320),
            mem_clk_vf_offset_mhz: Some(1500),
        };
        assert_eq!(
            policy.describe(),
            "power-limit=320W, mem-vf-offset=+1500MHz"
        );
        assert_eq!(GpuPolicyApplied::default().describe(), "none");
    }
}
