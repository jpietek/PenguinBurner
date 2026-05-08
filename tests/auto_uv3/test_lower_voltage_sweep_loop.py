from __future__ import annotations

from auto_uv3.auto_uv_types import (
    ClockRecoveryBudget,
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv3.auto_uv_scan_settings import AutoUvScanSettings
from auto_uv3.lower_voltage_sweep_loop import (
    LowerVoltageSweepHooks,
    maybe_raise_target_before_probe,
    run_lower_voltage_sweep_loop,
)
from auto_uv3.voltage_sweep_state import VoltageProbeOutcome, VoltageSweepState
from auto_uv3_test_data import base_curve, probe_summary, wide_base_curve


def test_lower_voltage_sweep_loop_accepts_next_lower_voltage_through_hooks() -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    probed: list[int] = []
    written: list[int] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append(int(candidate.voltage_mv))
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

    hooks = LowerVoltageSweepHooks(
        probe_candidate=probe,
        write_verified_candidate=lambda candidate, _outcome: written.append(
            int(candidate.voltage_mv)
        ),
        mark_unsafe_candidate=lambda _candidate, _outcome: None,
    )
    result = run_lower_voltage_sweep_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=950,
            preserve_base_below_mv=None,
            baseline_core_clock_mhz=2160.0,
            reference_actual_voltage_mv=1000.0,
        ),
        initial_stable_candidate=VfCurveCandidate(
            label="baseline",
            voltage_mv=1000,
            target_mhz=2160,
            flattened_plan=curve,
        ),
        hooks=hooks,
    )

    assert probed == [950]
    assert written == [950]
    assert result.stable_candidate.voltage_mv == 950
    assert result.state.next_voltage_mv is None


def test_performance_voltage_floor_spends_remaining_clock_budget() -> None:
    curve = base_curve(900, 1050, 25, 2000, 40)
    probed: list[tuple[int, int]] = []
    written: list[tuple[int, int]] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append((int(candidate.voltage_mv), int(candidate.target_mhz)))
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

    hooks = LowerVoltageSweepHooks(
        probe_candidate=probe,
        write_verified_candidate=lambda candidate, _outcome: written.append(
            (int(candidate.voltage_mv), int(candidate.target_mhz))
        ),
        mark_unsafe_candidate=lambda _candidate, _outcome: None,
    )
    result = run_lower_voltage_sweep_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=950,
            preserve_base_below_mv=None,
            baseline_core_clock_mhz=2200.0,
            min_core_clock_pct=90.0,
            reference_actual_voltage_mv=1000.0,
            measured_clock_cap_mhz=2200.0,
            recovery_budget_limit_pct=3.0,
            spend_remaining_clock_budget_at_voltage_floor=True,
        ),
        initial_stable_candidate=VfCurveCandidate(
            label="baseline",
            voltage_mv=1000,
            target_mhz=2160,
            flattened_plan=curve,
        ),
        hooks=hooks,
    )

    assert probed == [(950, 2080), (950, 2100), (950, 2130)]
    assert written == [(950, 2080), (950, 2100), (950, 2130)]
    assert result.stable_candidate.voltage_mv == 950
    assert result.stable_candidate.target_mhz == 2130
    assert result.state.recovery_budget.used_pct == 3.0


def test_performance_voltage_floor_bumps_voltage_for_clock_recovery() -> None:
    curve = base_curve(900, 1050, 25, 2000, 40)
    probed: list[tuple[int, int]] = []
    written: list[tuple[int, int]] = []
    unsafe: list[tuple[int, int]] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append((int(candidate.voltage_mv), int(candidate.target_mhz)))
        passed = not (
            int(candidate.voltage_mv) == 950 and int(candidate.target_mhz) > 2080
        )
        return VoltageProbeOutcome(
            decision=StableRunDecision(
                passed=passed,
                failure_kind=FailureKind.NONE if passed else FailureKind.LOW_CLOCK,
                severity=(
                    FailureSeverity.PASS
                    if passed
                    else FailureSeverity.RECOVERABLE
                ),
                reason="stable run" if passed else "clock too low",
            ),
            measured_core_clock_mhz=float(candidate.target_mhz),
            measured_voltage_mv=float(candidate.voltage_mv),
            raw_probe=probe_summary(
                candidate.voltage_mv,
                clock_mhz=float(candidate.target_mhz),
            ),
        )

    hooks = LowerVoltageSweepHooks(
        probe_candidate=probe,
        write_verified_candidate=lambda candidate, _outcome: written.append(
            (int(candidate.voltage_mv), int(candidate.target_mhz))
        ),
        mark_unsafe_candidate=lambda candidate, _outcome: unsafe.append(
            (int(candidate.voltage_mv), int(candidate.target_mhz))
        ),
    )
    result = run_lower_voltage_sweep_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=950,
            preserve_base_below_mv=None,
            baseline_core_clock_mhz=2200.0,
            min_core_clock_pct=90.0,
            reference_actual_voltage_mv=1000.0,
            measured_clock_cap_mhz=2200.0,
            recovery_budget_limit_pct=1.6,
            spend_remaining_clock_budget_at_voltage_floor=True,
            allow_voltage_bump_for_floor_clock_recovery=True,
        ),
        initial_stable_candidate=VfCurveCandidate(
            label="baseline",
            voltage_mv=1000,
            target_mhz=2160,
            flattened_plan=curve,
        ),
        hooks=hooks,
    )

    assert probed == [(950, 2080), (950, 2100), (975, 2100), (1000, 2100)]
    assert written == [(950, 2080), (975, 2100)]
    assert unsafe == [(950, 2100)]
    assert result.stable_candidate.voltage_mv == 975
    assert result.stable_candidate.target_mhz == 2100


def test_performance_voltage_floor_recovery_respects_voltage_ceiling() -> None:
    curve = base_curve(900, 1050, 25, 2000, 40)
    probed: list[tuple[int, int]] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append((int(candidate.voltage_mv), int(candidate.target_mhz)))
        passed = not (
            int(candidate.voltage_mv) == 950 and int(candidate.target_mhz) > 2080
        )
        return VoltageProbeOutcome(
            decision=StableRunDecision(
                passed=passed,
                failure_kind=FailureKind.NONE if passed else FailureKind.LOW_CLOCK,
                severity=(
                    FailureSeverity.PASS
                    if passed
                    else FailureSeverity.RECOVERABLE
                ),
                reason="stable run" if passed else "clock too low",
            ),
            measured_core_clock_mhz=float(candidate.target_mhz),
            measured_voltage_mv=float(candidate.voltage_mv),
            raw_probe=probe_summary(
                candidate.voltage_mv,
                clock_mhz=float(candidate.target_mhz),
            ),
        )

    result = run_lower_voltage_sweep_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=950,
            preserve_base_below_mv=None,
            baseline_core_clock_mhz=2200.0,
            min_core_clock_pct=90.0,
            reference_actual_voltage_mv=1000.0,
            measured_clock_cap_mhz=2200.0,
            recovery_voltage_ceiling_mv=975,
            recovery_budget_limit_pct=1.6,
            spend_remaining_clock_budget_at_voltage_floor=True,
            allow_voltage_bump_for_floor_clock_recovery=True,
        ),
        initial_stable_candidate=VfCurveCandidate(
            label="baseline",
            voltage_mv=1000,
            target_mhz=2160,
            flattened_plan=curve,
        ),
        hooks=LowerVoltageSweepHooks(
            probe_candidate=probe,
            write_verified_candidate=lambda _candidate, _outcome: None,
            mark_unsafe_candidate=lambda _candidate, _outcome: None,
        ),
    )

    assert probed == [(950, 2080), (950, 2100), (975, 2100)]
    assert result.stable_candidate.voltage_mv == 975
    assert result.stable_candidate.target_mhz == 2100


def test_performance_voltage_recovery_keeps_target_and_stops_when_fps_stops_improving() -> None:
    curve = base_curve(900, 1050, 25, 2000, 40)
    probed: list[tuple[int, int]] = []
    written: list[tuple[int, int]] = []
    recorded: list[tuple[int, int]] = []
    fps_by_candidate = {
        (950, 2080): 100.0,
        (950, 2100): 101.0,
        (975, 2100): 102.0,
        (1000, 2100): 101.5,
        (950, 2130): 103.0,
        (975, 2130): 102.5,
    }

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        key = (int(candidate.voltage_mv), int(candidate.target_mhz))
        probed.append(key)
        fps = fps_by_candidate[key]
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
                fps=fps,
            ),
        )

    hooks = LowerVoltageSweepHooks(
        probe_candidate=probe,
        write_verified_candidate=lambda candidate, _outcome: written.append(
            (int(candidate.voltage_mv), int(candidate.target_mhz))
        ),
        mark_unsafe_candidate=lambda _candidate, _outcome: None,
        record_passed_candidate=lambda candidate, _outcome: recorded.append(
            (int(candidate.voltage_mv), int(candidate.target_mhz))
        ),
    )
    result = run_lower_voltage_sweep_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=950,
            preserve_base_below_mv=None,
            baseline_core_clock_mhz=2200.0,
            min_core_clock_pct=90.0,
            reference_actual_voltage_mv=1000.0,
            measured_clock_cap_mhz=2200.0,
            recovery_budget_limit_pct=3.0,
            spend_remaining_clock_budget_at_voltage_floor=True,
            allow_voltage_bump_for_floor_clock_recovery=True,
        ),
        initial_stable_candidate=VfCurveCandidate(
            label="baseline",
            voltage_mv=1000,
            target_mhz=2160,
            flattened_plan=curve,
        ),
        hooks=hooks,
    )

    assert probed == [
        (950, 2080),
        (950, 2100),
        (975, 2100),
        (1000, 2100),
        (950, 2130),
        (975, 2130),
    ]
    assert written == [(950, 2080), (975, 2100), (950, 2130)]
    assert recorded == [(950, 2100)]
    assert result.stable_candidate.voltage_mv == 950
    assert result.stable_candidate.target_mhz == 2130


def test_efficiency_voltage_floor_spends_clock_budget_without_voltage_bump() -> None:
    curve = base_curve(900, 1050, 25, 2000, 40)
    probed: list[tuple[int, int]] = []
    unsafe: list[tuple[int, int]] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append((int(candidate.voltage_mv), int(candidate.target_mhz)))
        passed = int(candidate.target_mhz) <= 2080
        return VoltageProbeOutcome(
            decision=StableRunDecision(
                passed=passed,
                failure_kind=FailureKind.NONE if passed else FailureKind.LOW_CLOCK,
                severity=(
                    FailureSeverity.PASS
                    if passed
                    else FailureSeverity.RECOVERABLE
                ),
                reason="stable run" if passed else "clock too low",
            ),
            measured_core_clock_mhz=float(candidate.target_mhz),
            measured_voltage_mv=float(candidate.voltage_mv),
            raw_probe=probe_summary(
                candidate.voltage_mv,
                clock_mhz=float(candidate.target_mhz),
            ),
        )

    hooks = LowerVoltageSweepHooks(
        probe_candidate=probe,
        write_verified_candidate=lambda _candidate, _outcome: None,
        mark_unsafe_candidate=lambda candidate, _outcome: unsafe.append(
            (int(candidate.voltage_mv), int(candidate.target_mhz))
        ),
    )
    result = run_lower_voltage_sweep_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=950,
            preserve_base_below_mv=None,
            baseline_core_clock_mhz=2200.0,
            min_core_clock_pct=90.0,
            reference_actual_voltage_mv=1000.0,
            measured_clock_cap_mhz=2200.0,
            recovery_budget_limit_pct=1.6,
            spend_remaining_clock_budget_at_voltage_floor=True,
            allow_voltage_bump_for_floor_clock_recovery=False,
        ),
        initial_stable_candidate=VfCurveCandidate(
            label="baseline",
            voltage_mv=1000,
            target_mhz=2160,
            flattened_plan=curve,
        ),
        hooks=hooks,
    )

    assert probed == [(950, 2080), (950, 2100)]
    assert unsafe == [(950, 2100)]
    assert result.stable_candidate.voltage_mv == 950
    assert result.stable_candidate.target_mhz == 2080


def test_spent_clock_recovery_budget_still_raises_later_targets() -> None:
    target_mhz, budget = maybe_raise_target_before_probe(
        wide_base_curve(),
        settings=AutoUvScanSettings(
            start_voltage_mv=1025,
            min_search_voltage_mv=None,
            preserve_base_below_mv=None,
            baseline_core_clock_mhz=2763.5,
            min_core_clock_pct=90.0,
            measured_clock_cap_mhz=2763.5,
            recovery_budget_limit_pct=2.2,
        ),
        state=VoltageSweepState(
            stable_voltage_mv=915,
            stable_target_mhz=2565,
            stable_measured_target_mhz=2511,
            next_voltage_mv=910,
            recovery_budget=ClockRecoveryBudget(used_pct=2.2, limit_pct=2.2),
        ),
        measured_target_mhz=2511,
        probe_history=[],
    )

    assert target_mhz == 2580
    assert budget == ClockRecoveryBudget(used_pct=2.2, limit_pct=2.2)


def test_reapplied_clock_recovery_budget_charges_snapped_target() -> None:
    target_mhz, budget = maybe_raise_target_before_probe(
        wide_base_curve(),
        settings=AutoUvScanSettings(
            start_voltage_mv=1025,
            min_search_voltage_mv=None,
            preserve_base_below_mv=None,
            baseline_core_clock_mhz=2753.73,
            min_core_clock_pct=90.0,
            measured_clock_cap_mhz=2753.73,
            recovery_budget_limit_pct=2.4,
        ),
        state=VoltageSweepState(
            stable_voltage_mv=950,
            stable_target_mhz=2550,
            stable_measured_target_mhz=2504,
            next_voltage_mv=935,
            recovery_budget=ClockRecoveryBudget(used_pct=2.16, limit_pct=2.4),
        ),
        measured_target_mhz=2504,
        probe_history=[],
    )

    assert target_mhz == 2565
    assert budget == ClockRecoveryBudget(used_pct=2.4, limit_pct=2.4)
