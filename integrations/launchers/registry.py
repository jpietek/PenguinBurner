"""Which launchers this machine actually has.

The tab asks once, on first entry, and gets back only the launchers that are
installed. Each source answers for itself, because "is it here" means something
different per launcher -- a Steam directory, a Lutris database -- and neither
answer belongs in the GUI.
"""

from __future__ import annotations

from pathlib import Path

from .library import LauncherSource


def build_sources(
    *,
    home: Path | None = None,
    steam_settings_path: str | Path | None = None,
    lutris_settings_path: str | Path | None = None,
) -> tuple[LauncherSource, ...]:
    """Every known launcher, whether or not it is installed."""
    from integrations.lutris.library_source import LutrisLibrarySource
    from integrations.steam.library_source import SteamLibrarySource

    return (
        SteamLibrarySource(home=home, settings_path=steam_settings_path),
        LutrisLibrarySource(home=home, settings_path=lutris_settings_path),
    )


def available_sources(
    sources: tuple[LauncherSource, ...] | None = None,
    **kwargs,
) -> tuple[LauncherSource, ...]:
    """Only the launchers present on this machine, in a stable order.

    Order is the declaration order above rather than anything discovered, so
    the library list does not reshuffle because one launcher answered first.
    """
    candidates = build_sources(**kwargs) if sources is None else tuple(sources)
    return tuple(source for source in candidates if source.available())


def known_launcher_names() -> tuple[str, ...]:
    """Every launcher PenguinBurner can read, installed here or not.

    For the empty state: with nothing installed there is no source to ask, and
    the tab still owes the user the names of what it was looking for.
    """
    return tuple(source.display_name for source in build_sources())
