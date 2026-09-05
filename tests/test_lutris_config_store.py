"""Injecting the wrapper into a Lutris game's prefix_command, and taking it out."""

from __future__ import annotations

import json

import pytest
import yaml

from integrations.lutris.config_store import (
    LutrisConfigError,
    inject_prefix_command,
    prefix_command_wrapped,
    read_game_config,
    read_prefix_command,
    remove_injection,
    write_prefix_command,
)


def _config(tmp_path, document: dict):
    path = tmp_path / "game.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


# -- token building ------------------------------------------------------------


def test_injection_puts_the_wrapper_in_front_of_the_users_own_prefix() -> None:
    """Lutris runs prefix_command outermost, so ours goes first and theirs follows."""
    result = inject_prefix_command("game-performance", overlay=True, game_id="27")

    assert result == "PENGUIN_BURNER --pb-overlay=1 --pb-lutris-id=27 game-performance"


def test_injection_carries_the_game_id_because_lutris_publishes_none() -> None:
    result = inject_prefix_command("", overlay=False, game_id="27")

    assert "--pb-lutris-id=27" in result
    assert "--pb-overlay=0" in result


def test_injection_is_idempotent() -> None:
    once = inject_prefix_command("game-performance", overlay=True, game_id="27")
    twice = inject_prefix_command(once, overlay=True, game_id="27")

    assert twice == once


def test_injection_normalizes_a_hand_added_wrapper() -> None:
    """A user who added the bare wrapper themselves gets the flags it needs."""
    result = inject_prefix_command(
        "game-performance PENGUIN_BURNER", overlay=False, game_id="27"
    )

    assert result == "PENGUIN_BURNER --pb-overlay=0 --pb-lutris-id=27 game-performance"


def test_removal_restores_a_matching_stored_original() -> None:
    original = "game-performance"
    injected = inject_prefix_command(original, overlay=True, game_id="27")

    assert (
        remove_injection(
            injected, stored_original=original, stored_injected=injected
        )
        == original
    )


def test_removal_of_an_edited_prefix_strips_only_our_tokens() -> None:
    """The user changed it after we wrote it; their edit must survive."""
    edited = "PENGUIN_BURNER --pb-overlay=1 --pb-lutris-id=27 gamemoderun mangohud"

    assert remove_injection(edited, stored_original="x", stored_injected="y") == (
        "gamemoderun mangohud"
    )


def test_wrapped_detection() -> None:
    assert prefix_command_wrapped("PENGUIN_BURNER --pb-overlay=0") is True
    assert prefix_command_wrapped("game-performance") is False
    assert prefix_command_wrapped(None) is False


# -- reading and writing the file ----------------------------------------------


def test_writing_keeps_every_other_key_in_the_game_config(tmp_path) -> None:
    path = _config(
        tmp_path,
        {
            "game": {"exe": "game.exe", "prefix": "/prefix"},
            "system": {"mangohud": True, "env": {"DXVK_HUD": "0"}},
            "wine": {"version": "proton-cachyos"},
        },
    )

    result = write_prefix_command(path, "PENGUIN_BURNER --pb-overlay=1")

    assert result.ok is True
    document = read_game_config(path)
    assert document["game"] == {"exe": "game.exe", "prefix": "/prefix"}
    assert document["wine"] == {"version": "proton-cachyos"}
    assert document["system"]["mangohud"] is True
    assert document["system"]["env"] == {"DXVK_HUD": "0"}
    assert document["system"]["prefix_command"] == "PENGUIN_BURNER --pb-overlay=1"


def test_an_empty_value_removes_the_key_rather_than_blanking_it(tmp_path) -> None:
    """A disabled game should look like one we never touched."""
    path = _config(tmp_path, {"system": {"prefix_command": "PENGUIN_BURNER", "x": 1}})

    assert write_prefix_command(path, "").ok is True

    assert "prefix_command" not in read_game_config(path)["system"]
    assert read_game_config(path)["system"]["x"] == 1


def test_removing_the_last_system_key_drops_the_empty_section(tmp_path) -> None:
    path = _config(tmp_path, {"game": {"exe": "g"}, "system": {"prefix_command": "x"}})

    assert write_prefix_command(path, "").ok is True

    assert "system" not in read_game_config(path)


def test_a_game_with_no_config_file_yet_gets_one(tmp_path) -> None:
    path = tmp_path / "games" / "never-configured.yml"

    result = write_prefix_command(path, "PENGUIN_BURNER --pb-overlay=0")

    assert result.ok is True
    assert read_prefix_command(path) == "PENGUIN_BURNER --pb-overlay=0"


def test_reading_a_missing_config_is_empty_not_an_error(tmp_path) -> None:
    assert read_prefix_command(tmp_path / "absent.yml") == ""
    assert read_game_config(tmp_path / "absent.yml") == {}


def test_invalid_yaml_is_reported_rather_than_silently_replaced(tmp_path) -> None:
    """Overwriting a config we failed to parse would destroy the user's setup."""
    path = tmp_path / "game.yml"
    path.write_text("system: [unbalanced\n", encoding="utf-8")

    with pytest.raises(LutrisConfigError):
        read_prefix_command(path)

    result = write_prefix_command(path, "PENGUIN_BURNER")

    assert result.ok is False
    assert "not valid YAML" in result.message
    assert path.read_text(encoding="utf-8") == "system: [unbalanced\n"


def test_a_write_lutris_undoes_is_reported_not_assumed(tmp_path, monkeypatch) -> None:
    """An open Lutris config window rewrites the file when it saves."""
    path = _config(tmp_path, {"system": {}})
    import integrations.lutris.config_store as store

    def clobber(target, document):
        target.write_text(yaml.safe_dump({"system": {}}), encoding="utf-8")

    monkeypatch.setattr(store, "_atomic_write_yaml", clobber)

    result = write_prefix_command(path, "PENGUIN_BURNER --pb-overlay=1")

    assert result.ok is False
    assert "Lutris overwrote the change" in result.message


def test_a_write_leaves_no_temporary_file_behind(tmp_path) -> None:
    path = _config(tmp_path, {"system": {}})

    assert write_prefix_command(path, "PENGUIN_BURNER").ok is True

    assert [p.name for p in tmp_path.iterdir()] == ["game.yml"]


def test_the_latency_opt_in_survives_a_restart(tmp_path) -> None:
    """It used to be written by neither side and read by neither, so it lived
    only for the session: the switch came back off after every restart while
    the game's own command still carried `env PB_INGAME_LATENCY=1`."""
    from integrations.lutris.settings import (
        LutrisGameSetting,
        load_lutris_game_settings,
        store_lutris_game_setting,
    )

    path = tmp_path / "settings.json"
    store_lutris_game_setting(
        "29",
        LutrisGameSetting(enabled=True, ingame_latency=True),
        path=path,
    )

    assert load_lutris_game_settings(path)["29"].ingame_latency is True


def test_a_file_written_before_the_key_existed_reads_it_off_the_command(
    tmp_path,
) -> None:
    """Upgrades must not silently drop the opt-in. The injected line is stored
    beside the flag and carries the answer, so it is the migration source."""
    from integrations.lutris.settings import load_lutris_game_settings

    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "games": {
                    "29": {
                        "enabled": True,
                        "mode": "adaptive",
                        "overlay": False,
                        "original_prefix_command": "",
                        "injected_prefix_command": (
                            "env PB_INGAME_LATENCY=1 PENGUIN_BURNER --pb-overlay=0"
                        ),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert load_lutris_game_settings(path)["29"].ingame_latency is True


def test_an_old_file_without_the_opt_in_stays_off(tmp_path) -> None:
    from integrations.lutris.settings import load_lutris_game_settings

    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "games": {
                    "29": {
                        "enabled": True,
                        "mode": "adaptive",
                        "overlay": True,
                        "original_prefix_command": "",
                        "injected_prefix_command": "PENGUIN_BURNER --pb-overlay=1",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert load_lutris_game_settings(path)["29"].ingame_latency is False
