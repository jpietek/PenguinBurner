"""Set a toggle switch from data without emitting ``toggled``.

``ui.components.toggle_switch.ToggleSwitch`` moves its knob from its own
``toggled`` signal (or snaps when hidden). Blocking signals around
``setChecked`` therefore leaves a visible switch with the knob where the
previous value had it. ``set_switch`` blocks the signal and then snaps the knob
to the new value.
"""

from __future__ import annotations

from PySide6 import QtCore as C
from PySide6 import QtWidgets as W


def set_switch(switch: W.QAbstractButton, checked: bool) -> None:
    """Show ``checked`` on ``switch`` with no ``toggled`` emission."""
    checked = bool(checked)
    with C.QSignalBlocker(switch):
        switch.setChecked(checked)
    animation = getattr(switch, "_animation", None)
    if animation is not None:
        animation.stop()
    if hasattr(switch, "_slide"):
        switch._slide = 1.0 if checked else 0.0  # type: ignore[attr-defined]
    switch.update()
