from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from integrations.steam.library import InstalledSteamGame
from integrations.steam.launch_options import inject_launch_options, remove_injection
from integrations.steam.manager import SteamGameRow, SteamIntegrationManager
from integrations.steam.settings import GAME_MODE_ADAPTIVE, SteamGameSetting
from ui.components.steam_panel import (
    SORT_ALPHABETICAL,
    SORT_INSTALLED,
    SORT_RECENT,
    SteamPanel,
    _mode_keys_for_standing_mode,
    last_played_text,
    sorted_steam_rows,
)
from ui.qt import import_qt
from ui.styles import STYLESHEET


def _row(
    tmp_path: Path,
    app_id: str,
    name: str,
    last_played: int,
    *,
    last_updated: int = 0,
    command: str = "%command%",
    compat_tool: str = "proton_experimental",
    effective_compat_tool: str | None = "proton_experimental",
) -> SteamGameRow:
    return SteamGameRow(
        game=InstalledSteamGame(
            app_id=app_id,
            name=name,
            install_dir=name,
            steamapps_dir=tmp_path / "steamapps",
            state_flags=4,
            last_played=last_played,
            last_updated=last_updated,
            icon_path=None,
            compat_tool=compat_tool,
            effective_compat_tool=effective_compat_tool,
            effective_compat_tool_display=(
                "Proton Experimental"
                if effective_compat_tool == "proton_experimental"
                else str(effective_compat_tool or "")
            ),
        ),
        setting=SteamGameSetting(),
        launch_options=command,
    )


class _FakeManager:
    def __init__(self, rows: tuple[SteamGameRow, ...]) -> None:
        self.rows = rows
        self.running: set[str] = set()
        self.stop_requests: list[str] = []
        self.compat_changes: list[tuple[str, str]] = []
        self.launch_changes: list[tuple[str, str]] = []
        self.enabled_changes: list[tuple[str, bool]] = []
        self.target_fps_changes: list[tuple[str, float | None]] = []
        self.bulk_enabled: list[tuple[list[str], bool]] = []
        self.bulk_overlay: list[tuple[list[str], bool]] = []
        self.stop_ok = True
        self.marker = True
        self.cdp = True
        self.initialize_calls = 0
        self.initialize_defaults_calls: list[bool] = []

    def refresh(
        self,
        *,
        read_launch_options: bool = True,
        initialize_defaults: bool = False,
    ):
        del read_launch_options
        self.initialize_defaults_calls.append(initialize_defaults)
        return self.rows

    def standing_mode_label(self) -> str:
        return "Balanced"

    def active_user(self):
        return SimpleNamespace(display_name="jan.pietek")

    def marker_present(self) -> bool:
        return self.marker

    def steam_running(self) -> bool:
        return True

    def cdp_ready(self) -> bool:
        return self.cdp

    def initialize(self):
        self.initialize_calls += 1
        self.marker = True
        return SimpleNamespace(ok=True, message="Steam library connected.")

    def available_compat_tools(self, app_id: str):
        del app_id
        return (
            ("proton_experimental", "Proton Experimental"),
            ("GE-Proton10-34", "GE-Proton10-34"),
        )

    def set_game_compat_tool(self, app_id: str, tool_name: str):
        self.compat_changes.append((app_id, tool_name))
        return SimpleNamespace(ok=True, message=f"Compatibility tool changed to {tool_name}.")

    def set_raw_launch_options(self, app_id: str, text: str):
        self.launch_changes.append((app_id, text))
        return SimpleNamespace(ok=True, message="Applied live.")

    def set_game_enabled(self, app_id: str, enabled: bool):
        self.enabled_changes.append((app_id, enabled))
        updated = []
        launch_options = ""
        for row in self.rows:
            if row.game.app_id != app_id:
                updated.append(row)
                continue
            launch_options = (
                inject_launch_options(row.launch_options, overlay=False)
                if enabled
                else remove_injection(row.launch_options)
            )
            updated.append(
                replace(
                    row,
                    setting=replace(row.setting, enabled=enabled, overlay=False),
                    launch_options=launch_options,
                )
            )
        self.rows = tuple(updated)
        return SimpleNamespace(
            ok=True,
            message="Applied live.",
            launch_options=launch_options,
        )

    def set_all_games_enabled(self, app_ids, enabled):
        self.bulk_enabled.append((sorted(app_ids), bool(enabled)))
        self.rows = tuple(
            replace(row, setting=replace(row.setting, enabled=bool(enabled)))
            for row in self.rows
        )
        return SimpleNamespace(ok=True, message="bulk enable done")

    def set_all_games_overlay(self, app_ids, overlay):
        self.bulk_overlay.append((sorted(app_ids), bool(overlay)))
        return SimpleNamespace(ok=True, message="bulk overlay done")

    def set_game_target_fps(self, app_id: str, target_fps):
        self.target_fps_changes.append((app_id, target_fps))
        self.rows = tuple(
            replace(row, setting=replace(row.setting, target_fps=target_fps))
            if row.game.app_id == app_id
            else row
            for row in self.rows
        )
        return SimpleNamespace(ok=True, message="Applied live.")

    def hot_reapply(self, app_id: str):
        del app_id
        return None

    def game_running(self, app_id: str) -> bool:
        return app_id in self.running

    def running_game_ids(self):
        return frozenset(self.running)

    def stop_game(self, app_id: str):
        self.stop_requests.append(app_id)
        return SimpleNamespace(ok=self.stop_ok, message="Stop requested via Steam.")


class _RunningThread(threading.Thread):
    def is_alive(self) -> bool:
        return True


class _NoRefreshManager(SteamIntegrationManager):
    def refresh(self, **kwargs):
        raise AssertionError("auto-sync must not race the full rescan")


class _BlockingRefreshManager(_FakeManager):
    def __init__(self, rows: tuple[SteamGameRow, ...]) -> None:
        super().__init__(rows)
        self.release_refresh = threading.Event()

    def refresh(
        self,
        *,
        read_launch_options: bool = True,
        initialize_defaults: bool = False,
    ):
        self.release_refresh.wait(timeout=2.0)
        return super().refresh(
            read_launch_options=read_launch_options,
            initialize_defaults=initialize_defaults,
        )


def test_recent_sort_uses_last_played_and_puts_never_played_last(
    tmp_path: Path,
) -> None:
    rows = (
        _row(tmp_path, "10", "Alpha", 100),
        _row(tmp_path, "20", "Beta", 0),
        _row(tmp_path, "30", "Zeta", 300),
    )

    assert [row.game.app_id for row in sorted_steam_rows(rows, SORT_RECENT)] == [
        "30",
        "10",
        "20",
    ]
    assert [
        row.game.app_id for row in sorted_steam_rows(rows, SORT_ALPHABETICAL)
    ] == ["10", "20", "30"]
    assert last_played_text(0) == "Never played on this PC"


def test_installed_sort_uses_last_updated_and_puts_unknown_last(
    tmp_path: Path,
) -> None:
    rows = (
        _row(tmp_path, "10", "Alpha", 300, last_updated=100),
        _row(tmp_path, "20", "Beta", 100, last_updated=0),
        _row(tmp_path, "30", "Zeta", 0, last_updated=300),
    )

    # Newest install first; a missing LastUpdated trails regardless of playtime.
    assert [row.game.app_id for row in sorted_steam_rows(rows, SORT_INSTALLED)] == [
        "30",
        "10",
        "20",
    ]


def test_mode_menu_contains_adaptive_tiers_and_stock() -> None:
    mode_keys = _mode_keys_for_standing_mode("Stock")

    # Per-game Stock stays available: the standing profile can be tuned
    # system-wide while one game pins the factory GPU state.
    assert mode_keys == (
        GAME_MODE_ADAPTIVE,
        "efficiency",
        "balanced",
        "performance",
        "stock",
    )


def test_explicit_adaptive_remains_available_when_default_is_not_adaptive() -> None:
    mode_keys = _mode_keys_for_standing_mode("Balanced")

    assert GAME_MODE_ADAPTIVE in mode_keys


def test_auto_sync_skips_while_full_rescan_is_running() -> None:
    panel = object.__new__(SteamPanel)
    panel._rescan_thread = _RunningThread()
    panel.manager = _NoRefreshManager()

    panel._auto_sync()


def test_library_setup_stays_hidden_until_initial_scan_finishes(
    qtbot,
    tmp_path: Path,
) -> None:
    QtCore, QtGui, QtWidgetsModule, _pg = import_qt()
    manager = _BlockingRefreshManager((_row(tmp_path, "10", "Alpha", 100),))
    panel = SteamPanel(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgetsModule,
        manager=cast(SteamIntegrationManager, manager),
    )
    qtbot.addWidget(panel.widget)
    panel.widget.show()

    assert panel._scan_running
    assert not panel.setup_view.isVisibleTo(panel.widget)

    manager.release_refresh.set()
    qtbot.waitUntil(lambda: not panel._scan_running, timeout=2000)
    assert not panel.setup_view.isVisibleTo(panel.widget)


def test_uninitialized_steam_uses_centered_library_setup(
    qtbot,
    tmp_path: Path,
) -> None:
    QtCore, QtGui, QtWidgetsModule, _pg = import_qt()
    manager = _FakeManager((_row(tmp_path, "10", "Alpha", 100),))
    manager.marker = False
    manager.cdp = False
    panel = SteamPanel(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgetsModule,
        manager=cast(SteamIntegrationManager, manager),
    )
    qtbot.addWidget(panel.widget)
    panel.widget.show()
    qtbot.waitUntil(lambda: not panel._scan_running, timeout=2000)

    assert panel.setup_view.isVisibleTo(panel.widget)
    assert not panel.splitter.isVisibleTo(panel.widget)
    assert panel.setup_title.text() == "Set up your Steam library"
    assert panel.setup_button.text() == "Scan my Steam library"

    panel.setup_button.click()
    qtbot.waitUntil(lambda: not panel._scan_running, timeout=2000)

    assert manager.initialize_calls == 1
    assert manager.initialize_defaults_calls == [False, True]
    assert panel.setup_title.text() == "Your library is ready"
    assert panel.setup_button.text() == "Restart Steam to finish"


def test_native_game_shows_runtime_and_grays_proton_selector(
    qtbot,
    tmp_path: Path,
) -> None:
    QtCore, QtGui, QtWidgetsModule, _pg = import_qt()
    rows = (
        _row(
            tmp_path,
            "10",
            "NativeGame",
            200,
            compat_tool="",
            effective_compat_tool="",
        ),
        _row(
            tmp_path,
            "20",
            "WinGame",
            100,
            compat_tool="",
            effective_compat_tool="proton_experimental",
        ),
    )
    manager = _FakeManager(rows)
    panel = SteamPanel(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgetsModule,
        manager=cast(SteamIntegrationManager, manager),
    )
    qtbot.addWidget(panel.widget)
    qtbot.waitUntil(lambda: not panel._scan_running, timeout=2000)

    # Most recently played (native) game is selected first.
    assert panel.game_title.text() == "NativeGame"
    assert "Native Linux" in panel.game_metadata.text()
    assert not panel.proton_combo.isEnabled()
    assert "natively on Linux" in panel.proton_combo.toolTip()
    compat_disabled_style = STYLESHEET.split(
        "QComboBox#steamCompatTool:disabled {", 1
    )[1].split("}", 1)[0]
    assert "color: #7f8794;" in compat_disabled_style

    panel.game_list.setCurrentItem(panel.game_list.item(1))
    qtbot.waitUntil(lambda: panel.game_title.text() == "WinGame", timeout=2000)
    assert "· Proton ·" in panel.game_metadata.text()
    # The exact tool version lives only in the selector, not the header line.
    assert "proton_experimental" not in panel.game_metadata.text()
    assert panel.proton_combo.isEnabled()
    assert panel.proton_combo.currentText() == "Steam default (Proton Experimental)"
    assert "natively on Linux" not in panel.proton_combo.toolTip()


def test_panel_keeps_library_left_and_one_selected_game_editor(
    qtbot,
    tmp_path: Path,
) -> None:
    QtCore, QtGui, QtWidgetsModule, _pg = import_qt()
    rows = (
        _row(tmp_path, "10", "Alpha", 100, last_updated=100, command="alpha %command%"),
        _row(tmp_path, "20", "Beta", 0, last_updated=500, command="beta %command%"),
        _row(tmp_path, "30", "Zeta", 300, last_updated=200, command="zeta %command%"),
    )
    manager = _FakeManager(rows)
    panel = SteamPanel(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgetsModule,
        manager=cast(SteamIntegrationManager, manager),
    )
    qtbot.addWidget(panel.widget)
    qtbot.waitUntil(lambda: not panel._scan_running, timeout=2000)

    assert panel.splitter.indexOf(panel.library_pane) == 0
    assert panel.splitter.indexOf(panel.details_pane) == 1
    assert not panel.splitter.isCollapsible(0)
    assert panel.sort_combo.currentData() == SORT_RECENT
    assert [panel.game_list.item(i).text() for i in range(3)] == [
        "Zeta",
        "Alpha",
        "Beta",
    ]
    assert panel.game_title.text() == "Zeta"
    assert panel.launch_edit.toPlainText() == "zeta %command%"
    assert panel.launch_edit.textCursor().position() == 0
    assert panel.mode_combo.currentData() == GAME_MODE_ADAPTIVE
    assert panel.enabled_checkbox.text() == "Enable PenguinBurner per-game profiles"
    assert panel.enabled_checkbox.isChecked() is False
    assert panel.mode_label.isEnabled() is False
    assert panel.mode_combo.isEnabled() is False
    assert panel.target_fps_label.isEnabled() is False
    assert panel.target_fps_spin.isEnabled() is False
    assert panel.overlay_checkbox.isChecked() is False
    assert panel.overlay_checkbox.isEnabled() is False

    panel.enabled_checkbox.click()
    assert manager.enabled_changes == [("30", True)]
    assert panel.enabled_checkbox.isChecked() is True
    assert panel.mode_label.isEnabled() is True
    assert panel.mode_combo.isEnabled() is True
    assert panel.target_fps_label.isEnabled() is True
    assert panel.target_fps_spin.isEnabled() is True
    assert panel.overlay_checkbox.isEnabled() is True

    panel.launch_edit.setPlainText("gamemoderun %command%")
    qtbot.waitUntil(lambda: bool(manager.launch_changes), timeout=1500)
    assert manager.launch_changes == [("30", "gamemoderun %command%")]

    line_edits = [
        edit
        for edit in panel.widget.findChildren(QtWidgets.QLineEdit)
        # A QDoubleSpinBox carries an internal QLineEdit editor.
        if not isinstance(edit.parent(), QtWidgets.QAbstractSpinBox)
    ]
    plain_text_edits = panel.widget.findChildren(QtWidgets.QPlainTextEdit)
    tables = panel.widget.findChildren(QtWidgets.QTableWidget)
    play_buttons = [
        button
        for button in panel.widget.findChildren(QtWidgets.QPushButton)
        if button.text() == "Play"
    ]
    stop_buttons = [
        button
        for button in panel.widget.findChildren(QtWidgets.QPushButton)
        if button.text() == "Stop"
    ]
    reset_buttons = [
        button
        for button in panel.widget.findChildren(QtWidgets.QPushButton)
        if button.text() == "Reset"
    ]
    assert line_edits == []
    assert plain_text_edits == [panel.launch_edit]
    assert tables == []
    assert play_buttons == [panel.play_button]
    # One mutating Play/Stop button: no separate Stop button exists.
    assert stop_buttons == []
    # Nothing was launched from the panel yet: no lifecycle status, the
    # button idles as Play.
    assert not panel.game_status.isVisibleTo(panel.widget)
    assert panel.play_button.property("playState") == "idle"
    assert panel.play_button.isEnabled()
    assert reset_buttons == []

    panel.sort_combo.setCurrentIndex(panel.sort_combo.findData(SORT_ALPHABETICAL))
    assert [panel.game_list.item(i).text() for i in range(3)] == [
        "Alpha",
        "Beta",
        "Zeta",
    ]
    # Sorting changes position, not the selected game or its editor state.
    assert panel.game_title.text() == "Zeta"
    assert panel.launch_edit.toPlainText() == "gamemoderun %command%"
    assert panel.launch_edit.textCursor().position() == 0

    # Recently installed orders by LastUpdated, distinct from both other modes.
    panel.sort_combo.setCurrentIndex(panel.sort_combo.findData(SORT_INSTALLED))
    assert [panel.game_list.item(i).text() for i in range(3)] == [
        "Beta",
        "Zeta",
        "Alpha",
    ]
    assert panel.game_title.text() == "Zeta"

    panel._sync_timer.stop()


def test_all_games_menu_bulk_enables_and_disables_with_confirmation(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    QtCore, QtGui, QtWidgetsModule, _pg = import_qt()
    rows = (
        _row(tmp_path, "10", "Alpha", 100),
        _row(tmp_path, "20", "Beta", 200),
    )
    manager = _FakeManager(rows)
    panel = SteamPanel(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgetsModule,
        manager=cast(SteamIntegrationManager, manager),
    )
    qtbot.addWidget(panel.widget)
    qtbot.waitUntil(lambda: not panel._scan_running, timeout=2000)

    assert panel.all_games_button.isEnabled()
    # Nothing enabled yet: only "enable all" can change something.
    assert panel.enable_all_action.isEnabled() is True
    assert panel.disable_all_action.isEnabled() is False
    assert panel.overlay_all_action.isEnabled() is False
    assert panel.overlay_none_action.isEnabled() is False

    # Declining the confirmation applies nothing.
    monkeypatch.setattr(panel, "_confirm_bulk", lambda *args: False)
    panel.enable_all_action.trigger()
    assert manager.bulk_enabled == []

    # Disabled overlay action is a no-op even when triggered.
    monkeypatch.setattr(panel, "_confirm_bulk", lambda *args: True)
    panel.overlay_all_action.trigger()
    assert manager.bulk_overlay == []

    panel.enable_all_action.trigger()
    assert manager.bulk_enabled == [(["10", "20"], True)]
    # Everything enabled now: mass-enable grays out, the others open up.
    assert panel.enable_all_action.isEnabled() is False
    assert panel.disable_all_action.isEnabled() is True
    assert panel.overlay_all_action.isEnabled() is True
    assert panel.overlay_none_action.isEnabled() is False

    panel.overlay_all_action.trigger()
    assert manager.bulk_overlay == [(["10", "20"], True)]

    panel.disable_all_action.trigger()
    assert manager.bulk_enabled[-1] == (["10", "20"], False)
    assert panel.disable_all_action.isEnabled() is False

    panel._sync_timer.stop()


def test_target_fps_spin_stores_per_game_value(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    import ui.components.steam_panel as steam_panel_module

    monkeypatch.setattr(
        steam_panel_module,
        "adaptive_target_fps_from_env",
        lambda **_kwargs: 60.0,
    )
    QtCore, QtGui, QtWidgetsModule, _pg = import_qt()
    manager = _FakeManager((_row(tmp_path, "10", "Alpha", 100),))
    panel = SteamPanel(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgetsModule,
        manager=cast(SteamIntegrationManager, manager),
    )
    qtbot.addWidget(panel.widget)
    qtbot.waitUntil(lambda: not panel._scan_running, timeout=2000)

    # Unset per-game value shows the global default and stays grayed out
    # until PenguinBurner is toggled on for the game.
    assert panel.target_fps_spin.objectName() == "steamAdaptiveTargetFps"
    assert panel.target_fps_spin.text() == "60 FPS"
    assert panel.target_fps_spin.isEnabled() is False

    panel.enabled_checkbox.click()
    assert panel.target_fps_spin.isEnabled() is True

    panel.target_fps_spin.setValue(120)

    assert manager.target_fps_changes == [("10", 120.0)]
    assert manager.rows[0].setting.target_fps == 120.0

    panel._sync_timer.stop()


def test_play_stop_lifecycle_status_under_title(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    QtCore, QtGui, QtWidgetsModule, _pg = import_qt()
    rows = (
        _row(tmp_path, "10", "Alpha", 100),
        _row(tmp_path, "30", "Zeta", 300),
    )
    manager = _FakeManager(rows)
    panel = SteamPanel(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgetsModule,
        manager=cast(SteamIntegrationManager, manager),
    )
    qtbot.addWidget(panel.widget)
    qtbot.waitUntil(lambda: not panel._scan_running, timeout=2000)
    monkeypatch.setattr(
        "ui.components.steam_panel.launch_steam_game", lambda app_id: True
    )
    assert panel.game_title.text() == "Zeta"

    # Play: the game reads as Running immediately, before Steam spawns it.
    # The button mutates in place to a disabled "Starting…" until the session
    # actually exists (TerminateApp on a not-yet-spawned game is a silent
    # no-op), so no click can land in the launch window.
    panel.play_button.click()
    assert panel.game_status.isVisibleTo(panel.widget)
    assert panel.game_status.text() == "Running"
    assert panel.play_button.text() == "Starting…"
    assert not panel.play_button.isEnabled()
    assert panel.play_button.property("playState") == "starting"
    assert panel._game_state_timer.isActive()
    # Transitional states are a hard no-op even when triggered directly:
    # no stop request goes out and the state cannot double-transition.
    panel._play_stop_clicked()
    assert manager.stop_requests == []
    assert panel._tracked["30"].state == "launching"

    # A slow launch stays Running; once the session appears it is confirmed
    # and the button becomes an armed red Stop.
    panel._apply_game_states(manager.running_game_ids())
    assert panel.game_status.text() == "Running"
    manager.running.add("30")
    panel._apply_game_states(manager.running_game_ids())
    assert panel._tracked["30"].state == "running"
    assert panel.play_button.text() == "Stop"
    assert panel.play_button.isEnabled()
    assert panel.play_button.property("playState") == "running"

    # Stop: pending shutdown reads as Stopping and blocks repeat clicks.
    panel.play_button.click()
    assert manager.stop_requests == ["30"]
    assert panel.game_status.text() == "Stopping"
    assert panel.play_button.text() == "Stopping…"
    assert not panel.play_button.isEnabled()
    assert panel.play_button.property("playState") == "stopping"
    panel._apply_game_states(manager.running_game_ids())
    assert panel.game_status.text() == "Stopping"

    # The session went away: one poll could be a fluke (a timed-out process
    # check), two agreeing polls mean Stopped, the timer winds down, and the
    # button mutates back to Play.
    manager.running.discard("30")
    panel._apply_game_states(manager.running_game_ids())
    assert panel.game_status.text() == "Stopping"
    panel._apply_game_states(manager.running_game_ids())
    assert panel.game_status.text() == "Stopped"
    assert panel.play_button.text() == "Play"
    assert panel.play_button.isEnabled()
    assert panel.play_button.property("playState") == "idle"
    assert not panel._game_state_timer.isActive()

    # Games never played from the panel show no status at all.
    panel.game_list.setCurrentItem(panel.game_list.item(1))
    assert panel.game_title.text() == "Alpha"
    assert not panel.game_status.isVisibleTo(panel.widget)
    assert panel.play_button.text() == "Play"
    # …and the stopped game still shows Stopped when reselected.
    panel.game_list.setCurrentItem(panel.game_list.item(0))
    assert panel.game_status.text() == "Stopped"
    assert panel.game_status.isVisibleTo(panel.widget)

    panel._sync_timer.stop()
    panel._game_state_timer.stop()


def test_one_active_game_blocks_playing_another(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    QtCore, QtGui, QtWidgetsModule, _pg = import_qt()
    rows = (
        _row(tmp_path, "10", "Alpha", 100),
        _row(tmp_path, "30", "Zeta", 300),
    )
    manager = _FakeManager(rows)
    panel = SteamPanel(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgetsModule,
        manager=cast(SteamIntegrationManager, manager),
    )
    qtbot.addWidget(panel.widget)
    qtbot.waitUntil(lambda: not panel._scan_running, timeout=2000)
    launched = []
    monkeypatch.setattr(
        "ui.components.steam_panel.launch_steam_game",
        lambda app_id: launched.append(app_id) or True,
    )

    # Launch Zeta (the recently-played selection), confirm it as running.
    assert panel.game_title.text() == "Zeta"
    panel.play_button.click()
    manager.running.add("30")
    panel._apply_game_states(manager.running_game_ids())
    assert panel._tracked["30"].state == "running"
    assert panel.play_button.text() == "Stop"

    # Select Alpha while Zeta runs: Play is blocked, not launchable.
    panel.game_list.setCurrentItem(panel.game_list.item(1))
    assert panel.game_title.text() == "Alpha"
    assert panel.play_button.text() == "Play"
    assert not panel.play_button.isEnabled()
    assert "Zeta" in panel.play_button.toolTip()
    # A programmatic click is a hard no-op too.
    panel._play_stop_clicked()
    assert launched == ["30"]

    # Zeta exits -> Alpha becomes launchable again.
    manager.running.discard("30")
    panel._apply_game_states(manager.running_game_ids())
    panel._apply_game_states(manager.running_game_ids())
    panel.game_list.setCurrentItem(panel.game_list.item(1))
    assert panel.play_button.isEnabled()

    panel._sync_timer.stop()
    panel._game_state_timer.stop()


def test_failed_stop_keeps_running_status(qtbot, tmp_path: Path, monkeypatch) -> None:
    QtCore, QtGui, QtWidgetsModule, _pg = import_qt()
    manager = _FakeManager((_row(tmp_path, "30", "Zeta", 300),))
    manager.stop_ok = False
    panel = SteamPanel(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgetsModule,
        manager=cast(SteamIntegrationManager, manager),
    )
    qtbot.addWidget(panel.widget)
    qtbot.waitUntil(lambda: not panel._scan_running, timeout=2000)
    monkeypatch.setattr(
        "ui.components.steam_panel.launch_steam_game", lambda app_id: True
    )

    panel.play_button.click()
    manager.running.add("30")
    panel._apply_game_states(manager.running_game_ids())
    panel.play_button.click()

    assert panel.game_status.text() == "Running"
    assert panel.play_button.text() == "Stop"
    assert panel.play_button.isEnabled()
    assert "FAILED to stop" in panel.status_label.text()

    panel._sync_timer.stop()
    panel._game_state_timer.stop()


def test_cancelled_launch_times_out_to_stopped(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    QtCore, QtGui, QtWidgetsModule, _pg = import_qt()
    manager = _FakeManager((_row(tmp_path, "30", "Zeta", 300),))
    panel = SteamPanel(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgetsModule,
        manager=cast(SteamIntegrationManager, manager),
    )
    qtbot.addWidget(panel.widget)
    qtbot.waitUntil(lambda: not panel._scan_running, timeout=2000)
    monkeypatch.setattr(
        "ui.components.steam_panel.launch_steam_game", lambda app_id: True
    )

    panel.play_button.click()
    assert panel.game_status.text() == "Running"
    assert panel.play_button.text() == "Starting…"
    panel._tracked["30"].deadline = 0.0  # fast-forward the launch grace window
    panel._apply_game_states(manager.running_game_ids())

    assert panel.game_status.text() == "Stopped"
    assert panel.play_button.text() == "Play"
    assert panel.play_button.isEnabled()
    assert not panel._game_state_timer.isActive()

    panel._sync_timer.stop()
    panel._game_state_timer.stop()


def test_refused_stop_falls_back_to_running(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    QtCore, QtGui, QtWidgetsModule, _pg = import_qt()
    manager = _FakeManager((_row(tmp_path, "30", "Zeta", 300),))
    panel = SteamPanel(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgetsModule,
        manager=cast(SteamIntegrationManager, manager),
    )
    qtbot.addWidget(panel.widget)
    qtbot.waitUntil(lambda: not panel._scan_running, timeout=2000)
    monkeypatch.setattr(
        "ui.components.steam_panel.launch_steam_game", lambda app_id: True
    )

    panel.play_button.click()
    manager.running.add("30")
    panel._apply_game_states(manager.running_game_ids())
    panel.play_button.click()
    assert panel.game_status.text() == "Stopping"
    assert panel.play_button.text() == "Stopping…"

    # The game ignored the stop request (save dialog, hung shutdown): past
    # the stop grace window the panel re-arms Stop instead of lying forever.
    panel._tracked["30"].deadline = 0.0
    panel._apply_game_states(manager.running_game_ids())

    assert panel.game_status.text() == "Running"
    assert panel.play_button.text() == "Stop"
    assert panel.play_button.isEnabled()
    assert "did not stop" in panel.status_label.text()

    panel._sync_timer.stop()
    panel._game_state_timer.stop()


def test_frame_dna_badge_states_peek_and_open_callback(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    from runtime.frame_history import (
        FRAME_HISTORY_ARCHIVE_DIR_ENV,
        FRAME_HISTORY_LIVE_DIR_ENV,
        MetricsSample,
        write_frame_history,
    )

    monkeypatch.setenv(FRAME_HISTORY_LIVE_DIR_ENV, str(tmp_path / "fh-live"))
    monkeypatch.setenv(FRAME_HISTORY_ARCHIVE_DIR_ENV, str(tmp_path / "fh-arch"))

    def _metric(t_rel: int) -> MetricsSample:
        return MetricsSample(
            t_rel_s=t_rel, frame_count=96, clock_mhz=2550, mem_clock_mhz=10251,
            voltage_mv=965, power_w=214, gpu_util_pct=92, cpu_util_pct=34,
            cpu_peak_thread_pct=60, fan_pct=52, temperature_c=66,
            uv_offset_mv=-135, present_fps=96.0, framegen_fps=0.0,
            latency_ms=22.0, display_latency_ms=12.0, framegen_active=False,
            adaptive=True, fps_source=2, tier="balanced",
            ft_p50_ms=10.4, ft_p99_ms=14.1, ft_p999_ms=22.0,
        )

    # Zeta (app 30): qualified 6-minute ring. Beta (app 20): 2-minute warming
    # ring. Alpha (app 10): no ring at all.
    assert write_frame_history(
        tmp_path / "fh-live" / "30.ring",
        samples=[_metric(i) for i in range(360)],
        frametimes_ms=[10.4] * 2000,
        app_id=30, power_limit_w=360, frame_cap=1 << 12,
    )
    assert write_frame_history(
        tmp_path / "fh-live" / "20.ring",
        samples=[_metric(i) for i in range(120)],
        frametimes_ms=[10.4] * 500,
        app_id=20, power_limit_w=360, frame_cap=1 << 12,
    )

    QtCore, QtGui, QtWidgetsModule, _pg = import_qt()
    rows = (
        _row(tmp_path, "10", "Alpha", 100),
        _row(tmp_path, "20", "Beta", 0),
        _row(tmp_path, "30", "Zeta", 300),
    )
    manager = _FakeManager(rows)
    panel = SteamPanel(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgetsModule,
        manager=cast(SteamIntegrationManager, manager),
    )
    qtbot.addWidget(panel.widget)
    qtbot.waitUntil(lambda: not panel._scan_running, timeout=2000)
    try:
        opened: list[tuple[str, str, float | None]] = []
        panel.on_open_frame_dna = lambda app_id, name, fps: opened.append(
            (app_id, name, fps)
        )

        # Recent sort selects Zeta first: qualified ring -> real fingerprint.
        assert panel.game_title.text() == "Zeta"
        badge = panel.frame_dna_badge
        assert badge.isVisibleTo(panel.widget)
        assert not badge.icon().isNull()
        assert badge.toolTip() == ""
        assert panel._frame_dna_summary is not None

        panel._show_frame_dna_peek()
        peek = panel._frame_dna_peek
        assert peek is not None and peek.widget.isVisible()
        assert peek.title.text() == "Zeta · Frame DNA"
        assert peek._stat_values["Power"].text() == "214 W"

        badge.click()  # opens the tab and hides the peek
        assert opened == [("30", "Zeta", None)]
        assert not peek.widget.isVisible()

        # Alpha: no ring -> dashed badge + "no telemetry" tooltip; the click
        # still opens the tab, which explains the empty state.
        panel.game_list.setCurrentItem(panel.game_list.item(1))
        assert panel.game_title.text() == "Alpha"
        assert badge.isVisibleTo(panel.widget)
        assert "No Frame DNA yet" in badge.toolTip()
        assert panel._frame_dna_summary is None
        badge.click()
        assert opened[-1] == ("10", "Alpha", None)

        # Beta: under the 5-minute gate -> dashed badge + warming tooltip.
        panel.game_list.setCurrentItem(panel.game_list.item(2))
        assert panel.game_title.text() == "Beta"
        assert "Warming up" in badge.toolTip()
        assert panel._frame_dna_summary is None
    finally:
        panel._sync_timer.stop()
        panel._game_state_timer.stop()
