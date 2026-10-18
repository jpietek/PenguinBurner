from __future__ import annotations

import html
from pathlib import Path

from overlay.config import (
    ADVANCED_OVERLAY_ITEM_IDS,
    BASIC_OVERLAY_ITEM_IDS,
    MAX_OVERLAY_UPDATE_INTERVAL_S,
    MIN_OVERLAY_UPDATE_INTERVAL_S,
    OVERLAY_SCALE_OPTIONS,
    load_overlay_config,
    save_overlay_config,
    set_overlay_item_enabled,
    set_overlay_scale,
    set_overlay_update_interval_s,
    snap_overlay_scale,
)
from overlay.overlay_text import (
    SAMPLE_OVERLAY_VALUES,
    format_overlay_item,
    preview_overlay_text,
)
from overlay.state import read_overlay_state

ITEM_LABELS = {
    "base_fps": "Base FPS",
    "fg_fps": "FG FPS",
    "clock_mhz": "Clock MHz",
    "voltage_mv": "Voltage mV",
    "power_w": "Power W",
    "profile": "Profile",
    "gpu_util_pct": "GPU %",
    "cpu_util_pct": "CPU %",
    "cpu_peak_thread_pct": "CPU-T %",
    "fan_pct": "Fan %",
    "temperature_c": "Temp C",
    "latency_ms": "Latency ms",
    "uv_offset_mv": "UV offset mV",
}

ITEM_TELEMETRY_KEYS = {
    "clock_mhz": "clock_mhz",
    "voltage_mv": "voltage_mv",
    "power_w": "power_w",
    "profile": "profile_tier",
    "gpu_util_pct": "gpu_util_pct",
    "cpu_util_pct": "cpu_util_pct",
    "cpu_peak_thread_pct": "cpu_peak_thread_pct",
    "fan_pct": "fan_pct",
    "temperature_c": "temperature_c",
    "uv_offset_mv": "uv_offset_mv",
}

ITEM_VALUE_WIDTH_EXAMPLES = (
    "9999 FPS",
    "9999 MHz",
    "9999 mV",
    "CPU-T 9999%",
    "T 9999 C",
    "UV -9999 mV",
)

ITEM_TOOLTIPS = {
    "base_fps": (
        "Exact base present FPS published by PenguinBurner. This is the "
        "pre-frame-generation present_fps headline, not the output FG cadence."
    ),
    "fg_fps": (
        "Frame-generation output FPS. It is shown only when PenguinBurner marks "
        "frame generation as active."
    ),
    "clock_mhz": "Current GPU graphics clock in MHz.",
    "voltage_mv": "Current live GPU voltage in mV.",
    "power_w": "Current GPU board power draw in watts.",
    "profile": "Compact active profile tier: BAL, EFF, PERF, or a shortened custom tier.",
    "gpu_util_pct": "Current GPU utilization (0-100%) from NVML. Missing data is hidden.",
    "cpu_util_pct": (
        "Current game process CPU usage from procfs, normalized to total system "
        "CPU capacity so it stays in the 0-100% range. Missing process data is "
        "hidden."
    ),
    "cpu_peak_thread_pct": (
        "Busiest game thread over the last overlay interval, normalized to one "
        "logical CPU. Values near 100% indicate a likely single-thread or main "
        "thread bottleneck. This is not tied to one fixed core because the "
        "scheduler may migrate the thread."
    ),
    "fan_pct": "Current reported GPU fan speed percentage. Missing fan data is hidden.",
    "temperature_c": "Current GPU temperature in C. Missing temperature data is hidden.",
    "latency_ms": (
        "NVIDIA App-style PC latency for the game: render (Reflex input-to-present "
        "span, including the frame-gen hold) plus the present->scanout display "
        "tail, added automatically when supported. Measuring it injects a drop-in "
        "nvapi64.dll into the game's Proton prefix to tap the game's own Reflex "
        "markers, so it works even under frame generation; without the shim a "
        "native Vulkan marker fallback covers the non-frame-gen case. Not exactly "
        "click-to-photon -- mouse/USB input and panel pixel response are excluded."
    ),
    "uv_offset_mv": (
        "Current voltage delta from the stock VF point for the nearest active "
        "operating point. Missing VF data is hidden."
    ),
}


class OverlayConfigPanel:
    def __init__(
        self,
        *,
        QtCore,
        QtWidgets,
        config_path: str | Path | None = None,
    ):
        self.QtCore = QtCore
        self.QtWidgets = QtWidgets
        self.config_path = Path(config_path).expanduser() if config_path else None
        self.config = load_overlay_config(self.config_path)
        self._syncing = False
        self.item_checkboxes = {}
        self.item_value_labels = {}

        self.widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(self.widget)
        layout.setContentsMargins(12, 18, 12, 12)
        layout.setSpacing(14)

        settings_row = QtWidgets.QHBoxLayout()
        settings_row.setSpacing(8)
        interval_label = QtWidgets.QLabel("Update interval")
        interval_tooltip = _wrapped_tooltip(
            "Controls how often the daemon refreshes the overlay state. Range is "
            "1 to 10 seconds, default 2 seconds. Changes update the running "
            "runtime loop live."
        )
        interval_label.setToolTip(interval_tooltip)
        self.update_interval_spin = QtWidgets.QSpinBox()
        self.update_interval_spin.setObjectName("overlayUpdateIntervalSpin")
        self.update_interval_spin.setRange(
            MIN_OVERLAY_UPDATE_INTERVAL_S,
            MAX_OVERLAY_UPDATE_INTERVAL_S,
        )
        self.update_interval_spin.setSuffix(" s")
        self.update_interval_spin.setToolTip(interval_tooltip)
        settings_row.addWidget(interval_label)
        settings_row.addWidget(self.update_interval_spin)

        scale_label = QtWidgets.QLabel("Overlay scale")
        scale_tooltip = _wrapped_tooltip(
            "Manual size multiplier for the in-game overlay, applied on top of "
            "its automatic 1080p/1440p/4K sizing. 1x keeps the adaptive default; "
            "0.5x and 2x shrink or grow it. Changes update the running native "
            "overlay live."
        )
        scale_label.setToolTip(scale_tooltip)
        self.scale_combo = QtWidgets.QComboBox()
        self.scale_combo.setObjectName("overlayScaleCombo")
        for option in OVERLAY_SCALE_OPTIONS:
            self.scale_combo.addItem(_scale_option_label(option))
        self.scale_combo.setToolTip(scale_tooltip)
        settings_row.addSpacing(14)
        settings_row.addWidget(scale_label)
        settings_row.addWidget(self.scale_combo)

        settings_row.addStretch(1)
        layout.addLayout(settings_row)

        preview_group = QtWidgets.QGroupBox("Preview")
        preview_layout = QtWidgets.QVBoxLayout(preview_group)
        preview_layout.setContentsMargins(14, 24, 14, 14)
        preview_layout.setSpacing(12)
        self.preview_label = QtWidgets.QLabel("")
        self.preview_label.setObjectName("overlayPreviewLabel")
        self.preview_label.setTextInteractionFlags(self.QtCore.Qt.TextSelectableByMouse)
        self.preview_label.setMargin(8)
        self.preview_label.setMinimumHeight(36)
        preview_font = self.preview_label.font()
        preview_font.setFamily("monospace")
        preview_font.setBold(True)
        self.preview_label.setFont(preview_font)
        preview_layout.addWidget(self.preview_label)

        layout.addWidget(preview_group)

        options_widget = QtWidgets.QWidget()
        options_layout = QtWidgets.QVBoxLayout(options_widget)
        options_layout.setContentsMargins(0, 0, 0, 0)
        options_layout.setSpacing(10)
        groups_layout = QtWidgets.QHBoxLayout()
        groups_layout.setContentsMargins(0, 0, 0, 0)
        groups_layout.setSpacing(12)
        groups_layout.addWidget(
            self._build_group(
                "Basic",
                BASIC_OVERLAY_ITEM_IDS,
                object_name="overlayBasicOptionsGroup",
            ),
            1,
        )
        groups_layout.addWidget(
            self._build_group(
                "Advanced",
                ADVANCED_OVERLAY_ITEM_IDS,
                object_name="overlayAdvancedOptionsGroup",
            ),
            1,
        )
        options_layout.addLayout(groups_layout)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setObjectName("overlayConfigStatus")
        options_layout.addWidget(self.status_label)
        options_layout.addStretch(1)

        options_scroll = QtWidgets.QScrollArea()
        options_scroll.setObjectName("overlayOptionsScroll")
        options_scroll.setWidgetResizable(True)
        options_scroll.setFrameShape(self.QtWidgets.QFrame.NoFrame)
        options_scroll.setHorizontalScrollBarPolicy(self.QtCore.Qt.ScrollBarAlwaysOff)
        options_scroll.setWidget(options_widget)
        layout.addWidget(options_scroll, 1)

        self.update_interval_spin.valueChanged.connect(self._set_update_interval)
        self.scale_combo.currentIndexChanged.connect(self._set_overlay_scale)

        self.timer = self.QtCore.QTimer(self.widget)
        self.timer.timeout.connect(self.refresh_preview)
        self.timer.start(1000)
        self._sync_widgets()
        self.refresh_preview()

    def _build_group(
        self,
        title: str,
        item_ids: tuple[str, ...],
        *,
        object_name: str = "",
    ):
        group = self.QtWidgets.QGroupBox(title)
        if object_name:
            group.setObjectName(object_name)
        layout = self.QtWidgets.QGridLayout(group)
        layout.setContentsMargins(10, 18, 10, 10)
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(8)
        for row, item_id in enumerate(item_ids):
            checkbox = self.QtWidgets.QCheckBox(ITEM_LABELS[item_id])
            checkbox.setObjectName(f"overlayItemCheckbox_{item_id}")
            checkbox.setToolTip(_wrapped_tooltip(ITEM_TOOLTIPS[item_id]))
            sample = self.QtWidgets.QLabel(_sample_text(item_id, {}))
            sample.setObjectName(f"overlayItemValue_{item_id}")
            sample.setFixedWidth(_item_value_width(sample))
            sample.setToolTip(_wrapped_tooltip(ITEM_TOOLTIPS[item_id]))
            info_button = self._info_button(ITEM_TOOLTIPS[item_id])
            checkbox.toggled.connect(
                lambda checked, item=item_id: self._set_item_enabled(item, checked)
            )
            self.item_checkboxes[item_id] = checkbox
            self.item_value_labels[item_id] = sample
            layout.addWidget(checkbox, row, 0)
            layout.addWidget(sample, row, 1)
            layout.addWidget(info_button, row, 2)
        layout.setColumnStretch(3, 1)
        return group

    def _info_button(self, tooltip: str):
        button = self.QtWidgets.QToolButton()
        button.setObjectName("infoButton")
        button.setText("i")
        button.setToolTip(_wrapped_tooltip(tooltip))
        button.setToolTipDuration(20000)
        button.setCursor(self.QtCore.Qt.WhatsThisCursor)
        button.setFocusPolicy(self.QtCore.Qt.NoFocus)
        button.setFixedSize(18, 18)

        def show_tooltip(_checked=False, *, info=button):
            position = info.mapToGlobal(info.rect().bottomLeft())
            self.QtWidgets.QToolTip.showText(position, info.toolTip(), info)

        button.clicked.connect(show_tooltip)
        return button

    def _set_item_enabled(self, item_id: str, enabled: bool) -> None:
        if self._syncing:
            return
        before = self.config
        after = set_overlay_item_enabled(before, item_id, bool(enabled))
        if after.enabled_item_ids == before.enabled_item_ids and not enabled:
            self.status_label.setText("Keep at least one overlay item enabled.")
            self._sync_widgets()
            return
        self.config = after
        self._save("Saved. Running overlay updates live.")

    def _set_update_interval(self, value: int) -> None:
        if self._syncing:
            return
        self.config = set_overlay_update_interval_s(self.config, value)
        self._save("Saved. Running runtime loop updates live.")

    def _set_overlay_scale(self, index: int) -> None:
        if self._syncing:
            return
        if not 0 <= index < len(OVERLAY_SCALE_OPTIONS):
            return
        self.config = set_overlay_scale(self.config, OVERLAY_SCALE_OPTIONS[index])
        self._save("Saved. Running overlay updates live.")

    def _save(self, message: str) -> None:
        try:
            save_overlay_config(self.config, self.config_path)
        except OSError as exc:
            self.status_label.setText(f"Overlay config save failed: {exc}")
            self._sync_widgets()
            return
        self.status_label.setText(message)
        self._sync_widgets()
        self.refresh_preview()

    def _sync_widgets(self) -> None:
        self._syncing = True
        try:
            self.update_interval_spin.setValue(int(self.config.update_interval_s))
            self.scale_combo.setCurrentIndex(
                OVERLAY_SCALE_OPTIONS.index(snap_overlay_scale(self.config.scale))
            )
            enabled_items = set(self.config.enabled_item_ids)
            for item_id, checkbox in self.item_checkboxes.items():
                checkbox.setChecked(item_id in enabled_items)
        finally:
            self._syncing = False

    def refresh_preview(self) -> None:
        values = read_overlay_state()
        self.preview_label.setText(
            preview_overlay_text(values, config=self.config)
        )
        self.preview_label.setEnabled(bool(self.config.enabled))
        for item_id, label in self.item_value_labels.items():
            label.setText(_sample_text(item_id, values))

def _sample_text(item_id: str, telemetry: dict[str, str]) -> str:
    # Real in-game numbers lead whenever a wrapped game is publishing them;
    # the canned demo values (60 FPS / 120 FG / 40 ms) are only the
    # no-game-running fallback.
    demo = SAMPLE_OVERLAY_VALUES
    if item_id == "base_fps":
        live = _live_value(telemetry, "present_fps")
        return f"{live or demo['present_fps']} FPS"
    if item_id == "fg_fps":
        live = _live_value(telemetry, "framegen_fps")
        return f"{live or demo['framegen_fps']} FG"
    if item_id == "latency_ms":
        live = _live_value(telemetry, "latency_ms")
        return f"{live or demo['latency_ms']} ms"
    telemetry_key = ITEM_TELEMETRY_KEYS.get(item_id)
    live_value = _live_value(telemetry, telemetry_key or "")
    if not telemetry_key or not live_value:
        return "-"
    return format_overlay_item(item_id, telemetry) or "-"


def _live_value(telemetry: dict[str, str], key: str) -> str:
    value = str(telemetry.get(key) or "").strip()
    return "" if value.lower() == "n/a" else value


def _scale_option_label(option: float) -> str:
    return f"{float(option):g}x"


def _item_value_width(label) -> int:
    metrics = label.fontMetrics()
    return max(metrics.horizontalAdvance(text) for text in ITEM_VALUE_WIDTH_EXAMPLES) + 6


def _wrapped_tooltip(text: str) -> str:
    normalized = " ".join(str(text).split())
    escaped = html.escape(normalized)
    return f"<qt><table width='620'><tr><td>{escaped}</td></tr></table></qt>"
