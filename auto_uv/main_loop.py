"""Run the top-level voltage-frequency undervolt main loop.

This keeps phase order readable: setup, base-load probe, preset undervolt loop,
user final choice, final verification, and cleanup.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, Callable, cast

from stability.q2rtx.models import Q2RTXStabilityConfig
from stability.q2rtx.process_harness import cleanup_managed_q2rtx_processes

from auto_uv.domain.types import (
    AutoUvError,
    AutoUvFinalChoiceDiscarded,
    AutoUvPowerLimitApplyError,
    AutoUvProbeSummary,
    AutoUvVoltageScanResult,
    BaseLoadTarget,
    VfCurveCandidate,
)
from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.domain.console_log import log_phase, log_user_stage
from auto_uv.domain.scan_result import build_voltage_scan_result
from auto_uv.domain.user_options import AUTO_UV_DEFAULTS, AUTO_UV_METRIC_TUNING
from auto_uv.shared.positive_int import positive_int
from auto_uv.run.baseline_probe import (
    adjust_baseline_to_measured_clock,
    build_loaded_baseline_candidate,
    baseline_load_reference_power_limit_w,
    require_probe_summary,
    retarget_clock_ceiling_for_candidate,
    run_discovery_probe,
    run_discovery_probe_with_runner,
    write_verified_candidate,
)
from auto_uv.run.crash_recovery import (
    _float_or_none,
    append_unique_probe_summary,
    auto_uv_run_marker_details,
    auto_uv_run_profile_tier,
    base_probe_summary_from_candidate_record,
    consume_crash_cache,
    crash_recovery_decision,
    crash_recovery_entry_from_cache,
    final_choice_request_recovery_records,
    next_safer_recovery_candidate_id,
    probe_summary_from_candidate_record,
    recovery_candidate_records_for_failed_run,
    recovery_initial_target_voltage_mv,
    replay_recovered_resume_probe_rows,
)
from auto_uv.curve.base_vf_curve_validation import validate_base_vf_curve
from ui.features.auto_uv.candidate_choice import (
    candidate_records_from_history,
    choose_next_final_verification_candidate_after_failure,
    choose_final_verification_candidate,
    choose_recovery_final_verification_candidate,
)
from auto_uv.efficiency_tune.voltage_floor import min_search_voltage_mv
from auto_uv.gpu.gpu_vf_curve_applier import open_live_gpu_vf_curve_applier
from auto_uv.base_uv_loop import BaseUvLoopIO
from auto_uv.balanced_uv_loop import run_balanced_uv_loop
from auto_uv.efficiency_uv_loop import run_efficiency_uv_loop
from auto_uv.performance_uv_loop import (
    run_performance_uv_loop,
    select_power_bound_clock_reclaim_candidate,
    select_performance_auto_oc_candidate,
)
from auto_uv.curve.measured_probe_lock_clock import (
    probe_indicates_power_saturation,
)
from auto_uv.probes.runner import AutoUvProbeRunner
from auto_uv.probes.runtime_guardrails import (
    probe_failure_should_mark_voltage_unsafe,
)
from auto_uv.probes.voltage_probe import probe_voltage_candidate
from auto_uv.run.scan_runtime_settings import (
    adaptive_tier_clock_drop_margin_pct,
    adaptive_tier_option,
    final_verification_duration_s as resolve_final_verification_duration_s,
    read_scan_runtime_settings,
)
from auto_uv.gpu.memory_clock_offset_user_option import (
    driver_memory_offset_limit_mhz,
)
from auto_uv.scan_mode.efficiency_fps_per_w_policy import (
    best_efficiency_candidate_index,
    derive_efficiency_stop_streak_from_fps_variance,
)
from auto_uv.domain.events import AutoUvEventCallback, emit_auto_uv_event
from auto_uv.persistence.verified_candidate_result_file import (
    read_verified_candidates,
)
from auto_uv.persistence.auto_uv_persisted_json_files import (
    auto_uv_stop_request_aborts_final_choice,
    clear_auto_uv_stop_request,
)
from auto_uv.curve.vf_curve_flattening import build_flatten_target_for_plan
from auto_uv.probes.event_payload import vf_curve_event_points
from auto_uv.run.voltage_sweep_state import (
    LowerVoltageSweepEvent,
    LowerVoltageSweepResult,
    VoltageProbeOutcome,
)
from auto_uv.final_verification.crash_marker import memory_offset_from_gpu_policy
from auto_uv.final_verification.main_loop import run_final_verification_and_save
from auto_uv.scan_mode.auto_uv_mode import (
    AUTO_UV_MODE_ADAPTIVE,
    AUTO_UV_MODE_BALANCED,
    AUTO_UV_MODE_EFFICIENCY,
    AUTO_UV_MODE_PERFORMANCE,
)
from auto_uv.scan_mode.uv_limits import uv_limit_power_limit_pct_for_gpu


ADAPTIVE_BASELINE_REUSE_CLOCK_TOLERANCE_MHZ = 15
ADAPTIVE_BASELINE_REUSE_VOLTAGE_TOLERANCE_MV = 10


@dataclass(frozen=True, slots=True)
class FinalScanCandidate:
    plan: list[dict]
    voltage_mv: int
    lock_clock_mhz: int
    probe: AutoUvProbeSummary | None
    verification_duration_s: int
    auto_oc_metadata: dict
    tail_rise_bins: int


@dataclass(frozen=True, slots=True)
class AdaptiveTierBaseline:
    runner: AutoUvProbeRunner
    candidate: VfCurveCandidate
    target: BaseLoadTarget
    outcome: VoltageProbeOutcome
    stable_probe: AutoUvProbeSummary
    discovery_summary: AutoUvProbeSummary
    min_search_voltage_mv: int


def run_voltage_frequency_undervolt_main_loop(
    *,
    gpu_index: int,
    runtime_options: dict,
    q2rtx_config: Q2RTXStabilityConfig,
    log: Callable[[str], None] = print,
    event_callback: AutoUvEventCallback | None = None,
) -> AutoUvVoltageScanResult:
    unsafe_entries = consume_crash_cache(log=log)
    crash_recovery_entry = crash_recovery_entry_from_cache(unsafe_entries)
    gpu = open_live_gpu_vf_curve_applier(
        gpu_index=int(gpu_index),
        runtime_options=runtime_options,
        log=log,
    )
    try:
        settings = read_scan_runtime_settings(
            runtime_options,
            q2rtx_config,
            gpu_name=gpu.translated_gpu_policy.get("gpu_name"),
        )
        q2rtx_config = settings.q2rtx_config
        # NOTE: concurrent q2rtx+CUDA (Q2RTXStabilityConfig.companion_concurrent)
        # stays OFF for adaptive scans until the baseline probe runs the same
        # concurrent mix and companion durations are aligned — enabling it here
        # would measure candidates under compute contention against an
        # uncontended baseline and leak into final verification's serialized
        # duration split.
        final_verification_duration_s = int(settings.final_verification_duration_s)
        tail_rise_bins = int(getattr(settings, "tail_rise_bins", 0))
        run_profile_tier = auto_uv_run_profile_tier(
            runtime_options,
            settings,
            tail_rise_bins=int(tail_rise_bins),
        )
        descent_tail_rise_bins = int(tail_rise_bins)
        cleanup_managed_q2rtx_processes(q2rtx_config, log=log)
        base_curve = list(gpu.runtime_default_plan)
        validate_base_vf_curve(base_curve)
        emit_auto_uv_event(
            event_callback,
            "base_curve",
            points=vf_curve_event_points(base_curve),
        )
        # The applied NVML memory offset is a transfer rate (MT/s); the realized
        # memory clock moves by half. Surface the clock delta so the status bar
        # can show the currently applied memory offset for the whole run.
        applied_memory_offset_mt_s = int(
            gpu.translated_gpu_policy.get("mem_clk_vf_offset_mhz") or 0
        )
        emit_auto_uv_event(
            event_callback,
            "memory_offset_applied",
            offset_mt_s=int(applied_memory_offset_mt_s),
            offset_mhz=int(applied_memory_offset_mt_s) // 2,
        )
        pending_recovery_selection = None
        if bool(runtime_options.get("auto_uv_require_final_choice")) and isinstance(
            crash_recovery_entry,
            dict,
        ):
            recovery_candidates = recovery_candidate_records_for_failed_run(
                read_verified_candidates(),
                crash_recovery_entry=crash_recovery_entry,
                target_profile_tier=run_profile_tier,
            )
            failed_recovery_voltage_mv = positive_int(
                crash_recovery_entry.get("candidate_voltage_mv")
            )
            recovery_decision = crash_recovery_decision(crash_recovery_entry)
            if str(crash_recovery_entry.get("phase") or "") == "final-verify":
                recovery_candidates = final_choice_request_recovery_records(
                    base_curve,
                    fallback_records=recovery_candidates,
                    failed_voltage_mv=failed_recovery_voltage_mv,
                    failed_lock_clock_mhz=positive_int(
                        crash_recovery_entry.get("lock_clock_mhz")
                    ),
                    auto_uv_mode=settings.auto_uv_mode,
                    tail_rise_bins=int(tail_rise_bins),
                )
                log_phase(
                    log,
                    "crash-recovery",
                    "offering saved candidates after failed final verification "
                    f"failed={recovery_decision.get('candidate_voltage_mv')}mV@"
                    f"{recovery_decision.get('lock_clock_mhz')}MHz "
                    f"tier={run_profile_tier or 'unknown'} "
                    f"decision={recovery_decision.get('decision')}",
                )
                pending_recovery_selection = (
                    choose_next_final_verification_candidate_after_failure(
                        log=log,
                        event_callback=event_callback,
                        auto_uv_mode=settings.auto_uv_mode,
                        base_probe=None,
                        candidate_records=recovery_candidates,
                        stable_history=None,
                        failed_voltage_mv=int(failed_recovery_voltage_mv or 0),
                        final_verification_duration_s=int(
                            final_verification_duration_s
                        ),
                        initial_target_voltage_mv=recovery_initial_target_voltage_mv(
                            recovery_candidates,
                            fallback_voltage_mv=failed_recovery_voltage_mv,
                        ),
                        short_probe_base_duration_s=int(
                            settings.short_probe_base_duration_s
                        ),
                        recovery_decision=recovery_decision,
                        min_core_clock_pct=float(
                            settings.min_performance_core_clock_pct
                        ),
                    )
                )
            else:
                recovery_default_id = next_safer_recovery_candidate_id(
                    recovery_candidates,
                    failed_voltage_mv=failed_recovery_voltage_mv,
                    auto_uv_mode=settings.auto_uv_mode,
                )
                if recovery_default_id:
                    log_phase(
                        log,
                        "crash-recovery",
                        "offering saved candidates before discovery "
                        f"failed={recovery_decision.get('candidate_voltage_mv')}mV@"
                        f"{recovery_decision.get('lock_clock_mhz')}MHz "
                        f"tier={run_profile_tier or 'unknown'} "
                        f"default={recovery_default_id} "
                        f"decision={recovery_decision.get('decision')}",
                    )
                    pending_recovery_selection = (
                        choose_recovery_final_verification_candidate(
                            log=log,
                            event_callback=event_callback,
                            auto_uv_mode=settings.auto_uv_mode,
                            base_probe=None,
                            candidate_records=recovery_candidates,
                            default_candidate_id=recovery_default_id,
                            final_verification_duration_s=int(
                                final_verification_duration_s
                            ),
                            initial_target_voltage_mv=recovery_initial_target_voltage_mv(
                                recovery_candidates,
                                fallback_voltage_mv=failed_recovery_voltage_mv,
                            ),
                            short_probe_base_duration_s=int(
                                settings.short_probe_base_duration_s
                            ),
                            recovery_decision=recovery_decision,
                            min_core_clock_pct=float(
                                settings.min_performance_core_clock_pct
                            ),
                        )
                    )
            if pending_recovery_selection is not None:
                log_phase(
                    log,
                    "crash-recovery",
                    "saved candidate selected for recovery",
                )
            else:
                log_phase(
                    log,
                    "crash-recovery",
                    "no saved recovery candidate selected; starting a new scan",
                )
        log_phase(
            log,
            "auto-uv",
            "Auto-UV voltage-frequency main loop enabled "
            f"tail-rise-bins={int(tail_rise_bins)}",
        )
        if pending_recovery_selection is not None:
            return run_recovered_previous_crash_selection(
                pending_recovery_selection=pending_recovery_selection,
                base_curve=base_curve,
                gpu=gpu,
                settings=settings,
                q2rtx_config=q2rtx_config,
                runtime_options=runtime_options,
                final_verification_duration_s=int(final_verification_duration_s),
                tail_rise_bins=int(tail_rise_bins),
                log=log,
                event_callback=event_callback,
            )
        if settings.auto_uv_mode == AUTO_UV_MODE_ADAPTIVE:
            # The initial measurements ARE Efficiency's baseline. Establish
            # its full policy before discovery, including CLI table defaults.
            tail_rise_bins = adaptive_tier_descent_tail_rise_bins(
                AUTO_UV_MODE_EFFICIENCY
            )
            descent_tail_rise_bins = int(tail_rise_bins)
            apply_adaptive_tier_memory_offset(
                gpu,
                tier_mode=AUTO_UV_MODE_EFFICIENCY,
                runtime_options=runtime_options,
                fallback_offset_mhz=scan_wide_memory_offset_mhz(runtime_options),
                limit_mhz=driver_memory_offset_limit_mhz(gpu.policy_controller),
                event_callback=event_callback,
                log=log,
            )
            request_adaptive_tier_power_limit(
                gpu, tier_mode=AUTO_UV_MODE_EFFICIENCY, runtime_options=runtime_options
            )
            apply_pending_power_limit(
                gpu, log=log, purpose="adaptive efficiency baseline"
            )
        discovery_summary, discovery_result = run_discovery_probe(
            base_curve,
            gpu=gpu,
            q2rtx_config=q2rtx_config,
            short_probe_base_duration_s=int(settings.short_probe_base_duration_s),
            log=log,
            event_callback=event_callback,
            marker_details=auto_uv_run_marker_details(
                runtime_options,
                settings,
                tail_rise_bins=int(tail_rise_bins),
                profile_tier=run_profile_tier,
            ),
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
            power_limit_w=baseline_load_reference_power_limit_w(gpu),
            tail_rise_bins=int(tail_rise_bins),
        )
        gpu.start_clock_ceiling(
            build_flatten_target_for_plan(
                base_curve,
                baseline_candidate.flattened_plan,
                lock_clock_mhz=int(baseline_candidate.target_mhz),
                lock_voltage_mv=int(baseline_candidate.voltage_mv),
                tail_rise_bins=int(tail_rise_bins),
            )
        )
        runner = AutoUvProbeRunner(
            reader=gpu.reader,
            live_voltage_reader=gpu.live_voltage_reader,
            q2rtx_config=q2rtx_config,
            runtime_default_plan=gpu.runtime_default_plan,
            power_limit_w=gpu.power_limit_w,
            start_voltage_mv=int(baseline_candidate.voltage_mv),
            baseline_clock_mhz=float(baseline_target.measured_clock_mhz),
            min_performance_core_clock_pct=float(settings.min_performance_core_clock_pct),
            short_probe_base_duration_s=int(settings.short_probe_base_duration_s),
            log=log,
            event_callback=event_callback,
            marker_details=auto_uv_run_marker_details(
                runtime_options,
                settings,
                tail_rise_bins=int(tail_rise_bins),
                profile_tier=run_profile_tier,
            ),
        )
        probe_history: list[AutoUvProbeSummary] = [discovery_summary]
        stable_history: list[AutoUvProbeSummary] = []
        baseline_outcome = runner.probe_baseline_candidate(baseline_candidate)
        if baseline_outcome.raw_probe is not None:
            probe_history.append(baseline_outcome.raw_probe)
        if not baseline_outcome.decision.passed:
            raise AutoUvError(
                "baseline flattened curve failed the Q2RTX probe: "
                f"{baseline_outcome.decision.reason}"
            )
        stable_probe = require_probe_summary(baseline_outcome)
        baseline_candidate = adjust_baseline_to_measured_clock(
            base_curve,
            candidate=baseline_candidate,
            stable_probe=stable_probe,
            gpu=gpu,
            tail_rise_bins=int(tail_rise_bins),
        )
        stable_history.append(stable_probe)
        write_verified_candidate(
            baseline_candidate,
            stable_probe,
            discovery_summary=discovery_summary,
            tail_rise_bins=int(tail_rise_bins),
            configured_power_limit_w=positive_int(gpu.power_limit_w),
        )
        log_user_stage(
            log,
            "Auto-UV baseline accepted",
            [
                f"Starting point: {baseline_candidate.target_mhz}MHz at {baseline_candidate.voltage_mv}mV.",
                "Next, Auto-UV will walk downward through real editable voltage bins.",
            ],
        )
        efficiency_stop_streak_default = derive_efficiency_stop_streak_from_fps_variance(
            stable_probe,
            configured_streak=int(
                getattr(
                    settings,
                    "efficiency_stop_streak",
                    AUTO_UV_DEFAULTS.efficiency_stop_streak,
                )
            ),
            derive=bool(getattr(settings, "derive_efficiency_stop_streak", True)),
            high_variance_threshold_pct=float(
                AUTO_UV_METRIC_TUNING.efficiency_stop_high_fps_variance_pct
            ),
            low_variance_streak=int(
                AUTO_UV_METRIC_TUNING.efficiency_stop_low_variance_streak
            ),
            high_variance_streak=int(
                AUTO_UV_METRIC_TUNING.efficiency_stop_high_variance_streak
            ),
        )
        emit_auto_uv_event(
            event_callback,
            "derived_defaults",
            efficiency_stop_streak=int(efficiency_stop_streak_default.value),
            efficiency_stop_streak_source=str(efficiency_stop_streak_default.source),
            fps_variance_pct=efficiency_stop_streak_default.fps_variance_pct,
            fps_variance_threshold_pct=float(
                efficiency_stop_streak_default.threshold_pct
            ),
        )
        log_phase(
            log,
            "efficiency",
            "stop-streak="
            f"{int(efficiency_stop_streak_default.value)} "
            f"source={efficiency_stop_streak_default.source} "
            "fps_variance_pct="
            f"{_format_optional_pct(efficiency_stop_streak_default.fps_variance_pct)} "
            f"threshold={float(efficiency_stop_streak_default.threshold_pct):.2f}%",
        )

        stable_candidate = baseline_candidate
        effective_min_search_voltage_mv = min_search_voltage_mv(
            start_voltage_mv=int(baseline_candidate.voltage_mv),
            configured_min_voltage_mv=settings.configured_min_voltage_mv,
            configured_max_drop_pct=float(settings.configured_max_drop_pct),
            gpu_name=gpu.translated_gpu_policy.get("gpu_name"),
        )

        def probe_candidate(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
            retarget_clock_ceiling_for_candidate(gpu.clock_ceiling, candidate)
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
            write_verified_candidate(
                candidate,
                summary,
                discovery_summary=discovery_summary,
                tail_rise_bins=int(descent_tail_rise_bins),
                configured_power_limit_w=positive_int(gpu.power_limit_w),
            )

        def record_passed_candidate(
            candidate: VfCurveCandidate,
            outcome: VoltageProbeOutcome,
        ) -> None:
            summary = require_probe_summary(outcome)
            stable_history.append(summary)

        loop_io = BaseUvLoopIO(
            probe_candidate=probe_candidate,
            write_verified_candidate=accept_candidate,
            mark_unsafe_candidate=lambda _candidate, _outcome: None,
            record_passed_candidate=record_passed_candidate,
        )
        resumed_previous_crash = False

        def finish_with_final_verification(
            *,
            final_stable_plan: list[dict],
            final_stable_voltage_mv: int,
            final_stable_lock_clock_mhz: int,
            final_stable_probe: AutoUvProbeSummary | None,
            selected_final_verification_duration_s: int,
            final_tail_rise_bins: int,
            final_auto_oc_metadata: dict | None = None,
            final_auto_uv_mode: str | None = None,
            final_profile_tier: str | None = None,
            # Adaptive tiers verify against THEIR clock-drop margin, not the
            # scan-wide one; None keeps the scan-wide settings values.
            final_min_core_clock_pct: float | None = None,
            final_clock_drop_margin_pct: float | None = None,
            final_stable_history: list[AutoUvProbeSummary] | None = None,
            final_discovery_summary: AutoUvProbeSummary | None = None,
            final_baseline_candidate: VfCurveCandidate | None = None,
            final_measured_baseline_clock_mhz: float | None = None,
        ):
            apply_pending_power_limit(gpu, log=log)
            selected_stable_history = (
                final_stable_history
                if final_stable_history is not None
                else stable_history
            )
            selected_discovery_summary = (
                final_discovery_summary
                if final_discovery_summary is not None
                else discovery_summary
            )
            selected_baseline_candidate = (
                final_baseline_candidate
                if final_baseline_candidate is not None
                else baseline_candidate
            )
            selected_measured_baseline_clock_mhz = (
                float(final_measured_baseline_clock_mhz)
                if final_measured_baseline_clock_mhz is not None
                else float(baseline_target.measured_clock_mhz)
            )
            return run_final_verification_and_save(
                probe_voltage_candidate=probe_voltage_candidate,
                build_voltage_scan_result=build_voltage_scan_result,
                log=log,
                reader=gpu.reader,
                stable_plan=final_stable_plan,
                stable_voltage_mv=int(final_stable_voltage_mv),
                stable_lock_clock_mhz=int(final_stable_lock_clock_mhz),
                stable_probe=final_stable_probe,
                stable_history=selected_stable_history,
                probe_history=probe_history,
                q2rtx_config=q2rtx_config,
                final_verification_duration_s=int(
                    selected_final_verification_duration_s
                ),
                start_voltage_mv=int(selected_baseline_candidate.voltage_mv),
                measured_clock_mhz=float(selected_measured_baseline_clock_mhz),
                nvml_session=gpu.live_voltage_reader,
                clock_ceiling=gpu.clock_ceiling,
                discovery_summary=selected_discovery_summary,
                translated_gpu_policy=gpu.translated_gpu_policy,
                gpu_identity=getattr(gpu, "gpu_identity", {}),
                min_performance_core_clock_pct=float(
                    final_min_core_clock_pct
                    if final_min_core_clock_pct is not None
                    else settings.min_performance_core_clock_pct
                ),
                runtime_default_plan=gpu.runtime_default_plan,
                final_clock_drop_margin_pct=float(
                    final_clock_drop_margin_pct
                    if final_clock_drop_margin_pct is not None
                    else settings.final_clock_drop_margin_pct
                ),
                tail_rise_bins=int(final_tail_rise_bins),
                auto_uv_mode=str(final_auto_uv_mode or settings.auto_uv_mode),
                generated_profile_tier=(
                    final_profile_tier
                    if final_profile_tier is not None
                    else run_profile_tier
                ),
                auto_oc_metadata=dict(final_auto_oc_metadata or {}),
                event_callback=event_callback,
            )

        user_stop_final_choice = False
        adaptive_tier_phase = False
        try:
            if not bool(resumed_previous_crash):
                base_loop_settings = AutoUvScanSettings(
                    start_voltage_mv=int(baseline_candidate.voltage_mv),
                    min_search_voltage_mv=int(effective_min_search_voltage_mv),
                    baseline_core_clock_mhz=float(baseline_target.measured_clock_mhz),
                    auto_uv_mode=settings.auto_uv_mode,
                    min_core_clock_pct=float(settings.min_performance_core_clock_pct),
                    reference_actual_voltage_mv=stable_probe.avg_voltage_mv,
                    efficiency_stop_streak=int(efficiency_stop_streak_default.value),
                    min_efficiency_stop_voltage_drop_pct=float(
                        getattr(
                            settings,
                            "min_efficiency_stop_voltage_drop_pct",
                            10.0,
                        )
                    ),
                    tail_rise_bins=int(descent_tail_rise_bins),
                )
                initial_stable_outcome = VoltageProbeOutcome(
                    decision=baseline_outcome.decision,
                    measured_core_clock_mhz=stable_probe.avg_core_clock_mhz,
                    measured_voltage_mv=stable_probe.avg_voltage_mv,
                    raw_probe=stable_probe,
                    raw_result=baseline_outcome.raw_result,
                )
                if settings.auto_uv_mode == AUTO_UV_MODE_ADAPTIVE:
                    # A stop anywhere in the adaptive run must abort cleanly:
                    # the classic user-stop final-choice fallback would save a
                    # fourth, tier-less profile shadowing the confirmed ones,
                    # so the guard is armed before the first tier's descent.
                    adaptive_tier_phase = True
                    baseline_cache = {
                        (
                            positive_int(gpu.power_limit_w),
                            memory_offset_from_gpu_policy(gpu.translated_gpu_policy)
                            or 0,
                            int(tail_rise_bins),
                        ): (
                            AUTO_UV_MODE_EFFICIENCY,
                            AdaptiveTierBaseline(
                                runner=runner,
                                candidate=baseline_candidate,
                                target=baseline_target,
                                outcome=initial_stable_outcome,
                                stable_probe=stable_probe,
                                discovery_summary=discovery_summary,
                                min_search_voltage_mv=int(
                                    effective_min_search_voltage_mv
                                ),
                            ),
                        )
                    }

                    def configure_tier_probe_runner(
                        min_core_clock_pct: float,
                    ) -> AutoUvProbeRunner:
                        # Rebinds the closed-over runner so probe_candidate
                        # evaluates this tier's probes against the tier's own
                        # clock floor (the runner dataclass is frozen).
                        nonlocal runner
                        runner = replace(
                            runner,
                            power_limit_w=positive_int(gpu.power_limit_w),
                            min_performance_core_clock_pct=float(min_core_clock_pct),
                        )
                        return runner

                    def prepare_tier_baseline(
                        *,
                        tier_mode: str,
                        tier_runner: AutoUvProbeRunner,
                        tier_tail_rise_bins: int,
                    ) -> AdaptiveTierBaseline:
                        policy_key = (
                            positive_int(gpu.power_limit_w),
                            memory_offset_from_gpu_policy(gpu.translated_gpu_policy)
                            or 0,
                            int(tier_tail_rise_bins),
                        )
                        cached = baseline_cache.get(policy_key)
                        if cached is not None:
                            source_tier, measured = cached
                            # Baseline probes establish the reference and do
                            # not enforce a tier's clock floor. Rebind the
                            # runner so subsequent probes use this tier's own
                            # allowance against the shared measured reference.
                            prepared = replace(
                                measured,
                                runner=replace(
                                    tier_runner,
                                    start_voltage_mv=int(measured.candidate.voltage_mv),
                                    baseline_clock_mhz=float(
                                        measured.target.measured_clock_mhz
                                    ),
                                ),
                            )
                            retarget_clock_ceiling_for_candidate(
                                gpu.clock_ceiling, prepared.candidate
                            )
                            log_phase(
                                log,
                                "auto-uv",
                                f"adaptive {tier_mode} reusing {source_tier} stock "
                                "and flattened baselines "
                                f"power-limit={_format_power_limit_w(gpu.power_limit_w)}",
                            )
                            emit_auto_uv_event(
                                event_callback,
                                "tier_baseline_reused",
                                tier=tier_mode,
                                source_tier=source_tier,
                                power_limit_w=positive_int(gpu.power_limit_w),
                            )
                            return prepared
                        # A stock baseline must not inherit the dynamic core
                        # ceiling from the previous flattened candidate.
                        if gpu.clock_ceiling is not None:
                            gpu.clock_ceiling.close()
                        tier_discovery_summary, tier_discovery_result = (
                            run_discovery_probe_with_runner(
                                base_curve,
                                runner=tier_runner,
                                log=log,
                                event_callback=event_callback,
                                label=f"{str(tier_mode)} stock curve",
                            )
                        )
                        probe_history.append(tier_discovery_summary)
                        if not bool(getattr(tier_discovery_result, "success", False)):
                            raise AutoUvError(
                                f"adaptive {str(tier_mode)} stock baseline failed "
                                "the Q2RTX probe: "
                                f"{getattr(tier_discovery_result, 'reason', 'unknown')}"
                            )
                        tier_baseline_candidate, tier_baseline_target = (
                            build_loaded_baseline_candidate(
                                base_curve,
                                discovery_summary=tier_discovery_summary,
                                discovery_result=tier_discovery_result,
                                power_limit_w=positive_int(gpu.power_limit_w),
                                tail_rise_bins=int(tier_tail_rise_bins),
                            )
                        )
                        retarget_clock_ceiling_for_candidate(
                            gpu.clock_ceiling,
                            tier_baseline_candidate,
                        )
                        tier_runner = replace(
                            tier_runner,
                            power_limit_w=positive_int(gpu.power_limit_w),
                            start_voltage_mv=int(tier_baseline_candidate.voltage_mv),
                            baseline_clock_mhz=float(
                                tier_baseline_target.measured_clock_mhz
                            ),
                        )
                        tier_baseline_outcome = tier_runner.probe_baseline_candidate(
                            tier_baseline_candidate
                        )
                        if tier_baseline_outcome.raw_probe is not None:
                            probe_history.append(tier_baseline_outcome.raw_probe)
                        if not tier_baseline_outcome.decision.passed:
                            raise AutoUvError(
                                f"adaptive {str(tier_mode)} flattened baseline "
                                "failed the Q2RTX probe: "
                                f"{tier_baseline_outcome.decision.reason}"
                            )
                        tier_stable_probe = require_probe_summary(
                            tier_baseline_outcome
                        )
                        tier_baseline_candidate = adjust_baseline_to_measured_clock(
                            base_curve,
                            candidate=tier_baseline_candidate,
                            stable_probe=tier_stable_probe,
                            gpu=gpu,
                            tail_rise_bins=int(tier_tail_rise_bins),
                        )
                        write_verified_candidate(
                            tier_baseline_candidate,
                            tier_stable_probe,
                            discovery_summary=tier_discovery_summary,
                            tail_rise_bins=int(tier_tail_rise_bins),
                            configured_power_limit_w=positive_int(
                                gpu.power_limit_w
                            ),
                        )
                        tier_min_search_voltage_mv = min_search_voltage_mv(
                            start_voltage_mv=int(tier_baseline_candidate.voltage_mv),
                            configured_min_voltage_mv=settings.configured_min_voltage_mv,
                            configured_max_drop_pct=float(
                                settings.configured_max_drop_pct
                            ),
                            gpu_name=gpu.translated_gpu_policy.get("gpu_name"),
                        )
                        log_phase(
                            log,
                            "auto-uv",
                            f"adaptive {str(tier_mode)} capped baseline accepted "
                            f"{int(tier_baseline_candidate.voltage_mv)}mV@"
                            f"{int(tier_baseline_candidate.target_mhz)}MHz "
                            f"power-limit={_format_power_limit_w(gpu.power_limit_w)}",
                        )
                        prepared = AdaptiveTierBaseline(
                            runner=tier_runner,
                            candidate=tier_baseline_candidate,
                            target=tier_baseline_target,
                            outcome=tier_baseline_outcome,
                            stable_probe=tier_stable_probe,
                            discovery_summary=tier_discovery_summary,
                            min_search_voltage_mv=int(tier_min_search_voltage_mv),
                        )
                        baseline_cache[policy_key] = (tier_mode, prepared)
                        return prepared

                    return run_adaptive_tier_scans(
                        base_curve=base_curve,
                        gpu=gpu,
                        configure_tier_probe_runner=configure_tier_probe_runner,
                        settings=settings,
                        runtime_options=runtime_options,
                        base_loop_settings=base_loop_settings,
                        baseline_candidate=baseline_candidate,
                        initial_stable_outcome=initial_stable_outcome,
                        stable_probe=stable_probe,
                        discovery_summary=discovery_summary,
                        probe_history=probe_history,
                        baseline_target=baseline_target,
                        effective_min_search_voltage_mv=int(
                            effective_min_search_voltage_mv
                        ),
                        unsafe_entries=unsafe_entries,
                        prepare_tier_baseline=prepare_tier_baseline,
                        finish_with_final_verification=finish_with_final_verification,
                        event_callback=event_callback,
                        log=log,
                    )
                loop_result = run_preset_uv_loop(
                    base_curve,
                    settings=base_loop_settings,
                    initial_stable_candidate=stable_candidate,
                    io=loop_io,
                    unsafe_entries=unsafe_entries,
                    initial_stable_outcome=initial_stable_outcome,
                    min_search_voltage_mv=int(effective_min_search_voltage_mv),
                    initial_tail_rise_bins=int(descent_tail_rise_bins),
                    log=log,
                )
                log_lower_voltage_sweep_events(log, loop_result.events)
                stable_candidate = loop_result.stable_candidate
                selected_probe = (
                    loop_result.stable_outcome.raw_probe
                    if loop_result.stable_outcome is not None
                    else None
                )
                if selected_probe is not None:
                    stable_probe = selected_probe
        except KeyboardInterrupt:
            if adaptive_tier_phase:
                # A stop during the per-tier dialogs/finals aborts cleanly;
                # the past-candidates fallback below is for stops during
                # the sweep itself and would save a fourth, tier-less
                # profile shadowing the confirmed ones.
                clear_auto_uv_stop_request()
                raise
            if auto_uv_stop_request_aborts_final_choice():
                clear_auto_uv_stop_request()
                raise
            if not bool(runtime_options.get("auto_uv_require_final_choice")):
                raise
            user_stop_final_choice = True
            clear_auto_uv_stop_request()
            log_phase(
                log,
                "auto-uv",
                "user stop requested; offering past stable candidates for final verification",
            )
        final_tail_rise_bins = int(
            stable_candidate.metadata.get("tail_rise_bins", descent_tail_rise_bins)
        )
        final_selection = select_final_scan_candidate(
            base_curve=base_curve,
            settings=settings,
            runtime_options=runtime_options,
            stable_plan=stable_candidate.flattened_plan,
            stable_voltage_mv=int(stable_candidate.voltage_mv),
            stable_lock_clock_mhz=int(stable_candidate.target_mhz),
            stable_probe=stable_probe,
            stable_history=stable_history,
            runner=runner,
            gpu=gpu,
            probe_history=probe_history,
            log=log,
            tail_rise_bins=int(final_tail_rise_bins),
            measured_baseline_clock_mhz=float(baseline_target.measured_clock_mhz),
            discovery_summary=discovery_summary,
            baseline_candidate=baseline_candidate,
            final_verification_duration_s=int(final_verification_duration_s),
            event_callback=event_callback,
            run_performance_auto_oc=(
                settings.auto_uv_mode == AUTO_UV_MODE_PERFORMANCE
                and not user_stop_final_choice
            ),
            request_reason=(
                "user-stop" if bool(user_stop_final_choice) else "sweep-complete"
            ),
        )

        final_tail_rise_bins = int(final_selection.tail_rise_bins)
        failed_final_voltages: set[int] = set()
        while True:
            try:
                return finish_with_final_verification(
                    final_stable_plan=final_selection.plan,
                    final_stable_voltage_mv=int(final_selection.voltage_mv),
                    final_stable_lock_clock_mhz=int(final_selection.lock_clock_mhz),
                    final_stable_probe=final_selection.probe,
                    selected_final_verification_duration_s=int(
                        final_selection.verification_duration_s
                    ),
                    final_tail_rise_bins=int(final_tail_rise_bins),
                    final_auto_oc_metadata=final_selection.auto_oc_metadata,
                )
            except AutoUvError as exc:
                if not final_verification_failure_can_offer_retry(
                    exc,
                    runtime_options=runtime_options,
                ):
                    raise
                failed_voltage_mv = int(final_selection.voltage_mv)
                if failed_voltage_mv in failed_final_voltages:
                    raise
                failed_final_voltages.add(failed_voltage_mv)
                retry_selection = choose_next_candidate_after_final_failure(
                    base_curve=base_curve,
                    settings=settings,
                    stable_plan=final_selection.plan,
                    stable_voltage_mv=int(final_selection.voltage_mv),
                    stable_lock_clock_mhz=int(final_selection.lock_clock_mhz),
                    stable_history=stable_history,
                    discovery_summary=discovery_summary,
                    baseline_candidate=baseline_candidate,
                    final_verification_duration_s=int(
                        final_selection.verification_duration_s
                    ),
                    short_probe_base_duration_s=int(
                        settings.short_probe_base_duration_s
                    ),
                    failed_error=exc,
                    failed_selection=final_selection,
                    run_profile_tier=run_profile_tier,
                    log=log,
                    event_callback=event_callback,
                    tail_rise_bins=int(final_tail_rise_bins),
                )
                if retry_selection is None:
                    raise
                final_selection = retry_selection
                final_tail_rise_bins = int(final_selection.tail_rise_bins)
    finally:
        cleanup_managed_q2rtx_processes(q2rtx_config, log=log)
        gpu.close()


def run_preset_uv_loop(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    initial_stable_candidate: VfCurveCandidate,
    io: BaseUvLoopIO,
    unsafe_entries: list[dict] | None,
    initial_stable_outcome: VoltageProbeOutcome | None,
    min_search_voltage_mv: int,
    initial_tail_rise_bins: int,
    log: Callable[[str], None],
) -> LowerVoltageSweepResult:
    if settings.auto_uv_mode == AUTO_UV_MODE_EFFICIENCY:
        return run_efficiency_uv_loop(
            base_curve,
            settings=settings,
            initial_stable_candidate=initial_stable_candidate,
            io=io,
            unsafe_entries=unsafe_entries,
            initial_stable_outcome=initial_stable_outcome,
            min_search_voltage_mv=int(min_search_voltage_mv),
            initial_tail_rise_bins=int(initial_tail_rise_bins),
            log=log,
        )
    if settings.auto_uv_mode == AUTO_UV_MODE_PERFORMANCE:
        return run_performance_uv_loop(
            base_curve,
            settings=settings,
            initial_stable_candidate=initial_stable_candidate,
            io=io,
            unsafe_entries=unsafe_entries,
            initial_stable_outcome=initial_stable_outcome,
        )
    return run_balanced_uv_loop(
        base_curve,
        settings=settings,
        initial_stable_candidate=initial_stable_candidate,
        io=io,
        unsafe_entries=unsafe_entries,
        initial_stable_outcome=initial_stable_outcome,
    )


def run_adaptive_tier_scans(
    *,
    base_curve: list[dict],
    gpu,
    configure_tier_probe_runner: Callable[[float], AutoUvProbeRunner],
    settings,
    runtime_options: dict,
    base_loop_settings: AutoUvScanSettings,
    baseline_candidate: VfCurveCandidate,
    initial_stable_outcome: VoltageProbeOutcome | None,
    stable_probe: AutoUvProbeSummary | None,
    discovery_summary: AutoUvProbeSummary,
    probe_history: list,
    baseline_target,
    effective_min_search_voltage_mv: int,
    unsafe_entries: list[dict] | None,
    finish_with_final_verification: Callable[..., AutoUvVoltageScanResult],
    event_callback: AutoUvEventCallback | None,
    log: Callable[[str], None],
    prepare_tier_baseline: (
        Callable[..., AdaptiveTierBaseline] | None
    ) = None,
) -> AutoUvVoltageScanResult:
    """One scan, three profiles: run the proven per-tier descent for each tier.

    A single tail-less sweep cannot reproduce the per-tier held clocks — the
    rising tail compounds through the measured-clock ratchet, so each tier
    must descend with its OWN tail (efficiency +2 to the floor,
    balanced +4 to the FPS/W wall, performance +4 then the Auto-OC climb).
    Stock and flattened baselines are shared when power, memory and tail
    settings match; each tier keeps its own clock-loss allowance. Because Balanced
    and Performance descend with the same tail, Performance can reuse the
    Balanced descent only when those policy inputs also match (see
    performance_can_reuse_balanced_descent) and runs only its Auto-OC climb.
    The tiers accumulate genuinely-unsafe voltages so a later tier never
    re-crashes a point an earlier tier condemned. The
    first verified (deepest) profile soaks the full final duration; shallower
    tiers get the graduated confirm. Raises on producing no profile at all.
    """
    primary_scan_result: AutoUvVoltageScanResult | None = None
    last_tier_error: AutoUvError | None = None
    any_tier_discarded = False
    balanced_donation: BalancedDescentDonation | None = None
    accumulated_unsafe: list[dict] = list(unsafe_entries or [])

    def record_tier_failure(
        *,
        tier_mode: str,
        tier_error: AutoUvError,
        stage: str,
        event_details: dict,
    ) -> None:
        nonlocal last_tier_error, balanced_donation
        last_tier_error = tier_error
        if tier_mode == AUTO_UV_MODE_BALANCED:
            balanced_donation = None
        log_phase(
            log,
            "auto-uv",
            f"adaptive {tier_mode} {stage} failed: {tier_error}; "
            "continuing with the remaining tiers",
        )
        emit_auto_uv_event(
            event_callback,
            "tier_skipped",
            **event_details,
            reason=f"{stage}-failed",
        )
    gpu_name = gpu.translated_gpu_policy.get("gpu_name")
    # A tier without its own option restores the scan-wide request or zero,
    # including when scan open already applied Efficiency's override.
    # The driver limit is a per-GPU constant, fetched
    # once so mid-scan daemon hiccups can't clamp tiers inconsistently.
    scan_open_memory_offset_mhz = scan_wide_memory_offset_mhz(runtime_options)
    memory_offset_limit_mhz = int(
        driver_memory_offset_limit_mhz(gpu.policy_controller)
    )
    for tier_index, tier_mode in enumerate(ADAPTIVE_TIER_ORDER):
        next_tier = (
            str(ADAPTIVE_TIER_ORDER[tier_index + 1])
            if tier_index + 1 < len(ADAPTIVE_TIER_ORDER)
            else ""
        )
        tier_event_details = {
            "tier": str(tier_mode),
            "position": int(tier_index) + 1,
            "total": len(ADAPTIVE_TIER_ORDER),
            "next_tier": next_tier,
        }
        emit_auto_uv_event(event_callback, "tier_started", **tier_event_details)
        # Each tier descends and verifies against ITS clock-drop allowance
        # (per-tier option, scan-wide override, GPU table, generic fallback).
        tier_clock_drop_margin_pct = adaptive_tier_clock_drop_margin_pct(
            runtime_options,
            tier_mode=str(tier_mode),
            gpu_name=gpu_name,
        )
        tier_min_core_clock_pct = max(0.0, 100.0 - tier_clock_drop_margin_pct)
        log_phase(
            log,
            "auto-uv",
            f"adaptive {tier_mode} clock-drop allowance: "
            f"{tier_clock_drop_margin_pct:.1f}%",
        )
        apply_adaptive_tier_memory_offset(
            gpu,
            tier_mode=str(tier_mode),
            runtime_options=runtime_options,
            fallback_offset_mhz=scan_open_memory_offset_mhz,
            limit_mhz=memory_offset_limit_mhz,
            event_callback=event_callback,
            log=log,
        )
        tier_memory_offset_mhz = (
            memory_offset_from_gpu_policy(gpu.translated_gpu_policy) or 0
        )
        tier_tail_rise_bins = adaptive_tier_descent_tail_rise_bins(
            str(tier_mode)
        )
        request_adaptive_tier_power_limit(
            gpu,
            tier_mode=str(tier_mode),
            runtime_options=runtime_options,
        )
        apply_pending_power_limit(
            gpu,
            log=log,
            purpose=f"adaptive {str(tier_mode)} scan",
        )
        tier_runner = configure_tier_probe_runner(tier_min_core_clock_pct)
        tier_baseline_candidate = baseline_candidate
        tier_initial_stable_outcome = initial_stable_outcome
        tier_stable_probe = stable_probe
        tier_discovery_summary = discovery_summary
        tier_baseline_target = baseline_target
        tier_effective_min_search_voltage_mv = int(
            effective_min_search_voltage_mv
        )
        tier_base_loop_settings = base_loop_settings
        if prepare_tier_baseline is not None:
            try:
                prepared = prepare_tier_baseline(
                    tier_mode=str(tier_mode),
                    tier_runner=tier_runner,
                    tier_tail_rise_bins=int(tier_tail_rise_bins),
                )
            except AutoUvPowerLimitApplyError:
                raise
            except AutoUvError as tier_error:
                record_tier_failure(
                    tier_mode=str(tier_mode),
                    tier_error=tier_error,
                    stage="baseline",
                    event_details=tier_event_details,
                )
                continue
            tier_runner = prepared.runner
            tier_baseline_candidate = prepared.candidate
            tier_initial_stable_outcome = prepared.outcome
            tier_stable_probe = prepared.stable_probe
            tier_discovery_summary = prepared.discovery_summary
            tier_baseline_target = prepared.target
            tier_effective_min_search_voltage_mv = int(
                prepared.min_search_voltage_mv
            )
            tier_base_loop_settings = replace(
                base_loop_settings,
                start_voltage_mv=int(tier_baseline_candidate.voltage_mv),
                min_search_voltage_mv=int(tier_effective_min_search_voltage_mv),
                baseline_core_clock_mhz=float(
                    tier_baseline_target.measured_clock_mhz
                ),
                reference_actual_voltage_mv=tier_stable_probe.avg_voltage_mv,
            )
        try:
            if (
                tier_mode == AUTO_UV_MODE_PERFORMANCE
                and balanced_donation is not None
                and performance_can_reuse_balanced_descent(
                    balanced_donation,
                    performance_memory_offset_mhz=tier_memory_offset_mhz,
                    performance_min_core_clock_pct=float(tier_min_core_clock_pct),
                    measured_baseline_clock_mhz=float(
                        tier_baseline_target.measured_clock_mhz
                    ),
                    performance_baseline_voltage_mv=int(
                        tier_baseline_candidate.voltage_mv
                    ),
                    performance_baseline_target_mhz=int(
                        tier_baseline_candidate.target_mhz
                    ),
                    performance_power_limit_w=positive_int(gpu.power_limit_w),
                    log=log,
                )
            ):
                donation = balanced_donation
                # The descent would re-measure the same capped ladder balanced
                # already proved, so performance adopts it and goes straight to
                # the Auto-OC climb. The history is copied so performance's OC
                # passes never append into balanced's.
                tier_candidate = donation.candidate
                tier_final_tail = int(donation.tail_rise_bins)
                tier_probe = donation.probe
                tier_history = list(donation.history)
                if prepare_tier_baseline is not None and tier_stable_probe is not None:
                    tier_history = [tier_stable_probe, *tier_history[1:]]
                log_phase(
                    log,
                    "auto-uv",
                    "adaptive performance reusing balanced descent "
                    f"{int(tier_candidate.voltage_mv)}mV@"
                    f"{int(tier_candidate.target_mhz)}MHz: "
                    "skipping downsweep, starting Auto-OC",
                )
                emit_auto_uv_event(
                    event_callback,
                    "tier_descent_reused",
                    **tier_event_details,
                    source_tier=str(AUTO_UV_MODE_BALANCED),
                    voltage_mv=int(tier_candidate.voltage_mv),
                    target_mhz=int(tier_candidate.target_mhz),
                )
            else:
                tier_candidate, tier_final_tail, tier_probe, tier_history = (
                    run_adaptive_tier_descent(
                        base_curve,
                        tier_mode=str(tier_mode),
                        base_loop_settings=tier_base_loop_settings,
                        baseline_candidate=tier_baseline_candidate,
                        initial_stable_outcome=tier_initial_stable_outcome,
                        fallback_probe=tier_stable_probe,
                        discovery_summary=tier_discovery_summary,
                        accumulated_unsafe=accumulated_unsafe,
                        effective_min_search_voltage_mv=int(
                            tier_effective_min_search_voltage_mv
                        ),
                        runner=tier_runner,
                        probe_history=probe_history,
                        min_core_clock_pct=float(tier_min_core_clock_pct),
                        gpu=gpu,
                        log=log,
                    )
                )
                if tier_mode == AUTO_UV_MODE_BALANCED:
                    balanced_donation = BalancedDescentDonation(
                        candidate=tier_candidate,
                        tail_rise_bins=int(tier_final_tail),
                        probe=tier_probe,
                        history=tier_history,
                        descent_tail_rise_bins=adaptive_tier_descent_tail_rise_bins(
                            AUTO_UV_MODE_BALANCED
                        ),
                        memory_offset_mhz=tier_memory_offset_mhz,
                        power_limit_w=positive_int(gpu.power_limit_w),
                        baseline_voltage_mv=int(
                            tier_baseline_candidate.voltage_mv
                        ),
                        baseline_target_mhz=int(
                            tier_baseline_candidate.target_mhz
                        ),
                    )
        except AutoUvPowerLimitApplyError:
            raise
        except AutoUvError as tier_error:
            record_tier_failure(
                tier_mode=str(tier_mode),
                tier_error=tier_error,
                stage="descent",
                event_details=tier_event_details,
            )
            continue
        runs_auto_oc = tier_mode == AUTO_UV_MODE_PERFORMANCE
        # Per-tier final-verification soak: efficiency 1 min, balanced 3 min,
        # performance 5 min by default (an explicit --auto-uv-final-verification-s
        # overrides all tiers). The graduated per-tier lengths already give the
        # aggressive tiers the longer confirmation the old primary/secondary
        # split aimed for.
        tier_final_duration = resolve_final_verification_duration_s(
            runtime_options, auto_uv_mode=str(tier_mode)
        )
        try:
            tier_selection = select_final_scan_candidate(
                base_curve=base_curve,
                settings=settings,
                runtime_options=runtime_options,
                stable_plan=tier_candidate.flattened_plan,
                stable_voltage_mv=int(tier_candidate.voltage_mv),
                stable_lock_clock_mhz=int(tier_candidate.target_mhz),
                stable_probe=tier_probe,
                stable_history=tier_history,
                runner=tier_runner,
                gpu=gpu,
                probe_history=probe_history,
                log=log,
                tail_rise_bins=int(tier_final_tail),
                measured_baseline_clock_mhz=float(
                    tier_baseline_target.measured_clock_mhz
                ),
                discovery_summary=tier_discovery_summary,
                baseline_candidate=tier_baseline_candidate,
                final_verification_duration_s=int(tier_final_duration),
                event_callback=event_callback,
                run_performance_auto_oc=runs_auto_oc,
                run_power_bound_clock_reclaim=(
                    tier_mode
                    in {AUTO_UV_MODE_EFFICIENCY, AUTO_UV_MODE_BALANCED}
                    and probe_indicates_power_saturation(
                        tier_discovery_summary,
                        power_limit_w=positive_int(gpu.power_limit_w),
                    )
                ),
                request_reason=f"adaptive-{tier_mode}",
                auto_uv_mode_override=str(tier_mode),
                min_core_clock_pct_override=float(tier_min_core_clock_pct),
            )
            # The descent candidate is only the input to final selection. In
            # particular, Performance can replace it with a much higher
            # Auto-OC curve. Publish the tier trace only after that selection
            # so the persistent color-coded line matches the curve entering
            # final verification instead of freezing the preliminary descent.
            emit_auto_uv_event(
                event_callback,
                "tier_confirmed",
                **tier_event_details,
                voltage_mv=int(tier_selection.voltage_mv),
                target_mhz=int(tier_selection.lock_clock_mhz),
                points=vf_curve_event_points(tier_selection.plan),
            )
            tier_scan_result = finish_with_final_verification(
                final_stable_plan=tier_selection.plan,
                final_stable_voltage_mv=int(tier_selection.voltage_mv),
                final_stable_lock_clock_mhz=int(tier_selection.lock_clock_mhz),
                final_stable_probe=tier_selection.probe,
                selected_final_verification_duration_s=int(
                    tier_selection.verification_duration_s
                ),
                final_tail_rise_bins=int(tier_selection.tail_rise_bins),
                final_auto_oc_metadata=tier_selection.auto_oc_metadata,
                final_auto_uv_mode=str(tier_mode),
                final_profile_tier=str(tier_mode),
                final_min_core_clock_pct=float(tier_min_core_clock_pct),
                final_clock_drop_margin_pct=float(tier_clock_drop_margin_pct),
                final_stable_history=tier_history,
                final_discovery_summary=tier_discovery_summary,
                final_baseline_candidate=tier_baseline_candidate,
                final_measured_baseline_clock_mhz=float(
                    tier_baseline_target.measured_clock_mhz
                ),
            )
        except AutoUvFinalChoiceDiscarded:
            any_tier_discarded = True
            log_phase(
                log,
                "auto-uv",
                f"adaptive {tier_mode} discarded by user; "
                "continuing with the remaining tiers",
            )
            emit_auto_uv_event(
                event_callback,
                "tier_skipped",
                **tier_event_details,
                reason="discarded",
            )
            continue
        except AutoUvPowerLimitApplyError:
            raise
        except AutoUvError as tier_error:
            record_tier_failure(
                tier_mode=str(tier_mode),
                tier_error=tier_error,
                stage="verification",
                event_details=tier_event_details,
            )
            continue
        emit_auto_uv_event(
            event_callback,
            "tier_completed",
            **tier_event_details,
            voltage_mv=int(tier_scan_result.final_voltage_mv),
            target_mhz=int(tier_scan_result.lock_clock_mhz),
        )
        if primary_scan_result is None:
            primary_scan_result = tier_scan_result
    if primary_scan_result is not None:
        return primary_scan_result
    if last_tier_error is not None:
        raise last_tier_error
    if any_tier_discarded:
        # Every tier's candidate was discarded at its dialog — a deliberate
        # user choice, not a scan failure.
        raise AutoUvFinalChoiceDiscarded(
            "all adaptive tiers were discarded by the user"
        )
    raise AutoUvError("adaptive scan produced no verified profile")


def run_adaptive_tier_descent(
    base_curve: list[dict],
    *,
    tier_mode: str,
    base_loop_settings: AutoUvScanSettings,
    baseline_candidate: VfCurveCandidate,
    initial_stable_outcome: VoltageProbeOutcome | None,
    fallback_probe: AutoUvProbeSummary | None,
    discovery_summary: AutoUvProbeSummary,
    accumulated_unsafe: list[dict],
    effective_min_search_voltage_mv: int,
    runner: AutoUvProbeRunner,
    probe_history: list,
    min_core_clock_pct: float,
    gpu,
    log: Callable[[str], None],
) -> tuple[VfCurveCandidate, int, AutoUvProbeSummary | None, list[AutoUvProbeSummary]]:
    """Run one tier's tailed descent under its shipped cap.

    Returns ``(candidate, final_tail_rise_bins, probe, tier_history)``. The
    tier's verified/passed probes land in the returned ``tier_history`` (kept
    per tier so one tier's candidates never pollute another's final choice).
    """
    tier_descent_tail = adaptive_tier_descent_tail_rise_bins(tier_mode)
    tier_settings = replace(
        base_loop_settings,
        auto_uv_mode=tier_mode,
        tail_rise_bins=int(tier_descent_tail),
        min_core_clock_pct=float(min_core_clock_pct),
    )
    tier_history: list[AutoUvProbeSummary] = (
        [fallback_probe] if fallback_probe is not None else []
    )

    def tier_probe_candidate(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        retarget_clock_ceiling_for_candidate(gpu.clock_ceiling, candidate)
        outcome = runner.probe_sweep_candidate(
            candidate,
            stable_history=tier_history,
            phase_label=f"{str(tier_mode)}-candidate",
        )
        if outcome.raw_probe is not None:
            probe_history.append(outcome.raw_probe)
        return outcome

    def tier_write_verified(candidate, outcome) -> None:
        summary = require_probe_summary(outcome)
        tier_history.append(summary)
        write_verified_candidate(
            candidate,
            summary,
            discovery_summary=discovery_summary,
            configured_power_limit_w=positive_int(gpu.power_limit_w),
            tail_rise_bins=int(
                candidate.metadata.get("tail_rise_bins", tier_descent_tail)
            ),
        )

    def tier_mark_unsafe(candidate, outcome) -> None:
        # A tier's GENUINE hard fail seeds the shared cache so the remaining
        # tiers skip that voltage/clock band. Controlled/environmental
        # failures (launcher errors, FPS regressions) are exempt — the same
        # gate the persistent blacklist applies — so a transient event never
        # floors the other tiers.
        reason = str(getattr(outcome.decision, "reason", "") or "")
        if not probe_failure_should_mark_voltage_unsafe(reason):
            return
        accumulated_unsafe.append(
            {
                "candidate_voltage_mv": int(candidate.voltage_mv),
                "lock_clock_mhz": int(candidate.target_mhz),
                "reason": reason or "stability-probe-failed",
            }
        )

    def tier_record_passed(candidate, outcome) -> None:
        tier_history.append(require_probe_summary(outcome))

    tier_loop_io = BaseUvLoopIO(
        probe_candidate=tier_probe_candidate,
        write_verified_candidate=tier_write_verified,
        mark_unsafe_candidate=tier_mark_unsafe,
        record_passed_candidate=tier_record_passed,
    )
    # The tier power limit was applied before its stock/flattened baselines.
    # Keep it unchanged through every descent probe and final verification so
    # selection never relies on a power regime the saved profile will not use.
    loop_result = run_preset_uv_loop(
        base_curve,
        settings=tier_settings,
        initial_stable_candidate=baseline_candidate,
        io=tier_loop_io,
        unsafe_entries=accumulated_unsafe,
        initial_stable_outcome=initial_stable_outcome,
        min_search_voltage_mv=int(effective_min_search_voltage_mv),
        initial_tail_rise_bins=int(tier_descent_tail),
        log=log,
    )
    log_lower_voltage_sweep_events(log, loop_result.events)
    tier_candidate = loop_result.stable_candidate
    tier_final_tail = int(
        tier_candidate.metadata.get("tail_rise_bins", tier_descent_tail)
    )
    tier_probe = (
        require_probe_summary(loop_result.stable_outcome)
        if loop_result.stable_outcome is not None
        else fallback_probe
    )
    return tier_candidate, tier_final_tail, tier_probe, tier_history


def request_adaptive_tier_power_limit(
    gpu,
    *,
    tier_mode: str,
    runtime_options: dict,
) -> None:
    """Use the same tier budget at scan startup and during orchestration."""
    apply_adaptive_tier_power_limit(
        gpu,
        tier_mode=tier_mode,
        stock_power_limit_w=positive_int(getattr(gpu, "baseline_power_limit_w", None))
        or positive_int(gpu.power_limit_w),
        scan_request_w=positive_int(runtime_options.get("auto_uv_power_limit_w")),
        balanced_pct=uv_limit_power_limit_pct_for_gpu(
            gpu.translated_gpu_policy.get("gpu_name"), AUTO_UV_MODE_BALANCED
        ),
        explicit_watts=positive_int(
            adaptive_tier_option(
                runtime_options, tier_mode=tier_mode, option="power_limit_w"
            )
        ),
    )


def apply_adaptive_tier_power_limit(
    gpu,
    *,
    tier_mode: str,
    stock_power_limit_w: int | None,
    scan_request_w: int | None,
    balanced_pct: float | None,
    explicit_watts: int | None = None,
) -> None:
    """Request this tier's board-power cap for baseline through verification.

    An explicit per-tier request (the scan dialog's per-profile power slider)
    wins over the balanced-anchor scaling. A manual scan-wide request stays a
    hard ceiling in BOTH branches: neither scaling nor a per-tier flag may
    push a tier above what the user explicitly asked for scan-wide.
    """
    if explicit_watts is not None:
        watts = int(explicit_watts)
        if scan_request_w is not None:
            watts = min(watts, int(scan_request_w))
        gpu.requested_power_limit_w = gpu.clamp_power_limit_w(watts)
        return
    tier_watts = adaptive_tier_power_limit_w(
        power_limit_pct=uv_limit_power_limit_pct_for_gpu(
            gpu.translated_gpu_policy.get("gpu_name"), tier_mode
        ),
        baseline_power_limit_w=stock_power_limit_w,
        scan_request_w=scan_request_w,
        balanced_pct=balanced_pct,
    )
    if tier_watts is None:
        # A tier without an explicit/table limit restores the scan-wide
        # request or stock budget instead of inheriting the previous tier's
        # cap.
        tier_watts = scan_request_w or stock_power_limit_w
    if tier_watts is None:
        return
    if scan_request_w is not None:
        tier_watts = min(int(tier_watts), int(scan_request_w))
    gpu.requested_power_limit_w = gpu.clamp_power_limit_w(int(tier_watts))


def scan_wide_memory_offset_mhz(runtime_options: dict) -> int:
    return int(
        runtime_options.get(
            "auto_uv_memory_offset_mhz", runtime_options.get("memory_offset_mhz")
        )
        or 0
    )


def apply_adaptive_tier_memory_offset(
    gpu,
    *,
    tier_mode: str,
    runtime_options: dict,
    fallback_offset_mhz: int,
    limit_mhz: int,
    event_callback: AutoUvEventCallback | None,
    log: Callable[[str], None],
) -> None:
    """Resolve and apply this tier's memory V/F offset before its descent.

    The tier's own option wins; absent, the scan-open offset (scan-wide or
    zero) is RESTORED — an earlier tier's offset must never leak into a tier
    that didn't ask for it. The offset that is actually live (NVML read-back
    when it disagrees) lands in ``translated_gpu_policy`` so the tier's crash
    markers and saved profile record reality. An apply failure keeps the
    previous offset and lets the tier run — one tier's NVML hiccup must not
    abort the whole adaptive scan.
    """
    raw = adaptive_tier_option(
        runtime_options,
        tier_mode=tier_mode,
        option="memory_offset_mhz",
    )
    if raw is None:
        target_mhz = max(0, min(int(fallback_offset_mhz), int(limit_mhz)))
    else:
        target_mhz = max(0, min(int(cast(Any, raw)), int(limit_mhz)))
        if target_mhz != int(cast(Any, raw)):
            log(
                f"Auto-UV memory offset ({tier_mode}): requested "
                f"{int(cast(Any, raw))} MHz clamped to {target_mhz} MHz "
                f"(limit {int(limit_mhz)} MHz)"
            )
    current_mhz = int(gpu.translated_gpu_policy.get("mem_clk_vf_offset_mhz") or 0)
    applied_mhz = target_mhz
    if target_mhz != current_mhz:
        try:
            applied = gpu.gpu.apply_clock_offsets(
                mem_clk_vf_offset_mhz=int(target_mhz)
            )
        except Exception as exc:
            log(
                f"Auto-UV memory offset ({tier_mode}): failed to apply "
                f"{target_mhz:+d} MHz; keeping {current_mhz:+d} MHz: {exc}"
            )
            return
        readback_mhz = (applied or {}).get("mem_clk_vf_offset_readback_mhz")
        if readback_mhz is None:
            log(
                f"Auto-UV memory offset ({tier_mode}): applied {target_mhz:+d} MHz "
                "(driver does not support read-back)"
            )
        elif int(readback_mhz) != int(target_mhz):
            applied_mhz = int(readback_mhz)
            log(
                f"Auto-UV memory offset ({tier_mode}) MISMATCH: requested "
                f"{target_mhz:+d} MHz but NVML reads back {applied_mhz:+d} MHz "
                "-- the driver clamped or ignored it"
            )
        else:
            log(
                f"Auto-UV memory offset ({tier_mode}): applied {target_mhz:+d} MHz "
                f"(was {current_mhz:+d} MHz)"
            )
    gpu.translated_gpu_policy["mem_clk_vf_offset_mhz"] = int(applied_mhz)
    gpu.translated_gpu_policy["mem_clk_vf_offset_limit_mhz"] = int(limit_mhz)
    emit_auto_uv_event(
        event_callback,
        "memory_offset_applied",
        tier=str(tier_mode),
        offset_mt_s=int(applied_mhz),
        offset_mhz=int(applied_mhz) // 2,
    )


ADAPTIVE_TIER_ORDER = (
    AUTO_UV_MODE_EFFICIENCY,
    AUTO_UV_MODE_BALANCED,
    AUTO_UV_MODE_PERFORMANCE,
)


def adaptive_tier_descent_tail_rise_bins(tier_mode: str) -> int:
    """The rising tail each tier descends WITH.

    Efficiency keeps two rising bins through both voltage passes. Balanced and performance
    descend with their full tail the whole way — the tail is what holds the
    measured clock up through the ratchet, so it must be applied on descent,
    not decorated on afterward."""
    if tier_mode == AUTO_UV_MODE_BALANCED:
        return int(AUTO_UV_DEFAULTS.balanced_tail_rise_bins)
    if tier_mode == AUTO_UV_MODE_PERFORMANCE:
        return int(AUTO_UV_DEFAULTS.performance_tail_rise_bins)
    return int(AUTO_UV_DEFAULTS.tail_rise_bins)


@dataclass(frozen=True)
class BalancedDescentDonation:
    """The balanced tier's completed descent, offered to the performance tier.

    Captured with the inputs that made the descent what it was (tail bins and
    the live memory offset) so performance can check the ladder it would run
    is the one balanced already ran."""

    candidate: VfCurveCandidate
    tail_rise_bins: int
    probe: AutoUvProbeSummary | None
    history: list[AutoUvProbeSummary]
    descent_tail_rise_bins: int
    memory_offset_mhz: int
    power_limit_w: int | None
    baseline_voltage_mv: int
    baseline_target_mhz: int


def performance_can_reuse_balanced_descent(
    donation: BalancedDescentDonation | None,
    *,
    performance_memory_offset_mhz: int,
    performance_min_core_clock_pct: float,
    measured_baseline_clock_mhz: float,
    performance_baseline_voltage_mv: int,
    performance_baseline_target_mhz: int,
    performance_power_limit_w: int | None,
    log: Callable[[str], None],
) -> bool:
    """Whether the balanced descent doubles as performance's downsweep.

    When Balanced and Performance use the same baseline regime, rising tail,
    power limit, and memory offset, Performance's downsweep would re-measure
    the exact ladder Balanced just proved. Reuse skips it and sends
    Performance straight to the Auto-OC climb. Any input mismatch falls back
    to a full descent.

    Balanced descends under a LOOSER clock-drop allowance than performance
    (the efficiency-weighted blend), so the donated endpoint must also still
    clear performance's own floor — the rising tail normally holds the
    measured clock well above it, but a below-floor endpoint would only be
    rejected later, at performance's final verification. No probe evidence
    means no reuse."""
    if donation is None:
        return False
    performance_tail = adaptive_tier_descent_tail_rise_bins(
        AUTO_UV_MODE_PERFORMANCE
    )
    if int(performance_tail) != int(donation.descent_tail_rise_bins):
        log_phase(
            log,
            "auto-uv",
            "adaptive performance descending itself: tail "
            f"+{int(performance_tail)} differs from balanced "
            f"+{int(donation.descent_tail_rise_bins)}",
        )
        return False
    if positive_int(performance_power_limit_w) != positive_int(
        donation.power_limit_w
    ):
        log_phase(
            log,
            "auto-uv",
            "adaptive performance descending itself: power limit "
            f"{_format_power_limit_w(performance_power_limit_w)} differs "
            f"from balanced {_format_power_limit_w(donation.power_limit_w)}",
        )
        return False
    if (
        abs(
            int(performance_baseline_voltage_mv)
            - int(donation.baseline_voltage_mv)
        )
        > ADAPTIVE_BASELINE_REUSE_VOLTAGE_TOLERANCE_MV
        or abs(
            int(performance_baseline_target_mhz)
            - int(donation.baseline_target_mhz)
        )
        > ADAPTIVE_BASELINE_REUSE_CLOCK_TOLERANCE_MHZ
    ):
        log_phase(
            log,
            "auto-uv",
            "adaptive performance descending itself: capped baseline "
            f"{int(performance_baseline_voltage_mv)}mV@"
            f"{int(performance_baseline_target_mhz)}MHz differs from balanced "
            f"{int(donation.baseline_voltage_mv)}mV@"
            f"{int(donation.baseline_target_mhz)}MHz",
        )
        return False
    if int(performance_memory_offset_mhz) != int(donation.memory_offset_mhz):
        log_phase(
            log,
            "auto-uv",
            "adaptive performance descending itself: memory offset "
            f"{int(performance_memory_offset_mhz):+d}MHz differs from balanced "
            f"{int(donation.memory_offset_mhz):+d}MHz",
        )
        return False
    donated_clock_mhz = getattr(donation.probe, "avg_core_clock_mhz", None)
    if donated_clock_mhz is None:
        log_phase(
            log,
            "auto-uv",
            "adaptive performance descending itself: balanced endpoint "
            "carries no measured clock",
        )
        return False
    floor_mhz = (
        float(measured_baseline_clock_mhz)
        * float(performance_min_core_clock_pct)
        / 100.0
    )
    if float(donated_clock_mhz) < floor_mhz:
        log_phase(
            log,
            "auto-uv",
            "adaptive performance descending itself: balanced endpoint "
            f"{float(donated_clock_mhz):.0f}MHz sits below performance's "
            f"{floor_mhz:.0f}MHz clock floor",
        )
        return False
    return True


def adaptive_tier_power_limit_w(
    *,
    power_limit_pct: float | None,
    baseline_power_limit_w: int | None,
    scan_request_w: int | None,
    balanced_pct: float | None,
) -> int | None:
    """Each adaptive tier's board-power budget for its final and profile.

    The scan dialog's single slider is the balanced anchor: a manual value
    scales every tier proportionally; untouched, each tier gets its
    uv_limits percentage of the stock budget. GPUs without a table entry
    keep the scan-wide request unchanged.
    """
    if power_limit_pct is None or not baseline_power_limit_w:
        return scan_request_w
    if scan_request_w is not None and balanced_pct:
        watts = float(scan_request_w) * float(power_limit_pct) / float(balanced_pct)
    else:
        watts = float(baseline_power_limit_w) * float(power_limit_pct) / 100.0
    return min(int(round(watts)), int(baseline_power_limit_w))


def log_lower_voltage_sweep_events(
    log: Callable[[str], None],
    events: list[LowerVoltageSweepEvent],
) -> None:
    for event in events:
        if event.name not in {"stop", "low-clock-skip"}:
            continue
        log_phase(log, "auto-uv", f"sweep-{event.name} {event.message}")


def select_final_scan_candidate(
    *,
    base_curve: list[dict],
    settings,
    runtime_options: dict,
    stable_plan: list[dict],
    stable_voltage_mv: int,
    stable_lock_clock_mhz: int,
    stable_probe: AutoUvProbeSummary | None,
    stable_history: list[AutoUvProbeSummary],
    runner,
    gpu,
    probe_history: list[AutoUvProbeSummary],
    log: Callable[[str], None],
    tail_rise_bins: int,
    measured_baseline_clock_mhz: float,
    discovery_summary: AutoUvProbeSummary,
    baseline_candidate: VfCurveCandidate,
    final_verification_duration_s: int,
    event_callback: AutoUvEventCallback | None,
    run_performance_auto_oc: bool,
    run_power_bound_clock_reclaim: bool = False,
    request_reason: str,
    auto_uv_mode_override: str | None = None,
    min_core_clock_pct_override: float | None = None,
) -> FinalScanCandidate:
    # Adaptive scans select per tier: the tier name overrides the scan-wide
    # mode so the OC pass and the choice dialog see the tier, not "adaptive";
    # the clock-floor override likewise carries the tier's own allowance.
    selection_mode = str(auto_uv_mode_override or settings.auto_uv_mode)
    selection_min_core_clock_pct = float(
        min_core_clock_pct_override
        if min_core_clock_pct_override is not None
        else settings.min_performance_core_clock_pct
    )
    final_plan = stable_plan
    final_voltage_mv = int(stable_voltage_mv)
    final_lock_clock_mhz = int(stable_lock_clock_mhz)
    final_probe = stable_probe
    final_auto_oc_metadata: dict = {}

    if bool(run_power_bound_clock_reclaim):
        (
            final_plan,
            final_voltage_mv,
            final_lock_clock_mhz,
            final_probe,
            final_auto_oc_metadata,
        ) = select_power_bound_clock_reclaim_candidate(
            base_curve,
            auto_uv_mode=selection_mode,
            stable_plan=final_plan,
            stable_voltage_mv=int(final_voltage_mv),
            stable_lock_clock_mhz=int(final_lock_clock_mhz),
            stable_probe=final_probe,
            stable_history=stable_history,
            runner=runner,
            gpu_name=gpu.translated_gpu_policy.get("gpu_name"),
            clock_ceiling=gpu.clock_ceiling,
            probe_history=probe_history,
            log=log,
            tail_rise_bins=int(tail_rise_bins),
            measured_baseline_clock_mhz=float(measured_baseline_clock_mhz),
        )

    if bool(run_performance_auto_oc):
        (
            final_plan,
            final_voltage_mv,
            final_lock_clock_mhz,
            final_probe,
            final_auto_oc_metadata,
        ) = select_performance_auto_oc_candidate(
            base_curve,
            auto_uv_mode=selection_mode,
            stable_plan=final_plan,
            stable_voltage_mv=int(final_voltage_mv),
            stable_lock_clock_mhz=int(final_lock_clock_mhz),
            stable_probe=final_probe,
            stable_history=stable_history,
            runner=runner,
            gpu_name=gpu.translated_gpu_policy.get("gpu_name"),
            clock_ceiling=gpu.clock_ceiling,
            probe_history=probe_history,
            log=log,
            tail_rise_bins=int(tail_rise_bins),
            target_voltage_mv=positive_int(
                runtime_options.get("auto_oc_target_voltage_mv")
            ),
            target_clock_mhz=positive_int(
                runtime_options.get("auto_oc_target_clock_mhz")
            ),
            measured_baseline_clock_mhz=float(measured_baseline_clock_mhz),
        )

    if selection_mode == AUTO_UV_MODE_EFFICIENCY:
        # Select across the passed descent and climb, carrying the measured
        # candidate's actual plan rather than rebuilding it from its label.
        clock_floor = (
            float(measured_baseline_clock_mhz) * selection_min_core_clock_pct / 100.0
        )
        measured_candidates = [
            probe
            for probe in stable_history
            if getattr(probe, "tested_plan", None) is not None
            and float(probe.avg_core_clock_mhz or 0.0) >= clock_floor
        ]
        selected_index = best_efficiency_candidate_index(measured_candidates)
        if selected_index is not None:
            final_probe = measured_candidates[selected_index]
            final_plan = [dict(point) for point in final_probe.tested_plan or []]
            final_voltage_mv = int(final_probe.candidate_voltage_mv)
            final_lock_clock_mhz = int(final_probe.lock_clock_mhz)
            if "clock_reclaim_selected_mhz" in final_auto_oc_metadata:
                final_auto_oc_metadata["clock_reclaim_selected_mhz"] = (
                    final_lock_clock_mhz
                )

    # Every tier carries the selected measurement's complete curve into the
    # final choice and soak, including Performance's Auto-OC selection.
    if final_probe is not None and final_probe.tested_plan is not None:
        final_plan = [dict(point) for point in final_probe.tested_plan]

    if bool(runtime_options.get("auto_uv_require_final_choice")):
        (
            final_plan,
            final_voltage_mv,
            final_lock_clock_mhz,
            selected_stable_probe,
            selected_final_verification_duration_s,
        ) = choose_final_verification_candidate(
            log=log,
            event_callback=event_callback,
            auto_uv_mode=selection_mode,
            base_probe=discovery_summary,
            stable_plan=final_plan,
            stable_voltage_mv=int(final_voltage_mv),
            stable_lock_clock_mhz=int(final_lock_clock_mhz),
            stable_probe=final_probe,
            stable_history=stable_history,
            base_curve=base_curve,
            final_verification_duration_s=int(final_verification_duration_s),
            initial_target_voltage_mv=int(baseline_candidate.voltage_mv),
            short_probe_base_duration_s=int(settings.short_probe_base_duration_s),
            tail_rise_bins=int(tail_rise_bins),
            request_reason=str(request_reason or "sweep-complete"),
            min_core_clock_pct=float(selection_min_core_clock_pct),
        )
        final_verification_duration_s = int(selected_final_verification_duration_s)
        if selected_stable_probe is not None:
            final_probe = selected_stable_probe

    return FinalScanCandidate(
        plan=final_plan,
        voltage_mv=int(final_voltage_mv),
        lock_clock_mhz=int(final_lock_clock_mhz),
        probe=final_probe,
        verification_duration_s=int(final_verification_duration_s),
        auto_oc_metadata=dict(final_auto_oc_metadata or {}),
        tail_rise_bins=int(tail_rise_bins),
    )


def final_verification_failure_can_offer_retry(
    exc: AutoUvError,
    *,
    runtime_options: dict,
) -> bool:
    if not bool(runtime_options.get("auto_uv_require_final_choice")):
        return False
    return str(exc).startswith("final long verification failed:")


def apply_pending_power_limit(
    gpu,
    *,
    log,
    purpose: str = "final verification",
) -> int | None:
    apply_power_limit = getattr(gpu, "apply_requested_power_limit", None)
    if callable(apply_power_limit):
        applied_power_limit_w = apply_power_limit(log=log, purpose=str(purpose))
        return (
            int(cast(Any, applied_power_limit_w))
            if applied_power_limit_w is not None
            else None
        )
    power_limit_w = getattr(gpu, "power_limit_w", None)
    return int(power_limit_w) if power_limit_w is not None else None


def choose_next_candidate_after_final_failure(
    *,
    base_curve: list[dict],
    settings,
    stable_plan: list[dict],
    stable_voltage_mv: int,
    stable_lock_clock_mhz: int,
    stable_history: list[AutoUvProbeSummary],
    discovery_summary: AutoUvProbeSummary,
    baseline_candidate: VfCurveCandidate,
    final_verification_duration_s: int,
    short_probe_base_duration_s: int,
    failed_error: AutoUvError,
    failed_selection: FinalScanCandidate,
    run_profile_tier: str,
    log: Callable[[str], None],
    event_callback: AutoUvEventCallback | None,
    tail_rise_bins: int,
) -> FinalScanCandidate | None:
    failed_voltage_mv = int(failed_selection.voltage_mv)
    recovery_decision = final_verification_failure_recovery_decision(
        failed_error,
        failed_selection=failed_selection,
        run_profile_tier=run_profile_tier,
        tail_rise_bins=int(tail_rise_bins),
    )
    candidate_records = list(
        candidate_records_from_history(
            stable_history,
            base_curve=base_curve,
            stable_plan=stable_plan,
            stable_voltage_mv=int(stable_voltage_mv),
            stable_lock_clock_mhz=int(stable_lock_clock_mhz),
            tail_rise_bins=int(tail_rise_bins),
        ).values()
    )
    log_phase(
        log,
        "final-verify",
        "failed; offering saved candidates above "
        f"{failed_voltage_mv}mV without restarting scan",
    )
    selection = choose_next_final_verification_candidate_after_failure(
        log=log,
        event_callback=event_callback,
        auto_uv_mode=settings.auto_uv_mode,
        base_probe=discovery_summary,
        candidate_records=candidate_records,
        stable_history=stable_history,
        failed_voltage_mv=int(failed_voltage_mv),
        final_verification_duration_s=int(final_verification_duration_s),
        initial_target_voltage_mv=int(baseline_candidate.voltage_mv),
        short_probe_base_duration_s=int(short_probe_base_duration_s),
        recovery_decision=recovery_decision,
        min_core_clock_pct=float(settings.min_performance_core_clock_pct),
    )
    if selection is None:
        log_phase(
            log,
            "final-verify",
            "no safer saved candidate remained after final verification failure",
        )
        return None

    (
        selected_plan,
        selected_voltage_mv,
        selected_lock_clock_mhz,
        selected_probe,
        selected_final_duration_s,
        selected_tail_rise_bins,
        selected_record,
    ) = selection
    log_phase(
        log,
        "final-verify",
        "retrying saved candidate "
        f"{int(selected_voltage_mv)}mV@{int(selected_lock_clock_mhz)}MHz",
    )
    return FinalScanCandidate(
        plan=selected_plan,
        voltage_mv=int(selected_voltage_mv),
        lock_clock_mhz=int(selected_lock_clock_mhz),
        probe=selected_probe,
        verification_duration_s=int(selected_final_duration_s),
        auto_oc_metadata={
            key: value
            for key, value in dict(selected_record).items()
            if str(key).startswith("auto_oc")
        },
        tail_rise_bins=int(selected_tail_rise_bins or tail_rise_bins),
    )


def final_verification_failure_recovery_decision(
    exc: AutoUvError,
    *,
    failed_selection: FinalScanCandidate,
    run_profile_tier: str,
    tail_rise_bins: int,
) -> dict:
    decision = str(exc)
    prefix = "final long verification failed:"
    if decision.startswith(prefix):
        decision = decision[len(prefix) :].strip()
    return {
        "candidate_voltage_mv": int(failed_selection.voltage_mv),
        "lock_clock_mhz": int(failed_selection.lock_clock_mhz),
        "reason": "final-verification-failed",
        "phase": "final-verify",
        "decision": decision or str(exc),
        "result_reason": decision or str(exc),
        "generated_profile_tier": str(run_profile_tier or ""),
        "tail_rise_bins": int(tail_rise_bins),
    }


def run_recovered_previous_crash_selection(
    *,
    pending_recovery_selection,
    base_curve: list[dict],
    gpu,
    settings,
    q2rtx_config: Q2RTXStabilityConfig,
    runtime_options: dict,
    final_verification_duration_s: int,
    tail_rise_bins: int,
    log: Callable[[str], None],
    event_callback: AutoUvEventCallback | None,
) -> AutoUvVoltageScanResult:
    (
        recovery_plan,
        recovery_voltage_mv,
        recovery_lock_clock_mhz,
        recovery_probe,
        selected_final_duration_s,
        recovery_tail_rise_bins,
        recovery_record,
    ) = pending_recovery_selection
    final_verification_duration_s = int(selected_final_duration_s)
    stable_candidate = VfCurveCandidate(
        label="previous-crash-resume",
        voltage_mv=int(recovery_voltage_mv),
        target_mhz=int(recovery_lock_clock_mhz),
        flattened_plan=recovery_plan,
        metadata={"tail_rise_bins": int(recovery_tail_rise_bins)},
    )
    stable_probe = recovery_probe or probe_summary_from_candidate_record(
        recovery_record
    )
    if stable_probe is None:
        raise AutoUvError("Recovered Auto-UV candidate did not include probe metrics")
    recovered_base_probe = base_probe_summary_from_candidate_record(recovery_record)
    discovery_summary = recovered_base_probe or stable_probe
    baseline_voltage_mv = positive_int(
        recovery_record.get("base_candidate_voltage_mv")
    ) or recovery_initial_target_voltage_mv(
        [recovery_record],
        fallback_voltage_mv=int(recovery_voltage_mv),
    )
    baseline_clock_mhz = _float_or_none(
        recovery_record.get("base_avg_core_clock_mhz"),
        getattr(discovery_summary, "avg_core_clock_mhz", None),
        recovery_lock_clock_mhz,
    )
    baseline_lock_clock_mhz = positive_int(
        recovery_record.get("base_lock_clock_mhz")
    ) or int(round(float(baseline_clock_mhz or recovery_lock_clock_mhz)))
    baseline_candidate = VfCurveCandidate(
        label="previous-crash-resume-baseline",
        voltage_mv=int(baseline_voltage_mv),
        target_mhz=int(baseline_lock_clock_mhz),
        flattened_plan=base_curve,
        metadata={"tail_rise_bins": int(tail_rise_bins)},
    )
    baseline_target = SimpleNamespace(
        measured_clock_mhz=float(baseline_clock_mhz or baseline_lock_clock_mhz)
    )
    recovered_profile_tier = auto_uv_run_profile_tier(
        runtime_options,
        settings,
        tail_rise_bins=int(recovery_tail_rise_bins),
    )
    # Re-establish the board-power regime the candidate was measured under.
    # The verified-candidate pool is regime-heterogeneous (per-tier caps), so
    # resuming under whatever limit the fresh scan-open applied could
    # re-verify and save this candidate in a regime it never ran in. Records
    # from before this field resume under the current limit, logged.
    recorded_power_limit_w = positive_int(
        recovery_record.get("configured_power_limit_w")
    )
    if recorded_power_limit_w is None:
        log_phase(
            log,
            "crash-recovery",
            "saved candidate carries no recorded power limit; resuming under "
            f"the current {_format_power_limit_w(gpu.power_limit_w)} limit",
        )
    elif recorded_power_limit_w != positive_int(gpu.power_limit_w):
        gpu.requested_power_limit_w = int(recorded_power_limit_w)
        apply_pending_power_limit(
            gpu,
            log=log,
            purpose="crash-recovery regime",
        )
    runner = AutoUvProbeRunner(
        reader=gpu.reader,
        live_voltage_reader=gpu.live_voltage_reader,
        q2rtx_config=q2rtx_config,
        runtime_default_plan=gpu.runtime_default_plan,
        power_limit_w=gpu.power_limit_w,
        start_voltage_mv=int(baseline_candidate.voltage_mv),
        baseline_clock_mhz=float(baseline_target.measured_clock_mhz),
        min_performance_core_clock_pct=float(settings.min_performance_core_clock_pct),
        short_probe_base_duration_s=int(settings.short_probe_base_duration_s),
        log=log,
        marker_details=auto_uv_run_marker_details(
            runtime_options,
            settings,
            tail_rise_bins=int(recovery_tail_rise_bins),
            profile_tier=recovered_profile_tier,
        ),
        event_callback=event_callback,
    )
    probe_history: list[AutoUvProbeSummary] = []
    append_unique_probe_summary(probe_history, discovery_summary)
    append_unique_probe_summary(probe_history, stable_probe)
    stable_history: list[AutoUvProbeSummary] = []
    append_unique_probe_summary(stable_history, stable_probe)
    final_tail_rise_bins = int(recovery_tail_rise_bins)
    log_phase(
        log,
        "crash-recovery",
        f"resuming {str(settings.auto_uv_mode)} Auto-UV from saved candidate "
        "without baseline probe "
        f"candidate={int(recovery_voltage_mv)}mV@"
        f"{int(recovery_lock_clock_mhz)}MHz "
        f"base={int(baseline_voltage_mv)}mV@"
        f"{float(baseline_target.measured_clock_mhz):.0f}MHz",
    )
    replay_recovered_resume_probe_rows(
        event_callback=event_callback,
        base_probe=recovered_base_probe,
        stable_probe=stable_probe,
    )

    final_selection = select_final_scan_candidate(
        base_curve=base_curve,
        settings=settings,
        runtime_options=runtime_options,
        stable_plan=stable_candidate.flattened_plan,
        stable_voltage_mv=int(stable_candidate.voltage_mv),
        stable_lock_clock_mhz=int(stable_candidate.target_mhz),
        stable_probe=stable_probe,
        stable_history=stable_history,
        runner=runner,
        gpu=gpu,
        probe_history=probe_history,
        log=log,
        tail_rise_bins=int(final_tail_rise_bins),
        measured_baseline_clock_mhz=float(baseline_target.measured_clock_mhz),
        discovery_summary=discovery_summary,
        baseline_candidate=baseline_candidate,
        final_verification_duration_s=int(final_verification_duration_s),
        event_callback=event_callback,
        run_performance_auto_oc=settings.auto_uv_mode == AUTO_UV_MODE_PERFORMANCE,
        request_reason="sweep-complete",
    )

    apply_pending_power_limit(gpu, log=log)
    return run_final_verification_and_save(
        probe_voltage_candidate=probe_voltage_candidate,
        build_voltage_scan_result=build_voltage_scan_result,
        log=log,
        reader=gpu.reader,
        stable_plan=final_selection.plan,
        stable_voltage_mv=int(final_selection.voltage_mv),
        stable_lock_clock_mhz=int(final_selection.lock_clock_mhz),
        stable_probe=final_selection.probe,
        stable_history=stable_history,
        probe_history=probe_history,
        q2rtx_config=q2rtx_config,
        final_verification_duration_s=int(final_selection.verification_duration_s),
        start_voltage_mv=int(baseline_candidate.voltage_mv),
        measured_clock_mhz=float(baseline_target.measured_clock_mhz),
        nvml_session=gpu.live_voltage_reader,
        clock_ceiling=gpu.clock_ceiling,
        discovery_summary=discovery_summary,
        translated_gpu_policy=gpu.translated_gpu_policy,
        gpu_identity=getattr(gpu, "gpu_identity", {}),
        min_performance_core_clock_pct=float(settings.min_performance_core_clock_pct),
        runtime_default_plan=gpu.runtime_default_plan,
        final_clock_drop_margin_pct=float(settings.final_clock_drop_margin_pct),
        tail_rise_bins=int(final_tail_rise_bins),
        auto_uv_mode=str(settings.auto_uv_mode),
        generated_profile_tier=auto_uv_run_profile_tier(
            runtime_options,
            settings,
            tail_rise_bins=int(final_tail_rise_bins),
        ),
        auto_oc_metadata=dict(final_selection.auto_oc_metadata or {}),
        event_callback=event_callback,
    )




def _format_optional_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.2f}%"


def _format_power_limit_w(value: object) -> str:
    watts = positive_int(value)
    return f"{int(watts)}W" if watts is not None else "driver-managed"
