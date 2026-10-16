"""Overlay actions a desktop shortcut fires at a running game.

The layer captures no input: a key binding runs one of these, which writes the
user's own files, and the game picks the change up on its next poll.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from overlay.config import OVERLAY_DETAIL_IDS
from overlay.config import OVERLAY_DETAIL_PRESETS
from overlay.config import OverlayConfig
from overlay.config import default_overlay_config
from overlay.config import load_overlay_config
from overlay.config import next_overlay_detail
from overlay.config import save_overlay_config
from overlay.control import cycle_overlay_detail
from overlay.control import set_overlay_detail_level
from overlay.control import set_overlay_visibility
from overlay.control import toggle_overlay_visibility
from overlay.state import read_overlay_override


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
    detail = OVERLAY_DETAIL_IDS[0]
    for _ in OVERLAY_DETAIL_IDS:
        seen.append(detail)
        detail = next_overlay_detail(detail)

    assert tuple(seen) == OVERLAY_DETAIL_IDS
    assert detail == OVERLAY_DETAIL_IDS[0]


def test_detail_cycle_keeps_custom_reachable_from_a_key_binding() -> None:
    # Custom holds the user's own field selection; skipping it in the cycle
    # would make that selection unreachable without alt-tabbing.
    assert "custom" in OVERLAY_DETAIL_IDS
    assert next_overlay_detail("full") == "custom"


def test_cycle_writes_the_next_level_and_its_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "overlay.toml"
    save_overlay_config(
        OverlayConfig(enabled=True, enabled_item_ids=("base_fps",), detail="minimal"),
        config_path,
    )

    result = cycle_overlay_detail(config_path)

    assert result.detail == "standard"
    assert result.message == "Overlay detail: Standard."
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
