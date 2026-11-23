"""The new PenguinBurner window (ui/shell_window.py): shell, Appearance
persistence and the classic-window placeholders."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tomllib

import pytest
from PySide6 import QtWidgets as W
from PySide6.QtTest import QTest

import ui.design.style as design
import ui.shell_window as shell
from cli import runtime_config_file
from ui.design.tokens import AppearanceSettings, resolve_colors
from ui.features.profiles.profiles_page import ProfilesPage


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[ui]\npersist_on_startup = true\n\n[fan]\nmode = "auto"\n')
    monkeypatch.setattr(runtime_config_file, "default_config_path", lambda: path)
    return path


@pytest.fixture
def window(isolated_theme, config, offline_profiles):
    qapp = isolated_theme
    opened = []
    design.install_theme(qapp, AppearanceSettings())
    win = shell.ShellWindow(open_classic=lambda: opened.append(True) or True)
    win.opened_classic = opened
    shell.CURRENT["window"] = win
    yield win
    current = shell.CURRENT["window"]
    if current is not None:
        current.close()
    shell.CURRENT["window"] = None


def test_appearance_round_trips_through_ui_config_and_keeps_other_settings(config):
    assert shell.load_appearance() == AppearanceSettings()
    shell.save_appearance(AppearanceSettings(theme="light", accent="#12AB34"))
    stored = tomllib.loads(config.read_text())
    assert stored["ui"]["appearance_theme"] == "light"
    assert stored["ui"]["appearance_accent"] == "#12AB34"
    assert stored["ui"]["persist_on_startup"] is True
    assert stored["fan"] == {"mode": "auto"}
    assert shell.load_appearance() == AppearanceSettings(
        theme="light", accent="#12AB34"
    )


def test_unknown_or_corrupt_appearance_falls_back_to_defaults(config):
    config.write_text('[ui]\nappearance_theme = "sepia"\nappearance_font = 3\n')
    assert shell.load_appearance() == AppearanceSettings()
    config.write_text("not toml [")
    assert shell.load_appearance() == AppearanceSettings()
    # A corrupt config is never rewritten with only [ui].
    with pytest.raises(Exception):  # noqa: B017
        shell.save_appearance(AppearanceSettings(theme="light"))
    assert config.read_text() == "not toml ["


def test_every_tab_offers_the_classic_window_until_its_page_lands(window):
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
        "Auto UV",
        "Profiles",
        "Game Library",
        "In-Game Overlay",
    ]
    for index, page in enumerate(window.pages):
        window.tabs.setCurrentIndex(index)
        assert window.stack.currentWidget() is page
        assert isinstance(page, ProfilesPage) == (index == 1)
        assert isinstance(page, shell.ClassicPlaceholder) == (index != 1)
    window.pages[0].open_button.click()
    assert window.opened_classic == [True]


def test_status_band_shows_only_what_a_page_reports(window):
    window.show()
    # Profiles always reports what runs now and at startup.
    assert window.status_running.full_text() == (
        "No running/autostart profile available yet."
    )
    window.set_global_status(None, "profiles")
    assert not window.status_band.isVisible()
    window.set_global_status({"running": "Running Balanced", "detail": "GPU 0"}, "profiles")
    assert window.status_band.isVisible()
    assert window.status_running.full_text() == "Running Balanced"
    window.set_global_status(None, "profiles")
    assert not window.status_band.isVisible()


def test_apply_appearance_saves_and_rebuilds_on_the_same_tab(window, config):
    window.show()
    window.tabs.setCurrentIndex(2)
    window.apply_appearance(AppearanceSettings(theme="light", accent="violet"))
    QTest.qWait(20)
    rebuilt = shell.CURRENT["window"]
    assert rebuilt is not window
    assert rebuilt.tabs.currentIndex() == 2
    assert design.COL["accent"] == resolve_colors("light", "violet")["accent"]
    assert tomllib.loads(config.read_text())["ui"]["appearance_accent"] == "violet"


def test_a_failed_save_keeps_the_window_and_theme(window, config, monkeypatch):
    warnings = []
    monkeypatch.setattr(
        W.QMessageBox, "warning", lambda *args: warnings.append(args[2])
    )
    config.write_text("not toml [")
    window.apply_appearance(AppearanceSettings(theme="light"))
    assert shell.CURRENT["window"] is window
    assert design.MODE["current"] == "dark"
    assert "Could not save" in warnings[0]


def test_startup_checks_run_against_the_live_window(window, monkeypatch):
    calls = []
    monkeypatch.setattr(shell, "run_startup_checks", lambda **kwargs: calls.append(kwargs))
    shell.startup_checks(window)
    assert calls[0]["parent"] is window
    shell.startup_checks(None)
    assert len(calls) == 1
