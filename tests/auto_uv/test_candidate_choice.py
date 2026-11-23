"""Backend final-candidate choice: ranking, request payloads and the UI answer."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

from auto_uv.final_verification import candidate_choice
from auto_uv.final_verification.candidate_choice import (
    candidate_selection_summary as _candidate_selection_summary,
)
from auto_uv.final_verification.candidate_choice import (
    sorted_final_choice_candidates as _sorted_backend_final_choice_candidates,
)
from auto_uv.run.crash_recovery import probe_summary_from_candidate_record


def test_backend_final_choice_performance_mode_sorts_by_fps() -> None:
    candidates = [
        {
            "candidate_id": "fast",
            "candidate_voltage_mv": 930,
            "lock_clock_mhz": 2760,
            "avg_fps": 160.0,
            "efficiency_fps_per_w": 0.65,
        },
        {
            "candidate_id": "efficient",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2700,
            "avg_fps": 150.0,
            "efficiency_fps_per_w": 0.75,
        },
    ]

    sorted_candidates, sort_label = _sorted_backend_final_choice_candidates(
        candidates,
        auto_uv_mode="performance",
        base_probe=None,
    )

    assert sort_label == "fps"
    assert [candidate["candidate_id"] for candidate in sorted_candidates] == [
        "fast",
        "efficient",
    ]


def test_backend_final_choice_efficiency_mode_sorts_by_fps_per_w() -> None:
    candidates = [
        {
            "candidate_id": "efficient",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2700,
            "avg_fps": 150.0,
            "efficiency_fps_per_w": 0.75,
        },
        {
            "candidate_id": "fast",
            "candidate_voltage_mv": 930,
            "lock_clock_mhz": 2760,
            "avg_fps": 160.0,
            "efficiency_fps_per_w": 0.65,
        },
    ]

    sorted_candidates, sort_label = _sorted_backend_final_choice_candidates(
        candidates,
        auto_uv_mode="efficiency",
        base_probe=None,
    )

    assert sort_label == "fps-per-w"
    assert [candidate["candidate_id"] for candidate in sorted_candidates] == [
        "efficient",
        "fast",
    ]


def _final_choice_base_curve() -> list[dict]:
    return [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": 2200 + index * 90,
            "target_mhz": 2200 + index * 90,
            "new_offset_mhz": 0,
        }
        for index, voltage_mv in enumerate(range(850, 1025, 25))
    ]


def _final_choice_probe(voltage_mv: int, clock_mhz: int, *, fps: float, fpsw: float):
    return probe_summary_from_candidate_record(
        {
            "candidate_voltage_mv": int(voltage_mv),
            "lock_clock_mhz": int(clock_mhz),
            "avg_core_clock_mhz": float(clock_mhz),
            "avg_fps": float(fps),
            "efficiency_fps_per_w": float(fpsw),
        }
    )


def _run_backend_final_choice(tmp_path, monkeypatch, *, request_reason: str):
    request_path = tmp_path / "final-choice-request.json"
    response_path = tmp_path / "final-choice-response.json"
    monkeypatch.setattr(
        candidate_choice, "final_choice_request_path", lambda: request_path
    )
    monkeypatch.setattr(
        candidate_choice, "final_choice_response_path", lambda: response_path
    )
    # An empty UI response means the backend keeps the request's default.
    monkeypatch.setattr(
        candidate_choice, "wait_for_final_choice_response", lambda _path: {}
    )
    curve = _final_choice_base_curve()
    tier_probe = _final_choice_probe(850, 2430, fps=120.0, fpsw=0.60)
    history = [_final_choice_probe(900, 2700, fps=150.0, fpsw=0.75), tier_probe]

    (
        _plan,
        selected_voltage_mv,
        selected_lock_clock_mhz,
        _probe,
        _duration_s,
    ) = candidate_choice.choose_final_verification_candidate(
        log=lambda _message: None,
        event_callback=None,
        auto_uv_mode="efficiency",
        base_probe=_final_choice_probe(1000, 3150, fps=150.0, fpsw=0.5),
        stable_plan=curve,
        stable_voltage_mv=850,
        stable_lock_clock_mhz=2430,
        stable_probe=tier_probe,
        stable_history=history,
        base_curve=curve,
        final_verification_duration_s=300,
        initial_target_voltage_mv=1000,
        short_probe_base_duration_s=10,
        request_reason=request_reason,
    )
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    return payload, (int(selected_voltage_mv), int(selected_lock_clock_mhz))


def test_backend_adaptive_final_choice_defaults_to_tier_candidate(
    tmp_path, monkeypatch
) -> None:
    payload, selected = _run_backend_final_choice(
        tmp_path,
        monkeypatch,
        request_reason="adaptive-efficiency",
    )

    assert payload["request_reason"] == "adaptive-efficiency"
    assert payload["default_candidate_id"] == "850mv-2430mhz"
    assert selected == (850, 2430)


def test_backend_classic_final_choice_defaults_to_metric_best(
    tmp_path, monkeypatch
) -> None:
    payload, selected = _run_backend_final_choice(
        tmp_path,
        monkeypatch,
        request_reason="sweep-complete",
    )

    assert payload["default_candidate_id"] == "900mv-2700mhz"
    assert selected == (900, 2700)


def test_backend_final_choice_summary_includes_baseline_delta_metrics() -> None:
    summary = _candidate_selection_summary(
        {
            "candidate_id": "efficient",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2700,
            "avg_fps": 160.0,
            "efficiency_fps_per_w": 0.75,
        },
        base_probe=SimpleNamespace(
            avg_fps=150.0,
            avg_power_w=300.0,
            efficiency_fps_per_w=0.50,
        ),
    )

    assert summary["base_avg_fps"] == 150.0
    assert summary["base_avg_power_w"] == 300.0
    assert summary["base_efficiency_fps_per_w"] == 0.50


def test_backend_final_choice_summary_includes_core_oc_above_baseline() -> None:
    summary = _candidate_selection_summary(
        {
            "candidate_id": "resume",
            "candidate_voltage_mv": 885,
            "lock_clock_mhz": 2910,
            "avg_fps": 160.0,
            "efficiency_fps_per_w": 0.75,
        },
        base_probe=SimpleNamespace(
            avg_fps=150.0,
            avg_core_clock_mhz=2700.0,
            efficiency_fps_per_w=0.50,
        ),
    )

    assert summary["base_avg_core_clock_mhz"] == 2700.0
    assert summary["core_oc_mhz"] == 210


def test_final_choice_response_waits_out_a_half_written_answer(tmp_path) -> None:
    """A partial read must not become the default candidate: the reader
    re-reads until the answer parses (here a Discard)."""
    response_path = tmp_path / "final-choice-response.json"
    response_path.write_text('{"action": "dis', encoding="utf-8")

    def finish_writing() -> None:
        time.sleep(0.2)
        response_path.write_text('{"action": "discard"}', encoding="utf-8")

    writer = threading.Thread(target=finish_writing)
    writer.start()
    try:
        response = candidate_choice.wait_for_final_choice_response(response_path)
    finally:
        writer.join()
    assert response == {"action": "discard"}
    assert candidate_choice.final_choice_discarded(response)


def test_final_choice_response_that_never_parses_is_empty(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(candidate_choice, "FINAL_CHOICE_READ_RETRY_S", 0.1)
    response_path = tmp_path / "final-choice-response.json"
    response_path.write_text("not json", encoding="utf-8")
    assert candidate_choice.wait_for_final_choice_response(response_path) == {}
