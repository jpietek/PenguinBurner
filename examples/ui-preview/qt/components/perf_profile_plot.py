"""Game Perf Profile: the frame-ring model and the frametime figure.

Ported from PR #137 (``origin/build-pr-137`` @ 611e6d3):
``ui/components/game_perf_profile_panel.py`` (``densify_frametimes``,
``hitch_points``, ``live_tail``, the y-ceiling, ticks, captions and refresh
cadence) and ``runtime/frame_history.py`` (floor-index percentiles and the
5-minute qualify gate). The algorithms are kept equivalent to that commit;
the figure is restyled to the preview's green design tokens (``ui/design/style.py``).

The model half is pure Python and knows nothing about where a ring comes
from: a page receives a ``PerfProfileSource`` (the preview passes the sample
adapter in ``qt.sample.perf_rings``; the real app would pass a daemon
adapter reading ``runtime/frame_history``).
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Protocol

import pyqtgraph as pg
from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from ui.components.toggle_switch import toggle_switch_class
from ui.design.style import (
    COL,
    GAP,
    TYPE,
    WEIGHT,
    apply_chart_theme,
    chart_font,
    qcolor,
    style_value_badge,
)

# ---------- PR #137 constants ----------
QUALIFY_MINUTES = 5.0
REFRESH_MS = 2000
# A recording ring flushes once a second; the Live zoom matches it.
LIVE_REFRESH_MS = 1000
OVERVIEW_COLUMNS = 300
# Below ~100 frames a column's p99 is simply its worst frame; short or
# low-fps windows get fewer, wider columns instead.
MIN_COLUMN_FRAMES = 200
LIVE_WINDOW_MS = 10_000.0
LIVE_WINDOWS_S = (10, 30, 60)
STUTTER_RATIO = 1.6
STUTTER_MILD_MS = 16.7
STUTTER_SEVERE_MS = 33.3
# An overview hitch: over 33 ms and twice its column's median, so a 60 fps
# cap in a faster game, or a whole 30 fps game, is not one long stutter.
HITCH_RATIO = 2.0
# Overview dots are always severe (every hitch is over 33 ms, PR #137); only
# their size grows with the hitch, up to this.
HITCH_WORST_MS = 100.0
OVERVIEW_CAPTION = (
    "line: median frametime · band: up to the 1% worst frames · "
    f"dots: hitches over {STUTTER_SEVERE_MS:.0f} ms"
)
LIVE_CAPTION = (
    "flat & low = smooth · stutter needles yellow -> red by severity "
    "(red ~30 ms) · lines: median & 1%-low (p99)"
)
# The plot never shrinks below this; the page scrolls instead.
PLOT_MIN_HEIGHT = 260

TIER_EFFICIENCY = "Efficiency"
TIER_BALANCED = "Balanced"
TIER_PERFORMANCE = "Performance"
TIERS = (TIER_EFFICIENCY, TIER_BALANCED, TIER_PERFORMANCE)


# ---------- Model ----------
@dataclass(frozen=True)
class PerfSummary:
    """The rolled-up numbers the cards, fingerprint and peek are drawn from.

    Field for field ``runtime/frame_history.FrameHistorySummary`` (PR #137),
    so a daemon adapter passes the real summary through unchanged and the
    cards show the daemon's numbers, never a re-computation.
    """

    minutes: float
    qualified: bool
    tier: str
    bottleneck: str
    median_present_fps: float
    low_1pct_fps: float
    median_frametime_ms: float
    p99_frametime_ms: float
    median_power_w: int
    median_clock_mhz: int
    gpu_util_pct: int
    cpu_peak_thread_pct: int
    latency_ms: float
    framegen_seen: bool
    median_display_latency_ms: float = 0.0
    median_temperature_c: float = 0.0
    median_fan_pct: float = 0.0
    latency_estimated: bool = False


@dataclass(frozen=True, eq=False)
class PerfRecording:
    """One game's frame ring (oldest first) plus what the daemon knows of it.

    ``summary`` comes from the source (the daemon's summary, or ``None``
    before a ring has frames). ``tier`` is "" for an untuned session.
    ``target_is_game`` says whether ``target_fps`` is the game's own override
    or the system-wide target. ``version`` changes whenever the ring or the
    readings change, so a view can skip redrawing the same picture.
    """

    game_key: str
    frametimes_ms: tuple[float, ...]
    live: bool
    tier: str
    target_fps: float
    target_is_game: bool
    power_limit_w: int
    summary: PerfSummary | None
    version: int = 0

    @cached_property
    def columns(self) -> tuple[tuple[float, ...], ...]:
        return densify_frametimes(self.frametimes_ms)


class PerfProfileSource(Protocol):
    """What the Perf Profile page and its library entry read.

    ``record`` returns ``None`` when the game has no ring yet. A live ring is
    still recording; calling ``record`` again returns the grown ring.
    ``has_recording`` answers without decoding a ring (the library's
    Recording filter asks it for every game).
    """

    def game_name(self, game_key: str) -> str: ...

    def record(self, game_key: str) -> PerfRecording | None: ...

    def has_recording(self, game_key: str) -> bool: ...

    def capture_enabled(self, game_key: str) -> bool: ...


# ---------- Pure maths (PR #137) ----------
def percentile(sorted_values, fraction: float) -> float:
    """Floor-indexed percentile of sorted values: the p99 of 100 frames is the
    single worst frame, matching the gaming "1%-low" convention."""
    if not sorted_values:
        return 0.0
    count = len(sorted_values)
    return sorted_values[min(count - 1, max(0, math.floor(fraction * count)))]


def densify_frametimes(
    frametimes_ms: tuple[float, ...], columns: int = OVERVIEW_COLUMNS
) -> tuple[tuple[float, ...], ...]:
    """Bucket a frame ring into plot columns: (x_minutes, p50_ms, p99_ms, max_ms).

    x is minutes relative to now (0 = newest), measured at the column's end.
    A column holds at least ``MIN_COLUMN_FRAMES`` frames so its p99 is not
    just its worst frame.
    """
    if not frametimes_ms:
        return (), (), (), ()
    total_ms = sum(frametimes_ms)
    bucket = max(1, len(frametimes_ms) // columns, MIN_COLUMN_FRAMES)
    xs: list[float] = []
    medians: list[float] = []
    p99s: list[float] = []
    maxes: list[float] = []
    elapsed = 0.0
    for start in range(0, len(frametimes_ms), bucket):
        chunk = sorted(frametimes_ms[start : start + bucket])
        elapsed += sum(chunk)
        xs.append((elapsed - total_ms) / 60_000.0)
        medians.append(chunk[len(chunk) // 2])
        p99s.append(chunk[min(len(chunk) - 1, int(len(chunk) * 0.99))])
        maxes.append(chunk[-1])
    return tuple(xs), tuple(medians), tuple(p99s), tuple(maxes)


def hitch_points(
    xs: tuple[float, ...], medians: tuple[float, ...], maxes: tuple[float, ...]
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Overview columns whose worst frame is a hitch: (x_minutes, worst_ms)."""
    points = [
        (x, worst)
        for x, median, worst in zip(xs, medians, maxes, strict=True)
        if worst >= max(STUTTER_SEVERE_MS, median * HITCH_RATIO)
    ]
    return tuple(x for x, _ in points), tuple(worst for _, worst in points)


def live_tail(
    frametimes_ms: tuple[float, ...], window_ms: float = LIVE_WINDOW_MS
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """The last window of frames at full resolution: (x_seconds, frametime).

    x is the frame's start in seconds ago; the frame that crosses the window
    edge is left out once there is at least one value.
    """
    xs: list[float] = []
    values: list[float] = []
    elapsed = 0.0
    for frametime in reversed(frametimes_ms):
        elapsed += frametime
        if elapsed > window_ms and values:
            break
        xs.append(-elapsed / 1000.0)
        values.append(frametime)
    xs.reverse()
    values.reverse()
    return tuple(xs), tuple(values)


def frametime_ceiling(median_ms: float, p99_ms: float) -> float:
    """The y-ceiling, rounded to 5 ms so the top tick and range end coincide;
    spikes keep their height up to it, extremes clamp."""
    return float(math.ceil(max(20.0, median_ms * 2.4, p99_ms * 1.85) / 5.0) * 5)


def y_tick_step(ceiling: float) -> int:
    return 5 if ceiling <= 25 else 10 if ceiling <= 50 else 20


def overview_tick_step(span_minutes: float) -> float:
    """The smallest round "minutes ago" step that gives six ticks or fewer."""
    for step in (0.1, 0.2, 0.5, 1, 2, 5, 10, 15):
        if span_minutes / step <= 6:
            return float(step)
    return 15.0


def live_tick_step(window_s: float) -> float:
    return 2.0 if window_s <= 10 else 5.0 if window_s <= 30 else 10.0


def _mix_hex(start: str, end: str, t: float) -> str:
    t = min(1.0, max(0.0, t))
    a, b = G.QColor(start), G.QColor(end)
    return G.QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    ).name()


def stutter_color(frametime_ms: float) -> str:
    """Live needle colour: warning at 16.7 ms to severe at 33.3 ms, from the
    true value before the plot clamps it."""
    span = STUTTER_SEVERE_MS - STUTTER_MILD_MS
    return _mix_hex(
        COL["warning"], COL["severe"], (frametime_ms - STUTTER_MILD_MS) / span
    )


def hitch_color(_frametime_ms: float) -> str:
    """Overview dot colour: full severe, as in PR #137. Every overview hitch
    is at least 33.3 ms; amber stays reserved for the Live view's mild
    stutters."""
    return COL["severe"]


def clipped_caption(caption: str, worst_ms: float, ceiling_ms: float) -> str:
    """A 250 ms hitch must not silently render like a 20 ms blip: disclose the
    true worst frame whenever the plot clamps it."""
    if worst_ms > ceiling_ms:
        return f"{caption} · worst frame {worst_ms:.0f} ms (clipped)"
    return caption


def warming_text(minutes: float) -> str:
    # Floor, not round: 4.6 min must not read "5 of 5" while the gate is shut.
    return f"Warming up · {int(minutes)} of {QUALIFY_MINUTES:.0f} min captured"


def latency_text(summary: PerfSummary) -> str:
    """The session latency the way the overlay writes LAT: `~` marks an estimate."""
    return f"{'~' if summary.latency_estimated else ''}{summary.latency_ms:.0f} ms"


def meta_parts(recording: PerfRecording, summary: PerfSummary) -> list[str]:
    """The line under the game title: how much window is behind the numbers."""
    minutes = summary.minutes
    parts: list[str] = []
    if recording.live:
        parts.append("● REC")
    parts.append(
        f"{minutes:.1f} min captured"
        if minutes < 10.0
        else f"{minutes:.0f} min captured"
    )
    parts.append(f"{len(recording.frametimes_ms):,} frames")
    if not summary.qualified:
        parts.append(f"settling · fingerprint at {QUALIFY_MINUTES:.0f} min")
    if summary.framegen_seen:
        parts.append("frame-gen seen")
    return parts


def meta_text(recording: PerfRecording, summary: PerfSummary) -> str:
    return " · ".join(meta_parts(recording, summary))


def tier_label(tier: str) -> str:
    return tier if tier in TIERS else "Untuned"


def tier_color(tier: str) -> str:
    """Tier colour for the chip; untuned reads quiet."""
    return COL[tier.lower()] if tier in TIERS else COL["quiet"]


def fingerprint_color(tier: str) -> str:
    """The radar takes the recording's tier colour, accent when untuned."""
    return COL[tier.lower()] if tier in TIERS else COL["accent"]


def refresh_interval_ms(recording: PerfRecording | None, live_mode: bool) -> int:
    """1 Hz only where it shows: a recording ring in the Live zoom. An
    overview column spans seconds of play, so a faster tick would redraw the
    same picture."""
    return (
        LIVE_REFRESH_MS
        if recording is not None and recording.live and live_mode
        else REFRESH_MS
    )


# ---------- Figure ----------
class _EdgeLabelAxis(pg.AxisItem):
    """Keeps the edge tick labels ("0" at the origin) that stock pyqtgraph
    culls, and prints bottom ticks unsigned: the title already says "ago"."""

    def boundingRect(self):
        rect = super().boundingRect()
        if self.orientation in ("left", "right"):
            return rect.adjusted(0, -12, 0, 12)
        return rect.adjusted(-12, 0, 12, 0)

    def tickStrings(self, values, scale, spacing):
        if self.orientation == "bottom":
            return [f"{abs(value * scale):g}" for value in values]
        return super().tickStrings(values, scale, spacing)


class _ReferenceAxis(pg.AxisItem):
    """Right-edge gutter labels for the median and p99 reference lines,
    each in its line's colour and nudged apart when the lines sit close."""

    def __init__(self):
        super().__init__("right")
        self._colors: dict[str, G.QColor] = {}

    def set_references(self, refs: list[tuple[float, str, str]]) -> None:
        self._colors = {text: qcolor(color) for _value, text, color in refs}
        self.setTicks([[(value, text) for value, text, _color in refs]])

    def boundingRect(self):
        return super().boundingRect().adjusted(0, -12, 0, 12)

    def drawPicture(self, p, axisSpec, tickSpecs, textSpecs):
        p.setRenderHint(G.QPainter.RenderHint.TextAntialiasing, True)
        specs = sorted(textSpecs, key=lambda spec: spec[0].center().y())
        if len(specs) == 2:
            upper, lower = specs[0][0], specs[1][0]
            gap = upper.height() + 2 - (lower.center().y() - upper.center().y())
            if gap > 0:
                upper.translate(0, -gap / 2)
                lower.translate(0, gap / 2)
        p.setFont(chart_font())
        for rect, flags, text in specs:
            p.setPen(self._colors.get(text, qcolor("secondary")))
            p.drawText(rect, int(flags), text)


class _FrametimePlot(pg.PlotWidget):
    """The plot with a keyboard probe: arrows, Page Up/Down, Home/End, Esc."""

    probe_key = C.Signal(str)
    probe_leave = C.Signal()
    probe_move = C.Signal(C.QPointF)

    def mouseMoveEvent(self, ev):
        super().mouseMoveEvent(ev)
        self.probe_move.emit(self.mapToScene(ev.position().toPoint()))

    def keyPressEvent(self, ev):
        names = {
            C.Qt.Key.Key_Left: "left",
            C.Qt.Key.Key_Right: "right",
            C.Qt.Key.Key_PageUp: "pageup",
            C.Qt.Key.Key_PageDown: "pagedown",
            C.Qt.Key.Key_Home: "home",
            C.Qt.Key.Key_End: "end",
            C.Qt.Key.Key_Escape: "escape",
        }
        name = names.get(C.Qt.Key(ev.key()))
        if name is None:
            super().keyPressEvent(ev)
            return
        self.probe_key.emit(name)
        ev.accept()

    def leaveEvent(self, ev):
        super().leaveEvent(ev)
        if not self.hasFocus():
            self.probe_leave.emit()

    def focusOutEvent(self, ev):
        super().focusOutEvent(ev)
        self.probe_leave.emit()


class FocusRingSwitch:
    """The production toggle switch with the preview's 2 px keyboard focus ring."""

    _cls: type | None = None

    @classmethod
    def create(cls, parent=None):
        if cls._cls is None:
            base = toggle_switch_class(C, G, W)

            class _Switch(base):  # type: ignore[valid-type, misc]
                def __init__(self, parent=None):
                    super().__init__(parent)
                    self._keyboard_focus = False
                    self.setFocusPolicy(C.Qt.FocusPolicy.StrongFocus)

                def focusInEvent(self, event):
                    self._keyboard_focus = event.reason() in (
                        C.Qt.FocusReason.TabFocusReason,
                        C.Qt.FocusReason.BacktabFocusReason,
                        C.Qt.FocusReason.ShortcutFocusReason,
                    )
                    super().focusInEvent(event)

                def focusOutEvent(self, event):
                    self._keyboard_focus = False
                    super().focusOutEvent(event)

                def paintEvent(self, event):
                    super().paintEvent(event)
                    if not (self.hasFocus() and self._keyboard_focus):
                        return
                    painter = G.QPainter(self)
                    painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
                    pen = G.QPen(qcolor("focus"), 2)
                    painter.setPen(pen)
                    painter.setBrush(C.Qt.BrushStyle.NoBrush)
                    rect = C.QRectF(self.rect()).adjusted(1, 1, -1, -1)
                    painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
                    painter.end()

            cls._cls = _Switch
        switch = cls._cls(parent)
        # The switch slides in 130 ms; there is room for the 2 px ring.
        switch.setFixedSize(46, 24)
        return switch


class _FlowLayout(W.QLayout):
    """Left-to-right items that wrap, for a legend that must never clip."""

    def __init__(self, parent=None, *, h_spacing=6, v_spacing=4):
        super().__init__(parent)
        self._items: list[W.QLayoutItem] = []
        self._h, self._v = h_spacing, v_spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return C.Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._place(C.QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._place(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = C.QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + C.QSize(
            margins.left() + margins.right(), margins.top() + margins.bottom()
        )

    def _place(self, rect, *, apply):
        margins = self.contentsMargins()
        area = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        x, y, line = area.x(), area.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > area.right() + 1 and line > 0:
                x = area.x()
                y += line + self._v
                line = 0
            if apply:
                item.setGeometry(C.QRect(C.QPoint(x, y), hint))
            x += hint.width() + self._h
            line = max(line, hint.height())
        return y + line - rect.y() + margins.bottom()


class LegendSwatch(W.QWidget):
    """A painted legend key (never a Unicode glyph).

    kinds: "line", "band", "dots" (one severe hitch dot), "dot", "hollow",
    "gradient" (warning -> severe), "refs" (dashed median + dotted p99).
    """

    def __init__(self, kind: str, color: str = "accent", parent=None):
        super().__init__(parent)
        self.kind, self.color = kind, color
        width = {"dots": 10, "dot": 10, "hollow": 10, "refs": 34}.get(kind, 20)
        self.setFixedSize(width, 12)

    def paintEvent(self, _event):
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        mid = self.height() / 2
        width = self.width()
        kind = self.kind
        if kind == "line":
            painter.setPen(G.QPen(qcolor(self.color), 2))
            painter.drawLine(C.QPointF(0, mid), C.QPointF(width, mid))
        elif kind == "band":
            painter.setPen(C.Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(self.color, 0.28))
            painter.drawRoundedRect(C.QRectF(0, mid - 5, width, 10), 2, 2)
        elif kind == "gradient":
            gradient = G.QLinearGradient(0, 0, width, 0)
            gradient.setColorAt(0, qcolor("warning"))
            gradient.setColorAt(1, qcolor("severe"))
            painter.setPen(C.Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawRoundedRect(C.QRectF(0, mid - 4, width, 8), 2, 2)
        elif kind in ("dot", "dots", "hollow"):
            colors = ("severe",) if kind == "dots" else (self.color,)
            for index, color in enumerate(colors):
                center = C.QPointF(5 + index * 10, mid)
                if kind == "hollow":
                    painter.setPen(G.QPen(qcolor(color), 2))
                    painter.setBrush(C.Qt.BrushStyle.NoBrush)
                    painter.drawEllipse(center, 3.5, 3.5)
                else:
                    painter.setPen(C.Qt.PenStyle.NoPen)
                    painter.setBrush(qcolor(color))
                    painter.drawEllipse(center, 4, 4)
        elif kind == "refs":
            for start, color, dashes in (
                (0, "secondary", [5, 4]),
                (18, "quiet", [2, 3]),
            ):
                pen = G.QPen(qcolor(color), 1.5)
                pen.setDashPattern([d / 1.5 for d in dashes])
                painter.setPen(pen)
                painter.drawLine(C.QPointF(start, mid), C.QPointF(start + 16, mid))
        painter.end()


# The caption, split at " · " so each part carries its real swatch: the
# legend reads exactly as PR #137's caption sentence.
_CAPTION_SWATCHES = {
    "line: median frametime": ("line", "accent"),
    "band: up to the 1% worst frames": ("band", "accent"),
    f"dots: hitches over {STUTTER_SEVERE_MS:.0f} ms": ("dots", ""),
    "stutter needles yellow -> red by severity (red ~30 ms)": ("gradient", ""),
    "lines: median & 1%-low (p99)": ("refs", ""),
}


class CaptionLegend(W.QWidget):
    """PR #137's caption as an external legend with painted swatches."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("perfLegend")
        self._flow = _FlowLayout(self, h_spacing=6, v_spacing=4)
        self._flow.setContentsMargins(
            GAP["lg"], GAP["sm"] + 2, GAP["lg"], GAP["sm"] + 2
        )
        self.caption = ""
        size = self.sizePolicy()
        size.setHeightForWidth(True)
        self.setSizePolicy(size)

    def set_caption(self, caption: str) -> None:
        if caption == self.caption:
            return
        self.caption = caption
        while self._flow.count():
            item = self._flow.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        for index, part in enumerate(caption.split(" · ")):
            if index:
                sep = W.QLabel("·")
                sep.setObjectName("perfLegendSep")
                self._flow.addWidget(sep)
            entry = W.QWidget()
            row = W.QHBoxLayout(entry)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            swatch = _CAPTION_SWATCHES.get(part)
            if swatch is not None:
                row.addWidget(LegendSwatch(*swatch), 0, C.Qt.AlignmentFlag.AlignVCenter)
            text = W.QLabel(part)
            text.setObjectName(
                "perfLegendClip"
                if part.startswith("worst frame")
                else "perfLegendLabel"
            )
            row.addWidget(text)
            self._flow.addWidget(entry)
        self.updateGeometry()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._flow.heightForWidth(width)


@dataclass
class _ProbeModel:
    mode: str = ""
    xs: tuple[float, ...] = ()
    ys: tuple[float, ...] = ()
    texts: tuple[str, ...] = ()
    ceiling: float = 20.0
    index: int | None = None


class FrametimeFigure(W.QWidget):
    """The frametime plot and its legend; ``controls`` goes in the card header.

    Overview: the median line over a band reaching each column's 1% worst
    frames, graded hitch dots, and dashed median / p99 references labelled in
    the right gutter. Live: the last 10/30/60 s per frame with stutter
    needles graded warning -> severe. Hover or arrow keys read values.
    """

    live_changed = C.Signal(bool)
    window_changed = C.Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("perfFrametimeFigure")
        self._live = False
        self._probe = _ProbeModel()
        self.worst_ms = 0.0
        self.hitch_count = 0
        self.mode = ""

        # Header controls: the window select sits before the switch, so
        # showing it never moves the switch from under the pointer.
        self.controls = W.QWidget()
        self.controls.setObjectName("perfLiveControls")
        controls = W.QHBoxLayout(self.controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(GAP["sm"])
        self.window_combo = W.QComboBox()
        self.window_combo.setObjectName("perfLiveWindow")
        self.window_combo.setAccessibleName("Live window")
        for seconds in LIVE_WINDOWS_S:
            self.window_combo.addItem(f"{seconds} s", seconds)
        self.window_combo.setVisible(False)
        self.window_combo.currentIndexChanged.connect(self._window_changed)
        controls.addWidget(self.window_combo)
        self.live_label = W.QLabel("Live")
        self.live_label.setObjectName("perfLiveLabel")
        self.live_label.setCursor(C.Qt.CursorShape.PointingHandCursor)
        self.live_label.installEventFilter(self)
        controls.addWidget(self.live_label)
        self.live_switch = FocusRingSwitch.create()
        self.live_switch.setObjectName("perfLiveSwitch")
        self.live_switch.setAccessibleName("Live")
        self.live_switch.setToolTip(
            "Every frame of the last seconds, refreshed each second"
        )
        self.live_switch.toggled.connect(self.set_live)
        controls.addWidget(self.live_switch)

        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.plot = _FrametimePlot(
            axisItems={
                "left": _EdgeLabelAxis("left"),
                "bottom": _EdgeLabelAxis("bottom"),
                "right": _ReferenceAxis(),
            }
        )
        # pyqtgraph's PlotItem API is dynamic (its ``layout`` attribute
        # shadows QGraphicsWidget.layout()); one untyped handle keeps the
        # rest of the figure typed.
        self.item: Any = self.plot.getPlotItem()
        self.plot.setMouseTracking(True)
        self.plot.setObjectName("perfFrametimePlot")
        self.plot.setAccessibleName("Frametime")
        self._style_plot()
        self.plot.setMinimumHeight(PLOT_MIN_HEIGHT)
        policy = W.QSizePolicy.Policy
        self.plot.setSizePolicy(policy.Expanding, policy.Ignored)
        layout.addWidget(self.plot, 1)
        rule = W.QFrame()
        rule.setObjectName("perfRule")
        rule.setFixedHeight(1)
        layout.addWidget(rule)
        self.legend = CaptionLegend()
        layout.addWidget(self.legend)
        self._build_items()
        self.plot.probe_key.connect(self._probe_key)
        self.plot.probe_leave.connect(self._clear_probe)
        self.plot.probe_move.connect(self._mouse_moved)

    # ----- setup -----
    def _style_plot(self):
        plot = self.plot
        plot.setMenuEnabled(False)
        plot.setMouseEnabled(x=False, y=False)
        plot.hideButtons()
        plot.setAntialiasing(True)
        # Tab focus only: the ring shows for keyboard users, never after a click.
        plot.setFocusPolicy(C.Qt.FocusPolicy.TabFocus)
        plot.setCursor(C.Qt.CursorShape.CrossCursor)
        apply_chart_theme(plot, pg)
        self.item.layout.setContentsMargins(4, 10, 0, 4)
        plot.showAxis("right")
        right = plot.getAxis("right")
        right.setPen(pg.mkPen(G.QColor(0, 0, 0, 0)))
        right.setStyle(tickLength=0, tickTextOffset=GAP["sm"], tickFont=chart_font())
        right.setWidth(92)
        right.setGrid(False)
        for side in ("left", "bottom"):
            axis = plot.getAxis(side)
            axis.enableAutoSIPrefix(False)
            axis.setStyle(hideOverlappingLabels=False)
        label_style = {"color": COL["secondary"], "font-size": f"{TYPE['caption']}px"}
        plot.setLabel("left", "Frametime (ms)", **label_style)
        plot.setLabel("bottom", "Minutes ago", **label_style)
        self._label_style = label_style

    def _build_items(self):
        plot = self.plot
        accent = COL["accent"]
        self.ribbon_median = pg.PlotCurveItem([], [], pen=pg.mkPen(accent, width=1.6))
        self.ribbon_p99 = pg.PlotCurveItem([], [], pen=pg.mkPen(None))
        self.ribbon_fill = pg.FillBetweenItem(
            self.ribbon_median, self.ribbon_p99, brush=qcolor("accent", 0.16)
        )
        self.single_column = pg.ScatterPlotItem(
            x=[], y=[], size=6, pen=pg.mkPen(None), brush=qcolor("accent")
        )
        self.hitch_dots = pg.ScatterPlotItem(
            x=[], y=[], pen=pg.mkPen(COL["plotSurface"], width=1.5)
        )
        self.hitch_dots.setZValue(10)
        for item in (
            self.ribbon_fill,
            self.ribbon_median,
            self.single_column,
            self.hitch_dots,
        ):
            self.item.addItem(item)
        self.live_curve = plot.plot(
            [],
            [],
            pen=pg.mkPen(accent, width=1.4),
            fillLevel=0.0,
            brush=qcolor("accent", 0.10),
        )
        self.stutter_bars = pg.BarGraphItem(
            x=[], height=[], width=0.03, pen=None, brush=qcolor("severe")
        )
        self.stutter_bars.setZValue(5)
        self.item.addItem(self.stutter_bars)
        median_pen = pg.mkPen(COL["secondary"], width=1)
        median_pen.setDashPattern([5, 4])
        p99_pen = pg.mkPen(COL["quiet"], width=1)
        p99_pen.setDashPattern([2, 3])
        self.median_line = pg.InfiniteLine(angle=0, movable=False, pen=median_pen)
        self.p99_line = pg.InfiniteLine(angle=0, movable=False, pen=p99_pen)
        for line in (self.median_line, self.p99_line):
            line.setZValue(4)
            self.item.addItem(line, ignoreBounds=True)
        crosshair = pg.mkPen(qcolor("ink", 0.45), width=1)
        self.probe_line = pg.InfiniteLine(angle=90, movable=False, pen=crosshair)
        self.probe_dot = pg.ScatterPlotItem(
            x=[],
            y=[],
            size=8,
            pen=pg.mkPen(COL["plotSurface"], width=2),
            brush=qcolor("candidateCurve"),
        )
        self.probe_badge = pg.TextItem("", anchor=(0.5, 1.0))
        style_value_badge(self.probe_badge, pg)
        for item, z in (
            (self.probe_line, 20),
            (self.probe_dot, 21),
            (self.probe_badge, 22),
        ):
            item.setZValue(z)
            item.setVisible(False)
            self.item.addItem(item, ignoreBounds=True)

    def eventFilter(self, watched, event):
        if (
            watched is self.live_label
            and event.type() == C.QEvent.Type.MouseButtonRelease
        ):
            self.live_switch.toggle()
            return True
        return super().eventFilter(watched, event)

    # ----- state -----
    @property
    def live(self) -> bool:
        return self._live

    @property
    def window_s(self) -> int:
        return int(self.window_combo.currentData() or LIVE_WINDOWS_S[0])

    def set_live(self, on: bool) -> None:
        on = bool(on)
        if self.live_switch.isChecked() != on:
            self.live_switch.setChecked(on)
            return
        if on == self._live:
            return
        self._live = on
        self.window_combo.setVisible(on)
        self._clear_probe()
        self.live_changed.emit(on)

    def set_window(self, seconds: int) -> None:
        index = self.window_combo.findData(int(seconds))
        if index >= 0:
            self.window_combo.setCurrentIndex(index)

    def _window_changed(self, _index):
        self._clear_probe()
        self.window_changed.emit(self.window_s)

    # ----- rendering -----
    def show_recording(self, recording: PerfRecording, summary: PerfSummary) -> None:
        ceiling = frametime_ceiling(
            summary.median_frametime_ms, summary.p99_frametime_ms
        )
        overview = not self._live
        for item in (
            self.ribbon_fill,
            self.ribbon_median,
            self.single_column,
            self.hitch_dots,
        ):
            item.setVisible(overview)
        for item in (self.live_curve, self.stutter_bars):
            item.setVisible(not overview)
        self.median_line.setValue(summary.median_frametime_ms)
        self.p99_line.setValue(summary.p99_frametime_ms)
        self.plot.getAxis("right").set_references(
            [
                (
                    summary.median_frametime_ms,
                    f"Median {summary.median_frametime_ms:.1f}",
                    "secondary",
                ),
                (
                    summary.p99_frametime_ms,
                    f"p99 {summary.p99_frametime_ms:.1f}",
                    "quiet",
                ),
            ]
        )
        if overview:
            caption = self._render_overview(recording, ceiling)
        else:
            caption = self._render_live(recording, summary, ceiling)
        self.legend.set_caption(caption)
        self._probe.ceiling = ceiling
        self._sync_probe()
        self._update_accessible(summary)

    def _render_overview(self, recording: PerfRecording, ceiling: float) -> str:
        self.mode = "overview"
        xs, medians, p99s, maxes = recording.columns
        hitch_x, hitch_y = hitch_points(xs, medians, maxes)
        self.worst_ms = max(hitch_y) if hitch_y else 0.0
        self.hitch_count = len(hitch_x)
        self.plot.setLabel("bottom", "Minutes ago", **self._label_style)
        if len(xs) > 1:
            self.ribbon_median.setData(list(xs), list(medians))
            self.ribbon_p99.setData(list(xs), list(p99s))
            self.single_column.setData(x=[], y=[])
        else:
            # One column has no line to draw: a dot marks the median.
            self.ribbon_median.setData([], [])
            self.ribbon_p99.setData([], [])
            self.single_column.setData(x=list(xs), y=list(medians))
        sizes, brushes = [], []
        for worst in hitch_y:
            t = min(
                1.0,
                max(
                    0.0,
                    (worst - STUTTER_SEVERE_MS) / (HITCH_WORST_MS - STUTTER_SEVERE_MS),
                ),
            )
            sizes.append(2 * (3 + 1.5 * t))
            brushes.append(pg.mkBrush(hitch_color(worst)))
        self.hitch_dots.setData(
            x=list(hitch_x),
            y=[min(y, ceiling) for y in hitch_y],
            size=sizes,
            brush=brushes,
        )
        span = abs(xs[0]) if xs else 1.0
        span = span or 1.0
        x0 = xs[0] if xs else -1.0
        lo, hi = x0 - span * 0.01, span * 0.01
        self._set_ranges(lo, hi, overview_tick_step(span), ceiling)
        self._probe.mode = "overview"
        self._probe.xs = xs
        self._probe.ys = medians
        self._probe.texts = tuple(
            f"{abs(x):.1f} min ago · median {m:.1f} · p99 {p:.1f} · worst {w:.0f} ms"
            for x, m, p, w in zip(xs, medians, p99s, maxes, strict=True)
        )
        return clipped_caption(OVERVIEW_CAPTION, self.worst_ms, ceiling)

    def _render_live(
        self, recording: PerfRecording, summary: PerfSummary, ceiling: float
    ) -> str:
        self.mode = "live"
        window_s = float(self.window_s)
        xs, values = live_tail(recording.frametimes_ms, window_s * 1000.0)
        threshold = summary.median_frametime_ms * STUTTER_RATIO
        spikes = [(x, v) for x, v in zip(xs, values, strict=True) if v >= threshold]
        self.worst_ms = max((v for _x, v in spikes), default=0.0)
        self.hitch_count = len(spikes)
        self.plot.setLabel("bottom", "Seconds ago", **self._label_style)
        self.live_curve.setData(list(xs), list(values))
        self.stutter_bars.setOpts(
            x=[x for x, _v in spikes],
            height=[min(v, ceiling) for _x, v in spikes],
            width=window_s * 0.003,
            # Colours come from the TRUE spike magnitude, before the clamp.
            brushes=[pg.mkBrush(stutter_color(v)) for _x, v in spikes],
            pens=[pg.mkPen(None) for _ in spikes],
        )
        self._set_ranges(
            -window_s - window_s * 0.03,
            window_s * 0.01,
            live_tick_step(window_s),
            ceiling,
        )
        self._probe.mode = "live"
        self._probe.xs = xs
        self._probe.ys = values
        self._probe.texts = tuple(
            f"{abs(x):.2f} s ago · {v:.1f} ms" for x, v in zip(xs, values, strict=True)
        )
        return clipped_caption(LIVE_CAPTION, self.worst_ms, ceiling)

    def _set_ranges(self, lo: float, hi: float, x_step: float, ceiling: float) -> None:
        step = y_tick_step(ceiling)
        self.plot.getAxis("left").setTicks(
            [[(v, str(v)) for v in range(0, int(ceiling) + 1, step)], []]
        )
        count = math.floor(abs(lo) / x_step + 1e-9)
        majors = [(-k * x_step, f"{k * x_step:g}") for k in range(count + 1)]
        self.plot.getAxis("bottom").setTicks([majors, []])
        self.item.setXRange(lo, hi, padding=0.0)
        # 4% headroom keeps the top tick's label unclipped; clamped needles
        # and dots still end exactly on the ceiling tick.
        self.item.setYRange(0.0, ceiling * 1.04, padding=0.0)

    def _update_accessible(self, summary: PerfSummary) -> None:
        if self.mode == "live":
            lead = f"Live frametime, last {self.window_s} seconds: "
            count = f"{self.hitch_count} stutter frames"
        else:
            span = abs(self._probe.xs[0]) if self._probe.xs else 0.0
            lead = f"Frametime overview, last {span:.0f} minutes: "
            count = f"{self.hitch_count} hitches"
        worst = f", worst {self.worst_ms:.0f} ms" if self.worst_ms else ""
        self.plot.setAccessibleDescription(
            f"{lead}median {summary.median_frametime_ms:.1f} ms, p99 "
            f"{summary.p99_frametime_ms:.1f} ms, {count}{worst}. "
            "Use the arrow keys to read values."
        )

    # ----- probe -----
    def _mouse_moved(self, scene_pos):
        view = self.item.getViewBox()
        if not view.sceneBoundingRect().contains(scene_pos) or not self._probe.xs:
            if self._probe.index is not None and not self.plot.hasFocus():
                self._clear_probe()
            return
        x = view.mapSceneToView(scene_pos).x()
        self._probe.index = self._nearest(x)
        self._sync_probe()

    def _nearest(self, x: float) -> int:
        xs = self._probe.xs
        index = bisect_left(xs, x)
        if index <= 0:
            return 0
        if index >= len(xs):
            return len(xs) - 1
        return index if abs(xs[index] - x) < abs(xs[index - 1] - x) else index - 1

    def _probe_key(self, name: str) -> None:
        count = len(self._probe.xs)
        if not count:
            return
        if name == "escape":
            self._clear_probe()
            return
        big = max(1, round(count / 20))
        current = count - 1 if self._probe.index is None else self._probe.index
        moves = {"left": -1, "right": 1, "pageup": -big, "pagedown": big}
        if name in moves:
            current += moves[name]
        elif name == "home":
            current = 0
        elif name == "end":
            current = count - 1
        self._probe.index = min(count - 1, max(0, current))
        self._sync_probe()

    def _clear_probe(self) -> None:
        self._probe.index = None
        self._sync_probe()

    def probe_text(self) -> str:
        index = self._probe.index
        return (
            self._probe.texts[index] if index is not None and self._probe.texts else ""
        )

    def _sync_probe(self) -> None:
        probe = self._probe
        if probe.index is not None and probe.index >= len(probe.xs):
            probe.index = len(probe.xs) - 1 if probe.xs else None
        visible = probe.index is not None
        for item in (self.probe_line, self.probe_dot, self.probe_badge):
            item.setVisible(visible)
        if not visible:
            return
        index = probe.index
        assert index is not None
        x, y = probe.xs[index], min(probe.ys[index], probe.ceiling)
        self.probe_line.setValue(x)
        self.probe_dot.setData(x=[x], y=[y])
        self.probe_badge.setText(probe.texts[index])
        style_value_badge(self.probe_badge, pg)
        lo, hi = self.item.getViewBox().viewRange()[0]
        fraction = (x - lo) / (hi - lo) if hi > lo else 0.5
        anchor_x = 0.0 if fraction < 0.2 else 1.0 if fraction > 0.8 else 0.5
        self.probe_badge.setAnchor((anchor_x, 1.0))
        # Frametimes never reach 0 ms, so the bottom strip is always free;
        # the top row is where clipped hitch dots sit.
        self.probe_badge.setPos(x, probe.ceiling * 0.03)


def figure_stylesheet() -> str:
    """Object-name rules for the figure; the page includes them in its sheet."""
    c, t, w = COL, TYPE, WEIGHT
    return f"""
        QLabel#perfLegendLabel {{ font-size: {t["caption"]}px; color: {c["secondary"]}; }}
        QLabel#perfLegendSep {{ font-size: {t["caption"]}px; color: {c["quiet"]}; }}
        QLabel#perfLegendClip {{ font-size: {t["caption"]}px; color: {c["warning"]}; font-weight: {w["semibold"]}; }}
        QLabel#perfLiveLabel {{ font-weight: {w["semibold"]}; color: {c["ink"]}; }}
        QComboBox#perfLiveWindow {{ font-size: {t["caption"]}px; padding: 4px 10px; min-width: 64px; }}
        QFrame#perfRule {{ background: {c["divider"]}; border: none; }}
        QGraphicsView#perfFrametimePlot {{ border: 2px solid transparent; background: {c["plotSurface"]}; }}
        QGraphicsView#perfFrametimePlot:focus {{ border: 2px solid {c["focus"]}; }}
    """
