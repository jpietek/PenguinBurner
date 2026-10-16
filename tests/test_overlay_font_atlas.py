"""The overlay's signed-distance font atlas.

Rasterising both faces takes about two minutes, which is far too slow to spend
on every test run, so the committed header carries a fingerprint of everything
it was generated from and these tests check that instead.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest

from scripts.generate_overlay_font import CELL
from scripts.generate_overlay_font import COLUMNS
from scripts.generate_overlay_font import FONTS
from scripts.generate_overlay_font import GLYPH_COUNT
from scripts.generate_overlay_font import ROWS
from scripts.generate_overlay_font import source_fingerprint


REPO_ROOT = Path(__file__).resolve().parents[1]
FONT_HEADER = REPO_ROOT / "overlay/native/latency_layer/src/overlay_font.h"


def _header() -> str:
    return FONT_HEADER.read_text(encoding="utf-8")


def test_committed_atlas_matches_the_fonts_it_was_built_from() -> None:
    missing = [path for path, _ in FONTS.values() if not Path(path).exists()]
    if missing:
        pytest.skip(f"source fonts unavailable: {missing}")

    committed = re.search(
        r'kOverlayFontFingerprint = "([0-9a-f]{64})"', _header()
    )

    assert committed is not None, "header carries no fingerprint"
    assert committed.group(1) == source_fingerprint(FONTS)


@pytest.mark.parametrize("symbol", ["kOverlayFontMono", "kOverlayFontSans"])
def test_atlas_holds_one_cell_per_printable_character(symbol: str) -> None:
    header = _header()
    pixels = re.search(rf"std::array<uint8_t, (\d+)> {symbol}Pixels", header)
    glyphs = re.search(rf"std::array<OverlayFontGlyph, (\d+)> {symbol}Glyphs", header)

    assert pixels is not None and glyphs is not None
    assert int(pixels.group(1)) == COLUMNS * CELL * ROWS * CELL
    assert int(glyphs.group(1)) == GLYPH_COUNT


def test_the_unused_tail_of_the_sheet_reads_as_outside() -> None:
    """0 is "outside a glyph"; 255 would bleed into the cells beside it.

    The atlas grid has room for more cells than there are characters. Filling
    that remainder with 255 put a faint bar under the glyphs on the last used
    row, because bilinear filtering reached into it.
    """
    header = _header()
    body = header.split("kOverlayFontMonoPixels = {", 1)[1].split("};", 1)[0]
    values = [int(value) for value in re.findall(r"\b\d+\b", body)]
    row_stride = COLUMNS * CELL
    # The grid has one more cell than there are characters; that last cell is
    # the only part of the sheet no glyph occupies.
    cell_row, cell_col = divmod(GLYPH_COUNT, COLUMNS)
    spare = [
        values[(cell_row * CELL + y) * row_stride + cell_col * CELL + x]
        for y in range(CELL)
        for x in range(CELL)
    ]

    assert set(spare) == {0}


@pytest.mark.parametrize("font", ["pixel", "mono", "sans"])
@pytest.mark.parametrize("scale", [1, 2, 4])
def test_measuring_a_string_sums_its_glyph_advances(
    font: str,
    scale: int,
    native_overlay_probe: Path,
) -> None:
    # The column layout places every cell from these measurements, so the two
    # have to agree. The bitmap face is the one exception: its advance carries
    # a one-pixel gap that the last glyph of a row does not pay for.
    measured, summed, _advances = _measure(
        native_overlay_probe, font, scale, "min/avg/max 38/112/144"
    )
    trailing = scale if font == "pixel" else 0

    assert measured == pytest.approx(summed - trailing, abs=0.01)


def test_a_proportional_face_gives_every_character_its_own_width(
    native_overlay_probe: Path,
) -> None:
    """This is why the columns stopped being padded with spaces.

    DejaVu Sans puts 'i' at about a quarter of 'W'. Padding a cell with spaces
    to a character count lines nothing up once that is true.
    """
    _measured, _summed, advances = _measure(native_overlay_probe, "sans", 2, "iW")

    assert advances["i"] < advances["W"] / 3


@pytest.mark.parametrize("font", ["pixel", "mono"])
def test_the_fixed_pitch_faces_advance_identically(
    font: str,
    native_overlay_probe: Path,
) -> None:
    _measured, _summed, advances = _measure(native_overlay_probe, font, 2, "iWm.")

    assert len(set(advances.values())) == 1


def test_the_smooth_faces_measure_between_the_whole_scales(
    native_overlay_probe: Path,
) -> None:
    """A fractional scale has to land between its neighbours, not on one.

    While the bitmap face was the only one, sizes were whole multiples of a
    5x8 cell and anything between them was meaningless. The distance-field
    faces resample at any size, so 0.75x has to be a real size rather than a
    rounded-away alias of 0.5x or 1x.
    """
    half, _, _ = _measure(native_overlay_probe, "mono", 0.5, "2820 MHz")
    three_quarters, _, _ = _measure(native_overlay_probe, "mono", 0.75, "2820 MHz")
    whole, _, _ = _measure(native_overlay_probe, "mono", 1.0, "2820 MHz")

    assert half < three_quarters < whole


def _measure(
    binary: Path,
    font: str,
    scale: float,
    text: str,
) -> tuple[float, float, dict[str, float]]:
    result = subprocess.run(
        [str(binary), "--measure", font, str(scale), text],
        check=True,
        text=True,
        capture_output=True,
    )
    advances: dict[str, float] = {}
    measured = summed = 0.0
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts[0] == "advance":
            advances[chr(int(parts[1]))] = float(parts[2])
        elif parts[0] == "measured":
            measured, summed = float(parts[1]), float(parts[3])
    return measured, summed, advances
