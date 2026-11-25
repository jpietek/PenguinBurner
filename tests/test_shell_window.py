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
from ui.features.overlay.overlay_page import OverlayPage
from ui.features.profiles.profiles_page import ProfilesPage
from ui.features.tuning.auto_uv_page import AutoUvPage


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[ui]\npersist_on_startup = true\n\n[fan]\nmode = "auto"\n')
    monkeypatch.setattr(runtime_config_file, "default_config_path", lambda: path)
    return path


@pytest.fixture
def window(
    isolated_theme, config, offline_profiles, offline_auto_uv, monkeypatch, tmp_path, dispose_window
):
    qapp = isolated_theme
    # Game Library reads no host launcher, daemon or GPU here.
    from functools import partial

    from ui.features.library import library_tab

    monkeypatch.setenv("PENGUIN_BURNER_DAEMON_SOCKET", str(tmp_path / "no-daemon.sock"))
    monkeypatch.setattr(
        library_tab,
        "GameLibraryTab",
        partial(library_tab.GameLibraryTab, sources=(), gpu_choices=tuple),
    )
    design.install_theme(qapp, AppearanceSettings())
    win = shell.ShellWindow()
    shell.CURRENT["window"] = win
    yield win
    current = shell.CURRENT["window"]
    if current is not None:
        dispose_window(current)
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


def test_every_tab_has_its_page(window):
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
        "Auto UV",
        "Profiles",
        "Game Library",
        "In-Game Overlay",
    ]
    assert isinstance(window.pages[0], AutoUvPage)
    for index, page in enumerate(window.pages):
        window.tabs.setCurrentIndex(index)
        assert window.stack.currentWidget() is page
        if index == shell.LIBRARY_TAB:
            # The fixture builds it through a partial, so check by name.
            assert type(page).__name__ == "GameLibraryTab"
            assert page is window.library_tab
        elif index == shell.OVERLAY_PAGE:
            assert isinstance(page, OverlayPage)
        elif index == 1:
            assert isinstance(page, ProfilesPage)
        else:
            assert isinstance(page, AutoUvPage)


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


def test_library_reports_to_the_window_status(window):
    window.show()
    window.library_tab.library.status_changed.emit(
        {"running": "Playing Portal 2", "detail": "Balanced · via Steam"}
    )
    assert window.status_sources["library"]["running"] == "Playing Portal 2"
    assert "Playing Portal 2" in window.status_running.full_text()
    window.library_tab.library.status_changed.emit(None)
    assert window.status_sources["library"] is None


def test_refresh_pages_reloads_the_library(window, monkeypatch):
    refreshed = []
    monkeypatch.setattr(window.library_tab, "refresh", lambda: refreshed.append(True))
    window.refresh_pages()
    assert refreshed == [True]


def test_appearance_waits_for_a_running_action(window, config):
    """A rebuild deletes the pages, so it never runs under an Apply,
    verification or scan; it happens once they finish."""
    window.show()
    running = [True]
    window.busy_checks.append(lambda: running[0])
    window.apply_appearance(AppearanceSettings(theme="light"))
    assert shell.CURRENT["window"] is window
    assert window.status_running.full_text() == "Appearance saved"
    assert tomllib.loads(config.read_text())["ui"]["appearance_theme"] == "light"
    running[0] = False
    QTest.qWait(1200)
    assert shell.CURRENT["window"] is not window
    assert design.MODE["current"] == "light"


def test_pages_are_wired_together(window, monkeypatch):
    games, summaries = [], []
    monkeypatch.setattr(
        window.library_tab.library, "set_profile_summaries", summaries.append
    )
    monkeypatch.setattr(window.overlay_page.panel, "set_game", games.append)
    window.library_tab.library.selected_game_changed.emit("game")
    window.profiles_tab.page.adaptive_tiers_changed.emit(["Balanced"])
    assert games == ["game"]
    assert summaries == [None]


def test_profile_verification_runs_on_the_auto_uv_page(window, monkeypatch):
    page, scan = window.profiles_tab.page, window.auto_uv_page
    profile = {"profile_id": "bal", "candidate_id": "c1"}
    monkeypatch.setattr(page, "profile", lambda pid: profile if pid == "bal" else None)
    started = []
    monkeypatch.setattr(
        scan, "start_profile_verification", lambda p, **kw: started.append((p, kw)) or True
    )
    page.verify_requested.emit("bal", "Balanced", {"gpu_index": 1}, 5)
    page.verify_requested.emit("gone", "Gone", {"gpu_index": 1}, 5)
    assert started == [
        (profile, {"label": "Balanced", "duration_s": 300, "gpu_index": 1})
    ]
    finished = []
    monkeypatch.setattr(
        page.source, "finish_verification", lambda *a: finished.append(a)
    )
    scan.profile_verified.emit("bal", False, True)
    assert finished and finished[-1][0] == "bal" and finished[-1][2] is False
    assert "stopped" in page.notice[0]


def test_scan_and_profiles_lock_each_other(window):
    tab, scan = window.profiles_tab, window.auto_uv_page
    scan.busy_changed.emit(True)
    assert tab.page.busy()
    scan.busy_changed.emit(False)
    assert not tab.page.busy()
    tab.page.status_changed.emit({"running": "Applying", "busy": True})
    assert scan.other_busy
    tab.page.status_changed.emit({"running": "Idle"})
    assert not scan.other_busy


@pytest.mark.parametrize("silent_fan", [False, True])
def test_a_completed_scan_applies_its_profile(window, monkeypatch, silent_fan):
    page, scan = window.profiles_tab.page, window.auto_uv_page
    calls = []
    monkeypatch.setattr(window, "profiles_changed", lambda: calls.append("reload"))
    page.profiles = [
        {"profile_id": "old", "candidate_id": "c0"},
        {"profile_id": "new", "candidate_id": "c1"},
    ]
    monkeypatch.setattr(page, "set_target_gpu", lambda index: calls.append(("gpu", index)))
    monkeypatch.setattr(page, "show_new_profile", lambda pid: calls.append(("show", pid)))
    monkeypatch.setattr(page, "silent_fan_enabled", lambda: False)
    monkeypatch.setattr(page, "set_silent_fan", lambda on: calls.append(("fan", on)))
    monkeypatch.setattr(page, "apply_profile", lambda pid: calls.append(("apply", pid)))
    scan.scan_completed.emit(
        {"outcome": "complete", "candidate_id": "c1", "gpu_index": 0, "silent_fan": silent_fan}
    )
    assert ("apply", "new") not in calls  # after the scan process releases
    QTest.qWait(0)
    expected = ["reload", ("gpu", 0), ("show", "new")]
    expected += [("fan", True)] if silent_fan else []
    assert calls == [*expected, ("apply", "new")]


def test_a_saved_scan_profile_is_shown_and_reloads_the_library_gate(window, monkeypatch):
    page, scan = window.profiles_tab.page, window.auto_uv_page
    summaries, shown = [], []
    monkeypatch.setattr(window.library_tab.library, "set_profile_summaries", summaries.append)
    monkeypatch.setattr(window.profiles_tab.source, "refresh", lambda: None)
    monkeypatch.setattr(page, "reload", lambda: None)
    page.profiles = [{"profile_id": "p1", "candidate_id": "c9"}]
    monkeypatch.setattr(page, "show_new_profile", shown.append)
    scan.profile_saved.emit({"candidate_id": "c9", "gpu_index": 0})
    assert shown == ["p1"] and summaries == [None]
    scan.scan_finished.emit({"outcome": "aborted"})
    assert summaries == [None, None]


def test_use_adaptive_picks_a_game_then_applies_it_in_the_library(window, monkeypatch):
    library = window.library_tab.library
    monkeypatch.setattr(
        library, "adaptive_picker_state", lambda: ("steam:1", [("steam:1", "Game", 90)])
    )
    applied = []
    monkeypatch.setattr(library, "apply_adaptive", lambda key, fps: applied.append((key, fps)))
    seen = []

    class Picker:
        def __init__(self, games, selected, parent):
            seen.append((games, selected))

        def exec(self):
            return W.QDialog.DialogCode.Accepted

        def choice(self):
            return "steam:1", 117

    monkeypatch.setattr(shell, "AdaptiveGamePicker", Picker)
    window.profiles_tab.page.adaptive_requested.emit()
    for _ in range(100):  # the picker opens once the launchers are read
        if seen:
            break
        QTest.qWait(20)
    assert seen == [([shell.AdaptiveGame("steam:1", "Game", 90)], "steam:1")]
    assert applied == [("steam:1", 117)]
    assert window.tabs.currentIndex() == shell.LIBRARY_TAB


def test_scan_profile_id_matches_the_candidate():
    profiles = [{"profile_id": "a", "candidate_id": "c1"}, {"profile_id": "b"}]
    assert shell.scan_profile_id(profiles, "c1") == "a"
    assert shell.scan_profile_id(profiles, "") == ""
    assert shell.scan_profile_id(profiles, "missing") == ""


def test_adaptive_picker_without_eligible_games_cannot_apply(isolated_theme):
    from ui.dialogs.adaptive_game_picker import (
        NO_GAMES,
        AdaptiveGame,
        AdaptiveGamePicker,
    )

    empty = AdaptiveGamePicker([], "")
    assert not empty.apply_button.isEnabled()
    assert any(label.text() == NO_GAMES for label in empty.findChildren(W.QLabel))
    picker = AdaptiveGamePicker(
        [AdaptiveGame("a", "A", 60), AdaptiveGame("b", "B", 144)], "b"
    )
    assert picker.apply_button.isEnabled() and picker.choice() == ("b", 144)
    picker.game_combo.setCurrentIndex(0)
    assert picker.choice() == ("a", 60)


def test_one_import_afterburner_action_lands_on_profiles(window, monkeypatch):
    layout = window.header_layout
    texts = [
        layout.itemAt(i).widget().text()
        for i in range(layout.count())
        if isinstance(layout.itemAt(i).widget(), W.QPushButton)
    ]
    assert texts.count("Import Afterburner") == 1
    runs = []
    monkeypatch.setattr(
        window.profiles_tab.page, "run_afterburner_import", lambda: runs.append(1)
    )
    window.tabs.setCurrentIndex(shell.LIBRARY_TAB)
    window.auto_uv_page.import_button.click()
    assert runs == [1] and window.tabs.currentIndex() == 1
