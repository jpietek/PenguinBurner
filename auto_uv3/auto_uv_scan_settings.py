"""Hold the knobs that shape one Auto-UV3 scan.

Settings are plain data so tests and UI code can construct a scan without knowing loop internals.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AutoUvScanSettings:
    start_voltage_mv: int
    min_search_voltage_mv: int | None
    preserve_base_below_mv: int | None
    baseline_core_clock_mhz: float | None
    min_core_clock_pct: float = 90.0
    reference_actual_voltage_mv: float | None = None
    measured_clock_cap_mhz: float | None = None
    recovery_voltage_ceiling_mv: int | None = None
    recovery_budget_limit_pct: float = 0.0
    spend_remaining_clock_budget_at_voltage_floor: bool = False
    allow_voltage_bump_for_floor_clock_recovery: bool = False
