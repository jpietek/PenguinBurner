from __future__ import annotations

from dataclasses import dataclass

from auto_uv.curve.base_vf_curve_voltage_bins import base_target_clock_at_voltage
from auto_uv.shared.probe_data_fields import percent


@dataclass(frozen=True, slots=True)
class LowerVoltageProbeTargetRules:
    coarse_voltage_pct: float = 94.0
    medium_voltage_pct: float = 88.0


def lower_voltage_phase(
    *,
    start_voltage_mv: int,
    candidate_voltage_mv: int,
    rules: LowerVoltageProbeTargetRules = LowerVoltageProbeTargetRules(),
) -> str:
    ratio = (
        float(candidate_voltage_mv) / float(start_voltage_mv)
        if int(start_voltage_mv) > 0
        else 1.0
    )
    if ratio > percent(rules.coarse_voltage_pct):
        return "coarse"
    if ratio > percent(rules.medium_voltage_pct):
        return "medium"
    return "fine"


def base_curve_target_for_lower_voltage(
    base_curve: list[dict],
    *,
    candidate_voltage_mv: int,
    stable_target_mhz: int,
    stable_measured_target_mhz: int | None,
) -> int:
    if stable_measured_target_mhz is not None:
        target_mhz = int(stable_measured_target_mhz)
    else:
        base_target_mhz = base_target_clock_at_voltage(
            base_curve,
            voltage_mv=int(candidate_voltage_mv),
            fallback_mhz=int(stable_target_mhz),
        )
        # Lower voltage naturally descends with the base curve as bins get colder.
        target_mhz = min(int(stable_target_mhz), int(base_target_mhz))

    return int(target_mhz)
