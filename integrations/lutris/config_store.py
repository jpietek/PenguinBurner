"""Read and write ``system.prefix_command`` in a Lutris game's YAML config.

Lutris has no CDP or DBus API, so unlike Steam there is no live-apply path:
every change is a write to ``~/.local/share/lutris/games/<configpath>.yml``,
picked up the next time that game starts.

Two consequences shape this module. Lutris rewrites these files itself with
PyYAML, so round-tripping through the same loader/dumper does not degrade
anything Lutris preserves — but an open Lutris configuration window holds the
config in memory and will overwrite the file when it saves, so every write is
read back and the caller is told what actually landed.

Splicing our wrapper into the value, and remembering what was there before,
is not Lutris's business and lives in integrations/launchers/.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from common.atomic_write import atomic_write_text
from integrations.launchers.wrapper_manager import (
    SOURCE_GAME,
    CommandWrite,
    EffectiveCommand,
)

SYSTEM_SECTION = "system"
PREFIX_COMMAND_KEY = "prefix_command"

SOURCE_RUNNER = "runner"
SOURCE_SYSTEM = "system"

#: Which config level set a value, in words for the detail pane. Lutris
#: resolves settings across three levels and the innermost one that defines a
#: scalar wins outright (``system_config.update(...)`` in lutris/config.py, no
#: merging), so reading only the game file reports "unset" for a game that runs
#: with a runner-level prefix -- and writing the game level silently replaces
#: it.
SOURCE_LABELS = {
    SOURCE_GAME: "this game",
    SOURCE_RUNNER: "the runner",
    SOURCE_SYSTEM: "Lutris global settings",
}


class LutrisConfigError(RuntimeError):
    """A game config that cannot be read or written as Lutris would."""


def read_prefix_command(config_path: str | Path) -> str:
    """The value written at one config level, ignoring what it inherits."""
    document = read_game_config(config_path)
    system = document.get(SYSTEM_SECTION)
    if not isinstance(system, dict):
        return ""
    return str(system.get(PREFIX_COMMAND_KEY) or "").strip()


def effective_prefix_command(
    *,
    game_config: str | Path | None,
    runner_config: str | Path | None = None,
    system_config: str | Path | None = None,
) -> EffectiveCommand:
    """Innermost level that sets prefix_command wins, exactly as Lutris does."""
    for path, source in (
        (game_config, SOURCE_GAME),
        (runner_config, SOURCE_RUNNER),
        (system_config, SOURCE_SYSTEM),
    ):
        if path is None:
            continue
        try:
            value = read_prefix_command(path)
        except LutrisConfigError:
            # A level we cannot parse is a level we cannot claim anything
            # about; fall through rather than reporting it as unset.
            continue
        if value:
            return EffectiveCommand(value, source)
    return EffectiveCommand("", "")


def read_game_config(config_path: str | Path) -> dict:
    """The whole YAML document, or an empty one when the game has no config."""
    path = Path(config_path).expanduser()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        # A game Lutris knows but has never configured: an empty document is
        # the honest starting point, and the write below will create the file.
        return {}
    except OSError as error:
        raise LutrisConfigError(f"cannot read {path.name}: {error}") from error
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise LutrisConfigError(f"{path.name} is not valid YAML: {error}") from error
    return document if isinstance(document, dict) else {}


def write_prefix_command(
    config_path: str | Path,
    prefix_command: str,
) -> CommandWrite:
    """Write the value, then read it back and report what is really in the file.

    An empty value removes the key rather than storing a blank string, so a
    disabled game leaves a config indistinguishable from one we never touched.
    """
    path = Path(config_path).expanduser()
    try:
        document = read_game_config(path)
    except LutrisConfigError as error:
        return CommandWrite(False, "", str(error))

    system = document.get(SYSTEM_SECTION)
    system = dict(system) if isinstance(system, dict) else {}
    wanted = str(prefix_command or "").strip()
    if wanted:
        system[PREFIX_COMMAND_KEY] = wanted
    else:
        system.pop(PREFIX_COMMAND_KEY, None)
    if system:
        document[SYSTEM_SECTION] = system
    else:
        document.pop(SYSTEM_SECTION, None)

    try:
        # Atomic, so a crash cannot truncate a game config, and dumped through
        # the same PyYAML round trip Lutris itself writes these files with.
        atomic_write_text(
            path,
            yaml.safe_dump(document, default_flow_style=False, sort_keys=True),
            durable=True,
        )
    except (OSError, yaml.YAMLError) as error:
        return CommandWrite(False, "", f"cannot write {path.name}: {error}")

    try:
        landed = read_prefix_command(path)
    except LutrisConfigError as error:
        return CommandWrite(False, "", str(error))
    if landed != wanted:
        # Lutris's own configuration window keeps the config in memory and
        # rewrites the whole file on save, so it can undo this between our
        # write and our read.
        return CommandWrite(
            False,
            landed,
            "Lutris overwrote the change; close the game's configuration "
            "window in Lutris and try again.",
        )
    return CommandWrite(True, landed)
