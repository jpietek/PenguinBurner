"""Where Lutris keeps the things PenguinBurner reads and writes.

Lutris splits its state: the library is one SQLite file, but each game's
configuration is a separate YAML named by the ``configpath`` column, and the
artwork is named by ``slug``. Resolving those three shapes in one place keeps
the reader, the config store, and the panel from each guessing.

Every entry point takes an optional ``home`` so tests never reach the real
installation.
"""

from __future__ import annotations

from pathlib import Path

from integrations.launchers.host_paths import (
    host_config_home,
    host_data_home,
    safe_config_name,
)

LUTRIS_DATA_DIRNAME = Path(".local") / "share" / "lutris"
LUTRIS_CONFIG_DIRNAME = Path(".config") / "lutris"
LIBRARY_DB_FILENAME = "pga.db"
GAME_CONFIG_DIRNAME = "games"
RUNNER_CONFIG_DIRNAME = "runners"
SYSTEM_CONFIG_FILENAME = "system.yml"
COVERART_DIRNAME = "coverart"
BANNER_DIRNAME = "banners"


def lutris_data_root(home: Path | None = None) -> Path:
    """Lutris's DATA_DIR: the library database and artwork. Never the configs."""
    return host_data_home(home) / "lutris"


def lutris_config_root(home: Path | None = None) -> Path:
    """Lutris's CONFIG_DIR: game, runner and system configs.

    Upstream (lutris/settings.py) still prefers ``~/.config/lutris`` whenever
    that directory exists and only falls back to the data dir when it does not
    -- the deprecation is a fallback, not a migration. Writing the data-dir
    copy on a host whose legacy dir survives produces YAML Lutris never reads,
    so the wrapper silently never applies.
    """
    legacy = host_config_home(home) / "lutris"
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
    name = safe_config_name(configpath)
    return (
        None
        if name is None
        else lutris_config_root(home) / GAME_CONFIG_DIRNAME / f"{name}.yml"
    )


def runner_config_path(runner: str, home: Path | None = None) -> Path | None:
    """The YAML holding one runner's config, or None when the name is unusable.

    Lutris resolves a setting across three levels — system, runner, game — and
    the game level wins outright for a scalar like ``prefix_command``. Reading
    only the game file therefore misses a value the game genuinely runs with.
    """
    name = safe_config_name(runner)
    return (
        None
        if name is None
        else lutris_config_root(home) / RUNNER_CONFIG_DIRNAME / f"{name}.yml"
    )


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
