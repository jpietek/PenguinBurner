"""Milestone B2 transport tests: Python GPU writes routed through the Rust daemon.

Each re-pointed ``drivers/nvidia`` write function is driven against the REAL
built ``penguin-burnerd`` binary running with the B1 mock-GPU seam
(``PENGUIN_BURNERD_TEST_MOCK_GPU=1``) on a temp socket. The daemon echoes the
backend ops it executed (``mock_ops``) in each result, so the tests prove the
transport swap ships byte-identical writes: same indices, same kHz/MHz/W units,
same snap flags -- and that driver error text is relayed verbatim as the
RuntimeError message (the sweep's initial check pattern-matches those strings).

Skipped module-wide when the binary is not built (``cargo test`` or
``cargo build`` in ``burnerd/`` makes them run) -- same convention as
``test_burnerd_golden.py``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import pytest

import runtime.daemon_client as daemon_client
from drivers.nvidia.daemon_gpu import DaemonGpuClient
from drivers.nvidia.hidden_nvapi_vf import (
    ClockClientClkVfPointsControlV1,
    HiddenNvapiVfCurveReader,
)
from runtime.support.vf_curve_plan import apply_plan


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _find_binary() -> Path | None:
    for rel in (
        "burnerd/target/debug/penguin-burnerd",
        "burnerd/target/release/penguin-burnerd",
    ):
        candidate = _REPO_ROOT / rel
        if candidate.exists():
            return candidate
    return None


BINARY = _find_binary()

pytestmark = pytest.mark.skipif(
    BINARY is None,
    reason="penguin-burnerd binary not built (run `cargo build --release` in burnerd/)",
)


# Same stub contract as test_burnerd_golden.py: record argv, print two lines,
# then exit or loop (SIGINT exits) depending on env forwarded by the daemon.
_STUB = r"""import os, sys, signal, time

argv_file = os.environ.get("SCAN_STUB_ARGV_FILE")
if argv_file:
    import json
    with open(argv_file, "w") as handle:
        handle.write(json.dumps(sys.argv[1:]))

sys.stdout.write('{"event":"verify_start"}\n')
sys.stdout.write('elapsed=1.0s\n')
sys.stdout.flush()

if os.environ.get("SCAN_STUB_EXIT_AFTER_LINES") == "1":
    sys.exit(0)

signal.signal(signal.SIGINT, lambda *_: os._exit(0))
initial_ppid = os.getppid()
while True:
    if os.getppid() != initial_ppid:
        os._exit(0)
    time.sleep(0.05)
"""


@dataclass
class DaemonHandle:
    proc: "subprocess.Popen[bytes]"
    socket_path: Path
    home: Path
    log: BinaryIO

    def verify_stop_request_file(self) -> Path:
        return self.home / ".config" / "PenguinBurner" / "profile-verify-stop-requested"


def _wait_for_socket(handle: DaemonHandle) -> None:
    for _ in range(250):
        if handle.socket_path.exists():
            return
        if handle.proc.poll() is not None:
            log_text = Path(handle.log.name).read_text(
                encoding="utf-8", errors="replace"
            )
            raise AssertionError(
                f"daemon exited early (rc={handle.proc.returncode}):\n{log_text}"
            )
        time.sleep(0.02)
    raise AssertionError(f"socket was not created: {handle.socket_path}")


@pytest.fixture
def make_daemon(tmp_path, monkeypatch, write_desktop_rtd3_tree):
    """Start a fresh Rust daemon with the mock-GPU RPC seam enabled.

    Points ``PENGUIN_BURNER_DAEMON_SOCKET`` at the daemon's temp socket, so the
    re-pointed driver write functions transparently reach this daemon.
    """
    handles: list[DaemonHandle] = []
    counter = {"n": 0}

    def _make(
        *,
        mock_fail: str = "",
        exit_after_lines: bool = False,
        argv_file: Path | None = None,
    ) -> DaemonHandle:
        counter["n"] += 1
        base = tmp_path / f"daemon-{counter['n']}"
        base.mkdir()
        home = base / "home"
        home.mkdir()
        socket_path = base / "sock"
        stub = base / "verify_stub.py"
        stub.write_text(_STUB, encoding="utf-8")
        rtd3_sys, rtd3_proc = write_desktop_rtd3_tree(base)

        env = os.environ.copy()
        env["PENGUIN_BURNERD_TEST_MOCK_GPU"] = "1"
        env["PENGUIN_BURNER_DAEMON_PROGRAM_FILE"] = str(stub)
        env["PENGUIN_BURNER_HOME"] = str(home)
        env["PENGUIN_BURNERD_TEST_STATE_FILE"] = str(base / "state.json")
        env["PENGUIN_BURNERD_TEST_INERT_ENGINE"] = "1"
        env["PENGUIN_BURNERD_TEST_TIMINGS"] = "1"
        env["PENGUIN_BURNERD_TEST_RTD3_SYSFS"] = str(rtd3_sys)
        env["PENGUIN_BURNERD_TEST_RTD3_PROC"] = str(rtd3_proc)
        env.pop("PENGUIN_BURNER_DAEMON_ALLOWED_UID", None)
        env.pop("NOTIFY_SOCKET", None)
        if mock_fail:
            env["PENGUIN_BURNERD_TEST_MOCK_GPU_FAIL"] = mock_fail
        if exit_after_lines:
            env["SCAN_STUB_EXIT_AFTER_LINES"] = "1"
        if argv_file is not None:
            env["SCAN_STUB_ARGV_FILE"] = str(argv_file)

        log = open(base / "daemon.log", "wb")
        proc = subprocess.Popen(
            [str(BINARY), "--socket", str(socket_path)],
            env=env,
            stdout=log,
            stderr=log,
        )
        handle = DaemonHandle(proc, socket_path, home, log)
        handles.append(handle)
        _wait_for_socket(handle)
        # Route the drivers' daemon-client wrappers at this daemon.
        monkeypatch.setenv(daemon_client.DAEMON_SOCKET_ENV, str(socket_path))
        return handle

    yield _make

    for handle in handles:
        if handle.proc.poll() is None:
            handle.proc.send_signal(signal.SIGTERM)
            try:
                handle.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                handle.proc.kill()
                handle.proc.wait(timeout=5)
        try:
            handle.log.close()
        except OSError:
            pass


@pytest.fixture
def rpc_spy(monkeypatch):
    """Record every (request, result) round-trip while still using the REAL
    socket transport -- the daemon's echoed ``mock_ops`` prove which backend
    ops it executed for each write."""
    calls: list[tuple[dict, dict]] = []
    real = daemon_client.daemon_payload_request

    def spy(request, **kwargs):
        result = real(request, **kwargs)
        calls.append((request, result))
        return result

    monkeypatch.setattr(daemon_client, "daemon_payload_request", spy)
    return calls


def _policy_controller(gpu_index: int = 0) -> DaemonGpuClient:
    """The client is lazy, so write-only tests require no setup read."""
    return DaemonGpuClient(gpu_index)


def _vf_reader(gpu_index: int = 0) -> HiddenNvapiVfCurveReader:
    reader = HiddenNvapiVfCurveReader.__new__(HiddenNvapiVfCurveReader)
    reader._gpu_index = int(gpu_index)
    return reader


def _control_with_offsets(
    offsets_khz: dict[int, int],
) -> ClockClientClkVfPointsControlV1:
    """A control struct with exactly ``offsets_khz``'s indices masked and their
    ``freq_offset_khz`` values set (what a GET+mutate would produce)."""
    control = ClockClientClkVfPointsControlV1()
    for index, offset_khz in offsets_khz.items():
        control.vf_points_mask[index // 32] |= 1 << (index % 32)
        control.vf_points[index].prog.freq_offset_khz = offset_khz
    return control


# --- DaemonGpuClient writes --------------------------------------------------


def test_typed_gpu_read_snapshots_route_through_daemon(make_daemon, rpc_spy):
    make_daemon()

    status = daemon_client.require_daemon_capabilities(
        "gpu-capabilities-v1",
        "gpu-telemetry-v1",
        "gpu-vf-snapshot-v1",
    )
    capabilities = daemon_client.gpu_capabilities(0)
    telemetry = daemon_client.gpu_telemetry(0)
    vf_snapshot = daemon_client.gpu_vf_snapshot(0)

    assert status["protocol_major"] == 2
    assert capabilities["identity"]["uuid"] == "GPU-mock-0"
    assert capabilities["gpu_count"] == 1
    assert capabilities["supported_core_clock_steps_mhz"] == [1800, 1900, 2000, 2100]
    assert telemetry["temperature_c"] == 55.0
    assert telemetry["fan_speeds_pct"] == [42, 43]
    assert telemetry["voltage_mv"] == 900
    assert vf_snapshot["points"] == [
        {
            "index": 12,
            "type": 0,
            "voltage_based": 1,
            "freq_khz": 2_800_000,
            "voltage_uv": 900_000,
            "base_freq_khz": 2_680_000,
            "base_voltage_uv": 900_000,
            "current_offset_khz": 120_000,
        }
    ]
    assert [request["method"] for request, _result in rpc_spy] == [
        "status",
        "gpu_capabilities",
        "gpu_telemetry",
        "gpu_vf_snapshot",
    ]


def test_daemon_capability_gate_rejects_missing_feature(make_daemon):
    make_daemon()
    with pytest.raises(
        daemon_client.DaemonCompatibilityError,
        match="missing required capabilities: future-feature-v9",
    ):
        daemon_client.require_daemon_capabilities("future-feature-v9")


def test_apply_power_limit_routes_watts_through_daemon(make_daemon, rpc_spy):
    make_daemon()
    controller = _policy_controller(gpu_index=0)

    assert controller.apply_power_limit_w(300) == 300

    request, result = rpc_spy[-1]
    assert request == {
        "method": "gpu_apply_power_limit",
        "gpu_index": 0,
        "power_limit_w": 300,
    }
    assert result["applied_w"] == 300
    assert result["mock_ops"] == ["ApplyPowerLimit { power_limit_w: 300 }"]


def test_stock_reset_does_not_report_success_when_curve_readback_is_stale(make_daemon):
    make_daemon()
    # The transport mock deliberately keeps its fixed +120 MHz V/F readback
    # after writes. A successful power setter alone must not certify this reset.
    with pytest.raises(RuntimeError, match=r"stock GPU reset incomplete: V/F point 12 read back \+120000 kHz"):
        daemon_client.gpu_reset_defaults(0)


def test_probe_power_limit_support_routes_through_rust_daemon(make_daemon, rpc_spy):
    make_daemon()

    result = daemon_client.probe_power_limit_support(0)

    request, response = rpc_spy[-1]
    assert request == {"method": "probe_power_limit_support", "gpu_index": 0}
    assert result["supported"] is True
    assert result["probe_power_limit_w"] == 300
    assert response["mock_ops"] == ["ApplyPowerLimit { power_limit_w: 300 }"]


def test_enable_persistence_mode_routes_through_daemon(make_daemon, rpc_spy):
    make_daemon()
    controller = _policy_controller()

    assert controller.enable_persistence_mode() is True

    request, result = rpc_spy[-1]
    assert request == {"method": "gpu_enable_persistence_mode", "gpu_index": 0}
    assert result["mock_ops"] == ["EnablePersistence"]


def test_apply_locked_core_clock_returns_legacy_snap_dict(make_daemon, rpc_spy):
    make_daemon()
    controller = _policy_controller()

    snap = controller.apply_locked_core_clock_mhz(1950)

    # The daemon snaps on its supported steps (mock: 1800/1900/2000/2100) with
    # the identical floor-preference logic; the return is the exact legacy dict
    # (keys, order, no wire extras like mock_ops).
    assert snap == {
        "requested_clock_mhz": 1950,
        "applied_clock_mhz": 1900,
        "mode": "floor",
        "supported_steps_mhz": [1800, 1900, 2000, 2100],
    }
    assert list(snap.keys()) == [
        "requested_clock_mhz",
        "applied_clock_mhz",
        "mode",
        "supported_steps_mhz",
    ]
    request, result = rpc_spy[-1]
    assert request == {
        "method": "gpu_apply_locked_core_clock",
        "gpu_index": 0,
        "clock_mhz": 1950,
        "prefer_not_above": True,
        "snap_to_supported": True,
    }
    assert result["mock_ops"] == [
        "ApplyLockedCoreClock { clock_mhz: 1950, prefer_not_above: true, snap: true }"
    ]


def test_apply_locked_core_clock_unsnapped_flag_is_forwarded(make_daemon, rpc_spy):
    make_daemon()
    controller = _policy_controller()

    snap = controller.apply_locked_core_clock_mhz(
        1950, prefer_not_above=False, snap_to_supported=False
    )

    assert snap == {
        "requested_clock_mhz": 1950,
        "applied_clock_mhz": 1950,
        "mode": "unsnapped",
        "supported_steps_mhz": [],
    }
    request, _result = rpc_spy[-1]
    assert request["prefer_not_above"] is False
    assert request["snap_to_supported"] is False


def test_apply_locked_core_clock_range_returns_legacy_range_snap(make_daemon, rpc_spy):
    make_daemon()
    controller = _policy_controller()

    range_snap = controller.apply_locked_core_clock_range_mhz(
        1850, 2150, prefer_max_not_above=True
    )

    assert range_snap == {
        "requested_min_clock_mhz": 1850,
        "requested_max_clock_mhz": 2150,
        "applied_min_clock_mhz": 1900,
        "applied_max_clock_mhz": 2100,
        "min_mode": "ceil",
        "max_mode": "floor",
        "supported_steps_mhz": [1800, 1900, 2000, 2100],
    }
    assert list(range_snap.keys()) == [
        "requested_min_clock_mhz",
        "requested_max_clock_mhz",
        "applied_min_clock_mhz",
        "applied_max_clock_mhz",
        "min_mode",
        "max_mode",
        "supported_steps_mhz",
    ]
    request, result = rpc_spy[-1]
    assert request == {
        "method": "gpu_apply_locked_core_clock_range",
        "gpu_index": 0,
        "min_mhz": 1850,
        "max_mhz": 2150,
        "prefer_max_not_above": True,
        "snap_to_supported": True,
    }
    assert result["mock_ops"] == [
        "ApplyLockedCoreClockRange { min_mhz: 1850, max_mhz: 2150, "
        "prefer_max_not_above: true, snap: true }"
    ]


def test_reset_locked_clocks_route_through_daemon(make_daemon, rpc_spy):
    make_daemon()
    controller = _policy_controller()

    assert controller.reset_locked_core_clocks() is True
    assert controller.reset_locked_memory_clocks() is True

    assert [call[0]["method"] for call in rpc_spy] == [
        "gpu_reset_locked_core_clocks",
        "gpu_reset_locked_memory_clocks",
    ]
    assert rpc_spy[0][1]["mock_ops"] == ["ResetLockedCoreClocks"]
    assert rpc_spy[1][1]["mock_ops"] == ["ResetLockedMemoryClocks"]


def test_reset_fans_routes_through_daemon(make_daemon, rpc_spy):
    make_daemon()

    result = daemon_client.gpu_reset_fans(0)

    assert result["reset"] is True
    assert result["fan_count"] == 2
    request, response = rpc_spy[-1]
    assert request == {"method": "gpu_reset_fans", "gpu_index": 0}
    assert response["mock_ops"] == ["SetAllFansDefault { fan_count: 2 }"]


def test_apply_clock_offsets_units_and_readback(make_daemon, rpc_spy):
    make_daemon()
    controller = _policy_controller()

    applied = controller.apply_clock_offsets(
        gpc_clk_vf_offset_mhz=0, mem_clk_vf_offset_mhz=1500
    )

    # Legacy dict: requested sides only, original insertion order, with the
    # mandatory read-back (issue #20) relayed from the daemon.
    assert applied == {
        "gpc_clk_vf_offset_mhz": 0,
        "gpc_clk_vf_offset_readback_mhz": 0,
        "mem_clk_vf_offset_mhz": 1500,
        "mem_clk_vf_offset_readback_mhz": 1500,
    }
    assert list(applied.keys()) == [
        "gpc_clk_vf_offset_mhz",
        "gpc_clk_vf_offset_readback_mhz",
        "mem_clk_vf_offset_mhz",
        "mem_clk_vf_offset_readback_mhz",
    ]
    request, result = rpc_spy[-1]
    assert request == {
        "method": "gpu_apply_clock_offsets",
        "gpu_index": 0,
        "gpc_clk_vf_offset_mhz": 0,
        "mem_clk_vf_offset_mhz": 1500,
    }
    assert result["mock_ops"] == [
        "ApplyClockOffsets { gpc_mhz: Some(0), mem_mhz: Some(1500) }"
    ]


def test_apply_clock_offsets_single_side_omits_the_other(make_daemon, rpc_spy):
    make_daemon()
    controller = _policy_controller()

    applied = controller.apply_clock_offsets(mem_clk_vf_offset_mhz=-500)

    assert applied == {
        "mem_clk_vf_offset_mhz": -500,
        "mem_clk_vf_offset_readback_mhz": -500,
    }
    request, result = rpc_spy[-1]
    assert "gpc_clk_vf_offset_mhz" not in request
    assert result["mock_ops"] == [
        "ApplyClockOffsets { gpc_mhz: None, mem_mhz: Some(-500) }"
    ]


def test_apply_clock_offsets_with_no_sides_is_a_local_no_op(make_daemon, rpc_spy):
    make_daemon()
    controller = _policy_controller()

    assert controller.apply_clock_offsets() == {}
    assert rpc_spy == []  # no RPC at all, exactly like the empty legacy path


def test_gpu_index_is_carried_on_every_write(make_daemon, rpc_spy):
    make_daemon()
    controller = _policy_controller(gpu_index=1)

    controller.apply_power_limit_w(200)

    assert rpc_spy[-1][0]["gpu_index"] == 1


# --- VF-curve write (hidden NVAPI SET -> daemon get-mutate-set) ---------------


def test_set_control_struct_ships_the_full_masked_plan(make_daemon, rpc_spy):
    make_daemon()
    reader = _vf_reader(gpu_index=0)
    control = _control_with_offsets({3: 150_000, 5: 30_000, 9: -15_000, 40: 0})

    assert reader.set_control_struct(control) == 0

    # Every masked point ships with its struct offset (kHz), mask order -- the
    # daemon's get-mutate-set then programs exactly what the local SET wrote.
    request, result = rpc_spy[-1]
    assert request == {
        "method": "gpu_apply_vf_offsets",
        "gpu_index": 0,
        "offsets": [[3, 150_000], [5, 30_000], [9, -15_000], [40, 0]],
    }
    assert result["mock_ops"] == [
        "ApplyVfOffsets { offsets: [(3, 150000), (5, 30000), (9, -15000), (40, 0)] }"
    ]


def test_apply_plan_produces_byte_identical_offsets(make_daemon, rpc_spy):
    """The sweep's apply_plan: GET (ctypes, stubbed here) -> mutate plan
    indices -> re-pointed SET. Non-plan masked points keep their GET offsets,
    plan points get new_offset_mhz*1000 -- byte-identical to the local path."""
    make_daemon()
    reader = _vf_reader(gpu_index=0)
    control = _control_with_offsets({3: 0, 5: 30_000, 9: 0})
    reader.get_control_struct = lambda mask=None: control  # the GET, canned

    apply_plan(
        reader,
        [
            {"index": 3, "new_offset_mhz": 150},
            {"index": 9, "new_offset_mhz": -15},
        ],
    )

    request, result = rpc_spy[-1]
    assert request["offsets"] == [[3, 150_000], [5, 30_000], [9, -15_000]]
    assert result["mock_ops"] == [
        "ApplyVfOffsets { offsets: [(3, 150000), (5, 30000), (9, -15000)] }"
    ]


# --- rising-tail region: byte-identical plan through the transport -------------
#
# RULE ZERO watch: the sweep raises a small number of high-voltage "tail" bins
# for each preset (efficiency tail-tune = 2, balanced = 4, performance = 6).
# The transport swap must ship EXACTLY the offsets the old direct-ctypes SET
# applied for that raised-tail curve -- same tail indices, same offset_khz,
# same clamping. We drive the REAL sweep flattening code (imported read-only;
# no auto_uv edit) to compute a tail plan, run it through the REAL apply_plan,
# and assert the daemon recorded exactly what apply_plan wrote into the struct.

# Editable tail curve: indices ascend with voltage, so the TAIL (highest
# voltages) is the top of the mask -- the high-voltage end the sweep raises.
_TAIL_BASE_CURVE = [
    {
        "index": 10 + i,
        "voltage_mv": 800 + 15 * i,
        "base_mhz": 2100 + 20 * i,
        "target_mhz": 2100 + 20 * i,
    }
    for i in range(12)
]
_TAIL_LOCK_CLOCK_MHZ = 2400
_TAIL_CANDIDATE_VOLTAGE_MV = 890  # index 16; bins above it form the rising tail


def _expected_ship_from_plan(plan, base_curve):
    """What apply_plan writes into the struct, then set_control_struct ships:
    every base index in mask (ascending) order with new_offset_mhz*1000 kHz.
    Ground truth is the plan itself -- the same source apply_plan consumes."""
    offset_by_index = {
        int(item["index"]): int(item["new_offset_mhz"]) * 1000 for item in plan
    }
    return [
        [int(point["index"]), offset_by_index[int(point["index"])]]
        for point in sorted(base_curve, key=lambda p: int(p["index"]))
    ]


@pytest.mark.parametrize(
    ("tail_rise_bins", "preset"),
    [(2, "efficiency-tail-tune"), (4, "balanced"), (6, "performance")],
)
def test_rising_tail_plan_ships_byte_identical(
    make_daemon, rpc_spy, tail_rise_bins, preset
):
    from auto_uv.curve.vf_curve_flattening import build_flattened_plan

    make_daemon()
    reader = _vf_reader(gpu_index=0)

    plan = build_flattened_plan(
        _TAIL_BASE_CURVE,
        lock_clock_mhz=_TAIL_LOCK_CLOCK_MHZ,
        candidate_voltage_mv=_TAIL_CANDIDATE_VOLTAGE_MV,
        tail_rise_bins=tail_rise_bins,
    )

    # Fresh baseline GET (all masked at offset 0); apply_plan mutates in place.
    control = _control_with_offsets(
        {int(point["index"]): 0 for point in _TAIL_BASE_CURVE}
    )
    reader.get_control_struct = lambda mask=None: control
    apply_plan(reader, plan)

    expected = _expected_ship_from_plan(plan, _TAIL_BASE_CURVE)
    request, result = rpc_spy[-1]
    # The shipped offsets are byte-identical to what apply_plan programmed --
    # same indices, same offset_khz -- for the whole curve INCLUDING the raised
    # tail bins at the high-voltage end.
    assert request["offsets"] == expected, preset
    recorded = ", ".join(f"({i}, {o})" for i, o in expected)
    assert result["mock_ops"] == [f"ApplyVfOffsets {{ offsets: [{recorded}] }}"]

    # Sanity: the plan genuinely carries a RISING tail (target clocks strictly
    # increase across the first `tail_rise_bins` editable bins above the lock,
    # then plateau at the soft cap), so this test actually exercises raised
    # bins, not a flat curve. (Offsets themselves shrink because stock base_mhz
    # climbs faster than the tail soft-cap -- the raised bins live in the
    # target clocks, and those are what apply_plan turns into the shipped
    # offset_khz.)
    tail_targets = [
        int(item["target_mhz"])
        for item in sorted(plan, key=lambda p: int(p["voltage_mv"]))
        if int(item["voltage_mv"]) > _TAIL_CANDIDATE_VOLTAGE_MV
    ]
    rising_prefix = tail_targets[:tail_rise_bins]
    assert rising_prefix == sorted(rising_prefix)
    assert rising_prefix[-1] > rising_prefix[0], (preset, tail_targets)
    assert min(rising_prefix) >= _TAIL_LOCK_CLOCK_MHZ, (preset, tail_targets)


def test_rising_tail_bin_count_changes_the_shipped_tail(make_daemon, rpc_spy):
    """A larger tail-rise (performance 6 vs efficiency 2) ships a DIFFERENT,
    higher tail -- proving the raised bins flow through the transport unaltered
    rather than being flattened by the client."""
    from auto_uv.curve.vf_curve_flattening import build_flattened_plan

    def ship(tail_rise_bins):
        make_daemon()
        reader = _vf_reader(gpu_index=0)
        plan = build_flattened_plan(
            _TAIL_BASE_CURVE,
            lock_clock_mhz=_TAIL_LOCK_CLOCK_MHZ,
            candidate_voltage_mv=_TAIL_CANDIDATE_VOLTAGE_MV,
            tail_rise_bins=tail_rise_bins,
        )
        control = _control_with_offsets(
            {int(point["index"]): 0 for point in _TAIL_BASE_CURVE}
        )
        reader.get_control_struct = lambda mask=None: control
        apply_plan(reader, plan)
        return dict(rpc_spy[-1][0]["offsets"])

    efficiency = ship(2)
    performance = ship(6)

    # The top tail index carries a strictly higher offset under the 6-bin
    # performance tail than the 2-bin efficiency tail-tune.
    top_index = max(point["index"] for point in _TAIL_BASE_CURVE)
    assert performance[top_index] > efficiency[top_index]


# --- error relay ---------------------------------------------------------------


def test_write_error_text_is_relayed_verbatim(make_daemon):
    make_daemon(mock_fail="apply_power_limit_w")
    controller = _policy_controller()

    with pytest.raises(RuntimeError, match="^apply_power_limit_w mock failure$"):
        controller.apply_power_limit_w(300)


def test_vf_write_error_text_is_relayed_verbatim(make_daemon):
    make_daemon(mock_fail="apply_vf_offsets_khz")
    reader = _vf_reader()
    control = _control_with_offsets({1: 0})

    with pytest.raises(RuntimeError, match="^apply_vf_offsets_khz mock failure$"):
        reader.set_control_struct(control)


# --- profile verification (streaming) -----------------------------------------


def test_profile_verification_streams_and_builds_whitelisted_argv(
    make_daemon, tmp_path
):
    argv_file = tmp_path / "verify-argv.json"
    daemon = make_daemon(exit_after_lines=True, argv_file=argv_file)

    frames = list(
        daemon_client.daemon_stream_request(
            {
                "method": "start_profile_verification",
                "options": {
                    "stability_seconds": 5,
                    "gpu_index": 0,
                    "auto_uv_profile": "profile-a",
                },
            },
            socket_path=daemon.socket_path,
        )
    )

    assert frames[0]["ok"] is True
    assert frames[0]["control"] == "started"
    assert frames[1] == {"ok": True, "line": '{"event":"verify_start"}\n'}
    assert frames[2] == {"ok": True, "line": "elapsed=1.0s\n"}
    assert frames[3]["control"] == "finished"
    assert frames[3]["exit_code"] == 0

    launched = json.loads(argv_file.read_text(encoding="utf-8"))
    assert launched == [
        "--stability-test",
        "--stability-seconds",
        "5",
        "--gpu-index",
        "0",
        "--auto-uv-profile",
        "profile-a",
        "--stability-stop-request-file",
        str(daemon.verify_stop_request_file()),
    ]


def test_stream_profile_verification_client_returns_exit_code(make_daemon):
    import io

    daemon = make_daemon(exit_after_lines=True)
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = daemon_client.stream_profile_verification(
        {"stability_seconds": 5, "gpu_index": 0},
        socket_path=daemon.socket_path,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stdout.getvalue() == '{"event":"verify_start"}\nelapsed=1.0s\n'
    assert stderr.getvalue() == ""


def test_stop_profile_verification_writes_marker(make_daemon):
    daemon = make_daemon()  # stub loops until SIGINT

    stream = daemon_client.daemon_stream_request(
        {
            "method": "start_profile_verification",
            "options": {"stability_seconds": 600, "gpu_index": 0},
        },
        socket_path=daemon.socket_path,
    )
    try:
        assert next(stream)["control"] == "started"

        stop = daemon_client.stop_profile_verification(socket_path=daemon.socket_path)
        assert stop["stopped"] is True

        deadline = time.monotonic() + 4.0
        content = None
        while time.monotonic() < deadline:
            try:
                content = daemon.verify_stop_request_file().read_text(encoding="utf-8")
                break
            except OSError:
                time.sleep(0.02)
        assert content == "stop requested by PenguinBurner daemon client\n"
    finally:
        stream.close()


def test_stop_profile_verification_when_idle(make_daemon):
    daemon = make_daemon()
    stop = daemon_client.stop_profile_verification(socket_path=daemon.socket_path)
    assert stop == {"stopped": False, "state": "idle"}


# --- profile deletion -----------------------------------------------------------


def _seed_profiles(daemon: DaemonHandle, names: list[str]) -> list[Path]:
    profiles_dir = daemon.home / ".config" / "PenguinBurner" / "auto-uv-profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in names:
        path = profiles_dir / name
        path.write_text("{}", encoding="utf-8")
        paths.append(path)
    return paths


def test_delete_auto_uv_profiles_happy_path(make_daemon):
    daemon = make_daemon()
    first, second = _seed_profiles(
        daemon, ["auto-uv-profile-a.json", "auto-uv-profile-b.json"]
    )

    result = daemon_client.delete_auto_uv_profiles(
        [str(first), str(second)], socket_path=daemon.socket_path
    )

    deleted = result["deleted"]
    assert len(deleted) == 2
    assert deleted[0].endswith("auto-uv-profile-a.json")
    assert deleted[1].endswith("auto-uv-profile-b.json")
    assert not first.exists()
    assert not second.exists()


def test_delete_auto_uv_profiles_rejects_traversal(make_daemon):
    daemon = make_daemon()
    (profile,) = _seed_profiles(daemon, ["auto-uv-profile-a.json"])
    victim = daemon.home / "victim.json"
    victim.write_text("precious", encoding="utf-8")
    attack = (
        daemon.home
        / ".config"
        / "PenguinBurner"
        / "auto-uv-profiles"
        / ".."
        / ".."
        / ".."
        / "victim.json"
    )

    with pytest.raises(
        RuntimeError, match="^refusing to delete a path outside the Auto-UV profiles"
    ):
        daemon_client.delete_auto_uv_profiles(
            [str(profile), str(attack)], socket_path=daemon.socket_path
        )

    # Validate-all-first: nothing was deleted, the victim survives.
    assert victim.exists()
    assert profile.exists()


def test_delete_auto_uv_profiles_cli_subcommand(make_daemon):
    daemon = make_daemon()
    (profile,) = _seed_profiles(daemon, ["auto-uv-profile-a.json"])

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "runtime.daemon_client",
            "--socket",
            str(daemon.socket_path),
            "delete-auto-uv-profiles",
            json.dumps([str(profile)]),
        ],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Deleted 1 saved Auto-UV profile." in completed.stdout
    assert not profile.exists()
