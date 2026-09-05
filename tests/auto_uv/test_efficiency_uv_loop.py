from __future__ import annotations

from dataclasses import replace

import pytest

from auto_uv.base_uv_loop import BaseUvLoopIO
from auto_uv.curve.vf_curve_flattening import build_flattened_plan
from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.domain.types import (
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv.efficiency_uv_loop import run_efficiency_uv_loop
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome

from auto_uv_test_data import base_curve


def _passing_outcome(raw_probe: object | None = None) -> VoltageProbeOutcome:
    return VoltageProbeOutcome(
        decision=StableRunDecision(
            passed=True,
            failure_kind=FailureKind.NONE,
            severity=FailureSeverity.PASS,
            reason="stable",
        ),
        measured_core_clock_mhz=None,
        measured_voltage_mv=None,
        raw_probe=raw_probe,
        raw_result=None,
    )


def _initial_candidate(curve: list[dict], *, voltage_mv: int, lock_mhz: int) -> VfCurveCandidate:
    return VfCurveCandidate(
        label="baseline",
        voltage_mv=voltage_mv,
        target_mhz=lock_mhz,
        flattened_plan=build_flattened_plan(
            curve,
            lock_clock_mhz=lock_mhz,
            candidate_voltage_mv=voltage_mv,
            tail_rise_bins=0,
        ),
        metadata={"tail_rise_bins": 0},
    )


def _all_pass_io() -> BaseUvLoopIO:
    return BaseUvLoopIO(
        probe_candidate=lambda _candidate: _passing_outcome(),
        write_verified_candidate=lambda _candidate, _outcome: None,
        mark_unsafe_candidate=lambda _candidate, _outcome: None,
    )


def _settings(*, auto_uv_mode: str = "efficiency", min_search_voltage_mv: int) -> AutoUvScanSettings:
    return AutoUvScanSettings(
        start_voltage_mv=1000,
        min_search_voltage_mv=min_search_voltage_mv,
        auto_uv_mode=auto_uv_mode,
        reference_actual_voltage_mv=None,
        efficiency_stop_streak=0,
        min_efficiency_stop_voltage_drop_pct=100.0,
        tail_rise_bins=0,
    )


def _tail_targets_above_lock(candidate: VfCurveCandidate) -> list[int]:
    return [
        int(point["target_mhz"])
        for point in candidate.flattened_plan
        if int(point["voltage_mv"]) > int(candidate.voltage_mv)
        and not point.get("preserve_base")
    ]


def _fps_regression_outcome() -> VoltageProbeOutcome:
    return VoltageProbeOutcome(
        decision=StableRunDecision(
            passed=False,
            failure_kind=FailureKind.FPS_REGRESSION,
            severity=FailureSeverity.RECOVERABLE,
            reason="benchmark average FPS below floor",
        ),
        measured_core_clock_mhz=None,
        measured_voltage_mv=None,
        raw_probe=None,
        raw_result=None,
    )


def test_deeper_search_stops_on_fps_regression_without_accepting_it() -> None:
    curve = base_curve()
    probed: list[VfCurveCandidate] = []
    written: list[VfCurveCandidate] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append(candidate)
        if candidate.voltage_mv >= 950:
            return _passing_outcome()
        return _fps_regression_outcome()

    result = run_efficiency_uv_loop(
        curve, settings=_settings(min_search_voltage_mv=950),
        initial_stable_candidate=_initial_candidate(curve, voltage_mv=1000, lock_mhz=2240),
        io=BaseUvLoopIO(
            probe_candidate=probe,
            write_verified_candidate=lambda candidate, _outcome: written.append(candidate),
            mark_unsafe_candidate=lambda _candidate, _outcome: None,
        ),
        min_search_voltage_mv=825, initial_tail_rise_bins=0, log=lambda _: None,
    )
    assert len([c for c in probed if c.voltage_mv < 950]) == 1
    assert result.stable_candidate.voltage_mv == 950
    assert all(c.voltage_mv >= 950 for c in written)
    assert all(c.metadata["tail_rise_bins"] == 0 for c in probed)
    assert all(all(t == c.target_mhz for t in _tail_targets_above_lock(c)) for c in probed)


@pytest.mark.parametrize("tail_bins", [0, 2])
@pytest.mark.parametrize("first_floor,final_floor", [(900, 900), (950, 825)])
def test_efficiency_preserves_tested_tail_through_both_passes_and_persistence(
    tail_bins: int, first_floor: int, final_floor: int
) -> None:
    curve = base_curve()
    probed: list[VfCurveCandidate] = []
    written: list[tuple[VfCurveCandidate, VoltageProbeOutcome]] = []
    outcome = _passing_outcome(raw_probe=object())

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append(candidate)
        return outcome

    result = run_efficiency_uv_loop(
        curve,
        settings=replace(_settings(min_search_voltage_mv=first_floor), tail_rise_bins=tail_bins),
        initial_stable_candidate=_initial_candidate(curve, voltage_mv=1000, lock_mhz=2240),
        io=BaseUvLoopIO(
            probe_candidate=probe,
            write_verified_candidate=lambda candidate, measured: written.append((candidate, measured)),
            mark_unsafe_candidate=lambda *_: None,
        ),
        min_search_voltage_mv=final_floor, initial_tail_rise_bins=tail_bins,
        log=lambda _: None,
    )

    assert result.stable_candidate.voltage_mv == final_floor
    assert written[-1][0] is result.stable_candidate
    assert all(measured is outcome for _, measured in written)
    assert all(candidate.metadata["tail_rise_bins"] == tail_bins for candidate in probed)
    for candidate in probed:
        tail = _tail_targets_above_lock(candidate)
        assert max(tail) == candidate.target_mhz + 15 * tail_bins


def test_non_efficiency_mode_returns_first_pass_unchanged() -> None:
    curve = base_curve()
    result = run_efficiency_uv_loop(
        curve,
        settings=_settings(auto_uv_mode="balanced", min_search_voltage_mv=900),
        initial_stable_candidate=_initial_candidate(curve, voltage_mv=1000, lock_mhz=2240),
        io=_all_pass_io(),
        min_search_voltage_mv=900,
        initial_tail_rise_bins=0,
        log=lambda _message: None,
    )

    candidate = result.stable_candidate
    assert int(candidate.metadata.get("tail_rise_bins", 0)) == 0
    tail = _tail_targets_above_lock(candidate)
    assert all(target == int(candidate.target_mhz) for target in tail)
