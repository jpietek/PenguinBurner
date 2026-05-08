from __future__ import annotations

import html

from ..assets import asset_image_path
from ..styles import performance_bias_slider_stylesheet
from ..tuning import DEFAULT_AUTO_UV_MAX_CLOCK_DROP_PCT
from ..tuning import DEFAULT_AUTO_UV_PERFORMANCE_BIAS_PCT
from ..tuning import DEFAULT_SHORT_VERIFICATION_BASE_S
from ..tuning import GPU_UNDERVOLTING_PURPOSE_TEXT
from ..tuning import MAX_OVERCLOCK_BUDGET_PCT
from ..tuning import PERFORMANCE_BIAS_TOOLTIP_TEXT
from ..tuning import YOLO_MAX_OVERCLOCK_BUDGET_PCT
from ..tuning import auto_uv_mode_for_performance_bias
from ..tuning import auto_uv_voltage_drop_default
from ..tuning import memory_offset_mhz_range
from ..tuning import performance_bias_clock_recovery_pct
from ..tuning import performance_bias_slider_position
from ..tuning import slider_value_from_click_position
from .error_details import qt_flags


def select_scan_tuning(
    *,
    QtCore,
    QtGui,
    QtWidgets,
    parent,
    yolo: bool = False,
) -> dict | None:
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Automatic undervolt behavior")
    layout = QtWidgets.QVBoxLayout(dialog)
    layout.setSpacing(12)

    purpose = QtWidgets.QLabel(GPU_UNDERVOLTING_PURPOSE_TEXT)
    purpose.setObjectName("purposeText")
    purpose.setWordWrap(True)
    purpose.setAlignment(qt_flags(QtCore.Qt, "AlignmentFlag", "AlignLeft", "AlignVCenter"))

    max_bias_pct = YOLO_MAX_OVERCLOCK_BUDGET_PCT if yolo else MAX_OVERCLOCK_BUDGET_PCT
    bias_group = QtWidgets.QGroupBox("Performance bias")
    bias_group.setObjectName("performanceBiasGroup")
    bias_layout = QtWidgets.QVBoxLayout(bias_group)
    bias_layout.setContentsMargins(14, 24, 14, 14)
    bias_layout.setSpacing(12)

    bias_slider = _click_jump_slider_class(QtCore, QtWidgets)(QtCore.Qt.Horizontal)
    bias_slider.setObjectName("performanceBiasSlider")
    bias_slider.setRange(0, 100)
    bias_slider.setSingleStep(1)
    bias_slider.setPageStep(5)
    bias_slider.setToolTip(_wrapped_tooltip(PERFORMANCE_BIAS_TOOLTIP_TEXT))
    bias_slider.setToolTipDuration(20000)
    bias_slider.setStyleSheet(performance_bias_slider_stylesheet(max_bias_pct))

    slider_column = QtWidgets.QVBoxLayout()
    slider_column.setContentsMargins(0, 0, 0, 0)
    slider_column.setSpacing(5)
    bias_labels = QtWidgets.QHBoxLayout()
    efficiency_label = QtWidgets.QLabel("Efficiency")
    performance_label = QtWidgets.QLabel("Performance")
    performance_label.setAlignment(
        qt_flags(QtCore.Qt, "AlignmentFlag", "AlignRight", "AlignVCenter")
    )
    bias_labels.addWidget(efficiency_label)
    bias_labels.addStretch(1)
    bias_labels.addWidget(performance_label)
    slider_column.addWidget(bias_slider)
    slider_column.addLayout(bias_labels)

    bias_control_layout = QtWidgets.QHBoxLayout()
    bias_control_layout.setContentsMargins(0, 0, 0, 0)
    bias_control_layout.setSpacing(12)
    bias_control_layout.addWidget(
        _bias_icon(
            QtCore=QtCore,
            QtGui=QtGui,
            QtWidgets=QtWidgets,
            filename="penguin-burner-green.png",
            tooltip="Efficiency",
        )
    )
    bias_control_layout.addLayout(slider_column, 1)
    bias_control_layout.addWidget(
        _bias_icon(
            QtCore=QtCore,
            QtGui=QtGui,
            QtWidgets=QtWidgets,
            filename="penguin-burner.png",
            tooltip="Performance",
        )
    )
    bias_layout.addLayout(bias_control_layout)

    advanced_group = QtWidgets.QGroupBox("Advanced")
    advanced_group.setObjectName("advancedTuningGroup")
    form = QtWidgets.QFormLayout(advanced_group)
    form.setContentsMargins(14, 24, 14, 14)
    form.setHorizontalSpacing(16)
    form.setVerticalSpacing(10)
    form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldsStayAtSizeHint)

    voltage_drop_default = auto_uv_voltage_drop_default(yolo=bool(yolo))
    max_drop_spin = _double_spin(
        QtWidgets,
        1.0,
        30.0,
        float(voltage_drop_default.value_pct),
        "%",
    )
    max_drop_spin.setSingleStep(1.0)
    voltage_drop_note = QtWidgets.QLabel(
        _auto_voltage_drop_note_text(voltage_drop_default)
    )
    voltage_drop_note.setObjectName("autoVoltageDropNote")
    voltage_drop_note.setWordWrap(False)
    max_clock_drop_spin = _double_spin(
        QtWidgets,
        1.0,
        30.0,
        DEFAULT_AUTO_UV_MAX_CLOCK_DROP_PCT,
        "%",
    )
    short_seconds_spin = QtWidgets.QSpinBox()
    short_seconds_spin.setRange(10, 60)
    short_seconds_spin.setSuffix(" sec")
    short_seconds_spin.setSingleStep(5)
    short_seconds_spin.setFixedWidth(110)
    short_seconds_spin.setValue(DEFAULT_SHORT_VERIFICATION_BASE_S)
    memory_offset_spin = QtWidgets.QSpinBox()
    memory_min_mhz, memory_max_mhz = memory_offset_mhz_range()
    memory_offset_spin.setRange(memory_min_mhz, memory_max_mhz)
    memory_offset_spin.setSuffix(" MHz")
    memory_offset_spin.setSingleStep(50)
    memory_offset_spin.setFixedWidth(118)

    _add_form_row(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        form_layout=form,
        text="Max voltage drop",
        widget=max_drop_spin,
        tooltip=(
            "Default is calculated from the detected GPU preset efficiency "
            "voltage floor. If the GPU is unsupported or cannot be detected, "
            "the default is 15%. Changing this can result in instability; "
            "modify with care."
        ),
    )
    form.addRow("", voltage_drop_note)
    _add_form_row(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        form_layout=form,
        text="Max clock drop",
        widget=max_clock_drop_spin,
        tooltip=(
            "How much loaded frequency degradation Auto-UV may accept while "
            "lowering voltage. Higher values allow deeper undervolts with more "
            "performance loss. Changing this can result in instability; modify with care."
        ),
    )
    _add_form_row(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        form_layout=form,
        text="Memory Offset MHz",
        widget=memory_offset_spin,
        tooltip=(
            "Optional global memory clock V/F offset in MHz applied during the "
            "Auto-UV scan and saved with the final profile. Higher values can "
            "improve memory performance, but may introduce instability or be "
            "rejected by the Nvidia driver; modify with care."
        ),
    )
    _add_form_row(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        form_layout=form,
        text="Base verification length",
        widget=short_seconds_spin,
    )

    bias_slider.setValue(
        performance_bias_slider_position(
            DEFAULT_AUTO_UV_PERFORMANCE_BIAS_PCT,
            max_pct=max_bias_pct,
        )
    )

    buttons = QtWidgets.QDialogButtonBox()
    role_enum = getattr(QtWidgets.QDialogButtonBox, "ButtonRole", QtWidgets.QDialogButtonBox)
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

    layout.addWidget(purpose)
    layout.addWidget(bias_group)
    layout.addWidget(advanced_group)
    layout.addWidget(buttons)
    dialog.setMinimumWidth(520)
    if dialog.exec() != QtWidgets.QDialog.Accepted:
        return None

    performance_bias_pct = performance_bias_clock_recovery_pct(
        bias_slider.value(),
        max_pct=max_bias_pct,
    )
    return {
        "auto_uv_mode": auto_uv_mode_for_performance_bias(performance_bias_pct),
        "auto_uv_max_drop_pct": float(max_drop_spin.value()),
        "auto_uv_max_clock_drop_pct": float(max_clock_drop_spin.value()),
        "auto_uv_clock_bump_budget_ratio": performance_bias_pct / 100.0,
        "auto_uv_yolo": bool(yolo),
        "auto_uv_memory_offset_mhz": int(memory_offset_spin.value()),
        "auto_uv_short_seconds": int(short_seconds_spin.value()),
    }


def _auto_voltage_drop_note_text(default) -> str:
    gpu_label = str(default.gpu_name or default.gpu_family or "").strip()
    if bool(default.preset_matched):
        return f"Max voltage drop auto-filled for {gpu_label}"
    if gpu_label:
        return f"Using generic max voltage drop for {gpu_label}"
    return "Using generic max voltage drop"


def _click_jump_slider_class(QtCore, QtWidgets):
    class ClickJumpSlider(QtWidgets.QSlider):
        def _event_x(self, event) -> float:
            position = event.position() if hasattr(event, "position") else event.pos()
            return float(position.x())

        def _set_value_from_event(self, event) -> None:
            self.setValue(
                slider_value_from_click_position(
                    position_px=self._event_x(event),
                    width_px=self.width(),
                    minimum=self.minimum(),
                    maximum=self.maximum(),
                    inverted=self.invertedAppearance(),
                )
            )

        def mousePressEvent(self, event) -> None:
            left_button = getattr(
                QtCore.Qt.MouseButton,
                "LeftButton",
                QtCore.Qt.LeftButton,
            )
            if event.button() == left_button:
                self.setSliderDown(True)
                self._set_value_from_event(event)
                event.accept()
                return
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event) -> None:
            left_button = getattr(
                QtCore.Qt.MouseButton,
                "LeftButton",
                QtCore.Qt.LeftButton,
            )
            if event.buttons() & left_button:
                self._set_value_from_event(event)
                event.accept()
                return
            super().mouseMoveEvent(event)

    return ClickJumpSlider


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


def _add_form_row(*, QtCore, QtWidgets, form_layout, text: str, widget, tooltip: str = ""):
    label_widget = QtWidgets.QLabel(text)
    if not tooltip:
        form_layout.addRow(label_widget, widget)
        return

    wrapped = _wrapped_tooltip(tooltip)
    label_widget.setToolTip(wrapped)
    widget.setToolTip(wrapped)
    widget.setToolTipDuration(20000)
    label_container = QtWidgets.QWidget()
    label_layout = QtWidgets.QHBoxLayout(label_container)
    label_layout.setContentsMargins(0, 0, 0, 0)
    label_layout.setSpacing(6)
    info_button = QtWidgets.QToolButton()
    info_button.setObjectName("infoButton")
    info_button.setText("i")
    info_button.setToolTip(wrapped)
    info_button.setToolTipDuration(20000)
    info_button.setCursor(QtCore.Qt.WhatsThisCursor)
    info_button.setFocusPolicy(QtCore.Qt.NoFocus)
    info_button.setFixedSize(18, 18)

    def show_tooltip(_checked=False, *, button=info_button, tip=wrapped):
        position = button.mapToGlobal(button.rect().bottomLeft())
        QtWidgets.QToolTip.showText(position, tip, button)

    info_button.clicked.connect(show_tooltip)
    label_layout.addWidget(label_widget)
    label_layout.addWidget(info_button)
    label_layout.addStretch(1)
    form_layout.addRow(label_container, widget)


def _wrapped_tooltip(text: str) -> str:
    normalized = " ".join(str(text).split())
    escaped = html.escape(normalized)
    return f"<qt><table width='680'><tr><td>{escaped}</td></tr></table></qt>"


def _double_spin(QtWidgets, minimum: float, maximum: float, value: float, suffix: str):
    spin = QtWidgets.QDoubleSpinBox()
    spin.setRange(float(minimum), float(maximum))
    spin.setDecimals(1)
    spin.setSuffix(str(suffix))
    spin.setFixedWidth(96)
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
