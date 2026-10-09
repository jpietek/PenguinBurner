from __future__ import annotations

from auto_uv_test_data import (
    rtx_3080_issue109_stock_curve_cold,
    rtx_3080_issue109_stock_curve_warm,
)

from auto_uv.run.edge_margin import EDGE_MARGIN_BINS, EdgeMargin, hard_crash_entry


def _floor(margin: EdgeMargin, lock_clock_mhz: int) -> int:
    found = margin.minimum_voltage_mv(lock_clock_mhz)
    assert found is not None
    return found[0]

WARM_FREEZE = {  # 10-07 23:55, Efficiency final verification froze the card.
    "candidate_voltage_mv": 800, "lock_clock_mhz": 1750,
    "reason": "previous-run-abruptly-ended", "phase": "final-verify",
}
WATCHDOG_FREEZE = {  # 10-08 00:17, the climb rung whose CUDA phase hung.
    "candidate_voltage_mv": 937, "lock_clock_mhz": 1920, "reason": "stability-probe-failed",
    "details": {"result_reason": "cuda-bruteforce-failed exit=4"},
}
SOFT_FAILURE = {
    "candidate_voltage_mv": 912, "lock_clock_mhz": 1875, "reason": "stability-probe-failed",
    "details": {"result_reason": "fps-regression"},
}


def test_only_freezes_count_as_edge_points():
    assert hard_crash_entry(WARM_FREEZE)
    assert hard_crash_entry(WATCHDOG_FREEZE)
    assert hard_crash_entry({"reason": "stability-probe-failed", "details": {"shutdown_mode": "nvidia-xid-detected"}})
    assert not hard_crash_entry(SOFT_FAILURE)
    margin = EdgeMargin([SOFT_FAILURE], rtx_3080_issue109_stock_curve_warm())
    assert margin.points() == [] and margin.describe() == ""
    assert margin.block_reason(candidate_voltage_mv=700, lock_clock_mhz=1875) == ""


def test_efficiency_freeze_blocks_the_climb_rung_that_froze_the_card():
    """10-08: Performance climbed 1890, 1905 (pass) and 1920 (bus drop) at 937 mV."""
    margin = EdgeMargin([WARM_FREEZE], rtx_3080_issue109_stock_curve_warm())
    floors = {lock: _floor(margin, lock) for lock in (1875, 1890, 1905, 1920)}
    assert floors[1890] <= 937 and floors[1905] <= 937  # the rungs that passed
    assert floors[1920] > 937  # the rung that froze
    assert margin.block_reason(candidate_voltage_mv=937, lock_clock_mhz=1920).startswith(
        "predicted edge: 937mV@1920MHz is under the"
    )
    assert margin.block_reason(candidate_voltage_mv=937, lock_clock_mhz=1905) == ""
    # Balanced's actual stop (918 at 1875) stays reachable.
    assert floors[1875] <= 918
    assert "1 freeze(s) on record (800mV@1750MHz)" in margin.describe()


def test_two_bins_would_not_have_caught_it_and_three_do():
    curve = rtx_3080_issue109_stock_curve_warm()
    two = EdgeMargin([WARM_FREEZE], curve, margin_bins=2)
    three = EdgeMargin([WARM_FREEZE], curve, margin_bins=3)
    assert two.block_reason(candidate_voltage_mv=937, lock_clock_mhz=1920) == ""
    assert three.block_reason(candidate_voltage_mv=937, lock_clock_mhz=1920)
    assert EDGE_MARGIN_BINS == 3


def test_first_archive_freeze_predicts_the_balanced_freeze_one_bin_above_it():
    """10-06: Efficiency froze at 818 / 1770; Balanced froze at 937 / 1920 and had passed 943."""
    freeze = {"candidate_voltage_mv": 818, "lock_clock_mhz": 1770, "reason": "previous-run-abruptly-ended"}
    margin = EdgeMargin([freeze], rtx_3080_issue109_stock_curve_cold())
    floor = _floor(margin, 1920)
    assert 937 < floor <= 956  # stops before the freeze, within two bins of the last pass


def test_margin_uses_the_highest_floor_across_freezes_and_reads_the_live_list():
    curve = rtx_3080_issue109_stock_curve_warm()
    entries: list[dict] = [WARM_FREEZE]
    margin = EdgeMargin(entries, curve)
    before = _floor(margin, 1965)
    entries.append(WATCHDOG_FREEZE)  # recorded later in the same scan
    found = margin.minimum_voltage_mv(1965)
    assert found is not None
    after, point = found
    assert after > before and point.lock_clock_mhz == 1920
    assert len(margin.points()) == 2


def test_at_the_freeze_clock_the_floor_is_one_bin_above_the_freeze():
    """Efficiency re-descending at 1740 after the 800 / 1750 freeze stops at 806,
    which is the point the tester's 184 s soak then passed."""
    margin = EdgeMargin([WARM_FREEZE], rtx_3080_issue109_stock_curve_warm())
    assert _floor(margin, 1750) == 806
    assert _floor(margin, 1740) == 806
    assert margin.block_reason(candidate_voltage_mv=806, lock_clock_mhz=1740) == ""
    assert margin.block_reason(candidate_voltage_mv=800, lock_clock_mhz=1740)


def test_margin_zero_is_the_prediction_itself_and_unknown_clocks_do_not_block():
    curve = rtx_3080_issue109_stock_curve_warm()
    margin = EdgeMargin([WARM_FREEZE], curve, margin_bins=0)
    assert _floor(margin, 1750) == 800
    assert margin.block_reason(candidate_voltage_mv=800, lock_clock_mhz=1750) == ""
    assert margin.minimum_voltage_mv(9999) is None
    assert margin.block_reason(candidate_voltage_mv=500, lock_clock_mhz=9999) == ""


def test_nearest_freeze_decides_the_floor_not_the_highest_prediction():
    """10-08 rerun: the 1920 freeze predicted 831 at 1740 and outvoted the
    1750 freeze's 806, the point the card had proven (3DMark, 20 loops)."""
    curve = rtx_3080_issue109_stock_curve_warm()
    margin = EdgeMargin([WARM_FREEZE, WATCHDOG_FREEZE], curve)
    assert _floor(margin, 1740) == 806
    assert margin.minimum_voltage_mv(1740)[1].lock_clock_mhz == 1750
    far_only = EdgeMargin([WATCHDOG_FREEZE], curve)
    assert _floor(far_only, 1740) == 831  # what the max rule used to pick
    # Balanced at 1875 sits 45 MHz from the 1920 freeze: two bins, not three.
    assert _floor(margin, 1875) == 912
    assert "1 to 3 bins above the edge the nearest one predicts" in margin.describe()


def test_margin_bins_scale_with_extrapolation_distance():
    margin = EdgeMargin([WARM_FREEZE], rtx_3080_issue109_stock_curve_warm())
    assert margin.margin_bins_for(0) == 1 and margin.margin_bins_for(15) == 1
    assert margin.margin_bins_for(30) == 2 and margin.margin_bins_for(90) == 2
    assert margin.margin_bins_for(105) == 3 and margin.margin_bins_for(-170) == 3
