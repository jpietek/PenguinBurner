"""Overlay actions a desktop shortcut fires at a running game.

The layer captures no input: a key binding runs one of these, which writes the
user's own files, and the game picks the change up on its next poll.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from overlay.config import (
    OVERLAY_DETAIL_PRESETS,
    OVERLAY_POSITION_IDS,
    OVERLAY_PRESET_CYCLE,
    OverlayConfig,
    default_overlay_config,
    load_overlay_config,
    next_overlay_detail,
    save_overlay_config,
    set_overlay_item_enabled,
)
from overlay.control import (
    cycle_overlay_detail,
    cycle_overlay_position,
    set_overlay_detail_level,
    set_overlay_visibility,
    step_overlay_scale,
    toggle_overlay_visibility,
)
from overlay.state import read_overlay_override
from overlay.style import (
    OVERLAY_SCALE_OPTIONS,
)


def test_first_toggle_hides_an_overlay_the_config_shows(tmp_path: Path) -> None:
    override = tmp_path / "overlay-override"
    config_path = tmp_path / "overlay.toml"
    save_overlay_config(default_overlay_config(enabled=True), config_path)

    result = toggle_overlay_visibility(override, config_path)

    assert result.visible is False
    assert read_overlay_override(override) is False
    assert result.message == "Overlay hidden."


def test_first_toggle_shows_an_overlay_the_config_hides(tmp_path: Path) -> None:
    override = tmp_path / "overlay-override"
    config_path = tmp_path / "overlay.toml"
    save_overlay_config(default_overlay_config(enabled=False), config_path)

    result = toggle_overlay_visibility(override, config_path)

    assert result.visible is True
    assert read_overlay_override(override) is True
    assert result.message == "Overlay shown."


def test_toggle_flips_an_existing_override(tmp_path: Path) -> None:
    override = tmp_path / "overlay-override"
    set_overlay_visibility("on", override)

    assert toggle_overlay_visibility(override).visible is False
    assert toggle_overlay_visibility(override).visible is True
    assert toggle_overlay_visibility(override).visible is False


def test_visibility_auto_hands_the_overlay_back_to_the_launch_setting(
    tmp_path: Path,
) -> None:
    override = tmp_path / "overlay-override"
    set_overlay_visibility("off", override)

    result = set_overlay_visibility("auto", override)

    assert result.visible is None
    assert read_overlay_override(override) is None
    assert not override.exists()


def test_visibility_rejects_an_unknown_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="on, off, auto"):
        set_overlay_visibility("maybe", tmp_path / "overlay-override")


def test_detail_cycle_visits_every_level_and_wraps() -> None:
    seen = []
    detail = OVERLAY_PRESET_CYCLE[0]
    for _ in OVERLAY_PRESET_CYCLE:
        seen.append(detail)
        detail = next_overlay_detail(detail)

    assert tuple(seen) == ("minimal", "standard", "detailed", "full")
    assert detail == "minimal"


def test_detail_cycle_skips_custom_and_starts_custom_at_small() -> None:
    assert next_overlay_detail("full") == "minimal"
    assert next_overlay_detail("custom") == "minimal"
    assert next_overlay_detail("unknown") == "minimal"


def test_cycle_writes_the_next_level_and_its_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "overlay.toml"
    save_overlay_config(
        OverlayConfig(enabled=True, enabled_item_ids=("base_fps",), detail="minimal"),
        config_path,
    )

    result = cycle_overlay_detail(config_path)

    assert result.detail == "standard"
    assert result.message == "Overlay preset: Medium."
    saved = load_overlay_config(config_path)
    assert saved.detail == "standard"
    assert saved.enabled_item_ids == OVERLAY_DETAIL_PRESETS["standard"]


def test_cycle_starts_a_missing_config_at_the_level_after_its_default(
    tmp_path: Path,
) -> None:
    # No file yet means the default config, whose detail is "custom".
    result = cycle_overlay_detail(tmp_path / "absent.toml")

    assert result.detail == next_overlay_detail("custom")


def test_setting_a_level_leaves_the_rest_of_the_config_alone(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "overlay.toml"
    save_overlay_config(
        OverlayConfig(
            enabled=True,
            enabled_item_ids=("base_fps",),
            update_interval_s=7,
            scale=2.0,
            position="bottom_left",
        ),
        config_path,
    )

    set_overlay_detail_level("detailed", config_path)

    saved = load_overlay_config(config_path)
    assert saved.detail == "detailed"
    assert saved.update_interval_s == 7
    assert saved.scale == 2.0
    assert saved.position == "bottom_left"


def test_setting_an_unknown_level_is_refused_rather_than_snapped(
    tmp_path: Path,
) -> None:
    # normalize_overlay_detail() falls back to "custom" for unknown values,
    # which is right when reading a file and wrong when a user typed it.
    with pytest.raises(ValueError, match="minimal, standard"):
        set_overlay_detail_level("extreme", tmp_path / "overlay.toml")


def test_custom_fields_survive_a_full_cycle_and_reload(tmp_path):
    path = tmp_path / "overlay.toml"
    # Legacy configuration has no separate custom_items field.
    path.write_text('enabled = true\nitems = ["base_fps", "fan_pct"]\nscale = 1.5\n')
    for preset in (*OVERLAY_PRESET_CYCLE, "minimal"):
        assert cycle_overlay_detail(path).detail == preset
        saved = load_overlay_config(path)
        assert saved.scale == 1.5
        assert saved.enabled is True
        assert saved.custom_item_ids == ("base_fps", "fan_pct")
    set_overlay_detail_level("custom", path)
    assert load_overlay_config(path).enabled_item_ids == ("base_fps", "fan_pct")


def test_editing_preset_fields_replaces_saved_custom_selection(tmp_path):
    path = tmp_path / "overlay.toml"
    save_overlay_config(OverlayConfig(enabled_item_ids=("base_fps", "fan_pct")), path)
    set_overlay_detail_level("minimal", path)
    edited = set_overlay_item_enabled(load_overlay_config(path), "temperature_c", True)
    save_overlay_config(edited, path)
    assert load_overlay_config(path).detail == "custom"

    set_overlay_detail_level("full", path)
    step_overlay_scale(1, path)
    cycle_overlay_position(1, path)
    set_overlay_detail_level("custom", path)
    assert load_overlay_config(path).enabled_item_ids == ("base_fps", "temperature_c")


@pytest.mark.parametrize("action", [step_overlay_scale, cycle_overlay_position])
@pytest.mark.parametrize("direction", [-2, 0, 2])
def test_invalid_direction_does_not_write_config(tmp_path, action, direction):
    path = tmp_path / "overlay.toml"
    with pytest.raises(ValueError, match="direction must be -1 or 1"):
        action(direction, path)
    assert not path.exists()


@pytest.mark.parametrize("direction", [-1, 1])
def test_scale_steps_stop_at_endpoints_and_preserve_settings(tmp_path, direction):
    path = tmp_path / "overlay.toml"
    scales = OVERLAY_SCALE_OPTIONS[::direction]
    save_overlay_config(OverlayConfig(
        enabled=True, scale=scales[0], position="bottom_left",
        enabled_item_ids=("base_fps", "fan_pct"),
    ), path)
    for expected in (*scales[1:], scales[-1], scales[-1]):
        result = step_overlay_scale(direction, path)
        saved = load_overlay_config(path)
        assert result.scale == saved.scale == expected
        assert result.as_json()["scale"] == expected
        assert saved.position == "bottom_left"
        assert saved.detail == "custom"
        assert saved.enabled_item_ids == ("base_fps", "fan_pct")


@pytest.mark.parametrize("direction", [-1, 1])
def test_position_cycle_visits_every_position_and_wraps(tmp_path, direction):
    path = tmp_path / "overlay.toml"
    save_overlay_config(OverlayConfig(scale=1.5, position="top_left"), path)
    positions = []
    for _ in OVERLAY_POSITION_IDS:
        result = cycle_overlay_position(direction, path)
        positions.append(result.position)
        saved = load_overlay_config(path)
        assert saved.position == result.as_json()["position"]
        assert saved.scale == 1.5
        assert saved.enabled is False
    assert set(positions) == set(OVERLAY_POSITION_IDS)
    assert positions[-1] == "top_left"
    assert positions[0] == OVERLAY_POSITION_IDS[direction]
