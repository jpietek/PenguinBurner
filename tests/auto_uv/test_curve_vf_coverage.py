from __future__ import annotations

from pathlib import Path

import pytest
from auto_uv_test_data import base_curve

from auto_uv.curve.base_vf_curve_validation import validate_base_vf_curve
from auto_uv.curve.base_vf_curve_voltage_bins import (
    base_target_clock_at_voltage,
    higher_editable_voltage_bins,
    lock_voltage_for_target_clock,
    lower_editable_voltage_bins,
    nearest_editable_voltage_bin,
    next_higher_editable_voltage_bin,
)
from auto_uv.curve.measured_probe_lock_clock import probe_indicates_power_saturation
from auto_uv.domain.types import AutoUvError, AutoUvProbeSummary
from auto_uv.probes.summary import summarize_perf_cap_reason


def _all_preserved_curve() -> list[dict]:
    return [
        {
            "index": index,
            "voltage_mv": 800 + index * 25,
            "base_mhz": 2000 + index * 30,
            "target_mhz": 2000 + index * 30,
            "new_offset_mhz": 0,
            "preserve_base": True,
        }
        for index in range(3)
    ]


def _probe_summary(avg_core_clock_mhz: float | None) -> AutoUvProbeSummary:
    return AutoUvProbeSummary(
        candidate_voltage_mv=950,
        lock_clock_mhz=2550,
        live_voltage_before_mv=950,
        live_voltage_after_mv=950,
        avg_voltage_mv=950.0,
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=100.0,
        min_fps=100.0,
        max_fps=100.0,
        avg_power_w=200.0,
        max_power_w=210.0,
        avg_temperature_c=62.0,
        max_temperature_c=62.0,
        avg_fan_speed_pct=35.0,
        max_fan_speed_pct=35.0,
        avg_core_clock_mhz=avg_core_clock_mhz,
        efficiency_fps_per_w=0.5,
        efficiency_mhz_per_w=10.0,
        watts_per_mhz=0.1,
        used_companion_load=True,
        result_reason="stable run",
        log_path=Path("/tmp/q2rtx.log"),
    )


# --- base_vf_curve_validation.py ----------------------------------------------


def test_validate_rejects_too_few_editable_points() -> None:
    curve = base_curve(800, 850, 25, 2000, 30)  # only 2 points

    with pytest.raises(ValueError, match="at least 3 editable"):
        validate_base_vf_curve(curve)


def test_validate_rejects_duplicate_editable_voltage_bins() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)
    # Force a duplicate voltage bin while keeping >=3 points.
    curve.append(
        {
            "index": 99,
            "voltage_mv": curve[0]["voltage_mv"],
            "base_mhz": 2500,
            "target_mhz": 2500,
            "new_offset_mhz": 0,
        }
    )

    with pytest.raises(ValueError, match="duplicate editable voltage bins"):
        validate_base_vf_curve(curve)


def test_validate_rejects_invalid_editable_point() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)
    curve[1]["base_mhz"] = 0  # non-positive base clock

    with pytest.raises(ValueError) as excinfo:
        validate_base_vf_curve(curve)

    assert "invalid editable point" in str(excinfo.value)
    assert "base=0MHz" in str(excinfo.value)


def test_validate_accepts_healthy_curve() -> None:
    assert validate_base_vf_curve(base_curve()) is None


# --- base_vf_curve_voltage_bins.py --------------------------------------------


def test_nearest_editable_voltage_bin_raises_without_editable_bins() -> None:
    with pytest.raises(ValueError, match="did not contain editable voltage bins"):
        nearest_editable_voltage_bin(_all_preserved_curve(), 900)


def test_nearest_editable_voltage_bin_picks_closest() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)

    assert nearest_editable_voltage_bin(curve, 840) == 850


def test_lower_editable_voltage_bins_respects_min_search_floor() -> None:
    curve = base_curve(800, 1000, 25, 2000, 30)

    bins = lower_editable_voltage_bins(
        curve,
        start_voltage_mv=975,
        min_search_voltage_mv=850,
    )

    # 800 and 825 fall below the min-search floor and are dropped.
    assert bins == [950, 925, 900, 875, 850]


def test_lower_editable_voltage_bins_without_min_floor_returns_all_lower_bins() -> None:
    curve = base_curve(800, 1000, 25, 2000, 30)

    bins = lower_editable_voltage_bins(
        curve,
        start_voltage_mv=975,
        min_search_voltage_mv=None,
    )

    assert bins == [950, 925, 900, 875, 850, 825, 800]


def test_next_higher_editable_voltage_bin_returns_min_above() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)

    assert next_higher_editable_voltage_bin(curve, 825) == 850
    assert higher_editable_voltage_bins(curve, 825) == [850, 875]


def test_next_higher_editable_voltage_bin_none_at_top() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)

    assert next_higher_editable_voltage_bin(curve, 875) is None


def test_lock_voltage_for_target_clock_raises_when_unreachable() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)

    with pytest.raises(AutoUvError, match="never reaches 9999MHz"):
        lock_voltage_for_target_clock(curve, 9999)


def test_lock_voltage_for_target_clock_returns_first_match() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)

    # target_mhz at 850mV (index 2) is 2060, first to reach 2050.
    assert lock_voltage_for_target_clock(curve, 2050) == 850


def test_base_target_clock_at_voltage_matches_existing_bin() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)

    assert base_target_clock_at_voltage(curve, voltage_mv=825, fallback_mhz=0) == 2030


def test_base_target_clock_at_voltage_falls_back_when_missing() -> None:
    curve = base_curve(800, 900, 25, 2000, 30)

    assert (
        base_target_clock_at_voltage(curve, voltage_mv=12345, fallback_mhz=1500) == 1500
    )


# --- measured_probe_lock_clock.py ---------------------------------------------


def test_power_reason_can_trigger_clock_reclaim_without_proving_a_wall() -> None:
    probe = _probe_summary(avg_core_clock_mhz=2100.0)
    probe.perf_cap_reason = "sw-power"

    assert probe_indicates_power_saturation(probe, power_limit_w=575)
    assert not probe_indicates_power_saturation(
        probe, power_limit_w=575, require_power_evidence=True
    )


def test_measured_power_at_limit_proves_a_wall_without_perf_cap_reason() -> None:
    probe = _probe_summary(avg_core_clock_mhz=2100.0)
    probe.avg_power_w = 566.0

    assert probe_indicates_power_saturation(
        probe, power_limit_w=575, require_power_evidence=True
    )


def test_sparse_power_reason_summary_does_not_trigger_clock_reclaim() -> None:
    probe = _probe_summary(avg_core_clock_mhz=2100.0)
    probe.perf_cap_reason = summarize_perf_cap_reason(
        [
            {"perf_cap_reason": "sw-power"},
            *[{"perf_cap_reason": None} for _ in range(9)],
        ],
        require_sw_power_dominant=True,
    )

    assert probe.perf_cap_reason == "none"
    assert not probe_indicates_power_saturation(probe, power_limit_w=450)
