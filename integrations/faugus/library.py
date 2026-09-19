"""Read the Faugus Launcher library.

Faugus keeps every game in one ``games.json`` list -- no per-game files and no
store backends, because every entry is something the user added by hand. That
makes the read a single parse, and it makes ``gameid`` the only identity there
is: Faugus derives it from the title and uses it for the icon, the cover and
the launch, so it is what PenguinBurner keys a game on too.

Two things Faugus simply does not record: when a game was installed, and when
it was last played. Both are reported as unknown rather than guessed, so the
library's time-based sorts place these games last instead of somewhere wrong.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .paths import games_path

#: Faugus stores playtime as whole seconds accumulated across sessions.
_SECONDS_PER_HOUR = 3600.0

#: The one runner value that is not a Proton build, spelled as Faugus spells it.
NATIVE_RUNNER = "Linux-Native"

#: Entries Faugus hands to the Steam client instead of running itself.
STEAM_RUNNER = "Steam"


@dataclass(frozen=True)
class InstalledFaugusGame:
    game_id: str  # Faugus's gameid: a slug it derives from the title
    name: str
    runner: str
    executable: str
    prefix: str
    playtime_seconds: int
    art_path: Path | None
    hidden: bool

    @property
    def ready(self) -> bool:
        """Configurable: it has an executable to put a wrapper in front of."""
        return bool(self.executable)

    @property
    def display_name(self) -> str:
        return self.name or self.game_id

    @property
    def runner_label(self) -> str:
        """The runner, worded as the Faugus game-edit dialog words it."""
        return self.runner or "Proton"

    @property
    def is_native(self) -> bool:
        return self.runner == NATIVE_RUNNER

    @property
    def install_path(self) -> str:
        """The directory the game runs from, for renderer inspection."""
        return str(Path(self.executable).parent) if self.executable else ""

    @property
    def playtime_hours(self) -> float:
        return self.playtime_seconds / _SECONDS_PER_HOUR

    @property
    def last_played(self) -> int:
        """Unknown: Faugus totals playtime but never records a last launch."""
        return 0


def read_faugus_games(
    home: Path | None = None,
    *,
    include_hidden: bool = False,
) -> tuple[InstalledFaugusGame, ...]:
    """Every game Faugus knows, in the order Faugus stores them.

    Hidden entries are dropped by default: hiding one is the user saying they
    do not want to see it, and the library tab is not the place to overrule
    that. Ordering is left to the caller, which merges several launchers.
    """
    return tuple(
        game
        for game in (_game(entry) for entry in _entries(games_path(home)))
        if game is not None and (include_hidden or not game.hidden)
    )


def _entries(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        # A library that cannot be read is an empty one here; the write path
        # is what owes the user a real error about this file.
        return []
    return [entry for entry in payload if isinstance(entry, dict)] if isinstance(payload, list) else []


def _game(entry: dict) -> InstalledFaugusGame | None:
    game_id = str(entry.get("gameid") or "").strip()
    if not game_id:
        return None
    return InstalledFaugusGame(
        game_id=game_id,
        name=str(entry.get("title") or "").strip(),
        runner=str(entry.get("runner") or "").strip(),
        executable=str(entry.get("path") or "").strip(),
        prefix=str(entry.get("prefix") or "").strip(),
        playtime_seconds=_playtime_seconds(entry),
        art_path=_art_path(entry),
        hidden=bool(entry.get("hidden")),
    )


def _playtime_seconds(entry: dict) -> int:
    """What Faugus recorded, or 0 for the entries it does not record.

    A Steam-runner entry is launched through the Steam client, and Faugus
    reads its playtime live out of Steam's localconfig.vdf every time it shows
    it -- so the number left in games.json is whatever it happened to be, not
    a total. Our own Steam source answers for those games properly.
    """
    if str(entry.get("runner") or "").strip() == STEAM_RUNNER:
        return 0
    return _int(entry.get("playtime"))


def _art_path(entry: dict) -> Path | None:
    """The cover if there is one, else the icon.

    Same preference as the Faugus grid: the cover is the artwork made to be
    looked at, and the icon is a 32px fallback.
    """
    for key in ("cover", "icon"):
        value = str(entry.get(key) or "").strip()
        if value and Path(value).is_file():
            return Path(value)
    return None


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
