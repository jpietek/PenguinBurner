"""The Overlay tab's painted preview.

The preview is what makes the settings legible: it has to reflect the layout,
the corner, the face and the field list, and it has to draw nothing at all when
the overlay is switched off.
"""

from __future__ import annotations

import os

import pytest

from overlay.config import OverlayConfig
from overlay.overlay_text import SAMPLE_OVERLAY_VALUES


def _preview():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    from ui.components.overlay_preview import overlay_preview_class

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    return overlay_preview_class(QtCore, QtGui, QtWidgets)()


def _config(**kwargs) -> OverlayConfig:
    base = {
        "enabled": True,
        "enabled_item_ids": ("base_fps", "clock_mhz", "voltage_mv", "gpu_name"),
    }
    base.update(kwargs)
    return OverlayConfig(**base)


def test_the_hud_layout_gives_the_preview_the_designed_rows() -> None:
    preview = _preview()
    preview.set_config(_config(layout="hud"))
    preview.set_values(dict(SAMPLE_OVERLAY_VALUES))

    kinds = [row["kind"] for row in preview._rows()]

    assert kinds[0] == "hero"
    assert "foot" in kinds


def test_the_table_layout_gives_the_preview_one_row_per_line() -> None:
    preview = _preview()
    preview.set_config(_config(layout="table"))
    preview.set_values(dict(SAMPLE_OVERLAY_VALUES))

    rows = preview._rows()

    # The plain grid has no headline and no footer, just lines.
    assert rows
    assert {row["kind"] for row in rows} == {"value"}


@pytest.mark.parametrize(
    ("position", "left_edge", "top_edge"),
    [
        ("top_left", True, True),
        ("top_right", False, True),
        ("bottom_left", True, False),
        ("bottom_right", False, False),
    ],
)
def test_the_preview_puts_the_box_in_the_chosen_corner(
    position: str, left_edge: bool, top_edge: bool
) -> None:
    from ui.components.overlay_preview import CANVAS_H, CANVAS_W

    preview = _preview()
    preview.set_config(_config(position=position))
    preview.set_values(dict(SAMPLE_OVERLAY_VALUES))

    x, y = preview._origin(400.0, 200.0, 10.0)

    assert (x < CANVAS_W / 2) is left_edge
    assert (y < CANVAS_H / 2) is top_edge


def test_a_centred_position_centres_the_box() -> None:
    from ui.components.overlay_preview import CANVAS_W

    preview = _preview()
    preview.set_config(_config(position="top_center"))

    x, _y = preview._origin(400.0, 200.0, 10.0)

    assert x == pytest.approx((CANVAS_W - 400.0) / 2.0)


def test_the_face_follows_the_scale() -> None:
    preview = _preview()
    preview.set_config(_config(scale=1.0))
    single = preview._face().pointSizeF()
    preview.set_config(_config(scale=2.0))
    double = preview._face().pointSizeF()

    assert double == pytest.approx(single * 2.0, rel=0.02)


def test_the_graph_only_shows_when_its_field_is_on() -> None:
    preview = _preview()
    preview.set_config(_config())
    assert preview._graph_enabled() is False

    preview.set_config(
        _config(enabled_item_ids=("base_fps", "frametime_graph"))
    )
    assert preview._graph_enabled() is True


def test_a_disabled_overlay_paints_only_the_stand_in_frame() -> None:
    from PySide6 import QtGui

    preview = _preview()
    preview.set_config(_config(enabled=False))
    preview.set_values(dict(SAMPLE_OVERLAY_VALUES))
    preview.resize(640, 360)
    off = preview.grab().toImage()

    preview.set_config(_config(enabled=True))
    on = preview.grab().toImage()

    assert isinstance(off, QtGui.QImage)
    assert off != on


def test_the_overlay_tab_can_switch_the_overlay_on(tmp_path) -> None:
    """The master switch lived only on the Game Library row and in the CLI, so
    this tab could show an empty preview with no way to explain or fix it."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets

    from overlay.config import load_overlay_config
    from ui.components.overlay_config import OverlayConfigPanel

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    config_path = tmp_path / "overlay.toml"
    panel = OverlayConfigPanel(
        QtCore=QtCore, QtWidgets=QtWidgets, config_path=config_path
    )

    assert panel.enabled_switch.isChecked() is False
    assert panel.enabled_state_label.text() == "Off"

    panel.enabled_switch.setChecked(True)

    assert panel.config.enabled is True
    assert panel.enabled_state_label.text() == "On"
    assert load_overlay_config(config_path).enabled is True

    panel.enabled_switch.setChecked(False)

    assert load_overlay_config(config_path).enabled is False
