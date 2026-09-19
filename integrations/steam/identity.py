"""Which game a Steam launch is, in the spelling everything else uses.

Steam's own vocabulary is the numeric app id: its config files, its CDP calls
and the environment it starts a game with all speak that, and nothing here
rewrites any of them. What this adds is the one place Steam meets the other
launchers -- the daemon's registry of running games, keyed by a single opaque
string that a Lutris game 27 and a Steam app 27 must not both answer to. There
the identity is the same ``<launcher>:<id>`` every config-file launcher writes
onto its launch command, so one format covers the whole library.
"""

from __future__ import annotations

from overlay.wrapper_tokens import game_key

STEAM_LAUNCHER_ID = "steam"


def steam_game_key(app_id: str) -> str:
    """A Steam app as the daemon knows it: ``steam:570``, or "" without an id."""
    return game_key(STEAM_LAUNCHER_ID, app_id)


def steam_app_id_from_game_key(key: str) -> str:
    """The Steam app id behind a daemon key, in either spelling, or "".

    Wrappers written before Steam was namespaced register the bare app id, and
    one of those can still be running when this is asked -- a game started
    before the upgrade outlives it. Both spellings are therefore read; only
    the namespaced one is ever written.
    """
    value = str(key or "").strip()
    launcher, separator, app_id = value.partition(":")
    if not separator:
        return value if value.isdigit() else ""
    app_id = app_id.strip()
    return app_id if launcher == STEAM_LAUNCHER_ID and app_id.isdigit() else ""
