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

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from integrations.launchers.host_process import (
    host_has_command,
    host_terminate,
    run_on_host,
    running_in_flatpak,
)
from overlay.wrapper_tokens import GAME_KEY_ENV, split_game_key
from runtime.daemon_client import daemon_status
from .launch_sync import launch_with_current_settings

LAUNCHER_ID = "heroic"
COMMAND = "heroic"
#: Heroic's Flatpak application id, for hosts that installed it that way.
FLATPAK_APP_ID = "com.heroicgameslauncher.hgl"


@dataclass(frozen=True)
class HeroicSessions:
    """Controllable wrapper sessions and observed unwrapped Heroic launches."""

    wrapped: dict[str, tuple[int, ...]] = field(default_factory=dict)
    external: dict[str, tuple[int, ...]] = field(default_factory=dict)


# A stdlib-only host probe: the PenguinBurner package need not be installed
# outside our Flatpak. Only wrapper session leaders qualify for Stop. Heroic's
# direct launch children are observable separately when the wrapper is missing.
_SESSION_PROBE = """
import json, os, sys
from pathlib import Path

root = Path(sys.argv[1])
sessions = []
external = []
unreadable = []
for proc in root.iterdir():
    if not proc.name.isdecimal():
        continue
    try:
        if proc.stat().st_uid != os.getuid():
            continue
        fields = (proc / 'environ').read_bytes().split(b'\\0')
        env = dict(field.split(b'=', 1) for field in fields if b'=' in field)
        if env.get(b'PENGUIN_BURNER_TELEMETRY_SESSION') == proc.name.encode():
            key = env.get(sys.argv[2].encode(), b'').decode('utf-8', errors='replace')
            if key:
                sessions.append([int(proc.name), key])
        elif not env.get(b'PENGUIN_BURNER_TELEMETRY_SESSION'):
            app = env.get(b'HEROIC_APP_NAME', b'').decode('utf-8', errors='replace')
            runner = env.get(b'HEROIC_APP_RUNNER', b'')
            if app and runner in (b'legendary', b'gog', b'nile', b'sideload'):
                # Observe only Heroic's direct launch child. Wine servers and
                # crash handlers inherit the app id but can outlive the game.
                stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
                if stat[0] == 'Z':
                    continue
                parent = root / stat[1]
                command = (parent / 'cmdline').read_bytes().split(b'\\0')
                if command and (
                    Path(os.fsdecode(command[0])).name.lower() == 'heroic'
                    or (parent / 'exe').resolve().name.lower() == 'heroic'
                ):
                    external.append([int(proc.name), app])
    except PermissionError:
        unreadable.append(int(proc.name))
    except (FileNotFoundError, ProcessLookupError):
        continue
print(json.dumps({'sessions': sessions, 'external': external, 'unreadable': unreadable}))
"""


def heroic_available() -> bool:
    """Whether Heroic can be asked to start a game on this machine.

    Distinct from having a Heroic configuration: a machine can carry the
    library of a Heroic that is no longer installed, and those games are still
    worth listing and configuring -- just not startable.
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
    launcher = _launcher_command()
    return None if launcher is None else [*launcher, "--no-gui", url]


def launch_heroic_game(runner: str, app_name: str, *, home: Path | None = None) -> bool:
    """Ask Heroic to start a game after refreshing its cached settings."""
    command = launch_command(runner, app_name)
    return command is not None and launch_with_current_settings(command, home=home)


def probe_heroic_sessions(
    *, known_pids: tuple[int, ...] = (),
) -> HeroicSessions | None:
    """Sessions; None if a probe or known session cannot be read."""
    python = (
        os.environ.get("PENGUIN_BURNER_HOST_PYTHON") or "/usr/bin/python3"
        if running_in_flatpak() else sys.executable
    )
    result = run_on_host(
        [python, "-c", _SESSION_PROBE, "/proc", GAME_KEY_ENV], capture=True
    )
    if result is None or result.returncode != 0:
        return None
    running: dict[str, tuple[int, ...]] = {}
    try:
        payload = json.loads(result.stdout)
        sessions = payload["sessions"]
        unreadable = set(payload["unreadable"])
        if unreadable:
            # Daemon watches keep their identity independently of /proc access.
            # Only use them for inaccessible PIDs: an exited watch may remain
            # in the daemon's restore grace period after its process is gone.
            try:
                status = daemon_status(timeout_s=1.0)
            except (RuntimeError, OSError, ValueError):
                status = {}
            for watch in (status.get("game_runtime") or {}).get("watched", []):
                pid = watch["pid"]
                if pid in unreadable:
                    sessions.append((pid, watch["app_id"]))
                    unreadable.remove(pid)
            if unreadable.intersection(known_pids):
                return None
        for pid, key in sessions:
            launcher, game_id = split_game_key(key)
            if launcher == LAUNCHER_ID:
                running[game_id] = (*running.get(game_id, ()), int(pid))
    except (KeyError, ValueError, TypeError):
        return None
    external: dict[str, tuple[int, ...]] = {}
    try:
        for pid, game_id in payload.get("external", []):
            external[game_id] = (*external.get(game_id, ()), int(pid))
    except (ValueError, TypeError):
        return None
    return HeroicSessions(running, external)


def stop_heroic_game(pid: int) -> bool:
    """One SIGTERM to the wrapper, which is the game session after its exec."""
    return host_terminate(pid)
