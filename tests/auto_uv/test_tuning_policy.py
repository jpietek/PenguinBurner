from __future__ import annotations

import pytest
from auto_uv_test_data import rtx_3080_issue109_stock_curve_warm

from auto_uv.domain.types import AutoUvError
from auto_uv.run.tuning_policy import TuningPolicy
from auto_uv.scan_mode.tuning_mode import (
    AUTO_UV_TUNING_MODES,
    normalize_auto_uv_tuning_mode,
    tuning_mode_from_runtime_options,
)

FREEZE = {"candidate_voltage_mv": 937, "lock_clock_mhz": 1920, "reason": "previous-run-abruptly-ended"}


def test_mode_normalization_defaults_to_careful_and_rejects_unknown():
    assert AUTO_UV_TUNING_MODES == ("careful", "aggressive")
    assert normalize_auto_uv_tuning_mode(None) == "careful"
    assert normalize_auto_uv_tuning_mode(" Aggressive ") == "aggressive"
    assert tuning_mode_from_runtime_options({}) == "careful"
    assert tuning_mode_from_runtime_options({"auto_uv_tuning_mode": "aggressive"}) == "aggressive"
    with pytest.raises(AutoUvError, match="auto_uv_tuning_mode must be one of"):
        normalize_auto_uv_tuning_mode("yolo")


def test_careful_policy_keeps_every_guard_and_aggressive_drops_the_predictive_ones():
    curve = rtx_3080_issue109_stock_curve_warm()
    careful = TuningPolicy.build("careful", [FREEZE], curve)
    assert careful.edge_margin is not None and careful.floor_caution
    assert careful.performance_voltage_headroom_bins == 6
    assert not careful.retry_frozen_rungs
    assert "tuning mode: careful" in careful.describe()
    assert "one reboot per climb at most" in careful.describe()

    aggressive = TuningPolicy.build("aggressive", [FREEZE], curve)
    assert aggressive.edge_margin is None and not aggressive.floor_caution
    assert aggressive.performance_voltage_headroom_bins == 10
    assert aggressive.retry_frozen_rungs
    assert "no predicted-edge margin, no floor caution" in aggressive.describe()
    assert aggressive.with_entries([FREEZE]) is aggressive


def test_with_entries_rebinds_the_margin_to_the_live_list():
    curve = rtx_3080_issue109_stock_curve_warm()
    policy = TuningPolicy.build("careful", [], curve)
    assert policy.edge_margin is not None and policy.edge_margin.points() == []
    live: list[dict] = []
    bound = policy.with_entries(live)
    assert bound.edge_margin is not None
    live.append(FREEZE)  # a freeze recorded by an earlier tier of this scan
    assert len(bound.edge_margin.points()) == 1
    assert bound.edge_margin.margin_bins == policy.edge_margin.margin_bins
