#!/usr/bin/env python3
"""Generate the native overlay's 5x7 bitmap font header.

The in-game overlay draws every glyph as cleared rectangles, one run of set
pixels at a time, so its font is a table of 7 five-bit rows per character.
Hand-maintaining 95 rows of hex is how a font ends up with a wrong pixel that
nobody notices, so the readable pixel art below is the source of truth and
``overlay/native/latency_layer/src/overlay_glyphs.h`` is generated from it.

Run from the repository root:

    python scripts/generate_overlay_glyphs.py

``--check`` verifies the committed header still matches, which is what the
test suite uses.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


# Printable ASCII, 0x20 through 0x7E. '#' is a set pixel, '.' is clear.
#
# The cell is five wide and eight tall. Capitals and digits fill rows 0-6 and
# sit on the baseline at the bottom of row 6; lowercase shares that baseline
# with a five-row x-height starting at row 2. Row 7 exists only for descenders
# (g j p q y, and the comma's tail), so an entry may be written with seven rows
# and is padded at the bottom -- which is why every glyph that does not descend
# reads exactly as it did when the cell was seven rows tall.
GLYPH_ART: dict[str, list[str]] = {
    " ": [".....", ".....", ".....", ".....", ".....", ".....", "....."],
    "!": ["..#..", "..#..", "..#..", "..#..", "..#..", ".....", "..#.."],
    '"': [".#.#.", ".#.#.", ".....", ".....", ".....", ".....", "....."],
    "#": [".#.#.", ".#.#.", "#####", ".#.#.", "#####", ".#.#.", ".#.#."],
    "$": ["..#..", ".####", "#.#..", ".###.", "..#.#", "####.", "..#.."],
    "%": ["##..#", "##..#", "...#.", "..#..", ".#...", "#..##", "#..##"],
    "&": [".##..", "#..#.", "#.#..", ".#...", "#.#.#", "#..#.", ".##.#"],
    "'": ["..#..", "..#..", ".....", ".....", ".....", ".....", "....."],
    "(": ["...#.", "..#..", ".#...", ".#...", ".#...", "..#..", "...#."],
    ")": [".#...", "..#..", "...#.", "...#.", "...#.", "..#..", ".#..."],
    "*": [".....", "#.#.#", ".###.", "#####", ".###.", "#.#.#", "....."],
    "+": [".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."],
    ",": [".....", ".....", ".....", ".....", ".....", "..##.", "..##.", ".#..."],
    "-": [".....", ".....", ".....", "#####", ".....", ".....", "....."],
    ".": [".....", ".....", ".....", ".....", ".....", "..##.", "..##."],
    "/": ["....#", "....#", "...#.", "..#..", ".#...", "#....", "#...."],
    "0": [".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."],
    "1": ["..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "2": [".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"],
    "3": ["####.", "....#", "....#", ".###.", "....#", "....#", "####."],
    "4": ["...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."],
    "5": ["#####", "#....", "####.", "....#", "....#", "#...#", ".###."],
    "6": ["..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."],
    "7": ["#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."],
    "8": [".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."],
    "9": [".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."],
    ":": [".....", "..##.", "..##.", ".....", "..##.", "..##.", "....."],
    ";": [".....", ".....", "..##.", "..##.", ".....", "..##.", "..##.", ".#..."],
    "<": ["...#.", "..#..", ".#...", "#....", ".#...", "..#..", "...#."],
    "=": [".....", ".....", "#####", ".....", "#####", ".....", "....."],
    ">": [".#...", "..#..", "...#.", "....#", "...#.", "..#..", ".#..."],
    "?": [".###.", "#...#", "....#", "...#.", "..#..", ".....", "..#.."],
    "@": [".###.", "#...#", "#.###", "#.#.#", "#.###", "#....", ".###."],
    "A": [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "B": ["####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."],
    "C": [".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."],
    "D": ["####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."],
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
    "F": ["#####", "#....", "#....", "####.", "#....", "#....", "#...."],
    "G": [".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".####"],
    "H": ["#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "I": [".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "J": ["....#", "....#", "....#", "....#", "#...#", "#...#", ".###."],
    "K": ["#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"],
    "L": ["#....", "#....", "#....", "#....", "#....", "#....", "#####"],
    "M": ["#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"],
    "N": ["#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "P": ["####.", "#...#", "#...#", "####.", "#....", "#....", "#...."],
    "Q": [".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"],
    "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    "S": [".####", "#....", "#....", ".###.", "....#", "....#", "####."],
    "T": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."],
    "U": ["#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "V": ["#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."],
    "W": ["#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"],
    "X": ["#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"],
    "Y": ["#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."],
    "Z": ["#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"],
    "[": [".###.", ".#...", ".#...", ".#...", ".#...", ".#...", ".###."],
    "\\": ["#....", "#....", ".#...", "..#..", "...#.", "....#", "....#"],
    "]": [".###.", "...#.", "...#.", "...#.", "...#.", "...#.", ".###."],
    "^": ["..#..", ".#.#.", "#...#", ".....", ".....", ".....", "....."],
    "_": [".....", ".....", ".....", ".....", ".....", ".....", "#####"],
    "`": [".#...", "..#..", ".....", ".....", ".....", ".....", "....."],
    "a": [".....", ".....", ".###.", "....#", ".####", "#...#", ".####"],
    "b": ["#....", "#....", "####.", "#...#", "#...#", "#...#", "####."],
    "c": [".....", ".....", ".###.", "#....", "#....", "#....", ".###."],
    "d": ["....#", "....#", ".####", "#...#", "#...#", "#...#", ".####"],
    "e": [".....", ".....", ".###.", "#...#", "#####", "#....", ".###."],
    "f": ["..##.", ".#...", ".#...", "####.", ".#...", ".#...", ".#..."],
    "g": [".....", ".....", ".####", "#...#", "#...#", ".####", "....#", ".###."],
    "h": ["#....", "#....", "####.", "#...#", "#...#", "#...#", "#...#"],
    "i": ["..#..", ".....", ".##..", "..#..", "..#..", "..#..", ".###."],
    "j": ["...#.", ".....", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."],
    "k": ["#....", "#....", "#..#.", "#.#..", "##...", "#.#..", "#..#."],
    "l": [".##..", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "m": [".....", ".....", "##.#.", "#.#.#", "#.#.#", "#...#", "#...#"],
    "n": [".....", ".....", "####.", "#...#", "#...#", "#...#", "#...#"],
    "o": [".....", ".....", ".###.", "#...#", "#...#", "#...#", ".###."],
    "p": [".....", ".....", "####.", "#...#", "#...#", "####.", "#....", "#...."],
    "q": [".....", ".....", ".####", "#...#", "#...#", ".####", "....#", "....#"],
    "r": [".....", ".....", "#.##.", "##..#", "#....", "#....", "#...."],
    "s": [".....", ".....", ".####", "#....", ".###.", "....#", "####."],
    "t": [".#...", ".#...", "####.", ".#...", ".#...", ".#..#", "..##."],
    "u": [".....", ".....", "#...#", "#...#", "#...#", "#..##", ".##.#"],
    "v": [".....", ".....", "#...#", "#...#", "#...#", ".#.#.", "..#.."],
    "w": [".....", ".....", "#...#", "#...#", "#.#.#", "#.#.#", ".#.#."],
    "x": [".....", ".....", "#...#", ".#.#.", "..#..", ".#.#.", "#...#"],
    "y": [".....", ".....", "#...#", "#...#", "#...#", ".####", "....#", ".###."],
    "z": [".....", ".....", "#####", "...#.", "..#..", ".#...", "#####"],
    "{": ["..##.", ".#...", ".#...", "##...", ".#...", ".#...", "..##."],
    "|": ["..#..", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."],
    "}": [".##..", "...#.", "...#.", "...##", "...#.", "...#.", ".##.."],
    "~": [".....", ".....", ".#...", "#.#.#", "...#.", ".....", "....."],
}

GLYPH_LABELS = {" ": "space", "\\": "backslash"}

GLYPH_WIDTH = 5
GLYPH_HEIGHT = 8
# Rows an entry may omit from the bottom; only descenders spell out all eight.
ASCENT_ROWS = 7
FIRST_CODEPOINT = 0x20
LAST_CODEPOINT = 0x7E

HEADER_PATH = Path("overlay/native/latency_layer/src/overlay_glyphs.h")


def glyph_rows(art: list[str]) -> list[int]:
    """Pack one glyph's pixel art into 8 rows of 5 bits, high bit leftmost.

    Art with only the ascent rows is padded at the bottom, so a glyph that does
    not descend is written the same way it always was.
    """
    if len(art) not in (ASCENT_ROWS, GLYPH_HEIGHT):
        raise ValueError(
            f"expected {ASCENT_ROWS} or {GLYPH_HEIGHT} rows, got {len(art)}"
        )
    rows = []
    for line in art:
        if len(line) != GLYPH_WIDTH:
            raise ValueError(f"expected {GLYPH_WIDTH} columns, got {len(line)!r}")
        bits = 0
        for column, pixel in enumerate(line):
            if pixel == "#":
                bits |= 1 << (GLYPH_WIDTH - 1 - column)
            elif pixel != ".":
                raise ValueError(f"expected '#' or '.', got {pixel!r}")
        rows.append(bits)
    rows.extend([0] * (GLYPH_HEIGHT - len(rows)))
    return rows


def render_header() -> str:
    missing = [
        chr(code)
        for code in range(FIRST_CODEPOINT, LAST_CODEPOINT + 1)
        if chr(code) not in GLYPH_ART
    ]
    if missing:
        raise ValueError(f"no pixel art for {missing!r}")

    lines = [
        "// Generated by scripts/generate_overlay_glyphs.py -- do not edit by hand.",
        "//",
        "// 5x7 bitmap font for the in-game overlay, covering printable ASCII",
        "// (0x20-0x7E). Each entry is 7 rows of 5 bits, most significant bit",
        "// leftmost; edit the pixel art in the generator and re-run it instead.",
        "#ifndef PENGUINBURNER_OVERLAY_GLYPHS_H",
        "#define PENGUINBURNER_OVERLAY_GLYPHS_H",
        "",
        "#include <array>",
        "#include <cstdint>",
        "",
        "namespace pblayer {",
        "",
        f"constexpr uint32_t kOverlayGlyphWidth = {GLYPH_WIDTH};",
        f"constexpr uint32_t kOverlayGlyphHeight = {GLYPH_HEIGHT};",
        f"constexpr char kOverlayGlyphFirst = {FIRST_CODEPOINT:#04x};",
        f"constexpr char kOverlayGlyphLast = {LAST_CODEPOINT:#04x};",
        "",
        "constexpr std::array<",
        "    std::array<uint8_t, kOverlayGlyphHeight>,",
        f"    {LAST_CODEPOINT - FIRST_CODEPOINT + 1}>",
        "    kOverlayGlyphs = {{",
    ]
    for code in range(FIRST_CODEPOINT, LAST_CODEPOINT + 1):
        character = chr(code)
        rows = ", ".join(f"0x{value:02x}" for value in glyph_rows(GLYPH_ART[character]))
        # A trailing backslash in a // comment splices the next line into it,
        # which silently swallowed the following glyph; name it instead.
        label = GLYPH_LABELS.get(character, character)
        lines.append(f"    {{{{{rows}}}}},  // {label}")
    lines += [
        "}};",
        "",
        "}  // namespace pblayer",
        "",
        "#endif  // PENGUINBURNER_OVERLAY_GLYPHS_H",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail instead of writing when the committed header is stale",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=HEADER_PATH,
        help=f"header to write (default: {HEADER_PATH})",
    )
    args = parser.parse_args(argv)

    rendered = render_header()
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != rendered:
            print(
                f"{args.output} is stale; re-run scripts/generate_overlay_glyphs.py",
                file=sys.stderr,
            )
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
