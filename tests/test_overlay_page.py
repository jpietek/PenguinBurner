"""The In-Game Overlay page (ui/features/overlay/overlay_page.py): the real
overlay settings panel in the new window, with the classic tab's backend
behaviour (overlay.toml writes, telemetry, live preview, shortcuts)."""

from __future__ import annotations

import os
import shutil
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

import ui.shell_window as shell
from integrations.launchers.library import LibraryGame
from overlay.config import (
    OVERLAY_CONFIG_ENV,
    default_overlay_config,
    load_overlay_config,
    save_overlay_config,
)
from overlay.live_preview import MISSING_EXECUTABLE_MESSAGE
from ui.components import overlay_config
from ui.features.overlay import overlay_page
from ui.features.overlay.overlay_page import OverlayPage, OverlayPanel

LIVE = {
    "present_fps": "72",
    "profile_tier": "Performance",
    "clock_mhz": "2700",
}


@pytest.fixture
def telemetry(monkeypatch):
    """What a wrapped game publishes; empty means none is running."""
    published: dict[str, str] = {}
    monkeypatch.setattr(overlay_config, "read_overlay_state", lambda: dict(published))
    return published


@pytest.fixture
def panel(qtbot, tmp_path, telemetry):
    widget = OverlayPanel(config_path=tmp_path / "overlay.toml")
    widget.timer.stop()
    qtbot.addWidget(widget.widget)
    return widget


def caption(panel):
    return panel.widget.findChild(W.QLabel, "overlayPreviewCaption").text()


def test_shell_window_hosts_the_page_on_the_overlay_tab(qtbot, telemetry):
    window = shell.ShellWindow()
    qtbot.addWidget(window)
    page = window.pages[shell.OVERLAY_PAGE]
    assert isinstance(page, OverlayPage) and window.overlay_page is page
    assert shell.PAGES[shell.OVERLAY_PAGE][0] == "In-Game Overlay"
    title = page.panel.widget.findChild(W.QLabel, "overlayPageTitle")
    assert title.text() == "In-Game Overlay"
    # Headings sit inside their cards, one style.
    for card in page.panel.widget.findChildren(W.QFrame, "prefGroupCard"):
        heads = card.findChildren(W.QLabel, "overlayGroupTitle")
        assert len(heads) == 1 and heads[0].text()[0].isupper()
    # The 1 s re-sync runs only while the tab is shown.
    window.show()
    assert not page.panel.timer.isActive()
    window.tabs.setCurrentIndex(shell.OVERLAY_PAGE)
    assert page.panel.timer.isActive()
    window.tabs.setCurrentIndex(0)
    assert not page.panel.timer.isActive()


def test_opening_writes_nothing_and_each_edit_saves_overlay_toml(panel, tmp_path):
    path = tmp_path / "overlay.toml"
    assert not path.exists()
    panel.position_combo.setCurrentText("Bottom left")
    saved = load_overlay_config(path)
    assert saved.position == "bottom_left"
    # The classic save normalises the renderer on an explicit edit.
    assert saved.renderer == "blended"
    panel.opacity_slider.setValue(40)
    assert round(load_overlay_config(path).opacity * 100) == 40
    panel.enabled_switch.click()
    assert load_overlay_config(path).enabled == panel.config.enabled


def test_metric_edits_keep_at_least_one_item(panel, tmp_path):
    path = tmp_path / "overlay.toml"
    save_overlay_config(
        replace(default_overlay_config(enabled=True), enabled_item_ids=("base_fps",)), path
    )
    panel.refresh_preview()
    assert panel.item_checkboxes["base_fps"].isChecked()
    panel.item_checkboxes["base_fps"].setChecked(False)
    assert load_overlay_config(path).enabled_item_ids == ("base_fps",)
    assert panel.status_label.text() == "Keep at least one overlay item enabled."
    panel.item_checkboxes["power_w"].setChecked(True)
    assert "power_w" in load_overlay_config(path).enabled_item_ids


def test_shortcut_and_cli_edits_reload_on_refresh(panel, tmp_path):
    path = tmp_path / "overlay.toml"
    save_overlay_config(replace(default_overlay_config(enabled=True), position="top_right"), path)
    panel.refresh_preview()
    assert panel.position_combo.currentText() == "Top right"


def test_a_failed_save_reports_and_restores_the_saved_settings(panel, monkeypatch, tmp_path):
    def refuse(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(overlay_config, "save_overlay_config", refuse)
    before = panel.opacity_slider.value()
    panel.opacity_slider.setValue(60)
    assert panel.status_label.property("error") is True
    assert "Overlay config save failed" in panel.status_label.text()
    assert panel.opacity_slider.value() == before
    assert not (tmp_path / "overlay.toml").exists()


def test_readings_come_from_telemetry_and_the_running_tier(panel, telemetry):
    # Nothing publishing: sample HUD, sample caption, missing sensors '-'.
    assert caption(panel) == overlay_page.CAPTION_SAMPLE
    assert panel.item_value_labels["profile"].text() == "-"
    assert panel.item_value_labels["clock_mhz"].text() == "-"
    sample_tier = panel.preview._values["profile_tier"]
    # Profiles reports the running tier: the HUD and its row show it.
    panel.set_running_tier("Efficiency")
    assert panel.preview._values["profile_tier"] == "Efficiency"
    assert panel.item_value_labels["profile"].text() == "Efficiency"
    assert panel.item_value_labels["clock_mhz"].text() == "-"
    panel.set_running_tier("")
    assert panel.preview._values["profile_tier"] == sample_tier
    # The daemon publishes GPU readings with no game; its tier wins.
    panel.set_running_tier("Efficiency")
    telemetry.update({k: v for k, v in LIVE.items() if k != "present_fps"})
    panel.refresh_preview()
    assert caption(panel) == overlay_page.CAPTION_GPU
    assert panel.item_value_labels["profile"].text() == "Performance"
    # A wrapped game publishing frames; its missing sensors stay '-'.
    telemetry.update(LIVE)
    panel.refresh_preview()
    assert caption(panel) == overlay_page.CAPTION_LIVE
    assert panel.preview._values["profile_tier"] == "Performance"
    assert panel.item_value_labels["profile"].text() == "Performance"
    assert panel.item_value_labels["clock_mhz"].text() == "2,700 MHz"
    assert panel.item_value_labels["hotspot_temperature_c"].text() == "-"
    assert panel.preview._values["hotspot_temperature_c"] == ""


def test_live_preview_runs_the_layer_command_and_follows_saves(panel, monkeypatch, tmp_path):
    monkeypatch.setattr(overlay_config, "live_preview_command", lambda: None)
    panel.live_button.click()
    assert panel._live_process is None
    assert panel.status_label.text() == MISSING_EXECUTABLE_MESSAGE

    sleep = shutil.which("sleep")
    assert sleep
    monkeypatch.setattr(overlay_config, "live_preview_command", lambda: [sleep, "30"])
    panel.live_button.click()
    process = panel._live_process
    assert process is not None
    assert panel.live_button.text() == overlay_config.LIVE_PREVIEW_STOP
    assert panel.status_label.text() == overlay_config.LIVE_PREVIEW_RUNNING
    assert process.processEnvironment().value(OVERLAY_CONFIG_ENV) == str(
        tmp_path / "overlay.toml"
    )
    # A setting change saves and keeps saying the preview runs.
    panel.position_combo.setCurrentText("Top left")
    assert load_overlay_config(tmp_path / "overlay.toml").position == "top_left"
    assert panel.status_label.text() == overlay_config.LIVE_PREVIEW_RUNNING
    panel.live_button.click()
    assert panel._live_process is None
    assert panel.live_button.text() == overlay_config.LIVE_PREVIEW_START
    assert panel.status_label.text() == "", panel.status_label.text()
    assert panel.status_label.isHidden()


def test_kde_shortcuts_open_kde_settings(panel, monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    repaired, launched = [], []
    monkeypatch.setattr(overlay_config, "repair_user_desktop_entry", lambda: repaired.append(True))
    monkeypatch.setattr(overlay_config, "shortcut_settings_command", lambda: ["kcmshell6", "kcm_keys"])
    monkeypatch.setattr(
        overlay_config, "start_on_host", lambda command: launched.append(command) or True
    )
    panel.shortcuts_button.click()
    assert repaired == [True] and launched == [["kcmshell6", "kcm_keys"]]
    assert panel.status_label.text() == overlay_page.KDE_OPENED
    assert panel.widget.findChild(W.QDialog, "overlayShortcutsDialog") is None


def test_other_desktops_get_the_styled_shortcuts_dialog(panel, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "sway")
    launched = []
    monkeypatch.setattr(overlay_config, "start_on_host", lambda command: launched.append(command))
    panel.shortcuts_button.click()
    dialog = panel.widget.findChild(W.QDialog, "overlayShortcutsDialog")
    assert dialog is not None and dialog.isVisible()
    assert "overlayShortcutsDialog" in dialog.styleSheet()
    assert not launched and not (tmp_path / "overlay.toml").exists()
    dialog.close()


def test_selected_steam_game_puts_its_hero_art_behind_the_preview(panel, monkeypatch, tmp_path):
    root = tmp_path / "Steam"
    hero = root / "appcache" / "librarycache" / "10" / "library_hero.jpg"
    hero.parent.mkdir(parents=True)
    image = G.QImage(64, 20, G.QImage.Format.Format_RGB32)
    image.fill(G.QColor("red"))
    assert image.save(str(hero))
    monkeypatch.setattr(overlay_page, "default_steam_root", lambda: root)

    panel.set_game(LibraryGame(launcher="steam", game_id="10", name="Test Game"))
    assert panel.art_path == hero and not panel.preview.artwork.isNull()
    assert panel.preview.accessibleName() == "Test Game overlay preview"
    # No cached art, or another launcher: the token backdrop.
    panel.set_game(LibraryGame(launcher="steam", game_id="20", name="Bare"))
    assert panel.art_path is None and panel.preview.artwork.isNull()
    panel.set_game(LibraryGame(launcher="heroic", game_id="10", name="Other"))
    assert panel.art_path is None
    assert panel.preview.accessibleName() == "Illustrative overlay preview"
    panel.set_game(None)
    assert panel.art_path is None
