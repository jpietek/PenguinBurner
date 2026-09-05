"""Coverage-focused tests for the lower-voltage pure-logic modules.

These exercise the reachable decision branches in:
- auto_uv/lower_voltage_probe_target.py
- auto_uv/lower_voltage_search.py
- auto_uv/base_uv_loop.py

They follow the stubbing conventions of test_base_uv_loop.py and
test_voltage_search_and_cache.py (io + base_curve/probe_summary builders).
"""

from __future__ import annotations

from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.domain.types import (
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv.run.lower_voltage_probe_target import (
    lower_voltage_phase,
)
from auto_uv.run.lower_voltage_search import (
    filter_effective_voltage_candidates,
    select_aggressive_voltage_bins,
    select_next_lower_voltage,
)
from auto_uv.base_uv_loop import (
    SweepSelection,
    BaseUvLoopIO,
    build_next_lower_voltage_candidate,
    decide_passed_probe,
    float_or_none,
    run_base_uv_loop,
    state_for_selected_candidate,
    voltage_drop_from_start_pct,
)
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome, VoltageSweepState
from auto_uv_test_data import (
    base_curve,
    probe_summary,
    rtx_5080_20260524_high_oc_base_curve,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _passed_outcome(
    candidate: VfCurveCandidate,
    *,
    clock_mhz: float | None = None,
    power_w: float = 200.0,
    fps: float = 100.0,
) -> VoltageProbeOutcome:
    clock = float(candidate.target_mhz) if clock_mhz is None else float(clock_mhz)
    return VoltageProbeOutcome(
        decision=StableRunDecision(
            passed=True,
            failure_kind=FailureKind.NONE,
            severity=FailureSeverity.PASS,
            reason="stable run",
        ),
        measured_core_clock_mhz=clock,
        measured_voltage_mv=float(candidate.voltage_mv),
        raw_probe=probe_summary(
            candidate.voltage_mv,
            clock_mhz=clock,
            fps=fps,
            power_w=power_w,
        ),
    )


def _baseline_candidate(curve: list[dict]) -> VfCurveCandidate:
    return VfCurveCandidate(
        label="baseline",
        voltage_mv=1000,
        target_mhz=2160,
        flattened_plan=curve,
    )


# ---------------------------------------------------------------------------
# lower_voltage_probe_target.py
# ---------------------------------------------------------------------------


def test_lower_voltage_phase_returns_fine_below_medium_threshold() -> None:
    # ratio = 850/1000 = 0.85 < medium 0.88 -> "fine" (line 31)
    assert (
        lower_voltage_phase(start_voltage_mv=1000, candidate_voltage_mv=850) == "fine"
    )
    # ratio > coarse 0.94 -> "coarse"
    assert (
        lower_voltage_phase(start_voltage_mv=1000, candidate_voltage_mv=960)
        == "coarse"
    )
    # between -> "medium"
    assert (
        lower_voltage_phase(start_voltage_mv=1000, candidate_voltage_mv=900)
        == "medium"
    )
    # start_voltage_mv <= 0 -> ratio forced to 1.0 -> "coarse"
    assert (
        lower_voltage_phase(start_voltage_mv=0, candidate_voltage_mv=900) == "coarse"
    )


def test_lower_voltage_candidate_holds_requested_target_and_tail() -> None:
    curve = rtx_5080_20260524_high_oc_base_curve()
    candidate, _state = build_next_lower_voltage_candidate(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=860,
            auto_uv_mode="performance",
            tail_rise_bins=6,
            reference_actual_voltage_mv=None,
        ),
        state=VoltageSweepState(
            stable_voltage_mv=1000,
            stable_target_mhz=2730,
            stable_measured_target_mhz=2730,
            next_voltage_mv=860,
        ),
        probe_history=[],
    )

    tail_targets = [
        int(point["target_mhz"])
        for point in candidate.flattened_plan
        if int(point["voltage_mv"]) >= int(candidate.voltage_mv)
    ]

    assert candidate.voltage_mv == 860
    assert candidate.target_mhz == 2730
    assert candidate.metadata["tail_rise_bins"] == 6
    assert tail_targets[:7] == [2730, 2745, 2760, 2775, 2790, 2805, 2820]


# ---------------------------------------------------------------------------
# lower_voltage_search.py
# ---------------------------------------------------------------------------


def test_select_next_lower_voltage_filters_failed_floor() -> None:
    # failed_floor_voltage_mv=920 drops the 900 bin while keeping 925/950/975
    # (lines 44-48: the floor-filter list comprehension runs and removes a bin).
    selected = select_next_lower_voltage(
        base_curve(900, 1025, 25),
        start_voltage_mv=1000,
        stable_voltage_mv=1000,
        reference_actual_voltage_mv=1000.0,
        min_search_voltage_mv=900,
        failed_floor_voltage_mv=920,
    )
    assert selected == 925


def test_select_next_lower_voltage_none_when_floor_clears_all_bins() -> None:
    # failed floor removes every editable bin below stable -> bins empty -> None
    # (line 49-50 path: `if not bins: return None`).
    selected = select_next_lower_voltage(
        base_curve(900, 1025, 25),
        start_voltage_mv=1000,
        stable_voltage_mv=925,
        reference_actual_voltage_mv=None,
        min_search_voltage_mv=None,
        failed_floor_voltage_mv=924,
    )
    assert selected is None


def test_select_aggressive_bins_uses_fine_step_below_medium() -> None:
    # All bins far below medium threshold (90% of 1000 = 900) so fine step (1)
    # is used for each (line 92), and final bin appended (line 100 path).
    bins = [880, 860, 840, 820, 800]
    selected = select_aggressive_voltage_bins(bins, start_voltage_mv=1000)
    # fine step of 1 picks every bin; final already present
    assert selected == [880, 860, 840, 820, 800]


def test_select_aggressive_bins_lands_on_final_bin() -> None:
    # Aggressive (>= 90%) stepping always clamps pick_index to the last index,
    # so the final bin is selected by the loop itself.
    bins = [990, 980, 970, 960, 950]
    selected = select_aggressive_voltage_bins(bins, start_voltage_mv=1000)
    assert selected[-1] == 950
    assert 950 in selected


def test_filter_effective_returns_empty_for_empty_bins() -> None:
    # line 112
    assert (
        filter_effective_voltage_candidates(
            [],
            stable_voltage_mv=1000,
            reference_actual_voltage_mv=1000.0,
        )
        == []
    )


def test_filter_effective_passes_through_without_reference() -> None:
    # reference None -> return list copy unchanged (line 114)
    bins = [950, 925, 900]
    result = filter_effective_voltage_candidates(
        bins,
        stable_voltage_mv=1000,
        reference_actual_voltage_mv=None,
    )
    assert result == bins
    assert result is not bins


def test_filter_effective_uses_lower_voltage_gaps_and_appends_final() -> None:
    # Bins well below the medium ratio threshold so the lower-voltage gap rules
    # apply (lines 129-130). The final bin (803) fails both the 5mV nominal-drop
    # test (806-803=3) and the 5mV effective-gap test (|803-805|=2), so the loop
    # drops it -- then line 143 re-appends it as the testable low bin.
    result = filter_effective_voltage_candidates(
        [806, 803],
        stable_voltage_mv=1000,
        reference_actual_voltage_mv=805.0,
    )
    # first always kept (lower-voltage branch reached, lines 129-130)
    assert result[0] == 806
    # final low bin re-appended despite failing the gap tests (line 143)
    assert result[-1] == 803
    assert result == [806, 803]


# ---------------------------------------------------------------------------
# base_uv_loop.py -- module-level helpers
# ---------------------------------------------------------------------------


def test_voltage_drop_from_start_pct_zero_when_start_nonpositive() -> None:
    # lines 385-387
    assert voltage_drop_from_start_pct(start_voltage_mv=0, candidate_voltage_mv=900) == 0.0
    assert (
        voltage_drop_from_start_pct(start_voltage_mv=-5, candidate_voltage_mv=900)
        == 0.0
    )


def test_voltage_drop_from_start_pct_normal() -> None:
    assert voltage_drop_from_start_pct(
        start_voltage_mv=1000, candidate_voltage_mv=900
    ) == 10.0


def test_float_or_none_handles_none_and_bad_values() -> None:
    # line 395-398
    assert float_or_none(None) is None
    assert float_or_none("not-a-number") is None
    assert float_or_none(object()) is None
    assert float_or_none("3.5") == 3.5
    assert float_or_none(7) == 7.0


def test_state_for_selected_candidate_uses_candidate_target_without_outcome() -> None:
    # lines 362-369 with outcome None -> falls back to candidate.target_mhz
    state = VoltageSweepState(
        stable_voltage_mv=1000,
        stable_target_mhz=2160,
        next_voltage_mv=900,
    )
    candidate = VfCurveCandidate(
        label="x", voltage_mv=925, target_mhz=2100, flattened_plan=[]
    )
    new_state = state_for_selected_candidate(state, candidate=candidate, outcome=None)
    assert new_state.stable_voltage_mv == 925
    assert new_state.stable_target_mhz == 2100
    assert new_state.stable_measured_target_mhz == 2100
    assert new_state.next_voltage_mv is None


def test_decide_passed_probe_records_without_stopping() -> None:
    """Efficiency declines but stop is not yet armed: record-not-write path.

    Covers lines 292-311 + the non-stop return (323-332).
    """
    settings = AutoUvScanSettings(
        start_voltage_mv=1000,
        min_search_voltage_mv=900,
        reference_actual_voltage_mv=1000.0,
        efficiency_stop_streak=2,
    )
    state = VoltageSweepState(
        stable_voltage_mv=975, stable_target_mhz=2100, next_voltage_mv=950
    )
    # previous probe: better efficiency; candidate probe: worse efficiency.
    previous_outcome = VoltageProbeOutcome(
        decision=StableRunDecision(
            passed=True,
            failure_kind=FailureKind.NONE,
            severity=FailureSeverity.PASS,
            reason="ok",
        ),
        measured_core_clock_mhz=2100.0,
        measured_voltage_mv=1000.0,
        raw_probe=probe_summary(1000, clock_mhz=2100.0, fps=100.0, power_w=180.0),
    )
    candidate = VfCurveCandidate(
        label="c", voltage_mv=975, target_mhz=2100, flattened_plan=[]
    )
    candidate_outcome = VoltageProbeOutcome(
        decision=StableRunDecision(
            passed=True,
            failure_kind=FailureKind.NONE,
            severity=FailureSeverity.PASS,
            reason="ok",
        ),
        measured_core_clock_mhz=2100.0,
        measured_voltage_mv=975.0,
        # same fps, more power -> worse fps/W -> improved False
        raw_probe=probe_summary(975, clock_mhz=2100.0, fps=100.0, power_w=200.0),
    )
    selection = SweepSelection(
        selected_candidate=VfCurveCandidate(
            label="prev", voltage_mv=1000, target_mhz=2100, flattened_plan=[]
        ),
        selected_outcome=previous_outcome,
        no_gain_streak=0,
        pending_previous_curve=False,
    )
    decision = decide_passed_probe(
        settings=settings,
        state=state,
        selection=selection,
        candidate=candidate,
        outcome=candidate_outcome,
    )
    # First no-gain probe arms the stop but does not stop yet.
    assert decision.should_stop is False
    assert decision.write_current is False
    assert decision.record_current is True
    assert decision.pending_previous_curve is True
    assert decision.no_gain_streak == 1
    # keeps the previous selection
    assert decision.selected_candidate.voltage_mv == 1000


# ---------------------------------------------------------------------------
# base_uv_loop.py -- full-loop branches
# ---------------------------------------------------------------------------


def test_sweep_loop_stops_when_cached_unsafe_blocks_first_candidate() -> None:
    """Unsafe cache blocks the very first candidate -> stop before probing.

    Covers lines 119-120 (block_reason -> stop event + break).
    """
    curve = base_curve(900, 1025, 25, 2000, 40)
    probed: list[int] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        probed.append(int(candidate.voltage_mv))
        return _passed_outcome(candidate)

    io = BaseUvLoopIO(
        probe_candidate=probe,
        write_verified_candidate=lambda _c, _o: None,
        mark_unsafe_candidate=lambda _c, _o: None,
    )
    result = run_base_uv_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=900,
            auto_uv_mode="performance",
            reference_actual_voltage_mv=1000.0,
        ),
        initial_stable_candidate=_baseline_candidate(curve),
        io=io,
        # Clock-aware unsafe entry at the first candidate (950mV): it does NOT
        # raise the min-search floor (so 950 stays the first candidate) but it
        # blocks that candidate's clock band, tripping block_reason.
        unsafe_entries=[
            {"candidate_voltage_mv": 950, "lock_clock_mhz": 2000, "reason": "crash"}
        ],
    )

    assert probed == []  # never probed: blocked first
    assert [event.name for event in result.events] == ["stop"]
    assert "cached unsafe point" in result.events[0].message
    assert result.stable_candidate.voltage_mv == 1000


def test_sweep_loop_efficiency_records_pending_curve_then_finishes() -> None:
    """Efficiency declines on the last reachable bin.

    The probe passes but efficiency drops, so it is recorded (not written) and
    the previous stable curve is kept as the selection. Covers the
    record_passed_candidate branch (157-161) and the end-of-loop
    state_for_selected_candidate reconciliation (191-199 incl. 195).
    """
    curve = base_curve(900, 1025, 25, 2000, 40)
    written: list[int] = []
    recorded: list[int] = []

    # power increases as voltage drops so fps/W gets worse -> no efficiency gain.
    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        # higher power at lower voltage -> efficiency declines
        power = 180.0 + (1000 - int(candidate.voltage_mv))
        return _passed_outcome(candidate, fps=100.0, power_w=power)

    io = BaseUvLoopIO(
        probe_candidate=probe,
        write_verified_candidate=lambda c, _o: written.append(int(c.voltage_mv)),
        mark_unsafe_candidate=lambda _c, _o: None,
        record_passed_candidate=lambda c, _o: recorded.append(int(c.voltage_mv)),
    )
    result = run_base_uv_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=950,
            reference_actual_voltage_mv=1000.0,
            # high voltage-drop requirement so stop never arms; only one bin (950)
            min_efficiency_stop_voltage_drop_pct=99.0,
        ),
        initial_stable_candidate=_baseline_candidate(curve),
        io=io,
        initial_stable_outcome=_passed_outcome(
            _baseline_candidate(curve), fps=100.0, power_w=180.0
        ),
    )

    # the single lower bin (950) declined efficiency -> recorded, not written
    assert recorded == [950]
    assert written == []
    # selection stays on the baseline (1000mV) curve
    assert result.stable_candidate.voltage_mv == 1000
    # end-of-loop reconciliation restored state to the selected (baseline) curve
    assert result.state.stable_voltage_mv == 1000


def test_sweep_loop_balanced_uses_fps_per_w_selection_wall() -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    written: list[int] = []
    recorded: list[int] = []

    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        power = 180.0 + (1000 - int(candidate.voltage_mv))
        return _passed_outcome(candidate, fps=100.0, power_w=power)

    io = BaseUvLoopIO(
        probe_candidate=probe,
        write_verified_candidate=lambda c, _o: written.append(int(c.voltage_mv)),
        mark_unsafe_candidate=lambda _c, _o: None,
        record_passed_candidate=lambda c, _o: recorded.append(int(c.voltage_mv)),
    )
    result = run_base_uv_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=950,
            auto_uv_mode="balanced",
            reference_actual_voltage_mv=1000.0,
            min_efficiency_stop_voltage_drop_pct=99.0,
            tail_rise_bins=4,
        ),
        initial_stable_candidate=_baseline_candidate(curve),
        io=io,
        initial_stable_outcome=_passed_outcome(
            _baseline_candidate(curve), fps=100.0, power_w=180.0
        ),
    )

    assert recorded == [950]
    assert written == []
    assert result.stable_candidate.voltage_mv == 1000
    assert result.state.stable_voltage_mv == 1000


def test_sweep_loop_efficiency_stop_uses_current_curve() -> None:
    """Efficiency wall reached with use_current_curve -> stop on current curve.

    Drives the no-gain streak past the required confirmations so
    decide_efficiency_stop returns should_stop=True and use_current_curve=True,
    covering lines 169-184 (stop branch) and 312-322.
    """
    curve = base_curve(875, 1025, 25, 2000, 40)
    written: list[int] = []

    # First lower bins keep efficiency flat (same fps/W) at lower power, so
    # delta_pct ~ 0 (>= 0) and power does not rise -> use_current_curve True.
    def probe(candidate: VfCurveCandidate) -> VoltageProbeOutcome:
        # identical fps and power at every bin -> delta_pct exactly 0
        return _passed_outcome(candidate, fps=100.0, power_w=180.0)

    io = BaseUvLoopIO(
        probe_candidate=probe,
        write_verified_candidate=lambda c, _o: written.append(int(c.voltage_mv)),
        mark_unsafe_candidate=lambda _c, _o: None,
        record_passed_candidate=lambda _c, _o: None,
    )
    result = run_base_uv_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=1000,
            min_search_voltage_mv=875,
            reference_actual_voltage_mv=1000.0,
            efficiency_stop_streak=0,  # arm + confirm quickly
            min_efficiency_stop_voltage_drop_pct=0.0,
        ),
        initial_stable_candidate=_baseline_candidate(curve),
        io=io,
        initial_stable_outcome=_passed_outcome(
            _baseline_candidate(curve), fps=100.0, power_w=180.0
        ),
    )

    stop_events = [e for e in result.events if e.name == "stop"]
    assert stop_events, "expected an efficiency stop event"
    assert stop_events[-1].message == "fps-per-watt wall reached"
