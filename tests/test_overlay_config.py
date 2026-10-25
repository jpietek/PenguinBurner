from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from overlay.config import (
    ADVANCED_OVERLAY_ITEM_IDS,
    BASIC_OVERLAY_ITEM_IDS,
    OVERLAY_DETAIL_IDS,
    OVERLAY_DETAIL_PRESETS,
    OVERLAY_FONTS,
    OVERLAY_ITEM_IDS,
    OVERLAY_NON_TEXT_ITEM_IDS,
    OVERLAY_POSITION_IDS,
    OVERLAY_RENDERERS,
    OVERLAY_ROW_GROUPS,
    OVERLAY_SCALE_OPTIONS,
    OVERLAY_SCALE_REFERENCE,
    OverlayConfig,
    clamp_overlay_opacity,
    load_overlay_config,
    normalize_overlay_detail,
    normalize_overlay_font,
    normalize_overlay_position,
    normalize_overlay_renderer,
    save_overlay_config,
    set_overlay_detail,
    set_overlay_font,
    set_overlay_item_enabled,
    set_overlay_opacity,
    set_overlay_position,
    set_overlay_renderer,
    set_overlay_scale,
    set_overlay_update_interval_s,
    snap_overlay_scale,
)
from overlay.overlay_text import SAMPLE_OVERLAY_VALUES


def test_overlay_config_defaults_to_hidden_base_items() -> None:
    config = load_overlay_config("/tmp/does-not-exist-pb-overlay.toml")

    assert config.enabled is False
    assert config.enabled_item_ids == BASIC_OVERLAY_ITEM_IDS
    assert config.update_interval_s == 1
    assert config.scale == 1.0
    assert config.position == "top_right"
    # A config written before detail levels existed keeps its own fields.
    assert config.detail == "custom"
    assert config.opacity == 0.85
    assert config.renderer == "blended"
    assert config.font == "sans"


def test_overlay_config_keeps_gpu_and_cpu_util_in_advanced_items() -> None:
    assert "gpu_util_pct" in ADVANCED_OVERLAY_ITEM_IDS
    assert "cpu_util_pct" in ADVANCED_OVERLAY_ITEM_IDS
    assert "cpu_peak_thread_pct" in ADVANCED_OVERLAY_ITEM_IDS
    assert "gpu_util_pct" in OVERLAY_ITEM_IDS
    assert "cpu_util_pct" in OVERLAY_ITEM_IDS
    assert "cpu_peak_thread_pct" in OVERLAY_ITEM_IDS


def test_overlay_config_round_trips_ordered_items_and_interval(tmp_path) -> None:
    path = tmp_path / "overlay.toml"

    save_overlay_config(
        OverlayConfig(
            enabled=True,
            enabled_item_ids=("latency_ms", "base_fps", "fan_pct"),
            update_interval_s=9,
        ),
        path,
    )

    config = load_overlay_config(path)
    assert config.enabled is True
    assert config.enabled_item_ids == ("base_fps", "latency_ms", "fan_pct")
    assert config.update_interval_s == 9
    assert config.latency_enabled is True


def test_overlay_config_keeps_at_least_one_item_enabled() -> None:
    config = OverlayConfig(enabled=True, enabled_item_ids=("base_fps",))

    updated = set_overlay_item_enabled(config, "base_fps", False)

    assert updated.enabled_item_ids == ("base_fps",)


def test_hotspot_is_opt_in_and_round_trips(tmp_path) -> None:
    config = OverlayConfig(enabled=True)
    assert "hotspot_temperature_c" in ADVANCED_OVERLAY_ITEM_IDS
    assert "hotspot_temperature_c" not in config.enabled_item_ids
    assert "hotspot_temperature_c" in set_overlay_detail(config, "full").enabled_item_ids
    assert "hotspot_temperature_c" not in set_overlay_detail(config, "detailed").enabled_item_ids
    path = tmp_path / "overlay.toml"

    save_overlay_config(set_overlay_item_enabled(config, "hotspot_temperature_c", True), path)
    enabled = load_overlay_config(path)
    assert "hotspot_temperature_c" in enabled.enabled_item_ids
    assert set_overlay_item_enabled(enabled, "hotspot_temperature_c", False) == replace(
        config, custom_item_ids=config.enabled_item_ids
    )


def test_hotspot_panel_displays_live_reading_or_unavailable() -> None:
    from ui.components.overlay_config import ITEM_LABELS, _sample_text

    assert ITEM_LABELS["hotspot_temperature_c"] == "Hotspot temperature"
    assert _sample_text("hotspot_temperature_c", {"hotspot_temperature_c": "79"}) == "79 °C"
    assert _sample_text("hotspot_temperature_c", {}) == "-"


def test_overlay_config_clamps_update_interval() -> None:
    config = OverlayConfig(enabled=True, enabled_item_ids=("base_fps",))

    assert set_overlay_update_interval_s(config, 0).update_interval_s == 1
    assert set_overlay_update_interval_s(config, 11).update_interval_s == 10


def test_overlay_scale_round_trips(tmp_path) -> None:
    path = tmp_path / "overlay.toml"

    save_overlay_config(
        OverlayConfig(enabled=True, enabled_item_ids=("base_fps",), scale=2.0),
        path,
    )

    assert load_overlay_config(path).scale == 2.0


def test_overlay_scale_offers_fractional_steps() -> None:
    # Whole multiples only made sense while the bitmap face was the only one;
    # the distance-field faces resample at any size, so the sizes between the
    # old steps are offered too.
    assert OVERLAY_SCALE_OPTIONS == (0.5, 0.75, 1.0, 1.5, 2.0)


def test_overlay_scale_snaps_to_nearest_option() -> None:
    assert snap_overlay_scale(0.4) == 0.5
    assert snap_overlay_scale(0.72) == 0.75
    assert snap_overlay_scale(0.95) == 1.0
    assert snap_overlay_scale(1.4) == 1.5
    assert snap_overlay_scale(1.9) == 2.0
    assert snap_overlay_scale(5.0) == 2.0
    # Invalid or non-positive values fall back to the adaptive default.
    assert snap_overlay_scale("bogus") == 1.0
    assert snap_overlay_scale(0) == 1.0


def test_set_overlay_scale_preserves_other_fields() -> None:
    config = OverlayConfig(
        enabled=True,
        enabled_item_ids=("base_fps", "latency_ms"),
        update_interval_s=7,
        scale=1.0,
    )

    updated = set_overlay_scale(config, 0.5)

    assert updated.scale == 0.5
    assert updated.enabled is True
    assert updated.update_interval_s == 7
    assert updated.enabled_item_ids == ("base_fps", "latency_ms")


def test_overlay_non_text_items_contribute_no_readout_cell() -> None:
    from overlay.overlay_text import format_overlay_item

    for item_id in OVERLAY_NON_TEXT_ITEM_IDS:
        assert item_id in OVERLAY_ITEM_IDS
        assert format_overlay_item(item_id, dict(SAMPLE_OVERLAY_VALUES)) == ""


def test_overlay_detail_presets_select_known_items_in_order() -> None:
    assert OVERLAY_DETAIL_IDS[-1] == "custom"
    for detail, items in OVERLAY_DETAIL_PRESETS.items():
        assert detail in OVERLAY_DETAIL_IDS
        assert set(items) <= set(OVERLAY_ITEM_IDS)
        assert items == tuple(
            item_id for item_id in OVERLAY_ITEM_IDS if item_id in set(items)
        )


def test_overlay_detail_preset_owns_the_enabled_items() -> None:
    config = OverlayConfig(enabled=True, enabled_item_ids=("fan_pct",))

    assert set_overlay_detail(config, "minimal").enabled_item_ids == ("base_fps",)
    assert set_overlay_detail(config, "full").enabled_item_ids == OVERLAY_ITEM_IDS


def test_overlay_detail_round_trips_and_resolves_items(tmp_path) -> None:
    path = tmp_path / "overlay.toml"

    save_overlay_config(
        OverlayConfig(
            enabled=True,
            enabled_item_ids=("fan_pct",),
            detail="standard",
        ),
        path,
    )
    loaded = load_overlay_config(path)

    assert loaded.detail == "standard"
    assert loaded.enabled_item_ids == OVERLAY_DETAIL_PRESETS["standard"]


def test_toggling_an_item_leaves_the_detail_preset() -> None:
    config = set_overlay_detail(
        OverlayConfig(enabled=True, enabled_item_ids=("base_fps",)),
        "minimal",
    )

    updated = set_overlay_item_enabled(config, "power_w", True)

    assert updated.detail == "custom"
    assert updated.enabled_item_ids == ("base_fps", "power_w")


def test_overlay_detail_falls_back_to_the_checkbox_set() -> None:
    assert normalize_overlay_detail("FULL") == "full"
    assert normalize_overlay_detail("extreme") == "custom"
    assert normalize_overlay_detail(None) == "custom"


def test_overlay_opacity_is_clamped_to_a_readable_range() -> None:
    # A fully transparent panel is indistinguishable from a broken overlay.
    assert clamp_overlay_opacity(0.0) == 0.2
    assert clamp_overlay_opacity(-3) == 0.2
    assert clamp_overlay_opacity(5) == 1.0
    assert clamp_overlay_opacity("bogus") == 0.85
    assert clamp_overlay_opacity(0.5) == 0.5


def test_overlay_renderer_falls_back_to_blended() -> None:
    assert OVERLAY_RENDERERS == ("blended", "clear_rect")
    assert normalize_overlay_renderer("CLEAR_RECT") == "clear_rect"
    assert normalize_overlay_renderer("opengl") == "blended"
    assert normalize_overlay_renderer(None) == "blended"


def test_opacity_and_renderer_round_trip(tmp_path) -> None:
    path = tmp_path / "overlay.toml"
    save_overlay_config(
        set_overlay_renderer(
            set_overlay_opacity(
                OverlayConfig(enabled=True, enabled_item_ids=("base_fps",)), 0.4
            ),
            "clear_rect",
        ),
        path,
    )
    loaded = load_overlay_config(path)

    assert loaded.opacity == 0.4
    assert loaded.renderer == "clear_rect"


def test_overlay_font_falls_back_to_sans() -> None:
    assert OVERLAY_FONTS == ("pixel", "mono", "sans")
    assert normalize_overlay_font("MONO") == "mono"
    # Sans is the fallback because it is the face that survives the smallest
    # sizes: the fixed-pitch ones draw a slashed zero that closes up first.
    assert normalize_overlay_font("comic") == "sans"
    assert normalize_overlay_font(None) == "sans"


def test_font_round_trips_and_leaves_the_rest_alone(tmp_path) -> None:
    path = tmp_path / "overlay.toml"
    save_overlay_config(
        set_overlay_font(
            OverlayConfig(
                enabled=True,
                enabled_item_ids=("base_fps",),
                position="bottom_left",
                opacity=0.4,
            ),
            "sans",
        ),
        path,
    )
    loaded = load_overlay_config(path)

    assert loaded.font == "sans"
    assert loaded.position == "bottom_left"
    assert loaded.opacity == 0.4


def test_overlay_row_groups_place_every_item_exactly_once() -> None:
    grouped = [item_id for group in OVERLAY_ROW_GROUPS for item_id in group]

    assert sorted(grouped) == sorted(OVERLAY_ITEM_IDS)
    assert len(grouped) == len(set(grouped))


def test_overlay_position_round_trips(tmp_path) -> None:
    path = tmp_path / "overlay.toml"

    save_overlay_config(
        OverlayConfig(
            enabled=True,
            enabled_item_ids=("base_fps",),
            position="bottom_left",
        ),
        path,
    )

    assert load_overlay_config(path).position == "bottom_left"


def test_overlay_position_offers_four_corners_and_top_center() -> None:
    assert OVERLAY_POSITION_IDS == (
        "top_left",
        "top_center",
        "top_right",
        "bottom_left",
        "bottom_right",
    )


def test_overlay_position_falls_back_to_the_historical_corner() -> None:
    assert normalize_overlay_position("BOTTOM_RIGHT") == "bottom_right"
    assert normalize_overlay_position("middle") == "top_right"
    assert normalize_overlay_position(None) == "top_right"


def test_set_overlay_position_preserves_other_fields() -> None:
    config = OverlayConfig(
        enabled=True,
        enabled_item_ids=("base_fps", "latency_ms"),
        update_interval_s=7,
        scale=0.5,
    )

    updated = set_overlay_position(config, "top_center")

    assert updated.position == "top_center"
    assert updated.enabled is True
    assert updated.update_interval_s == 7
    assert updated.scale == 0.5
    assert updated.enabled_item_ids == ("base_fps", "latency_ms")


def test_overlay_panel_samples_keep_only_basic_example_values() -> None:
    from ui.components.overlay_config import _sample_text

    telemetry = {
        "present_fps": "22",
        "framegen_fps": "44",
        "latency_ms": "88",
    }

    # Real in-game numbers lead; the 60/120/40 demo values are only the
    # fallback when no wrapped game is publishing telemetry.
    assert _sample_text("base_fps", telemetry) == "22 FPS"
    assert _sample_text("fg_fps", telemetry) == "44 FPS"
    assert _sample_text("latency_ms", telemetry) == "~88 ms"

    assert _sample_text("base_fps", {}) == "60 FPS"
    assert _sample_text("fg_fps", {}) == "120 FPS"
    assert _sample_text("latency_ms", {}) == "~40 ms"
    assert _sample_text("base_fps", {"present_fps": "n/a"}) == "60 FPS"


def test_overlay_panel_values_use_live_telemetry_or_dash() -> None:
    from ui.components.overlay_config import _sample_text

    telemetry = {
        "clock_mhz": "607",
        "profile_tier": "Performance",
        "gpu_util_pct": "33",
        "cpu_peak_thread_pct": "",
    }

    assert _sample_text("clock_mhz", telemetry) == "607 MHz"
    assert _sample_text("profile", telemetry) == "Performance"
    assert _sample_text("gpu_util_pct", telemetry) == "33%"
    assert _sample_text("cpu_peak_thread_pct", telemetry) == "-"
    assert _sample_text("voltage_mv", telemetry) == "-"


def test_overlay_panel_values_pair_units_with_full_labels_without_duplicate_prefixes(
    monkeypatch,
) -> None:
    from ui.components import overlay_config

    monkeypatch.setattr(
        overlay_config,
        "SAMPLE_OVERLAY_VALUES",
        {**SAMPLE_OVERLAY_VALUES, "gpu_name": "NVIDIA GeForce RTX 5080"},
    )

    telemetry = dict(SAMPLE_OVERLAY_VALUES)
    telemetry.update(clock_mhz="2850", mem_clock_mhz="15000", cpu_peak_thread_pct="78")
    expected = {
        "clock_mhz": "2,850 MHz",
        "mem_clock_mhz": "15,000 MHz",
        "cpu_peak_thread_pct": "78%",
        "cpu_temp_c": "61 °C",
        "cpu_power_w": "88 W",
        "uv_offset_mv": "−75 mV",
        "vram": "9.4 / 16 GiB",
        "ram": "14.2 / 32 GiB",
        "fps_session": "38 / 58 / 61 FPS",
        "frametime_graph": "Graph",
        "gpu_name": "NVIDIA GeForce RTX 5080",
        "driver_version": "580.76",
        "resolution": "2560 × 1440",
        "present_mode": "FIFO",
    }

    for item_id, value in expected.items():
        assert overlay_config._sample_text(item_id, telemetry) == value, item_id


def test_overlay_panel_refreshes_value_rows_from_current_telemetry(
    monkeypatch, tmp_path
) -> None:
    from ui.components import overlay_config

    class Label:
        def __init__(self) -> None:
            self.text = ""
            self.enabled = False

        def setText(self, value: str) -> None:
            self.text = value

        def setEnabled(self, enabled: bool) -> None:
            self.enabled = enabled

    panel = object.__new__(overlay_config.OverlayConfigPanel)
    panel.config = OverlayConfig(
        enabled=True,
        enabled_item_ids=("clock_mhz",),
    )
    panel.config_path = tmp_path / "overlay.toml"
    save_overlay_config(panel.config, panel.config_path)
    panel.config = load_overlay_config(panel.config_path)

    class Preview:
        """Stands in for the painted preview: it takes the config and the
        values rather than a formatted string."""

        def __init__(self) -> None:
            self.config = None
            self.values: dict[str, str] = {}

        def set_config(self, config) -> None:
            self.config = config

        def set_values(self, values: dict[str, str]) -> None:
            self.values = dict(values)

    panel.preview = Preview()
    panel.item_value_labels = {
        "clock_mhz": Label(),
        "cpu_peak_thread_pct": Label(),
    }
    monkeypatch.setattr(
        overlay_config,
        "read_overlay_state",
        lambda: {"clock_mhz": "607", "cpu_peak_thread_pct": ""},
    )

    panel.refresh_preview()

    # The preview is handed the live reading and the config, not a rendered
    # string: it paints the rows itself.
    assert panel.preview.values["clock_mhz"] == "607"
    assert panel.preview.config is panel.config
    # A field the telemetry left blank keeps its demo value rather than
    # punching a hole in the preview.
    assert panel.preview.values["cpu_peak_thread_pct"] != ""
    assert panel.item_value_labels["clock_mhz"].text == "607 MHz"
    assert panel.item_value_labels["cpu_peak_thread_pct"].text == "-"


def test_overlay_override_write_clear_roundtrip(tmp_path) -> None:
    """The live-toggle override file the native layer polls: atomic writes,
    idempotent clear, env-pinned path."""
    from overlay.state import (
        clear_overlay_override,
        overlay_override_path,
        write_overlay_override,
    )

    target = tmp_path / "overlay-override"
    assert write_overlay_override(True, target)
    assert target.read_text() == "1"
    assert write_overlay_override(False, target)
    assert target.read_text() == "0"
    assert not list(tmp_path.glob("*.tmp"))  # atomic replace, no leftovers

    clear_overlay_override(target)
    assert not target.exists()
    clear_overlay_override(target)  # idempotent

    env = {"PENGUIN_BURNER_OVERLAY_OVERRIDE": str(tmp_path / "explicit")}
    assert overlay_override_path(env) == tmp_path / "explicit"
    home_env = {"HOME": str(tmp_path)}
    assert overlay_override_path(home_env).name == "overlay-override"


def test_the_layer_and_the_preview_agree_on_what_1x_means() -> None:
    # The desktop preview sizes its text with OVERLAY_SCALE_REFERENCE and the
    # layer with kOverlayScaleReference; if they drifted, the preview would
    # stop showing the size the game draws.
    header = (
        Path(__file__).resolve().parents[1]
        / "overlay/native/latency_layer/src/latency_layer_internal.h"
    ).read_text(encoding="utf-8")
    match = re.search(r"kOverlayScaleReference = ([0-9.]+);", header)
    assert match is not None
    assert float(match.group(1)) == OVERLAY_SCALE_REFERENCE == 0.75
