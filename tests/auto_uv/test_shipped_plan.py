"""Validate real curve geometries without rewriting selected points."""

from __future__ import annotations

from copy import deepcopy

import pytest

from auto_uv.curve.shipped_plan import assert_monotonic_editable_targets
from auto_uv.curve.vf_curve_flattening import build_flattened_plan
from auto_uv.domain.types import AutoUvError
from auto_uv_test_data import (
    rtx_5080_20260524_high_oc_base_curve,
    rtx_5090_steep_synthetic_curve,
)


@pytest.mark.parametrize("curve_factory,voltage,clock", [
    (rtx_5080_20260524_high_oc_base_curve, 925, 2950),
    (rtx_5090_steep_synthetic_curve, 1000, 2595),
])
def test_selected_ramp_and_tail_pass_validation_unchanged(
    curve_factory, voltage: int, clock: int
) -> None:
    plan = build_flattened_plan(
        curve_factory(), candidate_voltage_mv=voltage,
        lock_clock_mhz=clock, tail_rise_bins=4,
    )
    tested = deepcopy(plan)

    assert_monotonic_editable_targets(plan)

    assert plan == tested
    by_voltage = {point["voltage_mv"]: point for point in plan}
    assert by_voltage[voltage]["target_mhz"] == clock
    assert any(point["target_mhz"] > clock for point in plan)


def test_non_monotonic_selected_region_is_rejected() -> None:
    with pytest.raises(AutoUvError, match="refusing to ship non-monotonic"):
        assert_monotonic_editable_targets([
            {"voltage_mv": 950, "target_mhz": 2600},
            {"voltage_mv": 975, "target_mhz": 2580},
        ])
