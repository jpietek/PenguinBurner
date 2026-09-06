"""Focused coverage tests for small pure-logic modules.

Targets previously-uncovered branches in:
- auto_uv/shared/probe_data_fields.py
- auto_uv/probes/event_payload.py
- auto_uv/run/cli_runtime.py
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from auto_uv.run.cli_runtime import (
    AutoUvForegroundDependencies,
    run_auto_uv_voltage_scan,
)
from auto_uv.shared.probe_data_fields import numeric_values, percent, read_field
from auto_uv.probes.event_payload import probe_summary_event_payload
from common.penguin_burner_errors import NvmlError


# ---------------------------------------------------------------------------
# probe_data_fields.py
# ---------------------------------------------------------------------------


def test_numeric_values_skips_none_fields() -> None:
    """Line 22: records missing the field are skipped, present ones coerced."""
    records = [
        {"x": 1},
        {"x": None},
        {},  # field absent -> read_field returns None -> skipped
        {"x": "3.5"},
    ]
    assert numeric_values(records, "x") == [1.0, 3.5]


def test_read_field_dict_and_object_and_percent() -> None:
    assert read_field({"a": 7}, "a") == 7
    assert read_field(SimpleNamespace(a=9), "a") == 9
    assert read_field(SimpleNamespace(a=9), "missing") is None
    assert percent(50) == 0.5
    assert percent(-10) == 0.0


# ---------------------------------------------------------------------------
# event_payload.py
# ---------------------------------------------------------------------------


def test_probe_summary_event_payload_reads_dict_probe() -> None:
    """Line 55: dict-backed probe goes through the dict.get() branch."""
    probe = {
        "candidate_voltage_mv": 925,
        "lock_clock_mhz": 2700,
        "avg_core_clock_mhz": 2695.123,
        "avg_voltage_mv": 924.0,
        "loaded_qualified_sample_count": 12,
        "used_companion_load": True,
        "avg_fps": 142.4,
        "perf_cap_reason": None,
        "log_path": "/tmp/run.log",
    }
    payload = probe_summary_event_payload(probe, stage="probe", decision="keep")

    assert payload["voltage_mv"] == 925
    assert payload["clock_mhz"] == 2700
    assert payload["measured_clock_mhz"] == 2695.12
    assert payload["loaded_qualified_sample_count"] == 12
    assert payload["used_companion_load"] is True
    assert payload["fps"] == 142.4
    assert payload["perf_cap_reason"] == ""
    assert payload["log_path"] == "/tmp/run.log"
    assert payload["stage"] == "probe"
    assert payload["decision"] == "keep"


# ---------------------------------------------------------------------------
# cli_runtime.py
# ---------------------------------------------------------------------------


def test_voltage_scan_wraps_initial_check_runtime_error_as_nvml_error() -> None:
    """Lines 107-108: a RuntimeError from the initial check becomes NvmlError."""

    def failing_initial_check(**_kwargs):
        raise RuntimeError("initial check unavailable")

    args = SimpleNamespace(json_events=False)
    with pytest.raises(NvmlError, match="initial check unavailable"):
        run_auto_uv_voltage_scan(
            args,
            gpu_index=0,
            config_path="/tmp/config.toml",
            auto_uv_runtime_options={},
            dependencies=AutoUvForegroundDependencies(
                require_auto_uv_initial_check=failing_initial_check,
                log=lambda *_a, **_k: None,
                emit_json_event=lambda *_a, **_k: None,
            ),
        )
