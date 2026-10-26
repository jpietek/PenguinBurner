from __future__ import annotations

import html
import importlib
from pathlib import Path

from integrations.launchers.host_process import start_on_host
from overlay.config import (
    MAX_OVERLAY_OPACITY,
    MAX_OVERLAY_UPDATE_INTERVAL_S,
    MIN_OVERLAY_OPACITY,
    MIN_OVERLAY_UPDATE_INTERVAL_S,
    OVERLAY_DETAIL_IDS,
    OVERLAY_DETAIL_LABELS,
    OVERLAY_FONTS,
    OVERLAY_GRAPH_MODE_LABELS,
    OVERLAY_GRAPH_MODES,
    OVERLAY_ITEM_SECTIONS,
    OVERLAY_LAYOUTS,
    OVERLAY_POSITION_IDS,
    OVERLAY_SCALE_OPTIONS,
    default_overlay_config_path,
    load_overlay_config,
    normalize_overlay_detail,
    normalize_overlay_font,
    normalize_overlay_layout,
    normalize_overlay_position,
    save_overlay_config,
    set_overlay_detail,
    set_overlay_enabled,
    set_overlay_font,
    set_overlay_graph_mode,
    set_overlay_item_enabled,
    set_overlay_layout,
    set_overlay_opacity,
    set_overlay_position,
    set_overlay_renderer,
    set_overlay_scale,
    set_overlay_update_interval_s,
    snap_overlay_scale,
)
from overlay.live_preview import (
    MISSING_EXECUTABLE_MESSAGE,
    live_preview_command,
    live_preview_env,
)
from overlay.overlay_text import SAMPLE_OVERLAY_VALUES, format_overlay_item
from overlay.shortcuts import shortcut_desktop, shortcut_settings_command
from overlay.state import read_overlay_state
from ui.dialogs.overlay_shortcuts import overlay_shortcuts_dialog

from .overlay_preview import overlay_preview_class
from .preference_rows import preference_group, preference_row
from .toggle_switch import make_toggle_switch, toggle_state_text

LIVE_PREVIEW_START = "Live preview"
LIVE_PREVIEW_STOP = "Stop preview"

LAYOUT_LABELS = {
    "hud": "HUD",
    "table": "Table",
}

FONT_LABELS = {
    "pixel": "Pixel (bitmap)",
    "mono": "Mono",
    "sans": "Sans",
}

POSITION_LABELS = {
    "top_left": "Top left",
    "top_center": "Top center",
    "top_right": "Top right",
    "bottom_left": "Bottom left",
    "bottom_right": "Bottom right",
}

ITEM_LABELS = {
    "base_fps": "Base frame rate",
    "fg_fps": "FPS after frame generation",
    "latency_ms": "Frame latency",
    "nr_gpu_ms": "DLSS 5 neural rendering GPU time",
    "fps_1pct_low": "1% low frame rate",
    "frametime_p97": "Frame time · 97th percentile",
    "fps_session": "Session minimum / average / maximum",
    "frametime_graph": "Frame time graph",
    "clock_mhz": "GPU clock",
    "voltage_mv": "GPU voltage",
    "mem_clock_mhz": "Memory clock",
    "power_w": "Power draw",
    "temperature_c": "GPU temperature",
    "hotspot_temperature_c": "Hotspot temperature",
    "gpu_util_pct": "GPU usage",
    "fan_pct": "Fan speed",
    "vram": "Video memory",
    "uv_offset_mv": "Undervolt offset",
    "profile": "Active profile",
    "cpu_util_pct": "CPU usage",
    "cpu_peak_thread_pct": "Busiest CPU thread",
    "cpu_temp_c": "CPU temperature",
    "cpu_power_w": "CPU power draw",
    "ram": "System memory",
    "gpu_name": "Graphics card",
    "driver_version": "Driver version",
    "resolution": "Resolution",
    "present_mode": "Presentation mode",
}

ITEM_TELEMETRY_KEYS = {
    "nr_gpu_ms": "nr_gpu_ms",
    "clock_mhz": "clock_mhz",
    "voltage_mv": "voltage_mv",
    "power_w": "power_w",
    "profile": "profile_tier",
    "gpu_util_pct": "gpu_util_pct",
    "cpu_util_pct": "cpu_util_pct",
    "cpu_peak_thread_pct": "cpu_peak_thread_pct",
    "fan_pct": "fan_pct",
    "temperature_c": "temperature_c",
    "hotspot_temperature_c": "hotspot_temperature_c",
    "uv_offset_mv": "uv_offset_mv",
    "mem_clock_mhz": "mem_clock_mhz",
    "cpu_temp_c": "cpu_temp_c",
    "cpu_power_w": "cpu_power_w",
}

ITEM_TOOLTIPS = {
    "nr_gpu_ms": (
        "Average GPU elapsed time per DLSS 5 neural-rendering model evaluation, "
        "in milliseconds. This excludes surrounding injector work and is not "
        "added input latency. Enable before launching the game; restart an "
        "already running game to start capture. Missing measurements display a dash."
    ),
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
    "profile": "Active profile tier reported by the runtime.",
    "fps_1pct_low": (
        "Frame rate of the slowest one per cent of recent frames, averaged. "
        "This is the number that moves when an undervolt starts costing you "
        "stutter: a tier that holds 1% low while dropping voltage is a good "
        "tier, and the headline FPS will not tell you that. Measured inside "
        "the game by the overlay layer over its last 120 presents, so it is "
        "blank until a wrapped game is running."
    ),
    "frametime_p97": (
        "The frametime 97% of recent frames came in under, in milliseconds. "
        "A tail measure like the 1% low, expressed as time rather than rate: "
        "useful when you care how long the worst frames took, not how many "
        "frames per second that implies. Measured in-game by the overlay "
        "layer."
    ),
    "fps_session": (
        "Slowest, average and fastest frame rate since the game started. The "
        "average is taken over the session's total frame time, so a burst of "
        "cheap frames cannot drag it upwards. The extremes are single frames "
        "and will look spiky -- one alt-tab or shader compile sets the "
        "minimum for the rest of the session."
    ),
    "frametime_graph": (
        "Draws the last frames as bars under the readout, newest on the right, "
        "with a rule across the window's median frametime. Bars that reach "
        "twice the median turn amber, so a stutter is visible without reading "
        "any number. The bars are scaled against the worse of the window's "
        "slowest frame or twice its median, which keeps a smooth run looking "
        "flat instead of amplifying noise. Drawn by the overlay layer inside "
        "the game."
    ),
    "gpu_name": (
        "The GPU the game is actually rendering on, as its driver reports it. "
        "Read inside the game by the overlay layer, so on a multi-GPU machine "
        "it shows the card the game picked rather than the one the desktop app "
        "is tuning."
    ),
    "driver_version": (
        "Graphics driver version, decoded the way the card's vendor packs it. "
        "Read inside the game by the overlay layer."
    ),
    "resolution": (
        "Size of the game's Vulkan presentation surface. Upscaling may render "
        "the scene at a different internal resolution."
    ),
    "present_mode": (
        "Presentation mode requested when the game creates its Vulkan swapchain. "
        "FIFO waits for display refresh; Mailbox keeps the newest pending frame; "
        "Immediate does not wait for refresh. This readout does not change VSync "
        "or establish the monitor's final VRR behavior. Per-present mode changes "
        "may not be reflected."
    ),
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
    "hotspot_temperature_c": (
        "Hottest GPU sensor temperature in C, when supported by the GPU and driver. "
        "Missing hotspot data is hidden."
    ),
    "latency_ms": (
        "Average frame latency over the last 3 seconds, from input/simulation "
        "or render submission to the driver-reported GPU completion. A ~ prefix "
        "marks an estimate. Without Reflex markers, the Vulkan overlay pairs "
        "the previous Present call with this frame's GPU completion, including "
        "GPU queueing. That frame-cycle proxy can miss engine/input work, "
        "especially with asynchronous rendering or frame generation. "
        "P95 is shown instead with older daemons that do not publish the average. "
        "This is not measured mouse-to-screen latency; input sampling waits, "
        "presentation and monitor response are not fully covered. Driver timing "
        "accuracy under Proton/frame generation still needs validation."
    ),
    "uv_offset_mv": (
        "Current voltage delta from the stock VF point for the nearest active "
        "operating point. Missing VF data is hidden."
    ),
    "mem_clock_mhz": (
        "Current memory clock from NVML, published by the daemon beside the "
        "core clock. Hidden when the card does not report one."
    ),
    "vram": (
        "Video memory in use and the card's total, as the driver counts it -- "
        "so it includes whatever else is on the GPU, not just this game. "
        "Published by the daemon; hidden when the card reports no memory info."
    ),
    "ram": (
        'System memory in use and installed. "Used" is total minus available, '
        "not total minus free: Linux lends nearly all idle memory to the page "
        "cache, so total-minus-free would read as a full machine at all times."
    ),
    "cpu_temp_c": (
        "CPU package temperature, read by the daemon from the kernel's hwmon "
        "sensors. Colours later than the GPU's: a CPU is expected to run "
        "hotter. Hidden on a box whose sensor driver we do not recognise -- a "
        "missing row is honest, a wrong number is not."
    ),
    "cpu_power_w": (
        "CPU package power, averaged over the interval since the last refresh "
        "from the kernel's RAPL energy counter. The first reading after a "
        "start has no interval to average over, so it is blank. Hidden "
        "entirely on a box without RAPL."
    ),
}


_WIDGET_CLASSES: dict[int, tuple[type, type]] = {}


def _panel_widget_classes(QtCore, QtWidgets):
    cached = _WIDGET_CLASSES.get(id(QtWidgets))
    if cached is not None:
        return cached

    class ReflowFilter(QtCore.QObject):
        def __init__(self, panel):
            super().__init__(panel.widget)
            self.panel = panel

        def eventFilter(self, watched, event):
            if event.type() == QtCore.QEvent.Type.Resize:
                self.panel._reflow_columns(watched.width())
            return False

    class MetricLabel(QtWidgets.QLabel):
        def __init__(self, text, checkbox):
            super().__init__(text)
            self.checkbox = checkbox
            self.setWordWrap(True)
            self.setObjectName("prefRowTitle")
            self.setToolTip(checkbox.toolTip())
            self.setBuddy(checkbox)

        def mouseReleaseEvent(self, event):
            if event.button() == QtCore.Qt.LeftButton and self.property("editing"):
                self.checkbox.click()
                event.accept()
            else:
                super().mouseReleaseEvent(event)

    _WIDGET_CLASSES[id(QtWidgets)] = ReflowFilter, MetricLabel
    return ReflowFilter, MetricLabel


def _qt_gui_module(QtWidgets):
    package = QtWidgets.__name__.rsplit(".", 1)[0]
    return importlib.import_module(f"{package}.QtGui")


class OverlayConfigPanel:
    def __init__(self, *, QtCore, QtWidgets, config_path: str | Path | None = None):
        self.QtCore = QtCore
        self.QtWidgets = QtWidgets
        self.config_path = Path(config_path).expanduser() if config_path else None
        self.config = load_overlay_config(self.config_path)
        self._syncing = False
        self._live_process = None
        self.item_checkboxes = {}
        self.item_value_labels = {}
        self.item_rows = {}
        self.section_count_labels = {}
        self.section_empty_labels = {}
        self.widget = QtWidgets.QWidget()
        self.widget.setObjectName("overlayConfigPanel")
        self._create_controls()
        self._build_ui()
        self._sync_widgets()
        self._connect_controls()
        self.refresh_preview()
        self.timer = QtCore.QTimer(self.widget)
        self.timer.timeout.connect(self.refresh_preview)
        self.timer.start(1000)
        reflow_filter, _ = _panel_widget_classes(QtCore, QtWidgets)
        self._reflow_filter = reflow_filter(self)
        self.widget.installEventFilter(self._reflow_filter)

    def _create_controls(self):
        QtCore, QtWidgets = self.QtCore, self.QtWidgets
        self.enabled_switch = make_toggle_switch(
            QtCore=QtCore,
            QtGui=_qt_gui_module(QtWidgets),
            QtWidgets=QtWidgets,
            object_name="overlayEnabledSwitch",
            tooltip="Show the overlay in wrapped games.",
        )
        self.enabled_state_label = QtWidgets.QLabel()
        self.shortcuts_button = QtWidgets.QPushButton("Keyboard shortcuts…")
        self.shortcuts_button.setObjectName("overlayShortcutsButton")
        self.shortcuts_button.clicked.connect(self._show_shortcuts)

        def make_combo(name, ids, labels):
            combo = QtWidgets.QComboBox()
            combo.setObjectName(f"overlay{name.title()}Combo")
            for option in ids:
                combo.addItem(labels[option])
            return combo

        self.position_combo = make_combo(
            "position", OVERLAY_POSITION_IDS, POSITION_LABELS
        )
        self.layout_combo = make_combo("layout", OVERLAY_LAYOUTS, LAYOUT_LABELS)
        self.graph_mode_combo = make_combo("graphmode", OVERLAY_GRAPH_MODES, OVERLAY_GRAPH_MODE_LABELS)
        self.graph_mode_combo.setToolTip(
            "Present calls measures Vulkan API intervals. Displayed frames uses driver presentation timestamps, "
            "including generated frames when exposed by the driver. Both shows two unsmoothed plots. "
            "Unavailable feedback is labeled, never replaced by Present-call timing. Restart an existing game after updating the layer."
        )
        self.font_combo = make_combo("font", OVERLAY_FONTS, FONT_LABELS)
        self.detail_combo = make_combo("detail", OVERLAY_DETAIL_IDS, OVERLAY_DETAIL_LABELS)
        self.position_combo.setToolTip(
            "Position of the overlay within the game's image."
        )
        self.layout_combo.setToolTip(
            "HUD groups the readout around the frame rate. Table uses a plain grid."
        )
        self.font_combo.setToolTip("Typeface used by the in-game overlay.")
        self.detail_combo.setToolTip(
            "Small, Medium, Large or Biggest. The preset shortcut cycles these four. Scale is independent. Custom restores your saved field selection; editing a metric selection updates it."
        )
        self.opacity_control = QtWidgets.QWidget()
        opacity_layout = QtWidgets.QHBoxLayout(self.opacity_control)
        opacity_layout.setContentsMargins(0, 0, 0, 0)
        opacity_layout.setSpacing(12)
        self.opacity_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.opacity_slider.setObjectName("overlayOpacitySlider")
        self.opacity_slider.setAccessibleName("Overlay background opacity")
        self.opacity_slider.setRange(
            round(MIN_OVERLAY_OPACITY * 100), round(MAX_OVERLAY_OPACITY * 100)
        )
        self.opacity_slider.setSingleStep(5)
        self.opacity_slider.setPageStep(10)
        self.opacity_value_label = self._label("", "overlayMuted")
        self.opacity_value_label.setFixedWidth(42)
        self.opacity_value_label.setAlignment(
            QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
        )
        opacity_layout.addWidget(self.opacity_slider, 1)
        opacity_layout.addWidget(self.opacity_value_label)
        self.opacity_control.setToolTip(
            "Opacity of the background panel; text stays fully opaque."
        )
        self.update_interval_spin = QtWidgets.QSpinBox()
        self.update_interval_spin.setObjectName("overlayUpdateIntervalSpin")
        self.update_interval_spin.setRange(
            MIN_OVERLAY_UPDATE_INTERVAL_S, MAX_OVERLAY_UPDATE_INTERVAL_S
        )
        self.update_interval_spin.setSuffix(" s")
        self.update_interval_spin.setToolTip(
            "How often the runtime refreshes overlay telemetry."
        )
        self.scale_control = QtWidgets.QWidget()
        scale_layout = QtWidgets.QHBoxLayout(self.scale_control)
        scale_layout.setContentsMargins(0, 0, 0, 0)
        scale_layout.setSpacing(12)
        self.scale_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.scale_slider.setObjectName("overlayScaleSlider")
        self.scale_slider.setAccessibleName("Overlay scale")
        self.scale_slider.setRange(0, len(OVERLAY_SCALE_OPTIONS) - 1)
        self.scale_slider.setSingleStep(1)
        self.scale_slider.setPageStep(1)
        self.scale_value_label = self._label("", "overlayMuted")
        self.scale_value_label.setFixedWidth(42)
        self.scale_value_label.setAlignment(
            QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
        )
        scale_layout.addWidget(self.scale_slider, 1)
        scale_layout.addWidget(self.scale_value_label)
        self.scale_control.setToolTip(
            "Overlay scale, on top of automatic sizing for the game's resolution."
        )
        preview_class = overlay_preview_class(
            QtCore, _qt_gui_module(QtWidgets), QtWidgets
        )
        self.preview = preview_class()
        self.preview.setObjectName("overlayPreviewCanvas")
        self.live_button = QtWidgets.QPushButton(LIVE_PREVIEW_START)
        self.live_button.setObjectName("overlayLivePreviewButton")
        self.live_button.setToolTip(
            "Open a Vulkan window with the real overlay, following these settings live."
        )
        self.live_button.clicked.connect(self._toggle_live_preview)
        self.status_label = QtWidgets.QLabel()
        self.status_label.setObjectName("overlayConfigStatus")
        self.status_label.setWordWrap(True)
        self.status_label.hide()

    def _label(self, text, name="overlayMuted"):
        widget = self.QtWidgets.QLabel(text)
        widget.setObjectName(name)
        widget.setWordWrap(True)
        return widget

    def _column(self):
        widget = self.QtWidgets.QWidget()
        layout = self.QtWidgets.QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        return widget, layout

    def _build_ui(self):
        QtCore, QtWidgets = self.QtCore, self.QtWidgets
        root = self.widget
        _, MetricLabel = _panel_widget_classes(QtCore, QtWidgets)
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(24, 20, 24, 16)
        outer.setSpacing(20)
        header = QtWidgets.QHBoxLayout()
        titles = QtWidgets.QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(self._label("Overlay", "overlayPageTitle"))
        titles.addWidget(self._label("Choose how your performance overlay looks."))
        header.addLayout(titles, 1)
        header.addWidget(self.shortcuts_button)
        header.addSpacing(12)
        self.enabled_state_label.setObjectName("overlayMuted")
        header.addWidget(self.enabled_state_label)
        header.addWidget(self.enabled_switch)
        outer.addLayout(header)

        self._options_scroll = QtWidgets.QScrollArea()
        self._options_scroll.setObjectName("overlayOptionsScroll")
        self._options_scroll.setWidgetResizable(True)
        self._options_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._options_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.page = QtWidgets.QWidget()
        self.page.setObjectName("overlayPage")
        self.grid = QtWidgets.QGridLayout(self.page)
        self.grid.setContentsMargins(0, 0, 8, 0)
        self.grid.setHorizontalSpacing(20)
        self.grid.setVerticalSpacing(20)
        self._options_scroll.setWidget(self.page)
        outer.addWidget(self._options_scroll, 1)

        self.appearance, appearance = self._column()
        preview_frame = QtWidgets.QFrame()
        preview_frame.setObjectName("overlayPreviewCard")
        preview_layout = QtWidgets.QVBoxLayout(preview_frame)
        preview_layout.setContentsMargins(12, 10, 12, 12)
        preview_layout.setSpacing(8)
        preview_layout.addWidget(self._label("Preview", "prefRowTitle"))
        self.preview.setMinimumSize(0, 190)
        self.preview.setMaximumSize(16777215, 205)
        preview_layout.addWidget(self.preview)
        preview_footer = QtWidgets.QHBoxLayout()
        preview_footer.addWidget(
            self._label(
                "Illustrative preview · sample readings", "overlayPreviewCaption"
            ),
            1,
        )
        preview_footer.addWidget(self.live_button)
        preview_layout.addLayout(preview_footer)
        appearance.addWidget(preview_frame)

        scale_control = self.scale_control

        group, rows = preference_group(QtWidgets=QtWidgets, heading="Appearance")
        controls = [
            ("Position", self.position_combo),
            ("Scale", scale_control),
            ("Background opacity", self.opacity_control),
            ("Layout", self.layout_combo),
            ("Frame timing graphs", self.graph_mode_combo),
            ("Font", self.font_combo),
        ]
        control_width = max(
            180, *(control.sizeHint().width() for _, control in controls)
        )
        for title, control in controls:
            control.setFixedWidth(control_width)
            row = preference_row(
                QtWidgets=QtWidgets,
                QtCore=QtCore,
                rows_layout=rows,
                title=title,
                control=control,
            )
            row.layout().setContentsMargins(14, 5, 14, 5)
            row.property("titleLabel").setWordWrap(True)
            row.property("titleLabel").setToolTip(control.toolTip())
        appearance.addWidget(group)
        group, rows = preference_group(QtWidgets=QtWidgets, heading="Behaviour")
        for title, control in [
            ("Preset", self.detail_combo),
            ("Refresh interval", self.update_interval_spin),
        ]:
            control.setFixedWidth(control_width)
            row = preference_row(
                QtWidgets=QtWidgets,
                QtCore=QtCore,
                rows_layout=rows,
                title=title,
                control=control,
            )
            row.layout().setContentsMargins(14, 5, 14, 5)
            row.property("titleLabel").setWordWrap(True)
            row.property("titleLabel").setToolTip(control.toolTip())
        appearance.addWidget(group)
        appearance.addStretch(1)

        self.metrics_a, metrics_a = self._column()
        self.metrics_b, metrics_b = self._column()
        self.metric_groups = []
        self.metric_views = []
        self.editing_sections = set()
        self.section_edit_buttons = {}
        self.metric_infos = {}
        self.item_title_labels = {}
        for title, items in OVERLAY_ITEM_SECTIONS:
            group, rows = preference_group(QtWidgets=QtWidgets, heading=title)
            outer_group = group.layout()
            group_heading = outer_group.takeAt(0).widget()
            group_header = QtWidgets.QHBoxLayout()
            group_header.setContentsMargins(0, 0, 0, 0)
            group_header.setSpacing(8)
            group_header.addWidget(group_heading)
            count_label = self._label("", "overlayMetricCount")
            group_header.addWidget(count_label)
            group_header.addStretch(1)
            edit = QtWidgets.QPushButton("Edit")
            edit.setObjectName("overlayEditMetrics")
            edit.setAccessibleName(f"Edit {title.lower()} metrics")
            group_header.addWidget(edit)
            outer_group.insertLayout(0, group_header)
            empty = self._label("No metrics selected.", "overlayEmptyMetrics")
            rows.addWidget(empty)
            self.section_count_labels[title] = count_label
            self.section_empty_labels[title] = empty
            self.metric_views.append((title, items, rows, empty, count_label, {}))
            metric_rows = self.metric_views[-1][-1]
            self.section_edit_buttons[title] = edit
            edit.clicked.connect(
                lambda _checked=False, section=title: self._toggle_section_edit(section)
            )
            for key in items:
                checkbox = QtWidgets.QCheckBox()
                checkbox.setObjectName(f"overlayItemCheckbox_{key}")
                checkbox.setToolTip(_wrapped_tooltip(ITEM_TOOLTIPS[key]))
                self.item_checkboxes[key] = checkbox
                checkbox.toggled.connect(
                    lambda checked, item=key: self._set_item_enabled(item, checked)
                )
                checkbox.setText("")
                checkbox.setAccessibleName(ITEM_LABELS[key])
                checkbox.setSizePolicy(
                    QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed
                )
                sample = QtWidgets.QLabel()
                self.item_value_labels[key] = sample
                sample.setToolTip(checkbox.toolTip())
                sample.setObjectName("overlaySampleValue")
                sample.setTextFormat(QtCore.Qt.PlainText)
                sample.setMinimumWidth(0)
                sample.setMaximumWidth(16777215)
                sample.setSizePolicy(
                    QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred
                )
                sample.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                sample.setWordWrap(
                    key
                    in {
                        "gpu_name",
                        "fps_session",
                        "profile",
                        "present_mode",
                        "driver_version",
                    }
                )
                row = QtWidgets.QFrame()
                row.setObjectName("prefRow")
                layout = QtWidgets.QHBoxLayout(row)
                layout.setContentsMargins(14, 12, 14, 12)
                layout.setSpacing(14)
                layout.addWidget(checkbox)
                metric_label = MetricLabel(ITEM_LABELS[key], checkbox)
                self.item_title_labels[key] = metric_label
                layout.addWidget(metric_label, 1)
                layout.addWidget(sample, 0)
                info = self._info_button(ITEM_TOOLTIPS[key])
                info.setFixedSize(16, 16)
                self.metric_infos[key] = info
                layout.addWidget(info)
                rows.addWidget(row)
                metric_rows[key] = row
                self.item_rows[key] = row
                checkbox.toggled.connect(
                    lambda _checked=False: self._refresh_metric_sections()
                )
            self.metric_groups.append(group)
        metrics_a.addWidget(self.metric_groups[0])
        metrics_a.addWidget(self.metric_groups[2])
        metrics_a.addStretch(1)
        metrics_b.addWidget(self.metric_groups[1])
        metrics_b.addWidget(self.metric_groups[3])
        metrics_b.addStretch(1)
        self._layout_columns = 0
        self._reflow_columns(1300)
        outer.addWidget(self.status_label)

    def _toggle_section_edit(self, title):
        if title in self.editing_sections:
            self.editing_sections.remove(title)
        else:
            self.editing_sections.add(title)
        self._refresh_metric_sections()

    def _refresh_metric_sections(self):
        for title, items, _layout, empty, count_label, metric_rows in self.metric_views:
            editing = title in self.editing_sections
            selected = [key for key in items if self.item_checkboxes[key].isChecked()]
            count_label.setText(f"{len(selected)} selected")
            self.section_edit_buttons[title].setText("Done" if editing else "Edit")
            self.section_edit_buttons[title].setAccessibleName(
                f"{'Finish editing' if editing else 'Edit'} {title.lower()} metrics"
            )
            empty.setVisible(not selected and not editing)
            visible = list(items) if editing else selected
            for key, row in metric_rows.items():
                row.setVisible(key in visible)
                self.item_checkboxes[key].setVisible(editing)
                self.metric_infos[key].setVisible(editing)
                self.item_title_labels[key].setProperty("editing", editing)
                self.item_title_labels[key].setCursor(
                    self.QtCore.Qt.PointingHandCursor
                    if editing
                    else self.QtCore.Qt.ArrowCursor
                )
                row.setProperty("hasSeparator", bool(visible and key != visible[0]))
                row.style().unpolish(row)
                row.style().polish(row)

    def _reflow_columns(self, width):
        QtCore = self.QtCore
        mode = 3 if width >= 1238 else 2 if width >= 888 else 1
        if mode == self._layout_columns:
            return
        self._layout_columns = mode
        for widget in (self.appearance, self.metrics_a, self.metrics_b):
            self.grid.removeWidget(widget)
        for i in range(3):
            self.grid.setColumnStretch(i, 0)
            self.grid.setColumnMinimumWidth(i, 0)
        self.grid.addWidget(
            self.appearance, 0, 0, 1 if mode != 2 else 2, 1, QtCore.Qt.AlignTop
        )
        if mode == 3:
            self.grid.addWidget(self.metrics_a, 0, 1, QtCore.Qt.AlignTop)
            self.grid.addWidget(self.metrics_b, 0, 2, QtCore.Qt.AlignTop)
            for i, stretch in enumerate((10, 11, 11)):
                self.grid.setColumnStretch(i, stretch)
        elif mode == 2:
            self.grid.addWidget(self.metrics_a, 0, 1, QtCore.Qt.AlignTop)
            self.grid.addWidget(self.metrics_b, 1, 1, QtCore.Qt.AlignTop)
            self.grid.setColumnStretch(0, 10)
            self.grid.setColumnStretch(1, 13)
        else:
            self.grid.addWidget(self.metrics_a, 1, 0, QtCore.Qt.AlignTop)
            self.grid.addWidget(self.metrics_b, 2, 0, QtCore.Qt.AlignTop)
            self.grid.setColumnStretch(0, 1)

    def _connect_controls(self):
        self.enabled_switch.toggled.connect(self._set_overlay_enabled)
        self.update_interval_spin.valueChanged.connect(self._set_update_interval)
        self.scale_slider.valueChanged.connect(self._set_overlay_scale)
        self.position_combo.currentIndexChanged.connect(self._set_overlay_position)
        self.detail_combo.currentIndexChanged.connect(self._set_overlay_detail)
        self.opacity_slider.valueChanged.connect(self._set_overlay_opacity)
        self.graph_mode_combo.currentIndexChanged.connect(self._set_overlay_graph_mode)
        self.font_combo.currentIndexChanged.connect(self._set_overlay_font)
        self.layout_combo.currentIndexChanged.connect(self._set_overlay_layout)

    def _show_shortcuts(self):
        error = ""
        if shortcut_desktop() == "kde":
            command = shortcut_settings_command()
            if command and start_on_host(command):
                return
            error = (
                "Could not open KDE shortcut settings. Try again below, or open "
                "System Settings → Keyboard → Shortcuts and select PenguinBurner."
            )
        self._shortcuts_dialog = overlay_shortcuts_dialog(
            QtCore=self.QtCore, QtWidgets=self.QtWidgets, parent=self.widget,
            error=error,
        )
        self._shortcuts_dialog.show()

    def _show_status(self, message: str, *, error: bool = False):
        self.status_label.setText(message)
        self.status_label.setVisible(bool(message))
        self.status_label.setProperty("error", error)

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
        before = load_overlay_config(self.config_path)
        self.config = before
        after = set_overlay_item_enabled(before, item_id, bool(enabled))
        if after.enabled_item_ids == before.enabled_item_ids and not enabled:
            self._show_status("Keep at least one overlay item enabled.")
            self._sync_widgets()
            return
        self.config = after
        self._save()

    def _toggle_live_preview(self) -> None:
        if self._live_process is not None:
            self._stop_live_preview()
            return
        command = live_preview_command()
        if command is None:
            self._show_status(MISSING_EXECUTABLE_MESSAGE)
            return
        config_path = self.config_path or default_overlay_config_path()
        process = self.QtCore.QProcess(self.widget)
        environment = self.QtCore.QProcessEnvironment()
        for key, value in live_preview_env(config_path).items():
            environment.insert(key, value)
        process.setProcessEnvironment(environment)
        process.finished.connect(self._on_live_preview_finished)
        process.errorOccurred.connect(self._on_live_preview_error)
        process.start(command[0], command[1:])
        if not process.waitForStarted(4000):
            self._show_status(MISSING_EXECUTABLE_MESSAGE)
            return
        self._live_process = process
        self.live_button.setText(LIVE_PREVIEW_STOP)
        self._show_status(
            "Live preview running. It follows these settings as you change them."
        )

    def _stop_live_preview(self) -> None:
        process, self._live_process = self._live_process, None
        self.live_button.setText(LIVE_PREVIEW_START)
        if process is None:
            return
        process.terminate()
        if not process.waitForFinished(2000):
            process.kill()

    def _on_live_preview_finished(self, *_args) -> None:
        # Covers the window being closed from its own titlebar as well as our
        # own stop, so the button never claims something is running that is not.
        self._live_process = None
        self.live_button.setText(LIVE_PREVIEW_START)

    def _on_live_preview_error(self, *_args) -> None:
        self._live_process = None
        self.live_button.setText(LIVE_PREVIEW_START)
        self._show_status(MISSING_EXECUTABLE_MESSAGE)

    def _set_overlay_enabled(self, checked: bool) -> None:
        if self._syncing:
            return
        # A desktop shortcut may have changed settings since the last refresh.
        self.config = set_overlay_enabled(load_overlay_config(self.config_path), bool(checked))
        self.enabled_state_label.setText(toggle_state_text(bool(checked)))
        self._save()
        self.refresh_preview()

    def _set_update_interval(self, value: int) -> None:
        if self._syncing:
            return
        self.config = set_overlay_update_interval_s(load_overlay_config(self.config_path), value)
        self._save()

    def _set_overlay_scale(self, index: int) -> None:
        if self._syncing:
            return
        if not 0 <= index < len(OVERLAY_SCALE_OPTIONS):
            return
        self.config = set_overlay_scale(load_overlay_config(self.config_path), OVERLAY_SCALE_OPTIONS[index])
        self._save()

    def _set_overlay_detail(self, index: int) -> None:
        if self._syncing:
            return
        if not 0 <= index < len(OVERLAY_DETAIL_IDS):
            return
        self.config = set_overlay_detail(load_overlay_config(self.config_path), OVERLAY_DETAIL_IDS[index])
        self._save()

    def _set_overlay_position(self, index: int) -> None:
        if self._syncing:
            return
        if not 0 <= index < len(OVERLAY_POSITION_IDS):
            return
        self.config = set_overlay_position(load_overlay_config(self.config_path), OVERLAY_POSITION_IDS[index])
        self._save()

    def _show_opacity_value(self, percent: int) -> None:
        self.opacity_value_label.setText(f"{percent}%")
        self.opacity_slider.setToolTip(f"{percent}%")
        self.opacity_slider.setAccessibleDescription(f"{percent} percent")

    def _set_overlay_opacity(self, value: int) -> None:
        self._show_opacity_value(value)
        if self._syncing:
            return
        self.config = set_overlay_opacity(load_overlay_config(self.config_path), value / 100.0)
        self._save()

    def _set_overlay_graph_mode(self, index: int) -> None:
        if self._syncing or not 0 <= index < len(OVERLAY_GRAPH_MODES):
            return
        self.config = set_overlay_graph_mode(load_overlay_config(self.config_path), OVERLAY_GRAPH_MODES[index])
        self._save()

    def _set_overlay_font(self, index: int) -> None:
        if self._syncing:
            return
        if not 0 <= index < len(OVERLAY_FONTS):
            return
        self.config = set_overlay_font(load_overlay_config(self.config_path), OVERLAY_FONTS[index])
        self._save()

    def _set_overlay_layout(self, index: int) -> None:
        if self._syncing:
            return
        if not 0 <= index < len(OVERLAY_LAYOUTS):
            return
        self.config = set_overlay_layout(load_overlay_config(self.config_path), OVERLAY_LAYOUTS[index])
        self._save()

    def _save(self) -> None:
        # Normalize the renderer only on an explicit edit. Opening the page or
        # using Edit/Done never migrates or writes existing settings.
        candidate = set_overlay_renderer(self.config, "blended")
        try:
            save_overlay_config(candidate, self.config_path)
        except OSError as exc:
            self.config = load_overlay_config(self.config_path)
            self._sync_widgets()
            self._show_status(f"Overlay config save failed: {exc}", error=True)
            self.preview.set_config(self.config)
            return
        self.config = candidate
        self._show_status("")
        self._sync_widgets()
        self.refresh_preview()

    def _sync_widgets(self) -> None:
        self._syncing = True
        try:
            self.enabled_switch.setChecked(bool(self.config.enabled))
            self.enabled_state_label.setText(
                toggle_state_text(bool(self.config.enabled))
            )
            self.update_interval_spin.setValue(int(self.config.update_interval_s))
            index = OVERLAY_SCALE_OPTIONS.index(snap_overlay_scale(self.config.scale))
            self.scale_slider.setValue(index)
            percent = round(OVERLAY_SCALE_OPTIONS[index] * 100)
            self.scale_value_label.setText(f"{percent}%")
            self.scale_slider.setToolTip(f"{percent}%")
            self.scale_slider.setAccessibleDescription(f"{percent} percent")
            self.position_combo.setCurrentIndex(
                OVERLAY_POSITION_IDS.index(
                    normalize_overlay_position(self.config.position)
                )
            )
            self.detail_combo.setCurrentIndex(
                OVERLAY_DETAIL_IDS.index(normalize_overlay_detail(self.config.detail))
            )
            opacity_percent = round(float(self.config.opacity) * 100)
            self.opacity_slider.setValue(opacity_percent)
            self._show_opacity_value(opacity_percent)
            self.graph_mode_combo.setCurrentIndex(OVERLAY_GRAPH_MODES.index(self.config.graph_mode))
            self.font_combo.setCurrentIndex(
                OVERLAY_FONTS.index(normalize_overlay_font(self.config.font))
            )
            self.layout_combo.setCurrentIndex(
                OVERLAY_LAYOUTS.index(normalize_overlay_layout(self.config.layout))
            )
            enabled = set(self.config.enabled_item_ids)
            for key, checkbox in self.item_checkboxes.items():
                checkbox.setChecked(key in enabled)
        finally:
            self._syncing = False
        self._refresh_metric_sections()

    def refresh_preview(self) -> None:
        saved = load_overlay_config(self.config_path)
        if saved != self.config:
            self.config = saved
            self._sync_widgets()
        values = read_overlay_state()
        self.preview.set_config(self.config)
        self.preview.set_values(_preview_values(values))
        for key, value in self.item_value_labels.items():
            value.setText(_sample_text(key, values))


def _preview_values(telemetry: dict[str, str]) -> dict[str, str]:
    """What the preview should read, live values winning over the demo set."""
    values = dict(SAMPLE_OVERLAY_VALUES)
    for key, value in telemetry.items():
        text = str(value).strip()
        if text:
            values[key] = text
    if telemetry:
        # Sensor loss or an older daemon must hide hotspot, not show demo data.
        values["hotspot_temperature_c"] = str(
            telemetry.get("hotspot_temperature_c") or ""
        ).strip()
    return values


def _sample_text(item_id: str, telemetry: dict[str, str]) -> str:
    """Settings values retain the native measurements, with readable captions.

    Only values unavailable outside the game use examples. Missing live
    sensors remain unavailable, so the desktop never invents telemetry.
    """
    demo = SAMPLE_OVERLAY_VALUES
    if item_id in ("base_fps", "fg_fps"):
        key = "present_fps" if item_id == "base_fps" else "framegen_fps"
        return f"{_live_value(telemetry, key) or demo[key]} FPS"
    if item_id == "latency_ms":
        live = _live_value(telemetry, "latency_avg_ms") or _live_value(
            telemetry, "latency_ms"
        )
        return (
            format_overlay_item(item_id, telemetry if live else demo) or "-"
        ).removeprefix("LAT AVG ").removeprefix("LAT P95 ")
    if item_id == "frametime_graph":
        return "Graph"
    examples = {
        "fps_1pct_low",
        "frametime_p97",
        "fps_session",
        "gpu_name",
        "driver_version",
        "resolution",
        "present_mode",
    }
    if item_id in examples:
        values = demo
    else:
        key = ITEM_TELEMETRY_KEYS.get(item_id)
        if item_id in ("vram", "ram"):
            key = "vram_used_mb" if item_id == "vram" else "ram_used_mb"
        if not key or not _live_value(telemetry, key):
            return "-"
        values = telemetry
    text = format_overlay_item(item_id, values) or "-"
    if item_id == "profile":
        tier = str(values.get("profile_tier") or "").strip()
        return {
            "perf": "Performance",
            "performance": "Performance",
            "bal": "Balanced",
            "balanced": "Balanced",
            "eff": "Efficiency",
            "efficiency": "Efficiency",
        }.get(tier.lower(), tier) or "-"
    prefixes = {
        "mem_clock_mhz": "MEM ",
        "temperature_c": "T ",
        "hotspot_temperature_c": "HOT ",
        "gpu_util_pct": "GPU ",
        "cpu_util_pct": "CPU ",
        "cpu_peak_thread_pct": "CPU-T ",
        "fan_pct": "Fan ",
        "uv_offset_mv": "UV ",
        "nr_gpu_ms": "NR GPU ",
        "fps_1pct_low": "1% low ",
        "frametime_p97": "p97 ",
        "fps_session": "min/avg/max ",
        "ram": "RAM ",
        "vram": "VRAM ",
        "cpu_temp_c": "CPU ",
        "cpu_power_w": "CPU ",
        "driver_version": "driver ",
    }
    text = text.removeprefix(prefixes.get(item_id, ""))
    if item_id in ("temperature_c", "hotspot_temperature_c", "cpu_temp_c"):
        text = text.removesuffix(" C") + " °C" if text != "-" else text
    if item_id in (
        "clock_mhz",
        "mem_clock_mhz",
        "voltage_mv",
        "power_w",
        "cpu_power_w",
    ):
        number, space, unit = text.partition(" ")
        if number.lstrip("+-").isdigit():
            text = f"{int(number):,}{space}{unit}"
    if item_id in ("fps_1pct_low", "fps_session") and text != "-":
        text = text.replace("/", " / ") + " FPS"
    if item_id == "uv_offset_mv" and text.startswith("-") and text != "-":
        text = "−" + text[1:]
    if item_id == "gpu_name":
        text = str(values.get("gpu_name") or "-")
    if item_id == "resolution":
        text = text.replace("x", " × ")
    if item_id == "present_mode":
        text = {
            "fifo": "FIFO",
            "fifo-relaxed": "FIFO relaxed",
            "mailbox": "Mailbox",
            "immediate": "Immediate",
        }.get(text, text)
    return text


def _live_value(telemetry: dict[str, str], key: str) -> str:
    value = str(telemetry.get(key) or "").strip()
    return "" if value.lower() == "n/a" else value


def _wrapped_tooltip(text: str) -> str:
    normalized = " ".join(str(text).split())
    escaped = html.escape(normalized)
    return f"<qt><table width='620'><tr><td>{escaped}</td></tr></table></qt>"
