"""The plate every shipped tab icon is drawn on.

The tab icons are 128x128 PNGs in one house style: a dark rounded square with a
single bright glyph. Only the glyph differs, so only the glyph lives in each
script -- and a new tab costs one small function rather than another copy of
the Qt setup.

Not a CLI itself. `scripts/render-*-tab-icon.py` each call `main` with their
own glyph.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from collections.abc import Callable

SIZE = 128
CORNER_RADIUS = 26
BACKGROUND = "#0d1014"
BORDER = "#2e3440"
#: PenguinBurner's accent green, and the light tone that pairs with it.
GLYPH = "#5ef38c"
BODY = "#e8edf2"

ASSET_DIR = pathlib.Path(__file__).resolve().parents[1] / "ui" / "assets"


def render(output: pathlib.Path, draw_glyph: Callable[..., None]) -> pathlib.Path:
    """Draw the plate, hand the painter to ``draw_glyph``, save the PNG."""
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
    draw_glyph(painter, QtCore, QtGui)
    painter.end()

    output.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(output), "PNG"):
        raise SystemExit(f"could not write {output}")
    del app
    return output


def main(
    draw_glyph: Callable[..., None],
    *,
    asset_name: str,
    doc: str,
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=doc)
    parser.add_argument("--output", type=pathlib.Path, default=ASSET_DIR / asset_name)
    written = render(parser.parse_args(argv).output, draw_glyph)
    print(f"wrote {written} ({SIZE}x{SIZE})")
    return 0


def brush(QtGui, colour: str):
    return QtGui.QBrush(QtGui.QColor(colour))
