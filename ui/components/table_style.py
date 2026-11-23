"""The one table spec, for tables the preview restyles (the real dialogs'
QTableWidget/QTableView): 32 px rows, no vertical gridlines, row dividers,
and a selected row drawn as ``selection`` with a 2 px ``accent`` bar on its
left edge. Each cell keeps its own foreground colour when selected, so a
green delta stays green.
"""

from __future__ import annotations

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from ui.design.style import COL, qcolor

ROW_HEIGHT = 32


class SelectionBarDelegate(W.QStyledItemDelegate):
    """Paints the selection itself: background, accent bar on the first
    visible column, and the cell's own text colour."""

    def paint(self, painter: G.QPainter, option, index) -> None:
        selected = bool(option.state & W.QStyle.StateFlag.State_Selected)
        view = self.parent()
        painter.save()
        if selected:
            painter.fillRect(option.rect, qcolor("selection"))
            header = view.horizontalHeader() if isinstance(view, W.QTableView) else None
            first = header.logicalIndex(0) if header is not None else 0
            if header is not None:
                for visual in range(header.count()):
                    logical = header.logicalIndex(visual)
                    if not header.isSectionHidden(logical):
                        first = logical
                        break
            if index.column() == first:
                bar = C.QRect(option.rect.left(), option.rect.top(), 2, option.rect.height())
                painter.fillRect(bar, qcolor("accent"))
        # Row divider, as the 32 px table spec draws it.
        painter.setPen(qcolor("divider"))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        painter.restore()
        plain = W.QStyleOptionViewItem(option)
        plain.state &= ~W.QStyle.StateFlag.State_Selected
        plain.state &= ~W.QStyle.StateFlag.State_HasFocus
        foreground = index.data(C.Qt.ItemDataRole.ForegroundRole)
        if foreground is None:
            plain.palette.setColor(G.QPalette.ColorRole.Text, G.QColor(COL["ink"]))
        super().paint(painter, plain, index)

    def sizeHint(self, option, index) -> C.QSize:
        size = super().sizeHint(option, index)
        return C.QSize(size.width(), max(size.height(), ROW_HEIGHT))


def apply_table_spec(table: W.QTableView) -> None:
    """32 px rows, no grid, the selection bar delegate."""
    table.setShowGrid(False)
    table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
    table.verticalHeader().setMinimumSectionSize(ROW_HEIGHT)
    for row in range(table.model().rowCount() if table.model() else 0):
        table.verticalHeader().resizeSection(row, ROW_HEIGHT)
    table.setItemDelegate(SelectionBarDelegate(table))
    table.setStyleSheet(
        table.styleSheet()
        + "QTableView::item{padding:0 10px;border:none}"
        + f"QTableView{{gridline-color:{COL['divider']}}}"
    )
