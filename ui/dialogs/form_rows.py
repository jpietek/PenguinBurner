"""Form-row building blocks shared by PenguinBurner's settings dialogs.

The Auto Undervolt dialog established how a settings row looks here: a label
with a small "i" button that shows a wide, wrapped tooltip, a fixed-width field
beside it, and Enter committing a spin box's typed text. Every dialog that asks
the user to tune numbers should look and behave the same, so those pieces live
here rather than inside one dialog.
"""

from __future__ import annotations

import html

from .error_details import qt_flags


def wrapped_tooltip(text: str) -> str:
    """A tooltip wide enough to read: Qt wraps at the table width, not the screen."""
    normalized = " ".join(str(text).split())
    escaped = html.escape(normalized)
    return f"<qt><table width='680'><tr><td>{escaped}</td></tr></table></qt>"


def dialog_form_layout(*, QtCore, QtWidgets):
    form = QtWidgets.QFormLayout()
    form.setContentsMargins(0, 0, 0, 0)
    form.setHorizontalSpacing(24)
    form.setVerticalSpacing(10)
    form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldsStayAtSizeHint)
    form.setFormAlignment(qt_flags(QtCore.Qt, "AlignmentFlag", "AlignLeft", "AlignTop"))
    form.setLabelAlignment(
        qt_flags(QtCore.Qt, "AlignmentFlag", "AlignLeft", "AlignVCenter")
    )
    return form


def add_form_row(
    *,
    QtCore,
    QtWidgets,
    form_layout,
    text: str,
    widget,
    tooltip: str = "",
) -> None:
    label_widget = QtWidgets.QLabel(text)
    if not tooltip:
        form_layout.addRow(label_widget, widget)
        return

    wrapped = wrapped_tooltip(tooltip)
    label_widget.setToolTip(wrapped)
    widget.setToolTip(wrapped)
    widget.setToolTipDuration(20000)
    label_container = QtWidgets.QWidget()
    label_layout = QtWidgets.QHBoxLayout(label_container)
    label_layout.setContentsMargins(0, 2, 12, 2)
    label_layout.setSpacing(8)
    info_button = info_tooltip_button(
        QtCore=QtCore, QtWidgets=QtWidgets, tooltip=wrapped
    )
    label_layout.addWidget(label_widget)
    label_layout.addWidget(info_button)
    label_layout.addStretch(1)
    form_layout.addRow(label_container, widget)


def info_tooltip_button(*, QtCore, QtWidgets, tooltip: str):
    """The small round "i" that reveals a long explanation on click."""
    info_button = QtWidgets.QToolButton()
    info_button.setObjectName("infoButton")
    info_button.setText("i")
    info_button.setToolTip(tooltip)
    info_button.setToolTipDuration(20000)
    info_button.setCursor(QtCore.Qt.WhatsThisCursor)
    info_button.setFocusPolicy(QtCore.Qt.NoFocus)
    info_button.setFixedSize(18, 18)

    def show_tooltip(_checked=False, *, button=info_button, tip=tooltip):
        position = button.mapToGlobal(button.rect().bottomLeft())
        QtWidgets.QToolTip.showText(position, tip, button)

    info_button.clicked.connect(show_tooltip)
    return info_button


def install_spinbox_enter_commit_filter(
    *, QtCore, QtWidgets, parent, spinboxes
) -> None:
    """Make Enter commit typed spin-box text instead of waiting for focus loss."""
    del QtWidgets  # Kept for a uniform call shape across these helpers.
    event_type = getattr(getattr(QtCore.QEvent, "Type", QtCore.QEvent), "KeyPress")
    key_enum = getattr(QtCore.Qt, "Key", QtCore.Qt)
    enter_keys = {
        qt_enum_value(getattr(key_enum, "Key_Return")),
        qt_enum_value(getattr(key_enum, "Key_Enter")),
    }

    class _SpinBoxEnterFilter(QtCore.QObject):
        def __init__(self):
            super().__init__(parent)
            self._spinboxes_by_target = {}
            for spinbox in spinboxes:
                self._spinboxes_by_target[spinbox] = spinbox
                try:
                    editor = spinbox.lineEdit()
                except AttributeError:
                    editor = None
                if editor is not None:
                    self._spinboxes_by_target[editor] = spinbox

        def eventFilter(self, watched, event):  # noqa: N802 - Qt override name
            if watched not in self._spinboxes_by_target:
                return False
            if event.type() != event_type:
                return False
            try:
                key = qt_enum_value(event.key())
            except AttributeError:
                return False
            if key not in enter_keys:
                return False
            spinbox = self._spinboxes_by_target[watched]
            spinbox.interpretText()
            event.accept()
            return True

    event_filter = _SpinBoxEnterFilter()
    for target in event_filter._spinboxes_by_target:
        target.installEventFilter(event_filter)
    parent._penguin_burner_spinbox_enter_filter = event_filter


def qt_enum_value(value) -> int:
    return int(getattr(value, "value", value))
