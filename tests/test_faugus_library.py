"""Reading the Faugus Launcher library out of its games.json."""

from __future__ import annotations

import json

import pytest

from integrations.faugus.library import read_faugus_games
from integrations.faugus.paths import faugus_installed, games_path


@pytest.fixture(autouse=True)
def _faugus_installed_natively(native_launcher_installed) -> None:
    """Which tree wins depends on the host, so these tests name theirs."""


GAME = {
    "gameid": "expedition-33",
    "title": "Expedition 33",
    "path": "/games/e33/drive_c/Game/e33.exe",
    "prefix": "/games/e33",
    "launch_arguments": "game-performance",
    "runner": "Proton-CachyOS (System)",
    "playtime": 7200,
    "hidden": False,
}


def _faugus(tmp_path, games, *, flatpak: bool = False):
    if flatpak:
        base = tmp_path / ".var/app/io.github.Faugus.faugus-launcher"
        data = base / "data/faugus-launcher"
        config = base / "config/faugus-launcher"
    else:
        data = tmp_path / ".local/share/faugus-launcher"
        config = tmp_path / ".config/faugus-launcher"
    data.mkdir(parents=True)
    config.mkdir(parents=True)
    (data / "games.json").write_text(json.dumps(games))
    (config / "config.json").write_text(json.dumps({}))
    return data


def test_reads_a_game_with_its_runner_and_playtime(tmp_path):
    _faugus(tmp_path, [GAME])

    (game,) = read_faugus_games(tmp_path)

    assert game.game_id == "expedition-33"
    assert game.display_name == "Expedition 33"
    assert game.runner_label == "Proton-CachyOS (System)"
    # Faugus totals playtime in seconds; the library tab wants hours.
    assert game.playtime_hours == 2.0
    assert game.ready


def test_playtime_is_read_in_seconds_not_minutes(tmp_path):
    _faugus(tmp_path, [{**GAME, "playtime": 90}])

    (game,) = read_faugus_games(tmp_path)

    assert game.playtime_hours == 0.025


def test_missing_executable_and_last_played_are_unknown(tmp_path):
    """Missing local metadata must not turn into a fabricated recent date."""
    _faugus(tmp_path, [GAME])

    (game,) = read_faugus_games(tmp_path)

    assert game.last_played == 0
    assert getattr(game, "installed_at", 0) == 0


def test_hidden_games_are_left_out_unless_asked_for(tmp_path):
    _faugus(tmp_path, [GAME, {**GAME, "gameid": "tucked-away", "hidden": True}])

    assert [game.game_id for game in read_faugus_games(tmp_path)] == ["expedition-33"]
    assert len(read_faugus_games(tmp_path, include_hidden=True)) == 2


def test_a_proton_runner_is_not_native_but_linux_native_is(tmp_path):
    _faugus(tmp_path, [GAME, {**GAME, "gameid": "elf", "runner": "Linux-Native"}])

    native = {game.game_id: game.is_native for game in read_faugus_games(tmp_path)}

    assert native == {"expedition-33": False, "elf": True}


def test_the_cover_is_preferred_over_the_icon(tmp_path):
    cover = tmp_path / "cover.png"
    icon = tmp_path / "icon.ico"
    cover.write_bytes(b"")
    icon.write_bytes(b"")
    _faugus(tmp_path, [{**GAME, "cover": str(cover), "icon": str(icon)}])

    (game,) = read_faugus_games(tmp_path)

    assert game.art_path == cover


def test_art_that_is_not_on_disk_is_not_reported(tmp_path):
    _faugus(tmp_path, [{**GAME, "cover": str(tmp_path / "gone.png")}])

    (game,) = read_faugus_games(tmp_path)

    assert game.art_path is None


def test_an_entry_without_an_id_is_skipped(tmp_path):
    _faugus(tmp_path, [{"title": "Nameless"}, GAME])

    assert [game.game_id for game in read_faugus_games(tmp_path)] == ["expedition-33"]


def test_a_library_that_is_not_a_list_reads_as_empty(tmp_path):
    _faugus(tmp_path, {"games": [GAME]})

    assert read_faugus_games(tmp_path) == ()


def test_the_flatpak_tree_is_found_when_the_native_one_is_absent(tmp_path):
    _faugus(tmp_path, [GAME], flatpak=True)

    assert faugus_installed(tmp_path)
    assert ".var/app" in str(games_path(tmp_path))
    assert [game.game_id for game in read_faugus_games(tmp_path)] == ["expedition-33"]


def test_the_native_tree_wins_when_both_are_present(tmp_path):
    _faugus(tmp_path, [GAME], flatpak=True)
    _faugus(tmp_path, [{**GAME, "gameid": "native-one"}])

    assert [game.game_id for game in read_faugus_games(tmp_path)] == ["native-one"]


def test_a_machine_without_faugus_reports_none_installed(tmp_path):
    assert not faugus_installed(tmp_path)
    assert read_faugus_games(tmp_path) == ()


def test_a_steam_runner_entry_reports_no_playtime_of_its_own(tmp_path):
    """Faugus reads those live from Steam, so games.json holds no total."""
    _faugus(
        tmp_path,
        [GAME, {**GAME, "gameid": "via-steam", "runner": "Steam", "playtime": 999}],
    )

    hours = {game.game_id: game.playtime_hours for game in read_faugus_games(tmp_path)}

    assert hours == {"expedition-33": 2.0, "via-steam": 0.0}


@pytest.mark.parametrize("executable", [
    "EA Desktop/EALauncher.exe", "EA Desktop/EADesktop.exe",
    "Battle.net/Battle.net.exe", "Ubisoft Game Launcher/UbisoftConnect.exe",
    "Epic Games/EpicGamesLauncher.exe", "GOG Galaxy/GalaxyClient.exe",
    "Amazon Games/Amazon Games.exe", "Wargaming.net/GameCenter/wgc.exe",
    "Rockstar Games/Launcher/Launcher.exe",
])
def test_store_clients_are_excluded_even_when_renamed(executable):
    document = [{**GAME, "title": "Custom name", "path": f"/prefix/{executable}"}, GAME]
    assert [g.name for g in read_faugus_games(document=document)] == [GAME["title"]]


def test_game_launcher_executable_and_shared_ea_prefix_are_kept():
    document = [
        {**GAME, "gameid": "ea-app", "path": "/ea/EALauncher.exe"},
        {**GAME, "gameid": "nfs", "path": "/ea/EA Games/NFS/NFS11Remastered.exe"},
        {**GAME, "gameid": "game", "path": "/games/Game/Launcher.exe"},
    ]
    assert [g.game_id for g in read_faugus_games(document=document)] == ["nfs", "game"]


def test_install_estimate_uses_birth_time_and_reaches_shared_sort(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from integrations.faugus import library
    from integrations.faugus.library_source import FaugusLibrarySource
    from integrations.launchers.game_settings import LauncherGameSetting
    from integrations.launchers.wrapper_manager import (
        SOURCE_GAME,
        EffectiveCommand,
        LauncherGameRow,
    )

    executable = tmp_path / "nfs.exe"
    executable.touch()
    monkeypatch.setattr(library.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="1790278954\n"))
    (game,) = read_faugus_games(document=[{**GAME, "path": str(executable)}])
    source = FaugusLibrarySource(manager=object())
    source._rows = (LauncherGameRow(game=game, setting=LauncherGameSetting(),
                                   effective=EffectiveCommand(value="", source=SOURCE_GAME)),)
    assert source.games()[0].installed_at == 1790278954


@pytest.mark.parametrize("stamp", ["0", "-1", "unknown"])
def test_missing_birth_time_stays_unknown(tmp_path, monkeypatch, stamp):
    from types import SimpleNamespace

    from integrations.faugus import library
    executable = tmp_path / "game.exe"
    executable.touch()
    monkeypatch.setattr(library.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=stamp))
    assert read_faugus_games(document=[{**GAME, "path": str(executable)}])[0].installed_at == 0


@pytest.mark.parametrize("stamp, expected", [
    ("2026-09-24T19:00:00+00:00", 1790276400),
    ("", 0), (None, 0), ("bad timestamp", 0), ("1900-01-01", 0),
])
def test_reads_saved_last_played(stamp, expected):
    assert read_faugus_games(document=[{**GAME, "last_played": stamp}])[0].last_played == expected


def test_birth_time_probe_failure_keeps_game_visible(tmp_path, monkeypatch):
    from integrations.faugus import library
    executable = tmp_path / "game.exe"
    executable.touch()
    def unavailable(*args, **kwargs):
        raise OSError("stat unavailable")
    monkeypatch.setattr(library.subprocess, "run", unavailable)
    (game,) = read_faugus_games(document=[{**GAME, "path": str(executable)}])
    assert game.installed_at == 0
    assert game.name == GAME["title"]


def test_filtered_clients_are_identified_to_the_session_ui(tmp_path):
    from integrations.faugus.library_source import FaugusLibrarySource
    from integrations.faugus.manager import FaugusIntegrationManager

    _faugus(tmp_path, [GAME, {**GAME, 'gameid': 'ea-app', 'path': '/ea/EALauncher.exe'}])
    manager = FaugusIntegrationManager(home=tmp_path)
    manager.read_games()
    source = FaugusLibrarySource(manager=manager)
    assert source.non_game_ids == frozenset({'ea-app'})
    assert GAME['gameid'] not in source.non_game_ids


def test_store_identity_survives_rescan_failure_and_removal(tmp_path, monkeypatch):
    from integrations.faugus import manager as module
    from integrations.faugus.config_store import FaugusConfigError

    data = _faugus(tmp_path, [{**GAME, 'gameid': 'client', 'path': '/ea/EALauncher.exe'}])
    manager = module.FaugusIntegrationManager(home=tmp_path)
    manager.read_games()
    (data / 'games.json').write_text('[]')
    manager.read_games()
    assert manager.non_game_ids == frozenset({'client'})
    with monkeypatch.context() as patch:
        def failed(*args, **kwargs):
            raise FaugusConfigError('temporarily unreadable')
        patch.setattr(module, 'read_games_document', failed)
        manager.read_games()
        assert manager.non_game_ids == frozenset({'client'})
    (data / 'games.json').write_text(json.dumps([{**GAME, 'gameid': 'client'}]))
    manager.read_games()
    assert manager.non_game_ids == frozenset()


def test_a_client_told_to_launch_a_game_is_that_game():
    """Lutris-style Battle.net entries run the client with a launch argument."""
    client = {**GAME, "gameid": "battlenet", "path": "/bnet/Battle.net.exe"}
    diablo = {**client, "gameid": "diablo-iii", "game_arguments": '--exec="launch D3"'}
    assert [g.game_id for g in read_faugus_games(document=[client, diablo])] == ["diablo-iii"]
