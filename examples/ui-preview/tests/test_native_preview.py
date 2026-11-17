"""Exercise the graphics-only native workflow, including its browser handoff."""

import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-dev-shm-usage"
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import preview
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    preview.configure_app(instance)
    return instance


def javascript(window, expression):
    values = []
    loop = QEventLoop()

    def completed(value):
        values.append(value)
        loop.quit()

    QTimer.singleShot(5000, loop, loop.quit)
    window.browser.page().runJavaScript(expression, completed)
    loop.exec()
    assert values, "The embedded browser did not respond"
    return values[0]


def test_verified_row_and_library_handoff(app, monkeypatch):
    window = preview.Preview()
    window.show()
    try:
        for _ in range(500):
            if window.ready:
                break
            QTest.qWait(10)
        assert window.ready
        scan = window.scan
        assert scan.table.rowCount() == 0
        assert scan.latest_log.isHidden() and scan.log_strip.isHidden()
        assert scan.copy_button.parentWidget() is scan.log_strip
        monkeypatch.setattr(
            preview,
            "setup_options",
            lambda _: {
                "auto_uv_mode": "efficiency",
                "auto_uv_memory_offset_mhz": 200,
            },
        )
        scan.setup()
        scan.timer.stop()
        while scan.stage == "running":
            scan.tick()
        assert scan.stage == "choice"
        assert not scan.log_strip.isHidden()
        scan.copy_button.click()
        assert QApplication.clipboard().text() == "\n".join(scan.log_lines)
        assert "Copied the full scan log" in scan.copy_button.accessibleDescription()
        passed = [i for i, row in enumerate(scan.plan) if i and row["passed"]]
        picked = passed[0]

        def choose(**kwargs):
            assert (
                kwargs["default_duration_s"]
                == preview.DEFAULT_FINAL_VERIFICATION_DURATION_S
            )
            candidate = next(
                c for c in kwargs["candidates"] if c["candidate_id"] == str(picked)
            )
            return candidate, 420, False

        monkeypatch.setattr(preview, "select_final_candidate", choose)
        scan.next_action()
        scan.timer.stop()
        assert scan.stage == "verifying"
        while scan.stage == "verifying":
            scan.tick()
        assert scan.stage == "complete"
        row = scan.plan[picked]
        result = json.loads(
            javascript(
                window,
                "JSON.stringify(available().map(i=>[profiles[i],profileMeta[i]]))",
            )
        )
        assert len(result) == 1
        profile, meta = result[0]
        assert profile[1:5] == [row["mv"], row["target"], row["fps"], row["power"]]
        assert meta["fromVerification"] and meta["vf"] == row["points"]
        assert meta["durationMinutes"] == 7 and meta["mem"] == 100
        assert list(scan.tier_traces["Efficiency"].yData) == [
            y for _, y in row["points"]
        ]
        window.tabs.setCurrentIndex(2)
        window.library.apply_adaptive(4, 90)
        window.library.panel.target_fps_spin.setValue(108)
        scan.show_profiles.emit()
        assert json.loads(
            javascript(
                window,
                "adaptivePicker();JSON.stringify([Number(document.querySelector('#adaptiveGame').value),Number(document.querySelector('#adaptiveFps').value)])",
            )
        ) == [4, 108]
        javascript(window, "closePanel()")
        javascript(
            window,
            "adaptivePicker();document.querySelector('#adaptiveGame').value='1';document.querySelector('#adaptiveFps').value='93';applyGameAdaptive()",
        )
        QTest.qWait(50)
        assert window.stack.currentWidget() is window.library
        game = window.library.panel._selected_game()
        assert game and game.game_id == "2531310"
        assert isinstance(game.detail, SimpleNamespace)
        assert game.detail.setting.target_fps == 93
    finally:
        window.close()


def test_run_delta_lanes_and_legend(app):
    page = preview.ScanPage()
    page.resize(1220, 800)
    page.show()
    try:
        rect = page.rect()
        for col in (5, 6, 10):
            value, delta = page.lanes.cells(rect, col)
            assert value.right() < delta.left()
            assert delta.width() >= page.lanes.delta_width[col]
        assert not page.plot.plot.plotItem.legend.isVisible()
        assert page.plot.source_curve is not None
        assert (
            page.plot.source_curve.opts["pen"].color().name().lower()
            == preview.COL["baseCurve"].lower()
        )
        assert preview.delta_color(5, 0.9) == preview.COL["quiet"]
        assert preview.delta_color(5, 6) == preview.COL["gain"]
        assert preview.delta_color(6, -6) == preview.COL["gain"]
        assert preview.delta_color(10, -6) == preview.COL["loss"]
    finally:
        page.close()


def test_about_shows_purpose_or_live_fixture_energy(app, monkeypatch):
    window = preview.Preview()
    snapshots = []

    def inspect(dialog):
        dialog.show()
        QTest.qWait(30)
        labels = dialog.findChildren(preview.W.QLabel)
        assert any(label.pixmap() and not label.pixmap().isNull() for label in labels)
        version = dialog.findChild(preview.W.QLabel, "aboutVersion")
        assert (
            version
            and version.text() == f"Version {preview.about.application_version()}"
        )
        assert (
            version.textInteractionFlags()
            & preview.C.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        links = next(label for label in labels if label.openExternalLinks())
        assert preview.COL["accent"] in links.text()
        assert (
            preview.about.SPONSOR_URL in links.text()
            and preview.about.ISSUES_URL in links.text()
        )
        assert (
            links.textInteractionFlags()
            & preview.C.Qt.TextInteractionFlag.LinksAccessibleByKeyboard
        )
        savings = dialog.findChild(preview.W.QLabel, "aboutSavings")
        if savings:
            initial = savings.text()
            for _ in range(25):
                if savings.text() != initial:
                    break
                QTest.qWait(100)
            assert savings.text() != initial
            assert (
                "Total runtime:" in savings.text() and "Energy saved:" in savings.text()
            )
            snapshots.append("counter")
        else:
            purpose = dialog.findChild(preview.W.QLabel, "purposeText")
            assert purpose and purpose.text() == preview.COPY["aboutPurpose"]
            snapshots.append("purpose")
        dialog.close()
        return preview.W.QDialog.DialogCode.Accepted

    try:
        monkeypatch.setattr(preview.W.QDialog, "exec", inspect)
        window.show_about()
        window.on_scan_started()
        window.show_about()
        assert snapshots == ["purpose", "counter"]
    finally:
        window.close()


def test_import_selected_directory_and_fan_curve(app, monkeypatch, tmp_path):
    source = preview.HERE / "fixtures" / "Afterburner"
    chosen = tmp_path / "An Afterburner export"
    shutil.copytree(source, chosen)
    device = next((chosen / "Profiles").glob("VEN_10DE*.cfg"))
    renamed = device.with_name("VEN_10DE&DEV_ABCD.cfg")
    device.rename(renamed)
    expected_entries = preview.afterburner_import.afterburner_profile_entries(chosen)
    expected = next(
        entry for entry in expected_entries if entry["section"] == "Profile2"
    )
    window = preview.Preview()
    assert not window.scan.import_button.isEnabled()
    window.import_afterburner()
    window.show()
    for _ in range(500):
        if window.ready:
            break
        QTest.qWait(10)
    assert window.ready and window.scan.import_button.isEnabled()
    window.tabs.setCurrentIndex(1)
    assert javascript(
        window, "document.body.innerText.includes('No saved profiles yet')"
    )

    def choose(dialog):
        dialog.show()
        table = dialog.findChild(preview.W.QTableWidget)
        folder = dialog.findChild(preview.W.QLineEdit)
        browse = next(
            b
            for b in dialog.findChildren(preview.W.QToolButton)
            if b.accessibleName() == "Choose Afterburner Directory"
        )
        accept = next(
            b
            for b in dialog.findChildren(preview.W.QPushButton)
            if b.text() == "Import"
        )
        assert table and folder and table.rowCount() == 4
        table.selectRow(3)
        assert not accept.isEnabled()
        folder.setText(str(tmp_path / "absent"))
        folder.editingFinished.emit()
        assert table.rowCount() == 0 and not accept.isEnabled()
        browse.click()
        assert folder.text() == str(chosen) and table.rowCount() == 4
        table.selectRow(1)
        assert accept.isEnabled()
        accept.click()
        return dialog.result()

    try:
        monkeypatch.setattr(preview.W.QDialog, "exec", choose)
        monkeypatch.setattr(
            preview.W.QFileDialog, "getExistingDirectory", lambda *_: str(chosen)
        )
        window.import_afterburner()
        result = json.loads(
            javascript(
                window,
                "JSON.stringify([profiles[pi], profileMeta[pi], profileCurve('vf',pi), profileCurve('fan',pi)])",
            )
        )
        metrics, meta, vf, fan = result
        assert metrics[1:3] == [
            expected["target_voltage_mv"],
            expected["target_clock_mhz"],
        ]
        assert vf == [
            list(p) for p in preview.afterburner_import.entry_curve_points(expected)
        ]
        expected_fan = preview.afterburner_fan_curve_payload(chosen)
        assert expected_fan and fan == expected_fan["fan"]["curve"]
        assert meta["valid"] and meta["imported"] and meta["tier"] == "None"
        assert metrics[3:] == [None, None, None]
        assert window.tabs.currentIndex() == 1
        assert javascript(window, "document.body.innerText.includes(profiles[pi][0])")

    finally:
        window.close()


def test_each_launcher_badge_is_visible_in_the_composited_row(app):
    from native_library import LibraryPage

    window = LibraryPage()
    window.show()
    try:
        panel = window.panel
        badges = {}
        for game in panel._games:
            if game.launcher in badges:
                continue
            badge = panel._launcher_badge(game.launcher)
            assert badge and not badge.isNull()
            artwork = (
                panel._row_icon(game)
                .pixmap(preview.C.QSize(36, 48), badge.devicePixelRatio())
                .toImage()
            )
            marker = badge.toImage()
            matches = 0
            for y in range(marker.height()):
                for x in range(marker.width()):
                    pixel = marker.pixelColor(x, y)
                    if pixel.alpha() != 255:
                        continue
                    assert (
                        artwork.pixelColor(
                            artwork.width() - marker.width() + x,
                            artwork.height() - marker.height() + y,
                        )
                        == pixel
                    )
                    matches += 1
            assert matches > 8
            badges[game.launcher] = marker
        assert set(badges) == {"steam", "heroic", "lutris", "faugus"}
        assert all(
            a != b
            for i, a in enumerate(badges.values())
            for b in list(badges.values())[i + 1 :]
        )
    finally:
        window.close()


def test_overlay_game_art_tracks_library_and_native_hud(app):
    window = preview.Preview(overlay=True)
    window.show()
    try:
        assert (
            window.tabs.currentIndex() == 3
            and window.stack.currentWidget() is window.overlay
        )
        overlay = window.overlay.panel
        window.tabs.setCurrentIndex(2)
        for index, game_id in enumerate(("1091500", "2531310", "1888930", "782330")):
            window.library.select_game(index)
            assert overlay.art_path == preview.HERE / "artwork" / f"{game_id}art.webp"
            assert not overlay.preview.artwork.isNull()
        window.tabs.setCurrentIndex(3)
        overlay.live_button.click()
        assert overlay.sample_dialog is not None
        assert (
            overlay.sample_canvas.artwork.cacheKey()
            == overlay.preview.artwork.cacheKey()
        )
        assert "DOOM Eternal" in overlay.sample_dialog.windowTitle()
        overlay.position_combo.setCurrentText("Bottom left")
        overlay.scale_slider.setValue(overlay.scale_slider.maximum())
        assert (
            overlay.sample_canvas._config == overlay.preview._config == overlay.config
        )
        assert overlay.config.position == "bottom_left"
        assert "preview" in overlay.preview.accessibleName()
        enabled = overlay.sample_canvas.grab().toImage()
        overlay.enabled_switch.click()
        backdrop = overlay.sample_canvas.grab().toImage()
        assert backdrop != enabled and not overlay.config.enabled
        window.library.select_game(4)
        assert overlay.art_path is None
        assert (
            overlay.sample_canvas.artwork.isNull() and overlay.preview.artwork.isNull()
        )
        scenery = overlay.sample_canvas.grab().toImage()
        assert scenery != backdrop
        # A 4:1 source must lose its outer red bands to fill a 16:9 game
        # frame. Stretching the source would leave red pixels at both edges.
        art = preview.G.QPixmap(400, 100)
        art.fill(preview.G.QColor("red"))
        brush = preview.G.QPainter(art)
        brush.fillRect(100, 0, 200, 100, preview.G.QColor("lime"))
        brush.end()
        overlay.sample_canvas.set_artwork(art)
        frame = preview.G.QImage(160, 90, preview.G.QImage.Format.Format_RGB32)
        brush = preview.G.QPainter(frame)
        brush.scale(160 / 2560, 90 / 1440)
        overlay.sample_canvas._paint_scene(brush)
        brush.end()
        top = frame.pixelColor(2, 1)
        bottom = frame.pixelColor(157, 88)
        assert top.green() > top.red() and bottom.green() > bottom.red()
        assert 0 < bottom.green() < top.green() < 255
        overlay.live_button.click()
        assert overlay.sample_dialog is None
    finally:
        window.close()
