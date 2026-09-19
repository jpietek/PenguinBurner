"""The wrapper's own vocabulary: writing it into a command and taking it out.

Shell command syntax is where this gets dangerous. Every launcher stores a
string a shell or an argv exec will later run, and these functions edit that
string in place -- so a quoted script that merely *mentions* our flags must
come back untouched, and stripping must never leave a command the user can no
longer launch.
"""

from __future__ import annotations

import pytest

from overlay.wrapper_tokens import (
    GAME_KEY_FLAG_PREFIX,
    INGAME_LATENCY_TOKENS,
    LEGACY_LUTRIS_ID_FLAG_PREFIX,
    game_key_flag,
    game_key_from_flag,
    ingame_latency_present,
    overlay_present,
    strip_penguin_burner_tokens,
    wrapper_present,
    wrapper_tokens,
)

WRAPPED = "PENGUIN_BURNER --pb-overlay=1 --pb-game-id=lutris:27 game-performance"


# -- stripping -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (WRAPPED, "game-performance"),
        ("PENGUIN_BURNER --pb-overlay=0", ""),
        ("", ""),
        # Every one of our shapes at once, including the env-assignment opt-in
        # and the `env` that introduces it.
        (
            "env PB_INGAME_LATENCY=1 PENGUIN_BURNER --pb-overlay=0 "
            "--pb-game-id=lutris:27 gamemoderun",
            "gamemoderun",
        ),
        # Tabs and runs of spaces are word separators like any other.
        ("PENGUIN_BURNER\t--pb-overlay=1\tgamemoderun", "gamemoderun"),
        ("PENGUIN_BURNER   --pb-overlay=1   gamemoderun", "gamemoderun"),
        ("PENGUIN_BURNER --pb-overlay=1\nmangohud", "mangohud"),
        # An escaped space is part of one word, not a break between two.
        (r"PENGUIN_BURNER --pb-overlay=1 /opt/my\ game/run", r"/opt/my\ game/run"),
        # Quoting the user chose is theirs; we return the source, not a
        # re-quoted version of it.
        (
            "PENGUIN_BURNER --pb-overlay=1 env NAME='two  spaces'  gamemoderun",
            "env NAME='two  spaces'  gamemoderun",
        ),
        (
            'PENGUIN_BURNER --pb-overlay=1 env NAME="a b"  mangohud',
            'env NAME="a b"  mangohud',
        ),
        # Our tokens do not have to come first.
        (
            "gamemoderun --pb-overlay=1 mangohud --pb-game-id=lutris:27 PENGUIN_BURNER",
            "gamemoderun mangohud",
        ),
        # Shell operators the launcher may pass through are just words here.
        (
            "PENGUIN_BURNER --pb-overlay=1 sh -c 'a && b'",
            "sh -c 'a && b'",
        ),
        # An unclosed quote is the user's problem to fix, and it is still
        # handed back exactly as they wrote it.
        ("PENGUIN_BURNER --pb-overlay=1 'unclosed", "'unclosed"),
        # A quoted PB word is still our token: a launcher can write it that way.
        ('"PENGUIN_BURNER" --pb-overlay=1 game', "game"),
        # The wrapper's own env prefix, which earlier versions wrote.
        ("PENGUIN_BURNER_FLATPAK_APP_ID=x game", "game"),
        ("PENGUIN_BURNER --pb-lutris-id=27 gamemoderun", "gamemoderun"),
    ],
)
def test_stripping_removes_our_words_and_nothing_else(command, expected) -> None:
    assert strip_penguin_burner_tokens(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        # An opaque quoted script is one argument. Editing inside it would
        # change a program's input, not our wrapper.
        "sh -c 'echo --pb-overlay=1'",
        'sh -c "PENGUIN_BURNER --pb-overlay=1"',
        'prog --config="--pb-overlay=1"',
        # Not our namespace.
        "prog --pbx-overlay=1",
        "prog --pb=1",
        "PENGUIN_BURNERS --pb",
        # Nothing of ours at all.
        "gamemoderun mangohud",
        "env NAME='two  spaces'  gamemoderun",
    ],
)
def test_stripping_leaves_a_command_that_only_looks_like_ours(command) -> None:
    assert strip_penguin_burner_tokens(command) == command


def test_the_latency_env_prefix_only_goes_with_our_assignment() -> None:
    """`env` is ours only when it introduces the opt-in we wrote."""
    assert strip_penguin_burner_tokens("env FOO=1 PB_INGAME_LATENCY=1 game") == (
        "env FOO=1 game"
    )


@pytest.mark.parametrize(
    "command",
    [
        WRAPPED,
        "",
        "gamemoderun",
        "sh -c 'echo --pb-overlay=1'",
        "PENGUIN_BURNER --pb-overlay=1 'unclosed",
        "env PB_INGAME_LATENCY=1 PENGUIN_BURNER --pb-overlay=0 mangohud",
        r"PENGUIN_BURNER --pb-overlay=1 /opt/my\ game/run",
        "PENGUIN_BURNER --pb-overlay=1 PENGUIN_BURNER --pb-overlay=0 game",
    ],
)
def test_stripping_is_idempotent(command) -> None:
    """A second strip is a no-op, whatever the first one had to deal with.

    Every write path strips before it injects, so a command that changed on
    each pass would drift a little further from the user's own every time a
    setting is touched.
    """
    once = strip_penguin_burner_tokens(command)

    assert strip_penguin_burner_tokens(once) == once


def test_stripping_survives_any_arrangement_of_our_tokens() -> None:
    """Order and repetition are the launcher's, not ours: all of it goes."""
    ours = [
        "PENGUIN_BURNER",
        "--pb-overlay=1",
        "--pb-game-id=lutris:27",
        "--pb-ingame-latency=1",
        "--pb-lutris-id=27",
    ]
    for count in range(1, len(ours) + 1):
        command = " ".join([*ours[:count], "gamemoderun", *ours[count:]])

        assert strip_penguin_burner_tokens(command) == "gamemoderun"


# -- reading state back --------------------------------------------------------


def test_state_reads_only_top_level_words() -> None:
    assert wrapper_present(WRAPPED) is True
    assert overlay_present(WRAPPED) is True
    assert wrapper_present("sh -c 'PENGUIN_BURNER game'") is False
    assert overlay_present("sh -c 'echo --pb-overlay=1'") is False
    assert ingame_latency_present("PENGUIN_BURNER --pb-ingame-latency=1") is True
    assert ingame_latency_present("env PB_INGAME_LATENCY=1 PENGUIN_BURNER") is True
    assert ingame_latency_present(WRAPPED) is False


# -- the identity flag ---------------------------------------------------------


def test_the_identity_flag_round_trips_through_encoding() -> None:
    """Heroic ids are the store's own strings, spaces and all."""
    flag = game_key_flag("heroic:Sid Meier")

    assert flag == f"{GAME_KEY_FLAG_PREFIX}heroic:Sid%20Meier"
    assert game_key_from_flag(flag) == "heroic:Sid Meier"
    assert strip_penguin_burner_tokens(f"{flag} game") == "game"


def test_the_flag_earlier_versions_wrote_reads_as_a_namespaced_key() -> None:
    """Configured prefix_commands still carry --pb-lutris-id=27."""
    assert game_key_from_flag(f"{LEGACY_LUTRIS_ID_FLAG_PREFIX}27") == "lutris:27"
    assert game_key_from_flag(f"{LEGACY_LUTRIS_ID_FLAG_PREFIX}") == ""
    assert game_key_from_flag("--pb-overlay=1") == ""
    assert game_key_from_flag("") == ""


# -- writing -------------------------------------------------------------------


def test_the_written_tokens_are_the_ones_that_strip_back_out() -> None:
    tokens = wrapper_tokens(overlay=True, game_key="lutris:27", ingame_latency=True)

    assert tokens.startswith(INGAME_LATENCY_TOKENS)
    assert strip_penguin_burner_tokens(f"{tokens} gamemoderun") == "gamemoderun"


def test_the_latency_opt_in_is_a_flag_where_a_shell_is_not_running_it() -> None:
    """Steam's tokens land where %command% was; an assignment there is a
    program name to anything that execs its child directly."""
    tokens = wrapper_tokens(
        overlay=False,
        game_key="steam:570",
        ingame_latency=True,
        latency_as_flag=True,
    )

    assert INGAME_LATENCY_TOKENS not in tokens
    assert "--pb-ingame-latency=1" in tokens
    assert strip_penguin_burner_tokens(tokens) == ""


def test_no_identity_flag_is_written_without_a_key() -> None:
    assert GAME_KEY_FLAG_PREFIX not in wrapper_tokens(overlay=False)
    assert GAME_KEY_FLAG_PREFIX not in wrapper_tokens(overlay=False, game_key="  ")
