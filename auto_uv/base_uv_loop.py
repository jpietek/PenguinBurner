"""Shared base undervolt sweep used by all Auto-UV presets.

Algorithm:
- start from the loaded stable baseline curve
- build and probe one lower-voltage VF curve at a time
- accept passing candidates and descend again
- Balanced may stop at an older FPS/W-best point; Efficiency reaches its floor
- hard failures are marked unsafe and stop the sweep

GPU side effects stay behind IO callbacks; this file shows scan order and
decision flow.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, cast

from auto_uv.domain.types import (
    AutoUvCriticalProbeError,
    FailureSeverity,
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
)
from auto_uv.curve.flattened_voltage_probe_curve import build_flattened_voltage_probe_curve
from auto_uv.run.lower_voltage_probe_target import (
    base_curve_target_for_lower_voltage,
    lower_voltage_phase,
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
        # Retained measured gains can raise the clock. Check the built
        # candidate, before any GPU operation, rather than the previous lock.
        candidate, state = build_next_lower_voltage_candidate(
            base_curve,
            settings=settings,
            state=state,
            probe_history=probe_history,
        )
        block_reason = unsafe_voltage_block_reason(
            list(unsafe_entries or []),
            candidate_voltage_mv=int(candidate.voltage_mv),
            lock_clock_mhz=int(candidate.target_mhz),
            profile_tier=settings.auto_uv_mode,
        )
        if block_reason:
            events.append(LowerVoltageSweepEvent("stop", block_reason))
            break

        outcome = io.probe_candidate(candidate)
        probe_history.append(outcome)

        # Passing probes become the latest safe point; Balanced may select
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

        # Failed probes stop this sweep. The persistence layer decides
        #    which failures identify an unsafe voltage.
        io.mark_unsafe_candidate(candidate, outcome)
        if outcome.decision.severity is FailureSeverity.CRITICAL:
            raise AutoUvCriticalProbeError(
                f"Voltage sweep stopped after critical probe failure: {outcome.decision.reason}"
            )
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
    return str(settings.auto_uv_mode) == AUTO_UV_MODE_BALANCED


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
