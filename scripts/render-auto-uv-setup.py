#!/usr/bin/env python3
"""Render the real Auto-UV setup dialog using fixed example GPU facts."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "1")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402
from ui.dialogs import scan_tuning  # noqa: E402
from ui.features.tuning.tuning import AutoUvNvmlInfo  # noqa: E402
from ui.qt import apply_dark_palette  # noqa: E402
from ui.styles import STYLESHEET  # noqa: E402


def main() -> None:
    app = QtWidgets.QApplication([])
    apply_dark_palette(app, QtGui)
    app.setStyleSheet(STYLESHEET)
    name = "NVIDIA GeForce RTX 5080"
    info = AutoUvNvmlInfo(
        power_management_enabled=True,
        power_limit_set_supported=True,
        power_limit_w=360,
        power_limit_default_w=360,
        power_limit_min_w=300,
        power_limit_max_w=400,
        graphics_clock_mhz=210,
        memory_clock_mhz=405,
        supported_memory_clocks_mhz=(15001,),
        supported_graphics_clock_steps_mhz=(210, 3150),
    )
    output = ROOT / "docs/assets/auto-uv-setup.png"

    def capture(dialog):
        dialog.show()
        for button in dialog.findChildren(QtWidgets.QPushButton, "autoUvPresetButton"):
            if button.property("presetId") == "performance":
                button.click()
        app.processEvents()
        assert dialog.grab().save(str(output))
        dialog.hide()
        return QtWidgets.QDialog.Rejected

    client = SimpleNamespace(
        capabilities=lambda: SimpleNamespace(identity=SimpleNamespace(name=name))
    )
    with (
        patch.object(
            scan_tuning,
            "gpu_choices_with_fallback",
            return_value=([SimpleNamespace(index=0, label=name)], 0),
        ),
        patch.object(scan_tuning, "DaemonGpuClient", return_value=client),
        patch.object(scan_tuning, "read_auto_uv_nvml_info", return_value=info),
        patch.object(
            scan_tuning, "auto_uv_voltage_floor_range_mv", return_value=(725, 1240)
        ),
        patch.object(scan_tuning, "memory_offset_mhz_range", return_value=(0, 4000)),
        patch.object(QtWidgets.QDialog, "exec", capture),
    ):
        scan_tuning.select_scan_tuning(
            QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, parent=None, gpu_index=0
        )
    print(output)


if __name__ == "__main__":
    main()
