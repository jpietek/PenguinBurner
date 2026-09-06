"""Test explicit tier targets after the normal, reusable voltage descent."""

from __future__ import annotations

from typing import Callable

from auto_uv.auto_oc.search import (
    AutoOcProbeRunner,
    AutoOcSearchResult,
    auto_oc_endpoint,
    retarget_clock_ceiling,
    run_auto_oc_candidate_search,
)
from auto_uv.curve.base_vf_curve_voltage_bins import editable_voltage_bins
from auto_uv.curve.flattened_voltage_probe_curve import (
    build_flattened_voltage_probe_curve,
)
from auto_uv.domain.console_log import log_phase
from auto_uv.domain.types import (
    AutoUvCriticalProbeError,
    AutoUvError,
    AutoUvProbeSummary,
    FailureKind,
    FailureSeverity,
    VfCurveCandidate,
)
from auto_uv.persistence.unsafe_voltage_blacklist_file import (
    load_unsafe_voltage_blacklist,
)
from auto_uv.persistence.unsafe_voltage_cache import unsafe_voltage_block_reason
from auto_uv.scan_mode.efficiency_fps_per_w_policy import (
    best_efficiency_candidate_index,
)
from auto_uv.scan_mode.target_overrides import TierTargetOverrides


def run_custom_tier_target_search(
    *,
    base_curve: list[dict],
    start_candidate: VfCurveCandidate,
    start_probe: AutoUvProbeSummary | None,
    runner: AutoOcProbeRunner,
    gpu_name: object | None,
    clock_ceiling,
    probe_history: list[AutoUvProbeSummary],
    stable_history: list[AutoUvProbeSummary],
    log: Callable[[str], None],
    tier: str,
    overrides: TierTargetOverrides,
    tail_rise_bins: int,
    measured_baseline_clock_mhz: float,
) -> AutoOcSearchResult:
    endpoint = auto_oc_endpoint(
        gpu_name,
        target_profile_id=tier,
        target_voltage_mv=overrides.voltage_mv,
        target_clock_mhz=overrides.clock_mhz,
    )
    if endpoint is None:
        endpoint = auto_oc_endpoint(
            gpu_name,
            target_profile_id=tier,
            target_voltage_mv=overrides.voltage_mv or start_candidate.voltage_mv,
            target_clock_mhz=overrides.clock_mhz or start_candidate.target_mhz,
        )
    assert endpoint is not None
    voltages = sorted(editable_voltage_bins(base_curve))
    if not voltages or not voltages[0] <= endpoint.voltage_mv <= voltages[-1]:
        raise AutoUvError(f"{tier} voltage target is outside the editable GPU curve")
    voltage_limit = max(v for v in voltages if v <= endpoint.voltage_mv)
    clock_limit = int(endpoint.clock_mhz)
    selected, selected_probe = start_candidate, start_probe
    passed = [(selected, selected_probe)]
    unsafe = load_unsafe_voltage_blacklist()

    def probe(voltage: int, clock: int) -> bool:
        nonlocal selected, selected_probe
        blocked = unsafe_voltage_block_reason(
            unsafe,
            candidate_voltage_mv=voltage,
            lock_clock_mhz=clock,
            profile_tier=tier,
        )
        if blocked:
            log_phase(log, "custom-target", f"{tier}: {blocked}")
            return False
        candidate = build_flattened_voltage_probe_curve(
            base_curve,
            candidate_voltage_mv=voltage,
            target_clock_mhz=clock,
            label=f"{tier} custom target",
            tail_rise_bins=tail_rise_bins,
            metadata={"generated_profile_tier": tier, "custom_target": True},
        )
        retarget_clock_ceiling(clock_ceiling, candidate=candidate, log=log)
        log_phase(log, "custom-target", f"{tier} testing {voltage}mV@{clock}MHz")
        outcome = runner.probe_candidate(
            candidate,
            # An intentional clock reduction establishes its own FPS reference.
            # Workload failures and complete measurements are still required.
            stable_history=[],
            phase_label="candidate",
            summarize_saturated_tail=False,
            use_power_limit_floor=False,
            use_companion_load=True,
        )
        if outcome.decision.failure_kind is FailureKind.USER_STOP:
            raise AutoUvError("user-stop-requested during custom target search")
        if outcome.decision.severity is FailureSeverity.CRITICAL:
            raise AutoUvCriticalProbeError(
                f"Custom target search stopped after critical probe failure: {outcome.decision.reason}"
            )
        summary = outcome.raw_probe
        if summary is not None:
            probe_history.append(summary)
        if not outcome.decision.passed or summary is None:
            return False
        measured = summary.avg_core_clock_mhz
        if measured is None:
            log_phase(
                log,
                "custom-target",
                f"{tier} measured clock missing",
            )
            return False
        stable_history.append(summary)
        passed.append((candidate, summary))
        selected, selected_probe = candidate, summary
        return True

    # Apply a lower custom clock at the voltage already proven by the sweep.
    if clock_limit < selected.target_mhz:
        initial_clock = selected.target_mhz
        steps = min(10, max(1, (initial_clock - clock_limit + 14) // 15))
        for index in range(1, steps + 1):
            clock = max(
                clock_limit,
                (initial_clock - (initial_clock - clock_limit) * index // steps)
                // 15
                * 15,
            )
            if index == steps:
                clock = clock_limit
            if not probe(selected.voltage_mv, clock):
                break

    # Keep the voltage proved by the initial descent. A custom lower MHz
    # target never starts another voltage-down sweep.
    voltage_selection_limit = max(voltage_limit, start_candidate.voltage_mv)
    if start_candidate.voltage_mv > voltage_limit:
        log_phase(
            log,
            "custom-target",
            f"{tier} retaining sweep voltage {start_candidate.voltage_mv}mV; "
            f"requested voltage target {voltage_limit}mV was not reached",
        )

    if clock_limit > selected.target_mhz and selected.voltage_mv <= voltage_limit:
        climbed = run_auto_oc_candidate_search(
            base_curve=base_curve,
            start_candidate=selected,
            start_probe=selected_probe,
            runner=runner,
            gpu_name=gpu_name,
            clock_ceiling=clock_ceiling,
            probe_history=probe_history,
            log=log,
            tail_rise_bins=tail_rise_bins,
            target_voltage_mv=voltage_limit,
            target_clock_mhz=clock_limit,
            measured_baseline_clock_mhz=measured_baseline_clock_mhz,
            target_profile_id=tier,
            probe_stable_history=[start_probe] if start_probe else [],
        )
        for attempt in climbed.attempts:
            summary = attempt.outcome.raw_probe
            if attempt.outcome.decision.passed and summary is not None:
                stable_history.append(summary)
                passed.append((attempt.candidate, summary))
        selected, selected_probe = climbed.selected_candidate, climbed.selected_probe

    eligible = [
        (candidate, summary)
        for candidate, summary in passed
        if candidate.target_mhz <= clock_limit
        and candidate.voltage_mv <= voltage_selection_limit
        and summary is not None
        and summary.avg_core_clock_mhz is not None
    ]
    if not eligible:
        raise AutoUvError(
            f"No stable {tier} candidate within custom targets "
            f"{clock_limit}MHz at the proven sweep voltage; earlier checkpoints remain available"
        )
    if tier == "efficiency":
        index = best_efficiency_candidate_index([summary for _, summary in eligible])
        selected, selected_probe = eligible[index if index is not None else -1]
    elif not any(candidate is selected for candidate, _ in eligible):
        selected, selected_probe = eligible[-1]
    return AutoOcSearchResult(
        selected_candidate=selected, selected_probe=selected_probe, endpoint=endpoint
    )
