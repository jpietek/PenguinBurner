"""The designed readout layout.

A headline frame rate, dim labels beside their values, rules between groups
and a quieter footer. The row table lives once per language and both renderers
draw from it, so this checks the layer's rows rather than either renderer's
pixels.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from overlay.config import OVERLAY_HUD_ROWS
from overlay.config import OVERLAY_ITEM_IDS
from overlay.config import OVERLAY_NON_TEXT_ITEM_IDS
from overlay.config import OverlayConfig
from overlay.overlay_text import ALERT_TEMPERATURE_C
from overlay.overlay_text import CELL_ALERT
from overlay.overlay_text import CELL_DIM
from overlay.overlay_text import CELL_GOOD
from overlay.overlay_text import CELL_NORMAL
from overlay.overlay_text import CELL_TIER
from overlay.overlay_text import CELL_WARN
from overlay.overlay_text import WARN_TEMPERATURE_C
from overlay.overlay_text import format_overlay_hud_rows


LIVE = {
    "present_fps": "119",
    "framegen_fps": "176",
    "framegen_active": "1",
    "latency_ms": "23",
    "clock_mhz": "2820",
    "voltage_mv": "925",
    "power_w": "214",
    "temperature_c": "67",
    "fan_pct": "62",
    "gpu_util_pct": "97",
    "uv_offset_mv": "-75",
    "profile_tier": "Balanced",
}


def _rows(items, values=None):
    return format_overlay_hud_rows(
        dict(LIVE) | (values or {}),
        config=OverlayConfig(enabled=True, enabled_item_ids=tuple(items)),
    )


def test_every_field_has_a_place_in_the_designed_layout() -> None:
    placed = [item for _kind, _label, items in OVERLAY_HUD_ROWS for item in items]

    assert sorted(placed) == sorted(
        set(OVERLAY_ITEM_IDS) - set(OVERLAY_NON_TEXT_ITEM_IDS)
    )
    assert len(placed) == len(set(placed)), "an item appears in two rows"


def test_the_frame_rate_leads_with_its_unit_beside_it() -> None:
    rows = _rows(["base_fps"])

    assert [row["kind"] for row in rows] == ["hero"]
    assert [cell["text"] for cell in rows[0]["cells"]] == ["119", "fps"]
    # The unit is dim so the number carries the row.
    assert rows[0]["cells"][1]["level"] == CELL_DIM


def test_a_group_with_nothing_switched_on_draws_no_row() -> None:
    rows = _rows(["base_fps", "latency_ms"])

    assert [row["label"] for row in rows if row["kind"] == "value"] == ["lat"]
    assert not any(row["kind"] == "rule" for row in rows)


def test_a_rule_never_opens_or_closes_the_readout() -> None:
    # Rules separate groups; one with nothing on either side is just a line.
    rows = _rows(["clock_mhz"])

    assert rows[0]["kind"] != "rule"
    assert rows[-1]["kind"] != "rule"


def test_the_second_gpu_row_continues_the_group_it_belongs_to() -> None:
    rows = _rows(["clock_mhz", "power_w", "temperature_c"])
    labels = [row["label"] for row in rows if row["kind"] == "value"]

    assert labels == ["gpu", ""]


def test_the_footer_collapses_the_device_fields_into_one_quiet_line() -> None:
    rows = _rows(
        ["base_fps", "gpu_name", "resolution"],
        {"gpu_name": "NVIDIA GeForce RTX 5070 Ti", "resolution": "2560x1440"},
    )
    foot = [row for row in rows if row["kind"] == "foot"]

    assert len(foot) == 1
    # The vendor words come off: they are the same on every card the overlay
    # runs on, and the footer is what sets the panel's width.
    assert foot[0]["cells"][0]["text"] == "RTX 5070 Ti | 2560x1440"
    assert foot[0]["cells"][0]["level"] == CELL_DIM


def test_the_tier_and_the_undervolt_carry_their_own_accents() -> None:
    # These two are what the tool exists to change, so they read differently
    # from the numbers it merely reports.
    rows = _rows(["profile", "uv_offset_mv"])
    levels = {
        row["label"]: row["cells"][0]["level"]
        for row in rows
        if row["kind"] == "value"
    }

    assert levels["tier"] == CELL_TIER
    assert levels["uv"] == CELL_GOOD


@pytest.mark.parametrize(
    ("celsius", "expected"),
    [
        ("50", CELL_GOOD),
        (str(WARN_TEMPERATURE_C - 1), CELL_GOOD),
        (str(WARN_TEMPERATURE_C), CELL_WARN),
        (str(ALERT_TEMPERATURE_C - 1), CELL_WARN),
        (str(ALERT_TEMPERATURE_C), CELL_ALERT),
        ("95", CELL_ALERT),
    ],
)
def test_temperature_warns_before_the_clocks_move(
    celsius: str,
    expected: int,
) -> None:
    # Earlier than the point a card throttles: on a tuning HUD the useful
    # moment is the one before the clocks drop.
    rows = _rows(["temperature_c"], {"temperature_c": celsius})

    assert rows[0]["cells"][0]["level"] == expected


def test_a_plain_value_stays_plain() -> None:
    rows = _rows(["clock_mhz"])

    assert rows[0]["cells"][0]["level"] == CELL_NORMAL


def test_the_layer_builds_the_same_rows(
    native_overlay_probe: Path,
    tmp_path: Path,
) -> None:
    items = ("base_fps", "latency_ms", "profile", "clock_mhz", "temperature_c",
             "uv_offset_mv")
    config_path = tmp_path / "overlay.toml"
    config_path.write_text(
        'version = 1\nenabled = true\nlayout = "hud"\nitems = ['
        + ", ".join(f'"{item}"' for item in items)
        + "]\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "overlay-state.txt"
    state_path.write_text(
        "".join(f"{key}={LIVE[key]}\n" for key in LIVE if key in LIVE)
        + "temperature_c=84\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(native_overlay_probe), "--hud-rows", "119"],
        check=True,
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PENGUIN_BURNER_OVERLAY_CONFIG": str(config_path),
            "PENGUIN_BURNER_OVERLAY_STATE": str(state_path),
            "PENGUIN_BURNER_OVERLAY_OVERRIDE": str(tmp_path / "absent"),
            "PB_OVERLAY": "1",
        },
    )
    native = [line for line in result.stdout.splitlines() if line.startswith("row ")]
    python = _rows(items, {"temperature_c": "84"})
    kinds = {"value": 0, "hero": 1, "rule": 2, "foot": 3}

    assert len(native) == len(python)
    for line, row in zip(native, python):
        head, *cells = line[len("row ") :].split("|")
        kind, _, label = head.partition(" ")
        assert int(kind) == kinds[row["kind"]]
        assert label == row["label"]
        assert cells == [
            f"{cell['level']}:{cell['text']}" for cell in row["cells"]
        ]
