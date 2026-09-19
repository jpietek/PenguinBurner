"""Which of a launcher's games are running through our wrapper.

A launcher that starts a game through a wrapper command cannot be asked what
it started: it hands the session over and forgets it. The session identifies
itself in its own environment instead, which survives the exec that replaces
the wrapper with the game -- unlike the wrapper's argv, which does not.

Wrapper sessions are the ones that can be stopped. A launcher that also
stamps its own launches with a game id in the environment can have those
observed in the same walk: pass the variable it uses, and a game running
without our wrapper is reported separately instead of looking like nothing.
A launcher needing more than one variable to recognise its own launches keeps
that probe in its own package.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

from overlay.wrapper_tokens import GAME_KEY_ENV, split_game_key
from runtime.daemon_client import daemon_status

from .host_process import run_on_host, running_in_flatpak


@dataclass(frozen=True)
class LauncherSessions:
    """Controllable wrapper sessions, and observed unwrapped launches."""

    wrapped: dict[str, tuple[int, ...]] = field(default_factory=dict)
    external: dict[str, tuple[int, ...]] = field(default_factory=dict)


# A stdlib-only host probe: the PenguinBurner package need not be installed
# outside our Flatpak. Only the session leader qualifies as a wrapper session;
# helpers and game children inherit its identity but must never become targets
# for Stop. The launcher's own marker is inherited by a whole game tree, so
# those pids are reported for observation only.
_SESSION_PROBE = """
import json, os, sys
from pathlib import Path

marker = sys.argv[3].encode() if len(sys.argv) > 3 and sys.argv[3] else None
sessions = []
external = []
unreadable = []
for proc in Path(sys.argv[1]).iterdir():
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
        elif marker is not None and not env.get(b'PENGUIN_BURNER_TELEMETRY_SESSION'):
            game = env.get(marker, b'').decode('utf-8', errors='replace')
            # A zombie still has its environment but no longer has a game.
            if game and (proc / 'stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z':
                external.append([int(proc.name), game])
    except PermissionError:
        unreadable.append(int(proc.name))
    except (FileNotFoundError, ProcessLookupError, IndexError):
        continue
print(json.dumps({
    'sessions': sessions, 'external': external, 'unreadable': unreadable,
}))
"""


def host_python() -> str:
    """The interpreter that can read the host's /proc, sandbox or not."""
    return (
        os.environ.get("PENGUIN_BURNER_HOST_PYTHON") or "/usr/bin/python3"
        if running_in_flatpak() else sys.executable
    )


def running_wrapped_sessions(
    launcher_id: str,
    *,
    external_env: str = "",
    known_pids: tuple[int, ...] = (),
) -> LauncherSessions | None:
    """This launcher's sessions, or None when the answer is unknowable.

    ``external_env`` names the variable the launcher stamps its own launches
    with, whose value is the game id. Given one, a game running without our
    wrapper is reported in ``external``; without one, only wrapper sessions
    are found.

    None is not "nothing is running": a caller told None must hold what it
    already knows rather than read a failed probe as every game having exited.
    """
    result = run_on_host(
        [host_python(), "-c", _SESSION_PROBE, "/proc", GAME_KEY_ENV, external_env],
        capture=True,
    )
    if result is None or result.returncode != 0:
        return None
    running: dict[str, tuple[int, ...]] = {}
    external: dict[str, tuple[int, ...]] = {}
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
            if launcher == launcher_id:
                running[game_id] = (*running.get(game_id, ()), int(pid))
        for pid, game_id in payload["external"]:
            external[game_id] = (*external.get(game_id, ()), int(pid))
    except (KeyError, ValueError, TypeError):
        return None
    # A wrapped game's whole tree inherits the launcher's marker too, so the
    # game it already accounts for is not also an unwrapped launch.
    return LauncherSessions(
        wrapped=running,
        external={
            game_id: pids for game_id, pids in external.items() if game_id not in running
        },
    )
