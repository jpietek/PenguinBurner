"""The overlay's blended-quad renderer.

Nothing here needs a GPU: the shaders are compiled ahead of time into a
committed header, and the geometry the pipeline draws is built by pure
functions the probe can call. What a test cannot cover is whether a driver
actually accepts the pipeline -- that is what the clear-rect fallback is for.
"""

from __future__ import annotations

from pathlib import Path
import re
import struct
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SHADER_HEADER = REPO_ROOT / "overlay/native/latency_layer/src/overlay_shaders.h"
SPIRV_MAGIC = 0x07230203


def test_committed_shader_header_matches_its_sources() -> None:
    import shutil

    if shutil.which("glslangValidator") is None and shutil.which("glslang") is None:
        pytest.skip("glslang unavailable; cannot recompile the shaders to compare")
    result = subprocess.run(
        [sys.executable, "scripts/generate_overlay_shaders.py", "--check"],
        check=False,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "symbol", ["kOverlayVertexSpirv", "kOverlayFragmentSpirv"]
)
def test_embedded_spirv_survives_the_trip_through_a_c_header(symbol: str) -> None:
    words = _spirv_words(symbol)

    assert words, f"{symbol} is empty"
    assert words[0] == SPIRV_MAGIC


@pytest.mark.parametrize(
    "symbol", ["kOverlayVertexSpirv", "kOverlayFragmentSpirv"]
)
def test_embedded_spirv_is_valid(symbol: str, tmp_path: Path) -> None:
    import shutil

    if shutil.which("spirv-val") is None:
        pytest.skip("spirv-val unavailable")
    words = _spirv_words(symbol)
    blob = tmp_path / f"{symbol}.spv"
    blob.write_bytes(struct.pack(f"<{len(words)}I", *words))

    result = subprocess.run(
        ["spirv-val", str(blob)], check=False, text=True, capture_output=True
    )

    assert result.returncode == 0, result.stderr


def test_rounded_panel_bands_tile_the_box_without_overlapping(
    native_overlay_probe: Path,
) -> None:
    # Two translucent quads stacked on top of each other blend twice and draw a
    # visible seam, so the panel has to be built from slices that touch but do
    # not overlap -- and that between them cover every row.
    bands = _panel_bands(native_overlay_probe, 0, 0, 20, 10, 3)

    rows: dict[int, list[tuple[int, int]]] = {}
    for x, y, width, height in bands:
        for row in range(y, y + height):
            rows.setdefault(row, []).append((x, x + width))
    assert sorted(rows) == list(range(10))
    for spans in rows.values():
        spans.sort()
        for first, second in zip(spans, spans[1:]):
            assert first[1] <= second[0], "bands overlap on a row"


def test_rounded_panel_narrows_towards_the_corners(
    native_overlay_probe: Path,
) -> None:
    bands = _panel_bands(native_overlay_probe, 0, 0, 20, 10, 3)
    by_row = {y: width for _x, y, width, _h in bands}

    assert by_row[0] < by_row[1] <= by_row[2] < by_row[3]
    assert by_row[0] == by_row[9]
    assert by_row[3] == 20


def test_a_radius_too_big_for_the_box_draws_one_plain_rectangle(
    native_overlay_probe: Path,
) -> None:
    bands = _panel_bands(native_overlay_probe, 5, 5, 8, 6, 5)

    assert bands == [(5, 5, 8, 6)]


@pytest.mark.parametrize(
    ("x", "y", "expected"),
    [
        (0, 0, (-1.0, -1.0)),
        (1920, 1080, (1.0, 1.0)),
        (960, 540, (0.0, 0.0)),
    ],
)
def test_pixel_edges_map_onto_the_clip_space_grid(
    x: int,
    y: int,
    expected: tuple[float, float],
    native_overlay_probe: Path,
) -> None:
    result = subprocess.run(
        [str(native_overlay_probe), "--ndc", str(x), str(y), "1920", "1080"],
        check=True,
        text=True,
        capture_output=True,
    )
    measured = tuple(float(value) for value in result.stdout.split())

    assert measured == pytest.approx(expected)


def _spirv_words(symbol: str) -> list[int]:
    header = SHADER_HEADER.read_text(encoding="utf-8")
    body = header.split(f"{symbol} = {{", 1)[1].split("};", 1)[0]
    return [int(word, 16) for word in re.findall(r"0x[0-9a-f]{8}", body)]


def _panel_bands(
    binary: Path,
    x: int,
    y: int,
    width: int,
    height: int,
    radius: int,
) -> list[tuple[int, int, int, int]]:
    result = subprocess.run(
        [
            str(binary),
            "--panel-bands",
            str(x),
            str(y),
            str(width),
            str(height),
            str(radius),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    return [
        tuple(int(value) for value in line.split())  # type: ignore[misc]
        for line in result.stdout.splitlines()
        if line.strip()
    ]
