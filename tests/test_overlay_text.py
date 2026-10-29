from __future__ import annotations

from overlay.config import OverlayConfig
from overlay.overlay_text import format_overlay_text
from overlay.overlay_text import preview_overlay_text


def test_overlay_text_formats_compact_line_with_explicit_fg_fps() -> None:
    assert (
        format_overlay_text(
            {
                "present_fps": "50",
                "framegen_fps": "100",
                "framegen_active": "1",
                "latency_ms": "34",
                "clock_mhz": "2500",
                "voltage_mv": "850",
                "power_w": "220",
                "profile_tier": "Balanced",
            }
        )
        == "50 FPS 100 FG LAT 34 ms 2500 MHz 850 mV 220 W BAL"
    )


def test_overlay_text_shows_latency_when_enabled() -> None:
    config = OverlayConfig(
        enabled=True,
        enabled_item_ids=(
            "base_fps",
            "fg_fps",
            "clock_mhz",
            "voltage_mv",
            "power_w",
            "profile",
            "latency_ms",
        ),
    )

    assert (
        format_overlay_text(
            {
                "present_fps": "50",
                "framegen_fps": "100",
                "framegen_active": "1",
                "latency_ms": "34",
                "clock_mhz": "2500",
                "voltage_mv": "850",
                "power_w": "220",
                "profile_tier": "Balanced",
            },
            config=config,
        )
        == "50 FPS 100 FG LAT 34 ms 2500 MHz 850 mV 220 W BAL"
    )


def test_overlay_text_adds_display_tail_into_single_latency_number() -> None:
    config = OverlayConfig(enabled=True, enabled_item_ids=("latency_ms",))

    assert (
        format_overlay_text(
            {"latency_ms": "30", "display_latency_ms": "4"},
            config=config,
        )
        == "LAT 34 ms"
    )


def test_overlay_text_latency_falls_back_to_render_without_display_tail() -> None:
    config = OverlayConfig(enabled=True, enabled_item_ids=("latency_ms",))

    assert (
        format_overlay_text({"latency_ms": "30"}, config=config) == "LAT 30 ms"
    )
    assert (
        format_overlay_text(
            {"latency_ms": "30", "display_latency_ms": ""},
            config=config,
        )
        == "LAT 30 ms"
    )


def test_overlay_text_never_shows_the_display_tail_alone() -> None:
    config = OverlayConfig(enabled=True, enabled_item_ids=("latency_ms",))

    # No marker latency (non-Reflex title): a bare present->scanout tail is
    # scanout timing, not a latency figure — the LAT slot stays empty rather
    # than showing garbage like "LAT 1 ms".
    assert (
        format_overlay_text(
            {"latency_ms": "", "display_latency_ms": "6"},
            config=config,
        )
        == "PB waiting"
    )


def test_overlay_text_omits_latency_placeholder_when_signal_is_incomplete() -> None:
    config = OverlayConfig(enabled=True, enabled_item_ids=("latency_ms",))

    assert format_overlay_text({"latency_ms": "--"}, config=config) == "PB waiting"


def test_overlay_text_normalizes_latency_into_fps_block() -> None:
    config = OverlayConfig(
        enabled=True,
        enabled_item_ids=(
            "base_fps",
            "clock_mhz",
            "voltage_mv",
            "power_w",
            "profile",
            "latency_ms",
        ),
    )

    assert (
        format_overlay_text(
            {
                "present_fps": "42",
                "latency_ms": "16",
                "clock_mhz": "2500",
                "voltage_mv": "850",
                "power_w": "220",
                "profile_tier": "Efficiency",
            },
            config=config,
        )
        == "42 FPS LAT 16 ms 2500 MHz 850 mV 220 W EFF"
    )


def test_overlay_text_does_not_guess_static_framegen_from_ratio() -> None:
    assert (
        format_overlay_text(
            {
                "present_fps": "50",
                "framegen_fps": "100",
                "latency_ms": "34",
                "clock_mhz": "2500",
                "voltage_mv": "850",
                "power_w": "220",
                "profile_tier": "Balanced",
            }
        )
        == "50 FPS LAT 34 ms 2500 MHz 850 mV 220 W BAL"
    )


def test_overlay_text_keeps_base_fps_when_output_is_not_framegen() -> None:
    assert (
        format_overlay_text(
            {
                "present_fps": "58",
                "framegen_fps": "60",
                "latency_ms": "18",
                "clock_mhz": "2500",
                "voltage_mv": "850",
                "power_w": "220",
                "profile_tier": "Balanced",
            }
        )
        == "58 FPS LAT 18 ms 2500 MHz 850 mV 220 W BAL"
    )


def test_overlay_text_keeps_base_fps_when_dynamic_fg_is_unmarked() -> None:
    assert (
        format_overlay_text(
            {
                "present_fps": "39",
                "framegen_fps": "177",
                "latency_ms": "21",
                "clock_mhz": "2550",
                "voltage_mv": "850",
                "power_w": "194",
                "profile_tier": "Efficiency",
            }
        )
        == "39 FPS LAT 21 ms 2550 MHz 850 mV 194 W EFF"
    )


def test_overlay_text_shows_dynamic_fg_when_marked_active() -> None:
    assert (
        format_overlay_text(
            {
                "present_fps": "51",
                "framegen_fps": "120",
                "framegen_active": "1",
                "latency_ms": "32",
                "clock_mhz": "2500",
                "voltage_mv": "850",
                "power_w": "220",
                "profile_tier": "Balanced",
            }
        )
        == "51 FPS 120 FG LAT 32 ms 2500 MHz 850 mV 220 W BAL"
    )


def test_overlay_text_falls_back_to_base_when_output_missing() -> None:
    assert (
        format_overlay_text(
            {
                "present_fps": "58",
                "framegen_fps": "n/a",
                "latency_ms": "18",
                "clock_mhz": "2500",
                "voltage_mv": "850",
                "power_w": "220",
                "profile_tier": "Balanced",
            }
        )
        == "58 FPS LAT 18 ms 2500 MHz 850 mV 220 W BAL"
    )


def test_overlay_text_does_not_substitute_output_fps_when_base_is_missing() -> None:
    assert (
        format_overlay_text(
            {
                "framegen_fps": "120",
                "latency_ms": "18",
                "clock_mhz": "2500",
                "voltage_mv": "850",
                "power_w": "220",
                "profile_tier": "Balanced",
            }
        )
        == "n/a FPS LAT 18 ms 2500 MHz 850 mV 220 W BAL"
    )


def test_overlay_text_omits_missing_latency() -> None:
    config = OverlayConfig(enabled=True, enabled_item_ids=("base_fps", "latency_ms"))

    assert (
        format_overlay_text(
            {
                "present_fps": "72",
                "latency_ms": "",
                "clock_mhz": "2500",
                "voltage_mv": "850",
                "power_w": "220",
                "profile_tier": "Efficiency",
            },
            config=config,
        )
        == "72 FPS"
    )


def test_overlay_text_handles_missing_values() -> None:
    # An absent tier is stock/default GPU state and must read DEF — showing
    # BAL would claim a tuned profile that is not applied.
    assert format_overlay_text({}) == "n/a FPS n/a MHz n/a mV n/a W DEF"


def test_overlay_text_hides_missing_advanced_values() -> None:
    config = OverlayConfig(
        enabled=True,
        enabled_item_ids=(
            "base_fps",
            "gpu_util_pct",
            "cpu_util_pct",
            "cpu_peak_thread_pct",
            "fan_pct",
            "temperature_c",
            "uv_offset_mv",
        ),
    )

    assert (
        format_overlay_text({"present_fps": "72"}, config=config)
        == "72 FPS CPU --% CPU-T --%"
    )


def test_overlay_text_formats_advanced_values_when_available() -> None:
    config = OverlayConfig(
        enabled=True,
        enabled_item_ids=(
            "base_fps",
            "gpu_util_pct",
            "cpu_util_pct",
            "cpu_peak_thread_pct",
            "fan_pct",
            "temperature_c",
            "uv_offset_mv",
        ),
    )

    assert (
        format_overlay_text(
            {
                "present_fps": "72",
                "gpu_util_pct": "97",
                "cpu_util_pct": "32",
                "cpu_peak_thread_pct": "98",
                "fan_pct": "62",
                "temperature_c": "67",
                "uv_offset_mv": "-75",
            },
            config=config,
        )
        == "72 FPS GPU 97% CPU 32% CPU-T 98% Fan 62% T 67 C UV -75 mV"
    )


def test_overlay_preview_uses_sample_values_for_missing_items() -> None:
    config = OverlayConfig(
        enabled=True,
        enabled_item_ids=(
            "base_fps",
            "gpu_util_pct",
            "cpu_util_pct",
            "cpu_peak_thread_pct",
            "fan_pct",
            "temperature_c",
            "uv_offset_mv",
        ),
    )

    assert preview_overlay_text({"present_fps": "60"}, config=config) == (
        "60 FPS GPU 97% CPU 32% CPU-T 98% Fan 62% T 67 C UV -75 mV"
    )


def test_overlay_preview_keeps_sample_fg_when_live_state_is_inactive() -> None:
    config = OverlayConfig(
        enabled=True,
        enabled_item_ids=("base_fps", "fg_fps", "clock_mhz"),
    )

    assert (
        preview_overlay_text(
            {
                "present_fps": "60",
                "framegen_fps": "",
                "framegen_active": "0",
                "clock_mhz": "2700",
            },
            config=config,
        )
        == "60 FPS 120 FG 2700 MHz"
    )
