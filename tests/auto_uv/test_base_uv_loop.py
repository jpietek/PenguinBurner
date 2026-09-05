from __future__ import annotations

from dataclasses import replace

import pytest

from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.domain.types import (
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv.base_uv_loop import (
    BaseUvLoopIO,
    run_base_uv_loop,
)
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome
from auto_uv_test_data import base_curve, probe_summary


def _passed_outcome(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
    return VoltageProbeOutcome(
        decision=StableRunDecision(
            passed=True,
            failure_kind=FailureKind.NONE,
            severity=FailureSeverity.PASS,
            reason="stable run",
        ),
        measured_core_clock_mhz=float(candidate.target_mhz),
        measured_voltage_mv=float(candidate.voltage_mv),
        raw_probe=probe_summary(
            candidate.voltage_mv,
            clock_mhz=float(candidate.target_mhz),
        ),
    )


def test_base_uv_loop_accepts_next_lower_voltage_through_io() -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    probed: list[int] = []
    written: list[int] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append(int(candidate.voltage_mv))
        return _passed_outcome(candidate)

    io = BaseUvLoopIO(
        probe_candidate=probe,
        write_verified_candidate=lambda candidate, _outcome: written.append(
            int(candidate.voltage_mv)
        ),
        mark_unsafe_candidate=lambda _candidate, _outcome: None,
    )
    result = run_base_uv_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=950,
            baseline_core_clock_mhz=2160.0,
            reference_actual_voltage_mv=1000.0,
        ),
        initial_stable_candidate=VfCurveCandidate(
            label="baseline",
            voltage_mv=1000,
            target_mhz=2160,
            flattened_plan=curve,
        ),
        io=io,
    )

    assert probed == [950]
    assert written == [950]
    assert result.stable_candidate.voltage_mv == 950
    assert result.state.next_voltage_mv is None


@pytest.mark.parametrize("mode,tail", [("efficiency", 0), ("balanced", 4), ("performance", 4)])
def test_descent_does_not_compound_lower_measured_clocks(mode, tail) -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    probed: list[tuple[int, int, int]] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append(
            (
                int(candidate.voltage_mv),
                int(candidate.target_mhz),
                int(candidate.metadata.get("tail_rise_bins", -1)),
            )
        )
        return VoltageProbeOutcome(
            decision=StableRunDecision(
                passed=True,
                failure_kind=FailureKind.NONE,
                severity=FailureSeverity.PASS,
                reason="stable run",
            ),
            measured_core_clock_mhz=float(candidate.target_mhz - 80),
            measured_voltage_mv=float(candidate.voltage_mv),
            raw_probe=probe_summary(
                candidate.voltage_mv,
                clock_mhz=float(candidate.target_mhz - 80),
                power_w=float(candidate.voltage_mv),
            ),
        )

    io = BaseUvLoopIO(
        probe_candidate=probe,
        write_verified_candidate=lambda _candidate, _outcome: None,
        mark_unsafe_candidate=lambda _candidate, _outcome: None,
    )
    result = run_base_uv_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=900,
            baseline_core_clock_mhz=2160.0,
            reference_actual_voltage_mv=1000.0,
            auto_uv_mode=mode,
            tail_rise_bins=tail,
        ),
        initial_stable_candidate=VfCurveCandidate(
            label="baseline",
            voltage_mv=1000,
            target_mhz=2160,
            flattened_plan=curve,
        ),
        io=io,
        initial_stable_outcome=VoltageProbeOutcome(
            decision=StableRunDecision(
                passed=True,
                failure_kind=FailureKind.NONE,
                severity=FailureSeverity.PASS,
                reason="stable run",
            ),
            measured_core_clock_mhz=2115.0,
            measured_voltage_mv=1000.0,
            raw_probe=probe_summary(1000, clock_mhz=2115.0, power_w=1000.0),
        ),
    )

    assert probed == [(925, 2160, tail), (900, 2160, tail)]
    assert result.stable_candidate.voltage_mv == 900
    assert result.stable_candidate.target_mhz == 2160


@pytest.mark.parametrize("cap_clears", [False, True])
def test_lower_voltage_sweep_keeps_target_when_power_limiting_clears(cap_clears) -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    probed: list[tuple[int, int]] = []

    def power_limited_outcome(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append((int(candidate.voltage_mv), int(candidate.target_mhz)))
        raw_probe = probe_summary(
            candidate.voltage_mv,
            clock_mhz=float(candidate.target_mhz - 80),
            power_w=float(candidate.voltage_mv),
        )
        raw_probe["perf_cap_reason"] = "none" if cap_clears else "sw-power"
        return VoltageProbeOutcome(
            decision=StableRunDecision(
                passed=True,
                failure_kind=FailureKind.NONE,
                severity=FailureSeverity.PASS,
                reason="stable run",
            ),
            measured_core_clock_mhz=float(candidate.target_mhz - 80),
            measured_voltage_mv=float(candidate.voltage_mv),
            raw_probe=raw_probe,
        )

    initial_raw_probe = probe_summary(1000, clock_mhz=2115.0, power_w=1000.0)
    initial_raw_probe["perf_cap_reason"] = "sw-power"
    io = BaseUvLoopIO(
        probe_candidate=power_limited_outcome,
        write_verified_candidate=lambda _candidate, _outcome: None,
        mark_unsafe_candidate=lambda _candidate, _outcome: None,
    )
    result = run_base_uv_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=900,
            baseline_core_clock_mhz=2160.0,
            reference_actual_voltage_mv=1000.0,
            tail_rise_bins=0,
        ),
        initial_stable_candidate=VfCurveCandidate(
            label="baseline",
            voltage_mv=1000,
            target_mhz=2160,
            flattened_plan=curve,
        ),
        io=io,
        initial_stable_outcome=VoltageProbeOutcome(
            decision=StableRunDecision(
                passed=True,
                failure_kind=FailureKind.NONE,
                severity=FailureSeverity.PASS,
                reason="stable run",
            ),
            measured_core_clock_mhz=2115.0,
            measured_voltage_mv=1000.0,
            raw_probe=initial_raw_probe,
        ),
    )

    assert probed == [(925, 2160), (900, 2160)]
    assert result.stable_candidate.voltage_mv == 900
    assert result.stable_candidate.target_mhz == 2160


def test_rising_tail_measured_gains_can_still_raise_the_next_target() -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    targets = []

    def probe(candidate):
        targets.append(candidate.target_mhz)
        return replace(_passed_outcome(candidate), measured_core_clock_mhz=candidate.target_mhz + 30)

    run_base_uv_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000, min_search_voltage_mv=900,
            baseline_core_clock_mhz=2160.0, auto_uv_mode="balanced", tail_rise_bins=4,
        ),
        initial_stable_candidate=VfCurveCandidate("baseline", 1000, 2160, curve),
        io=BaseUvLoopIO(
            probe_candidate=probe,
            write_verified_candidate=lambda *_: None,
            mark_unsafe_candidate=lambda *_: None,
        ),
    )
    assert targets == [2160, 2190]


def test_performance_mode_lower_sweep_uses_plain_lower_voltage_probe() -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    probed: list[tuple[int, int, str]] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append(
            (
                int(candidate.voltage_mv),
                int(candidate.target_mhz),
                candidate.label,
            )
        )
        return _passed_outcome(candidate)

    io = BaseUvLoopIO(
        probe_candidate=probe,
        write_verified_candidate=lambda _candidate, _outcome: None,
        mark_unsafe_candidate=lambda _candidate, _outcome: None,
    )
    result = run_base_uv_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=950,
            baseline_core_clock_mhz=2160.0,
            auto_uv_mode="performance",
            reference_actual_voltage_mv=1000.0,
            tail_rise_bins=6,
        ),
        initial_stable_candidate=VfCurveCandidate(
            label="baseline",
            voltage_mv=1000,
            target_mhz=2160,
            flattened_plan=curve,
        ),
        io=io,
    )

    assert len(probed) == 1
    assert probed[0][0] == 950
    assert "oc-budget" not in probed[0][2]


def _low_clock_outcome(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
    return VoltageProbeOutcome(
        decision=StableRunDecision(
            passed=False,
            failure_kind=FailureKind.LOW_CLOCK,
            severity=FailureSeverity.RECOVERABLE,
            reason="telemetry-live-core_clock current=1900MHz floor=2000MHz",
        ),
        measured_core_clock_mhz=float(candidate.target_mhz - 120),
        measured_voltage_mv=float(candidate.voltage_mv),
        raw_probe=probe_summary(
            candidate.voltage_mv,
            clock_mhz=float(candidate.target_mhz - 120),
        ),
    )


def _critical_outcome(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
    return VoltageProbeOutcome(
        decision=StableRunDecision(
            passed=False,
            failure_kind=FailureKind.Q2RTX_FAILED,
            severity=FailureSeverity.CRITICAL,
            reason="benchmark-crashed-signal",
        ),
        measured_core_clock_mhz=float(candidate.target_mhz),
        measured_voltage_mv=float(candidate.voltage_mv),
        raw_probe=probe_summary(
            candidate.voltage_mv,
            clock_mhz=float(candidate.target_mhz),
        ),
    )


def _run_low_clock_sweep(
    *,
    descend_through_low_clock: bool,
    probe,
    min_search_voltage_mv: int = 900,
):
    curve = base_curve(880, 1025, 20, 2000, 40)
    probed: list[int] = []
    unsafe: list[int] = []
    written: list[int] = []

    def wrapped(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append(int(candidate.voltage_mv))
        return probe(candidate)

    io = BaseUvLoopIO(
        probe_candidate=wrapped,
        write_verified_candidate=lambda candidate, _outcome: written.append(
            int(candidate.voltage_mv)
        ),
        mark_unsafe_candidate=lambda candidate, _outcome: unsafe.append(
            int(candidate.voltage_mv)
        ),
    )
    result = run_base_uv_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=min_search_voltage_mv,
            baseline_core_clock_mhz=2160.0,
            auto_uv_mode="performance",
            reference_actual_voltage_mv=1000.0,
            descend_through_low_clock=descend_through_low_clock,
        ),
        initial_stable_candidate=VfCurveCandidate(
            label="baseline",
            voltage_mv=1000,
            target_mhz=2160,
            flattened_plan=curve,
        ),
        io=io,
    )
    return result, probed, unsafe, written


def test_low_clock_first_pass_stops_without_marking_unsafe() -> None:
    # The first descent stops at the natural clock floor, but a low-clock dip is
    # NOT instability: the voltage must not be cached unsafe, or the deeper
    # low-voltage search could never retry it.
    result, probed, unsafe, _written = _run_low_clock_sweep(
        descend_through_low_clock=False,
        probe=_low_clock_outcome,
    )

    assert len(probed) == 1
    assert unsafe == []
    assert result.stable_candidate.voltage_mv == 1000
    assert [event.name for event in result.events] == ["stop"]


def test_low_clock_floor_search_pass_descends_to_lower_passing_voltage() -> None:
    # Deeper search: low-clock above 940mV, but a lower voltage holds the floor
    # with the same tail. The sweep must skip the low-clock voltages and keep
    # the lowest one that passes.
    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        if int(candidate.voltage_mv) > 940:
            return _low_clock_outcome(candidate)
        return _passed_outcome(candidate)

    result, probed, unsafe, written = _run_low_clock_sweep(
        descend_through_low_clock=True,
        probe=probe,
    )

    assert unsafe == []
    # It probed past the first low-clock voltage instead of stopping there.
    assert len(probed) >= 2
    assert min(probed) <= 940
    assert result.stable_candidate.voltage_mv <= 940
    assert written  # at least one lower-voltage candidate was verified


def test_low_clock_floor_search_descends_to_min_when_never_recovers() -> None:
    # Even if no lower voltage holds the floor, the deeper search keeps probing
    # toward the minimum (never marking unsafe) and falls back to the start point.
    result, probed, unsafe, written = _run_low_clock_sweep(
        descend_through_low_clock=True,
        probe=_low_clock_outcome,
        min_search_voltage_mv=900,
    )

    assert unsafe == []
    assert written == []
    assert len(probed) >= 2
    assert min(probed) <= 920  # reached deep into the range toward the minimum
    assert result.stable_candidate.voltage_mv == 1000


def test_critical_failure_marks_unsafe_and_stops_even_in_floor_search() -> None:
    # A genuine crash is still terminal and still cached unsafe, regardless of
    # the low-clock descent flag.
    result, probed, unsafe, _written = _run_low_clock_sweep(
        descend_through_low_clock=True,
        probe=_critical_outcome,
    )

    assert len(probed) == 1
    assert len(unsafe) == 1
    assert result.stable_candidate.voltage_mv == 1000
    assert "stop" in [event.name for event in result.events]
