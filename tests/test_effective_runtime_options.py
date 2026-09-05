from __future__ import annotations

from types import SimpleNamespace

from auto_uv.domain.user_options import AUTO_UV_DEFAULTS
from cli.effective_runtime_options import build_effective_auto_uv_runtime_options


def _args(**overrides):
    values = {
        "auto_uv_min_voltage_mv": None,
        "auto_uv_memory_offset_mhz": None,
        "auto_uv_power_limit_w": None,
        "auto_uv_tail_rise_bins": None,
        "auto_oc_target_voltage_mv": None,
        "auto_oc_target_clock_mhz": None,
        "auto_uv_max_clock_drop_pct": None,
        "auto_uv_mode": None,
        "auto_uv_require_final_choice": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_effective_runtime_options_default_to_empty_auto_uv_options() -> None:
    assert build_effective_auto_uv_runtime_options(_args()) == {}


def test_effective_runtime_options_apply_gui_scan_options_and_clamps() -> None:
    effective = build_effective_auto_uv_runtime_options(
        _args(
            auto_uv_min_voltage_mv=850,
            auto_uv_memory_offset_mhz=99999,
            auto_uv_power_limit_w=390,
            auto_uv_tail_rise_bins=999,
            auto_oc_target_voltage_mv=925,
            auto_oc_target_clock_mhz=2670,
            auto_uv_max_clock_drop_pct=-4.0,
            auto_uv_mode="performance",
            auto_uv_require_final_choice=True,
        )
    )

    assert effective["auto_uv_min_voltage_mv"] == 850
    # No static cap at CLI parse time: the Auto-UV apply path clamps against
    # the driver-reported limit and logs the clamp.
    assert effective["auto_uv_memory_offset_mhz"] == 99999
    assert effective["auto_uv_power_limit_w"] == 390
    assert effective["auto_uv_tail_rise_bins"] == AUTO_UV_DEFAULTS.max_tail_rise_bins
    assert effective["auto_oc_target_voltage_mv"] == 925
    assert effective["auto_oc_target_clock_mhz"] == 2670
    assert "auto_uv_max_clock_drop_pct" not in effective
    assert effective["auto_uv_mode"] == "performance"
    assert effective["auto_uv_require_final_choice"] is True


def test_effective_runtime_options_balanced_mode_uses_balanced_tail_default() -> None:
    effective = build_effective_auto_uv_runtime_options(_args(auto_uv_mode="balanced"))

    assert effective["auto_uv_requested_mode"] == "balanced"
    assert effective["auto_uv_mode"] == "balanced"
    assert effective["auto_uv_tail_rise_bins"] == AUTO_UV_DEFAULTS.balanced_tail_rise_bins


def test_effective_runtime_options_balanced_mode_keeps_explicit_tail_override() -> None:
    effective = build_effective_auto_uv_runtime_options(
        _args(auto_uv_mode="balanced", auto_uv_tail_rise_bins=4)
    )

    assert effective["auto_uv_mode"] == "balanced"
    assert effective["auto_uv_tail_rise_bins"] == 4


def test_effective_runtime_options_map_per_tier_full_scan_overrides() -> None:
    effective = build_effective_auto_uv_runtime_options(
        _args(
            auto_uv_mode="adaptive",
            auto_uv_efficiency_max_clock_drop_pct=15.0,
            auto_uv_efficiency_power_limit_w=250,
            auto_uv_efficiency_memory_offset_mhz=500,
            auto_uv_balanced_max_clock_drop_pct=6.0,
            auto_uv_balanced_power_limit_w=300,
            auto_uv_balanced_memory_offset_mhz=0,
            auto_uv_performance_max_clock_drop_pct=5.4,
            auto_uv_performance_power_limit_w=360,
            auto_uv_performance_memory_offset_mhz=1000,
        )
    )

    assert effective["auto_uv_mode"] == "adaptive"
    assert "auto_uv_efficiency_max_clock_drop_pct" not in effective
    assert effective["auto_uv_efficiency_power_limit_w"] == 250
    assert effective["auto_uv_efficiency_memory_offset_mhz"] == 500
    assert "auto_uv_balanced_max_clock_drop_pct" not in effective
    assert effective["auto_uv_balanced_power_limit_w"] == 300
    assert effective["auto_uv_balanced_memory_offset_mhz"] == 0
    assert "auto_uv_performance_max_clock_drop_pct" not in effective
    assert effective["auto_uv_performance_power_limit_w"] == 360
    assert effective["auto_uv_performance_memory_offset_mhz"] == 1000
