"""Which launchers this machine actually has.

The tab asks once, on first entry, and gets back only the launchers that are
installed. Each source answers for itself, because "is it here" means something
different per launcher -- a Steam directory, a Lutris database, a Heroic config
-- and none of those answers belongs in the GUI.
"""

from __future__ import annotations

from pathlib import Path

from .library import LauncherSource


def build_sources(
    *,
    home: Path | None = None,
    steam_settings_path: str | Path | None = None,
    lutris_settings_path: str | Path | None = None,
    heroic_settings_path: str | Path | None = None,
) -> tuple[LauncherSource, ...]:
    """Every known launcher, whether or not it is installed."""
    from integrations.heroic.library_source import HeroicLibrarySource
    from integrations.lutris.library_source import LutrisLibrarySource
    from integrations.steam.library_source import SteamLibrarySource

    return (
        SteamLibrarySource(home=home, settings_path=steam_settings_path),
        LutrisLibrarySource(home=home, settings_path=lutris_settings_path),
        HeroicLibrarySource(home=home, settings_path=heroic_settings_path),
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


def any_launcher_installed(**kwargs) -> bool:
    """Whether this machine has any launcher whose games could carry our wrapper.

    Asked by the Flatpak startup path, which installs the host wrapper only for
    a host that has something to run it. That question is this module's -- a
    launcher added here must not also have to be added to ``common/``.
    """
    return bool(available_sources(**kwargs))
