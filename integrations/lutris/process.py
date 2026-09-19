"""Lutris client process control: start a game, see what runs, stop it.

Lutris has a CLI, and going through it means the game launches from its own
config -- so the ``prefix_command`` PenguinBurner wrote is already in the line
Lutris builds. Nothing here re-implements a launch; it asks Lutris for one.

Reaching the host from inside a Flatpak is the same problem for every
launcher, and is solved once in integrations/launchers/host_process.py.
"""

from __future__ import annotations

import re

from integrations.launchers.host_process import (
    host_has_command,
    host_pgrep,
    host_terminate,
    start_on_host,
)

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


def lutris_available() -> bool:
    """Whether the Lutris CLI is reachable, which is what launching needs.

    Distinct from having a Lutris library: a machine can carry the database of
    a Lutris that is no longer installed, and those games are still worth
    listing and configuring -- just not startable.
    """
    return host_has_command("lutris")


def launch_lutris_game(game_id: str) -> bool:
    """Ask Lutris to start a game (detached).

    ``lutris:rungameid/<id>`` takes the numeric database id, which is the same
    id the library is keyed by. Lutris runs the game without showing its
    window and exits when the game does.
    """
    game_id = str(game_id).strip()
    if not game_id.isdigit():
        return False
    return start_on_host(["lutris", f"lutris:rungameid/{game_id}"])


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
    matches = host_pgrep(r"[l]utris-wrapper")
    if matches is None:
        return None
    running: dict[str, tuple[int, ...]] = {}
    for pid, line in matches:
        title = title_from_wrapper_line(line, titles)
        if title is not None:
            running[title] = (*running.get(title, ()), pid)
    return running


def stop_lutris_game(pid: int) -> bool:
    """Ask a game's Lutris wrapper to stop, with one SIGTERM.

    One signal, the way Lutris's own stop sends one: lutris-wrapper answers it
    by passing SIGTERM to its children, and only a second one trips the handler
    that SIGKILLs them. Whether to insist is the user's call -- the tab leaves
    the button live so a game that shrugs the polite signal off can be told
    again -- rather than something decided for them on a timer here.
    """
    return host_terminate(pid)
