#!/usr/bin/env python3
"""Draw ui/assets/tab-game-perf-profile.png, the Game Perf Profile tab's icon.

The glyph is the feature's own radar: a five-spoke frame in the light tone with
an uneven fingerprint filled in the accent green inside it -- the same pairing
the Game Library icon uses, so it sits in the family.

Drawn for 18 px, the size the tab bar uses: the frame is a thick outline with
no spokes or inner rings, which turn to grey mush at that size, and the
fingerprint is lopsided enough that it never reads as a second pentagon.

    python3 scripts/render-game-perf-profile-tab-icon.py [--output PATH]
"""

from __future__ import annotations

import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from tab_icon import BODY, GLYPH, SIZE, brush, main

CENTER_Y = SIZE / 2 + 5  # a pentagon's mass sits low; nudge it to look centred
RADIUS = 50.0
#: PWR, GPU, FPS, LOW, CPU -- the order and a plausible shape of the real chart.
FINGERPRINT = (0.86, 0.92, 0.55, 0.74, 0.48)


def _vertex(QtCore, index: int, fraction: float):
    angle = -math.pi / 2 + index * 2 * math.pi / 5
    return QtCore.QPointF(
        SIZE / 2 + math.cos(angle) * RADIUS * fraction,
        CENTER_Y + math.sin(angle) * RADIUS * fraction,
    )


def draw_glyph(painter, QtCore, QtGui) -> None:
    painter.setBrush(brush(QtGui, GLYPH))
    painter.drawPolygon(
        QtGui.QPolygonF([_vertex(QtCore, i, f) for i, f in enumerate(FINGERPRINT)])
    )

    pen = QtGui.QPen(QtGui.QColor(BODY), 9)
    pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    painter.drawPolygon(QtGui.QPolygonF([_vertex(QtCore, i, 1.0) for i in range(5)]))


if __name__ == "__main__":
    raise SystemExit(
        main(draw_glyph, asset_name="tab-game-perf-profile.png", doc=__doc__)
    )
