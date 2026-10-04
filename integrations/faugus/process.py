"""Faugus process control: start a game, see what runs, stop it.

``faugus-launcher --game <gameid>`` starts a game from its own entry, so the
wrapper PenguinBurner wrote into that game's ``launch_arguments`` is already
in the command Faugus builds. Nothing here re-implements a launch.

Faugus keeps a running-games file of its own, but only its window writes it,
so a game we started would be missing from it. Our wrapper carries its session
identity, and Faugus stamps its launches with FAUGUSID. A store-client handoff
can inherit the client's ID instead; match the full configured executable in
its Wine prefix to observe the actual game without claiming wrapper ownership.
"""

from __future__ import annotations

from pathlib import Path

from integrations.launchers.host_process import start_on_host
from integrations.launchers.wrapped_sessions import (
    LauncherSessions,
    running_wrapped_sessions,
)

from .paths import faugus_installation

_EXTERNAL_PROBE = r"""
def external_game_id(proc, env, stat):
    app = env.get(b'FAUGUSID', b'').decode('utf-8', errors='replace')
    if not app or not executables:
        return app
    command = os.fsdecode((proc / 'cmdline').read_bytes().split(b'\0')[0])
    command = command.replace('\\', '/')
    prefix = os.fsdecode(env.get(b'WINEPREFIX', b''))
    if len(command) > 2 and command[1:3] == ':/':
        if not prefix:
            return app
        command = str(Path(prefix) / 'dosdevices' / command[:2].lower() / command[3:])
    if os.path.isabs(command):
        matches = [game for game, executable in executables.items()
                   if os.path.realpath(executable) == os.path.realpath(command)]
        if len(matches) == 1:
            return matches[0]
    return app
"""

LAUNCHER_ID = "faugus"
#: What Faugus puts in front of every game command, holding that game's id.
GAME_ID_ENV = "FAUGUSID"


def faugus_available(home: Path | None = None) -> bool:
    """Whether Faugus can be asked to start a game on this machine.

    Distinct from having a Faugus library: a machine can carry games.json for
    a Faugus that is no longer installed, and those games are still worth
    listing and configuring -- just not startable.
    """
    return faugus_installation(home).command() is not None


def launch_command(game_id: str, *, home: Path | None = None) -> list[str] | None:
    """The command that asks Faugus to start one game, or None if unusable."""
    game_id = str(game_id or "").strip()
    # The id is spliced into a command line and Faugus resolves it against its
    # own files; a separator in it is refused rather than passed on.
    if not game_id or "/" in game_id or game_id.startswith("-"):
        return None
    return faugus_installation(home).command("--game", game_id)


def launch_faugus_game(game_id: str, *, home: Path | None = None) -> bool:
    """Ask Faugus to start a game (detached)."""
    command = launch_command(game_id, home=home)
    return bool(command) and start_on_host(command)


def probe_faugus_sessions(
    *, known_pids: tuple[int, ...] = (), executables: dict[str, str] | None = None,
) -> LauncherSessions | None:
    """Wrapped sessions and observed Faugus launches; None if unreadable."""
    return running_wrapped_sessions(
        LAUNCHER_ID, external_env=GAME_ID_ENV, known_pids=known_pids,
        external_probe=f"executables = {executables or {}!r}\n" + _EXTERNAL_PROBE,
        keep_external_parents=True,
    )
