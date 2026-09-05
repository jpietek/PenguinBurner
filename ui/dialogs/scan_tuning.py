from __future__ import annotations

from auto_uv.scan_mode.auto_uv_mode import adaptive_tier_option_key
from drivers.nvidia.daemon_gpu import DaemonGpuClient
from ..assets import asset_image_path
from ui.features.tuning.gpu_selection import gpu_choices_with_fallback
from ui.features.tuning.tuning import AUTO_UV_PRESET_ADAPTIVE
from ui.features.tuning.tuning import AUTO_UV_PRESET_BALANCED
from ui.features.tuning.tuning import AUTO_UV_PRESET_EFFICIENCY
from ui.features.tuning.tuning import AUTO_UV_PRESET_PERFORMANCE
from ui.features.tuning.tuning import GPU_UNDERVOLTING_PURPOSE_TEXT
from ui.features.tuning.tuning import auto_uv_clock_drop_default
from ui.features.tuning.tuning import auto_uv_voltage_floor_range_mv
from ui.features.tuning.tuning import auto_uv_nvml_info_text
from ui.features.tuning.tuning import auto_uv_performance_preset_label
from ui.features.tuning.tuning import auto_uv_performance_preset_tooltip
from ui.features.tuning.tuning import auto_uv_performance_target_default
from ui.features.tuning.tuning import auto_uv_power_limit_default
from ui.features.tuning.tuning import auto_uv_preset
from ui.features.tuning.tuning import auto_uv_scan_estimate_minutes
from ui.features.tuning.tuning import auto_uv_scan_estimate_text
from ui.features.tuning.tuning import auto_uv_voltage_drop_default
from ui.features.tuning.tuning import memory_offset_mhz_range
from ui.features.tuning.tuning import read_auto_uv_nvml_info
from .error_details import qt_flags
from .form_rows import add_form_row
from .form_rows import dialog_form_layout
from .form_rows import install_spinbox_enter_commit_filter
from .form_rows import wrapped_tooltip


SCAN_SCOPE_FULL = "full"
SCAN_SCOPE_SELECTED_PROFILE = "selected-profile"

_PRESET_ORDER = (
    AUTO_UV_PRESET_EFFICIENCY,
    AUTO_UV_PRESET_BALANCED,
    AUTO_UV_PRESET_PERFORMANCE,
)


def select_scan_tuning(
    *,
    QtCore,
    QtGui,
    QtWidgets,
    parent,
    gpu_index: int | None = None,
) -> dict | None:
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Automatic undervolt behavior")
    layout = QtWidgets.QVBoxLayout(dialog)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(8)

    purpose = QtWidgets.QLabel(GPU_UNDERVOLTING_PURPOSE_TEXT)
    purpose.setObjectName("purposeText")
    purpose.setWordWrap(True)
    purpose.setContentsMargins(0, 0, 0, 0)
    purpose.setAlignment(qt_flags(QtCore.Qt, "AlignmentFlag", "AlignLeft", "AlignTop"))

    gpu_choices, selected_gpu_index = gpu_choices_with_fallback(
        selected_index=gpu_index
    )
    gpu_clients: dict[int, DaemonGpuClient] = {}

    def gpu_client_for(index: int) -> DaemonGpuClient:
        selected = int(index)
        if selected not in gpu_clients:
            gpu_clients[selected] = DaemonGpuClient(selected)
        return gpu_clients[selected]

    def gpu_name_for(index: int) -> str | None:
        try:
            name = gpu_client_for(index).capabilities().identity.name.strip()
        except Exception:
            return None
        return name or None

    gpu_combo = QtWidgets.QComboBox()
    gpu_combo.setObjectName("gpuSelector")
    gpu_combo.setMinimumWidth(360)
    size_adjust_policy = getattr(
        getattr(QtWidgets.QComboBox, "SizeAdjustPolicy", QtWidgets.QComboBox),
        "AdjustToContents",
    )
    gpu_combo.setSizeAdjustPolicy(size_adjust_policy)
    for choice in gpu_choices:
        gpu_combo.addItem(choice.label, int(choice.index))
    selected_combo_index = _gpu_combo_index(gpu_combo, selected_gpu_index)
    if selected_combo_index >= 0:
        gpu_combo.setCurrentIndex(selected_combo_index)

    gpu_group = QtWidgets.QGroupBox("GPU")
    gpu_group.setObjectName("gpuSelectionGroup")
    gpu_layout = dialog_form_layout(QtCore=QtCore, QtWidgets=QtWidgets)
    gpu_layout.setContentsMargins(14, 18, 14, 12)
    gpu_group.setLayout(gpu_layout)
    add_form_row(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        form_layout=gpu_layout,
        text="Graphics card",
        widget=gpu_combo,
        tooltip=(
            "Select the NVIDIA GPU PenguinBurner should use for the full "
            "Auto-UV scan, Q2RTX stability workload, profile verification, "
            "and runtime profile application."
        ),
    )
    gpu_nvml_info = QtWidgets.QLabel()
    gpu_nvml_info.setObjectName("gpuNvmlInfo")
    gpu_nvml_info.setWordWrap(True)
    gpu_nvml_info.setTextInteractionFlags(
        qt_flags(QtCore.Qt, "TextInteractionFlag", "TextSelectableByMouse")
    )
    gpu_nvml_info.setMinimumWidth(360)
    add_form_row(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        form_layout=gpu_layout,
        text="NVML limits",
        widget=gpu_nvml_info,
        tooltip=(
            "Read-only values detected from public NVML for the selected GPU. "
            "They are sampled when the dialog opens or the selected GPU changes."
        ),
    )

    scope_group = QtWidgets.QGroupBox("Scan scope")
    scope_group.setObjectName("autoUvScanScopeGroup")
    scope_layout = QtWidgets.QHBoxLayout(scope_group)
    scope_layout.setContentsMargins(14, 18, 14, 12)
    scope_layout.setSpacing(10)
    scope_button_group = QtWidgets.QButtonGroup(dialog)
    scope_button_group.setExclusive(True)
    full_minimum, full_maximum = auto_uv_scan_estimate_minutes(
        AUTO_UV_PRESET_ADAPTIVE
    )
    full_scan_button = QtWidgets.QPushButton(
        f"Full scan (~{full_minimum}-{full_maximum} min)"
    )
    full_scan_button.setObjectName("autoUvScopeButton")
    full_scan_button.setCheckable(True)
    full_scan_button.setProperty("scopeId", SCAN_SCOPE_FULL)
    full_scan_button.setToolTip(
        wrapped_tooltip(
            "Scan Efficiency, Balanced, and Performance in one run. The "
            f"scan-only estimate is {auto_uv_scan_estimate_text(AUTO_UV_PRESET_ADAPTIVE)}. "
            "Each profile keeps its own Advanced settings: click a profile "
            "below to review or adjust them before starting."
        )
    )
    selected_profile_button = QtWidgets.QPushButton("Selected profile")
    selected_profile_button.setObjectName("autoUvScopeButton")
    selected_profile_button.setCheckable(True)
    selected_profile_button.setProperty(
        "scopeId", SCAN_SCOPE_SELECTED_PROFILE
    )
    selected_profile_button.setToolTip(
        wrapped_tooltip(
            "Scan only the Efficiency, Balanced, or Performance preset selected below."
        )
    )
    for scope_button in (full_scan_button, selected_profile_button):
        scope_button.setAutoDefault(False)
        scope_button.setDefault(False)
        scope_button.setToolTipDuration(20000)
        scope_button_group.addButton(scope_button)
        scope_layout.addWidget(scope_button, 1)
    full_scan_button.setChecked(True)
    scan_estimate_note = QtWidgets.QLabel(
        "Scan estimates exclude final verification; you choose its duration later."
    )
    scan_estimate_note.setObjectName("autoUvScanEstimate")
    scan_estimate_note.setWordWrap(True)
    scope_layout.addWidget(scan_estimate_note, 1)

    preset_group = QtWidgets.QGroupBox("Auto-UV preset")
    preset_group.setObjectName("autoUvPresetGroup")
    preset_layout = QtWidgets.QHBoxLayout(preset_group)
    preset_layout.setContentsMargins(14, 18, 14, 12)
    preset_layout.setSpacing(12)
    preset_layout.addWidget(
        _bias_icon(
            QtCore=QtCore,
            QtGui=QtGui,
            QtWidgets=QtWidgets,
            filename="penguin-burner-green.png",
            tooltip="Efficiency",
        )
    )

    preset_buttons_layout = QtWidgets.QHBoxLayout()
    preset_buttons_layout.setContentsMargins(0, 0, 0, 0)
    preset_buttons_layout.setSpacing(0)
    preset_button_group = QtWidgets.QButtonGroup(dialog)
    preset_button_group.setExclusive(True)
    preset_buttons = {}
    preset_tooltips = {
        "efficiency": (
            "Deepest undervolt: accepts the largest loaded clock drop and "
            "prefers the best FPS per watt. The exact clock-drop allowance is "
            "editable under Advanced."
        ),
        "balanced": (
            "Try to maintain baseline clock while lowering the voltage; "
            "the tail of the curve goes 4 V/F bins up."
        ),
        "performance": auto_uv_performance_preset_tooltip(),
    }
    preset_labels = {}
    preset_estimates = {}
    for preset_id in _PRESET_ORDER:
        preset = auto_uv_preset(preset_id)
        minimum, maximum = auto_uv_scan_estimate_minutes(preset.preset_id)
        label = (
            auto_uv_performance_preset_label()
            if preset.preset_id == AUTO_UV_PRESET_PERFORMANCE
            else preset.label
        )
        preset_labels[preset.preset_id] = label
        preset_estimates[preset.preset_id] = (minimum, maximum)
        button = QtWidgets.QPushButton(f"{label}\n~{minimum}-{maximum} min scan")
        button.setObjectName("autoUvPresetButton")
        button.setCheckable(True)
        button.setAutoDefault(False)
        button.setDefault(False)
        button.setProperty("presetId", preset.preset_id)
        button.setProperty("scanIncluded", "false")
        button.setToolTip(wrapped_tooltip(preset_tooltips[preset.preset_id]))
        button.setToolTipDuration(20000)
        preset_button_group.addButton(button)
        preset_buttons[preset.preset_id] = button
        preset_buttons_layout.addWidget(button)
    _install_hover_tooltip_filter(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        parent=dialog,
        widgets=tuple(preset_buttons.values()),
    )
    preset_buttons[AUTO_UV_PRESET_BALANCED].setChecked(True)
    preset_center = QtWidgets.QWidget()
    preset_center_layout = QtWidgets.QVBoxLayout(preset_center)
    preset_center_layout.setContentsMargins(0, 0, 0, 0)
    preset_center_layout.setSpacing(5)
    preset_center_layout.addLayout(preset_buttons_layout)
    preset_sequence_note = QtWidgets.QLabel()
    preset_sequence_note.setObjectName("autoUvPresetSequence")
    preset_sequence_note.setAlignment(
        qt_flags(QtCore.Qt, "AlignmentFlag", "AlignCenter")
    )
    preset_center_layout.addWidget(preset_sequence_note)
    preset_layout.addWidget(preset_center, 1)
    preset_layout.addWidget(
        _bias_icon(
            QtCore=QtCore,
            QtGui=QtGui,
            QtWidgets=QtWidgets,
            filename="penguin-burner.png",
            tooltip="Performance",
        )
    )

    advanced_group = QtWidgets.QGroupBox("Advanced")
    advanced_group.setObjectName("advancedTuningGroup")
    advanced_layout = QtWidgets.QVBoxLayout(advanced_group)
    advanced_layout.setContentsMargins(18, 28, 18, 16)
    advanced_layout.setSpacing(10)

    # Every profile owns a full Advanced page (clock drop, memory offset,
    # power limit, plus its preset-specific fields). The pages stay editable
    # in both scopes: a full scan runs all three profiles, each with the
    # values tuned on its page.
    preset_advanced_stack = QtWidgets.QStackedWidget()
    preset_advanced_stack.setSizePolicy(
        QtWidgets.QSizePolicy.Policy.Expanding,
        QtWidgets.QSizePolicy.Policy.Fixed,
    )

    # Programmatic default syncs must not trip the per-field manual-override
    # latches; the flag covers every tier control at once.
    defaults_syncing = {"active": False}

    def _mark_touched(touched: dict) -> None:
        if not bool(defaults_syncing["active"]):
            touched["value"] = True

    def _latched(spin, controls: dict, key: str) -> None:
        """Register a control's per-profile manual-override latch."""
        controls[f"{key}_touched"] = latch = {"value": False}
        spin.valueChanged.connect(lambda _value, touched=latch: _mark_touched(touched))

    tier_controls: dict[str, dict] = {}
    for preset_id in _PRESET_ORDER:
        page = QtWidgets.QWidget()
        form = dialog_form_layout(QtCore=QtCore, QtWidgets=QtWidgets)
        page.setLayout(form)
        controls: dict = {"page": page}
        tier_controls[preset_id] = controls

        if preset_id == AUTO_UV_PRESET_EFFICIENCY:
            floor_spin = QtWidgets.QSpinBox()
            floor_spin.setObjectName("voltageFloorSpin")
            floor_spin.setSuffix(" mV")
            floor_spin.setSingleStep(5)
            floor_spin.setFixedWidth(136)
            controls["floor"] = floor_spin
            _latched(floor_spin, controls, "floor")
            add_form_row(
                QtCore=QtCore,
                QtWidgets=QtWidgets,
                form_layout=form,
                text="Min voltage",
                widget=floor_spin,
                tooltip=(
                    "Lowest V/F voltage bin Auto-UV may try in Efficiency. The "
                    "default comes from PenguinBurner's GPU table when detected; "
                    "unknown GPUs use Auto (-10%), calculated from the loaded "
                    "starting voltage measured during the baseline probe."
                ),
            )

        if preset_id == AUTO_UV_PRESET_PERFORMANCE:
            oc_voltage_spin = QtWidgets.QSpinBox()
            oc_voltage_spin.setObjectName("performanceVoltageSpin")
            oc_voltage_spin.setRange(700, 1250)
            oc_voltage_spin.setSuffix(" mV")
            oc_voltage_spin.setSingleStep(5)
            oc_voltage_spin.setFixedWidth(136)
            oc_clock_spin = QtWidgets.QSpinBox()
            oc_clock_spin.setObjectName("performanceClockSpin")
            oc_clock_spin.setRange(1000, 4000)
            oc_clock_spin.setSuffix(" MHz")
            oc_clock_spin.setSingleStep(15)
            oc_clock_spin.setFixedWidth(136)
            controls["oc_voltage"] = oc_voltage_spin
            controls["oc_clock"] = oc_clock_spin
            _latched(oc_voltage_spin, controls, "oc_voltage")
            _latched(oc_clock_spin, controls, "oc_clock")
            add_form_row(
                QtCore=QtCore,
                QtWidgets=QtWidgets,
                form_layout=form,
                text="Auto-OC voltage target",
                widget=oc_voltage_spin,
                tooltip=(
                    "Editable voltage cap for the internal Performance Auto-OC pass."
                ),
            )
            add_form_row(
                QtCore=QtCore,
                QtWidgets=QtWidgets,
                form_layout=form,
                text="Auto-OC clock target",
                widget=oc_clock_spin,
                tooltip=(
                    "Editable core clock cap for the internal Performance Auto-OC pass."
                ),
            )

        clock_drop_spin = _double_spin(QtWidgets, 1.0, 30.0, 10.0, "%")
        clock_drop_spin.setObjectName("maxClockDropSpin")
        controls["clock_drop"] = clock_drop_spin
        _latched(clock_drop_spin, controls, "clock_drop")
        add_form_row(
            QtCore=QtCore,
            QtWidgets=QtWidgets,
            form_layout=form,
            text="Max loaded clock drop",
            widget=clock_drop_spin,
            tooltip=(
                "How much loaded core-clock degradation this profile may "
                "accept. The default is preset-aware from the GPU table when "
                "detected; unknown GPUs use a generic fallback."
            ),
        )

        memory_spin = QtWidgets.QSpinBox()
        memory_spin.setObjectName("memoryOffsetSpin")
        # The driver range and the applied offset are NVML transfer-rate units
        # (MT/s); the realized memory clock moves by half. The user picks the
        # memory clock (MHz) here, so the box works in MHz (half the MT/s
        # range) and the equivalent MT/s is shown alongside. Converted back to
        # MT/s on accept.
        memory_spin.setSuffix(" MHz")
        memory_spin.setSingleStep(25)
        memory_spin.setFixedWidth(136)
        memory_clock_label = QtWidgets.QLabel()
        memory_clock_label.setObjectName("memoryOffsetClockLabel")

        def _update_memory_label(value: int, label=memory_clock_label) -> None:
            # NVML memory offsets are transfer-rate units; the realized memory
            # clock moves by half the offset (verified on Blackwell, issue
            # #20), so the MT/s offset is twice the selected clock value.
            label.setText(f"= +{int(value) * 2} MT/s transfer rate")

        memory_spin.valueChanged.connect(_update_memory_label)
        controls["memory"] = memory_spin
        _latched(memory_spin, controls, "memory")
        _update_memory_label(memory_spin.value())
        memory_widget = QtWidgets.QWidget()
        memory_layout = QtWidgets.QHBoxLayout(memory_widget)
        memory_layout.setContentsMargins(0, 0, 0, 0)
        memory_layout.setSpacing(10)
        memory_layout.addWidget(memory_spin)
        memory_layout.addWidget(memory_clock_label)
        memory_layout.addStretch(1)
        memory_tooltip = (
            "Optional memory clock V/F offset applied while this profile "
            "scans and saved with its final profile. You set the memory "
            "clock rise in MHz; the equivalent NVML transfer-rate offset "
            "(MT/s, like Afterburner/LACT), which is twice the clock, is "
            "shown alongside. The range is read from the driver for this "
            "GPU. Higher values can improve memory performance, but may "
            "introduce instability; modify with care."
        )
        if preset_id in (AUTO_UV_PRESET_BALANCED, AUTO_UV_PRESET_PERFORMANCE):
            memory_tooltip += (
                " In a full scan, Balanced and Performance share this value "
                "so Performance can reuse the Balanced downsweep instead of "
                "re-running it."
            )
        add_form_row(
            QtCore=QtCore,
            QtWidgets=QtWidgets,
            form_layout=form,
            text="Memory Offset",
            widget=memory_widget,
            tooltip=memory_tooltip,
        )

        power_slider = QtWidgets.QSlider(_horizontal_orientation(QtCore))
        power_slider.setObjectName("powerLimitSlider")
        power_slider.setMinimumWidth(220)
        power_slider.setSingleStep(1)
        power_spin = QtWidgets.QSpinBox()
        power_spin.setObjectName("powerLimitSpin")
        power_spin.setSuffix(" W")
        power_spin.setSingleStep(1)
        power_spin.setFixedWidth(116)
        power_widget = QtWidgets.QWidget()
        power_widget.setMinimumWidth(360)
        power_layout = QtWidgets.QHBoxLayout(power_widget)
        power_layout.setContentsMargins(0, 0, 0, 0)
        power_layout.setSpacing(10)
        power_layout.addWidget(power_slider, 1)
        power_layout.addWidget(power_spin)
        power_slider.valueChanged.connect(power_spin.setValue)
        power_spin.valueChanged.connect(power_slider.setValue)
        controls["power_slider"] = power_slider
        controls["power_spin"] = power_spin
        _latched(power_spin, controls, "power")
        add_form_row(
            QtCore=QtCore,
            QtWidgets=QtWidgets,
            form_layout=form,
            text="Power limit",
            widget=power_widget,
            tooltip=(
                "Power limit in watts applied to this profile's stock "
                "baseline, descent, final verification, and saved profile. "
                "The range comes from NVML for the selected GPU; the "
                "default position is preset-aware (Efficiency caps below "
                "stock, Balanced and Performance keep the stock budget)."
            ),
        )

        preset_advanced_stack.addWidget(page)

    preset_advanced_stack.setMinimumHeight(
        max(
            tier_controls[preset_id]["page"].sizeHint().height()
            for preset_id in _PRESET_ORDER
        )
    )
    advanced_layout.addWidget(preset_advanced_stack)

    def checked_profile_id() -> str:
        checked_button = preset_button_group.checkedButton()
        if checked_button is None:
            return AUTO_UV_PRESET_BALANCED
        return str(
            checked_button.property("presetId") or AUTO_UV_PRESET_BALANCED
        )

    def scan_preset_id() -> str:
        if full_scan_button.isChecked():
            return AUTO_UV_PRESET_ADAPTIVE
        return checked_profile_id()

    def sync_visible_preset_page() -> None:
        profile_id = checked_profile_id()
        preset_advanced_stack.setCurrentWidget(tier_controls[profile_id]["page"])
        advanced_group.setTitle(f"Advanced — {preset_labels[profile_id]}")

    def sync_gpu_dependent_defaults() -> None:
        """Ranges and untouched defaults for every profile page.

        Runs when the dialog opens and when the selected GPU changes. Fields
        the user already edited (per-profile latches) keep their value; only
        their ranges are refreshed (Qt clamps out-of-range values).
        """
        selected = _selected_gpu_index(gpu_combo, selected_gpu_index)
        client = gpu_client_for(selected)
        info = read_auto_uv_nvml_info(selected, gpu_client=client)
        gpu_nvml_info.setText(auto_uv_nvml_info_text(info))
        # Resolve the name once, falling back to the enumerated choice label:
        # the default helpers below would otherwise each open a fresh daemon
        # client (against the config-default GPU, not the selected one) when
        # handed no name.
        gpu_name = gpu_name_for(selected) or next(
            (
                choice.name
                for choice in gpu_choices
                if int(choice.index) == int(selected)
            ),
            None,
        )
        drop_default = auto_uv_voltage_drop_default(gpu_name=gpu_name)
        floor_default_mv = getattr(drop_default, "floor_voltage_mv", None)
        oc_target = auto_uv_performance_target_default(
            gpu_name=getattr(drop_default, "gpu_name", None) or gpu_name,
        )
        floor_lo, floor_hi = auto_uv_voltage_floor_range_mv(
            gpu_index=selected,
            gpu_client=client,
        )
        memory_min_mt_s, memory_max_mt_s = memory_offset_mhz_range(
            gpu_index=selected,
            gpu_client=client,
        )
        defaults_syncing["active"] = True
        try:
            for preset_id in _PRESET_ORDER:
                controls = tier_controls[preset_id]
                floor_spin = controls.get("floor")
                if floor_spin is not None:
                    if floor_default_mv is None:
                        auto_floor_value = int(floor_lo) - int(
                            floor_spin.singleStep()
                        )
                        controls["floor_auto_value_mv"] = auto_floor_value
                        floor_spin.setSpecialValueText(
                            f"Auto (-{float(drop_default.value_pct):g}%)"
                        )
                        floor_spin.setRange(auto_floor_value, floor_hi)
                        if not bool(controls["floor_touched"]["value"]):
                            floor_spin.setValue(auto_floor_value)
                    else:
                        controls["floor_auto_value_mv"] = None
                        floor_spin.setSpecialValueText("")
                        floor_spin.setRange(floor_lo, floor_hi)
                        if not bool(controls["floor_touched"]["value"]):
                            floor_spin.setValue(
                                max(
                                    floor_lo,
                                    min(int(floor_default_mv), floor_hi),
                                )
                            )
                oc_voltage_spin = controls.get("oc_voltage")
                if oc_voltage_spin is not None and not bool(
                    controls["oc_voltage_touched"]["value"]
                ):
                    oc_voltage_spin.setValue(
                        int(getattr(oc_target, "voltage_mv", None) or 950)
                    )
                oc_clock_spin = controls.get("oc_clock")
                if oc_clock_spin is not None and not bool(
                    controls["oc_clock_touched"]["value"]
                ):
                    oc_clock_spin.setValue(
                        int(getattr(oc_target, "clock_mhz", None) or 3000)
                    )
                if not bool(controls["clock_drop_touched"]["value"]):
                    controls["clock_drop"].setValue(
                        float(
                            auto_uv_clock_drop_default(
                                gpu_name=gpu_name,
                                preset_id=preset_id,
                            ).value_pct
                        )
                    )
                controls["memory"].setRange(
                    int(memory_min_mt_s) // 2,
                    int(memory_max_mt_s) // 2,
                )
                power_spin = controls["power_spin"]
                # _sync_power_limit_controls re-ranges AND resets the value to
                # the card's NVML default; remember a manually-set value so
                # the latch survives a GPU switch (clamped to the new range).
                touched_power_w = (
                    int(power_spin.value())
                    if bool(controls["power_touched"]["value"])
                    else None
                )
                _sync_power_limit_controls(
                    {
                        "slider": controls["power_slider"],
                        "spin": controls["power_spin"],
                    },
                    info,
                )
                if not power_spin.isEnabled():
                    continue
                if touched_power_w is not None:
                    watts = max(
                        power_spin.minimum(),
                        min(power_spin.maximum(), touched_power_w),
                    )
                else:
                    power_default = auto_uv_power_limit_default(
                        max_w=getattr(info, "power_limit_max_w", None),
                        min_w=getattr(info, "power_limit_min_w", None),
                        default_w=getattr(info, "power_limit_default_w", None),
                        gpu_name=gpu_name,
                        preset_id=preset_id,
                    )
                    if power_default.watts is None:
                        continue
                    watts = max(
                        power_spin.minimum(),
                        min(power_spin.maximum(), int(power_default.watts)),
                    )
                power_spin.setValue(watts)
                controls["power_slider"].setValue(watts)
        finally:
            defaults_syncing["active"] = False

    # A full scan reuses the Balanced downsweep for Performance only when
    # both tiers descend at the same memory clock, so in that scope the two
    # memory boxes mirror each other. Efficiency stays independent, and the
    # power limit and clock drop stay per-tier. A Balanced descent may be
    # donated only when Performance uses the same power and memory policy;
    # otherwise Performance runs its own capped baseline and descent.
    balanced_memory_spin = tier_controls[AUTO_UV_PRESET_BALANCED]["memory"]
    performance_memory_spin = tier_controls[AUTO_UV_PRESET_PERFORMANCE]["memory"]

    def _mirror_memory_offset(target_spin, value: int) -> None:
        if not full_scan_button.isChecked():
            return
        if int(target_spin.value()) != int(value):
            target_spin.setValue(int(value))

    balanced_memory_spin.valueChanged.connect(
        lambda value: _mirror_memory_offset(performance_memory_spin, value)
    )
    performance_memory_spin.valueChanged.connect(
        lambda value: _mirror_memory_offset(balanced_memory_spin, value)
    )

    def sync_preset_highlights() -> None:
        combined_scan = full_scan_button.isChecked()
        for position, preset_id in enumerate(_PRESET_ORDER, start=1):
            button = preset_buttons[preset_id]
            included = combined_scan or button.isChecked()
            button.setProperty("scanIncluded", "true" if included else "false")
            minimum, maximum = preset_estimates[preset_id]
            sequence_prefix = f"{position}. " if combined_scan else ""
            button.setText(
                f"{sequence_prefix}{preset_labels[preset_id]}\n"
                f"~{minimum}-{maximum} min scan"
            )
            button.style().unpolish(button)
            button.style().polish(button)
        preset_sequence_note.setText(
            "Full scan order: Efficiency → Balanced → Performance — "
            "click a profile to tune its settings."
            if combined_scan
            else "Choose one profile to scan."
        )

    def sync_scan_scope() -> None:
        sync_preset_highlights()
        sync_visible_preset_page()
        # Entering the full scan reconciles the mirrored memory boxes;
        # Balanced is the anchor when they disagree.
        _mirror_memory_offset(
            performance_memory_spin, int(balanced_memory_spin.value())
        )

    gpu_combo.currentIndexChanged.connect(
        lambda _index: sync_gpu_dependent_defaults()
    )
    preset_button_group.buttonClicked.connect(lambda _button: sync_scan_scope())
    scope_button_group.buttonClicked.connect(lambda _button: sync_scan_scope())
    sync_scan_scope()
    sync_gpu_dependent_defaults()

    buttons = QtWidgets.QDialogButtonBox()
    role_enum = getattr(
        QtWidgets.QDialogButtonBox, "ButtonRole", QtWidgets.QDialogButtonBox
    )
    standard_enum = getattr(
        QtWidgets.QDialogButtonBox,
        "StandardButton",
        QtWidgets.QDialogButtonBox,
    )
    start_button = buttons.addButton(
        "Start Auto Undervolt",
        getattr(role_enum, "AcceptRole"),
    )
    buttons.addButton(getattr(standard_enum, "Cancel"))
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    start_button.setDefault(True)
    commit_spinboxes = []
    for preset_id in _PRESET_ORDER:
        controls = tier_controls[preset_id]
        commit_spinboxes.extend(
            controls[key]
            for key in ("clock_drop", "memory", "power_spin")
        )
        for key in ("floor", "oc_voltage", "oc_clock"):
            if key in controls:
                commit_spinboxes.append(controls[key])
    install_spinbox_enter_commit_filter(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        parent=dialog,
        spinboxes=commit_spinboxes,
    )

    layout.addWidget(purpose)
    layout.addWidget(gpu_group)
    layout.addWidget(scope_group)
    layout.addWidget(preset_group)
    layout.addWidget(advanced_group)
    layout.addWidget(buttons)
    dialog.setMinimumWidth(860)
    dialog.resize(860, dialog.sizeHint().height())
    if dialog.exec() != QtWidgets.QDialog.Accepted:
        return None

    preset = auto_uv_preset(scan_preset_id())
    options: dict = {
        "gpu_index": _selected_gpu_index(gpu_combo, selected_gpu_index),
        "auto_uv_mode": preset.auto_uv_mode,
    }
    efficiency_controls = tier_controls[AUTO_UV_PRESET_EFFICIENCY]
    performance_controls = tier_controls[AUTO_UV_PRESET_PERFORMANCE]
    if preset.preset_id == AUTO_UV_PRESET_ADAPTIVE:
        # Full scan: every profile carries its own Advanced values, so the
        # scan-wide keys stay unset and each tier gets its own option triple.
        efficiency_floor_mv = _configured_voltage_floor_mv(efficiency_controls)
        if efficiency_floor_mv is not None:
            options["auto_uv_min_voltage_mv"] = int(efficiency_floor_mv)
        options["auto_oc_target_voltage_mv"] = int(
            performance_controls["oc_voltage"].value()
        )
        options["auto_oc_target_clock_mhz"] = int(
            performance_controls["oc_clock"].value()
        )
        for preset_id in _PRESET_ORDER:
            controls = tier_controls[preset_id]
            options[adaptive_tier_option_key(preset_id, "max_clock_drop_pct")] = (
                float(controls["clock_drop"].value())
            )
            # The box is in memory-clock MHz; the applied NVML offset is
            # transfer rate (MT/s), which is twice the clock delta.
            options[adaptive_tier_option_key(preset_id, "memory_offset_mhz")] = (
                int(controls["memory"].value()) * 2
            )
            power_spin = controls["power_spin"]
            if power_spin.isEnabled() and int(power_spin.value()) > 0:
                options[adaptive_tier_option_key(preset_id, "power_limit_w")] = (
                    int(power_spin.value())
                )
        return options

    controls = tier_controls[preset.preset_id]
    options["auto_uv_max_clock_drop_pct"] = float(controls["clock_drop"].value())
    # The box is in memory-clock MHz; the applied NVML offset is transfer
    # rate (MT/s), which is twice the clock delta.
    options["auto_uv_memory_offset_mhz"] = int(controls["memory"].value()) * 2
    power_spin = controls["power_spin"]
    if power_spin.isEnabled() and int(power_spin.value()) > 0:
        options["auto_uv_power_limit_w"] = int(power_spin.value())
    if int(preset.tail_rise_bins) > 0:
        options["auto_uv_tail_rise_bins"] = int(preset.tail_rise_bins)
    if preset.preset_id == AUTO_UV_PRESET_EFFICIENCY:
        efficiency_floor_mv = _configured_voltage_floor_mv(controls)
        if efficiency_floor_mv is not None:
            options["auto_uv_min_voltage_mv"] = int(efficiency_floor_mv)
    if preset.preset_id == AUTO_UV_PRESET_PERFORMANCE:
        options.update(
            {
                "auto_oc_target_voltage_mv": int(controls["oc_voltage"].value()),
                "auto_oc_target_clock_mhz": int(controls["oc_clock"].value()),
            }
        )
    return options


def _configured_voltage_floor_mv(controls: dict) -> int | None:
    value_mv = int(controls["floor"].value())
    auto_value_mv = controls.get("floor_auto_value_mv")
    if auto_value_mv is not None and value_mv == int(auto_value_mv):
        return None
    return value_mv


def _gpu_combo_index(gpu_combo, gpu_index: int) -> int:
    for index in range(gpu_combo.count()):
        try:
            if int(gpu_combo.itemData(index)) == int(gpu_index):
                return index
        except (TypeError, ValueError):
            continue
    return -1


def _selected_gpu_index(gpu_combo, fallback: int) -> int:
    try:
        return max(0, int(gpu_combo.currentData()))
    except (TypeError, ValueError):
        return max(0, int(fallback))


def _bias_icon(*, QtCore, QtGui, QtWidgets, filename: str, tooltip: str):
    label = QtWidgets.QLabel()
    label.setFixedSize(56, 56)
    label.setAlignment(QtCore.Qt.AlignCenter)
    label.setToolTip(tooltip)
    path = asset_image_path(filename)
    if path is not None:
        pixmap = QtGui.QPixmap(str(path))
        if not pixmap.isNull():
            label.setPixmap(
                pixmap.scaled(
                    54,
                    54,
                    _aspect_mode(QtCore),
                    _transform_mode(QtCore),
                )
            )
    return label


def _install_hover_tooltip_filter(*, QtCore, QtWidgets, parent, widgets) -> None:
    """Show important preset help on every pointer entry.

    Qt's delayed native tooltip timer can remain asleep when the pointer moves
    directly between adjacent buttons, especially under Wayland.  Showing the
    same tooltip explicitly on Enter keeps all three preset explanations
    dependable while leaving the buttons' normal hover and click events alone.
    """
    event_types = getattr(QtCore.QEvent, "Type", QtCore.QEvent)
    enter_type = getattr(event_types, "Enter")
    leave_type = getattr(event_types, "Leave")
    targets = tuple(widgets)

    class _HoverTooltipFilter(QtCore.QObject):
        def eventFilter(self, watched, event):  # noqa: N802 - Qt override name
            if watched not in targets:
                return False
            if event.type() == enter_type:
                tooltip = str(watched.toolTip()).strip()
                if tooltip:
                    position = watched.mapToGlobal(watched.rect().bottomLeft())
                    # A direct showText ignores setToolTipDuration unless the
                    # duration is forwarded explicitly.
                    QtWidgets.QToolTip.showText(
                        position,
                        tooltip,
                        watched,
                        QtCore.QRect(),
                        watched.toolTipDuration(),
                    )
            elif event.type() == leave_type:
                QtWidgets.QToolTip.hideText()
            return False

    event_filter = _HoverTooltipFilter(parent)
    for widget in targets:
        widget.installEventFilter(event_filter)
    parent._penguin_burner_hover_tooltip_filter = event_filter


def _double_spin(QtWidgets, minimum: float, maximum: float, value: float, suffix: str):
    spin = QtWidgets.QDoubleSpinBox()
    spin.setRange(float(minimum), float(maximum))
    spin.setDecimals(1)
    spin.setSuffix(str(suffix))
    spin.setFixedWidth(116)
    spin.setValue(float(value))
    return spin


def _aspect_mode(QtCore):
    return getattr(
        getattr(QtCore.Qt, "AspectRatioMode", QtCore.Qt),
        "KeepAspectRatio",
    )


def _transform_mode(QtCore):
    return getattr(
        getattr(QtCore.Qt, "TransformationMode", QtCore.Qt),
        "SmoothTransformation",
    )


def _horizontal_orientation(QtCore):
    return getattr(
        getattr(QtCore.Qt, "Orientation", QtCore.Qt),
        "Horizontal",
    )


def _sync_power_limit_controls(controls: dict, info) -> None:
    slider = controls.get("slider")
    spin = controls.get("spin")
    if slider is None or spin is None:
        return

    values = _power_limit_control_values(info)
    if values is None:
        slider.blockSignals(True)
        spin.blockSignals(True)
        slider.setRange(0, 0)
        spin.setRange(0, 0)
        slider.setValue(0)
        spin.setValue(0)
        slider.setEnabled(False)
        spin.setEnabled(False)
        spin.blockSignals(False)
        slider.blockSignals(False)
        return

    min_w, max_w, default_w = values
    page_step = max(1, int(round((max_w - min_w) / 6.0)))
    slider.blockSignals(True)
    spin.blockSignals(True)
    slider.setRange(min_w, max_w)
    spin.setRange(min_w, max_w)
    slider.setPageStep(page_step)
    spin.setSingleStep(1)
    slider.setValue(default_w)
    spin.setValue(default_w)
    slider.setEnabled(True)
    spin.setEnabled(True)
    spin.blockSignals(False)
    slider.blockSignals(False)


def _power_limit_control_values(info) -> tuple[int, int, int] | None:
    if info is None:
        return None
    if getattr(info, "power_management_enabled", None) is False:
        return None
    if getattr(info, "power_limit_set_supported", None) is not True:
        return None
    min_w = _positive_rounded_int(getattr(info, "power_limit_min_w", None))
    max_w = _positive_rounded_int(getattr(info, "power_limit_max_w", None))
    if min_w is None or max_w is None or max_w < min_w:
        return None
    default_w = _positive_rounded_int(getattr(info, "power_limit_default_w", None))
    if default_w is None:
        default_w = _positive_rounded_int(getattr(info, "power_limit_w", None))
    if default_w is None:
        default_w = int(round((min_w + max_w) / 2.0))
    default_w = max(min_w, min(max_w, default_w))
    return min_w, max_w, default_w


def _positive_rounded_int(value) -> int | None:
    if value is None:
        return None
    try:
        rounded = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return rounded if rounded > 0 else None
