#!/usr/bin/env python3
"""Generate the overlay's signed-distance-field font atlas.

The in-game overlay draws text at anything from 1x to 8x, because its pixel
size follows the display tier and then the user's own scale on top. A plain
coverage atlas goes soft the moment it is magnified past the size it was baked
at; a signed distance field stays crisp at every one of those scales for a
fraction of the storage, because the shader recovers the edge rather than the
pixels.

Glyphs are rasterised far above the cell size, the exact Euclidean distance to
the nearest edge is measured on that high-resolution mask, and the field is
then sampled down onto the cell grid. Running this needs a TTF and Pillow;
shipping it needs neither, because the result is a committed header.

Run from the repository root:

    python scripts/generate_overlay_font.py

``--check`` verifies the committed header still matches, which is what the test
suite uses.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sys

import numpy as np


FIRST_CODEPOINT = 0x20
LAST_CODEPOINT = 0x7E
GLYPH_COUNT = LAST_CODEPOINT - FIRST_CODEPOINT + 1

# Cell and em in atlas pixels. The em is smaller than the cell so ascenders,
# descenders and the distance spread all have room inside their own cell.
CELL = 32
EM = 22
# How far the field reaches past an edge, in cell pixels. Too small and the
# shader has nothing to antialias with; too large and thin stems lose contrast.
SPREAD = 4.0
# Supersampling for the mask the distances are measured on.
SUPERSAMPLE = 8

COLUMNS = 12
ROWS = (GLYPH_COUNT + COLUMNS - 1) // COLUMNS

HEADER_PATH = Path("overlay/native/latency_layer/src/overlay_font.h")

# Where each face may live. Distributions disagree about the directory, and the
# same upstream file hashes the same wherever it landed, so the first path that
# exists wins and the fingerprint is unaffected by which one it was.
FONT_CANDIDATES = {
    "mono": (
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ),
    # Roboto Medium rather than DejaVu Sans. Both carry tabular figures, which
    # a live readout needs -- proportional digits make the number jump as it
    # counts -- but Roboto sets about a tenth narrower for the same cap height.
    #
    # Medium rather than Regular because of what a HUD does to a light face.
    # Measured against MangoHud on the same frame, the regular weight put only
    # 6.6% of its inked pixels at full opacity where MangoHud's bold face
    # managed 55%: at body size a regular stem is about two pixels, and split
    # across a pixel boundary it never leaves a solid core, so the text reads
    # as a grey haze however large it is. Medium's stem is around a quarter
    # thicker and keeps its middle.
    # Package: ttf-roboto (Arch), fonts-roboto (Debian/Ubuntu).
    "sans": (
        "/usr/share/fonts/roboto/Roboto-Medium.ttf",
        "/usr/share/fonts/TTF/Roboto-Medium.ttf",
        "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Medium.ttf",
        str(Path.home() / ".fonts/Roboto-Medium.ttf"),
    ),
}
SYMBOLS = {"mono": "kOverlayFontMono", "sans": "kOverlayFontSans"}


def resolve_font(face: str) -> str:
    """First candidate path that exists, else the preferred one for the error."""
    for candidate in FONT_CANDIDATES[face]:
        if Path(candidate).exists():
            return candidate
    return FONT_CANDIDATES[face][0]


FONTS = {
    face: (resolve_font(face), SYMBOLS[face]) for face in FONT_CANDIDATES
}


@dataclass(frozen=True)
class Glyph:
    """One glyph's placement in the atlas and its metrics, in cell pixels."""

    cell_x: int
    cell_y: int
    bearing_x: float
    bearing_y: float
    advance: float


def _edt_1d(f: np.ndarray) -> np.ndarray:
    """Exact squared distance transform of one row (Felzenszwalb & Huttenlocher)."""
    n = f.shape[0]
    d = np.empty(n, dtype=np.float64)
    v = np.zeros(n, dtype=np.int64)
    z = np.empty(n + 1, dtype=np.float64)
    k = 0
    v[0] = 0
    z[0] = -np.inf
    z[1] = np.inf
    for q in range(1, n):
        while True:
            s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2.0 * q - 2.0 * v[k])
            if s <= z[k]:
                k -= 1
                if k < 0:
                    k = 0
                    break
            else:
                break
        k += 1
        v[k] = q
        z[k] = s
        z[k + 1] = np.inf
    k = 0
    for q in range(n):
        while z[k + 1] < q:
            k += 1
        d[q] = (q - v[k]) ** 2 + f[v[k]]
    return d


def squared_edt(mask: np.ndarray) -> np.ndarray:
    """Squared distance from every pixel to the nearest True pixel."""
    large = 1e12
    field = np.where(mask, 0.0, large)
    for axis in (0, 1):
        field = np.apply_along_axis(_edt_1d, axis, field)
    return field


def signed_distance(mask: np.ndarray) -> np.ndarray:
    """Distance to the nearest edge: negative inside the glyph, positive outside."""
    outside = np.sqrt(squared_edt(mask))
    inside = np.sqrt(squared_edt(~mask))
    return np.where(mask, -inside, outside)


def build_atlas(font_path: Path) -> tuple[np.ndarray, list[Glyph], dict[str, float]]:
    """Rasterise one font into an SDF atlas plus per-glyph metrics."""
    # Imported here, not at the top: only rasterising needs Pillow, and the
    # tests import this module's constants on machines without it (CI).
    from PIL import Image, ImageDraw, ImageFont

    hi_em = EM * SUPERSAMPLE
    font = ImageFont.truetype(str(font_path), hi_em)
    ascent, descent = font.getmetrics()

    # 0 is "far outside a glyph" in this encoding, which is what the unused
    # tail of the sheet has to read as: 255 would mean "deep inside", and
    # bilinear filtering would bleed that into the cells beside it.
    atlas = np.zeros((ROWS * CELL, COLUMNS * CELL), dtype=np.uint8)
    glyphs: list[Glyph] = []
    for index in range(GLYPH_COUNT):
        character = chr(FIRST_CODEPOINT + index)
        cell_x = (index % COLUMNS) * CELL
        cell_y = (index // COLUMNS) * CELL

        # Draw the glyph into a supersampled cell, with the baseline placed so
        # the tallest ascender and deepest descender both fit.
        hi_cell = CELL * SUPERSAMPLE
        image = Image.new("L", (hi_cell, hi_cell), 0)
        origin_x = (CELL - EM) * SUPERSAMPLE // 2
        origin_y = (hi_cell - (ascent + descent)) // 2
        ImageDraw.Draw(image).text(
            (origin_x, origin_y), character, font=font, fill=255
        )
        mask = np.asarray(image) > 127

        if mask.any():
            field = signed_distance(mask)
        else:
            # A blank cell is "far outside" everywhere, not "on an edge".
            field = np.full(mask.shape, SPREAD * SUPERSAMPLE * 2.0)
        # Sample the field at cell-pixel centres and convert to cell units.
        centres = (np.arange(CELL) * SUPERSAMPLE + SUPERSAMPLE // 2)
        sampled = field[np.ix_(centres, centres)] / SUPERSAMPLE
        # 0.5 is the edge; inside reads above it, which is what the shader
        # compares against.
        encoded = np.clip(0.5 - sampled / (2.0 * SPREAD), 0.0, 1.0) * 255.0
        atlas[cell_y : cell_y + CELL, cell_x : cell_x + CELL] = encoded.astype(np.uint8)

        glyphs.append(
            Glyph(
                cell_x=cell_x,
                cell_y=cell_y,
                bearing_x=(CELL - EM) / 2.0,
                bearing_y=(CELL - (ascent + descent) / SUPERSAMPLE) / 2.0,
                advance=font.getlength(character) / SUPERSAMPLE,
            )
        )

    # Cap height, not line height, is what a reader compares one face to
    # another by: the ascent carries room for accents nothing here draws.
    cap_box = font.getbbox("H")
    metrics = {
        "em": float(EM),
        "cell": float(CELL),
        "spread": SPREAD,
        "ascent": ascent / SUPERSAMPLE,
        "descent": descent / SUPERSAMPLE,
        "line_height": (ascent + descent) / SUPERSAMPLE,
        "cap_height": (cap_box[3] - cap_box[1]) / SUPERSAMPLE,
    }
    return atlas, glyphs, metrics


def source_fingerprint(fonts: dict[str, tuple[str, str]]) -> str:
    """Everything the atlas depends on, as one hash.

    Rasterising both fonts takes about two minutes, which is too slow to spend
    on every test run. The fingerprint lets a test assert the committed header
    still matches its inputs for the price of hashing two files.
    """
    digest = hashlib.sha256()
    digest.update(
        f"cell={CELL} em={EM} spread={SPREAD} ss={SUPERSAMPLE} "
        f"cols={COLUMNS} first={FIRST_CODEPOINT} last={LAST_CODEPOINT}".encode()
    )
    for name, (path, _symbol) in sorted(fonts.items()):
        digest.update(name.encode())
        digest.update(Path(path).read_bytes())
    return digest.hexdigest()


def render_header(fonts: dict[str, tuple[str, str]]) -> str:
    lines = [
        "// Generated by scripts/generate_overlay_font.py -- do not edit by hand.",
        "//",
        "// Signed-distance-field atlases for the overlay's text. 0.5 is the glyph",
        "// edge; the shader recovers a crisp outline from that at any scale, which",
        "// is what a coverage atlas cannot do once the HUD is magnified past the",
        "// size it was baked at.",
        "#ifndef PENGUINBURNER_OVERLAY_FONT_H",
        "#define PENGUINBURNER_OVERLAY_FONT_H",
        "",
        "#include <array>",
        "#include <cstdint>",
        "",
        "namespace pblayer {",
        "",
        "// sha256 of the generator's parameters and both source fonts. A test",
        "// compares this against the current inputs, which is far cheaper than",
        "// rasterising the atlases again just to find out they are unchanged.",
        f'constexpr const char* kOverlayFontFingerprint = "{source_fingerprint(fonts)}";',
        "",
        f"constexpr uint32_t kOverlayFontCell = {CELL};",
        f"constexpr uint32_t kOverlayFontColumns = {COLUMNS};",
        f"constexpr uint32_t kOverlayFontRows = {ROWS};",
        f"constexpr uint32_t kOverlayFontAtlasWidth = {COLUMNS * CELL};",
        f"constexpr uint32_t kOverlayFontAtlasHeight = {ROWS * CELL};",
        f"constexpr float kOverlayFontSpread = {SPREAD}f;",
        f"constexpr char kOverlayFontFirst = {FIRST_CODEPOINT:#04x};",
        f"constexpr char kOverlayFontLast = {LAST_CODEPOINT:#04x};",
        "",
        "// Metrics are in atlas cell pixels; the draw path scales them by the",
        "// overlay's own pixel scale.",
        "struct OverlayFontGlyph {",
        "    uint16_t cell_x;",
        "    uint16_t cell_y;",
        "    float bearing_x;",
        "    float bearing_y;",
        "    float advance;",
        "};",
        "",
        "struct OverlayFontAtlas {",
        "    const uint8_t* pixels;",
        "    const OverlayFontGlyph* glyphs;",
        "    float line_height;",
        "    float ascent;",
        "    float cap_height;",
        "};",
    ]
    for name, (path, symbol) in fonts.items():
        atlas, glyphs, metrics = build_atlas(Path(path))
        flat = atlas.reshape(-1)
        lines += [
            "",
            f"// {name}: {Path(path).name}",
            f"constexpr std::array<uint8_t, {flat.size}> {symbol}Pixels = {{",
        ]
        for start in range(0, flat.size, 32):
            row = ", ".join(str(int(value)) for value in flat[start : start + 32])
            lines.append(f"    {row},")
        lines += [
            "};",
            "",
            f"constexpr std::array<OverlayFontGlyph, {GLYPH_COUNT}> {symbol}Glyphs = {{{{",
        ]
        for index, glyph in enumerate(glyphs):
            label = "space" if index == 0 else chr(FIRST_CODEPOINT + index)
            if label == "\\":
                label = "backslash"
            lines.append(
                f"    {{{glyph.cell_x}, {glyph.cell_y}, {glyph.bearing_x:.4f}f, "
                f"{glyph.bearing_y:.4f}f, {glyph.advance:.4f}f}},  // {label}"
            )
        lines += [
            "}};",
            "",
            f"constexpr OverlayFontAtlas {symbol} = {{",
            f"    {symbol}Pixels.data(),",
            f"    {symbol}Glyphs.data(),",
            f"    {metrics['line_height']:.4f}f,",
            f"    {metrics['ascent']:.4f}f,",
            f"    {metrics['cap_height']:.4f}f,",
            "};",
        ]
    lines += ["", "}  // namespace pblayer", "", "#endif  // PENGUINBURNER_OVERLAY_FONT_H", ""]
    return "\n".join(lines)


def preview(font_path: Path, text: str, scale: int, out: Path) -> None:
    """Draw text from the atlas the way the shader will, for eyeballing.

    The GPU samples the field bilinearly and then recovers the edge, so the
    preview has to do the same: nearest-neighbour magnification would show
    staircases the shader never draws and make a good atlas look bad.
    """
    from PIL import Image

    atlas, glyphs, _metrics = build_atlas(font_path)
    width = int(sum(glyphs[ord(c) - FIRST_CODEPOINT].advance for c in text) * scale) + 8
    height = int(CELL * scale) + 8
    canvas = np.zeros((height, width), dtype=np.float64)
    field = atlas.astype(np.float64) / 255.0
    pen = 4.0
    for character in text:
        glyph = glyphs[ord(character) - FIRST_CODEPOINT]
        size = int(CELL * scale)
        # Texture coordinates of this cell's pixel centres, magnified.
        axis = (np.arange(size) + 0.5) / scale - 0.5
        axis = np.clip(axis, 0.0, CELL - 1.0)
        lo = np.floor(axis).astype(int)
        hi = np.minimum(lo + 1, CELL - 1)
        frac = axis - lo
        cell = field[
            glyph.cell_y : glyph.cell_y + CELL, glyph.cell_x : glyph.cell_x + CELL
        ]
        rows = cell[lo, :] * (1 - frac)[:, None] + cell[hi, :] * frac[:, None]
        sampled = rows[:, lo] * (1 - frac)[None, :] + rows[:, hi] * frac[None, :]
        # One cell pixel of field covers `scale` screen pixels, so the edge is
        # that many pixels wide in field units.
        softness = 1.0 / max(2.0 * SPREAD * scale, 1.0)
        alpha = np.clip((sampled - 0.5) / softness * 0.5 + 0.5, 0.0, 1.0)
        x0 = int(pen)
        patch = canvas[4 : 4 + size, x0 : x0 + size]
        canvas[4 : 4 + size, x0 : x0 + size] = np.maximum(
            patch, alpha[: patch.shape[0], : patch.shape[1]]
        )
        pen += glyph.advance * scale
    Image.fromarray((canvas * 255).astype(np.uint8)).save(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=HEADER_PATH)
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--preview-font", default="mono")
    parser.add_argument("--preview-text", default="119 FPS  1% low 29  2820 MHz")
    parser.add_argument("--preview-scale", type=int, default=2)
    args = parser.parse_args(argv)

    if args.preview:
        preview(
            Path(FONTS[args.preview_font][0]),
            args.preview_text,
            args.preview_scale,
            args.preview,
        )
        return 0

    rendered = render_header(FONTS)
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != rendered:
            print(
                f"{args.output} is stale; re-run scripts/generate_overlay_font.py",
                file=sys.stderr,
            )
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
