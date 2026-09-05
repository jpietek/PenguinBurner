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
