#!/usr/bin/env python3
"""Render the native Qt mock's review images from local fixtures.

Every native tab at 1350x900 (or --size): Auto UV (setup dialog and a running
full scan), Profiles (and its context menu), Game Library (and the Perf Profile
peek), the Perf Profile page, In-Game Overlay, Prototype controls, About and
Appearance. --theme and --accent render the whole set in another Appearance.
"""

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from preview import COPY, Preview, configure_app
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QTableWidget

from ui.design.style import install_theme
from ui.design.tokens import AppearanceSettings

SIZE = (1350, 900)


def composite(window, popup):
    """The window with a popup (menu, peek) painted where it opened: a
    popup is its own top-level, so window.grab() alone leaves it out."""
    image = window.grab()
    painter = QPainter(image)
    painter.drawPixmap(window.mapFromGlobal(popup.pos()), popup.grab())
    painter.end()
    return image


def capture(
    output: Path | None = None,
    size: tuple[int, int] = SIZE,
    appearance: AppearanceSettings | None = None,
):
    app = QApplication(sys.argv[:1])
    configure_app(app)
    if appearance is not None:
        install_theme(app, appearance)
    window = Preview()
    window.profile_source.command_delay_ms = 0
    window.resize(*size)
    window.show()
    QTest.qWait(150)
    scan = window.scan
    output = (output or Path(__file__).with_name("screenshots")).resolve()
    output.mkdir(parents=True, exist_ok=True)

    def save(name, image=None):
        QTest.qWait(80)
        assert (image or window.grab()).save(str(output / name)), name

    choices = 0

    def press(dialog, text):
        next(b for b in dialog.findChildren(QPushButton) if b.text() == text).click()

    def drive_dialogs():
        """Answers the setup dialog, then each tier's final-candidate choice,
        which ScanPage opens modally as soon as that tier's descent ends."""
        nonlocal choices
        dialog = app.activeModalWidget()
        if not isinstance(dialog, QDialog) or not dialog.isVisible():
            return
        labels = {b.text() for b in dialog.findChildren(QPushButton)}
        if "Start Auto Undervolt" in labels:
            purpose = dialog.findChild(QLabel, "purposeText")
            assert purpose and purpose.text() == COPY["setupNote"]
            cautions = dialog.findChildren(QLabel, "autoUvTargetCaution")
            assert cautions and all(
                label.text() == COPY["targetCaution"] for label in cautions
            )
            QTest.qWait(40)
            assert dialog.grab().save(str(output / "auto-uv-setup-qt.png"))
            press(dialog, "Start Auto Undervolt")
        elif "Use Selected" in labels:
            table = dialog.findChild(QTableWidget)
            assert table and table.rowCount() > 1
            # Choose a distinct Performance run so both the live candidate
            # and the scan-confirmed Performance curve show in the capture.
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
            press(dialog, "Use Selected")

    save("auto-uv-idle-qt.png")
    driver = QTimer()
    driver.timeout.connect(drive_dialogs)
    driver.start(60)
    scan.setup_button.click()
    scan.timer.stop()
    while scan.busy() and not (
        scan.stage == "verifying" and scan.final_job.get("tier") == "Performance"
    ):
        scan.tick()
    assert choices == 3, choices
    for _ in range(4):
        scan.tick()
    scan.table.setCurrentCell(13, 0)
    save("auto-uv.png")
    while scan.busy():
        scan.tick()
    driver.stop()

    # Profiles: the finished scan applied its newest profile.
    window.tabs.setCurrentIndex(1)
    QTest.qWait(150)
    save("profiles-qt.png")
    profiles = window.profiles
    menu_opened = []

    def grab_menu():
        menu = app.activePopupWidget()
        if menu is None:
            QTimer.singleShot(40, grab_menu)
            return
        menu_opened.append(True)
        save("profiles-menu-qt.png", composite(window, menu))
        menu.close()

    QTimer.singleShot(120, grab_menu)
    table = profiles.table
    rect = table.visualRect(table.model().index(0, 1))
    profiles._open_menu(
        [table.row_id(0)], table.viewport().mapToGlobal(rect.center() + QPoint(40, 0))
    )
    assert menu_opened

    # Game Library, the Perf Profile peek, then the page.
    window.tabs.setCurrentIndex(2)
    window.library.select_game(0)
    QTest.qWait(100)
    save("game-library-qt.png")
    button = window.library.panel.perf_button
    button.show_peek()
    # The offscreen screen is smaller than the window; place the peek where
    # a real screen would, right-aligned under the button.
    anchor = button.mapToGlobal(button.rect().bottomRight())
    button.peek.move(anchor.x() - button.peek.width(), anchor.y() + 6)
    QTest.qWait(100)
    save("perf-peek-qt.png", composite(window, button.peek))
    button.hide_peek()
    button.click()
    QTest.qWait(150)
    save("perf-profile-qt.png")
    scroll = window.perf_page.scroll_area.verticalScrollBar()
    scroll.setValue(scroll.maximum())
    save("perf-profile-frametime-qt.png")
    window.perf_page.figure.set_live(True)
    save("perf-profile-live-qt.png")
    window.perf_page.figure.set_live(False)
    window.perf_page.back_button.click()

    window.tabs.setCurrentIndex(3)
    overlay = window.overlay.panel
    overlay.position_combo.setCurrentText("Top left")
    overlay.scale_slider.setValue(overlay.scale_slider.maximum())
    save("overlay-qt.png")

    window.prototype_button.click()
    dialog = window.prototype_dialog
    QTest.qWait(120)
    assert dialog is not None and dialog.grab().save(
        str(output / "prototype-controls-qt.png")
    )
    dialog.close()

    # About is a modal dialog: grab it once it is built, then close.
    about_opened = []

    def grab_about():
        about = app.activeModalWidget()
        if (
            not isinstance(about, QDialog)
            or about.findChild(QLabel, "aboutTitle") is None
        ):
            QTimer.singleShot(40, grab_about)
            return
        about_opened.append(True)
        QTest.qWait(120)
        assert about.grab().save(str(output / "about-qt.png"))
        about.accept()

    QTimer.singleShot(80, grab_about)
    window.show_about()
    assert about_opened

    window.show_appearance()
    QTest.qWait(120)
    assert window.appearance_dialog.grab().save(str(output / "appearance-qt.png"))
    window.appearance_dialog.reject()
    window.close()
    app.processEvents()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="directory for the PNGs (default: screenshots/ beside this script)",
    )
    parser.add_argument(
        "--size",
        default="x".join(map(str, SIZE)),
        help="window size as WIDTHxHEIGHT (default: %(default)s)",
    )
    parser.add_argument("--theme", choices=("dark", "light", "auto"))
    parser.add_argument("--accent", help="preset name or #rrggbb")
    args = parser.parse_args()
    width, _, height = args.size.partition("x")
    appearance = None
    if args.theme or args.accent:
        default = AppearanceSettings()
        appearance = AppearanceSettings(
            theme=args.theme or default.theme, accent=args.accent or default.accent
        )
    capture(args.output_dir, (int(width), int(height)), appearance)
