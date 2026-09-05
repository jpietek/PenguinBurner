"""The Lutris manager: settings, game configs, and keeping the two honest."""

from __future__ import annotations

import sqlite3
import threading

import pytest
import yaml

from integrations.lutris.manager import LutrisIntegrationManager
from integrations.lutris.settings import load_lutris_game_settings
from profiles.game_profile import GAME_MODE_ADAPTIVE, GAME_MODE_STOCK

_SCHEMA = (
    "create table games (id INTEGER PRIMARY KEY, name TEXT, sortname TEXT, "
    "slug TEXT, installer_slug TEXT, parent_slug TEXT, platform TEXT, "
    "runner TEXT, executable TEXT, directory TEXT, updated DATETIME, "
    "lastplayed INTEGER, installed INTEGER, installed_at INTEGER, year INTEGER, "
    "configpath TEXT, has_custom_banner INTEGER, has_custom_icon INTEGER, "
    "has_custom_coverart_big INTEGER, playtime REAL, service TEXT, "
    "service_id TEXT, discord_id TEXT)"
)


def _home(tmp_path, *, prefix_command: str | None = None, configpath="game-1"):
    root = tmp_path / ".local" / "share" / "lutris"
    (root / "games").mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / "pga.db")
    connection.execute(_SCHEMA)
    connection.execute(
        "insert into games (id, name, slug, runner, platform, installed, "
        "lastplayed, playtime, configpath, directory) values "
        "(27, 'Test Game', 'test-game', 'wine', 'Windows', 1, 10, 1.0, ?, '/g')",
        (configpath,),
    )
    connection.commit()
    connection.close()
    if configpath:
        document = {"game": {"exe": "game.exe"}, "system": {"mangohud": True}}
        if prefix_command is not None:
            document["system"]["prefix_command"] = prefix_command
        (root / "games" / f"{configpath}.yml").write_text(
            yaml.safe_dump(document), encoding="utf-8"
        )
    return tmp_path


def _manager(tmp_path, **kwargs):
    home = _home(tmp_path, **kwargs)
    manager = LutrisIntegrationManager(
        home=home, settings_path=tmp_path / "lutris-game-settings.json"
    )
    manager.refresh()
    return manager


def _config(tmp_path, configpath="game-1") -> dict:
    path = tmp_path / ".local/share/lutris/games" / f"{configpath}.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# -- reading -------------------------------------------------------------------


def test_rows_carry_the_live_prefix_command(tmp_path) -> None:
    """The config file is the truth, not our stored copy of it."""
    manager = _manager(tmp_path, prefix_command="game-performance")

    row = manager.row("27")

    assert row.game.display_name == "Test Game"
    assert row.prefix_command == "game-performance"
    assert row.wrapped is False


def test_availability_reflects_whether_lutris_is_installed(tmp_path) -> None:
    absent = LutrisIntegrationManager(home=tmp_path / "empty")

    assert absent.available is False
    assert _manager(tmp_path).available is True


# -- enabling and disabling ----------------------------------------------------


def test_enabling_wraps_the_game_and_records_the_original(tmp_path) -> None:
    manager = _manager(tmp_path, prefix_command="game-performance")

    result = manager.set_game_enabled("27", True)

    assert result.ok is True
    prefix = _config(tmp_path)["system"]["prefix_command"]
    assert prefix.startswith("env PB_INGAME_LATENCY=1 PENGUIN_BURNER ")
    assert "--pb-lutris-id=27" in prefix
    assert prefix.endswith("game-performance")
    stored = load_lutris_game_settings(tmp_path / "lutris-game-settings.json")["27"]
    assert stored.original_prefix_command == "game-performance"
    assert stored.mode == GAME_MODE_ADAPTIVE


def test_enabling_repairs_the_host_wrapper_before_writing(
    tmp_path, monkeypatch
) -> None:
    """The written line execs PENGUIN_BURNER on the host, so inside a Flatpak
    the wrapper must be made real before a config names it -- a Lutris-only
    host otherwise gets a prefix_command that stops the game launching."""
    import integrations.lutris.manager as manager_module

    calls: list[bool] = []
    monkeypatch.setattr(
        manager_module, "ensure_host_integration", lambda: calls.append(True)
    )
    manager = _manager(tmp_path)

    assert manager.set_game_enabled("27", True).ok
    assert calls == [True]


def test_a_failed_wrapper_repair_blocks_the_write(tmp_path, monkeypatch) -> None:
    import integrations.lutris.manager as manager_module

    def boom() -> None:
        raise RuntimeError("packaged NVAPI shim is missing")

    monkeypatch.setattr(manager_module, "ensure_host_integration", boom)
    manager = _manager(tmp_path)

    result = manager.set_game_enabled("27", True)

    assert not result.ok
    assert "integration repair failed" in result.message
    assert "prefix_command" not in _config(tmp_path)["system"]


def test_disabling_restores_the_users_own_prefix(tmp_path) -> None:
    manager = _manager(tmp_path, prefix_command="game-performance")
    manager.set_game_enabled("27", True)

    manager.set_game_enabled("27", False)

    assert _config(tmp_path)["system"]["prefix_command"] == "game-performance"
    stored = load_lutris_game_settings(tmp_path / "lutris-game-settings.json")["27"]
    assert stored.enabled is False
    assert stored.injected_prefix_command == ""


def test_disabling_keeps_the_users_choices_for_the_next_enable(tmp_path) -> None:
    """Off/on must round-trip, as it does on Steam.

    Tier, GPU choice and FPS target are decisions the user made; deleting the
    record on disable silently reset a configured game to
    Adaptive-with-no-GPU — which on a multi-GPU host means no profile applies
    at all after re-enabling.
    """
    manager = _manager(tmp_path, prefix_command="game-performance")
    manager.set_game_enabled("27", True)
    manager.set_game_mode("27", "balanced")
    manager.set_game_gpu("27", "GPU-abc")
    manager.set_game_target_fps("27", 90.0)

    manager.set_game_enabled("27", False)
    manager.set_game_enabled("27", True)

    stored = load_lutris_game_settings(tmp_path / "lutris-game-settings.json")["27"]
    assert stored.enabled is True
    assert stored.mode == "balanced"
    assert stored.gpu_uuid == "GPU-abc"
    assert stored.target_fps == 90.0
    assert stored.ingame_latency is False  # fixed tiers need no hidden markers


def test_disabling_a_game_that_had_no_prefix_removes_the_key(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.set_game_enabled("27", True)

    manager.set_game_enabled("27", False)

    assert "prefix_command" not in _config(tmp_path)["system"]


def test_a_hand_added_wrapper_is_adopted_without_losing_the_prefix(tmp_path) -> None:
    """The user wrapped the game themselves before ever opening this tab."""
    manager = _manager(tmp_path, prefix_command="game-performance PENGUIN_BURNER")

    manager.set_game_enabled("27", True)
    manager.set_game_enabled("27", False)

    assert _config(tmp_path)["system"]["prefix_command"] == "game-performance"


def test_enabling_leaves_the_rest_of_the_game_config_alone(tmp_path) -> None:
    manager = _manager(tmp_path)

    manager.set_game_enabled("27", True)

    document = _config(tmp_path)
    assert document["game"] == {"exe": "game.exe"}
    assert document["system"]["mangohud"] is True


# -- the individual settings ---------------------------------------------------


def test_the_overlay_toggle_rewrites_the_flag_in_place(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.set_game_enabled("27", True)
    assert "--pb-overlay=0" in _config(tmp_path)["system"]["prefix_command"]

    manager.set_game_overlay("27", True)

    assert "--pb-overlay=1" in _config(tmp_path)["system"]["prefix_command"]


def test_the_target_fps_is_stored_but_stays_out_of_the_prefix(tmp_path) -> None:
    """The wrapper reads the target from the settings file, not from argv."""
    manager = _manager(tmp_path)
    manager.set_game_enabled("27", True)

    manager.set_game_target_fps("27", 120)

    stored = load_lutris_game_settings(tmp_path / "lutris-game-settings.json")["27"]
    assert stored.target_fps == 120.0
    assert "120" not in _config(tmp_path)["system"]["prefix_command"]


def test_a_mode_change_is_stored(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.set_game_enabled("27", True)

    manager.set_game_mode("27", GAME_MODE_STOCK)

    stored = load_lutris_game_settings(tmp_path / "lutris-game-settings.json")["27"]
    assert stored.mode == GAME_MODE_STOCK
    assert stored.ingame_latency is False
    assert "PB_INGAME_LATENCY" not in _config(tmp_path)["system"]["prefix_command"]


def test_enabling_every_game_reports_how_many_changed(tmp_path) -> None:
    manager = _manager(tmp_path)

    result = manager.set_all_games_enabled(["27"], True)

    assert result.ok is True
    assert "1 game(s)" in result.message


# -- refusing rather than half-applying ----------------------------------------

def test_a_game_without_a_config_file_cannot_be_enabled(tmp_path) -> None:
    manager = _manager(tmp_path, configpath="")

    result = manager.set_game_enabled("27", True)

    assert result.ok is False
    assert "no Lutris configuration file" in result.message


def test_an_unknown_game_is_refused(tmp_path) -> None:
    manager = _manager(tmp_path)

    assert manager.set_game_enabled("999", True).ok is False


def test_a_refused_config_write_stores_no_setting(tmp_path, monkeypatch) -> None:
    """A stored setting must never claim a state the game config does not have."""
    manager = _manager(tmp_path)
    import integrations.lutris.manager as manager_module
    from integrations.lutris.config_store import PrefixCommandWrite

    monkeypatch.setattr(
        manager_module,
        "write_prefix_command",
        lambda path, value: PrefixCommandWrite(False, "", "Lutris overwrote it"),
    )

    result = manager.set_game_enabled("27", True)

    assert result.ok is False
    assert load_lutris_game_settings(tmp_path / "lutris-game-settings.json") == {}


def test_a_malformed_game_config_still_lists_the_game(tmp_path) -> None:
    home = _home(tmp_path)
    (home / ".local/share/lutris/games/game-1.yml").write_text(
        "system: [unbalanced\n", encoding="utf-8"
    )
    manager = LutrisIntegrationManager(
        home=home, settings_path=tmp_path / "settings.json"
    )

    rows = manager.refresh()

    assert [row.game.game_id for row in rows] == ["27"]
    assert rows[0].prefix_command == ""


# -- config levels -------------------------------------------------------------


def _runner_config(tmp_path, prefix_command: str) -> None:
    """Lutris resolves prefix_command across system, runner, and game levels."""
    runners = tmp_path / ".local" / "share" / "lutris" / "runners"
    runners.mkdir(parents=True, exist_ok=True)
    (runners / "wine.yml").write_text(
        yaml.safe_dump({"system": {"prefix_command": prefix_command}}),
        encoding="utf-8",
    )


def test_a_prefix_inherited_from_the_runner_is_reported(tmp_path) -> None:
    """Reading only the game file calls an inherited prefix "unset"."""
    home = _home(tmp_path)
    _runner_config(tmp_path, "game-performance")
    manager = LutrisIntegrationManager(
        home=home, settings_path=tmp_path / "settings.json"
    )
    manager.refresh()

    row = manager.row("27")

    assert row.prefix_command == "game-performance"
    assert row.effective.source == "runner"
    assert row.inherited_prefix is True
    assert row.prefix_source_label == "the runner"


def test_enabling_keeps_a_prefix_the_game_only_inherited(tmp_path) -> None:
    """The game level overwrites the runner level outright, so an injection
    that ignores what is inherited silently drops it for that game."""
    home = _home(tmp_path)
    _runner_config(tmp_path, "game-performance")
    manager = LutrisIntegrationManager(
        home=home, settings_path=tmp_path / "settings.json"
    )
    manager.refresh()

    manager.set_game_enabled("27", True)

    written = _config(tmp_path)["system"]["prefix_command"]
    assert written.startswith("env PB_INGAME_LATENCY=1 PENGUIN_BURNER ")
    assert written.endswith("game-performance")


def test_disabling_lets_inheritance_resume_instead_of_freezing_it(tmp_path) -> None:
    """Writing the inherited value back at the game level would pin today's
    runner setting into this game forever."""
    home = _home(tmp_path)
    _runner_config(tmp_path, "game-performance")
    manager = LutrisIntegrationManager(
        home=home, settings_path=tmp_path / "settings.json"
    )
    manager.refresh()
    manager.set_game_enabled("27", True)

    manager.set_game_enabled("27", False)

    assert "prefix_command" not in _config(tmp_path).get("system", {})
    row = manager.row("27")
    assert row.prefix_command == "game-performance"
    assert row.effective.source == "runner"


def test_disable_resumes_updated_inheritance_after_restart(tmp_path) -> None:
    manager = _manager(tmp_path)
    _runner_config(tmp_path, "old-prefix")
    manager.refresh()
    assert manager.set_game_enabled("27", True).ok
    _runner_config(tmp_path, "new-prefix")
    manager = LutrisIntegrationManager(
        home=tmp_path, settings_path=tmp_path / "lutris-game-settings.json"
    )
    manager.refresh()
    assert manager.set_game_overlay("27", True).ok
    assert manager.set_game_enabled("27", False).ok
    assert "prefix_command" not in _config(tmp_path).get("system", {})
    row = manager.row("27")
    assert row is not None
    assert row.prefix_command == "new-prefix"


def test_disable_preserves_explicit_prefix_equal_to_runner(tmp_path) -> None:
    manager = _manager(tmp_path, prefix_command="same-prefix")
    _runner_config(tmp_path, "same-prefix")
    manager.refresh()
    assert manager.set_game_enabled("27", True).ok
    assert manager.set_game_enabled("27", False).ok
    assert _config(tmp_path)["system"]["prefix_command"] == "same-prefix"


def test_disable_preserves_external_edits_to_inherited_wrapped_prefix(tmp_path) -> None:
    manager = _manager(tmp_path)
    _runner_config(tmp_path, "runner-prefix")
    manager.refresh()
    assert manager.set_game_enabled("27", True).ok
    path = tmp_path / ".local/share/lutris/games/game-1.yml"
    document = _config(tmp_path)
    document["system"]["prefix_command"] += " user-added"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    assert manager.set_game_overlay("27", True).ok
    assert manager.set_game_enabled("27", False).ok
    assert _config(tmp_path)["system"]["prefix_command"] == "runner-prefix user-added"


def test_raw_prefix_preserves_quoted_spaces_across_wrap_cycle(tmp_path) -> None:
    manager = _manager(tmp_path)
    command = "env NAME='two  spaces'  gamemoderun"
    assert manager.set_game_prefix_command("27", command).ok
    assert _config(tmp_path)["system"]["prefix_command"] == command
    assert manager.set_game_enabled("27", True).ok
    assert manager.set_game_enabled("27", False).ok
    assert _config(tmp_path)["system"]["prefix_command"] == command


def test_a_game_level_prefix_wins_over_the_runner(tmp_path) -> None:
    home = _home(tmp_path, prefix_command="dlss-swapper")
    _runner_config(tmp_path, "game-performance")
    manager = LutrisIntegrationManager(
        home=home, settings_path=tmp_path / "settings.json"
    )
    manager.refresh()

    row = manager.row("27")

    assert row.prefix_command == "dlss-swapper"
    assert row.effective.source == "game"
    assert row.inherited_prefix is False


def test_a_game_level_prefix_is_restored_on_disable(tmp_path) -> None:
    home = _home(tmp_path, prefix_command="dlss-swapper")
    _runner_config(tmp_path, "game-performance")
    manager = LutrisIntegrationManager(
        home=home, settings_path=tmp_path / "settings.json"
    )
    manager.refresh()
    manager.set_game_enabled("27", True)

    manager.set_game_enabled("27", False)

    assert _config(tmp_path)["system"]["prefix_command"] == "dlss-swapper"


def test_a_hand_written_prefix_is_taken_verbatim(tmp_path) -> None:
    """The form composes a line; this preserves an expert's explicit line."""
    manager = _manager(tmp_path)
    manager.set_game_enabled("27", True)

    result = manager.set_game_prefix_command(
        "27", "  PB_INGAME_LATENCY=1   PENGUIN_BURNER --pb-overlay=0  "
    )

    assert result.ok
    assert (
        _config(tmp_path)["system"]["prefix_command"]
        == "PB_INGAME_LATENCY=1   PENGUIN_BURNER --pb-overlay=0"
    )


def test_a_hand_edit_re_reads_the_toggles_from_what_landed(tmp_path) -> None:
    """After a hand edit the file is the truth, not the stored setting.

    A stored setting still claiming the wrapper is on would leave the tab
    disagreeing with the config it had just written.
    """
    manager = _manager(tmp_path)
    manager.set_game_enabled("27", True)
    assert manager.row("27").setting.enabled is True

    manager.set_game_prefix_command("27", "game-performance")

    row = manager.row("27")
    assert row.setting.enabled is False
    assert row.prefix_command == "game-performance"


def test_a_hand_edit_that_adds_the_overlay_flag_is_read_back(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.set_game_enabled("27", True)

    manager.set_game_prefix_command("27", "PENGUIN_BURNER --pb-overlay=1")

    row = manager.row("27")
    assert row.setting.enabled is True
    assert row.setting.overlay is True


def test_a_hand_edit_under_overlay_keeps_adaptive_markers_enabled(tmp_path) -> None:
    """Injection omits the latency token while the overlay is on, so a raw
    edit of such a line must not read its absence as the user opting out."""
    manager = _manager(tmp_path)
    manager.set_game_enabled("27", True)
    manager.set_game_overlay("27", True)

    manager.set_game_prefix_command("27", "PENGUIN_BURNER --pb-overlay=1")

    row = manager.row("27")
    assert row is not None
    assert row.setting.ingame_latency is True


def test_a_hand_edit_that_removes_the_wrapper_keeps_the_choices(tmp_path) -> None:
    """The preset goes inert, not blank: re-enabling gets the old choices."""
    manager = _manager(tmp_path)
    manager.set_game_enabled("27", True)
    manager.set_game_mode("27", "balanced")
    manager.set_game_target_fps("27", 90.0)

    manager.set_game_prefix_command("27", "game-performance")

    stored = load_lutris_game_settings(tmp_path / "lutris-game-settings.json")["27"]
    assert stored.enabled is False
    assert stored.mode == "balanced"
    assert stored.target_fps == 90.0


def test_adaptive_markers_lead_the_line_without_the_overlay(tmp_path) -> None:
    """It is an environment assignment for the wrapper, so it goes first.

    `env` introduces it because Lutris spawns prefix_command as a command list
    with no shell, where a bare `VAR=1` first token would be the program name.
    """
    manager = _manager(tmp_path)
    manager.set_game_enabled("27", True)

    prefix = _config(tmp_path)["system"]["prefix_command"]
    assert prefix.startswith("env PB_INGAME_LATENCY=1 PENGUIN_BURNER ")
    assert manager.row("27").setting.ingame_latency is True


def test_the_opt_in_is_not_written_when_the_overlay_already_implies_it(
    tmp_path,
) -> None:
    manager = _manager(tmp_path)
    manager.set_game_enabled("27", True)

    manager.set_game_overlay("27", True)

    assert "PB_INGAME_LATENCY" not in _config(tmp_path)["system"]["prefix_command"]
    # The preference survives, so turning the overlay back off restores it
    # rather than silently dropping the markers.
    assert manager.row("27").setting.ingame_latency is True
    manager.set_game_overlay("27", False)
    assert "PB_INGAME_LATENCY=1" in _config(tmp_path)["system"]["prefix_command"]


def test_disabling_the_game_takes_the_opt_in_with_it(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.set_game_enabled("27", True)

    manager.set_game_enabled("27", False)

    assert "PB_INGAME_LATENCY" not in _config(tmp_path)["system"].get(
        "prefix_command", ""
    )


@pytest.mark.parametrize("bulk", [False, True])
def test_library_scan_cannot_revert_a_setting_before_the_next_edit(
    tmp_path, monkeypatch, qapp, qtbot, bulk
) -> None:
    """A scan that read old settings must finish before a write uses the cache."""
    import integrations.lutris.manager as manager_module
    from integrations.lutris.library_source import LutrisLibrarySource
    from ui.components.game_library_panel import GameLibraryPanel
    from ui.qt import import_qt

    monkeypatch.setattr(manager_module, "ensure_host_integration", lambda: None)
    manager = _manager(tmp_path, prefix_command="gamemoderun")
    source = LutrisLibrarySource(manager, home=tmp_path)
    QtCore, QtGui, QtWidgets, _pg = import_qt()
    panel = GameLibraryPanel(
        QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, sources=(source,)
    )
    qtbot.addWidget(panel.widget)
    panel.ensure_scanned()
    panel._library_timer.stop()
    started, release, write_started = (threading.Event() for _ in range(3))
    original_load = manager_module.load_lutris_game_settings
    intercepted = False

    def delayed_load(path):
        nonlocal intercepted
        snapshot = original_load(path)
        if not intercepted:
            intercepted = True
            started.set()
            if not release.wait(5):
                raise TimeoutError("test did not release library scan")
        return snapshot

    original_write = manager.set_game_enabled

    def enable(game_id, enabled):
        write_started.set()
        return original_write(game_id, enabled)

    monkeypatch.setattr(manager_module, "load_lutris_game_settings", delayed_load)
    monkeypatch.setattr(manager, "set_game_enabled", enable)
    panel.rescan(deep=False, quiet=True)
    try:
        assert started.wait(1)
        if bulk:
            panel._confirm = lambda *_: True
            action = next(action for action in source.bulk_actions() if action.key == "enable_all")
            panel._bulk_apply(action, (source,))
        else:
            panel.enable_switch.setChecked(True)
        # Qt can paint the user's intent while the scan still owns the cache.
        painted = []
        QtCore.QTimer.singleShot(0, lambda: painted.append(True))
        qtbot.waitUntil(lambda: bool(painted))
        if not bulk:
            assert panel.enable_switch.isChecked()
        assert not write_started.wait(0.1)
    finally:
        release.set()
        qtbot.waitUntil(lambda: panel._setting_thread is None, timeout=5000)
        qtbot.waitUntil(lambda: panel._scan_result is None, timeout=5000)

    assert manager.row("27").setting.enabled is True
    panel._write_async(panel._selected_game(), "set_game_gpu", "GPU-test")
    qtbot.waitUntil(lambda: panel._setting_thread is None, timeout=5000)

    stored = load_lutris_game_settings(tmp_path / "lutris-game-settings.json")["27"]
    assert stored.enabled is True
    assert stored.gpu_uuid == "GPU-test"
    assert "PENGUIN_BURNER" in _config(tmp_path)["system"]["prefix_command"]
