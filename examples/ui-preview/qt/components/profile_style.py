"""Token styling for the Profiles widgets: one style sheet, painted marks.

Every colour, size and radius comes from the design tokens in design.py
(pburn-design.json); nothing here hard-codes a hex value.
"""

from __future__ import annotations

from design import COL, RAD, TIER_COLORS, TYPE, WEIGHT, focus_visible, qcolor
from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

ROW_HEIGHT = 32
CELL_PAD_X = 10
PILL_HEIGHT = 20
# Pill tones -> (text/dot token, outline token).
TONES = {
    "accent": ("accent", "primaryBorder"),
    "warning": ("warning", "warning"),
    "quiet": ("secondary", "border"),
    "loss": ("loss", "loss"),
}


def tier_color(tier: str) -> str:
    return TIER_COLORS.get(str(tier).strip().title(), COL["accent"])


def profiles_stylesheet() -> str:
    c, t, w, r = COL, TYPE, WEIGHT, RAD
    return f"""
        QWidget#profilesPage, QScrollArea#profilesScroll, QWidget#profilesBody {{background:{c["window"]}}}
        QScrollArea#profilesScroll {{border:none}}
        QFrame#profilesCard {{background:{c["panel"]};border:1px solid {c["border"]};border-radius:{r["panel"]}px}}
        QFrame#profilesDivider {{background:{c["divider"]};border:none;max-height:1px;min-height:1px}}
        QFrame#profilesVDivider {{background:{c["divider"]};border:none;max-width:1px;min-width:1px}}
        QLabel#profilesPageTitle {{font-size:{t["title"]}px;font-weight:{w["bold"]}}}
        QLabel#profilesTitle {{font-size:{t["section"]}px;font-weight:{w["semibold"]}}}
        QLabel#profilesCaption, QLabel#profilesHint {{font-size:{t["caption"]}px;color:{c["secondary"]}}}
        QLabel#profilesHint {{color:{c["quiet"]}}}
        QLabel#profilesRowTitle {{font-weight:{w["semibold"]}}}
        QLabel#profilesRowSubtitle {{font-size:{t["caption"]}px;color:{c["secondary"]}}}
        QLabel#profilesRowSubtitle[tone="warning"] {{color:{c["warning"]}}}
        QLabel#profilesValue {{color:{c["ink"]};font-weight:{w["semibold"]}}}
        QLabel#metricLabel {{font-size:{t["caption"]}px;color:{c["secondary"]}}}
        QLabel#metricValue {{font-size:{t["title"]}px;font-weight:{w["bold"]};color:{c["ink"]}}}
        QLabel#metricUnit {{font-size:{t["caption"]}px;color:{c["secondary"]}}}
        QLabel#profilesNotice {{font-size:{t["caption"]}px;color:{c["secondary"]}}}
        QLabel#profilesNotice[tone="warning"] {{color:{c["warning"]}}}
        QLabel#profilesNotice[tone="loss"] {{color:{c["loss"]}}}
        QLabel#profilesNotice[tone="accent"] {{color:{c["gain"]}}}
        /* Primary colours, hover, pressed and disabled come from
           design.preview_stylesheet; this page only widens the padding. */
        QPushButton#profilesPrimary {{padding:6px 16px}}
        QPushButton#profilesPrimary:focus {{border:2px solid {c["focus"]};padding:5px 15px}}
        QPushButton#profilesPrimary[role="secondary"] {{background:{c["control"]};border-color:{c["border"]};color:{c["ink"]};font-weight:{w["semibold"]}}}
        QPushButton#profilesPrimary[role="secondary"]:hover {{background:{c["controlHover"]};border-color:{c["quiet"]}}}
        QPushButton#profilesDanger {{border-color:{c["loss"]}}}
        QPushButton#profilesDanger:hover {{background:{c["controlHover"]};border-color:{c["loss"]}}}
        QPushButton#profilesDanger:disabled {{border-color:{c["divider"]}}}
        QPushButton::menu-indicator {{subcontrol-origin:padding;subcontrol-position:center right;right:8px}}
        QPushButton#profilesMenuButton {{padding-right:26px}}
        QToolButton#profilesIconButton {{background:{c["control"]};border:1px solid {c["border"]};border-radius:{r["control"]}px;padding:5px}}
        QToolButton#profilesIconButton:hover {{background:{c["controlHover"]};border-color:{c["quiet"]}}}
        QToolButton#profilesIconButton:focus {{border:2px solid {c["focus"]};padding:4px}}
        QTableWidget#profileTable {{background:{c["panel"]};border:none;border-top:1px solid {c["divider"]};outline:none;font-size:{t["table"]}px}}
        QTableWidget#profileTable:focus {{border:none;border-top:1px solid {c["divider"]}}}
        QTableWidget#profileTable[focusVisible="true"] {{border:2px solid {c["focus"]}}}
        QFrame#profilesSession {{background:{c["raised"]};border:1px solid {c["divider"]};border-radius:{r["control"]}px}}
        QComboBox#profileTargetGpu {{background:{c["control"]};border:1px solid {c["border"]};border-radius:{r["control"]}px;padding:5px {12}px}}
        QComboBox#profileTargetGpu:hover {{background:{c["controlHover"]};border-color:{c["quiet"]}}}
        QComboBox#profileTargetGpu:focus {{border:2px solid {c["focus"]};padding:4px 11px}}
        QMenu#profilesMenu QWidget {{font-size:{t["body"]}px}}
    """


def apply_style(widget: W.QWidget) -> None:
    widget.setStyleSheet(focus_visible(profiles_stylesheet()))


def repolish(widget: W.QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def caption_font(*, weight: int | None = None, size: int | None = None) -> G.QFont:
    font = G.QFont(TYPE["family"])
    font.setPixelSize(size or TYPE["caption"])
    if weight is not None:
        font.setWeight(G.QFont.Weight(weight))
    return font


def pill_width(text: str, *, dot: bool = True) -> int:
    metrics = G.QFontMetrics(caption_font(weight=WEIGHT["semibold"]))
    return metrics.horizontalAdvance(text) + (2 * 8 + 10 if dot else 2 * 7)


def paint_pill(
    painter: G.QPainter,
    rect: C.QRectF,
    text: str,
    tone: str,
    *,
    dot: bool = True,
    dot_color: str = "",
) -> None:
    """A rounded outline pill: tone dot + label (radius = half its height).

    Table rows use the dotless form so the profile name keeps its room.
    ``dot_color`` (a token or hex) overrides the dot, e.g. a tier colour."""
    ink, outline = TONES.get(tone, TONES["quiet"])
    painter.save()
    painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
    painter.setPen(G.QPen(qcolor(outline, 0.9), 1))
    painter.setBrush(qcolor(ink, 0.08))
    radius = rect.height() / 2
    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    if dot:
        painter.setPen(C.Qt.PenStyle.NoPen)
        painter.setBrush(qcolor(dot_color or ink))
        painter.drawEllipse(C.QPointF(rect.left() + 9, rect.center().y()), 3, 3)
    painter.setPen(qcolor(ink))
    painter.setFont(caption_font(weight=WEIGHT["semibold"]))
    painter.drawText(
        rect.adjusted(16 if dot else 7, 0, -6, 0),
        int(C.Qt.AlignmentFlag.AlignVCenter | C.Qt.AlignmentFlag.AlignLeft),
        text,
    )
    painter.restore()


class Pill(W.QWidget):
    """A status pill widget (dot + label) for the detail card header."""

    def __init__(self, text: str = "", tone: str = "accent", parent=None):
        super().__init__(parent)
        self._text, self._tone, self._dot_color = text, tone, ""
        self.setSizePolicy(W.QSizePolicy.Policy.Fixed, W.QSizePolicy.Policy.Fixed)
        self._resize()

    def set_status(self, text: str, tone: str, *, dot_color: str = "") -> None:
        self._text, self._tone, self._dot_color = text, tone, dot_color
        self.setAccessibleName(text)
        self._resize()
        self.update()

    def text(self) -> str:
        return self._text

    def tone(self) -> str:
        return self._tone

    def _resize(self) -> None:
        self.setFixedSize(pill_width(self._text), PILL_HEIGHT + 2)

    def paintEvent(self, _event):
        painter = G.QPainter(self)
        paint_pill(
            painter,
            C.QRectF(0, 1, self.width(), PILL_HEIGHT),
            self._text,
            self._tone,
            dot_color=self._dot_color,
        )
        painter.end()


class TierChip(W.QWidget):
    """Tier dot + tier name, in the tier's own colour for the dot."""

    def __init__(self, tier: str, parent=None):
        super().__init__(parent)
        self.tier = tier
        font = caption_font(size=TYPE["body"])
        width = G.QFontMetrics(font).horizontalAdvance(tier) + 16
        self.setFixedSize(width, 20)
        self.setAccessibleName(tier)

    def paintEvent(self, _event):
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        painter.setPen(C.Qt.PenStyle.NoPen)
        painter.setBrush(qcolor(tier_color(self.tier)))
        painter.drawEllipse(C.QPointF(4, self.height() / 2), 4, 4)
        painter.setPen(qcolor("ink"))
        painter.setFont(caption_font(size=TYPE["body"]))
        painter.drawText(
            C.QRectF(14, 0, self.width() - 14, self.height()),
            int(C.Qt.AlignmentFlag.AlignVCenter),
            self.tier,
        )
        painter.end()


def trash_icon(
    color_token: str = "secondary", disabled_token: str = "quiet"
) -> G.QIcon:
    """A line trash can drawn at device pixel ratio (normal + disabled)."""
    icon = G.QIcon()
    for token, mode in (
        (color_token, G.QIcon.Mode.Normal),
        (disabled_token, G.QIcon.Mode.Disabled),
    ):
        ratio = 3
        pixmap = G.QPixmap(16 * ratio, 16 * ratio)
        pixmap.fill(C.Qt.GlobalColor.transparent)
        pixmap.setDevicePixelRatio(ratio)
        painter = G.QPainter(pixmap)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        pen = G.QPen(qcolor(token), 1.4)
        pen.setCapStyle(C.Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(C.Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        path = G.QPainterPath()
        path.moveTo(2.5, 4)
        path.lineTo(13.5, 4)
        path.moveTo(6, 4)
        path.lineTo(6, 2.5)
        path.lineTo(10, 2.5)
        path.lineTo(10, 4)
        path.moveTo(4, 4)
        path.lineTo(4.7, 13.5)
        path.lineTo(11.3, 13.5)
        path.lineTo(12, 4)
        path.moveTo(6.6, 6.5)
        path.lineTo(6.6, 11.5)
        path.moveTo(9.4, 6.5)
        path.lineTo(9.4, 11.5)
        painter.drawPath(path)
        painter.end()
        icon.addPixmap(pixmap, mode)
    return icon


def divider(vertical: bool = False) -> W.QFrame:
    line = W.QFrame()
    line.setObjectName("profilesVDivider" if vertical else "profilesDivider")
    return line


def text_label(text: str, role: str, *, wrap: bool = False) -> W.QLabel:
    label = W.QLabel(text)
    label.setObjectName(role)
    label.setWordWrap(wrap)
    return label
