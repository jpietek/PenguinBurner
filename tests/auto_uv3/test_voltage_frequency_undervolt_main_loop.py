from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from auto_uv3.auto_uv_types import (
    AutoUvProbeSummary,
    ClockRecoveryBudget,
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv3 import voltage_frequency_undervolt_main_loop as undervolt_main_loop
from auto_uv3.voltage_sweep_state import (
    LowerVoltageSweepResult,
    VoltageProbeOutcome,
    VoltageSweepState,
)
from auto_uv3_test_data import base_curve


def _summary(voltage_mv: int, clock_mhz: int) -> AutoUvProbeSummary:
    return AutoUvProbeSummary(
        candidate_voltage_mv=int(voltage_mv),
        lock_clock_mhz=int(clock_mhz),
        live_voltage_before_mv=int(voltage_mv),
        live_voltage_after_mv=int(voltage_mv),
        avg_voltage_mv=float(voltage_mv),
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=100.0,
        min_fps=100.0,
        max_fps=100.0,
        avg_power_w=200.0,
        max_power_w=210.0,
        avg_temperature_c=60.0,
        max_temperature_c=62.0,
        avg_fan_speed_pct=35.0,
        max_fan_speed_pct=36.0,
        avg_core_clock_mhz=float(clock_mhz),
        efficiency_fps_per_w=0.5,
        efficiency_mhz_per_w=10.0,
        watts_per_mhz=0.1,
        used_companion_load=False,
        result_reason="stable run",
        log_path=Path("/tmp/q2rtx.log"),
    )


def test_discovery_probe_runner_uses_live_voltage_reader_keyword(monkeypatch) -> None:
    captured = {}

    class FakeGpu:
        reader = object()
        live_voltage_reader = object()
        runtime_default_plan = []
        power_limit_w = 360

    class FakeRunner:
        def __init__(self, *, reader, live_voltage_reader, **kwargs):
            captured["reader"] = reader
            captured["live_voltage_reader"] = live_voltage_reader
            captured["kwargs"] = kwargs

        def probe_default_curve(self, *, base_curve, label_voltage_mv, label_clock_mhz):
            captured["base_curve"] = base_curve
            captured["label_voltage_mv"] = label_voltage_mv
            captured["label_clock_mhz"] = label_clock_mhz
            return object(), type("Result", (), {"success": True, "reason": "ok"})()

    monkeypatch.setattr(undervolt_main_loop, "Q2RtxCudaProbeRunner", FakeRunner)
    monkeypatch.setattr(
        undervolt_main_loop,
        "emit_ui_json_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "probe_summary_ui_payload",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(undervolt_main_loop, "log_benchmark", lambda *args, **kwargs: None)

    undervolt_main_loop.run_discovery_probe(
        base_curve(900, 1025, 25, 2000, 40),
        gpu=FakeGpu(),
        q2rtx_config=object(),
        short_probe_base_duration_s=10,
        timedemo_warmup_runs=0,
        log=lambda _message: None,
        event_callback=None,
    )

    assert captured["live_voltage_reader"] is FakeGpu.live_voltage_reader
    assert captured["label_voltage_mv"] == 1000


def test_auto_uv3_final_choice_runs_before_final_verification(monkeypatch) -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    captured: dict[str, object] = {}

    class FakeGpu:
        reader = object()
        live_voltage_reader = object()
        runtime_default_plan = curve
        power_limit_w = 320
        clock_ceiling = None
        translated_gpu_policy = {}

        def start_clock_ceiling(self, _target) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeRunner:
        def __init__(self, **_kwargs):
            return None

        def probe_baseline_candidate(self, candidate):
            return VoltageProbeOutcome(
                decision=StableRunDecision(
                    True,
                    FailureKind.NONE,
                    FailureSeverity.PASS,
                    "stable run",
                ),
                measured_core_clock_mhz=float(candidate.target_mhz),
                measured_voltage_mv=float(candidate.voltage_mv),
                raw_probe=_summary(candidate.voltage_mv, candidate.target_mhz),
            )

    def fake_sweep_loop(_base_curve, *, settings, initial_stable_candidate, hooks, unsafe_entries):
        _ = settings, initial_stable_candidate, unsafe_entries
        candidate = VfCurveCandidate(
            label="lower-voltage recovery-budget=1.20/1.20%",
            voltage_mv=950,
            target_mhz=2120,
            flattened_plan=curve,
        )
        outcome = VoltageProbeOutcome(
            decision=StableRunDecision(
                True,
                FailureKind.NONE,
                FailureSeverity.PASS,
                "stable run",
            ),
            measured_core_clock_mhz=2120.0,
            measured_voltage_mv=950.0,
            raw_probe=_summary(950, 2120),
        )
        hooks.write_verified_candidate(candidate, outcome)
        return LowerVoltageSweepResult(
            stable_candidate=candidate,
            state=VoltageSweepState(
                stable_voltage_mv=950,
                stable_target_mhz=2120,
                next_voltage_mv=None,
                recovery_budget=ClockRecoveryBudget(used_pct=1.2, limit_pct=1.2),
            ),
        )

    def fake_choice(**kwargs):
        captured["choice_called"] = True
        return (
            kwargs["stable_plan"],
            kwargs["stable_voltage_mv"],
            kwargs["stable_lock_clock_mhz"],
            kwargs["stable_probe"],
            180,
        )

    def fake_final(**kwargs):
        captured["final_duration_s"] = kwargs["final_verification_duration_s"]
        captured["final_budget_used_pct"] = kwargs["clock_bump_budget_used_pct"]
        return "final-result"

    monkeypatch.setattr(
        undervolt_main_loop,
        "read_scan_runtime_settings",
        lambda runtime_options, q2rtx_config: SimpleNamespace(
            q2rtx_config=q2rtx_config,
            clock_bump_budget_limit_pct=1.2,
            auto_uv_mode="efficiency",
            timedemo_warmup_runs=0,
            short_probe_base_duration_s=10,
            configured_max_drop_pct=15.0,
            preserve_base_below_mv=None,
            min_performance_core_clock_pct=90.0,
            final_verification_duration_s=600,
            final_clock_drop_margin_pct=10.0,
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "consume_crash_cache", lambda **_kwargs: [])
    monkeypatch.setattr(
        undervolt_main_loop,
        "recovery_budget_limit_after_crash_cache",
        lambda *_args, **_kwargs: 1.2,
    )
    monkeypatch.setattr(undervolt_main_loop, "cleanup_managed_q2rtx_processes", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(undervolt_main_loop, "open_live_gpu_vf_curve_applier", lambda **_kwargs: FakeGpu())
    monkeypatch.setattr(
        undervolt_main_loop,
        "run_discovery_probe",
        lambda *_args, **_kwargs: (_summary(1000, 2200), SimpleNamespace(success=True)),
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "build_loaded_baseline_candidate",
        lambda *_args, **_kwargs: (
            VfCurveCandidate("baseline", 1000, 2200, curve),
            SimpleNamespace(measured_clock_mhz=2200.0),
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "Q2RtxCudaProbeRunner", FakeRunner)
    monkeypatch.setattr(
        undervolt_main_loop,
        "adjust_baseline_to_measured_clock",
        lambda _base_curve, *, candidate, **_kwargs: candidate,
    )
    monkeypatch.setattr(undervolt_main_loop, "write_verified_candidate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(undervolt_main_loop, "run_lower_voltage_sweep_loop", fake_sweep_loop)
    monkeypatch.setattr(undervolt_main_loop, "choose_final_verification_candidate", fake_choice)
    monkeypatch.setattr(undervolt_main_loop, "run_final_verification_and_save", fake_final)

    result = undervolt_main_loop.run_voltage_frequency_undervolt_main_loop(
        gpu_index=0,
        runtime_options={"auto_uv_require_final_choice": True},
        q2rtx_config=object(),
        log=lambda _message: None,
    )

    assert result == "final-result"
    assert captured["choice_called"] is True
    assert captured["final_duration_s"] == 180
    assert captured["final_budget_used_pct"] == 1.2


def test_auto_uv3_user_stop_offers_stable_history_for_final_choice(monkeypatch) -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    captured: dict[str, object] = {}

    class FakeGpu:
        reader = object()
        live_voltage_reader = object()
        runtime_default_plan = curve
        power_limit_w = 320
        clock_ceiling = None
        translated_gpu_policy = {}

        def start_clock_ceiling(self, _target) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeRunner:
        def __init__(self, **_kwargs):
            return None

        def probe_baseline_candidate(self, candidate):
            return VoltageProbeOutcome(
                decision=StableRunDecision(
                    True,
                    FailureKind.NONE,
                    FailureSeverity.PASS,
                    "stable run",
                ),
                measured_core_clock_mhz=float(candidate.target_mhz),
                measured_voltage_mv=float(candidate.voltage_mv),
                raw_probe=_summary(candidate.voltage_mv, candidate.target_mhz),
            )

    def fake_sweep_loop(*_args, **_kwargs):
        raise KeyboardInterrupt()

    def fake_choice(**kwargs):
        captured["choice_called"] = True
        captured["request_reason"] = kwargs["request_reason"]
        captured["history"] = [
            (
                int(probe.candidate_voltage_mv),
                int(probe.lock_clock_mhz),
            )
            for probe in kwargs["stable_history"]
        ]
        return (
            kwargs["stable_plan"],
            kwargs["stable_voltage_mv"],
            kwargs["stable_lock_clock_mhz"],
            kwargs["stable_probe"],
            240,
        )

    def fake_final(**kwargs):
        captured["final_duration_s"] = kwargs["final_verification_duration_s"]
        captured["final_budget_used_pct"] = kwargs["clock_bump_budget_used_pct"]
        return "final-result"

    monkeypatch.setattr(
        undervolt_main_loop,
        "read_scan_runtime_settings",
        lambda runtime_options, q2rtx_config: SimpleNamespace(
            q2rtx_config=q2rtx_config,
            clock_bump_budget_limit_pct=1.2,
            auto_uv_mode="performance",
            timedemo_warmup_runs=0,
            short_probe_base_duration_s=10,
            configured_max_drop_pct=15.0,
            preserve_base_below_mv=None,
            min_performance_core_clock_pct=90.0,
            final_verification_duration_s=600,
            final_clock_drop_margin_pct=10.0,
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "consume_crash_cache", lambda **_kwargs: [])
    monkeypatch.setattr(
        undervolt_main_loop,
        "recovery_budget_limit_after_crash_cache",
        lambda *_args, **_kwargs: 1.2,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "cleanup_managed_q2rtx_processes",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "open_live_gpu_vf_curve_applier",
        lambda **_kwargs: FakeGpu(),
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "run_discovery_probe",
        lambda *_args, **_kwargs: (_summary(1000, 2200), SimpleNamespace(success=True)),
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "build_loaded_baseline_candidate",
        lambda *_args, **_kwargs: (
            VfCurveCandidate("baseline", 1000, 2200, curve),
            SimpleNamespace(measured_clock_mhz=2200.0),
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "Q2RtxCudaProbeRunner", FakeRunner)
    monkeypatch.setattr(
        undervolt_main_loop,
        "adjust_baseline_to_measured_clock",
        lambda _base_curve, *, candidate, **_kwargs: candidate,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "write_verified_candidate",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(undervolt_main_loop, "run_lower_voltage_sweep_loop", fake_sweep_loop)
    monkeypatch.setattr(undervolt_main_loop, "choose_final_verification_candidate", fake_choice)
    monkeypatch.setattr(undervolt_main_loop, "run_final_verification_and_save", fake_final)
    monkeypatch.setattr(
        undervolt_main_loop,
        "clear_auto_uv_stop_request",
        lambda: captured.setdefault("stop_request_cleared", True),
    )

    result = undervolt_main_loop.run_voltage_frequency_undervolt_main_loop(
        gpu_index=0,
        runtime_options={"auto_uv_require_final_choice": True},
        q2rtx_config=object(),
        log=lambda _message: None,
    )

    assert result == "final-result"
    assert captured["choice_called"] is True
    assert captured["request_reason"] == "user-stop"
    assert captured["stop_request_cleared"] is True
    assert captured["history"] == [(1000, 2200)]
    assert captured["final_duration_s"] == 240
    assert captured["final_budget_used_pct"] == 0.0
