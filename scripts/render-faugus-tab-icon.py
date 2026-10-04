#!/usr/bin/env python3
"""Draw ui/assets/tab-faugus.png, the Faugus Launcher library badge.

The badge is only used where the machine has no Faugus icon of its own -- a
library left behind by an uninstalled Faugus -- so it is deliberately NOT the
Faugus brand mark: shipping someone's logo is the maintainer's call, not a
detail to slip in with a feature. Swapping in a real one later means replacing
the file, nothing else.

A pair of fangs, for the name, over the bar they bite into. Two tapered shapes
keep a silhouette that is still readable at the 18 px the badge is drawn at,
where Heroic's shield and Lutris's mark must stay tellable apart from it.

    python3 scripts/render-faugus-tab-icon.py [--output PATH]
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from tab_icon import BODY, GLYPH, brush, main


def _fang(QtGui, top_left: float, top_right: float) -> object:
    """One fang: a wide root at the jaw, curving to a point well below it."""
    tip = (top_left + top_right) / 2
    fang = QtGui.QPainterPath()
    fang.moveTo(top_left, 54)
    fang.lineTo(top_right, 54)
    # The flanks stay near vertical for the first third and only then close
    # on the point: a straight taper reads as a triangle, and the length is
    # what makes it a tooth instead of a stud.
    fang.cubicTo(top_right, 82, tip + 5, 96, tip, 108)
    fang.cubicTo(tip - 5, 96, top_left, 82, top_left, 54)
    fang.closeSubpath()
    return fang


def draw_glyph(painter, QtCore, QtGui) -> None:
    # The jaw the fangs hang from: without it they float as two unrelated
    # shapes at tab size. Kept thin so the teeth carry the silhouette.
    painter.setBrush(brush(QtGui, BODY))
    painter.drawRoundedRect(QtCore.QRectF(28, 34, 72, 16), 7, 7)

    painter.setBrush(brush(QtGui, GLYPH))
    painter.drawPath(_fang(QtGui, 36, 56))
    painter.drawPath(_fang(QtGui, 72, 92))


if __name__ == "__main__":
    raise SystemExit(
        main(draw_glyph, asset_name="tab-faugus.png", doc=__doc__)
    )
