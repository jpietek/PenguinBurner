from __future__ import annotations

from .dialogs.error_details import (
    process_failure_details,
    qt_enum_name,
    show_error_dialog,
)


class ErrorReporter:
    def __init__(self, *, QtCore, QtGui, QtWidgets, parent, controls, log_view):
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self.parent = parent
        self.controls = controls
        self.log_view = log_view

    def show(self, title: str, message: str) -> None:
        self.controls.set_status_text(str(message).splitlines()[0])
        self.log_view.append(f"\n{title}: {message}\n")
        show_error_dialog(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            parent=self.parent,
            title=title,
            message=message,
        )

    def show_process(
        self,
        *,
        title: str,
        action_label: str,
        exit_code,
        exit_status,
    ) -> None:
        show_process_failure(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            parent=self.parent,
            title=title,
            action_label=action_label,
            exit_code=exit_code,
            exit_status=exit_status,
            log_text=self.log_view.text_edit.toPlainText(),
        )


def show_process_failure(
    *,
    QtCore,
    QtGui,
    QtWidgets,
    parent,
    title: str,
    action_label: str,
    exit_code,
    exit_status,
    log_text: str,
) -> None:
    """The copyable error dialog for a scan or verification that failed."""
    show_error_dialog(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        parent=parent,
        title=title,
        message=(
            f"{action_label} stopped unexpectedly. "
            "The full error details can be copied for troubleshooting."
        ),
        details=process_failure_details(
            action_label=action_label,
            exit_code=exit_code,
            exit_status=qt_enum_name(exit_status),
            log_tail=recent_log_tail(log_text),
        ),
    )


def recent_log_tail(text: str, *, lines: int = 80, char_limit: int = 12000) -> str:
    tail = "\n".join(str(text or "").splitlines()[-max(1, int(lines)) :])
    return tail[-int(char_limit) :] if len(tail) > int(char_limit) else tail
