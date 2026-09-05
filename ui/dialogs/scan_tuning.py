from __future__ import annotations

from auto_uv.scan_mode.auto_uv_mode import adaptive_tier_option_key
from auto_uv.scan_mode.uv_limits import uv_limit_clock_target_range_for_gpu
from drivers.nvidia.daemon_gpu import DaemonGpuClient
from ..assets import asset_image_path
from ui.features.tuning.gpu_selection import gpu_choices_with_fallback
from ui.features.tuning.tuning import AUTO_UV_PRESET_ADAPTIVE
from ui.features.tuning.tuning import AUTO_UV_PRESET_BALANCED
from ui.features.tuning.tuning import AUTO_UV_PRESET_EFFICIENCY
from ui.features.tuning.tuning import AUTO_UV_PRESET_PERFORMANCE
from ui.features.tuning.tuning import GPU_UNDERVOLTING_PURPOSE_TEXT
from ui.features.tuning.tuning import auto_uv_voltage_floor_range_mv
from ui.features.tuning.tuning import auto_uv_nvml_info_text
from ui.features.tuning.tuning import auto_uv_performance_preset_label
from ui.features.tuning.tuning import auto_uv_performance_preset_tooltip
from ui.features.tuning.tuning import auto_uv_target_default
from ui.features.tuning.tuning import auto_uv_power_limit_default
from ui.features.tuning.tuning import auto_uv_preset
from ui.features.tuning.tuning import auto_uv_scan_estimate_minutes
from ui.features.tuning.tuning import auto_uv_scan_estimate_text
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
    gpu_combo.setEnabled(len(gpu_choices) > 1)
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
            "Deepest undervolt: selects the highest measured FPS per watt "
            "within the automatic GPU/tier clock-loss allowance, with "
            "2 rising tail bins."
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

    # Every profile owns the same Advanced controls. The pages stay editable
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

        for field, label, unit, step, tooltip in (
            (
                "voltage",
                "Voltage target",
                "mV",
                5,
                "Voltage target for this tier after the normal voltage sweep. The rising "
                "tail can operate above this anchor; it is not a strict voltage limit.",
            ),
            (
                "clock",
                "Core clock target",
                "MHz",
                15,
                "Clock target for this tier. A lower custom target is tested after the "
                "voltage sweep; the two-bin tail adds nominal boost headroom. "
                "The scan may choose a lower tested clock if the target is unsafe.",
            ),
        ):
            spin = QtWidgets.QSpinBox()
            spin.setObjectName(f"{preset_id}{field.title()}Spin")
            spin.setSuffix(f" {unit}")
            spin.setSingleStep(step)
            spin.setFixedWidth(136)
            controls[f"target_{field}"] = spin
            _latched(spin, controls, f"target_{field}")
            add_form_row(
                QtCore=QtCore,
                QtWidgets=QtWidgets,
                form_layout=form,
                text=label,
                widget=spin,
                tooltip=tooltip,
            )

        target_caution = QtWidgets.QLabel(
            "Default targets are optimized for most GPUs. Change them only "
            "if you understand GPU voltage/frequency tuning and the risks "
            "of instability or crashes."
        )
        target_caution.setObjectName("autoUvTargetCaution")
        target_caution.setWordWrap(True)
        target_caution.setMaximumWidth(680)
        form.addRow(target_caution)

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
            text="Memory offset",
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
        if not gpu_choices:
            gpu_nvml_info.setText(
                "No NVIDIA GPU detected. Close this dialog, check the GPU driver "
                "and PenguinBurner hardware service, then open Setup Auto Undervolt again."
            )
            return
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
        voltage_bounds = auto_uv_voltage_floor_range_mv(
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
                target = auto_uv_target_default(gpu_name=gpu_name, profile_id=preset_id)
                clock_bounds = uv_limit_clock_target_range_for_gpu(gpu_name, preset_id)
                supported_clocks = tuple(
                    getattr(info, "supported_graphics_clock_steps_mhz", ()) or ()
                )
                if clock_bounds is None and supported_clocks:
                    clock_bounds = (min(supported_clocks), max(supported_clocks))
                for key, default, bounds, step in (
                    ("target_voltage", target.voltage_mv, voltage_bounds, 5),
                    ("target_clock", target.clock_mhz, clock_bounds, 15),
                ):
                    spin = controls[key]
                    touched = bool(controls[f"{key}_touched"]["value"])
                    previous = (
                        _configured_target_value(controls, key) if touched else None
                    )
                    if bounds is None:
                        controls[f"{key}_auto_value"] = 0
                        spin.setRange(0, 0)
                        spin.setSpecialValueText("Auto")
                        spin.setEnabled(False)
                        continue
                    low, high = bounds
                    auto_value = max(0, int(low) - step) if default is None else None
                    controls[f"{key}_auto_value"] = auto_value
                    spin.setEnabled(True)
                    spin.setSpecialValueText("Auto" if auto_value is not None else "")
                    spin.setRange(
                        auto_value if auto_value is not None else int(low), int(high)
                    )
                    value = previous if touched and previous is not None else default
                    spin.setValue(
                        max(int(low), min(int(high), int(value)))
                        if value is not None
                        else auto_value
                        if auto_value is not None
                        else int(low)
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
    # power limits stay per-tier. A Balanced descent may be
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
    start_button.setEnabled(bool(gpu_choices))
    for group in (scope_group, preset_group, advanced_group):
        group.setEnabled(bool(gpu_choices))
    commit_spinboxes = []
    for preset_id in _PRESET_ORDER:
        controls = tier_controls[preset_id]
        commit_spinboxes.extend(
            controls[key]
            for key in ("target_voltage", "target_clock", "memory", "power_spin")
        )
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
    if dialog.exec() != QtWidgets.QDialog.Accepted or not gpu_choices:
        return None

    preset = auto_uv_preset(scan_preset_id())
    options: dict = {
        "gpu_index": _selected_gpu_index(gpu_combo, selected_gpu_index),
        "auto_uv_mode": preset.auto_uv_mode,
    }
    if preset.preset_id == AUTO_UV_PRESET_ADAPTIVE:
        for preset_id in _PRESET_ORDER:
            controls = tier_controls[preset_id]
            _add_target_options(options, controls, preset_id)
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
    # The box is in memory-clock MHz; the applied NVML offset is transfer
    # rate (MT/s), which is twice the clock delta.
    options["auto_uv_memory_offset_mhz"] = int(controls["memory"].value()) * 2
    power_spin = controls["power_spin"]
    if power_spin.isEnabled() and int(power_spin.value()) > 0:
        options["auto_uv_power_limit_w"] = int(power_spin.value())
    if int(preset.tail_rise_bins) > 0:
        options["auto_uv_tail_rise_bins"] = int(preset.tail_rise_bins)
    _add_target_options(options, controls, preset.preset_id)
    return options


def _configured_target_value(controls: dict, key: str) -> int | None:
    value = int(controls[key].value())
    if value == controls.get(f"{key}_auto_value") or not controls[key].isEnabled():
        return None
    return value


def _add_target_options(options: dict, controls: dict, tier: str) -> None:
    for key, field in (("target_voltage", "voltage_mv"), ("target_clock", "clock_mhz")):
        value = _configured_target_value(controls, key)
        if value is not None:
            options[adaptive_tier_option_key(tier, f"target_{field}")] = value


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
