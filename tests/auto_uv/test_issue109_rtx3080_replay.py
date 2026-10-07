"""Replay the RTX 3080 scan history attached to issue #109 (2026-10-06).

The tester's archive gives the stock curves, every probe decision, the crash
points and the checkpoint/blacklist state across six GUI launches. These tests
feed that history through the real checkpoint, recovery and search code so the
three reported outcomes cannot silently return:

* the first restart after a crash refused to resume;
* Balanced locked above its table clock;
* Performance never climbed past Balanced.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any

import pytest
from auto_uv_test_data import (
    rtx_3080_issue109_stock_curve_cold,
    rtx_3080_issue109_stock_curve_warm,
)

from auto_uv.curve.flattened_voltage_probe_curve import (
    build_flattened_voltage_probe_curve,
)
from auto_uv.domain.types import AutoUvProbeSummary
from auto_uv.persistence.scan_checkpoint import ScanCheckpoint

GPU_NAME = "NVIDIA GeForce RTX 3080"


def _blank(cls) -> dict[str, Any]:
    return {f.name: None for f in fields(cls)}


def _summary(voltage_mv: int, clock_mhz: int, plan: list[dict]) -> AutoUvProbeSummary:
    summary = AutoUvProbeSummary(**_blank(AutoUvProbeSummary))
    summary.candidate_voltage_mv, summary.lock_clock_mhz = voltage_mv, clock_mhz
    summary.avg_voltage_mv = float(voltage_mv)
    summary.avg_core_clock_mhz = float(clock_mhz)
    summary.avg_fps, summary.avg_power_w = 100.0, 250.0
    summary.tested_plan = plan
    return summary


def _candidate(curve, voltage_mv, clock_mhz):
    return build_flattened_voltage_probe_curve(
        curve,
        candidate_voltage_mv=voltage_mv,
        target_clock_mhz=clock_mhz,
        label=f"lower-voltage {voltage_mv}mV",
        metadata={"tail_rise_bins": 2},
    )


def _shift_one_bin(curve: list[dict], *, from_mhz: int) -> list[dict]:
    """Ampere stock curves drift one 15 MHz bin between launches; keep the grid."""
    return [
        dict(point, base_mhz=point["base_mhz"] + 15, target_mhz=point["target_mhz"] + 15)
        if point["base_mhz"] >= from_mhz
        else dict(point)
        for point in curve
    ]


def _interrupted(voltage_mv: int, clock_mhz: int, phase: str) -> dict:
    # Shape of the entry consume_crash_cache() builds from the probe marker.
    return {
        "candidate_voltage_mv": voltage_mv,
        "lock_clock_mhz": clock_mhz,
        "phase": phase,
        "reason": "previous-run-abruptly-ended",
    }


@pytest.mark.parametrize(
    "tier, lock_mhz, passed_mv, crashed_mv, expected_recovery_mv, completed",
    [
        # Launch 2 -> 3: Efficiency at 268 W, descent held 1815 MHz, crash at 856 mV.
        ("efficiency", 1815, (893, 875), 856, 881, ()),
        # Launch 4 -> 5: Balanced at 380 W, descent held 1920 MHz, crash at 937 mV.
        ("balanced", 1920, (1043, 1025, 1006, 987, 968, 950, 943), 937, 950, ("efficiency",)),
    ],
)
@pytest.mark.parametrize("drift", [0, 15])
def test_first_restart_after_a_fixed_clock_descent_crash_resumes(
    tmp_path, tier, lock_mhz, passed_mv, crashed_mv, expected_recovery_mv, completed, drift
):
    curve = rtx_3080_issue109_stock_curve_warm()
    identity = {"gpu": {"uuid": "GPU-3080"}, "base_curve": curve,
                "options": {"auto_uv_mode": "adaptive"}}
    path = tmp_path / "checkpoint.json"
    saved = ScanCheckpoint(identity=identity, callback=None, log=lambda _: None, path=path)
    for done in completed:
        saved.event("tier_started", {"tier": done})
        saved.event("tier_completed", {"tier": done, "voltage_mv": 881, "target_mhz": 1815})
    saved.event("tier_started", {"tier": tier})
    for voltage in passed_mv:
        point = _candidate(curve, voltage, lock_mhz)
        saved.event("candidate_curve", {"stage": f"{tier}-candidate", "voltage_mv": voltage,
                                        "clock_mhz": lock_mhz})
        saved.event("probe_start", {"stage": f"{tier}-candidate", "voltage_mv": voltage,
                                    "clock_mhz": lock_mhz})
        saved.note_pass(point, _summary(voltage, lock_mhz, point.flattened_plan))
        saved.event("probe_result", {"stage": f"{tier}-candidate", "voltage_mv": voltage,
                                     "clock_mhz": lock_mhz, "decision": "pass"})
    # The crashed probe never produced a result; only the marker survives.
    unsafe = [_interrupted(crashed_mv, lock_mhz, f"{tier}-candidate")]
    live_curve = _shift_one_bin(curve, from_mhz=1650) if drift else curve
    identity["base_curve"] = live_curve
    messages: list[str] = []

    restarted = ScanCheckpoint(
        identity=identity, callback=None, log=messages.append, path=path
    )
    assert restarted.resuming
    restarted.prepare_recovery(live_curve, unsafe, unsafe[0])

    recovery = restarted.recovery_for(tier)
    assert recovery is not None, messages
    assert (recovery.voltage_mv, recovery.target_mhz) == (expected_recovery_mv, lock_mhz)
    assert recovery.metadata["resume_source_voltage_mv"] == min(
        v for v in passed_mv if v > crashed_mv
    )
    assert recovery.metadata["resume_recovery"] is True
    if drift:
        assert any("shifted by at most one 15MHz bin" in m for m in messages)
    for done in completed:
        assert done in {
            item["payload"]["tier"] for item in restarted.events
            if item["event"] == "tier_completed"
        }


# ---------------------------------------------------------------------------
# Balanced locked at the boosted stock clock instead of the table clock.
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from auto_uv.run.baseline_probe import build_loaded_baseline_candidate  # noqa: E402
from auto_uv.scan_mode.target_overrides import tier_lock_clock_cap_mhz  # noqa: E402
from stability.q2rtx.models import TelemetrySample  # noqa: E402


def _loaded_discovery(clock_mhz: float, voltage_mv: float, power_w: float):
    """Discovery probe telemetry as the tester's card produced it under Q2RTX."""
    samples = [
        TelemetrySample(float(i), 100.0, power_w, clock_mhz, 65.0, voltage_mv, 60.0)
        for i in range(20)
    ]
    summary = _summary(int(voltage_mv), int(clock_mhz), [])
    summary.avg_core_clock_mhz = clock_mhz
    summary.avg_power_w = power_w
    return summary, SimpleNamespace(telemetry_samples=samples, success=True)


@pytest.mark.parametrize(
    "tier, power_limit_w, loaded_clock, loaded_mv, expected_lock",
    [
        # Launch 4: Balanced stock curve loaded at 1924 MHz / 1062 mV under 380 W.
        ("balanced", 380, 1924.4, 1062.0, 1875),
        # Launch 2: Efficiency stock curve loaded at 1830 MHz / 937 mV under 268 W.
        ("efficiency", 268, 1830.0, 937.0, 1740),
        # Performance table clock 1930 -> highest 15 MHz step at or below it.
        ("performance", 380, 1950.0, 1075.0, 1920),
    ],
)
def test_loaded_baseline_locks_at_or_below_the_tier_table_clock(
    tier, power_limit_w, loaded_clock, loaded_mv, expected_lock
):
    curve = rtx_3080_issue109_stock_curve_warm()
    summary, result = _loaded_discovery(loaded_clock, loaded_mv, power_limit_w * 0.83)
    cap = tier_lock_clock_cap_mhz({}, gpu_name=GPU_NAME, tier=tier)
    assert cap is not None and cap >= expected_lock

    uncapped, _ = build_loaded_baseline_candidate(
        curve, discovery_summary=summary, discovery_result=result,
        power_limit_w=power_limit_w, tail_rise_bins=2,
    )
    capped, target = build_loaded_baseline_candidate(
        curve, discovery_summary=summary, discovery_result=result,
        power_limit_w=power_limit_w, tail_rise_bins=2, max_clock_mhz=cap,
    )
    assert uncapped.target_mhz > cap  # The tester's run started here.
    assert capped.target_mhz == expected_lock
    assert target.measured_clock_mhz == pytest.approx(loaded_clock)
    locked = [p for p in capped.flattened_plan if p["voltage_mv"] == capped.voltage_mv]
    assert locked and locked[0]["target_mhz"] == expected_lock
    assert max(p["target_mhz"] for p in capped.flattened_plan) == expected_lock + 30


def test_power_bound_baseline_below_the_table_clock_is_not_raised():
    curve = rtx_3080_issue109_stock_curve_warm()
    summary, result = _loaded_discovery(1080.0, 712.0, 220.0)
    capped, _ = build_loaded_baseline_candidate(
        curve, discovery_summary=summary, discovery_result=result,
        power_limit_w=220, tail_rise_bins=2,
        max_clock_mhz=tier_lock_clock_cap_mhz({}, gpu_name=GPU_NAME, tier="balanced"),
    )
    assert capped.target_mhz == 1080


def test_custom_clock_target_replaces_the_table_cap():
    options = {"auto_uv_balanced_target_clock_mhz": 1920}
    assert tier_lock_clock_cap_mhz(options, gpu_name=GPU_NAME, tier="balanced") == 1920
    assert tier_lock_clock_cap_mhz(
        {"auto_uv_balanced_target_clock_mhz": 1885}, gpu_name=GPU_NAME, tier="balanced"
    ) == 1885
    assert tier_lock_clock_cap_mhz({}, gpu_name="Unknown GPU", tier="balanced") is None


# ---------------------------------------------------------------------------
# Performance never climbed past Balanced: GUI default targets became bounds.
# ---------------------------------------------------------------------------

from auto_uv.domain.types import (  # noqa: E402
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv.main_loop import select_final_scan_candidate  # noqa: E402
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome  # noqa: E402

GUI_DEFAULT_TARGETS = {
    # What the scan dialog sent for the tester's full scan: every tier's table
    # default, untouched.
    "auto_uv_efficiency_target_voltage_mv": 800,
    "auto_uv_efficiency_target_clock_mhz": 1750,
    "auto_uv_balanced_target_voltage_mv": 875,
    "auto_uv_balanced_target_clock_mhz": 1885,
    "auto_uv_performance_target_voltage_mv": 900,
    "auto_uv_performance_target_clock_mhz": 1930,
}


class _PassingRunner:
    power_limit_w = 380

    def __init__(self):
        self.tried: list[tuple[int, int]] = []

    def probe_candidate(self, candidate, **_kwargs):
        self.tried.append((candidate.voltage_mv, candidate.target_mhz))
        probe = _summary(candidate.voltage_mv, candidate.target_mhz, candidate.flattened_plan)
        probe.avg_fps = 100.0 + candidate.target_mhz / 100.0
        return VoltageProbeOutcome(
            decision=StableRunDecision(True, FailureKind.NONE, FailureSeverity.PASS, "stable"),
            raw_probe=probe,
        )


@pytest.mark.parametrize("runtime_options", [{}, GUI_DEFAULT_TARGETS], ids=["cli", "gui"])
@pytest.mark.parametrize(
    "balanced_mv, balanced_lock, expected_top",
    [
        (950, 1920, 1930),  # The tester's run: one step remained and was never tried.
        (937, 1875, 1930),  # With the table cap: four steps up to the Performance clock.
    ],
)
def test_performance_climbs_from_the_verified_balanced_point(
    runtime_options, balanced_mv, balanced_lock, expected_top
):
    curve = rtx_3080_issue109_stock_curve_warm()
    start = _candidate(curve, balanced_mv, balanced_lock)
    start_probe = _summary(balanced_mv, balanced_lock, start.flattened_plan)
    runner = _PassingRunner()
    selected = select_final_scan_candidate(
        base_curve=curve,
        settings=SimpleNamespace(auto_uv_mode="adaptive"),
        runtime_options=dict(runtime_options),
        stable_plan=start.flattened_plan,
        stable_voltage_mv=balanced_mv,
        stable_lock_clock_mhz=balanced_lock,
        stable_probe=start_probe,
        stable_history=[start_probe],
        runner=runner,
        gpu=SimpleNamespace(translated_gpu_policy={"gpu_name": GPU_NAME}, clock_ceiling=None),
        probe_history=[],
        log=lambda _: None,
        tail_rise_bins=2,
        measured_baseline_clock_mhz=1926.32,
        discovery_summary=start_probe,
        baseline_candidate=VfCurveCandidate("baseline", 1062, 1920, start.flattened_plan),
        final_verification_duration_s=300,
        event_callback=None,
        run_performance_auto_oc=True,
        run_power_bound_clock_reclaim=False,
        request_reason="adaptive-performance",
        auto_uv_mode_override="performance",
    )
    assert runner.tried, "Auto-OC must probe above the Balanced point"
    assert all(voltage == balanced_mv for voltage, _ in runner.tried)
    assert all(balanced_lock < clock <= expected_top for _, clock in runner.tried)
    assert (selected.voltage_mv, selected.lock_clock_mhz) == (balanced_mv, expected_top)


# ---------------------------------------------------------------------------
# The whole GUI full scan, launch by launch, through the real main loop.
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from auto_uv import main_loop as main  # noqa: E402
from auto_uv.final_verification import main_loop as final_module  # noqa: E402
from auto_uv.persistence.probe_in_progress_marker_file import (  # noqa: E402
    write_probe_in_progress_marker,
)
from auto_uv.persistence.scan_checkpoint import scan_checkpoint_path  # noqa: E402
from auto_uv.persistence.unsafe_voltage_blacklist_file import (  # noqa: E402
    load_unsafe_voltage_blacklist,
)
from auto_uv.probes import runner as runner_module  # noqa: E402
from profiles.uv.profile_store import auto_uv_profiles_dir  # noqa: E402
from stability.q2rtx.models import (  # noqa: E402
    Q2RTXBenchmarkSummary,
    Q2RTXStabilityConfig,
    Q2RTXStabilityResult,
)

# Loaded stock behaviour measured on the tester's card: (clock, voltage).
STOCK_LOAD = {268: (1830.0, 937.0), 380: (1924.4, 1062.0)}
PROBE_POWER_W = {268: 252.0, 380: 315.0}


def _probe_result(voltage_mv, lock_mhz, plan, *, measured_clock, measured_mv, power_w):
    summary = _summary(voltage_mv, lock_mhz, plan)
    summary.avg_voltage_mv = summary.loaded_median_voltage_mv = measured_mv
    summary.avg_core_clock_mhz = summary.loaded_median_core_clock_mhz = measured_clock
    summary.avg_fps = fps = 100.0 + measured_clock / 100.0
    summary.avg_power_w = power_w
    summary.efficiency_fps_per_w = fps / power_w
    summary.log_path = Path("/tmp/simulated-q2rtx.log")
    summary.loaded_qualified_sample_count = 10
    summary.hw_power_brake_samples = 0
    summary.used_companion_load = True
    summary.result_reason = "stable run"
    benchmark = Q2RTXBenchmarkSummary(**_blank(Q2RTXBenchmarkSummary))
    benchmark.reason, benchmark.loops = "target", 1
    benchmark.render_frames, benchmark.measured_s = 1000, 10.0
    benchmark.fps_avg = benchmark.fps_min = benchmark.fps_max = benchmark.fps_mean = fps
    samples = [
        TelemetrySample(float(i), 100.0, power_w, measured_clock, 64.0, measured_mv, 55.0)
        for i in range(20)
    ]
    result = Q2RTXStabilityResult(
        True, "ok", "benchmark", "q2demo1", [], Path("/tmp/q2rtx"), Path("/tmp"),
        10, 10.0, None, summary.log_path, 0, "benchmark-duration-complete",
        [], [], samples, samples, [], benchmark,
    )
    return summary, result


class Rtx3080:
    """The tester's card: 380 W stock budget, Ampere stock curve, no memory offset."""

    clock_ceiling = None
    baseline_power_limit_w = 380

    def __init__(self, curve):
        self.runtime_default_plan = curve
        self.power_limit_w = 380
        self.requested_power_limit_w = None
        self.reader = self.live_voltage_reader = self.policy_controller = self
        self.gpu_identity = {"uuid": "GPU-3080-issue109", "name": GPU_NAME}
        self.translated_gpu_policy = {
            "gpu_name": GPU_NAME, "power_limit_w": 380, "mem_clk_vf_offset_mhz": 0,
        }
        self.gpu = SimpleNamespace(
            apply_clock_offsets=lambda mem_clk_vf_offset_mhz: {
                "mem_clk_vf_offset_readback_mhz": int(mem_clk_vf_offset_mhz)
            }
        )

    def apply_requested_power_limit(self, **_kw):
        self.power_limit_w = self.requested_power_limit_w or self.power_limit_w
        self.translated_gpu_policy["power_limit_w"] = self.power_limit_w
        return self.power_limit_w

    def clamp_power_limit_w(self, watts):
        return int(watts)

    def get_memory_clock_offset_range_mhz(self):
        return (0, 4000)

    def capabilities(self):
        return SimpleNamespace(identity=SimpleNamespace(driver_version="615.71.09"))

    def start_clock_ceiling(self, _target):
        pass

    def close(self):
        pass


def test_issue109_gui_full_scan_history_replays_into_three_profiles(monkeypatch):
    """Launches 1-4 of the tester's archive, with the fixes, end to end.

    Hardware is scripted from the tester's telemetry; everything else is the
    shipped code: discovery, baselines, descent, crash marker, blacklist,
    checkpoint, resume verification, Balanced reuse, Auto-OC and the saved
    profiles. Each crash is the first probe below the voltage the card
    actually froze at, in the CUDA phase of a medium probe.
    """
    cold, warm = rtx_3080_issue109_stock_curve_cold(), rtx_3080_issue109_stock_curve_warm()
    launches = [
        # (stock curve read at launch, tier that crashes, freeze voltage)
        (cold, "efficiency", 818),
        (warm, "efficiency", 856),           # curve 60 MHz lower: checkpoint rejected
        (_shift_one_bin(warm, from_mhz=1650), "balanced", 937),  # one-bin drift
        (warm, None, None),                  # one-bin drift back; completes
    ]
    options = {"auto_uv_mode": "adaptive", **GUI_DEFAULT_TARGETS,
               **{f"auto_uv_{t}_memory_offset_mhz": 0 for t in ("efficiency", "balanced", "performance")}}
    state = {"launch": 0, "crash": (None, None)}
    calls: list[tuple[int, str, int, int, int]] = []
    logs: list[list[str]] = []
    events: list[list[tuple[str, dict]]] = []

    def probe(**kw):
        stage = kw["phase_label"]
        voltage, lock = kw["candidate_voltage_mv"], kw["lock_clock_mhz"]
        cap = int(kw["power_limit_w"])
        calls.append((state["launch"], stage, voltage, lock, cap))
        if stage == "discover":
            clock, mv = STOCK_LOAD[cap]
            return _probe_result(voltage, lock, kw["candidate_plan"],
                                 measured_clock=clock, measured_mv=mv, power_w=PROBE_POWER_W[cap])
        crash_tier, crash_mv = state["crash"]
        if crash_tier and stage == f"{crash_tier}-candidate" and voltage <= crash_mv:
            write_probe_in_progress_marker(
                phase=stage, candidate_voltage_mv=voltage, lock_clock_mhz=lock,
            )
            raise SystemExit("simulated Xid 79: GPU fell off the bus in the CUDA phase")
        # The card ran its two-bin tail above the lock, read ~6 mV above target,
        # and drew less power at each lower voltage (board power ~ V^2).
        stock_mv = STOCK_LOAD[cap][1]
        return _probe_result(voltage, lock, kw["candidate_plan"],
                             measured_clock=float(lock + 30), measured_mv=float(voltage + 6),
                             power_w=PROBE_POWER_W[cap] * (voltage / stock_mv) ** 2)

    monkeypatch.setattr(main, "cleanup_managed_q2rtx_processes", lambda *_a, **_k: None)
    monkeypatch.setattr(runner_module, "probe_voltage_candidate", probe)
    monkeypatch.setattr(main, "probe_voltage_candidate", probe)
    monkeypatch.setattr(final_module, "apply_plan_and_refresh", lambda *_args: None)

    for index, (curve, crash_tier, crash_mv) in enumerate(launches, start=1):
        state.update(launch=index, crash=(crash_tier, crash_mv))
        monkeypatch.setattr(main, "open_live_gpu_vf_curve_applier", lambda c=curve, **_kw: Rtx3080(c))
        logs.append([])
        events.append([])
        run = lambda: main.run_voltage_frequency_undervolt_main_loop(  # noqa: E731
            gpu_index=0, runtime_options=dict(options), q2rtx_config=Q2RTXStabilityConfig(),
            event_callback=lambda name, payload: events[-1].append((name, payload)),
            log=logs[-1].append,
        )
        if crash_tier:
            with pytest.raises(SystemExit, match="Xid 79"):
                run()
            assert scan_checkpoint_path().is_file()
        else:
            run()

    def launch_calls(n):
        return [c for c in calls if c[0] == n]

    def passes(n, tier):
        crashed = [v for _, s, v, _, _ in launch_calls(n) if s == f"{tier}-candidate"]
        return sorted(set(crashed[:-1]), reverse=True)

    def completed(n):
        return {p["tier"]: (p["voltage_mv"], p["target_mhz"])
                for e, p in events[n - 1] if e == "tier_completed"}

    def froze_at(n, tier, freeze_mv):
        probes = [c for c in launch_calls(n) if c[1] == f"{tier}-candidate"]
        assert launch_calls(n)[-1] is probes[-1]
        assert probes[-1][2] <= freeze_mv < min(c[2] for c in probes[:-1])

    # Launch 1: the first efficiency probe at or below 818 mV froze the card.
    froze_at(1, "efficiency", 818)
    # Launch 2: the warm curve differs by up to 60 MHz, so the scan started over.
    assert any("checkpoint rejected: scan inputs changed: base_curve" in m for m in logs[1])
    froze_at(2, "efficiency", 856)
    # Launch 3: resumed across the one-bin drift from the last pass above 856 mV,
    # one voltage bin up, at the same descent clock, with no efficiency re-sweep.
    assert any("shifted by at most one 15MHz bin" in m for m in logs[2]), logs[2][:40]
    assert not any("Cannot resume" in m for m in logs[2])
    eff_lock = launch_calls(2)[-1][3]
    resume = next(c for c in launch_calls(3) if c[1] == "resume-verify")
    assert (resume[2], resume[3]) == (min(passes(2, "efficiency")) + 6, eff_lock)
    assert not [c for c in launch_calls(3) if c[1] == "efficiency-candidate"]
    assert completed(3)["efficiency"] == (resume[2], eff_lock)
    froze_at(3, "balanced", 937)
    # Launch 4: resumed Balanced the same way, then Performance climbed.
    assert not any("Cannot resume" in m for m in logs[3])
    bal_lock = launch_calls(3)[-1][3]
    resume = next(c for c in launch_calls(4) if c[1] == "resume-verify")
    assert (resume[2], resume[3]) == (min(passes(3, "balanced")) + 7, bal_lock)
    assert not [c for c in launch_calls(4) if c[1] == "balanced-candidate"]
    final = {**completed(3), **completed(4)}  # Efficiency stays verified from launch 3.
    assert set(completed(4)) == {"balanced", "performance"}
    assert set(final) == {"efficiency", "balanced", "performance"}
    assert final["balanced"] == (resume[2], bal_lock)

    # The table clocks bound both descents: 1750 -> 1740 at 268 W, 1885 -> 1875 at 380 W.
    assert eff_lock == 1740 and bal_lock == 1875
    assert all(c[4] == 268 for c in calls if c[1].startswith("efficiency"))
    assert all(c[4] == 380 for c in calls if c[1].startswith(("balanced", "performance")))
    # Performance reused the verified Balanced point and climbed at its voltage
    # even though the GUI sent the 900 mV table default.
    climb = [c for c in launch_calls(4) if c[1] == "candidate"]  # Auto-OC rungs
    assert climb and all(c[2] == final["balanced"][0] for c in climb)
    assert [c[3] for c in climb] == [1890, 1905, 1920, 1930]
    assert final["performance"] == (final["balanced"][0], 1930)
    assert final["performance"][1] > final["balanced"][1] > final["efficiency"][1]

    blacklist = {(e["candidate_voltage_mv"], e["lock_clock_mhz"]) for e in load_unsafe_voltage_blacklist()}
    assert blacklist == {
        (launch_calls(n)[-1][2], launch_calls(n)[-1][3]) for n in (1, 2, 3)
    }
    assert {lock for _, lock in blacklist} == {1740, 1875}
    assert len(list(auto_uv_profiles_dir().glob("*.json"))) == 3
    assert not scan_checkpoint_path().exists()
