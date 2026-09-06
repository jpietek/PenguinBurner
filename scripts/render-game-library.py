#!/usr/bin/env python3
"""Capture the current Game Library widget using installed games, read-only."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "1")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402
from integrations.launchers.library import SORT_RECENT  # noqa: E402
from ui.components.game_library_panel import GameLibraryPanel, game_key  # noqa: E402
from ui.qt import apply_dark_palette  # noqa: E402
from ui.styles import STYLESHEET  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--select", default="", help="Select a game by name.")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/assets/game-library.png")
    args = parser.parse_args()
    app = QtWidgets.QApplication([])
    app.setFont(QtGui.QFont("Noto Sans", 10))
    apply_dark_palette(app, QtGui)
    app.setStyleSheet(STYLESHEET)
    panel = GameLibraryPanel(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    panel.widget.resize(1320, 880)
    panel.widget.show()
    panel.ensure_scanned()
    deadline = time.monotonic() + 30
    while panel._scan_active and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    if panel._scan_active or not panel._games:
        raise SystemExit("The installed game library did not finish loading.")
    panel.sort_combo.setCurrentIndex(panel.sort_combo.findData(SORT_RECENT))
    matches = [game for game in panel._games if args.select.casefold() in game.name.casefold()]
    if args.select and not matches:
        raise SystemExit(f"Game not found: {args.select}")
    selected = matches[0] if args.select else next(
        (game for game in panel._games if game.launcher == "lutris"), panel._games[0]
    )
    panel._select_key(game_key(selected))
    app.processEvents()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if not panel.widget.grab().save(str(args.output)):
        raise SystemExit(f"Could not save {args.output}")
    print(f"Captured {selected.launcher}: {selected.name} to {args.output}")
    panel.widget.close()


if __name__ == "__main__":
    main()
