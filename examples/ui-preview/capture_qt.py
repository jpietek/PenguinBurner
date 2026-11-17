#!/usr/bin/env python3
"""Render native Auto UV and game-art overlay review images from local fixtures."""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-dev-shm-usage"
)

from preview import Preview, configure_app
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QTableWidget


def capture():
    app = QApplication(sys.argv)
    configure_app(app)
    window = Preview()
    window.resize(1350, 900)
    window.show()
    QTest.qWait(150)
    scan = window.scan
    output = Path(__file__).with_name("screenshots")
    output.mkdir(exist_ok=True)

    def start_full_scan():
        dialog = app.activeModalWidget()
        assert isinstance(dialog, QDialog)
        assert dialog.grab().save(str(output / "auto-uv-setup-qt.png"))
        next(
            b
            for b in dialog.findChildren(QPushButton)
            if b.text() == "Start Auto Undervolt"
        ).click()

    QTimer.singleShot(120, start_full_scan)
    scan.setup_button.click()
    scan.timer.stop()
    while scan.stage == "running":
        scan.tick()
    assert scan.stage == "choice"

    choices = 0

    def choose_candidate():
        nonlocal choices
        dialog = app.activeModalWidget()
        assert isinstance(dialog, QDialog)
        table = dialog.findChild(QTableWidget)
        assert table and table.rowCount() > 1
        # Choose a distinct Performance run so both the live candidate and
        # the scan-confirmed Performance curve are visible in the capture.
        row = next(
            (
                i
                for i in range(table.rowCount())
                if (cell := table.item(i, 0)) is not None
                and cell.data(Qt.ItemDataRole.UserRole) == "11"
            ),
            0,
        )
        table.setCurrentCell(row, 0)
        table.selectRow(row)
        choices += 1
        if choices < 3:
            QTimer.singleShot(50, choose_candidate)
        next(
            b for b in dialog.findChildren(QPushButton) if b.text() == "Use Selected"
        ).click()

    QTimer.singleShot(50, choose_candidate)
    scan.next_button.click()
    assert choices == 3 and scan.stage == "verifying"
    scan.timer.stop()
    for _ in range(23):
        scan.tick()
    scan.table.setCurrentCell(13, 0)
    QTest.qWait(100)
    assert window.grab().save(str(output / "auto-uv.png"))
    window.tabs.setCurrentIndex(2)
    window.library.select_game(0)
    window.tabs.setCurrentIndex(3)
    QTest.qWait(100)
    assert window.grab().save(str(output / "overlay-qt.png"))
    window.close()
    app.processEvents()


if __name__ == "__main__":
    capture()
