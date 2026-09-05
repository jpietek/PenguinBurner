from pathlib import Path
from types import SimpleNamespace

import pytest

from profiles import game_profile

import integrations.steam.manager as manager_module
from integrations.steam.cdp import SteamAppDetails
from integrations.steam.manager import SteamIntegrationManager
from integrations.steam.settings import (
    load_steam_game_settings,
)
from profiles.game_profile import (
    GAME_MODE_ADAPTIVE,
    GAME_MODE_DEFAULT,
    GAME_MODE_STOCK,
)
from integrations.steam.users import STEAMID64_BASE


ACCOUNT_ID = "78675700"
APP_ID = "10"


class _FakeCdpClient:
    launch_options: dict[str, str] = {}
    terminated: list[str] = []
    compat_tool: dict[str, str] = {}
    app_details_by_id: dict[str, SteamAppDetails] = {}
    fail = False

    def __init__(self, **kwargs):
        if type(self).fail:
            raise manager_module.SteamCdpError("no endpoint")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def app_launch_options(self, app_id, **kwargs):
        return type(self).launch_options.get(app_id)

    def app_details(self, app_id, **kwargs):
        details = type(self).app_details_by_id.get(str(app_id))
        if details is None:
            return None
        tool_name = type(self).compat_tool.get(str(app_id), details.compat_tool_name)
        return SteamAppDetails(
            launch_options=type(self).launch_options.get(str(app_id), ""),
            compat_tool_name=tool_name,
            compat_tool_display_name=(
                "Proton Experimental"
                if tool_name == "proton_experimental"
                else tool_name
            ),
            compat_tool_priority=details.compat_tool_priority,
            platforms=details.platforms,
        )

    def app_details_many(self, app_ids, **kwargs):
        return {
            app_id: details
            for app_id in app_ids
            if (details := self.app_details(app_id, **kwargs)) is not None
        }

    def set_app_launch_options(self, app_id, value, **kwargs):
        type(self).launch_options[app_id] = value
        return True

    def terminate_app_supported(self):
        return True

    def terminate_app(self, app_id):
        type(self).terminated.append(str(app_id))

    def compat_tool_selection_supported(self):
        return True

    def available_compat_tools(self, app_id):
        return (
            ("proton_experimental", "Proton Experimental"),
            ("GE-Proton10-34", "GE-Proton10-34"),
        )

    def specify_compat_tool(self, app_id, tool_name):
        type(self).compat_tool[str(app_id)] = str(tool_name)


@pytest.fixture()
def steam_home(tmp_path: Path) -> Path:
    root = tmp_path / ".local" / "share" / "Steam"
    steamapps = root / "steamapps"
    steamapps.mkdir(parents=True)
    (root / "config").mkdir()
    (root / "userdata" / ACCOUNT_ID / "config").mkdir(parents=True)
    (steamapps / f"appmanifest_{APP_ID}.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"10"\n\t"name"\t\t"Test Game"\n'
        '\t"StateFlags"\t\t"4"\n\t"installdir"\t\t"TestGame"\n}\n',
        encoding="utf-8",
    )
    (root / "config" / "loginusers.vdf").write_text(
        '"users"\n{\n\t"%d"\n\t{\n\t\t"AccountName"\t\t"jan"\n'
        '\t\t"PersonaName"\t\t"jan.pietek"\n\t\t"MostRecent"\t\t"1"\n'
        '\t\t"Timestamp"\t\t"1"\n\t}\n}\n' % (STEAMID64_BASE + int(ACCOUNT_ID)),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def manager(steam_home: Path, tmp_path: Path, monkeypatch) -> SteamIntegrationManager:
    _FakeCdpClient.launch_options = {APP_ID: "gamemoderun %command%"}
    _FakeCdpClient.terminated = []
    _FakeCdpClient.compat_tool = {}
    _FakeCdpClient.app_details_by_id = {
        APP_ID: SteamAppDetails(
            launch_options="gamemoderun %command%",
            compat_tool_name="proton_experimental",
            compat_tool_display_name="Proton Experimental",
            compat_tool_priority=75,
            platforms=("windows", "linux"),
        )
    }
    _FakeCdpClient.fail = False
    monkeypatch.setattr(manager_module, "SteamCdpClient", _FakeCdpClient)
    monkeypatch.setattr(manager_module, "steam_running", lambda: True)
    return SteamIntegrationManager(
        home=steam_home,
        settings_path=tmp_path / "steam-game-settings.json",
    )


def test_refresh_merges_library_settings_and_launch_options(manager) -> None:
    rows = manager.refresh()
    assert [row.game.app_id for row in rows] == [APP_ID]
    assert rows[0].launch_options == "gamemoderun %command%"
    assert rows[0].setting.mode == GAME_MODE_ADAPTIVE
    assert rows[0].setting.enabled is False
    assert rows[0].setting.overlay is False
    # No explicit config.vdf override exists, but Steam's API reports the
    # effective default Proton. Absence of an override must not mean native.
    assert rows[0].game.compat_tool == ""
    assert rows[0].game.effective_compat_tool == "proton_experimental"
    assert rows[0].game.is_proton
    assert not rows[0].game.is_native_linux


def test_library_uses_one_batch_and_keeps_cached_details_for_missing_apps(manager, monkeypatch):
    from dataclasses import replace

    game = manager.refresh()[0].game
    other = replace(game, app_id="20")
    fresh = SteamAppDetails(
        launch_options='env TITLE="other game" %command%',
        compat_tool_name="GE-Proton10-34", compat_tool_display_name="GE-Proton10-34",
        compat_tool_priority=75, platforms=("windows",),
    )
    calls = []

    def read_batch(_client, app_ids):
        calls.append(tuple(app_ids))
        return {"20": fresh}

    monkeypatch.setattr(_FakeCdpClient, "app_details_many", read_batch)
    games = manager._read_all_app_details((game, other))
    assert calls == [(APP_ID, "20")]
    assert manager._launch_options[APP_ID] == "gamemoderun %command%"
    assert manager._launch_options["20"] == fresh.launch_options
    assert games[0].effective_compat_tool == "proton_experimental"
    assert games[1].effective_compat_tool == fresh.compat_tool_name


def test_refresh_marks_native_only_when_steam_api_reports_no_compat_tool(
    manager,
) -> None:
    _FakeCdpClient.app_details_by_id[APP_ID] = SteamAppDetails(
        launch_options="gamemoderun %command%",
        compat_tool_name="",
        compat_tool_display_name="",
        compat_tool_priority=0,
        platforms=("windows", "linux"),
    )

    game = manager.refresh()[0].game

    assert game.runtime_known
    assert game.is_native_linux
    assert not game.is_proton


def test_library_scan_writes_nothing_for_an_unwrapped_game(manager, tmp_path) -> None:
    """The scan is a read. A missing entry already means "disabled, Adaptive
    preselected", so materialising it would only make opening a tab write
    everyone's settings file."""
    rows = manager.refresh()

    assert rows[0].launch_options == "gamemoderun %command%"
    assert _FakeCdpClient.launch_options[APP_ID] == rows[0].launch_options
    assert rows[0].setting.enabled is False
    assert rows[0].setting.mode == GAME_MODE_ADAPTIVE
    assert not (tmp_path / "steam-game-settings.json").exists()


def test_bulk_enable_and_disable_all_games(manager, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(manager, "_watched_running_app_ids", lambda: frozenset())
    manager.refresh()

    result = manager.set_all_games_enabled([APP_ID], True)

    assert result.ok
    assert "enabled for 1 game" in result.message
    assert "PENGUIN_BURNER" in _FakeCdpClient.launch_options[APP_ID]
    stored = load_steam_game_settings(tmp_path / "steam-game-settings.json")
    setting = stored[ACCOUNT_ID][APP_ID]
    assert setting.enabled is True
    # Bulk enable never turns the overlay on.
    assert setting.overlay is False

    result = manager.set_all_games_enabled([APP_ID], False)

    assert result.ok
    assert _FakeCdpClient.launch_options[APP_ID] == "gamemoderun %command%"
    stored = load_steam_game_settings(tmp_path / "steam-game-settings.json")
    assert stored[ACCOUNT_ID][APP_ID].enabled is False


def test_bulk_apply_hot_reapplies_only_watched_running_games(
    manager,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        manager, "_watched_running_app_ids", lambda: frozenset({APP_ID})
    )
    reapplied = []
    monkeypatch.setattr(manager, "hot_reapply", lambda app_id: reapplied.append(app_id))
    manager.refresh()

    manager.set_all_games_enabled([APP_ID], True)

    assert reapplied == [APP_ID]


def test_bulk_overlay_show_updates_wrapper_flag(manager, monkeypatch) -> None:
    monkeypatch.setattr(manager, "_watched_running_app_ids", lambda: frozenset())
    manager.refresh()
    manager.set_game_enabled(APP_ID, True)

    result = manager.set_all_games_overlay([APP_ID], True)

    assert result.ok
    assert "--pb-overlay=1" in _FakeCdpClient.launch_options[APP_ID]


def test_library_scan_adopts_existing_wrapper_as_enabled_adaptive_choice(
    manager,
    tmp_path,
) -> None:
    """A wrapped launch line is user intent recorded in Steam's own config;
    after a lost settings file (reinstall, migrated home) the plain scan
    recreates the entry, or the wrapper runs with no profile behind it."""
    _FakeCdpClient.launch_options[APP_ID] = (
        "gamemoderun PENGUIN_BURNER --pb-overlay=1 %command%"
    )

    rows = manager.refresh()

    assert rows[0].setting.enabled is True
    assert rows[0].setting.mode == GAME_MODE_ADAPTIVE
    assert rows[0].setting.overlay is True
    stored = load_steam_game_settings(tmp_path / "steam-game-settings.json")
    assert stored[ACCOUNT_ID][APP_ID] == rows[0].setting


def test_library_scan_does_not_overwrite_existing_game_choice(manager, tmp_path) -> None:
    manager.refresh()
    manager.set_game_enabled(APP_ID, True)
    manager.set_game_mode(APP_ID, "balanced")

    rows = manager.refresh()

    assert rows[0].setting.mode == "balanced"
    assert rows[0].launch_options == (
        "gamemoderun PENGUIN_BURNER --pb-overlay=0 %command%"
    )


def test_set_mode_injects_and_persists(manager, tmp_path) -> None:
    manager.refresh()
    manager.set_game_enabled(APP_ID, True)
    result = manager.set_game_mode(APP_ID, "balanced")
    assert result.ok
    assert result.launch_options == (
        "gamemoderun PENGUIN_BURNER --pb-overlay=0 %command%"
    )
    assert _FakeCdpClient.launch_options[APP_ID] == result.launch_options
    stored = load_steam_game_settings(tmp_path / "steam-game-settings.json")
    setting = stored[ACCOUNT_ID][APP_ID]
    assert setting.mode == "balanced"
    assert setting.original_launch_options == "gamemoderun %command%"


def test_overlay_toggle_updates_tokens(manager) -> None:
    manager.refresh()
    manager.set_game_enabled(APP_ID, True)
    manager.set_game_mode(APP_ID, "adaptive")
    result = manager.set_game_overlay(APP_ID, True)
    assert result.ok
    assert result.launch_options == (
        "gamemoderun PENGUIN_BURNER --pb-overlay=1 %command%"
    )


def test_overlay_off_keeps_penguin_burner_wrapper_active(manager, tmp_path) -> None:
    manager.refresh()
    manager.set_game_enabled(APP_ID, True)
    enabled = manager.set_game_overlay(APP_ID, True)
    assert enabled.ok

    disabled = manager.set_game_overlay(APP_ID, False)

    assert disabled.ok
    assert disabled.launch_options == (
        "gamemoderun PENGUIN_BURNER --pb-overlay=0 "
        "--pb-ingame-latency=1 %command%"
    )
    stored = load_steam_game_settings(tmp_path / "steam-game-settings.json")
    setting = stored[ACCOUNT_ID][APP_ID]
    assert setting.mode == GAME_MODE_ADAPTIVE
    assert setting.enabled is True
    assert setting.overlay is False
    assert setting.active is True


def test_proton_selection_uses_steam_and_default_is_not_forced(manager) -> None:
    assert manager.available_compat_tools(APP_ID)[0] == (
        "proton_experimental",
        "Proton Experimental",
    )
    result = manager.set_game_compat_tool(APP_ID, "GE-Proton10-34")
    assert result.ok
    assert _FakeCdpClient.compat_tool[APP_ID] == "GE-Proton10-34"

    result = manager.set_game_compat_tool(APP_ID, "")
    assert result.ok
    assert _FakeCdpClient.compat_tool[APP_ID] == ""


def test_disabling_penguin_burner_restores_original_command(manager, tmp_path) -> None:
    manager.refresh()
    manager.set_game_enabled(APP_ID, True)
    manager.set_game_mode(APP_ID, "balanced")
    result = manager.set_game_enabled(APP_ID, False)
    assert result.ok
    assert _FakeCdpClient.launch_options[APP_ID] == "gamemoderun %command%"
    stored = load_steam_game_settings(tmp_path / "steam-game-settings.json")
    assert stored[ACCOUNT_ID][APP_ID].enabled is False


def test_stock_choice_persists_per_game(manager, tmp_path) -> None:
    """Stock is a first-class per-game mode: the system-wide profile stays
    tuned while this game pins the factory GPU state. It must round-trip,
    not silently migrate to Adaptive (which read as the combo snapping
    back when the user picked Stock)."""
    manager.refresh()
    manager.set_game_enabled(APP_ID, True)
    result = manager.set_game_mode(APP_ID, GAME_MODE_STOCK)
    assert result.ok
    assert result.launch_options == (
        "gamemoderun PENGUIN_BURNER --pb-overlay=0 %command%"
    )
    stored = load_steam_game_settings(tmp_path / "steam-game-settings.json")
    assert stored[ACCOUNT_ID][APP_ID].mode == GAME_MODE_STOCK


def test_raw_edit_validates_and_syncs_setting(manager, tmp_path) -> None:
    manager.refresh()
    manager.set_game_enabled(APP_ID, True)
    manager.set_game_mode(APP_ID, "balanced")
    bad = manager.set_raw_launch_options(APP_ID, '%command% "broken')
    assert not bad.ok and "unbalanced" in bad.message
    good = manager.set_raw_launch_options(
        APP_ID, "mangohud PENGUIN_BURNER --pb-overlay=1 %command%"
    )
    assert good.ok
    stored = load_steam_game_settings(tmp_path / "steam-game-settings.json")
    assert stored[ACCOUNT_ID][APP_ID].overlay


def test_raw_edit_under_overlay_keeps_adaptive_markers_enabled(manager, tmp_path) -> None:
    """Injection omits the latency flag while the overlay is on, so a raw edit
    of such a line must not read the flag's absence as the user opting out."""
    manager.refresh()
    manager.set_game_enabled(APP_ID, True)
    manager.set_game_overlay(APP_ID, True)

    result = manager.set_raw_launch_options(
        APP_ID, "PENGUIN_BURNER --pb-overlay=1 %command% -novid"
    )

    assert result.ok
    stored = load_steam_game_settings(tmp_path / "steam-game-settings.json")
    assert stored[ACCOUNT_ID][APP_ID].ingame_latency is True
    # With the overlay off the line is the whole truth again.
    off = manager.set_raw_launch_options(
        APP_ID, "PENGUIN_BURNER --pb-overlay=0 %command% -novid"
    )
    assert off.ok
    stored = load_steam_game_settings(tmp_path / "steam-game-settings.json")
    assert stored[ACCOUNT_ID][APP_ID].ingame_latency is False


def test_raw_edit_removing_wrapper_deactivates_mode(manager, tmp_path) -> None:
    manager.refresh()
    manager.set_game_enabled(APP_ID, True)
    manager.set_game_mode(APP_ID, "balanced")
    result = manager.set_raw_launch_options(APP_ID, "gamemoderun %command%")
    assert result.ok
    stored = load_steam_game_settings(tmp_path / "steam-game-settings.json")
    assert stored[ACCOUNT_ID][APP_ID].enabled is False
    assert stored[ACCOUNT_ID][APP_ID].mode == GAME_MODE_ADAPTIVE


def test_a_write_refuses_until_the_games_launch_options_were_read(manager) -> None:
    """No cache entry means the game's last read failed (a per-app CDP timeout
    leaves the others populated). Composing a new line from "" would clobber
    the user's real command and record "" as the original to restore later."""
    result = manager.set_game_enabled(APP_ID, True)

    assert not result.ok
    assert "could not be read" in result.message


def test_write_blocked_while_steam_runs_without_cdp(manager) -> None:
    manager.refresh()
    _FakeCdpClient.fail = True
    result = manager.set_game_enabled(APP_ID, True)
    assert not result.ok and "live apply" in result.message


def test_write_falls_back_to_disk_when_steam_stopped(
    manager, steam_home, monkeypatch
) -> None:
    manager.refresh()
    _FakeCdpClient.fail = True
    monkeypatch.setattr(manager_module, "steam_running", lambda: False)
    real_rewrite = manager_module.rewrite_launch_options
    verify_delays: list[float] = []

    def rewrite_without_stopped_client_delay(**kwargs):
        verify_delays.append(kwargs["verify_delay_s"])
        return real_rewrite(**kwargs)

    monkeypatch.setattr(
        manager_module,
        "rewrite_launch_options",
        rewrite_without_stopped_client_delay,
    )
    localconfig = (
        steam_home
        / ".local"
        / "share"
        / "Steam"
        / "userdata"
        / ACCOUNT_ID
        / "config"
        / "localconfig.vdf"
    )
    localconfig.write_text(
        '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n'
        '\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"apps"\n\t\t\t\t{\n'
        f'\t\t\t\t\t"{APP_ID}"\n'
        "\t\t\t\t\t{\n"
        '\t\t\t\t\t\t"LaunchOptions"\t\t"gamemoderun %command%"\n'
        "\t\t\t\t\t}\n"
        "\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n",
        encoding="utf-8",
    )
    result = manager.set_game_enabled(APP_ID, True)
    assert result.ok and "config" in result.message
    assert verify_delays == [0.0]
    assert (
        "PENGUIN_BURNER --pb-overlay=0 --pb-ingame-latency=1 %command%"
        in localconfig.read_text(
            encoding="utf-8"
        )
    )


def test_hot_reapply_none_when_game_not_running(manager, monkeypatch) -> None:
    import runtime.daemon_client as daemon_client

    monkeypatch.setattr(
        daemon_client, "daemon_status", lambda **kwargs: {"state": "idle"}
    )
    assert manager.hot_reapply(APP_ID) is None


@pytest.mark.parametrize("enabled", [False, True])
def test_overlay_change_updates_live_override(manager, monkeypatch, tmp_path, enabled) -> None:
    from overlay.state import OVERLAY_OVERRIDE_ENV

    override = tmp_path / "overlay-override"
    monkeypatch.setenv(OVERLAY_OVERRIDE_ENV, str(override))
    monkeypatch.setattr(manager, "running_game_ids", lambda: frozenset({APP_ID}))
    manager.refresh()
    assert manager.set_game_enabled(APP_ID, True).ok
    assert manager.set_game_overlay(APP_ID, enabled).ok
    result = manager.hot_reapply_overlay(APP_ID)
    assert result is not None and result.ok
    assert override.read_text() == ("1" if enabled else "0")


@pytest.mark.parametrize("running", [None, frozenset(), frozenset({"other-game"})])
def test_overlay_edit_for_idle_game_does_not_touch_live_override(
    manager, monkeypatch, tmp_path, running
) -> None:
    from overlay.state import OVERLAY_OVERRIDE_ENV

    override = tmp_path / "overlay-override"
    override.write_text("1")
    monkeypatch.setenv(OVERLAY_OVERRIDE_ENV, str(override))
    monkeypatch.setattr(manager, "running_game_ids", lambda: running)
    assert manager.hot_reapply_overlay(APP_ID) is None
    assert override.read_text() == "1"


def test_bulk_overlay_updates_live_visibility_without_reapplying_profile(
    manager, monkeypatch, tmp_path
) -> None:
    from overlay.state import OVERLAY_OVERRIDE_ENV

    override = tmp_path / "overlay-override"
    monkeypatch.setenv(OVERLAY_OVERRIDE_ENV, str(override))
    monkeypatch.setattr(manager, "running_game_ids", lambda: frozenset({APP_ID}))
    manager.refresh()
    assert manager.set_game_enabled(APP_ID, True).ok
    assert manager.set_all_games_overlay([APP_ID], True).ok
    assert override.read_text() == "1"
    monkeypatch.setattr("overlay.state.write_overlay_override", lambda _enabled: False)
    result = manager.set_all_games_overlay([APP_ID], False)
    assert not result.ok
    assert "live visibility update failed" in result.message


def test_hot_reapply_pushes_profile_to_running_game(manager, monkeypatch) -> None:
    import runtime.daemon_client as daemon_client
    import integrations.steam.game_runtime as game_runtime

    manager.refresh()
    manager.set_game_enabled(APP_ID, True)
    manager.set_game_mode(APP_ID, "balanced")
    monkeypatch.setattr(
        daemon_client,
        "daemon_status",
        lambda **kwargs: {
            "state": "runtime_profile_running",
            "game_runtime": {
                "active": True,
                "watched": [{"pid": 4242, "app_id": APP_ID}],
            },
        },
    )
    monkeypatch.setattr(
        game_profile, "read_auto_uv_profiles", lambda: []
    )
    monkeypatch.setattr(
        game_profile,
        "resolve_profile_tier_profiles",
        lambda profiles, **_kwargs: {"balanced": {"profile_id": "profile-9"}},
    )
    monkeypatch.setattr(
        game_runtime.DaemonGpuClient,
        "discover_identities",
        classmethod(
            lambda cls: [SimpleNamespace(index=0, uuid="GPU-only")]
        ),
    )
    calls = []

    def fake_start(argv, *, watch_pid, app_id="", **kwargs):
        calls.append((list(argv), watch_pid, app_id))
        return {"started": True}

    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", fake_start)
    result = manager.hot_reapply(APP_ID)
    assert result is not None and result.ok
    assert calls == [
        (["--auto-uv-profile", "profile-9", "--gpu-index", "0"], 4242, APP_ID)
    ]


def test_hot_reapply_legacy_default_migrates_to_adaptive(manager, monkeypatch) -> None:
    import runtime.daemon_client as daemon_client
    import integrations.steam.game_runtime as game_runtime

    manager.refresh()
    manager.set_game_enabled(APP_ID, True)
    manager.set_game_mode(APP_ID, GAME_MODE_DEFAULT)
    monkeypatch.setattr(
        daemon_client,
        "daemon_status",
        lambda **kwargs: {
            "game_runtime": {
                "active": True,
                "watched": [{"pid": 4242, "app_id": APP_ID}],
                "standing_runtime_mode": "adaptive",
                "standing_profile_id": "performance-9",
            }
        },
    )
    calls = []

    def fake_start(argv, *, watch_pid, app_id="", **kwargs):
        calls.append((list(argv), watch_pid, app_id))
        return {"started": True}

    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", fake_start)
    monkeypatch.setattr(game_profile, "read_auto_uv_profiles", lambda: [])
    monkeypatch.setattr(
        game_profile,
        "resolve_profile_tier_profiles",
        lambda _profiles, **_kwargs: {
            "performance": {"profile_id": "performance-9"}
        },
    )
    monkeypatch.setattr(
        game_runtime.DaemonGpuClient,
        "discover_identities",
        classmethod(
            lambda cls: [SimpleNamespace(index=0, uuid="GPU-only")]
        ),
    )

    result = manager.hot_reapply(APP_ID)

    assert result is not None and result.ok
    assert calls == [
        (
            [
                "--auto-uv-profile",
                "performance-9",
                "--adaptive-auto-uv",
                "--gpu-index",
                "0",
            ],
            4242,
            APP_ID,
        )
    ]


def test_hot_reapply_tolerates_grace_window_exit(manager, monkeypatch) -> None:
    import integrations.steam.game_runtime as game_runtime
    import runtime.daemon_client as daemon_client

    manager.refresh()
    manager.set_game_enabled(APP_ID, True)
    manager.set_game_mode(APP_ID, "adaptive")
    monkeypatch.setattr(
        daemon_client,
        "daemon_status",
        lambda **kwargs: {
            "game_runtime": {
                "active": True,
                "watched": [{"pid": 4242, "app_id": APP_ID}],
                "standing_runtime_mode": "adaptive",
                "standing_profile_id": "performance-9",
            }
        },
    )
    monkeypatch.setattr(
        game_runtime.DaemonGpuClient,
        "discover_identities",
        classmethod(lambda cls: [SimpleNamespace(index=0, uuid="GPU-only")]),
    )

    def fake_start(argv, **kwargs):
        raise RuntimeError("watch_pid 4242 is not a running process")

    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", fake_start)
    result = manager.hot_reapply(APP_ID)
    assert result is not None and result.ok
    assert "next launch" in result.message


def test_hot_reapply_targets_saved_gpu_for_stock(manager, monkeypatch) -> None:
    from types import SimpleNamespace

    import integrations.steam.game_runtime as game_runtime
    import runtime.daemon_client as daemon_client

    manager.refresh()
    manager.set_game_enabled(APP_ID, True)
    manager.set_game_mode(APP_ID, GAME_MODE_STOCK)
    manager.set_game_gpu(APP_ID, "GPU-B")
    monkeypatch.setattr(
        daemon_client,
        "daemon_status",
        lambda **kwargs: {
            "game_runtime": {
                "active": True,
                "watched": [{"pid": 4242, "app_id": APP_ID}],
            }
        },
    )
    monkeypatch.setattr(
        game_runtime.DaemonGpuClient,
        "discover_identities",
        classmethod(
            lambda cls: [
                SimpleNamespace(index=0, uuid="GPU-A"),
                SimpleNamespace(index=1, uuid="GPU-B"),
            ]
        ),
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        daemon_client,
        "start_game_runtime_profile",
        lambda argv, **kwargs: calls.append(list(argv)) or {"started": True},
    )

    result = manager.hot_reapply(APP_ID)

    assert result is not None and result.ok
    assert calls == [
        ["--auto-uv-profile", "__stock__", "--gpu-index", "1"]
    ]


def test_hot_reapply_reports_ignored_concurrent_game(manager, monkeypatch) -> None:
    import integrations.steam.game_runtime as game_runtime
    import runtime.daemon_client as daemon_client

    manager.refresh()
    manager.set_game_enabled(APP_ID, True)
    manager.set_game_mode(APP_ID, GAME_MODE_STOCK)
    monkeypatch.setattr(
        daemon_client,
        "daemon_status",
        lambda **kwargs: {
            "game_runtime": {
                "active": True,
                "watched": [{"pid": 4242, "app_id": APP_ID}],
            }
        },
    )
    monkeypatch.setattr(
        game_runtime.DaemonGpuClient,
        "discover_identities",
        classmethod(lambda cls: [SimpleNamespace(index=0, uuid="GPU-only")]),
    )
    monkeypatch.setattr(
        daemon_client,
        "start_game_runtime_profile",
        lambda *args, **kwargs: {
            "started": False,
            "ignored": True,
            "reason": "first-game-runtime-active",
        },
    )

    result = manager.hot_reapply(APP_ID)

    assert result is not None and not result.ok
    assert "first-game-runtime-active" in result.message


def test_stop_game_terminates_via_cdp(manager) -> None:
    result = manager.stop_game(APP_ID)

    assert result.ok
    assert _FakeCdpClient.terminated == [APP_ID]


def test_stop_game_needs_live_apply(manager, monkeypatch) -> None:
    _FakeCdpClient.fail = True
    monkeypatch.setattr(manager_module, "cdp_available", lambda **kwargs: False)

    result = manager.stop_game(APP_ID)

    assert not result.ok
    assert "live apply" in result.message
    assert _FakeCdpClient.terminated == []


def test_stop_game_reports_steam_not_running(manager, monkeypatch) -> None:
    _FakeCdpClient.fail = True
    monkeypatch.setattr(manager_module, "steam_running", lambda: False)

    result = manager.stop_game(APP_ID)

    assert not result.ok
    assert result.message == "Steam is not running"


def test_stop_game_surfaces_real_cdp_error_when_live(manager, monkeypatch) -> None:
    # CDP is up (live apply initialized) but the call itself failed: the user
    # must see the actual error, not be told to initialize again.
    _FakeCdpClient.fail = True
    monkeypatch.setattr(manager_module, "cdp_available", lambda **kwargs: True)

    result = manager.stop_game(APP_ID)

    assert not result.ok
    assert "no endpoint" in result.message


def test_stop_game_rejects_bad_app_id(manager) -> None:
    assert not manager.stop_game("rm -rf /").ok
    assert _FakeCdpClient.terminated == []


def test_enabling_reads_the_original_off_the_live_line_not_a_stale_snapshot(
    manager,
) -> None:
    """A stored "original" is only the original while our wrapper is still in.

    On an unwrapped line the live command is the original: the user may have
    edited it in Steam since, or pulled our wrapper out by hand. And a snapshot
    recorded from a bad read -- a launch line with quotes in it used to parse
    as `LD_PRELOAD=\\` -- would otherwise be handed back to them as their own
    command the next time the wrapper came out.
    """
    from dataclasses import replace

    from integrations.steam.launch_options import injection_state
    from integrations.steam.settings import SteamGameSetting

    live = 'LD_PRELOAD="" PROTON_VKD3D_BRATAN=1 game-performance %command% --nologo'
    manager._launch_options["620"] = live
    stale = SteamGameSetting(enabled=False, original_launch_options="OLD=1 %command%")
    manager._store("620", stale)

    manager._apply("620", replace(stale, enabled=True))

    stored = manager._setting("620")
    assert stored.original_launch_options == live
    # And the line Steam now carries is that same command, wrapped.
    written = manager._launch_options["620"]
    assert injection_state(written).wrapped is True
