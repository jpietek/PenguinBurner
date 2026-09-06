"""Represent the state carried between lower-voltage sweep probes.

The sweep updates this immutable state instead of spreading voltage variables across locals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from auto_uv.domain.types import StableRunDecision, VfCurveCandidate


@dataclass(frozen=True, slots=True)
class VoltageSweepState:
    stable_voltage_mv: int
    stable_target_mhz: int
    next_voltage_mv: int | None
    stable_measured_target_mhz: int | None = None


@dataclass(frozen=True, slots=True)
class VoltageProbeOutcome:
    decision: StableRunDecision
    measured_core_clock_mhz: float | None = None
    measured_voltage_mv: float | None = None
    raw_probe: Any | None = None
    raw_result: Any | None = None


@dataclass(frozen=True, slots=True)
class LowerVoltageSweepEvent:
    name: str
    message: str


@dataclass(frozen=True, slots=True)
class LowerVoltageSweepResult:
    stable_candidate: VfCurveCandidate
    state: VoltageSweepState
    stable_outcome: VoltageProbeOutcome | None = None
    probe_history: list[VoltageProbeOutcome] = field(default_factory=list)
    events: list[LowerVoltageSweepEvent] = field(default_factory=list)
