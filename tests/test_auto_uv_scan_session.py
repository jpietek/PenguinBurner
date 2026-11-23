"""auto_uv/session/scan_session.py: the scan state both windows render.

The fixtures in tests/fixtures/auto_uv_scan are scan streams recorded from
the issue #109 replay harness (tests/auto_uv/test_issue109_rtx3080_replay.py)
running the real main loop: launch 1 freezes during the Efficiency descent,
launch 3 resumes from the checkpoint and finishes all three tiers. Curves are
thinned to every eighth point; launch 3 ends with the CLI's final_result.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_uv.session.scan_session import (
    DISCARDED_STATUS,
    RESTORED_STATUS,
    ScanSession,
    memory_offset_status_text,
    pre_scan_silent_fan,
    requested_memory_offset_mhz,
    verify_progress_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "auto_uv_scan"
CRASH = "issue109-launch1-efficiency-crash.jsonl"
RESUME = "issue109-launch3-resume-complete.jsonl"


def events(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]


def full_scan(name: str | None = None, limit: int | None = None) -> ScanSession:
    session = ScanSession()
    session.start({"auto_uv_mode": "adaptive"})
    for payload in events(name)[:limit] if name else []:
        session.handle_event(payload)
    return session


def test_start_shows_the_requested_memory_offset_and_pending_tiers() -> None:
    session = ScanSession()
    session.start({"auto_uv_mode": "adaptive", "auto_uv_efficiency_memory_offset_mhz": 2000})

    assert (session.stage, session.candidate) == (
        "Starting",
        "Writing to main Auto-UV profile store",
    )
    assert session.status == "Memory offset: +1000 MHz memory clock"
    assert session.tier_states == dict.fromkeys(
        ("efficiency", "balanced", "performance"), "pending"
    )
    session.start({"auto_uv_mode": "balanced", "auto_uv_memory_offset_mhz": 0})
    assert session.tier_states == {} and session.status == "Memory offset: none"


def test_requested_memory_offset_prefers_the_scan_wide_value() -> None:
    assert requested_memory_offset_mhz({"auto_uv_memory_offset_mhz": 600}) == 300
    assert requested_memory_offset_mhz({"auto_uv_efficiency_memory_offset_mhz": 900}) == 450
    assert requested_memory_offset_mhz({}) == 0
    assert memory_offset_status_text(-200) == "Memory offset: -200 MHz memory clock"


def test_crashed_efficiency_descent_fails_its_tier_and_the_running_probe() -> None:
    session = full_scan(CRASH)
    rows = session.runs.rows
    assert rows[0].kind == "baseline" and rows[0].decision == "Pass"
    assert rows[-1].decision == "Running"
    assert session.tier_states["efficiency"] == "active"
    assert session.candidate == "818 mV @ 1740 MHz"

    finish, update = session.finish(1, stopped_by_user=False)

    assert finish.outcome == "failed" and finish.show_error and not finish.apply_profile
    assert finish.log_text == "\nAuto-UV process stopped: exit_code=1\n"
    assert update.runs == [len(rows) - 1]
    assert (rows[-1].decision, rows[-1].row_state) == ("Failed", "error")
    assert session.stage == "Error" and session.status == "Auto-UV failed."
    assert session.tier_states == {
        "efficiency": "failed",
        "balanced": "not-run",
        "performance": "not-run",
    }


def test_resumed_scan_restores_saved_measurements_then_completes() -> None:
    stream = events(RESUME)
    session = full_scan()
    session.handle_event(stream[0])
    update = session.handle_event(stream[1])  # scan_resumed

    assert update.reset and update.restored
    assert session.stage == "Resuming" and session.status == RESTORED_STATUS
    assert session.candidate.endswith("(saved)")
    assert session.runs.rows and all(run.restored for run in session.runs.rows)
    assert session.tier_states["efficiency"] == "complete"
    assert all(
        "Saved measurement" in str(run.payload.get("reason", ""))
        for run in session.runs.rows
        if "decision" in run.payload
    )

    for payload in stream[2:]:
        session.handle_event(payload)
    assert set(session.tier_states.values()) == {"complete"}
    assert set(session.tier_curves) == {"efficiency", "balanced", "performance"}
    finish, _update = session.finish(0, stopped_by_user=False)

    assert finish.outcome == "complete" and finish.apply_profile
    assert finish.candidate_id == "987mv-1920mhz"
    assert session.stage == "Complete"
    assert session.status == "Final verification complete."
    assert session.final_result is None, "consumed by the finish"


def test_runs_follow_probe_progress_and_oc_against_the_baseline() -> None:
    session = full_scan()
    session.handle_event(
        {"event": "probe_result", "stage": "base-baseline", "voltage_mv": 1000,
         "clock_mhz": 2800, "measured_clock_mhz": 2730, "decision": "pass",
         "fps": 60.0, "power_w": 300.0, "efficiency_fps_per_w": 0.2}
    )
    session.handle_event(
        {"event": "candidate_curve", "stage": "candidate", "voltage_mv": 925,
         "clock_mhz": 2980, "points": [{"voltage_mv": 925, "clock_mhz": 2980}]}
    )
    start = session.handle_event(
        {"event": "probe_start", "stage": "candidate", "voltage_mv": 925,
         "clock_mhz": 2980, "elapsed_s": 0.0, "target_duration_s": 30}
    )
    run = session.runs.rows[start.runs[0]]
    assert run.oc == (250, 250) and run.decision == "Running"
    assert start.stage == "Undervolting Candidates Sweep" and start.probe_marker
    telemetry = session.handle_event(
        {"event": "load_telemetry", "voltage_mv": 925, "clock_mhz": 2980,
         "elapsed_s": 12, "perf_cap_reason": "sw-power"}
    )
    assert telemetry.runs == start.runs and telemetry.live_marker
    assert run.progress["elapsed_s"] == 12.0 and run.payload["perf_cap_reason"] == "sw-power"
    result = session.handle_event(
        {"event": "probe_result", "stage": "candidate", "voltage_mv": 925,
         "clock_mhz": 2980, "decision": "fail", "failure_kind": "low-clock",
         "failure_severity": "recoverable", "reason": "core clock fell below the floor",
         "fps": 58.0, "power_w": 280.0}
    )
    run = session.runs.rows[result.runs[0]]
    assert result.runs == start.runs
    assert (run.decision, run.row_state) == ("Failed", "warning")
    assert run.progress["label"] == "Clock too low"
    assert session.runs.delta_pct(run, "power_w") == pytest.approx(-6.6667, abs=1e-3)


def test_stop_marks_running_rows_and_ends_stopped() -> None:
    session = full_scan(CRASH)
    update = session.request_stop()
    assert update.stage == "Stopping" and update.status == "Stopping Auto-UV."
    assert session.runs.rows[-1].decision == "Stopping"

    finish, _update = session.finish(130, stopped_by_user=True)

    assert finish.outcome == "stopped" and not finish.show_error
    assert session.runs.rows[-1].decision == "Stopped"
    assert session.tier_states["efficiency"] == "stopped"


def test_discarded_and_aborted_choices_are_not_failures() -> None:
    session = full_scan(CRASH, limit=20)
    session.handle_event({"event": "final_choice_discarded", "reason": "user-discarded"})
    assert session.status == DISCARDED_STATUS and session.stage == "Discarded"
    finish, _update = session.finish(0, stopped_by_user=False)
    assert finish.outcome == "discarded" and not finish.apply_profile

    session = full_scan(CRASH, limit=20)
    session.record_final_choice(discarded=True, aborted=True, selected_candidate_id="")
    finish, _update = session.finish(1, stopped_by_user=False)
    assert finish.outcome == "aborted" and not finish.show_error
    assert session.stage == "Aborted"


def test_tier_events_word_the_status_like_the_classic_window() -> None:
    session = full_scan()
    update = session.handle_event(
        {"event": "tier_started", "tier": "balanced", "position": 2, "total": 3}
    )
    assert update.stage == "Balanced scan (2/3)"
    assert session.tier_states["efficiency"] == "complete"
    update = session.handle_event(
        {"event": "tier_skipped", "tier": "balanced", "next_tier": "performance"}
    )
    assert update.status == (
        "Balanced tier skipped: no stable candidate found. Continuing with Performance."
    )
    update = session.handle_event(
        {"event": "tier_descent_reused", "tier": "performance", "source_tier": "balanced",
         "voltage_mv": 950, "target_mhz": 1875}
    )
    assert update.status.startswith("Performance reuses the Balanced downsweep")


def test_human_lines_and_dependency_progress() -> None:
    session = full_scan()
    assert session.handle_human_line("Starting final verification").stage == (
        "Final verification"
    )
    update = session.handle_human_line("candidate 900mV @ 2700.123MHz")
    assert update.candidate == "candidate 900mV @ 2700.12MHz"
    update = session.handle_event(
        {"event": "dependency_progress", "percent": 140, "detail": "fetching"}
    )
    assert session.progress.text == "Downloading dependencies 100%"
    assert update.candidate == "" and session.progress.detail == "fetching"
    session.handle_event({"event": "probe_start", "voltage_mv": 900, "clock_mhz": 2700})
    assert session.progress is None


def test_pre_scan_silent_fan_keeps_a_running_fan_curve() -> None:
    assert pre_scan_silent_fan(False, {"silent_fan_curve": True}) is True
    assert pre_scan_silent_fan(True, None) is True
    assert pre_scan_silent_fan(False, None) is False


def test_profile_verification_texts() -> None:
    session = ScanSession()
    session.start_verification(label="Balanced", workload="Q2RTX", duration_s=300)
    assert session.stage == "Profile verification" and session.candidate == "Balanced"
    assert session.status == "Verifying Balanced with Q2RTX."
    assert session.progress.text == "Verifying profile 0s / 5min"
    assert verify_progress_text(400, 300) == "Verifying profile 5min / 5min"

    finish = session.finish_verification(0, stopped_by_user=False)
    assert finish.passed and not finish.show_error and session.candidate == ""
    assert session.progress.percent == 100
    finish = session.finish_verification(1, stopped_by_user=True)
    assert finish.stopped and not finish.show_error and session.stage == "Idle"
    finish = session.finish_verification(1, stopped_by_user=False)
    assert finish.show_error and session.stage == "Error" and session.progress is None


def test_classic_window_and_session_show_the_same_scan(qapp, monkeypatch) -> None:
    """The classic window renders the shared session: same header texts."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import ui.window as window_mod
    from ui.qt import import_qt

    monkeypatch.setattr(window_mod, "read_auto_uv_profile_summaries", lambda: [])
    monkeypatch.setattr(
        window_mod, "systemd_autostart_profile_info",
        lambda: {"selector": "", "silent_fan_curve": False},
    )
    monkeypatch.setattr(window_mod, "penguin_burner_runtime_is_active", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_from_runtime_config", lambda: False)
    monkeypatch.setattr(window_mod, "gpu_choices_with_fallback", lambda **_: ([], 0))
    window = window_mod.MainWindow(import_qt())
    window.scan_session.start({"auto_uv_mode": "adaptive"})
    window._show_scan_state()
    reference = full_scan()
    for payload in events(RESUME)[:30]:
        window._handle_scan_event(payload)
        reference.handle_event(payload)
        assert window.header.stage() == reference.stage
        assert window.header.candidate_label.text() == reference.candidate
        assert window.controls.status_label.fullText() == reference.status
    assert window.runs_table.widget.rowCount() == len(reference.runs.rows)
    for tier, state in reference.tier_states.items():
        assert window.auto_uv_tier_progress.state(tier) == state
    window.window.close()
