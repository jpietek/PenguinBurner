"""The new window's Auto UV page (ui/features/tuning/auto_uv_page.py).

Nothing here starts a process, talks to the root service or touches the
GPU: the scan and verification controllers are stubbed where they would
spawn, and recorded scan events (tests/fixtures/auto_uv_scan) drive the page
through the same ScanSession the classic window uses.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import shiboken6
from PySide6.QtTest import QTest

import ui.design.style as design
from ui.components.scan_runs_table import DELTA_TONE_ROLE, RESULT_ROLE
from ui.design.tokens import AppearanceSettings

FIXTURES = Path(__file__).parent / "fixtures" / "auto_uv_scan"
FULL_SCAN_OPTIONS = {
    "gpu_index": 0,
    "auto_uv_mode": "adaptive",
    "auto_uv_efficiency_memory_offset_mhz": 1000,
    "auto_uv_efficiency_power_limit_w": 268,
}


def events(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]


class Spy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def __call__(self, name, result=None):
        def record(*args, **kwargs):
            self.calls.append((name, kwargs or args))
            return result

        return record

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def kwargs(self, name: str):
        return next(kwargs for call, kwargs in self.calls if call == name)


@pytest.fixture
def page_env(isolated_theme, offline_auto_uv, monkeypatch):
    """A page over a fresh engine whose controllers never spawn."""
    page_mod = offline_auto_uv
    design.install_theme(isolated_theme, AppearanceSettings())
    spy = Spy()
    monkeypatch.setattr(
        page_mod, "ensure_daemon_ready_for_privileged_action", spy("daemon", True)
    )
    monkeypatch.setattr(page_mod, "select_scan_tuning", spy("setup", dict(FULL_SCAN_OPTIONS)))
    monkeypatch.setattr(page_mod, "persist_runtime_gpu_index", lambda index: int(index))
    monkeypatch.setattr(
        page_mod,
        "scan_command",
        lambda options: spy.calls.append(("scan_command", dict(options)))
        or ["penguin-burner", "scan"],
    )
    monkeypatch.setattr(page_mod, "penguin_burner_runtime_is_active", lambda: True)
    monkeypatch.setattr(
        page_mod,
        "running_auto_uv_profile_info",
        lambda: {"selector": "p1", "silent_fan_curve": True},
    )
    monkeypatch.setattr(page_mod, "show_process_failure", spy("failure"))
    monkeypatch.setattr(page_mod, "show_error_dialog", spy("error"))
    page = page_mod.AutoUvPage(gpu_index=0)
    engine = page.engine
    scan = engine.scan_controller
    monkeypatch.setattr(
        scan,
        "start",
        lambda command: spy.calls.append(("scan_start", list(command)))
        or setattr(scan, "process", object())
        or True,
    )
    monkeypatch.setattr(scan, "stop", spy("scan_stop"))
    verify = engine.verify_controller
    monkeypatch.setattr(
        verify,
        "start",
        lambda command, *, duration_s: spy.calls.append(
            ("verify_start", (list(command), duration_s))
        )
        or setattr(verify, "process", object())
        or True,
    )
    monkeypatch.setattr(verify, "stop", spy("verify_stop"))
    signals: dict[str, list] = {}
    for name in (
        "show_scan",
        "show_profiles",
        "status_changed",
        "progress_changed",
        "busy_changed",
        "profile_saved",
        "scan_finished",
        "scan_completed",
        "profile_verified",
    ):
        getattr(page, name).connect(
            lambda *args, name=name: signals.setdefault(name, []).append(args)
        )
    page.resize(1300, 860)
    yield page_mod, page, spy, signals
    if shiboken6.isValid(page):
        page.close()


def exit_scan(page, code=0, stopped=False):
    page.engine.scan_controller.process = None
    page.engine._scan_finished(code, 0, stopped)


def feed(page, name: str, limit: int | None = None) -> None:
    for payload in events(name)[:limit]:
        page.engine._scan_event(payload)


def test_setup_runs_the_classic_gates_and_starts_the_daemon_scan(page_env):
    _page_mod, page, spy, signals = page_env

    page.setup_button.click()

    assert spy.names()[:2] == ["daemon", "setup"]
    daemon = spy.kwargs("daemon")
    assert daemon["action_label"] == "Setting up Auto-UV"
    assert daemon["required_capabilities"] == ("scan-stream-v1",)
    assert spy.kwargs("setup")["gpu_index"] == 0
    assert spy.kwargs("scan_command") == FULL_SCAN_OPTIONS
    assert ("scan_start", ["penguin-burner", "scan"]) in spy.calls
    assert page.engine.log_lines[-1].endswith("$ penguin-burner scan")
    # The live running profile's silent fan curve is the pre-scan intent.
    assert page.engine.pre_scan_silent_fan is True
    assert page.session.stage == "Starting"
    assert page.session.candidate == "Writing to main Auto-UV profile store"
    assert page.session.status == "Memory offset: +500 MHz memory clock"
    assert page.tier_strip.isVisibleTo(page)
    assert signals["show_scan"] and signals["busy_changed"][-1] == (True,)
    assert signals["status_changed"][-1][0]["running"] == "Auto UV · Starting"
    assert not page.setup_button.isEnabled() and page.stop_button.isEnabled()


@pytest.mark.parametrize("cancelled", ["daemon", "setup"])
def test_setup_stops_when_the_service_or_dialog_is_declined(page_env, monkeypatch, cancelled):
    page_mod, page, spy, _signals = page_env
    if cancelled == "daemon":
        monkeypatch.setattr(page_mod, "ensure_daemon_ready_for_privileged_action", spy("daemon", False))
    else:
        monkeypatch.setattr(page_mod, "select_scan_tuning", spy("setup", None))

    page.setup()

    assert "scan_start" not in spy.names()
    assert ("setup" in spy.names()) is (cancelled == "setup")
    assert not page.busy()


def test_a_busy_page_or_another_pages_command_blocks_setup(page_env):
    _page_mod, page, spy, _signals = page_env
    page.set_other_busy(True)
    page.setup()
    assert spy.names() == [] and not page.setup_button.isEnabled()
    page.set_other_busy(False)
    page.setup()
    page.setup()
    assert spy.names().count("setup") == 1


def test_gpu_index_save_failure_is_reported_and_nothing_starts(page_env, monkeypatch):
    page_mod, page, spy, _signals = page_env

    def refuse(_index):
        raise OSError("read-only config")

    monkeypatch.setattr(page_mod, "persist_runtime_gpu_index", refuse)
    page.setup()

    assert "scan_start" not in spy.names()
    assert "read-only config" in spy.kwargs("error")["message"]


def test_recorded_full_scan_renders_rows_tiers_and_completes(page_env):
    _page_mod, page, _spy, signals = page_env
    page.setup()

    feed(page, "issue109-launch3-resume-complete.jsonl")

    session = page.session
    assert page.table.rowCount() == len(session.runs.rows) > 20
    assert page.tier_strip.states == dict.fromkeys(
        ("Efficiency", "Balanced", "Performance"), "complete"
    )
    restored = [run for run in session.runs.rows if run.restored]
    assert restored and page.table.item(0, 11).text() == "Pass"
    last = len(session.runs.rows) - 1
    assert page.table.item(last, 1).text() == "987"
    assert page.table.item(last, 12).text().startswith("Final · ")
    assert page.table.item(last, 11).data(RESULT_ROLE) == "gain"
    # Δ stock follows the classic window's colour thresholds.
    assert page.table.item(last, 6).data(DELTA_TONE_ROLE) == "gain"
    saved = signals["profile_saved"][-1][0]
    assert saved["candidate_id"] == "987mv-1920mhz" and saved["gpu_index"] == 0

    exit_scan(page, 0)

    assert session.stage == "Complete"
    assert page.stage_label.text() == "Complete"
    assert signals["scan_finished"][-1][0]["outcome"] == "complete"
    assert signals["show_profiles"]
    assert signals["scan_completed"][-1][0] == {
        "outcome": "complete",
        "candidate_id": "987mv-1920mhz",
        "gpu_index": 0,
        "silent_fan": True,
    }
    assert signals["busy_changed"][-1] == (False,)
    assert signals["status_changed"][-1] == (None,)
    assert page.setup_button.isEnabled() and not page.stop_button.isEnabled()


def test_a_crashed_scan_shows_the_error_with_the_log_tail(page_env):
    _page_mod, page, spy, signals = page_env
    page.setup()
    feed(page, "issue109-launch1-efficiency-crash.jsonl")
    running = len(page.session.runs.rows) - 1
    assert page.table.item(running, 11).text() == "Running"

    exit_scan(page, 1)

    failure = spy.kwargs("failure")
    assert failure["title"] == "Auto-UV failed" and failure["exit_code"] == 1
    assert "Auto-UV process stopped: exit_code=1" in failure["log_text"]
    assert page.table.item(running, 11).text() == "Failed"
    assert page.tier_strip.states["Efficiency"] == "failed"
    assert page.tier_strip.states["Performance"] == "not-run"
    assert "scan_completed" not in signals


def test_stop_asks_the_scan_to_stop_and_ends_stopped(page_env):
    _page_mod, page, spy, signals = page_env
    page.setup()
    feed(page, "issue109-launch1-efficiency-crash.jsonl")

    page.stop_button.click()

    assert "scan_stop" in spy.names()
    assert page.session.stage == "Stopping"
    running = len(page.session.runs.rows) - 1
    assert page.table.item(running, 11).text() == "Stopping"
    assert not page.stop_button.isEnabled()
    exit_scan(page, 130, stopped=True)
    assert page.session.stage == "Stopped"
    assert page.table.item(running, 11).text() == "Stopped"
    assert "failure" not in spy.names() and "scan_completed" not in signals


def test_final_choice_answers_the_scan_atomically(page_env, monkeypatch, tmp_path):
    page_mod, page, _spy, _signals = page_env
    response = tmp_path / "choice.json"
    seen = {}

    def choose(**kwargs):
        seen.update(kwargs)
        return kwargs["candidates"][1], 420, "select"

    monkeypatch.setattr(page_mod, "select_final_candidate", choose)
    page.setup()
    page.engine._scan_event(
        {
            "event": "final_choice_request",
            "auto_uv_mode": "balanced",
            "request_reason": "sweep-complete",
            "default_candidate_id": "900mv-2700mhz",
            "final_verification_duration_s": 180,
            "response_path": str(response),
            "candidates": [
                {"candidate_id": "900mv-2700mhz"},
                {"candidate_id": "890mv-2700mhz"},
            ],
        }
    )

    assert seen["parent"] is page and seen["default_duration_s"] == 180
    assert json.loads(response.read_text()) == {
        "candidate_id": "890mv-2700mhz",
        "final_verification_duration_s": 420,
    }
    assert page.session.last_candidate_id == "890mv-2700mhz"
    assert not page.blocks_rebuild()


def test_aborted_recovery_is_not_reported_as_a_failure(page_env, monkeypatch, tmp_path):
    page_mod, page, spy, signals = page_env
    response = tmp_path / "choice.json"
    monkeypatch.setattr(
        page_mod, "select_final_candidate", lambda **_: (None, 600, "abort")
    )
    page.setup()
    page.engine._scan_event(
        {
            "event": "final_choice_request",
            "request_reason": "previous-crash",
            "response_path": str(response),
            "candidates": [{"candidate_id": "885mv-2873mhz"}],
        }
    )
    assert json.loads(response.read_text()) == {"action": "abort"}

    exit_scan(page, 1)

    assert page.session.stage == "Aborted"
    assert "failure" not in spy.names() and "scan_completed" not in signals


def test_dependency_download_drives_the_header_progress(page_env):
    _page_mod, page, _spy, signals = page_env
    page.setup()
    page.engine._scan_event(
        {"event": "dependency_progress", "percent": 40, "detail": "Q2RTX 216 / 540 MB"}
    )
    assert signals["progress_changed"][-1] == (
        "Downloading dependencies 40%",
        40,
        "Q2RTX 216 / 540 MB",
    )
    page.engine._scan_event({"event": "base_curve", "points": []})
    assert signals["progress_changed"][-1] == (None, 0, "")


PROFILE = {
    "profile_id": "20261025-910mv-2955mhz",
    "candidate_id": "910mv-2955mhz",
    "path": "/tmp/auto-uv-profile-910mv-2955mhz.json",
}


def test_profile_verification_runs_the_verify_command_with_header_progress(
    page_env, monkeypatch
):
    page_mod, page, spy, signals = page_env
    monkeypatch.setattr(page_mod, "profile_verify_selector", lambda profile: profile["path"])
    monkeypatch.setattr(
        page_mod,
        "profile_verify_command",
        lambda **kwargs: spy.calls.append(("verify_command", kwargs)) or ["pb", "verify"],
    )
    monkeypatch.setattr(page_mod, "profile_base_curve_points", lambda _p: [(900, 2400)])
    monkeypatch.setattr(page_mod, "profile_curve_plan", lambda _p: [])

    assert page.start_profile_verification(
        PROFILE, label="Balanced · 910 mV", duration_s=300, gpu_index=1
    )

    command = spy.kwargs("verify_command")
    assert command["profile_selector"] == PROFILE["path"]
    assert command["duration_s"] == 300 and command["gpu_index"] == 1
    assert str(command["stop_request_path"]).endswith("profile-verify-stop-requested")
    assert ("verify_start", (["pb", "verify"], 300)) in spy.calls
    assert page.session.stage == "Profile verification"
    assert signals["show_scan"]
    assert signals["status_changed"][-1][0]["running"] == "Profile verification"
    assert signals["progress_changed"][-1][0] == "Verifying profile 0s / 5min"
    # One run at a time.
    assert not page.start_profile_verification(
        PROFILE, label="again", duration_s=60, gpu_index=0
    )
    page.engine._verify_progress(50, elapsed_s=150, target_s=300, detail="elapsed=150s")
    assert signals["progress_changed"][-1][:2] == ("Verifying profile 2min 30s / 5min", 50)

    page.engine.verify_controller.process = None
    page.engine._verify_finished(0, 0, False)

    assert signals["profile_verified"][-1] == ("20261025-910mv-2955mhz", True, False)
    assert page.session.status == "Profile verification complete."
    assert signals["progress_changed"][-1][:2] == ("Verifying profile 5min / 5min", 100)
    assert "failure" not in spy.names()
    page.hide_finished_progress()
    assert signals["progress_changed"][-1] == (None, 0, "")


@pytest.mark.parametrize(("stopped", "error"), [(True, False), (False, True)])
def test_profile_verification_failures(page_env, monkeypatch, stopped, error):
    page_mod, page, spy, signals = page_env
    monkeypatch.setattr(page_mod, "profile_verify_selector", lambda profile: "p")
    monkeypatch.setattr(page_mod, "profile_verify_command", lambda **_: ["pb"])
    page.start_profile_verification(PROFILE, label="P", duration_s=60, gpu_index=0)
    if stopped:
        page.stop_button.click()
        assert "verify_stop" in spy.names()
        assert page.session.status == "Stopping profile verification."
    page.engine.verify_controller.process = None
    page.engine._verify_finished(1, 0, stopped)

    assert signals["profile_verified"][-1] == (PROFILE["profile_id"], False, stopped)
    assert ("failure" in spy.names()) is error
    assert page.session.stage == ("Idle" if stopped else "Error")
    assert signals["progress_changed"][-1] == (None, 0, "")


def test_a_rebuilt_page_keeps_the_running_scan(page_env):
    page_mod, page, _spy, _signals = page_env
    page.setup()
    feed(page, "issue109-launch1-efficiency-crash.jsonl")
    rows, log = page.table.rowCount(), list(page.log_lines)

    rebuilt = page_mod.AutoUvPage(gpu_index=0)
    page.deleteLater()
    QTest.qWait(10)

    assert rebuilt.engine is page_mod.ENGINE["engine"] and rebuilt.engine.page is rebuilt
    assert rebuilt.table.rowCount() == rows
    assert rebuilt.log_lines == log
    assert rebuilt.busy() and rebuilt.stop_button.isEnabled()
    assert rebuilt.tier_strip.states["Efficiency"] == "active"
    rebuilt.close()


def test_shell_hosts_the_page_with_its_header_actions_and_status(
    isolated_theme, offline_auto_uv, monkeypatch, tmp_path, dispose_window
):
    import ui.shell_window as shell
    from cli import runtime_config_file

    monkeypatch.setattr(runtime_config_file, "default_config_path", lambda: tmp_path / "c.toml")
    design.install_theme(isolated_theme, AppearanceSettings())
    window = shell.ShellWindow()
    page = window.pages[0]
    layout = window.header_layout
    actions = [layout.itemAt(i).widget() for i in range(layout.count())]
    assert actions.index(page.setup_button) < actions.index(page.import_button)
    assert actions.index(page.import_button) < actions.index(window.header_divider)
    window.show()
    assert not page.stop_button.isVisible(), "Stop only while there is something to stop"

    monkeypatch.setattr(page.engine.scan_controller, "is_running", lambda: True)
    page.session.start({"auto_uv_mode": "balanced"})
    page.engine._scan_event({"event": "dependency_progress", "percent": 25, "detail": "x"})
    # On the Auto UV tab the card shows the scan; the window band stays out.
    assert not window.status_band.isVisible()
    assert window.header_progress.isVisible()
    assert window.header_progress.format() == "Downloading dependencies 25%"
    assert page.stop_button.isVisible()

    window.tabs.setCurrentIndex(2)
    assert window.status_band.isVisible()
    assert window.status_running.full_text() == "Auto UV · Downloading dependencies"
    assert page.setup_button.property("headerRole") == "secondary"
    page.show_scan.emit()
    assert window.tabs.currentIndex() == 0
    page.show_profiles.emit()
    assert window.tabs.currentIndex() == 1
    dispose_window(window)


def test_a_long_live_line_never_widens_the_page(page_env, monkeypatch):
    _page_mod, page, _spy, _signals = page_env
    monkeypatch.setattr(page.engine.scan_controller, "is_running", lambda: True)
    page.session.start({"auto_uv_mode": "balanced"})
    page.refresh_view()
    before = page.minimumSizeHint().width()
    page.session.candidate = "Auto-UV phase=candidate-live " + "x=1 " * 400
    page.refresh_view()
    assert page.minimumSizeHint().width() <= before
    assert page.candidate_label.full_text().startswith("Auto-UV phase=")


def test_the_live_candidate_takes_the_scanned_tier_colour(page_env, monkeypatch):
    from ui.design.style import COL, TIER_COLORS

    _page_mod, page, _spy, _signals = page_env
    monkeypatch.setattr(page.engine.scan_controller, "is_running", lambda: True)
    page.session.start({"auto_uv_mode": "balanced"})
    page.session.tier_states = {"balanced": "active"}
    page.refresh_view()
    assert page.plot.candidate_tier_color == TIER_COLORS["Balanced"]
    assert page.plot._candidate_curve_pen().color().name() == TIER_COLORS["Balanced"].lower()
    monkeypatch.setattr(page.engine.scan_controller, "is_running", lambda: False)
    page.refresh_view()
    assert page.plot.candidate_tier_color is None
    assert page.plot._candidate_curve_pen().color().name() == COL["candidateCurve"].lower()


def test_a_long_candidate_list_keeps_the_choice_dialog_on_screen(page_env, monkeypatch):
    from PySide6 import QtCore as C
    from PySide6 import QtGui as G
    from PySide6 import QtWidgets as W

    from ui.dialogs.final_choice import select_final_candidate
    from ui.features.tuning.auto_uv_view import fit_choice_dialog

    candidates = [
        {
            "candidate_id": f"{mv}mv-2685mhz",
            "candidate_voltage_mv": mv,
            "lock_clock_mhz": 2685,
            "avg_fps": 58.0,
            "efficiency_fps_per_w": 0.21,
        }
        for mv in range(800, 1200, 10)
    ]
    seen = {}

    def exec_(dialog):
        dialog.show()
        monkeypatch.setattr(W.QApplication, "activeModalWidget", lambda: dialog)
        fit_choice_dialog()
        W.QApplication.processEvents()
        screen = dialog.screen().availableGeometry()
        table = dialog.findChild(W.QTableView)
        below = [
            w for w in dialog.findChildren(W.QWidget)
            if w.isVisible() and isinstance(w, (W.QSpinBox, W.QPushButton))
            and w.mapTo(dialog, C.QPoint(0, 0)).y() > table.mapTo(dialog, C.QPoint(0, 0)).y()
        ]
        table_bottom = table.mapTo(dialog, C.QPoint(0, table.height())).y()
        seen.update(
            height=dialog.height(),
            screen=screen.height(),
            scrolls=table.verticalScrollBar().maximum() > 0,
            overlap=[w for w in below if w.mapTo(dialog, C.QPoint(0, 0)).y() < table_bottom],
            clipped=[
                w for w in below
                if w.mapTo(dialog, C.QPoint(0, w.height())).y() > dialog.height()
            ],
        )
        return 0

    monkeypatch.setattr(W.QDialog, "exec", exec_)
    select_final_candidate(
        QtCore=C, QtGui=G, QtWidgets=W, parent=None,
        candidates=candidates, default_candidate_id="900mv-2685mhz",
        auto_uv_mode="efficiency",
    )
    assert seen["height"] <= seen["screen"] * 0.9 + 1
    assert seen["scrolls"], "rows that do not fit scroll inside the table"
    assert not seen["overlap"] and not seen["clipped"]


def test_live_probe_lines_read_as_one_summary():
    from auto_uv.session.scan_events import top_status_text

    assert top_status_text(
        "Auto-UV phase=final-verify-live candidate=860mV target=2800MHz elapsed=71.40s "
        "running=q2rtx live=860.00mV power=263.30W load=busy core_clock=2745MHz temp=62C fan=40%"
    ) == (
        "Final verification · 860 mV @ 2800 MHz · 1:11 · Q2RTX · 263 W · 2745 MHz · 62 °C · fan 40%"
    )
    assert top_status_text(
        "Auto-UV phase=efficiency-candidate-live candidate=885mV target=2760MHz "
        "elapsed=0.0s phase=startup running=cuda live=885.0mV power=37.2W"
    ) == "Testing · starting · 885 mV @ 2760 MHz · 0:00 · CUDA · 37 W"
    # Other lines keep their text.
    assert top_status_text("Auto-UV final state: 2760MHz@870mV") == "Auto-UV final state: 2760MHz@870mV"


def test_a_tier_that_repeats_another_is_dashed_over_it(page_env):
    from PySide6 import QtCore as C

    _page_mod, page, _spy, _signals = page_env
    curve = [(850.0, 2700.0), (860.0, 2800.0), (900.0, 2800.0)]
    page.show_tier_curve("balanced", curve)
    page.show_tier_curve("performance", curve)
    balanced, performance = page.tier_traces["Balanced"], page.tier_traces["Performance"]
    assert balanced.opts["pen"].style() == C.Qt.PenStyle.SolidLine
    assert performance.opts["pen"].style() == C.Qt.PenStyle.CustomDashLine
    assert performance.zValue() > balanced.zValue()
    assert page.shared_tier("Performance") == "Balanced"
    # The live candidate on the same curve is dashed too.
    page.plot.set_candidate_points(curve, curve_id="c1")
    page.sync_shared_curves()
    assert page.plot.candidate_dashed
    page.plot.set_candidate_points([(850.0, 2600.0), (900.0, 2650.0)], curve_id="c2")
    page.sync_shared_curves()
    assert not page.plot.candidate_dashed


def test_the_empty_runs_card_stays_compact_until_a_scan_runs(page_env, monkeypatch):
    _page_mod, page, _spy, _signals = page_env
    page.refresh_view()
    assert 200 < page.runs_card.maximumHeight() < 320  # idle: about five rows
    monkeypatch.setattr(page.engine.scan_controller, "is_running", lambda: True)
    page.session.start({"auto_uv_mode": "balanced"})
    page.refresh_view()
    assert page.runs_card.maximumHeight() == 16777215
