"""Writing PenguinBurner's wrapper into a Faugus game's launch arguments."""

from __future__ import annotations

import json

import pytest

from integrations.faugus.config_store import (
    FaugusConfigError,
    effective_launch_arguments,
    read_games_document,
    write_launch_arguments,
)
from integrations.faugus.paths import games_path

WRAPPER = "PENGUIN_BURNER --pb-overlay=1 --pb-game-id=faugus:e33"


def _faugus(tmp_path, games):
    data = tmp_path / ".local/share/faugus-launcher"
    data.mkdir(parents=True)
    (data / "games.json").write_text(
        json.dumps(games, indent=4, ensure_ascii=False), encoding="utf-8"
    )
    return data / "games.json"


def _game(**overrides):
    return {
        "gameid": "e33",
        "title": "Expedition 33",
        "path": "/games/e33.exe",
        "launch_arguments": "game-performance",
        "runner": "Proton-CachyOS (System)",
        "playtime": 10,
        **overrides,
    }


def test_reads_the_launch_arguments_as_the_games_own_level(tmp_path):
    _faugus(tmp_path, [_game()])

    effective = effective_launch_arguments("e33", tmp_path)

    assert effective.value == "game-performance"
    assert effective.source == "game"
    # Faugus has no global launch arguments, so nothing is ever inherited.
    assert not effective.inherited


def test_a_game_that_is_not_in_the_library_has_no_command(tmp_path):
    _faugus(tmp_path, [_game()])

    assert effective_launch_arguments("missing", tmp_path).value == ""


def test_writing_keeps_every_other_field_of_the_entry(tmp_path):
    path = _faugus(tmp_path, [_game()])

    assert write_launch_arguments("e33", f"{WRAPPER} game-performance", tmp_path).ok

    (entry,) = json.loads(path.read_text())
    assert entry["launch_arguments"] == f"{WRAPPER} game-performance"
    assert entry["title"] == "Expedition 33"
    assert entry["playtime"] == 10
    assert entry["runner"] == "Proton-CachyOS (System)"


def test_writing_keeps_the_order_of_the_whole_library(tmp_path):
    path = _faugus(
        tmp_path,
        [_game(gameid="first"), _game(gameid="e33"), _game(gameid="last")],
    )

    assert write_launch_arguments("e33", WRAPPER, tmp_path).ok

    assert [entry["gameid"] for entry in json.loads(path.read_text())] == [
        "first",
        "e33",
        "last",
    ]


def test_removing_the_wrapper_leaves_the_empty_string_faugus_expects(tmp_path):
    path = _faugus(tmp_path, [_game()])

    assert write_launch_arguments("e33", None, tmp_path).ok

    (entry,) = json.loads(path.read_text())
    # Not a missing key: Faugus's own editor writes "" and would put it back.
    assert entry["launch_arguments"] == ""


def test_the_file_keeps_the_shape_faugus_itself_writes(tmp_path):
    path = _faugus(tmp_path, [_game(title="Café Ω")])

    assert write_launch_arguments("e33", WRAPPER, tmp_path).ok

    written = path.read_text(encoding="utf-8")
    assert written == json.dumps(
        json.loads(written), indent=4, ensure_ascii=False
    )
    # json.dump writes no trailing newline, and neither may we.
    assert not written.endswith("\n")
    # ensure_ascii=False, so a non-ASCII title survives as itself.
    assert "Café Ω" in written


def test_writing_to_a_game_faugus_does_not_have_is_refused(tmp_path):
    _faugus(tmp_path, [_game()])

    result = write_launch_arguments("ghost", WRAPPER, tmp_path)

    assert not result.ok
    assert "not in the Faugus library" in result.message


def test_a_library_that_is_not_json_is_reported_not_swallowed(tmp_path):
    data = tmp_path / ".local/share/faugus-launcher"
    data.mkdir(parents=True)
    (data / "games.json").write_text("{not json")

    with pytest.raises(FaugusConfigError):
        read_games_document(tmp_path)

    result = write_launch_arguments("e33", WRAPPER, tmp_path)
    assert not result.ok
    assert "not valid JSON" in result.message


def test_a_library_holding_an_object_is_refused(tmp_path):
    data = tmp_path / ".local/share/faugus-launcher"
    data.mkdir(parents=True)
    (data / "games.json").write_text(json.dumps({"games": []}))

    with pytest.raises(FaugusConfigError):
        read_games_document(tmp_path)


def test_a_write_that_does_not_land_is_reported_as_a_failure(tmp_path, monkeypatch):
    """Faugus rewrites the file when its window saves; say so rather than lie."""
    _faugus(tmp_path, [_game()])

    def overwrite(path, text, **kwargs):
        del text, kwargs
        path.write_text(json.dumps([_game()], indent=4), encoding="utf-8")

    monkeypatch.setattr(
        "integrations.faugus.config_store.atomic_write_text", overwrite
    )

    result = write_launch_arguments("e33", WRAPPER, tmp_path)

    assert not result.ok
    assert result.command == "game-performance"
    assert "Faugus Launcher" in result.message


def test_the_write_goes_to_the_tree_that_exists(tmp_path):
    base = tmp_path / ".var/app/io.github.Faugus.faugus-launcher/data/faugus-launcher"
    base.mkdir(parents=True)
    (base / "games.json").write_text(json.dumps([_game()], indent=4))

    assert write_launch_arguments("e33", WRAPPER, tmp_path).ok
    assert games_path(tmp_path) == base / "games.json"
    assert json.loads((base / "games.json").read_text())[0]["launch_arguments"] == WRAPPER


def test_readback_failure_is_returned_by_the_writer(tmp_path, monkeypatch):
    _faugus(tmp_path, [_game()])
    monkeypatch.setattr(
        "integrations.faugus.config_store.atomic_write_text",
        lambda path, text, **kwargs: path.write_text("{incomplete rewrite"),
    )
    result = write_launch_arguments("e33", WRAPPER, tmp_path)
    assert not result.ok
    assert "not valid JSON" in result.message
