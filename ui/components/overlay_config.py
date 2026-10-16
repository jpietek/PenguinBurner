from __future__ import annotations

import html
import importlib
from pathlib import Path

from overlay.config import (
    MAX_OVERLAY_OPACITY,
    MAX_OVERLAY_UPDATE_INTERVAL_S,
    MIN_OVERLAY_OPACITY,
    MIN_OVERLAY_UPDATE_INTERVAL_S,
    OVERLAY_DETAIL_IDS,
    OVERLAY_FONTS,
    OVERLAY_ITEM_SECTIONS,
    OVERLAY_LAYOUTS,
    OVERLAY_POSITION_IDS,
    OVERLAY_RENDERERS,
    OVERLAY_SCALE_OPTIONS,
    default_overlay_config_path,
    load_overlay_config,
    normalize_overlay_detail,
    normalize_overlay_font,
    normalize_overlay_layout,
    normalize_overlay_position,
    normalize_overlay_renderer,
    save_overlay_config,
    set_overlay_detail,
    set_overlay_enabled,
    set_overlay_font,
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
from overlay.state import read_overlay_state

from .overlay_preview import MAX_PREVIEW_H, overlay_preview_class
from .toggle_switch import make_toggle_switch, toggle_state_text

DETAIL_LABELS = {
    "minimal": "Minimal",
    "standard": "Standard",
    "detailed": "Detailed",
    "full": "Everything",
    "custom": "Custom",
}

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

RENDERER_LABELS = {
    "blended": "Blended",
    "clear_rect": "Solid (compatibility)",
}

POSITION_LABELS = {
    "top_left": "Top left",
    "top_center": "Top center",
    "top_right": "Top right",
    "bottom_left": "Bottom left",
    "bottom_right": "Bottom right",
}

ITEM_LABELS = {
    "base_fps": "Base FPS",
    "fg_fps": "FG FPS",
    "clock_mhz": "Clock MHz",
    "voltage_mv": "Voltage mV",
    "power_w": "Power W",
    "profile": "Profile",
    "fps_1pct_low": "1% low FPS",
    "frametime_p97": "Frametime p97",
    "fps_session": "Session min/avg/max",
    "frametime_graph": "Frametime graph",
    "gpu_name": "GPU name",
    "driver_version": "Driver version",
    "resolution": "Resolution",
    "present_mode": "Present mode",
    "gpu_util_pct": "GPU %",
    "cpu_util_pct": "CPU %",
    "cpu_peak_thread_pct": "CPU-T %",
    "fan_pct": "Fan %",
    "temperature_c": "Temp C",
    "latency_ms": "Latency ms",
    "uv_offset_mv": "UV offset mV",
    "mem_clock_mhz": "Memory MHz",
    "vram": "VRAM used/total",
    "ram": "System RAM",
    "cpu_temp_c": "CPU temp C",
    "cpu_power_w": "CPU power W",
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
    "mem_clock_mhz": "mem_clock_mhz",
    "cpu_temp_c": "cpu_temp_c",
    "cpu_power_w": "cpu_power_w",
}

ITEM_VALUE_WIDTH_EXAMPLES = (
    "9999 FPS",
    "min/avg/max 999/999/999",
    "9999 MHz",
    "9999 mV",
    "CPU-T 9999%",
    "T 9999 C",
    "UV -9999 mV",
    "99.9 / 99 GiB",
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
        "The game's own render resolution, taken from the swapchain it created "
        "-- not the desktop resolution, so upscaling and windowed modes show "
        "their real size."
    ),
    "present_mode": (
        "How the game hands finished frames to the compositor: fifo is vsync, "
        "immediate is uncapped and can tear, mailbox is triple buffering. "
        "Worth checking when a frame rate cap appears out of nowhere."
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
        "System memory in use and installed. \"Used\" is total minus available, "
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


_REFLOW_FILTER_CACHE: dict[int, type] = {}


def _section_reflow_filter_class(QtCore) -> type:
    """A QObject that reports the field area's width back to the panel.

    Qt has no wrapping layout, so the sections are re-laid by hand whenever the
    area they sit in changes width. Built once per Qt module set, the way the
    other widget factories in this package are.
    """
    cached = _REFLOW_FILTER_CACHE.get(id(QtCore))
    if cached is not None:
        return cached

    class SectionReflowFilter(QtCore.QObject):
        def __init__(self, panel, parent=None):
            super().__init__(parent)
            self._panel = panel

        def eventFilter(self, watched, event):
            if event.type() == QtCore.QEvent.Type.Resize:
                self._panel._fit_sections_to_width(watched.width())
            return False

    _REFLOW_FILTER_CACHE[id(QtCore)] = SectionReflowFilter
    return SectionReflowFilter


def _qt_gui_module(QtWidgets):
    """QtGui from the same binding the panel was handed.

    The panel is given QtCore and QtWidgets rather than importing them, so the
    painter module has to come from the same package instead of a hard-coded
    PySide6 import.
    """
    package = QtWidgets.__name__.rsplit(".", 1)[0]
    return importlib.import_module(f"{package}.QtGui")


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

        # The master switch. It lived only on the Game Library's per-game row
        # and in the CLI, so opening this tab with the overlay off showed an
        # empty preview, every control still live, and nothing saying why or
        # any way to change it.
        enable_row = QtWidgets.QHBoxLayout()
        enable_row.setSpacing(10)
        enable_tooltip = _wrapped_tooltip(
            "Draws the overlay in wrapped games. Off is the same as unticking "
            "every field: the layer still loads, but it renders nothing. The "
            "per-game switch in Game Library and the desktop shortcut toggle "
            "this same setting."
        )
        self.enabled_switch = make_toggle_switch(
            QtCore=self.QtCore,
            QtGui=_qt_gui_module(self.QtWidgets),
            QtWidgets=self.QtWidgets,
            object_name="overlayEnabledSwitch",
            tooltip=enable_tooltip,
        )
        enable_label = QtWidgets.QLabel("Enable overlay")
        enable_label.setObjectName("overlayEnabledLabel")
        enable_label.setToolTip(enable_tooltip)
        self.enabled_state_label = QtWidgets.QLabel("")
        self.enabled_state_label.setObjectName("overlayEnabledState")
        enable_row.addWidget(self.enabled_switch)
        enable_row.addWidget(enable_label)
        enable_row.addWidget(self.enabled_state_label)
        enable_row.addStretch(1)
        layout.addLayout(enable_row)

        # The controls are grouped by the question they answer rather than
        # strung along one row: a single row needed 1365px before it began
        # clipping, and put "how often does it refresh" next to "which corner".
        def _row(form, text, widget, tooltip):
            label = QtWidgets.QLabel(text)
            label.setToolTip(tooltip)
            widget.setToolTip(tooltip)
            form.addRow(label, widget)

        def _group(title, object_name):
            box = QtWidgets.QGroupBox(title)
            box.setObjectName(object_name)
            form = QtWidgets.QFormLayout(box)
            form.setContentsMargins(12, 20, 12, 12)
            form.setSpacing(8)
            form.setLabelAlignment(self.QtCore.Qt.AlignRight
                                   | self.QtCore.Qt.AlignVCenter)
            form.setFieldGrowthPolicy(QtWidgets.QFormLayout.ExpandingFieldsGrow)
            return box, form

        placement_group, placement_form = _group(
            "Placement and size", "overlayPlacementGroup"
        )
        style_group, style_form = _group("Style", "overlayStyleGroup")
        behaviour_group, behaviour_form = _group(
            "Behaviour", "overlayBehaviourGroup"
        )

        position_tooltip = _wrapped_tooltip(
            "Which corner of the game's image the overlay box sits in. The "
            "preview moves with it. Changes update the running native overlay "
            "live."
        )
        self.position_combo = QtWidgets.QComboBox()
        self.position_combo.setObjectName("overlayPositionCombo")
        for position_id in OVERLAY_POSITION_IDS:
            self.position_combo.addItem(POSITION_LABELS[position_id])
        _row(placement_form, "Position", self.position_combo, position_tooltip)

        scale_tooltip = _wrapped_tooltip(
            "Manual size multiplier for the in-game overlay, applied on top of "
            "its automatic 1080p/1440p/4K sizing. 1x keeps the adaptive default; "
            "the steps below and above shrink or grow it. The fractional steps "
            "need one of the smooth fonts -- the pixel face can only be "
            "magnified whole, so it uses the nearest whole size. The preview is "
            "drawn against a 1440p frame, so it shows how much of the screen "
            "each step actually covers. Changes update the running native "
            "overlay live."
        )
        self.scale_combo = QtWidgets.QComboBox()
        self.scale_combo.setObjectName("overlayScaleCombo")
        for option in OVERLAY_SCALE_OPTIONS:
            self.scale_combo.addItem(_scale_option_label(option))
        _row(placement_form, "Size", self.scale_combo, scale_tooltip)

        opacity_tooltip = _wrapped_tooltip(
            "How much of the game shows through the overlay's panel. Only the "
            "blended renderer can honour this: the compatibility renderer "
            "writes its colour straight into the image, so its panel is always "
            "solid. The text itself stays fully opaque either way."
        )
        self.opacity_spin = QtWidgets.QSpinBox()
        self.opacity_spin.setObjectName("overlayOpacitySpin")
        self.opacity_spin.setRange(
            int(MIN_OVERLAY_OPACITY * 100), int(MAX_OVERLAY_OPACITY * 100)
        )
        self.opacity_spin.setSingleStep(5)
        self.opacity_spin.setSuffix(" %")
        _row(placement_form, "Opacity", self.opacity_spin, opacity_tooltip)

        layout_tooltip = _wrapped_tooltip(
            "How the readout is arranged. HUD is the designed layout: the frame "
            "rate as a headline, dim labels beside their values, rules between "
            "groups and a quieter footer. Table is the plain grid every field "
            "shares. Both renderers draw either one."
        )
        self.layout_combo = QtWidgets.QComboBox()
        self.layout_combo.setObjectName("overlayLayoutCombo")
        for layout_id in OVERLAY_LAYOUTS:
            self.layout_combo.addItem(LAYOUT_LABELS[layout_id])
        _row(style_form, "Layout", self.layout_combo, layout_tooltip)

        font_tooltip = _wrapped_tooltip(
            "Which face the overlay draws text in. Sans and Mono are real "
            "typefaces drawn from a signed-distance atlas, so they stay sharp "
            "at any overlay scale instead of turning into magnified pixels. "
            "Sans is the default and the one that holds together smallest; the "
            "fixed-pitch faces draw a slashed zero, which is the first thing "
            "to blur once the text is only a few pixels tall. Both need the "
            "blended renderer; Pixel is the original 5x8 bitmap and the only "
            "face the compatibility renderer can draw, so the overlay falls "
            "back to it on its own when there is no pipeline."
        )
        self.font_combo = QtWidgets.QComboBox()
        self.font_combo.setObjectName("overlayFontCombo")
        for font_id in OVERLAY_FONTS:
            self.font_combo.addItem(FONT_LABELS[font_id])
        _row(style_form, "Font", self.font_combo, font_tooltip)

        renderer_tooltip = _wrapped_tooltip(
            "Blended draws the overlay through a Vulkan pipeline, which is what "
            "makes translucency, rounded corners and the filled frametime trace "
            "possible. Solid forces the original renderer, which draws with "
            "cleared rectangles and can only manage a bar per sample; pick it "
            "if blending upsets a particular game. Blended falls back to Solid "
            "on its own when a driver will not give it a pipeline."
        )
        self.renderer_combo = QtWidgets.QComboBox()
        self.renderer_combo.setObjectName("overlayRendererCombo")
        for renderer_id in OVERLAY_RENDERERS:
            self.renderer_combo.addItem(RENDERER_LABELS[renderer_id])
        _row(style_form, "Renderer", self.renderer_combo, renderer_tooltip)

        detail_tooltip = _wrapped_tooltip(
            "How much the overlay shows. The named levels pick a field set for "
            "you; ticking or unticking any field below switches to Custom and "
            "keeps your own selection."
        )
        self.detail_combo = QtWidgets.QComboBox()
        self.detail_combo.setObjectName("overlayDetailCombo")
        for detail_id in OVERLAY_DETAIL_IDS:
            self.detail_combo.addItem(DETAIL_LABELS[detail_id])
        _row(behaviour_form, "Detail", self.detail_combo, detail_tooltip)

        interval_tooltip = _wrapped_tooltip(
            "Controls how often the daemon refreshes the overlay state. Range is "
            "1 to 10 seconds, default 2 seconds. Changes update the running "
            "runtime loop live."
        )
        self.update_interval_spin = QtWidgets.QSpinBox()
        self.update_interval_spin.setObjectName("overlayUpdateIntervalSpin")
        self.update_interval_spin.setRange(
            MIN_OVERLAY_UPDATE_INTERVAL_S,
            MAX_OVERLAY_UPDATE_INTERVAL_S,
        )
        self.update_interval_spin.setSuffix(" s")
        _row(behaviour_form, "Refresh", self.update_interval_spin,
             interval_tooltip)


        preview_group = QtWidgets.QGroupBox("Preview")
        preview_group.setObjectName("overlayPreviewGroup")
        preview_layout = QtWidgets.QVBoxLayout(preview_group)
        preview_layout.setContentsMargins(14, 22, 14, 14)
        preview_layout.setSpacing(8)
        # The real thing, painted from the rows the layer draws, over a
        # stand-in game frame: the settings above change nothing a block of
        # monospaced text could have shown.
        preview_class = overlay_preview_class(
            self.QtCore, _qt_gui_module(self.QtWidgets), self.QtWidgets
        )
        self.preview = preview_class()
        self.preview.setObjectName("overlayPreviewCanvas")
        # Left-aligned: the frame has a fixed aspect and a capped height, so
        # centring it in a wide group would only pad both sides.
        preview_row = QtWidgets.QHBoxLayout()
        preview_row.setContentsMargins(0, 0, 0, 0)
        preview_row.addWidget(self.preview, 1)
        preview_row.addStretch(0)
        preview_layout.addLayout(preview_row, 1)
        self.preview_hint = QtWidgets.QLabel(
            "Shown larger than life so the text is legible. The corner, the "
            "colours and the difference between the size steps are true to "
            "the game."
        )
        self.preview_hint.setObjectName("overlayPreviewHint")
        self.preview_hint.setWordWrap(True)

        # The drawing above is this panel's own reading of the layout. The
        # button opens a small Vulkan window with the real layer in it, which
        # re-reads this config four times a second -- so every change below
        # lands in that window without restarting it.
        self._live_process = None
        self.live_button = QtWidgets.QPushButton(LIVE_PREVIEW_START)
        self.live_button.setObjectName("overlayLivePreviewButton")
        self.live_button.setToolTip(
            _wrapped_tooltip(
                "Opens a spinning-cube window with the overlay drawn by the "
                "real layer: the actual typefaces, the blended panel and the "
                "frametime trace, at the size a game would get. It follows "
                "the settings live, so leave it open while you change them."
            )
        )
        self.live_button.clicked.connect(self._toggle_live_preview)
        hint_row = QtWidgets.QHBoxLayout()
        hint_row.setContentsMargins(0, 0, 0, 0)
        hint_row.setSpacing(10)
        hint_row.addWidget(self.preview_hint, 1)
        hint_row.addWidget(self.live_button, 0, self.QtCore.Qt.AlignBottom)
        preview_layout.addLayout(hint_row)

        # The preview and the controls stack in one column so the field list
        # can have the other one at full height. Side by side above it, the
        # fields were left with whatever vertical space remained -- which on
        # any ordinary window meant reaching for the edge to resize before the
        # last of twenty-six could be seen.
        left_column = QtWidgets.QVBoxLayout()
        left_column.setSpacing(10)
        left_column.addWidget(preview_group)
        left_column.addWidget(placement_group)
        left_column.addWidget(style_group)
        left_column.addWidget(behaviour_group)
        left_column.addStretch(1)
        left_holder = QtWidgets.QWidget()
        left_holder.setObjectName("overlayControlsColumn")
        left_holder.setLayout(left_column)
        left_holder.setFixedWidth(round(MAX_PREVIEW_H * 16 / 9) + 34)

        options_widget = QtWidgets.QWidget()
        options_layout = QtWidgets.QVBoxLayout(options_widget)
        options_layout.setContentsMargins(0, 0, 0, 0)
        options_layout.setSpacing(10)
        # Two rows of two sections rather than one row of long columns. With
        # twenty-six fields the old Basic/Advanced pair ran far enough down the
        # window that the tab had to be resized to reach the end of it, and the
        # split said nothing about what any field was: "Advanced" held the GPU
        # temperature next to the present mode.
        sections_grid = QtWidgets.QGridLayout()
        sections_grid.setContentsMargins(0, 0, 0, 0)
        sections_grid.setHorizontalSpacing(12)
        sections_grid.setVerticalSpacing(10)
        self._sections_grid = sections_grid
        self._section_groups = [
            self._build_group(
                title,
                item_ids,
                object_name=f"overlayItemSection_{title.lower().replace(' ', '-')}",
            )
            for title, item_ids in OVERLAY_ITEM_SECTIONS
        ]
        self._section_columns = 0
        self._reflow_sections(2)
        options_layout.addLayout(sections_grid)

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
        self._reflow_filter = _section_reflow_filter_class(self.QtCore)(
            self, self.widget
        )
        options_scroll.viewport().installEventFilter(self._reflow_filter)
        self._options_scroll = options_scroll

        main_row = QtWidgets.QHBoxLayout()
        main_row.setSpacing(12)
        main_row.addWidget(left_holder)
        main_row.addWidget(options_scroll, 1)
        layout.addLayout(main_row, 1)

        self.enabled_switch.toggled.connect(self._set_overlay_enabled)
        self.update_interval_spin.valueChanged.connect(self._set_update_interval)
        self.scale_combo.currentIndexChanged.connect(self._set_overlay_scale)
        self.position_combo.currentIndexChanged.connect(self._set_overlay_position)
        self.detail_combo.currentIndexChanged.connect(self._set_overlay_detail)
        self.opacity_spin.valueChanged.connect(self._set_overlay_opacity)
        self.renderer_combo.currentIndexChanged.connect(
            self._set_overlay_renderer
        )
        self.font_combo.currentIndexChanged.connect(self._set_overlay_font)
        self.layout_combo.currentIndexChanged.connect(self._set_overlay_layout)

        self.timer = self.QtCore.QTimer(self.widget)
        self.timer.timeout.connect(self.refresh_preview)
        self.timer.start(1000)
        self._sync_widgets()
        self.refresh_preview()

    def _reflow_sections(self, columns: int) -> None:
        """Re-lay the field sections into `columns` columns.

        Qt has no layout that wraps, so a fixed two-column grid pushed the
        second column past the right edge on a narrow window -- and with
        horizontal scrolling off, those fields could not be reached at all.
        """
        columns = max(1, int(columns))
        if columns == self._section_columns:
            return
        self._section_columns = columns
        grid = self._sections_grid
        for group in self._section_groups:
            grid.removeWidget(group)
        for index, group in enumerate(self._section_groups):
            grid.addWidget(group, index // columns, index % columns)
        for column in range(grid.columnCount()):
            grid.setColumnStretch(column, 1 if column < columns else 0)
        grid.setRowStretch(grid.rowCount(), 1)

    def _fit_sections_to_width(self, width: int) -> None:
        widest = max(
            (group.sizeHint().width() for group in self._section_groups), default=1
        )
        spacing = self._sections_grid.horizontalSpacing()
        fits = (width + spacing) // max(1, widest + spacing)
        self._reflow_sections(min(len(self._section_groups), max(1, fits)))

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
        # Soak up the spare height at the bottom. Without this the shorter of
        # the two groups spread its rows to match the taller one, so the same
        # checkboxes sat at different pitches side by side.
        layout.setRowStretch(len(item_ids), 1)
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

    def _toggle_live_preview(self) -> None:
        if self._live_process is not None:
            self._stop_live_preview()
            return
        command = live_preview_command()
        if command is None:
            self.status_label.setText(MISSING_EXECUTABLE_MESSAGE)
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
            self.status_label.setText(MISSING_EXECUTABLE_MESSAGE)
            return
        self._live_process = process
        self.live_button.setText(LIVE_PREVIEW_STOP)
        self.status_label.setText(
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
        self.status_label.setText(MISSING_EXECUTABLE_MESSAGE)

    def _set_overlay_enabled(self, checked: bool) -> None:
        if self._syncing:
            return
        self.config = set_overlay_enabled(self.config, bool(checked))
        self.enabled_state_label.setText(toggle_state_text(bool(checked)))
        self._save("Saved. The running overlay picks this up live.")
        self.refresh_preview()

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

    def _set_overlay_detail(self, index: int) -> None:
        if self._syncing:
            return
        if not 0 <= index < len(OVERLAY_DETAIL_IDS):
            return
        self.config = set_overlay_detail(self.config, OVERLAY_DETAIL_IDS[index])
        self._save("Saved. Running overlay updates live.")

    def _set_overlay_position(self, index: int) -> None:
        if self._syncing:
            return
        if not 0 <= index < len(OVERLAY_POSITION_IDS):
            return
        self.config = set_overlay_position(self.config, OVERLAY_POSITION_IDS[index])
        self._save("Saved. Running overlay updates live.")

    def _set_overlay_opacity(self, value: int) -> None:
        if self._syncing:
            return
        self.config = set_overlay_opacity(self.config, value / 100.0)
        self._save("Saved. Running overlay updates live.")

    def _set_overlay_renderer(self, index: int) -> None:
        if self._syncing:
            return
        if not 0 <= index < len(OVERLAY_RENDERERS):
            return
        self.config = set_overlay_renderer(self.config, OVERLAY_RENDERERS[index])
        self._save("Saved. Running overlay updates live.")

    def _set_overlay_layout(self, index: int) -> None:
        if self._syncing:
            return
        if not 0 <= index < len(OVERLAY_LAYOUTS):
            return
        self.config = set_overlay_layout(self.config, OVERLAY_LAYOUTS[index])
        self._save("Saved. Running overlay updates live.")

    def _set_overlay_font(self, index: int) -> None:
        if self._syncing:
            return
        if not 0 <= index < len(OVERLAY_FONTS):
            return
        self.config = set_overlay_font(self.config, OVERLAY_FONTS[index])
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
            self.enabled_switch.setChecked(bool(self.config.enabled))
            self.enabled_state_label.setText(
                toggle_state_text(bool(self.config.enabled))
            )
            self.update_interval_spin.setValue(int(self.config.update_interval_s))
            self.scale_combo.setCurrentIndex(
                OVERLAY_SCALE_OPTIONS.index(snap_overlay_scale(self.config.scale))
            )
            self.position_combo.setCurrentIndex(
                OVERLAY_POSITION_IDS.index(
                    normalize_overlay_position(self.config.position)
                )
            )
            self.detail_combo.setCurrentIndex(
                OVERLAY_DETAIL_IDS.index(normalize_overlay_detail(self.config.detail))
            )
            self.opacity_spin.setValue(round(float(self.config.opacity) * 100))
            self.renderer_combo.setCurrentIndex(
                OVERLAY_RENDERERS.index(
                    normalize_overlay_renderer(self.config.renderer)
                )
            )
            self.font_combo.setCurrentIndex(
                OVERLAY_FONTS.index(normalize_overlay_font(self.config.font))
            )
            self.layout_combo.setCurrentIndex(
                OVERLAY_LAYOUTS.index(normalize_overlay_layout(self.config.layout))
            )
            enabled_items = set(self.config.enabled_item_ids)
            for item_id, checkbox in self.item_checkboxes.items():
                checkbox.setChecked(item_id in enabled_items)
        finally:
            self._syncing = False

    def refresh_preview(self) -> None:
        values = read_overlay_state()
        # Live numbers when a wrapped game is publishing them, the canned demo
        # values otherwise, so the preview is never an empty box.
        self.preview.set_config(self.config)
        self.preview.set_values(_preview_values(values))
        for item_id, label in self.item_value_labels.items():
            label.setText(_sample_text(item_id, values))

def _preview_values(telemetry: dict[str, str]) -> dict[str, str]:
    """What the preview should read, live values winning over the demo set."""
    values = dict(SAMPLE_OVERLAY_VALUES)
    for key, value in telemetry.items():
        text = str(value).strip()
        if text:
            values[key] = text
    return values


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
    if item_id == "frametime_graph":
        return "(drawn in game)"
    if item_id in (
        "fps_1pct_low",
        "frametime_p97",
        "fps_session",
        "gpu_name",
        "driver_version",
        "resolution",
        "present_mode",
    ):
        # The layer measures these inside the game process and never publishes
        # them, so the desktop app can only ever show their shape.
        return format_overlay_item(item_id, demo) or "-"
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
