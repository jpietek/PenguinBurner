#!/usr/bin/env python3
"""Draw ui/assets/tab-game-library.png, the Game Library tab's icon.

Same house style as the other tab icons: a 128x128 PNG, a dark rounded square,
one bright glyph in PenguinBurner's accent green. Generated rather than drawn
so the asset in the tree can be reproduced and reviewed as code.

The glyph is a gamepad. Nothing about it belongs to Steam or to Lutris: the
tab holds both, and either brand mark would claim it for one of them.

Drawn for 18 px, which is the size the tab bar actually uses: a plain rounded
body with no grips, because flared ends collapse into an infinity sign at that
size, and the cutouts carry the reading instead. The body takes the light tone
the Profiles icon uses and the buttons the accent green, so it sits in the
family without being a second all-green glyph beside it.

    python3 scripts/render-game-library-tab-icon.py [--output PATH]
"""

from __future__ import annotations

import argparse
import pathlib
import sys

SIZE = 128
CORNER_RADIUS = 26
BACKGROUND = "#0d1014"
BORDER = "#2e3440"
BODY = "#e8edf2"
GLYPH = "#5ef38c"


def render(output: pathlib.Path) -> pathlib.Path:
    from PySide6 import QtCore, QtGui

    QtCore.QCoreApplication.setAttribute(
        QtCore.Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True
    )
    app = QtGui.QGuiApplication.instance() or QtGui.QGuiApplication(
        [sys.argv[0], "-platform", "offscreen"]
    )

    image = QtGui.QImage(SIZE, SIZE, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.transparent)

    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

    plate = QtCore.QRectF(2, 2, SIZE - 4, SIZE - 4)
    painter.setBrush(QtGui.QBrush(QtGui.QColor(BACKGROUND)))
    painter.setPen(QtGui.QPen(QtGui.QColor(BORDER), 3))
    painter.drawRoundedRect(plate, CORNER_RADIUS, CORNER_RADIUS)

    painter.setPen(QtCore.Qt.PenStyle.NoPen)

    # Body: a plain rounded slab. Grips and flared ends read as a controller at
    # 128 px and as a blob at 18, so the silhouette stays simple and the
    # cutouts do the work.
    painter.setBrush(QtGui.QBrush(QtGui.QColor(BODY)))
    painter.drawRoundedRect(QtCore.QRectF(12, 40, 104, 48), 20, 20)

    # D-pad punched back out in the plate colour, so it reads as a hole rather
    # than as another shape lying on top.
    painter.setBrush(QtGui.QBrush(QtGui.QColor(BACKGROUND)))
    pad_x, pad_y, arm, thickness = 42.0, 64.0, 32.0, 12.0
    painter.drawRoundedRect(
        QtCore.QRectF(pad_x - arm / 2, pad_y - thickness / 2, arm, thickness), 3, 3
    )
    painter.drawRoundedRect(
        QtCore.QRectF(pad_x - thickness / 2, pad_y - arm / 2, thickness, arm), 3, 3
    )

    # Two face buttons in the accent, kept clearly apart: touching circles
    # merge into one shapeless notch at tab size.
    painter.setBrush(QtGui.QBrush(QtGui.QColor(GLYPH)))
    painter.drawEllipse(QtCore.QRectF(72, 50, 17, 17))
    painter.drawEllipse(QtCore.QRectF(89, 67, 17, 17))
    painter.end()

    output.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(output), "PNG"):
        raise SystemExit(f"could not write {output}")
    del app
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parents[1]
        / "ui"
        / "assets"
        / "tab-game-library.png",
    )
    args = parser.parse_args(argv)
    written = render(args.output)
    print(f"wrote {written} ({SIZE}x{SIZE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
