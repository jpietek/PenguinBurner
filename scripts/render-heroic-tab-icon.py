#!/usr/bin/env python3
"""Draw ui/assets/tab-heroic.png, the Heroic launcher's library badge.

The badge is only used where the machine has no Heroic icon of its own -- a
config left behind by an uninstalled Heroic -- so it is deliberately NOT the
Heroic brand mark: shipping someone's logo is the maintainer's call, not a
detail to slip in with a feature. Swapping in a real one later means replacing
the file, nothing else.

A plain shield, which reads at the 18 px the badge is actually drawn at, with
one notch cut back out so it does not collapse into a lozenge.

    python3 scripts/render-heroic-tab-icon.py [--output PATH]
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from tab_icon import BACKGROUND, GLYPH, brush, main


def draw_glyph(painter, QtCore, QtGui) -> None:
    shield = QtGui.QPainterPath()
    shield.moveTo(33, 30)
    shield.lineTo(95, 30)
    shield.lineTo(95, 62)
    # One curve down each side into a single point: a straight-sided triangle
    # reads as an arrow at tab size, and a rounder skirt as a heart.
    shield.cubicTo(95, 84, 82, 97, 64, 106)
    shield.cubicTo(46, 97, 33, 84, 33, 62)
    shield.closeSubpath()
    painter.setBrush(brush(QtGui, GLYPH))
    painter.drawPath(shield)

    # A chevron punched back out in the plate colour, so the glyph reads as a
    # shield rather than as a filled blob.
    chevron = QtGui.QPainterPath()
    chevron.moveTo(64, 46)
    chevron.lineTo(82, 58)
    chevron.lineTo(75, 58)
    chevron.lineTo(64, 51)
    chevron.lineTo(53, 58)
    chevron.lineTo(46, 58)
    chevron.closeSubpath()
    painter.setBrush(brush(QtGui, BACKGROUND))
    painter.drawPath(chevron)
    painter.drawRoundedRect(QtCore.QRectF(58, 62, 12, 26), 5, 5)


if __name__ == "__main__":
    raise SystemExit(
        main(draw_glyph, asset_name="tab-heroic.png", doc=__doc__)
    )
