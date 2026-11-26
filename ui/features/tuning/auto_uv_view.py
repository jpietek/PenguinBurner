"""The Auto UV page layout: status card with tier strip, V/F plot beside the
log, and the runs table. ``AutoUvView`` only lays out and renders; the real
page drives it from the scan session and the UI mock from its replay."""

from __future__ import annotations

import pyqtgraph as pg
from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from ui.components.elided_label import ElidedLabel
from ui.components.page_header import PAGE_MARGINS
from ui.components.scan_plot import ScanPlot
from ui.components.scan_progress import StatusDot, TierStrip
from ui.components.scan_runs_table import MeasurementLanes, mixed, runs_table
from ui.components.table_style import apply_table_spec
from ui.design.style import COL, GAP, RAD, TYPE, WEIGHT, ChartLegend, focus_visible

LOG_LIMIT = 6000
IDLE_TITLE = "Ready to scan"
IDLE_DETAIL = (
    "No Auto UV runs in this session. Choose a GPU and scan scope to get started."
)
EMPTY_RUNS_META = "Measurements and results appear here during a scan."


def label(text: str, role: str) -> W.QLabel:
    widget = W.QLabel(text)
    widget.setObjectName(role)
    widget.setWordWrap(False)
    return widget


def button(text, slot, role=None):
    widget = W.QPushButton(text)
    if role:
        widget.setObjectName(role)
    widget.clicked.connect(slot)
    return widget


class VisibleWhileEnabled(C.QObject):
    """Keeps a button shown only while it is enabled."""

    def __init__(self, button):
        super().__init__(button)
        button.installEventFilter(self)
        button.setVisible(button.isEnabled())

    def eventFilter(self, watched, event):
        if event.type() == C.QEvent.Type.EnabledChange:
            watched.setVisible(watched.isEnabled())
        return False


def scan_page_stylesheet():
    """Auto UV page cards, log and scrollbars. The production setup and
    final-choice dialogs are children of the page, so they inherit the
    group-box, caution and focus rules too."""
    c, r, t, w = COL, RAD, TYPE, WEIGHT
    caution = mixed("warning", 0.07, "raised").name()
    return f"""
        QFrame#uvCard{{background:{c["panel"]};border:1px solid {c["border"]};border-radius:{r["panel"]}px}}
        QStackedWidget#uvPlotStack{{border-top:1px solid {c["divider"]}}}
        QSplitter#uvCards::handle{{background:transparent}}
        QSplitter#uvPlotLog::handle{{background:{c["divider"]}}}
        QSplitter#uvPlotLog::handle:hover{{background:{c["accent"]}}}
        QLabel#uvCandidate{{font-size:{t["body"]}px;color:{c["ink"]};padding-left:{GAP["sm"]}px;border-left:1px solid {c["border"]};margin-left:{GAP["xs"]}px}}
        QFrame#uvLogPane{{background:{c["window"]};border:none;border-bottom-right-radius:{r["panel"] - 1}px}}
        QLabel#uvLogTitle{{font-weight:{w["semibold"]}}}
        QLabel#uvLogCount{{font-size:{t["caption"]}px;color:{c["quiet"]}}}
        QPushButton#copyLogs{{font-size:{t["caption"]}px;padding:3px 10px}}
        QPushButton#copyLogs:focus{{padding:2px 9px}}
        QPlainTextEdit#uvLog{{background:{c["window"]};color:{c["secondary"]};border:none;border-top:1px solid {c["divider"]};padding:4px 6px;font-family:"JetBrains Mono","DejaVu Sans Mono","Noto Sans Mono",monospace;font-size:{t["caption"]}px}}
        QTableWidget#scanRunsTable{{background:{c["panel"]};border:none;border-top:1px solid {c["divider"]};border-bottom-left-radius:{r["panel"] - 1}px;border-bottom-right-radius:{r["panel"] - 1}px}}
        QTableWidget#scanRunsTable:focus{{border:2px solid {c["focus"]}}}
        QGroupBox{{border:1px solid {c["border"]};border-radius:{r["panel"]}px;margin-top:0;padding-top:34px;font-size:{t["section"]}px;font-weight:{w["semibold"]}}}
        QGroupBox *{{font-weight:{w["regular"]}}}
        QGroupBox::title{{subcontrol-origin:padding;subcontrol-position:top left;left:14px;top:12px;padding:0;color:{c["ink"]}}}
        QLabel#autoUvTargetCaution{{color:{c["warning"]};background:{caution};border:none;border-radius:{r["control"]}px;padding:7px 13px;font-size:{t["caption"]}px;qproperty-alignment:'AlignLeft|AlignVCenter'}}
        QTableView:focus{{border:2px solid {c["focus"]}}}
    """


# The idle runs card's body: room for the "No runs yet" line.
IDLE_RUN_ROWS_PX = 32


def fit_choice_dialog():
    """Give the final-choice dialog room for every column: content widths
    with Status stretching last, no sideways scroll, no blank rows, and the
    whole dialog on screen (the table scrolls when its rows do not fit)."""
    dialog = W.QApplication.activeModalWidget()
    if not isinstance(dialog, W.QDialog):
        return
    screen = dialog.screen() or W.QApplication.primaryScreen()
    available = screen.availableGeometry() if screen is not None else None
    width = dialog.width()
    tables = []
    for table in dialog.findChildren(W.QTableView):
        apply_table_spec(table)
        header = table.horizontalHeader()
        last = header.count() - 1
        for col in range(last):
            header.setSectionResizeMode(col, W.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(last, W.QHeaderView.ResizeMode.Stretch)
        # One table density: 32 px rows, one line each.
        table.setWordWrap(False)
        table.verticalHeader().setSectionResizeMode(W.QHeaderView.ResizeMode.Fixed)
        needed = (
            sum(
                header.sectionSize(col)
                for col in range(last)
                if not header.isSectionHidden(col)
            )
            + max(
                header.sectionSizeFromContents(last).width(),
                table.sizeHintForColumn(last),
            )
            + 2 * table.frameWidth()
            + 24
        )
        dialog_layout = dialog.layout()
        margins = dialog_layout.contentsMargins() if dialog_layout else None
        width = max(
            width, 1040, needed + (margins.left() + margins.right() if margins else 0)
        )
        full = (
            header.height()
            + table.verticalHeader().length()
            + 2 * table.frameWidth()
            + 2
        )
        row = table.verticalHeader().defaultSectionSize()
        table.setMinimumHeight(min(full, header.height() + 6 * row))
        table.setMaximumHeight(full)
        table.setVerticalScrollBarPolicy(C.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        tables.append((table, full))
    dialog.setMinimumHeight(0)
    if available is not None:
        width = min(width, int(available.width() * 0.95))
    # Everything but the tables, plus the tables' full height ...
    height = dialog.sizeHint().height() + sum(
        full - table.sizeHint().height() for table, full in tables
    )
    # ... capped to the screen: the table takes what is left and scrolls.
    if available is not None:
        height = min(height, int(available.height() * 0.9))
    dialog.resize(width, height)
    if available is not None:
        frame = dialog.frameGeometry()
        frame.moveCenter(available.center())
        dialog.move(frame.topLeft())


class AutoUvView(W.QWidget):
    """Status card (stage, candidate, tier strip, plot | log) over the runs
    table. Subclasses own ``setup``, ``stop`` and what the view shows."""

    import_requested = C.Signal()

    def __init__(self, lanes: MeasurementLanes, subtitle: str = ""):
        super().__init__()
        self.log_lines: list[str] = []
        page = W.QVBoxLayout(self)
        page.setContentsMargins(*PAGE_MARGINS)
        page.setSpacing(GAP["md"])
        # No page title: the tab names the page, and the plot needs the
        # height. The GPU caption sits in the card's detail line.
        self.gpu_caption = label(subtitle, "caption")
        self.gpu_caption.setVisible(bool(subtitle))
        self.setup_button = button("Setup Auto Undervolt", self.setup, "setupScan")
        self.stop_button = button("Stop", self.stop)
        self.import_button = button("Import Afterburner", self.import_requested.emit)

        # Card 1: status, tier strip, then plot | log.
        self.scan_card = W.QFrame()
        self.scan_card.setObjectName("uvCard")
        card = W.QVBoxLayout(self.scan_card)
        card.setContentsMargins(1, 1, 1, 1)
        card.setSpacing(0)
        head = W.QVBoxLayout()
        head.setContentsMargins(GAP["lg"] + 4, GAP["md"], GAP["lg"] + 4, GAP["sm"])
        head.setSpacing(GAP["sm"])
        card.addLayout(head)
        status = W.QVBoxLayout()
        status.setSpacing(2)
        stage_row = W.QHBoxLayout()
        stage_row.setSpacing(GAP["sm"])
        self.stage_dot = StatusDot()
        stage_row.addWidget(self.stage_dot)
        self.stage_label = label("", "sectionTitle")
        stage_row.addWidget(self.stage_label)
        # Elided: the live probe line is long, and a label sized to it widened
        # the window past the screen and re-laid it out on every update.
        self.candidate_label = ElidedLabel("uvCandidate", copyable=True)
        stage_row.addWidget(self.candidate_label, 1)
        status.addLayout(stage_row)
        self.stage_detail = label("", "caption")
        self.stage_detail.setSizePolicy(
            W.QSizePolicy.Policy.Ignored, W.QSizePolicy.Policy.Preferred
        )
        detail_row = W.QHBoxLayout()
        detail_row.setSpacing(GAP["md"])
        detail_row.addWidget(self.stage_detail, 1)
        detail_row.addWidget(self.gpu_caption)
        status.addLayout(detail_row)
        head.addLayout(status)
        self.tier_strip = TierStrip()
        head.addWidget(self.tier_strip)

        self.plot = ScanPlot(
            QtWidgets=W,
            pg=pg,
            x_label="Voltage",
            x_units="mV",
            y_label="Clock",
            y_units="MHz",
        )
        self.plot.plot.plotItem.legend.setVisible(False)
        self.plot_stack = W.QStackedWidget()
        self.plot_stack.setObjectName("uvPlotStack")
        empty = W.QWidget()
        v = W.QVBoxLayout(empty)
        # A card heading like every other: left-aligned at the top.
        v.setContentsMargins(GAP["lg"] + 4, GAP["lg"], GAP["lg"] + 4, GAP["lg"])
        v.setSpacing(2)
        for title, role in [
            ("Voltage / frequency curve", "sectionTitle"),
            ("The base curve appears when the scan starts measuring.", "caption"),
        ]:
            v.addWidget(label(title, role))
        v.addStretch()
        self.plot_stack.addWidget(empty)
        plot_panel = W.QWidget()
        plot_layout = W.QVBoxLayout(plot_panel)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        plot_layout.setSpacing(2)
        plot_layout.addWidget(self.plot.widget, 1)
        self.legend = ChartLegend()
        plot_layout.addWidget(self.legend)
        # Full scrollable log beside the plot.
        self.log_pane = W.QFrame()
        self.log_pane.setObjectName("uvLogPane")
        log_layout = W.QVBoxLayout(self.log_pane)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(0)
        log_head = W.QHBoxLayout()
        log_head.setContentsMargins(GAP["md"], GAP["xs"], GAP["xs"], GAP["xs"])
        log_head.setSpacing(GAP["sm"])
        log_head.addWidget(label("Log", "uvLogTitle"))
        self.log_count = label("0 lines", "uvLogCount")
        log_head.addWidget(self.log_count)
        log_head.addStretch()
        self.copy_button = button("Copy logs", self.copy_logs, "copyLogs")
        log_head.addWidget(self.copy_button)
        log_layout.addLayout(log_head)
        self.log_view = W.QPlainTextEdit()
        self.log_view.setObjectName("uvLog")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_LIMIT)
        # Long lines wrap at the pane edge instead of scrolling sideways.
        self.log_view.setLineWrapMode(W.QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.log_view.setHorizontalScrollBarPolicy(
            C.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        mono = G.QFontDatabase.systemFont(G.QFontDatabase.SystemFont.FixedFont)
        mono.setPixelSize(TYPE["caption"])
        self.log_view.setFont(mono)
        self.log_view.setAccessibleName("Scan log")
        log_layout.addWidget(self.log_view, 1)
        self.plot_log = W.QSplitter(C.Qt.Orientation.Horizontal)
        self.plot_log.setObjectName("uvPlotLog")
        self.plot_log.setHandleWidth(1)
        self.plot_log.addWidget(plot_panel)
        self.plot_log.addWidget(self.log_pane)
        self.plot_log.setChildrenCollapsible(False)
        self.plot_log.setStretchFactor(0, 3)
        self.plot_log.setStretchFactor(1, 1)
        self.plot_log.setSizes([840, 280])
        self.plot_stack.addWidget(self.plot_log)
        card.addWidget(self.plot_stack, 1)
        self.splitter = W.QSplitter(C.Qt.Orientation.Vertical)
        self.splitter.setObjectName("uvCards")
        self.splitter.setHandleWidth(GAP["md"])
        self.splitter.addWidget(self.scan_card)

        # Card 2: the runs table.
        table_frame = W.QFrame()
        table_frame.setObjectName("uvCard")
        self.runs_card = table_frame
        layout = W.QVBoxLayout(table_frame)
        layout.setContentsMargins(1, GAP["sm"], 1, 1)
        layout.setSpacing(GAP["xs"])
        # Title and run count on one line: the table needs the height.
        runs_head = W.QHBoxLayout()
        runs_head.setContentsMargins(GAP["lg"] + 3, 0, GAP["lg"] + 3, 0)
        runs_head.setSpacing(GAP["md"])
        runs_head.addWidget(label("Undervolting runs", "sectionTitle"))
        self.runs_meta = label("", "caption")
        runs_head.addWidget(self.runs_meta, 1, C.Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(runs_head)
        self.lanes = lanes
        self.table = runs_table(lanes)
        self.table.currentCellChanged.connect(self.highlight)
        layout.addWidget(self.table)
        # Empty state, centred over the table body.
        self.runs_empty = label("No runs yet", "caption")
        self.runs_empty.setParent(self.table.viewport())
        self.runs_empty.setAlignment(C.Qt.AlignmentFlag.AlignCenter)
        self.table.viewport().installEventFilter(self)
        self.splitter.addWidget(table_frame)
        self.splitter.setChildrenCollapsible(False)
        # The plot is the scan's main view; the runs table gets a third.
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([520, 260])
        page.addWidget(self.splitter, 1)
        self.setStyleSheet(focus_visible(scan_page_stylesheet()))

    # ---------- subclass hooks ----------

    def setup(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def highlight(self, row, *_):
        raise NotImplementedError

    # ---------- rendering ----------

    def eventFilter(self, watched, event):
        if watched is self.table.viewport() and event.type() == C.QEvent.Type.Resize:
            self.runs_empty.setGeometry(watched.rect())
        return super().eventFilter(watched, event)

    def append_log(self, line: str) -> None:
        self.log_lines.append(line)
        del self.log_lines[:-LOG_LIMIT]
        self.log_view.appendPlainText(line)

    def copy_logs(self):
        W.QApplication.clipboard().setText("\n".join(self.log_lines))
        message = f"Copied the full scan log ({len(self.log_lines)} lines)"
        self.copy_button.setText("Log copied")
        self.copy_button.setAccessibleDescription(message)
        announcement = getattr(G, "QAccessibleAnnouncementEvent", None)
        if announcement:
            G.QAccessible.updateAccessibility(announcement(self.copy_button, message))
        C.QTimer.singleShot(1400, lambda: self.copy_button.setText("Copy logs"))

    def show_stage(self, *, title: str, tone: str, candidate: str, detail: str,
                   detail_tooltip: str = "") -> None:
        self.stage_label.setText(title)
        self.stage_dot.set_tone(tone)
        self.candidate_label.set_full_text(candidate)
        self.candidate_label.setVisible(bool(candidate))
        self.stage_detail.setText(detail)
        self.stage_detail.setToolTip(detail_tooltip or detail)

    def show_actions(self, *, busy: bool, stopping: bool, import_ready: bool) -> None:
        self.setup_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy and not stopping)
        self.import_button.setEnabled(not busy and import_ready)

    def show_log_state(self) -> None:
        count = len(self.log_lines)
        self.copy_button.setEnabled(bool(count))
        self.copy_button.setToolTip(f"Copy the full scan log ({count} lines)")
        self.log_count.setText(f"{count} line{'s' if count != 1 else ''}")

    def show_layout(self, *, idle: bool, has_runs: bool, runs_meta: str) -> None:
        self.plot_stack.setCurrentIndex(0 if idle else 1)
        self.plot_stack.setMinimumHeight(150)
        # Idle: the empty runs card shrinks to its heading and "No runs
        # yet"; the plot card keeps the room the curve will use.
        compact = idle and not has_runs
        unlimited = 16777215
        if compact and self.runs_card.maximumHeight() == unlimited:
            header = self.table.horizontalHeader().sizeHint().height()
            self.runs_card.setMaximumHeight(header + 2 * IDLE_RUN_ROWS_PX + 48)
        elif not compact and self.runs_card.maximumHeight() < unlimited:
            # Leaving the idle layout: the plot two thirds, the runs a third.
            self.runs_card.setMaximumHeight(unlimited)
            total = max(self.splitter.height(), 600)
            self.splitter.setSizes([round(total * 2 / 3), round(total / 3)])
        self.runs_empty.setVisible(not has_runs)
        self.runs_meta.setText(runs_meta)
