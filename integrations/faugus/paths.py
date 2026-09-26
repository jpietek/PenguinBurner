"""Select the native or Flatpak installation that owns the Faugus library."""

from __future__ import annotations

from pathlib import Path

from integrations.launchers.host_paths import host_data_home
from integrations.launchers.installation import (
    LauncherInstallation,
    select_installation,
)

FAUGUS_DIRNAME = "faugus-launcher"
FAUGUS_COMMAND = "faugus-launcher"
FAUGUS_FLATPAK_APP_ID = "io.github.Faugus.faugus-launcher"
GAMES_FILENAME = "games.json"


def faugus_installation(home: Path | None = None) -> LauncherInstallation:
    base = home or Path.home()
    return select_installation(
        FAUGUS_COMMAND, FAUGUS_FLATPAK_APP_ID, home=home,
        native_roots=(host_data_home(home) / FAUGUS_DIRNAME,),
        flatpak_roots=(
            base / ".var/app" / FAUGUS_FLATPAK_APP_ID / "data" / FAUGUS_DIRNAME,
        ),
        markers=(GAMES_FILENAME,),
    )


def games_path(home: Path | None = None) -> Path:
    """Faugus's library file, in the installation this machine actually uses."""
    return faugus_installation(home).root / GAMES_FILENAME


def faugus_installed(home: Path | None = None) -> bool:
    """Whether there is a Faugus library to read at all.

    The library is the file that matters: a config.json alone describes a
    Faugus nobody has added a game to, and there is nothing there to wrap.
    """
    return games_path(home).is_file()
