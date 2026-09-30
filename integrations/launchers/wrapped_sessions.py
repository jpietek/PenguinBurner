"""Which of a launcher's games are running through our wrapper.

A launcher that starts a game through a wrapper command cannot be asked what
it started: it hands the session over and forgets it. The session identifies
itself in its own environment instead, which survives the exec that replaces
the wrapper with the game -- unlike the wrapper's argv, which does not.

Wrapper sessions are the ones that can be stopped. A launcher that also
stamps its own launches with a game id in the environment can have those
observed in the same walk: pass the variable it uses, and a game running
without our wrapper is reported separately instead of looking like nothing.
Launcher-specific recognition is supplied as a stdlib-only host probe function.
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
# outside our Flatpak. Wrapper leaders and identified successors qualify for
# Stop; PB's own detached helpers clear the session id and so do not. The
# launcher's own marker is inherited by a whole game tree, so those pids are
# reported for observation only.
_EXTERNAL_PROBE = """
def external_game_id(proc, env, stat):
    return env.get(sys.argv[3].encode(), b'').decode('utf-8', errors='replace')
"""

_SESSION_PROBE = """
import json, os, sys
from pathlib import Path

sessions, external, unreadable = [], [], []
for proc in Path(sys.argv[1]).iterdir():
    if not proc.name.isdecimal():
        continue
    try:
        if proc.stat().st_uid != os.getuid():
            continue
        env = dict(field.split(b'=', 1) for field in (proc / 'environ').read_bytes().split(b'\\0') if b'=' in field)
        stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        if stat[0] == 'Z':
            continue
        if (env.get(b'PENGUIN_BURNER_SESSION_ID')
                or env.get(b'PENGUIN_BURNER_TELEMETRY_SESSION') == proc.name.encode()):
            key = env.get(sys.argv[2].encode(), b'').decode('utf-8', errors='replace')
            if key:
                sessions.append([int(proc.name), key])
        elif not env.get(b'PENGUIN_BURNER_TELEMETRY_SESSION'):
            game = external_game_id(proc, env, stat)
            if game:
                external.append([int(proc.name), game])
    except PermissionError:
        unreadable.append(int(proc.name))
    except (FileNotFoundError, ProcessLookupError, IndexError):
        continue
print(json.dumps({'sessions': sessions, 'external': external, 'unreadable': unreadable}))
"""


_STOP_SESSION = """
import os, signal, sys
from pathlib import Path

pid = int(sys.argv[1])
try:
    fd = os.pidfd_open(pid)
except ProcessLookupError:
    raise SystemExit(0)  # Already exited between discovery and Stop.
try:
    root = Path('/proc') / str(pid)
    if root.stat().st_uid != os.getuid():
        raise SystemExit(1)
    stat = (root / 'stat').read_text().rsplit(')', 1)[1].split()
    if stat[0] == 'Z':
        raise SystemExit(0)
    env = dict(field.split(b'=', 1) for field in (root / 'environ').read_bytes().split(b'\\0') if b'=' in field)
    if (env.get(b'PENGUIN_BURNER_GAME_KEY') != sys.argv[2].encode()
            or not (env.get(b'PENGUIN_BURNER_SESSION_ID')
                    or env.get(b'PENGUIN_BURNER_TELEMETRY_SESSION') == str(pid).encode())):
        raise SystemExit(1)
    signal.pidfd_send_signal(fd, signal.SIGTERM)
except (FileNotFoundError, ProcessLookupError):
    pass
finally:
    os.close(fd)
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
    external_probe: str = _EXTERNAL_PROBE,
    keep_external_parents: bool = False,
) -> LauncherSessions | None:
    """This launcher's sessions, or None when the answer is unknowable.

    ``external_env`` names the variable the launcher stamps its own launches
    with, whose value is the game id. Given one, a game running without our
    wrapper is reported in ``external``; without one, only wrapper sessions
    are found.

    ``external_probe`` defines external_game_id(proc, env, stat) on the host.
    Heroic retains its outer launch parents even when a wrapper is also found;
    their lifetimes must remain observable during handoff.

    None is not "nothing is running": a caller told None must hold what it
    already knows rather than read a failed probe as every game having exited.
    """
    result = run_on_host(
        [host_python(), "-c", external_probe + _SESSION_PROBE, "/proc", GAME_KEY_ENV, external_env],
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
        for pid, game_id in payload.get("external", []):
            external[game_id] = (*external.get(game_id, ()), int(pid))
    except (KeyError, ValueError, TypeError):
        return None
    # A wrapped game's whole tree inherits the launcher's marker too, so the
    # game it already accounts for is not also an unwrapped launch.
    return LauncherSessions(
        wrapped=running,
        external={
            game_id: pids for game_id, pids in external.items() if keep_external_parents or game_id not in running
        },
    )


def stop_wrapped_session(pid: int, game_key: str) -> bool:
    """Pin and verify a wrapped member before signalling; reject reused PIDs."""
    result = run_on_host([host_python(), "-c", _STOP_SESSION, str(pid), game_key])
    return result is not None and result.returncode == 0
