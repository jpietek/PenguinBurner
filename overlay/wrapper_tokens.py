"""The tokens that put the PenguinBurner wrapper in front of a game.

Both launchers splice the same argv fragment — the wrapper name plus its
``--pb-*`` flags — into a string the launcher later runs. Only the surrounding
field differs: Steam replaces ``%command%`` inside its launch options, Lutris
prepends to ``prefix_command``. The vocabulary itself, and stripping it back
out, is the wrapper's own business, so it lives beside the wrapper rather than
inside either integration.
"""

from __future__ import annotations

import re
import shlex

from overlay.telemetry.steam_launch_check import PENGUIN_BURNER_WRAPPER

# The overlay switch rides as a wrapper FLAG, not an env-assignment token:
# gamescope (and anything else that execs its child directly, without a shell)
# cannot start "PB_OVERLAY=1" as a program, so env tokens after "gamescope --"
# brick the launch. A flag is plain argv everywhere. Explicit =0 (not merely
# absent) makes the per-game toggle deterministic -- it also decides the
# wrapper's MangoHud strip.
OVERLAY_FLAG = "--pb-overlay=1"
OVERLAY_OFF_FLAG = "--pb-overlay=0"

# Lutris has no stable per-launch app id of its own (LUTRIS_GAME_UUID is
# regenerated every run), so the game identity is injected by us and read back
# off argv. Steam does not need this: it publishes SteamAppId in the
# environment.
# Latency markers ride an env assignment rather than a --pb-* flag, because the
# launcher reads them before it parses anything: the opt-in has to be in the
# environment the wrapper starts with. It is introduced by `env` so the pair
# survives an argv exec -- Lutris spawns prefix_command as a command list with
# no shell, where a bare `VAR=1` first token would be taken as the program name.
INGAME_LATENCY_ASSIGNMENT = "PB_INGAME_LATENCY=1"
INGAME_LATENCY_TOKENS = f"env {INGAME_LATENCY_ASSIGNMENT}"
# Steam takes the same opt-in as a wrapper FLAG, for the reason the overlay
# switch is one: Steam's tokens land where %command% was, and an assignment
# there is a program name to anything that execs its child directly -- which
# is exactly what `gamescope -- %command%` does. Lutris cannot use a flag for
# it (the wrapper is not running yet when prefix_command's env is built), so
# the two launchers write the same meaning in the two shapes each can run.
INGAME_LATENCY_FLAG = "--pb-ingame-latency=1"

LUTRIS_ID_FLAG_PREFIX = "--pb-lutris-id="
# Where the wrapper parks the id it read off that flag, so the Lutris runtime
# hook can find it the same way the Steam one finds SteamAppId.
LUTRIS_GAME_ID_ENV = "PENGUIN_BURNER_LUTRIS_GAME_ID"

# Match complete shell words, never fragments inside a quoted argument.
_PB_TOKEN_RE = re.compile(
    r"(?:--pb-[a-z0-9-]+=\S*"
    r"|PB_[A-Za-z0-9_]+=\S*"
    rf"|{PENGUIN_BURNER_WRAPPER}(?:_[A-Za-z0-9_]+)?=\S*"
    rf"|{PENGUIN_BURNER_WRAPPER})"
)


def strip_penguin_burner_tokens(value: str) -> str:
    value = value or ""
    words = _command_words(value)
    pieces = []
    cursor = 0
    for index, (start, end, word) in enumerate(words):
        next_word = words[index + 1][2] if index + 1 < len(words) else ""
        remove = bool(_PB_TOKEN_RE.fullmatch(word)) or (
            word == "env" and next_word.startswith("PB_INGAME_LATENCY=")
        )
        if not remove:
            continue
        pieces.append(value[cursor:start])
        while end < len(value) and value[end].isspace():
            end += 1
        cursor = end
    pieces.append(value[cursor:])
    return "".join(pieces).strip()


def _command_words(value: str) -> list[tuple[int, int, str]]:
    """Shell words with source spans, so edits preserve quoting and spacing.

    A quoted script is one opaque argument: wrapper-looking text inside it
    must never be edited. Decode each complete word only for comparison; keep
    the original source for everything that survives.
    """
    words = []
    index = 0
    while index < len(value):
        if value[index].isspace():
            index += 1
            continue
        start = index
        quote = ""
        while index < len(value):
            char = value[index]
            if char == "\\" and quote != "'":
                index = min(index + 2, len(value))
                continue
            if quote:
                if char == quote:
                    quote = ""
            elif char in "\"'":
                quote = char
            elif char.isspace():
                break
            index += 1
        raw = value[start:index]
        try:
            decoded = shlex.split(raw)
            word = decoded[0] if len(decoded) == 1 else raw
        except ValueError:
            word = raw
        words.append((start, index, word))
    return words


def wrapper_present(value: str | None) -> bool:
    return any(word == PENGUIN_BURNER_WRAPPER for _, _, word in _command_words(value or ""))


def overlay_present(value: str | None) -> bool:
    return any(word in (OVERLAY_FLAG, "PB_OVERLAY=1") for _, _, word in _command_words(value or ""))


def ingame_latency_present(value: str | None) -> bool:
    """Either shape of the opt-in, so state reads back off any launch string."""
    return any(
        word in (INGAME_LATENCY_ASSIGNMENT, INGAME_LATENCY_FLAG)
        for _, _, word in _command_words(value or "")
    )


def overlay_flag(overlay: bool) -> str:
    return OVERLAY_FLAG if overlay else OVERLAY_OFF_FLAG


def lutris_id_flag(game_id: str) -> str:
    return f"{LUTRIS_ID_FLAG_PREFIX}{str(game_id).strip()}"


def wrapper_tokens(
    *,
    overlay: bool,
    lutris_game_id: str = "",
    ingame_latency: bool = False,
    latency_as_flag: bool = False,
) -> str:
    """The wrapper plus its flags, in the order the launcher will run them.

    As an assignment the latency opt-in comes first -- it is environment for
    the wrapper, so it has to be set before the wrapper is the thing running.
    As a flag it comes after, because then it is an argument to the wrapper.
    """
    parts = [] if (latency_as_flag or not ingame_latency) else [INGAME_LATENCY_TOKENS]
    parts += [PENGUIN_BURNER_WRAPPER, overlay_flag(overlay)]
    if ingame_latency and latency_as_flag:
        parts.append(INGAME_LATENCY_FLAG)
    game_id = str(lutris_game_id or "").strip()
    if game_id:
        parts.append(lutris_id_flag(game_id))
    return " ".join(parts)
