from __future__ import annotations

import pytest

from auto_uv.initial_check.auto_uv_hardware_initial_check import _validate_vf_curve
from drivers.nvidia.hidden_nvapi_vf import (
    ClockClientClkVfPointsControlV1,
    ClockClientClkVfPointsInfoV1,
    ClockClientClkVfPointsStatusV3,
    HiddenNvapiVfCurveReader,
)


def make_reader(monkeypatch, *, supported, base_freq, base_voltage, freq=900_000):
    info = ClockClientClkVfPointsInfoV1()
    status = ClockClientClkVfPointsStatusV3()
    control = ClockClientClkVfPointsControlV1()
    info.vf_points_mask[0] = 0xFF
    status.b_vf_tuple_base_supported = supported
    for index in range(8):
        info.vf_points[index].b_voltage_based = 1
        status.vf_points[index].freq_khz = freq
        status.vf_points[index].voltage_uv = 625_000 + index * 12_500
        status.vf_points[index].vf_tuple_base.freq_khz = base_freq
        status.vf_points[index].vf_tuple_base.voltage_uv = base_voltage
        control.vf_points[index].prog.freq_offset_khz = -15_000
    reader = object.__new__(HiddenNvapiVfCurveReader)
    reader._initialized = True
    reader._gpu = 1
    monkeypatch.setattr(reader, "get_info_struct", lambda: info)
    monkeypatch.setattr(reader, "get_status_struct", lambda mask: status)
    monkeypatch.setattr(reader, "get_control_struct", lambda mask: control)
    reader.refresh_points()
    return reader


@pytest.mark.parametrize("supported", [0, 1])
@pytest.mark.parametrize(
    ("base_freq", "base_voltage", "expected"),
    [
        (0, 0, (900_000, 625_000)),
        (915_000, 630_000, (915_000, 630_000)),
        (915_000, 0, (915_000, 625_000)),
        (0, 630_000, (900_000, 630_000)),
    ],
)
def test_base_tuple_fallback(monkeypatch, supported, base_freq, base_voltage, expected):
    reader = make_reader(
        monkeypatch, supported=supported, base_freq=base_freq, base_voltage=base_voltage
    )
    point = reader.editable_core_points()[0]
    assert (point["base_freq_khz"], point["base_voltage_uv"]) == expected
    assert point["freq_khz"] == 900_000
    assert point["voltage_uv"] == 625_000
    assert point["current_offset_khz"] == -15_000
    assert _validate_vf_curve(reader) == []


def test_missing_base_does_not_hide_invalid_reported_frequency(monkeypatch):
    reader = make_reader(monkeypatch, supported=0, base_freq=0, base_voltage=0, freq=0)
    assert reader.editable_core_points()[0]["base_freq_khz"] == 0
    assert _validate_vf_curve(reader)
