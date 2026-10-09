from __future__ import annotations

from dataclasses import replace

import pytest
from auto_uv_test_data import base_curve, probe_summary

from auto_uv.base_uv_loop import (
    BaseUvLoopIO,
    run_base_uv_loop,
)
from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.domain.types import (
    AutoUvCriticalProbeError,
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome


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
    # Every probe passed down to the floor: floor caution keeps 925 and sets
    # the floor pass aside (the clock contract below is what this test is for).
    assert result.stable_candidate.voltage_mv == 925
    assert result.excluded_candidate is not None and result.excluded_candidate.voltage_mv == 900
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
    assert result.stable_candidate.voltage_mv == 925  # floor caution; see above
    assert result.excluded_candidate is not None and result.excluded_candidate.voltage_mv == 900
    assert result.stable_candidate.target_mhz == 2160


@pytest.mark.parametrize("mode", ["efficiency", "balanced", "performance"])
@pytest.mark.parametrize("tail", [0, 2, 4])
@pytest.mark.parametrize("initial_measurement", [None, 2190.0])
def test_measured_gains_preserve_descent_target_and_tail(
    mode: str, tail: int, initial_measurement: float | None,
) -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    probed: list[VfCurveCandidate] = []
    saved_outcomes: list[VoltageProbeOutcome] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append(candidate)
        return replace(
            _passed_outcome(candidate),
            measured_core_clock_mhz=candidate.target_mhz + 30.9,
            raw_probe=probe_summary(
                candidate.voltage_mv,
                clock_mhz=candidate.target_mhz + 30.9,
                power_w=candidate.voltage_mv / 5,
            ),
        )

    initial = VfCurveCandidate("baseline", 1000, 2160, curve)
    result = run_base_uv_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000, min_search_voltage_mv=900,
            auto_uv_mode=mode, tail_rise_bins=tail,
        ),
        initial_stable_candidate=initial,
        initial_stable_outcome=replace(
            _passed_outcome(initial), measured_core_clock_mhz=initial_measurement,
        ),
        io=BaseUvLoopIO(
            probe_candidate=probe,
            write_verified_candidate=lambda _, outcome: saved_outcomes.append(outcome),
            mark_unsafe_candidate=lambda *_: None,
        ),
    )

    assert [(c.voltage_mv, c.target_mhz) for c in probed] == [(925, 2160), (900, 2160)]
    for candidate in probed:
        tail_points = [
            p["target_mhz"] for p in candidate.flattened_plan
            if p["voltage_mv"] >= candidate.voltage_mv
        ]
        assert candidate.metadata["tail_rise_bins"] == tail
        assert tail_points == [2160 + 15 * min(i, tail) for i in range(len(tail_points))]
    assert saved_outcomes == result.probe_history
    assert [o.measured_core_clock_mhz for o in result.probe_history] == [2190.9, 2190.9]
    assert result.stable_candidate.target_mhz == 2160


@pytest.mark.parametrize(
    "previous_mv,next_mv,target_mhz,measured_mhz",
    [(825, 818, 1812, 1828.9), (931, 925, 1920, 1939.6), (956, 950, 1935, 1953.5)],
)
def test_issue109_descent_keeps_requested_clock(
    previous_mv: int, next_mv: int, target_mhz: int, measured_mhz: float,
) -> None:
    curve = [
        {
            "index": i, "voltage_mv": voltage,
            "base_mhz": target_mhz - 30 + i * 15,
            "target_mhz": target_mhz - 30 + i * 15,
            "new_offset_mhz": 0,
        }
        for i, voltage in enumerate((next_mv, previous_mv, previous_mv + 6, previous_mv + 12))
    ]
    initial = VfCurveCandidate("previous pass", previous_mv, target_mhz, curve)
    probed: list[VfCurveCandidate] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append(candidate)
        return _passed_outcome(candidate)

    run_base_uv_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=previous_mv, min_search_voltage_mv=next_mv, tail_rise_bins=2,
        ),
        initial_stable_candidate=initial,
        initial_stable_outcome=replace(
            _passed_outcome(initial), measured_core_clock_mhz=measured_mhz,
        ),
        io=BaseUvLoopIO(
            probe_candidate=probe,
            write_verified_candidate=lambda *_: None,
            mark_unsafe_candidate=lambda *_: None,
        ),
    )

    assert [(c.voltage_mv, c.target_mhz) for c in probed] == [(next_mv, target_mhz)]
    assert max(p["target_mhz"] for p in probed[0].flattened_plan) == target_mhz + 30


@pytest.mark.parametrize("unsafe_clock_mhz,blocked", [(2240, True), (2250, False)])
def test_cached_unsafe_check_uses_requested_clock(
    unsafe_clock_mhz: int, blocked: bool,
) -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    initial = VfCurveCandidate("baseline", 1000, 2240, curve)
    initial_outcome = replace(_passed_outcome(initial), measured_core_clock_mhz=2260.0)

    probed: list[tuple[int, int]] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        assert not blocked, "cached unsafe candidate reached the GPU"
        probed.append((candidate.voltage_mv, candidate.target_mhz))
        return _passed_outcome(candidate)

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
            "lock_clock_mhz": unsafe_clock_mhz,
            "reason": "nvidia-xid",
        }],
        io=BaseUvLoopIO(
            probe_candidate=probe,
            write_verified_candidate=lambda *_: None,
            mark_unsafe_candidate=lambda *_: None,
        ),
    )

    if blocked:
        assert result.stable_candidate is initial
        assert result.stable_outcome is initial_outcome
        assert result.probe_history == []
        assert [event.name for event in result.events] == ["stop"]
    else:
        assert probed == [(925, 2240), (900, 2240)]


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


def test_clean_floor_descent_keeps_the_pass_one_step_above_the_floor() -> None:
    """Every probe passed down to the configured floor: the deepest point is set aside."""
    curve = base_curve(900, 1025, 25, 2000, 40)
    probed: list[int] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append(int(candidate.voltage_mv))
        return _passed_outcome(candidate)

    def run(floor_mv):
        probed.clear()
        return run_base_uv_loop(
            curve,
            settings=AutoUvScanSettings(
                start_voltage_mv=1000, min_search_voltage_mv=floor_mv,
                reference_actual_voltage_mv=1000.0,
            ),
            initial_stable_candidate=VfCurveCandidate(
                label="baseline", voltage_mv=1000, target_mhz=2160, flattened_plan=curve,
            ),
            io=BaseUvLoopIO(
                probe_candidate=probe,
                write_verified_candidate=lambda _c, _o: None,
                mark_unsafe_candidate=lambda _c, _o: None,
            ),
        )

    result = run(900)
    assert probed[-1] == 900 and len(probed) >= 2
    assert result.stable_candidate.voltage_mv == probed[-2]
    assert result.excluded_candidate is not None and result.excluded_candidate.voltage_mv == 900
    assert result.state.stable_voltage_mv == probed[-2]
    assert result.events[-1].name == "floor-caution"
    assert "900mV floor" in result.events[-1].message
    # Without a configured floor the curve bottom is a test fixture, not a
    # product floor: the deepest pass is kept as before.
    bottom = run(None)
    assert bottom.stable_candidate.voltage_mv == 900 and bottom.excluded_candidate is None


def test_descent_that_fails_or_has_a_single_pass_keeps_its_deepest_pass() -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)

    def failing_below(limit_mv):
        def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
            if int(candidate.voltage_mv) < limit_mv:
                return VoltageProbeOutcome(
                    decision=StableRunDecision(
                        False, FailureKind.FPS_REGRESSION, FailureSeverity.RECOVERABLE, "fps",
                    )
                )
            return _passed_outcome(candidate)
        return probe

    def run(probe, floor_mv):
        return run_base_uv_loop(
            curve,
            settings=AutoUvScanSettings(
                start_voltage_mv=1000, min_search_voltage_mv=floor_mv,
                reference_actual_voltage_mv=1000.0,
            ),
            initial_stable_candidate=VfCurveCandidate(
                label="baseline", voltage_mv=1000, target_mhz=2160, flattened_plan=curve,
            ),
            io=BaseUvLoopIO(
                probe_candidate=probe,
                write_verified_candidate=lambda _c, _o: None,
                mark_unsafe_candidate=lambda _c, _o: None,
            ),
        )

    failed = run(failing_below(925), 900)  # the edge showed itself at 900
    assert failed.excluded_candidate is None
    assert failed.stable_candidate.voltage_mv == 925
    single = run(lambda c: _passed_outcome(c), 975)  # one pass, nothing above it to keep
    assert single.excluded_candidate is None
    assert single.stable_candidate.voltage_mv == 975
