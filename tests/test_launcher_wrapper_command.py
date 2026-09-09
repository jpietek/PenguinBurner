"""Splicing the PenguinBurner wrapper into a launcher's launch command."""

from __future__ import annotations

from integrations.launchers.wrapper_command import (
    command_wrapped,
    game_key,
    inject_wrapper,
    remove_wrapper,
)


def _inject(command: str, *, overlay: bool = True, launcher_id: str = "lutris") -> str:
    return inject_wrapper(
        command, overlay=overlay, launcher_id=launcher_id, game_id="27"
    )


def test_the_game_key_is_namespaced_by_launcher() -> None:
    """Ids collide across launchers: Lutris game 27 is not Steam app 27."""
    assert game_key("lutris", "27") == "lutris:27"
    assert game_key("heroic", "Turkey") == "heroic:Turkey"
    assert game_key("lutris", "") == ""


def test_half_a_game_key_is_no_game_key() -> None:
    """":27" looks like an identity and namespaces nothing.

    It would be written into a launch command and read back at launch as a
    game nothing can resolve. Empty says so plainly, and every caller already
    leaves the flag out for it.
    """
    assert game_key("", "27") == ""
    assert game_key(None, "27") == ""
    assert game_key("lutris", None) == ""
    assert game_key(None, None) == ""


def test_a_game_key_is_trimmed_on_both_halves() -> None:
    assert game_key(" lutris ", " 27 ") == "lutris:27"
    assert game_key("  ", "27") == ""
    assert game_key("lutris", "   ") == ""


def test_a_command_carries_no_identity_flag_without_a_launcher() -> None:
    """A manager that never set its launcher id must not write ":27"."""
    result = inject_wrapper("game-performance", overlay=False, launcher_id="", game_id="27")

    assert "--pb-game-id" not in result
    assert result == "PENGUIN_BURNER --pb-overlay=0 game-performance"


def test_injection_puts_the_wrapper_in_front_of_the_users_own_command() -> None:
    """Our tokens run first; whatever the user had stays next to the game."""
    assert _inject("game-performance") == (
        "PENGUIN_BURNER --pb-overlay=1 --pb-game-id=lutris:27 game-performance"
    )


def test_injection_carries_the_game_key_because_launchers_publish_none() -> None:
    result = _inject("", overlay=False)

    assert "--pb-game-id=lutris:27" in result
    assert "--pb-overlay=0" in result


def test_injection_is_idempotent() -> None:
    once = _inject("game-performance")

    assert _inject(once) == once


def test_injection_normalizes_a_hand_added_wrapper() -> None:
    """A user who added the bare wrapper themselves gets the flags it needs."""
    assert _inject("game-performance PENGUIN_BURNER", overlay=False) == (
        "PENGUIN_BURNER --pb-overlay=0 --pb-game-id=lutris:27 game-performance"
    )


def test_injection_quotes_a_game_id_that_is_not_one_shell_word() -> None:
    """Heroic app names are the store's string, not something we choose."""
    result = inject_wrapper(
        "", overlay=False, launcher_id="heroic", game_id="Sid Meier"
    )

    assert "--pb-game-id=heroic:Sid%20Meier" in result
    assert remove_wrapper(result) == ""


def test_removal_restores_a_matching_stored_original() -> None:
    original = "game-performance"
    injected = _inject(original)

    assert (
        remove_wrapper(injected, stored_original=original, stored_injected=injected)
        == original
    )


def test_removal_of_an_edited_command_strips_only_our_tokens() -> None:
    """The user changed it after we wrote it; their edit must survive."""
    edited = "PENGUIN_BURNER --pb-overlay=1 --pb-game-id=lutris:27 gamemoderun mangohud"

    assert remove_wrapper(edited, stored_original="x", stored_injected="y") == (
        "gamemoderun mangohud"
    )


def test_removal_still_understands_the_flag_earlier_versions_wrote() -> None:
    """Configured prefix_commands out there still carry --pb-lutris-id."""
    legacy = "PENGUIN_BURNER --pb-overlay=1 --pb-lutris-id=27 gamemoderun"

    assert remove_wrapper(legacy) == "gamemoderun"


def test_wrapped_detection() -> None:
    assert command_wrapped("PENGUIN_BURNER --pb-overlay=0") is True
    assert command_wrapped("game-performance") is False
    assert command_wrapped(None) is False
