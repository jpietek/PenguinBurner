"""Exercise the native Qt mock: every tab is a widget, no QtWebEngine."""

import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import preview
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from qt.features.profiles_page import ProfileDialogs


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    preview.configure_app(instance)
    return instance


def native_window(**kwargs):
    """The mock window with instant sample commands (no 700 ms daemon delay)."""
    window = preview.Preview(**kwargs)
    window.profile_source.command_delay_ms = 0
    return window


def wait_until(condition, limit=300):
    for _ in range(limit):
        if condition():
            return True
        QTest.qWait(10)
    return bool(condition())


class ScriptedDialogs(ProfileDialogs):
    """Profiles' message boxes answered in place, and recorded."""

    def __init__(self, minutes=5):
        self.minutes = minutes
        self.messages = []

    def confirm(self, parent, title, text, *, accept="Yes"):
        self.messages.append(("confirm", title, text))
        return True

    def info(self, parent, title, text):
        self.messages.append(("info", title, text))

    def error(self, parent, title, text):
        self.messages.append(("error", title, text))

    def failure(self, parent, title, message, details):
        self.messages.append(("failure", title, message))

    def verify_minutes(self, parent, label):
        return self.minutes


def scan_options(mode="adaptive", **extra):
    return lambda *_, **__: {"auto_uv_mode": mode, **extra}


def cell(table, row, col):
    item = table.item(row, col)
    assert item is not None
    return item


def run_scan(scan, limit=400):
    """Drive the simulated backend; candidate dialogs open on their own."""
    for _ in range(limit):
        if not scan.busy():
            return
        scan.tick()
    raise AssertionError("scan did not finish")


def test_no_webengine_dependency():
    assert not any(name.startswith("PySide6.QtWebEngine") for name in sys.modules)
    source = Path(preview.__file__).read_text()
    assert "QWebEngine" not in source and "build_preview" not in source


def test_verified_row_and_library_handoff(app, monkeypatch):
    window = native_window()
    window.show()
    try:
        scan, profiles = window.scan, window.profiles
        profiles.dialogs = ScriptedDialogs(minutes=5)
        assert scan.table.rowCount() == 0
        assert scan.log_view.toPlainText() == ""
        assert not scan.copy_button.isEnabled()
        assert scan.copy_button.parentWidget() is scan.log_pane
        assert scan.plot_log.indexOf(scan.log_pane) == 1
        monkeypatch.setattr(
            preview,
            "setup_options",
            scan_options("efficiency", auto_uv_memory_offset_mhz=200),
        )
        efficiency = [r for r in preview.SAMPLE["rows"] if r["tier"] == "Efficiency"]
        picked = next(i for i, row in enumerate(efficiency, 1) if row["passed"])
        requests = []

        def choose(**kwargs):
            requests.append(kwargs)
            # The tier default reaches the dialog: Efficiency verifies 1 min.
            assert kwargs["default_duration_s"] == 60
            assert not scan.timer.isActive()
            candidate = next(
                c for c in kwargs["candidates"] if c["candidate_id"] == str(picked)
            )
            return candidate, 420, "select"

        monkeypatch.setattr(preview, "select_final_candidate", choose)
        scan.setup()
        scan.timer.stop()
        assert profiles.busy(), "Profiles locks while the scan runs"
        assert not scan.tier_strip.isVisibleTo(scan), "single-profile scans hide it"
        run_scan(scan)
        assert [r["request_reason"] for r in requests] == [""]
        assert scan.stage == "complete" and not profiles.busy()
        assert window.tabs.currentIndex() == 1, "completion switches to Profiles"
        assert window.stack.currentWidget() is profiles
        final = scan.runs[-1]
        assert final["kind"] == "final" and final["duration"] == 420
        assert cell(scan.table, len(scan.runs) - 1, 12).text() == "Final · 7min / 7min"
        assert scan.log_view.toPlainText().splitlines() == scan.log_lines
        assert "Auto-UV process finished: exit_code=0" in scan.log_lines[-1]
        scan.copy_button.click()
        assert QApplication.clipboard().text() == "\n".join(scan.log_lines)
        assert "Copied the full scan log" in scan.copy_button.accessibleDescription()
        # The saved profile is new, selected, on top, and applied by completion.
        row = scan.plan[picked]
        saved = profiles.preferred_id
        assert saved and profiles.selected_ids() == [saved]
        assert profiles.table.row_id(0) == saved
        assert wait_until(
            lambda: window.profile_source.runtime().running_selector == saved
        )
        payload = window.profile_source.payloads[saved]
        assert [
            payload["candidate_voltage_mv"],
            payload["lock_clock_mhz"],
            payload["avg_fps"],
            payload["avg_power_w"],
        ] == [row["mv"], row["target"], row["fps"], row["power"]]
        assert [(p["voltage_mv"], p["target_mhz"]) for p in payload["plan"]] == [
            (round(mv), round(mhz)) for mv, mhz in row["points"]
        ]
        # 200 MT/s in setup is +100 MHz memory clock, stored as the offset.
        assert payload["memory_offset_mhz"] == 200
        assert "Starting profile" not in window.status_running.text()
        assert list(scan.tier_traces["Efficiency"].yData) == [
            y for _, y in row["points"]
        ]

        # Use Adaptive for a game: the picker starts on the library's
        # selection and that game's target, then applies in Game Library.
        window.tabs.setCurrentIndex(2)
        window.library.apply_adaptive(4, 90)
        window.library.panel.target_fps_spin.setValue(108)
        scan.show_profiles.emit()
        seen = []

        def pick(dialog):
            seen.append((dialog.game_combo.currentData(), dialog.fps_spin.value()))
            if len(seen) == 1:
                return preview.W.QDialog.DialogCode.Rejected
            dialog.game_combo.setCurrentIndex(1)
            dialog.fps_spin.setValue(93)
            return preview.W.QDialog.DialogCode.Accepted

        monkeypatch.setattr(preview.AdaptiveGamePicker, "exec", pick)
        profiles.adaptive.use_adaptive.emit()
        assert seen == [("heroic:control", 108)]
        assert window.tabs.currentIndex() == 1
        profiles.adaptive.use_adaptive.emit()
        assert window.stack.currentWidget() is window.library
        game = window.library.panel._selected_game()
        assert game and game.game_id == "2531310"
        assert isinstance(game.detail, SimpleNamespace)
        assert game.detail.setting.target_fps == 93
        assert game.detail.setting.mode == "adaptive"

        # Profiles → Verify runs on the native Auto UV page with header progress.
        progress = []
        scan.progress_changed.connect(lambda text, *_: progress.append(text))
        window.tabs.setCurrentIndex(1)
        profiles.verify(saved)
        assert scan.profile_job and window.tabs.currentIndex() == 0
        assert window.header_progress.isVisible()
        scan.timer.stop()
        assert progress[0] == "Verifying profile 0s / 5min"
        run_scan(scan)
        assert progress[-1] == "Verifying profile 5min / 5min"
        assert scan.status_text == "Profile verification complete."
        assert profiles.notice[0].startswith("Profile verification passed")
    finally:
        window.close()


def test_full_scan_interleaves_tiers_and_discard_skips_one(app, monkeypatch):
    page = preview.ScanPage()
    saved, requests, statuses = [], [], []
    page.profile_saved.connect(lambda tier, *_: saved.append(tier))
    page.status_changed.connect(statuses.append)
    monkeypatch.setattr(preview, "setup_options", scan_options())

    def choose(**kwargs):
        requests.append(kwargs)
        tier = kwargs["auto_uv_mode"].title()
        # Earlier tiers are saved before a later tier's dialog opens.
        assert (
            saved == [t for t in ("Efficiency",) if tier == "Performance"]
            or (tier == "Efficiency" and saved == [])
            or tier == "Balanced"
        )
        assert kwargs["default_duration_s"] == preview.FINAL_MINUTES[tier] * 60
        if tier == "Balanced":
            return None, kwargs["default_duration_s"], "discard"
        default = next(
            c
            for c in kwargs["candidates"]
            if c["candidate_id"] == kwargs["default_candidate_id"]
        )
        return default, kwargs["default_duration_s"], "select"

    monkeypatch.setattr(preview, "select_final_candidate", choose)
    try:
        page.setup()
        page.timer.stop()
        assert page.tier_strip.isVisibleTo(page)
        run_scan(page)
        assert [r["request_reason"] for r in requests] == [
            "adaptive-efficiency",
            "adaptive-balanced",
            "adaptive-performance",
        ]
        assert saved == ["Efficiency", "Performance"]
        assert page.tier_state == {
            "Efficiency": "complete",
            "Balanced": "skipped",
            "Performance": "complete",
        }
        assert page.tier_strip.label_for("Efficiency") == "✓ Complete"
        assert page.stage == "complete"
        assert any(s and s["running"].startswith("Auto UV · ") for s in statuses)
        assert statuses[-1] is None
        kinds = [r["kind"] for r in page.runs]
        assert kinds.count("final") == 2 and kinds[0] == "baseline"
        # Results paint a coloured dot; status cells carry a progress fraction.
        failed = next(i for i, r in enumerate(page.runs) if r["state"] == "fail")
        assert cell(page.table, failed, 11).data(preview.RESULT_ROLE) == "warning"
        assert cell(page.table, failed, 12).text() == "Clock too low"
        assert "kind: low-clock" in cell(page.table, failed, 11).toolTip()
        assert cell(page.table, 1, 12).data(preview.PROGRESS_ROLE) == 1.0
        assert page.legend.labels()[:2] == ["Stock", "Previous runs"]
    finally:
        page.close()


def test_single_tier_discard_saves_nothing(app, monkeypatch):
    page = preview.ScanPage()
    saved = []
    page.profile_saved.connect(lambda *args: saved.append(args))
    monkeypatch.setattr(preview, "setup_options", scan_options("balanced"))
    monkeypatch.setattr(
        preview, "select_final_candidate", lambda **kw: (None, 180, "discard")
    )
    try:
        page.setup()
        page.timer.stop()
        run_scan(page)
        assert page.stage == "discarded" and page.header_stage == "Discarded"
        assert (
            page.stage_detail.text()
            == "Final verification discarded. No Auto-UV profile was saved."
        )
        assert saved == []
    finally:
        page.close()


def test_failed_final_verification_and_abort(app, monkeypatch):
    page = preview.ScanPage()
    page.prototype["finalVerificationFailure"] = True
    requests = []
    monkeypatch.setattr(preview, "setup_options", scan_options())

    def choose(**kwargs):
        requests.append(kwargs)
        if kwargs["request_reason"] == "final-verification-failed":
            return None, kwargs["default_duration_s"], "abort"
        return kwargs["candidates"][0], 60, "select"

    monkeypatch.setattr(preview, "select_final_candidate", choose)
    try:
        page.setup()
        page.timer.stop()
        run_scan(page)
        failed = requests[-1]
        assert failed["request_reason"] == "final-verification-failed"
        assert failed["recovery_decision"]["decision"].startswith(
            "failed final verification"
        )
        assert requests[0]["candidates"][0]["candidate_id"] not in [
            c["candidate_id"] for c in failed["candidates"]
        ]
        assert page.stage == "aborted" and page.header_stage == "Aborted"
        assert page.tier_state["Efficiency"] == "stopped"
        assert page.tier_state["Performance"] == "not-run"
        final = next(r for r in page.runs if r["kind"] == "final")
        assert final["failure"]["label"] == "Vulkan device lost"
    finally:
        page.close()


def test_crash_recovery_resume_and_process_failure(app, monkeypatch):
    page = preview.ScanPage()
    consumed, errors, requests = [], [], []
    page.prototype_consumed.connect(consumed.append)
    page.prototype["crashRecovery"] = True
    monkeypatch.setattr(preview, "setup_options", scan_options())

    def choose(**kwargs):
        requests.append(kwargs)
        default = next(
            c
            for c in kwargs["candidates"]
            if c["candidate_id"] == kwargs["default_candidate_id"]
        )
        return default, 0, "select"

    monkeypatch.setattr(preview, "select_final_candidate", choose)
    monkeypatch.setattr(preview, "show_error_dialog", lambda **kw: errors.append(kw))
    try:
        page.setup()
        page.timer.stop()
        assert requests[0]["request_reason"] == "previous-crash"
        assert requests[0]["recovery_decision"]["lock_clock_mhz"] == 2585
        assert page.runs[-1]["kind"] == "resume"
        assert page.header_stage == "Resume verification"
        run_scan(page)
        assert page.stage == "complete" and consumed == ["crashRecovery"]

        page.prototype["processFailure"] = True
        page.setup()
        page.timer.stop()
        run_scan(page)
        assert page.stage == "failed" and page.header_stage == "Error"
        assert errors and errors[0]["title"] == "Auto-UV failed"
        assert errors[0]["details"].startswith("Action: Auto-UV scan\nExit code: 1")
        assert page.tier_state["Efficiency"] == "failed"
        assert "processFailure" in consumed

        # A matching setup resumes the saved measurements.
        done = page.done
        page.setup()
        page.timer.stop()
        assert page.header_stage == "Resuming"
        assert len(page.runs) == done and all(r["restored"] for r in page.runs)
        assert cell(page.table, 1, 11).toolTip() == "Saved measurement: passed"
        assert page.candidate_text.endswith("(saved)")
    finally:
        page.close()


def test_resume_reopens_an_unverified_tier_and_keeps_final_rows(app, monkeypatch):
    page = preview.ScanPage()
    requests = []
    monkeypatch.setattr(preview, "setup_options", scan_options())

    def choose(**kwargs):
        requests.append(kwargs["auto_uv_mode"])
        if kwargs["auto_uv_mode"] == "balanced" and len(requests) == 2:
            return None, kwargs["default_duration_s"], "abort"
        return kwargs["candidates"][0], 60, "select"

    monkeypatch.setattr(preview, "select_final_candidate", choose)
    try:
        page.setup()
        page.timer.stop()
        run_scan(page)
        assert page.stage == "aborted" and requests == ["efficiency", "balanced"]
        finished = [r for r in page.runs if r["state"] in ("pass", "fail")]
        page.setup()
        page.timer.stop()
        assert page.tier_state["Balanced"] == "active", "not skipped"
        assert page.tier_state["Efficiency"] == "complete"
        assert len(page.runs) == len(finished) == page.table.rowCount()
        assert any(r["kind"] == "final" and r["restored"] for r in page.runs)
        page.tick()
        assert requests[-1] == "balanced", "the Balanced choice reopens"
        run_scan(page)
        assert page.stage == "complete"
        assert set(page.tier_saved) == {"Efficiency", "Balanced", "Performance"}
    finally:
        page.close()


def test_selection_dims_the_live_candidate_and_marks_the_row(app, monkeypatch):
    page = preview.ScanPage()
    page.resize(1220, 800)
    monkeypatch.setattr(preview, "setup_options", scan_options("efficiency"))
    page.show()
    try:
        page.setup()
        page.timer.stop()
        for _ in range(3 * preview.PROBE_TICKS + 1):
            page.tick()
        page.table.setCurrentCell(1, 0)
        assert page.legend.items[-1][0] == "Run 2 (selected)"
        assert page.legend.items[-1][2] == "selected"
        swatch = (
            page.legend._layout.itemAt(len(page.legend.items) - 1)
            .widget()
            .layout()
            .itemAt(0)
            .widget()
        )
        assert swatch.style == "selected"
        assert page.plot.candidate_curve.opacity() == pytest.approx(preview.DIM_ALPHA)
        assert page.runs_empty.isHidden()
        image = page.table.viewport().grab().toImage()
        rect = page.table.visualRect(page.table.model().index(1, 0))
        bar = image.pixelColor(rect.left(), rect.center().y()).name()
        assert bar == preview.COL["accent"].lower()
        page.table.setCurrentCell(-1, 0)
        assert page.plot.candidate_curve.opacity() == 1.0
        # V/F ticks follow the browser's niceStep (200 MHz over this range).
        assert page.plot.plot.getAxis("left").tickSpacing(1800, 3200, 300)[0][0] == 200
    finally:
        page.timer.stop()
        page.close()


def test_scan_on_second_gpu_keeps_silent_fan_and_applies_there(app, monkeypatch):
    window = native_window()
    window.show()
    try:
        scan, profiles = window.scan, window.profiles
        profiles.dialogs = ScriptedDialogs()
        window.set_prototype("twoGpus", True)
        assert scan.prototype["twoGpus"] and len(window.profile_source.gpus()) == 2
        assert profiles.target_combo.isVisibleTo(profiles)
        profiles.set_silent_fan(True)
        monkeypatch.setattr(
            preview, "setup_options", scan_options("efficiency", gpu_index=1)
        )
        monkeypatch.setattr(
            preview,
            "select_final_candidate",
            lambda **kw: (kw["candidates"][0], 60, "select"),
        )
        scan.setup()
        scan.timer.stop()
        run_scan(scan)
        assert scan.stage == "complete" and scan.gpu_index == 1
        second = window.profile_source.gpus()[1]
        saved = profiles.preferred_id
        assert wait_until(
            lambda: window.profile_source.runtime().running_selector == saved
        )
        runtime = window.profile_source.runtime()
        assert runtime.running_gpu_uuid == second.uuid
        assert runtime.running_silent_fan is True
        payload = window.profile_source.payloads[saved]
        assert payload["gpu_identity"]["uuid"] == second.uuid

        # A legacy profile that passes Verify is bound and promoted by Profiles.
        legacy = "20260602-210400-legacy"
        window.verify_ids.append(legacy)
        window.profile_verified(len(window.verify_ids) - 1, True)
        bound = window.profile_source.payloads[legacy]["gpu_identity"]
        assert bound["uuid"] == second.uuid
        assert profiles.preferred_id == legacy
    finally:
        window.close()


def test_setup_dialog_gpu_selector_and_no_gpu(app):
    seen = {}

    def inspect():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, preview.W.QDialog)
        combo = dialog.findChild(preview.W.QComboBox, "gpuSelector")
        assert combo is not None
        start = next(
            b
            for b in dialog.findChildren(preview.W.QPushButton)
            if b.text() == "Start Auto Undervolt"
        )
        seen[len(seen)] = (combo.count(), combo.isEnabled(), start.isEnabled())
        dialog.reject()

    QTimer.singleShot(150, inspect)
    assert preview.setup_options(None, gpu_count=2) is None
    QTimer.singleShot(150, inspect)
    assert preview.setup_options(None, no_gpu=True) is None
    assert seen[0] == (2, True, True)
    assert seen[1][0] == 0 and seen[1][2] is False


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
        # Links take the accent from the themed palette.
        assert (
            links.palette().color(preview.G.QPalette.ColorRole.Link).name().upper()
            == preview.COL["accent"].upper()
        )
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
    window = native_window()
    window.profiles.dialogs = dialogs = ScriptedDialogs()
    assert window.scan.import_button.isEnabled(), "no page load to wait for"
    window.show()

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
        before = set(window.profile_source.payloads)
        window.import_afterburner()
        (new_id,) = set(window.profile_source.payloads) - before
        payload = window.profile_source.payloads[new_id]
        assert [payload["candidate_voltage_mv"], payload["lock_clock_mhz"]] == [
            expected["target_voltage_mv"],
            expected["target_clock_mhz"],
        ]
        assert [(p["voltage_mv"], p["target_mhz"]) for p in payload["plan"]] == [
            tuple(p) for p in preview.afterburner_import.entry_curve_points(expected)
        ]
        expected_fan = preview.afterburner_fan_curve_payload(chosen)
        assert expected_fan and payload["fan_curve_payload"] == expected_fan
        assert payload["verification_status"] == "imported"
        assert payload.get("avg_fps") is None and payload.get("avg_power_w") is None
        assert window.tabs.currentIndex() == 1
        assert window.profiles.selected_ids() == [new_id]
        assert window.profiles.table.row_id(0) == new_id
        assert dialogs.messages[-1][:2] == ("info", "Import Afterburner")
        assert window.profiles.notice[0].startswith("Imported Afterburner profile")
    finally:
        window.close()


def test_each_launcher_badge_is_visible_in_the_composited_row(app):
    from native_library import LibraryPage
    from qt.sample.perf_rings import SamplePerfSource

    window = LibraryPage(SamplePerfSource())
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
    import native_overlay

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
        # Live preview is a separate vkcube window running the real layer:
        # the cube scene, not the game art, with a running status line.
        overlay.live_button.click()
        assert overlay.sample_dialog is not None
        assert overlay.sample_dialog.windowTitle() == native_overlay.LIVE_TITLE
        assert overlay.status_label.text() == native_overlay.LIVE_RUNNING
        overlay.position_combo.setCurrentText("Bottom left")
        overlay.scale_slider.setValue(overlay.scale_slider.maximum())
        assert (
            overlay.sample_canvas._config == overlay.preview._config == overlay.config
        )
        assert overlay.config.position == "bottom_left"
        # A setting change keeps the running status.
        assert overlay.status_label.text() == native_overlay.LIVE_RUNNING
        assert "preview" in overlay.preview.accessibleName()
        enabled = overlay.sample_canvas.grab().toImage()
        overlay.sample_canvas.spin.stop()
        overlay.enabled_switch.click()
        backdrop = overlay.sample_canvas.grab().toImage()
        assert backdrop != enabled and not overlay.config.enabled
        window.library.select_game(4)
        assert overlay.art_path is None and overlay.preview.artwork.isNull()
        # A 4:1 source must lose its outer red bands to fill a 16:9 game
        # frame. Stretching the source would leave red pixels at both edges.
        art = preview.G.QPixmap(400, 100)
        art.fill(preview.G.QColor("red"))
        brush = preview.G.QPainter(art)
        brush.fillRect(100, 0, 200, 100, preview.G.QColor("lime"))
        brush.end()
        overlay.preview.set_artwork(art)
        frame = preview.G.QImage(160, 90, preview.G.QImage.Format.Format_RGB32)
        brush = preview.G.QPainter(frame)
        brush.scale(160 / 2560, 90 / 1440)
        overlay.preview._paint_scene(brush)
        brush.end()
        top = frame.pixelColor(2, 1)
        bottom = frame.pixelColor(157, 88)
        assert top.green() > top.red() and bottom.green() > bottom.red()
        assert 0 < bottom.green() < top.green() < 255
        overlay.live_button.click()
        assert overlay.sample_dialog is None
        assert overlay.live_button.text() == "Live preview"
        assert overlay.status_label.isHidden()
    finally:
        window.close()


def test_native_overlay_matches_browser_states(app):
    import dataclasses

    import native_overlay
    from PySide6.QtWidgets import QFrame, QLabel, QLineEdit, QPushButton

    page = native_overlay.OverlayPage()
    page.resize(1300, 1000)
    page.show()
    try:
        overlay = page.panel
        title = overlay.widget.findChild(QLabel, "overlayPageTitle")
        assert title.text() == "In-Game Overlay"
        # Headings sit inside their cards, one 17px/600 style.
        for card in overlay.widget.findChildren(QFrame, "prefGroupCard"):
            heads = card.findChildren(QLabel, "overlayGroupTitle")
            assert len(heads) == 1 and heads[0].text()[0].isupper()
        # No game publishing: the panel's sample readings and caption.
        caption = overlay.widget.findChild(QLabel, "overlayPreviewCaption")
        assert caption.text() == native_overlay.CAPTION_SAMPLE
        # A wrapped game publishing: missing sensors read '-', the tier is bare.
        overlay.set_publishing(True)
        assert caption.text() == native_overlay.CAPTION_LIVE
        assert overlay.item_value_labels["hotspot_temperature_c"].text() == "-"
        assert overlay.item_value_labels["nr_gpu_ms"].text() == "-"
        assert overlay.item_value_labels["profile"].text() == "Default"
        overlay.running_tier = "Balanced"
        overlay.refresh_preview()
        assert overlay.item_value_labels["profile"].text() == "Balanced"
        # overlay.toml written elsewhere (a shortcut) reloads on the 1 s tick.
        overlay.disk = dataclasses.replace(overlay.disk, position="bottom_left")
        overlay.refresh_preview()
        assert overlay.position_combo.currentText() == "Bottom left"
        before = overlay.config.detail
        overlay.press_cycle_shortcut()
        overlay.refresh_preview()
        assert overlay.config.detail != before
        # A failed save shows the error; the next successful save clears it.
        overlay.set_prototype({"overlaySaveFailure": True})
        overlay.opacity_slider.setValue(60)
        assert overlay.status_label.isVisibleTo(overlay.widget)
        assert overlay.status_label.property("error") is True
        assert round(overlay.config.opacity * 100) != 60
        overlay.set_prototype({"overlaySaveFailure": False})
        overlay.opacity_slider.setValue(50)
        assert overlay.status_label.isHidden()
        assert round(overlay.config.opacity * 100) == 50
        # Missing vkcube explains itself instead of opening a window.
        overlay.set_prototype({"livePreviewMissing": True})
        overlay.live_button.click()
        assert overlay.sample_dialog is None
        assert "vkcube" in overlay.status_label.text()
        # KDE opens settings directly; other desktops get the dialog.
        overlay.set_prototype({"overlayDesktop": "kde"})
        overlay._show_shortcuts()
        assert overlay.status_label.text() == native_overlay.KDE_OPENED
        # The real ui/dialogs/overlay_shortcuts dialog, desktop simulated.
        for desktop, buttons in (
            ("kde-fail", {"Open shortcut settings", "Close"}),
            ("gnome", {"Set up GNOME shortcuts", "Open shortcut settings", "Close"}),
            ("other", {"Close"}),
            ("other-flatpak", {"Close"}),
        ):
            overlay.set_prototype({"overlayDesktop": desktop})
            overlay._show_shortcuts()
            dialog = overlay._shortcuts_dialog
            texts = {b.text() for b in dialog.findChildren(QPushButton)} - {
                "Copy command"
            }
            assert texts == buttons, desktop
            labels = [label.text() for label in dialog.findChildren(QLabel)]
            assert any("(default: Alt+X)" in t for t in labels), labels
            commands = [
                field.text()
                for field in dialog.findChildren(QLineEdit)
                if field.objectName().startswith("overlayShortcutCommand")
            ]
            assert len(commands) == (8 if desktop.startswith("other") else 0)
            if desktop == "other-flatpak":
                assert all(c.startswith(native_overlay.FLATPAK_CLI) for c in commands)
            assert bool(dialog.findChildren(QLabel, "overlayShortcutError")) == (
                desktop == "kde-fail"
            )
            dialog.close()
    finally:
        page.close()


def test_native_editors_import_and_shell(app, monkeypatch):
    """The production editors and Afterburner chooser get the mock's
    legend, labels and footer order; the status band follows a playing game;
    Prototype controls is a native dialog wired to the pages."""
    window = native_window()
    window.show()
    seen = {}

    def inspect(dialog):
        dialog.setModal(True)
        dialog.show()
        QTest.qWait(80)
        # Left-to-right order on screen, not creation order.
        visible = [
            b for b in dialog.findChildren(preview.W.QPushButton) if b.isVisible()
        ]
        buttons = [
            b.text()
            for b in sorted(
                visible, key=lambda b: b.mapTo(dialog, preview.C.QPoint(0, 0)).x()
            )
        ]
        legends = dialog.findChildren(preview.ChartLegend)
        table = dialog.findChild(preview.W.QTableWidget)
        seen[dialog.windowTitle()] = {
            "buttons": buttons,
            "legend": legends[0].labels() if legends else [],
            "devices": [table.item(r, 0).text() for r in range(table.rowCount())]
            if table
            else [],
            "browse": [
                b.text()
                for b in dialog.findChildren(preview.W.QToolButton)
                if b.accessibleName() == "Choose Afterburner Directory"
            ],
        }
        dialog.reject()
        return 0

    try:
        monkeypatch.setattr(preview.W.QDialog, "exec", inspect)
        profiles = window.profiles
        balanced = "20260713-223600-balanced"
        profiles.edit_fan(balanced)
        fan = seen.pop("Edit Fan Curve")
        assert fan["legend"] == ["Measured", "Before edit", "Edited draft"]
        assert fan["buttons"].index("Cancel") < fan["buttons"].index("Save edited copy")
        profiles.edit_vf(balanced)
        vf = next(v for k, v in seen.items() if "VF" in k or "V/F" in k)
        assert vf["legend"] == ["Reference", "Before edit", "Edited draft"]
        assert "Save" not in vf["buttons"]
        seen.clear()
        window.import_afterburner()
        chooser = seen["Import Afterburner"]
        assert chooser["devices"] and all(
            "&SUBSYS" not in d for d in chooser["devices"]
        )
        assert chooser["browse"] == ["Choose directory…"]
        assert chooser["buttons"].index("Cancel") < chooser["buttons"].index("Import")
        assert chooser["legend"] == ["Imported"]
        monkeypatch.undo()

        # The window status band: a playing game, then Profiles' summary.
        window.library.status_changed.emit(
            {"running": "Playing DOOM Eternal", "detail": "Performance · via Steam"}
        )
        assert "Playing DOOM Eternal" in window.status_running.text()
        window.set_global_status(None, "scan")
        assert "Playing DOOM Eternal" in window.status_running.text()
        window.library.status_changed.emit(None)
        assert "Playing" not in window.status_running.text()
        assert window.status_running.text() == profiles.status()["running"]
        for index in range(4):
            window.tabs.setCurrentIndex(index)
            assert window.status_band.isVisible()

        window.prototype_button.click()
        dialog = window.prototype_dialog
        assert dialog is not None and dialog.isVisible()
        actions = {b.text() for b in dialog.findChildren(preview.W.QPushButton)}
        assert {"Run startup checks", "Show an error report", "Close"} <= actions
        assert set(dialog.combos) == {
            "daemon",
            "launcherWriteState",
            "libraryScan",
            "libraryWrite",
            "nextLaunch",
            "overlayDesktop",
        }
        assert {
            "twoGpus",
            "emptyLibrary",
            "crashRecovery",
            "finalVerificationFailure",
            "dependencyDownload",
        } <= set(dialog.switches)
        dialog.switches["crashRecovery"].click()
        assert window.scan.prototype["crashRecovery"] is True
        dialog.combos["daemon"].setCurrentIndex(
            dialog.combos["daemon"].findData("missing")
        )
        assert window.scan.prototype["daemon"] == "missing"
        assert window.profile_source.daemon_available is False
        dialog.combos["overlayDesktop"].setCurrentIndex(
            dialog.combos["overlayDesktop"].findData("gnome")
        )
        assert window.overlay.panel.desktop == "gnome"
        window.scan.consume("crashRecovery")
        assert dialog.switches["crashRecovery"].isChecked() is False
        dialog.close()
    finally:
        window.close()


def test_setup_checks_the_root_service_first(app, monkeypatch):
    window = native_window()
    window.show()
    calls = []
    try:
        monkeypatch.setattr(
            preview, "setup_options", lambda *a, **k: calls.append(k) or None
        )
        window.set_prototype("daemon", "outdated")
        answers = []

        def decline(box):
            answers.append(box.text())
            return 0

        monkeypatch.setattr(preview.W.QMessageBox, "exec", decline)
        window.scan.setup()
        assert calls == [], "declining the install prompt opens no setup"
        assert "does not support scan streaming" in answers[0]

        def accept(box):
            go = next(b for b in box.buttons() if b.text() == "Update service")
            go.click()
            return 0

        monkeypatch.setattr(preview.W.QMessageBox, "exec", accept)
        window.scan.setup()
        assert len(calls) == 1 and window.prototype.daemon == "ready"
        assert window.profile_source.daemon_available is True
    finally:
        window.close()


def test_dependency_download_shows_header_progress(app, monkeypatch):
    window = native_window()
    window.show()
    try:
        window.set_prototype("dependencyDownload", True)
        monkeypatch.setattr(preview, "setup_options", scan_options("efficiency"))
        window.scan.setup()
        window.scan.timer.stop()
        assert window.header_progress.isVisible()
        assert window.header_progress.format().startswith("Downloading dependencies")
        assert "Auto UV" in window.status_running.text()
        window.tabs.setCurrentIndex(2)
        assert "Auto UV" in window.status_running.text(), "visible on every tab"
        window.scan.stop()
        assert not window.header_progress.isVisible()
    finally:
        window.scan.timer.stop()
        window.close()


def test_perf_profile_entry_opens_page_and_back_restores_library(app):
    window = native_window()
    window.resize(1350, 900)
    window.show()
    try:
        library, panel = window.library, window.library.panel
        window.tabs.setCurrentIndex(2)
        button = panel.perf_button
        from ui.features.library.library_page import find_in_layout

        header = panel.play_button.parentWidget().layout()
        found = find_in_layout(header, button)
        assert found is not None
        beside, index = found
        assert beside.itemAt(index + 1).widget() is panel.play_button
        # Filter the list, then open Cyberpunk's recording.
        panel.search.setText("cyber")
        library.select_game(0)
        assert button.isVisible() and button.has_recording
        assert not button.icon().isNull()
        assert button.property("perfState") == "ready"
        button.click()
        assert window.stack.currentWidget() is window.perf_page
        assert window.perf_page.game_key == "steam:1091500"
        assert window.perf_page.state == "detail"
        assert window.tabs.currentIndex() == 2
        # Another tab and back keeps the drill-down.
        window.tabs.setCurrentIndex(3)
        window.tabs.setCurrentIndex(2)
        assert window.stack.currentWidget() is window.perf_page
        window.perf_page.back_button.click()
        assert window.stack.currentWidget() is library
        assert panel.search.text() == "cyber"
        assert library.selected_key() == "steam:1091500"
        assert window.focusWidget() is button and not button.peek.isVisible()

        # No ring and capture off: the entry explains instead of opening.
        panel.search.clear()
        library.select_game(2)
        assert button.property("perfState") == "empty"
        assert "Capture telemetry" in button.accessibleDescription()
        button.click()
        assert window.stack.currentWidget() is library
        panel.telemetry_switch.setChecked(True)
        assert window.perf_source.capture_enabled("steam:1888930")
        panel.telemetry_switch.setChecked(False)
        assert not window.perf_source.capture_enabled("steam:1888930")

        # Playing a wrapped game on a tier records a live ring and swaps in
        # that tier's profile for the session.
        library.select_game(0)
        key = library.selected_key()
        library.session_changed(key, True)
        record = window.perf_source.record(key)
        assert record is not None and record.live
        assert button.property("perfLive") is True
        runtime = window.profile_source.runtime()
        assert runtime.game_override and runtime.running_selector == (
            window.tier_profile_id("Balanced")
        )
        library.session_changed(key, False)
        archived = window.perf_source.record(key)
        assert archived is not None and not archived.live
        assert not window.profile_source.runtime().game_override
    finally:
        window.close()


def test_prototype_library_states(app):
    window = native_window()
    window.show()
    try:
        library, panel = window.library, window.library.panel
        window.tabs.setCurrentIndex(2)
        library.select_game(0)
        window.set_prototype("launcherWriteState", "not-set-up")
        assert panel.write_action_button.isVisibleTo(panel.widget)
        assert panel.write_action_button.text() == "Scan my Steam library"
        assert not panel.enable_switch.isEnabled()
        window.set_prototype("launcherWriteState", "needs-restart")
        assert panel.write_action_button.text() == "Restart Steam to finish"
        window.set_prototype("launcherWriteState", "ready")
        assert not panel.write_action_button.isVisibleTo(panel.widget)
        assert panel.enable_switch.isEnabled()
        window.set_prototype("emptyLibrary", True)
        assert not panel._games
        window.set_prototype("emptyLibrary", False)
        assert len(panel._games) == len(preview_games())
    finally:
        window.close()


def preview_games():
    from native_library import GAMES

    return GAMES


def test_gpu_index_flag_and_busy_import_refusal(app, monkeypatch):
    assert preview.gpu_index_arg(["preview.py", "--gpu-index", "1"]) == 1
    assert preview.gpu_index_arg(["preview.py", "--index", "0"]) == 0
    assert preview.gpu_index_arg(["preview.py", "--gpu-index"]) is None
    window = native_window()
    window.profiles.dialogs = dialogs = ScriptedDialogs()
    window.show()
    try:
        window.preselect_gpu(1)
        assert window.prototype.twoGpus and window.scan.gpu_index == 1
        assert window.profiles.target.index == 1
        assert window.scan.gpu_caption.text().startswith("GPU 1 · ")
        # Import refuses while a scan runs instead of opening the chooser.
        monkeypatch.setattr(window.scan, "busy", lambda: True)
        opened = []
        monkeypatch.setattr(
            preview.afterburner_import,
            "select_afterburner_import",
            lambda **kw: opened.append(kw),
        )
        window.import_afterburner()
        assert not opened
        assert dialogs.messages[-1][2].startswith("Finish the current PenguinBurner")
    finally:
        window.close()
