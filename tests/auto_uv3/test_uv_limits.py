from __future__ import annotations

import pytest

from auto_uv3.scan_mode.uv_limits import (
    uv_limit_profile_target_for_gpu,
    uv_limit_voltage_floor_target_for_gpu,
    voltage_drop_pct,
)


def test_5080_voltage_table_exposes_efficiency_floor_and_performance_ceiling() -> None:
    floor = uv_limit_voltage_floor_target_for_gpu("NVIDIA GeForce RTX 5080")
    ceiling = uv_limit_profile_target_for_gpu("NVIDIA GeForce RTX 5080", "performance")

    assert floor is not None
    assert ceiling is not None
    assert floor.gpu_family == "RTX 5080"
    assert floor.voltage_mv == 850
    assert floor.clock_mhz == 2800
    assert ceiling.voltage_mv == 925
    assert ceiling.clock_mhz == 2980
    assert voltage_drop_pct(start_voltage_mv=1000, floor_voltage_mv=850) == pytest.approx(
        15.0
    )


def test_unlisted_gpu_has_no_voltage_table_match() -> None:
    assert uv_limit_voltage_floor_target_for_gpu("NVIDIA GeForce RTX 3080") is None
    assert uv_limit_profile_target_for_gpu("NVIDIA GeForce RTX 3080", "performance") is None


def test_target_matching_keeps_ti_super_before_base_4070() -> None:
    target = uv_limit_profile_target_for_gpu(
        "NVIDIA GeForce RTX 4070 Ti SUPER",
        "performance",
    )

    assert target is not None
    assert target.gpu_family == "RTX 4070 Ti Super"
    assert target.clock_mhz == 2730
