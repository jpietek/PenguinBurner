"""Reading and writing Heroic's wrapperOptions for one game."""

from __future__ import annotations

import json

import pytest

from integrations.heroic.config_store import (
    SOURCE_GLOBAL,
    HeroicConfigError,
    command_entries,
    effective_wrapper_command,
    entries_command,
    read_game_entries,
    write_wrapper_command,
)
from integrations.launchers.wrapper_manager import SOURCE_GAME

WRAPPED = "PENGUIN_BURNER --pb-overlay=1 --pb-game-id=heroic:Turkey"


def _heroic(tmp_path, *, defaults=(), game=None):
    root = tmp_path / ".config" / "heroic"
    (root / "GamesConfig").mkdir(parents=True)
    (root / "config.json").write_text(
        json.dumps({"defaultSettings": {"wrapperOptions": list(defaults)}})
    )
    if game is not None:
        (root / "GamesConfig" / "Turkey.json").write_text(json.dumps(game))
    return root


def _entry(exe, args=""):
    return {"exe": exe, "args": args}


def _game_config(tmp_path) -> dict:
    path = tmp_path / ".config" / "heroic" / "GamesConfig" / "Turkey.json"
    return json.loads(path.read_text())


# -- the table is one command line ---------------------------------------------


def test_the_wrapper_table_reads_as_the_command_it_runs() -> None:
    """Heroic pushes each row's exe and its split args onto one wrapper list."""
    assert entries_command([_entry("gamescope", "-w 2560 --"), _entry("mangohud")]) == (
        "gamescope -w 2560 -- mangohud"
    )


def test_a_row_with_unbalanced_quotes_is_not_claimed() -> None:
    """Heroic's own shlex.split would throw on it too."""
    assert entries_command([_entry("mangohud", '"unclosed')]) == ""


def test_writing_back_keeps_the_rows_the_user_configured() -> None:
    """Our tokens are prepended, so their rows are still the command's tail."""
    rows = [_entry("gamescope", "-w 2560 --"), _entry("mangohud")]

    assert command_entries(f"{WRAPPED} gamescope -w 2560 -- mangohud", rows) == [
        _entry("PENGUIN_BURNER", "--pb-overlay=1 --pb-game-id=heroic:Turkey"),
        *rows,
    ]


def test_removing_our_row_leaves_the_others_untouched() -> None:
    rows = [_entry("PENGUIN_BURNER", "--pb-overlay=1"), _entry("mangohud")]

    assert command_entries("mangohud", rows) == [_entry("mangohud")]


def test_a_hand_written_command_becomes_one_row() -> None:
    """Nothing of the old table survives a command the user typed himself."""
    assert command_entries("gamemoderun mangohud", [_entry("gamescope")]) == [
        _entry("gamemoderun", "mangohud")
    ]


# -- which level answers -------------------------------------------------------


def test_the_games_own_rows_win_over_the_global_ones(tmp_path) -> None:
    """Heroic replaces, never merges: writing the game level drops the global."""
    _heroic(
        tmp_path,
        defaults=[_entry("game-performance")],
        game={"Turkey": {"wrapperOptions": [_entry("mangohud")]}},
    )

    effective = effective_wrapper_command("Turkey", tmp_path)

    assert (effective.value, effective.source) == ("mangohud", SOURCE_GAME)
    assert effective.inherited is False


def test_a_game_with_no_rows_of_its_own_inherits_the_global_ones(tmp_path) -> None:
    _heroic(tmp_path, defaults=[_entry("game-performance")], game={})

    effective = effective_wrapper_command("Turkey", tmp_path)

    assert (effective.value, effective.source) == ("game-performance", SOURCE_GLOBAL)
    assert effective.inherited is True


def test_the_inherited_answer_ignores_the_games_own_level(tmp_path) -> None:
    """What a disable has to hand back, rather than freezing today's value."""
    _heroic(
        tmp_path,
        defaults=[_entry("game-performance")],
        game={"Turkey": {"wrapperOptions": [_entry("mangohud")]}},
    )

    inherited = effective_wrapper_command("Turkey", tmp_path, game_level=False)

    assert (inherited.value, inherited.source) == ("game-performance", SOURCE_GLOBAL)


def test_a_config_that_is_not_json_is_reported_not_overwritten(tmp_path) -> None:
    root = _heroic(tmp_path)
    (root / "GamesConfig" / "Turkey.json").write_text("{not json")

    with pytest.raises(HeroicConfigError):
        read_game_entries("Turkey", tmp_path)


# -- writing -------------------------------------------------------------------


def test_writing_keeps_every_other_setting_in_the_game_config(tmp_path) -> None:
    _heroic(
        tmp_path,
        game={
            "Turkey": {"winePrefix": "/prefixes/bl", "enableEsync": True},
            "version": "v0",
            "explicit": True,
        },
    )

    write = write_wrapper_command("Turkey", "mangohud", tmp_path)

    assert write.ok is True
    document = _game_config(tmp_path)
    assert document["Turkey"]["winePrefix"] == "/prefixes/bl"
    assert document["Turkey"]["enableEsync"] is True
    assert document["Turkey"]["wrapperOptions"] == [_entry("mangohud")]
    assert document["explicit"] is True


def test_a_config_we_create_says_which_schema_heroic_should_read_it_as(
    tmp_path,
) -> None:
    _heroic(tmp_path)

    write_wrapper_command("Turkey", "mangohud", tmp_path)

    assert _game_config(tmp_path)["version"] == "v0"


def test_an_empty_command_hands_the_game_back_to_the_global_rows(tmp_path) -> None:
    """Removing the key, not storing an empty list: inheritance must resume."""
    _heroic(
        tmp_path,
        defaults=[_entry("game-performance")],
        game={"Turkey": {"wrapperOptions": [_entry("mangohud")]}},
    )

    assert write_wrapper_command("Turkey", "", tmp_path).ok is True
    assert "wrapperOptions" not in _game_config(tmp_path)["Turkey"]
    assert effective_wrapper_command("Turkey", tmp_path).value == "game-performance"


def test_a_write_reports_what_actually_landed(tmp_path) -> None:
    """Heroic holds an open settings page in memory and rewrites the file."""
    _heroic(tmp_path)

    write = write_wrapper_command("Turkey", f"{WRAPPED} mangohud", tmp_path)

    assert write.ok is True
    assert write.command == f"{WRAPPED} mangohud"


def test_a_config_handed_back_is_the_file_heroic_left(tmp_path) -> None:
    """Enable then disable must leave no trace, down to the bytes.

    Heroic writes JSON.stringify(config, null, 2) with no trailing newline; a
    writer that formats differently turns every touched game config into a
    diff for whatever reads it next.
    """
    root = _heroic(tmp_path)
    path = root / "GamesConfig" / "Turkey.json"
    original = json.dumps(
        {
            "Turkey": {"winePrefix": "/prefixes/bl", "wrapperOptions": [_entry("mangohud")]},
            "version": "v0",
            "explicit": True,
        },
        indent=2,
    )
    path.write_text(original)

    assert write_wrapper_command("Turkey", f"{WRAPPED} mangohud", tmp_path).ok
    assert write_wrapper_command("Turkey", "mangohud", tmp_path).ok

    assert path.read_text() == original
