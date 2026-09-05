#!/usr/bin/env python3
"""Draw ui/assets/tab-lutris.png, the Lutris tab's icon.

The shipped tab icons are 128x128 PNGs in one house style: a dark rounded
square with a single bright glyph. This one is generated rather than drawn by
hand so the asset in the tree can always be reproduced and reviewed as code.

The glyph is a plain gamepad in PenguinBurner's own accent green, deliberately
NOT the Lutris brand mark: shipping someone's logo is the maintainer's call,
not a detail to slip in with a feature. Swapping in a real Lutris icon later
means replacing the file, nothing else.

    python3 scripts/render-lutris-tab-icon.py [--output PATH]
"""

from __future__ import annotations

import argparse
import pathlib
import sys

SIZE = 128
CORNER_RADIUS = 26
BACKGROUND = "#0d1014"
BORDER = "#2e3440"
GLYPH = "#5ef38c"


def render(output: pathlib.Path) -> pathlib.Path:
    from PySide6 import QtCore, QtGui

    # QPainter needs a QGuiApplication for font/paint device setup even
    # offscreen; the platform plugin is forced so this runs headless in CI.
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
    painter.setBrush(QtGui.QBrush(QtGui.QColor(GLYPH)))

    # A plain rounded body, no grips. Flared ends photograph well at 128 px
    # and collapse into an infinity sign at 18 px, which is the size that
    # actually matters here; the d-pad and buttons carry the reading instead.
    painter.drawRoundedRect(QtCore.QRectF(17, 47, 94, 36), 15, 15)

    # D-pad and buttons punched back out in the plate colour, so the glyph
    # reads as a gamepad rather than as a lozenge.
    painter.setBrush(QtGui.QBrush(QtGui.QColor(BACKGROUND)))
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
        / "tab-lutris.png",
    )
    args = parser.parse_args(argv)
    written = render(args.output)
    print(f"wrote {written} ({SIZE}x{SIZE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
