"""Verify and save the selected Auto-UV curve, preserving its tested shape."""

from __future__ import annotations

from runtime.support.vf_curve_plan import apply_plan
from stability.q2rtx.long_stability_config import (
    build_long_stability_test_config,
    long_stability_workload_durations,
)

from auto_uv.domain.console_log import log_benchmark, log_phase, log_user_stage
from auto_uv.domain.types import (
    AutoUvCriticalProbeError,
    AutoUvError,
    AutoUvProbeSummary,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from ..curve.rising_tail import tail_ceiling_clock_mhz
from ..shared.positive_int import positive_int
from auto_uv.probes.stability_decision import (
    evaluate_stable_run,
)
from auto_uv.domain.events import (
    AutoUvEventCallback,
    emit_auto_uv_event,
)
from auto_uv.probes.events import (
    emit_voltage_probe_finished,
    emit_voltage_probe_started,
)
from auto_uv.probes.event_payload import vf_curve_event_points
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome
from .crash_marker import (
    final_probe_crash_marker_details,
    memory_offset_from_gpu_policy,
)
from auto_uv.curve.shipped_plan import assert_monotonic_editable_targets
from .fan_curve import (
    FinalVerificationFanCurveResult,
    write_final_verification_fan_curve_payload,
)
from .result_files import (
    write_final_stable_result,
    write_final_verified_profile,
    write_last_stable_result_snapshot,
)
from ..persistence.verified_candidate_result_file import write_latest_verified_candidate
from ..persistence.unsafe_voltage_blacklist_file import load_unsafe_voltage_blacklist
from ..persistence.unsafe_voltage_cache import unsafe_voltage_block_reason


def run_final_verification_and_save(
    *,
    probe_voltage_candidate,
    build_voltage_scan_result,
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
    start_voltage_mv,
    measured_clock_mhz,
    nvml_session,
    clock_ceiling,
    discovery_summary,
    translated_gpu_policy,
    gpu_identity,
    runtime_default_plan,
    tail_rise_bins: int = 0,
    auto_uv_mode: str = "",
    generated_profile_tier: str = "",
    auto_oc_metadata: dict | None = None,
    event_callback: AutoUvEventCallback | None = None,
):
    gpu_policy = translated_gpu_policy if isinstance(translated_gpu_policy, dict) else {}
    final_voltage_mv = int(stable_voltage_mv)
    final_lock_clock_mhz = int(stable_lock_clock_mhz)
    final_plan = stable_plan
    final_status = "not-run"
    blocked = unsafe_voltage_block_reason(
        load_unsafe_voltage_blacklist(),
        candidate_voltage_mv=final_voltage_mv,
        lock_clock_mhz=final_lock_clock_mhz,
        profile_tier=generated_profile_tier,
    )
    if blocked:
        raise AutoUvError(f"Final verification blocked: {blocked}")

    # Verify the selected smooth curve intact. A separate sweep of flattened
    # lower points would replace its operating point and plot during the soak.
    assert_monotonic_editable_targets(final_plan)

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
        tail_rise_bins=int(tail_rise_bins),
    )
    log_phase(log, "final", f"last-stable-saved={last_stable_path}")
    apply_plan_and_refresh(reader, final_plan)

    final_config = build_long_stability_test_config(
        q2rtx_config,
        total_duration_s=int(final_verification_duration_s),
    )
    q2rtx_duration_s, cuda_duration_s = long_stability_workload_durations(
        int(final_verification_duration_s)
    )
    candidate = final_candidate(
        plan=final_plan,
        voltage_mv=int(final_voltage_mv),
        lock_clock_mhz=int(final_lock_clock_mhz),
        metadata=auto_oc_metadata,
    )
    emit_voltage_probe_started(
        event_callback,
        candidate,
        stage="final-verify",
        target_duration_s=int(final_verification_duration_s),
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
            ceiling_clock_mhz=tail_ceiling_clock_mhz(
                final_plan,
                fallback_clock_mhz=int(final_lock_clock_mhz),
                lock_voltage_mv=int(final_voltage_mv),
            ),
        )
        log_phase(log, "ceiling", clock_ceiling.describe())
    marker_details = final_probe_crash_marker_details(
        start_voltage_mv=int(start_voltage_mv),
        candidate_voltage_mv=int(final_voltage_mv),
        translated_gpu_policy=gpu_policy,
    )
    marker_details.update(
        {
            "auto_uv_mode": str(auto_uv_mode or ""),
            "generated_profile_tier": str(generated_profile_tier or ""),
            "tail_rise_bins": int(tail_rise_bins),
            "custom_target": bool((auto_oc_metadata or {}).get("custom_target")),
            "auto_oc": bool((auto_oc_metadata or {}).get("auto_oc")),
        }
    )
    final_probe, raw_result = probe_voltage_candidate(
        reader=reader,
        candidate_plan=final_plan,
        candidate_voltage_mv=int(final_voltage_mv),
        lock_clock_mhz=int(final_lock_clock_mhz),
        q2rtx_config=final_config,
        stable_history=(
            [stable_probe]
            if stable_probe is not None
            and (auto_oc_metadata or {}).get("custom_target")
            else stable_history
        ),
        initial_probe_clock_mhz=measured_clock_mhz,
        nvml_session=nvml_session,
        log=log,
        phase_label="final-verify",
        log_context="",
        power_limit_w=gpu_policy.get("power_limit_w"),
        reset_plan=runtime_default_plan,
        marker_details=marker_details,
        expected_total_duration_s=int(final_verification_duration_s),
        event_callback=event_callback,
    )
    probe_history.append(final_probe)
    decision = final_probe_stability_decision(
        raw_result,
        stable_history=stable_history,
        power_limit_w=gpu_policy.get("power_limit_w"),
        q2rtx_config=final_config,
        performance_reference=(
            stable_probe if (auto_oc_metadata or {}).get("custom_target") else None
        ),
    )
    outcome = VoltageProbeOutcome(
        decision=decision,
        measured_core_clock_mhz=final_probe.avg_core_clock_mhz,
        measured_voltage_mv=final_probe.avg_voltage_mv,
        raw_probe=final_probe,
        raw_result=raw_result,
    )
    emit_voltage_probe_finished(
        event_callback,
        candidate,
        outcome,
        stage="final-verify",
    )
    log_benchmark(
        log,
        phase="final-verify",
        probe=final_probe,
        reference_probe=discovery_summary,
        reference_label="initial",
    )
    if not decision.passed:
        raw_reason = str(getattr(raw_result, "reason", "") or "")
        reason = str(decision.reason or raw_reason or "unknown")
        log_phase(log, "final-verify", f"rejected {reason}")
        if decision.severity is FailureSeverity.CRITICAL:
            raise AutoUvCriticalProbeError(
                f"Final verification stopped after critical probe failure: {reason}"
            )
        raise AutoUvError(f"final long verification failed: {reason}")

    write_latest_verified_candidate(
        plan=final_plan,
        lock_clock_mhz=int(final_lock_clock_mhz),
        voltage_mv=int(final_voltage_mv),
        probe=final_probe,
        base_probe=discovery_summary,
        tail_rise_bins=int(tail_rise_bins),
        configured_power_limit_w=positive_int(
            gpu_policy.get("power_limit_w")
        ),
    )
    final_status = f"completed {format_user_duration(final_verification_duration_s)} long check"

    if final_plan is None:
        raise AutoUvError("final verification did not produce a final curve")
    profile_metrics_probe = choose_profile_metrics_probe(
        stable_probe=stable_probe,
        final_probe=final_probe,
        final_voltage_mv=int(final_voltage_mv),
        final_lock_clock_mhz=int(final_lock_clock_mhz),
    )
    stable_path = write_final_stable_result(
        plan=final_plan,
        lock_clock_mhz=int(final_lock_clock_mhz),
        voltage_mv=int(final_voltage_mv),
        probe=profile_metrics_probe,
        final_verification_probe=final_probe,
        verification_duration_s=int(final_verification_duration_s),
        tail_rise_bins=int(tail_rise_bins),
        power_limit_w=gpu_policy.get("power_limit_w"),
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
        probe=final_probe,
        final_verification_probe=final_probe,
        base_probe=discovery_summary,
        fan_curve_payload=fan_result.payload if fan_result is not None else None,
        memory_offset_mhz=memory_offset_from_gpu_policy(gpu_policy),
        power_limit_w=gpu_policy.get("power_limit_w"),
        tail_rise_bins=int(tail_rise_bins),
        auto_uv_mode=str(auto_uv_mode or ""),
        generated_profile_tier=str(generated_profile_tier or ""),
        gpu_identity=gpu_identity,
    )
    log_final_summary(
        log,
        profile_path=profile_path,
        voltage_mv=int(final_voltage_mv),
        lock_clock_mhz=int(final_lock_clock_mhz),
        final_status=final_status,
        final_probe=final_probe,
    )
    emit_auto_uv_event(
        event_callback,
        "candidate_curve",
        stage="final",
        voltage_mv=int(final_voltage_mv),
        clock_mhz=int(final_lock_clock_mhz),
        points=vf_curve_event_points(final_plan),
    )
    return build_voltage_scan_result(
        final_voltage_mv=int(final_voltage_mv),
        final_lock_clock_mhz=int(final_lock_clock_mhz),
        initial_probe=discovery_summary,
        probe_history=probe_history,
        final_probe=profile_metrics_probe,
    )


def final_probe_stability_decision(
    result,
    *,
    stable_history: list[AutoUvProbeSummary],
    power_limit_w: int | None,
    q2rtx_config,
    performance_reference: AutoUvProbeSummary | None = None,
) -> StableRunDecision:
    baseline = stable_history[0] if stable_history else None
    reference = performance_reference or baseline
    return evaluate_stable_run(
        result,
        baseline_fps=(reference.avg_fps if reference is not None else None),
        baseline_power_w=reference.avg_power_w if reference is not None else None,
        power_limit_w=power_limit_w,
        cuda_required=bool(getattr(q2rtx_config, "companion_command", None)),
        companion_result=(
            {"success": True}
            if bool(getattr(q2rtx_config, "companion_command", None))
            else None
        ),
        fatal_output_found=bool(getattr(result, "fatal_output_matches", [])),
        xid_found=bool(getattr(result, "xid_messages", [])),
    )


def final_candidate(
    *,
    plan: list[dict],
    voltage_mv: int,
    lock_clock_mhz: int,
    metadata: dict | None = None,
) -> VfCurveCandidate:
    return VfCurveCandidate(
        label=f"final-verify {int(voltage_mv)}mV",
        voltage_mv=int(voltage_mv),
        target_mhz=int(lock_clock_mhz),
        flattened_plan=plan,
        metadata=dict(metadata or {}),
    )


def choose_profile_metrics_probe(
    *,
    stable_probe,
    final_probe,
    final_voltage_mv: int,
    final_lock_clock_mhz: int,
):
    """Keep the comparable candidate metrics; long Q2 clock is stored separately."""
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
    emit_auto_uv_event(
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
