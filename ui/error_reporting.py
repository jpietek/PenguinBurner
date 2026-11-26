from __future__ import annotations

from .dialogs.error_details import (
    process_failure_details,
    qt_enum_name,
    show_error_dialog,
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
