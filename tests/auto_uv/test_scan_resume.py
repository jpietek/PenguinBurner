from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest
from auto_uv_test_data import rtx_5080_20260524_high_oc_base_curve

from auto_uv.curve.flattened_voltage_probe_curve import (
    build_flattened_voltage_probe_curve,
)
from auto_uv.domain.types import (
    AutoUvCriticalProbeError,
    AutoUvError,
    AutoUvProbeSummary,
)
from auto_uv.persistence.probe_in_progress_marker_file import (
    write_probe_in_progress_marker,
)
from auto_uv.persistence.scan_checkpoint import ScanCheckpoint, scan_checkpoint_path
from auto_uv.persistence.unsafe_voltage_blacklist_file import (
    load_unsafe_voltage_blacklist,
    record_unsafe_voltage,
)
from auto_uv.probes import runner as runner_module
from auto_uv.run.scan_resume import recovery_candidate
from stability.q2rtx.models import (
    Q2RTXBenchmarkSummary,
    Q2RTXStabilityConfig,
    Q2RTXStabilityResult,
    TelemetrySample,
)


def measured_result(voltage, clock, plan):
    summary = AutoUvProbeSummary(
        **{field.name: None for field in fields(AutoUvProbeSummary)}
    )
    summary.candidate_voltage_mv, summary.lock_clock_mhz = voltage, clock
    summary.avg_voltage_mv, summary.avg_core_clock_mhz = float(voltage), float(clock)
    summary.avg_fps, summary.avg_power_w = 100.0, 250.0
    summary.efficiency_fps_per_w = 0.4
    summary.log_path = Path("/tmp/simulated-q2rtx.log")
    summary.loaded_qualified_sample_count = 10
    summary.hw_power_brake_samples = 0
    summary.used_companion_load = True
    summary.result_reason = "stable run"
    summary.tested_plan = plan
    benchmark = Q2RTXBenchmarkSummary(
        **{field.name: None for field in fields(Q2RTXBenchmarkSummary)}
    )
    benchmark.reason, benchmark.loops = "target", 1
    benchmark.render_frames, benchmark.measured_s = 1000, 10.0
    benchmark.fps_avg = benchmark.fps_min = benchmark.fps_max = benchmark.fps_mean = (
        100.0
    )
    samples = [
        TelemetrySample(
            float(i), 100.0, 250.0, float(clock), 60.0, float(voltage), 40.0
        )
        for i in range(10)
    ]
    result = Q2RTXStabilityResult(
        True,
        "ok",
        "benchmark",
        "q2demo1",
        [],
        Path("/tmp/q2rtx"),
        Path("/tmp"),
        10,
        10.0,
        None,
        summary.log_path,
        0,
        "benchmark-duration-complete",
        [],
        [],
        samples,
        samples,
        [],
        benchmark,
    )
    return summary, result


def candidate(curve, voltage=850, clock=2745):
    return build_flattened_voltage_probe_curve(
        curve,
        candidate_voltage_mv=voltage,
        target_clock_mhz=clock,
        label="simulated candidate",
        metadata={"tail_rise_bins": 0},
    )


@pytest.mark.parametrize("failure_kind", ["hard-reset", "device-lost"])
@pytest.mark.parametrize("base_shift", [0, 15])
@pytest.mark.parametrize(
    "mode, override, duration",
    [
        ("efficiency", None, 60),
        ("balanced", None, 180),
        ("performance", None, 300),
        ("adaptive", None, 60),
        ("adaptive", 120, 120),
    ],
)
def test_failure_then_scan_click_restores_table_plot_and_long_verifies_safer_point(
    monkeypatch, qapp, tmp_path, failure_kind, mode, override, duration, base_shift
):
    """Run real orchestration/persistence/Qt twice; substitute only hardware and the search schedule."""
    from auto_uv import main_loop as main
    from auto_uv.final_verification import main_loop as final_module
    from ui import window as window_module
    from ui.qt import import_qt
    from ui.window import MainWindow

    curve = rtx_5080_20260524_high_oc_base_curve()
    for point in curve:
        point.update(current_offset_mhz=0, preserve_base=False)
    calls, finals, restored, workloads = [], [], [], []
    options = {
        "auto_uv_mode": mode,
        "auto_uv_efficiency_power_limit_w": 279,
        "auto_uv_efficiency_target_voltage_mv": 850,
        "auto_uv_efficiency_target_clock_mhz": 2800,
    }
    if override is not None:
        options["auto_uv_final_verification_s"] = override

    class Gpu:
        runtime_default_plan = curve
        power_limit_w = 279
        clock_ceiling = None
        baseline_power_limit_w = 360

        def __init__(self):
            self.reader = self.live_voltage_reader = self.policy_controller = self
            self.gpu_identity = {
                "uuid": "GPU-simulated",
                "name": "NVIDIA GeForce RTX 5080",
            }
            self.translated_gpu_policy = {
                "gpu_name": "NVIDIA GeForce RTX 5080",
                "power_limit_w": 279,
                "mem_clk_vf_offset_mhz": 0,
            }

        def apply_requested_power_limit(self, **_kw):
            self.power_limit_w = (
                getattr(self, "requested_power_limit_w", None) or self.power_limit_w
            )
            self.translated_gpu_policy["power_limit_w"] = self.power_limit_w
            return self.power_limit_w

        def clamp_power_limit_w(self, watts):
            return watts

        def capabilities(self):
            return SimpleNamespace(
                identity=SimpleNamespace(driver_version="test-driver")
            )

        def start_clock_ceiling(self, _target):
            pass

        def close(self):
            pass

    def probe(**kw):
        point = (kw["candidate_voltage_mv"], kw["lock_clock_mhz"])
        if finals and kw["phase_label"] == "discover":
            raise AutoUvCriticalProbeError("simulated later tier unavailable")
        calls.append(point)
        workloads.append(kw["q2rtx_config"])
        if point == (850, 2800):
            if failure_kind == "device-lost":
                record_unsafe_voltage(
                    candidate_voltage_mv=850,
                    lock_clock_mhz=2800,
                    phase="candidate",
                    reason="stability-probe-failed",
                    blocked_lock_clock_mhz=[2800, 2782, 2760],
                )
                summary, result = measured_result(*point, kw["candidate_plan"])
                result.success, result.reason = False, "fatal-q2rtx-output"
                result.xid_messages = ["NVIDIA Xid 109"]
                return summary, result
            write_probe_in_progress_marker(
                candidate_voltage_mv=850,
                lock_clock_mhz=2800,
                phase="candidate",
                details={"blocked_lock_clock_mhz": [2800, 2782, 2760]},
            )
            raise SystemExit("simulated hard reset")
        return measured_result(*point, kw["candidate_plan"])

    def sweep(_curve, *, io, **_kw):
        for voltage, clock in (
            (900, 2610),
            (875, 2610),
            (850, 2745),
            (850, 2760),
            (850, 2775),
            (850, 2800),
        ):
            point = candidate(curve, voltage, clock)
            outcome = io.probe_candidate(point)
            if not outcome.decision.passed:
                raise AutoUvCriticalProbeError("simulated device lost")
            io.write_verified_candidate(point, outcome)
        pytest.fail("failure did not interrupt the scan")

    def final(**kw):
        finals.append(
            {**kw, "translated_gpu_policy": dict(kw["translated_gpu_policy"])}
        )
        assert kw["stable_probe"] is None  # New voltage has no fabricated measurements.
        assert kw["auto_oc_metadata"]["resume_recovery"] is True
        # Observe the restored GUI BEFORE final verification starts.
        win = windows[-1]
        # Historical passing rows remain; changed stock clocks require two new baselines.
        assert win.runs_table.widget.rowCount() == (7 if base_shift else 5)
        clocks = [win.runs_table.widget.item(i, 2).text() for i in range(win.runs_table.widget.rowCount())]
        assert "2800" not in clocks and "2775" not in clocks and "2760" not in clocks
        if not base_shift:
            assert win.vf_plot._last_candidate_curve_id == "850mv-2745mhz"
            voltages, clocks = win.vf_plot.candidate_curve.getData()
            assert clocks[list(voltages).index(850)] == 2745
        else:
            for point in kw["stable_plan"]:
                base = next(p for p in curve if p["index"] == point["index"])
                assert point["new_offset_mhz"] == point["target_mhz"] - base["base_mhz"]
        win.window.show()
        qapp.processEvents()
        win.window.grab().save(str(tmp_path / "restored-scan.png"))
        return final_module.run_final_verification_and_save(**kw)

    monkeypatch.setattr(main, "open_live_gpu_vf_curve_applier", lambda **_kw: Gpu())
    monkeypatch.setattr(
        main, "cleanup_managed_q2rtx_processes", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        main,
        "build_loaded_baseline_candidate",
        lambda *_a, **_kw: (
            candidate(curve, 975, 2610),
            SimpleNamespace(measured_clock_mhz=2610.0),
        ),
    )
    monkeypatch.setattr(main, "run_base_uv_loop", sweep)
    monkeypatch.setattr(main, "run_final_verification_and_save", final)
    monkeypatch.setattr(runner_module, "probe_voltage_candidate", probe)
    monkeypatch.setattr(main, "probe_voltage_candidate", probe)
    monkeypatch.setattr(final_module, "apply_plan_and_refresh", lambda *_args: None)
    monkeypatch.setattr(window_module, "load_profile_summaries", lambda: [])
    monkeypatch.setattr(
        window_module, "gpu_choices_with_fallback", lambda **_kw: ([], 0)
    )
    monkeypatch.setattr(
        window_module,
        "systemd_autostart_profile_info",
        lambda: {"selector": "", "silent_fan_curve": False},
    )
    monkeypatch.setattr(window_module, "running_auto_uv_profile_info", lambda: {})
    monkeypatch.setattr(
        window_module, "penguin_burner_runtime_is_active", lambda: False
    )
    monkeypatch.setattr(
        window_module, "silent_fan_curve_from_runtime_config", lambda: False
    )
    monkeypatch.setattr(
        window_module, "ensure_daemon_ready_for_privileged_action", lambda **_kw: True
    )
    monkeypatch.setattr(window_module, "select_scan_tuning", lambda **_kw: options)
    monkeypatch.setattr(window_module, "persist_runtime_gpu_index", lambda index: index)
    monkeypatch.setattr(window_module, "scan_command", lambda _opts: ["simulated-scan"])
    modules = import_qt()
    monkeypatch.setattr(modules[2].QDialog, "exec", lambda _self: 0)
    windows = []

    class Controller:
        def is_running(self):
            return False

        def start(self, _command):
            win = windows[-1]

            def event(name, payload):
                win._handle_scan_event({"event": name, **payload})
                if name == "scan_resumed":
                    restored.append(payload)

            main.run_voltage_frequency_undervolt_main_loop(
                gpu_index=0,
                runtime_options=options,
                q2rtx_config=Q2RTXStabilityConfig(),
                event_callback=event,
                log=lambda _message: None,
            )
            return True

    try:
        first = MainWindow(modules)
        windows.append(first)
        first.scan_controller = Controller()
        assert not calls  # Opening the app does no scan work.
        with pytest.raises((SystemExit, AutoUvCriticalProbeError), match="simulated"):
            first.start_scan()
        assert scan_checkpoint_path().is_file()
        before_retry = list(calls)
        curve[16]["base_mhz"] += base_shift
        curve[16]["target_mhz"] += base_shift
        expected_calls = [*before_retry, *(before_retry[:2] if base_shift else []), (860, 2745)]
        second = MainWindow(modules)
        windows.append(second)
        second.scan_controller = Controller()
        assert calls == before_retry and not finals
        assert second.runs_table.widget.rowCount() == 0
        second.start_scan()  # The explicit click is the only resume trigger.
        assert calls == expected_calls  # No voltage sweep repeats.
        assert workloads[-1].duration_s == duration - round(duration * 0.25)
        assert "--duration-seconds" in workloads[-1].companion_command
        assert str(round(duration * 0.25)) in workloads[-1].companion_command
        assert len(restored) == len(finals) == 1
        assert (finals[0]["stable_voltage_mv"], finals[0]["stable_lock_clock_mhz"]) == (
            860,
            2745,
        )
        assert finals[0]["final_verification_duration_s"] == duration
        assert finals[0]["translated_gpu_policy"]["power_limit_w"] == 279
        assert finals[0]["translated_gpu_policy"]["mem_clk_vf_offset_mhz"] == 0
        assert load_unsafe_voltage_blacklist()[0]["candidate_voltage_mv"] == 850
        assert scan_checkpoint_path().exists() == (mode == "adaptive")
        if mode == "adaptive":
            # A later tier's unavailable GPU leaves the verified Efficiency profile
            # and its checkpoint. Another click must not repeat that long check.
            second.start_scan()
            assert len(finals) == 1
            assert calls == expected_calls
            assert second.auto_uv_tier_progress.state("efficiency") == "complete"
            assert second.vf_plot.comparison_curves
            volts, clocks = second.vf_plot.comparison_curves[0].getData()
            assert clocks[list(volts).index(860)] == 2745
    finally:
        for win in windows:
            win.window.close()


@pytest.mark.parametrize("change", ["gpu", "settings", "malformed", "profile"])
def test_incompatible_checkpoint_is_not_reused(tmp_path, change):
    identity = {"gpu": "one", "options": {"auto_uv_mode": "efficiency"}}
    path = tmp_path / "scan.json"
    initial = ScanCheckpoint(
        identity=identity, callback=None, log=lambda _s: None, path=path
    )
    initial.record("completed", 1)
    if change == "gpu":
        identity["gpu"] = "two"
    elif change == "settings":
        identity["options"]["auto_uv_mode"] = "balanced"
    elif change == "malformed":
        path.write_text("{broken")
    else:
        payload = json.loads(path.read_text())
        payload["profiles"] = {"removed.json": "old-hash"}
        path.write_text(json.dumps(payload))
    resumed = ScanCheckpoint(
        identity=identity, callback=None, log=lambda _s: None, path=path
    )
    assert not resumed.resuming
    assert resumed.lookup("completed") is None


def test_recovery_requires_both_margins_and_an_actual_voltage_bin():
    curve = rtx_5080_20260524_high_oc_base_curve()
    point = candidate(curve)
    passed = [{"tier": "efficiency", "candidate": point}]
    recovery = recovery_candidate(
        curve, passed, tier="efficiency", unsafe=[], failed_clock_mhz=2800
    )
    assert (recovery.voltage_mv, recovery.target_mhz) == (860, 2745)
    with pytest.raises(AutoUvError, match="no passed candidate"):
        recovery_candidate(
            curve, passed, tier="balanced", unsafe=[], failed_clock_mhz=2800
        )
    # A failure at the same requested clock is the normal end of a fixed-clock
    # descent: the pass one voltage bin above it is the safe source.
    same_clock = recovery_candidate(
        curve, passed, tier="efficiency", unsafe=[], failed_clock_mhz=2745,
        failed_voltage_mv=845,
    )
    assert (same_clock.voltage_mv, same_clock.target_mhz) == (860, 2745)
    for failed_voltage_mv in (None, 850, 860):
        with pytest.raises(AutoUvError, match="no passed candidate"):
            recovery_candidate(
                curve, passed, tier="efficiency", unsafe=[], failed_clock_mhz=2745,
                failed_voltage_mv=failed_voltage_mv,
            )


def test_resume_verification_failure_never_falls_back_to_an_unadjusted_point(
    monkeypatch,
):
    from auto_uv import main_loop as main

    calls = []

    def fail(**_kwargs):
        calls.append("resume")
        raise AutoUvError("final long verification failed: device lost")

    monkeypatch.setattr(main, "run_final_verification_and_save", fail)
    monkeypatch.setattr(
        main,
        "choose_next_candidate_after_final_failure",
        lambda **_kw: pytest.fail("must stop after failed recovery verification"),
    )
    selection = main.FinalScanCandidate(
        [], 860, 2745, None, 60, {"resume_recovery": True}, 0
    )
    with pytest.raises(AutoUvCriticalProbeError, match="Resume verification failed"):
        main.run_final_verification_with_fallbacks(
            selection=selection,
            stable_history=[],
            auto_uv_mode="efficiency",
            log=lambda _s: None,
        )
    assert calls == ["resume"]


def test_interrupted_resume_verification_is_blacklisted():
    from auto_uv.persistence.interrupted_probe_crash_cache import (
        consume_interrupted_probe_crash_marker,
    )

    write_probe_in_progress_marker(
        candidate_voltage_mv=860,
        lock_clock_mhz=2745,
        phase="resume-verify",
        details={"blocked_lock_clock_mhz": [2745, 2730, 2715]},
    )
    assert consume_interrupted_probe_crash_marker() is not None
    assert load_unsafe_voltage_blacklist()[0]["candidate_voltage_mv"] == 860


def test_failed_checkpoint_write_preserves_previous_durable_progress(
    monkeypatch, tmp_path
):
    from auto_uv.persistence import scan_checkpoint as module

    path = tmp_path / "checkpoint.json"
    checkpoint = ScanCheckpoint(
        identity={}, callback=None, log=lambda _s: None, path=path
    )
    checkpoint.record("first", 1)
    before = path.read_bytes()

    def fail(*_a):
        raise OSError("disk full")

    monkeypatch.setattr(module, "safe_json_write", fail)
    with pytest.raises(AutoUvCriticalProbeError, match="Cannot save Auto-UV resume checkpoint"):
        checkpoint.record("second", 2)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "reason, retained", [("discarded", False), ("baseline-failed", True)]
)
def test_finished_checkpoint_distinguishes_discard_from_failed_tier(
    tmp_path, reason, retained
):
    checkpoint = ScanCheckpoint(
        identity={},
        callback=None,
        log=lambda _s: None,
        path=tmp_path / "checkpoint.json",
    )
    for tier in ("efficiency", "balanced"):
        checkpoint.event("tier_completed", {"tier": tier})
    checkpoint.event("tier_skipped", {"tier": "performance", "reason": reason})
    checkpoint.finish("done", adaptive=True)
    assert checkpoint.path.exists() is retained


def test_completed_tier_cannot_bypass_new_blacklist(tmp_path):
    from auto_uv.domain.types import AutoUvVoltageScanResult

    identity = {"options": {"auto_uv_mode": "adaptive"}}
    path = tmp_path / "checkpoint.json"
    checkpoint = ScanCheckpoint(
        identity=identity, callback=None, log=lambda _s: None, path=path
    )
    checkpoint.record(
        {"completed_tier": "efficiency"},
        AutoUvVoltageScanResult(True, 850, 2775, "verified", None, []),
        profiles=True,
    )
    record_unsafe_voltage(
        candidate_voltage_mv=850,
        lock_clock_mhz=2800,
        blocked_lock_clock_mhz=[2800, 2782, 2760],
        reason="device lost",
    )
    resumed = ScanCheckpoint(
        identity=identity, callback=None, log=lambda _s: None, path=path
    )
    with pytest.raises(AutoUvCriticalProbeError, match="now blacklisted"):
        resumed.completed_tier("efficiency")


@pytest.mark.parametrize(
    "mode, expected",
    [(None, "efficiency"), ("aggressive", "performance"), ("all", "efficiency")],
)
def test_checkpoint_uses_the_same_cli_mode_aliases(tmp_path, mode, expected):
    checkpoint = ScanCheckpoint(
        identity={"options": {"auto_uv_mode": mode}},
        callback=None,
        log=lambda _s: None,
        path=tmp_path / "checkpoint.json",
    )
    assert checkpoint.current_tier == expected
