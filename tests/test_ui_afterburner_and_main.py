"""Coverage for ui/afterburner_import.py helpers, ui/afterburner_workflow.py,
and ui/main.py run().
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import importlib

import ui.features.integrations.afterburner_import as ab
import ui.features.integrations.afterburner_workflow as workflow_mod
from ui.features.integrations.afterburner_workflow import AfterburnerImportWorkflow
from ui.qt import import_qt

# `ui.main` the attribute is shadowed by the re-exported `main` function, so
# fetch the actual submodule object for monkeypatching.
ui_main = importlib.import_module("ui.main")


# --- afterburner_import pure helpers ------------------------------------------


def test_afterburner_profile_status_branches() -> None:
    assert ab.afterburner_profile_status({"is_valid_manual_candidate": True}) == ("Ready", True)
    label, ok = ab.afterburner_profile_status({"is_manual_candidate": False})
    assert ok is False and "same as Defaults" in label
    label, ok = ab.afterburner_profile_status(
        {"is_manual_candidate": True, "flatten_validation": {"reason": "too steep"}}
    )
    assert "too steep" in label
    assert ab.afterburner_profile_status({"is_manual_candidate": True})[1] is False


def test_afterburner_target_helpers() -> None:
    section = {"flatten_target": {"lock_clock_mhz": 2500, "lock_voltage_mv": 900}}
    assert ab.afterburner_profile_target_text(section) == "2500 MHz 900 mV"
    assert ab.afterburner_profile_target_pair(section) == (2500, 900)
    assert ab.afterburner_profile_target_value(section, "lock_clock_mhz") == 2500
    assert ab.afterburner_profile_target_pair({}) == (None, None)
    assert ab.afterburner_profile_target_value({}, "lock_clock_mhz") is None
    assert ab.afterburner_profile_target_text({}) == ""


def test_afterburner_curve_points() -> None:
    section = {"materialization": {"points": [{"voltage_mv": 900, "frequency_mhz": 2500}]}}
    assert ab.afterburner_section_curve_points(section) == [(900.0, 2500.0)]
    assert ab.afterburner_section_curve_points({"points": "bad"}) == []
    assert ab.entry_curve_points({"curve_points": [[900, 2500], "bad"]}) == [(900.0, 2500.0)]
    assert ab.entry_curve_points({}) == []


def test_relative_profile_path(tmp_path) -> None:
    root = tmp_path / "ab"
    (root / "Profiles").mkdir(parents=True)
    profile = root / "Profiles" / "p.cfg"
    profile.write_text("x", encoding="utf-8")
    assert ab.relative_profile_path(root, profile) == "Profiles/p.cfg"
    # Outside root -> just the name.
    assert ab.relative_profile_path(root, tmp_path / "other.cfg") == "other.cfg"


def test_runtime_gpu_index_and_mtime(tmp_path) -> None:
    # runtime_gpu_index is re-exported from gpu_selection (single shared impl).
    config = tmp_path / "c.toml"
    config.write_text("[gpu]\nindex = 2\n", encoding="utf-8")
    assert ab.runtime_gpu_index(config) == 2
    assert ab.runtime_gpu_index(tmp_path / "absent.toml") == 0


# --- afterburner workflow -----------------------------------------------------


def _workflow(monkeypatch, *, running=False):
    qtcore, qtgui, qtwidgets, pg = import_qt()
    monkeypatch.setattr(qtwidgets.QMessageBox, "information", lambda *a, **k: None)
    state = {"status": [], "loaded": 0}
    controls = SimpleNamespace(set_status_text=lambda t: state["status"].append(t))
    profile_list = SimpleNamespace(
        select_profile=lambda pid: None,
        table=SimpleNamespace(setFocus=lambda: None),
    )
    log_view = SimpleNamespace(append=lambda t: None)
    tabs = SimpleNamespace(setCurrentIndex=lambda i: None)
    errors: list = []
    wf = AfterburnerImportWorkflow(
        QtCore=qtcore, QtGui=qtgui, QtWidgets=qtwidgets, pg=pg, parent=None,
        tabs=tabs, profiles_tab_index=2, profile_list=profile_list, controls=controls,
        log_view=log_view, workflow_running=lambda: running,
        load_profiles=lambda: state.__setitem__("loaded", state["loaded"] + 1),
        show_error=lambda title, msg: errors.append(title),
    )
    return wf, state, errors


def test_workflow_blocked_when_running(monkeypatch) -> None:
    wf, state, _errors = _workflow(monkeypatch, running=True)
    wf.run()
    assert state["status"] and "Finish the current" in state["status"][0]


def test_workflow_cancelled(monkeypatch) -> None:
    wf, state, _errors = _workflow(monkeypatch)
    monkeypatch.setattr(workflow_mod, "select_afterburner_import", lambda **k: None)
    wf.run()
    assert state["loaded"] == 0


def test_workflow_dialog_error(monkeypatch) -> None:
    wf, _state, errors = _workflow(monkeypatch)

    def _boom(**k):
        raise RuntimeError("dialog broke")

    monkeypatch.setattr(workflow_mod, "select_afterburner_import", _boom)
    wf.run()
    assert errors == ["Import Afterburner"]


def test_workflow_success(monkeypatch) -> None:
    wf, state, _errors = _workflow(monkeypatch)
    monkeypatch.setattr(workflow_mod, "select_afterburner_import", lambda **k: {"section": "profile2"})
    monkeypatch.setattr(
        workflow_mod,
        "persist_afterburner_import_selection",
        lambda entry: {
            "section": "profile2",
            "device_profile_relative_path": "Profiles/p.cfg",
            "afterburner_root": "/ab",
            "profile_path": "/tmp/auto-uv-afterburner-profile2.json",
            "profile_id": "afterburner-profile2",
        },
    )
    wf.run()
    assert state["loaded"] == 1


# --- main.run -----------------------------------------------------------------


def test_main_run_parse_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ui_main, "parse_gui_launch_options", lambda argv: (_ for _ in ()).throw(ValueError("bad")))
    assert ui_main.run(["pburn", "--gpu-index", "x"]) == 2


def test_main_run_import_error(monkeypatch) -> None:
    def _boom():
        raise RuntimeError("no PySide6")

    monkeypatch.setattr(ui_main, "import_qt", _boom)
    assert ui_main.run(["pburn"]) == 1


def _patch_gui_run(monkeypatch) -> dict:
    """Stub every ui_main.run collaborator; returns the window-capture dict."""
    app = SimpleNamespace(
        setApplicationName=lambda n: None,
        setApplicationDisplayName=lambda n: None,
        setDesktopFileName=lambda n: None,
        setWindowIcon=lambda i: None,
        exec=lambda: 0,
    )
    fake_qtwidgets = SimpleNamespace(QApplication=lambda argv: app)
    fake_qtgui = SimpleNamespace()
    monkeypatch.setattr(ui_main, "import_qt", lambda: (object(), fake_qtgui, fake_qtwidgets, None))
    monkeypatch.setattr(ui_main, "prepare_desktop_scale_env", lambda: None)
    monkeypatch.setattr(ui_main, "apply_desktop_font_settings", lambda app, qtgui: None)
    monkeypatch.setattr(ui_main, "apply_dark_palette", lambda app, qtgui: None)
    monkeypatch.setattr(ui_main, "application_icon", lambda qtgui: SimpleNamespace(isNull=lambda: True))

    created = {}

    class _FakeWindow:
        def __init__(self, qt_modules, *, gpu_index=None):
            created["gpu_index"] = gpu_index
            self.window = SimpleNamespace(setWindowIcon=lambda i: None)

        def show(self):
            created["shown"] = True

    monkeypatch.setattr(ui_main, "MainWindow", _FakeWindow)
    return created


def test_main_run_launches_app(monkeypatch) -> None:
    created = _patch_gui_run(monkeypatch)
    integration_checks = []
    monkeypatch.setattr(
        ui_main,
        "ensure_host_integration",
        lambda: integration_checks.append(True),
    )

    assert ui_main.run(["pburn", "--gpu-index", "1"]) == 0
    assert created["shown"] is True
    assert created["gpu_index"] == 1
    assert integration_checks == [True]


def test_main_warns_but_starts_on_incomplete_flatpak_integration(
    monkeypatch, capsys
) -> None:
    created = _patch_gui_run(monkeypatch)
    monkeypatch.setattr(
        ui_main,
        "ensure_host_integration",
        lambda: (_ for _ in ()).throw(RuntimeError("missing shim")),
    )

    assert ui_main.run(["pburn"]) == 0
    assert created["shown"] is True
    error_output = capsys.readouterr().err
    assert "missing shim" in error_output
    assert "warning" in error_output
