from __future__ import annotations

import pytest

from auto_uv.probes.voltage_probe import (
    probe_crash_marker_details,
    probe_unsafe_details,
)


def test_probe_crash_marker_details_describe_normal_candidate_context() -> None:
    details = probe_crash_marker_details(
        phase_label="candidate",
        candidate_voltage_mv=875,
        lock_clock_mhz=1919,
        stable_history=[],
        initial_target_voltage_mv=987,
        initial_probe_clock_mhz=1933.29,
        used_companion_load=True,
        expected_total_duration_s=None,
        marker_details={"custom": "kept"},
    )

    assert details["phase_label"] == "candidate"
    assert details["candidate_voltage_mv"] == 875
    assert details["lock_clock_mhz"] == 1919
    assert details["start_voltage_mv"] == 987
    assert details["voltage_drop_from_start_pct"] == pytest.approx(11.3475)
    assert details["target_clock_pct_of_baseline"] == pytest.approx(99.2608)
    assert details["used_companion_load"] is True
    assert details["custom"] == "kept"


def test_probe_unsafe_details_keep_marker_tier_context() -> None:
    details = probe_unsafe_details(
        {
            "generated_profile_tier": "performance",
            "tail_rise_bins": 6,
            "phase_label": "candidate",
        },
        {
            "result_reason": "fatal-q2rtx-output",
            "phase_label": "candidate",
        },
    )

    assert details["generated_profile_tier"] == "performance"
    assert details["tail_rise_bins"] == 6
    assert details["result_reason"] == "fatal-q2rtx-output"
