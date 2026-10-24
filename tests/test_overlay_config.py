from __future__ import annotations

from overlay.config import (
    ADVANCED_OVERLAY_ITEM_IDS,
    BASIC_OVERLAY_ITEM_IDS,
    OVERLAY_ITEM_IDS,
    OVERLAY_SCALE_OPTIONS,
    OverlayConfig,
    load_overlay_config,
    save_overlay_config,
    set_overlay_item_enabled,
    set_overlay_scale,
    set_overlay_update_interval_s,
    snap_overlay_scale,
)


def test_overlay_config_defaults_to_hidden_base_items() -> None:
    config = load_overlay_config("/tmp/does-not-exist-pb-overlay.toml")

    assert config.enabled is False
    assert config.enabled_item_ids == BASIC_OVERLAY_ITEM_IDS
    assert config.update_interval_s == 1
    assert config.scale == 1.0


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


def test_overlay_scale_offers_half_one_and_double() -> None:
    assert OVERLAY_SCALE_OPTIONS == (0.5, 1.0, 2.0)


def test_overlay_scale_snaps_to_nearest_option() -> None:
    assert snap_overlay_scale(0.4) == 0.5
    assert snap_overlay_scale(0.9) == 1.0
    assert snap_overlay_scale(1.7) == 2.0
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
    assert _sample_text("fg_fps", telemetry) == "44 FG"
    assert _sample_text("latency_ms", telemetry) == "LAT P95 ~88 ms"
    assert _sample_text(
        "latency_ms", telemetry | {"latency_avg_ms": "70", "latency_source": "simulation-to-gpu"}
    ) == "LAT AVG ~70 ms"

    assert _sample_text("base_fps", {}) == "60 FPS"
    assert _sample_text("fg_fps", {}) == "120 FG"
    assert _sample_text("latency_ms", {}) == "LAT P95 ~40 ms"
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
    assert _sample_text("profile", telemetry) == "PERF"
    assert _sample_text("gpu_util_pct", telemetry) == "GPU 33%"
    assert _sample_text("cpu_peak_thread_pct", telemetry) == "-"
    assert _sample_text("voltage_mv", telemetry) == "-"


def test_overlay_panel_refreshes_value_rows_from_current_telemetry(monkeypatch) -> None:
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
    panel.preview_label = Label()
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

    assert panel.preview_label.text == "607 MHz"
    assert panel.preview_label.enabled is True
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
