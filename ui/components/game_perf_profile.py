"""Game Perf Profile: the five-spoke per-game telemetry fingerprint.

Pure axis math over a ``FrameHistorySummary`` plus QPainter rendering of the
radar (badge and labeled sizes), the warming-up placeholder, and the hover
peek popover. Everything is drawn natively — geometry, colors, and alpha
follow the approved mockup (``notes/game-perf-profile-mockup.html``) via the
``theme.PERF_PROFILE_*`` palette.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from profiles.uv.profile_tiers import (
    PROFILE_TIER_BALANCED,
    PROFILE_TIER_EFFICIENCY,
    PROFILE_TIER_LABELS,
    PROFILE_TIER_NONE,
    PROFILE_TIER_PERFORMANCE,
)
from runtime.frame_history import (
    QUALIFY_MINUTES,
    FrameHistorySummary,
)
from ui import theme

PERF_PROFILE_TIER_COLORS = {
    PROFILE_TIER_EFFICIENCY: theme.PERF_PROFILE_TIER_EFFICIENCY,
    PROFILE_TIER_BALANCED: theme.PERF_PROFILE_TIER_BALANCED,
    PROFILE_TIER_PERFORMANCE: theme.PERF_PROFILE_TIER_PERFORMANCE,
    PROFILE_TIER_NONE: theme.PERF_PROFILE_TIER_NONE,
}

# Axis normalization fallbacks: an unknown board limit reads against a
# desktop-class 350 W envelope, an unset per-game target against the global
# adaptive default.
_FALLBACK_POWER_LIMIT_W = 350.0
_FALLBACK_TARGET_FPS = 60.0

# Mockup geometry: fill alpha .18 labeled / .30 badge, 2 px / 1.6 px strokes,
# spokes start at 10% radius so a zero still reads as a vertex.
_FILL_ALPHA_LABELED = 72
_FILL_ALPHA_BADGE = 77
_POINT_FLOOR = 0.1

# The labeled radar mixes "more is better" spokes (FPS, LOW) with cost spokes
# (PWR, CPU), so each spoke carries the band that reads as fine; its vertex
# dot shows whether the value sits inside that band. The bounds
# are spoke fractions: FPS within 5% of target, 1%-low at 85%+ of the median,
# power under 60% of the board limit, the hottest CPU thread under 80%. GPU
# load has no band: high load is a bottleneck or full use depending on FPS.
_GOOD_FPS = (0.95, 1.0)
_GOOD_LOW = (0.85, 1.0)
_GOOD_PWR = (0.0, 0.6)
_GOOD_CPU = (0.0, 0.8)
_RING_FRACTIONS = (0.25, 0.5, 0.75, 1.0)
# Below this pixmap size the value line and ring numbers would crowd the
# spokes (the 124 px hover peek); codes and arrows still fit.
_DETAIL_LABEL_MIN_SIZE = 160

AXIS_HIGHER_IS_BETTER = "higher"
AXIS_LOWER_IS_CHEAPER = "lower"
_DIRECTION_ARROWS = {AXIS_HIGHER_IS_BETTER: "↑", AXIS_LOWER_IS_CHEAPER: "↓"}


@dataclass(frozen=True, slots=True)
class PerfProfileAxis:
    code: str
    label: str
    text: str
    fraction: float
    direction: str = ""
    good_band: tuple[float, float] | None = None

    @property
    def caption(self) -> str:
        """Spoke label: the code plus its reading direction, if it has one."""
        arrow = _DIRECTION_ARROWS.get(self.direction)
        return f"{self.code} {arrow}" if arrow else self.code

    @property
    def in_good_band(self) -> bool | None:
        """Whether the value sits in the spoke's fine band; None without one."""
        if self.good_band is None:
            return None
        low, high = self.good_band
        return low <= self.fraction <= high


def _clamp(fraction: float) -> float:
    return min(1.0, max(0.0, fraction))


def _spoke_reach(radius: float, fraction: float) -> float:
    """Distance from the center for a spoke fraction, shared by points and rings."""
    return radius * (_POINT_FLOOR + (1 - _POINT_FLOOR) * _clamp(fraction))


def latency_text(summary: FrameHistorySummary) -> str:
    """The session latency the way the overlay writes LAT: `~` marks an estimate."""
    return f"{'~' if summary.latency_estimated else ''}{summary.latency_ms:.0f} ms"


def tier_color(tier: str) -> str:
    return PERF_PROFILE_TIER_COLORS.get(tier, theme.PERF_PROFILE_TIER_NONE)


def tier_label(tier: str) -> str:
    return PROFILE_TIER_LABELS.get(tier, "Untuned")


def warming_text(minutes: float) -> str:
    # Floor, not round: 4.6 min must not read "5 of 5" while the gate is shut.
    return f"Warming up · {int(minutes)} of {QUALIFY_MINUTES:.0f} min captured"


def perf_profile_axes(
    summary: FrameHistorySummary,
    *,
    target_fps: float | None,
    power_limit_w: int,
) -> tuple[PerfProfileAxis, ...]:
    """The five spokes, each normalized against this machine or this game.

    PWR crowns the pentagon, the load pair (GPU, CPU) flanks it, and the
    experience pair (FPS, LOW) sits below. Latency has no spoke: it is not
    reliably measurable for non-Reflex games, so it appears only as numeric
    data where actually measured.
    """
    power_limit = float(power_limit_w) if power_limit_w > 0 else _FALLBACK_POWER_LIMIT_W
    target = float(target_fps) if target_fps and target_fps > 0 else _FALLBACK_TARGET_FPS
    median_fps = summary.median_present_fps
    low_ratio = (summary.low_1pct_fps / median_fps) if median_fps > 0 else 0.0
    return (
        PerfProfileAxis(
            "PWR",
            "Power draw",
            f"{summary.median_power_w} W",
            _clamp(summary.median_power_w / power_limit),
            AXIS_LOWER_IS_CHEAPER,
            _GOOD_PWR,
        ),
        PerfProfileAxis(
            "GPU",
            "GPU load",
            f"{summary.gpu_util_pct}%",
            _clamp(summary.gpu_util_pct / 100.0),
        ),
        PerfProfileAxis(
            "FPS",
            "Median FPS",
            f"{median_fps:.0f} fps",
            _clamp(median_fps / target),
            AXIS_HIGHER_IS_BETTER,
            _GOOD_FPS,
        ),
        PerfProfileAxis(
            "LOW",
            "Fluency floor",
            f"{low_ratio:.0%} of median",
            _clamp(low_ratio),
            AXIS_HIGHER_IS_BETTER,
            _GOOD_LOW,
        ),
        PerfProfileAxis(
            "CPU",
            "CPU hot thread",
            f"{summary.cpu_peak_thread_pct}%",
            _clamp(summary.cpu_peak_thread_pct / 100.0),
            AXIS_LOWER_IS_CHEAPER,
            _GOOD_CPU,
        ),
    )


def _vertex(center: float, radius: float, index: int, count: int, fraction: float):
    angle = -math.pi / 2 + (index / count) * 2 * math.pi
    reach = _spoke_reach(radius, fraction)
    return (
        center + math.cos(angle) * reach,
        center + math.sin(angle) * reach,
        angle,
    )


def _dot_radius(size: int) -> float:
    return max(3.0, size * 0.026)


def _label_font(QtGui, size: int, *, compact: bool):
    font = QtGui.QFont()
    families = getattr(font, "setFamilies", None)
    if families is not None:
        families(["JetBrains Mono", "DejaVu Sans Mono", "Monospace"])
    font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
    font.setPixelSize(max(8, round(size * (0.125 if compact else 0.052))))
    return font


def axis_label_lines(axis: PerfProfileAxis, *, detailed: bool) -> tuple[str, ...]:
    """Spoke label text: caption, plus the spoke percentage when there is room."""
    if not detailed:
        return (axis.caption,)
    return (axis.caption, f"{round(axis.fraction * 100)}%")


def axis_point_color(axis: PerfProfileAxis) -> str:
    """Vertex dot color: inside the fine band, outside it, or no band at all."""
    inside = axis.in_good_band
    if inside is None:
        return theme.PERF_PROFILE_POINT_NEUTRAL
    return theme.PERF_PROFILE_POINT_GOOD if inside else theme.PERF_PROFILE_POINT_OUTSIDE


def _draw_text_block(QtCore, painter, metrics, lines, *, center_x, center_y, size):
    """Draw centered lines around a point, clamped so no size clips them."""
    line_height = metrics.height()
    top = center_y - line_height * len(lines) / 2
    top = min(max(top, 0.0), size - line_height * len(lines))
    for row, line in enumerate(lines):
        width = metrics.horizontalAdvance(line)
        text_x = min(max(center_x - width / 2, 0.0), size - width)
        baseline = top + row * line_height + metrics.ascent()
        painter.drawText(QtCore.QPointF(text_x, baseline), line)


def perf_profile_pixmap(
    QtCore,
    QtGui,
    *,
    axes: tuple[PerfProfileAxis, ...] | None,
    tier: str,
    size: int,
    labels: bool = False,
    device_pixel_ratio: float = 1.0,
):
    """Render the fingerprint (or the dashed warming placeholder) to a pixmap.

    ``axes=None`` draws the warming-up state. ``labels=True`` is the large
    variant: rings at 25/50/75/100% of the spoke scale, captions with reading
    direction, and vertex dots colored by whether the value sits in the
    spoke's fine band. The badge is the bare shape — at miniature sizes
    the silhouette is the information. The labeled radius is computed from the
    label band's real font metrics so every caption always lands inside the
    pixmap, at any size.
    """
    ratio = max(1.0, float(device_pixel_ratio))
    pixmap = QtGui.QPixmap(round(size * ratio), round(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    try:
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        center = size / 2
        detailed = labels and size >= _DETAIL_LABEL_MIN_SIZE
        label_rows = 2 if detailed else 1
        if labels:
            font = _label_font(QtGui, size, compact=False)
            painter.setFont(font)
            metrics = QtGui.QFontMetricsF(font)
            # Room for a vertex dot on the outer ring, so it never meets its code.
            gap = max(2.0, size * 0.02) + _dot_radius(size)
            radius = size / 2 - (metrics.height() * label_rows + gap)
        else:
            metrics = None
            gap = 0.0
            radius = size * 0.42
        # The labeled chart sits on the app's card surface, where the mock's
        # near-black grid disappears; it takes the theme's card border instead.
        grid_pen = QtGui.QPen(
            QtGui.QColor(theme.BORDER_STRONG if labels else theme.PERF_PROFILE_GRID)
        )
        grid_pen.setWidthF(1.0)

        if axes is None:
            dashed = QtGui.QPen(QtGui.QColor(theme.PERF_PROFILE_AXIS))
            dashed.setWidthF(1.0)
            dashed.setStyle(QtCore.Qt.PenStyle.DashLine)
            painter.setPen(dashed)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QtCore.QPointF(center, center), radius, radius)
            return pixmap

        painter.setPen(grid_pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        # Rings sit where those spoke values land, floor included, so a vertex
        # on a ring really is at that percentage.
        ring_fractions = _RING_FRACTIONS if labels else (1.0,)
        for fraction in ring_fractions:
            reach = _spoke_reach(radius, fraction)
            painter.drawEllipse(QtCore.QPointF(center, center), reach, reach)

        count = len(axes)
        points = [
            _vertex(center, radius, index, count, axis.fraction)[:2]
            for index, axis in enumerate(axes)
        ]
        if metrics is not None:
            label_pen = QtGui.QPen(QtGui.QColor(theme.TEXT_MUTED))
            for index, axis in enumerate(axes):
                angle = -math.pi / 2 + (index / count) * 2 * math.pi
                cos, sin = math.cos(angle), math.sin(angle)
                painter.setPen(grid_pen)
                painter.drawLine(
                    QtCore.QPointF(center, center),
                    QtCore.QPointF(center + cos * radius, center + sin * radius),
                )
                lines = axis_label_lines(axis, detailed=detailed)
                distance = radius + gap + metrics.height() * len(lines) / 2
                painter.setPen(label_pen)
                _draw_text_block(
                    QtCore,
                    painter,
                    metrics,
                    lines,
                    center_x=center + cos * distance,
                    center_y=center + sin * distance,
                    size=size,
                )
            if detailed:
                _draw_ring_scale(QtCore, QtGui, painter, center, radius, count, size)

        color = QtGui.QColor(tier_color(tier))
        fill = QtGui.QColor(color)
        fill.setAlpha(_FILL_ALPHA_LABELED if labels else _FILL_ALPHA_BADGE)
        outline = QtGui.QPen(color)
        outline.setWidthF(2.4 if labels else 1.6)
        outline.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        polygon = QtGui.QPolygonF([QtCore.QPointF(x, y) for x, y in points])
        painter.setPen(outline)
        painter.setBrush(QtGui.QBrush(fill))
        painter.drawPolygon(polygon)

        if labels:
            ring = QtGui.QPen(QtGui.QColor(theme.SURFACE_BG))
            ring.setWidthF(1.5)
            dot_radius = _dot_radius(size)
            for axis, (x, y) in zip(axes, points, strict=True):
                painter.setPen(ring)
                painter.setBrush(QtGui.QBrush(QtGui.QColor(axis_point_color(axis))))
                painter.drawEllipse(QtCore.QPointF(x, y), dot_radius, dot_radius)
    finally:
        painter.end()
    return pixmap


def _draw_ring_scale(QtCore, QtGui, painter, center, radius, count, size):
    """Number the rings along the gap between the first two spokes."""
    font = QtGui.QFont(painter.font())
    font.setPixelSize(max(7, round(size * 0.04)))
    painter.setFont(font)
    metrics = QtGui.QFontMetricsF(font)
    angle = -math.pi / 2 + math.pi / count
    painter.setPen(QtGui.QPen(QtGui.QColor(theme.TEXT_DISABLED)))
    for fraction in _RING_FRACTIONS:
        reach = _spoke_reach(radius, fraction)
        text = f"{round(fraction * 100)}"
        x = center + math.cos(angle) * reach - metrics.horizontalAdvance(text) / 2
        y = center + math.sin(angle) * reach + metrics.ascent() / 2
        painter.drawText(QtCore.QPointF(x, y), text)


class GamePerfProfilePeek:
    """The hover popover: enlarged fingerprint plus the numbers that matter.

    A frameless tool-tip window drawn entirely with Qt widgets and the mock
    palette; the badge click (not this popover) opens the Game Perf Profile tab.
    """

    def __init__(self, *, QtCore, QtGui, QtWidgets, parent=None) -> None:
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        flags = (
            QtCore.Qt.WindowType.ToolTip
            | QtCore.Qt.WindowType.FramelessWindowHint
        )
        # Parented so the popover is torn down with its owner; the ToolTip
        # window flag keeps it a floating top-level window regardless.
        self.widget = QtWidgets.QFrame(parent, flags)
        self.widget.setObjectName("gamePerfProfilePeek")
        self.widget.setStyleSheet(
            f"""
            QFrame#gamePerfProfilePeek {{
                background: {theme.PERF_PROFILE_SURFACE};
                border: 1px solid {theme.PERF_PROFILE_BORDER_STRONG};
                border-radius: 9px;
            }}
            QLabel#gamePerfProfilePeekKey {{
                font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
                color: {theme.PERF_PROFILE_TEXT_DIM}; font-size: 11px;
                background: transparent; border: none;
            }}
            QLabel#gamePerfProfilePeekValue {{
                font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
                color: {theme.PERF_PROFILE_TEXT}; font-size: 11px; font-weight: 600;
                background: transparent; border: none;
            }}
            QLabel#gamePerfProfilePeekHint {{
                font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
                color: {theme.PERF_PROFILE_TEXT_MUTED}; font-size: 10px;
                background: transparent; border: none;
                border-top: 1px solid {theme.PERF_PROFILE_BORDER};
                padding-top: 6px;
            }}
            """
        )
        layout = QtWidgets.QVBoxLayout(self.widget)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        # No title: the popover hangs off the badge in the game header, which
        # already names the game right beside it.
        body = QtWidgets.QHBoxLayout()
        body.setSpacing(12)
        self.profile_label = QtWidgets.QLabel("")
        self.profile_label.setFixedSize(124, 124)
        body.addWidget(self.profile_label, 0)
        stats = QtWidgets.QGridLayout()
        stats.setHorizontalSpacing(18)
        stats.setVerticalSpacing(4)
        self._stat_values: dict[str, Any] = {}
        self._stat_keys: dict[str, Any] = {}
        # Latency rides fifth, and only for games with a real marker source.
        for row, key in enumerate(
            ("Median", "1%-low", "Power", "Latency")
        ):
            key_label = QtWidgets.QLabel(key)
            key_label.setObjectName("gamePerfProfilePeekKey")
            value_label = QtWidgets.QLabel("")
            value_label.setObjectName("gamePerfProfilePeekValue")
            value_label.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignRight
                | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            stats.addWidget(key_label, row, 0)
            stats.addWidget(value_label, row, 1)
            self._stat_values[key] = value_label
            self._stat_keys[key] = key_label
        stats.setRowStretch(5, 1)
        body.addLayout(stats, 1)
        layout.addLayout(body)
        self.hint = QtWidgets.QLabel("Click the badge to open the Game Perf Profile tab")
        self.hint.setObjectName("gamePerfProfilePeekHint")
        layout.addWidget(self.hint)

    def show_for(
        self,
        *,
        summary: FrameHistorySummary,
        target_fps: float | None,
        power_limit_w: int,
        global_pos,
        device_pixel_ratio: float = 1.0,
        align_right: bool = False,
    ) -> None:
        axes = perf_profile_axes(summary, target_fps=target_fps, power_limit_w=power_limit_w)
        self.profile_label.setPixmap(
            perf_profile_pixmap(
                self.QtCore,
                self.QtGui,
                axes=axes,
                tier=summary.tier,
                size=124,
                labels=True,
                device_pixel_ratio=device_pixel_ratio,
            )
        )
        self._stat_values["Median"].setText(
            f"{summary.median_present_fps:.0f} fps · {summary.median_frametime_ms:.1f} ms"
        )
        self._stat_values["1%-low"].setText(
            f"{summary.low_1pct_fps:.0f} fps · {summary.p99_frametime_ms:.1f} ms"
        )
        self._stat_values["Power"].setText(f"{summary.median_power_w} W")
        # Absence of data is absence of the row — never a placeholder.
        has_latency = summary.latency_ms > 0
        self._stat_values["Latency"].setText(
            latency_text(summary) if has_latency else ""
        )
        self._stat_values["Latency"].setVisible(has_latency)
        self._stat_keys["Latency"].setVisible(has_latency)
        self.widget.adjustSize()
        screen = self.QtGui.QGuiApplication.screenAt(global_pos)
        position = self.QtCore.QPoint(global_pos)
        width = self.widget.sizeHint().width()
        height = self.widget.sizeHint().height()
        if align_right:
            # The badge lives in the header's right corner: anchor the
            # popover's right edge to it so it opens leftward into the panel
            # instead of off the window's edge.
            position.setX(position.x() - width)
        if screen is not None:
            available = screen.availableGeometry()
            position.setX(
                min(
                    max(position.x(), available.left() + 8),
                    available.right() - width - 8,
                )
            )
            position.setY(min(position.y(), available.bottom() - height - 8))
        self.widget.move(position)
        self.widget.show()

    def hide(self) -> None:
        try:
            self.widget.hide()
        except RuntimeError:
            # Teardown race: the C++ widget can be gone while a late Hide
            # event still routes through the badge's event filter.
            pass
