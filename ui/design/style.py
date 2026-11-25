"""Apply the design roles to Qt: colours, font, style sheet, icons and plots."""

import atexit
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from ui import theme
from ui.components.curve_plot import CurvePlot
from ui.components.toggle_switch import toggle_switch_class
from ui.design.tokens import (
    FALLBACK_ACCENT,
    SPEC,
    SYSTEM_ACCENT,
    AppearanceSettings,
    parse_portal_accent,
    resolve_colors,
    resolve_mode,
)
from ui.styles import STYLESHEET

# COL, TYPE and TIER_COLORS are updated in place by install_theme, so every
# module that imported them reads the current Appearance on its next build.
COL: dict[str, str] = {}
COPY = SPEC["copy"]
TYPE = dict(SPEC["typography"])
BUNDLED_FAMILY = SPEC["typography"]["family"]
WEIGHT = SPEC["weights"]
GAP = SPEC["spacing"]
RAD = SPEC["radii"]
TIER_COLORS: dict[str, str] = {}
FONT_DIR = Path(__file__).with_name("fonts")
APPEARANCE = AppearanceSettings()
# The concrete scheme after auto is resolved: "dark" or "light".
MODE = {"current": "dark"}


def set_colors(mode: str, accent: str) -> None:
    COL.clear()
    COL.update(resolve_colors(mode, accent))
    TIER_COLORS.clear()
    TIER_COLORS.update(
        {tier: COL[tier.lower()] for tier in ("Efficiency", "Balanced", "Performance")}
    )
    MODE["current"] = mode


set_colors("dark", FALLBACK_ACCENT)
# Qt's grid alpha for the chart spec: secondary ticks at 25% land on gridMajor.
GRID_ALPHA = 0.25
DIM_ALPHA = 0.35


def qcolor(token_or_hex: str, alpha: float = 1.0) -> G.QColor:
    color = G.QColor(COL.get(token_or_hex, token_or_hex))
    color.setAlphaF(alpha)
    return color


def mix(a: str, b: str, t: float) -> str:
    """Hex blend of two tokens (or hex colours): ``t`` = 0 is ``a``."""
    first, second = qcolor(a), qcolor(b)
    t = min(1.0, max(0.0, t))
    return G.QColor(
        round(first.red() + (second.red() - first.red()) * t),
        round(first.green() + (second.green() - first.green()) * t),
        round(first.blue() + (second.blue() - first.blue()) * t),
    ).name()


# ---------- tab icons ----------
TAB_ICON_SIZE = 20


def _tab_glyph(painter: G.QPainter, name: str) -> None:
    """Line drawings on a 20 px grid: a V/F curve, a profile stack, a
    gamepad and a screen with its HUD."""
    path = G.QPainterPath()
    if name == "auto-uv":
        path.moveTo(3, 16.5)
        path.cubicTo(7, 16, 9, 11, 11, 9)
        path.cubicTo(13, 7, 15, 5, 17, 4.5)
        painter.drawPath(path)
        painter.drawLine(C.QPointF(3, 3.5), C.QPointF(3, 17))
        painter.drawLine(C.QPointF(3, 17), C.QPointF(17.5, 17))
        painter.setBrush(painter.pen().color())
        painter.drawEllipse(C.QPointF(11, 9), 1.6, 1.6)
    elif name == "profiles":
        for y in (5, 10, 15):
            painter.drawRoundedRect(C.QRectF(3, y - 2.2, 14, 4.4), 1.5, 1.5)
    elif name == "game-library":
        path.addRoundedRect(C.QRectF(2.5, 5.5, 15, 9.5), 4.2, 4.2)
        painter.drawPath(path)
        painter.drawLine(C.QPointF(6.5, 8.3), C.QPointF(6.5, 12.3))
        painter.drawLine(C.QPointF(4.5, 10.3), C.QPointF(8.5, 10.3))
        painter.setBrush(painter.pen().color())
        painter.drawEllipse(C.QPointF(12.6, 9.4), 1, 1)
        painter.drawEllipse(C.QPointF(14.6, 11.4), 1, 1)
    elif name == "overlay":
        painter.drawRoundedRect(C.QRectF(2.5, 4, 15, 11), 2, 2)
        painter.drawLine(C.QPointF(7, 17.5), C.QPointF(13, 17.5))
        painter.setBrush(painter.pen().color())
        painter.drawRoundedRect(C.QRectF(4.6, 6.1, 5.4, 3.6), 0.8, 0.8)
    elif name == "import":
        # A tray with an arrow coming down into it.
        painter.drawLine(C.QPointF(10, 3), C.QPointF(10, 12))
        painter.drawPolyline(
            G.QPolygonF(
                [C.QPointF(6.5, 8.8), C.QPointF(10, 12.3), C.QPointF(13.5, 8.8)]
            )
        )
        painter.drawPolyline(
            G.QPolygonF(
                [
                    C.QPointF(3, 12.5),
                    C.QPointF(3, 16.5),
                    C.QPointF(17, 16.5),
                    C.QPointF(17, 12.5),
                ]
            )
        )
    elif name == "perf-profile":
        # A frametime chart: a baseline with steady frame bars and one tall
        # hitch.
        painter.drawLine(C.QPointF(2.5, 17), C.QPointF(17.5, 17))
        for x, top in ((4.5, 11.5), (7.5, 10.5), (10.5, 11.5), (13.5, 4), (16.5, 11)):
            painter.drawLine(C.QPointF(x, 14.5), C.QPointF(x, top))
    elif name == "stop":
        painter.setBrush(painter.pen().color())
        painter.drawRoundedRect(C.QRectF(5, 5, 10, 10), 1.6, 1.6)
    elif name == "appearance":
        # A painter's palette: a disc with a thumb notch and four paint dots.
        disc = G.QPainterPath()
        disc.addEllipse(C.QRectF(2.5, 2.5, 15, 15))
        notch = G.QPainterPath()
        notch.addEllipse(C.QPointF(14.6, 14.2), 3.2, 3.2)
        painter.drawPath(disc.subtracted(notch))
        painter.setBrush(painter.pen().color())
        painter.setPen(C.Qt.PenStyle.NoPen)
        for x, y in ((6.6, 7.6), (10, 5.6), (13.4, 7.6), (6.4, 11.6)):
            painter.drawEllipse(C.QPointF(x, y), 1.35, 1.35)


def glyph_icon(
    name: str, color: str, size: int = 16, disabled: str = "quiet"
) -> G.QIcon:
    """One of the tab glyphs in ``color`` (``disabled`` when the button is
    off), scaled to ``size`` px: the header actions and utilities."""
    icon = G.QIcon()
    for ratio, tone, mode in [
        (r, t, m)
        for t, m in ((color, G.QIcon.Mode.Normal), (disabled, G.QIcon.Mode.Disabled))
        for r in (1, 2, 3)
    ]:
        pixmap = G.QPixmap(size * ratio, size * ratio)
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(C.Qt.GlobalColor.transparent)
        painter = G.QPainter(pixmap)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        painter.scale(size / TAB_ICON_SIZE, size / TAB_ICON_SIZE)
        pen = G.QPen(qcolor(tone), 1.6)
        pen.setCapStyle(C.Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(C.Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        _tab_glyph(painter, name)
        painter.end()
        icon.addPixmap(pixmap, mode)
    return icon


def info_icon(color: str) -> G.QIcon:
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


def tab_icon(name: str) -> G.QIcon:
    """A painted tab icon: ``secondary`` when unselected, ``accent`` on the
    selected tab (QIcon state On), at 1x-3x device pixel ratios."""
    icon = G.QIcon()
    for token, state in (
        ("secondary", G.QIcon.State.Off),
        ("accent", G.QIcon.State.On),
    ):
        for ratio in (1, 2, 3):
            pixmap = G.QPixmap(TAB_ICON_SIZE * ratio, TAB_ICON_SIZE * ratio)
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(C.Qt.GlobalColor.transparent)
            painter = G.QPainter(pixmap)
            painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
            pen = G.QPen(qcolor(token), 1.6)
            pen.setCapStyle(C.Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(C.Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            _tab_glyph(painter, name)
            painter.end()
            icon.addPixmap(pixmap, G.QIcon.Mode.Normal, state)
    return icon


# ---------- focus ring: only on :focus-visible ----------
# Keyboard focus (Tab, Backtab, a shortcut) shows the ring; so does clicking
# into a text field, as the browser's :focus-visible does. Focus a dialog
# or the window gives a widget on its own (programmatic, activation) never
# does.
_KEYBOARD_REASONS = (
    C.Qt.FocusReason.TabFocusReason,
    C.Qt.FocusReason.BacktabFocusReason,
    C.Qt.FocusReason.ShortcutFocusReason,
)
_TEXT_ENTRY = (W.QLineEdit, W.QAbstractSpinBox, W.QPlainTextEdit, W.QTextEdit)
_FOCUS_RE = re.compile(r"([A-Za-z_][\w#-]*(?:\[[^\]]*\])*)((?:::[\w-]+)?):focus")


def focus_visible(qss: str) -> str:
    """Rewrite every ``X:focus`` rule to ``X[keyboardFocus="true"]:focus``
    (``A::sub:focus`` to ``A[keyboardFocus="true"]::sub:focus``)."""

    def keyboard_only(match: re.Match) -> str:
        if "keyboardFocus" in match[1]:
            return match[0]
        return f'{match[1]}[keyboardFocus="true"]{match[2]}:focus'

    return _FOCUS_RE.sub(keyboard_only, qss)


def _is_text_entry(widget: W.QWidget) -> bool:
    if isinstance(widget, _TEXT_ENTRY):
        return True
    return isinstance(widget, W.QComboBox) and widget.isEditable()


class FocusVisibleFilter(C.QObject):
    """App-wide: keeps each widget's ``keyboardFocus`` property in step with
    why it got focus, so the ``focus_visible`` rules draw the ring."""

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind in (C.QEvent.Type.FocusIn, C.QEvent.Type.FocusOut) and isinstance(
            watched, W.QWidget
        ):
            visible = kind == C.QEvent.Type.FocusIn and (
                event.reason() in _KEYBOARD_REASONS
                or (
                    event.reason() == C.Qt.FocusReason.MouseFocusReason
                    and _is_text_entry(watched)
                )
            )
            if bool(watched.property("keyboardFocus")) != visible:
                watched.setProperty("keyboardFocus", visible)
                style = watched.style()
                style.unpolish(watched)
                style.polish(watched)
                watched.update()
        return False


def chart_font() -> G.QFont:
    font = G.QFont(TYPE["family"])
    font.setPixelSize(TYPE["caption"])
    return font


def apply_chart_theme(plot, pg) -> None:
    """Chart spec for any pyqtgraph PlotWidget: plotSurface background, quiet
    grid, `border` axis line and 12 px secondary tick labels."""
    plot.setBackground(COL["plotSurface"])
    plot.showGrid(x=True, y=True, alpha=GRID_ALPHA)
    font = chart_font()
    for side in ("left", "bottom"):
        axis = plot.getAxis(side)
        axis.setPen(pg.mkPen(COL["border"], width=1))
        axis.setTickPen(pg.mkPen(COL["secondary"], width=1))
        axis.setTextPen(COL["secondary"])
        axis.setStyle(tickFont=font, tickTextOffset=6, tickLength=-4)
        axis.label.setDefaultTextColor(qcolor("secondary"))
        axis.label.setFont(font)


def style_value_badge(badge, pg) -> None:
    """Probe value badge: raised fill, border outline, ink text."""
    badge.fill = pg.mkBrush(COL["raised"])
    badge.border = pg.mkPen(COL["border"], width=1)
    badge.setColor(COL["ink"])
    badge.textItem.setFont(chart_font())
    badge.update()


class ChartLegend(W.QWidget):
    """External legend with painted line swatches (never Unicode glyphs).

    set_items([(label, color, style)]) where style is "solid", "dashed",
    "dot" (line with a point marker), "area" (filled band) or "selected"
    (a selected run's 3 px line on a plotSurface halo)."""

    SWATCH_W = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chartLegend")
        self._layout = W.QHBoxLayout(self)
        self._layout.setContentsMargins(GAP["md"], GAP["xs"], GAP["md"], GAP["xs"])
        self._layout.setSpacing(GAP["lg"] + GAP["xs"])
        self._layout.addStretch()
        self.items: list[tuple[str, str, str]] = []

    def set_items(self, items):
        self.items = [(str(label), color, style) for label, color, style in items]
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            child = item.widget() if item is not None else None
            if child is not None:
                child.deleteLater()
        for i, (label, color, style) in enumerate(self.items):
            entry = W.QWidget()
            row = W.QHBoxLayout(entry)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            row.addWidget(_Swatch(color, style))
            text = W.QLabel(label)
            text.setObjectName("chartLegendLabel")
            row.addWidget(text)
            self._layout.insertWidget(i, entry)

    def labels(self) -> list[str]:
        return [label for label, *_ in self.items]

    def add_widget(self, widget: W.QWidget) -> None:
        """A control at the right end of the legend row (e.g. Keys)."""
        self._layout.addWidget(widget)


class _Swatch(W.QWidget):
    def __init__(self, color: str, style):
        super().__init__()
        self.color, self.style = COL.get(color, color), style
        self.setFixedSize(ChartLegend.SWATCH_W, 12)

    def paintEvent(self, _event):
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        mid = self.height() / 2
        if self.style == "selected":
            # A selected run: its 3 px line on a plotSurface halo, so it
            # reads apart from the same-hue tier trace.
            painter.setPen(G.QPen(qcolor("border"), 1))
            painter.setBrush(qcolor("plotSurface"))
            painter.drawRoundedRect(C.QRectF(0.5, mid - 3.5, self.width() - 1, 7), 2, 2)
            painter.fillRect(
                C.QRectF(2, mid - 1.5, self.width() - 4, 3), qcolor(self.color)
            )
            painter.end()
            return
        if self.style == "area":
            fill = qcolor(self.color, 0.22)
            painter.fillRect(C.QRectF(0, mid - 4, self.width(), 8), fill)
        pen = G.QPen(qcolor(self.color), 2)
        if self.style == "dashed":
            pen.setStyle(C.Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(C.QPointF(0, mid), C.QPointF(self.width(), mid))
        if self.style == "dot":
            painter.setBrush(qcolor(self.color))
            painter.setPen(C.Qt.PenStyle.NoPen)
            painter.drawEllipse(C.QPointF(self.width() / 2, mid), 3, 3)
        painter.end()


class DemoCurvePlot(CurvePlot):
    """The production CurvePlot, drawn to the shared chart spec.

    A selected run keeps its own tier colour (set ``selection_color``; the
    candidate colour is the fallback) at 3 px over a 5 px plotSurface halo,
    while tier traces and other runs dim to 35%."""

    selection_color: str | None = None
    _selection_halo = None

    def __init__(self, **kwargs):
        kwargs["source_color"] = COL["baseCurve"]
        kwargs["candidate_color"] = COL["candidateCurve"]
        super().__init__(**kwargs)
        if (
            self.candidate_curve is None
            or self.probe_marker is None
            or self.probe_vline is None
            or self.probe_hline is None
        ):
            return
        self._manual_selection_color = COL["candidateCurve"]
        apply_chart_theme(self.plot, self.pg)
        if self.source_curve is not None:
            self.source_curve.setPen(
                self.pg.mkPen(COL["baseCurve"], width=1.5, style=C.Qt.PenStyle.DashLine)
            )
            self.source_curve.setSymbol(None)
        self.candidate_curve.setZValue(5)
        self.candidate_curve.setSymbolPen(self.pg.mkPen(COL["candidateCurve"]))
        self.candidate_curve.setSymbolSize(4)
        # Neutral probe: ink crosshair at 45%, candidate-coloured marker.
        self.probe_marker.setZValue(9)
        self.probe_marker.setSymbolBrush(qcolor("candidateCurve"))
        self.probe_marker.setSymbolPen(self.pg.mkPen(COL["plotSurface"], width=2))
        crosshair = self.pg.mkPen(qcolor("ink", 0.45), width=1)
        self.probe_vline.setPen(crosshair)
        self.probe_hline.setPen(crosshair)
        for badge in (self.probe_x_axis_badge, self.probe_y_axis_badge):
            if badge is not None:
                style_value_badge(badge, self.pg)

    # The tier a scan is measuring colours its live candidate; None = neutral.
    candidate_tier_color: str | None = None

    def _live_color(self) -> str:
        return self.candidate_tier_color or COL["candidateCurve"]

    def _candidate_curve_pen(self):
        return self.pg.mkPen(self._live_color(), width=2.2)

    def set_candidate_tier_color(self, color: str | None) -> None:
        """Draw the live candidate, its points and the probe in ``color``."""
        color = COL.get(color, color) if color else None
        if color == self.candidate_tier_color or self.candidate_curve is None:
            return
        self.candidate_tier_color = color
        live = self._live_color()
        self.candidate_curve.setSymbolPen(self.pg.mkPen(live))
        self.candidate_curve.setSymbolBrush(live)
        if self.probe_marker is not None:
            self.probe_marker.setSymbolBrush(qcolor(live))
        self._apply_curve_highlights()

    def _emphasis(self) -> bool:
        return bool(self._highlighted_curve_id)

    def _previous_curve_pen(self, *, highlighted):
        if highlighted:
            return self._manual_selection_curve_pen()
        alpha = DIM_ALPHA if self._emphasis() else 0.6
        return self.pg.mkPen(qcolor("curveHistory", alpha), width=1)

    def _manual_selection_curve_pen(self):
        return self.pg.mkPen(self.selection_color or COL["candidateCurve"], width=3)

    def set_selection_color(self, color: str | None) -> None:
        self.selection_color = COL.get(color, color) if color else None
        self._apply_curve_highlights()

    def _apply_curve_highlights(self):
        super()._apply_curve_highlights()
        emphasis = self._emphasis()
        selected_item = self._highlight_overlay_curve
        for trace in self.previous_curves:
            selected = self._curve_is_highlighted(trace["curve_id"])
            trace["item"].setZValue(8 if selected else -9)
            if selected:
                selected_item = trace["item"]
        if self._highlight_overlay_curve is not None:
            self._highlight_overlay_curve.setZValue(8)
        for trace in self.comparison_curves:
            trace.setOpacity(DIM_ALPHA if emphasis else 1.0)
        self._sync_selection_halo(selected_item if emphasis else None)

    def _sync_selection_halo(self, item) -> None:
        if not hasattr(self, "plot"):
            return
        if self._selection_halo is not None:
            self.plot.removeItem(self._selection_halo)
            self._selection_halo = None
        if item is None:
            return
        xs, ys = item.getData()
        if xs is None or not len(xs):
            return
        self._selection_halo = self.plot.plot(
            xs, ys, pen=self.pg.mkPen(COL["plotSurface"], width=8), symbol=None
        )
        self._selection_halo.setZValue(7)


def _toggle_colors(self) -> tuple[str, str, str]:
    """Switch: on = primary track + primaryBorder; off = quiet outline.
    Disabled keeps on and off apart: a muted primary track when on."""
    if not self.isEnabled():
        if self.isChecked():
            return mix("control", "primary", 0.45), COL["divider"], COL["quiet"]
        return "transparent", COL["divider"], COL["quiet"]
    if self.isChecked():
        return (
            COL["primaryHover"] if self.underMouse() else COL["primary"],
            COL["primaryBorder"],
            COL["onPrimary"],
        )
    return (
        COL["controlHover"] if self.underMouse() else "transparent",
        COL["secondary"] if self.underMouse() else COL["quiet"],
        COL["secondary"],
    )


# Production theme constants -> preview tokens. Painted widgets read
# ui.theme at paint time, so the module attributes are re-pointed too.
NATIVE_TOKENS = {
    "WINDOW_BG": "window",
    "SURFACE_BG": "panel",
    "SURFACE_ALT_BG": "raised",
    "CONTROL_BG": "control",
    "CONTROL_HOVER_BG": "controlHover",
    "BORDER": "divider",
    "BORDER_STRONG": "border",
    "TEXT": "ink",
    "TEXT_STRONG": "ink",
    "TEXT_MUTED": "secondary",
    "TEXT_DISABLED": "quiet",
    "TEXT_ON_LIGHT": "window",
    "TEXT_ON_ERROR": "ink",
    "TEXT_PROGRESS": "onPrimary",
    "TOOLTIP_BG": "raised",
    "STAGE": "accent",
    "CANDIDATE": "secondary",
    "GOOD": "gain",
    "GOOD_TEXT": "focus",
    "GOOD_BRIGHT": "accent",
    "WARNING": "warning",
    "WARNING_TEXT": "ink",
    "ERROR": "loss",
    "ERROR_TEXT": "loss",
    "RUNNING": "accent",
    "RUNNING_TEXT": "ink",
    "BASELINE": "baseCurve",
    "BASELINE_TEXT": "ink",
    "LINK": "accent",
    "ROW_GOOD_BG": "selection",
    "ROW_WARNING_BG": "raised",
    "ROW_ERROR_BG": "raised",
    "ROW_RUNNING_BG": "raised",
    "ROW_BASELINE_BG": "raised",
    "PROFILE_SELECTED_BG": "selection",
    "PRIMARY_BUTTON_BG": "primary",
    "PRIMARY_BUTTON_BORDER": "primaryBorder",
    "PRIMARY_BUTTON_TEXT": "onPrimary",
    "PRIMARY_BUTTON_HOVER_BORDER": "accent",
    "PRIMARY_BUTTON_PRESSED_BG": "primaryPressed",
    "PRIMARY_BUTTON_PRESSED_BORDER": "primaryBorder",
    "AUTO_UV_BUTTON_BG": "primary",
    "AUTO_UV_BUTTON_BORDER": "primaryBorder",
    "AUTO_UV_BUTTON_TEXT": "onPrimary",
    "AUTO_UV_BUTTON_HOVER_BORDER": "accent",
    "AUTO_UV_BUTTON_PRESSED_BG": "primaryPressed",
    "AUTO_UV_BUTTON_PRESSED_BORDER": "primaryBorder",
    "DANGER_BUTTON_BG": "control",
    "DANGER_BUTTON_BORDER": "loss",
    "DANGER_BUTTON_TEXT": "ink",
    "DANGER_BUTTON_HOVER_BORDER": "loss",
    "DANGER_BUTTON_PRESSED_BG": "raised",
    "DANGER_BUTTON_PRESSED_BORDER": "loss",
    "ABOUT_BUTTON_BG": "control",
    "ABOUT_BUTTON_BORDER": "border",
    "ABOUT_BUTTON_PRESSED_BG": "raised",
    "PLOT_SOURCE": "baseCurve",
    "PLOT_CANDIDATE": "candidateCurve",
    "PLOT_COMPARISON": "curveHistory",
    "PLOT_BADGE": "ink",
    "TIER_CURVE_EFFICIENCY": "efficiency",
    "TIER_CURVE_BALANCED": "balanced",
    "TIER_CURVE_PERFORMANCE": "performance",
}
# Literal colours in production rules that bypass ui.theme.
NATIVE_LITERALS = {
    "#7f93ad": "accent",
    "#9fb1c7": "focus",
    "#2f3844": "controlHover",
    "#252a31": "control",
    "#5b6675": "border",
    "#c7eaff": "accent",
    "#e7f7ff": "focus",
    "#fff0f0": "focus",
    "#aab3c1": "secondary",
    "#963030": "controlHover",
    "#631b1b": "raised",
    "#fff2c7": "ink",
    "#9fd3b6": "accent",
}


# ui.theme as shipped: install_theme re-points its attributes, and the
# production STYLESHEET was formatted from these originals.
_SHIPPED_THEME = {
    key: getattr(theme, key) for key in NATIVE_TOKENS if hasattr(theme, key)
}


def native_substitutions() -> dict[str, str]:
    subs: dict[str, str] = {}
    for key, token in NATIVE_TOKENS.items():
        value = _SHIPPED_THEME.get(key)
        # The first mapping of a shared hex wins (e.g. PLOT_MANUAL_SELECTION
        # reuses PRIMARY_BUTTON_BG).
        if isinstance(value, str) and value.lower() not in subs:
            subs[value.lower()] = COL[token]
    subs.update({literal: COL[token] for literal, token in NATIVE_LITERALS.items()})
    return subs


_BUNDLED_FONTS_LOADED = False


def load_bundled_fonts() -> list[str]:
    """Register the shipped Inter faces once; returns their families."""
    global _BUNDLED_FONTS_LOADED
    families: set[str] = set()
    if not _BUNDLED_FONTS_LOADED:
        for path in sorted(FONT_DIR.glob("*.ttf")):
            font_id = G.QFontDatabase.addApplicationFont(str(path))
            if font_id >= 0:
                families.update(G.QFontDatabase.applicationFontFamilies(font_id))
        _BUNDLED_FONTS_LOADED = True
    return sorted(families)


def system_font_family() -> str:
    return G.QFontDatabase.systemFont(G.QFontDatabase.SystemFont.GeneralFont).family()


def font_family(settings: AppearanceSettings) -> str:
    """System uses the desktop's general font; Custom the chosen family,
    the bundled Inter unless another one is picked."""
    if settings.font == "system":
        return system_font_family()
    return settings.font_family or BUNDLED_FAMILY


def system_prefers_light() -> bool | None:
    """The desktop's colour scheme, or None when Qt cannot tell."""
    scheme = G.QGuiApplication.styleHints().colorScheme()
    if scheme == C.Qt.ColorScheme.Light:
        return True
    if scheme == C.Qt.ColorScheme.Dark:
        return False
    return None


# Platforms with no desktop behind them: tests, captures, CI.
HEADLESS_PLATFORMS = ("offscreen", "minimal")
_PLATFORM_ACCENT: dict[str, str | None] = {}
PORTAL_ACCENT_CALL = (
    "gdbus",
    "call",
    "--session",
    "--dest",
    "org.freedesktop.portal.Desktop",
    "--object-path",
    "/org/freedesktop/portal/desktop",
    "--method",
    "org.freedesktop.portal.Settings.ReadOne",
    "org.freedesktop.appearance",
    "accent-color",
)


def read_portal_accent() -> str | None:
    """The desktop accent from the XDG settings portal (Plasma 6, GNOME
    47+, and inside Flatpak), or None when it sets none or cannot answer."""
    try:
        reply = subprocess.run(
            PORTAL_ACCENT_CALL, capture_output=True, text=True, timeout=1, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_portal_accent(reply.stdout) if reply.returncode == 0 else None


def capture_platform_accent(app) -> None:
    """Remember the platform theme's accent before install_theme replaces
    the palette; Qt's own default (the style's standard palette) is not a
    desktop choice, so it counts as none."""
    if "accent" in _PLATFORM_ACCENT:
        return
    role = G.QPalette.ColorRole.Accent
    accent = app.palette().color(role).name().upper()
    standard = app.style().standardPalette().color(role).name().upper()
    _PLATFORM_ACCENT["accent"] = None if accent == standard else accent


def system_accent() -> str | None:
    app = G.QGuiApplication.instance()
    if app is None or G.QGuiApplication.platformName() in HEADLESS_PLATFORMS:
        return None
    return read_portal_accent() or _PLATFORM_ACCENT.get("accent")


def effective_accent(accent: str) -> str:
    """A preset or hex as is; System as the desktop's accent, or the
    PenguinBurner green when the desktop has none."""
    if accent != SYSTEM_ACCENT:
        return accent
    return system_accent() or FALLBACK_ACCENT


def resolved_mode(settings: AppearanceSettings) -> str:
    return resolve_mode(settings.theme, system_prefers_light())


def install_theme(app, settings: AppearanceSettings | None = None):
    """Apply ``settings`` (or the current Appearance) to the whole app:
    colour roles, font, palette and the shared style sheet. Widgets that set
    their own style sheet pick the new roles up when they are rebuilt."""
    global APPEARANCE
    if settings is not None:
        APPEARANCE = settings.normalized()
    load_bundled_fonts()
    capture_platform_accent(app)
    set_colors(resolved_mode(APPEARANCE), effective_accent(APPEARANCE.accent))
    TYPE["family"] = font_family(APPEARANCE)
    font = G.QFont(TYPE["family"])
    font.setPixelSize(TYPE["body"])
    app.setFont(font)
    palette = app.palette()
    for key, token in {
        "Window": "window",
        "Base": "panel",
        "AlternateBase": "raised",
        "Button": "control",
        "Text": "ink",
        "WindowText": "ink",
        "ButtonText": "ink",
        "Highlight": "selection",
        "HighlightedText": "ink",
        "BrightText": "loss",
        "Link": "accent",
        "LinkVisited": "accent",
        "PlaceholderText": "quiet",
        "ToolTipBase": "raised",
        "ToolTipText": "ink",
        "Accent": "focus",
    }.items():
        role = getattr(G.QPalette.ColorRole, key, None)
        if role is not None:
            palette.setColor(role, G.QColor(COL[token]))
    for key in ("Text", "ButtonText", "WindowText"):
        role = getattr(G.QPalette.ColorRole, key)
        palette.setColor(G.QPalette.ColorGroup.Disabled, role, G.QColor(COL["quiet"]))
    app.setPalette(palette)
    substitutions = native_substitutions()
    for key, token in NATIVE_TOKENS.items():
        if hasattr(theme, key):
            setattr(theme, key, COL[token])
    toggle_switch_class(C, G, W)._colors = _toggle_colors
    base = re.sub(
        r"#[0-9A-Fa-f]{6}\b",
        lambda match: substitutions.get(match[0].lower(), match[0]),
        STYLESHEET,
    )
    # Full scan includes all three profiles. The active button selects which
    # profile's settings are visible, independently of that inclusion state.
    preset_rules = []
    for tier in ("efficiency", "balanced", "performance"):
        selector = (
            f'QPushButton#autoUvPresetButton[presetId="{tier}"][scanIncluded="true"]'
        )
        preset_rules.append(f"""
            {selector} {{background:{COL["control"]}; color:{COL["ink"]}; border-color:{COL["border"]}}}
            {selector}:hover {{background:{COL["controlHover"]}; border-color:{COL["secondary"]}}}
            {selector}:checked {{background:{COL["selection"]}; border-color:{COL["accent"]}}}
            {selector}:focus {{border:2px solid {COL["focus"]}}}
        """)
    base += "\n".join(preset_rules)
    # Scope is a segmented choice, not an action: selected = selection +
    # accent border, so Start Auto Undervolt stays the dialog's one primary.
    base += f"""
        QPushButton#autoUvScopeButton:checked {{background:{COL["selection"]}; color:{COL["ink"]}; border-color:{COL["accent"]}}}
        QPushButton#autoUvScopeButton:checked:hover {{background:{COL["selection"]}; border-color:{COL["focus"]}}}
    """
    app.setStyleSheet(focus_visible(base + preview_stylesheet() + control_stylesheet()))
    global _FOCUS_FILTER
    if _FOCUS_FILTER is None:
        _FOCUS_FILTER = FocusVisibleFilter(app)
        app.installEventFilter(_FOCUS_FILTER)


_FOCUS_FILTER: FocusVisibleFilter | None = None
# Appearance's accent swatches are 28 px circles.
SWATCH_RADIUS = 14


_ICON_DIR: Path | None = None


def control_icons() -> dict[str, str]:
    """Token-coloured SVG arrows and check marks for Qt style sheets.

    The browser draws the same shapes in pburn.css (a 10x5 chevron in
    secondary, a check in onPrimary), so a combo box or checkbox reads the
    same in both renderers.
    """
    global _ICON_DIR
    shapes = {
        "chevron-down": (
            "10",
            "5",
            f'<path d="M0 0h10L5 5z" fill="{COL["secondary"]}"/>',
        ),
        "chevron-down-quiet": (
            "10",
            "5",
            f'<path d="M0 0h10L5 5z" fill="{COL["quiet"]}"/>',
        ),
        "chevron-up": (
            "10",
            "5",
            f'<path d="M0 5h10L5 0z" fill="{COL["secondary"]}"/>',
        ),
        "chevron-up-quiet": (
            "10",
            "5",
            f'<path d="M0 5h10L5 0z" fill="{COL["quiet"]}"/>',
        ),
        "check": (
            "12",
            "12",
            (
                f'<path d="M2.6 6.3l2.3 2.3 4.6-5" fill="none" stroke="{COL["onPrimary"]}" '
                'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
            ),
        ),
    }
    if _ICON_DIR is None:
        import tempfile

        _ICON_DIR = Path(tempfile.mkdtemp(prefix="pburn-ui-preview-icons-"))
        atexit.register(shutil.rmtree, _ICON_DIR, True)
    paths = {}
    for name, (width, height, body) in shapes.items():
        # Named by content: Qt caches style sheet images by path, so a theme
        # change must point at a new file rather than rewrite the old one.
        digest = hashlib.sha1(body.encode()).hexdigest()[:10]
        path = _ICON_DIR / f"{name}-{digest}.svg"
        if not path.exists():
            path.write_text(
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
                f'height="{height}" viewBox="0 0 {width} {height}">{body}</svg>'
            )
        paths[name] = path.as_posix()
    return paths


def control_stylesheet() -> str:
    """Combo, spin box and checkbox sub-controls in the token look."""
    c, r, i = COL, RAD, control_icons()
    return f"""
        QComboBox::drop-down{{subcontrol-origin:padding;subcontrol-position:center right;width:24px;border:none;background:transparent}}
        QComboBox::down-arrow{{image:url({i["chevron-down"]});width:10px;height:5px}}
        QComboBox::down-arrow:disabled{{image:url({i["chevron-down-quiet"]})}}
        QComboBox::down-arrow:on{{image:url({i["chevron-up"]})}}
        QSpinBox,QDoubleSpinBox{{padding-right:24px}}
        QSpinBox::up-button,QDoubleSpinBox::up-button{{subcontrol-origin:border;subcontrol-position:top right;width:22px;border:none;background:transparent;margin-top:3px}}
        QSpinBox::down-button,QDoubleSpinBox::down-button{{subcontrol-origin:border;subcontrol-position:bottom right;width:22px;border:none;background:transparent;margin-bottom:3px}}
        QSpinBox::up-button:hover,QSpinBox::down-button:hover,QDoubleSpinBox::up-button:hover,QDoubleSpinBox::down-button:hover{{background:{c["controlHover"]};border-radius:{r["control"] - 1}px}}
        QSpinBox::up-arrow,QDoubleSpinBox::up-arrow{{image:url({i["chevron-up"]});width:8px;height:4px}}
        QSpinBox::down-arrow,QDoubleSpinBox::down-arrow{{image:url({i["chevron-down"]});width:8px;height:4px}}
        QSpinBox::up-arrow:disabled,QSpinBox::up-arrow:off,QDoubleSpinBox::up-arrow:disabled,QDoubleSpinBox::up-arrow:off{{image:url({i["chevron-up-quiet"]})}}
        QSpinBox::down-arrow:disabled,QSpinBox::down-arrow:off,QDoubleSpinBox::down-arrow:disabled,QDoubleSpinBox::down-arrow:off{{image:url({i["chevron-down-quiet"]})}}
        QCheckBox::indicator{{width:16px;height:16px;border:1px solid {c["quiet"]};border-radius:{r["control"]}px;background:{c["control"]}}}
        QCheckBox::indicator:hover{{border-color:{c["secondary"]};background:{c["controlHover"]}}}
        QCheckBox::indicator:checked{{background:{c["primary"]};border-color:{c["primaryBorder"]};image:url({i["check"]})}}
        QCheckBox::indicator:checked:hover{{background:{c["primaryHover"]}}}
        QCheckBox::indicator:disabled{{background:{c["control"]};border-color:{c["divider"]}}}
        QCheckBox:focus{{outline:none}}
        QCheckBox::indicator:focus{{border:2px solid {c["focus"]}}}
    """


def preview_stylesheet() -> str:
    c, t, w, r = COL, TYPE, WEIGHT, RAD
    # One filled primary per view: the header Setup on Auto UV, a dialog's
    # accept, and Profiles' current-session action.
    primary = "QPushButton#setupScan,QPushButton#nextAction,QPushButton#profilesPrimary,QDialogButtonBox QPushButton:default"
    return f"""
        QWidget{{font-family:"{t["family"]}";font-size:{t["body"]}px;color:{c["ink"]}}}
        QToolTip{{background:{c["raised"]};color:{c["ink"]};border:1px solid {c["border"]};padding:4px 6px}}
        QLabel#brand{{font-size:{t["section"]}px;font-weight:{w["bold"]};padding:0 4px 0 0}}
        QLabel#pageTitle{{font-size:{t["title"]}px;font-weight:{w["bold"]}}}
        QLabel#pageSubtitle{{font-size:{t["caption"]}px;color:{c["secondary"]}}}
        QLabel#sectionTitle{{font-size:{t["section"]}px;font-weight:{w["semibold"]}}}
        QLabel#caption,QLabel#chartLegendLabel{{font-size:{t["caption"]}px;color:{c["secondary"]}}}
        QWidget#shellHeader,QWidget#statusBand,QFrame#tabStrip{{background:{c["panel"]}}}
        QFrame#tabStrip{{border-bottom:1px solid {c["divider"]};border-top:1px solid {c["divider"]}}}
        QLabel#statusRunning{{font-weight:{w["semibold"]}}}
        QLabel#statusDetail,QLabel#sampleNote{{font-size:{t["caption"]}px;color:{c["secondary"]}}}
        QLabel#sampleNote{{color:{c["quiet"]}}}
        QLabel#pageNote{{color:{c["secondary"]}}}
        QWidget#shellFooter{{border-top:1px solid {c["divider"]}}}
        QProgressBar#headerProgress{{background:{c["control"]};border:1px solid {c["border"]};border-radius:{r["control"]}px;color:{c["ink"]};font-size:{t["caption"]}px;text-align:center;max-height:16px}}
        QProgressBar#headerProgress::chunk{{background:{c["primary"]};border-radius:{r["control"] - 1}px}}
        QFrame#scanLogStrip{{background:{c["panel"]};border:1px solid {c["border"]};border-radius:{r["control"]}px}}
        QLabel#latestLog{{font-size:{t["caption"]}px;color:{c["quiet"]};padding:1px 0}}
        QPushButton#copyFullLog{{font-size:{t["caption"]}px;color:{c["secondary"]};background:transparent;border:none;border-left:1px solid {c["divider"]};border-radius:0;padding:3px 8px}}
        QPushButton#copyFullLog:hover{{background:{c["controlHover"]};color:{c["ink"]}}}
        QPushButton#copyFullLog:focus{{border:2px solid {c["focus"]};padding:1px 6px}}
        QLabel#curveLegend{{font-size:{t["caption"]}px;color:{c["secondary"]};padding:3px 8px}}
        QPushButton{{padding:6px 12px;background:{c["control"]};border:1px solid {c["border"]};border-radius:{r["control"]}px}}
        QComboBox{{padding:5px 10px;background:{c["control"]};border:1px solid {c["border"]};border-radius:{r["control"]}px}}
        QComboBox:hover{{background:{c["controlHover"]};border-color:{c["quiet"]}}}
        QPushButton:hover{{background:{c["controlHover"]};border-color:{c["quiet"]}}}
        QPushButton:pressed{{background:{c["raised"]}}}
        QPushButton:focus{{border:2px solid {c["focus"]};padding:5px 11px}}
        {primary}{{background:{c["primary"]};border-color:{c["primaryBorder"]};color:{c["onPrimary"]};font-weight:{w["semibold"]}}}
        QPushButton#setupScan:hover,QPushButton#nextAction:hover,QPushButton#profilesPrimary:hover,QDialogButtonBox QPushButton:default:hover{{background:{c["primaryHover"]};border-color:{c["primaryBorder"]}}}
        QPushButton#setupScan:pressed,QPushButton#nextAction:pressed,QPushButton#profilesPrimary:pressed,QDialogButtonBox QPushButton:default:pressed{{background:{c["primaryPressed"]}}}
        QPushButton#setupScan[headerRole="secondary"]{{background:{c["control"]};color:{c["ink"]};border-color:{c["accent"]};font-weight:{w["semibold"]}}}
        QPushButton#setupScan[headerRole="secondary"]:hover{{background:{c["controlHover"]}}}
        QPushButton:disabled,QPushButton#setupScan:disabled,QPushButton#nextAction:disabled,QPushButton#profilesPrimary:disabled,QToolButton:disabled,QComboBox:disabled,QLineEdit:disabled,QSpinBox:disabled,QDoubleSpinBox:disabled{{background:{c["control"]};color:{c["quiet"]};border-color:{c["divider"]}}}
        QToolButton#headerUtility{{background:transparent;border:1px solid transparent;border-radius:{r["control"]}px;padding:0}}
        QToolButton#headerUtility:hover{{background:{c["controlHover"]}}}
        QToolButton#headerUtility:pressed{{background:{c["raised"]}}}
        QFrame#headerDivider{{background:{c["divider"]};border:none}}
        QPushButton#aboutPreview{{background:transparent;color:{c["secondary"]};border:1px solid transparent;padding:4px 8px}}
        QPushButton#aboutPreview:hover{{color:{c["ink"]};background:{c["controlHover"]}}}
        QPushButton#footerAction{{background:transparent;color:{c["secondary"]};border:1px solid transparent;padding:3px 4px;font-size:{t["caption"]}px}}
        QPushButton#footerAction:hover{{color:{c["ink"]};background:transparent}}
        QPushButton#footerAction:focus{{border:2px solid {c["focus"]};padding:2px 3px}}
        QToolButton:focus,QComboBox:focus,QSpinBox:focus,QDoubleSpinBox:focus,QLineEdit:focus,QTableWidget:focus,QListWidget:focus{{border:2px solid {c["focus"]}}}
        QGroupBox{{border-radius:{r["panel"]}px}}
        QMenu{{background:{c["raised"]};border:1px solid {c["border"]};border-radius:{r["control"]}px;padding:4px}}
        QMenu::item{{padding:7px 28px 7px 12px;border-radius:{r["control"] - 1}px;color:{c["ink"]}}}
        QMenu::item:disabled{{color:{c["quiet"]}}}
        QMenu::separator{{height:1px;background:{c["divider"]};margin:4px 6px}}
        QMenu::indicator{{width:14px;height:14px;left:6px}}
        QFrame#prefGroupCard{{background:{c["panel"]};border:1px solid {c["border"]};border-radius:{r["panel"]}px}}
        QFrame#prefRow[hasSeparator="true"]{{border-top:1px solid {c["divider"]}}}
        QLabel#prefRowSubtitle{{font-size:{t["caption"]}px;color:{c["secondary"]}}}
        QScrollArea#prototypeScroll,QScrollArea#prototypeScroll > QWidget > QWidget{{background:transparent}}
        QMenu::item:selected,QComboBox QAbstractItemView::item:selected,QListView::item:selected,QTreeView::item:selected{{background:{c["selection"]};color:{c["ink"]}}}
        QComboBox QAbstractItemView::item:hover,QListView::item:hover,QTreeView::item:hover{{background:{c["raised"]};color:{c["ink"]}}}
        QLineEdit,QPlainTextEdit,QTextEdit{{selection-background-color:{c["selection"]};selection-color:{c["ink"]}}}
        QTabBar{{background:transparent}}
        QTabBar::tab{{background:{c["panel"]};border:none;border-bottom:3px solid transparent;padding:10px 14px 8px;margin-right:4px;color:{c["secondary"]};font-size:{t["body"]}px;font-weight:{w["semibold"]}}}
        QTabBar::tab:hover{{color:{c["ink"]};background:{c["panel"]}}}
        QTabBar::tab:selected{{color:{c["ink"]};background:{c["panel"]};border-bottom-color:{c["accent"]}}}
        QTabBar::tab:focus{{color:{c["ink"]}}}
        QTableWidget{{font-size:{t["table"]}px;selection-background-color:{c["selection"]};selection-color:{c["ink"]}}}
        QTableWidget#scanRunsTable{{background:{c["panel"]};border:1px solid {c["border"]}}}
        QTableWidget#scanRunsTable::item{{padding:1px 3px;border-bottom:1px solid {c["divider"]}}}
        QHeaderView::section{{font-size:{t["table"]}px;font-weight:{w["semibold"]};background:{c["raised"]};color:{c["secondary"]};border:none;border-bottom:1px solid {c["border"]};padding:8px 10px}}
        QTableWidget#scanRunsTable QHeaderView::section{{padding:4px}}
        {scrollbar_stylesheet()}
        QSlider{{min-height:30px}}
        QSlider::groove:horizontal{{background:{c["border"]};height:4px;border-radius:2px}}
        QSlider::sub-page:horizontal{{background:{c["primaryBorder"]};border-radius:2px}}
        QSlider::handle:horizontal{{background:{c["onPrimary"]};border:2px solid {c["primaryBorder"]};width:13px;margin:-6px 0;border-radius:8px}}
        QSlider::handle:horizontal:disabled{{background:{c["quiet"]};border-color:{c["divider"]}}}
        QSlider::sub-page:horizontal:disabled{{background:{c["divider"]}}}
        QPushButton#dangerAction{{color:{c["loss"]};border-color:{c["loss"]};font-weight:{w["semibold"]}}}
        QPushButton#dangerAction:hover{{background:{c["controlHover"]};border-color:{c["loss"]}}}
        QLabel#confirmText{{color:{c["secondary"]}}}
        /* ui.theme uses one near-white (#f2f5f2) for both text on a filled
           button and strong text on a surface; the substitution maps it to
           onPrimary, so the surface uses are put back on ink here. */
        QLabel#aboutTitle,QPushButton#aboutButton,QProgressBar#dependencyProgress,QWidget#overlayConfigPanel QPushButton#overlayEditMetrics:hover{{color:{c["ink"]}}}
        QLabel#appearancePreviewTitle{{font-size:{t["body"]}px;font-weight:{w["semibold"]}}}
        QPushButton[segment="true"]{{padding:6px 14px;min-width:56px}}
        QPushButton[segment="true"]:checked{{background:{c["selection"]};color:{c["ink"]};border-color:{c["accent"]};font-weight:{w["semibold"]}}}
        QPushButton[segment="true"]:focus{{padding:5px 13px}}
        QToolButton#accentSwatch{{background:transparent;border:2px solid transparent;border-radius:{SWATCH_RADIUS}px;padding:0}}
        QToolButton#accentSwatch:hover{{border-color:{c["quiet"]}}}
        QToolButton#accentSwatch:checked{{border-color:{c["ink"]}}}
    """


def scrollbar_stylesheet() -> str:
    """One token scrollbar for every scroll area and dialog: no arrows, a
    border-coloured handle that lightens on hover."""
    c, r = COL, RAD
    return f"""
        QScrollBar:vertical{{background:transparent;width:10px;margin:2px;border:none}}
        QScrollBar:horizontal{{background:transparent;height:10px;margin:2px;border:none}}
        QScrollBar::handle{{background:{c["border"]};border-radius:{r["control"] - 2}px;min-height:32px;min-width:32px}}
        QScrollBar::handle:hover{{background:{c["quiet"]}}}
        QScrollBar::add-line,QScrollBar::sub-line{{width:0;height:0;border:none;background:none}}
        QScrollBar::add-page,QScrollBar::sub-page{{background:none}}
    """
