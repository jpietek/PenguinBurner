"""Faugus process control: start a game, see what runs, stop it.

``faugus-launcher --game <gameid>`` starts a game from its own entry, so the
wrapper PenguinBurner wrote into that game's ``launch_arguments`` is already
in the command Faugus builds. Nothing here re-implements a launch.

Faugus keeps a running-games file of its own, but only its window writes it,
so a game we started would be missing from it. The environment is what holds
regardless of who did the launching: our wrapper carries its session identity,
and Faugus stamps every game it starts with FAUGUSID -- the same marker its
own "kill this game" walks /proc for.
"""

from __future__ import annotations

from integrations.launchers.host_process import (
    host_has_command,
    host_terminate,
    run_on_host,
    start_on_host,
)
from integrations.launchers.wrapped_sessions import (
    LauncherSessions,
    running_wrapped_sessions,
)

LAUNCHER_ID = "faugus"
COMMAND = "faugus-launcher"
#: Faugus's Flatpak application id, for hosts that installed it that way.
FLATPAK_APP_ID = "io.github.Faugus.faugus-launcher"
#: What Faugus puts in front of every game command, holding that game's id.
GAME_ID_ENV = "FAUGUSID"


def faugus_available() -> bool:
    """Whether Faugus can be asked to start a game on this machine.

    Distinct from having a Faugus library: a machine can carry games.json for
    a Faugus that is no longer installed, and those games are still worth
    listing and configuring -- just not startable.
    """
    return _launcher_command() is not None


def _launcher_command() -> list[str] | None:
    if host_has_command(COMMAND):
        return [COMMAND]
    if host_has_command("flatpak"):
        installed = run_on_host(["flatpak", "info", FLATPAK_APP_ID])
        if installed is not None and installed.returncode == 0:
            return ["flatpak", "run", FLATPAK_APP_ID]
    return None


def launch_command(game_id: str) -> list[str] | None:
    """The command that asks Faugus to start one game, or None if unusable."""
    game_id = str(game_id or "").strip()
    # The id is spliced into a command line and Faugus resolves it against its
    # own files; a separator in it is refused rather than passed on.
    if not game_id or "/" in game_id or game_id.startswith("-"):
        return None
    launcher = _launcher_command()
    return None if launcher is None else [*launcher, "--game", game_id]


def launch_faugus_game(game_id: str) -> bool:
    """Ask Faugus to start a game (detached)."""
    command = launch_command(game_id)
    return bool(command) and start_on_host(command)


def probe_faugus_sessions(
    *, known_pids: tuple[int, ...] = (),
) -> LauncherSessions | None:
    """Wrapped sessions and observed Faugus launches; None if unreadable."""
    return running_wrapped_sessions(
        LAUNCHER_ID, external_env=GAME_ID_ENV, known_pids=known_pids
    )


def stop_faugus_game(pid: int) -> bool:
    """One SIGTERM to the wrapper, which is the game session after its exec."""
    return host_terminate(pid)
