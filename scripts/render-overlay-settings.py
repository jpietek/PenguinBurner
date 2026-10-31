#!/usr/bin/env python3
"""Render the source Overlay settings with deterministic, private sample data."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "2.5")
os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from overlay.config import (  # noqa: E402
    OverlayConfig,
    save_overlay_config,
)
from overlay.metrics import (  # noqa: E402
    OVERLAY_ITEM_IDS,
    OVERLAY_ITEM_SECTIONS,
)
from overlay.overlay_text import SAMPLE_OVERLAY_VALUES  # noqa: E402
from ui.components import overlay_config  # noqa: E402
from ui.qt import apply_dark_palette  # noqa: E402
from ui.styles import STYLESHEET  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/assets/overlay.png")
    parser.add_argument("--width", type=int, default=1430, help="Logical width in pixels.")
    parser.add_argument("--height", type=int, default=784, help="Logical height in pixels.")
    parser.add_argument(
        "--edit-section", default="GPU",
        choices=["none", *(title for title, _items in OVERLAY_ITEM_SECTIONS)],
        help="Show a section's inline choices, or none for summaries only.",
    )
    args = parser.parse_args()
    if args.width <= 0 or args.height <= 0:
        parser.error("Width and height must be positive.")

    samples = dict(SAMPLE_OVERLAY_VALUES)
    samples.update(
        clock_mhz="2850", voltage_mv="950", mem_clock_mhz="15000",
        power_w="245", temperature_c="62", hotspot_temperature_c="74",
        gpu_util_pct="96", fan_pct="48", profile_tier="Performance",
        uv_offset_mv="-100", cpu_util_pct="34", cpu_peak_thread_pct="78",
        cpu_temp_c="58", cpu_power_w="65", vram_used_mb="8397",
        vram_total_mb="16384", ram_used_mb="12698", ram_total_mb="32768",
        gpu_name="NVIDIA GeForce RTX 5080", driver_version="Example driver",
        resolution="3840x2160", present_mode="mailbox", nr_gpu_ms="2.2",
    )
    selected = {
        "base_fps", "fg_fps", "latency_ms", "nr_gpu_ms", "fps_1pct_low",
        "power_w", "hotspot_temperature_c", "gpu_util_pct", "uv_offset_mv", "profile",
    }
    config = OverlayConfig(
        enabled=True,
        enabled_item_ids=tuple(item for item in OVERLAY_ITEM_IDS if item in selected),
        scale=0.75, font="sans", opacity=0.85,
    )
    app = QtWidgets.QApplication([])
    app.setFont(QtGui.QFont("Noto Sans", 11))
    apply_dark_palette(app, QtGui)
    app.setStyleSheet(STYLESHEET)

    with TemporaryDirectory(prefix="penguinburner-overlay-render-") as temporary:
        config_path = Path(temporary) / "overlay.toml"
        save_overlay_config(config, config_path)
        # Use the production widget and formatters; only the input readings are
        # fixtures. Never read or modify the user's actual overlay settings.
        with (
            patch.object(overlay_config, "read_overlay_state", return_value=samples),
            patch.object(overlay_config, "SAMPLE_OVERLAY_VALUES", samples),
        ):
            panel = overlay_config.OverlayConfigPanel(
                QtCore=QtCore, QtWidgets=QtWidgets, config_path=config_path,
            )
            panel.timer.stop()
            panel.widget.resize(args.width, args.height)
            if args.edit_section != "none":
                panel.section_edit_buttons[args.edit_section].click()
            panel.widget.show()
            for _ in range(6):
                app.processEvents()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            capture = panel.widget.grab()
            if not capture.save(str(args.output)):
                raise SystemExit(f"Could not save {args.output}")
            print(
                f"Captured {len(panel.item_checkboxes)} source metrics "
                f"at {capture.width()}×{capture.height()} to {args.output}"
            )
            panel.widget.close()
            panel.widget.deleteLater()
            app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    main()
