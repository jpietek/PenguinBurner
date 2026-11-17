#!/usr/bin/env python3
"""PenguinBurner UX study. Native Auto UV widgets replay an in-memory fixture."""

import json
import math
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, TypedDict
from unittest.mock import patch

# Use the active checkout's widgets even when a different PenguinBurner wheel is installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pyqtgraph as pg
from build_preview import build_preview
from design import COL, GAP, TYPE, DemoCurvePlot, install_theme
from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineSettings,
)
from PySide6.QtWebEngineWidgets import QWebEngineView

from auto_uv.scan_mode.auto_uv_mode import adaptive_tier_option_key
from ui.assets import asset_image_path
from ui.components import fan_curve_editor, vf_curve_editor
from ui.components.fan_curve_editor import open_fan_curve_editor_dialog
from ui.components.vf_curve_editor import open_vf_curve_editor_dialog
from ui.constants import DEFAULT_FINAL_VERIFICATION_DURATION_S
from ui.dialogs import about, afterburner_import, scan_tuning
from ui.dialogs.final_choice import select_final_candidate
from ui.features.integrations.afterburner_import import afterburner_fan_curve_payload
from ui.features.tuning.tuning import AutoUvNvmlInfo
from ui.qt import apply_dark_palette

HERE = Path(__file__).resolve().parent
SAMPLE = json.loads((HERE / "sample.json").read_text())
GPU = "NVIDIA GeForce RTX 5080"
TIERS = ("Efficiency", "Balanced", "Performance")
COLORS = tuple(COL[t.lower()] for t in TIERS)


class ScanRun(TypedDict):
    tier: str
    points: list[tuple[float, float]]
    mv: int
    target: int
    meas: float
    fps: float
    power: float
    temp: float
    fan: float
    passed: bool


BASE = ScanRun(
    tier="Stock baseline",
    points=SAMPLE["base"],
    mv=1000,
    target=2762,
    meas=2734,
    fps=63.4,
    power=341.0,
    temp=67.0,
    fan=41.0,
    passed=True,
)
BASE_VOLTAGES, BASE_CLOCKS = zip(*BASE["points"])
PAGES = ("Auto UV", "Profiles", "Game Library", "In-Game Overlay")


def setup_options(parent):
    """Show the app's setup dialog with fixed demo facts for every GPU query."""
    gpu = SimpleNamespace(index=0, name=GPU, label="GPU 0 · " + GPU)
    info = AutoUvNvmlInfo(
        power_management_enabled=True,
        power_limit_set_supported=True,
        power_limit_w=360,
        power_limit_default_w=360,
        power_limit_min_w=300,
        power_limit_max_w=400,
        graphics_clock_mhz=210,
        memory_clock_mhz=405,
        supported_memory_clocks_mhz=(15001,),
        supported_graphics_clock_steps_mhz=(210, 3150),
    )
    client = SimpleNamespace(
        capabilities=lambda: SimpleNamespace(identity=SimpleNamespace(name=GPU))
    )
    fixtures = {
        "gpu_choices_with_fallback": ([gpu], 0),
        "DaemonGpuClient": client,
        "read_auto_uv_nvml_info": info,
        "auto_uv_voltage_floor_range_mv": (725, 1240),
        "memory_offset_mhz_range": (0, 4000),
    }
    with ExitStack() as stack:
        for name, reply in fixtures.items():
            stack.enter_context(patch.object(scan_tuning, name, return_value=reply))
        return scan_tuning.select_scan_tuning(
            QtCore=C, QtGui=G, QtWidgets=W, parent=parent, gpu_index=0
        )


def label(text, role):
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
GROUP_ENDS = {0, 4, 10}


def draw_group_edge(painter, rect):
    painter.save()
    painter.setPen(G.QColor(COL["border"]))
    painter.drawLine(rect.topRight(), rect.bottomRight())
    painter.restore()


DELTA_ROLE = int(C.Qt.ItemDataRole.UserRole) + 1


def delta_color(column, change):
    if abs(change) < 1:
        return COL["quiet"]
    improves = change < 0 if column == 6 else change > 0
    return COL["gain"] if improves else COL["loss"]


class MeasurementLanes:
    """All rows in one measurement column share the same numeric anchors."""

    units: ClassVar[dict[int, str]] = {5: "FPS", 6: "W", 10: "FPS/W"}

    def __init__(self):
        self.font = G.QFont(TYPE["family"])
        self.font.setPixelSize(TYPE["table"])
        self.font.setFeature(G.QFont.Tag.fromString("tnum"), 1)
        metrics = G.QFontMetrics(self.font)
        self.delta_width = {}
        self.minimum_width = {}
        for col, unit in self.units.items():
            rows = [BASE, *SAMPLE["rows"]]
            numbers = [
                r["fps"]
                if col == 5
                else r["power"]
                if col == 6
                else r["fps"] / r["power"]
                for r in rows
            ]
            digits = 4 if col == 10 else 2
            deltas = [
                "Δ stock",
                *[f"{100 * (v / numbers[0] - 1):+.1f}%" for v in numbers[1:]],
            ]
            self.delta_width[col] = max(metrics.horizontalAdvance(v) for v in deltas)
            absolute_width = max(
                metrics.horizontalAdvance(v)
                for v in [unit, *[f"{n:.{digits}f}" for n in numbers]]
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
        if col not in self.lanes.units:
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
                painter.setPen(G.QColor(delta_color(col, change)))
                painter.drawText(delta_rect, align, f"{change:+.1f}%")
            painter.restore()
        if col in GROUP_ENDS:
            draw_group_edge(painter, option.rect)


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


class LatestLog(W.QLabel):
    def __init__(self):
        super().__init__()
        self.setObjectName("latestLog")
        self.setTextFormat(C.Qt.TextFormat.PlainText)
        self.setSizePolicy(W.QSizePolicy.Policy.Ignored, W.QSizePolicy.Policy.Fixed)
        self.hide()

    def show_line(self, line):
        self.setToolTip(line)
        self.setAccessibleName("Latest log · " + line)
        self.fit_text()
        self.show()

    def fit_text(self):
        self.setText(
            self.fontMetrics().elidedText(
                self.accessibleName(),
                C.Qt.TextElideMode.ElideRight,
                self.contentsRect().width(),
            )
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_text()


class ScanPage(W.QWidget):
    verified = C.Signal(list)
    show_profiles = C.Signal()

    import_requested = C.Signal()
    show_scan = C.Signal()

    def __init__(self):
        super().__init__()
        self.stage = "idle"
        self.options = None
        self.plan: list[ScanRun] = []
        self.chosen = {}
        self.verify_minutes = {}
        self.tier_traces = {}
        self.elapsed_seconds = 0
        self.done = self.ticks = 0
        self.log_lines = []
        self.timer = C.QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.tick)

        page = W.QVBoxLayout(self)
        page.setContentsMargins(GAP["xl"], GAP["lg"], GAP["xl"], GAP["lg"])
        page.setSpacing(GAP["md"])
        header = W.QHBoxLayout()
        titles = W.QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(label("Auto UV", "pageTitle"))
        titles.addWidget(label("GPU 0 · " + GPU, "caption"))
        header.addLayout(titles)
        header.addStretch()
        self.setup_button = button("Setup Auto Undervolt", self.setup, "setupScan")
        self.stop_button = button("Stop", self.stop)
        self.copy_button = button("Copy full log", self.copy_logs, "copyFullLog")
        self.import_button = button("Import Afterburner", self.import_requested.emit)
        page.addLayout(header)
        status = W.QHBoxLayout()
        self.stage_label = label("", "sectionTitle")
        self.stage_detail = label("", "caption")
        status.addWidget(self.stage_label)
        status.addWidget(self.stage_detail, 1)
        self.next_button = button("", self.next_action, "nextAction")
        status.addWidget(self.next_button)
        page.addLayout(status)
        self.latest_log = LatestLog()
        self.log_strip = W.QFrame()
        self.log_strip.setObjectName("scanLogStrip")
        log_layout = W.QHBoxLayout(self.log_strip)
        log_layout.setContentsMargins(8, 1, 2, 1)
        log_layout.addWidget(self.latest_log, 1)
        log_layout.addWidget(self.copy_button)
        page.addWidget(self.log_strip)
        self.plot = DemoCurvePlot(
            QtWidgets=W,
            pg=pg,
            x_label="Voltage",
            x_units="mV",
            y_label="Clock",
            y_units="MHz",
            x_range=(800, 1200),
            y_range=(1800, 3200),
        )
        self.plot.plot.plotItem.legend.setVisible(False)
        self.plot_stack = W.QStackedWidget()
        self.plot_stack.setMinimumHeight(135)
        empty = W.QWidget()
        v = W.QVBoxLayout(empty)
        v.addStretch()
        for title, role in [
            ("Voltage / frequency curve", "sectionTitle"),
            ("The base curve appears when the scan starts measuring.", "caption"),
        ]:
            txt = label(title, role)
            txt.setAlignment(C.Qt.AlignmentFlag.AlignCenter)
            v.addWidget(txt)
        v.addStretch()
        self.plot_stack.addWidget(empty)
        plot_panel = W.QWidget()
        plot_layout = W.QVBoxLayout(plot_panel)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        plot_layout.setSpacing(2)
        plot_layout.addWidget(self.plot.widget, 1)
        self.legend = W.QLabel()
        self.legend.setTextFormat(C.Qt.TextFormat.RichText)
        self.legend.setObjectName("curveLegend")
        self.legend.setWordWrap(True)
        plot_layout.addWidget(self.legend)
        self.plot_stack.addWidget(plot_panel)
        self.splitter = W.QSplitter(C.Qt.Orientation.Vertical)
        self.splitter.addWidget(self.plot_stack)

        table_frame = W.QWidget()
        layout = W.QVBoxLayout(table_frame)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.addWidget(label("Undervolting runs", "sectionTitle"))
        self.tier_bar = label("", "caption")
        layout.addWidget(self.tier_bar)
        self.table = W.QTableWidget(0, len(TABLE_HEADERS))
        self.table.setObjectName("scanRunsTable")
        self.lanes = MeasurementLanes()
        self.table.setHorizontalHeader(RunHeader(self.table, self.lanes))
        self.table.setItemDelegate(RunDelegate(self.table, self.lanes))
        self.table.setHorizontalHeaderLabels(TABLE_HEADERS)
        self.table.setHorizontalScrollMode(
            W.QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            W.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(W.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setMinimumSectionSize(20)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(
            W.QHeaderView.ResizeMode.Interactive
        )
        header_font = G.QFont(TYPE["family"])
        header_font.setPixelSize(TYPE["table"])
        header_metrics = G.QFontMetrics(header_font)
        for col, width in enumerate(
            [34, 52, 75, 68, 94, 100, 101, 80, 65, 104, 111, 50, 110]
        ):
            label_width = max(
                header_metrics.horizontalAdvance(line)
                for line in TABLE_HEADERS[col].splitlines()
            )
            self.table.setColumnWidth(
                col, max(width, label_width + 9, self.lanes.minimum_width.get(col, 0))
            )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.currentCellChanged.connect(self.highlight)
        self.table.setMinimumHeight(7 * 24 + 47 + 16)
        layout.addWidget(self.table)
        self.splitter.addWidget(table_frame)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([295, 315])
        page.addWidget(self.splitter, 1)
        self.refresh()

    def log(self, message):
        minutes, seconds = divmod(self.elapsed_seconds, 60)
        self.log_lines.append(f"[{minutes:02d}:{seconds:02d}] {message}")
        self.latest_log.show_line(self.log_lines[-1])

    def setup(self):
        options = setup_options(self)
        if options is None:
            return
        resume = (
            options == self.options
            and self.stage == "stopped"
            and self.done < len(self.plan)
        )
        self.options = options
        if not resume:
            mode = options["auto_uv_mode"]
            self.full = mode == "adaptive"
            mode_to_tier = {
                "efficiency": "Efficiency",
                "balanced": "Balanced",
                "performance": "Performance",
                "safe": "Efficiency",
                "normal": "Balanced",
                "aggressive": "Performance",
            }
            tier = mode_to_tier.get(mode, mode.title())
            self.plan = [BASE] + [
                r for r in SAMPLE["rows"] if self.full or r["tier"] == tier
            ]
            if len(self.plan) == 1:
                raise ValueError(f"Unrecognized sample tier {mode!r}")
            self.done = self.elapsed_seconds = 0
            self.chosen.clear()
            self.plot.clear()
            self.tier_traces.clear()
            self.plot.set_source_points(BASE["points"])
            self.table.setRowCount(0)
            self.log_lines.clear()
        self.stage = "running"
        self.show_scan.emit()
        self.log("Resuming example scan" if resume else "Starting example scan")
        self.log("Setup " + json.dumps(options, sort_keys=True))
        self.log("Measurements replay the prerecorded example for the selected scope.")
        self.start_probe()
        self.timer.start()

    def start_probe(self):
        self.ticks = 0
        r = self.plan[self.done]
        if self.done:
            self.plot.set_candidate_points(r["points"], curve_id=f"run-{self.done}")
        self.plot.set_target_point(r["mv"], r["target"])
        self.show_live(r)
        self.log(
            f"Run {self.done + 1} · {r['tier']} · {r['mv']} mV @ {r['target']} MHz"
        )
        self.write_row(self.done, "Running")
        self.refresh()

    def show_live(self, r, *, verifying=False):
        phase = self.ticks + self.done * 0.7
        self.live_mv = r["mv"] + round(4.5 * math.sin(phase * 0.85))
        self.live_mhz = r["meas"] + round(8 * math.cos(phase * 0.8))
        self.plot.set_crosshair_point(self.live_mv, self.live_mhz)
        self.plot.set_target_point(r["mv"], r["target"])
        if r is BASE:
            return
        # Replay a few MHz of settling around the recorded candidate, leaving
        # the separately drawn base and confirmed-tier series at their measured points.
        points = [(v, f + 3 * math.sin(phase + v * 0.02)) for v, f in r["points"]]
        curve_id = "verify-" + r["tier"] if verifying else f"run-{self.done}"
        self.plot.set_candidate_points(
            points, curve_id=curve_id, remember_previous=False
        )

    def write_row(self, i, result):
        r = self.plan[i]
        measured = result in {"Pass", "Failed"}

        def delta(n, base, digits):
            return (
                f"{n:.{digits}f} {(n / base - 1) * 100:+.1f}%"
                if i
                else f"{n:.{digits}f}"
            )

        data = [
            str(i + 1),
            str(r["mv"]),
            str(r["target"]),
            f"{r['target'] - np.interp(r['mv'], BASE_VOLTAGES, BASE_CLOCKS):+.0f}"
            if i
            else "—",
            f"{r['meas']:.2f}",
            delta(r["fps"], BASE["fps"], 2),
            delta(r["power"], BASE["power"], 2),
            f"{r['temp']:.2f}",
            f"{r['fan']:.2f}",
            "none",
            delta(r["fps"] / r["power"], BASE["fps"] / BASE["power"], 4),
            result,
            ("20 / 20 s" if result == "Pass" else "Average FPS low · 20 / 20 s")
            if measured
            else f"{self.ticks * 2} / 20 s",
        ]
        if not measured:
            data[4:11] = ["—"] * 7
            if result == "Running":
                data[4] = f"{self.live_mhz:.0f}"
        scroll = self.table.verticalScrollBar()
        follow = scroll.maximum() - scroll.value() <= 1 and self.table.currentRow() < 0
        old_top = scroll.value()
        if i >= self.table.rowCount():
            self.table.insertRow(i)
        for col, value in enumerate(data):
            cell = self.table.item(i, col)
            if cell is None:
                cell = W.QTableWidgetItem()
                self.table.setItem(i, col, cell)
            cell.setText(value)
            cell.setToolTip(value)
            if col in (5, 6, 10):
                raw, reference = {
                    5: (r["fps"], BASE["fps"]),
                    6: (r["power"], BASE["power"]),
                    10: (r["fps"] / r["power"], BASE["fps"] / BASE["power"]),
                }[col]
                change = 100 * (raw / reference - 1) if i and measured else None
                cell.setData(DELTA_ROLE, change)
                if change is not None:
                    cell.setToolTip(
                        f"{raw:.5f} · {change:+.3f}% vs stock baseline {reference:.5f}"
                    )
            if 0 < col < 11:
                cell.setTextAlignment(
                    C.Qt.AlignmentFlag.AlignRight | C.Qt.AlignmentFlag.AlignVCenter
                )
            if col == 11:
                cell.setForeground(
                    G.QColor(
                        {"Pass": COL["gain"], "Failed": COL["loss"]}.get(
                            result, COL["warning"]
                        )
                    )
                )
        if follow:
            self.table.scrollToBottom()
        else:
            scroll.setValue(old_top)

    def highlight(self, row, *_):
        self.plot.set_highlighted_curve(f"run-{row}" if row > 0 else None)
        self.update_legend()

    def update_legend(self):
        keys = [("Base", COL["baseCurve"], "┄┄")]
        if self.plot._candidate_points:
            keys.append(("Candidate", COL["candidateCurve"], "━●"))
        if self.plot.previous_curves:
            keys.append(("Previous runs", COL["curveHistory"], "━━"))
        for tier, color in zip(TIERS, COLORS):
            if tier in self.tier_traces:
                keys.append((tier + " tier", color, "━━"))
        if self.table.currentRow() > 0:
            keys.append(
                (
                    f"Selected run {self.table.currentRow() + 1}",
                    COL["selectedCurve"],
                    "┄┄",
                )
            )
        self.legend.setText(
            " &nbsp; ".join(
                f'<span style="color:{c}">{key}</span>&nbsp;{name.replace(" ", "&nbsp;")}'
                for name, c, key in keys
            )
        )

    def confirm_tier(self, row):
        tier = row["tier"]
        points = row["points"]
        if tier in self.tier_traces:
            voltages, clocks = zip(*points)
            self.tier_traces[tier].setData(voltages, clocks)
        else:
            self.plot.add_comparison_points(
                points,
                name=tier + " tier",
                color=COLORS[TIERS.index(tier)],
                alpha=255,
                width=3,
                z_value=2,
            )
            self.tier_traces[tier] = self.plot.comparison_curves[-1]

    def tick(self):
        if self.stage == "running":
            self.elapsed_seconds += 2
        elif self.stage == "verifying":
            tier = self.selected_tiers()[self.ticks // 10]
            self.elapsed_seconds += 6 * self.verify_minutes[tier]
        self.ticks += 1
        if self.stage == "verifying":
            tiers = self.selected_tiers()
            step = self.ticks // 10
            if step >= len(tiers):
                self.stage = "complete"
                for tier in tiers:
                    self.confirm_tier(self.chosen[tier])
                self.plot.clear_load_markers()
                self.plot.set_candidate_points([], remember_previous=False)
                self.log("Final verification passed · " + ", ".join(tiers))
                self.timer.stop()
                self.verified.emit(tiers)
            else:
                self.show_verification_tier(tiers[step])
            self.refresh()
            return
        if self.stage != "running":
            self.timer.stop()
            return
        if self.ticks < 10:
            self.show_live(self.plan[self.done])
            self.write_row(self.done, "Running")
            self.refresh()
            return
        i = self.done
        r = self.plan[i]
        result = "Pass" if r["passed"] else "Failed"
        self.write_row(i, result)
        self.log(f"Run {i + 1} · {result} · {r['fps']:.2f} FPS · {r['power']:.2f} W")
        self.done += 1
        if r["tier"] in TIERS and (
            self.done == len(self.plan) or self.plan[self.done]["tier"] != r["tier"]
        ):
            self.confirm_tier(r)
        if self.done < len(self.plan):
            self.start_probe()
            return
        self.timer.stop()
        self.plot.set_candidate_points([])
        self.plot.clear_load_markers()
        self.stage = "choice"
        self.refresh()

    def stop(self):
        if self.stage not in ("running", "verifying"):
            return
        self.timer.stop()
        if self.stage == "running":
            self.write_row(self.done, "Stopped")
            self.stage = "stopped"
        else:
            self.stage = "verify_stopped"
        self.plot.clear_load_markers()
        self.log("Stopped by user")
        self.refresh()

    def selected_tiers(self):
        return [t for t in TIERS if any(r["tier"] == t for r in self.plan)]

    def next_action(self):
        if self.stage == "complete":
            self.show_profiles.emit()
        elif self.stage in ("choice", "stopped", "verify_stopped"):
            selection = {}
            for tier in self.selected_tiers():
                passed = [
                    (i, r)
                    for i, r in enumerate(self.plan[: self.done])
                    if r["tier"] == tier and r["passed"]
                ]
                candidates = []
                for i, r in passed:
                    candidates.append(
                        dict(
                            candidate_id=str(i),
                            candidate_voltage_mv=r["mv"],
                            lock_clock_mhz=r["target"],
                            avg_core_clock_mhz=r["meas"],
                            avg_fps=r["fps"],
                            avg_power_w=r["power"],
                            efficiency_fps_per_w=r["fps"] / r["power"],
                            base_avg_core_clock_mhz=BASE["meas"],
                            base_avg_fps=BASE["fps"],
                            base_avg_power_w=BASE["power"],
                            base_efficiency_fps_per_w=BASE["fps"] / BASE["power"],
                        )
                    )
                if not candidates:
                    return
                choice, duration, _ = select_final_candidate(
                    QtCore=C,
                    QtGui=G,
                    QtWidgets=W,
                    parent=self,
                    candidates=candidates,
                    default_candidate_id=candidates[-1]["candidate_id"],
                    auto_uv_mode=tier.lower(),
                    default_duration_s=DEFAULT_FINAL_VERIFICATION_DURATION_S,
                    request_reason="adaptive-" + tier.lower()
                    if self.full
                    else ("user-stop" if self.stage == "stopped" else ""),
                )
                if choice is None:
                    self.log("Candidate selection dismissed")
                    return
                selection[tier] = self.plan[int(choice["candidate_id"])]
                self.verify_minutes[tier] = duration // 60
                self.log(
                    f"{tier} candidate · {choice['candidate_voltage_mv']} mV @ {choice['lock_clock_mhz']} MHz · {duration // 60} min verification"
                )
            self.chosen = selection
            self.stage, self.ticks = "verifying", 0
            self.show_verification_tier(self.selected_tiers()[0])
            self.timer.start()
            self.refresh()

    def show_verification_tier(self, tier):
        r = self.chosen[tier]
        if self.ticks % 10 == 0:
            self.log("Verifying " + tier + f" · {r['mv']} mV @ {r['target']} MHz")
        self.show_live(r, verifying=True)

    def copy_logs(self):
        W.QApplication.clipboard().setText("\n".join(self.log_lines))
        message = f"Copied the full scan log ({len(self.log_lines)} lines)"
        self.copy_button.setText("Log copied")
        self.copy_button.setAccessibleDescription(message)
        announcement = getattr(G, "QAccessibleAnnouncementEvent", None)
        if announcement:
            G.QAccessible.updateAccessibility(announcement(self.copy_button, message))
        C.QTimer.singleShot(1400, lambda: self.copy_button.setText("Copy full log"))

    def refresh(self):
        names = dict(
            idle="Ready to scan",
            running="Scanning",
            stopped="Scan stopped",
            choice="Ready for final verification",
            verifying="Final verification",
            complete="Scan complete",
            verify_stopped="Verification stopped",
        )
        details = dict(
            idle="Open setup to choose the GPU and scan scope.",
            stopped="Start with matching settings to resume.",
            verifying="Checking the selected tiers",
            verify_stopped="Candidate measurements are available below.",
            complete="Verified profiles are available in this preview.",
            choice="Choose a duration to verify the selected tiers.",
        )
        if self.stage == "running":
            r = self.plan[self.done]
            details["running"] = (
                f"{r['tier']} · target {r['mv']} mV @ {r['target']} MHz  |  "
                f"measured {self.live_mv} mV @ {self.live_mhz:.0f} MHz · {self.ticks * 2} / 20 s"
            )
        if self.stage == "verifying":
            tiers = self.selected_tiers()
            details["verifying"] = (
                f"{tiers[self.ticks // 10]} · {self.live_mv} mV @ {self.live_mhz:.0f} MHz"
                f" · {self.ticks * 10 // len(tiers)}%"
            )
        self.update_legend()
        self.stage_label.setText(names[self.stage])
        self.stage_detail.setText(details[self.stage])
        busy = self.stage in ("running", "verifying")
        self.setup_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.log_strip.setVisible(bool(self.log_lines))
        self.copy_button.setEnabled(bool(self.log_lines))
        self.copy_button.setToolTip(
            f"Copy the full scan log ({len(self.log_lines)} lines)"
        )
        stopped_with_candidates = (
            self.stage == "stopped"
            and not self.full
            and any(r["passed"] for r in self.plan[1 : self.done])
        )
        self.next_button.setVisible(
            self.stage in ("choice", "complete", "verify_stopped")
            or stopped_with_candidates
        )
        self.next_button.setText(
            "View verified profiles →"
            if self.stage == "complete"
            else "Set up final verification"
        )
        self.plot_stack.setCurrentIndex(0 if self.stage == "idle" else 1)
        if self.stage == "idle":
            self.tier_bar.setText("No runs yet")
        else:
            progress = []
            for tier in self.selected_tiers():
                count = sum(r["tier"] == tier for r in self.plan[: self.done])
                total = sum(r["tier"] == tier for r in self.plan)
                progress.append(f"{tier} · {count}/{total}")
            self.tier_bar.setText("      ".join(progress))


class PreviewPage(QWebEnginePage):
    open_setup = C.Signal()
    open_editor = C.Signal(str, int)
    back_to_library = C.Signal()
    adaptive_game = C.Signal(int, int)

    def javaScriptConsoleMessage(self, level, message, line, source):
        if message == "pb-qt-preview-open-setup":
            self.open_setup.emit()
        elif message == "pb-qt-preview-back":
            self.back_to_library.emit()
        elif message.startswith("pb-qt-preview-adaptive:"):
            parts = message.split(":")
            if len(parts) == 3 and all(p.isdigit() and len(p) <= 4 for p in parts[1:]):
                self.adaptive_game.emit(int(parts[1]), int(parts[2]))
        elif message.startswith("pb-qt-preview-edit-"):
            kind, _, number = message.removeprefix("pb-qt-preview-edit-").partition(":")
            if (
                kind in ("fan", "vf")
                and number.isascii()
                and number.isdigit()
                and len(number) < 7
            ):
                self.open_editor.emit(kind, int(number))
        else:
            super().javaScriptConsoleMessage(level, message, line, source)


class Preview(W.QWidget):
    def __init__(self, *, history=False):
        super().__init__()
        self.setWindowTitle("PenguinBurner · Qt UX preview")
        self.setWindowIcon(G.QIcon(str(asset_image_path("penguin-burner.png"))))
        self.resize(1230, 810)
        page = W.QVBoxLayout(self)
        page.setContentsMargins(12, 10, 12, 10)
        self.ready = False
        self.sample_runtime = C.QElapsedTimer()
        self.start_history = history
        self.verified = []
        top = W.QHBoxLayout()
        top.addWidget(label("PenguinBurner", "brand"))
        top.addWidget(button("About", self.show_about, "aboutPreview"))
        top.addStretch()
        top.addWidget(label("Qt prototype · sample data", "caption"))
        page.addLayout(top)
        self.tabs = W.QTabBar()
        for text, icon in zip(
            PAGES, ["tab-auto-uv", "tab-profiles", "tab-game-library", "tab-overlay"]
        ):
            self.tabs.addTab(G.QIcon(str(asset_image_path(icon + ".png"))), text)
        self.tabs.setExpanding(False)
        page.addWidget(self.tabs)
        self.stack = W.QStackedWidget()
        page.addWidget(self.stack, 1)
        self.scan = ScanPage()
        self.scan.import_button.setEnabled(False)
        for action in (
            self.scan.setup_button,
            self.scan.stop_button,
            self.scan.import_button,
        ):
            top.addWidget(action)
        self.scan.show_scan.connect(self.on_scan_started)
        self.stack.addWidget(self.scan)
        self.browser = QWebEngineView(self)
        self.profile = QWebEngineProfile(self)
        page_bridge = PreviewPage(self.profile, self.browser)
        self.browser.setPage(page_bridge)
        page_bridge.open_setup.connect(self.setup_from_profile)
        page_bridge.open_editor.connect(self.open_editor)
        page_bridge.back_to_library.connect(self.back_to_library)
        page_bridge.adaptive_game.connect(self.apply_game_adaptive)
        self.browser.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False
        )
        self.browser.loadFinished.connect(self.loaded)
        self.stack.addWidget(self.browser)
        from native_library import LibraryPage, OverlayPage

        self.library = LibraryPage()
        self.overlay = OverlayPage()
        self.library.selected_game_changed.connect(self.overlay.panel.set_game)
        self.overlay.panel.set_game(self.library.panel._selected_game())
        self.stack.addWidget(self.library)
        self.stack.addWidget(self.overlay)
        self.library.open_history.connect(self.open_history)
        self.tabs.currentChanged.connect(self.change_page)
        self.scan.import_requested.connect(self.import_afterburner)
        self.scan.verified.connect(self.set_verified)
        self.scan.show_profiles.connect(lambda: self.tabs.setCurrentIndex(1))
        if history:
            self.tabs.setCurrentIndex(2)
        self.browser.load(C.QUrl.fromLocalFile(str(build_preview())))

    def loaded(self, ok):
        self.ready = ok
        self.scan.import_button.setEnabled(ok)
        if ok:
            self.browser.page().runJavaScript("""
              window.pbQtPreview = true;
              const originalApplyGame=applyGameAdaptive;
              window.applyGameAdaptive=function(){
                  originalApplyGame();
                  if(!dlg().open && tab==='Game Library') console.info('pb-qt-preview-adaptive:'+idx+':'+g().target);
              };

              const style=document.createElement('style');
              style.textContent='header.app,nav.nav,.docfoot{display:none}main{padding:16px 20px;max-width:none}';
              document.head.appendChild(style);
              document.addEventListener('click', event => {
                  const b = event.target.closest('button');
                  if(b?.id==='back'){
                      event.stopImmediatePropagation();
                      console.info('pb-qt-preview-back');
                      return;
                  }
                  if (b && b.textContent.trim()==='Setup Auto Undervolt') {
                      event.stopImmediatePropagation();
                      console.info('pb-qt-preview-open-setup');
                  }
              }, true);
            """)
            self.change_page(self.tabs.currentIndex())
            if self.start_history and self.tabs.currentIndex() == 2:
                self.start_history = False
                self.open_history(0)

    def open_editor(self, kind, index):
        def got_points(encoded):
            if not encoded:
                return
            data = json.loads(encoded)
            anchor_voltage = data["anchorVoltage"]
            points = [(round(x), round(y)) for x, y in data["points"]]
            if points:
                # Leave the renderer callback before opening the modal Qt editor.
                C.QTimer.singleShot(
                    0, lambda: self.edit_points(kind, index, anchor_voltage, points)
                )

        self.browser.page().runJavaScript(
            f"(profileMeta[{index}] && JSON.stringify({{anchorVoltage: profiles[{index}][1], points:profileCurve({json.dumps(kind)},{index})}}))",
            got_points,
        )

    def edit_points(self, kind, index, anchor_voltage, points):
        def saved(values):
            javascript = (
                f"saveEditedCurve({json.dumps(kind)}, {index}, {json.dumps(values)})"
            )
            self.browser.page().runJavaScript(javascript)
            return "Saved in this mock session."

        if kind == "fan":
            with patch.object(fan_curve_editor, "CurvePlot", DemoCurvePlot):
                open_fan_curve_editor_dialog(
                    QtCore=C,
                    QtGui=G,
                    QtWidgets=W,
                    pg=pg,
                    parent=self,
                    curve_points=points,
                    measured_points=[],
                    target_point=points[len(points) // 2],
                    save_callback=lambda edit: saved(edit.points),
                )
            return

        reference = BASE["points"]
        plan = []
        for i, (mv, mhz) in enumerate(points):
            base = round(float(np.interp(mv, BASE_VOLTAGES, BASE_CLOCKS)))
            plan.append(
                dict(
                    index=i,
                    voltage_mv=mv,
                    base_mhz=base,
                    target_mhz=mhz,
                    new_offset_mhz=mhz - base,
                )
            )
        target_mv = anchor_voltage or points[len(points) // 2][0]
        anchor = min(points, key=lambda point: abs(point[0] - target_mv))
        with patch.object(vf_curve_editor, "CurvePlot", DemoCurvePlot):
            open_vf_curve_editor_dialog(
                QtCore=C,
                QtGui=G,
                QtWidgets=W,
                pg=pg,
                parent=self,
                plan=plan,
                anchor=anchor,
                base_points=reference,
                save_callback=lambda edit: saved(
                    [[point["voltage_mv"], point["target_mhz"]] for point in edit.plan]
                ),
            )

    def on_scan_started(self):
        if not self.sample_runtime.isValid():
            self.sample_runtime.start()
        self.tabs.setCurrentIndex(0)

    def sample_energy_status(self, **_):
        if not self.sample_runtime.isValid():
            return {}
        # A simulated active profile saving 45 W. The native About dialog
        # formats the totals and requests a fresh sample once a second.
        seconds = 1 + self.sample_runtime.elapsed() // 1000
        return {
            "energy_savings": {
                "active_seconds": seconds,
                "saved_watt_seconds": 45 * seconds,
            }
        }

    def show_about(self):
        links = about.ABOUT_LINKS_HTML.replace(
            "<a href=", f'<a style="color:{COL["accent"]}" href='
        )
        with (
            patch(
                "runtime.daemon_client.daemon_status",
                side_effect=self.sample_energy_status,
            ),
            patch.object(about, "ABOUT_LINKS_HTML", links),
        ):
            about.show_about_dialog(QtCore=C, QtGui=G, QtWidgets=W, parent=self)

    def import_afterburner(self):
        if not self.ready:
            return
        example = HERE / "fixtures" / "Afterburner"
        with (
            patch.object(afterburner_import, "CurvePlot", DemoCurvePlot),
            patch.object(
                afterburner_import,
                "configured_afterburner_root",
                return_value=str(example),
            ),
        ):
            entry = afterburner_import.select_afterburner_import(
                QtCore=C, QtGui=G, QtWidgets=W, pg=pg, parent=self
            )
        if not entry:
            return
        # Parsing the selected directory is read-only. The UI preview stores
        # the selected slot in its own temporary profile list.
        fan_payload = afterburner_fan_curve_payload(entry["afterburner_root"])
        imported = {
            "device": entry["device_profile_name"],
            "section": entry["section"],
            "mv": entry["target_voltage_mv"],
            "clock": entry["target_clock_mhz"],
            "points": afterburner_import.entry_curve_points(entry),
            "fan": fan_payload["fan"]["curve"] if fan_payload else None,
        }
        self.browser.page().runJavaScript(
            f"if(dlg().open)closePanel();saveImportedProfile({json.dumps(imported)});render()"
        )
        self.tabs.setCurrentIndex(1)

    def open_history(self, game_index):
        if not self.ready:
            return
        self.stack.setCurrentWidget(self.browser)
        self.browser.page().runJavaScript(
            f"navigate('Game Library');selectGame({int(game_index)});openReport();"
            "requestAnimationFrame(()=>requestAnimationFrame(()=>window.scrollTo(0,0)))"
        )

    def back_to_library(self):
        self.stack.setCurrentWidget(self.library)
        self.library.return_from_history()

    def apply_game_adaptive(self, game_index, target_fps):
        self.tabs.setCurrentIndex(2)
        self.stack.setCurrentWidget(self.library)
        self.library.set_adaptive_available(True)
        self.library.apply_adaptive(game_index, target_fps)

    def setup_from_profile(self):
        self.tabs.setCurrentIndex(0)
        if self.scan.setup_button.isEnabled():
            C.QTimer.singleShot(0, self.scan.setup)

    def set_verified(self, tiers):
        self.verified = list(dict.fromkeys(self.verified + tiers))
        if self.ready:
            self.library.set_adaptive_available(bool(self.verified))
            candidates = [
                [
                    tier,
                    self.scan.chosen[tier],
                    (self.scan.options or {}).get(
                        adaptive_tier_option_key(tier.lower(), "memory_offset_mhz")
                        if self.scan.full
                        else "auto_uv_memory_offset_mhz",
                        0,
                    )
                    / 2,
                    self.scan.verify_minutes[tier],
                ]
                for tier in tiers
            ]
            self.browser.page().runJavaScript(
                f"{{const added={json.dumps(candidates)}.map(args=>saveVerifiedCandidate(...args));"
                "pi=added[0];useExamples=false;render()}"
            )

    def change_page(self, i):
        self.stack.setCurrentWidget(
            (self.scan, self.browser, self.library, self.overlay)[i]
        )
        if not self.ready:
            return
        if i == 1:
            selected, targets = self.library.adaptive_picker_state()
            self.browser.page().runJavaScript(
                f"{json.dumps(targets)}.forEach((fps,i)=>games[i].target=fps);"
                f"idx={json.dumps(selected)} ?? idx;navigate('Profiles')"
            )
        elif i == 2:
            self.browser.page().runJavaScript(
                "adaptiveTiers().length>0",
                lambda enabled: self.library.set_adaptive_available(bool(enabled)),
            )

    def closeEvent(self, event):
        self.scan.timer.stop()
        super().closeEvent(event)


def configure_app(app):
    app.setApplicationName("PenguinBurner UX preview")
    apply_dark_palette(app, G)
    install_theme(app)


def main():
    app = W.QApplication(sys.argv)
    configure_app(app)
    win = Preview(history="--history" in sys.argv)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
