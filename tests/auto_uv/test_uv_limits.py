from __future__ import annotations

import pytest

from auto_uv.scan_mode.uv_limits import (
    uv_limit_clock_target_range_for_gpu,
    uv_limit_power_limit_pct_for_gpu,
    uv_limit_profile_target_for_gpu,
    uv_limit_voltage_floor_target_for_gpu,
    voltage_drop_pct,
)


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("efficiency", (2380, 2800)),
        ("balanced", (2800, 2950)),
        ("performance", (2800, 3098)),
    ],
)
def test_5080_editable_clock_target_ranges(profile, expected) -> None:
    assert uv_limit_clock_target_range_for_gpu("RTX 5080", profile) == expected


def test_clock_target_ranges_contain_all_gpu_defaults() -> None:
    from auto_uv.scan_mode.uv_limits import _UV_LIMIT_TARGETS

    for entry in _UV_LIMIT_TARGETS:
        for tier in ("efficiency", "balanced", "performance"):
            bounds = uv_limit_clock_target_range_for_gpu(entry["family"], tier)
            target = uv_limit_profile_target_for_gpu(entry["family"], tier)
            assert bounds is not None and target is not None
            assert bounds[0] <= target.clock_mhz <= bounds[1], (entry["family"], tier)


def test_clock_target_range_requires_known_gpu_and_tier() -> None:
    assert uv_limit_clock_target_range_for_gpu("Unknown GPU", "efficiency") is None
    assert uv_limit_clock_target_range_for_gpu("RTX 5080", "adaptive") is None


def test_5080_voltage_table_exposes_efficiency_floor_and_performance_ceiling() -> None:
    floor = uv_limit_voltage_floor_target_for_gpu("NVIDIA GeForce RTX 5080")
    ceiling = uv_limit_profile_target_for_gpu("NVIDIA GeForce RTX 5080", "performance")

    assert floor is not None
    assert ceiling is not None
    assert floor.gpu_family == "RTX 5080"
    assert floor.voltage_mv == 850
    assert floor.clock_mhz == 2800
    assert ceiling.voltage_mv == 925
    assert ceiling.clock_mhz == 2950
    assert voltage_drop_pct(start_voltage_mv=1000, floor_voltage_mv=850) == pytest.approx(
        15.0
    )


def test_unlisted_gpu_has_no_voltage_table_match() -> None:
    assert uv_limit_voltage_floor_target_for_gpu("NVIDIA GeForce GTX 1080") is None
    assert uv_limit_profile_target_for_gpu("NVIDIA GeForce GTX 1080", "performance") is None


def test_rtx_5060_shares_the_5060_ti_vf_targets() -> None:
    # The lower-TBP GB206 cut reuses the 5060 Ti ladder and relies on the
    # efficiency power cap to stay inside its envelope.
    for profile in ("efficiency", "balanced", "performance"):
        base = uv_limit_profile_target_for_gpu("NVIDIA GeForce RTX 5060", profile)
        ti = uv_limit_profile_target_for_gpu("NVIDIA GeForce RTX 5060 Ti", profile)
        assert base is not None and ti is not None
        assert base.gpu_family == "RTX 5060"
        assert (base.voltage_mv, base.clock_mhz) == (ti.voltage_mv, ti.clock_mhz)
    # 80% stored, less the fixed 12% efficiency reduction: 80 * 0.88 = 70.4.
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 5060", profile_id="efficiency"
    ) == pytest.approx(70.4)


def test_power_limit_pct_caps_only_efficiency() -> None:
    # Blackwell: efficiency cap is the stored per-family value LESS the fixed
    # 12% efficiency reduction (88 * 0.88 = 77.44). Balanced and performance
    # keep the stock budget so the full scan's balanced descent stays
    # donatable to the performance tier (matching power regimes).
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 5080", profile_id="efficiency"
    ) == pytest.approx(77.44)
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 5080", profile_id="balanced"
    ) == pytest.approx(100.0)
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 5080", profile_id="performance"
    ) == pytest.approx(100.0)


def test_rtx_5090_power_limit_percentages_and_575w_tier_caps() -> None:
    from auto_uv.main_loop import adaptive_tier_power_limit_w

    gpu_name = "NVIDIA GeForce RTX 5090"
    percentages = {
        tier: uv_limit_power_limit_pct_for_gpu(gpu_name, profile_id=tier)
        for tier in ("efficiency", "balanced", "performance")
    }

    assert percentages == pytest.approx(
        {"efficiency": 74.8, "balanced": 100.0, "performance": 100.0}
    )
    assert {
        tier: adaptive_tier_power_limit_w(
            power_limit_pct=percentage,
            baseline_power_limit_w=575,
            scan_request_w=None,
            balanced_pct=percentages["balanced"],
        )
        for tier, percentage in percentages.items()
    } == {"efficiency": 430, "balanced": 575, "performance": 575}


def test_power_limit_pct_ampere_efficiency_takes_fixed_reduction() -> None:
    # 80% stored - 12% reduction = 70.4; balanced keeps the stock budget.
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 3080", profile_id="efficiency"
    ) == pytest.approx(70.4)
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 3080", profile_id="balanced"
    ) == pytest.approx(100.0)
    assert uv_limit_power_limit_pct_for_gpu(
        "NVIDIA GeForce RTX 3080", profile_id="performance"
    ) == pytest.approx(100.0)


def test_power_limit_pct_ada_stays_full_power_on_every_tier() -> None:
    # Ada is deliberately uncapped (stored efficiency == full power), so the
    # fixed reduction does not apply: you cannot lower a limit that is not
    # there. Every tier stays at full power.
    for profile in ("efficiency", "balanced", "performance"):
        assert uv_limit_power_limit_pct_for_gpu(
            "NVIDIA GeForce RTX 4090", profile_id=profile
        ) == pytest.approx(100.0)


def test_power_limit_pct_unlisted_gpu_returns_none() -> None:
    assert uv_limit_power_limit_pct_for_gpu("NVIDIA GeForce GTX 1080") is None


def test_target_matching_keeps_ti_super_before_base_4070() -> None:
    target = uv_limit_profile_target_for_gpu(
        "NVIDIA GeForce RTX 4070 Ti SUPER",
        "performance",
    )

    assert target is not None
    assert target.gpu_family == "RTX 4070 Ti Super"
    assert target.clock_mhz == 2705


def test_4070_ti_performance_target_matches_reference_table() -> None:
    target = uv_limit_profile_target_for_gpu(
        "NVIDIA GeForce RTX 4070 Ti",
        "performance",
    )

    assert target is not None
    assert target.gpu_family == "RTX 4070 Ti"
    assert target.voltage_mv == 950
    assert target.clock_mhz == 2660


def test_3080_uses_ampere_table_values() -> None:
    floor = uv_limit_voltage_floor_target_for_gpu("NVIDIA GeForce RTX 3080")
    ceiling = uv_limit_profile_target_for_gpu("NVIDIA GeForce RTX 3080", "performance")

    assert floor is not None
    assert ceiling is not None
    assert floor.gpu_family == "RTX 3080"
    assert floor.voltage_mv == 800
    assert floor.clock_mhz == 1750
    assert ceiling.voltage_mv == 900
    assert ceiling.clock_mhz == 1930


def test_3080_12gb_matches_before_base_3080() -> None:
    target = uv_limit_profile_target_for_gpu(
        "NVIDIA GeForce RTX 3080 12GB",
        "performance",
    )

    assert target is not None
    assert target.gpu_family == "RTX 3080 12GB"
    assert target.voltage_mv == 900
    assert target.clock_mhz == 1900
    assert (
        uv_limit_profile_target_for_gpu("NVIDIA GeForce RTX 3080 12GB", "max")
        is None
    )
