"""The tokens that put the PenguinBurner wrapper in front of a game.

Every launcher splices the same argv fragment — the wrapper name plus its
``--pb-*`` flags — into a string the launcher later runs. Only the surrounding
field differs: Steam replaces ``%command%`` inside its launch options, Lutris
prepends to ``prefix_command``, Heroic adds a wrapper entry. The vocabulary
itself, and stripping it back out, is the wrapper's own business, so it lives
beside the wrapper rather than inside any one integration.
"""

from __future__ import annotations

import re
import shlex
from urllib.parse import quote, unquote

from overlay.telemetry.steam_launch_check import PENGUIN_BURNER_WRAPPER

# The overlay switch rides as a wrapper FLAG, not an env-assignment token:
# gamescope (and anything else that execs its child directly, without a shell)
# cannot start "PB_OVERLAY=1" as a program, so env tokens after "gamescope --"
# brick the launch. A flag is plain argv everywhere. Explicit =0 (not merely
# absent) makes the per-game toggle deterministic -- it also decides the
# wrapper's MangoHud strip.
OVERLAY_FLAG = "--pb-overlay=1"
OVERLAY_OFF_FLAG = "--pb-overlay=0"

# Only Steam publishes a usable game identity in the environment (SteamAppId).
# Lutris regenerates LUTRIS_GAME_UUID every run and Heroic publishes nothing,
# so for those the identity is injected by us and read back off argv.
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
# the launchers write the same meaning in the two shapes each can run.
INGAME_LATENCY_FLAG = "--pb-ingame-latency=1"

# "<launcher>:<game id>" -- the same string the daemon keys a running game by,
# so one identity travels from the library tab to the wrapper to the daemon.
GAME_KEY_FLAG_PREFIX = "--pb-game-id="
#: What the Lutris tab wrote before launchers shared one flag. Still parsed,
#: because it is sitting in prefix_commands users configured; never written.
LEGACY_LUTRIS_ID_FLAG_PREFIX = "--pb-lutris-id="
LEGACY_LUTRIS_LAUNCHER_ID = "lutris"
# Where the wrapper parks the key it read off that flag, so the per-launcher
# runtime hook can find it the same way the Steam one finds SteamAppId.
GAME_KEY_ENV = "PENGUIN_BURNER_GAME_KEY"

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


def game_key_flag(game_key: str) -> str:
    """The identity flag, percent-encoded.

    A game id is the launcher's own string -- Heroic's app names are whatever
    the store called the game -- and this flag is spliced into a command line
    that gets split on whitespace. Encoding keeps it one shell word without
    quoting, which is what lets the token stay recognisable to the stripper.
    """
    return f"{GAME_KEY_FLAG_PREFIX}{quote(str(game_key).strip(), safe=':._-')}"


def game_key_from_flag(flag: str) -> str:
    """The "<launcher>:<id>" a flag carries, in either spelling, or ""."""
    word = str(flag or "").strip()
    if word.startswith(GAME_KEY_FLAG_PREFIX):
        return unquote(word[len(GAME_KEY_FLAG_PREFIX) :].strip())
    if word.startswith(LEGACY_LUTRIS_ID_FLAG_PREFIX):
        game_id = word[len(LEGACY_LUTRIS_ID_FLAG_PREFIX) :].strip()
        return f"{LEGACY_LUTRIS_LAUNCHER_ID}:{game_id}" if game_id else ""
    return ""


def wrapper_tokens(
    *,
    overlay: bool,
    game_key: str = "",
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
    key = str(game_key or "").strip()
    if key:
        parts.append(game_key_flag(key))
    return " ".join(parts)
