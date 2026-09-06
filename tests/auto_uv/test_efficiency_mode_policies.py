from __future__ import annotations

import pytest

from auto_uv.scan_mode.efficiency_fps_per_w_policy import (
    EfficiencyPolicyRules,
    best_efficiency_candidate_index,
    compare_temperature_normalized_fps_per_w,
    decide_efficiency_stop,
    derive_efficiency_stop_streak_from_fps_variance,
    fps_variance_pct,
    measured_probe_voltage_mv,
    measured_voltage_close_to_requested,
    measured_voltage_drop,
    power_increased_while_efficiency_flat,
    reference_temperature,
    requested_voltage_drop,
    temperature_normalized_fps_per_w,
    temperature_normalized_power_w,
)
from auto_uv_test_data import probe_summary


def test_efficiency_selects_highest_fps_per_w_from_recorded_5080_tradeoffs() -> None:
    # Unrounded short-probe results from the 2026-09-05 scan (runs 21/23/30/31).
    probes = [
        {"avg_core_clock_mhz": clock, "avg_fps": fps, "avg_power_w": watts}
        for clock, fps, watts in (
            (2450.3846, 55.944, 231.40276923076922),
            (2518.6923, 56.826, 236.03084615384614),
            (2733.6154, 60.197, 254.84053846153847),
            (2758.0, 59.856, 255.89746153846153),
        )
    ]
    assert best_efficiency_candidate_index(probes) == 0


def test_efficiency_does_not_trade_fps_per_w_for_more_clock() -> None:
    probes = [
        {"efficiency_fps_per_w": 1.0, "avg_core_clock_mhz": 2500, "lock_clock_mhz": 2600},
        {"efficiency_fps_per_w": 0.98, "avg_core_clock_mhz": 2600, "lock_clock_mhz": 2700},
        {"efficiency_fps_per_w": 0.96, "avg_core_clock_mhz": 2700, "lock_clock_mhz": 2800},
        {"efficiency_fps_per_w": 0.99, "avg_core_clock_mhz": 2400, "lock_clock_mhz": 3000},
    ]
    assert best_efficiency_candidate_index(probes) == 0
    assert best_efficiency_candidate_index([None, {"efficiency_fps_per_w": float("nan")}]) is None


def test_absolute_power_breaks_ties_without_rewarding_a_lower_clock() -> None:
    probes = [
        {"efficiency_fps_per_w": 0.24, "avg_core_clock_mhz": 2700, "avg_power_w": 250},
        {"efficiency_fps_per_w": 0.24, "avg_core_clock_mhz": 2700, "avg_power_w": 240},
    ]
    assert best_efficiency_candidate_index(probes) == 1
    probes[1]["avg_core_clock_mhz"] = 2600
    assert best_efficiency_candidate_index(probes) == 0


def test_efficiency_policy_temperature_normalizes_fps_per_w() -> None:
    delta = compare_temperature_normalized_fps_per_w(
        probe_summary(
            1000,
            clock_mhz=2200.0,
            fps=100.0,
            power_w=200.0,
            temperature_c=60.0,
        ),
        probe_summary(
            950,
            clock_mhz=2200.0,
            fps=100.0,
            power_w=190.0,
            temperature_c=65.0,
        ),
    )

    assert delta["improved"] is True
    assert delta["measured_voltage_close_to_requested"] is True


def test_efficiency_policy_requires_one_percent_fps_per_w_gain() -> None:
    delta = compare_temperature_normalized_fps_per_w(
        probe_summary(
            1000,
            clock_mhz=2200.0,
            fps=100.0,
            power_w=200.0,
        ),
        probe_summary(
            950,
            clock_mhz=2200.0,
            fps=100.75,
            power_w=200.0,
        ),
    )

    assert delta["delta_pct"] == pytest.approx(0.75)
    assert delta["improved"] is False


def test_efficiency_stop_reverts_to_previous_curve_after_confirmed_wall() -> None:
    decision = decide_efficiency_stop(
        efficiency_stop_candidate=True,
        voltage_drop_from_start_pct=12.0,
        min_voltage_drop_pct=10.0,
        no_gain_streak=3,
        required_extra_confirmations=1,
        pending_previous_curve=True,
        power_up_efficiency_down=True,
        efficiency_delta_pct=-0.2,
    )

    assert decision.should_stop
    assert not decision.use_current_curve
    assert decision.reason == "fps-per-watt wall reached"


def test_efficiency_stop_streak_uses_two_confirmations_for_low_fps_variance() -> None:
    decision = derive_efficiency_stop_streak_from_fps_variance(
        {"fps_variance_pct": 1.2},
        configured_streak=2,
        derive=True,
        high_variance_threshold_pct=2.0,
        low_variance_streak=2,
        high_variance_streak=4,
    )

    assert decision.value == 2
    assert decision.source == "low-fps-variance"


def test_efficiency_stop_streak_uses_four_confirmations_for_high_fps_variance() -> None:
    decision = derive_efficiency_stop_streak_from_fps_variance(
        {"fps_variance_pct": 2.1},
        configured_streak=2,
        derive=True,
        high_variance_threshold_pct=2.0,
        low_variance_streak=2,
        high_variance_streak=4,
    )

    assert decision.value == 4
    assert decision.source == "high-fps-variance"


def test_efficiency_stop_streak_keeps_user_override() -> None:
    decision = derive_efficiency_stop_streak_from_fps_variance(
        {"fps_variance_pct": 3.0},
        configured_streak=1,
        derive=False,
    )

    assert decision.value == 1
    assert decision.source == "user"


# --- fps_variance_pct -------------------------------------------------------


def test_fps_variance_pct_returns_none_when_probe_missing() -> None:
    assert fps_variance_pct(None) is None


def test_fps_variance_pct_returns_none_when_field_missing() -> None:
    assert fps_variance_pct({"avg_fps": 100.0}) is None


def test_fps_variance_pct_returns_none_for_non_numeric_value() -> None:
    assert fps_variance_pct({"fps_variance_pct": "not-a-number"}) is None


def test_fps_variance_pct_clamps_negative_to_zero() -> None:
    assert fps_variance_pct({"fps_variance_pct": -3.0}) == 0.0


# --- derive streak fallback -------------------------------------------------


def test_efficiency_stop_streak_falls_back_when_variance_unavailable() -> None:
    decision = derive_efficiency_stop_streak_from_fps_variance(
        None,
        configured_streak=2,
        derive=True,
    )

    assert decision.value == 2
    assert decision.source == "fallback"
    assert decision.fps_variance_pct is None


# --- compare_temperature_normalized_fps_per_w short-circuit -----------------


def test_compare_returns_none_deltas_when_previous_fps_per_w_zero() -> None:
    # avg_fps == 0 -> previous_fps_per_w == 0.0, hitting the early-return branch.
    delta = compare_temperature_normalized_fps_per_w(
        probe_summary(1000, clock_mhz=2200.0, fps=0.0, power_w=200.0),
        probe_summary(950, clock_mhz=2200.0, fps=100.0, power_w=190.0),
    )

    assert delta["previous_fps_per_w"] == 0.0
    assert delta["delta_fps_per_w"] is None
    assert delta["delta_pct"] is None
    assert delta["improved"] is None


# --- decide_efficiency_stop branches ----------------------------------------


def test_decide_efficiency_stop_not_candidate() -> None:
    decision = decide_efficiency_stop(
        efficiency_stop_candidate=False,
        voltage_drop_from_start_pct=20.0,
        min_voltage_drop_pct=10.0,
        no_gain_streak=5,
        required_extra_confirmations=1,
        pending_previous_curve=True,
        power_up_efficiency_down=False,
        efficiency_delta_pct=1.0,
    )

    assert decision.should_stop is False
    assert decision.confirmations == 0
    assert decision.use_current_curve is False
    assert decision.reason == "efficiency still improving"


def test_decide_efficiency_stop_below_minimum_voltage_drop() -> None:
    decision = decide_efficiency_stop(
        efficiency_stop_candidate=True,
        voltage_drop_from_start_pct=5.0,
        min_voltage_drop_pct=10.0,
        no_gain_streak=5,
        required_extra_confirmations=1,
        pending_previous_curve=True,
        power_up_efficiency_down=False,
        efficiency_delta_pct=1.0,
    )

    assert decision.should_stop is False
    assert decision.reason == "minimum voltage drop not reached"


def test_decide_efficiency_stop_first_probe_arms_stop() -> None:
    decision = decide_efficiency_stop(
        efficiency_stop_candidate=True,
        voltage_drop_from_start_pct=12.0,
        min_voltage_drop_pct=10.0,
        no_gain_streak=1,
        required_extra_confirmations=1,
        pending_previous_curve=False,
        power_up_efficiency_down=False,
        efficiency_delta_pct=1.0,
    )

    assert decision.should_stop is False
    assert decision.confirmations == 0
    assert decision.reason == "first no-gain probe arms stop"


def test_decide_efficiency_stop_confirming_no_gain() -> None:
    decision = decide_efficiency_stop(
        efficiency_stop_candidate=True,
        voltage_drop_from_start_pct=12.0,
        min_voltage_drop_pct=10.0,
        no_gain_streak=2,
        required_extra_confirmations=2,
        pending_previous_curve=True,
        power_up_efficiency_down=False,
        efficiency_delta_pct=1.0,
    )

    assert decision.should_stop is False
    assert decision.confirmations == 1
    assert decision.reason == "confirming no-gain"


def test_decide_efficiency_stop_uses_current_curve_when_efficiency_held() -> None:
    decision = decide_efficiency_stop(
        efficiency_stop_candidate=True,
        voltage_drop_from_start_pct=12.0,
        min_voltage_drop_pct=10.0,
        no_gain_streak=3,
        required_extra_confirmations=1,
        pending_previous_curve=True,
        power_up_efficiency_down=False,
        efficiency_delta_pct=0.0,
    )

    assert decision.should_stop is True
    assert decision.use_current_curve is True
    assert decision.confirmations == 2
    assert decision.reason == "fps-per-watt wall reached"


# --- power_increased_while_efficiency_flat ----------------------------------


def test_power_flat_false_when_power_missing() -> None:
    assert (
        power_increased_while_efficiency_flat(
            previous_power_w=None,
            candidate_power_w=200.0,
            previous_fps_per_w=0.5,
            candidate_fps_per_w=0.5,
        )
        is False
    )


def test_power_flat_false_when_previous_fps_per_w_zero() -> None:
    assert (
        power_increased_while_efficiency_flat(
            previous_power_w=190.0,
            candidate_power_w=200.0,
            previous_fps_per_w=0.0,
            candidate_fps_per_w=0.5,
        )
        is False
    )


def test_power_flat_true_when_power_up_and_efficiency_flat() -> None:
    # Power rose, fps/W gain (0%) is below the 1% min gain threshold.
    assert (
        power_increased_while_efficiency_flat(
            previous_power_w=190.0,
            candidate_power_w=200.0,
            previous_fps_per_w=0.5,
            candidate_fps_per_w=0.5,
        )
        is True
    )


def test_power_flat_false_when_efficiency_improved() -> None:
    # Power rose but fps/W jumped 10% -> not a flat-efficiency power increase.
    assert (
        power_increased_while_efficiency_flat(
            previous_power_w=190.0,
            candidate_power_w=200.0,
            previous_fps_per_w=0.5,
            candidate_fps_per_w=0.55,
        )
        is False
    )


# --- temperature_normalized_power_w -----------------------------------------


def test_temperature_normalized_power_w_returns_none_without_power() -> None:
    assert (
        temperature_normalized_power_w(
            {"avg_temperature_c": 60.0},
            reference_temperature_c=60.0,
        )
        is None
    )


def test_temperature_normalized_power_w_uncorrected_without_reference() -> None:
    assert (
        temperature_normalized_power_w(
            {"avg_power_w": 200.0, "avg_temperature_c": 60.0},
            reference_temperature_c=None,
        )
        == 200.0
    )


def test_temperature_normalized_power_w_returns_raw_when_correction_non_positive() -> None:
    # A very large negative temperature delta drives the correction factor to
    # <= 0, so the function falls back to the raw power reading.
    rules = EfficiencyPolicyRules(max_temperature_delta_c=1000.0, power_pct_per_c=1.0)
    result = temperature_normalized_power_w(
        {"avg_power_w": 200.0, "avg_temperature_c": -200.0},
        reference_temperature_c=0.0,
        rules=rules,
    )

    assert result == 200.0


# --- temperature_normalized_fps_per_w ---------------------------------------


def test_normalized_fps_per_w_none_when_power_zero() -> None:
    assert (
        temperature_normalized_fps_per_w({"avg_fps": 100.0}, normalized_power_w=0.0)
        is None
    )


def test_normalized_fps_per_w_uses_efficiency_fallback() -> None:
    # No avg_fps; reconstruct fps from efficiency_fps_per_w * avg_power_w.
    result = temperature_normalized_fps_per_w(
        {"efficiency_fps_per_w": 0.5, "avg_power_w": 200.0},
        normalized_power_w=190.0,
    )

    assert result == pytest.approx(0.5 * 200.0 / 190.0)


def test_normalized_fps_per_w_none_without_any_fps_source() -> None:
    assert (
        temperature_normalized_fps_per_w({"avg_power_w": 200.0}, normalized_power_w=190.0)
        is None
    )


# --- reference_temperature --------------------------------------------------


def test_reference_temperature_falls_back_to_candidate() -> None:
    result = reference_temperature(
        {"avg_power_w": 200.0},
        {"avg_temperature_c": 70.0},
    )

    assert result == 70.0


def test_reference_temperature_none_when_unavailable() -> None:
    assert reference_temperature({"avg_power_w": 200.0}, {"avg_power_w": 190.0}) is None


# --- voltage drop helpers ---------------------------------------------------


def test_measured_voltage_drop_none_when_probe_voltage_missing() -> None:
    assert measured_voltage_drop({"avg_power_w": 200.0}, {"avg_voltage_mv": 950.0}) is None


def test_requested_voltage_drop_none_when_probe_missing() -> None:
    assert requested_voltage_drop(None, {"candidate_voltage_mv": 950}) is None


def test_requested_voltage_drop_none_when_field_missing() -> None:
    assert requested_voltage_drop({"avg_power_w": 200.0}, {"candidate_voltage_mv": 950}) is None


def test_requested_voltage_drop_computes_difference() -> None:
    result = requested_voltage_drop(
        {"candidate_voltage_mv": 1000},
        {"candidate_voltage_mv": 950},
    )

    assert result == 50.0


# --- measured_voltage_close_to_requested ------------------------------------


def test_voltage_close_true_when_no_request() -> None:
    assert (
        measured_voltage_close_to_requested(
            measured_voltage_drop_mv=0.0,
            requested_voltage_drop_mv=None,
        )
        is True
    )


def test_voltage_close_true_when_measured_missing() -> None:
    assert (
        measured_voltage_close_to_requested(
            measured_voltage_drop_mv=None,
            requested_voltage_drop_mv=50.0,
        )
        is True
    )


def test_voltage_close_false_when_measured_far_below_requested() -> None:
    assert (
        measured_voltage_close_to_requested(
            measured_voltage_drop_mv=10.0,
            requested_voltage_drop_mv=50.0,
        )
        is False
    )


# --- measured_probe_voltage_mv ----------------------------------------------


def test_measured_probe_voltage_none_for_missing_probe() -> None:
    assert measured_probe_voltage_mv(None) is None


def test_measured_probe_voltage_uses_live_after_fallback() -> None:
    assert measured_probe_voltage_mv({"live_voltage_after_mv": 925.0}) == 925.0


def test_measured_probe_voltage_none_without_any_voltage_field() -> None:
    assert measured_probe_voltage_mv({"avg_power_w": 200.0}) is None
