"""Coverage for ui/controllers/scan.py and ui/controllers/verify.py.

Line parsing / json response are tested directly; the QProcess-backed
start/stop/read/finish paths run real short-lived commands under qtbot.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore

from ui.controllers.scan import ScanController
from ui.controllers.verify import VerifyController

# --- ScanController: pure-ish line handling -----------------------------------


def test_scan_handle_line_routes_events_and_human_text() -> None:
    controller = ScanController(QtCore=QtCore)
    events: list[dict] = []
    humans: list[str] = []
    controller.on_event = events.append
    controller.on_human_line = humans.append

    controller._handle_line('{"event": "probe_start"}')
    controller._handle_line("plain human progress line")
    controller._handle_line("{bad json")  # starts with { but won't parse
    controller._handle_line("   ")  # blank -> ignored

    assert events == [{"event": "probe_start"}]
    assert humans == ["plain human progress line", "{bad json"]


class _DeletedQProcess:
    """Stand-in for a QProcess whose underlying C++ object has been deleted:
    touching it raises RuntimeError, mirroring shiboken's behaviour."""

    def readAllStandardOutput(self):
        raise RuntimeError("Internal C++ object (QProcess) already deleted.")


def test_scan_handle_line_keeps_json_out_of_log_view() -> None:
    # The copied GUI log must show human lines but NOT the raw JSON events
    # (which are already rendered as UI state) — otherwise it doubles per tick.
    controller = ScanController(QtCore=QtCore)
    output: list[str] = []
    events: list[dict] = []
    humans: list[str] = []
    controller.on_output = output.append
    controller.on_event = events.append
    controller.on_human_line = humans.append

    controller._handle_line('{"event": "load_telemetry", "voltage_mv": 880}')
    controller._handle_line("Auto-UV phase=discover-live live=880mV power=24.5W")
    controller._handle_line("{not valid json")

    assert events == [{"event": "load_telemetry", "voltage_mv": 880}]
    # JSON line is not echoed; the human line and the bad-json line are.
    assert "".join(output) == (
        "Auto-UV phase=discover-live live=880mV power=24.5W\n{not valid json\n"
    )
    assert humans == [
        "Auto-UV phase=discover-live live=880mV power=24.5W",
        "{not valid json",
    ]


def test_scan_interrupted_line_marks_user_stop() -> None:
    controller = ScanController(QtCore=QtCore)

    controller._handle_line("Interrupted by user.")

    assert controller.stop_requested is True


def test_scan_read_output_survives_deleted_process() -> None:
    # A readyRead delivered after the process was retired must not crash.
    controller = ScanController(QtCore=QtCore)
    controller.process = _DeletedQProcess()
    controller._read_output()  # no exception propagates


class _RecordingSignal:
    def __init__(self) -> None:
        self.disconnected: list = []

    def disconnect(self, slot) -> None:
        self.disconnected.append(slot)


class _FinishingQProcess:
    def __init__(self) -> None:
        self.readyReadStandardOutput = _RecordingSignal()
        self.finished = _RecordingSignal()
        self.deleted = False

    def readAllStandardOutput(self):
        return b""

    def deleteLater(self) -> None:
        self.deleted = True


class _KillableQProcess:
    def __init__(self, state) -> None:
        self._state = state
        self.killed = False

    def state(self):
        return self._state

    def kill(self) -> None:
        self.killed = True


def test_scan_finished_detaches_slots_and_reports() -> None:
    controller = ScanController(QtCore=QtCore)
    finished: list = []
    controller.on_finished = lambda code, status, stopped: finished.append(
        (code, stopped)
    )
    process = _FinishingQProcess()
    controller.process = process
    controller.stop_requested = True

    controller._finished(0, 0)

    assert controller.process is None
    assert finished == [(0, True)]
    assert process.deleted is True
    # Both slots were detached so a late callback cannot touch the dead process.
    assert controller._read_output in process.readyReadStandardOutput.disconnected
    assert controller._finished in process.finished.disconnected


def test_scan_finished_treats_sigint_exit_as_stopped() -> None:
    controller = ScanController(QtCore=QtCore)
    finished: list = []
    controller.on_finished = lambda code, status, stopped: finished.append(
        (code, stopped)
    )
    process = _FinishingQProcess()
    controller.process = process

    controller._finished(130, 0)

    assert finished == [(130, True)]


def test_scan_stop_watchdog_only_kills_original_process() -> None:
    controller = ScanController(QtCore=QtCore)
    old_process = _KillableQProcess(QtCore.QProcess.Running)
    new_process = _KillableQProcess(QtCore.QProcess.Running)
    controller.process = new_process

    controller.kill_if_running(old_process)

    assert old_process.killed is False
    assert new_process.killed is False

    controller.kill_if_running(new_process)

    assert new_process.killed is True


def test_scan_write_json_response(tmp_path) -> None:
    controller = ScanController(QtCore=QtCore)
    out = tmp_path / "resp.json"
    controller.write_json_response(out, {"candidate_id": "c1"})
    assert "candidate_id" in out.read_text(encoding="utf-8")
    # Empty path is a no-op.
    controller.write_json_response("   ", {"x": 1})


def test_scan_start_rejects_empty_and_busy(qtbot) -> None:
    controller = ScanController(QtCore=QtCore)
    assert controller.start([]) is False
    assert controller.start([sys.executable, "-c", "import time; time.sleep(0.4)"]) is True
    assert controller.start([sys.executable, "-c", "pass"]) is False  # already running
    qtbot.waitUntil(lambda: not controller.is_running(), timeout=5000)


def test_scan_runs_command_and_parses_output(qtbot, tmp_path) -> None:
    controller = ScanController(QtCore=QtCore, stop_request_path=tmp_path / "stop")
    events: list[dict] = []
    humans: list[str] = []
    finished: list[tuple] = []
    controller.on_event = events.append
    controller.on_human_line = humans.append
    controller.on_finished = lambda code, status, stopped: finished.append((code, stopped))

    script = "print('{\"event\": \"auto_uv_start\"}'); print('human tail line')"
    assert controller.start([sys.executable, "-c", script]) is True
    qtbot.waitUntil(lambda: not controller.is_running(), timeout=5000)

    assert {"event": "auto_uv_start"} in events
    assert any("human tail line" in line for line in humans)
    assert finished and finished[0][1] is False


def test_scan_failed_start_reports(qtbot) -> None:
    controller = ScanController(QtCore=QtCore)
    outputs: list[str] = []
    controller.on_output = outputs.append
    assert controller.start(["/nonexistent/pburn-binary"]) is False
    assert any("Failed to start" in text for text in outputs)


def test_scan_stop_writes_request_and_marks_stopped(qtbot, tmp_path) -> None:
    stop_path = tmp_path / "stop-requested"
    controller = ScanController(QtCore=QtCore, stop_request_path=stop_path)
    finished: list[tuple] = []
    controller.on_finished = lambda code, status, stopped: finished.append(stopped)

    assert controller.start([sys.executable, "-c", "import time; time.sleep(10)"]) is True
    controller.stop()
    assert stop_path.exists()
    qtbot.waitUntil(lambda: not controller.is_running(), timeout=6000)
    assert finished and finished[0] is True
    # kill_if_running is a no-op once the process has exited.
    controller.kill_if_running()


# --- VerifyController ---------------------------------------------------------


def test_verify_start_rejects_empty() -> None:
    controller = VerifyController(QtCore=QtCore, stop_request_path=None)  # type: ignore[arg-type]
    assert controller.start([], duration_s=60) is False


def test_verify_runs_and_reports_progress(qtbot, tmp_path) -> None:
    controller = VerifyController(QtCore=QtCore, stop_request_path=tmp_path / "vstop")
    progress: list[tuple] = []
    finished: list[tuple] = []
    controller.on_progress = lambda pct, *, elapsed_s, target_s, detail: progress.append((pct, elapsed_s))
    controller.on_finished = lambda code, status, stopped: finished.append((code, stopped))

    script = "print('status elapsed=30.0s running'); print('status elapsed=60.0s done')"
    assert controller.start([sys.executable, "-c", script], duration_s=60) is True
    qtbot.waitUntil(lambda: not controller.is_running(), timeout=5000)

    assert controller.target_duration_s == 60
    assert progress and progress[-1][0] == 100  # 60/60 -> 100%
    assert finished and finished[0][1] is False
    assert not (tmp_path / "vstop").exists()  # cleaned up on finish




def test_verify_routes_telemetry_json_out_of_the_log_view(qtbot, tmp_path) -> None:
    controller = VerifyController(QtCore=QtCore, stop_request_path=tmp_path / "vstop")
    outputs: list[str] = []
    telemetry: list[dict] = []
    controller.on_output = outputs.append
    controller.on_telemetry = telemetry.append

    # The last line has no trailing newline, exercising the drain-on-finish path.
    script = (
        "import sys\n"
        "print('{\"event\": \"load_telemetry\", \"voltage_mv\": 925, \"clock_mhz\": 2950}')\n"
        "print('status elapsed=5.0s running')\n"
        "sys.stdout.write('{\"event\": \"load_telemetry\", \"voltage_mv\": 926, \"clock_mhz\": 2951}')\n"
    )
    assert controller.start([sys.executable, "-c", script], duration_s=60) is True
    qtbot.waitUntil(lambda: not controller.is_running(), timeout=5000)

    assert telemetry == [
        {"event": "load_telemetry", "voltage_mv": 925, "clock_mhz": 2950},
        {"event": "load_telemetry", "voltage_mv": 926, "clock_mhz": 2951},
    ]
    joined_output = "".join(outputs)
    assert "load_telemetry" not in joined_output
    assert "status elapsed=5.0s running" in joined_output


def test_verify_failed_start_reports(qtbot, tmp_path) -> None:
    controller = VerifyController(QtCore=QtCore, stop_request_path=tmp_path / "vstop")
    outputs: list[str] = []
    controller.on_output = outputs.append
    assert controller.start(["/nonexistent/pburn-verify"], duration_s=10) is False
    assert any("Failed to start" in text for text in outputs)


def test_verify_stop_marks_stopped(qtbot, tmp_path) -> None:
    stop_path = tmp_path / "vstop"
    controller = VerifyController(QtCore=QtCore, stop_request_path=stop_path)
    finished: list[bool] = []
    controller.on_finished = lambda code, status, stopped: finished.append(stopped)

    assert controller.start([sys.executable, "-c", "import time; time.sleep(10)"], duration_s=60) is True
    controller.stop()
    assert stop_path.exists()
    qtbot.waitUntil(lambda: not controller.is_running(), timeout=6000)
    assert finished and finished[0] is True
