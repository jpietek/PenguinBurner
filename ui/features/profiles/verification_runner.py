"""Profiles → Verify: rerun final verification on a saved profile.

The classic window runs it with its VerifyController and shows it on the
Auto UV tab (profile_actions._verify_profile). Until the new Auto UV page
takes the page's ``verify_requested`` over, this runs the same daemon
command with the same controller and reports through the window: header
progress, window status, the error report of a failed run, and Stop.
"""

from __future__ import annotations

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from profiles.uv.profile_rules import profile_for_selector, profile_verify_selector
from runtime.daemon_commands import profile_verify_command
from ui.controllers.verify import VerifyController
from ui.dialogs.error_details import (
    process_failure_details,
    qt_enum_name,
    show_error_dialog,
)
from ui.features.profiles.command_runner import output_tail
from ui.features.tuning.verify import stop_request_path, workload_label

# How long the finished bar stays up, as the classic controls keep it.
DONE_PROGRESS_MS = 2500


def duration_text(seconds: float) -> str:
    """5min, 4min 30s or 45s, as the preview's verification progress reads."""
    total = max(0, round(float(seconds)))
    minutes, rest = divmod(total, 60)
    if minutes and rest:
        return f"{minutes}min {rest}s"
    return f"{minutes}min" if minutes else f"{rest}s"


class ProfileVerificationRunner(C.QObject):
    """One verification at a time over the root daemon's streaming command."""

    # Header progress: (text or None to hide, percent, tooltip).
    progress_changed = C.Signal(object, int, str)
    # Window status (running, detail, tone); {} clears it.
    status_changed = C.Signal(dict)
    running_changed = C.Signal(bool)
    # profile id, passed, stopped by the user
    finished = C.Signal(str, bool, bool)

    def __init__(self, profiles, *, parent: W.QWidget | None = None, controller=None):
        """``profiles`` returns the current profile summaries."""
        super().__init__(parent)
        self.parent_widget = parent
        self.profiles = profiles
        self.controller = controller or VerifyController(
            QtCore=C, parent=self, stop_request_path=stop_request_path()
        )
        self.controller.on_output = self._output
        self.controller.on_progress = self._progress
        self.controller.on_finished = self._finished
        self.profile_id = ""
        self.label = ""
        self.output = ""

    def is_running(self) -> bool:
        return self.controller.is_running()

    def start(self, profile_id: str, label: str, row: dict, minutes: int) -> bool:
        """Start verifying a profile on ``row["gpu_index"]`` for ``minutes``."""
        profile = profile_for_selector(self.profiles(), profile_id)
        if self.is_running() or profile is None:
            return False
        duration_s = max(1, int(minutes)) * 60
        gpu_index = row.get("gpu_index")
        command = profile_verify_command(
            profile_selector=profile_verify_selector(profile),
            duration_s=duration_s,
            stop_request_path=stop_request_path(),
            gpu_index=None if gpu_index is None else int(gpu_index),
        )
        self.profile_id, self.label, self.output = profile_id, label, ""
        detail = f"Verifying {label} with {workload_label()}."
        self.status_changed.emit(
            {"running": "Profile verification", "detail": detail, "tone": ""}
        )
        self.progress_changed.emit(
            f"Verifying profile 0s / {duration_text(duration_s)}", 0, detail
        )
        self.running_changed.emit(True)
        return self.controller.start(command, duration_s=duration_s)

    def stop(self) -> None:
        if self.is_running():
            self.status_changed.emit(
                {
                    "running": "Stopping profile verification.",
                    "detail": self.label,
                    "tone": "",
                }
            )
            self.controller.stop()

    def _output(self, text: str) -> None:
        self.output = output_tail(self.output + str(text))

    def _progress(self, percent, *, elapsed_s=None, target_s=None, detail="") -> None:
        text = (
            f"Verifying profile {duration_text(elapsed_s or 0)} / "
            f"{duration_text(target_s or 0)}"
        )
        self.progress_changed.emit(text, int(percent), str(detail or text))

    def _finished(self, exit_code, exit_status, stopped_by_user: bool) -> None:
        passed = int(exit_code) == 0
        target = int(self.controller.target_duration_s or 0)
        if passed:
            self.progress_changed.emit(
                f"Verifying profile {duration_text(target)} / {duration_text(target)}",
                100,
                "Profile verification complete.",
            )
            C.QTimer.singleShot(
                DONE_PROGRESS_MS, lambda: self.progress_changed.emit(None, 0, "")
            )
        else:
            self.progress_changed.emit(None, 0, "")
        self.status_changed.emit({})
        self.running_changed.emit(False)
        profile_id = self.profile_id
        self.profile_id = ""
        if not passed and not stopped_by_user:
            show_error_dialog(
                QtCore=C,
                QtGui=G,
                QtWidgets=W,
                parent=self.parent_widget,
                title="Profile verification failed",
                message=(
                    "Profile verification stopped unexpectedly. "
                    "The full error details can be copied for troubleshooting."
                ),
                details=process_failure_details(
                    action_label="Profile verification",
                    exit_code=exit_code,
                    exit_status=qt_enum_name(exit_status),
                    log_tail=self.output,
                ),
            )
        self.finished.emit(profile_id, passed, bool(stopped_by_user and not passed))
