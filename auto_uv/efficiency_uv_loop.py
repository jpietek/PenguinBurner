"""Descend once to the Efficiency floor; final selection ranks passed curves."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from auto_uv.base_uv_loop import BaseUvLoopIO, run_base_uv_loop
from auto_uv.domain.console_log import log_user_stage
from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.domain.types import VfCurveCandidate
from auto_uv.run.voltage_sweep_state import LowerVoltageSweepResult, VoltageProbeOutcome


def run_efficiency_uv_loop(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    initial_stable_candidate: VfCurveCandidate,
    io: BaseUvLoopIO,
    unsafe_entries: list[dict] | None = None,
    initial_stable_outcome: VoltageProbeOutcome | None = None,
    min_search_voltage_mv: int,
    initial_tail_rise_bins: int,
    log: Callable[[str], None],
) -> LowerVoltageSweepResult:
    log_user_stage(
        log,
        "Auto-UV efficiency lower-voltage search",
        [
            (
                "Searching toward the card minimum voltage with "
                f"{int(initial_tail_rise_bins)} tail-rise bins."
            ),
            f"Keeping target clock: {int(initial_stable_candidate.target_mhz)}MHz.",
        ],
    )
    return run_base_uv_loop(
        base_curve,
        settings=replace(
            settings,
            min_search_voltage_mv=int(min_search_voltage_mv),
            tail_rise_bins=int(initial_tail_rise_bins),
        ),
        initial_stable_candidate=initial_stable_candidate,
        io=io,
        unsafe_entries=unsafe_entries,
        initial_stable_outcome=initial_stable_outcome,
    )
