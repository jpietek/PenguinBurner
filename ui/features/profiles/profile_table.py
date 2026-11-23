"""Saved-profiles table: sortable plain-text headers, extended selection.

One density (32 px rows, 8x10 px cells), a selection fill with a 2 px
accent bar, tier dot + label, the power delta against the factory limit,
and status pills (New, Running, At startup, Needs verification) beside the
profile name. The table only shows rows; the page owns what they mean.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from ui.components.profile_style import (
    CELL_PAD_X,
    PILL_HEIGHT,
    ROW_HEIGHT,
    paint_pill,
    pill_width,
    tier_color,
)
from ui.design.style import TYPE, WEIGHT, qcolor
from ui.features.profiles.profile_model import (
    COLUMNS,
    POWER,
    PROFILE,
    RIGHT_ALIGNED,
    SORTABLE_COLUMNS,
    TIER,
    Cell,
)

ID_ROLE = int(C.Qt.ItemDataRole.UserRole)
PILLS_ROLE = ID_ROLE + 1
DELTA_ROLE = ID_ROLE + 2
HOVER_ARROW = "quiet"
# Row pills use short words so the profile name keeps its room; the cell
# tooltip and the detail card spell them out.
SHORT_PILLS = {"Needs verification": "Unverified", "At startup": "Startup"}


@dataclass(frozen=True)
class TableRow:
    profile_id: str
    cells: list[Cell]
    pills: tuple[tuple[str, str], ...] = ()


def table_font(*, weight: int | None = None) -> G.QFont:
    font = G.QFont(TYPE["family"])
    font.setPixelSize(TYPE["table"])
    if weight is not None:
        font.setWeight(G.QFont.Weight(weight))
    return font


def _sort_arrow(
    painter: G.QPainter, x: float, y: float, descending: bool, token: str
) -> None:
    path = G.QPainterPath()
    if descending:
        path.moveTo(x, y - 2)
        path.lineTo(x + 8, y - 2)
        path.lineTo(x + 4, y + 2.5)
    else:
        path.moveTo(x, y + 2)
        path.lineTo(x + 8, y + 2)
        path.lineTo(x + 4, y - 2.5)
    path.closeSubpath()
    painter.fillPath(path, qcolor(token))


class SortHeader(W.QHeaderView):
    """Plain-text headers; an arrow shows on the active and hovered column."""

    sort_requested = C.Signal(int)

    def __init__(self, parent=None):
        super().__init__(C.Qt.Orientation.Horizontal, parent)
        self.sort_column = 0
        self.descending = True
        self.hover_column = -1
        self.setMouseTracking(True)
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        self.setFixedHeight(ROW_HEIGHT + 2)
        self.sectionClicked.connect(self._clicked)

    def set_sort(self, column: int, descending: bool) -> None:
        self.sort_column, self.descending = column, descending
        self.viewport().update()

    def _clicked(self, column: int) -> None:
        if column in SORTABLE_COLUMNS:
            self.sort_requested.emit(column)

    def mouseMoveEvent(self, event):
        column = self.logicalIndexAt(event.position().toPoint())
        if column != self.hover_column:
            self.hover_column = column
            self.viewport().update()
            self.setCursor(
                C.Qt.CursorShape.PointingHandCursor
                if column in SORTABLE_COLUMNS
                else C.Qt.CursorShape.ArrowCursor
            )
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.hover_column = -1
        self.viewport().update()
        super().leaveEvent(event)

    def sectionSizeFromContents(self, logical_index: int) -> C.QSize:
        text = COLUMNS[logical_index] if 0 <= logical_index < len(COLUMNS) else ""
        width = G.QFontMetrics(table_font(weight=WEIGHT["semibold"])).horizontalAdvance(
            text
        )
        return C.QSize(width + 2 * CELL_PAD_X + 14, ROW_HEIGHT)

    def paintSection(
        self, painter: G.QPainter, rect: C.QRect, logical_index: int
    ) -> None:
        if not rect.isValid():
            return
        painter.save()
        painter.fillRect(rect, qcolor("raised"))
        painter.setPen(qcolor("border"))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        active = logical_index == self.sort_column
        sortable = logical_index in SORTABLE_COLUMNS
        hovered = sortable and logical_index == self.hover_column
        painter.setFont(table_font(weight=WEIGHT["semibold"]))
        painter.setPen(qcolor("ink" if active or hovered else "secondary"))
        text = COLUMNS[logical_index]
        right = logical_index in RIGHT_ALIGNED
        inner = rect.adjusted(CELL_PAD_X, 0, -CELL_PAD_X, 0)
        width = painter.fontMetrics().horizontalAdvance(text)
        arrow_space = 14 if (active or hovered) else 0
        if right:
            text_rect = C.QRect(
                inner.right() - width - arrow_space + 1,
                inner.top(),
                width,
                inner.height(),
            )
        else:
            text_rect = C.QRect(inner.left(), inner.top(), width, inner.height())
        painter.drawText(text_rect, int(C.Qt.AlignmentFlag.AlignVCenter), text)
        if active or hovered:
            descending = self.descending if active else logical_index == 0
            _sort_arrow(
                painter,
                text_rect.right() + 6,
                rect.center().y() + 0.5,
                descending,
                "accent" if active else HOVER_ARROW,
            )
        painter.restore()


class RowDelegate(W.QStyledItemDelegate):
    def __init__(self, table: ProfileTable):
        super().__init__(table)
        self.table = table

    def sizeHint(self, option, index):
        item = self.table.item(index.row(), index.column())
        text = "" if item is None else item.text()
        font = table_font(
            weight=WEIGHT["semibold"] if index.column() == PROFILE else None
        )
        # A little slack: widget-device metrics can round wider than these.
        width = G.QFontMetrics(font).horizontalAdvance(text) + 2 * CELL_PAD_X + 6
        if index.column() == POWER:
            delta = index.data(DELTA_ROLE)
            if delta:
                width += G.QFontMetrics(font).horizontalAdvance(delta[0]) + 8
        if index.column() == TIER and text:
            width += 14
        return C.QSize(width, ROW_HEIGHT)

    def paint(self, painter: G.QPainter, option, index) -> None:
        painter.save()
        rect = option.rect
        row, column = index.row(), index.column()
        selected = self.table.selectionModel().isRowSelected(row, C.QModelIndex())
        if selected:
            painter.fillRect(rect, qcolor("selection"))
        elif row == self.table.hover_row:
            painter.fillRect(rect, qcolor("raised"))
        painter.setPen(qcolor("divider"))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        if selected and column == self.table.first_visual_column():
            painter.fillRect(
                C.QRect(rect.left(), rect.top(), 2, rect.height()), qcolor("accent")
            )
        item = self.table.item(row, column)
        text = "" if item is None else item.text()
        inner = rect.adjusted(CELL_PAD_X, 0, -CELL_PAD_X, 0)
        ink = qcolor("ink")
        if column == PROFILE:
            self._paint_profile(painter, inner, text, index.data(PILLS_ROLE) or (), ink)
        elif column == TIER:
            self._paint_tier(painter, inner, text, ink)
        elif column == POWER:
            self._paint_power(painter, inner, text, index.data(DELTA_ROLE), ink)
        elif text:
            painter.setFont(table_font())
            painter.setPen(ink)
            align = C.Qt.AlignmentFlag.AlignVCenter | (
                C.Qt.AlignmentFlag.AlignRight
                if column in RIGHT_ALIGNED
                else C.Qt.AlignmentFlag.AlignLeft
            )
            elided = painter.fontMetrics().elidedText(
                text, C.Qt.TextElideMode.ElideRight, inner.width()
            )
            painter.drawText(inner, int(align), elided)
        else:
            self._paint_missing(painter, inner, column)
        painter.restore()

    def _paint_missing(self, painter, inner, column) -> None:
        painter.setFont(table_font())
        painter.setPen(qcolor("quiet"))
        align = C.Qt.AlignmentFlag.AlignVCenter | (
            C.Qt.AlignmentFlag.AlignRight
            if column in RIGHT_ALIGNED
            else C.Qt.AlignmentFlag.AlignLeft
        )
        if column != TIER:
            painter.drawText(inner, int(align), "–")

    def _paint_profile(self, painter, inner, text, pills, ink) -> None:
        font = table_font(weight=WEIGHT["semibold"])
        painter.setFont(font)
        metrics = painter.fontMetrics()
        gap = 4
        pills = [
            (SHORT_PILLS.get(str(label), str(label)), tone) for label, tone in pills
        ]
        widths = [pill_width(label, dot=False) for label, _tone in pills]
        full = metrics.horizontalAdvance(text)
        # The name comes first; then as many pills as fit, most important
        # first. A long name is elided only to keep the first pill visible.
        # The cell tooltip lists every pill.
        room = inner.width()
        if widths and full + 8 + widths[0] > room:
            room = room - 8 - widths[0]
        name = metrics.elidedText(text, C.Qt.TextElideMode.ElideRight, max(24, room))
        painter.setPen(ink)
        painter.drawText(inner, int(C.Qt.AlignmentFlag.AlignVCenter), name)
        x = inner.left() + metrics.horizontalAdvance(name) + 8
        top = inner.center().y() - PILL_HEIGHT / 2 + 0.5
        for (label, tone), width in zip(pills, widths, strict=True):
            if x + width > inner.right() + 1:
                break
            paint_pill(
                painter, C.QRectF(x, top, width, PILL_HEIGHT), label, tone, dot=False
            )
            x += width + gap

    def _paint_tier(self, painter, inner, text, ink) -> None:
        if not text:
            return
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        painter.setPen(C.Qt.PenStyle.NoPen)
        painter.setBrush(qcolor(tier_color(text)))
        painter.drawEllipse(C.QPointF(inner.left() + 4, inner.center().y() + 0.5), 4, 4)
        painter.setFont(table_font())
        painter.setPen(ink)
        painter.drawText(
            inner.adjusted(14, 0, 0, 0), int(C.Qt.AlignmentFlag.AlignVCenter), text
        )

    def _paint_power(self, painter, inner, text, delta, ink) -> None:
        if not text:
            self._paint_missing(painter, inner, POWER)
            return
        painter.setFont(table_font())
        right = inner
        if delta:
            delta_text, good = delta
            width = painter.fontMetrics().horizontalAdvance(delta_text)
            painter.setPen(qcolor("gain" if good else "secondary"))
            painter.drawText(
                right,
                int(C.Qt.AlignmentFlag.AlignVCenter | C.Qt.AlignmentFlag.AlignRight),
                delta_text,
            )
            right = inner.adjusted(0, 0, -(width + 8), 0)
        painter.setPen(ink)
        painter.drawText(
            right,
            int(C.Qt.AlignmentFlag.AlignVCenter | C.Qt.AlignmentFlag.AlignRight),
            text,
        )


class ProfileTable(W.QTableWidget):
    """Rows of saved profiles with Ctrl/Shift multi-select."""

    sort_requested = C.Signal(int)
    selection_changed = C.Signal(list)
    delete_requested = C.Signal()
    menu_requested = C.Signal(list, C.QPoint)
    activated_profile = C.Signal(str)

    def __init__(self, parent=None):
        super().__init__(0, len(COLUMNS), parent)
        self.setObjectName("profileTable")
        self.setAccessibleName("Saved profiles")
        self.hover_row = -1
        self._populating = False
        self._rows: list[TableRow] = []
        header = SortHeader(self)
        self.setHorizontalHeader(header)
        self.header = header
        header.sort_requested.connect(self.sort_requested)
        self.setHorizontalHeaderLabels(list(COLUMNS))
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.verticalHeader().setMinimumSectionSize(ROW_HEIGHT)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setMouseTracking(True)
        self.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(W.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setHorizontalScrollMode(W.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollBarPolicy(C.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFocusPolicy(C.Qt.FocusPolicy.StrongFocus)
        self.setContextMenuPolicy(C.Qt.ContextMenuPolicy.DefaultContextMenu)
        self.setItemDelegate(RowDelegate(self))
        header.setMinimumSectionSize(48)
        for column in range(len(COLUMNS)):
            header.setSectionResizeMode(
                column, W.QHeaderView.ResizeMode.ResizeToContents
            )
        header.setSectionResizeMode(PROFILE, W.QHeaderView.ResizeMode.Stretch)
        self.itemSelectionChanged.connect(self._selection_changed)
        self.itemDoubleClicked.connect(
            lambda item: self.activated_profile.emit(self.row_id(item.row()))
        )
        self.setProperty("focusVisible", False)

    # ---------------- rows ----------------

    def set_rows(self, rows: Sequence[TableRow], selected_ids: Sequence[str]) -> None:
        self._populating = True
        try:
            self._rows = list(rows)
            self.clearContents()
            self.setRowCount(len(self._rows))
            for r, row in enumerate(self._rows):
                for column, cell in enumerate(row.cells):
                    item = W.QTableWidgetItem(cell.text)
                    item.setData(ID_ROLE, row.profile_id)
                    if cell.tooltip:
                        item.setToolTip(cell.tooltip)
                    if column == PROFILE:
                        item.setData(PILLS_ROLE, tuple(row.pills))
                        tips = [cell.text, *(label for label, _tone in row.pills)]
                        item.setToolTip(" · ".join(tips))
                    if column == POWER and cell.delta:
                        item.setData(DELTA_ROLE, (cell.delta, cell.delta_good))
                    if column in RIGHT_ALIGNED:
                        item.setTextAlignment(
                            C.Qt.AlignmentFlag.AlignRight
                            | C.Qt.AlignmentFlag.AlignVCenter
                        )
                    self.setItem(r, column, item)
            self.select_ids(selected_ids)
        finally:
            self._populating = False
        self.viewport().update()

    def row_ids(self) -> list[str]:
        return [row.profile_id for row in self._rows]

    def row_id(self, row: int) -> str:
        return self._rows[row].profile_id if 0 <= row < len(self._rows) else ""

    def row_of(self, profile_id: str) -> int:
        return next(
            (i for i, row in enumerate(self._rows) if row.profile_id == profile_id), -1
        )

    def cell_text(self, profile_id: str, column: int) -> str:
        item = self.item(self.row_of(profile_id), column)
        return "" if item is None else item.text()

    def cell_tooltip(self, profile_id: str, column: int) -> str:
        item = self.item(self.row_of(profile_id), column)
        return "" if item is None else item.toolTip()

    def pills(self, profile_id: str) -> list[str]:
        row = self.row_of(profile_id)
        return [label for label, _tone in self._rows[row].pills] if row >= 0 else []

    def first_visual_column(self) -> int:
        return self.horizontalHeader().logicalIndex(0)

    # ---------------- selection ----------------

    def selected_ids(self) -> list[str]:
        model = self.selectionModel()
        rows = sorted({index.row() for index in model.selectedRows()}) if model else []
        return [self.row_id(r) for r in rows]

    def select_ids(self, ids: Sequence[str], *, current: str = "") -> None:
        model = self.selectionModel()
        if model is None:
            return
        wanted = set(ids)
        selection = C.QItemSelection()
        for r, row in enumerate(self._rows):
            if row.profile_id in wanted:
                selection.select(
                    self.model().index(r, 0), self.model().index(r, len(COLUMNS) - 1)
                )
        flags = C.QItemSelectionModel.SelectionFlag
        model.select(selection, flags.ClearAndSelect)
        lead = current or (list(ids)[-1] if ids else "")
        row = self.row_of(lead)
        if row >= 0:
            model.setCurrentIndex(self.model().index(row, PROFILE), flags.NoUpdate)
        self.viewport().update()

    def _selection_changed(self) -> None:
        self.viewport().update()
        if self._populating:
            return
        self.selection_changed.emit(self.selected_ids())

    # ---------------- interaction ----------------

    def mouseMoveEvent(self, event):
        row = self.rowAt(event.position().toPoint().y())
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.hover_row = -1
        self.viewport().update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._set_focus_visible(False)
        index = self.indexAt(event.position().toPoint())
        if (
            event.button() == C.Qt.MouseButton.RightButton
            and index.isValid()
            and self.selectionModel().isRowSelected(index.row(), C.QModelIndex())
        ):
            # Right-clicking inside a selection keeps the whole selection.
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        if event.reason() == G.QContextMenuEvent.Reason.Keyboard:
            row = (
                self.currentRow()
                if self.currentRow() >= 0
                else (self.selected_rows() or [-1])[0]
            )
            if row < 0:
                return
            if not self.selectionModel().isRowSelected(row, C.QModelIndex()):
                self.select_ids([self.row_id(row)])
            rect = self.visualRect(self.model().index(row, PROFILE))
            pos = self.viewport().mapToGlobal(rect.center())
        else:
            row = self.rowAt(event.pos().y())
            if row < 0:
                return
            if not self.selectionModel().isRowSelected(row, C.QModelIndex()):
                self.select_ids([self.row_id(row)])
            pos = event.globalPos()
        self.menu_requested.emit(self.selected_ids(), pos)

    def selected_rows(self) -> list[int]:
        return sorted({index.row() for index in self.selectionModel().selectedRows()})

    def keyPressEvent(self, event):
        if event.key() == C.Qt.Key.Key_Delete:
            event.accept()
            self.delete_requested.emit()
            return
        if event.key() in (
            C.Qt.Key.Key_Up,
            C.Qt.Key.Key_Down,
            C.Qt.Key.Key_Home,
            C.Qt.Key.Key_End,
            C.Qt.Key.Key_PageUp,
            C.Qt.Key.Key_PageDown,
        ):
            self._set_focus_visible(True)
        super().keyPressEvent(event)

    def focusInEvent(self, event):
        self._set_focus_visible(
            event.reason()
            in (C.Qt.FocusReason.TabFocusReason, C.Qt.FocusReason.BacktabFocusReason)
        )
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self._set_focus_visible(False)
        super().focusOutEvent(event)

    def _set_focus_visible(self, visible: bool) -> None:
        if bool(self.property("focusVisible")) == visible:
            return
        self.setProperty("focusVisible", visible)
        style = self.style()
        style.unpolish(self)
        style.polish(self)

    def fit_height(self, max_rows: int = 9) -> None:
        rows = max(1, min(self.rowCount(), max_rows))
        # Header, rows and the 1 px top rule.
        self.setFixedHeight(self.horizontalHeader().height() + rows * ROW_HEIGHT + 1)
