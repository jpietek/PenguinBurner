"""Game Perf Profile: the five-spoke per-game telemetry fingerprint.

Ported from PR #137 (``origin/build-pr-137`` @ 611e6d3,
``ui/components/game_perf_profile.py``): the spoke maths (each spoke against
its own scale, the 10% floor, the fine bands that colour the vertex dots)
are kept; drawing follows the preview's chart spec — Inter labels with the
value on a second line, ``gridMajor`` rings on ``plotSurface``, the
recording's tier colour (accent when untuned), and a hollow ring for an
out-of-range vertex so it reads by shape as well as hue.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from design import TYPE, WEIGHT, qcolor
from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from qt.components.perf_profile_plot import (
    QUALIFY_MINUTES,
    PerfSummary,
    fingerprint_color,
)

# Unknown board limit: a desktop-class 350 W envelope; unset target: 60 fps.
FALLBACK_POWER_LIMIT_W = 350.0
FALLBACK_TARGET_FPS = 60.0
_POINT_FLOOR = 0.1
_RING_FRACTIONS = (0.25, 0.5, 0.75, 1.0)
# FPS within 5% of target, 1%-low at 85%+ of the median, power under 60% of
# the board limit, the hottest CPU thread under 80%. GPU load has no band.
_GOOD_FPS = (0.95, 1.0)
_GOOD_LOW = (0.85, 1.0)
_GOOD_PWR = (0.0, 0.6)
_GOOD_CPU = (0.0, 0.8)
HIGHER_IS_BETTER = "higher"
LOWER_IS_CHEAPER = "lower"
_ARROWS = {HIGHER_IS_BETTER: "↑", LOWER_IS_CHEAPER: "↓"}
_LINE_H = 15.0


@dataclass(frozen=True)
class PerfAxis:
    code: str
    label: str
    text: str
    fraction: float
    direction: str = ""
    good_band: tuple[float, float] | None = None
    # The value printed on the spoke when ``text`` is too long for it.
    spoke_text: str = ""

    @property
    def caption(self) -> str:
        arrow = _ARROWS.get(self.direction)
        return f"{self.code} {arrow}" if arrow else self.code

    @property
    def in_good_band(self) -> bool | None:
        if self.good_band is None:
            return None
        low, high = self.good_band
        return low <= self.fraction <= high


def _clamp(fraction: float) -> float:
    return min(1.0, max(0.0, fraction))


def perf_profile_axes(
    summary: PerfSummary, *, target_fps: float | None, power_limit_w: float
) -> tuple[PerfAxis, ...]:
    """The five spokes: PWR crowns the pentagon, GPU and CPU flank it, FPS and
    LOW sit below. Latency has no spoke; it shows only where measured."""
    limit = (
        float(power_limit_w)
        if power_limit_w and power_limit_w > 0
        else FALLBACK_POWER_LIMIT_W
    )
    target = float(target_fps) if target_fps and target_fps > 0 else FALLBACK_TARGET_FPS
    median_fps = summary.median_present_fps
    low_ratio = summary.low_1pct_fps / median_fps if median_fps > 0 else 0.0
    return (
        PerfAxis(
            "PWR",
            "Power draw",
            f"{summary.median_power_w} W",
            _clamp(summary.median_power_w / limit),
            LOWER_IS_CHEAPER,
            _GOOD_PWR,
        ),
        PerfAxis(
            "GPU",
            "GPU load",
            f"{summary.gpu_util_pct}%",
            _clamp(summary.gpu_util_pct / 100.0),
        ),
        PerfAxis(
            "FPS",
            "Median FPS",
            f"{median_fps:.0f} fps",
            _clamp(median_fps / target),
            HIGHER_IS_BETTER,
            _GOOD_FPS,
        ),
        PerfAxis(
            "LOW",
            "Fluency floor",
            f"{low_ratio:.0%} of median",
            _clamp(low_ratio),
            HIGHER_IS_BETTER,
            _GOOD_LOW,
            f"{low_ratio:.0%}",
        ),
        PerfAxis(
            "CPU",
            "CPU hot thread",
            f"{summary.cpu_peak_thread_pct}%",
            _clamp(summary.cpu_peak_thread_pct / 100.0),
            LOWER_IS_CHEAPER,
            _GOOD_CPU,
        ),
    )


def axes_tooltip(axes: tuple[PerfAxis, ...]) -> str:
    return "\n".join(f"{axis.label}: {axis.text}" for axis in axes)


def _font(*, bold: bool = False) -> G.QFont:
    font = G.QFont(TYPE["family"])
    font.setPixelSize(TYPE["caption"])
    if bold:
        font.setWeight(G.QFont.Weight(WEIGHT["semibold"]))
    return font


def _angle(index: int, count: int = 5) -> float:
    return -math.pi / 2 + index * 2 * math.pi / count


def paint_fingerprint(
    painter: G.QPainter,
    rect: C.QRectF,
    *,
    axes: tuple[PerfAxis, ...] | None,
    tier: str = "",
    labels: bool = True,
    detailed: bool = True,
    warming_minutes: float | None = None,
    background: bool = True,
) -> None:
    """Draw the fingerprint, or the dashed warming placeholder (``axes=None``).

    ``labels`` draws captions and band-coloured vertex dots; ``detailed`` adds
    each spoke's value under its code (the full view), while the 124 px peek
    keeps codes only. Without labels it is the bare silhouette.
    """
    w, h = rect.width(), rect.height()
    size = min(w, h)
    detailed = labels and detailed
    dot_r = min(4.5, max(3.0, size * 0.026)) if labels else 0.0
    gap = max(2.0, size * 0.02) + dot_r if labels else 0.0
    regular, bold = _font(), _font(bold=True)
    metrics, bold_metrics = G.QFontMetricsF(regular), G.QFontMetricsF(bold)
    lines_for = (
        []
        if axes is None
        else [(axis.caption, axis.spoke_text or axis.text) for axis in axes]
    )
    half = 24.0
    if lines_for:
        half = max(
            max(metrics.horizontalAdvance(a), bold_metrics.horizontalAdvance(b)) / 2
            for a, b in lines_for
        )

    def label_reach(cos: float, sin: float) -> float:
        # The label box's support along its spoke, so no corner meets the dot.
        return abs(cos) * half + abs(sin) * _LINE_H

    cx = rect.x() + w / 2
    if detailed:
        low, low_cos = math.sin(0.3 * math.pi), math.cos(0.3 * math.pi)
        side, side_sin = math.cos(0.1 * math.pi), math.sin(0.1 * math.pi)
        low_reach, side_reach = label_reach(low_cos, low), label_reach(side, side_sin)
        by_height = (h - (1 + low) * gap - 2 * _LINE_H - low * low_reach - _LINE_H) / (
            1 + low
        )
        by_width = (w / 2 - half) / side - gap - side_reach
        radius = min(by_height, by_width)
        above = radius + gap + 2 * _LINE_H
        below = low * (radius + gap + low_reach) + _LINE_H
        cy = rect.y() + (h - above - below) / 2 + above
    else:
        radius = size / 2 - _LINE_H - 7 if labels else size * 0.42
        cy = rect.y() + h / 2
    if radius < 8:
        return
    painter.save()
    painter.setRenderHint(G.QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(G.QPainter.RenderHint.TextAntialiasing, True)
    if labels and background:
        painter.fillRect(rect, qcolor("plotSurface"))
    center = C.QPointF(cx, cy)

    def reach(fraction: float) -> float:
        return radius * (_POINT_FLOOR + (1 - _POINT_FLOOR) * _clamp(fraction))

    if axes is None:
        pen = G.QPen(qcolor("border"), 1)
        pen.setDashPattern([4, 4])
        painter.setPen(pen)
        painter.setBrush(C.Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, radius, radius)
        if warming_minutes is not None and labels:
            painter.setFont(bold)
            painter.setPen(qcolor("secondary"))
            line_rect = C.QRectF(cx - radius, cy - 9 - _LINE_H / 2, 2 * radius, _LINE_H)
            painter.drawText(
                line_rect, int(C.Qt.AlignmentFlag.AlignCenter), "Warming up"
            )
            painter.setFont(regular)
            painter.setPen(qcolor("quiet"))
            line_rect.translate(0, 18)
            painter.drawText(
                line_rect,
                int(C.Qt.AlignmentFlag.AlignCenter),
                f"{int(warming_minutes)} of {QUALIFY_MINUTES:.0f} min captured",
            )
        painter.restore()
        return

    grid = G.QPen(qcolor("gridMajor" if labels else "border"), 1)
    painter.setPen(grid)
    painter.setBrush(C.Qt.BrushStyle.NoBrush)
    for fraction in _RING_FRACTIONS if labels else (1.0,):
        painter.drawEllipse(center, reach(fraction), reach(fraction))
    count = len(axes)
    points = [
        C.QPointF(
            cx + math.cos(_angle(i, count)) * reach(axis.fraction),
            cy + math.sin(_angle(i, count)) * reach(axis.fraction),
        )
        for i, axis in enumerate(axes)
    ]
    if labels:
        for i, axis in enumerate(axes):
            cos, sin = math.cos(_angle(i, count)), math.sin(_angle(i, count))
            painter.setPen(grid)
            painter.drawLine(center, C.QPointF(cx + cos * radius, cy + sin * radius))
            if detailed:
                _draw_detailed_label(
                    painter,
                    rect,
                    axis,
                    cx,
                    cy,
                    cos,
                    sin,
                    radius + gap + label_reach(cos, sin),
                    regular,
                    bold,
                )
            else:
                _draw_code(
                    painter,
                    rect,
                    axis.code,
                    cx + cos * radius,
                    cy + sin * radius,
                    cos,
                    sin,
                    regular,
                )

    color = qcolor(fingerprint_color(tier))
    outline = G.QPen(color, 2.4 if labels else 1.6)
    outline.setJoinStyle(C.Qt.PenJoinStyle.RoundJoin)
    painter.setPen(outline)
    painter.setBrush(qcolor(fingerprint_color(tier), 0.18 if labels else 0.30))
    painter.drawPolygon(G.QPolygonF(points))
    if labels:
        surface = qcolor("plotSurface")
        for axis, point in zip(axes, points, strict=True):
            # A surface halo lifts each dot off any tier stroke.
            painter.setPen(C.Qt.PenStyle.NoPen)
            painter.setBrush(surface)
            painter.drawEllipse(point, dot_r + 2.5, dot_r + 2.5)
            band = axis.in_good_band
            if band is False:
                painter.setPen(G.QPen(qcolor("warning"), 2))
                painter.setBrush(surface)
                painter.drawEllipse(point, dot_r - 0.5, dot_r - 0.5)
            else:
                painter.setBrush(qcolor("accent" if band else "quiet"))
                painter.drawEllipse(point, dot_r, dot_r)
    painter.restore()


def _draw_detailed_label(
    painter, rect, axis, cx, cy, cos, sin, distance, regular, bold
):
    lines = (axis.caption, axis.spoke_text or axis.text)
    tx = cx + cos * distance
    top = cy + sin * distance - _LINE_H * len(lines) / 2
    top = min(max(top, rect.top()), rect.bottom() - _LINE_H * len(lines))
    for row, line in enumerate(lines):
        font = bold if row else regular
        painter.setFont(font)
        width = G.QFontMetricsF(font).horizontalAdvance(line)
        left = min(max(tx - width / 2, rect.left()), rect.right() - width)
        painter.setPen(qcolor("ink" if row else "secondary"))
        painter.drawText(
            C.QRectF(left, top + row * _LINE_H, width + 1, _LINE_H),
            int(C.Qt.AlignmentFlag.AlignLeft | C.Qt.AlignmentFlag.AlignVCenter),
            line,
        )


def _draw_code(painter, rect, code, ex, ey, cos, sin, font):
    """The compact peek: codes in fixed bands at the top and bottom, side
    codes tucked above their spokes, so 12 px text stays legible at 124 px."""
    painter.setFont(font)
    painter.setPen(qcolor("secondary"))
    width = G.QFontMetricsF(font).horizontalAdvance(code)
    inset = 3.0
    is_side = abs(cos) > 0.5
    if is_side:
        left = rect.right() - width - inset if cos > 0 else rect.left() + inset
        top = ey - _LINE_H - 3
    else:
        left = min(
            max(ex - width / 2, rect.left() + inset), rect.right() - width - inset
        )
        top = (
            max(rect.top(), ey - _LINE_H - 4)
            if sin < 0
            else min(rect.bottom() - _LINE_H, ey + 4)
        )
    painter.drawText(
        C.QRectF(left, top, width + 1, _LINE_H),
        int(C.Qt.AlignmentFlag.AlignLeft | C.Qt.AlignmentFlag.AlignVCenter),
        code,
    )


def fingerprint_pixmap(
    axes: tuple[PerfAxis, ...] | None,
    *,
    tier: str = "",
    size: int,
    labels: bool = True,
    detailed: bool = False,
    warming_minutes: float | None = None,
    device_pixel_ratio: float = 1.0,
) -> G.QPixmap:
    ratio = max(1.0, float(device_pixel_ratio))
    pixmap = G.QPixmap(round(size * ratio), round(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(C.Qt.GlobalColor.transparent)
    painter = G.QPainter(pixmap)
    try:
        paint_fingerprint(
            painter,
            C.QRectF(0, 0, size, size),
            axes=axes,
            tier=tier,
            labels=labels,
            detailed=detailed,
            warming_minutes=warming_minutes,
        )
    finally:
        painter.end()
    return pixmap


class PerfFingerprint(W.QWidget):
    """The full-view fingerprint, painted at the widget's own size and DPR."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("perfFingerprint")
        self.setMinimumHeight(220)
        self.setAccessibleName("Fingerprint")
        self.axes: tuple[PerfAxis, ...] | None = None
        self.tier = ""
        self.warming_minutes: float | None = None
        # Round the bottom corners when the widget ends its card.
        self.bottom_radius = 0.0
        # False: the bare silhouette on a transparent background (the 72 px
        # mark above the empty page's hint).
        self.labels = True

    def set_state(
        self,
        axes: tuple[PerfAxis, ...] | None,
        *,
        tier: str = "",
        warming_minutes: float | None = None,
    ) -> None:
        self.axes, self.tier = axes, tier
        self.warming_minutes = warming_minutes
        self.update()

    def paintEvent(self, _event):
        painter = G.QPainter(self)
        try:
            if not self.labels:
                paint_fingerprint(
                    painter,
                    C.QRectF(self.rect()),
                    axes=self.axes,
                    tier=self.tier,
                    labels=False,
                )
                return
            if self.bottom_radius > 0:
                rect, radius = C.QRectF(self.rect()), self.bottom_radius
                rounded, top = G.QPainterPath(), G.QPainterPath()
                rounded.addRoundedRect(rect, radius, radius)
                top.addRect(rect.adjusted(0, 0, 0, -rect.height() / 2))
                painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
                painter.fillPath(rounded.united(top), qcolor("plotSurface"))
            else:
                painter.fillRect(self.rect(), qcolor("plotSurface"))
            inner = C.QRectF(self.rect()).adjusted(12, 8, -12, -8)
            paint_fingerprint(
                painter,
                inner,
                axes=self.axes,
                tier=self.tier,
                warming_minutes=self.warming_minutes,
                background=False,
            )
        finally:
            painter.end()
