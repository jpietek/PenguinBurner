from __future__ import annotations

from dataclasses import MISSING, fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_uv.domain.types import (
    AutoUvCriticalProbeError,
    AutoUvProbeSummary,
    FailureKind,
    FailureSeverity,
    StableRunDecision,
)
from auto_uv.probes.runner import AutoUvProbeRunner
from auto_uv.run import performance_comparison as comparison
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome
from cli.final_choice_text import format_cli_final_choice_request
from stability.q2rtx.models import Q2RTXStabilityConfig
from ui.features.auto_uv.candidate_choice import (
    candidate_record_from_probe,
    candidate_selection_summary,
    candidate_short_verification_duration_s,
)


def summary(voltage, clock, fps, power, *, tail=4):
    required = {
        f.name: None
        for f in fields(AutoUvProbeSummary)
        if f.default is MISSING and f.default_factory is MISSING
    }
    required.update(
        candidate_voltage_mv=voltage,
        lock_clock_mhz=clock,
        avg_fps=fps,
        avg_power_w=power,
        avg_core_clock_mhz=clock + 15 * tail,
        used_companion_load=True,
        result_reason="ok",
        log_path=Path("probe.log"),
        tested_plan=[
            {
                "index": i,
                "voltage_mv": voltage + i * 6,
                "base_mhz": clock - 100 + 15 * i,
                "target_mhz": clock + 15 * min(i, tail),
            }
            for i in range(6)
        ],
    )
    return AutoUvProbeSummary(**required)


def reported_pair(tail=4):
    return summary(950, 1785, 82.002, 192.06, tail=tail), summary(
        925, 1815, 81.315, 183.91, tail=tail
    )


class Runner:
    q2rtx_config = Q2RTXStabilityConfig(gpu_index=2, width=1920, height=1080)

    def __init__(self, pair, fps):
        self.pair = pair
        self.fps = iter(fps)
        self.calls = []
        self.fail = None

    def probe_candidate(self, candidate, **kwargs):
        self.calls.append((candidate, kwargs))
        source = next(
            p for p in self.pair if p.candidate_voltage_mv == candidate.voltage_mv
        )
        value = next(self.fps)
        probe = replace(source, avg_fps=value)
        kind = self.fail or FailureKind.NONE
        decision = StableRunDecision(
            not self.fail,
            kind,
            FailureSeverity.UNSAFE if self.fail else FailureSeverity.PASS,
            "failure" if self.fail else "ok",
        )
        return VoltageProbeOutcome(decision, raw_probe=probe)


@pytest.mark.parametrize("tail", [2, 4])
def test_3070_close_fps_rechecked_without_changing_tested_tail(monkeypatch, tail):
    monkeypatch.setattr(comparison, "load_unsafe_voltage_blacklist", lambda: [])
    pair = reported_pair(tail)
    assert comparison.performance_comparison_pair(list(pair)) == pair
    runner = Runner(pair, [82.0, 83.1, 83.3, 82.2])
    ceilings = []
    history = []
    logs = []
    winner, compared = comparison.compare_performance_finalists(
        pair,
        runner=runner,
        clock_ceiling=SimpleNamespace(
            retarget=lambda **kw: ceilings.append(kw),
            describe=lambda: "test ceiling",
        ),
        tail_rise_bins=tail,
        probe_history=history,
        log=logs.append,
    )
    assert winner.candidate_voltage_mv == 925
    assert winner.avg_fps == pytest.approx(83.2)
    assert compared[0].avg_fps == pytest.approx(82.1)
    assert [c.voltage_mv for c, _ in runner.calls] == [950, 925, 925, 950]
    assert len(history) == 4
    for (candidate, options), ceiling in zip(runner.calls, ceilings):
        original = next(
            p for p in pair if p.candidate_voltage_mv == candidate.voltage_mv
        )
        assert candidate.flattened_plan == original.tested_plan
        assert ceiling["ceiling_clock_mhz"] == candidate.target_mhz + 15 * tail
        config = options["config_override"]
        assert (config.duration_s, config.width, config.height, config.gpu_index) == (
            30,
            1920,
            1080,
            2,
        )
        assert config.companion_command[-1] == "5"
        assert options["phase_label"] == "candidate"  # normal crash-marker/unsafe path
        assert options["stable_history"] == list(pair)  # retain recovery context
    assert winner.comparison_runs == 2
    assert "long final verification still required" in logs[-1]


@pytest.mark.parametrize(
    "fps,expected", [([83.0, 82.9, 82.9, 83.0], 950), ([83, 83, 83, 83], 950)]
)
def test_small_real_fps_gain_and_exact_tie_keep_original_winner(
    monkeypatch, fps, expected
):
    monkeypatch.setattr(comparison, "load_unsafe_voltage_blacklist", lambda: [])
    pair = reported_pair()
    winner, _ = comparison.compare_performance_finalists(
        pair,
        runner=Runner(pair, fps),
        clock_ceiling=None,
        tail_rise_bins=4,
        probe_history=[],
        log=lambda _: None,
    )
    assert winner.candidate_voltage_mv == expected


def test_aggressive_gain_and_clear_fps_lead_do_not_trigger_extra_probes():
    standard = summary(925, 2950, 100, 300, tail=2)
    aggressive = summary(925, 2950, 100.4, 302, tail=4)
    assert comparison.performance_comparison_pair([standard, aggressive]) is None
    leader, rival = reported_pair()
    assert (
        comparison.performance_comparison_pair([replace(leader, avg_fps=90), rival])
        is None
    )
    assert (
        comparison.performance_comparison_pair(
            [leader, replace(rival, tested_plan=None)]
        )
        is None
    )


@pytest.mark.parametrize("metric", [None, float("nan"), float("inf"), 0, -1])
def test_invalid_metrics_never_enter_comparison(metric):
    a, b = reported_pair()
    assert (
        comparison.performance_comparison_pair([a, replace(b, avg_fps=metric)]) is None
    )


@pytest.mark.parametrize(
    "kind",
    [
        FailureKind.CUDA_FAILED,
        FailureKind.NVIDIA_XID,
        FailureKind.METRICS_MISSING,
        FailureKind.USER_STOP,
    ],
)
def test_failed_recheck_cannot_recommend_stale_pass(monkeypatch, kind):
    monkeypatch.setattr(comparison, "load_unsafe_voltage_blacklist", lambda: [])
    pair = reported_pair()
    runner = Runner(pair, [82])
    runner.fail = kind
    error = (
        KeyboardInterrupt if kind is FailureKind.USER_STOP else AutoUvCriticalProbeError
    )
    with pytest.raises(error):
        comparison.compare_performance_finalists(
            pair,
            runner=runner,
            clock_ceiling=None,
            tail_rise_bins=4,
            probe_history=[],
            log=lambda _: None,
        )
    assert len(runner.calls) == 1


def test_blacklist_rechecked_before_every_launch(monkeypatch):
    monkeypatch.setattr(comparison, "load_unsafe_voltage_blacklist", lambda: [])
    blocks = iter(["", "new unsafe band"])
    monkeypatch.setattr(
        comparison, "unsafe_voltage_block_reason", lambda *a, **k: next(blocks)
    )
    pair = reported_pair()
    runner = Runner(pair, [82])
    with pytest.raises(AutoUvCriticalProbeError, match="new unsafe band"):
        comparison.compare_performance_finalists(
            pair,
            runner=runner,
            clock_ceiling=None,
            tail_rise_bins=4,
            probe_history=[],
            log=lambda _: None,
        )
    assert len(runner.calls) == 1


def test_comparison_duration_and_recommendation_survive_cli_handoff():
    probe = replace(reported_pair()[1], comparison_runs=2, comparison_duration_s=35)
    record = candidate_record_from_probe(
        probe,
        base_curve=[],
        stable_plan=[],
        stable_voltage_mv=925,
        stable_lock_clock_mhz=1815,
        tail_rise_bins=4,
    )
    assert record is not None
    duration = candidate_short_verification_duration_s(
        record, initial_target_voltage_mv=1025, use_recorded_duration=False
    )
    assert duration == 70
    row = candidate_selection_summary(record, short_verification_duration_s=duration)
    payload = {
        "auto_uv_mode": "performance",
        "request_reason": "performance-comparison",
        "default_candidate_id": row["candidate_id"],
        "candidates": [
            dict(row, candidate_id="old-short", avg_fps=999, comparison_runs=0),
            row,
        ],
    }
    text = "\n".join(format_cli_final_choice_request(payload))
    assert "Comparison winner" in text and "Mean of 2 matched runs" in text
    assert "1min 10s" in text


def test_probe_runner_uses_same_comparison_config_for_different_voltage_bands(
    monkeypatch,
):
    runner = AutoUvProbeRunner(
        reader=object(),
        live_voltage_reader=object(),
        q2rtx_config=Q2RTXStabilityConfig(),
        runtime_default_plan=[],
        power_limit_w=220,
        start_voltage_mv=1025,
        baseline_clock_mhz=1800,
        short_probe_base_duration_s=10,
        log=lambda _: None,
    )
    configs = []

    def probe(**kwargs):
        configs.append(kwargs["q2rtx_config"])
        return reported_pair()[0], object()

    monkeypatch.setattr(
        AutoUvProbeRunner, "_probe", lambda self, **kwargs: probe(**kwargs)
    )
    monkeypatch.setattr(
        AutoUvProbeRunner,
        "outcome_from_probe_result",
        lambda self, s, r, **kw: VoltageProbeOutcome(
            StableRunDecision(True, FailureKind.NONE, FailureSeverity.PASS, "ok"),
            raw_probe=s,
        ),
    )
    from auto_uv.domain.types import VfCurveCandidate

    config = Q2RTXStabilityConfig(duration_s=30)
    for voltage in [1000, 800]:
        runner.probe_candidate(
            VfCurveCandidate("test", voltage, 1800, []),
            stable_history=[],
            phase_label="candidate",
            summarize_saturated_tail=False,
            use_power_limit_floor=False,
            use_companion_load=True,
            config_override=config,
        )
    assert configs == [config, config]


@pytest.mark.parametrize(
    "mode,reason,expected_runs",
    [
        ("performance", "sweep-complete", 4),
        ("performance", "adaptive-performance", 4),
        ("performance", "user-stop", 0),
        ("balanced", "adaptive-balanced", 0),
        ("efficiency", "sweep-complete", 0),
    ],
)
def test_selection_pipeline_only_rechecks_normal_performance(
    monkeypatch, mode, reason, expected_runs
):
    from auto_uv import main_loop
    from auto_uv.domain.types import VfCurveCandidate

    monkeypatch.setattr(comparison, "load_unsafe_voltage_blacklist", lambda: [])
    monkeypatch.setattr(main_loop, "load_unsafe_voltage_blacklist", lambda: [])
    pair = reported_pair()
    runner = Runner(pair, [82, 83, 83, 82])
    leader = pair[0]
    result = main_loop.select_final_scan_candidate(
        base_curve=[],
        settings=SimpleNamespace(auto_uv_mode=mode, short_probe_base_duration_s=10),
        runtime_options={},
        stable_plan=leader.tested_plan,
        stable_voltage_mv=950,
        stable_lock_clock_mhz=1785,
        stable_probe=leader,
        stable_history=list(pair),
        runner=runner,
        gpu=SimpleNamespace(
            translated_gpu_policy={"gpu_name": "NVIDIA GeForce RTX 3070"},
            clock_ceiling=None,
        ),
        probe_history=[],
        log=lambda _: None,
        tail_rise_bins=4,
        measured_baseline_clock_mhz=1792.5,
        discovery_summary=leader,
        baseline_candidate=VfCurveCandidate("baseline", 1025, 1785, []),
        final_verification_duration_s=300,
        event_callback=None,
        run_performance_auto_oc=False,
        request_reason=reason,
    )
    assert len(runner.calls) == expected_runs
    assert result.verification_duration_s == 300
    if expected_runs:
        assert (result.voltage_mv, result.lock_clock_mhz) == (925, 1815)
        assert result.plan == pair[1].tested_plan


def test_missing_measurements_on_recheck_abort_instead_of_selecting(monkeypatch):
    monkeypatch.setattr(comparison, "load_unsafe_voltage_blacklist", lambda: [])
    pair = reported_pair()
    runner = Runner(pair, [float("nan")])
    with pytest.raises(AutoUvCriticalProbeError):
        comparison.compare_performance_finalists(
            pair,
            runner=runner,
            clock_ceiling=None,
            tail_rise_bins=4,
            probe_history=[],
            log=lambda _: None,
        )
    assert len(runner.calls) == 1
