"""Where Lutris keeps the things PenguinBurner reads and writes.

Lutris splits its state: the library is one SQLite file, but each game's
configuration is a separate YAML named by the ``configpath`` column, and the
artwork is named by ``slug``. Resolving those three shapes in one place keeps
the reader, the config store, and the panel from each guessing.

Every entry point takes an optional ``home`` so tests never reach the real
installation.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from integrations.launchers.desktop_icons import desktop_icon

LUTRIS_DATA_DIRNAME = Path(".local") / "share" / "lutris"
LUTRIS_CONFIG_DIRNAME = Path(".config") / "lutris"
LIBRARY_DB_FILENAME = "pga.db"
GAME_CONFIG_DIRNAME = "games"
RUNNER_CONFIG_DIRNAME = "runners"
SYSTEM_CONFIG_FILENAME = "system.yml"
# Lutris's freedesktop application id, which is also its icon filename.
DESKTOP_ICON_NAME = "net.lutris.Lutris"
COVERART_DIRNAME = "coverart"
BANNER_DIRNAME = "banners"
FLATPAK_INFO_PATH = Path("/.flatpak-info")


def _running_in_flatpak() -> bool:
    return bool(str(os.environ.get("FLATPAK_ID") or "").strip()) or (
        FLATPAK_INFO_PATH.is_file()
    )


def lutris_data_root(home: Path | None = None) -> Path:
    """Lutris's DATA_DIR: the library database and artwork. Never the configs.

    An explicit ``home`` is the test seam and wins over the environment, so a
    test cannot be broken by whatever XDG variables the host session exports.
    """
    if home is not None:
        return Path(home) / LUTRIS_DATA_DIRNAME
    if _running_in_flatpak():
        # Flatpak redirects XDG_DATA_HOME into PenguinBurner's own app data
        # directory. Lutris is an external host application, so following that
        # value looks for its database under
        # ~/.var/app/io.github.jpietek.PenguinBurner instead of the host home
        # the manifest deliberately exposes. Steam discovery likewise starts
        # from Path.home() for this reason.
        return Path.home() / LUTRIS_DATA_DIRNAME
    xdg = str(os.environ.get("XDG_DATA_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "lutris"


def lutris_config_root(home: Path | None = None) -> Path:
    """Lutris's CONFIG_DIR: game, runner and system configs.

    Upstream (lutris/settings.py) still prefers ``~/.config/lutris`` whenever
    that directory exists and only falls back to the data dir when it does not
    -- the deprecation is a fallback, not a migration. Writing the data-dir
    copy on a host whose legacy dir survives produces YAML Lutris never reads,
    so the wrapper silently never applies.
    """
    if home is not None:
        legacy = Path(home) / LUTRIS_CONFIG_DIRNAME
    elif _running_in_flatpak():
        # XDG_CONFIG_HOME belongs to the PenguinBurner sandbox here, not to the
        # host Lutris whose configuration this integration edits.
        legacy = Path.home() / LUTRIS_CONFIG_DIRNAME
    else:
        xdg = str(os.environ.get("XDG_CONFIG_HOME") or "").strip()
        base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
        legacy = base / "lutris"
    return legacy if legacy.is_dir() else lutris_data_root(home)


def lutris_library_db(home: Path | None = None) -> Path:
    return lutris_data_root(home) / LIBRARY_DB_FILENAME


def lutris_installed(home: Path | None = None) -> bool:
    """Whether there is a Lutris library to read at all.

    The tab uses this to explain itself instead of showing an empty list on a
    machine that simply has no Lutris.
    """
    return lutris_library_db(home).is_file()


def game_config_path(configpath: str, home: Path | None = None) -> Path | None:
    """The YAML holding one game's config, or None when the name is unusable.

    ``configpath`` comes straight out of the library row and is spliced into a
    filename, so a value containing a separator is refused rather than allowed
    to escape the config directory.
    """
    name = str(configpath or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return None
    return lutris_config_root(home) / GAME_CONFIG_DIRNAME / f"{name}.yml"


def lutris_desktop_icon(
    home: Path | None = None,
    *,
    data_dirs: Sequence[Path] | None = None,
) -> Path | None:
    """Lutris's own application icon, if this machine has Lutris installed.

    The lookup itself is launcher-agnostic and shared -- Steam wants the same
    thing -- so it lives in integrations/launchers/desktop_icons.py and this
    only names the icon.
    """
    return desktop_icon(DESKTOP_ICON_NAME, home, data_dirs=data_dirs)


def runner_config_path(runner: str, home: Path | None = None) -> Path | None:
    """The YAML holding one runner's config, or None when the name is unusable.

    Lutris resolves a setting across three levels — system, runner, game — and
    the game level wins outright for a scalar like ``prefix_command``. Reading
    only the game file therefore misses a value the game genuinely runs with.
    """
    name = str(runner or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return None
    return lutris_config_root(home) / RUNNER_CONFIG_DIRNAME / f"{name}.yml"


def system_config_path(home: Path | None = None) -> Path:
    return lutris_config_root(home) / SYSTEM_CONFIG_FILENAME


def game_cover_path(slug: str, home: Path | None = None) -> Path | None:
    """Cover art for a slug, falling back to the banner, else None."""
    name = str(slug or "").strip()
    if not name or "/" in name or "\\" in name:
        return None
    root = lutris_data_root(home)
    for directory, suffix in (
        (COVERART_DIRNAME, ".jpg"),
        (COVERART_DIRNAME, ".png"),
        (BANNER_DIRNAME, ".jpg"),
        (BANNER_DIRNAME, ".png"),
    ):
        candidate = root / directory / f"{name}{suffix}"
        if candidate.is_file():
            return candidate
    return None
