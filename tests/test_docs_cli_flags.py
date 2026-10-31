"""Guard against documentation drift: every PenguinBurner CLI flag mentioned in
the user docs must still exist in the argument parser.

Scoped to the flag families PenguinBurner owns (``--auto-uv-*``, ``--auto-oc-*``,
``--adaptive-*``, ``--lact-*``, ``--overlay-*``) so third-party flags in the docs
(pip, gamescope)
do not cause false failures.
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest

REPO = Path(__file__).resolve().parent.parent
DOC_FILES = [
    REPO / "README.md",
    REPO / "docs" / "install.md",
    *(REPO / "docs" / "features").glob("*.md"),
]
OWNED_PREFIXES = ("--auto-uv-", "--auto-oc-", "--adaptive-", "--lact-", "--overlay-")
FLAG_RE = re.compile(r"--[a-z][a-z0-9-]+")


def _documented_flags() -> set[str]:
    flags: set[str] = set()
    for path in DOC_FILES:
        if not path.exists():
            continue
        for token in FLAG_RE.findall(path.read_text(encoding="utf-8")):
            if token.startswith(OWNED_PREFIXES):
                flags.add(token)
    return flags


def _cli_source() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8") for p in (REPO / "cli").glob("*.py")
    )


@pytest.mark.parametrize("flag", sorted(_documented_flags()))
def test_documented_flag_exists_in_parser(flag: str) -> None:
    assert flag in _cli_source(), (
        f"{flag} is documented but not defined in cli/*.py — "
        "update the docs or the parser."
    )


def test_some_flags_were_found() -> None:
    # Sanity: the docs really do mention owned flags, so the guard is live.
    assert _documented_flags(), "no PenguinBurner CLI flags found in the docs"
