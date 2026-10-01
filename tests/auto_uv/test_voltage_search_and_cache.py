from __future__ import annotations

from auto_uv_test_data import base_curve

from auto_uv.base_uv_loop import build_next_lower_voltage_candidate
from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.persistence.unsafe_voltage_cache import (
    cache_profile_tier,
    controlled_failure_reason,
    unsafe_entry_blocks_future_search,
    unsafe_entry_blocks_voltage_candidate,
    unsafe_entry_clock_floor_mhz,
    unsafe_entry_profile_tier,
    unsafe_entry_reason_values,
    unsafe_min_search_voltage,
    unsafe_voltage_block_reason,
)
from auto_uv.run.lower_voltage_search import (
    filter_effective_voltage_candidates,
    select_next_lower_voltage,
)
from auto_uv.run.voltage_sweep_state import VoltageSweepState


def test_lower_voltage_search_keeps_final_low_bin_testable() -> None:
    selected = select_next_lower_voltage(
        base_curve(900, 1025, 25),
        start_voltage_mv=1000,
        stable_voltage_mv=1000,
        reference_actual_voltage_mv=1000.0,
        min_search_voltage_mv=900,
    )
    filtered = filter_effective_voltage_candidates(
        [925, 900],
        stable_voltage_mv=1000,
        reference_actual_voltage_mv=1000.0,
    )

    assert selected == 925
    assert filtered[-1] == 900


def test_lower_voltage_probe_holds_target_without_a_measurement() -> None:
    curve = base_curve(800, 1025, 25, 2000, 30)
    candidate = build_next_lower_voltage_candidate(
        curve,
        settings=AutoUvScanSettings(start_voltage_mv=1000, min_search_voltage_mv=900),
        state=VoltageSweepState(
            stable_voltage_mv=1000,
            stable_target_mhz=2240,
            next_voltage_mv=900,
        ),
    )

    assert candidate.voltage_mv == 900
    assert candidate.target_mhz == 2240


def test_unsafe_cache_blocks_only_the_recorded_clock_band_when_clock_aware() -> None:
    entry = {"candidate_voltage_mv": 900, "lock_clock_mhz": 2200}

    assert unsafe_entry_blocks_voltage_candidate(
        entry,
        candidate_voltage_mv=875,
        lock_clock_mhz=2200,
    )
    assert not unsafe_entry_blocks_voltage_candidate(
        entry,
        candidate_voltage_mv=875,
        lock_clock_mhz=2050,
    )


def test_unsafe_cache_scopes_tiered_entries_to_matching_profile() -> None:
    entry = {
        "candidate_voltage_mv": 970,
        "lock_clock_mhz": 2734,
        "blocked_lock_clock_mhz": [2734, 2707, 2692],
        "reason": "stability-probe-failed",
        "details": {"generated_profile_tier": "performance"},
    }

    assert unsafe_entry_blocks_voltage_candidate(
        entry,
        candidate_voltage_mv=920,
        lock_clock_mhz=2712,
        profile_tier="performance",
    )
    assert not unsafe_entry_blocks_voltage_candidate(
        entry,
        candidate_voltage_mv=920,
        lock_clock_mhz=2712,
        profile_tier="balanced",
    )


def test_unsafe_cache_keeps_legacy_entries_global_when_profile_is_missing() -> None:
    entry = {"candidate_voltage_mv": 970, "lock_clock_mhz": 2734, "reason": "crash"}

    assert unsafe_entry_blocks_voltage_candidate(
        entry,
        candidate_voltage_mv=920,
        lock_clock_mhz=2734,
        profile_tier="balanced",
    )


def test_unsafe_cache_raises_min_search_above_legacy_unsafe_floor() -> None:
    unsafe_floor_mv, min_search_mv = unsafe_min_search_voltage(
        base_curve(850, 1025, 25),
        start_voltage_mv=1000,
        unsafe_entries=[{"candidate_voltage_mv": 925, "reason": "benchmark-crash"}],
    )

    assert unsafe_floor_mv == 925
    assert min_search_mv == 950


def test_unsafe_min_search_voltage_skips_controlled_clock_floor_and_locked_entries() -> None:
    # A controlled low-clock exit (telemetry clock-floor) must not raise the floor,
    # nor should clock-aware entries (lock_clock_mhz set) or blocked-band lists.
    unsafe_floor_mv, min_search_mv = unsafe_min_search_voltage(
        base_curve(850, 1025, 25),
        start_voltage_mv=1000,
        unsafe_entries=[
            # line 33: controlled failure -> not a future-search blocker
            {"candidate_voltage_mv": 925, "reason": "telemetry-live-core_clock-too-low"},
            # line 37: clock-aware entry (lock_clock_mhz set) is band-specific, skip here
            {"candidate_voltage_mv": 950, "reason": "crash", "lock_clock_mhz": 2200},
            # line 37: entry with no candidate voltage
            {"reason": "crash"},
            # line 39: explicit blocked-band list is handled per-band, not as a floor
            {"candidate_voltage_mv": 960, "reason": "crash", "blocked_lock_clock_mhz": [2100]},
            # line 41: candidate at/above the start voltage cannot lower the floor
            {"candidate_voltage_mv": 1000, "reason": "crash"},
        ],
    )

    assert unsafe_floor_mv is None
    assert min_search_mv is None


def test_unsafe_min_search_voltage_keeps_highest_legacy_floor() -> None:
    unsafe_floor_mv, _ = unsafe_min_search_voltage(
        base_curve(850, 1025, 25),
        start_voltage_mv=1000,
        unsafe_entries=[
            {"candidate_voltage_mv": 875, "reason": "crash"},
            {"candidate_voltage_mv": 925, "reason": "crash"},
            {"candidate_voltage_mv": 900, "reason": "crash"},
        ],
    )

    assert unsafe_floor_mv == 925


def test_unsafe_entry_blocks_voltage_candidate_ignores_controlled_and_voltageless() -> None:
    # line 57: controlled exit never blocks a future candidate
    assert not unsafe_entry_blocks_voltage_candidate(
        {"candidate_voltage_mv": 900, "reason": "user-stop-requested"},
        candidate_voltage_mv=875,
        lock_clock_mhz=2200,
    )
    # line 60: entry without a recorded voltage cannot block anything
    assert not unsafe_entry_blocks_voltage_candidate(
        {"reason": "crash"},
        candidate_voltage_mv=875,
        lock_clock_mhz=2200,
    )


def test_unsafe_legacy_entry_without_clock_blocks_voltage_and_everything_below() -> None:
    entry = {"candidate_voltage_mv": 900, "reason": "crash"}

    # line 69: legacy entry blocks the recorded voltage and anything lower, any clock
    assert unsafe_entry_blocks_voltage_candidate(
        entry, candidate_voltage_mv=900, lock_clock_mhz=2050
    )
    assert unsafe_entry_blocks_voltage_candidate(
        entry, candidate_voltage_mv=850, lock_clock_mhz=9999
    )
    assert not unsafe_entry_blocks_voltage_candidate(
        entry, candidate_voltage_mv=925, lock_clock_mhz=2050
    )


def test_unsafe_voltage_block_reason_clock_aware_includes_band_text() -> None:
    reason = unsafe_voltage_block_reason(
        [{"candidate_voltage_mv": 900, "lock_clock_mhz": 2200, "reason": "crash"}],
        candidate_voltage_mv=875,
        lock_clock_mhz=2200,
    )

    assert reason == "cached unsafe point 900mV@2200MHz band>=2200MHz"


def test_unsafe_voltage_block_reason_legacy_entry_reports_unknown_clock() -> None:
    reason = unsafe_voltage_block_reason(
        [{"candidate_voltage_mv": 900, "reason": "crash"}],
        candidate_voltage_mv=875,
        lock_clock_mhz=2050,
    )

    assert reason == "cached unsafe point 900mV@unknown"


def test_unsafe_voltage_block_reason_uses_blocked_band_floor() -> None:
    reason = unsafe_voltage_block_reason(
        [
            {
                "candidate_voltage_mv": 900,
                "lock_clock_mhz": 2300,
                "blocked_lock_clock_mhz": [2400, 2100, 2250],
                "reason": "crash",
            }
        ],
        candidate_voltage_mv=875,
        lock_clock_mhz=2200,
    )

    # clock floor comes from the minimum of the blocked-band list (2100)
    assert reason == "cached unsafe point 900mV@2300MHz band>=2100MHz"


def test_unsafe_voltage_block_reason_ignores_other_profile_tier() -> None:
    reason = unsafe_voltage_block_reason(
        [
            {
                "candidate_voltage_mv": 970,
                "lock_clock_mhz": 2734,
                "blocked_lock_clock_mhz": [2734, 2707, 2692],
                "reason": "stability-probe-failed",
                "details": {"generated_profile_tier": "performance"},
            }
        ],
        candidate_voltage_mv=920,
        lock_clock_mhz=2712,
        profile_tier="balanced",
    )

    assert reason == ""


def test_unsafe_voltage_block_reason_returns_empty_when_nothing_blocks() -> None:
    assert (
        unsafe_voltage_block_reason(
            [{"candidate_voltage_mv": 900, "reason": "crash"}],
            candidate_voltage_mv=925,
            lock_clock_mhz=2200,
        )
        == ""
    )


def test_unsafe_entry_clock_floor_mhz_prefers_blocked_band_minimum() -> None:
    floor = unsafe_entry_clock_floor_mhz(
        {"blocked_lock_clock_mhz": [2400, "bad", 0, 2100, 2250]},
        fallback_lock_clock_mhz=2000,
    )

    # only positive, parseable values count; minimum wins over the fallback
    assert floor == 2100


def test_unsafe_entry_clock_floor_mhz_falls_back_when_no_valid_band() -> None:
    assert (
        unsafe_entry_clock_floor_mhz(
            {"blocked_lock_clock_mhz": ["bad", 0, -5]},
            fallback_lock_clock_mhz=2200,
        )
        == 2200
    )
    assert unsafe_entry_clock_floor_mhz({}, fallback_lock_clock_mhz=-10) == 0


def test_unsafe_entry_blocks_future_search_rejects_non_dict() -> None:
    assert not unsafe_entry_blocks_future_search(None)
    assert not unsafe_entry_blocks_future_search("not-a-dict")


def test_unsafe_entry_reason_values_collects_detail_fields() -> None:
    values = unsafe_entry_reason_values(
        {
            "reason": "crash",
            "details": {
                "result_reason": "telemetry-live-core_clock-avg-low",
                "shutdown_mode": "user-stop-requested",
                "ignored": "skip-me",
            },
        }
    )

    assert values == [
        "crash",
        "telemetry-live-core_clock-avg-low",
        "user-stop-requested",
    ]


def test_unsafe_entry_reason_values_drops_empty_and_ignores_non_dict_details() -> None:
    assert unsafe_entry_reason_values({"reason": "", "details": "not-a-dict"}) == []


def test_unsafe_entry_blocks_future_search_treats_controlled_detail_as_safe() -> None:
    # A controlled clock-floor exit recorded in details must not block future search.
    assert not unsafe_entry_blocks_future_search(
        {
            "reason": "crash",
            "details": {"result_reason": "core_clock-regression-detected"},
        }
    )
    assert not unsafe_entry_blocks_future_search(
        {
            "reason": "stability-probe-failed",
            "details": {"result_reason": "q2rtx-launcher-error"},
        }
    )


def test_unsafe_entry_profile_tier_reads_nested_details() -> None:
    assert (
        unsafe_entry_profile_tier(
            {
                "reason": "previous-run-abruptly-ended",
                "details": {
                    "marker_details": {"generated_profile_tier": "performance"}
                },
            }
        )
        == "performance"
    )


def test_unsafe_entry_profile_tier_reads_zero_tail_as_efficiency() -> None:
    assert unsafe_entry_profile_tier({"details": {"tail_rise_bins": 0}}) == "efficiency"


def test_unsafe_entry_from_adaptive_scan_stays_tier_agnostic() -> None:
    # Adaptive scans sweep every tier, so their flat-tail crash markers must
    # not masquerade as efficiency history: they block all tiers instead.
    entry = {
        "candidate_voltage_mv": 875,
        "lock_clock_mhz": 2900,
        "reason": "previous-run-abruptly-ended",
        "details": {
            "marker_details": {
                "auto_uv_mode": "adaptive",
                "generated_profile_tier": "",
                "tail_rise_bins": 0,
            }
        },
    }

    assert unsafe_entry_profile_tier(entry) == ""
    for tier in ("efficiency", "balanced", "performance"):
        assert unsafe_entry_blocks_voltage_candidate(
            entry,
            candidate_voltage_mv=875,
            lock_clock_mhz=2900,
            profile_tier=tier,
        )


def test_cache_profile_tier_normalizes_efficiency_tail_tune() -> None:
    assert cache_profile_tier("efficiency-tail-tune") == "efficiency"


def test_controlled_failure_reason_matches_known_prefixes_only() -> None:
    assert controlled_failure_reason("telemetry-live-core_clock-too-low")
    assert controlled_failure_reason("core_clock-regression-x")
    assert controlled_failure_reason("user-stop-requested")
    assert controlled_failure_reason("cuda-bruteforce-failed exit=-15")
    assert controlled_failure_reason("q2rtx-launcher-error")
    assert not controlled_failure_reason("benchmark-crash")
    assert not controlled_failure_reason(None)
