"""Runs one short root-service command at a time (Apply, Restore, Delete).

The classic window's CommandController (a QProcess) with the finish turned
into a ``CommandResult``: the captured output becomes the copyable error
report of a failed command, as the window's ErrorReporter builds it.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtCore as C

from ui.controllers.command import CommandController
from ui.dialogs.error_details import process_failure_details, qt_enum_name
from ui.features.profiles.profile_model import CommandResult

LOG_TAIL_LINES = 80
LOG_TAIL_CHARS = 12000


def output_tail(text: str) -> str:
    """The last lines of a command's output, as the error report keeps them."""
    tail = "\n".join(str(text).splitlines()[-LOG_TAIL_LINES:])
    return tail[-LOG_TAIL_CHARS:]


def failure_message(output: str, exit_code) -> str:
    """The line a failed command ends with (not its echoed command line)."""
    for line in reversed(str(output).splitlines()):
        text = line.strip()
        if text and not text.startswith("$ "):
            return text
    return f"Exit code {exit_code}."


class ProfileCommandRunner:
    """One command at a time; ``finished`` is called exactly once per start."""

    def __init__(self, parent: C.QObject | None = None, *, controller=None):
        self.controller = controller or CommandController(QtCore=C, parent=parent)
        self.controller.on_output = self._output
        self.controller.on_finished = self._finished
        self._output_text = ""
        self._pending: tuple[str, str, Callable[[CommandResult], None]] | None = None

    def is_running(self) -> bool:
        return self._pending is not None or self.controller.is_running()

    def start(
        self,
        label: str,
        kind: str,
        command: list[str],
        finished: Callable[[CommandResult], None],
        *,
        fail_text: str = "",
        success_message: str = "",
    ) -> None:
        if self.is_running():
            finished(CommandResult(False, "Another PenguinBurner command is still running."))
            return
        self._output_text = ""
        self._pending = (label, success_message, finished)
        # A command that cannot start finishes inside start() with exit -1.
        if not self.controller.start(kind, command, fail_text=fail_text):
            self._resolve_start_refusal()

    def _resolve_start_refusal(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            pending[2](CommandResult(False, "The command did not start."))

    def _output(self, text: str) -> None:
        self._output_text = output_tail(self._output_text + str(text))

    def _finished(self, _kind: str, exit_code, exit_status) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            return
        label, success_message, finished = pending
        if int(exit_code) == 0:
            finished(CommandResult(True, success_message or f"{label} complete."))
            return
        output = self._output_text
        finished(
            CommandResult(
                False,
                failure_message(output, exit_code),
                details=process_failure_details(
                    action_label=label,
                    exit_code=exit_code,
                    exit_status=qt_enum_name(exit_status),
                    log_tail=output,
                ),
            )
        )
