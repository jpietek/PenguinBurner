from __future__ import annotations

import configparser
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest
from PySide6 import QtCore, QtWidgets

from cli.arguments import parse_arguments
from overlay import shortcuts
from overlay.config import (
    OVERLAY_DETAIL_IDS,
    OVERLAY_DETAIL_PRESETS,
    OverlayConfig,
    load_overlay_config,
    save_overlay_config,
)
from overlay.control import (
    cycle_overlay_detail,
    cycle_overlay_position,
    step_overlay_scale,
)
from overlay.state import write_overlay_override
from ui.components import overlay_config
from ui.components.overlay_config import OverlayConfigPanel
from ui.dialogs import overlay_shortcuts


def test_native_shortcut_quotes_the_installed_executable(monkeypatch):
    monkeypatch.setattr(shortcuts, "running_in_flatpak", lambda: False)
    monkeypatch.setattr(shortcuts.shutil, "which", lambda _: "/home/test user/bin/pburn-cli")
    for _, flag in shortcuts.SHORTCUT_ACTIONS:
        assert shlex.split(shortcuts.shortcut_command(flag)) == [
            "/home/test user/bin/pburn-cli", flag,
        ]


def test_shortcut_defaults_include_all_overlay_actions():
    assert shortcuts.SHORTCUT_DEFAULTS == {
        "--overlay-toggle": "Alt+X",
        "--overlay-detail-next": "Alt+C",
        "--overlay-scale-down": "Alt+[",
        "--overlay-scale-up": "Alt+]",
        "--overlay-position-prev": "Alt+<",
        "--overlay-position-next": "Alt+>",
    }


def test_flatpak_shortcut_targets_the_host_install(monkeypatch):
    monkeypatch.setattr(shortcuts, "running_in_flatpak", lambda: True)
    assert shlex.split(shortcuts.shortcut_command("--overlay-toggle")) == [
        "flatpak", "run", "--command=penguin-burner-cli",
        "io.github.jpietek.PenguinBurner", "--overlay-toggle",
    ]


@pytest.mark.parametrize(
    "desktop, expected",
    [
        ("KDE", ["/usr/bin/kcmshell6", "kcm_keys", "--args", "io.github.jpietek.PenguinBurner.desktop"]),
        ("GNOME-Classic:GNOME", ["/usr/bin/gnome-control-center", "keyboard"]),
        ("sway", None),
    ],
)
def test_settings_target_matches_the_desktop(monkeypatch, desktop, expected):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)
    assert shortcuts.shortcut_settings_command() == expected


def test_gnome_setup_registers_on_explicit_click_without_copy_fields(qtbot, monkeypatch, tmp_path):
    from overlay.gnome_shortcuts import GnomeShortcutSetupResult

    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    launched, calls = [], []
    monkeypatch.setattr(overlay_shortcuts, "start_on_host", lambda command: launched.append(command) or True)
    def setup():
        calls.append(True)
        return GnomeShortcutSetupResult(success=True, message="All six shortcuts are ready.")
    monkeypatch.setattr(overlay_shortcuts, "setup_gnome_shortcuts", setup)
    config = tmp_path / "overlay.toml"
    panel = OverlayConfigPanel(QtCore=QtCore, QtWidgets=QtWidgets, config_path=config)
    qtbot.addWidget(panel.widget)
    panel.shortcuts_button.click()
    dialog = panel.widget.findChild(QtWidgets.QDialog, "overlayShortcutsDialog")
    assert dialog is not None and dialog.isVisible()
    assert not config.exists() and not calls and not launched
    assert not dialog.findChildren(QtWidgets.QLineEdit)
    assert not any(button.text() == "Copy command" for button in dialog.findChildren(QtWidgets.QPushButton))
    button = dialog.findChild(QtWidgets.QPushButton, "overlayShortcutSetup")
    button.click()
    qtbot.waitUntil(lambda: bool(launched))
    assert calls == [True]
    assert launched == [["/usr/bin/gnome-control-center", "keyboard"]]
    assert button.isEnabled()
    assert any("All six shortcuts are ready." in label.text() for label in dialog.findChildren(QtWidgets.QLabel))
    assert not config.exists()
    dialog.close()


def test_gnome_setup_failure_shows_error_without_opening_settings(qtbot, monkeypatch):
    from overlay.gnome_shortcuts import GnomeShortcutSetupResult

    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    launched = []
    monkeypatch.setattr(overlay_shortcuts, "start_on_host", lambda command: launched.append(command) or True)
    monkeypatch.setattr(overlay_shortcuts, "setup_gnome_shortcuts", lambda: GnomeShortcutSetupResult(
        success=False, message="GNOME keyboard settings are unavailable."
    ))
    dialog = overlay_shortcuts.overlay_shortcuts_dialog(QtCore=QtCore, QtWidgets=QtWidgets, parent=None)
    qtbot.addWidget(dialog)
    button = dialog.findChild(QtWidgets.QPushButton, "overlayShortcutSetup")
    button.click()
    qtbot.waitUntil(lambda: button.isEnabled())
    assert not launched
    assert any(label.text() == "GNOME keyboard settings are unavailable." for label in dialog.findChildren(QtWidgets.QLabel))


def test_gnome_setup_can_finish_after_dialog_closes(qtbot, monkeypatch):
    import threading

    from overlay.gnome_shortcuts import GnomeShortcutSetupResult

    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    launched = []
    def setup():
        started.set()
        release.wait(5)
        done.set()
        return GnomeShortcutSetupResult(success=True, message="Ready.")
    monkeypatch.setattr(overlay_shortcuts, "setup_gnome_shortcuts", setup)
    monkeypatch.setattr(overlay_shortcuts, "start_on_host", lambda command: launched.append(command) or True)
    dialog = overlay_shortcuts.overlay_shortcuts_dialog(QtCore=QtCore, QtWidgets=QtWidgets, parent=None)
    qtbot.addWidget(dialog)
    dialog.show()
    button = dialog.findChild(QtWidgets.QPushButton, "overlayShortcutSetup")
    button.click()
    qtbot.waitUntil(started.is_set)
    assert not button.isEnabled()
    dialog.close()
    release.set()
    qtbot.waitUntil(done.is_set)
    assert not launched


def test_other_desktops_keep_manual_shortcut_commands(qtbot, monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "sway")
    dialog = overlay_shortcuts.overlay_shortcuts_dialog(QtCore=QtCore, QtWidgets=QtWidgets, parent=None)
    qtbot.addWidget(dialog)
    assert len(dialog.findChildren(QtWidgets.QLineEdit)) == 6
    assert dialog.findChild(QtWidgets.QPushButton, "overlayShortcutSetup") is None
    command = dialog.findChild(QtWidgets.QLineEdit, "overlayShortcutCommand0")
    dialog.findChild(QtWidgets.QPushButton, "overlayShortcutCopy0").click()
    assert QtWidgets.QApplication.clipboard().text() == command.text()
    assert shlex.split(command.text())[-1] == "--overlay-toggle"


def test_kde_panel_opens_penguinburner_shortcuts_directly(qtbot, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    launched = []
    monkeypatch.setattr(
        overlay_config, "start_on_host", lambda command: launched.append(command) or True
    )
    path = tmp_path / "overlay.toml"
    save_overlay_config(OverlayConfig(enabled_item_ids=("base_fps", "fan_pct")), path)
    original = path.read_bytes()
    panel = OverlayConfigPanel(QtCore=QtCore, QtWidgets=QtWidgets, config_path=path)
    qtbot.addWidget(panel.widget)
    assert not launched
    panel.shortcuts_button.click()

    assert launched == [[
        "/usr/bin/kcmshell6", "kcm_keys", "--args", "io.github.jpietek.PenguinBurner.desktop"
    ]]
    assert not panel.widget.findChildren(QtWidgets.QDialog)
    assert path.read_bytes() == original


def test_kde_launch_failure_shows_retry_and_summary_without_copy_fields(qtbot, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setattr(overlay_config, "start_on_host", lambda _command: False)
    path = tmp_path / "overlay.toml"
    panel = OverlayConfigPanel(QtCore=QtCore, QtWidgets=QtWidgets, config_path=path)
    qtbot.addWidget(panel.widget)
    panel.shortcuts_button.click()

    dialog = panel.widget.findChild(QtWidgets.QDialog, "overlayShortcutsDialog")
    assert dialog is not None and dialog.isVisible()
    error = dialog.findChild(QtWidgets.QLabel, "overlayShortcutError")
    assert "Could not open KDE shortcut settings" in error.text()
    assert not dialog.findChildren(QtWidgets.QLineEdit)
    assert not any(button.text() == "Copy command" for button in dialog.findChildren(QtWidgets.QPushButton))
    text = " ".join(label.text() for label in dialog.findChildren(QtWidgets.QLabel))
    for label, _action in shortcuts.SHORTCUT_ACTIONS:
        assert label in text
    for key in shortcuts.SHORTCUT_DEFAULTS.values():
        assert key in text
    assert not path.exists()
    launched = []
    monkeypatch.setattr(
        overlay_shortcuts, "start_on_host", lambda command: launched.append(command) or True
    )
    dialog.findChild(QtWidgets.QPushButton, "overlayShortcutSettings").click()
    assert launched == [shortcuts.shortcut_settings_command()]
    assert not path.exists()
    dialog.close()


def test_gui_follows_detail_changes_made_by_a_shortcut(qtbot, tmp_path):
    config = tmp_path / "overlay.toml"
    panel = OverlayConfigPanel(QtCore=QtCore, QtWidgets=QtWidgets, config_path=config)
    qtbot.addWidget(panel.widget)
    before = panel.config
    cycle_overlay_detail(config)
    saved_bytes = config.read_bytes()
    panel.refresh_preview()
    assert panel.config != before
    assert panel.detail_combo.currentText() == "Small"
    assert {key for key, box in panel.item_checkboxes.items() if box.isChecked()} == set(
        panel.config.enabled_item_ids
    )
    assert config.read_bytes() == saved_bytes


def test_kde_guidance_uses_registered_actions_without_manual_commands(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    assert "PenguinBurner" in shortcuts.shortcut_instructions()
    assert "paste" not in shortcuts.shortcut_instructions()


def test_shortcut_dialog_opens_settings_only_on_explicit_click(qtbot, monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    launched = []
    monkeypatch.setattr(
        overlay_shortcuts,
        "start_on_host",
        lambda command: launched.append(command) or True,
    )
    dialog = overlay_shortcuts.overlay_shortcuts_dialog(
        QtCore=QtCore, QtWidgets=QtWidgets, parent=None
    )
    qtbot.addWidget(dialog)
    dialog.show()
    assert not launched
    settings = dialog.findChild(QtWidgets.QPushButton, "overlayShortcutSettings")
    assert settings is not None
    settings.click()
    assert launched == [[
        "/usr/bin/kcmshell6", "kcm_keys", "--args", "io.github.jpietek.PenguinBurner.desktop"
    ]]


@pytest.mark.parametrize("package", ["linux", "flatpak"])
def test_desktop_shortcut_actions_route_to_supported_cli_commands(package):
    entry = configparser.ConfigParser(interpolation=None)
    entry.read(Path("packaging") / package / "io.github.jpietek.PenguinBurner.desktop")
    actions = entry["Desktop Entry"]["Actions"].strip(";").split(";")
    assert actions == [
        "ToggleOverlay", "NextOverlayDetail", "SmallerOverlayText",
        "LargerOverlayText", "PreviousOverlayPosition", "NextOverlayPosition",
    ]
    for action, (_, flag) in zip(actions, shortcuts.SHORTCUT_ACTIONS, strict=True):
        command = shlex.split(entry[f"Desktop Action {action}"]["Exec"])
        assert command == ["penguin-burner-cli", flag]
        args = parse_arguments(command[1:])
        assert getattr(args, flag.removeprefix("--").replace("-", "_")) is True
        assert entry[f"Desktop Action {action}"].get("X-KDE-Shortcuts", "") == shortcuts.SHORTCUT_DEFAULTS.get(flag, "")


def test_gui_restores_custom_selection_after_external_cycle(qtbot, tmp_path):
    path = tmp_path / "overlay.toml"
    panel = OverlayConfigPanel(QtCore=QtCore, QtWidgets=QtWidgets, config_path=path)
    qtbot.addWidget(panel.widget)
    panel.item_checkboxes["fan_pct"].click()
    selected = panel.config.enabled_item_ids
    for label in ("Small", "Medium", "Large", "Biggest", "Small"):
        cycle_overlay_detail(path)
        panel.refresh_preview()
        assert panel.detail_combo.currentText() == label
    panel.detail_combo.setCurrentIndex(OVERLAY_DETAIL_IDS.index("custom"))
    assert panel.config.enabled_item_ids == selected
    assert load_overlay_config(path).enabled_item_ids == selected


def test_gui_follows_external_size_and_position_changes(qtbot, tmp_path):
    path = tmp_path / "overlay.toml"
    panel = OverlayConfigPanel(QtCore=QtCore, QtWidgets=QtWidgets, config_path=path)
    qtbot.addWidget(panel.widget)
    step_overlay_scale(1, path)
    cycle_overlay_position(1, path)
    saved_bytes = path.read_bytes()
    panel.refresh_preview()
    assert panel.config.scale == 1.5
    assert panel.config.position == "bottom_left"
    assert path.read_bytes() == saved_bytes


@pytest.mark.parametrize(
    "control, value, changed_field, expected",
    [
        ("enabled_switch", True, "enabled", True),
        ("update_interval_spin", 3, "update_interval_s", 3),
        ("scale_slider", 4, "scale", 2.0),
        ("position_combo", 0, "position", "top_left"),
        ("detail_combo", 2, "detail", "detailed"),
        ("opacity_spin", 70, "opacity", 0.7),
        ("font_combo", 1, "font", "mono"),
        ("layout_combo", 1, "layout", "table"),
    ],
)
def test_gui_edit_keeps_shortcut_changes_before_timer_refresh(
    qtbot, tmp_path, control, value, changed_field, expected
):
    path = tmp_path / "overlay.toml"
    save_overlay_config(OverlayConfig(enabled_item_ids=("base_fps", "fan_pct")), path)
    panel = OverlayConfigPanel(QtCore=QtCore, QtWidgets=QtWidgets, config_path=path)
    qtbot.addWidget(panel.widget)
    panel.timer.stop()
    cycle_overlay_detail(path)
    cycle_overlay_position(1, path)
    step_overlay_scale(1, path)
    before = load_overlay_config(path)

    widget = getattr(panel, control)
    if control == "enabled_switch":
        widget.setChecked(value)
    elif control.endswith("combo"):
        widget.setCurrentIndex(value)
    else:
        widget.setValue(value)

    saved = load_overlay_config(path)
    assert getattr(saved, changed_field) == expected
    for field in ("detail", "position", "scale", "custom_item_ids"):
        if field != changed_field:
            assert getattr(saved, field) == getattr(before, field)


def test_metric_edit_uses_shortcut_preset_before_timer_refresh(qtbot, tmp_path):
    path = tmp_path / "overlay.toml"
    save_overlay_config(OverlayConfig(enabled_item_ids=("base_fps", "fan_pct")), path)
    panel = OverlayConfigPanel(QtCore=QtCore, QtWidgets=QtWidgets, config_path=path)
    qtbot.addWidget(panel.widget)
    panel.timer.stop()
    cycle_overlay_detail(path)
    cycle_overlay_position(1, path)
    panel.item_checkboxes["temperature_c"].click()

    saved = load_overlay_config(path)
    assert saved.enabled_item_ids == ("base_fps", "temperature_c")
    assert saved.custom_item_ids == saved.enabled_item_ids
    assert saved.position == "bottom_left"


def test_last_metric_guard_refreshes_shortcut_state_without_saving(qtbot, tmp_path):
    path = tmp_path / "overlay.toml"
    panel = OverlayConfigPanel(QtCore=QtCore, QtWidgets=QtWidgets, config_path=path)
    qtbot.addWidget(panel.widget)
    panel.timer.stop()
    cycle_overlay_detail(path)
    original = path.read_bytes()
    panel.item_checkboxes["base_fps"].click()

    assert path.read_bytes() == original
    assert panel.config == load_overlay_config(path)
    assert panel.item_checkboxes["base_fps"].isChecked()



def test_native_layer_picks_up_presets_size_and_position_in_one_process(native_overlay_probe, tmp_path):
    path = tmp_path / "overlay.toml"
    env = dict(os.environ, PENGUIN_BURNER_OVERLAY_CONFIG=str(path))
    with subprocess.Popen(
        [str(native_overlay_probe), "--config-watch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env,
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        try:
            for tick, count in enumerate(
                (len(OVERLAY_DETAIL_PRESETS[key]) for key in ("minimal", "standard", "detailed", "full", "minimal")),
                start=1,
            ):
                cycle_overlay_detail(path)
                step_overlay_scale(1, path)
                cycle_overlay_position(1, path)
                process.stdin.write(f"{tick * 1000000}\n")
                process.stdin.flush()
                parts = process.stdout.readline().split()
                assert int(parts[0]) == count
                assert float(parts[1]) == (1.5 if tick == 1 else 2.0)
                assert tuple(map(int, parts[2:])) == (
                    (10, 610), (1070, 610), (10, 10), (540, 10), (1070, 10)
                )[tick - 1]
        finally:
            process.stdin.close()
            process.wait(timeout=5)


def test_native_visibility_reacts_before_the_old_one_second_poll(native_overlay_probe, tmp_path):
    override = tmp_path / "overlay-override"
    assert write_overlay_override(True, override)
    import os

    env = dict(os.environ, PENGUIN_BURNER_OVERLAY_OVERRIDE=str(override), PB_OVERLAY="1")
    with subprocess.Popen(
        [str(native_overlay_probe), "--visibility-watch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env,
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        try:
            for visible in [True, False, True]:
                assert write_overlay_override(visible, override)
                time.sleep(0.1)
                process.stdin.write("tick\n")
                process.stdin.flush()
                assert process.stdout.readline().strip() == str(int(visible))
        finally:
            process.stdin.close()
            process.wait(timeout=5)
