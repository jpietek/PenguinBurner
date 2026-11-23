"""Coverage for UI helper modules that hold pure logic (no live Qt needed).

Covers ui/verify.py and ui/error_reporting
(driven through fake controls/log_view + monkeypatched dialog functions).
"""

from __future__ import annotations

import ui.error_reporting as error_reporting
from ui.features.tuning.verify import (
    elapsed_from_line,
    progress_percent,
    workload_label,
)

# --- ui/verify.py -------------------------------------------------------------


def test_elapsed_from_line_parses_and_floors_at_zero() -> None:
    assert elapsed_from_line("status elapsed=12.5s remaining") == 12.5
    assert elapsed_from_line("elapsed=0s") == 0.0


def test_elapsed_from_line_returns_none_without_match() -> None:
    assert elapsed_from_line("no timing here") is None
    assert elapsed_from_line("") is None
    assert elapsed_from_line(None) is None


def test_progress_percent_clamps_to_0_100() -> None:
    assert progress_percent(0, 600) == 0
    assert progress_percent(300, 600) == 50
    assert progress_percent(9999, 600) == 100


def test_progress_percent_returns_zero_on_bad_input() -> None:
    assert progress_percent(None, 600) == 0
    assert progress_percent("x", "y") == 0


def test_workload_label_is_fixed_combined_workload() -> None:
    assert workload_label() == "Q2RTX benchmark and CUDA compute test"


# --- ui/error_reporting.py ----------------------------------------------------


class _FakeControls:
    def __init__(self) -> None:
        self.status_text = None

    def set_status_text(self, text: str) -> None:
        self.status_text = text


class _FakeTextEdit:
    def __init__(self, text: str) -> None:
        self._text = text

    def toPlainText(self) -> str:
        return self._text


class _FakeLogView:
    def __init__(self, text: str = "") -> None:
        self.appended: list[str] = []
        self.text_edit = _FakeTextEdit(text)

    def append(self, text: str) -> None:
        self.appended.append(text)


def _reporter(monkeypatch, *, log_text: str = ""):
    calls: list[dict] = []
    monkeypatch.setattr(
        error_reporting, "show_error_dialog", lambda **kw: calls.append(kw)
    )
    monkeypatch.setattr(
        error_reporting,
        "process_failure_details",
        lambda **kw: f"details:{kw['action_label']}:{kw['exit_code']}",
    )
    monkeypatch.setattr(error_reporting, "qt_enum_name", lambda status: f"status:{status}")
    controls = _FakeControls()
    log_view = _FakeLogView(log_text)
    reporter = error_reporting.ErrorReporter(
        QtCore=object(),
        QtGui=object(),
        QtWidgets=object(),
        parent=object(),
        controls=controls,
        log_view=log_view,
    )
    return reporter, controls, log_view, calls


def test_error_reporter_show_sets_status_logs_and_opens_dialog(monkeypatch) -> None:
    reporter, controls, log_view, calls = _reporter(monkeypatch)

    reporter.show("VF Curve", "Something failed\nsecond line")

    assert controls.status_text == "Something failed"
    assert any("VF Curve: Something failed" in entry for entry in log_view.appended)
    assert calls and calls[0]["title"] == "VF Curve"


def test_error_reporter_show_process_includes_details(monkeypatch) -> None:
    reporter, _controls, _log_view, calls = _reporter(monkeypatch, log_text="line\n" * 5)

    reporter.show_process(
        title="Scan failed",
        action_label="Auto-UV scan",
        exit_code=3,
        exit_status="crash",
    )

    assert calls[0]["title"] == "Scan failed"
    assert calls[0]["details"] == "details:Auto-UV scan:3"


def test_recent_log_tail_limits_lines_and_chars() -> None:
    log_text = "\n".join(f"line{i}" for i in range(200))

    tail = error_reporting.recent_log_tail(log_text, lines=10, char_limit=12000)
    assert tail.splitlines() == [f"line{i}" for i in range(190, 200)]

    clipped = error_reporting.recent_log_tail(log_text, lines=200, char_limit=20)
    assert len(clipped) == 20
