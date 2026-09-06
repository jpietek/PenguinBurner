from __future__ import annotations

from pathlib import Path
from contextlib import nullcontext

import pytest

import auto_uv.auto_oc.search as auto_oc_search
from auto_uv.auto_oc.ladder import AutoOcStep, build_auto_oc_ladder
from auto_uv.auto_oc.scoring import auto_oc_probe_key
from auto_uv.auto_oc.search import run_auto_oc_candidate_search
from auto_uv.domain.types import (
    AutoUvCriticalProbeError,
    AutoUvProbeSummary,
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome
from auto_uv_test_data import base_curve, rtx_5080_20260524_high_oc_base_curve


def _probe(
    voltage_mv: int,
    clock_mhz: int,
    *,
    fps: float = 100.0,
    q2rtx_clock_mhz: float | None = None,
) -> AutoUvProbeSummary:
    return AutoUvProbeSummary(
        candidate_voltage_mv=int(voltage_mv),
        lock_clock_mhz=int(clock_mhz),
        live_voltage_before_mv=int(voltage_mv),
        live_voltage_after_mv=int(voltage_mv),
        avg_voltage_mv=float(voltage_mv),
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=float(fps),
        min_fps=float(fps),
        max_fps=float(fps),
        avg_power_w=200.0,
        max_power_w=210.0,
        avg_temperature_c=60.0,
        max_temperature_c=62.0,
        avg_fan_speed_pct=35.0,
        max_fan_speed_pct=36.0,
        avg_core_clock_mhz=float(q2rtx_clock_mhz or clock_mhz),
        efficiency_fps_per_w=0.5,
        efficiency_mhz_per_w=10.0,
        watts_per_mhz=0.1,
        used_companion_load=True,
        result_reason="stable run",
        log_path=Path("/tmp/q2rtx.log"),
        q2rtx_avg_core_clock_mhz=float(q2rtx_clock_mhz or clock_mhz),
        loaded_p90_core_clock_mhz=float(q2rtx_clock_mhz or clock_mhz),
    )


def _passed_outcome(probe: AutoUvProbeSummary) -> VoltageProbeOutcome:
    return VoltageProbeOutcome(
        decision=StableRunDecision(
            True,
            FailureKind.NONE,
            FailureSeverity.PASS,
            "stable run",
        ),
        measured_core_clock_mhz=probe.avg_core_clock_mhz,
        measured_voltage_mv=probe.avg_voltage_mv,
        raw_probe=probe,
        raw_result=object(),
    )


def _failed_outcome(probe: AutoUvProbeSummary) -> VoltageProbeOutcome:
    return VoltageProbeOutcome(
        decision=StableRunDecision(
            False,
            FailureKind.FPS_REGRESSION,
            FailureSeverity.RECOVERABLE,
            "benchmark average FPS below floor",
        ),
        measured_core_clock_mhz=probe.avg_core_clock_mhz,
        measured_voltage_mv=probe.avg_voltage_mv,
        raw_probe=probe,
        raw_result=object(),
    )


def test_auto_oc_ladder_interpolates_to_endpoint_without_exceeding_caps() -> None:
    curve = base_curve(875, 955, 5, 2400, 15)

    ladder = build_auto_oc_ladder(
        curve,
        start_voltage_mv=900,
        start_clock_mhz=2670,
        endpoint_voltage_mv=950,
        endpoint_clock_mhz=2745,
        max_steps=10,
    )

    assert 0 < len(ladder) <= 10
    assert (ladder[-1].voltage_mv, ladder[-1].target_mhz) == (950, 2745)
    assert all(step.voltage_mv <= 950 for step in ladder)
    assert all(step.target_mhz <= 2745 for step in ladder)
    assert all(step.target_mhz % 15 == 0 for step in ladder)
    assert len({(step.voltage_mv, step.target_mhz) for step in ladder}) == len(ladder)


def test_auto_oc_ladder_climbs_strictly_by_voltage_on_sparse_bins() -> None:
    curve = rtx_5080_20260524_high_oc_base_curve()

    ladder = build_auto_oc_ladder(
        curve,
        start_voltage_mv=870,
        start_clock_mhz=2806,
        endpoint_voltage_mv=915,
        endpoint_clock_mhz=2980,
        max_steps=10,
    )

    tried = [(step.voltage_mv, step.target_mhz) for step in ladder]
    assert tried == [
        (875, 2820),
        (885, 2865),
        (890, 2880),
        (895, 2910),
        (900, 2925),
        (910, 2940),
        (915, 2980),
    ]
    assert len({step.voltage_mv for step in ladder}) == len(ladder)


def test_auto_oc_ladder_can_reclaim_clock_at_fixed_voltage() -> None:
    curve = base_curve(850, 950, 5, 2400, 15)

    ladder = build_auto_oc_ladder(
        curve,
        start_voltage_mv=900,
        start_clock_mhz=2400,
        endpoint_voltage_mv=900,
        endpoint_clock_mhz=2700,
        max_steps=4,
    )

    assert [(step.voltage_mv, step.target_mhz) for step in ladder] == [
        (900, 2475),
        (900, 2550),
        (900, 2625),
        (900, 2700),
    ]


def test_auto_oc_probe_key_uses_q2rtx_clock_before_fps() -> None:
    lower_fps_higher_clock = _probe(950, 2745, fps=80.0, q2rtx_clock_mhz=2735.0)
    higher_fps_lower_clock = _probe(925, 2670, fps=120.0, q2rtx_clock_mhz=2680.0)

    assert auto_oc_probe_key(
        lower_fps_higher_clock,
        voltage_mv=950,
        step_index=2,
    ) > auto_oc_probe_key(
        higher_fps_lower_clock,
        voltage_mv=925,
        step_index=1,
    )


def test_auto_oc_clock_climb_skips_cached_unsafe_points(monkeypatch) -> None:
    monkeypatch.setattr(auto_oc_search, "load_unsafe_voltage_blacklist", lambda: [{
        "candidate_voltage_mv": 900, "lock_clock_mhz": 2401, "reason": "benchmark-crash",
    }])
    curve = base_curve(850, 950, 5, 2000, 15)
    start = VfCurveCandidate("start", 900, 2400, curve)

    class RejectRunner:
        def probe_candidate(self, *_args, **_kwargs):
            raise AssertionError("known unsafe point reached the GPU")

    result = run_auto_oc_candidate_search(
        base_curve=curve, start_candidate=start, start_probe=_probe(900, 2400),
        runner=RejectRunner(), gpu_name="NVIDIA GeForce RTX 5080", clock_ceiling=None,
        probe_history=[], log=lambda _: None, target_voltage_mv=900,
        target_clock_mhz=2800, target_profile_id="efficiency",
    )
    assert result.selected_candidate is start
    assert result.attempts
    assert all(a.outcome.decision.failure_kind is FailureKind.CACHED_UNSAFE for a in result.attempts)


def test_cached_unsafe_backoff_uses_existing_voltage_bin_and_passed_clock(monkeypatch) -> None:
    monkeypatch.setattr(auto_oc_search, "load_unsafe_voltage_blacklist", lambda: [{
        "candidate_voltage_mv": 950, "lock_clock_mhz": 2410, "reason": "benchmark-crash",
    }])
    curve = base_curve(850, 950, 20, 2400, 15)
    start = VfCurveCandidate("start", 850, 2400, curve)
    tried: list[tuple[int, int]] = []

    class FakeRunner:
        def probe_candidate(self, candidate, **_kwargs):
            tried.append((candidate.voltage_mv, candidate.target_mhz))
            return _passed_outcome(_probe(candidate.voltage_mv, candidate.target_mhz))

    result = run_auto_oc_candidate_search(
        base_curve=curve, start_candidate=start, start_probe=_probe(850, 2400),
        runner=FakeRunner(), gpu_name="NVIDIA GeForce RTX 5080", clock_ceiling=None,
        probe_history=[], log=lambda _: None, target_voltage_mv=925,
        target_clock_mhz=2950,
    )

    assert tried == [(910, 2400)]
    assert result.attempts[0].outcome.decision.failure_kind is FailureKind.CACHED_UNSAFE
    assert (result.selected_candidate.voltage_mv, result.selected_candidate.target_mhz) == (910, 2400)
    assert result.selected_candidate.flattened_plan
    assert result.selected_probe is result.attempts[-1].outcome.raw_probe


def test_auto_oc_search_climbs_voltage_and_clock_to_target() -> None:
    curve = base_curve(850, 950, 5, 2400, 15)
    start = VfCurveCandidate("undervolt-winner", 865, 2824, curve)
    tried: list[tuple[int, int]] = []

    class FakeRunner:
        def probe_candidate(self, candidate, **_kwargs):
            tried.append((candidate.voltage_mv, candidate.target_mhz))
            return _passed_outcome(
                _probe(
                    candidate.voltage_mv,
                    candidate.target_mhz,
                    q2rtx_clock_mhz=float(candidate.target_mhz),
                )
            )

    result = run_auto_oc_candidate_search(
        base_curve=curve,
        start_candidate=start,
        start_probe=_probe(865, 2824, q2rtx_clock_mhz=2824.0),
        runner=FakeRunner(),
        gpu_name="NVIDIA GeForce RTX 4090",
        clock_ceiling=None,
        probe_history=[],
        log=lambda _message: None,
        tail_rise_bins=0,
        max_interpolation_steps=10,
        target_voltage_mv=925,
        target_clock_mhz=2980,
        measured_baseline_clock_mhz=2735,
    )

    assert tried == [
        (870, 2835),
        (875, 2850),
        (885, 2865),
        (890, 2880),
        (895, 2895),
        (900, 2925),
        (905, 2940),
        (915, 2955),
        (920, 2970),
        (925, 2980),
    ]
    assert (result.selected_candidate.voltage_mv, result.selected_candidate.target_mhz) == (
        925,
        2980,
    )


def test_auto_oc_search_reclaims_clock_at_fixed_voltage_with_capped_history() -> None:
    curve = base_curve(850, 950, 5, 2400, 15)
    start = VfCurveCandidate("balanced-winner", 900, 2400, curve)
    baseline = _probe(1000, 2500, fps=90.0)
    descended = _probe(900, 2400, fps=92.0)
    tried: list[tuple[int, int]] = []
    received_histories: list[list[AutoUvProbeSummary]] = []

    class FakeRunner:
        def probe_candidate(self, candidate, **kwargs):
            tried.append((candidate.voltage_mv, candidate.target_mhz))
            received_histories.append(kwargs["stable_history"])
            return _passed_outcome(
                _probe(
                    candidate.voltage_mv,
                    candidate.target_mhz,
                    fps=93.0,
                    q2rtx_clock_mhz=float(candidate.target_mhz),
                )
            )

    result = run_auto_oc_candidate_search(
        base_curve=curve,
        start_candidate=start,
        start_probe=descended,
        runner=FakeRunner(),
        gpu_name="NVIDIA GeForce RTX 5090",
        clock_ceiling=None,
        probe_history=[],
        log=lambda _message: None,
        max_interpolation_steps=4,
        target_voltage_mv=900,
        target_clock_mhz=2700,
        target_profile_id="balanced",
        probe_stable_history=[baseline, descended],
    )

    assert tried == [
        (900, 2475),
        (900, 2550),
        (900, 2625),
        (900, 2700),
    ]
    assert all(history == [baseline, descended] for history in received_histories)
    assert (
        result.selected_candidate.voltage_mv,
        result.selected_candidate.target_mhz,
    ) == (900, 2700)


def test_auto_oc_search_skips_target_voltage_below_uv_candidate() -> None:
    curve = base_curve(850, 950, 5, 2400, 15)
    start = VfCurveCandidate("uv-winner", 925, 2824, curve)

    class FakeRunner:
        def probe_candidate(self, *_args, **_kwargs):
            raise AssertionError("lower-voltage Auto-OC target must not be probed")

    result = run_auto_oc_candidate_search(
        base_curve=curve,
        start_candidate=start,
        start_probe=_probe(925, 2824, q2rtx_clock_mhz=2824.0),
        runner=FakeRunner(),
        gpu_name="NVIDIA GeForce RTX 4090",
        clock_ceiling=None,
        probe_history=[],
        log=lambda _message: None,
        target_voltage_mv=900,
        target_clock_mhz=2980,
    )

    assert result.selected_candidate is start
    assert result.attempts == ()


def test_auto_oc_search_keeps_exploring_after_measured_clock_regression() -> None:
    curve = base_curve(875, 955, 5, 2400, 15)
    start = VfCurveCandidate("start", 925, 2670, curve)
    probes: list[AutoUvProbeSummary] = []
    tried: list[tuple[int, int]] = []

    class FakeRunner:
        def probe_candidate(self, candidate, **kwargs):
            assert kwargs["phase_label"] == "candidate"
            tried.append((candidate.voltage_mv, candidate.target_mhz))
            if len(tried) == 1:
                return _failed_outcome(
                    _probe(
                        candidate.voltage_mv,
                        candidate.target_mhz,
                        q2rtx_clock_mhz=2660.0,
                    )
                )
            return _passed_outcome(
                _probe(
                    candidate.voltage_mv,
                    candidate.target_mhz,
                    fps=70.0,
                    q2rtx_clock_mhz=(
                        2745.0 if candidate.target_mhz > 2715 else 2735.0
                    ),
                )
            )

    result = run_auto_oc_candidate_search(
        base_curve=curve,
        start_candidate=start,
        start_probe=_probe(925, 2670, q2rtx_clock_mhz=2670.0),
        runner=FakeRunner(),
        gpu_name="NVIDIA GeForce RTX 4090",
        clock_ceiling=None,
        probe_history=probes,
        log=lambda _message: None,
        tail_rise_bins=0,
        max_interpolation_steps=2,
        target_voltage_mv=950,
        target_clock_mhz=2745,
        measured_baseline_clock_mhz=2670,
    )

    assert tried == [(935, 2715), (940, 2715), (950, 2745)]
    assert (result.selected_candidate.voltage_mv, result.selected_candidate.target_mhz) == (
        950,
        2745,
    )
    assert [attempt.candidate.metadata["auto_oc_applied_mhz"] for attempt in result.attempts] == [
        45,
        45,
        75,
    ]
    assert {attempt.candidate.metadata["auto_oc_limit_mhz"] for attempt in result.attempts} == {
        75,
    }


def test_auto_oc_search_retries_failed_clock_at_higher_voltage_before_climbing() -> None:
    curve = base_curve(875, 955, 5, 2400, 15)
    start = VfCurveCandidate("start", 925, 2670, curve)
    tried: list[tuple[int, int]] = []

    class FakeRunner:
        def probe_candidate(self, candidate, **kwargs):
            _ = kwargs
            tried.append((candidate.voltage_mv, candidate.target_mhz))
            probe = _probe(
                candidate.voltage_mv,
                candidate.target_mhz,
                q2rtx_clock_mhz=2660.0,
            )
            if len(tried) > 1:
                return _passed_outcome(
                    _probe(
                        candidate.voltage_mv,
                        candidate.target_mhz,
                        q2rtx_clock_mhz=float(candidate.target_mhz),
                    )
                )
            return VoltageProbeOutcome(
                decision=StableRunDecision(
                    False,
                    FailureKind.FPS_REGRESSION,
                    FailureSeverity.RECOVERABLE,
                    "benchmark average FPS below floor",
                ),
                measured_core_clock_mhz=probe.avg_core_clock_mhz,
                measured_voltage_mv=probe.avg_voltage_mv,
                raw_probe=probe,
                raw_result=object(),
            )

    result = run_auto_oc_candidate_search(
        base_curve=curve,
        start_candidate=start,
        start_probe=_probe(925, 2670, q2rtx_clock_mhz=2670.0),
        runner=FakeRunner(),
        gpu_name="NVIDIA GeForce RTX 4090",
        clock_ceiling=None,
        probe_history=[],
        log=lambda _message: None,
        tail_rise_bins=0,
        max_interpolation_steps=2,
        target_voltage_mv=950,
        target_clock_mhz=2745,
        measured_baseline_clock_mhz=2670,
    )

    assert tried == [(935, 2715), (940, 2715), (950, 2745)]
    assert (result.selected_candidate.voltage_mv, result.selected_candidate.target_mhz) == (
        950,
        2745,
    )


def test_auto_oc_search_skips_more_mhz_until_failed_clock_is_stable(monkeypatch) -> None:
    curve = rtx_5080_20260524_high_oc_base_curve()
    start = VfCurveCandidate("start", 870, 2806, curve)
    tried: list[tuple[int, int]] = []
    log_messages: list[str] = []

    monkeypatch.setattr(
        auto_oc_search,
        "build_auto_oc_ladder",
        lambda *_args, **_kwargs: [
            AutoOcStep(1, 875, 2820, 0.1),
            AutoOcStep(2, 875, 2835, 0.2),
            AutoOcStep(3, 885, 2865, 0.3),
            AutoOcStep(4, 890, 2880, 0.4),
        ],
    )

    class FakeRunner:
        def probe_candidate(self, candidate, **_kwargs):
            tried.append((candidate.voltage_mv, candidate.target_mhz))
            if len(tried) == 1:
                probe = _probe(
                    candidate.voltage_mv,
                    candidate.target_mhz,
                    q2rtx_clock_mhz=2810.0,
                )
                return VoltageProbeOutcome(
                    decision=StableRunDecision(
                        False,
                        FailureKind.FPS_REGRESSION,
                        FailureSeverity.RECOVERABLE,
                        "benchmark average FPS below floor",
                    ),
                    measured_core_clock_mhz=probe.avg_core_clock_mhz,
                    measured_voltage_mv=probe.avg_voltage_mv,
                    raw_probe=probe,
                    raw_result=object(),
                )
            return _passed_outcome(
                _probe(
                    candidate.voltage_mv,
                    candidate.target_mhz,
                    q2rtx_clock_mhz=float(candidate.target_mhz),
                )
            )

    result = run_auto_oc_candidate_search(
        base_curve=curve,
        start_candidate=start,
        start_probe=_probe(870, 2806, q2rtx_clock_mhz=2806.0),
        runner=FakeRunner(),
        gpu_name="Custom GPU",
        clock_ceiling=None,
        probe_history=[],
        log=log_messages.append,
        tail_rise_bins=0,
        max_interpolation_steps=10,
        target_voltage_mv=915,
        target_clock_mhz=2980,
        measured_baseline_clock_mhz=2735,
    )

    assert tried == [(875, 2820), (885, 2820), (890, 2880)]
    assert (result.selected_candidate.voltage_mv, result.selected_candidate.target_mhz) == (
        890,
        2880,
    )
    assert (875, 2835) not in tried
    assert (885, 2865) not in tried


@pytest.mark.parametrize(
    "kind",
    [
        FailureKind.USER_STOP,
        FailureKind.METRICS_MISSING,
        FailureKind.METRICS_INVALID,
    ],
)
def test_auto_oc_search_aborts_without_retry_after_stop_or_critical_failure(
    kind,
) -> None:
    curve = base_curve(875, 955, 5, 2400, 15)
    start = VfCurveCandidate("start", 925, 2670, curve)
    tried: list[tuple[int, int]] = []

    class FakeRunner:
        def probe_candidate(self, candidate, **_kwargs):
            tried.append((candidate.voltage_mv, candidate.target_mhz))
            probe = _probe(
                candidate.voltage_mv,
                candidate.target_mhz,
                q2rtx_clock_mhz=2660.0,
            )
            return VoltageProbeOutcome(
                decision=StableRunDecision(
                    False,
                    kind,
                    FailureSeverity.CRITICAL,
                    kind.value,
                ),
                measured_core_clock_mhz=probe.avg_core_clock_mhz,
                measured_voltage_mv=probe.avg_voltage_mv,
                raw_probe=probe,
                raw_result=object(),
            )

    result = None
    with nullcontext() if kind is FailureKind.USER_STOP else pytest.raises(AutoUvCriticalProbeError):
        result = run_auto_oc_candidate_search(
            base_curve=curve,
            start_candidate=start,
            start_probe=_probe(925, 2670, q2rtx_clock_mhz=2670.0),
            runner=FakeRunner(),
            gpu_name="NVIDIA GeForce RTX 4090",
            clock_ceiling=None,
            probe_history=[],
            log=lambda _message: None,
            tail_rise_bins=0,
            max_interpolation_steps=2,
            target_voltage_mv=950,
            target_clock_mhz=2745,
            measured_baseline_clock_mhz=2670,
        )

    assert tried == [(935, 2715)]
    if kind is FailureKind.USER_STOP:
        assert result is not None and result.selected_candidate is start


def test_wall_limited_rung_is_not_adopted_and_stops_the_climb() -> None:
    # A power-bound card delivers at most the wall clock no matter what lock
    # the rung requests. The first rung whose request exceeds the wall by
    # more than the shortfall tolerance must not be adopted as capability
    # (its lock would overstate the curve), and the climb must stop there —
    # higher rungs can only measure the same wall.
    curve = base_curve(850, 955, 5, 2400, 15)
    start = VfCurveCandidate("reclaim-start", 900, 2415, curve)
    wall_mhz = 2450.0
    tried: list[int] = []

    class WallRunner:
        power_limit_w = 460

        def probe_candidate(self, candidate, **_kwargs):
            tried.append(int(candidate.target_mhz))
            measured = min(float(candidate.target_mhz), wall_mhz)
            probe = _probe(
                candidate.voltage_mv,
                candidate.target_mhz,
                q2rtx_clock_mhz=measured,
            )
            if measured < float(candidate.target_mhz):
                probe.perf_cap_reason = "sw-power"
                # A real wall shows up as measured power AT the limit, not as
                # a perf-cap reason alone.
                probe.avg_power_w = 458.0
            return _passed_outcome(probe)

    result = run_auto_oc_candidate_search(
        base_curve=curve,
        start_candidate=start,
        start_probe=_probe(900, 2415, q2rtx_clock_mhz=2415.0),
        runner=WallRunner(),
        gpu_name="NVIDIA GeForce RTX 4090",
        clock_ceiling=None,
        probe_history=[],
        log=lambda _message: None,
        target_voltage_mv=900,
        target_clock_mhz=2700,
    )

    selected = result.selected_candidate
    # The adopted lock never overstates the wall beyond the tolerance.
    assert float(selected.target_mhz) <= wall_mhz + 22.5
    # The climb stopped at the first wall-limited rung instead of walking
    # the remaining ladder to the 2700MHz endpoint.
    beyond_tolerance = [t for t in tried if t > wall_mhz + 22.5]
    assert len(beyond_tolerance) == 1
    assert max(tried) < 2700


def test_power_cap_reason_without_power_evidence_does_not_stop_the_climb() -> None:
    # Blackwell reports a power cap while hundreds of watts of headroom
    # remain, and always measures a little below its requested lock. If the
    # reason token alone ended the climb, every power-bound reclaim would
    # stop at its first rung and ship the capped stock clock.
    curve = base_curve(850, 955, 5, 2400, 15)
    start = VfCurveCandidate("reclaim-start", 900, 2415, curve)
    tried: list[int] = []

    class OffWallRunner:
        power_limit_w = 460

        def probe_candidate(self, candidate, **_kwargs):
            tried.append(int(candidate.target_mhz))
            probe = _probe(
                candidate.voltage_mv,
                candidate.target_mhz,
                # A droop shortfall past the wall tolerance, every rung.
                q2rtx_clock_mhz=float(candidate.target_mhz) - 40.0,
            )
            probe.perf_cap_reason = "sw-power"
            probe.avg_power_w = 300.0
            return _passed_outcome(probe)

    result = run_auto_oc_candidate_search(
        base_curve=curve,
        start_candidate=start,
        start_probe=_probe(900, 2415, q2rtx_clock_mhz=2415.0),
        runner=OffWallRunner(),
        gpu_name="NVIDIA GeForce RTX 4090",
        clock_ceiling=None,
        probe_history=[],
        log=lambda _message: None,
        target_voltage_mv=900,
        target_clock_mhz=2700,
    )

    assert len(tried) > 1
    assert int(result.selected_candidate.target_mhz) > 2415


def test_hw_power_brake_stops_climb_below_software_power_limit() -> None:
    # The hardware brake is direct wall evidence even when aggregate board
    # power is far below the configured software cap. Continuing upward would
    # repeatedly ask the board for clocks its protection circuit already
    # refused to deliver.
    curve = base_curve(850, 955, 5, 2400, 15)
    start = VfCurveCandidate("reclaim-start", 900, 2415, curve)
    wall_mhz = 2450.0
    tried: list[int] = []

    class HardwareBrakeRunner:
        power_limit_w = 460

        def probe_candidate(self, candidate, **_kwargs):
            tried.append(int(candidate.target_mhz))
            measured = min(float(candidate.target_mhz), wall_mhz)
            probe = _probe(
                candidate.voltage_mv,
                candidate.target_mhz,
                q2rtx_clock_mhz=measured,
            )
            if measured < float(candidate.target_mhz):
                probe.perf_cap_reason = "sw-power+hw-power-brake"
                probe.avg_power_w = 300.0
            return _passed_outcome(probe)

    result = run_auto_oc_candidate_search(
        base_curve=curve,
        start_candidate=start,
        start_probe=_probe(900, 2415, q2rtx_clock_mhz=2415.0),
        runner=HardwareBrakeRunner(),
        gpu_name="NVIDIA GeForce RTX 4090",
        clock_ceiling=None,
        probe_history=[],
        log=lambda _message: None,
        target_voltage_mv=900,
        target_clock_mhz=2700,
    )

    assert float(result.selected_candidate.target_mhz) <= wall_mhz + 22.5
    assert len([target for target in tried if target > wall_mhz + 22.5]) == 1
    assert max(tried) < 2700


def test_restart_skips_crashed_endpoint_before_hardware_and_verifies_lower_clock(
    tmp_path, monkeypatch
):
    import json
    from auto_uv.persistence import interrupted_probe_crash_cache as crash_cache
    from auto_uv.persistence import unsafe_voltage_blacklist_file as blacklist

    marker = tmp_path / "marker.json"
    cache = tmp_path / "unsafe.json"
    marker.write_text(
        json.dumps(
            {
                "state": "probing",
                "phase": "candidate",
                "candidate_voltage_mv": 925,
                "lock_clock_mhz": 3098,
                "details": {
                    "start_voltage_mv": 1025,
                    "initial_probe_clock_mhz": 2740,
                    "generated_profile_tier": "performance",
                    "blocked_lock_clock_mhz": [3098, 3097, 3090],
                },
            }
        )
    )
    monkeypatch.setattr(crash_cache, "probe_in_progress_path", lambda: marker)
    monkeypatch.setattr(blacklist, "unsafe_voltage_blacklist_path", lambda: cache)
    assert crash_cache.consume_interrupted_probe_crash_marker() is not None
    assert cache.exists() and not marker.exists()
    curve = base_curve(850, 950, 5, 2400, 15)
    start = VfCurveCandidate("last passing point", 920, 3015, curve)
    monkeypatch.setattr(
        auto_oc_search,
        "build_auto_oc_ladder",
        lambda *_a, **_k: [AutoOcStep(1, 925, 3098, 1.0)],
    )
    tried = []

    class Runner:
        def probe_candidate(self, candidate, **_kwargs):
            tried.append((candidate.voltage_mv, candidate.target_mhz))
            assert tried[-1] == (925, 3015), "known crashing region reached hardware"
            return _passed_outcome(_probe(925, 3015))

    class Ceiling:
        def retarget(self, **kwargs):
            assert (kwargs["lock_voltage_mv"], kwargs["lock_clock_mhz"]) == (925, 3015)

        def describe(self):
            return "test ceiling"

    result = run_auto_oc_candidate_search(
        base_curve=curve,
        start_candidate=start,
        start_probe=_probe(920, 3015),
        runner=Runner(),
        gpu_name="RTX 5080",
        clock_ceiling=Ceiling(),
        probe_history=[],
        log=lambda _: None,
        target_voltage_mv=925,
        target_clock_mhz=3098,
        target_profile_id="performance",
    )
    assert tried == [(925, 3015)]
    assert (
        result.selected_candidate.voltage_mv,
        result.selected_candidate.target_mhz,
    ) == (925, 3015)
    assert result.attempts[0].outcome.decision.failure_kind is FailureKind.CACHED_UNSAFE


@pytest.mark.parametrize("tier", ["efficiency", "balanced", "performance"])
def test_xid_at_voltage_cap_retreats_below_new_crash_band(monkeypatch, tier):
    from auto_uv.probes.stability_decision import evaluate_stable_run

    curve = base_curve(850, 955, 5, 2400, 15)
    start = VfCurveCandidate("start", 850, 2730, curve)
    unsafe, tried = [], []
    monkeypatch.setattr(auto_oc_search, "load_unsafe_voltage_blacklist", lambda: unsafe)

    class Runner:
        def probe_candidate(self, candidate, **_kwargs):
            tried.append((candidate.voltage_mv, candidate.target_mhz))
            summary = _probe(candidate.voltage_mv, candidate.target_mhz)
            summary.tested_plan = candidate.flattened_plan
            if candidate.target_mhz == 2800:
                unsafe.append({
                    "candidate_voltage_mv": 850, "lock_clock_mhz": 2800,
                    "blocked_lock_clock_mhz": [2800, 2790, 2767], "reason": "nvidia-xid",
                })
                return VoltageProbeOutcome(decision=evaluate_stable_run(
                    {}, baseline_fps=None, baseline_power_w=None, power_limit_w=None,
                    cuda_required=True, xid_found=True,
                ), raw_probe=summary)
            return _passed_outcome(summary)

    result = run_auto_oc_candidate_search(
        base_curve=curve, start_candidate=start, start_probe=_probe(850, 2730),
        runner=Runner(), gpu_name="NVIDIA GeForce RTX 5080", clock_ceiling=None,
        probe_history=[], log=lambda _: None, tail_rise_bins=2,
        target_voltage_mv=850, target_clock_mhz=2800, target_profile_id=tier,
    )
    assert tried == [(850, clock) for clock in (2745, 2760, 2775, 2790, 2800)]
    assert result.selected_candidate.target_mhz == 2760
    assert result.selected_candidate.flattened_plan == result.selected_probe.tested_plan


def test_new_crash_retries_only_a_lower_passed_clock_with_voltage_headroom(monkeypatch):
    curve = base_curve(875, 955, 5, 2400, 15)
    tried = []
    start = VfCurveCandidate("start", 925, 2670, curve)
    monkeypatch.setattr(auto_oc_search, "load_unsafe_voltage_blacklist", lambda: [])

    class Runner:
        def probe_candidate(self, candidate, **_kwargs):
            tried.append((candidate.voltage_mv, candidate.target_mhz))
            summary = _probe(candidate.voltage_mv, candidate.target_mhz)
            if len(tried) == 1:
                return VoltageProbeOutcome(decision=StableRunDecision(
                    False, FailureKind.NVIDIA_XID, FailureSeverity.UNSAFE, "Xid 109",
                ), raw_probe=summary)
            return _passed_outcome(summary)

    result = run_auto_oc_candidate_search(
        base_curve=curve, start_candidate=start, start_probe=_probe(925, 2670),
        runner=Runner(), gpu_name="NVIDIA GeForce RTX 5080", clock_ceiling=None,
        probe_history=[], log=lambda _: None, max_interpolation_steps=2,
        target_voltage_mv=950, target_clock_mhz=2745,
    )
    assert tried == [(935, 2715), (950, 2670)]
    assert (result.selected_candidate.voltage_mv, result.selected_candidate.target_mhz) == (950, 2670)


def test_crash_during_voltage_retry_stops_instead_of_retrying_failed_clock_again(monkeypatch):
    curve = base_curve(875, 955, 5, 2400, 15)
    tried = []
    start = VfCurveCandidate("start", 925, 2670, curve)
    monkeypatch.setattr(auto_oc_search, "load_unsafe_voltage_blacklist", lambda: [])

    class Runner:
        def probe_candidate(self, candidate, **_kwargs):
            tried.append((candidate.voltage_mv, candidate.target_mhz))
            summary = _probe(candidate.voltage_mv, candidate.target_mhz)
            if len(tried) == 1:
                return _failed_outcome(summary)
            if len(tried) == 2:
                return VoltageProbeOutcome(decision=StableRunDecision(
                    False, FailureKind.NVIDIA_XID, FailureSeverity.UNSAFE, "Xid 109",
                ), raw_probe=summary)
            return _passed_outcome(summary)

    result = run_auto_oc_candidate_search(
        base_curve=curve, start_candidate=start, start_probe=_probe(925, 2670),
        runner=Runner(), gpu_name="NVIDIA GeForce RTX 5080", clock_ceiling=None,
        probe_history=[], log=lambda _: None, max_interpolation_steps=2,
        target_voltage_mv=950, target_clock_mhz=2745,
    )
    assert tried == [(935, 2715), (940, 2715), (950, 2670)]
    assert result.selected_candidate.target_mhz == 2670
