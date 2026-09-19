"""Reading the Faugus Launcher library out of its games.json."""

from __future__ import annotations

import json

from integrations.faugus.library import read_faugus_games
from integrations.faugus.paths import config_path, faugus_installed, games_path

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


def test_install_time_and_last_played_are_unknown(tmp_path):
    """Faugus records neither, so both sort last rather than sort wrong."""
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
    assert ".var/app" in str(config_path(tmp_path))
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
