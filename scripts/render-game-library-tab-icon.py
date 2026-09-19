#!/usr/bin/env python3
"""Draw ui/assets/tab-game-library.png, the Game Library tab's icon.

The glyph is a gamepad. Nothing about it belongs to Steam, Lutris or Heroic:
the tab holds them all, and any one brand mark would claim it for one of them.

Drawn for 18 px, which is the size the tab bar actually uses: a plain rounded
body with no grips, because flared ends collapse into an infinity sign at that
size, and the cutouts carry the reading instead. The body takes the light tone
the Profiles icon uses and the buttons the accent green, so it sits in the
family without being a second all-green glyph beside it.

    python3 scripts/render-game-library-tab-icon.py [--output PATH]
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from tab_icon import BACKGROUND, BODY, GLYPH, brush, main


def draw_glyph(painter, QtCore, QtGui) -> None:
    # Body: a plain rounded slab. Grips and flared ends read as a controller at
    # 128 px and as a blob at 18, so the silhouette stays simple and the
    # cutouts do the work.
    painter.setBrush(brush(QtGui, BODY))
    painter.drawRoundedRect(QtCore.QRectF(12, 40, 104, 48), 20, 20)

    # D-pad punched back out in the plate colour, so it reads as a hole rather
    # than as another shape lying on top.
    painter.setBrush(brush(QtGui, BACKGROUND))
    pad_x, pad_y, arm, thickness = 42.0, 64.0, 32.0, 12.0
    painter.drawRoundedRect(
        QtCore.QRectF(pad_x - arm / 2, pad_y - thickness / 2, arm, thickness), 3, 3
    )
    painter.drawRoundedRect(
        QtCore.QRectF(pad_x - thickness / 2, pad_y - arm / 2, thickness, arm), 3, 3
    )

    # Two face buttons in the accent, kept clearly apart: touching circles
    # merge into one shapeless notch at tab size.
    painter.setBrush(brush(QtGui, GLYPH))
    painter.drawEllipse(QtCore.QRectF(72, 50, 17, 17))
    painter.drawEllipse(QtCore.QRectF(89, 67, 17, 17))


if __name__ == "__main__":
    raise SystemExit(
        main(draw_glyph, asset_name="tab-game-library.png", doc=__doc__)
    )
