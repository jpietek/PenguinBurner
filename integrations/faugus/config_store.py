"""Read and write one Faugus game's ``launch_arguments``.

Faugus builds its launch line by pushing pieces onto a list in a fixed order:
the Proton environment, then ``launch_arguments``, then gamemoderun, mangohud
and finally umu-run with the game. So ``launch_arguments`` is a command prefix
that runs in front of everything, and the shared injection appends our wrapper
at its end. Faugus hoists every ``NAME=value`` word into the environment
before it execs the rest as argv, so assignments work anywhere in it.

Faugus rewrites the whole games.json when its window saves, so every write is
read back and the caller is told what actually landed.
"""

from __future__ import annotations

import json
from pathlib import Path

from common.atomic_write import atomic_write_text
from integrations.launchers.wrapper_manager import (
    SOURCE_GAME,
    CommandWrite,
    EffectiveCommand,
)

from .paths import games_path

GAME_ID_KEY = "gameid"
COMMAND_KEY = "launch_arguments"

#: Faugus has one level: a game's own entry. Nothing is inherited, so nothing
#: here ever reports another source.
SOURCE_LABELS = {SOURCE_GAME: "this game"}

#: json.dump(data, f, indent=4, ensure_ascii=False), which is what Faugus
#: itself writes; matching it keeps a file we touched indistinguishable from
#: one we never opened.
_JSON_INDENT = 4


class FaugusConfigError(RuntimeError):
    """A games.json that cannot be read or written as Faugus would."""


def read_games_document(home: Path | None = None, *, strict: bool = False) -> list[dict]:
    """The whole library file, entries only."""
    path = games_path(home)
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        return []
    except OSError as error:
        raise FaugusConfigError(f"cannot read {path.name}: {error}") from error
    except json.JSONDecodeError as error:
        raise FaugusConfigError(f"{path.name} is not valid JSON: {error}") from error
    if not isinstance(payload, list):
        raise FaugusConfigError(f"{path.name} does not hold a list of games.")
    if strict and any(not isinstance(entry, dict) for entry in payload):
        raise FaugusConfigError(f"{path.name} contains an invalid game entry.")
    return [entry for entry in payload if isinstance(entry, dict)]


def _entry(document: list[dict], game_id: str) -> dict | None:
    for entry in document:
        if str(entry.get(GAME_ID_KEY) or "").strip() == str(game_id):
            return entry
    return None


def effective_launch_arguments(
    game_id: str,
    home: Path | None = None,
    *,
    document: list[dict] | None = None,
) -> EffectiveCommand:
    """What the game really launches through, and which level said so.

    ``document`` lets a caller resolving the whole library hand in the file it
    has already parsed, so a scan reads games.json once rather than once a game.
    """
    entries = read_games_document(home) if document is None else document
    entry = _entry(entries, game_id)
    if entry is None:
        return EffectiveCommand("", "")
    return EffectiveCommand(str(entry.get(COMMAND_KEY) or "").strip(), SOURCE_GAME)


def write_launch_arguments(
    game_id: str,
    command: str | None,
    home: Path | None = None,
) -> CommandWrite:
    """Write one game's launch arguments, then report what is really in the file.

    Every other field of the entry, and the order of the whole list, is kept:
    Faugus shows this file as the user's own library and a reordering would be
    visible to them.
    """
    path = games_path(home)
    try:
        document = read_games_document(home)
    except FaugusConfigError as error:
        return CommandWrite(False, "", str(error))
    entry = _entry(document, game_id)
    if entry is None:
        return CommandWrite(False, "", f"{game_id} is not in the Faugus library.")
    # Faugus writes "" for "no launch arguments" and reads a missing key the
    # same way, so removing the wrapper restores the empty string it expects
    # rather than dropping a key its own editor would put back.
    entry[COMMAND_KEY] = "" if command is None else str(command)

    try:
        atomic_write_text(
            path,
            json.dumps(document, indent=_JSON_INDENT, ensure_ascii=False),
            durable=True,
        )
        landed = effective_launch_arguments(game_id, home).value
    except FaugusConfigError as error:
        return CommandWrite(False, "", str(error))
    except OSError as error:
        return CommandWrite(False, "", f"cannot write {path.name}: {error}")

    wanted = str(command or "").strip()
    if landed != wanted:
        return CommandWrite(
            False,
            landed,
            "Faugus overwrote the change; close the game's settings window in "
            "Faugus Launcher and try again.",
        )
    return CommandWrite(True, landed)
