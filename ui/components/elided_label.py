"""A one-line label that elides to its width and keeps the full text.

For the window status band: the visible text ends in "…" when it does not
fit, the tooltip holds the full text, and (when ``copyable``) the text is
selectable and its context menu copies the full line, as the real
``StatusHeader`` does for the live candidate.
"""

from __future__ import annotations

import html

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W


class ElidedLabel(W.QLabel):
    """``set_full_text(text, prefix_html="")``: ``prefix_html`` (e.g. a
    status dot) is drawn before the elided plain text."""

    PREFIX_WIDTH = 18

    def __init__(self, role: str, *, copyable: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName(role)
        self.setWordWrap(False)
        self.setSizePolicy(W.QSizePolicy.Policy.Ignored, W.QSizePolicy.Policy.Preferred)
        self._full = ""
        self._prefix = ""
        self.copyable = copyable
        if copyable:
            self.setTextInteractionFlags(C.Qt.TextInteractionFlag.TextSelectableByMouse)
            self.setContextMenuPolicy(C.Qt.ContextMenuPolicy.CustomContextMenu)
            self.customContextMenuRequested.connect(self._menu)

    def full_text(self) -> str:
        return self._full

    def set_full_text(self, text: str, prefix_html: str = "") -> None:
        self._full, self._prefix = str(text or ""), prefix_html
        self.setToolTip(self._full)
        self._elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        width = self.contentsRect().width() - (self.PREFIX_WIDTH if self._prefix else 0)
        elided = self.fontMetrics().elidedText(
            self._full, C.Qt.TextElideMode.ElideRight, max(0, width)
        )
        if self._prefix:
            self.setTextFormat(C.Qt.TextFormat.RichText)
            self.setText(self._prefix + html.escape(elided))
        else:
            self.setTextFormat(C.Qt.TextFormat.PlainText)
            self.setText(elided)

    def _menu(self, position: C.QPoint) -> None:
        menu = W.QMenu(self)
        copy = menu.addAction("Copy")
        copy.setEnabled(bool(self._full))
        copy.triggered.connect(lambda: G.QGuiApplication.clipboard().setText(self._full))
        menu.exec(self.mapToGlobal(position))
