"""The Heroic side of the shared wrapper write path, and its library adapter."""

from __future__ import annotations

import json
import shutil

import pytest

from common import flatpak_wrappers
from integrations.heroic.library_source import HeroicLibrarySource
from integrations.heroic.manager import HeroicIntegrationManager
from integrations.heroic.settings import HEROIC_GAME_SETTINGS_STORE
from profiles.game_profile import GAME_MODE_ADAPTIVE

WRAPPER_FLAG = "--pb-game-id=heroic:Turkey"


def test_flatpak_enable_disable_restores_inheritance(monkeypatch, tmp_path):
    from integrations.heroic.paths import HEROIC_FLATPAK_DIRNAME, heroic_installation
    from integrations.launchers import flatpak as flatpak_module

    _home(tmp_path)
    root = tmp_path / HEROIC_FLATPAK_DIRNAME
    root.parent.mkdir(parents=True)
    (tmp_path / ".config/heroic").rename(root)
    repairs = []
    monkeypatch.setattr(flatpak_module, "ensure_integration", lambda installation: repairs.append(installation.home))
    manager = HeroicIntegrationManager(home=tmp_path, settings_path=tmp_path / "settings.json")
    manager.refresh()
    result = manager.set_game_enabled("Turkey", True)
    assert result.ok
    assert heroic_installation(tmp_path).wrapper in result.command
    row = manager.row("Turkey")
    assert row is not None and row.wrapped
    assert row.setting.injected_command == row.command
    assert repairs == [tmp_path]
    assert "Play in Game Library" in result.message
    assert manager.set_game_enabled("Turkey", False).ok
    settings = json.loads((root / "GamesConfig/Turkey.json").read_text())
    assert "wrapperOptions" not in settings["Turkey"]
    row = manager.row("Turkey")
    assert row is not None and row.command == "game-performance"


def test_stale_native_config_is_preserved_when_wrapping_flatpak_game(monkeypatch, tmp_path):
    from integrations.heroic import paths
    from integrations.heroic.paths import heroic_installation
    from integrations.launchers import flatpak as flatpak_module

    _home(tmp_path, game={'Turkey': {'wrapperOptions': [{'exe': 'existing', 'args': ''}]}})
    native = tmp_path / '.config/heroic'
    flatpak = tmp_path / paths.HEROIC_FLATPAK_DIRNAME
    shutil.copytree(native, flatpak)
    original = {p.relative_to(native): p.read_bytes() for p in native.rglob('*') if p.is_file()}
    from integrations.launchers import installation
    monkeypatch.setattr(installation, 'host_command_path', lambda _name: None)
    monkeypatch.setattr(flatpak_module, 'ensure_integration', lambda _home: None)
    manager = HeroicIntegrationManager(home=tmp_path, settings_path=tmp_path / 'settings.json')
    try:
        manager.refresh()
        assert manager.set_game_enabled('Turkey', True).ok
        config = json.loads((flatpak / 'GamesConfig/Turkey.json').read_text())
        assert heroic_installation(tmp_path).wrapper in json.dumps(config['Turkey']['wrapperOptions'])
        assert {p.relative_to(native): p.read_bytes() for p in native.rglob('*') if p.is_file()} == original
        assert manager.set_game_enabled('Turkey', False).ok
        assert json.loads((flatpak / 'GamesConfig/Turkey.json').read_text())['Turkey'] == json.loads(
            (native / 'GamesConfig/Turkey.json').read_text(),
        )['Turkey']
    finally:
        installation.native_command.cache_clear()


def test_flatpak_failed_preflight_does_not_save_enabled_command(monkeypatch, tmp_path):
    from integrations.heroic.paths import HEROIC_FLATPAK_DIRNAME
    from integrations.launchers import flatpak as flatpak_module

    _home(tmp_path)
    root = tmp_path / HEROIC_FLATPAK_DIRNAME
    root.parent.mkdir(parents=True)
    (tmp_path / ".config/heroic").rename(root)

    def fail(_home):
        raise RuntimeError("sandbox layer unavailable")

    monkeypatch.setattr(flatpak_module, "ensure_integration", fail)
    manager = HeroicIntegrationManager(home=tmp_path, settings_path=tmp_path / "settings.json")
    manager.refresh()
    result = manager.set_game_enabled("Turkey", True)
    assert not result.ok and "sandbox layer unavailable" in result.message
    assert not (root / "GamesConfig/Turkey.json").exists()
    assert not (tmp_path / "settings.json").exists()


def test_flatpak_malformed_manual_command_reports_error(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    result = manager.set_game_command("Turkey", "gamemoderun 'unterminated")
    assert not result.ok
    assert "quotation" in result.message


@pytest.fixture(autouse=True)
def _no_host_repair(monkeypatch):
    """Outside a Flatpak this is a no-op; here it must never touch the host."""
    monkeypatch.setattr(flatpak_wrappers, "ensure_host_integration", lambda: None)


def _home(
    tmp_path,
    *,
    defaults=("game-performance",),
    game=None,
    platform="Windows",
    app_names=("Turkey",),
):
    root = tmp_path / ".config" / "heroic"
    (root / "GamesConfig").mkdir(parents=True, exist_ok=True)
    (root / "store_cache").mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(
        json.dumps(
            {
                "defaultSettings": {
                    "wrapperOptions": [{"exe": exe, "args": ""} for exe in defaults]
                }
            }
        )
    )
    (root / "store_cache" / "legendary_library.json").write_text(
        json.dumps(
            {
                "library": [
                    {
                        "app_name": app_name,
                        "title": "Borderlands",
                        "runner": "legendary",
                        "is_installed": True,
                    }
                    for app_name in app_names
                ]
            }
        )
    )
    # What is installed, and where, is the store backend's record -- Heroic
    # only copies it into the cached library when it next refreshes.
    installed = root / "legendaryConfig" / "legendary" / "installed.json"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text(
        json.dumps(
            {
                app_name: {
                    "app_name": app_name,
                    "platform": platform,
                    "install_path": "/g/bl",
                }
                for app_name in app_names
            }
        )
    )
    if game is not None:
        (root / "GamesConfig" / "Turkey.json").write_text(json.dumps(game))
    return tmp_path


def _manager(tmp_path, **kwargs) -> HeroicIntegrationManager:
    manager = HeroicIntegrationManager(
        home=_home(tmp_path, **kwargs),
        settings_path=tmp_path / "heroic-game-settings.json",
    )
    manager.refresh()
    return manager


def _wrappers(tmp_path) -> list[dict]:
    path = tmp_path / ".config" / "heroic" / "GamesConfig" / "Turkey.json"
    return json.loads(path.read_text())["Turkey"].get("wrapperOptions", [])


def _stored(tmp_path):
    return HEROIC_GAME_SETTINGS_STORE.load(tmp_path / "heroic-game-settings.json")["Turkey"]


def test_rows_carry_what_the_game_actually_launches_through(tmp_path) -> None:
    manager = _manager(tmp_path)

    (row,) = manager.rows()

    assert row.command == "game-performance"
    assert row.inherited is True
    assert row.source_label == "Heroic global settings"
    assert row.wrapped is False


def test_enabling_keeps_the_wrappers_the_game_inherited(tmp_path) -> None:
    """Writing the game level replaces the global rows, so they come along.

    Adaptive also asks for latency markers, and the opt-in has to be set
    before the wrapper is the thing running -- so it rides an `env` prefix,
    which Heroic can start because it spawns wrappers as plain argv.
    """
    manager = _manager(tmp_path)

    assert manager.set_game_enabled("Turkey", True).ok

    assert _wrappers(tmp_path) == [
        {"exe": "game-performance", "args": ""},
        {
            "exe": "env",
            "args": (
                f"PB_INGAME_LATENCY=1 PENGUIN_BURNER --pb-overlay=0 {WRAPPER_FLAG}"
            ),
        },
    ]
    assert _stored(tmp_path).original_command == "game-performance"


def test_disabling_lets_the_global_wrappers_take_over_again(tmp_path) -> None:
    """A game we found inheriting must be left inheriting, not frozen."""
    manager = _manager(tmp_path)
    manager.set_game_enabled("Turkey", True)

    assert manager.set_game_enabled("Turkey", False).ok

    assert _wrappers(tmp_path) == []
    row = manager.row("Turkey")
    assert row is not None
    assert (row.command, row.inherited) == ("game-performance", True)


def test_a_games_own_wrappers_are_restored_rather_than_dropped(tmp_path) -> None:
    manager = _manager(tmp_path, game={"Turkey": {"wrapperOptions": [{"exe": "mangohud", "args": ""}]}})
    manager.set_game_enabled("Turkey", True)

    assert manager.set_game_enabled("Turkey", False).ok

    assert _wrappers(tmp_path) == [{"exe": "mangohud", "args": ""}]


def test_the_overlay_switch_rewrites_only_our_own_row(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.set_game_enabled("Turkey", True)

    assert manager.set_game_overlay("Turkey", True).ok

    rows = _wrappers(tmp_path)
    # Overlay on, so the wrapper turns the markers on by itself and the env
    # prefix goes away with them.
    assert rows[-1] == {
        "exe": "PENGUIN_BURNER",
        "args": f"--pb-overlay=1 {WRAPPER_FLAG}",
    }
    assert rows[:-1] == [{"exe": "game-performance", "args": ""}]


def test_a_hand_edit_re_reads_the_toggles_from_what_landed(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.set_game_enabled("Turkey", True)

    assert manager.set_game_command("Turkey", "mangohud").ok

    stored = _stored(tmp_path)
    assert stored.enabled is False
    assert stored.original_command == "mangohud"


def test_settings_land_in_heroics_own_file(tmp_path) -> None:
    """App names and Lutris ids must not share one map."""
    manager = _manager(tmp_path)

    manager.set_game_mode("Turkey", GAME_MODE_ADAPTIVE)

    assert (tmp_path / "heroic-game-settings.json").is_file()


def test_the_library_adapter_describes_the_game_the_tab_draws(tmp_path) -> None:
    source = HeroicLibrarySource(_manager(tmp_path), home=tmp_path)
    source.refresh()

    (game,) = source.games()

    assert (game.launcher, game.game_id, game.name) == ("heroic", "Turkey", "Borderlands")
    assert game.subtitle == "Epic"
    assert game.installed_at == 0  # Heroic does not report an install timestamp.
    assert game.overlay_supported is True  # Proton translates everything to Vulkan
    (compatibility, field) = source.fields(game)
    assert compatibility.key == "compat_tool"
    assert field.key == "wrapper_command"
    assert field.setter == "set_game_command"
    assert "inherited from Heroic global settings" in field.subtitle


def test_a_linux_native_game_is_the_only_one_the_overlay_can_miss(
    tmp_path, monkeypatch
) -> None:
    """Proton translates everything to Vulkan; a native build need not be.

    The probe walks the install directory, so it must run on the scan worker
    and be answered from the cache afterwards -- never on the GUI thread.
    """
    from integrations.heroic import library_source as heroic_source

    source = HeroicLibrarySource(_manager(tmp_path), home=tmp_path)
    probed: list[str] = []
    monkeypatch.setattr(
        heroic_source,
        "overlay_support",
        lambda **kwargs: probed.append(str(kwargs.get("directory"))) or (False, "OpenGL"),
    )

    source.refresh()
    assert probed == []  # a Windows game never reaches the probe

    _home(tmp_path, platform="linux")
    source.refresh()

    (game,) = source.games()
    assert (game.overlay_supported, game.overlay_unsupported_reason) == (
        False,
        "OpenGL",
    )
    assert probed == ["/g/bl"]

    # The view reads the cached fact; a cheap tick never probes disk again.
    source.refresh(deep=False)
    source.games()
    assert probed == ["/g/bl"]


def test_the_global_wrappers_are_read_once_a_scan_not_once_a_game(
    tmp_path, monkeypatch
) -> None:
    """Every game without wrappers of its own falls back to that one file.

    Reading it per game turned a library scan -- which the tab repeats on a
    timer -- into one full parse of Heroic's whole settings block per title.
    """
    import integrations.heroic.manager as manager_module

    home = _home(tmp_path, app_names=("Turkey", "Eel", "Wren"))
    manager = HeroicIntegrationManager(
        home=home, settings_path=tmp_path / "heroic-game-settings.json"
    )
    reads: list[bool] = []
    original = manager_module.read_global_entries
    monkeypatch.setattr(
        manager_module,
        "read_global_entries",
        lambda where: reads.append(True) or original(where),
    )

    rows = manager.refresh()

    assert len(rows) == 3
    assert reads == [True]
    assert {row.command for row in rows} == {"game-performance"}


@pytest.fixture(autouse=True)
def _wrapped_overlay_identity(monkeypatch):
    monkeypatch.setattr("integrations.launchers.live_overlay.wrapped_game_keys",
                        lambda: frozenset({"heroic:Turkey", "heroic:Other", "lutris:27"}))


@pytest.mark.parametrize("enabled", [True, False])
def test_overlay_toggle_updates_running_heroic_session(tmp_path, monkeypatch, enabled):
    from integrations.heroic import library_source
    from integrations.launchers.wrapped_sessions import LauncherSessions
    from overlay.state import OVERLAY_OVERRIDE_ENV

    manager = _manager(tmp_path)
    assert manager.set_game_enabled("Turkey", True).ok
    source = HeroicLibrarySource(manager, home=tmp_path)
    override = tmp_path / "overlay-override"
    override.write_text("0" if enabled else "1")
    monkeypatch.setenv(OVERLAY_OVERRIDE_ENV, str(override))
    monkeypatch.setattr(
        library_source, "probe_heroic_sessions",
        lambda **kwargs: LauncherSessions(wrapped={"Turkey": (42,)}),
    )

    assert manager.set_game_overlay("Turkey", enabled).ok
    result = source.after_setting_write("Turkey", "set_game_overlay")

    assert result is not None and result.ok
    assert "live" in result.message
    assert override.read_text() == ("1" if enabled else "0")
    assert _stored(tmp_path).overlay is enabled


@pytest.mark.parametrize("state", ["idle", "other_game", "external", "unknown"])
def test_overlay_toggle_does_not_claim_live_update_without_wrapped_session(
    tmp_path, monkeypatch, state
):
    from integrations.heroic import library_source
    from integrations.launchers.wrapped_sessions import LauncherSessions
    from overlay.state import OVERLAY_OVERRIDE_ENV

    manager = _manager(tmp_path)
    assert manager.set_game_enabled("Turkey", True).ok
    source = HeroicLibrarySource(manager, home=tmp_path)
    sessions = {
        "idle": LauncherSessions(),
        "other_game": LauncherSessions(wrapped={"Other": (42,)}),
        "external": LauncherSessions(external={"Turkey": (42,)}),
        "unknown": None,
    }
    monkeypatch.setattr(
        library_source, "probe_heroic_sessions", lambda **kwargs: sessions[state]
    )
    if state == "external":
        monkeypatch.setattr("integrations.launchers.live_overlay.wrapped_game_keys", frozenset)
    override = tmp_path / "overlay-override"
    override.write_text("0")
    monkeypatch.setenv(OVERLAY_OVERRIDE_ENV, str(override))

    assert manager.set_game_overlay("Turkey", True).ok
    result = source.after_setting_write("Turkey", "set_game_overlay")

    assert override.read_text() == "0"
    assert _stored(tmp_path).overlay is True
    if state in ("idle", "other_game"):
        assert result is None
    else:
        assert result is not None and not result.ok
        assert "saved" in result.message
        if state == "external":
            assert "relaunch" in result.message


def test_live_overlay_write_failure_keeps_saved_preference(tmp_path, monkeypatch):
    from integrations.heroic import library_source
    from integrations.launchers.wrapped_sessions import LauncherSessions

    manager = _manager(tmp_path)
    assert manager.set_game_enabled("Turkey", True).ok
    source = HeroicLibrarySource(manager, home=tmp_path)
    monkeypatch.setattr(
        library_source, "probe_heroic_sessions",
        lambda **kwargs: LauncherSessions(wrapped={"Turkey": (42,)}),
    )
    monkeypatch.setattr("overlay.state.write_overlay_override", lambda enabled: False)

    assert manager.set_game_overlay("Turkey", True).ok
    result = source.after_setting_write("Turkey", "set_game_overlay")

    assert result is not None and not result.ok
    assert "live visibility update failed" in result.message
    assert _stored(tmp_path).overlay is True


def test_other_heroic_settings_do_not_change_live_overlay(tmp_path, monkeypatch):
    source = HeroicLibrarySource(_manager(tmp_path), home=tmp_path)
    monkeypatch.setattr(
        source, "_running_sessions", lambda: pytest.fail("Unexpected live overlay update")
    )
    for setter in ("set_game_enabled", "set_game_mode", "set_game_gpu", "set_game_target_fps"):
        assert source.after_setting_write("Turkey", setter) is None


def test_heroic_target_qt_edit_applies_saved_target_live(qapp, qtbot, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from integrations.launchers import runtime_profile
    from profiles import game_profile
    from runtime import daemon_client
    from ui.components.game_library_panel import GameLibraryPanel
    from ui.qt import import_qt

    manager = _manager(tmp_path)
    assert manager.set_game_enabled("Turkey", True).ok
    assert manager.set_game_target_fps("Turkey", 120).ok
    source = HeroicLibrarySource(manager, home=tmp_path)
    monkeypatch.setattr(source, "probe_can_launch", lambda: False)
    monkeypatch.setattr(daemon_client, "daemon_status", lambda **kw: {
        "active_job": {"runtime_mode": "adaptive", "gpu_uuid": "GPU-1"},
        "game_runtime": {"active": True, "watched": [{"pid": 4242, "app_id": "heroic:Turkey"}]},
    })
    monkeypatch.setattr(runtime_profile.DaemonGpuClient, "discover_identities",
                        lambda: [SimpleNamespace(uuid="GPU-1", index=0)])
    monkeypatch.setattr(game_profile, "read_auto_uv_profiles", list)
    monkeypatch.setattr(game_profile, "resolve_profile_tier_profiles",
                        lambda *a, **kw: {"performance": {"profile_id": "perf-1"}})
    calls = []

    def apply(argv, **kwargs):
        calls.append((argv, kwargs))
        return {"started": True}

    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", apply)
    QtCore, QtGui, QtWidgets, _pg = import_qt()
    panel = GameLibraryPanel(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, sources=(source,))
    qtbot.addWidget(panel.widget)
    panel.ensure_scanned()
    qtbot.waitUntil(lambda: bool(panel._games), timeout=5000)
    panel._select_key("heroic:Turkey")
    panel.target_fps_spin.setValue(90)
    qtbot.waitUntil(lambda: panel._setting_thread is None, timeout=5000)
    assert _stored(tmp_path).target_fps == 90
    argv, kwargs = calls.pop()
    assert argv[argv.index("--adaptive-target-fps") + 1] == "90"
    assert kwargs["watch_pid"] == 4242
    assert "applied to the running game" in panel.status_label.text()

    panel.per_game_target_switch.click()
    qtbot.waitUntil(lambda: panel._setting_thread is None, timeout=5000)
    assert _stored(tmp_path).target_fps is None
    assert "--adaptive-target-fps" not in calls.pop()[0]


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("launcher", ["heroic", "lutris"])
def test_overlay_qt_toggle_writes_live_visibility(qapp, qtbot, tmp_path, monkeypatch, bulk, launcher):
    from integrations.heroic import library_source
    from integrations.launchers.wrapped_sessions import LauncherSessions
    from overlay.state import OVERLAY_OVERRIDE_ENV
    from ui.components.game_library_panel import GameLibraryPanel
    from ui.qt import import_qt

    if launcher == "heroic":
        manager = _manager(tmp_path)
        game_id = "Turkey"
        source = HeroicLibrarySource(manager, home=tmp_path)
        monkeypatch.setattr(library_source, "probe_heroic_sessions",
                            lambda **kwargs: LauncherSessions(wrapped={game_id: (42,)}))
    else:
        from test_lutris_manager import _manager as lutris_manager

        from integrations.lutris.library_source import LutrisLibrarySource

        manager = lutris_manager(tmp_path)
        game_id = "27"
        source = LutrisLibrarySource(manager, home=tmp_path)
        monkeypatch.setattr(source, "running_game_ids", lambda: frozenset({game_id}))
    assert manager.set_game_enabled(game_id, True).ok
    monkeypatch.setattr(source, "probe_can_launch", lambda: True)
    override = tmp_path / "overlay-override"
    monkeypatch.setenv(OVERLAY_OVERRIDE_ENV, str(override))
    QtCore, QtGui, QtWidgets, _pg = import_qt()
    panel = GameLibraryPanel(
        QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, sources=(source,)
    )
    qtbot.addWidget(panel.widget)
    panel.ensure_scanned()
    qtbot.waitUntil(lambda: bool(panel._games), timeout=5000)
    panel._select_key(f"{launcher}:{game_id}")

    panel._confirm = lambda *_args: True
    for enabled in (True, False):
        if bulk:
            action, sources = next(
                (action, sources) for action, sources in panel._bulk_entries
                if action.key == ("overlay_all" if enabled else "overlay_none")
            )
            panel._bulk_apply(action, sources)
        else:
            panel.overlay_switch.click()
        qtbot.waitUntil(lambda: panel._setting_thread is None, timeout=5000)
        assert override.read_text() == ("1" if enabled else "0")
        assert source.saved_overlay(game_id) is enabled
        assert "live" in panel.status_label.text()


@pytest.mark.parametrize("flatpak", [False, True])
def test_bulk_overlay_preserves_settings_and_skips_unwrapped_games(tmp_path, monkeypatch, flatpak):
    from integrations.heroic.paths import HEROIC_FLATPAK_DIRNAME

    _home(tmp_path, app_names=("Turkey", "Other", "Disabled"))
    if flatpak:
        root = tmp_path / HEROIC_FLATPAK_DIRNAME
        root.parent.mkdir(parents=True)
        (tmp_path / ".config/heroic").rename(root)
        monkeypatch.setattr("integrations.launchers.flatpak.ensure_integration", lambda home: None)
    manager = HeroicIntegrationManager(home=tmp_path, settings_path=tmp_path / "settings.json")
    manager.refresh()
    assert manager.set_all_games_enabled(("Turkey", "Other"), True).ok
    assert manager.set_game_mode("Turkey", GAME_MODE_ADAPTIVE).ok
    assert manager.set_game_target_fps("Turkey", 72).ok
    before = manager.row("Turkey").setting
    disabled = manager.row("Disabled")
    for enabled in (True, False):
        result = manager.set_all_games_overlay(("Turkey", "Other", "Disabled"), enabled)
        assert result.ok
        assert result.applied_game_ids == ("Turkey", "Other")
        for game_id in result.applied_game_ids:
            row = manager.row(game_id)
            assert row.setting.overlay is enabled
            assert f"--pb-overlay={int(enabled)}" in row.command
            assert "game-performance" in row.command
        row = manager.row("Turkey")
        assert (row.setting.mode, row.setting.target_fps, row.setting.gpu_uuid) == (
            before.mode, before.target_fps, before.gpu_uuid,
        )
        assert row.setting.ingame_latency
        assert manager.row("Disabled") == disabled
    assert "PB_INGAME_LATENCY=1" in manager.row("Turkey").command


@pytest.mark.parametrize("running_id", ["Turkey", "Other"])
def test_bulk_overlay_live_update_uses_only_successfully_saved_games(tmp_path, monkeypatch, running_id):
    from integrations.launchers.wrapped_sessions import LauncherSessions
    from overlay.state import OVERLAY_OVERRIDE_ENV

    manager = _manager(tmp_path, app_names=("Turkey", "Other"))
    assert manager.set_all_games_enabled(("Turkey", "Other"), True).ok
    path = tmp_path / ".config/heroic/GamesConfig/Turkey.json"
    path.write_text("{broken")
    source = HeroicLibrarySource(manager, home=tmp_path)
    probes = []
    def sessions():
        probes.append(True)
        return LauncherSessions(wrapped={running_id: (42,)})
    monkeypatch.setattr(source, "_running_sessions", sessions)
    override = tmp_path / "overlay-override"
    override.write_text("0")
    monkeypatch.setenv(OVERLAY_OVERRIDE_ENV, str(override))
    result = manager.set_all_games_overlay(("Turkey", "Other"), True)
    assert not result.ok and "1 game(s) updated" in result.message
    assert result.applied_game_ids == ("Other",)
    followup = source.after_bulk_write("set_all_games_overlay", result)
    assert probes == [True]
    assert (followup is not None) == (running_id == "Other")
    assert override.read_text() == ("1" if running_id == "Other" else "0")
    assert path.read_text() == "{broken"


@pytest.mark.parametrize("launcher", ["heroic", "lutris"])
@pytest.mark.parametrize("accepted", [True, False])
def test_launcher_mode_qt_edit_reports_verified_runtime(qapp, qtbot, tmp_path, monkeypatch, accepted, launcher):
    from integrations.launchers import runtime_profile
    from runtime import daemon_client
    from ui.components.game_library_panel import GameLibraryPanel
    from ui.qt import import_qt

    if launcher == "heroic":
        manager = _manager(tmp_path)
        game_id = "Turkey"
        source = HeroicLibrarySource(manager, home=tmp_path)
    else:
        from test_lutris_manager import _manager as lutris_manager

        from integrations.lutris.library_source import LutrisLibrarySource

        manager = lutris_manager(tmp_path)
        game_id = "27"
        source = LutrisLibrarySource(manager, home=tmp_path)
    assert manager.set_game_enabled(game_id, True).ok
    monkeypatch.setattr(source, "probe_can_launch", lambda: False)
    status = {
        "active_job": {"runtime_mode": "adaptive", "gpu_uuid": "GPU-1"},
        "game_runtime": {"active": True, "watched": [{"pid": 4242, "app_id": f"{launcher}:{game_id}"}]},
    }
    monkeypatch.setattr(daemon_client, "daemon_status", lambda **kw: status)
    monkeypatch.setattr(runtime_profile, "profile_argv", lambda setting: (
        ["--auto-uv-profile", "perf-1"] + (["--adaptive-auto-uv"] if setting.mode == "adaptive" else [])
    ))
    def apply(argv, **kwargs):
        assert kwargs["watch_pid"] == 4242
        if accepted:
            status["active_job"].update(
                runtime_mode="adaptive" if "--adaptive-auto-uv" in argv else "static",
                profile_id="perf-1",
            )
        return {"started": True}
    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", apply)
    QtCore, QtGui, QtWidgets, _pg = import_qt()
    panel = GameLibraryPanel(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, sources=(source,))
    qtbot.addWidget(panel.widget)
    panel.ensure_scanned()
    qtbot.waitUntil(lambda: bool(panel._games), timeout=5000)
    panel._select_key(f"{launcher}:{game_id}")
    panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("performance"))
    qtbot.waitUntil(lambda: panel._setting_thread is None, timeout=5000)
    assert manager.row(game_id).setting.mode == "performance"
    if accepted:
        assert status["active_job"]["runtime_mode"] == "static"
        assert "verified with daemon" in panel.status_label.text()
        panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("adaptive"))
        qtbot.waitUntil(lambda: panel._setting_thread is None, timeout=5000)
        assert status["active_job"]["runtime_mode"] == "adaptive"
        assert "verified with daemon" in panel.status_label.text()
    else:
        assert status["active_job"]["runtime_mode"] == "adaptive"
        assert "unconfirmed (daemon mode: adaptive)" in panel.status_label.text()
