from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ..shared.probe_data_fields import read_field


@dataclass(frozen=True, slots=True)
class EfficiencyPolicyRules:
    min_fps_per_w_gain_pct: float = 1.0
    max_temperature_delta_c: float = 10.0
    power_pct_per_c: float = 0.5


def best_efficiency_candidate_index(probes: list[Any]) -> int | None:
    """Highest measured FPS/W; measured clock breaks equal-efficiency ties.

    Compare unrounded measurements against a single best score, not successive
    candidates: otherwise many individually small losses can compound. Clock
    targets never substitute for a measured clock. Temperature-normalized
    comparisons remain diagnostic and continue to guide descent stopping.
    """
    scored = []
    for index, probe in enumerate(probes):
        score = read_field(probe, "efficiency_fps_per_w")
        fps, power = read_field(probe, "avg_fps"), read_field(probe, "avg_power_w")
        if fps is not None and power is not None and float(power) > 0:
            score = float(fps) / float(power)
        if score is None or not math.isfinite(float(score)) or float(score) <= 0:
            continue
        clock = read_field(probe, "q2rtx_avg_core_clock_mhz")
        if clock is None:
            clock = read_field(probe, "avg_core_clock_mhz")
        measured_clock = (
            float(clock) if clock is not None and math.isfinite(float(clock)) else -1.0
        )
        watts = float(power) if power is not None and math.isfinite(float(power)) and float(power) > 0 else math.inf
        scored.append((index, float(score), measured_clock, watts))
    if not scored:
        return None
    return max(scored, key=lambda item: (item[1], item[2], -item[3], -item[0]))[0]


@dataclass(frozen=True, slots=True)
class EfficiencyStopDecision:
    should_stop: bool
    confirmations: int
    use_current_curve: bool
    reason: str


@dataclass(frozen=True, slots=True)
class EfficiencyStopStreakDefault:
    value: int
    source: str
    fps_variance_pct: float | None
    threshold_pct: float


def derive_efficiency_stop_streak_from_fps_variance(
    probe: Any | None,
    *,
    configured_streak: int,
    derive: bool,
    high_variance_threshold_pct: float = 2.0,
    low_variance_streak: int = 2,
    high_variance_streak: int = 4,
) -> EfficiencyStopStreakDefault:
    threshold = max(0.0, float(high_variance_threshold_pct))
    configured = max(0, int(configured_streak))
    if not bool(derive):
        return EfficiencyStopStreakDefault(
            configured,
            "user",
            fps_variance_pct(probe),
            threshold,
        )

    variance_pct = fps_variance_pct(probe)
    if variance_pct is None:
        return EfficiencyStopStreakDefault(
            configured,
            "fallback",
            None,
            threshold,
        )
    if float(variance_pct) >= threshold:
        return EfficiencyStopStreakDefault(
            max(0, int(high_variance_streak)),
            "high-fps-variance",
            float(variance_pct),
            threshold,
        )
    return EfficiencyStopStreakDefault(
        max(0, int(low_variance_streak)),
        "low-fps-variance",
        float(variance_pct),
        threshold,
    )


def fps_variance_pct(probe: Any | None) -> float | None:
    if probe is None:
        return None
    value = read_field(probe, "fps_variance_pct")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def compare_temperature_normalized_fps_per_w(
    previous_probe: Any | None,
    candidate_probe: Any | None,
    *,
    rules: EfficiencyPolicyRules = EfficiencyPolicyRules(),
) -> dict[str, float | bool | None]:
    reference_temperature_c = reference_temperature(previous_probe, candidate_probe)
    previous_power_w = temperature_normalized_power_w(
        previous_probe,
        reference_temperature_c=reference_temperature_c,
        rules=rules,
    )
    candidate_power_w = temperature_normalized_power_w(
        candidate_probe,
        reference_temperature_c=reference_temperature_c,
        rules=rules,
    )
    previous_fps_per_w = temperature_normalized_fps_per_w(
        previous_probe,
        normalized_power_w=previous_power_w,
    )
    candidate_fps_per_w = temperature_normalized_fps_per_w(
        candidate_probe,
        normalized_power_w=candidate_power_w,
    )
    measured_voltage_drop_mv = measured_voltage_drop(previous_probe, candidate_probe)
    requested_voltage_drop_mv = requested_voltage_drop(previous_probe, candidate_probe)
    voltage_close = measured_voltage_close_to_requested(
        measured_voltage_drop_mv=measured_voltage_drop_mv,
        requested_voltage_drop_mv=requested_voltage_drop_mv,
    )
    if previous_fps_per_w in (None, 0.0) or candidate_fps_per_w is None:
        return {
            "reference_temperature_c": reference_temperature_c,
            "previous_power_w": previous_power_w,
            "candidate_power_w": candidate_power_w,
            "previous_fps_per_w": previous_fps_per_w,
            "candidate_fps_per_w": candidate_fps_per_w,
            "measured_voltage_drop_mv": measured_voltage_drop_mv,
            "requested_voltage_drop_mv": requested_voltage_drop_mv,
            "measured_voltage_close_to_requested": voltage_close,
            "delta_fps_per_w": None,
            "delta_pct": None,
            "improved": None,
        }

    delta = float(candidate_fps_per_w) - float(previous_fps_per_w)
    delta_pct = delta / float(previous_fps_per_w) * 100.0
    return {
        "reference_temperature_c": reference_temperature_c,
        "previous_power_w": previous_power_w,
        "candidate_power_w": candidate_power_w,
        "previous_fps_per_w": previous_fps_per_w,
        "candidate_fps_per_w": candidate_fps_per_w,
        "measured_voltage_drop_mv": measured_voltage_drop_mv,
        "requested_voltage_drop_mv": requested_voltage_drop_mv,
        "measured_voltage_close_to_requested": voltage_close,
        "delta_fps_per_w": float(delta),
        "delta_pct": float(delta_pct),
        "improved": bool(delta_pct > float(rules.min_fps_per_w_gain_pct)),
    }


def decide_efficiency_stop(
    *,
    efficiency_stop_candidate: bool,
    voltage_drop_from_start_pct: float,
    min_voltage_drop_pct: float,
    no_gain_streak: int,
    required_extra_confirmations: int,
    pending_previous_curve: bool,
    power_up_efficiency_down: bool,
    efficiency_delta_pct: float | None,
) -> EfficiencyStopDecision:
    if not efficiency_stop_candidate:
        return EfficiencyStopDecision(False, 0, False, "efficiency still improving")
    if float(voltage_drop_from_start_pct) < float(min_voltage_drop_pct):
        return EfficiencyStopDecision(False, 0, False, "minimum voltage drop not reached")
    if not pending_previous_curve:
        return EfficiencyStopDecision(False, 0, False, "first no-gain probe arms stop")
    confirmations = max(0, int(no_gain_streak) - 1)
    if int(no_gain_streak) <= int(required_extra_confirmations):
        return EfficiencyStopDecision(False, confirmations, False, "confirming no-gain")

    use_current = (
        not bool(power_up_efficiency_down)
        and efficiency_delta_pct is not None
        and float(efficiency_delta_pct) >= 0.0
    )
    return EfficiencyStopDecision(
        True,
        confirmations,
        use_current,
        "fps-per-watt wall reached",
    )


def power_increased_while_efficiency_flat(
    *,
    previous_power_w: float | None,
    candidate_power_w: float | None,
    previous_fps_per_w: float | None,
    candidate_fps_per_w: float | None,
    rules: EfficiencyPolicyRules = EfficiencyPolicyRules(),
) -> bool:
    if previous_power_w is None or candidate_power_w is None:
        return False
    if previous_fps_per_w in (None, 0.0) or candidate_fps_per_w is None:
        return False
    delta_pct = (
        (float(candidate_fps_per_w) - float(previous_fps_per_w))
        / float(previous_fps_per_w)
        * 100.0
    )
    return (
        float(candidate_power_w) > float(previous_power_w)
        and delta_pct <= float(rules.min_fps_per_w_gain_pct)
    )


def temperature_normalized_power_w(
    probe: Any | None,
    *,
    reference_temperature_c: float | None,
    rules: EfficiencyPolicyRules = EfficiencyPolicyRules(),
) -> float | None:
    power_w = read_field(probe, "avg_power_w") if probe is not None else None
    temperature_c = read_field(probe, "avg_temperature_c") if probe is not None else None
    if power_w is None:
        return None
    if reference_temperature_c is None or temperature_c is None:
        return float(power_w)
    delta_c = max(
        -float(rules.max_temperature_delta_c),
        min(float(rules.max_temperature_delta_c), float(temperature_c) - reference_temperature_c),
    )
    correction = 1.0 + (float(rules.power_pct_per_c) / 100.0 * delta_c)
    if correction <= 0.0:
        return float(power_w)
    return float(power_w) / correction


def temperature_normalized_fps_per_w(
    probe: Any | None,
    *,
    normalized_power_w: float | None,
) -> float | None:
    if probe is None or normalized_power_w in (None, 0.0):
        return None
    avg_fps = read_field(probe, "avg_fps")
    if avg_fps is not None:
        return float(avg_fps) / float(normalized_power_w)
    fps_per_w = read_field(probe, "efficiency_fps_per_w")
    avg_power_w = read_field(probe, "avg_power_w")
    if fps_per_w is not None and avg_power_w is not None:
        return float(fps_per_w) * float(avg_power_w) / float(normalized_power_w)
    return None


def reference_temperature(previous_probe: Any | None, candidate_probe: Any | None) -> float | None:
    previous_temp = (
        read_field(previous_probe, "avg_temperature_c")
        if previous_probe is not None
        else None
    )
    candidate_temp = (
        read_field(candidate_probe, "avg_temperature_c")
        if candidate_probe is not None
        else None
    )
    if previous_temp is not None:
        return float(previous_temp)
    if candidate_temp is not None:
        return float(candidate_temp)
    return None


def measured_voltage_drop(previous_probe: Any | None, candidate_probe: Any | None) -> float | None:
    previous_mv = measured_probe_voltage_mv(previous_probe)
    candidate_mv = measured_probe_voltage_mv(candidate_probe)
    if previous_mv is None or candidate_mv is None:
        return None
    return float(previous_mv) - float(candidate_mv)


def requested_voltage_drop(previous_probe: Any | None, candidate_probe: Any | None) -> float | None:
    if previous_probe is None or candidate_probe is None:
        return None
    previous_mv = read_field(previous_probe, "candidate_voltage_mv")
    candidate_mv = read_field(candidate_probe, "candidate_voltage_mv")
    if previous_mv is None or candidate_mv is None:
        return None
    return float(previous_mv) - float(candidate_mv)


def measured_voltage_close_to_requested(
    *,
    measured_voltage_drop_mv: float | None,
    requested_voltage_drop_mv: float | None,
) -> bool:
    if requested_voltage_drop_mv is None or float(requested_voltage_drop_mv) <= 0.0:
        return True
    if measured_voltage_drop_mv is None:
        return True
    return abs(float(measured_voltage_drop_mv)) >= abs(float(requested_voltage_drop_mv)) * 0.5


def measured_probe_voltage_mv(probe: Any | None) -> float | None:
    if probe is None:
        return None
    avg_voltage_mv = read_field(probe, "avg_voltage_mv")
    if avg_voltage_mv is not None:
        return float(avg_voltage_mv)
    live_voltage_after_mv = read_field(probe, "live_voltage_after_mv")
    if live_voltage_after_mv is not None:
        return float(live_voltage_after_mv)
    return None
