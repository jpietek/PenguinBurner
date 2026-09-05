"""A small activity indicator, animated only while it is visible."""

from __future__ import annotations

from .. import theme


def make_spinner(*, QtCore, QtGui, QtWidgets, size: int = 28, parent=None):
    """Create the widget without requiring Qt when this module is imported."""

    class Spinner(QtWidgets.QWidget):
        def __init__(self):
            super().__init__(parent)
            self.setFixedSize(size, size)
            self.setAccessibleName("Loading")
            self._angle = 0
            self._timer = QtCore.QTimer(self)
            self._timer.setInterval(40)
            self._timer.timeout.connect(self._advance)

        def _advance(self) -> None:
            self._angle = (self._angle + 18) % 360
            self.update()

        def showEvent(self, event) -> None:  # noqa: N802 - Qt override
            super().showEvent(event)
            self._timer.start()

        def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
            self._timer.stop()
            super().hideEvent(event)

        def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            rect = QtCore.QRectF(2, 2, self.width() - 4, self.height() - 4)
            faint = QtGui.QColor(theme.STAGE)
            faint.setAlpha(45)
            painter.setPen(QtGui.QPen(faint, 2))
            painter.drawEllipse(rect)
            pen = QtGui.QPen(QtGui.QColor(theme.STAGE), 2)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            painter.setPen(pen)
            painter.drawArc(rect, -self._angle * 16, 110 * 16)
            painter.end()

    return Spinner()
