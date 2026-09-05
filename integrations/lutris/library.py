"""Read the Lutris library out of pga.db.

Lutris keeps its library in SQLite rather than Steam's VDF manifests, which
makes the scan a single query instead of a directory walk. The database belongs
to a running Lutris, so it is opened read-only through a URI and every failure
degrades to an empty library: a locked, missing, or older-schema database must
leave the tab empty and explaining itself, never raise into the GUI.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .paths import game_config_path, game_cover_path, lutris_library_db

# Columns this integration needs. Queried explicitly (never SELECT *) so a
# Lutris release that adds or reorders columns cannot shift what we read.
_COLUMNS = (
    "id",
    "name",
    "sortname",
    "slug",
    "runner",
    "platform",
    "installed",
    "lastplayed",
    "playtime",
    "configpath",
    "directory",
)


@dataclass(frozen=True)
class InstalledLutrisGame:
    game_id: str
    name: str
    slug: str
    runner: str
    platform: str
    installed: bool
    last_played: int
    playtime_hours: float
    configpath: str
    directory: str
    config_path: Path | None
    cover_path: Path | None

    @property
    def ready(self) -> bool:
        """Configurable: installed, and with a config file we can actually write."""
        return self.installed and self.config_path is not None

    @property
    def display_name(self) -> str:
        return self.name or self.slug or self.game_id

    @property
    def runner_label(self) -> str:
        """A runner worth showing; Lutris leaves it empty for some entries."""
        return (self.runner or "").strip() or "unknown"

    @property
    def is_wine(self) -> bool:
        """Wine/Proton games are the ones the NVAPI shim and overlay can reach."""
        return self.runner_label.lower() in ("wine", "proton", "umu")


def read_lutris_games(
    home: Path | None = None,
    *,
    db_path: Path | None = None,
    include_uninstalled: bool = False,
) -> tuple[InstalledLutrisGame, ...]:
    """Every game Lutris knows about, newest-played first.

    Uninstalled entries are dropped by default: they have no prefix to wrap and
    would only pad the list.
    """
    path = Path(db_path) if db_path is not None else lutris_library_db(home)
    rows = _query_games(path)
    games = []
    for row in rows:
        game = _game_from_row(row, home)
        if game is None:
            continue
        if not include_uninstalled and not game.installed:
            continue
        games.append(game)
    games.sort(key=lambda g: (-int(g.last_played or 0), g.display_name.casefold()))
    return tuple(games)


def lutris_game(
    game_id: str,
    home: Path | None = None,
    *,
    db_path: Path | None = None,
) -> InstalledLutrisGame | None:
    wanted = str(game_id or "").strip()
    if not wanted:
        return None
    for game in read_lutris_games(home, db_path=db_path, include_uninstalled=True):
        if game.game_id == wanted:
            return game
    return None


def _query_games(path: Path) -> list[sqlite3.Row]:
    if not path.is_file():
        return []
    columns = ", ".join(_COLUMNS)
    try:
        # Read-only URI: Lutris may be running and owns this file. immutable=0
        # is the default, so a concurrent write still gets a consistent view.
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        connection.row_factory = sqlite3.Row
        return list(connection.execute(f"select {columns} from games"))
    except sqlite3.Error:
        # Missing column, corrupt page, locked past the timeout: an empty
        # library is a workable answer, an exception into the GUI is not.
        return []
    finally:
        connection.close()


def _game_from_row(row: sqlite3.Row, home: Path | None) -> InstalledLutrisGame | None:
    game_id = str(_value(row, "id") or "").strip()
    if not game_id:
        return None
    configpath = str(_value(row, "configpath") or "").strip()
    slug = str(_value(row, "slug") or "").strip()
    return InstalledLutrisGame(
        game_id=game_id,
        name=str(_value(row, "name") or "").strip(),
        slug=slug,
        runner=str(_value(row, "runner") or "").strip(),
        platform=str(_value(row, "platform") or "").strip(),
        installed=bool(_int(_value(row, "installed"))),
        last_played=_int(_value(row, "lastplayed")),
        playtime_hours=_float(_value(row, "playtime")),
        configpath=configpath,
        directory=str(_value(row, "directory") or "").strip(),
        config_path=game_config_path(configpath, home) if configpath else None,
        cover_path=game_cover_path(slug, home) if slug else None,
    )


def _value(row: sqlite3.Row, key: str):
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(result, 0.0)
