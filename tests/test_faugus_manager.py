"""Enabling and disabling PenguinBurner on a Faugus game."""

from __future__ import annotations

import json

from integrations.faugus.library_source import FaugusLibrarySource
from integrations.faugus.manager import FaugusIntegrationManager
from overlay.wrapper_tokens import overlay_present, wrapper_present


def _faugus(tmp_path, games):
    data = tmp_path / ".local/share/faugus-launcher"
    data.mkdir(parents=True)
    path = data / "games.json"
    path.write_text(json.dumps(games, indent=4, ensure_ascii=False), encoding="utf-8")
    return path


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


def _manager(tmp_path):
    return FaugusIntegrationManager(
        home=tmp_path, settings_path=tmp_path / "pb-settings.json"
    )


def test_enabling_puts_the_wrapper_innermost_after_what_was_there(tmp_path):
    path = _faugus(tmp_path, [_game()])
    manager = _manager(tmp_path)
    manager.refresh()

    assert manager.set_game_enabled("e33", True).ok

    command = json.loads(path.read_text())[0]["launch_arguments"]
    assert wrapper_present(command)
    # The user's own prefix is kept, and kept first: ours sits innermost,
    # next to the game, so gamescope and friends never inherit our layer env.
    assert command.startswith("game-performance ")
    assert command.endswith("--pb-game-id=faugus:e33")


def test_disabling_restores_the_original_command_exactly(tmp_path):
    path = _faugus(tmp_path, [_game()])
    before = path.read_text(encoding="utf-8")
    manager = _manager(tmp_path)
    manager.refresh()

    assert manager.set_game_enabled("e33", True).ok
    manager.refresh()
    assert manager.set_game_enabled("e33", False).ok

    assert path.read_text(encoding="utf-8") == before


def test_the_overlay_switch_rewrites_the_flag_in_place(tmp_path):
    path = _faugus(tmp_path, [_game()])
    manager = _manager(tmp_path)
    manager.refresh()
    manager.set_game_enabled("e33", True)
    manager.refresh()

    assert manager.set_game_overlay("e33", True).ok

    command = json.loads(path.read_text())[0]["launch_arguments"]
    assert overlay_present(command)
    assert command.count("--pb-overlay") == 1
    assert command.startswith("game-performance ")


def test_a_game_with_no_command_of_its_own_gets_only_ours(tmp_path):
    path = _faugus(tmp_path, [_game(launch_arguments="")])
    manager = _manager(tmp_path)
    manager.refresh()

    assert manager.set_game_enabled("e33", True).ok

    command = json.loads(path.read_text())[0]["launch_arguments"]
    assert wrapper_present(command)
    assert "game-performance" not in command


def test_enabling_twice_does_not_stack_two_wrappers(tmp_path):
    path = _faugus(tmp_path, [_game()])
    manager = _manager(tmp_path)
    manager.refresh()

    manager.set_game_enabled("e33", True)
    manager.refresh()
    manager.set_game_enabled("e33", True)

    command = json.loads(path.read_text())[0]["launch_arguments"]
    assert command.count("--pb-game-id") == 1


def test_nothing_is_inherited_because_faugus_has_no_global_level(tmp_path):
    _faugus(tmp_path, [_game()])
    manager = _manager(tmp_path)
    (row,) = manager.refresh()

    assert not row.inherited
    assert manager.read_inherited(row.game) == ""


def test_the_library_is_parsed_once_per_refresh_not_once_per_game(tmp_path, monkeypatch):
    _faugus(tmp_path, [_game(gameid=f"g{index}") for index in range(5)])
    manager = _manager(tmp_path)

    import integrations.faugus.config_store as store

    reads = []
    original = store.read_games_document
    monkeypatch.setattr(
        store,
        "read_games_document",
        lambda home=None: (reads.append(home), original(home))[1],
    )
    monkeypatch.setattr(
        "integrations.faugus.manager.read_games_document",
        store.read_games_document,
    )

    manager.refresh()

    assert len(reads) == 1


def test_a_game_without_an_executable_is_blocked_from_writes(tmp_path):
    _faugus(tmp_path, [_game(path="")])
    manager = _manager(tmp_path)
    (row,) = manager.refresh()

    assert "no executable" in manager.write_block(row.game)


def test_the_source_lists_faugus_games_for_the_library_tab(tmp_path):
    _faugus(tmp_path, [_game()])
    source = FaugusLibrarySource(
        home=tmp_path, settings_path=tmp_path / "pb-settings.json"
    )
    source.refresh()

    (game,) = source.games()

    assert game.launcher == "faugus"
    assert game.name == "Expedition 33"
    assert game.subtitle == "Proton-CachyOS (System)"
    assert not game.wrapped
    # A Proton game is translated to Vulkan, so the overlay always reaches it.
    assert game.overlay_supported


def test_a_malformed_library_leaves_the_tab_standing(tmp_path):
    data = tmp_path / ".local/share/faugus-launcher"
    data.mkdir(parents=True)
    (data / "games.json").write_text("{not json")
    manager = _manager(tmp_path)

    assert manager.refresh() == ()


def test_assignment_prefix_launches_and_restores_exactly(tmp_path, monkeypatch):
    import os
    import shlex
    import subprocess
    import sys
    from pathlib import Path

    original = 'PROTON_ENABLE_WAYLAND=0 LABEL="two words" env EXTRA=kept'
    path = _faugus(tmp_path, [_game(launch_arguments=original)])
    before = path.read_bytes()
    manager = _manager(tmp_path)
    manager.refresh()
    # Execute the real flag consumer and exec boundary, without GPU operations.
    wrapper = tmp_path / "PENGUIN_BURNER"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from overlay.launcher import _consume_wrapper_flags\n"
        "env = dict(os.environ)\n"
        "args = _consume_wrapper_flags(sys.argv[1:], env)\n"
        "os.execvpe(args[0], args, env)\n"
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    for overlay in (False, True, False):
        assert manager.set_game_enabled("e33", True).ok
        assert manager.set_game_overlay("e33", overlay).ok
        command = json.loads(path.read_text())[0]["launch_arguments"]
        assert command.count("--pb-game-id") == 1
        probe = "import os,json; print(json.dumps([os.environ[k] for k in ('PROTON_ENABLE_WAYLAND','LABEL','EXTRA','PENGUIN_BURNER_GAME_KEY','PB_OVERLAY')]))"
        result = subprocess.run(
            ["/bin/sh", "-c", command + " " + shlex.join([sys.executable, "-c", probe])],
            capture_output=True, text=True, check=True,
        )
        assert json.loads(result.stdout) == ["0", "two words", "kept", "faugus:e33", str(int(overlay))]
    assert manager.set_game_enabled("e33", False).ok
    assert path.read_bytes() == before
