"""Heroic process control: start a game, see what runs, stop it.

Heroic answers ``heroic://launch/<runner>/<appName>``, which starts the game
from its own configuration -- so the wrapper PenguinBurner wrote into that
game's ``wrapperOptions`` is already in the command Heroic builds. Nothing here
re-implements a launch; it asks Heroic for one.

Which sessions are alive is read off our own wrapper's command line, which
carries the game key. That is exact where a name match is not, and it means
this module knows about games PenguinBurner wraps -- the ones the tab can act
on -- and nothing else.
"""

from __future__ import annotations

from integrations.launchers.host_process import (
    host_has_command,
    host_terminate,
    start_on_host,
)
from integrations.launchers.wrapper_command import running_wrapped_games

LAUNCHER_ID = "heroic"
COMMAND = "heroic"
#: Heroic's Flatpak application id, for hosts that installed it that way.
FLATPAK_APP_ID = "com.heroicgameslauncher.hgl"


def heroic_available() -> bool:
    """Whether Heroic can be asked to start a game on this machine.

    Distinct from having a Heroic configuration: a machine can carry the
    library of a Heroic that is no longer installed, and those games are still
    worth listing and configuring -- just not startable.
    """
    return host_has_command(COMMAND) or host_has_command("flatpak")


def launch_command(runner: str, app_name: str) -> list[str] | None:
    """The command that asks Heroic to start one game, or None if unusable."""
    app_name = str(app_name or "").strip()
    runner = str(runner or "").strip()
    if not app_name or not runner or "/" in app_name or "/" in runner:
        return None
    # gui=false asks Heroic to stay out of the way. It rides in the URL rather
    # than only on argv because a second `heroic` process hands its URL to the
    # running one, whose own argv was parsed at ITS startup -- the flag alone
    # would raise the window over the game every time Heroic was already open.
    url = f"heroic://launch/{runner}/{app_name}?gui=false"
    if host_has_command(COMMAND):
        return [COMMAND, "--no-gui", url]
    return ["flatpak", "run", FLATPAK_APP_ID, "--no-gui", url]


def launch_heroic_game(runner: str, app_name: str) -> bool:
    """Ask Heroic to start a game (detached)."""
    command = launch_command(runner, app_name)
    return bool(command) and start_on_host(command)


def running_heroic_games() -> dict[str, tuple[int, ...]] | None:
    """Every wrapped Heroic session, mapped to its wrapper pids."""
    return running_wrapped_games(LAUNCHER_ID)


def stop_heroic_game(pid: int) -> bool:
    """One SIGTERM to the wrapper, which is the game session after its exec."""
    return host_terminate(pid)
