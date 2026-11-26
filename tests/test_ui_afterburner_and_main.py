"""Coverage for ui/afterburner_import.py helpers, ui/afterburner_workflow.py,
and ui/main.py run().
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from cli import runtime_config_file

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import importlib

import integrations.afterburner.profile_import as ab

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


def test_afterburner_import_reads_the_shared_runtime_gpu_index(tmp_path) -> None:
    # One implementation, owned by the runtime config module.
    assert ab.runtime_gpu_index is runtime_config_file.runtime_gpu_index
    config = tmp_path / "c.toml"
    config.write_text("[gpu]\nindex = 2\n", encoding="utf-8")
    assert ab.runtime_gpu_index(config) == 2
    assert ab.runtime_gpu_index(tmp_path / "absent.toml") == 0












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
        def __init__(self, kind, gpu_index):
            created["kind"] = kind
            created["gpu_index"] = gpu_index
            self.window = SimpleNamespace(setWindowIcon=lambda i: None)

        def setWindowIcon(self, _icon):
            pass

        def show(self):
            created["shown"] = True

    monkeypatch.setattr(
        ui_main, "_open_window", lambda app, gpu_index: _FakeWindow("new", gpu_index)
    )
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
    assert created["kind"] == "new"
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
