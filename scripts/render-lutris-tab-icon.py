#!/usr/bin/env python3
"""Draw ui/assets/tab-lutris.png, the Lutris launcher's library badge.

The badge is only used where the machine has no Lutris icon of its own, so it
is deliberately NOT the Lutris brand mark: shipping someone's logo is the
maintainer's call, not a detail to slip in with a feature. Swapping in a real
Lutris icon later means replacing the file, nothing else.

A plain gamepad in PenguinBurner's accent green. Drawn for 18 px, which is the
size that actually matters here: a rounded body with no grips, because flared
ends collapse into an infinity sign at that size, and the d-pad and buttons
carry the reading instead.

    python3 scripts/render-lutris-tab-icon.py [--output PATH]
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from tab_icon import BACKGROUND, GLYPH, brush, main


def draw_glyph(painter, QtCore, QtGui) -> None:
    painter.setBrush(brush(QtGui, GLYPH))
    painter.drawRoundedRect(QtCore.QRectF(17, 47, 94, 36), 15, 15)

    # D-pad and buttons punched back out in the plate colour, so the glyph
    # reads as a gamepad rather than as a lozenge.
    painter.setBrush(brush(QtGui, BACKGROUND))
    d_pad_x, d_pad_y, arm, thickness = 44.0, 65.0, 24.0, 8.0
    painter.drawRect(
        QtCore.QRectF(d_pad_x - arm / 2, d_pad_y - thickness / 2, arm, thickness)
    )
    painter.drawRect(
        QtCore.QRectF(d_pad_x - thickness / 2, d_pad_y - arm / 2, thickness, arm)
    )
    # Two face buttons, kept clearly apart: touching circles merge into one
    # shapeless notch at tab size.
    painter.drawEllipse(QtCore.QRectF(76, 54, 12, 12))
    painter.drawEllipse(QtCore.QRectF(76, 68, 12, 12))


if __name__ == "__main__":
    raise SystemExit(
        main(draw_glyph, asset_name="tab-lutris.png", doc=__doc__)
    )
