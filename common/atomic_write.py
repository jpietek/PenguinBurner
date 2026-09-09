"""Atomic file writes shared by the persistence sites.

Writes land in a same-directory ``<name>.tmp`` file that is moved into place
with ``os.replace``, so readers never observe a partial file. Parent
directories are created, and files written by elevated runs are chowned back
to the desktop user (``claim_ownership``). ``durable`` additionally fsyncs the
file and its directory, for files that must survive a crash mid-scan.

Atomicity only protects a file being written. A file that was already damaged
-- truncated by a full disk, edited by hand into invalid JSON -- is the other
half of the problem: a store that reads it as "empty" and then rewrites it
turns one unreadable game's entry into every game's entry gone, so
``preserve_unreadable_file`` moves it aside before that rewrite happens.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from common.penguin_burner_paths import claim_desktop_user_ownership


def atomic_write_text(
    path: Path,
    text: str,
    *,
    durable: bool = False,
    claim_ownership: bool = True,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if claim_ownership:
        claim_desktop_user_ownership(path.parent, include_parents=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        if durable:
            handle.flush()
            os.fsync(handle.fileno())
    temp_path.replace(path)
    if durable:
        _fsync_directory(path.parent)
    if claim_ownership:
        claim_desktop_user_ownership(path)
    return path


def atomic_write_json(
    path: Path,
    payload: dict,
    *,
    durable: bool = False,
    claim_ownership: bool = True,
) -> Path:
    return atomic_write_text(
        path,
        json.dumps(payload, indent=2) + "\n",
        durable=durable,
        claim_ownership=claim_ownership,
    )


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def preserve_unreadable_file(path: Path) -> Path | None:
    """Move a file we could not parse aside, and say where it went.

    Returns the backup path, or None when there was nothing to preserve.
    Raises ``OSError`` when the file exists but cannot be moved -- the caller
    must then leave it alone rather than write over content it never read.

    The name is timestamped and never overwrites an earlier backup: a second
    corruption must not destroy the evidence of the first.
    """
    # lexists, not exists: a dangling symlink is still a file in the way,
    # and writing "through" it is exactly what must not happen here.
    if not os.path.lexists(path):
        return None
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}")
    suffix = 2
    while os.path.lexists(backup):
        backup = path.with_name(f"{path.name}.corrupt-{stamp}-{suffix}")
        suffix += 1
    os.replace(path, backup)
    return backup
