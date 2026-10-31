"""A picture of the in-game overlay, drawn from the same rows the layer draws.

The settings panel used to preview the overlay as a block of monospaced text,
which answered none of the questions the settings actually raise: how much of
the screen the box covers, whether the corner it sits in is the right one, how
much of the game shows through it, what the colours mean, which face is
drawing. This paints the readout the way the layer does -- hero number, dim
label column, rules, accents, footer, frametime trace -- over a stand-in game
frame, so every control has something visible to change.

Geometry and colours come from overlay.style, which also generates the native
constants. The Qt painter remains independent of the Vulkan resource lifecycle.

The Qt modules arrive as arguments like everywhere else in ``ui``: the widget
class is built on first use so importing this module never requires Qt.
"""

from __future__ import annotations

from overlay.overlay_text import (
    CELL_ALERT,
    CELL_DIM,
    CELL_GOOD,
    CELL_TIER,
    CELL_WARN,
    format_overlay_hud_rows,
    format_overlay_text,
)
from overlay.style import (
    ALERT_COLOR,
    DIM_COLOR,
    GAP,
    GOOD_COLOR,
    GRAPH_MIN_COLS,
    GRAPH_ROWS,
    LABEL_COL,
    LEAD,
    NORMAL_COLOR,
    OVERLAY_SCALE_REFERENCE,
    PAD_X,
    PAD_Y,
    PANEL_COLOR,
    RADIUS,
    RULE_H,
    TIER_COLOR,
    TRACE_COLOR,
    WARN_COLOR,
)

# The preview draws a stand-in 1440p game frame. The overlay's own sizing is
# resolution-adaptive, so showing it against a fixed height is the only way a
# scale change reads as "this much of the screen" rather than "a bigger box".
CANVAS_W = 2560
CANVAS_H = 1440
# How tall the widget is allowed to get. The frame keeps its aspect inside
# this, so a wide window shows a wide letterboxed frame rather than a tall one.
MAX_PREVIEW_H = 360
# What the layer calls the base resolution scale at 1440p, times the 7 rows a
# capital fills in the bitmap cell the atlas faces are matched to.
# Times the layer's scale reference, so 1x here is the 1x drawn in game.
BASE_CAP_PX = 14.0 * OVERLAY_SCALE_REFERENCE
# The overlay is drawn larger than its true share of the frame. At true scale a
# 1440p frame shrunk into a few hundred pixels puts the body text at about
# three pixels tall -- you can see that something is there, and nothing about
# what it says, which is the one thing a settings preview has to show. The
# magnification is a constant, so the size steps still differ from each other
# by the right ratio; only the absolute size is flattering.
PREVIEW_MAGNIFY = 3.0



# Shared colour roles, as 0-255 triples.
LEVEL_COLORS = {
    CELL_DIM: DIM_COLOR,
    CELL_GOOD: GOOD_COLOR,
    CELL_WARN: WARN_COLOR,
    CELL_ALERT: ALERT_COLOR,
    CELL_TIER: TIER_COLOR,
}

# Which family stands in for each of the layer's faces. Mono and Sans are the
# very files the atlas is rasterised from -- including the weight, since the
# medium cut is most of what makes the in-game text hold together -- so the
# preview shows the real letterforms; Pixel has no desktop equivalent and is
# approximated below.
FONT_FAMILIES = {
    "mono": ("DejaVu Sans Mono", "monospace"),
    "sans": ("Roboto Medium", "Roboto", "DejaVu Sans", "sans-serif"),
    "pixel": ("DejaVu Sans Mono", "monospace"),
}

# A frametime window with one stutter in it, so the trace has a shape to show.
SAMPLE_TRACE = (
    8.3, 8.4, 8.2, 8.6, 8.3, 8.5, 9.1, 8.4, 8.3, 8.2, 8.8, 8.4, 8.3, 8.5,
    8.2, 8.4, 12.6, 16.9, 11.2, 8.9, 8.4, 8.3, 8.5, 8.2, 8.4, 8.3, 8.6, 8.4,
    8.2, 8.5, 8.3, 8.4, 8.7, 8.3, 8.2, 8.5, 8.4, 8.3, 9.4, 8.4, 8.3, 8.5,
)

_CLASS_CACHE: dict[int, type] = {}


def overlay_preview_class(QtCore, QtGui, QtWidgets) -> type:
    """The preview class bound to these Qt modules, built once per module set."""
    key = id(QtWidgets)
    cached = _CLASS_CACHE.get(key)
    if cached is not None:
        return cached

    def _color(rgb, alpha=255):
        return QtGui.QColor(rgb[0], rgb[1], rgb[2], alpha)

    class OverlayPreview(QtWidgets.QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setMinimumHeight(200)
            # The frame has the aspect of the screen it stands for, so the
            # widget claims its height from its width rather than letterboxing
            # a 16:9 image inside whatever box the layout felt like giving it.
            policy = QtWidgets.QSizePolicy(
                QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred
            )
            policy.setHeightForWidth(True)
            self.setSizePolicy(policy)
            # Wider than this and the capped height would letterbox the frame,
            # leaving bands of empty group either side of it.
            self.setMaximumWidth(round(MAX_PREVIEW_H * CANVAS_W / CANVAS_H))
            self._config = None
            self._values: dict[str, str] = {}

        def hasHeightForWidth(self) -> bool:
            return True

        def heightForWidth(self, width: int) -> int:
            # Capped: the preview is here to show what the settings do, and it
            # does that at a readable size. Letting it keep the frame's aspect
            # at any width made it claim most of a wide window, which is what
            # pushed the field list off the bottom.
            return max(200, min(MAX_PREVIEW_H, round(width * CANVAS_H / CANVAS_W)))

        def set_config(self, config) -> None:
            self._config = config
            self.update()

        def set_values(self, values: dict[str, str]) -> None:
            self._values = dict(values)
            self.update()

        # -- metrics ---------------------------------------------------------

        def _face(self, magnify=PREVIEW_MAGNIFY):
            """A font whose cap height matches what the layer would draw."""
            config = self._config
            scale = float(getattr(config, "scale", 1.0) or 1.0)
            names = FONT_FAMILIES.get(getattr(config, "font", "mono"),
                                      FONT_FAMILIES["mono"])
            font = QtGui.QFont()
            font.setFamilies(list(names))
            target_cap = BASE_CAP_PX * scale * magnify
            # Size by cap height, not by the nominal point size: the layer
            # matches faces on their capitals, and two families at the same
            # nominal size can differ by a third.
            font.setPixelSize(64)
            probe = QtGui.QFontMetricsF(font)
            cap = probe.capHeight() or 1.0
            font.setPointSizeF(max(1.0, 64.0 * target_cap / cap * 0.75))
            return font

        # -- painting --------------------------------------------------------

        def paintEvent(self, event) -> None:
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
            painter.setRenderHint(QtGui.QPainter.TextAntialiasing, True)

            rect = self.rect()
            factor = min(rect.width() / CANVAS_W, rect.height() / CANVAS_H)
            frame_w = CANVAS_W * factor
            frame_h = CANVAS_H * factor
            painter.translate(
                (rect.width() - frame_w) / 2.0, (rect.height() - frame_h) / 2.0
            )
            painter.scale(factor, factor)
            painter.setClipRect(QtCore.QRectF(0, 0, CANVAS_W, CANVAS_H))

            self._paint_scene(painter)
            if self._config is not None and getattr(self._config, "enabled", True):
                self._paint_overlay(painter)
            painter.end()

        def _paint_scene(self, painter) -> None:
            """A stand-in for the game underneath, so opacity has something to
            show through and the panel has both light and dark to sit on."""
            sky = QtGui.QLinearGradient(0, 0, 0, CANVAS_H)
            sky.setColorAt(0.0, QtGui.QColor(32, 38, 56))
            sky.setColorAt(0.55, QtGui.QColor(58, 48, 66))
            sky.setColorAt(1.0, QtGui.QColor(22, 22, 32))
            painter.fillRect(QtCore.QRectF(0, 0, CANVAS_W, CANVAS_H), sky)

            glow = QtGui.QRadialGradient(
                CANVAS_W * 0.66, CANVAS_H * 0.30, CANVAS_W * 0.30
            )
            glow.setColorAt(0.0, QtGui.QColor(224, 148, 74, 190))
            glow.setColorAt(1.0, QtGui.QColor(224, 148, 74, 0))
            painter.fillRect(QtCore.QRectF(0, 0, CANVAS_W, CANVAS_H), glow)

            for index, (height, shade) in enumerate(
                ((0.66, (62, 58, 86)), (0.76, (44, 42, 64)), (0.88, (26, 26, 40)))
            ):
                path = QtGui.QPainterPath()
                top = CANVAS_H * height
                path.moveTo(0, top)
                span = CANVAS_W / 4.0
                for step in range(5):
                    path.quadTo(
                        span * step + span * 0.5,
                        top - CANVAS_H * (0.10 - index * 0.025),
                        span * (step + 1),
                        top + CANVAS_H * 0.012 * (step % 2),
                    )
                path.lineTo(CANVAS_W, CANVAS_H)
                path.lineTo(0, CANVAS_H)
                path.closeSubpath()
                painter.fillPath(path, _color(shade))

        def _fitted_magnification(self) -> float:
            """The largest magnification whose panel still fits the frame.

            Every metric is proportional to the font size, so the box measured
            at one magnification scales linearly to any other: one extra
            measuring pass is enough to find the limit. Without it the biggest
            size step spilled off all four sides of the preview, which is the
            one thing it would never do in a game.
            """
            layout = self._layout(1.0)
            if layout is None:
                return PREVIEW_MAGNIFY
            box_w, box_h = layout["box_w"], layout["box_h"]
            if not (box_w > 0.0 and box_h > 0.0):
                return PREVIEW_MAGNIFY
            room = min(CANVAS_W * 0.92 / box_w, CANVAS_H * 0.92 / box_h)
            return max(1.0, min(PREVIEW_MAGNIFY, room))

        def _layout(self, magnify):
            """Everything the painter needs, measured at one magnification."""
            config = self._config
            font = self._face(magnify)
            metrics = QtGui.QFontMetricsF(font)
            line_h = metrics.height()

            hero_font = QtGui.QFont(font)
            hero_font.setPointSizeF(font.pointSizeF() * 2.0)
            hero_metrics = QtGui.QFontMetricsF(hero_font)

            pad_y = line_h * PAD_Y
            pad_x = line_h * PAD_X
            lead = line_h * LEAD
            rule_h = line_h * RULE_H
            gap = line_h * GAP
            radius = line_h * RADIUS
            margin = (
                5.0
                * BASE_CAP_PX
                * float(getattr(config, "scale", 1.0) or 1.0)
                * magnify
                / 7.0
            )

            rows = self._rows()
            if not rows:
                return None
            graph = self._graph_enabled()
            graph_h = line_h * (GRAPH_ROWS + 1) * (2 if getattr(config, "graph_mode", "present") == "both" else 1) if graph else 0.0

            label_w = line_h * LABEL_COL
            has_label = any(row.get("label") for row in rows if row["kind"] == "value")
            if has_label:
                for row in rows:
                    if row["kind"] == "value" and row.get("label"):
                        label_w = max(label_w, metrics.horizontalAdvance(row["label"]))
            else:
                label_w = 0.0

            content_w = 0.0
            content_h = 0.0
            for row in rows:
                if row["kind"] == "rule":
                    content_h += rule_h + lead
                    continue
                if row["kind"] == "hero":
                    width = hero_metrics.horizontalAdvance(row["cells"][0]["text"])
                    if len(row["cells"]) > 1:
                        width += gap + metrics.horizontalAdvance(row["cells"][1]["text"])
                    content_h += hero_metrics.height() + lead
                else:
                    width = label_w + gap if label_w else 0.0
                    for index, cell in enumerate(row["cells"]):
                        if index:
                            width += gap
                        width += metrics.horizontalAdvance(cell["text"])
                    content_h += line_h + lead
                content_w = max(content_w, width)
            if content_h:
                content_h -= lead
            if graph:
                content_w = max(content_w, line_h * GRAPH_MIN_COLS)
                content_h += graph_h + lead

            box_w = content_w + pad_x * 2
            box_h = content_h + pad_y * 2
            return {
                "font": font,
                "hero_font": hero_font,
                "line_h": line_h,
                "pad_x": pad_x,
                "pad_y": pad_y,
                "lead": lead,
                "rule_h": rule_h,
                "gap": gap,
                "radius": radius,
                "margin": margin,
                "rows": rows,
                "graph": graph,
                "graph_h": graph_h,
                "label_w": label_w,
                "content_w": content_w,
                "box_w": box_w,
                "box_h": box_h,
            }

        def _paint_overlay(self, painter) -> None:
            config = self._config
            layout = self._layout(self._fitted_magnification())
            if layout is None:
                return
            font = layout["font"]
            hero_font = layout["hero_font"]
            line_h = layout["line_h"]
            pad_x, pad_y = layout["pad_x"], layout["pad_y"]
            lead, rule_h, gap = layout["lead"], layout["rule_h"], layout["gap"]
            radius, margin = layout["radius"], layout["margin"]
            rows, graph, graph_h = layout["rows"], layout["graph"], layout["graph_h"]
            label_w, content_w = layout["label_w"], layout["content_w"]
            box_w, box_h = layout["box_w"], layout["box_h"]
            x0, y0 = self._origin(box_w, box_h, margin)

            opacity = float(getattr(config, "opacity", 0.85) or 0.85)
            blended = getattr(config, "renderer", "blended") == "blended"
            panel = _color(PANEL_COLOR, round(255 * opacity) if blended else 255)
            path = QtGui.QPainterPath()
            if blended:
                path.addRoundedRect(
                    QtCore.QRectF(x0, y0, box_w, box_h), radius, radius
                )
            else:
                path.addRect(QtCore.QRectF(x0, y0, box_w, box_h))
            painter.fillPath(path, panel)
            if blended:
                pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 33))
                pen.setWidthF(max(1.0, line_h * 0.04))
                painter.setPen(pen)
                painter.drawPath(path)

            self._paint_rows(
                painter, rows, font, hero_font, x0 + pad_x, y0 + pad_y,
                content_w, label_w, gap, lead, line_h, rule_h, graph, graph_h,
                blended,
            )

        def _paint_rows(
            self, painter, rows, font, hero_font, left, top, width, label_w,
            gap, lead, line_h, rule_h, graph, graph_h, blended,
        ) -> None:
            metrics = QtGui.QFontMetricsF(font)
            hero_metrics = QtGui.QFontMetricsF(hero_font)
            y = top
            for row in rows:
                kind = row["kind"]
                if kind == "rule":
                    painter.fillRect(
                        QtCore.QRectF(left, y + rule_h * 0.4, width,
                                      max(1.0, line_h * 0.045)),
                        QtGui.QColor(255, 255, 255, 31) if blended
                        else _color((58, 65, 72)),
                    )
                    y += rule_h + lead
                    continue
                if kind == "hero":
                    painter.setFont(hero_font)
                    painter.setPen(_color(NORMAL_COLOR))
                    baseline = y + hero_metrics.ascent()
                    painter.drawText(
                        QtCore.QPointF(left, baseline), row["cells"][0]["text"]
                    )
                    if len(row["cells"]) > 1:
                        unit_x = left + hero_metrics.horizontalAdvance(
                            row["cells"][0]["text"]
                        ) + gap
                        painter.setFont(font)
                        painter.setPen(_color(LEVEL_COLORS[CELL_DIM]))
                        painter.drawText(QtCore.QPointF(unit_x, baseline),
                                         row["cells"][1]["text"])
                    y += hero_metrics.height() + lead
                    if graph:
                        self._paint_graph(
                            painter, left, y, width, graph_h, blended, line_h
                        )
                        y += graph_h + lead
                    continue

                painter.setFont(font)
                baseline = y + metrics.ascent()
                if kind == "value" and row.get("label"):
                    painter.setPen(_color(LEVEL_COLORS[CELL_DIM]))
                    painter.drawText(QtCore.QPointF(left, baseline), row["label"])
                pen_x = left + label_w + gap if label_w else left
                if kind == "foot":
                    pen_x = left
                for cell in row["cells"]:
                    painter.setPen(
                        _color(LEVEL_COLORS.get(cell["level"], NORMAL_COLOR))
                    )
                    painter.drawText(QtCore.QPointF(pen_x, baseline), cell["text"])
                    pen_x += metrics.horizontalAdvance(cell["text"]) + gap
                y += line_h + lead

        def _paint_graph(self, painter, x, y, width, height, blended, line_h) -> None:
            mode = getattr(self._config, "graph_mode", "present")
            labels = ["Present calls"] if mode == "present" else ["Displayed (post-FG)"]
            if mode == "both":
                labels = ["Present calls", "Displayed (post-FG)"]
            slot = height / len(labels)
            for index, label in enumerate(labels):
                top = y + index * slot
                painter.setPen(_color(LEVEL_COLORS[CELL_DIM]))
                painter.drawText(QtCore.QPointF(x, top + line_h * 0.8), label + " (example)")
                self._paint_graph_trace(painter, x, top + line_h, width, slot - line_h, blended, line_h)

        def _paint_graph_trace(self, painter, x, y, width, height, blended, line_h) -> None:
            """The trace, split the way the layer splits it: a filled area under
            the line for the blended pipeline, a bar per sample for the one that
            can only clear rectangles."""
            samples = SAMPLE_TRACE
            span = max(max(samples), 2.0 * sorted(samples)[len(samples) // 2])
            if blended:
                points = []
                step = width / max(1, len(samples) - 1)
                for index, value in enumerate(samples):
                    points.append(
                        QtCore.QPointF(
                            x + index * step,
                            y + height - (value / span) * height * 0.9,
                        )
                    )
                area = QtGui.QPainterPath()
                area.moveTo(points[0])
                for point in points[1:]:
                    area.lineTo(point)
                area.lineTo(x + width, y + height)
                area.lineTo(x, y + height)
                area.closeSubpath()
                fill = QtGui.QLinearGradient(0, y, 0, y + height)
                fill.setColorAt(0.0, QtGui.QColor(*TRACE_COLOR, 107))
                fill.setColorAt(1.0, QtGui.QColor(*TRACE_COLOR, 0))
                painter.fillPath(area, fill)
                line = QtGui.QPainterPath()
                line.moveTo(points[0])
                for point in points[1:]:
                    line.lineTo(point)
                pen = QtGui.QPen(_color(TRACE_COLOR))
                pen.setWidthF(max(1.0, line_h * 0.07))
                painter.setPen(pen)
                painter.drawPath(line)
                return

            bar_w = max(1.0, width / (len(samples) * 2.0))
            median = sorted(samples)[len(samples) // 2]
            for index, value in enumerate(samples):
                bar_h = max(1.0, (value / span) * height)
                painter.fillRect(
                    QtCore.QRectF(x + index * bar_w * 2, y + height - bar_h,
                                  bar_w, bar_h),
                    _color((255, 158, 41)) if value >= median * 2
                    else _color(NORMAL_COLOR),
                )
            painter.fillRect(
                QtCore.QRectF(x, y + height - (median / span) * height, width,
                              max(1.0, line_h * 0.05)),
                _color(TRACE_COLOR),
            )

        # -- content ---------------------------------------------------------

        def _graph_enabled(self) -> bool:
            config = self._config
            return "frametime_graph" in set(
                getattr(config, "enabled_item_ids", ()) or ()
            )

        def _rows(self) -> list[dict]:
            config = self._config
            if getattr(config, "layout", "hud") == "hud":
                return format_overlay_hud_rows(self._values, config=config)
            # The plain grid: one line per field, drawn as ordinary value rows
            # so the same painter handles both.
            text = format_overlay_text(self._values, config=config)
            rows = []
            for line in text.splitlines():
                if not line.strip():
                    continue
                rows.append(
                    {"kind": "value", "label": "",
                     "cells": [{"text": line, "level": 0}]}
                )
            return rows

        def _origin(self, box_w, box_h, margin):
            position = getattr(self._config, "position", "top_right")
            if position.endswith("left"):
                x = margin
            elif position.endswith("center"):
                x = (CANVAS_W - box_w) / 2.0
            else:
                x = CANVAS_W - box_w - margin
            y = margin if position.startswith("top") else CANVAS_H - box_h - margin
            return x, y

    _CLASS_CACHE[key] = OverlayPreview
    return OverlayPreview
