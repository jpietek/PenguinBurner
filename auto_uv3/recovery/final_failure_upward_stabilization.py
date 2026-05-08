"""Raise voltage after final verification fails for reasons other than low-clock tolerance.

The target clock is kept fixed; only real editable base V/F voltage bins are tried upward.
"""

from __future__ import annotations

from types import SimpleNamespace

from ..auto_uv_console_log import log_benchmark, log_phase
from ..auto_uv_types import AutoUvProbeSummary
from ..curve.base_vf_curve_voltage_bins import higher_editable_voltage_bins
from ..q2rtx.probe_stability_decision import evaluate_stable_run
from ..q2rtx.q2rtx_cuda_probe_config import q2rtx_cuda_probe_config_for_voltage_band
from ..q2rtx.q2rtx_cuda_voltage_probe import probe_voltage_candidate
from ..curve.vf_curve_flattening import build_flattened_plan


def find_upward_stable_final_candidate(
    *,
    reader,
    plan_source: list[dict],
    failure_voltage_mv: int,
    failure_live_voltage_mv: int | None,
    minimum_candidate_voltage_mv: int | None,
    target_clock_mhz: int,
    q2rtx_config,
    stable_history: list[AutoUvProbeSummary],
    nvml_session,
    clock_ceiling,
    log,
    probe_history: list[AutoUvProbeSummary],
    baseline_probe: AutoUvProbeSummary | None,
    initial_target_voltage_mv: int,
    initial_probe_clock_mhz: float | None,
    power_limit_w: int | None,
    min_performance_core_clock_pct: float,
    max_candidate_voltage_mv: int | None = None,
    short_probe_base_duration_s: int | None = None,
    reset_plan: list[dict] | None = None,
    timedemo_warmup_runs: int = 0,
    event_callback=None,
) -> tuple[object | None, AutoUvProbeSummary | None, object | None]:
    _ = failure_live_voltage_mv
    floor_mv = int(minimum_candidate_voltage_mv or failure_voltage_mv)
    upward_bins = [
        int(value)
        for value in higher_editable_voltage_bins(plan_source, floor_mv - 1)
        if int(value) >= int(floor_mv)
        and (
            max_candidate_voltage_mv is None
            or int(value) <= int(max_candidate_voltage_mv)
        )
    ]
    if max_candidate_voltage_mv is not None:
        log_phase(
            log,
            "stabilize",
            f"voltage-ceiling={int(max_candidate_voltage_mv)}mV",
        )
    for voltage_mv in upward_bins:
        plan = build_flattened_plan(
            plan_source,
            lock_clock_mhz=int(target_clock_mhz),
            candidate_voltage_mv=int(voltage_mv),
        )
        log_phase(log, "stabilize", f"try={voltage_mv}mV@{int(target_clock_mhz)}MHz")
        if clock_ceiling is not None:
            clock_ceiling.retarget(
                lock_clock_mhz=int(target_clock_mhz),
                lock_voltage_mv=int(voltage_mv),
            )
            log_phase(log, "ceiling", clock_ceiling.describe())
        probe_config = q2rtx_cuda_probe_config_for_voltage_band(
            q2rtx_config,
            initial_target_voltage_mv=int(initial_target_voltage_mv),
            candidate_voltage_mv=int(voltage_mv),
            base_duration_s=short_probe_base_duration_s,
        )
        summary, result = probe_voltage_candidate(
            reader=reader,
            candidate_plan=plan,
            candidate_voltage_mv=int(voltage_mv),
            lock_clock_mhz=int(target_clock_mhz),
            q2rtx_config=probe_config,
            stable_history=stable_history,
            initial_probe_clock_mhz=initial_probe_clock_mhz,
            nvml_session=nvml_session,
            log=log,
            phase_label="stabilize",
            power_limit_w=power_limit_w,
            min_performance_core_clock_pct=float(min_performance_core_clock_pct),
            reset_plan=reset_plan,
            timedemo_warmup_runs=int(timedemo_warmup_runs),
            event_callback=event_callback,
        )
        probe_history.append(summary)
        log_benchmark(
            log,
            phase="stabilize",
            probe=summary,
            reference_probe=baseline_probe,
            reference_label="initial",
        )
        if result.success and final_recovery_probe_passed(
            summary,
            result,
            stable_history=stable_history,
            power_limit_w=power_limit_w,
        ):
            return (
                SimpleNamespace(
                    plan=plan,
                    candidate_voltage_mv=int(voltage_mv),
                    target_clock_mhz=int(target_clock_mhz),
                ),
                summary,
                result,
            )
        log_phase(log, "stabilize", f"rejected {getattr(result, 'reason', 'unknown')}")
    return None, None, None


def final_recovery_probe_passed(
    summary: AutoUvProbeSummary,
    result,
    *,
    stable_history: list[AutoUvProbeSummary],
    power_limit_w: int | None,
) -> bool:
    baseline = stable_history[0] if stable_history else None
    decision = evaluate_stable_run(
        result,
        baseline_frames=baseline.frames_per_run if baseline is not None else None,
        baseline_fps=baseline.avg_fps if baseline is not None else None,
        baseline_power_w=baseline.avg_power_w if baseline is not None else None,
        baseline_core_clock_mhz=(
            baseline.avg_core_clock_mhz if baseline is not None else None
        ),
        power_limit_w=power_limit_w,
        cuda_required=bool(getattr(result, "companion_command", None)),
        companion_result={"success": True},
        fatal_output_found=bool(getattr(result, "fatal_output_matches", [])),
        xid_found=bool(getattr(result, "xid_messages", [])),
    )
    _ = summary
    return bool(decision.passed)
