"""Where the host's config and data live, seen from wherever we are running.

Every launcher's ``paths.py`` asks the same two questions before it names its
own subdirectory, and both have one sandbox-shaped answer: inside a Flatpak the
XDG variables describe PenguinBurner's own sandbox, not the host whose
launchers this reads and writes, so the host home is used instead.

The name guard lives here for the same reason: every launcher splices an id it
was given into a filename.
"""

from __future__ import annotations

import os
from pathlib import Path

from .host_process import running_in_flatpak


def host_config_home(home: Path | None = None) -> Path:
    """The host's ``~/.config``. An explicit ``home`` is the test seam."""
    return _host_home(home, xdg="XDG_CONFIG_HOME", default=Path(".config"))


def host_data_home(home: Path | None = None) -> Path:
    """The host's ``~/.local/share``."""
    return _host_home(home, xdg="XDG_DATA_HOME", default=Path(".local") / "share")


def _host_home(home: Path | None, *, xdg: str, default: Path) -> Path:
    if home is not None:
        return Path(home) / default
    if not running_in_flatpak():
        configured = str(os.environ.get(xdg) or "").strip()
        if configured:
            return Path(configured).expanduser()
    # Inside a Flatpak the variable points into PenguinBurner's own app data
    # directory. The launchers being configured are host applications, and the
    # host home is what the manifest deliberately exposes.
    return Path.home() / default


def safe_config_name(value: str) -> str | None:
    """``value`` if it can be spliced into a filename, else None.

    Launcher ids come straight out of someone else's library -- a Lutris
    configpath, a Heroic app name -- so a value carrying a separator is refused
    rather than allowed to escape the directory it is resolved against.
    """
    name = str(value or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return None
    return name
