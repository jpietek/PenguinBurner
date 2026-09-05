"""Performance Auto-UV preset entry point.

Performance runs the shared base undervolt sweep first. After the base stable
candidate is known, the existing Auto-OC ladder can search higher clock targets
before final verification.
"""

from __future__ import annotations

from typing import Callable

from auto_uv.auto_oc.search import run_auto_oc_candidate_search
from auto_uv.domain.console_log import log_phase
from auto_uv.domain.types import AutoUvProbeSummary, VfCurveCandidate
from auto_uv.probes.runner import AutoUvProbeRunner
from auto_uv.scan_mode.auto_uv_mode import (
    AUTO_UV_MODE_BALANCED,
    AUTO_UV_MODE_EFFICIENCY,
    AUTO_UV_MODE_PERFORMANCE,
)
from auto_uv.scan_mode.uv_limits import uv_limit_profile_target_for_gpu
from auto_uv.scan_mode.efficiency_fps_per_w_policy import (
    best_efficiency_candidate_index,
)


def select_performance_auto_oc_candidate(
    base_curve: list[dict],
    *,
    auto_uv_mode: str,
    stable_plan: list[dict],
    stable_voltage_mv: int,
    stable_lock_clock_mhz: int,
    stable_probe: AutoUvProbeSummary | None,
    stable_history: list[AutoUvProbeSummary] | None,
    runner: AutoUvProbeRunner,
    gpu_name: object | None,
    clock_ceiling,
    probe_history: list[AutoUvProbeSummary],
    log: Callable[[str], None],
    tail_rise_bins: int = 0,
    target_voltage_mv: int | None = None,
    target_clock_mhz: int | None = None,
    measured_baseline_clock_mhz: float | int | None = None,
) -> tuple[list[dict], int, int, AutoUvProbeSummary | None, dict]:
    if str(auto_uv_mode) != AUTO_UV_MODE_PERFORMANCE:
        return (
            stable_plan,
            int(stable_voltage_mv),
            int(stable_lock_clock_mhz),
            stable_probe,
            {},
        )
    start_candidate = VfCurveCandidate(
        label="performance-auto-oc-start",
        voltage_mv=int(stable_voltage_mv),
        target_mhz=int(stable_lock_clock_mhz),
        flattened_plan=stable_plan,
    )
    result = run_auto_oc_candidate_search(
        base_curve=base_curve,
        start_candidate=start_candidate,
        start_probe=stable_probe,
        runner=runner,
        gpu_name=gpu_name,
        clock_ceiling=clock_ceiling,
        probe_history=probe_history,
        log=log,
        tail_rise_bins=int(tail_rise_bins),
        target_voltage_mv=target_voltage_mv,
        target_clock_mhz=target_clock_mhz,
        measured_baseline_clock_mhz=measured_baseline_clock_mhz,
    )
    if stable_history is not None:
        for attempt in getattr(result, "attempts", ()) or ():
            if (
                attempt.outcome.decision.passed
                and attempt.outcome.raw_probe is not None
            ):
                stable_history.append(attempt.outcome.raw_probe)
    # The chosen rung already contains its tested ramp and tail. Combining
    # lower-voltage anchors from other rungs would create an untested curve.
    selected = result.selected_candidate
    auto_oc_metadata = performance_auto_oc_progress_metadata(
        endpoint=getattr(result, "endpoint", None),
        measured_baseline_clock_mhz=measured_baseline_clock_mhz,
        selected_clock_mhz=int(selected.target_mhz),
    )
    return (
        selected.flattened_plan,
        int(selected.voltage_mv),
        int(selected.target_mhz),
        result.selected_probe,
        auto_oc_metadata,
    )


def select_power_bound_clock_reclaim_candidate(
    base_curve: list[dict],
    *,
    auto_uv_mode: str,
    stable_plan: list[dict],
    stable_voltage_mv: int,
    stable_lock_clock_mhz: int,
    stable_probe: AutoUvProbeSummary | None,
    stable_history: list[AutoUvProbeSummary],
    runner: AutoUvProbeRunner,
    gpu_name: object | None,
    clock_ceiling,
    probe_history: list[AutoUvProbeSummary],
    log: Callable[[str], None],
    tail_rise_bins: int = 0,
    measured_baseline_clock_mhz: float | int | None = None,
) -> tuple[list[dict], int, int, AutoUvProbeSummary | None, dict]:
    """Raise clock at the proven voltage after a capped savings-tier descent."""
    mode = str(auto_uv_mode)
    if mode not in {AUTO_UV_MODE_EFFICIENCY, AUTO_UV_MODE_BALANCED}:
        return (
            stable_plan,
            int(stable_voltage_mv),
            int(stable_lock_clock_mhz),
            stable_probe,
            {},
        )
    endpoint = uv_limit_profile_target_for_gpu(gpu_name, mode)
    if endpoint is None or int(endpoint.clock_mhz) <= int(stable_lock_clock_mhz):
        return (
            stable_plan,
            int(stable_voltage_mv),
            int(stable_lock_clock_mhz),
            stable_probe,
            {},
        )
    log_phase(
        log,
        "clock-reclaim",
        f"{mode} fixed-voltage climb "
        f"{int(stable_voltage_mv)}mV@{int(stable_lock_clock_mhz)}MHz -> "
        f"{int(endpoint.clock_mhz)}MHz",
    )
    start_candidate = VfCurveCandidate(
        label=f"{mode}-clock-reclaim-start",
        voltage_mv=int(stable_voltage_mv),
        target_mhz=int(stable_lock_clock_mhz),
        flattened_plan=stable_plan,
    )
    result = run_auto_oc_candidate_search(
        base_curve=base_curve,
        start_candidate=start_candidate,
        start_probe=stable_probe,
        runner=runner,
        gpu_name=gpu_name,
        clock_ceiling=clock_ceiling,
        probe_history=probe_history,
        log=log,
        tail_rise_bins=int(tail_rise_bins),
        target_voltage_mv=int(stable_voltage_mv),
        target_clock_mhz=int(endpoint.clock_mhz),
        measured_baseline_clock_mhz=measured_baseline_clock_mhz,
        target_profile_id=mode,
        probe_stable_history=stable_history,
    )
    for attempt in getattr(result, "attempts", ()) or ():
        if attempt.outcome.decision.passed and attempt.outcome.raw_probe is not None:
            stable_history.append(attempt.outcome.raw_probe)
    selected = result.selected_candidate
    selected_probe = result.selected_probe
    if mode == AUTO_UV_MODE_EFFICIENCY:
        # Keep the highest measured FPS/W, including
        # the point from which the ladder started.
        selected, selected_probe = start_candidate, stable_probe
        candidates = [(start_candidate, stable_probe)] + [
            (attempt.candidate, attempt.outcome.raw_probe)
            for attempt in result.attempts
            if attempt.outcome.decision.passed
        ]
        selected_index = best_efficiency_candidate_index([probe for _, probe in candidates])
        if selected_index is not None:
            selected, selected_probe = candidates[selected_index]
        log_phase(
            log,
            "clock-reclaim",
            f"Efficiency selected {selected.voltage_mv}mV@{selected.target_mhz}MHz "
            "by highest measured FPS/W",
        )
    selected_changed = int(selected.target_mhz) != int(stable_lock_clock_mhz)
    metadata = {
        "clock_reclaim": True,
        "clock_reclaim_start_mhz": int(stable_lock_clock_mhz),
        "clock_reclaim_target_mhz": int(endpoint.clock_mhz),
        "clock_reclaim_selected_mhz": int(selected.target_mhz),
        "clock_reclaim_voltage_mv": int(stable_voltage_mv),
    }
    return (
        selected.flattened_plan,
        int(selected.voltage_mv),
        int(selected.target_mhz),
        selected_probe if selected_changed else stable_probe,
        metadata,
    )


def performance_auto_oc_progress_metadata(
    *,
    endpoint,
    measured_baseline_clock_mhz: float | int | None,
    selected_clock_mhz: int,
) -> dict:
    """Auto-OC offset metadata relative to the measured baseline clock."""
    if endpoint is None or measured_baseline_clock_mhz is None:
        return {}
    baseline_clock = float(measured_baseline_clock_mhz)
    endpoint_clock = int(endpoint.clock_mhz)
    limit_mhz = int(round(float(endpoint_clock) - baseline_clock))
    applied_mhz = int(round(float(selected_clock_mhz) - baseline_clock))
    return {
        "auto_oc": True,
        "auto_oc_baseline_clock_mhz": round(baseline_clock, 2),
        "auto_oc_target_clock_mhz": endpoint_clock,
        "auto_oc_applied_mhz": applied_mhz,
        "auto_oc_limit_mhz": limit_mhz,
    }
