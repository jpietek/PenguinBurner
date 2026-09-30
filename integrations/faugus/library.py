"""Read the Faugus Launcher library.

Faugus keeps every game in one ``games.json`` list -- no per-game files and no
store backends, because every entry is something the user added by hand. That
makes the read a single parse, and it makes ``gameid`` the only identity there
is: Faugus derives it from the title and uses it for the icon, the cover and
the launch, so it is what PenguinBurner keys a game on too.

Faugus has no install timestamp. The executable creation time provides a local
install-time estimate; unsupported filesystems leave it unknown. Recent Faugus
versions also record an ISO last-played timestamp.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config_store import FaugusConfigError, read_games_document

#: Faugus stores playtime as whole seconds accumulated across sessions.
_SECONDS_PER_HOUR = 3600.0

#: The one runner value that is not a Proton build, spelled as Faugus spells it.
NATIVE_RUNNER = "Linux-Native"

#: Entries Faugus hands to the Steam client instead of running itself.
STEAM_RUNNER = "Steam"

# Store clients are launch infrastructure, not games. Match executables rather
# than editable titles; Launcher.exe alone is also used by real games.
_STORE_CLIENTS = {
    "amazon games.exe", "battle.net.exe", "ealauncher.exe", "eadesktop.exe",
    "epicgameslauncher.exe", "galaxyclient.exe", "ubisoftconnect.exe",
    "upc.exe", "uplay.exe", "origin.exe", "wgc.exe", "steam.exe",
}


@dataclass(frozen=True)
class InstalledFaugusGame:
    game_id: str  # Faugus's gameid: a slug it derives from the title
    name: str
    runner: str
    executable: str
    playtime_seconds: int
    art_path: Path | None
    hidden: bool
    installed_at: int = 0
    last_played: int = 0

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


def read_faugus_games(
    home: Path | None = None,
    *,
    include_hidden: bool = False,
    document: list[dict] | None = None,
) -> tuple[InstalledFaugusGame, ...]:
    """Games, excluding store clients, in the order Faugus stores them.

    Hidden entries are dropped by default: hiding one is the user saying they
    do not want to see it, and the library tab is not the place to overrule
    that. Ordering is left to the caller, which merges several launchers.
    """
    if document is None:
        try:
            document = read_games_document(home)
        except FaugusConfigError:
            return ()
    return tuple(
        game
        for game in (_game(entry) for entry in document)
        if game is not None and (include_hidden or not game.hidden)
    )


def is_store_client(entry: dict) -> bool:
    """A bare store client. The same client told to launch a game -- Battle.net's
    ``--exec="launch D3"``, EA's ``origin2://game/launch`` -- is that game."""
    if str(entry.get("game_arguments") or "").strip():
        return False
    path = str(entry.get("path") or "").replace("\\", "/").strip().casefold()
    return Path(path).name in _STORE_CLIENTS or path.endswith(
        "/rockstar games/launcher/launcher.exe"
    )


def _game(entry: dict) -> InstalledFaugusGame | None:
    game_id = str(entry.get("gameid") or "").strip()
    executable = str(entry.get("path") or "").strip()
    if not game_id or is_store_client(entry):
        return None
    return InstalledFaugusGame(
        game_id=game_id,
        name=str(entry.get("title") or "").strip(),
        runner=str(entry.get("runner") or "").strip(),
        executable=executable,
        playtime_seconds=_playtime_seconds(entry),
        art_path=_art_path(entry),
        hidden=bool(entry.get("hidden")),
        installed_at=_created_at(executable),
        last_played=_last_played(entry.get("last_played")),
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


def _created_at(executable: str) -> int:
    """Local file creation, never mtime (which archives can preserve)."""
    try:
        if not executable or not Path(executable).is_absolute() or not Path(executable).is_file():
            return 0
        result = subprocess.run(
            ["stat", "-L", "--format=%W", "--", executable],
            capture_output=True, text=True, timeout=1, check=True,
        )
        return max(_int(result.stdout.strip()), 0)
    except (OSError, subprocess.SubprocessError):
        return 0


def _last_played(value: object) -> int:
    try:
        return max(int(datetime.fromisoformat(str(value)).timestamp()), 0)
    except (ValueError, OverflowError, OSError):
        return 0
