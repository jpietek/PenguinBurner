"""Run the final long verification loop for the selected Auto-UV curve.

The loop either proves the selected curve, spends remaining clock-recovery budget for low-clock misses, or raises voltage to the next stable bin.
"""

from __future__ import annotations

from afterburner.import_vf_curve import apply_plan

from ..auto_uv_console_log import log_benchmark, log_phase, log_user_stage
from ..auto_uv_types import (
    AutoUvError,
    AutoUvProbeSummary,
    StableRunDecision,
    VfCurveCandidate,
)
from ..curve.base_vf_curve_voltage_bins import next_higher_editable_voltage_bin
from ..q2rtx.probe_stability_decision import (
    StabilityThresholds,
    evaluate_stable_run,
)
from ..ui.clock_recovery_budget_ui_payload import clock_recovery_budget_ui_payload
from ..ui.ui_json_event_writer import AutoUvEventCallback, emit_ui_json_event
from ..ui.ui_voltage_probe_events import (
    emit_ui_voltage_probe_finished,
    emit_ui_voltage_probe_started,
)
from ..ui.vf_curve_ui_points import vf_curve_ui_points
from ..voltage_sweep_state import VoltageProbeOutcome
from .final_verification_clock_recovery import (
    build_final_clock_recovery_candidate,
    final_failure_allows_clock_recovery,
    format_clock_recovery_budget,
)
from .final_verification_crash_marker import (
    final_probe_crash_marker_details,
    memory_offset_from_gpu_policy,
)
from .final_verification_fan_curve import (
    FinalVerificationFanCurveResult,
    write_final_verification_fan_curve_payload,
)
from .final_verification_probe_config import (
    final_q2rtx_cuda_duration_s,
    final_q2rtx_cuda_probe_config,
)
from .final_verification_result_files import (
    write_final_stable_result,
    write_final_verified_profile,
    write_last_stable_result_snapshot,
)
from ..persistence.verified_candidate_result_file import write_latest_verified_candidate


def run_final_verification_and_save(
    *,
    probe_voltage_candidate,
    probe_stabilization_search,
    build_voltage_scan_result,
    curve_overclock_summary,
    log,
    reader,
    stable_plan,
    stable_voltage_mv,
    stable_lock_clock_mhz,
    stable_probe,
    stable_history,
    probe_history,
    q2rtx_config,
    final_verification_duration_s,
    source_result,
    start_voltage_mv,
    measured_clock_mhz,
    nvml_session,
    clock_ceiling,
    discovery_summary,
    translated_gpu_policy,
    min_performance_core_clock_pct,
    runtime_default_plan,
    final_clock_drop_margin_pct,
    clock_bump_budget_limit_pct,
    recovery_voltage_ceiling_mv=None,
    clock_bump_recovery_count=0,
    clock_bump_budget_used_pct=0.0,
    max_bump_recovery_was_used=False,
    short_probe_base_duration_s: int | None = None,
    timedemo_warmup_runs: int = 0,
    event_callback: AutoUvEventCallback | None = None,
):
    _ = curve_overclock_summary
    base_curve = list(source_result.get("plan") or runtime_default_plan or stable_plan)
    gpu_policy = translated_gpu_policy if isinstance(translated_gpu_policy, dict) else {}
    final_voltage_mv = int(stable_voltage_mv)
    final_lock_clock_mhz = int(stable_lock_clock_mhz)
    final_plan = stable_plan
    final_probe = None
    final_status = "not-run"
    recovery_count = max(0, int(clock_bump_recovery_count))
    budget_used_pct = max(0.0, float(clock_bump_budget_used_pct))
    budget_limit_pct = max(0.0, float(clock_bump_budget_limit_pct))
    recovery_marker_details = None

    log_phase(
        log,
        "final",
        f"last-stable={final_voltage_mv}mV@{final_lock_clock_mhz}MHz",
    )
    last_stable_path = write_last_stable_result_snapshot(
        plan=final_plan,
        lock_clock_mhz=final_lock_clock_mhz,
        voltage_mv=final_voltage_mv,
        probe=stable_probe,
    )
    log_phase(log, "final", f"last-stable-saved={last_stable_path}")
    apply_plan_and_refresh(reader, final_plan)

    final_config = final_q2rtx_cuda_probe_config(
        q2rtx_config,
        total_duration_s=int(final_verification_duration_s),
    )
    q2rtx_duration_s, cuda_duration_s = final_q2rtx_cuda_duration_s(
        int(final_verification_duration_s)
    )
    while final_plan is not None:
        candidate = final_candidate(
            plan=final_plan,
            voltage_mv=int(final_voltage_mv),
            lock_clock_mhz=int(final_lock_clock_mhz),
            budget_used_pct=float(budget_used_pct),
            budget_limit_pct=float(budget_limit_pct),
        )
        budget_payload = clock_recovery_budget_ui_payload(
            used_pct=float(budget_used_pct),
            limit_pct=float(budget_limit_pct),
            max_clock_drop_pct=float(final_clock_drop_margin_pct),
        )
        emit_ui_voltage_probe_started(
            event_callback,
            candidate,
            stage="final-verify",
            max_clock_drop_pct=float(final_clock_drop_margin_pct),
        )
        log_final_probe_start(
            log,
            voltage_mv=int(final_voltage_mv),
            lock_clock_mhz=int(final_lock_clock_mhz),
            total_duration_s=int(final_verification_duration_s),
            q2rtx_duration_s=int(q2rtx_duration_s),
            cuda_duration_s=int(cuda_duration_s),
        )
        if clock_ceiling is not None:
            clock_ceiling.retarget(
                lock_clock_mhz=int(final_lock_clock_mhz),
                lock_voltage_mv=int(final_voltage_mv),
            )
            log_phase(log, "ceiling", clock_ceiling.describe())
        final_probe, raw_result = probe_voltage_candidate(
            reader=reader,
            candidate_plan=final_plan,
            candidate_voltage_mv=int(final_voltage_mv),
            lock_clock_mhz=int(final_lock_clock_mhz),
            q2rtx_config=final_config,
            stable_history=stable_history,
            initial_probe_clock_mhz=measured_clock_mhz,
            nvml_session=nvml_session,
            log=log,
            phase_label="final-recovery" if recovery_marker_details else "final-verify",
            log_context=format_clock_recovery_budget(
                used_pct=float(budget_used_pct),
                limit_pct=float(budget_limit_pct),
            ),
            power_limit_w=gpu_policy.get("power_limit_w"),
            min_performance_core_clock_pct=float(min_performance_core_clock_pct),
            enforce_target_core_clock_floor=False,
            reset_plan=runtime_default_plan,
            suppress_unsafe_for_controlled_clock_abort=(
                bool(max_bump_recovery_was_used)
                or float(budget_used_pct) < float(budget_limit_pct)
            ),
            marker_details=final_probe_crash_marker_details(
                start_voltage_mv=int(start_voltage_mv),
                candidate_voltage_mv=int(final_voltage_mv),
                budget_payload=budget_payload,
                translated_gpu_policy=gpu_policy,
                recovery_marker_details=recovery_marker_details,
            ),
            expected_total_duration_s=int(final_verification_duration_s),
            timedemo_warmup_runs=int(timedemo_warmup_runs),
            event_callback=event_callback,
        )
        probe_history.append(final_probe)
        decision = final_probe_stability_decision(
            raw_result,
            stable_history=stable_history,
            power_limit_w=gpu_policy.get("power_limit_w"),
            q2rtx_config=final_config,
            min_performance_core_clock_pct=float(min_performance_core_clock_pct),
        )
        outcome = VoltageProbeOutcome(
            decision=decision,
            measured_core_clock_mhz=final_probe.avg_core_clock_mhz,
            measured_voltage_mv=final_probe.avg_voltage_mv,
            raw_probe=final_probe,
            raw_result=raw_result,
        )
        emit_ui_voltage_probe_finished(
            event_callback,
            candidate,
            outcome,
            stage="final-verify",
            max_clock_drop_pct=float(final_clock_drop_margin_pct),
        )
        log_benchmark(
            log,
            phase="final-verify",
            probe=final_probe,
            reference_probe=discovery_summary,
            reference_label="initial",
        )
        if decision.passed:
            write_latest_verified_candidate(
                plan=final_plan,
                lock_clock_mhz=int(final_lock_clock_mhz),
                voltage_mv=int(final_voltage_mv),
                probe=final_probe,
                base_probe=discovery_summary,
            )
            final_status = f"completed {format_user_duration(final_verification_duration_s)} long check"
            break

        raw_reason = str(getattr(raw_result, "reason", "") or "")
        reason = str(decision.reason or raw_reason or "unknown")
        log_phase(log, "final-verify", f"rejected {reason}")
        if final_failure_allows_clock_recovery(decision, raw_reason=raw_reason):
            recovery = maybe_build_final_clock_recovery(
                base_curve,
                voltage_mv=int(final_voltage_mv),
                previous_target_mhz=int(final_lock_clock_mhz),
                final_probe=final_probe,
                baseline_clock_mhz=measured_clock_mhz,
                max_clock_drop_pct=float(final_clock_drop_margin_pct),
                budget_used_pct=float(budget_used_pct),
                budget_limit_pct=float(budget_limit_pct),
                reason=reason,
            )
            if recovery is not None:
                recovery_count += 1
                final_plan = recovery.candidate.flattened_plan
                final_lock_clock_mhz = int(recovery.candidate.target_mhz)
                budget_used_pct = float(recovery.budget_used_pct)
                recovery_marker_details = recovery.marker_details
                max_bump_recovery_was_used = budget_used_pct >= budget_limit_pct
                log_clock_recovery_retry(
                    log,
                    voltage_mv=int(final_voltage_mv),
                    target_mhz=int(final_lock_clock_mhz),
                    attempt=int(recovery_count),
                    budget_used_pct=float(budget_used_pct),
                    budget_limit_pct=float(budget_limit_pct),
                    reason=reason,
                )
                continue
            final_status = "accepted lowest curve after clock-floor guardrail miss"
            log_user_stage(
                log,
                "Final long verification",
                [
                    "The selected curve only missed the loaded-clock floor.",
                    "Keeping the lowest-voltage curve instead of raising voltage for this guardrail.",
                ],
            )
            break

        recovery_candidate, recovery_summary, recovery_result = probe_stabilization_search(
            reader=reader,
            plan_source=base_curve,
            failure_voltage_mv=int(final_voltage_mv),
            failure_live_voltage_mv=final_probe.live_voltage_after_mv,
            minimum_candidate_voltage_mv=next_higher_editable_voltage_bin(
                base_curve,
                int(final_voltage_mv),
            ),
            target_clock_mhz=int(final_lock_clock_mhz),
            q2rtx_config=q2rtx_config,
            stable_history=stable_history,
            nvml_session=nvml_session,
            clock_ceiling=clock_ceiling,
            log=log,
            probe_history=probe_history,
            baseline_probe=discovery_summary,
            initial_target_voltage_mv=int(start_voltage_mv),
            initial_probe_clock_mhz=measured_clock_mhz,
            power_limit_w=gpu_policy.get("power_limit_w"),
            min_performance_core_clock_pct=float(min_performance_core_clock_pct),
            max_candidate_voltage_mv=recovery_voltage_ceiling_mv,
            short_probe_base_duration_s=short_probe_base_duration_s,
            reset_plan=runtime_default_plan,
            timedemo_warmup_runs=int(timedemo_warmup_runs),
            event_callback=event_callback,
        )
        if recovery_candidate is None or recovery_summary is None or recovery_result is None:
            raise AutoUvError(
                "final long verification failed and upward voltage recovery found no stable point"
            )
        final_plan = recovery_candidate.plan
        final_voltage_mv = int(recovery_candidate.candidate_voltage_mv)
        final_lock_clock_mhz = int(recovery_candidate.target_clock_mhz)
        stable_probe = recovery_summary
        stable_history.append(recovery_summary)
        if not recovery_summary.used_companion_load:
            write_latest_verified_candidate(
                plan=final_plan,
                lock_clock_mhz=int(final_lock_clock_mhz),
                voltage_mv=int(final_voltage_mv),
                probe=stable_probe,
                base_probe=discovery_summary,
            )

    if final_plan is None:
        raise AutoUvError("final verification did not produce a final curve")
    final_comparison_probe = choose_final_comparison_probe(
        stable_probe=stable_probe,
        final_probe=final_probe,
        final_voltage_mv=int(final_voltage_mv),
        final_lock_clock_mhz=int(final_lock_clock_mhz),
    )
    stable_path = write_final_stable_result(
        plan=final_plan,
        lock_clock_mhz=int(final_lock_clock_mhz),
        voltage_mv=int(final_voltage_mv),
        probe=final_comparison_probe,
        verification_duration_s=int(final_verification_duration_s),
    )
    log_phase(log, "final", f"stable-config-saved={stable_path}")
    fan_result = write_final_verification_fan_curve_payload(
        final_probe=final_probe,
        probes=probe_history,
    )
    log_fan_curve_result(log, event_callback, fan_result)
    profile_path = write_final_verified_profile(
        plan=final_plan,
        lock_clock_mhz=int(final_lock_clock_mhz),
        voltage_mv=int(final_voltage_mv),
        probe=final_comparison_probe,
        base_probe=discovery_summary,
        fan_curve_payload=fan_result.payload if fan_result is not None else None,
        memory_offset_mhz=memory_offset_from_gpu_policy(gpu_policy),
    )
    log_final_summary(
        log,
        profile_path=profile_path,
        voltage_mv=int(final_voltage_mv),
        lock_clock_mhz=int(final_lock_clock_mhz),
        final_status=final_status,
        final_probe=final_comparison_probe,
    )
    emit_ui_json_event(
        event_callback,
        "candidate_curve",
        stage="final",
        voltage_mv=int(final_voltage_mv),
        clock_mhz=int(final_lock_clock_mhz),
        points=vf_curve_ui_points(final_plan),
        **clock_recovery_budget_ui_payload(
            used_pct=float(budget_used_pct),
            limit_pct=float(budget_limit_pct),
            max_clock_drop_pct=float(final_clock_drop_margin_pct),
        ),
    )
    return build_voltage_scan_result(
        final_voltage_mv=int(final_voltage_mv),
        final_lock_clock_mhz=int(final_lock_clock_mhz),
        initial_probe=discovery_summary,
        probe_history=probe_history,
        final_probe=final_comparison_probe,
    )


def final_probe_stability_decision(
    result,
    *,
    stable_history: list[AutoUvProbeSummary],
    power_limit_w: int | None,
    q2rtx_config,
    min_performance_core_clock_pct: float,
) -> StableRunDecision:
    baseline = stable_history[0] if stable_history else None
    return evaluate_stable_run(
        result,
        baseline_frames=baseline.frames_per_run if baseline is not None else None,
        baseline_fps=baseline.avg_fps if baseline is not None else None,
        baseline_power_w=baseline.avg_power_w if baseline is not None else None,
        baseline_core_clock_mhz=(
            baseline.avg_core_clock_mhz if baseline is not None else None
        ),
        power_limit_w=power_limit_w,
        cuda_required=bool(getattr(q2rtx_config, "companion_command", None)),
        companion_result=(
            {"success": True}
            if bool(getattr(q2rtx_config, "companion_command", None))
            else None
        ),
        fatal_output_found=bool(getattr(result, "fatal_output_matches", [])),
        xid_found=bool(getattr(result, "xid_messages", [])),
        thresholds=StabilityThresholds(
            min_core_clock_pct=float(min_performance_core_clock_pct)
        ),
    )


def maybe_build_final_clock_recovery(
    base_curve: list[dict],
    *,
    voltage_mv: int,
    previous_target_mhz: int,
    final_probe: AutoUvProbeSummary,
    baseline_clock_mhz: float | None,
    max_clock_drop_pct: float,
    budget_used_pct: float,
    budget_limit_pct: float,
    reason: str,
):
    if float(budget_used_pct) >= float(budget_limit_pct):
        return None
    measured_target_mhz = int(final_probe.avg_core_clock_mhz or previous_target_mhz)
    return build_final_clock_recovery_candidate(
        base_curve,
        voltage_mv=int(voltage_mv),
        previous_target_mhz=int(previous_target_mhz),
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=baseline_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
        current_budget_used_pct=float(budget_used_pct),
        budget_limit_pct=float(budget_limit_pct),
        clock_cap_mhz=baseline_clock_mhz,
        reason=str(reason),
    )


def final_candidate(
    *,
    plan: list[dict],
    voltage_mv: int,
    lock_clock_mhz: int,
    budget_used_pct: float,
    budget_limit_pct: float,
) -> VfCurveCandidate:
    return VfCurveCandidate(
        label=(
            f"final-verify {int(voltage_mv)}mV "
            f"recovery-budget={float(budget_used_pct):.2f}/"
            f"{float(budget_limit_pct):.2f}%"
        ),
        voltage_mv=int(voltage_mv),
        target_mhz=int(lock_clock_mhz),
        flattened_plan=plan,
    )


def choose_final_comparison_probe(
    *,
    stable_probe,
    final_probe,
    final_voltage_mv: int,
    final_lock_clock_mhz: int,
):
    if (
        stable_probe is not None
        and int(stable_probe.candidate_voltage_mv) == int(final_voltage_mv)
        and int(stable_probe.lock_clock_mhz) == int(final_lock_clock_mhz)
    ):
        return stable_probe
    return final_probe or stable_probe


def apply_plan_and_refresh(reader, plan: list[dict]) -> None:
    if plan is None:
        return
    apply_plan(reader, plan)
    if hasattr(reader, "refresh_points"):
        reader.refresh_points()


def log_final_probe_start(
    log,
    *,
    voltage_mv: int,
    lock_clock_mhz: int,
    total_duration_s: int,
    q2rtx_duration_s: int,
    cuda_duration_s: int,
) -> None:
    log_phase(
        log,
        "final-verify",
        f"starting total-duration={int(total_duration_s)}s "
        f"q2rtx-duration={int(q2rtx_duration_s)}s "
        f"cuda-duration={int(cuda_duration_s)}s "
        f"target={int(lock_clock_mhz)}MHz voltage={int(voltage_mv)}mV",
    )
    log_user_stage(
        log,
        "Final long verification",
        [
            f"Candidate: {int(lock_clock_mhz)}MHz at {int(voltage_mv)}mV.",
            f"Running {format_user_duration(q2rtx_duration_s)} of Q2RTX plus {format_user_duration(cuda_duration_s)} of CUDA load.",
        ],
    )


def log_clock_recovery_retry(
    log,
    *,
    voltage_mv: int,
    target_mhz: int,
    attempt: int,
    budget_used_pct: float,
    budget_limit_pct: float,
    reason: str,
) -> None:
    log_phase(
        log,
        "final-verify",
        f"clock-recovery-retry attempt={int(attempt)} "
        f"{format_clock_recovery_budget(used_pct=budget_used_pct, limit_pct=budget_limit_pct)} "
        f"voltage={int(voltage_mv)}mV target={int(target_mhz)}MHz reason={reason}",
    )


def log_fan_curve_result(
    log,
    event_callback: AutoUvEventCallback | None,
    fan_result: FinalVerificationFanCurveResult | None,
) -> None:
    if fan_result is None:
        return
    if fan_result.blocked:
        log_phase(
            log,
            "fan-tune",
            f"curve-blocked reason={fan_result.block_reason or 'unknown'} marker={fan_result.path}",
        )
        return
    log_phase(
        log,
        "fan-tune",
        f"curve-saved={fan_result.path} points={len(fan_result.curve)}",
    )
    emit_ui_json_event(
        event_callback,
        "fan_curve_suggested",
        curve=fan_result.curve,
        measured_points=fan_result.payload.get("telemetry", {}).get(
            "measured_fan_points",
            [],
        ),
        loaded_temperature_c=fan_result.payload.get("load_anchor_temperature_c"),
        load_anchor_fan_speed_pct=fan_result.payload.get("load_anchor_fan_speed_pct"),
    )


def log_final_summary(
    log,
    *,
    profile_path,
    voltage_mv: int,
    lock_clock_mhz: int,
    final_status: str,
    final_probe: AutoUvProbeSummary | None,
) -> None:
    fps = "n/a" if final_probe is None or final_probe.avg_fps is None else f"{final_probe.avg_fps:.2f}"
    clock = (
        "n/a"
        if final_probe is None or final_probe.avg_core_clock_mhz is None
        else f"{final_probe.avg_core_clock_mhz:.0f}MHz"
    )
    log_phase(
        log,
        "final",
        f"curve-saved={profile_path} voltage={int(voltage_mv)}mV "
        f"target={int(lock_clock_mhz)}MHz measured-clock={clock} fps={fps} "
        f"status={final_status}",
    )


def format_user_duration(duration_s: int | float | None) -> str:
    if duration_s is None:
        return "n/a"
    seconds = int(round(float(duration_s)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, remaining_seconds = divmod(seconds, 60)
    if remaining_seconds == 0:
        return f"{minutes}min"
    return f"{minutes}min {remaining_seconds}s"
