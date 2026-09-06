from __future__ import annotations

from dataclasses import replace

import pytest

from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.domain.types import (
    AutoUvCriticalProbeError,
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
            auto_uv_mode="balanced", tail_rise_bins=4,
        ),
        initial_stable_candidate=VfCurveCandidate("baseline", 1000, 2160, curve),
        io=BaseUvLoopIO(
            probe_candidate=probe,
            write_verified_candidate=lambda *_: None,
            mark_unsafe_candidate=lambda *_: None,
        ),
    )
    assert targets == [2160, 2190]


def test_cached_unsafe_check_uses_the_next_candidates_raised_clock() -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    initial = VfCurveCandidate("baseline", 1000, 2240, curve)
    initial_outcome = replace(_passed_outcome(initial), measured_core_clock_mhz=2260.0)

    def unexpected_probe(_candidate):
        pytest.fail("cached unsafe candidate reached the GPU")

    result = run_base_uv_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000, min_search_voltage_mv=900,
            auto_uv_mode="balanced", tail_rise_bins=4,
        ),
        initial_stable_candidate=initial,
        initial_stable_outcome=initial_outcome,
        unsafe_entries=[{
            "candidate_voltage_mv": 925,
            "lock_clock_mhz": 2250,
            "reason": "nvidia-xid",
        }],
        io=BaseUvLoopIO(
            probe_candidate=unexpected_probe,
            write_verified_candidate=lambda *_: None,
            mark_unsafe_candidate=lambda *_: None,
        ),
    )

    assert result.stable_candidate is initial
    assert result.stable_outcome is initial_outcome
    assert result.probe_history == []
    assert [event.name for event in result.events] == ["stop"]


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
    assert result.stable_candidate.voltage_mv == 950


def test_critical_failure_marks_unsafe_and_aborts() -> None:
    curve = base_curve(880, 1025, 20, 2000, 40)
    probed: list[int] = []
    unsafe: list[int] = []
    written: list[int] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append(int(candidate.voltage_mv))
        return replace(_passed_outcome(candidate), decision=StableRunDecision(
            passed=False,
            failure_kind=FailureKind.Q2RTX_FAILED,
            severity=FailureSeverity.CRITICAL,
            reason="benchmark-summary-missing",
        ))

    io = BaseUvLoopIO(
        probe_candidate=probe,
        write_verified_candidate=lambda candidate, _outcome: written.append(
            int(candidate.voltage_mv)
        ),
        mark_unsafe_candidate=lambda candidate, _outcome: unsafe.append(
            int(candidate.voltage_mv)
        ),
    )
    with pytest.raises(AutoUvCriticalProbeError, match="benchmark-summary-missing"):
        run_base_uv_loop(
            curve,
            settings=AutoUvScanSettings(
                start_voltage_mv=1000,
                min_search_voltage_mv=900,
                auto_uv_mode="performance",
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

    assert len(probed) == 1
    assert unsafe == probed
    assert written == []
