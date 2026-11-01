"""Layout harness for the native overlay text builder.

The Vulkan layer formats the in-game HUD in C++ (``overlay_text.cpp``) while
the desktop Overlay tab previews it in Python (``overlay/overlay_text.py``).
The two have drifted before -- the preview promised ``Fan 62%`` while the game
drew ``FAN 62%`` -- so these tests compile the layer's own formatter into a
host probe and diff it against the Python one over a shared case table.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from overlay.config import (
    OverlayConfig,
    save_overlay_config,
)
from overlay.metrics import (
    OVERLAY_ITEM_IDS,
)
from overlay.overlay_text import format_overlay_text


@dataclass(frozen=True)
class OverlayCase:
    """One set of inputs fed to both formatters.

    ``state`` is what the daemon publishes through overlay-state.txt. The
    remaining fields are what the layer measures itself from the present ring;
    they never reach the state file, so the harness feeds them to the probe as
    arguments and to the Python formatter as values.
    """

    name: str
    state: dict[str, str]
    items: tuple[str, ...]
    median_fps: int = 0
    low_1pct_fps: int = 0
    p97_frametime_us: int = 0
    session_min_fps: int = 0
    session_avg_fps: int = 0
    session_max_fps: int = 0
    # What the driver told the layer at swapchain creation. Never in the state
    # file: the desktop app cannot see a running game's device.
    gpu_name: str = ""
    driver_version: str = ""
    resolution: str = ""
    present_mode: str = ""


BASIC_ITEMS = (
    "base_fps",
    "fg_fps",
    "latency_ms",
    "clock_mhz",
    "voltage_mv",
    "power_w",
    "profile",
)

LIVE_STATE = {
    "clock_mhz": "2820",
    "voltage_mv": "925",
    "power_w": "318",
    "gpu_util_pct": "97",
    "cpu_util_pct": "32",
    "cpu_peak_thread_pct": "98",
    "fan_pct": "62",
    "temperature_c": "67",
    "hotspot_temperature_c": "79",
    "uv_offset_mv": "-75",
    "vram_used_mb": "9626",
    "vram_total_mb": "16376",
    "mem_clock_mhz": "10501",
    "ram_used_mb": "14540",
    "ram_total_mb": "32768",
    "cpu_temp_c": "61",
    "cpu_power_w": "88",
    "present_fps": "119",
    "framegen_fps": "176",
    "framegen_active": "1",
    "latency_ms": "23",
    "display_latency_ms": "4",
    "profile_tier": "Balanced",
    "profile_id": "uv_925mv_2820mhz",
}

EMPTY_STATE = {key: "" for key in LIVE_STATE} | {
    "framegen_active": "0",
    "profile_tier": "",
    "profile_id": "",
}

OVERLAY_CASES = (
    OverlayCase("basic-live", dict(LIVE_STATE), BASIC_ITEMS),
    OverlayCase("all-items-live", dict(LIVE_STATE), OVERLAY_ITEM_IDS),
    OverlayCase("all-items-empty", dict(EMPTY_STATE), OVERLAY_ITEM_IDS, 0),
    OverlayCase(
        "fps-from-the-layer",
        dict(LIVE_STATE) | {"present_fps": "", "framegen_fps": ""},
        BASIC_ITEMS,
        median_fps=144,
    ),
    OverlayCase(
        "framegen-off",
        dict(LIVE_STATE) | {"framegen_active": "0"},
        BASIC_ITEMS,
    ),
    OverlayCase(
        "no-display-tail",
        dict(LIVE_STATE) | {"display_latency_ms": ""},
        ("base_fps", "latency_ms"),
    ),
    OverlayCase(
        "stock-tier",
        dict(LIVE_STATE) | {"profile_tier": "", "profile_id": ""},
        ("profile",),
    ),
    OverlayCase(
        "custom-tier",
        dict(LIVE_STATE) | {"profile_tier": "Whisper Quiet"},
        ("profile",),
    ),
    OverlayCase(
        "requested-voltage-wins-over-live",
        dict(LIVE_STATE) | {"voltage_mv": "1010", "profile_id": "uv_875mv_2700mhz"},
        ("voltage_mv",),
    ),
    OverlayCase(
        "positive-uv-offset",
        dict(LIVE_STATE) | {"uv_offset_mv": "40"},
        ("uv_offset_mv",),
    ),
    OverlayCase(
        "everything-hidden",
        dict(EMPTY_STATE),
        ("fan_pct",),
    ),
    OverlayCase(
        "layer-measured-timing",
        dict(LIVE_STATE),
        OVERLAY_ITEM_IDS,
        median_fps=119,
        low_1pct_fps=94,
        p97_frametime_us=11234,
        session_min_fps=38,
        session_avg_fps=112,
        session_max_fps=144,
    ),
    OverlayCase(
        # Nothing measured yet: the timing row must vanish rather than claim a
        # 1% low of zero.
        "timing-before-the-ring-fills",
        dict(LIVE_STATE),
        ("base_fps", "fps_1pct_low", "frametime_p97", "fps_session"),
        median_fps=119,
    ),
    OverlayCase(
        # The graph is drawn, not written: on its own it leaves the readout
        # with nothing to say.
        "graph-contributes-no-text",
        dict(LIVE_STATE),
        ("frametime_graph",),
        median_fps=119,
    ),
    OverlayCase(
        "device-facts-from-the-driver",
        dict(LIVE_STATE),
        OVERLAY_ITEM_IDS,
        median_fps=119,
        low_1pct_fps=94,
        p97_frametime_us=11234,
        session_min_fps=38,
        session_avg_fps=112,
        session_max_fps=144,
        gpu_name="NVIDIA GeForce RTX 5070 Ti",
        driver_version="580.76",
        resolution="2560x1440",
        present_mode="fifo",
    ),
    OverlayCase(
        # Before a swapchain exists there is nothing to report, and an empty
        # device row must not be drawn.
        "device-facts-missing",
        dict(LIVE_STATE),
        ("base_fps", "gpu_name", "driver_version", "resolution", "present_mode"),
        median_fps=119,
    ),
    OverlayCase(
        # Only the vendor words come off. A card whose name carries neither
        # prefix, or only one of them, has to survive the trim intact.
        "device-facts-with-a-bare-model-name",
        dict(LIVE_STATE),
        ("base_fps", "gpu_name", "resolution"),
        median_fps=119,
        gpu_name="GeForce GTX 1080 Ti",
        resolution="3440x1440",
    ),
    OverlayCase(
        "device-facts-with-no-vendor-prefix",
        dict(LIVE_STATE),
        ("base_fps", "gpu_name", "resolution"),
        median_fps=119,
        gpu_name="Radeon RX 7900 XTX",
        resolution="3440x1440",
    ),
    OverlayCase(
        # The M2 fields, which arrive from the daemon rather than the layer.
        "host-and-memory-telemetry",
        dict(LIVE_STATE),
        ("base_fps", "mem_clock_mhz", "vram", "ram", "cpu_temp_c", "cpu_power_w"),
        median_fps=119,
    ),
    OverlayCase(
        # A board that reports no sensors must omit those rows, not show zeros.
        "host-telemetry-absent",
        dict(LIVE_STATE)
        | {
            "vram_used_mb": "",
            "vram_total_mb": "",
            "mem_clock_mhz": "",
            "ram_used_mb": "",
            "ram_total_mb": "",
            "cpu_temp_c": "",
            "cpu_power_w": "",
        },
        ("base_fps", "mem_clock_mhz", "vram", "ram", "cpu_temp_c", "cpu_power_w"),
        median_fps=119,
    ),
    OverlayCase(
        # A hot CPU colours later than a hot card: 88 C is warn on a CPU and
        # would already be alert on the GPU.
        "cpu-temperature-warns-later-than-the-card",
        dict(LIVE_STATE) | {"cpu_temp_c": "88", "temperature_c": "88"},
        ("base_fps", "cpu_temp_c", "temperature_c"),
        median_fps=119,
    ),
    OverlayCase(
        "session-stats-without-percentiles",
        dict(LIVE_STATE),
        ("fps_1pct_low", "fps_session"),
        median_fps=119,
        session_min_fps=38,
        session_avg_fps=112,
        session_max_fps=144,
    ),
)


@pytest.mark.parametrize("case", OVERLAY_CASES, ids=lambda case: case.name)
def test_native_and_python_overlay_text_agree(
    case: OverlayCase,
    native_overlay_probe: Path,
    tmp_path: Path,
) -> None:
    native = _run_native_overlay_probe(native_overlay_probe, tmp_path, case)
    python = format_overlay_text(
        case.state | _layer_measured_values(case),
        config=OverlayConfig(enabled=True, enabled_item_ids=case.items),
    )

    assert native == python


def test_native_overlay_env_true_overrides_disabled_config(
    native_overlay_probe: Path,
    tmp_path: Path,
) -> None:
    case = OverlayCase(
        "env-on",
        {
            "clock_mhz": "1777",
            "voltage_mv": "885",
            "power_w": "54",
            "latency_ms": "73",
            "profile_tier": "Performance",
        },
        ("base_fps", "latency_ms", "clock_mhz", "voltage_mv", "power_w", "profile"),
        median_fps=19,
    )

    native = _run_native_overlay_probe(
        native_overlay_probe, tmp_path, case, enabled=False, pb_overlay="1"
    )

    assert native == "19 fps\nlat  ~73 ms\ntier Performance\n--------------------\ngpu  1777 MHz 885 mV\n     54 W"


def test_native_overlay_env_false_still_disables_enabled_config(
    native_overlay_probe: Path,
    tmp_path: Path,
) -> None:
    case = OverlayCase("env-off", dict(LIVE_STATE), BASIC_ITEMS)

    native = _run_native_overlay_probe(
        native_overlay_probe, tmp_path, case, enabled=True, pb_overlay="0"
    )

    assert native == ""


@pytest.mark.parametrize(
    ("position", "origin"),
    [
        ("top_left", (10, 10)),
        ("top_center", (760, 10)),
        ("top_right", (1510, 10)),
        ("bottom_left", (10, 1040)),
        ("bottom_right", (1510, 1040)),
        # A name the layer does not know keeps the corner the overlay always had.
        ("middle_of_nowhere", (1510, 10)),
    ],
)
def test_native_overlay_places_the_box_where_the_config_asks(
    position: str,
    origin: tuple[int, int],
    native_overlay_probe: Path,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "overlay.toml"
    config_path.write_text(
        "version = 1\n"
        "enabled = true\n"
        f'position = "{position}"\n'
        'items = ["base_fps"]\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(native_overlay_probe), "--origin", "1920", "1080", "400", "30", "10"],
        check=True,
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PENGUIN_BURNER_OVERLAY_CONFIG": str(config_path),
            "PENGUIN_BURNER_OVERLAY_OVERRIDE": str(tmp_path / "absent-override"),
            "PB_OVERLAY": "1",
        },
    )

    assert tuple(int(value) for value in result.stdout.split()) == origin


def test_native_overlay_keeps_an_oversized_box_inside_the_image(
    native_overlay_probe: Path,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "overlay.toml"
    config_path.write_text(
        'version = 1\nenabled = true\nposition = "bottom_right"\nitems = ["base_fps"]\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(native_overlay_probe), "--origin", "320", "200", "900", "400", "10"],
        check=True,
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PENGUIN_BURNER_OVERLAY_CONFIG": str(config_path),
            "PENGUIN_BURNER_OVERLAY_OVERRIDE": str(tmp_path / "absent-override"),
            "PB_OVERLAY": "1",
        },
    )

    assert result.stdout.split() == ["0", "0"]


@pytest.mark.parametrize(
    ("samples", "expected"),
    [
        # Six frames at 120 FPS and a 40 ms hitch. The median stays at 120, the
        # tail measures report the hitch, and the session average lands between.
        (
            [8333] * 6 + [40000],
            {
                "median_fps": 120,
                "low_1pct_fps": 25,
                "p97_frametime_us": 40000,
                "session_min_fps": 25,
                "session_avg_fps": 78,
                "session_max_fps": 120,
            },
        ),
        # A perfectly even 60 FPS: every measure agrees.
        (
            [16667] * 20,
            {
                "median_fps": 60,
                "low_1pct_fps": 60,
                "p97_frametime_us": 16667,
                "session_min_fps": 60,
                "session_avg_fps": 60,
                "session_max_fps": 60,
            },
        ),
        # An empty ring measures nothing at all; zeros in the buffer are the
        # slots the ring has not filled yet, not zero-length frames.
        ([0, 0, 0], {"median_fps": 0, "low_1pct_fps": 0, "sample_count": 0}),
    ],
)
def test_native_frame_stats_read_the_present_ring(
    samples: list[int],
    expected: dict[str, int],
    native_overlay_probe: Path,
) -> None:
    result = subprocess.run(
        [
            str(native_overlay_probe),
            "--frame-stats",
            ",".join(str(sample) for sample in samples),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    measured = dict(
        pair.split("=", 1) for pair in result.stdout.split()
    )

    assert {key: int(measured[key]) for key in expected} == expected


def test_native_frame_stats_put_the_worst_frame_in_the_tail(
    native_overlay_probe: Path,
) -> None:
    # 99 good frames and one terrible one: the median must not notice, and the
    # 1% low must be the terrible one. That gap is the whole point of the field.
    samples = [8333] * 99 + [100000]

    result = subprocess.run(
        [
            str(native_overlay_probe),
            "--frame-stats",
            ",".join(str(sample) for sample in samples),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    measured = dict(pair.split("=", 1) for pair in result.stdout.split())

    assert int(measured["median_fps"]) == 120
    assert int(measured["low_1pct_fps"]) == 10
    assert int(measured["sample_count"]) == 100


@pytest.mark.parametrize(
    ("samples", "expected_samples", "expected_median"),
    [
        ([1000, 2000, 3000, 4000, 5000], [1000, 2000, 3000, 4000, 5000], 3000),
        # More presents than the ring holds: the snapshot keeps the newest 120
        # in the order they happened, not in the order they sit in the buffer.
        (list(range(1, 131)), list(range(11, 131)), 71),
        ([], [], 0),
    ],
)
def test_native_graph_snapshot_reads_the_ring_in_order(
    samples: list[int],
    expected_samples: list[int],
    expected_median: int,
    native_overlay_probe: Path,
) -> None:
    result = subprocess.run(
        [
            str(native_overlay_probe),
            "--graph",
            ",".join(str(sample) for sample in samples),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    head, _, sample_text = result.stdout.strip().partition(" samples=")
    fields = dict(pair.split("=", 1) for pair in head.split())
    drawn = [int(value) for value in sample_text.split(",") if value]

    assert drawn == expected_samples
    assert int(fields["count"]) == len(expected_samples)
    assert int(fields["median_us"]) == expected_median


@pytest.mark.parametrize(
    ("samples", "height", "expected_span_us", "expected_heights"),
    [
        # A steady run is measured against twice its median, so it draws as a
        # flat band at half height rather than as amplified noise.
        ([16667] * 4, 28, 33334, [14, 14, 14, 14]),
        # One hitch anchors the scale and fills the graph; the good frames
        # shrink to show how far out of line it is.
        ([8333, 8333, 8333, 50000], 28, 50000, [5, 5, 5, 28]),
        # A frame that was measured always leaves a mark, however quick.
        ([100, 50000], 28, 100000, [1, 14]),
    ],
)
def test_native_graph_bars_scale_against_the_window(
    samples: list[int],
    height: int,
    expected_span_us: int,
    expected_heights: list[int],
    native_overlay_probe: Path,
) -> None:
    result = subprocess.run(
        [
            str(native_overlay_probe),
            "--graph-bars",
            str(height),
            ",".join(str(sample) for sample in samples),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    span_text, _, heights_text = result.stdout.strip().partition(" heights=")

    assert int(span_text.split("=", 1)[1]) == expected_span_us
    assert [int(value) for value in heights_text.split(",")] == expected_heights


@pytest.mark.parametrize(
    ("line", "max_chars", "expected"),
    [
        ("min/avg/max 38/112/144", 30, "min/avg/max 38/112/144"),
        ("min/avg/max 38/112/144", 22, "min/avg/max 38/112/144"),
        # One glyph too narrow: the last one drawn becomes the marker.
        ("min/avg/max 38/112/144", 21, "min/avg/max 38/112/1>"),
        ("min/avg/max 38/112/144", 15, "min/avg/max 38>"),
        ("min/avg/max 38/112/144", 1, ">"),
        ("min/avg/max 38/112/144", 0, ""),
    ],
)
def test_native_marks_a_row_it_had_to_cut(
    line: str,
    max_chars: int,
    expected: str,
    native_overlay_probe: Path,
) -> None:
    result = subprocess.run(
        [str(native_overlay_probe), "--fit-line", str(max_chars), line],
        check=True,
        text=True,
        capture_output=True,
    )

    assert result.stdout.rstrip("\n") == expected
    assert len(result.stdout.rstrip("\n")) <= max_chars


@pytest.mark.parametrize(
    ("temperature_c", "expected_level"),
    [
        # One threshold table for both layouts: a value where you want it
        # reads good (level 4), 72 C warns (1), 80 C alerts (2).
        ("67", 4),
        ("71", 4),
        ("72", 1),
        ("79", 1),
        ("80", 2),
        ("103", 2),
        # Nothing published, nothing to colour.
        ("", None),
        ("n/a", None),
    ],
)
def test_native_colours_the_temperature_cell_by_threshold(
    temperature_c: str,
    expected_level: int | None,
    native_overlay_probe: Path,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "overlay.toml"
    config_path.write_text(
        'version = 1\nenabled = true\nlayout = "table"\n'
        'items = ["base_fps", "power_w", "temperature_c"]\n',
        encoding="utf-8",
    )
    state_path = tmp_path / "overlay-state.txt"
    state_path.write_text(
        f"power_w=318\ntemperature_c={temperature_c}\n", encoding="utf-8"
    )

    result = subprocess.run(
        [str(native_overlay_probe), "--highlights", "119"],
        check=True,
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PENGUIN_BURNER_OVERLAY_CONFIG": str(config_path),
            "PENGUIN_BURNER_OVERLAY_STATE": str(state_path),
            "PENGUIN_BURNER_OVERLAY_OVERRIDE": str(tmp_path / "absent-override"),
            "PB_OVERLAY": "1",
        },
    )
    lines = result.stdout.splitlines()
    spans = [line.split()[1:] for line in lines if line.startswith("highlight")]
    readout = [line for line in lines if not line.startswith("highlight")]

    if expected_level is None:
        assert spans == []
        return
    assert len(spans) == 1
    line_index, column, length, level = (int(value) for value in spans[0])
    assert level == expected_level
    # The span has to survive column padding, so check it lands on the cell.
    assert readout[line_index][column : column + length] == f"T {temperature_c} C"


@pytest.mark.parametrize(
    ("cpu_temp_c", "expected_level"),
    [
        # A CPU is expected to run hotter than a card, so the colours arrive
        # later: 88 C is merely warm here and would already be alert on the GPU.
        ("61", 4),
        ("84", 4),
        ("85", 1),
        ("94", 1),
        ("95", 2),
        ("", None),
    ],
)
def test_native_colours_the_cpu_temperature_on_its_own_thresholds(
    cpu_temp_c: str,
    expected_level: int | None,
    native_overlay_probe: Path,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "overlay.toml"
    config_path.write_text(
        'version = 1\nenabled = true\nlayout = "table"\n'
        'items = ["base_fps", "cpu_temp_c"]\n',
        encoding="utf-8",
    )
    state_path = tmp_path / "overlay-state.txt"
    state_path.write_text(f"cpu_temp_c={cpu_temp_c}\n", encoding="utf-8")

    result = subprocess.run(
        [str(native_overlay_probe), "--highlights", "119"],
        check=True,
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PENGUIN_BURNER_OVERLAY_CONFIG": str(config_path),
            "PENGUIN_BURNER_OVERLAY_STATE": str(state_path),
            "PENGUIN_BURNER_OVERLAY_OVERRIDE": str(tmp_path / "absent-override"),
            "PB_OVERLAY": "1",
        },
    )
    lines = result.stdout.splitlines()
    spans = [line.split()[1:] for line in lines if line.startswith("highlight")]

    if expected_level is None:
        assert spans == []
        return
    assert len(spans) == 1
    assert int(spans[0][3]) == expected_level


def test_native_leaves_power_uncoloured_without_a_board_limit(
    native_overlay_probe: Path,
    tmp_path: Path,
) -> None:
    # An absolute watt threshold means nothing without the card's limit, and
    # overlay-state.txt does not publish one, so power stays the default colour
    # however high it reads.
    config_path = tmp_path / "overlay.toml"
    config_path.write_text(
        'version = 1\nenabled = true\nlayout = "table"\nitems = ["power_w"]\n',
        encoding="utf-8",
    )
    state_path = tmp_path / "overlay-state.txt"
    state_path.write_text("power_w=600\n", encoding="utf-8")

    result = subprocess.run(
        [str(native_overlay_probe), "--highlights", "119"],
        check=True,
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PENGUIN_BURNER_OVERLAY_CONFIG": str(config_path),
            "PENGUIN_BURNER_OVERLAY_STATE": str(state_path),
            "PENGUIN_BURNER_OVERLAY_OVERRIDE": str(tmp_path / "absent-override"),
            "PB_OVERLAY": "1",
        },
    )

    assert "highlight" not in result.stdout


@pytest.mark.parametrize(
    ("version", "vendor_id", "expected"),
    [
        # NVIDIA packs its own scheme; a generic Vulkan decode prints nonsense
        # for the exact cards this tool exists for.
        ((580 << 22) | (76 << 14), 0x10DE, "580.76"),
        ((535 << 22) | (183 << 14) | (1 << 6), 0x10DE, "535.183"),
        # Intel on Linux is Mesa ANV, which uses the standard layout: Mesa
        # 24.2.3. The Windows-only 18/14-bit decode printed "6144.8195".
        ((24 << 22) | (2 << 12) | 3, 0x8086, "24.2.3"),
        # Everyone else gets the standard Vulkan layout.
        ((1 << 22) | (3 << 12) | 268, 0x1002, "1.3.268"),
    ],
)
def test_native_decodes_driver_version_the_way_the_vendor_packed_it(
    version: int,
    vendor_id: int,
    expected: str,
    native_overlay_probe: Path,
) -> None:
    result = subprocess.run(
        [str(native_overlay_probe), "--driver-version", str(version), str(vendor_id)],
        check=True,
        text=True,
        capture_output=True,
    )

    assert result.stdout.strip() == expected


def _layer_measured_values(case: OverlayCase) -> dict[str, str]:
    """What the layer measures, as the Python formatter expects to see it.

    The C++ builder takes these as an OverlayFrameStats argument; the Python
    formatter only has the values dict, so the harness folds them in. The
    microsecond-to-millisecond rounding is stated here on purpose: it is part
    of what the two sides have to agree on.
    """
    values: dict[str, str] = {}
    if not str(case.state.get("present_fps") or "").strip() and case.median_fps:
        values["present_fps"] = str(case.median_fps)
    if case.low_1pct_fps:
        values["fps_1pct_low"] = str(case.low_1pct_fps)
    if case.p97_frametime_us:
        values["frametime_p97"] = f"{case.p97_frametime_us / 1000.0:.1f}"
    if case.session_avg_fps:
        values["fps_session_min"] = str(case.session_min_fps)
        values["fps_session_avg"] = str(case.session_avg_fps)
        values["fps_session_max"] = str(case.session_max_fps)
    for key in ("gpu_name", "driver_version", "resolution", "present_mode"):
        value = getattr(case, key)
        if value:
            values[key] = value
    return values


def _run_native_overlay_probe(
    binary: Path,
    tmp_path: Path,
    case: OverlayCase,
    *,
    enabled: bool = True,
    pb_overlay: str = "1",
) -> str:
    config_path = tmp_path / "overlay.toml"
    save_overlay_config(
        OverlayConfig(enabled=enabled, enabled_item_ids=case.items),
        config_path,
    )
    state_path = tmp_path / "overlay-state.txt"
    state_path.write_text(
        "".join(f"{key}={value}\n" for key, value in case.state.items()),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(binary),
            str(case.median_fps),
            str(case.low_1pct_fps),
            str(case.p97_frametime_us),
            str(case.session_min_fps),
            str(case.session_avg_fps),
            str(case.session_max_fps),
        ],
        check=True,
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PENGUIN_BURNER_OVERLAY_CONFIG": str(config_path),
            "PENGUIN_BURNER_OVERLAY_STATE": str(state_path),
            # Point the live on/off override at a path that cannot exist, so a
            # stale override on the developer's box cannot decide the result.
            "PENGUIN_BURNER_OVERLAY_OVERRIDE": str(tmp_path / "absent-override"),
            "PB_OVERLAY": pb_overlay,
            "PB_PROBE_GPU_NAME": case.gpu_name,
            "PB_PROBE_DRIVER_VERSION": case.driver_version,
            "PB_PROBE_RESOLUTION": case.resolution,
            "PB_PROBE_PRESENT_MODE": case.present_mode,
        },
    )
    return result.stdout.strip("\n")


@pytest.mark.parametrize(
    ("launcher", "config_enabled"),
    [("steam", False), ("lutris", True), ("heroic", None)],
)
def test_native_overlay_switches_on_and_off_in_same_process(live_overlay_probe,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launcher: str,
    config_enabled: bool | None,
) -> None:
    """A live overlay toggle must reach a game that is already running.

    Every launcher writes the same override file, and the layer re-reads it
    inside the running process, so turning the overlay on or off from the
    Game Library must change the next frame rather than the next launch.
    """
    from integrations.launchers.registry import build_sources
    from integrations.launchers.wrapper_manager import ApplyResult
    from overlay.state import OVERLAY_OVERRIDE_ENV

    binary = live_overlay_probe
    config_path = tmp_path / "overlay.toml"
    if config_enabled is not None:
        config_path.write_text(
            f'enabled = {str(config_enabled).lower()}\nitems = ["base_fps"]\n',
            encoding="utf-8",
        )
    override = tmp_path / "overlay-override"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PENGUIN_BURNER_OVERLAY_CONFIG": str(config_path),
        "PB_OVERLAY": "0",
        OVERLAY_OVERRIDE_ENV: str(override),
        "PENGUIN_BURNER_GAME_KEY": f"{launcher}:native-probe",
        "PENGUIN_BURNER_SESSION_ID": "overlay-contract-test",
    }
    monkeypatch.setenv(OVERLAY_OVERRIDE_ENV, str(override))
    source = next(
        source
        for source in build_sources(home=tmp_path)
        if source.launcher_id == launcher
    )
    monkeypatch.setattr(source, "running_game_ids", lambda: frozenset({"native-probe"}))

    with subprocess.Popen(
        [str(binary), "--live"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None
        assert process.stdin is not None
        assert process.stdout.readline().strip() == ""
        for enabled in (True, False, True):
            monkeypatch.setattr(
                source, "saved_overlay", lambda _id, enabled=enabled: enabled
            )
            result = (
                source.after_setting_write("native-probe", "set_game_overlay")
                if enabled
                else source.after_bulk_write(
                    "set_all_games_overlay",
                    ApplyResult(True, "saved", applied_game_ids=("native-probe",)),
                )
            )
            assert result.ok
            process.stdin.write("tick\n")
            process.stdin.flush()
            text = process.stdout.readline().strip()
            assert ("19 fps" in text) is enabled
        process.stdin.close()
        assert process.wait(timeout=5) == 0


@pytest.mark.parametrize("layout", ["hud", "table"])
@pytest.mark.parametrize("detail", ["custom", "full"])
@pytest.mark.parametrize("hotspot", ["79", "", "n/a", None])
def test_native_hotspot_matches_python_in_both_layouts_and_detail_modes(
    native_overlay_probe, tmp_path, layout, detail, hotspot,
) -> None:
    from overlay.config import load_overlay_config

    config_path = tmp_path / "overlay.toml"
    state_path = tmp_path / "overlay-state.txt"
    save_overlay_config(
        OverlayConfig(
            enabled=True, layout=layout, detail=detail,
            enabled_item_ids=("base_fps", "temperature_c", "hotspot_temperature_c"),
        ),
        config_path,
    )
    values = {"present_fps": "19", "temperature_c": "67"}
    if hotspot is not None:
        values["hotspot_temperature_c"] = hotspot
    state_path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PENGUIN_BURNER_OVERLAY_CONFIG": str(config_path),
        "PENGUIN_BURNER_OVERLAY_STATE": str(state_path),
        "PENGUIN_BURNER_OVERLAY_OVERRIDE": str(tmp_path / "absent-override"),
        "PB_OVERLAY": "1",
    }
    result = subprocess.run(
        [str(native_overlay_probe), "19"], env=env,
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == format_overlay_text(
        values, config=load_overlay_config(config_path),
    ).strip()
    label = "hotspot" if layout == "hud" else "HOT"
    assert (label in result.stdout) == (hotspot == "79")
    assert "67 C" in result.stdout


@pytest.mark.parametrize("layout", ["hud", "table"])
def test_gui_hotspot_toggle_persists_and_updates_running_native_overlay(live_overlay_probe,
    tmp_path, monkeypatch, qtbot, layout,
) -> None:
    from PySide6 import QtCore, QtWidgets

    from overlay.config import load_overlay_config
    from ui.components.overlay_config import OverlayConfigPanel

    binary = live_overlay_probe
    config_path = tmp_path / "overlay.toml"
    state_path = tmp_path / "overlay-state.txt"
    monkeypatch.setenv("PENGUIN_BURNER_OVERLAY_STATE", str(state_path))
    state_path.write_text("present_fps=19\ntemperature_c=67\nhotspot_temperature_c=79\n")
    save_overlay_config(
        OverlayConfig(
            enabled=True, layout=layout,
            enabled_item_ids=("base_fps", "temperature_c"),
        ),
        config_path,
    )
    panel = OverlayConfigPanel(QtCore=QtCore, QtWidgets=QtWidgets, config_path=config_path)
    qtbot.addWidget(panel.widget)
    panel.widget.resize(1500, 1000)
    panel.widget.show()
    panel.section_edit_buttons["GPU"].click()
    checkbox = panel.item_checkboxes["hotspot_temperature_c"]
    assert panel.item_title_labels["hotspot_temperature_c"].text() == "Hotspot temperature"
    assert not checkbox.isChecked()
    assert panel.item_value_labels["hotspot_temperature_c"].text() == "79 °C"

    env = {
        "PATH": os.environ.get("PATH", ""),
        "PENGUIN_BURNER_OVERLAY_CONFIG": str(config_path),
        "PENGUIN_BURNER_OVERLAY_STATE": str(state_path),
        "PENGUIN_BURNER_OVERLAY_OVERRIDE": str(tmp_path / "absent-override"),
        "PB_OVERLAY": "1",
    }
    with subprocess.Popen(
        [str(binary), "--live"], env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True,
    ) as process:
        assert process.stdin is not None and process.stdout is not None

        def read_matches_preview(*, tick=True):
            if tick:
                process.stdin.write("tick\n")
                process.stdin.flush()
            text = process.stdout.readline().strip()
            assert text == format_overlay_text(
                panel.preview._values, config=panel.config,
            ).replace("\n", " ").strip()
            return text

        label = "hotspot" if layout == "hud" else "HOT"
        assert label not in read_matches_preview(tick=False)
        qtbot.mouseClick(checkbox, QtCore.Qt.LeftButton)
        saved = load_overlay_config(config_path)
        assert "hotspot_temperature_c" in saved.enabled_item_ids
        assert saved.detail == "custom"
        assert label in read_matches_preview()
        rows = panel.preview._rows()
        if layout == "hud":
            assert any(row["label"] == "hotspot" for row in rows)
        else:
            assert any("HOT 79 C" in cell["text"] for row in rows for cell in row["cells"])

        reopened = OverlayConfigPanel(
            QtCore=QtCore, QtWidgets=QtWidgets, config_path=config_path,
        )
        qtbot.addWidget(reopened.widget)
        assert reopened.item_checkboxes["hotspot_temperature_c"].isChecked()
        assert reopened.config.layout == layout

        # Missing keys from older daemons and failed reads must not use demo 79 C.
        for hotspot in ("", None, "81"):
            reading = "" if hotspot is None else f"hotspot_temperature_c={hotspot}\n"
            state_path.write_text("present_fps=19\ntemperature_c=67\n" + reading)
            panel.refresh_preview()
            assert panel.item_value_labels["hotspot_temperature_c"].text() == (
                f"{hotspot} °C" if hotspot else "-"
            )
            text = read_matches_preview()
            assert (label in text) == bool(hotspot)
            assert "67 C" in text
            if hotspot:
                assert "81 C" in text

        qtbot.mouseClick(checkbox, QtCore.Qt.LeftButton)
        assert "hotspot_temperature_c" not in load_overlay_config(config_path).enabled_item_ids
        assert label not in read_matches_preview()
        process.stdin.close()
        assert process.wait(timeout=5) == 0


@pytest.fixture(scope="session")
def live_overlay_probe(tmp_path_factory, build_overlay_probe) -> Path:
    """Build a probe that keeps formatting in one process, tick after tick.

    The shared ``native_overlay_probe`` fixture prints once and exits, which
    cannot show a toggle landing mid-session; this one holds the layer's
    formatter open and reformats on every line it reads from stdin.
    """
    tmp_path = tmp_path_factory.mktemp("live-overlay-probe")
    source = tmp_path / "live_overlay_probe.cpp"
    source.write_text(
        r"""
#include <chrono>
#include <iostream>
#include <string>
#include <thread>

#include "latency_layer_internal.h"

// The readout is several rows, but the test reads one line per tick, so the
// rows travel joined and the caller stays in step with the ticks it sent.
std::string one_line(const std::string& readout) {
    std::string flat;
    for (char ch : readout) {
        flat += (ch == '\n') ? ' ' : ch;
    }
    return flat;
}

int main(int argc, char**) {
    pblayer::OverlayFrameStats stats{};
    stats.median_fps = 19;
    stats.sample_count = 1;
    pblayer::OverlayDeviceFacts facts{};
    pblayer::OverlayGpuState state{};
    state.available = true;
    state.clock_mhz = "1777";
    state.voltage_mv = "885";
    state.power_w = "54";
    state.latency_ms = "73";
    const char* nr_value = std::getenv("TEST_NR_GPU_MS");
    state.nr_gpu_ms = nr_value ? nr_value : "2.20";
    state.profile_tier = "Performance";
    const bool use_file = std::getenv("PENGUIN_BURNER_OVERLAY_STATE") != nullptr;
    if (use_file) state = pblayer::read_overlay_gpu_state();
    uint64_t now_us = 1000000;
    std::cout << one_line(
                     pblayer::build_overlay_text(stats, facts, state, now_us))
              << std::endl;
    std::string command;
    while (argc > 1 && std::getline(std::cin, command)) {
        // The layer caches the config for a second, so a toggle only shows
        // once that window has passed -- which is what a game would see.
        std::this_thread::sleep_for(std::chrono::milliseconds(1100));
        now_us += 1100000;
        if (use_file) state = pblayer::read_overlay_gpu_state();
        std::cout << one_line(
                         pblayer::build_overlay_text(stats, facts, state, now_us))
                  << std::endl;
    }
    return 0;
}
""",
        encoding="utf-8",
    )
    output = tmp_path / "live_overlay_probe"
    build_overlay_probe(source, output)
    return output


def test_native_table_gives_each_cpu_metric_its_own_label(
    native_overlay_probe: Path,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "overlay.toml"
    config_path.write_text(
        'version = 1\nenabled = true\nlayout = "table"\n'
        'items = ["cpu_peak_thread_pct", "cpu_temp_c", "cpu_power_w"]\n',
        encoding="utf-8",
    )
    state_path = tmp_path / "overlay-state.txt"
    state_path.write_text(
        "cpu_peak_thread_pct=98\ncpu_temp_c=61\ncpu_power_w=88\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(native_overlay_probe), "119"],
        check=True,
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PENGUIN_BURNER_OVERLAY_CONFIG": str(config_path),
            "PENGUIN_BURNER_OVERLAY_STATE": str(state_path),
            "PENGUIN_BURNER_OVERLAY_OVERRIDE": str(tmp_path / "absent-override"),
            "PB_OVERLAY": "1",
        },
    )

    assert "CPU-T 98%" in result.stdout
    assert "CPU 61 C" in result.stdout
    assert "CPU 88 W" in result.stdout
    assert result.stdout.count("CPU-T") == 1


@pytest.mark.parametrize("nr_value", ["2.20", "", "-"])
def test_native_nr_gpu_format_matches_python(live_overlay_probe, tmp_path, nr_value):
    from overlay.overlay_text import format_overlay_item

    binary = live_overlay_probe
    config = tmp_path / "overlay.toml"
    config.write_text('enabled = true\nlayout = "table"\nitems = ["nr_gpu_ms"]\n')
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PENGUIN_BURNER_OVERLAY_CONFIG": str(config),
        "PENGUIN_BURNER_OVERLAY_OVERRIDE": str(tmp_path / "absent"),
        "PB_OVERLAY": "1",
        "TEST_NR_GPU_MS": nr_value,
    }
    result = subprocess.run([str(binary)], check=True, text=True, capture_output=True, env=env)
    assert result.stdout.strip() == format_overlay_item("nr_gpu_ms", {"nr_gpu_ms": nr_value})


@pytest.fixture(scope="module")
def latency_probe(live_overlay_probe) -> Path:
    return live_overlay_probe


@pytest.mark.parametrize("average", ["80", ""])
@pytest.mark.parametrize(
    "source", ["input-to-gpu", "simulation-to-gpu", "submit-to-gpu", "marker-estimate", ""]
)
def test_native_latency_state_matches_python(
    latency_probe: Path, tmp_path: Path, average: str, source: str
) -> None:
    from overlay.config import OverlayConfig
    from overlay.overlay_text import format_overlay_text

    values = {
        "latency_avg_ms": average, "latency_ms": "120",
        "latency_source": source, "display_latency_ms": "9",
    }
    state_path = tmp_path / "overlay-state.txt"
    state_path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    config_path = tmp_path / "overlay.toml"
    config_path.write_text('enabled = true\nlayout = "table"\nitems = ["latency_ms"]\n')
    env = {"PATH": os.environ.get("PATH", ""), "PB_OVERLAY": "1", "PENGUIN_BURNER_OVERLAY_CONFIG": str(config_path), "PENGUIN_BURNER_OVERLAY_OVERRIDE": str(tmp_path / "absent")}
    env["PENGUIN_BURNER_OVERLAY_STATE"] = str(state_path)
    result = subprocess.run(
        [str(latency_probe), "--state"], check=True, capture_output=True, text=True, env=env
    )
    expected = format_overlay_text(
        values, config=OverlayConfig(enabled=True, layout="table", enabled_item_ids=("latency_ms",))
    )
    assert result.stdout.strip() == expected
