from __future__ import annotations

from overlay.config import OVERLAY_ITEM_IDS
from overlay.config import OVERLAY_NON_TEXT_ITEM_IDS
from overlay.config import OverlayConfig
from overlay.config import default_overlay_config
from overlay.config import set_overlay_layout
from overlay.overlay_text import SAMPLE_OVERLAY_VALUES
from overlay.overlay_text import format_overlay_item
from overlay.overlay_text import format_overlay_text
from overlay.overlay_text import format_overlay_value
from overlay.overlay_text import preview_overlay_text


# Everything below describes the table layout -- the plain grid every field
# shares. The designed HUD layout has its own tests in
# tests/test_overlay_hud_layout.py.
def _table(config: OverlayConfig | None = None) -> OverlayConfig:
    return set_overlay_layout(
        config or default_overlay_config(enabled=True), "table"
    )


def test_hotspot_is_independent_from_core_temperature_and_optional() -> None:
    config = OverlayConfig(
        enabled=True, layout="table",
        enabled_item_ids=("temperature_c", "hotspot_temperature_c"),
    )
    values = {"temperature_c": "67", "hotspot_temperature_c": "79"}
    assert format_overlay_text(values, config=config) == "T 67 C HOT 79 C"
    for missing in ("", "n/a"):
        values["hotspot_temperature_c"] = missing
        assert format_overlay_text(values, config=config) == "T 67 C"
    values.pop("hotspot_temperature_c")
    assert format_overlay_text(values, config=config) == "T 67 C"
    assert "HOT" not in format_overlay_text({"hotspot_temperature_c": "79"})


def test_hotspot_preview_does_not_replace_unavailable_live_data_with_demo() -> None:
    config = OverlayConfig(
        enabled=True, layout="table",
        enabled_item_ids=("temperature_c", "hotspot_temperature_c"),
    )
    assert preview_overlay_text({}, config=config) == "T 67 C HOT 79 C"
    assert preview_overlay_text({"temperature_c": "50"}, config=config) == "T 50 C"
    assert preview_overlay_text(
        {"temperature_c": "50", "hotspot_temperature_c": ""}, config=config
    ) == "T 50 C"
    assert preview_overlay_text(
        {"temperature_c": "50", "hotspot_temperature_c": "65"}, config=config
    ) == "T 50 C HOT 65 C"


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
            },
            config=_table(),
        )
        == "\n".join(
            [
                "50 FPS   100 FG LAT 34 ms BAL",
                "2500 MHz 850 mV",
                "220 W",
            ]
        )
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
            config=_table(config),
        )
        == "\n".join(
            [
                "50 FPS   100 FG LAT 34 ms BAL",
                "2500 MHz 850 mV",
                "220 W",
            ]
        )
    )


def test_overlay_text_adds_display_tail_into_single_latency_number() -> None:
    config = OverlayConfig(enabled=True, enabled_item_ids=("latency_ms",))

    assert (
        format_overlay_text(
            {"latency_ms": "30", "display_latency_ms": "4"},
            config=_table(config),
        )
        == "LAT 34 ms"
    )


def test_overlay_text_latency_falls_back_to_render_without_display_tail() -> None:
    config = OverlayConfig(enabled=True, enabled_item_ids=("latency_ms",))

    assert (
        format_overlay_text({"latency_ms": "30"}, config=_table(config)) == "LAT 30 ms"
    )
    assert (
        format_overlay_text(
            {"latency_ms": "30", "display_latency_ms": ""},
            config=_table(config),
        )
        == "LAT 30 ms"
    )


def test_overlay_text_omits_latency_placeholder_when_signal_is_incomplete() -> None:
    config = OverlayConfig(enabled=True, enabled_item_ids=("latency_ms",))

    assert format_overlay_text({"latency_ms": "--"}, config=_table(config)) == "PB waiting"


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
            config=_table(config),
        )
        == "\n".join(
            [
                "42 FPS   LAT 16 ms EFF",
                "2500 MHz 850 mV",
                "220 W",
            ]
        )
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
            },
            config=_table(),
        )
        == "\n".join(
            [
                "50 FPS   LAT 34 ms BAL",
                "2500 MHz 850 mV",
                "220 W",
            ]
        )
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
            },
            config=_table(),
        )
        == "\n".join(
            [
                "58 FPS   LAT 18 ms BAL",
                "2500 MHz 850 mV",
                "220 W",
            ]
        )
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
            },
            config=_table(),
        )
        == "\n".join(
            [
                "39 FPS   LAT 21 ms EFF",
                "2550 MHz 850 mV",
                "194 W",
            ]
        )
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
            },
            config=_table(),
        )
        == "\n".join(
            [
                "51 FPS   120 FG LAT 32 ms BAL",
                "2500 MHz 850 mV",
                "220 W",
            ]
        )
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
            },
            config=_table(),
        )
        == "\n".join(
            [
                "58 FPS   LAT 18 ms BAL",
                "2500 MHz 850 mV",
                "220 W",
            ]
        )
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
            },
            config=_table(),
        )
        == "\n".join(
            [
                "n/a FPS  LAT 18 ms BAL",
                "2500 MHz 850 mV",
                "220 W",
            ]
        )
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
            config=_table(config),
        )
        == "72 FPS"
    )


def test_overlay_text_handles_missing_values() -> None:
    # An absent tier is stock/default GPU state and must read DEF — showing
    # BAL would claim a tuned profile that is not applied.
    assert format_overlay_text({}, config=_table()) == "\n".join(
        [
            "n/a FPS DEF",
            "n/a MHz n/a mV",
            "n/a W",
        ]
    )


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
        format_overlay_text({"present_fps": "72"}, config=_table(config))
        == "72 FPS\nCPU --% CPU-T --%"
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
            config=_table(config),
        )
        == "\n".join(
            [
                "72 FPS",
                "UV -75 mV",
                "T 67 C    Fan 62%",
                "GPU 97%   CPU 32% CPU-T 98%",
            ]
        )
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

    assert preview_overlay_text({"present_fps": "60"}, config=_table(config)) == "\n".join(
        [
            "60 FPS",
            "UV -75 mV",
            "T 67 C    Fan 62%",
            "GPU 97%   CPU 32% CPU-T 98%",
        ]
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
            config=_table(config),
        )
        == "60 FPS   120 FG\n2700 MHz"
    )


def test_short_gpu_name_drops_only_the_vendor_words() -> None:
    from overlay.overlay_text import short_gpu_name

    # The footer sets the panel width, and these two words are the same on
    # every card the overlay runs on.
    assert short_gpu_name("NVIDIA GeForce RTX 5070 Ti") == "RTX 5070 Ti"
    assert short_gpu_name("GeForce GTX 1080 Ti") == "GTX 1080 Ti"
    assert short_gpu_name("NVIDIA RTX A4000") == "RTX A4000"
    # Anything that is not a vendor prefix stays, including a model that
    # merely starts with a similar word.
    assert short_gpu_name("Radeon RX 7900 XTX") == "Radeon RX 7900 XTX"
    assert short_gpu_name("  NVIDIA GeForce RTX 4080  ") == "RTX 4080"
    assert short_gpu_name(None) == ""


def test_memory_pairs_show_a_moving_figure_against_a_fixed_one() -> None:
    from overlay.overlay_text import format_overlay_value

    values = {
        "vram_used_mb": "9626",
        "vram_total_mb": "16376",
        "ram_used_mb": "14540",
        "ram_total_mb": "32768",
    }

    # The used figure carries a decimal because it is what moves; the total
    # does not, because a card does not grow one mid-game.
    assert format_overlay_value("vram", values) == "9.4 / 16 GiB"
    assert format_overlay_value("ram", values) == "14.2 / 32 GiB"


def test_a_memory_pair_without_both_halves_shows_nothing() -> None:
    from overlay.overlay_text import format_overlay_value

    # A card that reports no memory info omits the row rather than showing a
    # zero that would read as a real measurement.
    assert format_overlay_value("vram", {}) == ""
    assert format_overlay_value("vram", {"vram_used_mb": "9626"}) == ""
    assert format_overlay_value("vram", {"vram_total_mb": "16376"}) == ""
    assert (
        format_overlay_value("vram", {"vram_used_mb": "0", "vram_total_mb": "0"}) == ""
    )


def test_the_host_metrics_carry_their_units() -> None:
    from overlay.overlay_text import format_overlay_value

    values = {"mem_clock_mhz": "10501", "cpu_temp_c": "61", "cpu_power_w": "88"}

    assert format_overlay_value("mem_clock_mhz", values) == "10501 MHz"
    assert format_overlay_value("cpu_temp_c", values) == "61 C"
    assert format_overlay_value("cpu_power_w", values) == "88 W"
    # A box whose sensors we cannot read omits the row.
    assert format_overlay_value("cpu_temp_c", {}) == ""
    assert format_overlay_value("cpu_power_w", {"cpu_power_w": ""}) == ""


def test_a_cpu_colours_later_than_a_card() -> None:
    from overlay.overlay_text import (
        CELL_ALERT,
        CELL_GOOD,
        CELL_WARN,
        overlay_value_level,
    )

    # 88 C is merely warm on a CPU and would already be alert on the GPU.
    assert overlay_value_level("cpu_temp_c", {"cpu_temp_c": "88"}) == CELL_WARN
    assert overlay_value_level("temperature_c", {"temperature_c": "88"}) == CELL_ALERT
    assert overlay_value_level("cpu_temp_c", {"cpu_temp_c": "61"}) == CELL_GOOD
    assert overlay_value_level("cpu_temp_c", {"cpu_temp_c": "96"}) == CELL_ALERT


def test_the_sample_values_fill_every_text_field() -> None:
    # The settings tab previews SAMPLE_OVERLAY_VALUES whenever no game is
    # publishing; a field missing from it silently vanishes from the preview.
    for item_id in OVERLAY_ITEM_IDS:
        if item_id in OVERLAY_NON_TEXT_ITEM_IDS:
            continue
        assert format_overlay_value(item_id, SAMPLE_OVERLAY_VALUES), item_id
        assert format_overlay_item(item_id, SAMPLE_OVERLAY_VALUES), item_id


def test_the_table_gives_each_cpu_metric_its_own_label() -> None:
    values = {"cpu_peak_thread_pct": "98", "cpu_temp_c": "61", "cpu_power_w": "88"}
    assert format_overlay_item("cpu_peak_thread_pct", values) == "CPU-T 98%"
    assert format_overlay_item("cpu_temp_c", values) == "CPU 61 C"
    assert format_overlay_item("cpu_power_w", values) == "CPU 88 W"
