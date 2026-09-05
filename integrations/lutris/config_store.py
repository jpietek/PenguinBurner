"""Read and write ``system.prefix_command`` in a Lutris game's YAML config.

Lutris has no CDP or DBus API, so unlike Steam there is no live-apply path:
every change is a write to ``~/.local/share/lutris/games/<configpath>.yml``,
picked up the next time that game starts.

Two consequences shape this module. Lutris rewrites these files itself with
PyYAML, so round-tripping through the same loader/dumper does not degrade
anything Lutris preserves — but an open Lutris configuration window holds the
config in memory and will overwrite the file when it saves, so every write is
read back and the caller is told what actually landed.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from overlay.wrapper_tokens import (
    strip_penguin_burner_tokens,
    wrapper_present,
    wrapper_tokens,
)

SYSTEM_SECTION = "system"
PREFIX_COMMAND_KEY = "prefix_command"


class LutrisConfigError(RuntimeError):
    """A game config that cannot be read or written as Lutris would."""


@dataclass(frozen=True)
class PrefixCommandWrite:
    """What a write attempt actually achieved, after reading the file back."""

    ok: bool
    prefix_command: str
    message: str = ""


def read_prefix_command(config_path: str | Path) -> str:
    """The value written at one config level, ignoring what it inherits."""
    document = read_game_config(config_path)
    system = document.get(SYSTEM_SECTION)
    if not isinstance(system, dict):
        return ""
    return str(system.get(PREFIX_COMMAND_KEY) or "").strip()


@dataclass(frozen=True)
class EffectivePrefixCommand:
    """The prefix_command a game really launches with, and where it came from.

    Lutris resolves settings across system, runner, and game levels, and for a
    scalar like this one the innermost level that defines it wins outright --
    ``system_config.update(...)`` in lutris/config.py, no merging. Reading only
    the game file therefore reports "unset" for a game that runs with a
    runner-level prefix, and writing the game level silently replaces it.
    """

    value: str
    source: str  # "game", "runner", "system", or "" when nothing sets it

    @property
    def inherited(self) -> bool:
        return bool(self.value) and self.source != SOURCE_GAME


SOURCE_GAME = "game"
SOURCE_RUNNER = "runner"
SOURCE_SYSTEM = "system"

_SOURCE_LABELS = {
    SOURCE_GAME: "this game",
    SOURCE_RUNNER: "the runner",
    SOURCE_SYSTEM: "Lutris global settings",
}


def prefix_command_source_label(source: str) -> str:
    return _SOURCE_LABELS.get(str(source or ""), "")


def effective_prefix_command(
    *,
    game_config: str | Path | None,
    runner_config: str | Path | None = None,
    system_config: str | Path | None = None,
) -> EffectivePrefixCommand:
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
            return EffectivePrefixCommand(value, source)
    return EffectivePrefixCommand("", "")


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


def inject_prefix_command(
    prefix_command: str | None,
    *,
    overlay: bool,
    game_id: str,
    ingame_latency: bool = False,
) -> str:
    """Put our tokens in front of whatever the user already had there.

    Lutris runs prefix_command as the outermost argv prefix, so unlike Steam's
    ``%command%`` splice there is no token to replace: our part goes first and
    the user's own prefix follows, staying between us and the game.
    """
    base = strip_penguin_burner_tokens(prefix_command or "")
    tokens = wrapper_tokens(
        overlay=overlay,
        lutris_game_id=game_id,
        # With the overlay on the launcher turns the markers on by itself, so
        # writing the opt-in as well would only be noise in the line.
        ingame_latency=ingame_latency and not overlay,
    )
    return f"{tokens} {base}".strip() if base else tokens


def remove_injection(
    prefix_command: str | None,
    *,
    stored_original: str | None = None,
    stored_injected: str | None = None,
) -> str:
    """Undo an injection, restoring the stored original when it still matches."""
    value = prefix_command or ""
    if stored_injected is not None and value == stored_injected:
        return stored_original or ""
    return strip_penguin_burner_tokens(value)


def write_prefix_command(
    config_path: str | Path,
    prefix_command: str,
) -> PrefixCommandWrite:
    """Write the value, then read it back and report what is really in the file.

    An empty value removes the key rather than storing a blank string, so a
    disabled game leaves a config indistinguishable from one we never touched.
    """
    path = Path(config_path).expanduser()
    try:
        document = read_game_config(path)
    except LutrisConfigError as error:
        return PrefixCommandWrite(False, "", str(error))

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
        _atomic_write_yaml(path, document)
    except (OSError, yaml.YAMLError) as error:
        return PrefixCommandWrite(False, "", f"cannot write {path.name}: {error}")

    try:
        landed = read_prefix_command(path)
    except LutrisConfigError as error:
        return PrefixCommandWrite(False, "", str(error))
    if landed != wanted:
        # Lutris's own configuration window keeps the config in memory and
        # rewrites the whole file on save, so it can undo this between our
        # write and our read.
        return PrefixCommandWrite(
            False,
            landed,
            "Lutris overwrote the change; close the game's configuration "
            "window in Lutris and try again.",
        )
    return PrefixCommandWrite(True, landed)


def _atomic_write_yaml(path: Path, document: dict) -> None:
    """Replace the config in one step so a crash cannot truncate a game config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            yaml.safe_dump(document, handle, default_flow_style=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def prefix_command_wrapped(prefix_command: str | None) -> bool:
    return wrapper_present(prefix_command)
