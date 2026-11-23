"""Auto UV progress widgets: the stage dot and the full-scan tier strip."""

from __future__ import annotations

from typing import ClassVar

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from ui.design.style import GAP, TIER_COLORS, TYPE, qcolor

TIERS = ("Efficiency", "Balanced", "Performance")
TIER_STATE_LABELS = {
    "pending": "Pending",
    "active": "Scanning",
    "complete": "✓ Complete",
    "skipped": "Skipped",
    "stopped": "Stopped",
    "failed": "Failed",
    "not-run": "Not run",
}


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
    """Full scan progress: three segments with a 3 px state bar."""

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
