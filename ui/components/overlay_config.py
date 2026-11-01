from __future__ import annotations

import html
import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from integrations.launchers.host_process import start_on_host
from overlay.config import (
    MAX_OVERLAY_UPDATE_INTERVAL_S,
    MIN_OVERLAY_UPDATE_INTERVAL_S,
    OVERLAY_DETAIL_IDS,
    OVERLAY_DETAIL_LABELS,
    OVERLAY_FONTS,
    OVERLAY_GRAPH_MODE_LABELS,
    OVERLAY_GRAPH_MODES,
    OVERLAY_LAYOUTS,
    OVERLAY_POSITION_IDS,
    OverlayConfig,
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
from overlay.kde_shortcuts import repair_user_desktop_entry
from overlay.live_preview import (
    MISSING_EXECUTABLE_MESSAGE,
    live_preview_command,
    live_preview_env,
)
from overlay.metrics import (
    METRICS,
    OVERLAY_ITEM_SECTIONS,
)
from overlay.overlay_text import SAMPLE_OVERLAY_VALUES, format_settings_metric
from overlay.shortcuts import shortcut_desktop, shortcut_settings_command
from overlay.state import read_overlay_state
from overlay.style import (
    MAX_OVERLAY_OPACITY,
    MIN_OVERLAY_OPACITY,
    OVERLAY_SCALE_OPTIONS,
)
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
                checkbox.setToolTip(_wrapped_tooltip(METRICS[key].tooltip))
                self.item_checkboxes[key] = checkbox
                checkbox.toggled.connect(
                    lambda checked, item=key: self._set_item_enabled(item, checked)
                )
                checkbox.setText("")
                checkbox.setAccessibleName(METRICS[key].label)
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
                metric_label = MetricLabel(METRICS[key].label, checkbox)
                self.item_title_labels[key] = metric_label
                layout.addWidget(metric_label, 1)
                layout.addWidget(sample, 0)
                info = self._info_button(METRICS[key].tooltip)
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
            try:
                repair_user_desktop_entry()
            except OSError as exc:
                error = f"Could not update the PenguinBurner shortcut commands: {exc}"
            else:
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

    def _update_setting(self, setter: Callable[..., OverlayConfig], value: Any) -> None:
        if self._syncing:
            return
        self.config = setter(load_overlay_config(self.config_path), value)
        self._save()

    def _update_choice(self, setter, choices, index: int) -> None:
        if 0 <= index < len(choices):
            self._update_setting(setter, choices[index])

    def _set_overlay_enabled(self, checked: bool) -> None:
        self._update_setting(set_overlay_enabled, bool(checked))

    def _set_update_interval(self, value: int) -> None:
        self._update_setting(set_overlay_update_interval_s, value)

    def _set_overlay_scale(self, index: int) -> None:
        self._update_choice(set_overlay_scale, OVERLAY_SCALE_OPTIONS, index)

    def _set_overlay_detail(self, index: int) -> None:
        self._update_choice(set_overlay_detail, OVERLAY_DETAIL_IDS, index)

    def _set_overlay_position(self, index: int) -> None:
        self._update_choice(set_overlay_position, OVERLAY_POSITION_IDS, index)

    def _show_opacity_value(self, percent: int) -> None:
        self.opacity_value_label.setText(f"{percent}%")
        self.opacity_slider.setToolTip(f"{percent}%")
        self.opacity_slider.setAccessibleDescription(f"{percent} percent")

    def _set_overlay_opacity(self, value: int) -> None:
        self._show_opacity_value(value)
        self._update_setting(set_overlay_opacity, value / 100.0)

    def _set_overlay_graph_mode(self, index: int) -> None:
        self._update_choice(set_overlay_graph_mode, OVERLAY_GRAPH_MODES, index)

    def _set_overlay_font(self, index: int) -> None:
        self._update_choice(set_overlay_font, OVERLAY_FONTS, index)

    def _set_overlay_layout(self, index: int) -> None:
        self._update_choice(set_overlay_layout, OVERLAY_LAYOUTS, index)

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
    """Only game-local measurements use examples; missing live sensors stay missing."""
    if item_id == "frametime_graph":
        return "Graph"
    spec = METRICS[item_id]
    live = _live_value(telemetry, spec.telemetry_key)
    if item_id == "latency_ms":
        live = _live_value(telemetry, "latency_avg_ms") or live
    if spec.example or (not live and item_id in ("base_fps", "fg_fps", "latency_ms")):
        values = SAMPLE_OVERLAY_VALUES
    elif live:
        values = telemetry
    else:
        return "-"
    return format_settings_metric(item_id, values)


def _live_value(telemetry: dict[str, str], key: str) -> str:
    value = str(telemetry.get(key) or "").strip()
    return "" if value.lower() == "n/a" else value


def _wrapped_tooltip(text: str) -> str:
    normalized = " ".join(str(text).split())
    escaped = html.escape(normalized)
    return f"<qt><table width='620'><tr><td>{escaped}</td></tr></table></qt>"
