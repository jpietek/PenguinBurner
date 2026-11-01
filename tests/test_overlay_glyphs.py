"""The in-game overlay's 5x7 bitmap font.

The font ships as a generated header the Vulkan layer compiles in, so these
tests hold three things together: the committed header matches its generator,
the layer indexes the table the way the generator laid it out, and the glyphs
that shipped before full-ASCII support kept their exact pixels.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from scripts.generate_overlay_glyphs import ASCENT_ROWS
from scripts.generate_overlay_glyphs import FIRST_CODEPOINT
from scripts.generate_overlay_glyphs import GLYPH_ART
from scripts.generate_overlay_glyphs import GLYPH_HEIGHT
from scripts.generate_overlay_glyphs import LAST_CODEPOINT
from scripts.generate_overlay_glyphs import glyph_rows


REPO_ROOT = Path(__file__).resolve().parents[1]

# Every character the overlay could draw before the table went full-ASCII, with
# the pixels it drew in its original seven rows. Redrawing one of these would
# silently change how a shipped HUD reads, so the generator's art has to
# reproduce them exactly -- and none of them descends, so the eighth row the
# cell later grew has to stay empty for all of them.
LEGACY_GLYPHS = {
    "0": (0x0e, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0e),
    "1": (0x04, 0x0c, 0x04, 0x04, 0x04, 0x04, 0x0e),
    "2": (0x0e, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1f),
    "3": (0x1e, 0x01, 0x01, 0x0e, 0x01, 0x01, 0x1e),
    "4": (0x02, 0x06, 0x0a, 0x12, 0x1f, 0x02, 0x02),
    "5": (0x1f, 0x10, 0x1e, 0x01, 0x01, 0x11, 0x0e),
    "6": (0x06, 0x08, 0x10, 0x1e, 0x11, 0x11, 0x0e),
    "7": (0x1f, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
    "8": (0x0e, 0x11, 0x11, 0x0e, 0x11, 0x11, 0x0e),
    "9": (0x0e, 0x11, 0x11, 0x0f, 0x01, 0x02, 0x0c),
    "A": (0x0e, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11),
    "B": (0x1e, 0x11, 0x11, 0x1e, 0x11, 0x11, 0x1e),
    "C": (0x0e, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0e),
    "D": (0x1e, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1e),
    "E": (0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x1f),
    "F": (0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x10),
    "G": (0x0e, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0f),
    "H": (0x11, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11),
    "I": (0x0e, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0e),
    "J": (0x01, 0x01, 0x01, 0x01, 0x11, 0x11, 0x0e),
    "K": (0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11),
    "L": (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1f),
    "M": (0x11, 0x1b, 0x15, 0x15, 0x11, 0x11, 0x11),
    "N": (0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11),
    "O": (0x0e, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e),
    "P": (0x1e, 0x11, 0x11, 0x1e, 0x10, 0x10, 0x10),
    "Q": (0x0e, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0d),
    "R": (0x1e, 0x11, 0x11, 0x1e, 0x14, 0x12, 0x11),
    "S": (0x0f, 0x10, 0x10, 0x0e, 0x01, 0x01, 0x1e),
    "T": (0x1f, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    "U": (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e),
    "V": (0x11, 0x11, 0x11, 0x11, 0x11, 0x0a, 0x04),
    "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x1b, 0x11),
    "X": (0x11, 0x11, 0x0a, 0x04, 0x0a, 0x11, 0x11),
    "Y": (0x11, 0x11, 0x0a, 0x04, 0x04, 0x04, 0x04),
    "Z": (0x1f, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1f),
    "+": (0x00, 0x04, 0x04, 0x1f, 0x04, 0x04, 0x00),
    "-": (0x00, 0x00, 0x00, 0x1f, 0x00, 0x00, 0x00),
    "/": (0x01, 0x01, 0x02, 0x04, 0x08, 0x10, 0x10),
    "%": (0x19, 0x19, 0x02, 0x04, 0x08, 0x13, 0x13),
    ".": (0x00, 0x00, 0x00, 0x00, 0x00, 0x06, 0x06),
}


def test_committed_glyph_header_matches_its_generator() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/generate_overlay_glyphs.py", "--check"],
        check=False,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr


def test_generator_reproduces_every_legacy_glyph() -> None:
    redrawn = {
        character: tuple(glyph_rows(GLYPH_ART[character])[:ASCENT_ROWS])
        for character in LEGACY_GLYPHS
    }

    assert redrawn == LEGACY_GLYPHS


def test_legacy_glyphs_never_reach_into_the_descender_row() -> None:
    descenders = {
        character: glyph_rows(GLYPH_ART[character])[ASCENT_ROWS:]
        for character in LEGACY_GLYPHS
    }

    assert descenders == {character: [0] for character in LEGACY_GLYPHS}


@pytest.mark.parametrize("character", ["g", "j", "p", "q", "y", ","])
def test_descenders_use_the_row_below_the_baseline(character: str) -> None:
    # "avg" read as "avq" while the cell stopped at the baseline: the tail had
    # nowhere to go, so it curled back up into the bowl.
    rows = glyph_rows(GLYPH_ART[character])

    assert rows[ASCENT_ROWS] != 0


def test_layer_draws_the_generated_table_for_all_printable_ascii(
    native_overlay_probe: Path,
) -> None:
    text = "".join(chr(code) for code in range(FIRST_CODEPOINT, LAST_CODEPOINT + 1))

    drawn = _probe_glyphs(native_overlay_probe, text)

    assert drawn == {
        character: tuple(glyph_rows(GLYPH_ART[character])) for character in text
    }


def test_layer_no_longer_folds_lowercase_onto_capitals(
    native_overlay_probe: Path,
) -> None:
    drawn = _probe_glyphs(native_overlay_probe, "msMS")

    assert drawn["m"] != drawn["M"]
    assert drawn["s"] != drawn["S"]


@pytest.mark.parametrize("character", ["\t", "\x7f", "\x01"])
def test_layer_draws_blank_outside_the_table(
    character: str,
    native_overlay_probe: Path,
) -> None:
    drawn = _probe_glyphs(native_overlay_probe, character)

    assert drawn[character] == (0,) * GLYPH_HEIGHT


def _probe_glyphs(binary: Path, text: str) -> dict[str, tuple[int, ...]]:
    result = subprocess.run(
        [str(binary), "--glyphs", text],
        check=True,
        text=True,
        capture_output=True,
    )
    drawn: dict[str, tuple[int, ...]] = {}
    for line in result.stdout.splitlines():
        code, *rows = line.split()
        drawn[chr(int(code))] = tuple(int(row, 16) for row in rows)
    return drawn
