"""The stylesheet must parse. Qt drops the whole sheet when it does not.

A single unbalanced brace leaves every widget in the application unstyled --
not the one rule that broke, all of them. It shows up only as one line on
stderr, which is easy to miss, so it is pinned here instead.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from ui.styles import STYLESHEET


def test_the_stylesheet_braces_balance() -> None:
    assert STYLESHEET.count("{") == STYLESHEET.count("}")


def test_no_rule_is_left_open() -> None:
    """Depth may only ever be 0 (between rules) or 1 (inside one)."""
    depth = 0
    for number, line in enumerate(STYLESHEET.splitlines(), 1):
        depth += line.count("{") - line.count("}")
        assert 0 <= depth <= 1, f"line {number} leaves depth {depth}: {line!r}"
    assert depth == 0, "the sheet ends inside a rule"


def test_qt_accepts_the_stylesheet(qapp) -> None:
    """The end-to-end check: Qt itself reports a parse failure as a warning."""
    from ui.qt import import_qt

    QtCore, _QtGui, QtWidgets, _pg = import_qt()
    if QtWidgets is None:
        pytest.skip("PySide6 not available")

    messages: list[str] = []
    previous = QtCore.qInstallMessageHandler(
        lambda _mode, _ctx, message: messages.append(str(message))
    )
    try:
        window = QtWidgets.QMainWindow()
        window.setStyleSheet(STYLESHEET)
        window.show()
        qapp.processEvents()
        window.close()
    finally:
        QtCore.qInstallMessageHandler(previous)

    parse_failures = [m for m in messages if "Could not parse" in m]
    assert not parse_failures, parse_failures


@pytest.mark.parametrize("object_name", ["autoUvScopeButton", "autoUvTuningButton"])
def test_checked_selector_buttons_look_selected(qapp, object_name: str) -> None:
    """A checked option in an exclusive group must not render like an unchecked one."""
    from ui.qt import import_qt

    _QtCore, _QtGui, QtWidgets, _pg = import_qt()
    if QtWidgets is None:
        pytest.skip("PySide6 not available")

    container = QtWidgets.QWidget()
    container.setStyleSheet(STYLESHEET)
    layout = QtWidgets.QHBoxLayout(container)
    buttons = []
    for checked in (True, False):
        button = QtWidgets.QPushButton("Option")
        button.setObjectName(object_name)
        button.setCheckable(True)
        button.setChecked(checked)
        layout.addWidget(button)
        buttons.append(button)
    container.show()
    qapp.processEvents()

    checked_image, unchecked_image = (button.grab().toImage() for button in buttons)
    container.close()

    center = checked_image.rect().center()
    assert checked_image.pixelColor(center) != unchecked_image.pixelColor(center)
