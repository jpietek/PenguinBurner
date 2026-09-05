"""Shared base undervolt sweep used by all Auto-UV presets.

Algorithm:
- start from the loaded stable baseline curve
- build and probe one lower-voltage VF curve at a time
- accept passing candidates and descend again
- Efficiency may keep an older FPS/W-best candidate instead of the latest pass
- low-clock-only failures stop the first pass or are skipped in deeper search
- hard failures are marked unsafe and stop the sweep

GPU side effects stay behind IO callbacks; this file shows scan order and
decision flow.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, cast

from auto_uv.domain.types import (
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.curve.base_vf_curve_validation import validate_base_vf_curve
from auto_uv.scan_mode.efficiency_fps_per_w_policy import (
    compare_temperature_normalized_fps_per_w,
    decide_efficiency_stop,
    power_increased_while_efficiency_flat,
)
from auto_uv.scan_mode.auto_uv_mode import (
    AUTO_UV_MODE_BALANCED,
    AUTO_UV_MODE_EFFICIENCY,
)
from auto_uv.curve.flattened_voltage_probe_curve import build_flattened_voltage_probe_curve
from auto_uv.run.lower_voltage_probe_target import (
    base_curve_target_for_lower_voltage,
    lower_voltage_phase,
    scan_clock_floor_mhz,
)
from auto_uv.run.lower_voltage_search import select_next_lower_voltage
from auto_uv.persistence.unsafe_voltage_cache import (
    unsafe_min_search_voltage,
    unsafe_voltage_block_reason,
)
from auto_uv.run.voltage_sweep_state import (
    LowerVoltageSweepEvent,
    LowerVoltageSweepResult,
    VoltageProbeOutcome,
    VoltageSweepState,
)


@dataclass(frozen=True, slots=True)
class BaseUvLoopIO:
    probe_candidate: Callable[[VfCurveCandidate], VoltageProbeOutcome]
    write_verified_candidate: Callable[[VfCurveCandidate, VoltageProbeOutcome], None]
    mark_unsafe_candidate: Callable[[VfCurveCandidate, VoltageProbeOutcome], None]
    record_passed_candidate: (
        Callable[[VfCurveCandidate, VoltageProbeOutcome], None] | None
    ) = None


@dataclass(frozen=True, slots=True)
class SweepSelection:
    selected_candidate: VfCurveCandidate
    selected_outcome: VoltageProbeOutcome | None
    no_gain_streak: int = 0
    pending_previous_curve: bool = False


@dataclass(frozen=True, slots=True)
class PassedProbeDecision:
    selected_candidate: VfCurveCandidate
    selected_outcome: VoltageProbeOutcome | None
    no_gain_streak: int
    pending_previous_curve: bool
    write_current: bool
    record_current: bool
    should_stop: bool
    stop_message: str | None = None


@dataclass(frozen=True, slots=True)
class PassedProbeStep:
    latest_stable_candidate: VfCurveCandidate
    selected_result: SweepSelection
    state: VoltageSweepState
    should_stop: bool


@dataclass(frozen=True, slots=True)
class LowClockStep:
    state: VoltageSweepState
    should_continue: bool


def run_base_uv_loop(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    initial_stable_candidate: VfCurveCandidate,
    io: BaseUvLoopIO,
    unsafe_entries: list[dict] | None = None,
    initial_stable_outcome: VoltageProbeOutcome | None = None,
) -> LowerVoltageSweepResult:
    validate_base_vf_curve(base_curve)
    unsafe_floor_mv, unsafe_min_search_mv = unsafe_min_search_voltage(
        base_curve,
        start_voltage_mv=int(settings.start_voltage_mv),
        unsafe_entries=list(unsafe_entries or []),
        profile_tier=settings.auto_uv_mode,
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
            min_search_voltage_mv=min_search_voltage_mv,
            failed_floor_voltage_mv=unsafe_floor_mv,
        ),
        stable_measured_target_mhz=propagated_measured_target_mhz(
            initial_stable_candidate,
            initial_stable_outcome,
        ),
    )
    latest_stable_candidate = initial_stable_candidate
    selected_result = SweepSelection(
        selected_candidate=initial_stable_candidate,
        selected_outcome=initial_stable_outcome,
    )
    probe_history: list[VoltageProbeOutcome] = []
    events: list[LowerVoltageSweepEvent] = []

    while state.next_voltage_mv is not None:
        # 1. Do not retest a voltage/clock pair already marked unsafe.
        block_reason = unsafe_voltage_block_reason(
            list(unsafe_entries or []),
            candidate_voltage_mv=int(state.next_voltage_mv),
            lock_clock_mhz=int(state.stable_target_mhz),
            profile_tier=settings.auto_uv_mode,
        )
        if block_reason:
            events.append(LowerVoltageSweepEvent("stop", block_reason))
            break

        # 2. Build and probe the next lower-voltage curve.
        candidate, state = build_next_lower_voltage_candidate(
            base_curve,
            settings=settings,
            state=state,
            probe_history=probe_history,
        )
        outcome = io.probe_candidate(candidate)
        probe_history.append(outcome)

        # 3. Passing probes become the latest safe point; Efficiency may select
        #    the previous better FPS/W point and stop.
        if outcome.decision.passed:
            step = accept_passing_probe(
                base_curve,
                settings=settings,
                state=state,
                selected_result=selected_result,
                candidate=candidate,
                outcome=outcome,
                io=io,
                events=events,
                min_search_voltage_mv=min_search_voltage_mv,
            )
            latest_stable_candidate = step.latest_stable_candidate
            selected_result = step.selected_result
            state = step.state
            if step.should_stop:
                break
            continue

        # 4. Low-clock-only probes are stable but below the clock floor. The
        #    base pass stops; Efficiency's deeper search can keep descending.
        if is_recoverable_low_clock(outcome.decision):
            step = handle_low_clock_probe(
                base_curve,
                settings=settings,
                state=state,
                candidate=candidate,
                outcome=outcome,
                events=events,
                min_search_voltage_mv=min_search_voltage_mv,
            )
            state = step.state
            if step.should_continue:
                continue
            break

        # 5. Real stability failures are cached unsafe and stop this sweep.
        io.mark_unsafe_candidate(candidate, outcome)
        events.append(LowerVoltageSweepEvent("stop", outcome.decision.reason))
        break

    if not same_candidate_identity(
        selected_result.selected_candidate,
        latest_stable_candidate,
    ):
        state = state_for_selected_candidate(
            state,
            candidate=selected_result.selected_candidate,
            outcome=selected_result.selected_outcome,
        )
    return LowerVoltageSweepResult(
        stable_candidate=selected_result.selected_candidate,
        state=state,
        stable_outcome=selected_result.selected_outcome,
        probe_history=probe_history,
        events=events,
    )


def accept_passing_probe(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    selected_result: SweepSelection,
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome,
    io: BaseUvLoopIO,
    events: list[LowerVoltageSweepEvent],
    min_search_voltage_mv: int | None,
) -> PassedProbeStep:
    latest_stable_candidate, state = accept_voltage_probe(
        base_curve,
        settings=settings,
        state=state,
        candidate=candidate,
        outcome=outcome,
        min_search_voltage_mv=min_search_voltage_mv,
    )
    decision = decide_passed_probe(
        settings=settings,
        state=state,
        selection=selected_result,
        candidate=latest_stable_candidate,
        outcome=outcome,
    )
    selected_result = SweepSelection(
        selected_candidate=decision.selected_candidate,
        selected_outcome=decision.selected_outcome,
        no_gain_streak=int(decision.no_gain_streak),
        pending_previous_curve=bool(decision.pending_previous_curve),
    )
    if decision.write_current:
        io.write_verified_candidate(latest_stable_candidate, outcome)
    elif decision.record_current and io.record_passed_candidate is not None:
        io.record_passed_candidate(latest_stable_candidate, outcome)
    events.append(
        LowerVoltageSweepEvent(
            "accept",
            f"{latest_stable_candidate.voltage_mv}mV@"
            f"{latest_stable_candidate.target_mhz}MHz",
        )
    )
    if not decision.should_stop:
        return PassedProbeStep(
            latest_stable_candidate=latest_stable_candidate,
            selected_result=selected_result,
            state=state,
            should_stop=False,
        )

    selected_candidate = selected_result.selected_candidate
    state = state_for_selected_candidate(
        state,
        candidate=selected_candidate,
        outcome=selected_result.selected_outcome,
    )
    events.append(
        LowerVoltageSweepEvent(
            "stop",
            decision.stop_message or "fps-per-watt wall reached",
        )
    )
    return PassedProbeStep(
        latest_stable_candidate=selected_candidate,
        selected_result=selected_result,
        state=state,
        should_stop=True,
    )


def handle_low_clock_probe(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome,
    events: list[LowerVoltageSweepEvent],
    min_search_voltage_mv: int | None,
) -> LowClockStep:
    # The GPU stayed stable; only the core clock dipped below the floor.
    # This is not an unstable voltage, so it must NOT be cached unsafe: doing so
    # would also block the Efficiency lower-voltage pass from retrying it.
    if settings.descend_through_low_clock:
        events.append(
            LowerVoltageSweepEvent(
                "low-clock-skip",
                f"{candidate.voltage_mv}mV below clock floor; descending further",
            )
        )
        return LowClockStep(
            state=advance_through_low_clock(
                base_curve,
                settings=settings,
                state=state,
                candidate=candidate,
                outcome=outcome,
                min_search_voltage_mv=min_search_voltage_mv,
            ),
            should_continue=True,
        )

    # First pass: the natural clock floor is reached. Stop here and let the
    # Efficiency preset continue its lower-voltage search if applicable.
    events.append(LowerVoltageSweepEvent("stop", outcome.decision.reason))
    return LowClockStep(state=state, should_continue=False)


def build_next_lower_voltage_candidate(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    probe_history: list[VoltageProbeOutcome],
) -> tuple[VfCurveCandidate, VoltageSweepState]:
    assert state.next_voltage_mv is not None
    _ = probe_history
    tail_rise_bins = max(0, int(settings.tail_rise_bins))
    target_mhz = base_curve_target_for_lower_voltage(
        base_curve,
        candidate_voltage_mv=int(state.next_voltage_mv),
        stable_target_mhz=int(state.stable_target_mhz),
        stable_measured_target_mhz=state.stable_measured_target_mhz,
        baseline_core_clock_mhz=settings.baseline_core_clock_mhz,
        min_core_clock_pct=settings.min_core_clock_pct,
    )
    floor_mhz = scan_clock_floor_mhz(
        baseline_core_clock_mhz=settings.baseline_core_clock_mhz,
        min_core_clock_pct=settings.min_core_clock_pct,
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
            f"phase={phase}"
        ),
        tail_rise_bins=int(tail_rise_bins),
        metadata={
            "tail_rise_bins": int(tail_rise_bins),
            "target_policy": "hold-required-clock",
            **(
                {
                    "scan_clock_floor_mhz": int(floor_mhz),
                    "scan_clock_floor_pct": float(settings.min_core_clock_pct),
                    "scan_clock_floor_reference_mhz": float(
                        settings.baseline_core_clock_mhz or 0.0
                    ),
                }
                if floor_mhz is not None
                else {}
            ),
        },
    )
    return candidate, state


def propagated_measured_target_mhz(
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome | None,
) -> int:
    """Keep requested headroom when a passing probe holds a lower clock.

    Replacing the request with each lower measured average compounds normal
    request-to-measurement gaps across voltage steps. This happens even after
    a power cap stops binding, and is especially visible with a flat tail.
    Preserve upward measured gains from rising tails, but let failed probes
    stop the descent instead of progressively lowering a passing target.
    """
    if outcome is None or outcome.measured_core_clock_mhz is None:
        return int(candidate.target_mhz)
    return max(int(candidate.target_mhz), int(outcome.measured_core_clock_mhz))


def decide_passed_probe(
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    selection: SweepSelection,
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome,
) -> PassedProbeDecision:
    if not uses_efficiency_fps_per_w_wall(settings):
        return accept_current_candidate(
            candidate,
            outcome,
            write_current=True,
        )

    previous_outcome = selection.selected_outcome
    previous_probe = previous_outcome.raw_probe if previous_outcome is not None else None
    candidate_probe = outcome.raw_probe
    if previous_probe is None or candidate_probe is None:
        return accept_current_candidate(
            candidate,
            outcome,
            write_current=True,
        )

    normalized = compare_temperature_normalized_fps_per_w(
        previous_probe,
        candidate_probe,
    )
    improved = normalized.get("improved")
    voltage_close = bool(normalized.get("measured_voltage_close_to_requested", True))
    if improved is not False or not voltage_close:
        return accept_current_candidate(
            candidate,
            outcome,
            write_current=True,
        )

    no_gain_streak = int(selection.no_gain_streak) + 1
    power_up_efficiency_down = power_increased_while_efficiency_flat(
        previous_power_w=float_or_none(normalized.get("previous_power_w")),
        candidate_power_w=float_or_none(normalized.get("candidate_power_w")),
        previous_fps_per_w=float_or_none(normalized.get("previous_fps_per_w")),
        candidate_fps_per_w=float_or_none(normalized.get("candidate_fps_per_w")),
    )
    stop_decision = decide_efficiency_stop(
        efficiency_stop_candidate=True,
        voltage_drop_from_start_pct=voltage_drop_from_start_pct(
            start_voltage_mv=int(settings.start_voltage_mv),
            candidate_voltage_mv=int(candidate.voltage_mv),
        ),
        min_voltage_drop_pct=float(settings.min_efficiency_stop_voltage_drop_pct),
        no_gain_streak=int(no_gain_streak),
        required_extra_confirmations=max(0, int(settings.efficiency_stop_streak)),
        pending_previous_curve=bool(selection.pending_previous_curve),
        power_up_efficiency_down=bool(power_up_efficiency_down),
        efficiency_delta_pct=float_or_none(normalized.get("delta_pct")),
    )
    if bool(stop_decision.should_stop) and bool(stop_decision.use_current_curve):
        return PassedProbeDecision(
            selected_candidate=candidate,
            selected_outcome=outcome,
            no_gain_streak=int(no_gain_streak),
            pending_previous_curve=False,
            write_current=True,
            record_current=False,
            should_stop=True,
            stop_message=stop_decision.reason,
        )
    return PassedProbeDecision(
        selected_candidate=selection.selected_candidate,
        selected_outcome=selection.selected_outcome,
        no_gain_streak=int(no_gain_streak),
        pending_previous_curve=True,
        write_current=False,
        record_current=True,
        should_stop=bool(stop_decision.should_stop),
        stop_message=stop_decision.reason,
    )


def uses_efficiency_fps_per_w_wall(settings: AutoUvScanSettings) -> bool:
    return str(settings.auto_uv_mode) in {
        AUTO_UV_MODE_BALANCED,
        AUTO_UV_MODE_EFFICIENCY,
    }


def accept_current_candidate(
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome,
    *,
    write_current: bool,
) -> PassedProbeDecision:
    return PassedProbeDecision(
        selected_candidate=candidate,
        selected_outcome=outcome,
        no_gain_streak=0,
        pending_previous_curve=False,
        write_current=bool(write_current),
        record_current=False,
        should_stop=False,
    )


def state_for_selected_candidate(
    state: VoltageSweepState,
    *,
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome | None,
) -> VoltageSweepState:
    measured_target_mhz = propagated_measured_target_mhz(candidate, outcome)
    return replace(
        state,
        stable_voltage_mv=int(candidate.voltage_mv),
        stable_target_mhz=int(candidate.target_mhz),
        stable_measured_target_mhz=int(measured_target_mhz),
        next_voltage_mv=None,
    )


def same_candidate_identity(left: VfCurveCandidate, right: VfCurveCandidate) -> bool:
    return int(left.voltage_mv) == int(right.voltage_mv) and int(left.target_mhz) == int(
        right.target_mhz
    )


def voltage_drop_from_start_pct(*, start_voltage_mv: int, candidate_voltage_mv: int) -> float:
    if int(start_voltage_mv) <= 0:
        return 0.0
    return (
        max(0.0, float(start_voltage_mv) - float(candidate_voltage_mv))
        / float(start_voltage_mv)
        * 100.0
    )


def float_or_none(value: object) -> float | None:
    raw_value = cast(Any, value)
    try:
        return None if raw_value is None else float(raw_value)
    except (TypeError, ValueError):
        return None


def is_recoverable_low_clock(decision: StableRunDecision) -> bool:
    return (
        not decision.passed
        and decision.failure_kind is FailureKind.LOW_CLOCK
        and decision.severity is FailureSeverity.RECOVERABLE
    )


def advance_through_low_clock(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome,
    min_search_voltage_mv: int | None,
) -> VoltageSweepState:
    """Skip a low-clock voltage and keep descending toward the minimum.

    The stable point is left untouched (its measured clock stays the held
    target), so only the next voltage to probe moves down one step.
    """

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
        min_search_voltage_mv=min_search_voltage_mv,
        failed_floor_voltage_mv=state.failed_floor_voltage_mv,
    )
    return replace(state, next_voltage_mv=next_voltage_mv)


def accept_voltage_probe(
    base_curve: list[dict],
    *,
    settings: AutoUvScanSettings,
    state: VoltageSweepState,
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome,
    min_search_voltage_mv: int | None,
) -> tuple[VfCurveCandidate, VoltageSweepState]:
    measured_target_mhz = propagated_measured_target_mhz(candidate, outcome)
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
