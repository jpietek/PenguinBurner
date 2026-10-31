"""Recheck a contradictory Performance near-tie without changing curve geometry."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from statistics import mean

from auto_uv.auto_oc.search import retarget_clock_ceiling
from auto_uv.domain.types import (
    AutoUvCriticalProbeError,
    AutoUvProbeSummary,
    FailureKind,
    VfCurveCandidate,
)
from auto_uv.persistence.unsafe_voltage_blacklist_file import (
    load_unsafe_voltage_blacklist,
)
from auto_uv.persistence.unsafe_voltage_cache import unsafe_voltage_block_reason
from auto_uv.probes.config import short_q2rtx_probe_config
from auto_uv.probes.runner import AutoUvProbeRunner
from stability.q2rtx.cuda_companion import cuda_bruteforce_companion_command

# This window triggers more measurement; it never discards a small FPS gain.
PERFORMANCE_COMPARISON_WINDOW_PCT = 2.0
PERFORMANCE_COMPARISON_Q2RTX_S = 30
PERFORMANCE_COMPARISON_CUDA_S = 5


def performance_comparison_pair(
    history: list[AutoUvProbeSummary],
) -> tuple[AutoUvProbeSummary, AutoUvProbeSummary] | None:
    """Find an FPS leader with a nearly tied, faster-clocked, lower-power rival."""
    latest = {
        (probe.candidate_voltage_mv, probe.lock_clock_mhz): probe
        for probe in history
    }
    eligible = [probe for probe in latest.values() if _valid_metrics(probe)]
    if len(eligible) < 2:
        return None
    leader = max(eligible, key=lambda probe: float(probe.avg_fps or 0))
    rivals = [
        probe for probe in eligible
        if probe is not leader
        and float(probe.avg_fps or 0) >= float(leader.avg_fps or 0)
        * (1 - PERFORMANCE_COMPARISON_WINDOW_PCT / 100)
        and float(probe.avg_core_clock_mhz or 0) > float(leader.avg_core_clock_mhz or 0)
        and float(probe.avg_power_w or 0) < float(leader.avg_power_w or 0)
        and probe.tested_plan != leader.tested_plan
    ]
    if not rivals:
        return None
    return leader, max(rivals, key=lambda probe: float(probe.avg_fps or 0))


def compare_performance_finalists(
    pair: tuple[AutoUvProbeSummary, AutoUvProbeSummary],
    *,
    runner: AutoUvProbeRunner,
    clock_ceiling,
    tail_rise_bins: int,
    probe_history: list[AutoUvProbeSummary],
    log: Callable[[str], None],
) -> tuple[AutoUvProbeSummary, list[AutoUvProbeSummary]]:
    """Run A/B/B/A at equal duration, then rank their mean FPS without a deadband.

    Only exact, already-passed plans are replayed. Failures stop this optional
    comparison through the normal probe safety path; stale passes must not win.
    The selected curve still needs the ordinary final verification.
    """
    config = replace(
        short_q2rtx_probe_config(
            runner.q2rtx_config, target_duration_s=PERFORMANCE_COMPARISON_Q2RTX_S,
        ),
        companion_command=cuda_bruteforce_companion_command(
            gpu_index=int(runner.q2rtx_config.gpu_index),
            duration_s=PERFORMANCE_COMPARISON_CUDA_S,
        ),
    )
    log(
        "Auto-UV phase=performance-comparison close FPS results disagree with "
        "clock and power; comparing two passed curves twice each "
        "(30s Q2RTX + 5s CUDA per run, A/B/B/A)."
    )
    samples: list[list[AutoUvProbeSummary]] = [[], []]
    for index in (0, 1, 1, 0):
        probe = pair[index]
        blocked = unsafe_voltage_block_reason(
            load_unsafe_voltage_blacklist(),
            candidate_voltage_mv=probe.candidate_voltage_mv,
            lock_clock_mhz=probe.lock_clock_mhz,
            profile_tier="performance",
        )
        if blocked:
            raise AutoUvCriticalProbeError(f"Performance comparison stopped: {blocked}")
        candidate = VfCurveCandidate(
            "performance-comparison", probe.candidate_voltage_mv, probe.lock_clock_mhz,
            [dict(point) for point in probe.tested_plan or []],
            metadata={"tail_rise_bins": tail_rise_bins, "auto_uv_mode": "performance"},
        )
        retarget_clock_ceiling(clock_ceiling, candidate=candidate, log=log)
        outcome = runner.probe_candidate(
            candidate, stable_history=list(pair), phase_label="candidate",
            summarize_saturated_tail=False, use_power_limit_floor=False,
            use_companion_load=True, config_override=config,
        )
        if outcome.raw_probe is not None:
            probe_history.append(outcome.raw_probe)
        if outcome.decision.failure_kind is FailureKind.USER_STOP:
            raise KeyboardInterrupt
        if not outcome.decision.passed or not _valid_metrics(outcome.raw_probe):
            reason = (
                outcome.decision.reason if not outcome.decision.passed
                else "missing or invalid comparison metrics"
            )
            raise AutoUvCriticalProbeError(
                "Performance comparison stopped; final verification not started: "
                f"{reason}"
            )
        assert outcome.raw_probe is not None
        samples[index].append(outcome.raw_probe)

    compared = [_comparison_summary(group) for group in samples]
    # An exact tie retains the original leader. No fixed threshold suppresses
    # a repeatable small Performance gain on another GPU or tail shape.
    winner = max(compared, key=lambda probe: float(probe.avg_fps or 0))
    log(
        "Auto-UV phase=performance-comparison "
        + "; ".join(
            f"{probe.candidate_voltage_mv}mV@{probe.lock_clock_mhz}MHz "
            f"mean={probe.avg_fps:.3f}FPS"
            for probe in compared
        )
        + f"; selected={winner.candidate_voltage_mv}mV@{winner.lock_clock_mhz}MHz; "
        "long final verification still required"
    )
    return winner, compared


def _valid_metrics(probe: AutoUvProbeSummary | None) -> bool:
    return probe is not None and bool(probe.tested_plan) and all(
        value is not None and math.isfinite(value) and value > 0
        for value in (probe.avg_fps, probe.avg_power_w, probe.avg_core_clock_mhz)
    )


def _comparison_summary(samples: list[AutoUvProbeSummary]) -> AutoUvProbeSummary:
    fps = mean(float(probe.avg_fps or 0) for probe in samples)
    power = mean(float(probe.avg_power_w or 0) for probe in samples)
    clock = mean(float(probe.avg_core_clock_mhz or 0) for probe in samples)
    return replace(
        samples[-1], avg_fps=fps, avg_power_w=power, avg_core_clock_mhz=clock,
        efficiency_fps_per_w=fps / power, efficiency_mhz_per_w=clock / power,
        watts_per_mhz=power / clock, result_reason="performance-comparison",
        comparison_runs=len(samples),
        comparison_duration_s=PERFORMANCE_COMPARISON_Q2RTX_S + PERFORMANCE_COMPARISON_CUDA_S,
    )
