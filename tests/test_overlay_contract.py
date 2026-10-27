"""Cross-language contracts, including hand-edited config and unavailable data."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from overlay.config import (
    clamp_overlay_opacity,
    load_overlay_config,
    normalize_overlay_scale,
    save_overlay_config,
)
from overlay.metrics import (
    METRICS,
    OVERLAY_HUD_ROWS,
    OVERLAY_ITEM_IDS,
    OVERLAY_ITEM_SECTIONS,
)
from overlay.overlay_text import (
    format_overlay_item,
    format_settings_metric,
    metric_value,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("script", ["generate_overlay_contract.py", "generate_overlay_shortcuts.py"])
def test_generated_overlay_contract_is_current(script):
    subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--check"], check=True)


def test_every_visible_metric_has_metadata_and_a_settings_section():
    assert set(METRICS) == set(OVERLAY_ITEM_IDS)
    sections = [item for _, items in OVERLAY_ITEM_SECTIONS for item in items]
    assert sorted(sections) == sorted(METRICS)
    assert all(spec.label and spec.tooltip for spec in METRICS.values())
    assert all(item in METRICS for _, _, items in OVERLAY_HUD_ROWS for item in items)


@pytest.mark.parametrize("value,scale,opacity", [
    ("", 1.0, .85), ("broken", 1.0, .85), ("nan", 1.0, .85),
    ("inf", 1.0, .85), ("-inf", 1.0, .85), ("1junk", 1.0, .85),
    ("0x1p0", 1.0, .85), ("1_0", 1.0, .85),
    ("0", 1.0, .2), ("-1", 1.0, .2), ("0.1", .25, .2),
    ("0.85", .85, .85), ("1.13", 1.13, 1.0), ("9", 4.0, 1.0),
    (" 0.75 ", .75, .75),
])
def test_python_and_native_numeric_policy(native_overlay_probe, value, scale, opacity):
    result = subprocess.run([str(native_overlay_probe), "--config-numbers", value, value],
                            check=True, text=True, capture_output=True)
    assert tuple(map(float, result.stdout.split())) == (scale, opacity)
    assert normalize_overlay_scale(value) == scale
    assert clamp_overlay_opacity(value) == opacity


def test_unrelated_save_preserves_hand_edited_continuous_scale(tmp_path):
    path = tmp_path / "overlay.toml"
    path.write_text('enabled = true\nscale = 1.13\nopacity = 0.63\n')
    config = load_overlay_config(path)
    assert config.scale == 1.13
    save_overlay_config(config, path)
    assert load_overlay_config(path).scale == 1.13
    assert load_overlay_config(path).opacity == .63


def test_metric_value_keeps_units_labels_and_availability_separate():
    value = metric_value("temperature_c", {"temperature_c": "67"})
    assert (value.value, value.unit, value.label, value.available) == ("67", "C", "T", True)
    assert format_settings_metric("temperature_c", {"temperature_c": "67"}) == "67 °C"
    assert format_overlay_item("temperature_c", {"temperature_c": "67"}) == "T 67 C"
    assert not metric_value("temperature_c", {}).available
    assert format_settings_metric("nr_gpu_ms", {"nr_gpu_ms": "2.2"}) == "2.2 ms"
