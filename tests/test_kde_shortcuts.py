from __future__ import annotations

import shlex
import site
from configparser import ConfigParser
from pathlib import Path

import pytest
from PySide6 import QtCore, QtWidgets

from overlay import kde_shortcuts
from overlay.kde_shortcuts import (
    DESKTOP_ID,
    pin_exec_commands,
    repair_user_desktop_entry,
)
from ui.components import overlay_config

PACKAGED_ENTRY = Path("packaging/linux") / DESKTOP_ID


def _exec_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("Exec=")]


def _user_install(*, commands=("penguin-burner", "penguin-burner-cli")) -> Path:
    base = Path(site.getuserbase())
    entry = base / "share" / "applications" / DESKTOP_ID
    entry.parent.mkdir(parents=True)
    entry.write_text(PACKAGED_ENTRY.read_text(encoding="utf-8"), encoding="utf-8")
    (base / "bin").mkdir()
    for name in commands:
        (base / "bin" / name).write_text("#!/bin/sh\n", encoding="utf-8")
    return entry


def test_pin_keeps_every_action_argument_and_only_changes_the_program():
    text = PACKAGED_ENTRY.read_text(encoding="utf-8")
    bin_dir = Path("/home/user/.local/bin")
    pinned = pin_exec_commands(
        text, {name: bin_dir / name for name in ("penguin-burner", "penguin-burner-cli")}
    )

    before, after = _exec_lines(text), _exec_lines(pinned)
    assert len(after) == len(before) > 2
    for old, new in zip(before, after, strict=True):
        old_argv = shlex.split(old.removeprefix("Exec="))
        new_argv = shlex.split(new.removeprefix("Exec="))
        assert new_argv[0] == str(bin_dir / old_argv[0])
        assert new_argv[1:] == old_argv[1:]
    assert [line for line in pinned.splitlines() if not line.startswith("Exec=")] == [
        line for line in text.splitlines() if not line.startswith("Exec=")
    ]


def test_pin_quotes_paths_the_desktop_spec_reserves():
    text = "Exec=penguin-burner-cli --overlay-graph-next\n"
    pinned = pin_exec_commands(text, {"penguin-burner-cli": Path('/home/a b/100%/"x"/cli')})

    assert pinned == 'Exec="/home/a b/100%%/\\"x\\"/cli" --overlay-graph-next\n'


def test_pin_leaves_absolute_and_unknown_programs_alone():
    text = "Exec=/usr/bin/penguin-burner-cli --overlay-toggle\nExec=flatpak run x\n"

    assert pin_exec_commands(text, {"penguin-burner-cli": Path("/x/cli")}) == text


def test_repair_pins_the_user_entry_once():
    entry = _user_install()
    bin_dir = Path(site.getuserbase()) / "bin"

    assert repair_user_desktop_entry() == entry
    lines = _exec_lines(entry.read_text(encoding="utf-8"))
    assert lines[0] == f"Exec={bin_dir / 'penguin-burner'}"
    assert all(line.startswith(f"Exec={bin_dir}/") for line in lines)
    parser = ConfigParser(interpolation=None)
    parser.read(entry)
    assert parser["Desktop Action NextOverlayGraph"]["Exec"] == (
        f"{bin_dir / 'penguin-burner-cli'} --overlay-graph-next"
    )
    assert repair_user_desktop_entry() is None


def test_repair_leaves_commands_without_a_user_script_bare():
    entry = _user_install(commands=("penguin-burner",))

    assert repair_user_desktop_entry() == entry
    lines = _exec_lines(entry.read_text(encoding="utf-8"))
    assert lines[0].startswith(f"Exec={Path(site.getuserbase()) / 'bin'}/")
    assert all(line.startswith("Exec=penguin-burner-cli ") for line in lines[1:])


def test_repair_without_a_user_install_writes_nothing():
    assert repair_user_desktop_entry() is None
    assert not Path(site.getuserbase()).exists()


def test_repair_skips_flatpak(monkeypatch):
    entry = _user_install()
    original = entry.read_text(encoding="utf-8")
    monkeypatch.setattr(kde_shortcuts, "running_in_flatpak", lambda: True)

    assert repair_user_desktop_entry() is None
    assert entry.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("desktop", ["GNOME", "sway"])
def test_only_the_kde_button_repairs_the_entry(qtbot, monkeypatch, tmp_path, desktop):
    entry = _user_install()
    original = entry.read_text(encoding="utf-8")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)
    panel = overlay_config.OverlayConfigPanel(
        QtCore=QtCore, QtWidgets=QtWidgets, config_path=tmp_path / "overlay.toml"
    )
    qtbot.addWidget(panel.widget)
    panel.shortcuts_button.click()

    assert entry.read_text(encoding="utf-8") == original


def test_kde_button_repairs_the_entry_before_opening_settings(qtbot, monkeypatch, tmp_path):
    entry = _user_install()
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    pinned_when_launched = []
    monkeypatch.setattr(
        overlay_config,
        "start_on_host",
        lambda _command: pinned_when_launched.append(
            "Exec=penguin-burner-cli" not in entry.read_text(encoding="utf-8")
        ) or True,
    )
    panel = overlay_config.OverlayConfigPanel(
        QtCore=QtCore, QtWidgets=QtWidgets, config_path=tmp_path / "overlay.toml"
    )
    qtbot.addWidget(panel.widget)
    panel.shortcuts_button.click()

    assert pinned_when_launched == [True]


def test_kde_button_reports_a_failed_repair(qtbot, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    launched = []
    monkeypatch.setattr(overlay_config, "start_on_host", lambda command: launched.append(command))

    def fail():
        raise PermissionError("read-only")

    monkeypatch.setattr(overlay_config, "repair_user_desktop_entry", fail)
    panel = overlay_config.OverlayConfigPanel(
        QtCore=QtCore, QtWidgets=QtWidgets, config_path=tmp_path / "overlay.toml"
    )
    qtbot.addWidget(panel.widget)
    panel.shortcuts_button.click()

    dialog = panel.widget.findChild(QtWidgets.QDialog, "overlayShortcutsDialog")
    error = dialog.findChild(QtWidgets.QLabel, "overlayShortcutError")
    assert "Could not update the PenguinBurner shortcut commands: read-only" in error.text()
    assert not launched
