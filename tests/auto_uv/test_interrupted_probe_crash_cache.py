from __future__ import annotations

import json

import pytest

from auto_uv.persistence import interrupted_probe_crash_cache as crash_cache
from auto_uv.persistence import unsafe_voltage_blacklist_file as blacklist_file
from auto_uv.persistence.auto_uv_persisted_json_files import probe_in_progress_path


def test_validation_accepts_interrupted_candidate() -> None:
    validation = crash_cache.interrupted_marker_crash_cache_validation(
        {
            "state": "probing",
            "phase": "candidate",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 1919,
            "details": {
                "start_voltage_mv": 987,
                "initial_probe_clock_mhz": 1933.29,
            },
        }
    )

    assert validation["accepted"] is True
    assert validation["reason"] == "interrupted candidate probe"
    assert validation["candidate_voltage_mv"] == 875
    assert validation["lock_clock_mhz"] == 1919


def test_validation_accepts_interrupted_candidate_below_baseline_clock() -> None:
    validation = crash_cache.interrupted_marker_crash_cache_validation(
        {
            "state": "probing",
            "phase": "candidate",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 1700,
            "details": {
                "start_voltage_mv": 987,
                "initial_probe_clock_mhz": 1933.29,
            },
        }
    )

    assert validation["accepted"] is True
    assert validation["lock_clock_mhz"] == 1700


def test_validation_accepts_final_verify_without_oc_budget() -> None:
    validation = crash_cache.interrupted_marker_crash_cache_validation(
        {
            "state": "probing",
            "phase": "final-verify",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 1919,
            "details": {
                "start_voltage_mv": 987,
                "initial_probe_clock_mhz": 1933.29,
            },
        }
    )

    assert validation["accepted"] is True
    assert validation["reason"] == "interrupted candidate probe"


def test_consume_records_interrupted_candidate_and_clock_bindings(
    tmp_path,
    monkeypatch,
) -> None:
    marker_path = tmp_path / "auto-uv-probe-in-progress.json"
    unsafe_path = tmp_path / "auto-uv-unsafe-voltages.json"
    marker_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "state": "probing",
                "phase": "candidate",
                "candidate_voltage_mv": 875,
                "lock_clock_mhz": 1919,
                "pid": 1234,
                "host": "test-host",
                "started_at": "2026-05-10T06:57:12+02:00",
                "log_context": "lower-voltage 875mV",
                "details": {
                    "start_voltage_mv": 987,
                    "initial_probe_clock_mhz": 1933.29,
                    "target_clock_pct_of_baseline": 99.2603,
                    "blocked_lock_clock_mhz": [1919, 1905],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(crash_cache, "probe_in_progress_path", lambda: marker_path)
    monkeypatch.setattr(
        blacklist_file,
        "unsafe_voltage_blacklist_path",
        lambda: unsafe_path,
    )

    consumed = crash_cache.consume_interrupted_probe_crash_marker()

    assert consumed is not None
    path, entry = consumed
    assert path == unsafe_path
    assert marker_path.exists() is False
    assert entry["reason"] == "previous-run-abruptly-ended"
    assert entry["candidate_voltage_mv"] == 875
    assert entry["lock_clock_mhz"] == 1919
    assert entry["blocked_lock_clock_mhz"] == [1919, 1905]
    assert (
        entry["details"]["crash_cache_validation"]["reason"]
        == "interrupted candidate probe"
    )


@pytest.mark.parametrize("phase", ["candidate", "final-verify"])
@pytest.mark.parametrize(
    "flag,voltage,clock", [("custom_target", 850, 2380), ("auto_oc", 925, 3098)]
)
def test_explicit_target_crash_does_not_require_old_voltage_or_clock_margin(
    phase, flag, voltage, clock
):
    marker = {
        "state": "probing",
        "phase": phase,
        "candidate_voltage_mv": voltage,
        "lock_clock_mhz": clock,
        "details": {
            flag: True,
            "used_companion_load": True,
            "start_voltage_mv": voltage,
            "initial_probe_clock_mhz": 2800,
        },
    }
    assert (
        crash_cache.interrupted_marker_crash_cache_validation(marker)["accepted"]
        is True
    )
    marker["phase"] = "baseline"
    assert (
        crash_cache.interrupted_marker_crash_cache_validation(marker)["accepted"]
        is False
    )


def test_crash_marker_survives_failed_blacklist_write(tmp_path, monkeypatch):
    path = tmp_path / "marker.json"
    path.write_text(
        json.dumps(
            {
                "state": "probing",
                "phase": "candidate",
                "candidate_voltage_mv": 925,
                "lock_clock_mhz": 3098,
                "details": {"start_voltage_mv": 1025, "initial_probe_clock_mhz": 2740},
            }
        )
    )
    monkeypatch.setattr(crash_cache, "probe_in_progress_path", lambda: path)

    def fail_write(**_kwargs):
        raise OSError("simulated disk write failure")

    monkeypatch.setattr(crash_cache, "record_unsafe_voltage", fail_write)
    with pytest.raises(OSError, match="disk write failure"):
        crash_cache.consume_interrupted_probe_crash_marker()
    assert path.exists()


@pytest.mark.parametrize("field", ["candidate_voltage_mv", "lock_clock_mhz"])
@pytest.mark.parametrize("invalid", [None, 0, -1, True, "bad", float("inf"), float("nan"), 970.5])
def test_consume_discards_invalid_candidate_identifiers(field, invalid) -> None:
    path = probe_in_progress_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = {"state": "probing", "phase": "efficiency-candidate",
              "candidate_voltage_mv": 970, "lock_clock_mhz": 2400, field: invalid}
    path.write_text(json.dumps(marker))

    assert crash_cache.consume_interrupted_probe_crash_marker() is None
    assert not path.exists()
    assert blacklist_file.load_unsafe_voltage_blacklist() == []


@pytest.mark.parametrize("state,phase", [
    ("completed", "candidate"), (None, "candidate"),
    ("probing", "baseline"), ("probing", "base-baseline"),
    ("probing", "unknown-candidate"),
])
def test_validation_rejects_non_probing_or_unrecognized_markers(state, phase) -> None:
    marker = {"state": state, "phase": phase,
              "candidate_voltage_mv": 970, "lock_clock_mhz": 2400}
    assert not crash_cache.interrupted_marker_crash_cache_validation(marker)["accepted"]
