from __future__ import annotations

import pytest

from drivers.nvidia import daemon_gpu
from drivers.nvidia.daemon_gpu import DaemonGpuClient
from stability.q2rtx.telemetry import query_gpu_metrics


POINTS = [
    {
        "index": 12,
        "type": 0,
        "voltage_based": 1,
        "freq_khz": 2_800_000,
        "voltage_uv": 900_000,
        "base_freq_khz": 2_680_000,
        "base_voltage_uv": 900_000,
        "current_offset_khz": 120_000,
    },
    {
        "index": 13,
        "type": 1,
        "voltage_based": 0,
        "freq_khz": 2_900_000,
        "voltage_uv": 950_000,
        "base_freq_khz": 2_800_000,
        "base_voltage_uv": 950_000,
        "current_offset_khz": 100_000,
    },
]


def _capabilities(gpu_index: int = 0, *, gpu_count: int = 2) -> dict:
    return {
        "gpu_index": gpu_index,
        "gpu_count": gpu_count,
        "identity": {
            "index": gpu_index,
            "name": f"GPU {gpu_index}",
            "driver_version": "610.1",
            "pci_bus_id": f"00000000:0{gpu_index + 1}:00.0",
            "pci_device_id": "0x123410DE",
            "uuid": f"GPU-{gpu_index}",
        },
        "memory": {
            "index": gpu_index,
            "total_bytes": 8 * 1024**3,
            "free_bytes": 3 * 1024**3,
            "used_bytes": 5 * 1024**3,
        },
        "architecture": 10,
        "power_limits": {
            "power_management_enabled": True,
            "power_limit_w": 320,
            "enforced_power_limit_w": 310,
            "power_limit_default_w": 350,
            "power_limit_min_w": 200,
            "power_limit_max_w": 450,
        },
        "supported_memory_clock_steps_mhz": [10_500, 9_000, 10_500],
        "supported_core_clock_steps_mhz": [2_100, 1_800, 2_000, 1_900],
        "clock_offset_ranges_mhz": {"memory": [-1000, 3000]},
        "clock_offsets_mhz": {"gpc": 120, "memory": 500},
        "fan": {"count": 2, "min_speed_pct": 30, "max_speed_pct": 100},
        "features": {"vf_curve": True, "voltage": True},
        "vf_summary": {"active_points": 2, "editable_core_points": 1},
    }


def _telemetry(gpu_index: int = 0, *, voltage_uv: int = 912_000) -> dict:
    return {
        "gpu_index": gpu_index,
        "updated_unix_ns": 123,
        "gpu_utilization_pct": 98,
        "power_draw_w": 250.25,
        "clocks_mhz": {
            "graphics": 2_100,
            "sm": 2_055,
            "memory": 10_500,
            "video": 1_800,
        },
        "temperature_c": 61.5,
        "fan_speeds_pct": [40, 42],
        "voltage_uv": voltage_uv,
        "throttle_reason_mask": 0x4,
        "clock_offsets_mhz": {"gpc": 120, "memory": 500},
    }


def test_snapshot_is_typed_and_reuses_both_cached_daemon_reads(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        daemon_gpu,
        "gpu_capabilities",
        lambda index: calls.append(("capabilities", index)) or _capabilities(index),
    )
    monkeypatch.setattr(
        daemon_gpu,
        "gpu_telemetry",
        lambda index: calls.append(("telemetry", index)) or _telemetry(index),
    )

    client = DaemonGpuClient(1)
    snapshot = client.snapshot()

    assert snapshot.capabilities.identity.name == "GPU 1"
    assert snapshot.capabilities.memory is not None
    assert snapshot.capabilities.memory.total_bytes == 8 * 1024**3
    assert snapshot.capabilities.power.management_enabled is True
    assert snapshot.capabilities.power.current_w == 320.0
    assert snapshot.capabilities.power.enforced_w == 310.0
    assert snapshot.capabilities.power.default_w == 350.0
    assert snapshot.capabilities.power.minimum_w == 200.0
    assert snapshot.capabilities.power.maximum_w == 450.0
    assert snapshot.capabilities.supported_memory_clocks_mhz == (9000, 10500)
    assert snapshot.capabilities.memory_clock_offset_range_mhz == (-1000, 3000)
    assert snapshot.capabilities.clock_offsets.memory_mhz == 500
    assert snapshot.capabilities.fan.minimum_speed_pct == 30
    assert snapshot.capabilities.features.vf_curve is True
    assert snapshot.capabilities.vf.editable_core_points == 1
    assert snapshot.telemetry.clocks.graphics_mhz == 2100
    assert snapshot.telemetry.clocks.memory_mhz == 10500
    assert snapshot.telemetry.fan_speeds_pct == (40.0, 42.0)
    assert client.snapshot() is not snapshot
    assert calls == [("capabilities", 1), ("telemetry", 1)]

    refreshed = client.snapshot(refresh=True)
    assert refreshed.capabilities is snapshot.capabilities
    assert calls[-1] == ("telemetry", 1)
    assert len(calls) == 3

    monkeypatch.setattr(
        daemon_gpu,
        "gpu_apply_power_limit",
        lambda _index, watts: {"applied_w": watts},
    )
    client.apply_power_limit_w(300)
    client.capabilities()
    assert calls[-1] == ("capabilities", 1)


def test_discovery_fetches_each_gpu_capability_once(monkeypatch) -> None:
    requested: list[int] = []
    monkeypatch.setattr(
        daemon_gpu,
        "gpu_capabilities",
        lambda index: requested.append(index) or _capabilities(index),
    )

    identities = DaemonGpuClient.discover_identities()

    assert [item.index for item in identities] == [0, 1]
    assert requested == [0, 1]


def test_vf_points_are_cached_and_invalidated_after_write(monkeypatch) -> None:
    snapshots: list[int] = []
    applied: list[tuple[int, list[tuple[int, int]]]] = []
    monkeypatch.setattr(
        daemon_gpu,
        "gpu_vf_snapshot",
        lambda index: snapshots.append(index) or {"points": POINTS},
    )
    monkeypatch.setattr(
        daemon_gpu,
        "gpu_apply_vf_offsets",
        lambda index, offsets: applied.append((index, list(offsets))),
    )

    client = DaemonGpuClient(2)
    assert client.editable_core_points()[0]["index"] == 12
    assert snapshots == [2]

    client.apply_offsets_khz([(12, 130_000)])
    assert applied == [(2, [(12, 130_000)])]
    assert client.editable_core_points()[0]["current_offset_khz"] == 120_000
    assert snapshots == [2, 2]


def test_power_limit_support_uses_driver_setter_probe(monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        daemon_gpu,
        "probe_power_limit_support",
        lambda index: calls.append(int(index)) or {"supported": False},
    )

    assert DaemonGpuClient(2).power_limit_set_supported() is False
    assert calls == [2]


def test_deferred_power_support_is_not_reported_as_unsupported(monkeypatch) -> None:
    monkeypatch.setattr(daemon_gpu, "probe_power_limit_support", lambda _: {
        "supported": False, "reason": "auto-uv-scan-running",
    })
    with pytest.raises(RuntimeError, match="deferred"):
        DaemonGpuClient(2).power_limit_set_supported()


def test_voltage_preserves_implausible_raw_sample(monkeypatch) -> None:
    samples = iter((912_000, 0))
    monkeypatch.setattr(
        daemon_gpu,
        "gpu_telemetry",
        lambda index: _telemetry(index, voltage_uv=next(samples)),
    )

    client = DaemonGpuClient(0)
    assert client.read_microvolts() == 912_000
    assert client.read_microvolts() is None
    assert client.last_raw_microvolts() == 0


def test_perf_cap_reason_formats_known_and_unknown_bits() -> None:
    assert daemon_gpu.format_perf_cap_reason_mask(0) == "none"
    assert (
        daemon_gpu.format_perf_cap_reason_mask(0x4 | 0x80 | 0x8000)
        == "sw-power+hw-power-brake+unknown-0x8000"
    )


def test_q2rtx_sample_uses_one_daemon_telemetry_snapshot(monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        daemon_gpu,
        "gpu_telemetry",
        lambda index: calls.append(index) or _telemetry(index),
    )

    sample = query_gpu_metrics(1, gpu_client=DaemonGpuClient(1))

    assert sample is not None
    assert sample.gpu_util_pct == 98.0
    assert sample.power_w == 250.25
    assert sample.core_clock_mhz == 2100
    assert sample.temperature_c == 61.5
    assert sample.fan_speed_pct == 40.0
    assert sample.voltage_mv == 912.0
    assert sample.perf_cap_reason == "sw-power"
    assert calls == [1]
