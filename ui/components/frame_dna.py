"""Frame DNA: the five-spoke per-game telemetry fingerprint.

Pure axis math over a ``FrameHistorySummary`` plus QPainter rendering of the
radar (badge and labeled sizes), the warming-up placeholder, and the hover
peek popover. Everything is drawn natively — geometry, colors, and alpha
follow the approved mockup (``notes/frame-dna-mockup.html``) via the
``theme.DNA_*`` palette.
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

DNA_TIER_COLORS = {
    PROFILE_TIER_EFFICIENCY: theme.DNA_TIER_EFFICIENCY,
    PROFILE_TIER_BALANCED: theme.DNA_TIER_BALANCED,
    PROFILE_TIER_PERFORMANCE: theme.DNA_TIER_PERFORMANCE,
    PROFILE_TIER_NONE: theme.DNA_TIER_NONE,
}

# Axis normalization fallbacks: an unknown board limit reads against a
# desktop-class 350 W envelope, an unset per-game target against the global
# adaptive default.
_FALLBACK_POWER_LIMIT_W = 350.0
_FALLBACK_TARGET_FPS = 60.0

# Mockup geometry: fill alpha .18 labeled / .30 badge, 2 px / 1.6 px strokes,
# spokes start at 10% radius so a zero still reads as a vertex.
_FILL_ALPHA_LABELED = 46
_FILL_ALPHA_BADGE = 77
_POINT_FLOOR = 0.1


@dataclass(frozen=True, slots=True)
class DnaAxis:
    code: str
    label: str
    text: str
    fraction: float


def _clamp(fraction: float) -> float:
    return min(1.0, max(0.0, fraction))


def tier_color(tier: str) -> str:
    return DNA_TIER_COLORS.get(tier, theme.DNA_TIER_NONE)


def tier_label(tier: str) -> str:
    return PROFILE_TIER_LABELS.get(tier, "Untuned")


def warming_text(minutes: float) -> str:
    return f"Warming up · {minutes:.0f} of {QUALIFY_MINUTES:.0f} min captured"


def dna_axes(
    summary: FrameHistorySummary,
    *,
    target_fps: float | None,
    power_limit_w: int,
) -> tuple[DnaAxis, ...]:
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
        DnaAxis(
            "PWR",
            "Power draw",
            f"{summary.median_power_w} W",
            _clamp(summary.median_power_w / power_limit),
        ),
        DnaAxis(
            "GPU",
            "GPU load",
            f"{summary.gpu_util_pct}%",
            _clamp(summary.gpu_util_pct / 100.0),
        ),
        DnaAxis(
            "FPS",
            "Median FPS",
            f"{median_fps:.0f} fps",
            _clamp(median_fps / target),
        ),
        DnaAxis(
            "LOW",
            "Fluency floor",
            f"{low_ratio:.0%} of median",
            _clamp(low_ratio),
        ),
        DnaAxis(
            "CPU",
            "CPU hot thread",
            f"{summary.cpu_peak_thread_pct}%",
            _clamp(summary.cpu_peak_thread_pct / 100.0),
        ),
    )


def _vertex(center: float, radius: float, index: int, count: int, fraction: float):
    angle = -math.pi / 2 + (index / count) * 2 * math.pi
    reach = radius * (_POINT_FLOOR + (1 - _POINT_FLOOR) * _clamp(fraction))
    return (
        center + math.cos(angle) * reach,
        center + math.sin(angle) * reach,
        angle,
    )


def _label_font(QtGui, size: int, *, compact: bool):
    font = QtGui.QFont()
    families = getattr(font, "setFamilies", None)
    if families is not None:
        families(["JetBrains Mono", "DejaVu Sans Mono", "Monospace"])
    font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
    font.setPixelSize(max(8, round(size * (0.125 if compact else 0.052))))
    return font


def dna_pixmap(
    QtCore,
    QtGui,
    *,
    axes: tuple[DnaAxis, ...] | None,
    tier: str,
    size: int,
    labels: bool = False,
    device_pixel_ratio: float = 1.0,
):
    """Render the fingerprint (or the dashed warming placeholder) to a pixmap.

    ``axes=None`` draws the warming-up state. ``labels=True`` is the large
    variant with rings, spokes, axis codes, and vertex dots; the badge is the
    bare shape — at miniature sizes the silhouette is the information. The
    labeled radius is computed from the label band's real font metrics so
    every code always lands inside the pixmap, at any size.
    """
    ratio = max(1.0, float(device_pixel_ratio))
    pixmap = QtGui.QPixmap(round(size * ratio), round(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    try:
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        center = size / 2
        if labels:
            font = _label_font(QtGui, size, compact=False)
            painter.setFont(font)
            metrics = QtGui.QFontMetricsF(font)
            gap = max(2.0, size * 0.02)
            radius = size / 2 - (metrics.height() + gap)
        else:
            metrics = None
            gap = 0.0
            radius = size * 0.42
        grid_pen = QtGui.QPen(QtGui.QColor(theme.DNA_GRID))
        grid_pen.setWidthF(1.0)

        if axes is None:
            dashed = QtGui.QPen(QtGui.QColor(theme.DNA_AXIS))
            dashed.setWidthF(1.0)
            dashed.setStyle(QtCore.Qt.PenStyle.DashLine)
            painter.setPen(dashed)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QtCore.QPointF(center, center), radius, radius)
            return pixmap

        painter.setPen(grid_pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        ring_fractions = (0.33, 0.66, 1.0) if labels else (1.0,)
        for fraction in ring_fractions:
            painter.drawEllipse(
                QtCore.QPointF(center, center), radius * fraction, radius * fraction
            )

        count = len(axes)
        points = []
        for index, axis in enumerate(axes):
            x, y, angle = _vertex(center, radius, index, count, axis.fraction)
            points.append((x, y))
            if labels:
                painter.setPen(grid_pen)
                painter.drawLine(
                    QtCore.QPointF(center, center),
                    QtCore.QPointF(
                        center + math.cos(angle) * radius,
                        center + math.sin(angle) * radius,
                    ),
                )
                # Center the code just outside the ring, then clamp it into
                # the pixmap so no size can clip a label.
                width = metrics.horizontalAdvance(axis.code)
                distance = radius + gap + metrics.height() / 2
                text_x = center + math.cos(angle) * distance - width / 2
                text_x = min(max(text_x, 0.0), size - width)
                baseline = (
                    center
                    + math.sin(angle) * distance
                    + metrics.ascent()
                    - metrics.height() / 2
                )
                baseline = min(
                    max(baseline, metrics.ascent()), size - metrics.descent()
                )
                painter.setPen(QtGui.QPen(QtGui.QColor(theme.DNA_TEXT_MUTED)))
                painter.drawText(QtCore.QPointF(text_x, baseline), axis.code)

        color = QtGui.QColor(tier_color(tier))
        fill = QtGui.QColor(color)
        fill.setAlpha(_FILL_ALPHA_LABELED if labels else _FILL_ALPHA_BADGE)
        outline = QtGui.QPen(color)
        outline.setWidthF(2.0 if labels else 1.6)
        outline.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        polygon = QtGui.QPolygonF([QtCore.QPointF(x, y) for x, y in points])
        painter.setPen(outline)
        painter.setBrush(QtGui.QBrush(fill))
        painter.drawPolygon(polygon)

        if labels:
            ring = QtGui.QPen(QtGui.QColor(theme.DNA_SURFACE))
            ring.setWidthF(1.5)
            dot_radius = max(2.4, size * 0.018)
            for x, y in points:
                painter.setPen(ring)
                painter.setBrush(QtGui.QBrush(color))
                painter.drawEllipse(QtCore.QPointF(x, y), dot_radius, dot_radius)
    finally:
        painter.end()
    return pixmap


class FrameDnaPeek:
    """The hover popover: enlarged fingerprint plus the numbers that matter.

    A frameless tool-tip window drawn entirely with Qt widgets and the mock
    palette; the badge click (not this popover) opens the Frame DNA tab.
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
        self.widget.setObjectName("frameDnaPeek")
        self.widget.setStyleSheet(
            f"""
            QFrame#frameDnaPeek {{
                background: {theme.DNA_SURFACE};
                border: 1px solid {theme.DNA_BORDER_STRONG};
                border-radius: 9px;
            }}
            QLabel#frameDnaPeekTitle {{
                color: {theme.DNA_TEXT}; font-size: 12px; font-weight: 700;
                background: transparent; border: none;
            }}
            QLabel#frameDnaPeekKey {{
                font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
                color: {theme.DNA_TEXT_DIM}; font-size: 11px;
                background: transparent; border: none;
            }}
            QLabel#frameDnaPeekValue {{
                font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
                color: {theme.DNA_TEXT}; font-size: 11px; font-weight: 600;
                background: transparent; border: none;
            }}
            QLabel#frameDnaPeekHint {{
                font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
                color: {theme.DNA_TEXT_MUTED}; font-size: 10px;
                background: transparent; border: none;
                border-top: 1px solid {theme.DNA_BORDER};
                padding-top: 6px;
            }}
            """
        )
        layout = QtWidgets.QVBoxLayout(self.widget)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        self.title = QtWidgets.QLabel("")
        self.title.setObjectName("frameDnaPeekTitle")
        layout.addWidget(self.title)
        body = QtWidgets.QHBoxLayout()
        body.setSpacing(12)
        self.dna_label = QtWidgets.QLabel("")
        self.dna_label.setFixedSize(124, 124)
        body.addWidget(self.dna_label, 0)
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
            key_label.setObjectName("frameDnaPeekKey")
            value_label = QtWidgets.QLabel("")
            value_label.setObjectName("frameDnaPeekValue")
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
        self.hint = QtWidgets.QLabel("Click the badge to open the Game Stats tab")
        self.hint.setObjectName("frameDnaPeekHint")
        layout.addWidget(self.hint)

    def show_for(
        self,
        *,
        game_name: str,
        summary: FrameHistorySummary,
        target_fps: float | None,
        power_limit_w: int,
        global_pos,
        device_pixel_ratio: float = 1.0,
        align_right: bool = False,
    ) -> None:
        self.title.setText(f"{game_name} · Game Stats")
        axes = dna_axes(summary, target_fps=target_fps, power_limit_w=power_limit_w)
        self.dna_label.setPixmap(
            dna_pixmap(
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
            f"{summary.latency_ms:.0f} ms" if has_latency else ""
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
