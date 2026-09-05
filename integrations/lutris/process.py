"""Lutris client process control: start a game, see what runs, stop it.

Lutris has a CLI, and going through it means the game launches from its own
config -- so the ``prefix_command`` PenguinBurner wrote is already in the line
Lutris builds. Nothing here re-implements a launch; it asks Lutris for one.

Shaped after integrations/steam/process.py, including the Flatpak host bridge,
because the questions are the same three and the answers have to survive the
same sandbox.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

FLATPAK_INFO_PATH = Path("/.flatpak-info")
HOST_PGREP = "/usr/bin/pgrep"
HOST_KILL = "/usr/bin/kill"
HOST_SHELL = "/usr/bin/sh"
HOST_WORKING_DIRECTORY = "/tmp"

#: Lutris runs every game through this, the way Steam runs one through reaper.
#: It is exec'd as
#: ``lutris-wrapper <title> <n_include> <n_exclude> <names...> <command...>``
#: and then renames itself to ``lutris-wrapper: <title>`` -- the second form is
#: what a probe actually sees, since the rename happens during startup. Both
#: are matched, because the exec form is briefly real and a slow launch could
#: be caught in it.
#:
#: Either way the title is the game's name straight out of the same database
#: the library is read from, so the two sides always spell it identically.
WRAPPER_NAME = "lutris-wrapper"
_WRAPPER_RE = re.compile(rf"{re.escape(WRAPPER_NAME)}:?\s+(.*)$")
#: In the exec form the title is followed by the pair of counts, which is how
#: its end is recognised without guessing where a name with spaces stops. In
#: the renamed form the title simply runs to the end.
_AFTER_TITLE_RE = re.compile(r"^(\s+\d+\s+\d+(\s|$)|\s*$)")


def running_in_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID", "").strip()) or FLATPAK_INFO_PATH.is_file()


def _flatpak_host_command(command: list[str]) -> list[str] | None:
    flatpak_spawn = shutil.which("flatpak-spawn")
    if not flatpak_spawn:
        return None
    return [
        flatpak_spawn,
        "--host",
        f"--directory={HOST_WORKING_DIRECTORY}",
        *command,
    ]


def lutris_available() -> bool:
    """Whether the Lutris CLI is reachable, which is what launching needs.

    Distinct from having a Lutris library: a machine can carry the database of
    a Lutris that is no longer installed, and those games are still worth
    listing and configuring -- just not startable.

    Inside a Flatpak the sandbox PATH says nothing about the host, so the
    question goes through flatpak-spawn the way the Steam probe asks after its
    client -- everything else in this module already runs on the host, and an
    in-sandbox answer would keep the whole launch surface unreachable there.
    """
    if not running_in_flatpak():
        return shutil.which("lutris") is not None
    command = _flatpak_host_command([HOST_SHELL, "-c", "command -v lutris"])
    if command is None:
        return False
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def launch_lutris_game(game_id: str) -> bool:
    """Ask Lutris to start a game (detached).

    ``lutris:rungameid/<id>`` takes the numeric database id, which is the same
    id the library is keyed by. Lutris runs the game without showing its
    window and exits when the game does.
    """
    if not str(game_id).strip().isdigit():
        return False
    command = ["lutris", f"lutris:rungameid/{str(game_id).strip()}"]
    if running_in_flatpak():
        command = _flatpak_host_command(command) or []
    if not command:
        return False
    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


def title_from_wrapper_line(line: str, known_titles) -> str | None:
    """Which known game a ``lutris-wrapper`` command line belongs to.

    Matched against the names we already hold rather than parsed out of the
    line, because a title may contain spaces and argv arrives here joined by
    them. The longest match wins, so "Portal" cannot claim "Portal 2"'s
    session, and the counts that follow the title confirm the boundary.
    """
    match = _WRAPPER_RE.search(line)
    if match is None:
        return None
    tail = match.group(1)
    for title in sorted((t for t in known_titles if t), key=len, reverse=True):
        if tail.startswith(title) and _AFTER_TITLE_RE.match(tail[len(title) :]):
            return title
    return None


def running_lutris_games(known_titles) -> dict[str, tuple[int, ...]] | None:
    """Every running game among ``known_titles``, mapped to its wrapper pids.

    One ``pgrep -af`` for the whole library, as on the Steam side: a poller
    asks once per tick however many games it tracks. The ``[l]`` class keeps
    the query's own command line from matching itself.

    Pids, plural, because the title is all a wrapper command line carries and
    a title is not unique -- two library entries (a wine and a Proton install)
    can spell the same name, and two sessions of it can run at once. The
    caller decides what an ambiguous answer permits; collapsing it here is how
    a Stop signal lands on the wrong game's wrapper.

    ``None`` means the check failed -- not that nothing is running -- so a
    caller can hold what it knows instead of reading a stalled probe as every
    game having exited.
    """
    titles = tuple(known_titles)
    if not titles:
        return {}
    command = [HOST_PGREP, "-af", r"[l]utris-wrapper"]
    if running_in_flatpak():
        command = _flatpak_host_command(command) or []
    if not command:
        return None
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # 1 = ran fine and matched nothing; anything but 0/1 means the answer is
    # not trustworthy.
    if result.returncode not in (0, 1):
        return None
    running: dict[str, tuple[int, ...]] = {}
    for line in result.stdout.splitlines():
        pid_text, _, rest = line.partition(" ")
        if not pid_text.isdigit():
            continue
        title = title_from_wrapper_line(rest, titles)
        if title is not None:
            running[title] = (*running.get(title, ()), int(pid_text))
    return running


def stop_lutris_game(pid: int) -> bool:
    """Ask a game's Lutris wrapper to stop, with one SIGTERM.

    One signal, the way Lutris's own stop sends one: lutris-wrapper answers it
    by passing SIGTERM to its children, and only a second one trips the handler
    that SIGKILLs them. Whether to insist is the user's call -- the tab leaves
    the button live so a game that shrugs the polite signal off can be told
    again -- rather than something decided for them on a timer here.

    The pid came from the host pgrep above, so inside a Flatpak the signal has
    to travel the same way: the sandbox has its own PID namespace, where that
    number is nothing or -- worse -- some unrelated sandbox process.
    """
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return False
    if running_in_flatpak():
        command = _flatpak_host_command([HOST_KILL, "-TERM", str(pid)])
        if not command:
            return False
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True
