#!/usr/bin/env python3
"""PenguinBurner Qt UI mock. Every tab is a native widget over sample sources;
Auto UV replays an in-memory scan fixture."""

import copy
import html
import json
import math
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, TypedDict
from unittest.mock import patch

# Use the active checkout's widgets even when a different PenguinBurner wheel is installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import design
import pyqtgraph as pg
from design import (
    COL,
    COPY,
    DIM_ALPHA,
    GAP,
    RAD,
    TIER_COLORS,
    TYPE,
    WEIGHT,
    ChartLegend,
    DemoCurvePlot,
    focus_visible,
    glyph_icon,
    install_theme,
    qcolor,
    tab_icon,
)
from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W
from qt.components.adaptive_game_picker import AdaptiveGame, AdaptiveGamePicker
from qt.components.elided_label import ElidedLabel
from qt.components.page_header import PAGE_MARGINS, PageHeader
from qt.components.profile_model import STOCK
from qt.components.table_style import apply_table_spec
from qt.features.appearance_dialog import AppearanceDialog
from qt.features.perf_profile_page import PerfProfilePage
from qt.features.profiles_page import CurveEditors, ProfilesPage
from qt.sample.perf_rings import LIVE_KEY, SamplePerfSource
from qt.sample.profiles_source import SampleProfilesSource
from qt.sample.prototype_controls import PrototypeControlsDialog, PrototypeState

from auto_uv.scan_mode.auto_uv_mode import adaptive_tier_option_key
from integrations.afterburner.profile_import import afterburner_fan_curve_payload
from ui.assets import asset_image_path
from ui.components import fan_curve_editor, vf_curve_editor
from ui.dialogs import about, afterburner_import, scan_tuning
from ui.dialogs.error_details import process_failure_details, show_error_dialog
from ui.dialogs.final_choice import select_final_candidate
from ui.features.tuning.tuning import AutoUvNvmlInfo
from ui.qt import apply_dark_palette

HERE = Path(__file__).resolve().parent
SAMPLE = json.loads((HERE / "sample.json").read_text())
GPU = "NVIDIA GeForce RTX 5080"
TIERS = ("Efficiency", "Balanced", "Performance")


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
BASE_VOLTAGES, BASE_CLOCKS = zip(*BASE["points"], strict=True)
PAGES = ("Auto UV", "Profiles", "Game Library", "In-Game Overlay")
# Accelerated replay: ticks of the 250 ms timer per probe / verification.
PROBE_TICKS, FINAL_TICKS, PROFILE_TICKS = 8, 12, 16
SHORT_PROBE_S = 30
# auto_uv/run/performance_comparison.py: window and the matched runs.
COMPARISON_WINDOW_PCT = 2.0
COMPARISON_RUNS = 2
COMPARISON_CUDA_S = 5
LOG_LIMIT = 6000
# auto_uv/final_verification defaults: Efficiency 1 min, Balanced 3 min, Performance 5 min.
FINAL_MINUTES = {"Efficiency": 1, "Balanced": 3, "Performance": 5}
MODE_TO_TIER = {
    "efficiency": "Efficiency",
    "balanced": "Balanced",
    "performance": "Performance",
    "safe": "Efficiency",
    "normal": "Balanced",
    "aggressive": "Performance",
}
# Failure facts for the sample's failed probes, worded like ui/models.py.
PROBE_FAILURES = {
    ("Efficiency", 840, 2430): dict(
        label="Clock too low",
        kind="low-clock",
        severity="recoverable",
        reason="core clock 2402 MHz fell below the 2420 MHz floor",
    ),
    ("Balanced", 840, 2585): dict(
        label="Average FPS low",
        kind="fps-regression",
        severity="recoverable",
        reason="average FPS 62.30 is 1.7% below stock (limit 1.5%)",
    ),
    ("Performance", 920, 3010): dict(
        label="Nvidia Xid fail",
        kind="nvidia-xid",
        severity="critical",
        reason="NVRM: Xid 13, graphics exception during the probe",
    ),
}
FINAL_FAILURE = dict(
    label="Vulkan device lost",
    kind="fatal-output",
    severity="critical",
    reason="final verification ended early",
    fatal=["VK_ERROR_DEVICE_LOST"],
)
TIER_STATE_LABELS = {
    "pending": "Pending",
    "active": "Scanning",
    "complete": "✓ Complete",
    "skipped": "Skipped",
    "stopped": "Stopped",
    "failed": "Failed",
    "not-run": "Not run",
}
RUN_STATE_LABELS = {
    "running": "Running",
    "stopping": "Stopping",
    "stopped": "Stopped",
    "aborted": "Aborted",
    "discarded": "Discarded",
    "failed": "Failed",
}
OUTCOMES: dict[str, tuple[str, str, int]] = {
    "complete": ("Complete", "Final verification complete.", 0),
    "stopped": ("Stopped", "Auto-UV stopped.", 1),
    "aborted": ("Aborted", "Auto-UV aborted by user.", 1),
    "discarded": (
        "Discarded",
        "Final verification discarded. No Auto-UV profile was saved.",
        0,
    ),
    "failed": ("Error", "Auto-UV failed.", 1),
}
# Prototype controls (qt/sample/prototype_controls.py) drive these simulations.
PROTOTYPE_DEFAULTS = {
    "daemon": "ready",
    "twoGpus": False,
    "noGpu": False,
    "crashRecovery": False,
    "finalVerificationFailure": False,
    "dependencyDownload": False,
    "processFailure": False,
    "performanceComparison": False,
}


SECOND_GPU = "NVIDIA GeForce RTX 4070"
GPU_INFO = {
    0: AutoUvNvmlInfo(
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
    ),
    1: AutoUvNvmlInfo(
        power_management_enabled=True,
        power_limit_set_supported=True,
        power_limit_w=200,
        power_limit_default_w=200,
        power_limit_min_w=150,
        power_limit_max_w=220,
        graphics_clock_mhz=210,
        memory_clock_mhz=405,
        supported_memory_clocks_mhz=(10501,),
        supported_graphics_clock_steps_mhz=(210, 3105),
    ),
}


def setup_options(parent, *, gpu_count=1, no_gpu=False, gpu_index=0):
    """Show the app's setup dialog with fixed demo facts for every GPU query.

    Two cards exercise the real GPU selector (re-read limits, re-ranged
    tiers); no card shows the dialog's disabled no-GPU state."""
    names = (GPU, SECOND_GPU)
    gpus = [
        SimpleNamespace(index=i, name=names[i], label=f"GPU {i} · {names[i]}")
        for i in range(0 if no_gpu else gpu_count)
    ]
    selected = min(int(gpu_index), max(0, len(gpus) - 1))

    def client(index=0, *_, **__):
        name = names[int(index)] if int(index) < len(names) else GPU
        return SimpleNamespace(
            capabilities=lambda: SimpleNamespace(identity=SimpleNamespace(name=name))
        )

    fixtures = {
        "gpu_choices_with_fallback": dict(return_value=(gpus, selected)),
        "DaemonGpuClient": dict(side_effect=client),
        "read_auto_uv_nvml_info": dict(
            side_effect=lambda index, **_: GPU_INFO.get(int(index), GPU_INFO[0])
        ),
        "auto_uv_voltage_floor_range_mv": dict(
            side_effect=lambda gpu_index=0, **_: (
                (725, 1240) if int(gpu_index or 0) == 0 else (700, 1100)
            )
        ),
        # Transfer-rate (MT/s) bounds: scan_tuning halves them, so the dialog
        # shows 0–2000 / 0–1500 MHz like the browser's GPU_FACTS.
        "memory_offset_mhz_range": dict(
            side_effect=lambda gpu_index=0, **_: (
                (0, 4000) if int(gpu_index or 0) == 0 else (0, 3000)
            )
        ),
    }

    def set_target_note():
        dialog = W.QApplication.activeModalWidget()
        if dialog:
            for note in dialog.findChildren(W.QLabel, "autoUvTargetCaution"):
                note.setText(COPY["targetCaution"])

    C.QTimer.singleShot(0, set_target_note)
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                scan_tuning, "GPU_UNDERVOLTING_PURPOSE_TEXT", COPY["setupNote"]
            )
        )
        for name, reply in fixtures.items():
            stack.enter_context(patch.object(scan_tuning, name, **reply))
        return scan_tuning.select_scan_tuning(
            QtCore=C, QtGui=G, QtWidgets=W, parent=parent, gpu_index=selected
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


ABOUT_AUTHORS = (
    ("Jan Pietek", "https://github.com/jpietek"),
    ("Michał Ernst (Ernold)", "https://github.com/Ernold11"),
)


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


def header_icon_button(icon, name, slot):
    """A flat, icon-only header utility (Appearance, About): same size and
    look for both, with the name as tooltip and accessible name."""
    tool = W.QToolButton()
    tool.setObjectName("headerUtility")
    tool.setIcon(icon)
    tool.setIconSize(C.QSize(18, 18))
    tool.setFixedSize(34, 34)
    tool.setToolTip(name)
    tool.setAccessibleName(name)
    tool.clicked.connect(slot)
    return tool


def info_icon(color):
    """A small outlined info circle for the header About button."""
    ratio = 3
    pixmap = G.QPixmap(16 * ratio, 16 * ratio)
    pixmap.fill(C.Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(ratio)
    painter = G.QPainter(pixmap)
    painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
    ink = G.QColor(color)
    painter.setPen(G.QPen(ink, 1.4))
    painter.drawEllipse(C.QRectF(1.5, 1.5, 13, 13))
    stem = G.QPen(ink, 1.5)
    stem.setCapStyle(C.Qt.PenCapStyle.RoundCap)
    painter.setPen(stem)
    painter.drawLine(C.QPointF(8, 7.2), C.QPointF(8, 11.2))
    painter.setPen(C.Qt.PenStyle.NoPen)
    painter.setBrush(ink)
    painter.drawEllipse(C.QPointF(8, 4.9), 0.95, 0.95)
    painter.end()
    return G.QIcon(pixmap)


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
PROGRESS_ROLE = DELTA_ROLE + 1
TONE_ROLE = DELTA_ROLE + 2
RESULT_ROLE = DELTA_ROLE + 3
FINAL_ROLE = DELTA_ROLE + 4


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
        if col in (11, 12):
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
                painter.setPen(G.QColor(delta_color(col, change)))
                painter.drawText(delta_rect, align, f"{change:+.1f}%")
            painter.restore()
        if col == 0:
            # Selected rows carry the 2 px accent bar; final verification
            # rows a 2 px border bar, as in the browser.
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
        if index.column() == 11:
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


class StatusDot(W.QWidget):
    """Stage dot: accent while running, gain/warning/loss for outcomes."""

    TONES: ClassVar[dict[str, str]] = {
        "running": "accent",
        "good": "gain",
        "warning": "warning",
        "error": "loss",
        "idle": "quiet",
    }

    def __init__(self):
        super().__init__()
        self.tone = "idle"
        self.setFixedSize(10, 10)

    def set_tone(self, tone):
        self.tone = tone if tone in self.TONES else "idle"
        self.update()

    def paintEvent(self, _event):
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        painter.setPen(C.Qt.PenStyle.NoPen)
        painter.setBrush(qcolor(self.TONES[self.tone]))
        painter.drawEllipse(C.QRectF(1, 1, 8, 8))
        painter.end()


class TierStrip(W.QWidget):
    """Full scan progress: three segments with a 3 px state bar, as in the browser."""

    def __init__(self):
        super().__init__()
        self.setObjectName("autoUvTierProgress")
        self.states = dict.fromkeys(TIERS, "pending")
        self.final_tier = None
        self.font_title = G.QFont(TYPE["family"])
        self.font_title.setPixelSize(TYPE["caption"])
        self.font_bold = G.QFont(self.font_title)
        self.font_bold.setWeight(G.QFont.Weight.DemiBold)
        self.setFixedHeight(32)
        self.hide()

    def set_states(self, states, final_tier=None):
        self.states = {tier: states.get(tier, "pending") for tier in TIERS}
        self.final_tier = final_tier
        self.setAccessibleName(
            "Full scan progress: "
            + ", ".join(f"{t} {self.label_for(t)}" for t in TIERS)
        )
        self.update()

    def label_for(self, tier):
        state = self.states[tier]
        if state == "active" and self.final_tier == tier:
            return "Final verification"
        return TIER_STATE_LABELS[state]

    def paintEvent(self, _event):
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        metrics = G.QFontMetrics(self.font_title)
        title_w = metrics.horizontalAdvance("Full scan") + GAP["md"]
        painter.setFont(self.font_title)
        painter.setPen(qcolor("secondary"))
        text_rect = C.QRect(0, 0, title_w, self.height() - 6)
        painter.drawText(
            text_rect,
            C.Qt.AlignmentFlag.AlignLeft | C.Qt.AlignmentFlag.AlignVCenter,
            "Full scan",
        )
        gap = GAP["md"]
        width = (self.width() - title_w - 2 * gap) / 3
        for i, tier in enumerate(TIERS):
            state = self.states[tier]
            x = title_w + i * (width + gap)
            seg = C.QRectF(x, 0, width, self.height())
            bar = C.QRectF(x, self.height() - 3, width, 3)
            color = {
                "active": qcolor(TIER_COLORS[tier]),
                "complete": qcolor(TIER_COLORS[tier], 0.7),
                "stopped": qcolor("warning"),
                "failed": qcolor("loss"),
            }.get(state, qcolor("border" if state == "skipped" else "divider"))
            if state == "skipped":
                pen = G.QPen(color, 3, C.Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.drawLine(
                    C.QPointF(bar.left(), bar.center().y()),
                    C.QPointF(bar.right(), bar.center().y()),
                )
            else:
                painter.fillRect(bar, color)
            text = seg.adjusted(2, 0, -2, -6)
            painter.setFont(self.font_bold)
            painter.setPen(qcolor("quiet" if state == "not-run" else "ink"))
            painter.drawText(
                text,
                C.Qt.AlignmentFlag.AlignLeft | C.Qt.AlignmentFlag.AlignVCenter,
                f"{i + 1}. {tier}",
            )
            painter.setFont(self.font_title)
            painter.setPen(
                qcolor(
                    {
                        "active": "ink",
                        "complete": "gain",
                        "stopped": "warning",
                        "failed": "loss",
                    }.get(
                        state,
                        "quiet" if state in ("skipped", "not-run") else "secondary",
                    )
                )
            )
            painter.drawText(
                text,
                C.Qt.AlignmentFlag.AlignRight | C.Qt.AlignmentFlag.AlignVCenter,
                self.label_for(tier),
            )
        painter.end()


def mixed(token, amount, base="panel"):
    """Token colour blended over a surface, like CSS color-mix()."""
    top, under = qcolor(token), qcolor(base)
    return G.QColor(
        round(under.red() + (top.red() - under.red()) * amount),
        round(under.green() + (top.green() - under.green()) * amount),
        round(under.blue() + (top.blue() - under.blue()) * amount),
    )


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


def nice_step(span, count):
    """charts.js niceStep: a 1/2/5 x 10^n step so ticks are round numbers."""
    raw = abs(span) / max(1, count)
    power = 10 ** math.floor(math.log10(raw or 1))
    unit = raw / power
    return (1 if unit <= 1 else 2 if unit <= 2 else 5 if unit <= 5 else 10) * power


class ScanPlot(DemoCurvePlot):
    """The Auto UV V/F plot: browser tick steps, and a selection that also
    dims the live candidate."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not hasattr(self, "plot"):
            return
        # plotVF: y major = niceStep(span, 8) with halves; x major follows the
        # width (one label per ~110 px) with fifths.
        self.plot.getAxis("left").tickSpacing = lambda lo, hi, size: [
            (major := nice_step(hi - lo, 8), 0),
            (major / 2, 0),
        ]
        self.plot.getAxis("bottom").tickSpacing = lambda lo, hi, size: [
            (major := nice_step(hi - lo, max(3, round(size / 110))), 0),
            (major / 5, 0),
        ]

    def _apply_curve_highlights(self):
        super()._apply_curve_highlights()
        if self.candidate_curve is not None:
            self.candidate_curve.setOpacity(DIM_ALPHA if self._emphasis() else 1.0)


class ExternalLegendPlot(DemoCurvePlot):
    """DemoCurvePlot without pyqtgraph's in-plot legend; the dialog polish
    adds a ChartLegend row under the plot instead."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        legend = getattr(self, "plot", None) and self.plot.getPlotItem().legend
        if legend is not None:
            legend.hide()


def fit_afterburner_table(table):
    """Device names up to "&SUBSYS" (the full name stays in the tooltip),
    content-width columns and a stretching, eliding Status column."""
    last = table.columnCount() - 1
    for row in range(table.rowCount()):
        # Regular weight throughout; rows that cannot be imported read quiet.
        for column in range(table.columnCount()):
            cell = table.item(row, column)
            if cell is None:
                continue
            font = cell.font()
            font.setBold(False)
            cell.setFont(font)
            if cell.foreground().style() != C.Qt.BrushStyle.NoBrush:
                cell.setForeground(qcolor("quiet"))
        item = table.item(row, 0)
        if item is not None and "&SUBSYS" in item.text():
            item.setToolTip(item.text())
            item.setText(item.text().split("&SUBSYS")[0])
        status = table.item(row, last)
        if status is not None:
            status.setToolTip(status.text())
    table.resizeColumnsToContents()
    header = table.horizontalHeader()
    # stretchLastSection keeps the content width as a floor; Stretch alone
    # gives Status the remaining width and elides it.
    header.setStretchLastSection(False)
    header.setSectionResizeMode(last, W.QHeaderView.ResizeMode.Stretch)


def polish_afterburner_import():
    """The production chooser, laid out like the browser's: the table gets
    about 55% of the width, short device names, left-aligned headers with
    Status visible, a text Choose directory… button, an external legend and
    Cancel before Import."""
    dialog = W.QApplication.activeModalWidget()
    if not isinstance(dialog, W.QDialog):
        return
    layout = dialog.layout()
    table = dialog.findChild(W.QTableWidget)
    splitter = dialog.findChild(W.QSplitter)
    if table is not None:
        table.horizontalHeader().setDefaultAlignment(
            C.Qt.AlignmentFlag.AlignLeft | C.Qt.AlignmentFlag.AlignVCenter
        )
        table.setTextElideMode(C.Qt.TextElideMode.ElideRight)
        apply_table_spec(table)
        fit_afterburner_table(table)
        # Choosing another directory refills the table.
        table.model().rowsInserted.connect(
            lambda *_: C.QTimer.singleShot(0, lambda: fit_afterburner_table(table))
        )
    if splitter is not None:
        width = max(splitter.width(), dialog.width() - 24)
        splitter.setSizes([round(width * 0.55), round(width * 0.45)])
        plot = splitter.findChild(pg.PlotWidget)
        if plot is not None:
            index = splitter.indexOf(plot)
            pane = W.QWidget()
            column = W.QVBoxLayout(pane)
            # Clear of the splitter grip beside the rotated axis title.
            column.setContentsMargins(GAP["sm"], 0, 0, 0)
            column.setSpacing(GAP["xs"])
            column.addWidget(plot, 1)
            legend = ChartLegend()
            legend.set_items([("Imported", "candidateCurve", "solid")])
            column.addWidget(legend)
            splitter.insertWidget(index, pane)
    for browse in dialog.findChildren(W.QToolButton):
        if browse.accessibleName() == "Choose Afterburner Directory":
            browse.setIcon(G.QIcon())
            browse.setText("Choose directory…")
            browse.setToolButtonStyle(C.Qt.ToolButtonStyle.ToolButtonTextOnly)
    box = dialog.findChild(W.QDialogButtonBox)
    cancel = box.button(W.QDialogButtonBox.StandardButton.Cancel) if box else None
    if isinstance(layout, W.QBoxLayout) and box is not None and cancel is not None:
        # QDialogButtonBox orders by platform; the preview puts Cancel first.
        cancel.hide()
        row = W.QHBoxLayout()
        row.addStretch(1)
        proxy = W.QPushButton("Cancel")
        proxy.clicked.connect(dialog.reject)
        row.addWidget(proxy)
        layout.removeWidget(box)
        row.addWidget(box)
        layout.addLayout(row)
        for button in box.buttons():
            if isinstance(button, W.QPushButton) and button.text() == "Import":
                button.setObjectName("nextAction")
                button.setDefault(True)
                repolish(button)


class EditorCurvePlot(ExternalLegendPlot):
    """The production curve editors' plot, drawn like the browser editors:
    accent draft, measured samples as dots, °C and "Fan speed" axis titles,
    and no in-plot legend (polish_curve_editor adds an external one)."""

    def __init__(self, **kwargs):
        if kwargs.get("x_units") == "C":
            kwargs["x_units"] = "°C"
        if kwargs.get("y_label") == "Fan":
            kwargs["y_label"] = "Fan speed"
        self.measured = kwargs.get("source_name") == "Measured"
        super().__init__(**kwargs)
        if self.candidate_curve is None:
            return
        self._candidate_color = COL["accent"]
        self.candidate_curve.setPen(self._candidate_curve_pen())
        self.candidate_curve.setSymbolBrush(qcolor("accent"))
        self.candidate_curve.setSymbolPen(self.pg.mkPen(COL["accent"]))
        if self.measured and self.source_curve is not None:
            self.source_curve.setPen(None)
            self.source_curve.setSymbol("o")
            self.source_curve.setSymbolSize(4)
            self.source_curve.setSymbolPen(None)
            self.source_curve.setSymbolBrush(qcolor("quiet"))

    def _candidate_curve_pen(self):
        return self.pg.mkPen(COL["accent"], width=2.2)


def polish_curve_editor(kind):
    """Bring the open production editor in line with the browser's: an
    external legend under the plot, the shortcut keys collapsed, and the
    footer as Cancel then a primary "Save edited copy" without an icon."""
    dialog = W.QApplication.activeModalWidget()
    if not isinstance(dialog, W.QDialog):
        return
    plot = dialog.findChild(pg.PlotWidget)
    layout = dialog.layout()
    if not isinstance(layout, W.QBoxLayout):
        return
    if plot is not None:
        legend = ChartLegend()
        legend.set_items(
            [
                ("Measured", "quiet", "dot"),
                ("Before edit", "curveHistory", "solid"),
                ("Edited draft", "accent", "solid"),
            ]
            if kind == "fan"
            else [
                ("Reference", "baseCurve", "dashed"),
                ("Before edit", "curveHistory", "solid"),
                ("Edited draft", "accent", "solid"),
            ]
        )
        layout.insertWidget(layout.indexOf(plot) + 1, legend)
        if kind == "fan":
            # Room for the 100% points and their rings at the plot edges,
            # with a 100 tick on the fan axis.
            view = plot.getViewBox()
            if view is not None:
                view.setRange(xRange=(30 - 1.3, 95 + 1.3), yRange=(-2, 104), padding=0)
            plot.getAxis("left").setTicks(
                [[(v, str(v)) for v in range(0, 101, 20)]]
            )
            plot.getAxis("bottom").setTicks(
                [[(v, str(v)) for v in range(30, 96, 10)]]
            )
        # The editor's floating key legend covered the first x tick: it opens
        # from a Keys button in the external legend row instead.
        for frame in dialog.findChildren(W.QFrame):
            if frame.objectName().endswith("ShortcutLegend"):
                frame.setStyleSheet(
                    frame.styleSheet() + f"QLabel{{font-size:{TYPE['caption']}px}}"
                )
                for toggle in frame.findChildren(W.QToolButton):
                    if toggle.objectName().endswith("Toggle") and toggle.text() == "+":
                        toggle.click()  # expanded whenever it is shown
                frame.hide()
                keys = W.QPushButton("Keys")
                keys.setObjectName("curveKeys")
                keys.setCheckable(True)
                keys.setToolTip("Show curve editor shortcuts")
                keys.toggled.connect(frame.setVisible)
                keys.toggled.connect(lambda _on, f=frame: f.raise_())
                legend.add_widget(keys)
    buttons = {b.text(): b for b in dialog.findChildren(W.QPushButton)}
    save, cancel = buttons.get("Save"), buttons.get("Cancel")
    if save is None or cancel is None:
        return
    save.setText("Save edited copy")
    save.setIcon(G.QIcon())
    save.setObjectName("nextAction")
    save.setDefault(True)
    rows = (layout.itemAt(i) for i in range(layout.count()))
    row = next(
        (
            box_row
            for item in rows
            if item is not None
            and isinstance(box_row := item.layout(), W.QBoxLayout)
            and box_row.indexOf(save) >= 0
        ),
        None,
    )
    if row is not None:
        row.removeWidget(cancel)
        row.insertWidget(row.indexOf(save), cancel)
    repolish(save)


def polish_about():
    """Links in the About dialog are links, not a text field: no caret after a
    link, and OK holds focus."""
    dialog = W.QApplication.activeModalWidget()
    if not isinstance(dialog, W.QDialog):
        return
    for text in dialog.findChildren(W.QLabel):
        if text.openExternalLinks():
            text.setTextInteractionFlags(
                C.Qt.TextInteractionFlag.LinksAccessibleByMouse
                | C.Qt.TextInteractionFlag.LinksAccessibleByKeyboard
            )
    box = dialog.findChild(W.QDialogButtonBox)
    ok = box.button(W.QDialogButtonBox.StandardButton.Ok) if box else None
    if ok is not None:
        ok.setFocus(C.Qt.FocusReason.OtherFocusReason)


def repolish(widget):
    """Re-apply the stylesheet after an objectName change."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


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


class ScanPage(W.QWidget):
    """Native Auto UV page. A simulated backend replays sample.json through the
    per-tier sequence of auto_uv/main_loop.py: each tier's descent opens the
    real candidate dialog, runs that tier's final verification and saves the
    profile before the next tier starts."""

    show_profiles = C.Signal()
    import_requested = C.Signal()
    show_scan = C.Signal()
    # tier, row, memory MHz, verification minutes, scanned GPU index
    profile_saved = C.Signal(str, object, float, int, int)
    # Emitted once setup is accepted, before the scan resets the GPU.
    scan_starting = C.Signal()
    scan_completed = C.Signal()
    status_changed = C.Signal(object)
    progress_changed = C.Signal(object, int, str)
    prototype_consumed = C.Signal(str)
    daemon_ready = C.Signal()
    profile_verified = C.Signal(int, bool)

    def __init__(self):
        super().__init__()
        self.stage = "idle"
        self.phase = ""
        self.options = None
        self.full = True
        self.prototype = dict(PROTOTYPE_DEFAULTS)
        self.plan: list[ScanRun] = []
        # Plan indexes whose numbers are two-run comparison means.
        self.compared: set[int] = set()
        self.runs: list[dict[str, Any]] = []
        self.done = self.ticks = 0
        self.active_run = -1
        self.tier_state: dict[str, str] = {}
        self.tier_saved: dict[str, dict[str, Any]] = {}
        self.tier_traces = {}
        # Empty while nothing of that kind is running or resumable.
        self.final_job: dict[str, Any] = {}
        self.profile_job: dict[str, Any] = {}
        self.last_scan: dict[str, Any] = {}
        self.resume_tier: str | None = None
        self.failed_once = self.any_discarded = False
        self.download_pct = 0
        self.header_stage: str = ""
        self.candidate_text: str = ""
        self.status_text: str = ""
        self.log_lines = []
        self.gpu_index = 0
        self.import_ready = True
        self.live_mv, self.live_mhz = 0, 0.0
        self.timer = C.QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.tick)

        page = W.QVBoxLayout(self)
        page.setContentsMargins(*PAGE_MARGINS)
        page.setSpacing(GAP["md"])
        self.header = PageHeader("Auto UV", "GPU 0 · " + GPU)
        self.gpu_caption = self.header.subtitle
        page.addWidget(self.header)
        self.setup_button = button("Setup Auto Undervolt", self.setup, "setupScan")
        self.stop_button = button("Stop", self.stop)
        self.import_button = button("Import Afterburner", self.import_requested.emit)

        # Card 1: status, tier strip, then plot | log (as the browser's panel).
        self.scan_card = W.QFrame()
        self.scan_card.setObjectName("uvCard")
        card = W.QVBoxLayout(self.scan_card)
        card.setContentsMargins(1, 1, 1, 1)
        card.setSpacing(0)
        head = W.QVBoxLayout()
        head.setContentsMargins(GAP["lg"] + 4, GAP["lg"], GAP["lg"] + 4, GAP["md"])
        head.setSpacing(GAP["md"])
        card.addLayout(head)
        status = W.QVBoxLayout()
        status.setSpacing(2)
        stage_row = W.QHBoxLayout()
        stage_row.setSpacing(GAP["sm"])
        self.stage_dot = StatusDot()
        stage_row.addWidget(self.stage_dot)
        self.stage_label = label("", "sectionTitle")
        stage_row.addWidget(self.stage_label)
        self.candidate_label = label("", "uvCandidate")
        self.candidate_label.setTextInteractionFlags(
            C.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.candidate_label.setToolTip(
            "Current candidate. Select or right-click to copy."
        )
        stage_row.addWidget(self.candidate_label)
        stage_row.addStretch()
        status.addLayout(stage_row)
        self.stage_detail = label("", "caption")
        self.stage_detail.setSizePolicy(
            W.QSizePolicy.Policy.Ignored, W.QSizePolicy.Policy.Preferred
        )
        status.addWidget(self.stage_detail)
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
        # Full scrollable log beside the plot, like the app's log view.
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
        self.plot_log.setStretchFactor(0, 2)
        self.plot_log.setStretchFactor(1, 1)
        self.plot_log.setSizes([720, 380])
        self.plot_stack.addWidget(self.plot_log)
        card.addWidget(self.plot_stack, 1)
        self.splitter = W.QSplitter(C.Qt.Orientation.Vertical)
        self.splitter.setObjectName("uvCards")
        self.splitter.setHandleWidth(GAP["md"])
        self.splitter.addWidget(self.scan_card)

        # Card 2: the runs table.
        table_frame = W.QFrame()
        table_frame.setObjectName("uvCard")
        layout = W.QVBoxLayout(table_frame)
        layout.setContentsMargins(1, GAP["lg"], 1, 1)
        layout.setSpacing(2)
        for widget in (
            label("Undervolting runs", "sectionTitle"),
            runs_meta := label("", "caption"),
        ):
            widget.setContentsMargins(GAP["lg"] + 3, 0, GAP["lg"] + 3, 0)
            layout.addWidget(widget)
        self.runs_meta = runs_meta
        layout.addSpacing(GAP["md"])
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
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(
            W.QHeaderView.ResizeMode.Interactive
        )
        header_font = G.QFont(TYPE["family"])
        header_font.setPixelSize(TYPE["table"])
        header_metrics = G.QFontMetrics(header_font)
        for col, width in enumerate(
            [34, 52, 75, 68, 94, 100, 101, 80, 65, 96, 111, 78, 136]
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
        self.table.setMinimumHeight(3 * 32 + 47 + 16)
        layout.addWidget(self.table)
        # Empty state, centred over the table body like the browser's.
        self.runs_empty = label("No runs yet", "caption")
        self.runs_empty.setParent(self.table.viewport())
        self.runs_empty.setAlignment(C.Qt.AlignmentFlag.AlignCenter)
        self.table.viewport().installEventFilter(self)
        self.splitter.addWidget(table_frame)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([440, 300])
        page.addWidget(self.splitter, 1)
        self.setStyleSheet(focus_visible(scan_page_stylesheet()))
        self.refresh()

    # ---------- shared helpers ----------

    def eventFilter(self, watched, event):
        if watched is self.table.viewport() and event.type() == C.QEvent.Type.Resize:
            self.runs_empty.setGeometry(watched.rect())
        return super().eventFilter(watched, event)

    def fit_plot(self, rows=None):
        """The browser's plotDataBounds: data x range, y padded 6% to 100 MHz."""
        curves = [BASE["points"], *(r["points"] for r in (rows or self.plan[1:]))]
        xs = [v for points in curves for v, _ in points]
        ys = [m for points in curves for _, m in points]
        pad = (max(ys) - min(ys)) * 0.06
        self.plot.plot.setRange(
            xRange=(min(xs), max(xs)),
            yRange=(
                math.floor((min(ys) - pad) / 100) * 100,
                math.ceil((max(ys) + pad) / 100) * 100,
            ),
            padding=0,
        )

    def busy(self):
        return self.stage in ("running", "verifying")

    def log(self, message):
        line = f"[{C.QTime.currentTime().toString('HH:mm:ss')}] {message}"
        self.log_lines.append(line)
        del self.log_lines[:-LOG_LIMIT]
        self.log_view.appendPlainText(line)

    def set_prototype(self, state):
        """Simulation switches from the browser's prototype controls."""
        self.prototype.update(
            {k: v for k, v in state.items() if k in PROTOTYPE_DEFAULTS}
        )

    def consume(self, key):
        self.prototype[key] = False
        self.prototype_consumed.emit(key)

    def tier_minutes(self, tier):
        return FINAL_MINUTES.get(tier, 5)

    def memory_mhz(self, tier):
        options = self.options or {}
        key = (
            adaptive_tier_option_key(tier.lower(), "memory_offset_mhz")
            if self.full
            else "auto_uv_memory_offset_mhz"
        )
        return float(options.get(key) or 0) / 2

    def ensure_daemon_ready(self, action_label="Setting up Auto-UV"):
        """Simulated scan-stream-v1 service check before a privileged action
        (ui/daemon_setup.py ensure_daemon_ready_for_privileged_action):
        install or update on Yes; declining aborts the action."""
        state = self.prototype.get("daemon", "ready")
        if state == "ready":
            return True
        missing = state == "missing"
        box = W.QMessageBox(self)
        box.setIcon(W.QMessageBox.Icon.Question)
        box.setWindowTitle("PenguinBurner hardware service")
        box.setText(
            f"{action_label} needs the PenguinBurner root hardware service. "
            + (
                "It is not installed."
                if missing
                else "The running service does not support scan streaming."
            )
        )
        box.setInformativeText(
            f"PenguinBurner can {'install' if missing else 'update'} it now. "
            "This may ask for your administrator password once."
        )
        go = box.addButton(
            "Install service" if missing else "Update service",
            W.QMessageBox.ButtonRole.AcceptRole,
        )
        box.addButton(W.QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(go)
        box.exec()
        if box.clickedButton() is not go:
            return False
        self.prototype["daemon"] = "ready"
        self.daemon_ready.emit()
        return True

    # ---------- scan lifecycle ----------

    def setup(self):
        if self.busy() or not self.ensure_daemon_ready():
            return
        options = setup_options(
            self,
            gpu_count=2 if self.prototype.get("twoGpus") else 1,
            no_gpu=bool(self.prototype.get("noGpu")),
            gpu_index=self.gpu_index,
        )
        if options is None:
            return
        mode = options["auto_uv_mode"]
        self.full = mode == "adaptive"
        self.gpu_index = int(options.get("gpu_index", self.gpu_index) or 0)
        self.gpu_caption.setText(
            f"GPU {self.gpu_index} · " + (GPU if self.gpu_index == 0 else SECOND_GPU)
        )
        resume = bool(self.last_scan) and self.last_scan["options"] == options
        self.options = options
        self.scan_starting.emit()
        self.show_scan.emit()
        if resume:
            self.resume_scan()
        elif self.prototype.get("crashRecovery"):
            self.consume("crashRecovery")
            self.start_crash_recovery(mode)
        else:
            self.start_fresh_scan(mode)

    def fresh_plan(self, mode):
        tier = MODE_TO_TIER.get(mode, mode.title())
        plan = [BASE] + [r for r in SAMPLE["rows"] if self.full or r["tier"] == tier]
        if len(plan) == 1:
            raise ValueError(f"Unrecognized sample tier {mode!r}")
        return plan

    def reset_scan(self, mode):
        self.plan = self.fresh_plan(mode)
        self.compared = set()
        self.done = 0
        self.runs = []
        self.active_run = -1
        self.tier_state = dict.fromkeys(TIERS, "pending") if self.full else {}
        self.tier_saved = {}
        self.final_job = {}
        self.profile_job = {}
        self.failed_once = self.any_discarded = False
        self.last_scan = {}
        self.resume_tier = None
        self.plot.clear()
        self.tier_traces.clear()
        self.plot.set_source_points(BASE["points"])
        self.fit_plot()
        self.table.setRowCount(0)

    def scan_command(self):
        mode = (self.options or {}).get("auto_uv_mode", "adaptive")
        return f"$ penguin-burner --auto-uv --auto-uv-mode {mode} --gpu-index {self.gpu_index} --scan-stream"

    def memory_status(self):
        mhz = self.memory_mhz("Efficiency" if self.full else self.single_tier())
        return (
            f"Memory offset: +{mhz:.0f} MHz memory clock"
            if mhz
            else "Memory offset: none"
        )

    def single_tier(self):
        return next(
            (r["tier"] for r in self.plan[1:] if r["tier"] in TIERS), "Balanced"
        )

    def start_fresh_scan(self, mode):
        self.reset_scan(mode)
        self.stage = "running"
        self.log(self.scan_command())
        self.log("Setup " + json.dumps(self.options, sort_keys=True))
        self.header_stage = "Starting"
        self.candidate_text = "Writing to main Auto-UV profile store"
        self.status_text = self.memory_status()
        if self.prototype.get("dependencyDownload"):
            self.phase = "download"
            self.download_pct = 0
            self.header_stage = "Downloading dependencies"
            self.candidate_text = ""
            self.log(
                "dependency_progress: fetching Q2RTX-headless and the CUDA workload"
            )
            self.progress_changed.emit(
                "Downloading dependencies 0%", 0, "Q2RTX-headless 1.8.1 · 0 / 540 MB"
            )
        else:
            self.start_probe()
        self.refresh()
        self.timer.start()

    def pending_choice_tier(self, plan, done, saved, skipped):
        """First tier whose descent finished but was neither saved nor skipped."""
        for tier in TIERS:
            last = max((i for i, r in enumerate(plan) if r["tier"] == tier), default=0)
            if 0 < last < done and tier not in saved and tier not in skipped:
                return tier
        return None

    def resume_scan(self):
        saved = self.last_scan
        self.plan, self.done = saved["plan"], saved["done"]
        self.tier_saved = saved["tier_saved"]
        self.resume_tier = saved["pending"]
        self.last_scan = {}
        self.table.setRowCount(0)
        self.plot.clear()
        self.tier_traces.clear()
        self.plot.set_source_points(BASE["points"])
        self.fit_plot()
        # Finished probes and final verifications return as saved measurements.
        self.runs = [dict(run, restored=True) for run in saved["runs"]]
        for i, run in enumerate(self.runs):
            if run["kind"] == "probe":
                self.plot.set_candidate_points(
                    self.plan[run["plan"]]["points"], curve_id=f"plan-{run['plan']}"
                )
            self.write_row(i)
        for tier, saved_tier in self.tier_saved.items():
            self.confirm_tier(tier, self.plan[saved_tier["index"]])
        focus = self.resume_tier or self.plan[self.done]["tier"]
        if self.full:
            position = TIERS.index(focus)
            self.tier_state = {
                t: "complete"
                if t in self.tier_saved
                else "skipped"
                if t in saved["skipped"] or TIERS.index(t) < position
                else "active"
                if TIERS.index(t) == position
                else "pending"
                for t in TIERS
            }
        self.log(self.scan_command())
        self.log(
            f"scan_resumed: restored {self.done} saved measurements for GPU {self.gpu_index}"
        )
        safe = next(
            (
                r
                for r in reversed(self.plan[1 : self.done])
                if r["passed"] and r["tier"] == focus
            ),
            None,
        )
        self.stage, self.phase, self.ticks = "running", "resuming", 0
        self.header_stage = "Resuming"
        self.candidate_text = probe_text(safe) + " (saved)" if safe else ""
        self.status_text = (
            f"Restored previous scan measurements. {self.resume_tier} still needs its final verification."
            if self.resume_tier
            else "Restored previous scan measurements and curves. Continuing from saved progress."
        )
        self.refresh()
        self.timer.start()

    def start_crash_recovery(self, mode):
        self.reset_scan(mode)
        tier = "Balanced" if self.full else self.single_tier()
        crashed = next(
            i for i, r in enumerate(self.plan) if r["tier"] == tier and not r["passed"]
        )
        saved = [
            i
            for i, r in enumerate(self.plan)
            if r["tier"] == tier and r["passed"] and i < crashed
        ]
        self.stage, self.phase = "running", "choice"
        self.log(self.scan_command())
        self.log(
            f"crash-recovery: the previous run ended during the probe at {probe_text(self.plan[crashed])}"
        )
        self.header_stage = "Starting"
        self.candidate_text = ""
        self.status_text = "A previous Auto-UV run ended during a GPU probe."
        if self.full:
            self.tier_state[tier] = "active"
        self.refresh()
        self.request_choice(
            dict(
                reason="previous-crash",
                tier=tier,
                indexes=saved,
                default=saved[-1],
                decision=dict(
                    candidate_voltage_mv=self.plan[crashed]["mv"],
                    lock_clock_mhz=self.plan[crashed]["target"],
                    decision="probe interrupted (GPU stopped responding)",
                ),
            )
        )

    def tick(self):
        self.ticks += 1
        if self.phase == "download":
            self.download_pct = min(100, self.download_pct + 20)
            self.progress_changed.emit(
                f"Downloading dependencies {self.download_pct}%",
                self.download_pct,
                f"Q2RTX-headless 1.8.1 · {round(5.4 * self.download_pct)} / 540 MB",
            )
            if self.download_pct >= 100:
                self.progress_changed.emit(None, 0, "")
                self.consume("dependencyDownload")
                self.log("dependency_progress: dependencies are ready")
                self.start_probe()
        elif self.phase == "probe":
            run = self.runs[self.active_run]
            run["elapsed"] = SHORT_PROBE_S * self.ticks / PROBE_TICKS
            if self.ticks >= PROBE_TICKS:
                self.finish_probe()
            else:
                self.show_live(self.plan[run["plan"]])
                self.write_row(self.active_run)
        elif self.phase == "final":
            run = self.runs[self.active_run]
            run["elapsed"] = run["duration"] * self.ticks / FINAL_TICKS
            if self.final_job["fail_at"] and self.ticks >= self.final_job["fail_at"]:
                self.fail_final()
            elif self.ticks >= FINAL_TICKS:
                self.finish_final()
            else:
                self.show_live(self.plan[run["plan"]], curve_id=f"verify-{run['tier']}")
                self.write_row(self.active_run)
        elif self.phase == "stopping":
            self.finish_stop()
        elif self.phase == "resuming":
            tier, self.resume_tier = self.resume_tier, None
            if tier:
                self.descent_done(tier)
            else:
                self.start_probe()
        elif self.phase == "profile":
            self.profile_tick()
        elif self.phase == "profile-stopping":
            self.end_profile_verification("stopped")
            return
        else:
            self.timer.stop()
        self.refresh()

    def tier_started(self, tier):
        position = TIERS.index(tier) + 1
        self.tier_state[tier] = "active"
        balanced = self.tier_saved.get("Balanced")
        self.status_text = (
            f"Performance reuses the Balanced downsweep ({probe_text(self.plan[balanced['index']])})"
            " — starting the Auto-OC climb."
            if tier == "Performance" and balanced
            else f"Scanning {tier} profile ({position}/3)."
        )
        self.log(f"tier_started: {tier.lower()} ({position}/3)")

    def start_probe(self):
        row = self.plan[self.done]
        previous = self.plan[self.done - 1] if self.done else None
        if (
            self.full
            and row["tier"] in TIERS
            and (previous is None or previous["tier"] != row["tier"])
        ):
            self.tier_started(row["tier"])
        elif self.done == 0:
            self.status_text = self.memory_status()
        self.runs.append(
            dict(
                kind="probe" if self.done else "baseline",
                plan=self.done,
                state="running",
                elapsed=0,
                duration=SHORT_PROBE_S,
                cap=cap_for(row),
            )
        )
        self.active_run = len(self.runs) - 1
        self.phase, self.stage, self.ticks = "probe", "running", 0
        self.header_stage = "Undervolting Candidates Sweep" if self.done else "Baseline"
        self.candidate_text = probe_text(row)
        if self.done:
            self.plot.set_candidate_points(row["points"], curve_id=f"plan-{self.done}")
        self.show_live(row)
        self.log(
            "probe_start "
            + (f"{row['tier'].lower()} candidate " if self.done else "stock baseline ")
            + probe_text(row)
        )
        self.write_row(self.active_run)

    def show_live(self, r, *, curve_id=None):
        phase = self.ticks + self.done * 0.7
        self.live_mv = r["mv"] + round(4.5 * math.sin(phase * 0.85))
        self.live_mhz = r["meas"] + round(8 * math.cos(phase * 0.8))
        self.plot.set_crosshair_point(self.live_mv, self.live_mhz)
        self.plot.set_target_point(r["mv"], r["target"])
        if r is BASE:
            return
        # Replay a few MHz of settling around the recorded candidate.
        points = [(v, f + 3 * math.sin(phase + v * 0.02)) for v, f in r["points"]]
        self.plot.set_candidate_points(
            points, curve_id=curve_id or f"plan-{self.done}", remember_previous=False
        )

    def finish_probe(self):
        run = self.runs[self.active_run]
        row = self.plan[run["plan"]]
        if self.prototype.get("processFailure") and run["plan"] >= 3:
            self.process_failed()
            return
        run["state"] = "pass" if row["passed"] else "fail"
        failure = None if row["passed"] else failure_for(row)
        run["failure"] = failure
        run["elapsed"] = SHORT_PROBE_S
        self.write_row(self.active_run)
        self.log(
            f"probe_result {probe_text(row)} · "
            + (
                f"pass · {row['fps']:.2f} FPS · {row['power']:.2f} W"
                if row["passed"]
                else f"fail · {failure['label']} · {failure['reason']}"
                if failure
                else "fail"
            )
        )
        self.done += 1
        self.active_run = -1
        nxt = self.plan[self.done] if self.done < len(self.plan) else None
        if row["tier"] in TIERS and (nxt is None or nxt["tier"] != row["tier"]):
            self.descent_done(row["tier"])
        elif nxt is not None:
            self.start_probe()
        else:
            self.finish_scan()

    def tier_candidates(self, tier):
        passed = [
            i
            for i, r in enumerate(self.plan[: self.done])
            if r["tier"] == tier and r["passed"]
        ]
        key = (
            (lambda i: self.plan[i]["fps"])
            if tier == "Performance"
            else (lambda i: self.plan[i]["fps"] / self.plan[i]["power"])
        )
        return sorted(passed, key=key, reverse=True)

    def descent_done(self, tier):
        indexes = self.tier_candidates(tier)
        if not indexes:
            self.tier_skip(tier, "no stable candidate found")
            self.continue_after(tier)
            return
        self.phase = "choice"
        self.plot.clear_load_markers()
        self.status_text = f"Choose the {tier} candidate for final verification."
        self.log(f"final_choice_request: {tier.lower()}")
        self.refresh()
        pair = self.comparison_pair(tier, indexes)
        if pair is not None:
            # PR #137: two close Performance candidates ran twice each; the
            # dialog shows their two-run means and preselects the winner.
            winner, rival = pair
            self.compared = {winner, rival}
            self.log(
                "performance_comparison: "
                f"{probe_text(self.plan[winner])} vs {probe_text(self.plan[rival])}"
            )
            if self.prototype.get("performanceComparison"):
                self.consume("performanceComparison")
            self.request_choice(
                dict(
                    reason="performance-comparison",
                    tier=tier,
                    indexes=indexes,
                    default=winner,
                )
            )
            return
        self.request_choice(
            dict(
                reason=f"adaptive-{tier.lower()}" if self.full else "",
                tier=tier,
                indexes=indexes,
                # The backend preselects the tier's confirmed (last passing) point.
                default=max(indexes) if self.full else indexes[0],
            )
        )

    def comparison_pair(self, tier, indexes):
        """auto_uv/run/performance_comparison.py performance_comparison_pair:
        an FPS leader and a rival within 2% FPS that clocks higher on less
        power. Prototype controls can force the two best to compare."""
        if tier != "Performance" or len(indexes) < 2:
            return None
        plan = self.plan
        leader = max(indexes, key=lambda i: plan[i]["fps"])
        rivals = [
            i
            for i in indexes
            if i != leader
            and plan[i]["fps"] >= plan[leader]["fps"] * (1 - COMPARISON_WINDOW_PCT / 100)
            and plan[i]["meas"] > plan[leader]["meas"]
            and plan[i]["power"] < plan[leader]["power"]
        ]
        if rivals:
            return leader, max(rivals, key=lambda i: plan[i]["fps"])
        if self.prototype.get("performanceComparison"):
            ranked = sorted(indexes, key=lambda i: plan[i]["fps"], reverse=True)
            return ranked[0], ranked[1]
        return None

    def candidate_payload(self, index):
        r = self.plan[index]
        return dict(
            candidate_id=str(index),
            candidate_voltage_mv=r["mv"],
            lock_clock_mhz=r["target"],
            avg_core_clock_mhz=r["meas"],
            avg_fps=r["fps"],
            avg_power_w=r["power"],
            efficiency_fps_per_w=r["fps"] / r["power"],
            # A compared candidate ran twice (30 s Q2RTX + 5 s CUDA each).
            short_verification_duration_s=(
                COMPARISON_RUNS * (SHORT_PROBE_S + COMPARISON_CUDA_S)
                if index in self.compared
                else SHORT_PROBE_S
            ),
            **({"comparison_runs": COMPARISON_RUNS} if index in self.compared else {}),
            base_avg_core_clock_mhz=BASE["meas"],
            base_avg_fps=BASE["fps"],
            base_avg_power_w=BASE["power"],
            base_efficiency_fps_per_w=BASE["fps"] / BASE["power"],
        )

    def request_choice(self, request):
        """Open the app's own final-candidate dialog for a backend request."""
        self.timer.stop()
        tier = request["tier"]
        minutes = request.get("minutes", self.tier_minutes(tier))
        C.QTimer.singleShot(0, fit_choice_dialog)
        choice, duration_s, action = select_final_candidate(
            QtCore=C,
            QtGui=G,
            QtWidgets=W,
            parent=self,
            candidates=[self.candidate_payload(i) for i in request["indexes"]],
            default_candidate_id=str(request["default"]),
            auto_uv_mode=tier.lower(),
            default_duration_s=minutes * 60,
            request_reason=request["reason"],
            recovery_decision=request.get("decision"),
        )
        if choice is not None and action == "select":
            action = "select"
        elif action == "select":
            action = "discard"
        self.handle_choice(
            request,
            action,
            int(choice["candidate_id"]) if choice is not None else -1,
            max(1, min(60, round(duration_s / 60))),
        )

    def handle_choice(self, request, action, index, minutes):
        reason, tier = request["reason"], request["tier"]
        recovery = reason in ("previous-crash", "final-verification-failed")
        if action == "select":
            row = self.plan[index]
            self.log(
                f"{tier} candidate · {probe_text(row)} · {minutes} min verification"
            )
            self.log(f"tier_confirmed: {tier.lower()} {probe_text(row)}")
            self.confirm_tier(tier, row)
            self.start_final(
                tier,
                index,
                minutes,
                "resume" if reason == "previous-crash" else "final",
                "finish" if reason in ("previous-crash", "user-stop") else "next",
            )
            return
        if action == "abort":
            self.end_scan("aborted")
            return
        if recovery:
            self.log("Starting a new scan from scratch")
            self.start_fresh_scan((self.options or {})["auto_uv_mode"])
            return
        self.any_discarded = True
        self.log(f"final_choice_discarded: {tier.lower()}")
        if reason == "user-stop" or not self.full:
            self.end_scan("discarded")
            return
        self.tier_skip(tier, "discarded")
        self.continue_after(tier)
        self.refresh()
        if self.busy():
            self.timer.start()

    def tier_skip(self, tier, reason):
        nxt = TIERS[TIERS.index(tier) + 1] if tier != TIERS[-1] else None
        self.tier_state[tier] = "skipped"
        self.status_text = f"{tier} tier skipped: {reason}" + (
            f". Continuing with {nxt}." if nxt and self.full else ""
        )
        self.log(f"tier_skipped: {tier.lower()} · {reason}")

    def start_final(self, tier, index, minutes, kind="final", then="next"):
        row = self.plan[index]
        self.runs.append(
            dict(
                kind=kind,
                plan=index,
                tier=tier,
                state="running",
                elapsed=0,
                duration=minutes * 60,
                cap=cap_for(row),
            )
        )
        self.active_run = len(self.runs) - 1
        fail = (
            kind == "final"
            and self.prototype.get("finalVerificationFailure")
            and not self.failed_once
        )
        self.final_job = dict(
            tier=tier,
            index=index,
            minutes=minutes,
            kind=kind,
            then=then,
            fail_at=7 if fail else 0,
        )
        if self.full:
            self.tier_state[tier] = "active"
        self.stage, self.phase, self.ticks = "verifying", "final", 0
        self.header_stage = (
            "Resume verification" if kind == "resume" else "Final verification"
        )
        self.candidate_text = probe_text(row)
        self.status_text = (
            f"Verifying {tier} for {fmt_duration(minutes * 60)}. Accelerated replay."
        )
        self.log(
            f"probe_start {'resume-verify' if kind == 'resume' else 'final'} {probe_text(row)}"
        )
        self.show_live(row, curve_id=f"verify-{tier}")
        self.write_row(self.active_run)
        self.refresh()
        self.timer.start()

    def finish_final(self):
        run = self.runs[self.active_run]
        job = self.final_job
        tier, index, minutes = job["tier"], job["index"], job["minutes"]
        row = self.plan[index]
        run["state"], run["elapsed"] = "pass", run["duration"]
        self.write_row(self.active_run)
        self.tier_saved[tier] = dict(index=index, minutes=minutes)
        self.tier_state[tier] = "complete"
        self.active_run = -1
        self.final_job = {}
        self.stage = "running"
        self.log(f"final_result: {tier} · {probe_text(row)} · profile saved")
        self.profile_saved.emit(
            tier, row, self.memory_mhz(tier), minutes, self.gpu_index
        )
        nxt = (
            TIERS[TIERS.index(tier) + 1]
            if self.full and job["then"] == "next" and tier != TIERS[-1]
            else None
        )
        self.status_text = f"{tier} profile complete." + (
            f" Continuing with {nxt}."
            if nxt
            else " All selected profiles are complete."
        )
        if job["then"] == "finish":
            self.finish_scan()
        else:
            self.continue_after(tier)

    def fail_final(self):
        run = self.runs[self.active_run]
        job = self.final_job
        tier, index = job["tier"], job["index"]
        row = self.plan[index]
        self.failed_once = True
        run["state"] = "fail"
        run["failure"] = dict(
            FINAL_FAILURE,
            reason=f"final verification ended early after {fmt_duration(run['elapsed'])}",
        )
        self.write_row(self.active_run)
        self.active_run = -1
        self.final_job = {}
        self.stage, self.phase = "running", "choice"
        self.plot.clear_load_markers()
        self.log(f"final verification failed: {probe_text(row)} · VK_ERROR_DEVICE_LOST")
        remaining = [i for i in self.tier_candidates(tier) if i != index]
        if not remaining:
            self.process_failed()
            return
        self.status_text = "Final verification failed. Choose a safer candidate."
        self.refresh()
        self.request_choice(
            dict(
                reason="final-verification-failed",
                tier=tier,
                indexes=remaining,
                default=remaining[0],
                minutes=job["minutes"],
                decision=dict(
                    candidate_voltage_mv=row["mv"],
                    lock_clock_mhz=row["target"],
                    decision=f"failed final verification ({FINAL_FAILURE['label']})",
                ),
            )
        )

    def continue_after(self, tier):
        nxt = TIERS[TIERS.index(tier) + 1] if self.full and tier != TIERS[-1] else None
        if nxt is None:
            self.finish_scan()
        elif self.done < len(self.plan):
            self.start_probe()
        else:
            self.tier_started(nxt)
            self.descent_done(nxt)

    def finish_scan(self):
        self.end_scan(
            "complete"
            if self.tier_saved
            else "discarded"
            if self.any_discarded
            else "stopped"
        )

    def mark_running(self, state):
        for i, run in enumerate(self.runs):
            if run["state"] in ("running", "stopping"):
                run["state"] = state
                self.write_row(i)

    def end_scan(self, outcome):
        self.timer.stop()
        label_text, text, exit_code = OUTCOMES[outcome]
        if outcome != "complete":
            self.mark_running(outcome if outcome != "discarded" else "discarded")
        terminal = "failed" if outcome == "failed" else "stopped"
        for tier, state in list(self.tier_state.items()):
            if state == "active":
                self.tier_state[tier] = terminal
            elif state == "pending":
                self.tier_state[tier] = "not-run"
        self.phase, self.active_run, self.final_job = "", -1, {}
        self.stage = outcome
        self.header_stage, self.candidate_text, self.status_text = label_text, "", text
        self.progress_changed.emit(None, 0, "")
        self.plot.clear_load_markers()
        self.plot.set_candidate_points([], remember_previous=False)
        self.log(
            f"Auto-UV process {'stopped' if exit_code else 'finished'}: exit_code={exit_code}"
        )
        # A discarded choice is final; otherwise a finished-but-unsaved tier
        # keeps the scan resumable even when every probe already ran.
        skipped = [t for t, state in self.tier_state.items() if state == "skipped"]
        pending = (
            None
            if outcome == "discarded"
            else self.pending_choice_tier(
                self.plan, self.done, self.tier_saved, skipped
            )
        )
        self.last_scan = (
            dict(
                options=self.options,
                plan=self.plan,
                done=self.done,
                tier_saved=self.tier_saved,
                skipped=skipped,
                pending=pending,
                runs=[
                    copy.deepcopy(run)
                    for run in self.runs
                    if run["state"] in ("pass", "fail")
                ],
            )
            if outcome != "complete"
            and self.done > 0
            and (self.done < len(self.plan) or pending)
            else {}
        )
        self.refresh()
        if outcome == "complete":
            self.scan_completed.emit()
            self.show_profiles.emit()
        elif outcome == "failed":
            show_error_dialog(
                QtCore=C,
                QtGui=G,
                QtWidgets=W,
                parent=self,
                title="Auto-UV failed",
                message=(
                    "Auto-UV scan stopped unexpectedly. "
                    "The full error details can be copied for troubleshooting."
                ),
                details=process_failure_details(
                    action_label="Auto-UV scan",
                    exit_code=exit_code,
                    exit_status="NormalExit",
                    log_tail="\n".join(self.log_lines[-80:]),
                ),
            )

    def process_failed(self):
        self.consume("processFailure")
        self.log("Traceback (most recent call last):")
        self.log('  File "auto_uv/main_loop.py", line 1311, in run_adaptive_scan')
        self.log("RuntimeError: Q2RTX exited with status 139 (SIGSEGV)")
        for i, run in enumerate(self.runs):
            if run["state"] in ("running", "stopping"):
                run["state"] = "failed"
                self.write_row(i)
        self.end_scan("failed")

    def stop(self):
        if self.phase == "profile":
            # As the app: Stopping until the verification process exits.
            self.phase, self.ticks = "profile-stopping", 0
            self.header_stage = "Stopping"
            self.status_text = "Stopping profile verification."
            self.log("Stop requested by user")
            self.refresh()
            return
        if self.phase == "download":
            self.log("Stopped by user")
            self.end_scan("stopped")
            return
        if self.phase not in ("probe", "final"):
            return
        self.runs[self.active_run]["state"] = "stopping"
        self.write_row(self.active_run)
        self.phase, self.ticks = "stopping", 0
        self.header_stage, self.status_text = "Stopping", "Stopping Auto-UV."
        self.log("Stop requested by user")
        self.refresh()

    def finish_stop(self):
        run = self.runs[self.active_run]
        run["state"] = "stopped"
        self.write_row(self.active_run)
        self.active_run = -1
        tier = self.plan[run["plan"]]["tier"]
        indexes = self.tier_candidates(tier) if tier in TIERS else []
        if run["kind"] == "probe" and indexes:
            self.phase = "choice"
            self.plot.clear_load_markers()
            self.status_text = (
                "Auto-UV was stopped. Choose a stable candidate or discard."
            )
            self.refresh()
            self.request_choice(
                dict(reason="user-stop", tier=tier, indexes=indexes, default=indexes[0])
            )
            return
        self.end_scan("stopped")

    # ---------- profile verification (Profiles → Verify) ----------

    def start_profile_verification(self, index, name, row, minutes):
        if self.busy():
            return False
        self.profile_job = dict(
            index=index,
            label=f"{name} · {probe_text(row)}",
            row=row,
            duration=max(1, min(60, int(minutes))) * 60,
            elapsed=0,
            fail=bool(self.prototype.get("processFailure")),
            prior=self.stage,
        )
        self.stage, self.phase, self.ticks = "verifying", "profile", 0
        self.header_stage = "Profile verification"
        self.candidate_text = self.profile_job["label"]
        self.status_text = f"Verifying {self.profile_job['label']} with Q2RTX."
        self.plot.clear()
        self.tier_traces.clear()
        self.plot.set_source_points(BASE["points"])
        self.fit_plot([row])
        self.plot.set_candidate_points(row["points"], remember_previous=False)
        self.log(
            f"$ penguin-burner --verify-profile {name.lower()} --duration "
            f"{self.profile_job['duration']} --gpu-index {self.gpu_index}"
        )
        self.progress_changed.emit(self.profile_progress_text(), 0, self.status_text)
        self.show_scan.emit()
        self.refresh()
        self.timer.start()
        return True

    def profile_progress_text(self):
        job = self.profile_job
        return f"Verifying profile {fmt_duration(job['elapsed'])} / {fmt_duration(job['duration'])}"

    def profile_tick(self):
        job = self.profile_job
        job["elapsed"] = min(
            job["duration"], job["duration"] * self.ticks / PROFILE_TICKS
        )
        self.show_live(job["row"], curve_id="profile")
        self.progress_changed.emit(
            self.profile_progress_text(),
            round(100 * job["elapsed"] / job["duration"]),
            self.status_text,
        )
        if job["fail"] and self.ticks >= PROFILE_TICKS // 2:
            self.end_profile_verification("failed")
        elif self.ticks >= PROFILE_TICKS:
            self.end_profile_verification("passed")

    def end_profile_verification(self, result):
        self.timer.stop()
        job, self.profile_job = self.profile_job, {}
        self.phase = ""
        self.stage = job["prior"]
        self.plot.clear_load_markers()
        self.log(
            f"Profile verification finished: exit_code={0 if result == 'passed' else 1}"
        )
        self.candidate_text = ""
        if result == "passed":
            self.header_stage = "Idle"
            self.status_text = "Profile verification complete."
            duration = fmt_duration(job["duration"])
            self.progress_changed.emit(
                f"Verifying profile {duration} / {duration}", 100, self.status_text
            )
            C.QTimer.singleShot(
                2500,
                lambda: (
                    not self.profile_job and self.progress_changed.emit(None, 0, "")
                ),
            )
        else:
            self.progress_changed.emit(None, 0, "")
            self.header_stage = "Idle" if result == "stopped" else "Error"
            self.status_text = (
                "Profile verification stopped."
                if result == "stopped"
                else "Profile verification failed."
            )
        self.profile_verified.emit(job["index"], result == "passed")
        self.refresh()
        if result == "failed":
            self.consume("processFailure")
            show_error_dialog(
                QtCore=C,
                QtGui=G,
                QtWidgets=W,
                parent=self,
                title="Profile verification failed",
                message=(
                    "Profile verification stopped unexpectedly. "
                    "The full error details can be copied for troubleshooting."
                ),
                details=process_failure_details(
                    action_label="Profile verification",
                    exit_code=1,
                    exit_status="NormalExit",
                    log_tail="\n".join(self.log_lines[-80:]),
                ),
            )

    # ---------- runs table ----------

    def write_row(self, i):
        run = self.runs[i]
        r = self.plan[run["plan"]]
        state = run["state"]
        measured = state in ("pass", "fail")
        is_base = run["kind"] == "baseline"
        tone = row_tone(run)

        def delta(n, base, digits):
            return (
                f"{n:.{digits}f} {(n / base - 1) * 100:+.1f}%"
                if not is_base
                else f"{n:.{digits}f}"
            )

        cap = run.get("cap") or cap_for(r)
        result = {"pass": "Pass", "fail": "Failed"}.get(
            state, RUN_STATE_LABELS.get(state, state)
        )
        stage_name = {"final": "Final · ", "resume": "Resume · "}.get(run["kind"], "")
        status = (
            run["failure"]["label"]
            if state == "fail"
            else f"{stage_name}{fmt_duration(run['elapsed'])} / {fmt_duration(run['duration'])}"
            if state in ("running", "pass")
            else RUN_STATE_LABELS.get(state, state)
        )
        data = [
            str(i + 1),
            str(r["mv"]),
            str(r["target"]),
            "-" if is_base else f"{round(r['target'] - BASE['meas']):+d}",
            f"{r['meas']:.2f}",
            delta(r["fps"], BASE["fps"], 2),
            delta(r["power"], BASE["power"], 2),
            f"{r['temp']:.2f}",
            f"{r['fan']:.2f}",
            cap["text"],
            delta(r["fps"] / r["power"], BASE["fps"] / BASE["power"], 4),
            result,
            status,
        ]
        if not measured:
            data[4:11] = ["—"] * 7
            if state == "running":
                data[4] = f"{self.live_mhz:.0f}"
                data[9] = cap["text"]
        tip = failure_tooltip(run, i)
        tips = {
            3: "Target clock delta from measured baseline",
            9: f"NVML clocks throttle reason: {cap['text']}"
            + (
                f"\nhw-power-brake on {cap['samples']} samples: the board's power delivery "
                "limited the clock, not the configured power limit"
                if cap["samples"]
                else ""
            ),
            10: "Base FPS/W reference" if is_base else "",
            11: tip,
            12: "\n".join(
                filter(
                    None,
                    (
                        {
                            "final": "Final verification",
                            "resume": "Resume verification",
                        }.get(run["kind"], ""),
                        tip,
                    ),
                )
            ),
        }
        scroll = self.table.verticalScrollBar()
        follow = scroll.maximum() - scroll.value() <= 1 and self.table.currentRow() < 0
        old_top = scroll.value()
        if i >= self.table.rowCount():
            self.table.insertRow(i)
        background = ROW_BACKGROUNDS.get(tone)
        for col, value in enumerate(data):
            cell = self.table.item(i, col)
            if cell is None:
                cell = W.QTableWidgetItem()
                self.table.setItem(i, col, cell)
            cell.setText(value)
            cell.setToolTip(tips.get(col) or (tip if is_base and col == 0 else value))
            if is_base and col == 0:
                cell.setToolTip("Baseline reference run")
            cell.setBackground(mixed(*background) if background else G.QBrush())
            cell.setForeground(
                qcolor(
                    "secondary"
                    if run.get("restored") and 0 < col < 11 or tone == "neutral"
                    else "ink"
                )
            )
            if col in (5, 6, 10):
                raw, reference = {
                    5: (r["fps"], BASE["fps"]),
                    6: (r["power"], BASE["power"]),
                    10: (r["fps"] / r["power"], BASE["fps"] / BASE["power"]),
                }[col]
                change = (
                    100 * (raw / reference - 1) if not is_base and measured else None
                )
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
                result_tone = (
                    "warning"
                    if state == "fail"
                    and (run.get("failure") or {}).get("severity") == "recoverable"
                    else RESULT_COLORS.get(str(result), "loss")
                )
                cell.setData(RESULT_ROLE, result_tone)
            if col == 0:
                cell.setData(FINAL_ROLE, run["kind"] in ("final", "resume"))
            if col == 12:
                cell.setData(
                    PROGRESS_ROLE, max(0.0, min(1.0, run["elapsed"] / run["duration"]))
                )
                cell.setData(TONE_ROLE, tone)
        if follow:
            self.table.scrollToBottom()
        else:
            scroll.setValue(old_top)

    def highlight(self, row, *_):
        run = self.runs[row] if 0 <= row < len(self.runs) else None
        if run is None or run["kind"] == "baseline":
            self.plot.set_selection_color(None)
            self.plot.set_highlighted_curve(None)
        else:
            tier = self.plan[run["plan"]]["tier"]
            self.plot.set_selection_color(TIER_COLORS.get(tier))
            self.plot.set_highlighted_curve(f"plan-{run['plan']}")
        self.update_legend()

    def update_legend(self):
        items = [("Stock", "baseCurve", "dashed")]
        if self.plot.previous_curves:
            items.append(("Previous runs", "curveHistory", "solid"))
        for tier in TIERS:
            if tier in self.tier_traces:
                items.append((tier + " tier", TIER_COLORS[tier], "solid"))
        if self.profile_job or (
            self.phase in ("probe", "final") and self.active_run >= 0
        ):
            items.append(("Live candidate", "candidateCurve", "dot"))
        row = self.table.currentRow()
        if 0 <= row < len(self.runs) and self.runs[row]["kind"] != "baseline":
            tier = self.plan[self.runs[row]["plan"]]["tier"]
            items.append(
                (
                    f"Run {row + 1} (selected)",
                    TIER_COLORS.get(tier, "candidateCurve"),
                    "selected",
                )
            )
        if [tuple(i) for i in self.legend.items] != items:
            self.legend.set_items(items)

    def confirm_tier(self, tier, row):
        points = row["points"]
        if tier in self.tier_traces:
            voltages, clocks = zip(*points, strict=True)
            self.tier_traces[tier].setData(voltages, clocks)
        else:
            self.plot.add_comparison_points(
                points,
                name=tier + " tier",
                color=TIER_COLORS[tier],
                alpha=255,
                width=3,
                z_value=2,
            )
            self.tier_traces[tier] = self.plot.comparison_curves[-1]

    def copy_logs(self):
        W.QApplication.clipboard().setText("\n".join(self.log_lines))
        message = f"Copied the full scan log ({len(self.log_lines)} lines)"
        self.copy_button.setText("Log copied")
        self.copy_button.setAccessibleDescription(message)
        announcement = getattr(G, "QAccessibleAnnouncementEvent", None)
        if announcement:
            G.QAccessible.updateAccessibility(announcement(self.copy_button, message))
        C.QTimer.singleShot(1400, lambda: self.copy_button.setText("Copy logs"))

    def stage_tone(self):
        if self.stage == "failed" or self.header_stage == "Error":
            return "error"
        if self.stage in ("stopped", "aborted") or self.phase in (
            "stopping",
            "profile-stopping",
        ):
            return "warning"
        if self.stage == "complete":
            return "good"
        return "running" if self.busy() else "idle"

    def refresh(self):
        idle = (
            not self.plan
            and not self.profile_job
            and self.stage == "idle"
            and not self.log_lines
        )
        self.stage_label.setText(
            "Ready to scan" if idle else self.header_stage or "Idle"
        )
        self.stage_dot.set_tone(self.stage_tone())
        self.candidate_label.setText(self.candidate_text)
        self.candidate_label.setVisible(bool(self.candidate_text))
        self.stage_detail.setText(
            "No Auto UV runs in this session. Choose a GPU and scan scope to get started."
            if idle
            else self.status_text
        )
        self.stage_detail.setToolTip(self.status_text)
        self.tier_strip.setVisible(self.full and bool(self.tier_state))
        self.tier_strip.set_states(
            self.tier_state, self.final_job["tier"] if self.final_job else None
        )
        busy = self.busy()
        self.setup_button.setEnabled(not busy)
        self.stop_button.setEnabled(
            busy and self.phase not in ("stopping", "profile-stopping")
        )
        self.import_button.setEnabled(not busy and self.import_ready)
        self.copy_button.setEnabled(bool(self.log_lines))
        self.copy_button.setToolTip(
            f"Copy the full scan log ({len(self.log_lines)} lines)"
        )
        self.log_count.setText(
            f"{len(self.log_lines)} line{'s' if len(self.log_lines) != 1 else ''}"
        )
        self.plot_stack.setCurrentIndex(0 if idle else 1)
        # Idle: a compact placeholder instead of an empty plot.
        if idle:
            self.plot_stack.setFixedHeight(125)
            self.scan_card.setMaximumHeight(self.scan_card.sizeHint().height())
        elif self.scan_card.maximumHeight() < 16777215:
            # Leaving the idle layout: give the plot card 60% again.
            self.plot_stack.setMinimumHeight(150)
            self.plot_stack.setMaximumHeight(16777215)
            self.scan_card.setMaximumHeight(16777215)
            total = max(self.splitter.height(), 600)
            self.splitter.setSizes([round(total * 0.6), round(total * 0.4)])
        self.runs_empty.setVisible(not self.runs)
        finished = sum(r["state"] not in ("running", "stopping") for r in self.runs)
        self.runs_meta.setText(
            "Measurements and results appear here during a scan."
            if not self.plan
            else f"{'Full scan' if self.full else 'Selected profile'} · {finished} completed run{'s' if finished != 1 else ''}"
        )
        self.update_legend()
        if busy:
            self.status_changed.emit(
                {
                    "running": self.header_stage
                    if self.profile_job
                    else "Auto UV · " + self.header_stage,
                    "detail": " · ".join(
                        filter(None, (self.candidate_text, self.status_text))
                    ),
                }
            )
        else:
            self.status_changed.emit(None)


def fit_choice_dialog():
    """Give the production final-choice dialog room for every column: content
    widths with Status stretching last, no sideways scroll, no blank rows."""
    dialog = W.QApplication.activeModalWidget()
    if not isinstance(dialog, W.QDialog):
        return
    width = dialog.width()
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
        rows = table.verticalHeader().length()
        table.setMinimumHeight(0)
        table.setFixedHeight(header.height() + rows + 2 * table.frameWidth() + 2)
    layout = dialog.layout()
    dialog.setMinimumHeight(0)
    dialog.resize(
        width,
        layout.heightForWidth(width)
        if layout is not None and layout.hasHeightForWidth()
        else dialog.sizeHint().height(),
    )


def fmt_duration(seconds):
    """ui/components/scan_controls.py _format_duration_compact."""
    total = max(0, round(float(seconds or 0)))
    if total < 60:
        return f"{total}s"
    minutes, rest = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}min {rest}s" if rest else f"{minutes}min"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}min" if mins else f"{hours}h"


def probe_text(row):
    return f"{row['mv']}mV @ {row['target']}MHz"


def failure_for(row):
    return PROBE_FAILURES.get(
        (row["tier"], row["mv"], row["target"]),
        dict(
            label="Average FPS low",
            kind="fps-regression",
            severity="recoverable",
            reason="average FPS below the stock floor",
        ),
    )


def cap_for(row):
    if row["power"] < 305:
        return dict(text="none", samples=0)
    samples = 3 if row["tier"] == "Performance" and row["target"] == 2980 else 0
    return dict(
        text="sw-power+hw-power-brake" if samples else "sw-power", samples=samples
    )


def row_tone(run):
    state = run["state"]
    if state == "running":
        return "running"
    if state in ("stopping", "stopped", "aborted"):
        return "warning"
    if state == "discarded":
        return "neutral"
    if state == "failed":
        return "error"
    if state == "fail":
        return (
            "warning"
            if (run.get("failure") or {}).get("severity") == "recoverable"
            else "error"
        )
    return "baseline" if run["kind"] == "baseline" else "good"


def failure_tooltip(run, i):
    failure = run.get("failure")
    if not failure:
        if run.get("restored"):
            return "Saved measurement: passed"
        return "Baseline reference run" if run["kind"] == "baseline" else ""
    return "\n".join(
        filter(
            None,
            (
                ("Saved measurement: " if run.get("restored") else "")
                + failure["reason"],
                f"kind: {failure['kind']}",
                f"severity: {failure['severity']}",
                "fatal output: " + ", ".join(failure["fatal"])
                if failure.get("fatal")
                else "",
                f"log: ~/.local/state/penguin-burner/auto-uv/run-{i + 1:04d}.log",
            ),
        )
    )


class PreviewCurveEditors(CurveEditors):
    """The production V/F and fan editors, drawn like the rest of the mock:
    an external legend, an accent draft and a primary "Save edited copy"."""

    def open_vf(self, parent, **kwargs):
        C.QTimer.singleShot(0, lambda: polish_curve_editor("vf"))
        with patch.object(vf_curve_editor, "CurvePlot", EditorCurvePlot):
            super().open_vf(parent, **kwargs)

    def open_fan(self, parent, **kwargs):
        C.QTimer.singleShot(0, lambda: polish_curve_editor("fan"))
        with patch.object(fan_curve_editor, "CurvePlot", EditorCurvePlot):
            super().open_fan(parent, **kwargs)


NO_NVIDIA_TITLE = "No NVIDIA GPU detected"
# ui/window.py _check_gpu_supported_on_startup.
NO_NVIDIA_TEXT = (
    "PenguinBurner tunes NVIDIA GPUs and could not find one on this "
    "system (no NVIDIA kernel driver is loaded).\n\n"
    "Make sure a supported NVIDIA card and the proprietary driver are "
    "installed, then restart PenguinBurner. Undervolt scans and profile "
    "applies will not work until then."
)
SAMPLE_ERROR_LOG = (
    "[21:04:12] $ penguin-burner --auto-uv --auto-uv-mode adaptive --gpu-index 0 --scan-stream",
    "[21:09:40] probe 6/16: 840mV @ 2585MHz",
    "Traceback (most recent call last):",
    '  File "auto_uv/main_loop.py", line 1311, in run_adaptive_scan',
    "RuntimeError: Q2RTX exited with status 139 (SIGSEGV)",
)


class Preview(W.QWidget):
    """The mock window: native Auto UV, Profiles, Game Library (with the
    Perf Profile drill-down) and In-Game Overlay over sample sources."""

    def __init__(self, *, history=False, overlay=False):
        super().__init__()
        self.setWindowTitle("PenguinBurner · Qt UX preview")
        self.setWindowIcon(G.QIcon(str(asset_image_path("penguin-burner.png"))))
        self.resize(1230, 810)
        page = W.QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)
        # Window status sources, highest priority first: a running scan or
        # verification, a Profiles command, a playing game, then Profiles'
        # idle summary of what runs now and at startup.
        self.status_sources: dict[str, dict | None] = dict.fromkeys(
            ("scan", "profilesBusy", "library", "profiles")
        )
        self.sample_runtime = C.QElapsedTimer()
        self.prototype = PrototypeState()
        self.prototype_dialog: PrototypeControlsDialog | None = None
        self.pre_scan_silent_fan = False
        # ScanPage reports profile verifications by index.
        self.verify_ids: list[str] = []
        # Header: logo + wordmark, About, header progress and actions, then
        # the two-line global status, a flat tab strip, the page and a footer.
        header = self.shell_section("shellHeader")
        top = W.QHBoxLayout(header)
        top.setContentsMargins(GAP["lg"], GAP["md"], GAP["lg"], GAP["sm"])
        top.setSpacing(GAP["md"])
        logo = W.QLabel()
        logo.setObjectName("brandLogo")
        logo.setPixmap(
            G.QIcon(str(asset_image_path("penguin-burner.png"))).pixmap(30, 30)
        )
        top.addWidget(logo)
        top.addWidget(label("PenguinBurner", "brand"))
        top.addStretch()
        self.header_progress = W.QProgressBar()
        self.header_progress.setObjectName("headerProgress")
        self.header_progress.setRange(0, 100)
        self.header_progress.setFixedWidth(260)
        self.header_progress.hide()
        top.addWidget(self.header_progress)
        page.addWidget(header)
        band = self.status_band = self.shell_section("statusBand")
        lines = W.QVBoxLayout(band)
        lines.setContentsMargins(GAP["lg"], 0, GAP["lg"], GAP["sm"] + 2)
        lines.setSpacing(0)
        # Each line elides to the window width with the full text in its
        # tooltip; the detail (the live candidate) is selectable and copyable.
        self.status_running = ElidedLabel("statusRunning")
        self.status_detail = ElidedLabel("statusDetail", copyable=True)
        for line in (self.status_running, self.status_detail):
            lines.addWidget(line)
        page.addWidget(band)
        strip = W.QFrame()
        strip.setObjectName("tabStrip")
        strip_layout = W.QHBoxLayout(strip)
        strip_layout.setContentsMargins(GAP["sm"], 0, GAP["sm"], 0)
        self.tabs = W.QTabBar()
        self.tabs.setDrawBase(False)
        self.tabs.setDocumentMode(True)
        self.tabs.setIconSize(C.QSize(20, 20))
        # Painted line icons (the app's tab PNGs are opaque squares).
        for text, icon in zip(
            PAGES, ["auto-uv", "profiles", "game-library", "overlay"], strict=True
        ):
            self.tabs.addTab(tab_icon(icon), text)
        self.tabs.setExpanding(False)
        strip_layout.addWidget(self.tabs)
        strip_layout.addStretch()
        page.addWidget(strip)
        body = W.QVBoxLayout()
        # Each page keeps its own PAGE_MARGINS, so titles line up across tabs.
        body.setContentsMargins(0, 0, 0, 0)
        self.stack = W.QStackedWidget()
        body.addWidget(self.stack)
        page.addLayout(body, 1)
        footer = self.shell_section("shellFooter")
        foot = W.QHBoxLayout(footer)
        foot.setContentsMargins(GAP["lg"], GAP["xs"] + 2, GAP["lg"], GAP["xs"] + 2)
        foot.addWidget(label("Qt prototype · sample data", "sampleNote"))
        foot.addStretch()
        self.prototype_button = button(
            "Prototype controls", self.open_prototype_controls, "footerAction"
        )
        foot.addWidget(self.prototype_button)
        page.addWidget(footer)

        # Pages.
        self.scan = ScanPage()
        # Workflow actions, then the app's own utilities at the far end:
        # Setup first, Stop beside the scan it stops (only while one runs),
        # then Import.
        for action in (
            self.scan.setup_button,
            self.scan.stop_button,
            self.scan.import_button,
        ):
            top.addWidget(action)
        divider = W.QFrame()
        divider.setObjectName("headerDivider")
        divider.setFixedSize(1, 22)
        top.addWidget(divider)
        top.setSpacing(GAP["sm"])
        self.scan.import_button.setIcon(glyph_icon("import", "secondary"))
        self.scan.stop_button.setIcon(glyph_icon("stop", "loss"))
        for action in (self.scan.import_button, self.scan.stop_button):
            action.setIconSize(C.QSize(16, 16))
        self.scan.setup_button.setIconSize(C.QSize(16, 16))
        # Stop only while there is something to stop: a disabled Stop beside
        # Setup was noise in every idle state.
        self.stop_visibility = VisibleWhileEnabled(self.scan.stop_button)
        self.appearance_button = header_icon_button(
            glyph_icon("appearance", "secondary", 18), "Appearance", self.show_appearance
        )
        self.about_button = header_icon_button(
            info_icon(COL["secondary"]), "About PenguinBurner", self.show_about
        )
        top.addWidget(self.appearance_button)
        top.addWidget(self.about_button)
        self.profile_source = SampleProfilesSource()
        # A failed Profiles command uses up "Fail the next scan or
        # verification" too, as a scan does.
        self.profile_source.failure_consumed = lambda: self.scan.consume(
            "processFailure"
        )
        self.profiles = ProfilesPage(
            self.profile_source,
            editors=PreviewCurveEditors(),
            ensure_daemon_ready=self.scan.ensure_daemon_ready,
        )
        from native_library import LibraryPage
        from native_overlay import OverlayPage

        self.perf_source = SamplePerfSource()
        # The sample's live ring belongs to a session the library starts:
        # Control's recording is archived until a game plays.
        self.perf_source.set_running(LIVE_KEY, False)
        self.library = LibraryPage(self.perf_source)
        # The sample source follows the library's sessions and Capture
        # telemetry switches (a daemon adapter would read both itself).
        self.library.recording_changed.connect(
            lambda key, running, tier: self.perf_source.set_running(
                key, running, tier=tier
            )
        )
        self.library.capture_changed.connect(self.perf_source.set_capture)
        self.library.prototype_changed.connect(self.library_prototype_changed)
        self.perf_page = PerfProfilePage(
            self.perf_source,
            on_back=self.back_to_library,
            on_enable_capture=self.library.enable_capture,
        )
        self.overlay = OverlayPage()
        for widget in (
            self.scan,
            self.profiles,
            self.library,
            self.perf_page,
            self.overlay,
        ):
            self.stack.addWidget(widget)
        # Game Library shows the list or the Perf Profile drill-down.
        self.library_view: W.QWidget = self.library

        # Auto UV.
        self.scan.show_scan.connect(self.on_scan_started)
        self.scan.import_requested.connect(self.import_afterburner)
        self.scan.profile_saved.connect(self.save_scan_profile)
        self.scan.scan_starting.connect(self.capture_scan_intent)
        self.scan.scan_completed.connect(self.apply_scan_profile)
        self.scan.status_changed.connect(
            lambda status: self.set_global_status(status, "scan")
        )
        self.scan.status_changed.connect(self.sync_scan_busy)
        self.scan.progress_changed.connect(self.set_header_progress)
        self.scan.prototype_consumed.connect(self.prototype_consumed)
        self.scan.daemon_ready.connect(self.daemon_installed)
        self.scan.profile_verified.connect(self.profile_verified)
        self.scan.show_profiles.connect(lambda: self.tabs.setCurrentIndex(1))

        # Profiles.
        self.profiles.status_changed.connect(self.set_profiles_status)
        self.profiles.verify_requested.connect(self.verify_profile)
        self.profiles.adaptive_requested.connect(self.pick_adaptive_game)
        self.profiles.setup_requested.connect(self.setup_from_profile)
        self.profiles.import_requested.connect(self.import_afterburner)
        self.profiles.adaptive_tiers_changed.connect(
            lambda tiers: self.library.set_adaptive_available(bool(tiers))
        )
        self.library.set_adaptive_available(bool(self.profiles.adaptive_tiers()))
        self.set_profiles_status(self.profiles.status())

        # Game Library and its Perf Profile; the overlay follows the game.
        self.library.selected_game_changed.connect(self.overlay.panel.set_game)
        self.overlay.panel.set_game(self.library.panel._selected_game())
        self.library.open_perf_profile.connect(self.open_perf_profile)
        self.library.status_changed.connect(
            lambda status: self.set_global_status(status, "library")
        )
        self.library.game_session_changed.connect(self.game_session_changed)

        self.tabs.currentChanged.connect(self.change_page)
        if history:
            self.tabs.setCurrentIndex(2)
            self.library.select_game(0)
            self.open_perf_profile(self.library.selected_key())
        elif overlay:
            self.tabs.setCurrentIndex(3)
        self.change_page(self.tabs.currentIndex())

    # ---------- About ----------

    def on_scan_started(self):
        if not self.sample_runtime.isValid():
            self.sample_runtime.start()
        self.tabs.setCurrentIndex(0)

    def sample_energy_status(self, **_):
        if self.prototype.daemon == "missing":
            raise RuntimeError("the PenguinBurner root service is not installed")
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
        authors = " · ".join(
            f'<a href="{url}">{html.escape(name)}</a>' for name, url in ABOUT_AUTHORS
        )
        links = (about.ABOUT_LINKS_HTML + f"<br><br>Authors<br>{authors}").replace(
            "<a href=", f'<a style="color:{COL["accent"]}" href='
        )
        with (
            patch(
                "runtime.daemon_client.daemon_status",
                side_effect=self.sample_energy_status,
            ),
            patch.object(about, "ABOUT_LINKS_HTML", links),
            patch.object(about, "GPU_UNDERVOLTING_PURPOSE_TEXT", COPY["aboutPurpose"]),
        ):
            C.QTimer.singleShot(0, polish_about)
            about.show_about_dialog(QtCore=C, QtGui=G, QtWidgets=W, parent=self)

    # ---------- Afterburner ----------

    def import_afterburner(self):
        if self.scan.busy() or self.profiles.busy():
            self.profiles.dialogs.info(
                self,
                "Import Afterburner",
                "Finish the current PenguinBurner action before importing Afterburner.",
            )
            return
        example = HERE / "fixtures" / "Afterburner"
        C.QTimer.singleShot(0, polish_afterburner_import)
        with (
            patch.object(afterburner_import, "CurvePlot", ExternalLegendPlot),
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
        # Parsing the selected directory is read-only; the sample profile
        # store keeps the imported slot for this session.
        self.tabs.setCurrentIndex(1)
        self.profiles.import_afterburner(
            entry, afterburner_fan_curve_payload(entry["afterburner_root"])
        )

    # ---------- Game Library and Perf Profile ----------

    def open_perf_profile(self, game_key):
        if self.tabs.currentIndex() != 2:
            self.tabs.setCurrentIndex(2)
        self.library_view = self.perf_page
        # The game first, so showing the page renders it once.
        self.perf_page.show_game(game_key)
        self.stack.setCurrentWidget(self.perf_page)
        self.perf_page.back_button.setFocus(C.Qt.FocusReason.OtherFocusReason)

    def back_to_library(self):
        self.library_view = self.library
        self.stack.setCurrentWidget(self.library)
        self.library.return_from_perf_profile()

    def pick_adaptive_game(self):
        """Profiles → Use Adaptive for a game: the game and FPS picker, then
        Game Library with that game wrapped on Adaptive."""
        from native_library import KEYS

        # Only games Adaptive can run on (Vulkan frame data, ready to launch).
        selected, eligible = self.library.adaptive_picker_state()
        games = [AdaptiveGame(key, name, target) for key, name, target in eligible]
        picker = AdaptiveGamePicker(games, selected, self)
        if picker.exec() != W.QDialog.DialogCode.Accepted:
            return
        key, fps = picker.choice()
        self.apply_game_adaptive(KEYS.index(key), fps)

    def apply_game_adaptive(self, game_index, target_fps):
        self.tabs.setCurrentIndex(2)
        self.back_to_library()
        self.library.apply_adaptive(game_index, target_fps)

    def game_session_changed(self, key, running, mode, _target_fps):
        """A game the library plays swaps in its per-game profile, as the
        daemon's game override does; stopping it restores the standing one.
        A wrapped game with its overlay on publishes overlay telemetry."""
        self.overlay.panel.set_publishing(
            bool(running) and self.library.overlay_publishing(key)
        )
        if not running:
            self.profile_source.set_game_session(None)
            return
        from native_library import RECORDING_TIERS

        tier = RECORDING_TIERS.get(mode, "")
        profile_id = self.tier_profile_id(tier)
        if tier and profile_id:
            adaptive = mode == "adaptive"
            self.profile_source.set_game_session(
                profile_id, adaptive=adaptive, live_tier=tier if adaptive else ""
            )

    def tier_profile_id(self, tier):
        """The profile Adaptive would pick for a tier on the target GPU."""
        if not tier:
            return ""
        winners = self.profiles._winners()
        gpus = self.profile_source.gpus()
        uuid = gpus[0].uuid if gpus else ""
        key = tier.lower()
        return winners.get(f"{uuid}\0{key}") or winners.get(key) or ""

    # ---------- Auto UV ↔ Profiles ----------

    def setup_from_profile(self):
        self.tabs.setCurrentIndex(0)
        if self.scan.setup_button.isEnabled():
            C.QTimer.singleShot(0, self.scan.setup)

    def capture_scan_intent(self):
        """Remember the silent-fan intent before the scan resets the GPU to
        stock (window.py _pre_scan_silent_fan)."""
        self.pre_scan_silent_fan = bool(
            self.profiles.silent_fan_enabled()
            or self.profile_source.runtime().running_silent_fan
        )

    def save_scan_profile(self, tier, row, memory_mhz, _minutes, gpu_index=0):
        """Each tier's verified profile is saved as soon as it passes, bound
        to the GPU the native setup scanned."""
        profile_id = self.profile_source.save_scan_profile(
            tier, row, int(memory_mhz), int(gpu_index)
        )
        self.profiles.set_target_gpu(int(gpu_index))
        self.profiles.show_new_profile(profile_id)

    def apply_scan_profile(self):
        """A finished scan applies the newest profile, like the app's daemonize."""
        page = self.profiles
        if not page.preferred_id:
            return
        page.set_target_gpu(self.scan.gpu_index)
        if self.pre_scan_silent_fan:
            page.set_silent_fan(True)
        page.apply_profile(page.preferred_id)

    def sync_scan_busy(self, *_):
        """While a scan or verification runs, Profiles locks Apply, Delete,
        Restore and Verify."""
        self.profiles.set_busy(self.scan.busy())

    def verify_profile(self, profile_id, name, row, minutes):
        """Profiles → Verify runs on the Auto UV page."""
        row = dict(row)
        if row.get("meas") is None and row.get("target") is not None:
            row["meas"] = float(row["target"]) - 22
        if not row.get("points") or row.get("mv") is None:
            return
        self.verify_ids.append(profile_id)
        index = len(self.verify_ids) - 1
        if self.scan.start_profile_verification(index, name, row, minutes):
            self.tabs.setCurrentIndex(0)

    def profile_verified(self, index, passed):
        if 0 <= index < len(self.verify_ids):
            self.profiles.verification_finished(self.verify_ids[index], passed)

    # ---------- Prototype controls ----------

    def open_prototype_controls(self):
        self.prototype.launcherWriteState = self.library.prototype.get(
            "launcherWriteState", self.prototype.launcherWriteState
        )
        if self.prototype_dialog is None:
            self.prototype_dialog = PrototypeControlsDialog(self.prototype, self)
            self.prototype_dialog.changed.connect(self.set_prototype)
            self.prototype_dialog.action_requested.connect(self.prototype_action)
        self.prototype_dialog.sync()
        self.prototype_dialog.show()
        self.prototype_dialog.raise_()
        self.prototype_dialog.activateWindow()

    def set_prototype(self, key, value):
        if hasattr(self.prototype, key):
            setattr(self.prototype, key, value)
        state = {key: value}
        self.scan.set_prototype(state)
        self.library.set_prototype(state)
        self.overlay.panel.set_prototype(state)
        if key in ("twoGpus", "noGpu"):
            gpus = 0 if self.prototype.noGpu else 2 if self.prototype.twoGpus else 1
            self.profile_source.set_gpu_count(gpus)
        elif key == "daemon":
            self.profile_source.set_daemon_available(value != "missing")
        elif key == "processFailure":
            self.profile_source.fail_next_command = bool(value)
        if key == "noGpu" and value:
            # As at startup: the warning follows the window painting.
            C.QTimer.singleShot(0, self.run_startup_checks)

    def library_prototype_changed(self, key, value):
        """A launcher action (Initialize / Restart Steam) changed a simulated
        state: the controls show it."""
        if getattr(self.prototype, key, value) != value:
            setattr(self.prototype, key, value)
        if self.prototype_dialog is not None:
            self.prototype_dialog.sync()

    def preselect_gpu(self, index):
        """Start on GPU ``index``; the sample's second card is GPU 1."""
        index = max(0, min(1, int(index)))
        if index and not self.prototype.twoGpus:
            self.prototype.twoGpus = True
            self.set_prototype("twoGpus", True)
        self.scan.gpu_index = index
        self.scan.gpu_caption.setText(f"GPU {index} · " + (GPU, SECOND_GPU)[index])
        self.profiles.set_target_gpu(index)

    def prototype_consumed(self, key):
        if key == "processFailure":
            self.profile_source.fail_next_command = False
        if hasattr(self.prototype, key):
            setattr(self.prototype, key, False)
        if self.prototype_dialog is not None:
            self.prototype_dialog.sync()

    def daemon_installed(self):
        self.prototype.daemon = "ready"
        self.profile_source.set_daemon_available(True)
        if self.prototype_dialog is not None:
            self.prototype_dialog.sync()

    def prototype_action(self, name):
        if self.prototype_dialog is not None:
            self.prototype_dialog.close()
        if name == "startupChecks":
            self.run_startup_checks()
        elif name in ("libraryFirstScan", "externalGame"):
            self.tabs.setCurrentIndex(2)
            self.back_to_library()
            self.library.prototype_action(name)
        elif name == "overlayShortcut":
            self.tabs.setCurrentIndex(3)
            self.overlay.panel.press_cycle_shortcut()
        elif name == "errorReport":
            show_error_dialog(
                QtCore=C,
                QtGui=G,
                QtWidgets=W,
                parent=self,
                title="Auto-UV failed",
                message=(
                    "Auto-UV scan stopped unexpectedly. "
                    "The full error details can be copied for troubleshooting."
                ),
                details=process_failure_details(
                    action_label="Auto-UV scan",
                    exit_code=1,
                    exit_status="NormalExit",
                    log_tail="\n".join(SAMPLE_ERROR_LOG),
                ),
            )

    def run_startup_checks(self):
        """ui/window.py _run_startup_checks with the simulated machine: the
        no-NVIDIA warning only without a GPU, then an outdated service."""
        if self.prototype.noGpu:
            W.QMessageBox.warning(self, NO_NVIDIA_TITLE, NO_NVIDIA_TEXT)
        if self.prototype.daemon == "outdated":
            self.scan.ensure_daemon_ready("Updating the PenguinBurner hardware service")

    # ---------- shell ----------

    @staticmethod
    def shell_section(name):
        section = W.QWidget()
        section.setObjectName(name)
        section.setAttribute(C.Qt.WidgetAttribute.WA_StyledBackground, True)
        return section

    def set_profiles_status(self, status):
        status = dict(status or {})
        busy = bool(status.get("busy"))
        self.status_sources["profilesBusy"] = status if busy else None
        if not busy:
            self.status_sources["profiles"] = status or None
        self.show_status()
        self.set_overlay_tier(self.running_tier())

    def set_global_status(self, status, source="scan"):
        """Two-line window status above the tabs; None clears that source."""
        status = status or {}
        self.status_sources[source] = (
            status if status.get("running") or status.get("detail") else None
        )
        self.show_status()

    def show_status(self):
        source, status = next(
            ((k, v) for k, v in self.status_sources.items() if v), ("", {})
        )
        running, detail = status.get("running"), status.get("detail")
        if not (running or detail):
            running = "Running stock settings"
            detail = "At startup: stock settings · GPU 0 · RTX 5080"
        # A dot marks activity (accent) or a failure (loss / warning).
        tone = status.get("tone") or ""
        prefix = ""
        if source in ("scan", "profilesBusy", "library") or tone:
            dot = {
                "error": COL["loss"],
                "loss": COL["loss"],
                "warn": COL["warning"],
                "warning": COL["warning"],
            }.get(tone, COL["accent"])
            prefix = f'<span style="color:{dot}">●</span>&nbsp;'
        self.status_running.set_full_text(running or "", prefix)
        self.status_detail.set_full_text(detail or "")
        self.status_band.setToolTip("\n".join(filter(None, (running, detail))))

    def set_header_progress(self, text, pct=0, tooltip=""):
        """Header progress bar, e.g. "Downloading dependencies"; None hides it."""
        if text is None:
            self.header_progress.hide()
            return
        self.header_progress.setValue(max(0, min(100, int(pct))))
        self.header_progress.setFormat(str(text))
        self.header_progress.setToolTip(tooltip or text)
        self.header_progress.show()

    def change_page(self, i):
        # The header's Setup action is the filled primary only on Auto UV;
        # its icon follows the text colour of that role.
        setup = self.scan.setup_button
        setup.setProperty("headerRole", "" if i == 0 else "secondary")
        setup.setIcon(glyph_icon("auto-uv", "onPrimary" if i == 0 else "ink"))
        setup.style().unpolish(setup)
        setup.style().polish(setup)
        self.stack.setCurrentWidget(
            (self.scan, self.profiles, self.library_view, self.overlay)[i]
        )
        if i == 3:
            self.set_overlay_tier(self.running_tier())

    def running_tier(self):
        """The overlay HUD's tier row: Adaptive's live tier, the running
        profile's tier, or Default at stock."""
        runtime = self.profile_source.runtime()
        if runtime.running_adaptive and runtime.live_tier:
            return runtime.live_tier
        selector = runtime.running_selector
        if not selector or selector == STOCK:
            return "Default"
        profile = self.profiles.profile(selector) or {}
        tier = str(profile.get("profile_tier") or "").title()
        return tier if tier in TIERS else "Custom"

    def set_overlay_tier(self, tier):
        panel = self.overlay.panel
        if panel.running_tier != str(tier or "Default"):
            panel.running_tier = str(tier or "Default")
            panel.refresh_preview()

    def closeEvent(self, event):
        self.scan.timer.stop()
        self.perf_page.timer.stop()
        self.library.perf_timer.stop()
        super().closeEvent(event)

    # ---------- Appearance ----------

    def show_appearance(self):
        dialog = AppearanceDialog(design.APPEARANCE, self)
        dialog.applied.connect(lambda settings: apply_appearance(self, settings))
        dialog.open()
        self.appearance_dialog = dialog


def apply_appearance(window, settings=None):
    """Restyle the app and rebuild the window on the same tab and geometry.

    Pages set their own style sheets, chart pens and painted icons when they
    are built, so a rebuild is what puts every one in the new theme. In the
    mock that resets the sample session (scan, edited profiles); the real
    window rebuilds its pages over the live runtime state instead.
    """
    app = W.QApplication.instance()
    install_theme(app, settings)
    replacement = Preview()
    replacement.setGeometry(window.geometry())
    replacement.tabs.setCurrentIndex(window.tabs.currentIndex())
    if window.isVisible():
        # Show the new window before closing the old one, so closing the
        # last window never quits the app.
        replacement.show()
        window.close()
    window.deleteLater()
    CURRENT["window"] = replacement
    return replacement


# The live mock window, so a desktop colour-scheme change can rebuild it.
CURRENT: dict[str, Preview | None] = {"window": None}


def follow_desktop_scheme(_scheme=None):
    """Auto theme: rebuild when the desktop switches light/dark."""
    window = CURRENT["window"]
    if window is None or design.APPEARANCE.theme != "auto":
        return
    if design.resolved_mode(design.APPEARANCE) != design.MODE["current"]:
        apply_appearance(window)


def configure_app(app):
    app.setApplicationName("PenguinBurner UX preview")
    apply_dark_palette(app, G)
    install_theme(app)
    G.QGuiApplication.styleHints().colorSchemeChanged.connect(follow_desktop_scheme)


def gpu_index_arg(argv):
    """--gpu-index N / --index N preselects a GPU, as the app's launcher does."""
    for flag in ("--gpu-index", "--index"):
        if flag in argv[:-1]:
            value = argv[argv.index(flag) + 1]
            if value.isdigit():
                return int(value)
    return None


def main():
    app = W.QApplication(sys.argv)
    configure_app(app)
    win = Preview(history="--history" in sys.argv, overlay="--overlay" in sys.argv)
    CURRENT["window"] = win
    index = gpu_index_arg(sys.argv)
    if index is not None:
        win.preselect_gpu(index)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
