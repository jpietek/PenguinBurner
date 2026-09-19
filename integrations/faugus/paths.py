"""Where Faugus Launcher keeps the two files PenguinBurner reads.

Faugus splits its state the XDG way: preferences under the config home, the
library under the data home. Both are read straight from disk, because Faugus
rewrites them on save rather than holding a lock we could wait on.

The Flatpak build resolves the same XDG variables inside its sandbox, so its
tree hangs off ``.var/app`` instead of the host home -- unlike its Steam and
compatibility-tool lookups, which deliberately reach out to the host.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from integrations.launchers.host_paths import host_config_home, host_data_home

FAUGUS_DIRNAME = "faugus-launcher"
FAUGUS_FLATPAK_APP_ID = "io.github.Faugus.faugus-launcher"
CONFIG_FILENAME = "config.json"
GAMES_FILENAME = "games.json"


def _flatpak_base(home: Path | None) -> Path:
    base = Path(home) if home is not None else Path.home()
    return base / ".var" / "app" / FAUGUS_FLATPAK_APP_ID


def config_candidates(home: Path | None = None) -> tuple[Path, ...]:
    """Every config.json this machine might hold, native build first."""
    return (
        host_config_home(home) / FAUGUS_DIRNAME / CONFIG_FILENAME,
        _flatpak_base(home) / "config" / FAUGUS_DIRNAME / CONFIG_FILENAME,
    )


def games_candidates(home: Path | None = None) -> tuple[Path, ...]:
    """Every games.json this machine might hold, native build first."""
    return (
        host_data_home(home) / FAUGUS_DIRNAME / GAMES_FILENAME,
        _flatpak_base(home) / "data" / FAUGUS_DIRNAME / GAMES_FILENAME,
    )


def _first_existing(candidates: Sequence[Path]) -> Path:
    """The candidate that is really there, else the native one.

    Returning the native path when nothing exists keeps the caller's error
    message about the location a user would expect, rather than about a
    sandbox they may never have installed.
    """
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def config_path(home: Path | None = None) -> Path:
    return _first_existing(config_candidates(home))


def games_path(home: Path | None = None) -> Path:
    return _first_existing(games_candidates(home))


def faugus_installed(home: Path | None = None) -> bool:
    """Whether there is a Faugus library to read at all.

    The library is the file that matters: a config.json alone describes a
    Faugus nobody has added a game to, and there is nothing there to wrap.
    """
    return games_path(home).is_file()
