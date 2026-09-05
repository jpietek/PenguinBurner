"""Balanced Auto-UV preset entry point.

Balanced is intentionally just the shared base undervolt sweep with the
Balanced tail shape selected by runtime settings. It has no second low-voltage
search pass and no Performance Auto-OC pass.
"""

from __future__ import annotations

from auto_uv.base_uv_loop import BaseUvLoopIO, run_base_uv_loop
from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.domain.types import VfCurveCandidate
from auto_uv.run.voltage_sweep_state import LowerVoltageSweepResult, VoltageProbeOutcome


def run_balanced_uv_loop(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    initial_stable_candidate: VfCurveCandidate,
    io: BaseUvLoopIO,
    unsafe_entries: list[dict] | None = None,
    initial_stable_outcome: VoltageProbeOutcome | None = None,
) -> LowerVoltageSweepResult:
    return run_base_uv_loop(
        base_curve,
        settings=settings,
        initial_stable_candidate=initial_stable_candidate,
        io=io,
        unsafe_entries=unsafe_entries,
        initial_stable_outcome=initial_stable_outcome,
    )
