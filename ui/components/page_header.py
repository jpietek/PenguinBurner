"""One page header for every tab: a description line and an actions slot.

Every page lays out its content inside ``PAGE_MARGINS`` (24 px sides, 16 px
top and bottom), so titles start at the same x and y on each tab. Headers
scroll with their page's content.
"""

from __future__ import annotations

from PySide6 import QtCore as C
from PySide6 import QtWidgets as W

from ui.design.style import GAP

# left, top, right, bottom: GAP xl horizontally, GAP lg vertically.
PAGE_MARGINS = (GAP["xl"], GAP["lg"], GAP["xl"], GAP["lg"])


class PageHeader(W.QWidget):
    """``PageHeader(title, subtitle)``: a 12 px secondary subtitle with
    ``add_action(widget)`` on the right; the title is hidden (the tab names
    the page) and kept as the header's accessible name."""

    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("pageHeader")
        row = W.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(GAP["md"])
        titles = W.QVBoxLayout()
        titles.setSpacing(2)
        self.title = W.QLabel(title)
        self.title.setObjectName("pageTitle")
        self.title.setTextFormat(C.Qt.TextFormat.PlainText)
        # The tab already names the page: the title stays for accessibility
        # and tests, the visible header is the description and actions.
        self.title.hide()
        self.setAccessibleName(title)
        titles.addWidget(self.title)
        self.subtitle = W.QLabel(subtitle)
        self.subtitle.setObjectName("pageSubtitle")
        self.subtitle.setWordWrap(True)
        self.subtitle.setVisible(bool(subtitle))
        titles.addWidget(self.subtitle)
        row.addLayout(titles, 1)
        self.action_row = W.QHBoxLayout()
        self.action_row.setSpacing(GAP["sm"])
        row.addLayout(self.action_row)

    def set_subtitle(self, text: str) -> None:
        self.subtitle.setText(text)
        self.subtitle.setVisible(bool(text))

    def add_action(self, widget: W.QWidget) -> None:
        self.action_row.addWidget(widget, 0, C.Qt.AlignmentFlag.AlignVCenter)
