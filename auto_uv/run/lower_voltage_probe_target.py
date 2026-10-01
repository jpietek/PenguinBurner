from __future__ import annotations

from dataclasses import dataclass

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
