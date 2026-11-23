from __future__ import annotations

import json
import os
import signal
from collections.abc import Callable
from pathlib import Path

from auto_uv.persistence.auto_uv_persisted_json_files import safe_json_write


class ScanController:
    def __init__(self, *, QtCore, parent=None, stop_request_path: Path | None = None):
        self.QtCore = QtCore
        self.parent = parent
        self.stop_request_path = stop_request_path
        self.process = None
        self.stop_requested = False
        self._buffer = ""
        self.on_output: Callable[..., None] = lambda _text: None
        self.on_event: Callable[..., None] = lambda _payload: None
        self.on_human_line: Callable[..., None] = lambda _line: None
        self.on_finished: Callable[..., None] = (
            lambda _exit_code, _exit_status, _stopped: None
        )

    def is_running(self) -> bool:
        return self.process is not None

    def start(self, command: list[str]) -> bool:
        if self.process is not None or not command:
            return False
        self.stop_requested = False
        self._buffer = ""
        if self.stop_request_path is not None:
            self.stop_request_path.parent.mkdir(parents=True, exist_ok=True)
            self.stop_request_path.unlink(missing_ok=True)
        process = self.QtCore.QProcess(self.parent)
        process.setProcessChannelMode(self.QtCore.QProcess.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_output)
        process.finished.connect(self._finished)
        self.process = process
        process.start(command[0], command[1:])
        if process.waitForStarted(3000):
            return True
        self.on_output("Failed to start Auto-UV process.\n")
        self._finished(-1, self.QtCore.QProcess.CrashExit)
        return False

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        self.stop_requested = True
        if self.stop_request_path is not None:
            self.stop_request_path.parent.mkdir(parents=True, exist_ok=True)
            self.stop_request_path.write_text(
                "stop requested by PenguinBurner UI\n",
                encoding="utf-8",
            )
            self.on_output(f"\nRequested cooperative Auto-UV stop: {self.stop_request_path}\n")
        pid = int(process.processId())
        if pid > 0:
            try:
                os.kill(pid, signal.SIGINT)
                self.on_output("Sent SIGINT to Auto-UV launcher.\n")
            except OSError:
                process.terminate()
        else:
            process.terminate()
        self.QtCore.QTimer.singleShot(30000, lambda: self.kill_if_running(process))

    def write_json_response(self, path: str | Path, payload: dict) -> None:
        """Answer the scan's final-choice request. Atomic, as the CLI's
        answer is: the scan reads the file as soon as it exists, and a
        partial read would fall back to the default candidate."""
        if not str(path).strip():
            return
        safe_json_write(Path(path).expanduser(), payload)

    def kill_if_running(self, process=None) -> None:
        target = self.process if process is None else process
        if target is None or self.process is not target:
            return
        if target.state() != self.QtCore.QProcess.NotRunning:
            target.kill()

    def _read_output(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            chunk = process.readAllStandardOutput()
        except RuntimeError:
            # A readyRead signal can be delivered after the QProcess has been
            # retired/deleted (e.g. right after a stop). The underlying C++
            # object is gone, so there is nothing left to read.
            return
        data = bytes(chunk).decode(
            "utf-8",
            errors="replace",
        )
        if not data:
            return
        # CLI JSON events are line-delimited, but QProcess chunks are not.
        # We route per line so machine events stay out of the copied log view.
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                # Machine event: already rendered as UI state (runs table,
                # curve, header). Do NOT echo it to the log view, so the log a
                # user copies from the GUI stays readable instead of doubling
                # every tick with a raw JSON dict.
                self.on_event(payload)
                return
        # Human-readable progress line: show it in the log and let the window
        # derive header text from it.
        if stripped == "Interrupted by user.":
            self.stop_requested = True
        self.on_output(line + "\n")
        self.on_human_line(stripped)

    def _finished(self, exit_code, exit_status) -> None:
        process = self.process
        # Drain anything still buffered in the pipe so trailing JSON events
        # (e.g. a final_choice_request emitted just before exit) are not lost.
        if process is not None:
            try:
                tail = bytes(process.readAllStandardOutput()).decode(
                    "utf-8", errors="replace"
                )
            except RuntimeError:
                tail = ""
            if tail:
                self._buffer += tail
        # Flush any remaining whole lines, then the trailing partial, through
        # the same router so the final line is shown/parsed (not raw-echoed).
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._handle_line(line)
        if self._buffer.strip():
            self._handle_line(self._buffer)
        self._buffer = ""
        self.process = None
        if process is not None:
            # Detach the slots so a late readyRead/finished can never fire on a
            # retired process (which would touch a deleted C++ object).
            try:
                process.readyReadStandardOutput.disconnect(self._read_output)
                process.finished.disconnect(self._finished)
            except (RuntimeError, TypeError):
                pass
        stopped = bool(self.stop_requested) or int(exit_code) == 130
        self.stop_requested = False
        self.on_finished(exit_code, exit_status, stopped)
        if process is not None:
            process.deleteLater()
