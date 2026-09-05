"""The shipped-curve contract: only scan-validated points leave the scan."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from auto_uv.curve.shipped_plan import (
    assert_monotonic_editable_targets,
    restore_stock_below_validated_floor,
    validated_floor_voltage_mv,
)
from auto_uv.curve.vf_curve_flattening import build_flattened_plan
from auto_uv.domain.types import AutoUvError
from auto_uv_test_data import rtx_5080_20260524_high_oc_base_curve


def _point(voltage_mv, base_mhz, target_mhz, preserve_base=False):
    point = {
        "voltage_mv": voltage_mv,
        "base_mhz": base_mhz,
        "target_mhz": target_mhz,
        "new_offset_mhz": target_mhz - base_mhz,
    }
    if preserve_base:
        point["preserve_base"] = True
    return point


def test_below_floor_bins_ship_stock() -> None:
    plan = [
        _point(450, 180, 180, preserve_base=True),
        # Scan-machinery debris: the baseline flatten floor at unprobed bins.
        _point(825, 2047, 2415),
        _point(845, 2130, 2415),
        # Validated region: floor rung and lock.
        _point(850, 2160, 2639),
        _point(925, 2475, 2920),
        # Rising tail above the lock stays untouched.
        _point(940, 2500, 2950),
    ]

    shipped = restore_stock_below_validated_floor(
        plan,
        floor_voltage_mv=850,
        lock_clock_mhz=2639,
    )

    by_voltage = {point["voltage_mv"]: point for point in shipped}
    assert by_voltage[825]["target_mhz"] == 2047
    assert by_voltage[825]["new_offset_mhz"] == 0
    assert by_voltage[845]["target_mhz"] == 2130
    assert by_voltage[845]["new_offset_mhz"] == 0
    # At and above the floor the validated points are untouched.
    assert by_voltage[850]["target_mhz"] == 2639
    assert by_voltage[925]["target_mhz"] == 2920
    assert by_voltage[940]["target_mhz"] == 2950
    # preserve_base bins are never rewritten (they are already stock).
    assert by_voltage[450]["target_mhz"] == 180
    # The input plan is not mutated.
    assert plan[1]["target_mhz"] == 2415


def test_steep_stock_curve_is_clamped_below_lock_without_downward_edge() -> None:
    plan = [
        _point(925, 2347, 2580),
        _point(950, 2602, 2580),
        _point(975, 2677, 2580),
        # Restoring this 5090-like stock point raw created the field-report
        # triangle: 2695MHz at 995mV followed by 2595MHz at 1000mV.
        _point(995, 2695, 2580),
        _point(1000, 2767, 2595),
        _point(1025, 2827, 2610),
    ]

    shipped = restore_stock_below_validated_floor(
        plan,
        floor_voltage_mv=1000,
        lock_clock_mhz=2595,
    )

    targets = {int(point["voltage_mv"]): int(point["target_mhz"]) for point in shipped}
    assert targets[925] == 2347
    assert targets[950] == 2580
    assert targets[975] == 2580
    assert targets[995] == 2580
    assert targets[1000] == 2595
    assert all(
        left <= right
        for left, right in zip(targets.values(), list(targets.values())[1:])
    )
    # The clamp never raises an unvalidated point above stock.
    assert all(
        int(point["target_mhz"]) <= int(point["base_mhz"])
        for point in shipped
        if int(point["voltage_mv"]) < 1000
    )


def test_non_monotonic_validated_region_is_rejected() -> None:
    with pytest.raises(AutoUvError, match="refusing to ship non-monotonic"):
        assert_monotonic_editable_targets(
            [
                _point(950, 2500, 2600),
                _point(975, 2550, 2580),
            ]
        )


def test_5080_fixture_keeps_the_existing_stock_restoration_result() -> None:
    base_curve = rtx_5080_20260524_high_oc_base_curve()
    plan = build_flattened_plan(
        base_curve,
        lock_clock_mhz=2805,
        candidate_voltage_mv=1025,
        tail_rise_bins=4,
    )
    expected = []
    for point in plan:
        restored = dict(point)
        if int(point["voltage_mv"]) < 950:
            restored["target_mhz"] = int(point["base_mhz"])
            restored["new_offset_mhz"] = 0
        expected.append(restored)

    shipped = restore_stock_below_validated_floor(
        plan,
        floor_voltage_mv=950,
        lock_clock_mhz=2805,
    )

    assert shipped == expected


def test_validated_floor_is_deepest_passed_probe() -> None:
    history = [
        SimpleNamespace(candidate_voltage_mv=915),
        SimpleNamespace(candidate_voltage_mv=850),
        SimpleNamespace(candidate_voltage_mv=870),
    ]
    assert validated_floor_voltage_mv(history, fallback_voltage_mv=925) == 850
    # Without history the final candidate voltage is the only proven point.
    assert validated_floor_voltage_mv([], fallback_voltage_mv=925) == 925
    assert validated_floor_voltage_mv(None, fallback_voltage_mv=870) == 870
