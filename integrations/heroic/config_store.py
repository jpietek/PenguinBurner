"""Read and write Heroic's ``wrapperOptions`` for one game.

Heroic runs a game through the wrappers listed in its settings, so that list is
where PenguinBurner's wrapper goes. The list is resolved like every other
Heroic setting: the game's own ``GamesConfig/<appName>.json`` wins outright
over ``config.json``'s ``defaultSettings``, no merging -- so a game that
inherits ``game-performance`` globally loses it the moment we write the game
level, which is why the injection is built on top of the effective value.

Heroic holds a game's config in memory while its settings page is open and
rewrites the whole file when it saves, so every write is read back and the
caller is told what actually landed. Same hazard as Lutris's config window.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from common.atomic_write import atomic_write_text
from integrations.launchers.wrapper_manager import (
    SOURCE_GAME,
    CommandWrite,
    EffectiveCommand,
)

from .paths import game_config_path, global_config_path

WRAPPER_KEY = "wrapperOptions"
DEFAULTS_KEY = "defaultSettings"
VERSION_KEY = "version"
#: The config schema Heroic reads a game file under when it says nothing else.
DEFAULT_CONFIG_VERSION = "v0"

SOURCE_GLOBAL = "global"

SOURCE_LABELS = {
    SOURCE_GAME: "this game",
    SOURCE_GLOBAL: "Heroic global settings",
}


class HeroicConfigError(RuntimeError):
    """A Heroic config that cannot be read or written as Heroic would."""


def entries_command(entries: object) -> str:
    """One ``wrapperOptions`` list as the single command line it runs.

    Heroic pushes each entry's ``exe`` and then its shell-split ``args`` onto
    one list, so the whole table is one command prefix -- which is the shape
    the shared wrapper logic (and the tab's editable field) works in.
    """
    words: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        executable = str(entry.get("exe") or "").strip()
        if not executable:
            continue
        try:
            words.extend([executable, *shlex.split(str(entry.get("args") or ""))])
        except ValueError:
            # Unbalanced quotes in a hand-edited config: Heroic's own
            # shlex.split would throw too, so claim nothing about this entry.
            continue
    return shlex.join(words)


def command_entries(command: str, previous: object = ()) -> list[dict]:
    """The ``wrapperOptions`` list that runs ``command``, keeping the user's rows.

    Our tokens are prepended, so every row the user configured is still at the
    tail of the command word for word. Matching from the end keeps those rows
    exactly as Heroic's settings table shows them, and rebuilds only the part
    that actually changed.
    """
    try:
        words = shlex.split(command or "")
    except ValueError:
        return []
    kept: list[dict] = []
    rows = [entry for entry in (previous if isinstance(previous, list) else []) if isinstance(entry, dict)]
    for entry in reversed(rows):
        entry_words = shlex.split(entries_command([entry]))
        if not entry_words or words[len(words) - len(entry_words) :] != entry_words:
            break
        kept.insert(0, entry)
        words = words[: len(words) - len(entry_words)]
    head = [{"exe": words[0], "args": shlex.join(words[1:])}] if words else []
    return head + kept


def read_game_config(app_name: str, home: Path | None = None) -> dict:
    """The whole game config document, or an empty one when it has none."""
    path = game_config_path(app_name, home)
    if path is None:
        return {}
    return _read_json(path)


def read_game_entries(app_name: str, home: Path | None = None) -> list[dict]:
    """The wrapper rows written at the game's own level."""
    settings = read_game_config(app_name, home).get(str(app_name))
    entries = settings.get(WRAPPER_KEY) if isinstance(settings, dict) else None
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def read_global_entries(home: Path | None = None) -> list[dict]:
    """The wrapper rows every game inherits unless it sets its own."""
    defaults = _read_json(global_config_path(home)).get(DEFAULTS_KEY)
    entries = defaults.get(WRAPPER_KEY) if isinstance(defaults, dict) else None
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def effective_wrapper_command(
    app_name: str,
    home: Path | None = None,
    *,
    game_level: bool = True,
    global_entries: list[dict] | None = None,
) -> EffectiveCommand:
    """What the game really launches through, and which level said so.

    ``game_level=False`` answers what it would launch through if its own level
    said nothing, which is what a disable needs in order to hand inheritance
    back instead of freezing today's value into the game config.

    ``global_entries`` lets a caller resolving a whole library hand in the
    global rows it has already read; every game that sets none of its own
    falls back to them, and that is the common case.
    """
    if game_level:
        entries = read_game_entries(app_name, home)
        if entries:
            return EffectiveCommand(entries_command(entries), SOURCE_GAME)
    if global_entries is None:
        global_entries = read_global_entries(home)
    return EffectiveCommand(entries_command(global_entries), SOURCE_GLOBAL)


def write_wrapper_command(
    app_name: str,
    command: str,
    home: Path | None = None,
    *,
    global_entries: list[dict] | None = None,
) -> CommandWrite:
    """Write the game's wrapper rows, then report what is really in the file.

    An empty command removes the key rather than storing an empty list, so the
    game goes back to inheriting Heroic's global wrappers exactly as it would
    have if PenguinBurner had never touched it.
    """
    path = game_config_path(app_name, home)
    if path is None:
        return CommandWrite(False, "", f"{app_name} is not a name Heroic can store.")
    document = _read_json(path)
    settings = document.get(str(app_name))
    settings = dict(settings) if isinstance(settings, dict) else {}
    # Matched against the EFFECTIVE rows: a game that inherits Heroic's global
    # wrappers has none of its own, and those inherited rows are exactly what
    # the injected command now carries along and must keep as separate rows.
    # Taken from the document already parsed above rather than read again.
    own = settings.get(WRAPPER_KEY)
    own = [entry for entry in own if isinstance(entry, dict)] if isinstance(own, list) else []
    if not own and global_entries is None:
        global_entries = read_global_entries(home)
    entries = command_entries(command, own or global_entries or [])
    if entries:
        settings[WRAPPER_KEY] = entries
    else:
        settings.pop(WRAPPER_KEY, None)
    document[str(app_name)] = settings
    # Heroic picks the schema to read a game file under from this key and
    # falls back to v0 when it is missing; writing it keeps a file we created
    # readable rather than leaving Heroic to guess.
    document.setdefault(VERSION_KEY, DEFAULT_CONFIG_VERSION)

    try:
        # Atomic, and byte-for-byte the two-space JSON Heroic itself writes
        # (JSON.stringify(config, null, 2), no trailing newline), so a config
        # we touched and handed back is indistinguishable from one we never
        # opened.
        atomic_write_text(path, json.dumps(document, indent=2), durable=True)
    except OSError as error:
        return CommandWrite(False, "", f"cannot write {path.name}: {error}")

    landed = entries_command(read_game_entries(app_name, home))
    wanted = entries_command(entries)
    if landed != wanted:
        return CommandWrite(
            False,
            landed,
            "Heroic overwrote the change; close the game's settings page in "
            "Heroic and try again.",
        )
    return CommandWrite(True, landed)


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        # A game Heroic knows but has never configured: an empty document is
        # the honest starting point, and the write below creates the file.
        return {}
    except OSError as error:
        raise HeroicConfigError(f"cannot read {path.name}: {error}") from error
    except json.JSONDecodeError as error:
        raise HeroicConfigError(f"{path.name} is not valid JSON: {error}") from error
    return payload if isinstance(payload, dict) else {}
