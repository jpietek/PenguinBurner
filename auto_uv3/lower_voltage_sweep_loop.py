"""Sweep downward through lower voltage bins and keep the last stable curve.

GPU side effects stay behind hooks; this file shows only scan order and decision flow.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from .auto_uv_types import ClockRecoveryBudget, FailureKind, VfCurveCandidate
from .auto_uv_scan_settings import AutoUvScanSettings
from .curve.base_vf_curve_voltage_bins import higher_editable_voltage_bins
from .curve.base_vf_curve_validation import validate_base_vf_curve
from .recovery.clock_recovery_budget import (
    charge_recovery_budget_for_target,
    max_clock_drop_pct_for_min_core_clock,
    next_recovery_budget_used_pct,
)
from .recovery.clock_recovery_target import choose_clock_recovery_target_mhz
from .curve.flattened_voltage_probe_curve import build_flattened_voltage_probe_curve
from .lower_voltage_probe_target import (
    base_curve_target_for_lower_voltage,
    lower_voltage_clock_floor_miss_reason,
    lower_voltage_phase,
)
from .lower_voltage_search import select_next_lower_voltage
from .persistence.unsafe_voltage_cache import (
    unsafe_min_search_voltage,
    unsafe_voltage_block_reason,
)
from .shared.probe_data_fields import read_field
from .voltage_sweep_state import (
    LowerVoltageSweepEvent,
    LowerVoltageSweepResult,
    VoltageProbeOutcome,
    VoltageSweepState,
)


@dataclass(frozen=True, slots=True)
class LowerVoltageSweepHooks:
    probe_candidate: Callable[[VfCurveCandidate], VoltageProbeOutcome]
    write_verified_candidate: Callable[[VfCurveCandidate, VoltageProbeOutcome], None]
    mark_unsafe_candidate: Callable[[VfCurveCandidate, VoltageProbeOutcome], None]
    record_passed_candidate: (
        Callable[[VfCurveCandidate, VoltageProbeOutcome], None] | None
    ) = None


@dataclass(frozen=True, slots=True)
class VoltageFloorClockProbeResult:
    best_candidate: VfCurveCandidate
    best_outcome: VoltageProbeOutcome
    passed_results: tuple[tuple[VfCurveCandidate, VoltageProbeOutcome], ...]


def run_lower_voltage_sweep_loop(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    initial_stable_candidate: VfCurveCandidate,
    hooks: LowerVoltageSweepHooks,
    unsafe_entries: list[dict] | None = None,
) -> LowerVoltageSweepResult:
    validate_base_vf_curve(base_curve)
    unsafe_floor_mv, unsafe_min_search_mv = unsafe_min_search_voltage(
        base_curve,
        start_voltage_mv=int(settings.start_voltage_mv),
        unsafe_entries=list(unsafe_entries or []),
    )
    min_search_voltage_mv = higher_min_search_voltage(
        configured_min_search_mv=settings.min_search_voltage_mv,
        unsafe_min_search_mv=unsafe_min_search_mv,
    )
    state = VoltageSweepState(
        stable_voltage_mv=int(initial_stable_candidate.voltage_mv),
        stable_target_mhz=int(initial_stable_candidate.target_mhz),
        next_voltage_mv=select_next_lower_voltage(
            base_curve,
            start_voltage_mv=int(settings.start_voltage_mv),
            stable_voltage_mv=int(initial_stable_candidate.voltage_mv),
            reference_actual_voltage_mv=settings.reference_actual_voltage_mv,
            preserve_base_below_mv=settings.preserve_base_below_mv,
            min_search_voltage_mv=min_search_voltage_mv,
            failed_floor_voltage_mv=unsafe_floor_mv,
        ),
        recovery_budget=ClockRecoveryBudget(
            used_pct=0.0,
            limit_pct=float(settings.recovery_budget_limit_pct),
        ),
    )
    stable_candidate = initial_stable_candidate
    probe_history: list[VoltageProbeOutcome] = []
    events: list[LowerVoltageSweepEvent] = []

    while state.next_voltage_mv is not None:
        block_reason = unsafe_voltage_block_reason(
            list(unsafe_entries or []),
            candidate_voltage_mv=int(state.next_voltage_mv),
            lock_clock_mhz=int(state.stable_target_mhz),
        )
        if block_reason:
            events.append(LowerVoltageSweepEvent("stop", block_reason))
            break

        candidate, state = build_next_lower_voltage_candidate(
            base_curve,
            settings=settings,
            state=state,
            probe_history=probe_history,
        )
        outcome = hooks.probe_candidate(candidate)
        probe_history.append(outcome)
        if outcome.decision.passed:
            stable_candidate, state = accept_voltage_probe(
                base_curve,
                settings=settings,
                state=state,
                candidate=candidate,
                outcome=outcome,
                min_search_voltage_mv=min_search_voltage_mv,
            )
            hooks.write_verified_candidate(stable_candidate, outcome)
            events.append(
                LowerVoltageSweepEvent(
                    "accept",
                    f"{stable_candidate.voltage_mv}mV@{stable_candidate.target_mhz}MHz",
                )
            )
            continue

        if outcome.decision.failure_kind is FailureKind.LOW_CLOCK:
            recovered = try_clock_recovery_probe(
                base_curve,
                settings=settings,
                state=state,
                failed_candidate=candidate,
                failed_outcome=outcome,
                hooks=hooks,
            )
            if recovered is not None:
                recovery_candidate, recovery_outcome, recovered_state = recovered
                probe_history.append(recovery_outcome)
                if recovery_outcome.decision.passed:
                    stable_candidate, state = accept_voltage_probe(
                        base_curve,
                        settings=settings,
                        state=recovered_state,
                        candidate=recovery_candidate,
                        outcome=recovery_outcome,
                        min_search_voltage_mv=min_search_voltage_mv,
                    )
                    hooks.write_verified_candidate(stable_candidate, recovery_outcome)
                    events.append(
                        LowerVoltageSweepEvent(
                            "recover",
                            f"{stable_candidate.voltage_mv}mV@"
                            f"{stable_candidate.target_mhz}MHz",
                        )
                    )
                    continue
                outcome = recovery_outcome

        hooks.mark_unsafe_candidate(candidate, outcome)
        events.append(LowerVoltageSweepEvent("stop", outcome.decision.reason))
        break

    if (
        state.next_voltage_mv is None
        and bool(settings.spend_remaining_clock_budget_at_voltage_floor)
    ):
        stable_candidate, state, floor_events = (
            spend_remaining_clock_budget_at_voltage_floor(
                base_curve,
                settings=settings,
                state=state,
                stable_candidate=stable_candidate,
                hooks=hooks,
                probe_history=probe_history,
            )
        )
        events.extend(floor_events)

    return LowerVoltageSweepResult(
        stable_candidate=stable_candidate,
        state=state,
        probe_history=probe_history,
        events=events,
    )


def build_next_lower_voltage_candidate(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    probe_history: list[VoltageProbeOutcome],
) -> tuple[VfCurveCandidate, VoltageSweepState]:
    assert state.next_voltage_mv is not None
    measured_target_mhz = base_curve_target_for_lower_voltage(
        base_curve,
        candidate_voltage_mv=int(state.next_voltage_mv),
        stable_target_mhz=int(state.stable_target_mhz),
        stable_measured_target_mhz=state.stable_measured_target_mhz,
    )
    target_mhz, recovery_budget = maybe_raise_target_before_probe(
        base_curve,
        settings=settings,
        state=state,
        measured_target_mhz=int(measured_target_mhz),
        probe_history=probe_history,
    )
    phase = lower_voltage_phase(
        start_voltage_mv=int(settings.start_voltage_mv),
        candidate_voltage_mv=int(state.next_voltage_mv),
    )
    candidate = build_flattened_voltage_probe_curve(
        base_curve,
        candidate_voltage_mv=int(state.next_voltage_mv),
        target_clock_mhz=int(target_mhz),
        label=(
            f"lower-voltage {int(state.next_voltage_mv)}mV "
            f"phase={phase} recovery-budget="
            f"{recovery_budget.used_pct:.2f}/{recovery_budget.limit_pct:.2f}%"
        ),
    )
    return candidate, replace(state, recovery_budget=recovery_budget)


def maybe_raise_target_before_probe(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    measured_target_mhz: int,
    probe_history: list[VoltageProbeOutcome],
) -> tuple[int, ClockRecoveryBudget]:
    max_drop_pct = max_clock_drop_pct_for_min_core_clock(settings.min_core_clock_pct)
    reason = lower_voltage_clock_floor_miss_reason(
        [outcome.raw_probe for outcome in probe_history if outcome.raw_probe is not None],
        candidate_voltage_mv=int(state.next_voltage_mv or state.stable_voltage_mv),
        baseline_core_clock_mhz=settings.baseline_core_clock_mhz,
        min_core_clock_pct=float(settings.min_core_clock_pct),
    )
    if reason is None or state.recovery_budget.spent_or_disabled:
        return apply_existing_recovery_budget(
            base_curve,
            settings=settings,
            budget=state.recovery_budget,
            measured_target_mhz=int(measured_target_mhz),
            max_clock_drop_pct=max_drop_pct,
        )

    next_used_pct = next_recovery_budget_used_pct(
        current_used_pct=float(state.recovery_budget.used_pct),
        limit_pct=float(state.recovery_budget.limit_pct),
        reason=reason,
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=settings.baseline_core_clock_mhz,
        max_clock_drop_pct=max_drop_pct,
    )
    if next_used_pct is None:
        return apply_existing_recovery_budget(
            base_curve,
            settings=settings,
            budget=state.recovery_budget,
            measured_target_mhz=int(measured_target_mhz),
            max_clock_drop_pct=max_drop_pct,
        )

    target_mhz = choose_clock_recovery_target_mhz(
        base_curve,
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=settings.baseline_core_clock_mhz,
        max_clock_drop_pct=max_drop_pct,
        budget_used_pct=float(next_used_pct),
        cap_clock_mhz=settings.measured_clock_cap_mhz,
        minimum_target_mhz=int(measured_target_mhz),
    )
    charged_pct = charge_recovery_budget_for_target(
        measured_target_mhz=int(measured_target_mhz),
        recovered_target_mhz=int(target_mhz),
        requested_used_pct=float(next_used_pct),
        limit_pct=float(state.recovery_budget.limit_pct),
        baseline_clock_mhz=settings.baseline_core_clock_mhz,
        max_clock_drop_pct=max_drop_pct,
    )
    if charged_pct is None:
        return int(measured_target_mhz), state.recovery_budget
    return int(target_mhz), ClockRecoveryBudget(
        used_pct=float(charged_pct),
        limit_pct=float(state.recovery_budget.limit_pct),
    )


def apply_existing_recovery_budget(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    budget: ClockRecoveryBudget,
    measured_target_mhz: int,
    max_clock_drop_pct: float,
) -> tuple[int, ClockRecoveryBudget]:
    if float(budget.used_pct) <= 0.0 or float(budget.limit_pct) <= 0.0:
        return int(measured_target_mhz), budget
    target_mhz = choose_clock_recovery_target_mhz(
        base_curve,
        measured_target_mhz=int(measured_target_mhz),
        baseline_clock_mhz=settings.baseline_core_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
        budget_used_pct=float(budget.used_pct),
        cap_clock_mhz=settings.measured_clock_cap_mhz,
        minimum_target_mhz=int(measured_target_mhz),
    )
    charged_pct = charge_recovery_budget_for_target(
        measured_target_mhz=int(measured_target_mhz),
        recovered_target_mhz=int(target_mhz),
        requested_used_pct=float(budget.used_pct),
        limit_pct=float(budget.limit_pct),
        baseline_clock_mhz=settings.baseline_core_clock_mhz,
        max_clock_drop_pct=float(max_clock_drop_pct),
    )
    if charged_pct is None:
        return int(measured_target_mhz), budget
    return int(target_mhz), ClockRecoveryBudget(
        used_pct=float(charged_pct),
        limit_pct=float(budget.limit_pct),
    )


def try_clock_recovery_probe(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    failed_candidate: VfCurveCandidate,
    failed_outcome: VoltageProbeOutcome,
    hooks: LowerVoltageSweepHooks,
) -> tuple[VfCurveCandidate, VoltageProbeOutcome, VoltageSweepState] | None:
    if state.recovery_budget.spent_or_disabled:
        return None
    measured_target_mhz = int(
        failed_outcome.measured_core_clock_mhz or failed_candidate.target_mhz
    )
    max_drop_pct = max_clock_drop_pct_for_min_core_clock(settings.min_core_clock_pct)
    next_used_pct = next_recovery_budget_used_pct(
        current_used_pct=float(state.recovery_budget.used_pct),
        limit_pct=float(state.recovery_budget.limit_pct),
        reason=failed_outcome.decision.reason,
        measured_target_mhz=measured_target_mhz,
        baseline_clock_mhz=settings.baseline_core_clock_mhz,
        max_clock_drop_pct=max_drop_pct,
    )
    if next_used_pct is None:
        return None
    target_mhz = choose_clock_recovery_target_mhz(
        base_curve,
        measured_target_mhz=measured_target_mhz,
        baseline_clock_mhz=settings.baseline_core_clock_mhz,
        max_clock_drop_pct=max_drop_pct,
        budget_used_pct=float(next_used_pct),
        cap_clock_mhz=settings.measured_clock_cap_mhz,
        minimum_target_mhz=int(failed_candidate.target_mhz) + 15,
    )
    charged_pct = charge_recovery_budget_for_target(
        measured_target_mhz=measured_target_mhz,
        recovered_target_mhz=int(target_mhz),
        requested_used_pct=float(next_used_pct),
        limit_pct=float(state.recovery_budget.limit_pct),
        baseline_clock_mhz=settings.baseline_core_clock_mhz,
        max_clock_drop_pct=max_drop_pct,
    )
    if charged_pct is None or int(target_mhz) <= int(failed_candidate.target_mhz):
        return None

    candidate = build_flattened_voltage_probe_curve(
        base_curve,
        candidate_voltage_mv=int(failed_candidate.voltage_mv),
        target_clock_mhz=int(target_mhz),
        label=(
            f"clock-recovery {failed_candidate.voltage_mv}mV "
            f"recovery-budget={float(charged_pct):.2f}/"
            f"{state.recovery_budget.limit_pct:.2f}%"
        ),
    )
    outcome = hooks.probe_candidate(candidate)
    recovered_state = replace(
        state,
        recovery_budget=ClockRecoveryBudget(
            used_pct=float(charged_pct),
            limit_pct=float(state.recovery_budget.limit_pct),
        ),
    )
    return candidate, outcome, recovered_state


def spend_remaining_clock_budget_at_voltage_floor(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    stable_candidate: VfCurveCandidate,
    hooks: LowerVoltageSweepHooks,
    probe_history: list[VoltageProbeOutcome],
) -> tuple[VfCurveCandidate, VoltageSweepState, list[LowerVoltageSweepEvent]]:
    events: list[LowerVoltageSweepEvent] = []
    floor_voltage_mv = int(stable_candidate.voltage_mv)
    while not state.recovery_budget.spent_or_disabled:
        next_target = next_voltage_floor_clock_target(
            base_curve,
            settings=settings,
            state=state,
            stable_candidate=stable_candidate,
        )
        if next_target is None:
            break
        target_mhz, recovery_budget = next_target
        recovered = try_voltage_floor_clock_probe(
            base_curve,
            settings=settings,
            state=state,
            target_mhz=int(target_mhz),
            recovery_budget=recovery_budget,
            floor_voltage_mv=int(floor_voltage_mv),
            hooks=hooks,
            probe_history=probe_history,
        )
        if recovered is None:
            events.append(
                LowerVoltageSweepEvent(
                    "stop",
                    "voltage floor clock recovery rejected",
                )
            )
            break
        for passed_candidate, passed_outcome in recovered.passed_results:
            if (
                passed_candidate is recovered.best_candidate
                and passed_outcome is recovered.best_outcome
            ):
                continue
            if hooks.record_passed_candidate is not None:
                hooks.record_passed_candidate(passed_candidate, passed_outcome)
        stable_candidate, state = accept_voltage_floor_clock_probe(
            state=state,
            candidate=recovered.best_candidate,
            outcome=recovered.best_outcome,
            recovery_budget=recovery_budget,
        )
        hooks.write_verified_candidate(stable_candidate, recovered.best_outcome)
        events.append(
            LowerVoltageSweepEvent(
                "recover",
                f"{stable_candidate.voltage_mv}mV@{stable_candidate.target_mhz}MHz",
            )
        )
    return stable_candidate, state, events


def next_voltage_floor_clock_target(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    stable_candidate: VfCurveCandidate,
) -> tuple[int, ClockRecoveryBudget] | None:
    max_drop_pct = max_clock_drop_pct_for_min_core_clock(settings.min_core_clock_pct)
    measured_target_mhz = int(
        state.stable_measured_target_mhz or stable_candidate.target_mhz
    )
    trial_used_pct = float(state.recovery_budget.used_pct)
    while trial_used_pct < float(state.recovery_budget.limit_pct):
        next_used_pct = next_recovery_budget_used_pct(
            current_used_pct=float(trial_used_pct),
            limit_pct=float(state.recovery_budget.limit_pct),
            reason="voltage floor reached",
            measured_target_mhz=int(measured_target_mhz),
            baseline_clock_mhz=settings.baseline_core_clock_mhz,
            max_clock_drop_pct=float(max_drop_pct),
        )
        if next_used_pct is None or float(next_used_pct) <= float(trial_used_pct):
            return None
        target_mhz = choose_clock_recovery_target_mhz(
            base_curve,
            measured_target_mhz=int(measured_target_mhz),
            baseline_clock_mhz=settings.baseline_core_clock_mhz,
            max_clock_drop_pct=float(max_drop_pct),
            budget_used_pct=float(next_used_pct),
            cap_clock_mhz=settings.measured_clock_cap_mhz,
            minimum_target_mhz=int(stable_candidate.target_mhz) + 15,
        )
        charged_pct = charge_recovery_budget_for_target(
            measured_target_mhz=int(measured_target_mhz),
            recovered_target_mhz=int(target_mhz),
            requested_used_pct=float(next_used_pct),
            limit_pct=float(state.recovery_budget.limit_pct),
            baseline_clock_mhz=settings.baseline_core_clock_mhz,
            max_clock_drop_pct=float(max_drop_pct),
        )
        if charged_pct is None:
            return None
        if (
            int(target_mhz) > int(stable_candidate.target_mhz)
            and float(charged_pct) > float(state.recovery_budget.used_pct)
        ):
            return int(target_mhz), ClockRecoveryBudget(
                used_pct=float(charged_pct),
                limit_pct=float(state.recovery_budget.limit_pct),
            )
        trial_used_pct = float(next_used_pct)
    return None


def try_voltage_floor_clock_probe(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    target_mhz: int,
    recovery_budget: ClockRecoveryBudget,
    hooks: LowerVoltageSweepHooks,
    probe_history: list[VoltageProbeOutcome],
    floor_voltage_mv: int | None = None,
) -> VoltageFloorClockProbeResult | None:
    best: tuple[VfCurveCandidate, VoltageProbeOutcome] | None = None
    best_fps: float | None = None
    passed_results: list[tuple[VfCurveCandidate, VoltageProbeOutcome]] = []
    for voltage_mv in voltage_floor_recovery_voltages(
        base_curve,
        floor_voltage_mv=int(
            state.stable_voltage_mv if floor_voltage_mv is None else floor_voltage_mv
        ),
        start_voltage_mv=int(settings.start_voltage_mv),
        allow_voltage_bump=bool(settings.allow_voltage_bump_for_floor_clock_recovery),
        recovery_voltage_ceiling_mv=settings.recovery_voltage_ceiling_mv,
    ):
        candidate = build_flattened_voltage_probe_curve(
            base_curve,
            candidate_voltage_mv=int(voltage_mv),
            target_clock_mhz=int(target_mhz),
            label=(
                f"voltage-floor-clock-recovery {int(voltage_mv)}mV "
                f"recovery-budget={recovery_budget.used_pct:.2f}/"
                f"{recovery_budget.limit_pct:.2f}%"
            ),
        )
        outcome = hooks.probe_candidate(candidate)
        probe_history.append(outcome)
        if outcome.decision.passed:
            fps = outcome_avg_fps(outcome)
            if best is None:
                passed_results.append((candidate, outcome))
                best = (candidate, outcome)
                best_fps = fps
                if fps is None:
                    break
                continue
            if fps is None or best_fps is None or float(fps) <= float(best_fps):
                break
            passed_results.append((candidate, outcome))
            best = (candidate, outcome)
            best_fps = float(fps)
            continue
        hooks.mark_unsafe_candidate(candidate, outcome)
        if best is not None:
            break
    if best is None:
        return None
    return VoltageFloorClockProbeResult(
        best_candidate=best[0],
        best_outcome=best[1],
        passed_results=tuple(passed_results),
    )


def outcome_avg_fps(outcome: VoltageProbeOutcome) -> float | None:
    value = read_field(outcome.raw_probe, "avg_fps")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def voltage_floor_recovery_voltages(
    base_curve: list[dict],
    *,
    floor_voltage_mv: int,
    start_voltage_mv: int,
    allow_voltage_bump: bool,
    recovery_voltage_ceiling_mv: int | None = None,
) -> list[int]:
    voltages = [int(floor_voltage_mv)]
    if not bool(allow_voltage_bump):
        return voltages
    ceiling_mv = int(start_voltage_mv)
    if recovery_voltage_ceiling_mv is not None:
        ceiling_mv = min(ceiling_mv, int(recovery_voltage_ceiling_mv))
    voltages.extend(
        int(value)
        for value in higher_editable_voltage_bins(base_curve, int(floor_voltage_mv))
        if int(value) <= int(ceiling_mv)
    )
    return voltages


def accept_voltage_floor_clock_probe(
    *,
    state: VoltageSweepState,
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome,
    recovery_budget: ClockRecoveryBudget,
) -> tuple[VfCurveCandidate, VoltageSweepState]:
    measured_target_mhz = int(outcome.measured_core_clock_mhz or candidate.target_mhz)
    return candidate, replace(
        state,
        stable_voltage_mv=int(candidate.voltage_mv),
        stable_target_mhz=int(candidate.target_mhz),
        stable_measured_target_mhz=int(measured_target_mhz),
        next_voltage_mv=None,
        recovery_budget=recovery_budget,
    )


def accept_voltage_probe(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome,
    min_search_voltage_mv: int | None,
) -> tuple[VfCurveCandidate, VoltageSweepState]:
    measured_target_mhz = int(outcome.measured_core_clock_mhz or candidate.target_mhz)
    reference_voltage_mv = (
        float(outcome.measured_voltage_mv)
        if outcome.measured_voltage_mv is not None
        else settings.reference_actual_voltage_mv
    )
    next_voltage_mv = select_next_lower_voltage(
        base_curve,
        start_voltage_mv=int(settings.start_voltage_mv),
        stable_voltage_mv=int(candidate.voltage_mv),
        reference_actual_voltage_mv=reference_voltage_mv,
        preserve_base_below_mv=settings.preserve_base_below_mv,
        min_search_voltage_mv=min_search_voltage_mv,
        failed_floor_voltage_mv=state.failed_floor_voltage_mv,
    )
    next_state = replace(
        state,
        stable_voltage_mv=int(candidate.voltage_mv),
        stable_target_mhz=int(candidate.target_mhz),
        stable_measured_target_mhz=measured_target_mhz,
        next_voltage_mv=next_voltage_mv,
    )
    return candidate, next_state


def higher_min_search_voltage(
    *,
    configured_min_search_mv: int | None,
    unsafe_min_search_mv: int | None,
) -> int | None:
    values = [
        int(value)
        for value in (configured_min_search_mv, unsafe_min_search_mv)
        if value is not None
    ]
    return max(values) if values else None
