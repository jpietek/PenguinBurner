"""A sliding on/off switch built to read as one of this app's own controls.

Qt has no switch widget, and a QCheckBox reads as an incidental option rather
than the master control of a window. The switch below is a plain
``QAbstractButton``: checkable, emitting ``toggled``, so it drops in wherever a
checkbox was, and it paints itself from ``ui.theme`` so it matches the checked
preset buttons in the Auto Undervolt dialog instead of inventing a palette.

The Qt modules arrive as arguments like everywhere else in ``ui``: the widget
classes are built on first use so importing this module never requires Qt.
"""

from __future__ import annotations

from .. import theme

TRACK_WIDTH = 46
TRACK_HEIGHT = 24
KNOB_MARGIN = 3
SLIDE_DURATION_MS = 130

_CLASS_CACHE: dict[int, type] = {}


def toggle_switch_class(QtCore, QtGui, QtWidgets) -> type:
    """The switch class bound to these Qt modules, built once per module set."""
    key = id(QtWidgets)
    cached = _CLASS_CACHE.get(key)
    if cached is not None:
        return cached

    class ToggleSwitch(QtWidgets.QAbstractButton):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setCheckable(True)
            self.setFixedSize(TRACK_WIDTH, TRACK_HEIGHT)
            self.setCursor(
                getattr(
                    getattr(QtCore.Qt, "CursorShape", QtCore.Qt),
                    "PointingHandCursor",
                )
            )
            # Position of the knob as 0.0 (off) .. 1.0 (on). Animated rather
            # than derived from isChecked() so the control shows the change
            # instead of snapping, which is the whole point of a switch.
            self._slide = 1.0 if self.isChecked() else 0.0
            self._animation = QtCore.QVariantAnimation(self)
            self._animation.setDuration(SLIDE_DURATION_MS)
            self._animation.valueChanged.connect(self._on_slide_value)
            self.toggled.connect(self._animate_to_checked)

        # -- animation -------------------------------------------------------

        def _on_slide_value(self, value) -> None:
            try:
                self._slide = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                return
            self.update()

        def _animate_to_checked(self, checked: bool) -> None:
            target = 1.0 if checked else 0.0
            self._animation.stop()
            self._animation.setStartValue(float(self._slide))
            self._animation.setEndValue(target)
            self._animation.start()

        def slide_position(self) -> float:
            """Knob travel, 0.0 off to 1.0 on. Exposed for tests."""
            return float(self._slide)

        def setChecked(self, checked: bool) -> None:  # noqa: N802 - Qt override
            super().setChecked(bool(checked))
            # A programmatic set before the widget is ever shown should land at
            # its final position rather than crawl there on the first repaint.
            if not self.isVisible():
                self._animation.stop()
                self._slide = 1.0 if self.isChecked() else 0.0
                self.update()

        # -- painting --------------------------------------------------------

        def sizeHint(self):  # noqa: N802 - Qt override name
            return QtCore.QSize(TRACK_WIDTH, TRACK_HEIGHT)

        def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override name
            painter = QtGui.QPainter(self)
            painter.setRenderHint(
                getattr(
                    getattr(QtGui.QPainter, "RenderHint", QtGui.QPainter),
                    "Antialiasing",
                )
            )
            track_color, border_color, knob_color = self._colors()

            radius = self.height() / 2.0
            track = QtCore.QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
            painter.setBrush(QtGui.QBrush(QtGui.QColor(track_color)))
            painter.setPen(QtGui.QPen(QtGui.QColor(border_color), 1))
            painter.drawRoundedRect(track, radius, radius)

            diameter = self.height() - 2.0 * KNOB_MARGIN
            travel = self.width() - diameter - 2.0 * KNOB_MARGIN
            left = KNOB_MARGIN + travel * self._slide
            painter.setBrush(QtGui.QBrush(QtGui.QColor(knob_color)))
            painter.setPen(
                getattr(getattr(QtCore.Qt, "PenStyle", QtCore.Qt), "NoPen")
            )
            painter.drawEllipse(
                QtCore.QRectF(left, KNOB_MARGIN, diameter, diameter)
            )
            painter.end()

        def _colors(self) -> tuple[str, str, str]:
            if not self.isEnabled():
                return theme.SURFACE_ALT_BG, theme.BORDER, theme.TEXT_DISABLED
            if self.isChecked():
                # The same green the Auto Undervolt dialog uses for a chosen
                # scope or preset, so "on" means one thing across the app.
                return (
                    theme.PRIMARY_BUTTON_BG,
                    theme.PRIMARY_BUTTON_HOVER_BORDER
                    if self.underMouse()
                    else theme.PRIMARY_BUTTON_BORDER,
                    theme.PRIMARY_BUTTON_TEXT,
                )
            return (
                theme.CONTROL_HOVER_BG if self.underMouse() else theme.CONTROL_BG,
                theme.BORDER_STRONG,
                theme.TEXT_MUTED,
            )

        def enterEvent(self, event) -> None:  # noqa: N802 - Qt override name
            self.update()
            super().enterEvent(event)

        def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override name
            self.update()
            super().leaveEvent(event)

    _CLASS_CACHE[key] = ToggleSwitch
    return ToggleSwitch


def make_toggle_switch(
    *,
    QtCore,
    QtGui,
    QtWidgets,
    checked: bool = False,
    object_name: str = "",
    tooltip: str = "",
    parent=None,
):
    switch = toggle_switch_class(QtCore, QtGui, QtWidgets)(parent)
    if object_name:
        switch.setObjectName(str(object_name))
    if tooltip:
        switch.setToolTip(str(tooltip))
    switch.setChecked(bool(checked))
    return switch


def toggle_state_text(checked: bool) -> str:
    """The word shown beside a switch. Pure so the caller can test the label."""
    return "On" if checked else "Off"
