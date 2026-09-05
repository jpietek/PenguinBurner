"""Hold the knobs that shape one Auto-UV scan.

Settings are plain data so tests and UI code can construct a scan without knowing loop internals.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AutoUvScanSettings:
    start_voltage_mv: int
    min_search_voltage_mv: int | None
    auto_uv_mode: str = "efficiency"
    reference_actual_voltage_mv: float | None = None
    efficiency_stop_streak: int = 2
    min_efficiency_stop_voltage_drop_pct: float = 10.0
    tail_rise_bins: int = 0
