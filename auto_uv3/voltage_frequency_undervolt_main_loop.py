"""Run the top-level voltage-frequency undervolt main loop.

This keeps phase order readable: setup, base-load probe, lower-voltage sweep,
user final choice, final verification, and cleanup.
"""

from __future__ import annotations

import re
from typing import Callable

from stability.q2rtx import Q2RTXStabilityConfig, cleanup_managed_q2rtx_processes

from .auto_uv_types import (
    AutoUvError,
    AutoUvProbeSummary,
    AutoUvVoltageScanResult,
    VfCurveCandidate,
)
from .auto_uv_scan_settings import AutoUvScanSettings
from .auto_uv_console_log import log_benchmark, log_phase, log_user_stage
from .auto_uv_scan_result import build_voltage_scan_result, curve_overclock_summary
from .recovery.baseline_upward_stabilization import find_upward_stable_baseline_candidate
from .curve.base_load_flatten_target import choose_base_load_flatten_target
from .curve.base_load_voltage import derive_loaded_voltage_band
from .curve.base_vf_curve import editable_base_vf_points
from .curve.base_vf_curve_validation import validate_base_vf_curve
from .curve.base_vf_curve_voltage_bins import (
    lock_voltage_for_target_clock,
    nearest_editable_voltage_bin,
    next_higher_editable_voltage_bin,
)
from .ui.final_verification_candidate_choice import choose_final_verification_candidate
from .recovery.final_failure_upward_stabilization import find_upward_stable_final_candidate
from .gpu.gpu_vf_curve_applier import open_live_gpu_vf_curve_applier
from .persistence.interrupted_probe_crash_cache import consume_interrupted_probe_crash_marker
from .lower_voltage_sweep_loop import (
    LowerVoltageSweepHooks,
    run_lower_voltage_sweep_loop,
)
from .curve.measured_probe_lock_clock import lock_clock_from_probe_loaded_clock
from .persistence.previous_crash_recovery_budget_limit import (
    recovery_budget_limit_after_crash_cache,
)
from .ui.probe_summary_ui_payload import probe_summary_ui_payload
from .q2rtx.q2rtx_cuda_probe_runner import Q2RtxCudaProbeRunner
from .q2rtx.q2rtx_cuda_voltage_probe import probe_voltage_candidate
from .scan_mode import AUTO_UV_MODE_PERFORMANCE
from .scan_mode.uv_limits import uv_limit_profile_target_for_gpu
from .scan_runtime_settings import read_scan_runtime_settings
from .ui.ui_json_event_writer import AutoUvEventCallback, emit_ui_json_event
from .persistence.unsafe_voltage_blacklist_file import load_unsafe_voltage_blacklist
from .persistence.verified_candidate_result_file import write_latest_verified_candidate
from .persistence.auto_uv_persisted_json_files import clear_auto_uv_stop_request
from .curve.vf_curve_flattening import build_flatten_target, build_flattened_plan
from .ui.vf_curve_ui_points import vf_curve_ui_points
from .voltage_sweep_state import VoltageProbeOutcome
from .final_verification import run_final_verification_and_save


RECOVERY_BUDGET_LABEL_RE = re.compile(
    r"\brecovery-budget=(?P<used>[0-9]+(?:\.[0-9]+)?)/"
    r"(?P<limit>[0-9]+(?:\.[0-9]+)?)%"
)


def run_voltage_frequency_undervolt_main_loop(
    *,
    gpu_index: int,
    runtime_options: dict,
    q2rtx_config: Q2RTXStabilityConfig,
    log: Callable[[str], None] = print,
    event_callback: AutoUvEventCallback | None = None,
) -> AutoUvVoltageScanResult:
    settings = read_scan_runtime_settings(runtime_options, q2rtx_config)
    q2rtx_config = settings.q2rtx_config
    timedemo_warmup_runs = int(settings.timedemo_warmup_runs)
    unsafe_entries = consume_crash_cache(log=log)
    effective_recovery_budget_pct = recovery_budget_limit_after_crash_cache(
        unsafe_entries,
        float(settings.clock_bump_budget_limit_pct),
    )
    cleanup_managed_q2rtx_processes(q2rtx_config, log=log)
    gpu = open_live_gpu_vf_curve_applier(
        gpu_index=int(gpu_index),
        runtime_options=runtime_options,
        log=log,
    )
    try:
        base_curve = list(gpu.runtime_default_plan)
        validate_base_vf_curve(base_curve)
        emit_ui_json_event(
            event_callback,
            "base_curve",
            points=vf_curve_ui_points(base_curve),
        )
        log_phase(
            log,
            "auto-uv3",
            "Auto-UV voltage-frequency main loop enabled",
        )
        discovery_summary, discovery_result = run_discovery_probe(
            base_curve,
            gpu=gpu,
            q2rtx_config=q2rtx_config,
            short_probe_base_duration_s=int(settings.short_probe_base_duration_s),
            timedemo_warmup_runs=int(timedemo_warmup_runs),
            log=log,
            event_callback=event_callback,
        )
        if not bool(getattr(discovery_result, "success", False)):
            raise AutoUvError(
                "base Defaults baseline failed the Q2RTX probe: "
                f"{getattr(discovery_result, 'reason', 'unknown')}"
            )

        baseline_candidate, baseline_target = build_loaded_baseline_candidate(
            base_curve,
            discovery_summary=discovery_summary,
            discovery_result=discovery_result,
            power_limit_w=gpu.power_limit_w,
        )
        gpu.start_clock_ceiling(
            build_flatten_target(
                base_curve,
                lock_clock_mhz=int(baseline_candidate.target_mhz),
                lock_voltage_mv=int(baseline_candidate.voltage_mv),
            )
        )
        runner = Q2RtxCudaProbeRunner(
            reader=gpu.reader,
            live_voltage_reader=gpu.live_voltage_reader,
            q2rtx_config=q2rtx_config,
            runtime_default_plan=gpu.runtime_default_plan,
            power_limit_w=gpu.power_limit_w,
            start_voltage_mv=int(baseline_candidate.voltage_mv),
            baseline_clock_mhz=float(baseline_target.measured_clock_mhz),
            min_performance_core_clock_pct=float(settings.min_performance_core_clock_pct),
            short_probe_base_duration_s=int(settings.short_probe_base_duration_s),
            timedemo_warmup_runs=int(timedemo_warmup_runs),
            log=log,
            event_callback=event_callback,
        )
        probe_history: list[AutoUvProbeSummary] = [discovery_summary]
        stable_history: list[AutoUvProbeSummary] = []
        baseline_outcome = runner.probe_baseline_candidate(baseline_candidate)
        if baseline_outcome.raw_probe is not None:
            probe_history.append(baseline_outcome.raw_probe)
        if not baseline_outcome.decision.passed:
            baseline_candidate, baseline_outcome = stabilize_failed_baseline(
                base_curve,
                failed_candidate=baseline_candidate,
                failed_outcome=baseline_outcome,
                runner=runner,
                gpu=gpu,
                q2rtx_config=q2rtx_config,
                probe_history=probe_history,
                discovery_summary=discovery_summary,
                settings=settings,
                log=log,
                event_callback=event_callback,
            )
        stable_probe = require_probe_summary(baseline_outcome)
        baseline_candidate = adjust_baseline_to_measured_clock(
            base_curve,
            candidate=baseline_candidate,
            stable_probe=stable_probe,
            gpu=gpu,
        )
        stable_history.append(stable_probe)
        recovery_budget_used_by_candidate_id = {
            final_choice_candidate_id(
                voltage_mv=int(baseline_candidate.voltage_mv),
                lock_clock_mhz=int(baseline_candidate.target_mhz),
            ): 0.0
        }
        write_verified_candidate(
            baseline_candidate,
            stable_probe,
            discovery_summary=discovery_summary,
        )
        log_user_stage(
            log,
            "Auto-UV3 baseline accepted",
            [
                f"Starting point: {baseline_candidate.target_mhz}MHz at {baseline_candidate.voltage_mv}mV.",
                "Next, Auto-UV3 will walk downward through real editable voltage bins.",
            ],
        )

        stable_candidate = baseline_candidate

        def probe_candidate(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
            outcome = runner.probe_sweep_candidate(
                candidate,
                stable_history=stable_history,
                phase_label="candidate",
            )
            if outcome.raw_probe is not None:
                probe_history.append(outcome.raw_probe)
            return outcome

        def accept_candidate(
            candidate: VfCurveCandidate,
            outcome: VoltageProbeOutcome,
        ) -> None:
            nonlocal stable_candidate, stable_probe
            summary = require_probe_summary(outcome)
            stable_candidate = candidate
            stable_probe = summary
            stable_history.append(summary)
            recovery_budget_used_by_candidate_id[
                final_choice_candidate_id(
                    voltage_mv=int(candidate.voltage_mv),
                    lock_clock_mhz=int(candidate.target_mhz),
                )
            ] = recovery_budget_used_from_label(candidate.label)
            write_verified_candidate(
                candidate,
                summary,
                discovery_summary=discovery_summary,
            )

        def record_passed_candidate(
            candidate: VfCurveCandidate,
            outcome: VoltageProbeOutcome,
        ) -> None:
            summary = require_probe_summary(outcome)
            stable_history.append(summary)
            recovery_budget_used_by_candidate_id[
                final_choice_candidate_id(
                    voltage_mv=int(candidate.voltage_mv),
                    lock_clock_mhz=int(candidate.target_mhz),
                )
            ] = recovery_budget_used_from_label(candidate.label)

        hooks = LowerVoltageSweepHooks(
            probe_candidate=probe_candidate,
            write_verified_candidate=accept_candidate,
            mark_unsafe_candidate=lambda _candidate, _outcome: None,
            record_passed_candidate=record_passed_candidate,
        )
        recovery_voltage_ceiling_mv = performance_recovery_voltage_ceiling_mv(
            gpu.translated_gpu_policy,
            auto_uv_mode=settings.auto_uv_mode,
            log=log,
        )
        user_stop_final_choice = False
        try:
            loop_result = run_lower_voltage_sweep_loop(
                base_curve,
                settings=AutoUvScanSettings(
                    start_voltage_mv=int(baseline_candidate.voltage_mv),
                    min_search_voltage_mv=min_search_voltage_mv(
                        start_voltage_mv=int(baseline_candidate.voltage_mv),
                        configured_max_drop_pct=float(settings.configured_max_drop_pct),
                    ),
                    preserve_base_below_mv=settings.preserve_base_below_mv,
                    baseline_core_clock_mhz=float(baseline_target.measured_clock_mhz),
                    min_core_clock_pct=float(settings.min_performance_core_clock_pct),
                    reference_actual_voltage_mv=stable_probe.avg_voltage_mv,
                    measured_clock_cap_mhz=float(baseline_target.measured_clock_mhz),
                    recovery_voltage_ceiling_mv=recovery_voltage_ceiling_mv,
                    recovery_budget_limit_pct=float(effective_recovery_budget_pct),
                    spend_remaining_clock_budget_at_voltage_floor=True,
                    allow_voltage_bump_for_floor_clock_recovery=(
                        settings.auto_uv_mode == AUTO_UV_MODE_PERFORMANCE
                    ),
                ),
                initial_stable_candidate=stable_candidate,
                hooks=hooks,
                unsafe_entries=unsafe_entries,
            )
            stable_candidate = loop_result.stable_candidate
            final_recovery_budget_used_pct = float(loop_result.state.recovery_budget.used_pct)
        except KeyboardInterrupt:
            if not bool(runtime_options.get("auto_uv_require_final_choice")):
                raise
            user_stop_final_choice = True
            clear_auto_uv_stop_request()
            log_phase(
                log,
                "auto-uv3",
                "user stop requested; offering past stable candidates for final verification",
            )
            final_recovery_budget_used_pct = recovery_budget_used_by_candidate_id.get(
                final_choice_candidate_id(
                    voltage_mv=int(stable_candidate.voltage_mv),
                    lock_clock_mhz=int(stable_candidate.target_mhz),
                ),
                0.0,
            )
        final_verification_duration_s = int(settings.final_verification_duration_s)
        final_stable_plan = stable_candidate.flattened_plan
        final_stable_voltage_mv = int(stable_candidate.voltage_mv)
        final_stable_lock_clock_mhz = int(stable_candidate.target_mhz)
        final_stable_probe = stable_probe
        if bool(runtime_options.get("auto_uv_require_final_choice")):
            (
                final_stable_plan,
                final_stable_voltage_mv,
                final_stable_lock_clock_mhz,
                selected_stable_probe,
                selected_final_verification_duration_s,
            ) = choose_final_verification_candidate(
                log=log,
                event_callback=event_callback,
                auto_uv_mode=settings.auto_uv_mode,
                base_probe=discovery_summary,
                stable_plan=final_stable_plan,
                stable_voltage_mv=int(final_stable_voltage_mv),
                stable_lock_clock_mhz=int(final_stable_lock_clock_mhz),
                stable_probe=final_stable_probe,
                stable_history=stable_history,
                base_curve=base_curve,
                final_verification_duration_s=int(final_verification_duration_s),
                initial_target_voltage_mv=int(baseline_candidate.voltage_mv),
                short_probe_base_duration_s=int(settings.short_probe_base_duration_s),
                request_reason=(
                    "user-stop" if bool(user_stop_final_choice) else "sweep-complete"
                ),
            )
            final_verification_duration_s = int(selected_final_verification_duration_s)
            if selected_stable_probe is not None:
                final_stable_probe = selected_stable_probe
            final_recovery_budget_used_pct = recovery_budget_used_by_candidate_id.get(
                final_choice_candidate_id(
                    voltage_mv=int(final_stable_voltage_mv),
                    lock_clock_mhz=int(final_stable_lock_clock_mhz),
                ),
                final_recovery_budget_used_pct,
            )

        return run_final_verification_and_save(
            probe_voltage_candidate=probe_voltage_candidate,
            probe_stabilization_search=find_upward_stable_final_candidate,
            build_voltage_scan_result=build_voltage_scan_result,
            curve_overclock_summary=curve_overclock_summary,
            log=log,
            reader=gpu.reader,
            stable_plan=final_stable_plan,
            stable_voltage_mv=int(final_stable_voltage_mv),
            stable_lock_clock_mhz=int(final_stable_lock_clock_mhz),
            stable_probe=final_stable_probe,
            stable_history=stable_history,
            probe_history=probe_history,
            q2rtx_config=q2rtx_config,
            final_verification_duration_s=int(final_verification_duration_s),
            source_result={"plan": base_curve, "translation_mode": "runtime-defaults"},
            start_voltage_mv=int(baseline_candidate.voltage_mv),
            measured_clock_mhz=float(baseline_target.measured_clock_mhz),
            nvml_session=gpu.live_voltage_reader,
            clock_ceiling=gpu.clock_ceiling,
            discovery_summary=discovery_summary,
            translated_gpu_policy=gpu.translated_gpu_policy,
            min_performance_core_clock_pct=float(settings.min_performance_core_clock_pct),
            runtime_default_plan=gpu.runtime_default_plan,
            final_clock_drop_margin_pct=float(settings.final_clock_drop_margin_pct),
            clock_bump_budget_limit_pct=float(effective_recovery_budget_pct),
            recovery_voltage_ceiling_mv=recovery_voltage_ceiling_mv,
            clock_bump_budget_used_pct=float(final_recovery_budget_used_pct),
            short_probe_base_duration_s=int(settings.short_probe_base_duration_s),
            timedemo_warmup_runs=int(timedemo_warmup_runs),
            event_callback=event_callback,
        )
    finally:
        cleanup_managed_q2rtx_processes(q2rtx_config, log=log)
        gpu.close()


def consume_crash_cache(*, log: Callable[[str], None]) -> list[dict]:
    interrupted = consume_interrupted_probe_crash_marker()
    if interrupted is not None:
        _path, unsafe_entry = interrupted
        log_phase(
            log,
            "crash-recovery",
            "previous auto-UV probe ended abruptly; "
            f"blacklisted={int(unsafe_entry['candidate_voltage_mv'])}mV "
            f"target={int(unsafe_entry['lock_clock_mhz'])}MHz",
        )
    return load_unsafe_voltage_blacklist()


def final_choice_candidate_id(*, voltage_mv: int, lock_clock_mhz: int) -> str:
    return f"{int(voltage_mv)}mv-{int(lock_clock_mhz)}mhz"


def recovery_budget_used_from_label(label: str) -> float:
    match = RECOVERY_BUDGET_LABEL_RE.search(str(label or ""))
    return 0.0 if match is None else float(match.group("used"))


def performance_recovery_voltage_ceiling_mv(
    gpu_policy: dict,
    *,
    auto_uv_mode: str,
    log: Callable[[str], None],
) -> int | None:
    if auto_uv_mode != AUTO_UV_MODE_PERFORMANCE:
        return None
    gpu_name = gpu_policy.get("gpu_name") if isinstance(gpu_policy, dict) else None
    target = uv_limit_profile_target_for_gpu(gpu_name, "performance")
    if target is None:
        return None
    voltage_mv = int(target.voltage_mv)
    log_phase(
        log,
        "auto-uv3",
        f"performance voltage recovery ceiling {target.gpu_family}: {voltage_mv}mV",
    )
    return voltage_mv


def run_discovery_probe(
    base_curve: list[dict],
    *,
    gpu,
    q2rtx_config: Q2RTXStabilityConfig,
    short_probe_base_duration_s: int,
    timedemo_warmup_runs: int,
    log: Callable[[str], None],
    event_callback: AutoUvEventCallback | None,
) -> tuple[AutoUvProbeSummary, object]:
    point = max(editable_base_vf_points(base_curve), key=lambda item: item.target_mhz)
    runner = Q2RtxCudaProbeRunner(
        reader=gpu.reader,
        live_voltage_reader=gpu.live_voltage_reader,
        q2rtx_config=q2rtx_config,
        runtime_default_plan=gpu.runtime_default_plan,
        power_limit_w=gpu.power_limit_w,
        start_voltage_mv=int(point.voltage_mv),
        baseline_clock_mhz=None,
        min_performance_core_clock_pct=90.0,
        short_probe_base_duration_s=int(short_probe_base_duration_s),
        timedemo_warmup_runs=int(timedemo_warmup_runs),
        log=log,
        event_callback=event_callback,
    )
    emit_ui_json_event(
        event_callback,
        "probe_start",
        stage="base-baseline",
        voltage_mv=int(point.voltage_mv),
        clock_mhz=int(point.target_mhz),
        label="base default curve",
    )
    summary, result = runner.probe_default_curve(
        base_curve=base_curve,
        label_voltage_mv=int(point.voltage_mv),
        label_clock_mhz=int(point.target_mhz),
    )
    emit_ui_json_event(
        event_callback,
        "probe_result",
        **probe_summary_ui_payload(
            summary,
            stage="base-baseline",
            decision="pass" if getattr(result, "success", False) else "fail",
            reason=str(getattr(result, "reason", "")),
        ),
    )
    log_benchmark(log, phase="discover", probe=summary)
    return summary, result


def build_loaded_baseline_candidate(
    base_curve: list[dict],
    *,
    discovery_summary: AutoUvProbeSummary,
    discovery_result: object,
    power_limit_w: int | None,
) -> tuple[VfCurveCandidate, object]:
    target = choose_base_load_flatten_target(
        base_curve,
        list(getattr(discovery_result, "telemetry_samples", []) or []),
        power_limit_w=power_limit_w,
        fallback_clock_mhz=discovery_summary.avg_core_clock_mhz,
    )
    voltage_band = derive_loaded_voltage_band(
        list(getattr(discovery_result, "telemetry_samples", []) or []),
        power_limit_w=power_limit_w,
        use_power_limit_floor=True,
    )
    fallback_voltage_mv = lock_voltage_for_target_clock(
        base_curve,
        int(target.target_clock_mhz),
    )
    start_voltage_mv = nearest_editable_voltage_bin(
        base_curve,
        int(voltage_band.average_mv or fallback_voltage_mv),
    )
    plan = build_flattened_plan(
        base_curve,
        lock_clock_mhz=int(target.target_clock_mhz),
        candidate_voltage_mv=int(start_voltage_mv),
    )
    return (
        VfCurveCandidate(
            label="baseline-loaded-flattened-curve",
            voltage_mv=int(start_voltage_mv),
            target_mhz=int(target.target_clock_mhz),
            flattened_plan=plan,
        ),
        target,
    )


def stabilize_failed_baseline(
    base_curve: list[dict],
    *,
    failed_candidate: VfCurveCandidate,
    failed_outcome: VoltageProbeOutcome,
    runner: Q2RtxCudaProbeRunner,
    gpu,
    q2rtx_config: Q2RTXStabilityConfig,
    probe_history: list[AutoUvProbeSummary],
    discovery_summary: AutoUvProbeSummary,
    settings,
    log: Callable[[str], None],
    event_callback: AutoUvEventCallback | None,
) -> tuple[VfCurveCandidate, VoltageProbeOutcome]:
    require_probe_summary(failed_outcome)
    recovery_candidate, recovery_outcome = find_upward_stable_baseline_candidate(
        base_curve,
        failed_candidate=failed_candidate,
        minimum_candidate_voltage_mv=next_higher_editable_voltage_bin(
            base_curve,
            int(failed_candidate.voltage_mv),
        ),
        runner=runner,
        clock_ceiling=gpu.clock_ceiling,
        probe_history=probe_history,
        discovery_summary=discovery_summary,
        log=log,
    )
    if recovery_candidate is None or recovery_outcome is None:
        raise AutoUvError(
            "baseline flattened curve failed and upward stabilization found no stable point"
        )
    return recovery_candidate, recovery_outcome


def adjust_baseline_to_measured_clock(
    base_curve: list[dict],
    *,
    candidate: VfCurveCandidate,
    stable_probe: AutoUvProbeSummary,
    gpu,
) -> VfCurveCandidate:
    measured_target_mhz = lock_clock_from_probe_loaded_clock(
        base_curve,
        probe=stable_probe,
        previous_lock_clock_mhz=int(candidate.target_mhz),
    )
    if int(measured_target_mhz) == int(candidate.target_mhz):
        return candidate
    plan = build_flattened_plan(
        base_curve,
        lock_clock_mhz=int(measured_target_mhz),
        candidate_voltage_mv=int(candidate.voltage_mv),
    )
    if gpu.clock_ceiling is not None:
        gpu.clock_ceiling.retarget(
            lock_clock_mhz=int(measured_target_mhz),
            lock_voltage_mv=int(candidate.voltage_mv),
        )
    return VfCurveCandidate(
        label="baseline-measured-clock-adjusted",
        voltage_mv=int(candidate.voltage_mv),
        target_mhz=int(measured_target_mhz),
        flattened_plan=plan,
    )


def write_verified_candidate(
    candidate: VfCurveCandidate,
    probe: AutoUvProbeSummary,
    *,
    discovery_summary: AutoUvProbeSummary,
) -> None:
    write_latest_verified_candidate(
        plan=candidate.flattened_plan,
        lock_clock_mhz=int(candidate.target_mhz),
        voltage_mv=int(candidate.voltage_mv),
        probe=probe,
        base_probe=discovery_summary,
    )


def require_probe_summary(outcome: VoltageProbeOutcome) -> AutoUvProbeSummary:
    if outcome.raw_probe is None:
        raise AutoUvError("Auto-UV3 probe outcome did not include a probe summary")
    return outcome.raw_probe


def min_search_voltage_mv(*, start_voltage_mv: int, configured_max_drop_pct: float) -> int:
    return max(
        0,
        int(
            round(
                float(start_voltage_mv)
                * (1.0 - (float(configured_max_drop_pct) / 100.0))
            )
        ),
    )
