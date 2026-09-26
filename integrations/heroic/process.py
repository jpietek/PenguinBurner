"""Heroic process control: start a game, see what runs, stop it.

Heroic answers ``heroic://launch/<runner>/<appName>``, which starts the game
from its own configuration -- so the wrapper PenguinBurner wrote into that
game's ``wrapperOptions`` is already in the command Heroic builds. Nothing here
re-implements a launch; it asks Heroic for one.

Session identity survives exec in the environment, including when profile
application is skipped or the daemon is unavailable. The wrapper's command
line flags do not survive exec and cannot identify a running game.
"""

from __future__ import annotations

from pathlib import Path

from integrations.launchers.wrapped_sessions import (
    LauncherSessions,
    running_wrapped_sessions,
)

from .launch_sync import launch_with_current_settings
from .paths import heroic_installation

_EXTERNAL_PROBE = """
def external_game_id(proc, env, stat):
    app = env.get(b'HEROIC_APP_NAME', b'').decode('utf-8', errors='replace')
    if not app or env.get(b'HEROIC_APP_RUNNER') not in (b'legendary', b'gog', b'nile', b'sideload'):
        return ''
    # Only the direct launch child owns the unwrapped lifetime; inherited
    # markers on Wine servers and crash handlers must not keep it running.
    parent = proc.parent / stat[1]
    command = (parent / 'cmdline').read_bytes().split(b'\\0')
    if command and (Path(os.fsdecode(command[0])).name.lower() == 'heroic'
                    or (parent / 'exe').resolve().name.lower() == 'heroic'):
        return app
    return ''
"""


def heroic_available(home: Path | None = None) -> bool:
    """Whether Heroic can be asked to start a game on this machine.

    Distinct from having a Heroic configuration: a machine can carry the
    library of a Heroic that is no longer installed, and those games are still
    worth listing and configuring -- just not startable.
    """
    return heroic_installation(home).command() is not None


def launch_command(runner: str, app_name: str, *, home: Path | None = None) -> list[str] | None:
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
    launcher = heroic_installation(home).command()
    return None if launcher is None else [*launcher, "--no-gui", url]


def launch_heroic_game(runner: str, app_name: str, *, home: Path | None = None) -> bool:
    """Ask Heroic to start a game after refreshing its cached settings."""
    command = launch_command(runner, app_name, home=home)
    return command is not None and launch_with_current_settings(command, home=home)


def probe_heroic_sessions(*, known_pids: tuple[int, ...] = ()) -> LauncherSessions | None:
    """Keep Heroic's outer launch process observable during wrapper handoff."""
    return running_wrapped_sessions(
        "heroic", known_pids=known_pids, external_probe=_EXTERNAL_PROBE,
        keep_external_parents=True,
    )
