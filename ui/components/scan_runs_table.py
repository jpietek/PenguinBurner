"""The Auto UV runs table: grouped measurement columns with aligned Δ stock
lanes, a coloured result dot and a progress bar per run.

Views describe each row as ``RunCells`` and ``write_run_cells`` puts it in the
table, so the cell roles the delegate paints from are written in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from ui.design.style import COL, RAD, TYPE, qcolor

TABLE_HEADERS = (
    "Run",
    "Voltage\nmV",
    "Target clock\nMHz",
    "Core offset\nMHz",
    "Measured clock\nMHz",
    "Frame rate\nFPS",
    "Board power\nW",
    "Temperature\n°C",
    "Fan speed\n%",
    "Power cap reason",
    "Efficiency\nFPS/W",
    "Result",
    "Status",
)
COLUMN_WIDTHS = (34, 52, 75, 68, 94, 100, 101, 80, 65, 96, 111, 78, 136)
GROUP_ENDS = {0, 4, 10}
DELTA_COLUMNS = (5, 6, 10)
RESULT_COLUMN, STATUS_COLUMN = 11, 12

DELTA_ROLE = int(C.Qt.ItemDataRole.UserRole) + 1
PROGRESS_ROLE = DELTA_ROLE + 1
TONE_ROLE = DELTA_ROLE + 2
RESULT_ROLE = DELTA_ROLE + 3
FINAL_ROLE = DELTA_ROLE + 4
# A delta's own colour token, when a rule other than delta_color decides it.
DELTA_TONE_ROLE = DELTA_ROLE + 5

ROW_BACKGROUNDS = {
    "running": ("accent", 0.08),
    "warning": ("warning", 0.08),
    "error": ("loss", 0.09),
    "baseline": ("raised", 1.0),
}
PROGRESS_FILL = {
    "running": ("primary", 1.0),
    "good": ("gain", 0.18),
    "baseline": ("secondary", 0.16),
    "warning": ("warning", 0.26),
    "error": ("loss", 0.28),
    "neutral": ("raised", 1.0),
}
RESULT_COLORS = {
    "Pass": "gain",
    "Running": "accent",
    "Failed": "loss",
    "Stopping": "warning",
    "Stopped": "warning",
    "Aborted": "warning",
    "Discarded": "quiet",
}


@dataclass(slots=True)
class RunCells:
    """One runs-table row as shown: texts, tooltips and paint roles."""

    texts: list[str]
    tooltips: dict[int, str] = field(default_factory=dict)
    # Column -> percent change vs stock, for the Δ stock lane.
    deltas: dict[int, float | None] = field(default_factory=dict)
    # Column -> colour token for that delta (else delta_color decides).
    delta_tones: dict[int, str] = field(default_factory=dict)
    tone: str = "good"
    result_tone: str = "secondary"
    progress: float = 0.0
    final: bool = False
    # Measurements shown in the secondary colour (saved or neutral rows).
    muted: bool = False


def draw_group_edge(painter, rect):
    painter.save()
    painter.setPen(G.QColor(COL["border"]))
    painter.drawLine(rect.topRight(), rect.bottomRight())
    painter.restore()


def delta_color(column, change):
    if abs(change) < 1:
        return COL["quiet"]
    improves = change < 0 if column == 6 else change > 0
    return COL["gain"] if improves else COL["loss"]


def mixed(token, amount, base="panel"):
    """Token colour blended over a surface, like CSS color-mix()."""
    top, under = qcolor(token), qcolor(base)
    return G.QColor(
        round(under.red() + (top.red() - under.red()) * amount),
        round(under.green() + (top.green() - under.green()) * amount),
        round(under.blue() + (top.blue() - under.blue()) * amount),
    )


class MeasurementLanes:
    """All rows in one measurement column share the same numeric anchors.

    ``value_samples`` and ``delta_samples`` hold, per column, the widest value
    and Δ stock texts that lane must fit."""

    units: ClassVar[dict[int, str]] = {5: "FPS", 6: "W", 10: "FPS/W"}

    def __init__(
        self,
        value_samples: dict[int, list[str]],
        delta_samples: dict[int, list[str]],
    ):
        self.font = G.QFont(TYPE["family"])
        self.font.setPixelSize(TYPE["table"])
        self.font.setFeature(G.QFont.Tag.fromString("tnum"), 1)
        metrics = G.QFontMetrics(self.font)
        self.delta_width = {}
        self.minimum_width = {}
        for col, unit in self.units.items():
            self.delta_width[col] = max(
                metrics.horizontalAdvance(v)
                for v in ["Δ stock", *delta_samples.get(col, [])]
            )
            absolute_width = max(
                metrics.horizontalAdvance(v)
                for v in [unit, *value_samples.get(col, [])]
            )
            self.minimum_width[col] = absolute_width + 8 + self.delta_width[col] + 10

    def cells(self, rect, col):
        bounds = rect.adjusted(5, 0, -5, 0)
        delta = C.QRect(bounds)
        delta.setLeft(delta.right() - self.delta_width[col] + 1)
        value = C.QRect(bounds)
        value.setRight(delta.left() - 9)
        return value, delta


class RunDelegate(W.QStyledItemDelegate):
    def __init__(self, table, lanes):
        super().__init__(table)
        self.lanes = lanes

    def paint(self, painter, option, index):
        col = index.column()
        if col in (RESULT_COLUMN, STATUS_COLUMN):
            self.paint_status(painter, option, index)
        elif col not in self.lanes.units:
            super().paint(painter, option, index)
        else:
            opts = W.QStyleOptionViewItem(option)
            self.initStyleOption(opts, index)
            value = opts.text.partition(" ")[0]
            opts.text = ""
            style = opts.widget.style() if opts.widget else W.QApplication.style()
            style.drawControl(
                W.QStyle.ControlElement.CE_ItemViewItem, opts, painter, opts.widget
            )
            value_rect, delta_rect = self.lanes.cells(opts.rect, col)
            align = C.Qt.AlignmentFlag.AlignRight | C.Qt.AlignmentFlag.AlignVCenter
            painter.save()
            painter.setFont(self.lanes.font)
            painter.setPen(
                opts.palette.highlightedText().color()
                if opts.state & W.QStyle.StateFlag.State_Selected
                else opts.palette.text().color()
            )
            painter.drawText(value_rect, align, value)
            change = index.data(DELTA_ROLE)
            if change is not None:
                tone = index.data(DELTA_TONE_ROLE)
                painter.setPen(
                    qcolor(tone) if tone else G.QColor(delta_color(col, change))
                )
                painter.drawText(delta_rect, align, f"{change:+.1f}%")
            painter.restore()
        if col == 0:
            # Selected rows carry the 2 px accent bar; final verification
            # rows a 2 px border bar.
            selected = bool(option.state & W.QStyle.StateFlag.State_Selected)
            if selected or index.data(FINAL_ROLE):
                bar = C.QRect(option.rect)
                bar.setWidth(2)
                painter.fillRect(bar, qcolor("accent" if selected else "border"))
        if col in GROUP_ENDS:
            draw_group_edge(painter, option.rect)

    def paint_status(self, painter, option, index):
        """Result: dot + label. Status: a progress bar to the run's duration."""
        opts = W.QStyleOptionViewItem(option)
        self.initStyleOption(opts, index)
        text, opts.text = opts.text, ""
        style = opts.widget.style() if opts.widget else W.QApplication.style()
        style.drawControl(
            W.QStyle.ControlElement.CE_ItemViewItem, opts, painter, opts.widget
        )
        painter.save()
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        painter.setFont(self.lanes.font)
        rect = opts.rect.adjusted(8, 0, -8, 0)
        if index.column() == RESULT_COLUMN:
            color = qcolor(index.data(RESULT_ROLE) or "secondary")
            painter.setPen(C.Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(
                C.QPointF(rect.left() + 3.5, rect.center().y() + 0.5), 3.5, 3.5
            )
            font = G.QFont(self.lanes.font)
            font.setWeight(G.QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.setPen(color)
            painter.drawText(
                rect.adjusted(13, 0, 0, 0),
                C.Qt.AlignmentFlag.AlignLeft | C.Qt.AlignmentFlag.AlignVCenter,
                text,
            )
        else:
            bar = C.QRectF(rect.adjusted(0, 6, 0, -6))
            painter.setPen(C.Qt.PenStyle.NoPen)
            painter.setBrush(qcolor("control"))
            painter.drawRoundedRect(bar, RAD["control"], RAD["control"])
            token, amount = PROGRESS_FILL.get(
                index.data(TONE_ROLE) or "good", ("gain", 0.18)
            )
            fill = C.QRectF(bar)
            fill.setWidth(bar.width() * float(index.data(PROGRESS_ROLE) or 0))
            painter.setBrush(mixed(token, amount, "control"))
            painter.setClipRect(fill)
            painter.drawRoundedRect(bar, RAD["control"], RAD["control"])
            painter.setClipping(False)
            painter.setPen(qcolor("ink"))
            painter.drawText(bar, C.Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()


class RunHeader(W.QHeaderView):
    def __init__(self, table, lanes):
        super().__init__(C.Qt.Orientation.Horizontal, table)
        self.lanes = lanes

    def paintSection(self, painter, rect, column):
        if column in self.lanes.units:
            painter.save()
            painter.setFont(self.lanes.font)
            painter.fillRect(rect, G.QColor(COL["raised"]))
            upper, lower = C.QRect(rect), C.QRect(rect)
            upper.setBottom(rect.center().y())
            lower.setTop(upper.bottom() + 1)
            painter.setPen(G.QColor(COL["ink"]))
            painter.drawText(
                upper,
                C.Qt.AlignmentFlag.AlignBottom | C.Qt.AlignmentFlag.AlignHCenter,
                TABLE_HEADERS[column].splitlines()[0],
            )
            painter.setPen(G.QColor(COL["secondary"]))
            val, delta = self.lanes.cells(lower, column)
            for region, text in [(val, self.lanes.units[column]), (delta, "Δ stock")]:
                painter.drawText(
                    region,
                    C.Qt.AlignmentFlag.AlignTop | C.Qt.AlignmentFlag.AlignRight,
                    text,
                )
            painter.setPen(G.QColor(COL["border"]))
            painter.drawLine(rect.bottomLeft(), rect.bottomRight())
            painter.restore()
        else:
            super().paintSection(painter, rect, column)
        if column in GROUP_ENDS:
            draw_group_edge(painter, rect)


def runs_table(lanes: MeasurementLanes) -> W.QTableWidget:
    """The runs table with its header, delegate and column widths."""
    table = W.QTableWidget(0, len(TABLE_HEADERS))
    table.setObjectName("scanRunsTable")
    table.setHorizontalHeader(RunHeader(table, lanes))
    table.setItemDelegate(RunDelegate(table, lanes))
    table.setHorizontalHeaderLabels(TABLE_HEADERS)
    table.setHorizontalScrollMode(W.QAbstractItemView.ScrollMode.ScrollPerPixel)
    table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(W.QAbstractItemView.SelectionMode.SingleSelection)
    table.setShowGrid(False)
    table.verticalHeader().setMinimumSectionSize(20)
    table.verticalHeader().setDefaultSectionSize(32)
    table.verticalHeader().hide()
    table.horizontalHeader().setSectionResizeMode(W.QHeaderView.ResizeMode.Interactive)
    header_font = G.QFont(TYPE["family"])
    header_font.setPixelSize(TYPE["table"])
    header_metrics = G.QFontMetrics(header_font)
    for col, width in enumerate(COLUMN_WIDTHS):
        label_width = max(
            header_metrics.horizontalAdvance(line)
            for line in TABLE_HEADERS[col].splitlines()
        )
        table.setColumnWidth(
            col, max(width, label_width + 9, lanes.minimum_width.get(col, 0))
        )
    table.horizontalHeader().setStretchLastSection(True)
    table.setMinimumHeight(3 * 32 + 47 + 16)
    return table


def write_run_cells(table: W.QTableWidget, row: int, cells: RunCells) -> None:
    """Put one row in the table; follow the newest row unless the user
    scrolled up or selected a run."""
    scroll = table.verticalScrollBar()
    follow = scroll.maximum() - scroll.value() <= 1 and table.currentRow() < 0
    old_top = scroll.value()
    while row >= table.rowCount():
        table.insertRow(table.rowCount())
    background = ROW_BACKGROUNDS.get(cells.tone)
    for col, value in enumerate(cells.texts):
        cell = table.item(row, col)
        if cell is None:
            cell = W.QTableWidgetItem()
            table.setItem(row, col, cell)
        cell.setText(value)
        cell.setToolTip(cells.tooltips.get(col) or value)
        cell.setBackground(mixed(*background) if background else G.QBrush())
        cell.setForeground(
            qcolor(
                "secondary"
                if cells.muted and 0 < col < RESULT_COLUMN or cells.tone == "neutral"
                else "ink"
            )
        )
        if col in DELTA_COLUMNS:
            cell.setData(DELTA_ROLE, cells.deltas.get(col))
            cell.setData(DELTA_TONE_ROLE, cells.delta_tones.get(col) or None)
        if 0 < col < RESULT_COLUMN:
            cell.setTextAlignment(
                C.Qt.AlignmentFlag.AlignRight | C.Qt.AlignmentFlag.AlignVCenter
            )
        if col == RESULT_COLUMN:
            cell.setData(RESULT_ROLE, cells.result_tone)
        if col == 0:
            cell.setData(FINAL_ROLE, cells.final)
        if col == STATUS_COLUMN:
            cell.setData(PROGRESS_ROLE, max(0.0, min(1.0, cells.progress)))
            cell.setData(TONE_ROLE, cells.tone)
    if follow:
        table.scrollToBottom()
    else:
        scroll.setValue(old_top)
