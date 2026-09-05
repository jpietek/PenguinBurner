"""An installed application's own icon, from the XDG icon directories.

Preferred over shipping a copy of someone's mark: the real icon appears
exactly when that launcher is present, nobody's artwork is redistributed, and
a machine without the launcher falls back to PenguinBurner's own glyph -- which
is the right icon there anyway.

Resolved by walking the directories rather than through QIcon.fromTheme, which
needs an icon theme to be configured and answers nothing when one is not. This
also keeps the lookup out of the GUI layer, where the launcher adapters cannot
reach.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path


def desktop_icon(
    name: str,
    home: Path | None = None,
    *,
    data_dirs: Sequence[Path] | None = None,
) -> Path | None:
    """The installed icon named ``name``, largest and sharpest first."""
    icon_name = str(name or "").strip()
    if not icon_name or "/" in icon_name or "\\" in icon_name:
        return None
    for root in icon_search_roots(home, data_dirs):
        for relative in (
            Path("icons") / "hicolor" / "scalable" / "apps" / f"{icon_name}.svg",
            Path("icons") / "hicolor" / "256x256" / "apps" / f"{icon_name}.png",
            Path("icons") / "hicolor" / "128x128" / "apps" / f"{icon_name}.png",
            Path("pixmaps") / f"{icon_name}.png",
        ):
            candidate = root / relative
            if candidate.is_file():
                return candidate
    return None


def icon_search_roots(
    home: Path | None = None,
    data_dirs: Sequence[Path] | None = None,
) -> list[Path]:
    if data_dirs is not None:
        return [Path(directory) for directory in data_dirs]
    base = Path.home() if home is None else Path(home)
    roots = [Path(os.environ.get("XDG_DATA_HOME") or (base / ".local" / "share"))]
    configured = str(os.environ.get("XDG_DATA_DIRS") or "").strip()
    listed = configured.split(os.pathsep) if configured else []
    roots.extend(Path(entry) for entry in listed if entry)
    if not listed:
        roots.extend((Path("/usr/local/share"), Path("/usr/share")))
    return roots
