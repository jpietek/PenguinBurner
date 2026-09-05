"""Coverage for the sliding on/off switch used in place of a checkbox."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from ui.components.toggle_switch import (
    TRACK_HEIGHT,
    TRACK_WIDTH,
    make_toggle_switch,
    toggle_state_text,
)
from ui.qt import import_qt


def _switch(**kwargs):
    QtCore, QtGui, QtWidgets, _pg = import_qt()
    if QtWidgets is None:
        pytest.skip("PySide6 not available")
    return make_toggle_switch(
        QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, **kwargs
    )


def test_state_text_names_both_positions() -> None:
    assert toggle_state_text(True) == "On"
    assert toggle_state_text(False) == "Off"


def test_the_switch_is_a_checkable_button(qapp) -> None:
    """It stands in for a QCheckBox, so it must behave like one."""
    switch = _switch()
    seen: list[bool] = []
    switch.toggled.connect(seen.append)

    switch.setChecked(True)

    assert switch.isChecked() is True
    assert seen == [True]


def test_an_initial_state_lands_without_animating(qapp) -> None:
    """An unshown window must open with the knob already in position."""
    switch = _switch(checked=True)

    assert switch.slide_position() == 1.0


def test_the_knob_travels_when_toggled(qapp) -> None:
    switch = _switch(checked=False)
    assert switch.slide_position() == 0.0

    switch.setChecked(True)

    assert switch.slide_position() == 1.0


def test_the_switch_keeps_a_switch_shaped_footprint(qapp) -> None:
    """Wider than tall, or it reads as a button rather than a track."""
    switch = _switch()

    assert switch.sizeHint().width() == TRACK_WIDTH
    assert switch.sizeHint().height() == TRACK_HEIGHT
    assert TRACK_WIDTH > TRACK_HEIGHT


def test_the_switch_paints_in_every_state(qapp) -> None:
    """Painting runs on a real device: a bad brush or pen would raise here."""
    QtCore, QtGui, QtWidgets, _pg = import_qt()
    if QtWidgets is None:
        pytest.skip("PySide6 not available")
    switch = _switch()
    image = QtGui.QImage(
        TRACK_WIDTH, TRACK_HEIGHT, QtGui.QImage.Format.Format_ARGB32
    )

    for checked in (False, True):
        for enabled in (True, False):
            switch.setChecked(checked)
            switch.setEnabled(enabled)
            image.fill(0)
            switch.render(image)
            assert image.constBits() is not None
