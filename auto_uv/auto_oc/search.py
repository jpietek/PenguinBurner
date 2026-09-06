"""Search the performance Auto-OC ladder before the final verification pass."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from auto_uv.shared.positive_int import positive_int

from auto_uv.domain.console_log import log_phase
from auto_uv.domain.types import (
    AutoUvCriticalProbeError,
    AutoUvError,
    AutoUvProbeSummary,
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv.curve.base_vf_curve_voltage_bins import editable_voltage_bins
from auto_uv.curve.measured_probe_lock_clock import probe_indicates_power_saturation
from auto_uv.curve.flattened_voltage_probe_curve import build_flattened_voltage_probe_curve
from auto_uv.curve.rising_tail import tail_ceiling_clock_mhz
from auto_uv.scan_mode.uv_limits import UvTierTarget, uv_limit_profile_target_for_gpu
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome
from auto_uv.persistence.unsafe_voltage_blacklist_file import load_unsafe_voltage_blacklist
from auto_uv.persistence.unsafe_voltage_cache import unsafe_voltage_block_reason
from .ladder import AutoOcStep, build_auto_oc_ladder
from .scoring import auto_oc_probe_key, effective_q2rtx_clock_mhz
from .settings import (
    AUTO_OC_DEFAULT_MAX_INTERPOLATION_STEPS,
    AUTO_OC_TARGET_PROFILE_ID,
)

# A stable rung whose measured clock trails its requested lock by more than
# this while the probe is power-saturated is wall-limited, not capability:
# 1.5 driver clock steps absorbs bin snapping and telemetry averaging noise.
AUTO_OC_WALL_SHORTFALL_TOLERANCE_MHZ = 22.5


class AutoOcProbeRunner(Protocol):
    def probe_candidate(
        self,
        candidate: VfCurveCandidate,
        *,
        stable_history: list[AutoUvProbeSummary],
        phase_label: str,
        summarize_saturated_tail: bool,
        use_power_limit_floor: bool,
        use_companion_load: bool,
    ) -> VoltageProbeOutcome: ...


@dataclass(frozen=True, slots=True)
class AutoOcAttempt:
    step: AutoOcStep
    candidate: VfCurveCandidate
    outcome: VoltageProbeOutcome


@dataclass(frozen=True, slots=True)
class AutoOcSearchResult:
    selected_candidate: VfCurveCandidate
    selected_probe: AutoUvProbeSummary | None
    endpoint: UvTierTarget | None = None
    attempts: tuple[AutoOcAttempt, ...] = ()


def run_auto_oc_candidate_search(
    *,
    base_curve: list[dict],
    start_candidate: VfCurveCandidate,
    start_probe: AutoUvProbeSummary | None,
    runner: AutoOcProbeRunner,
    gpu_name: object | None,
    clock_ceiling,
    probe_history: list[AutoUvProbeSummary],
    log: Callable[[str], None],
    tail_rise_bins: int = 0,
    max_interpolation_steps: int = AUTO_OC_DEFAULT_MAX_INTERPOLATION_STEPS,
    target_voltage_mv: int | None = None,
    target_clock_mhz: int | None = None,
    measured_baseline_clock_mhz: float | int | None = None,
    target_profile_id: str = AUTO_OC_TARGET_PROFILE_ID,
    probe_stable_history: list[AutoUvProbeSummary] | None = None,
) -> AutoOcSearchResult:
    endpoint = auto_oc_endpoint(
        gpu_name,
        target_voltage_mv=target_voltage_mv,
        target_clock_mhz=target_clock_mhz,
        target_profile_id=str(target_profile_id),
    )
    if endpoint is None:
        return AutoOcSearchResult(
            selected_candidate=start_candidate, selected_probe=start_probe
        )

    ladder = build_auto_oc_ladder(
        base_curve,
        start_voltage_mv=int(start_candidate.voltage_mv),
        start_clock_mhz=int(start_candidate.target_mhz),
        endpoint_voltage_mv=int(endpoint.voltage_mv),
        endpoint_clock_mhz=int(endpoint.clock_mhz),
        max_steps=int(max_interpolation_steps),
    )
    if not ladder:
        log_phase(
            log,
            "auto-oc",
            "skip no legal step from "
            f"{int(start_candidate.voltage_mv)}mV@{int(start_candidate.target_mhz)}MHz "
            f"to {int(endpoint.voltage_mv)}mV@{int(endpoint.clock_mhz)}MHz",
        )
        return AutoOcSearchResult(
            selected_candidate=start_candidate,
            selected_probe=start_probe,
            endpoint=endpoint,
        )

    log_phase(
        log,
        "auto-oc",
        f"target={endpoint.gpu_family}/{endpoint.profile_id} "
        f"cap={int(endpoint.voltage_mv)}mV@{int(endpoint.clock_mhz)}MHz "
        f"steps={len(ladder)}",
    )
    selected_candidate = start_candidate
    selected_probe = start_probe
    selected_key = auto_oc_probe_key(
        start_probe,
        voltage_mv=int(start_candidate.voltage_mv),
        step_index=0,
    )
    attempts: list[AutoOcAttempt] = []
    passed_candidates = [(start_candidate, start_probe)]
    failed_voltage_floor_mv: int | None = None
    consumed_voltage_floor_mv: int | None = None
    power_wall_reached = False

    def probe_step(step: AutoOcStep, *, action: str) -> tuple[VfCurveCandidate, VoltageProbeOutcome]:
        candidate = auto_oc_candidate(
            base_curve,
            step=step,
            total_steps=len(ladder),
            tail_rise_bins=int(tail_rise_bins),
            start_clock_mhz=int(start_candidate.target_mhz),
            endpoint_clock_mhz=int(endpoint.clock_mhz),
            measured_baseline_clock_mhz=measured_baseline_clock_mhz,
        )
        blocked = unsafe_voltage_block_reason(
            load_unsafe_voltage_blacklist(),
            candidate_voltage_mv=int(candidate.voltage_mv),
            lock_clock_mhz=int(candidate.target_mhz),
            profile_tier=target_profile_id,
        )
        if blocked:
            outcome = VoltageProbeOutcome(decision=StableRunDecision(
                False, FailureKind.CACHED_UNSAFE, FailureSeverity.UNSAFE, blocked
            ))
            attempts.append(AutoOcAttempt(step=step, candidate=candidate, outcome=outcome))
            return candidate, outcome
        retarget_clock_ceiling(
            clock_ceiling,
            candidate=candidate,
            log=log,
        )
        log_phase(
            log,
            "auto-oc",
            f"{action}={step.index}/{len(ladder)} "
            f"{candidate.voltage_mv}mV@{candidate.target_mhz}MHz",
        )
        outcome = runner.probe_candidate(
            candidate,
            stable_history=list(probe_stable_history or []),
            phase_label="candidate",
            summarize_saturated_tail=False,
            use_power_limit_floor=False,
            use_companion_load=True,
        )
        if outcome.raw_probe is not None:
            probe_history.append(outcome.raw_probe)
        attempts.append(AutoOcAttempt(step=step, candidate=candidate, outcome=outcome))
        if (
            outcome.decision.severity is FailureSeverity.CRITICAL
            and outcome.decision.failure_kind is not FailureKind.USER_STOP
        ):
            raise AutoUvCriticalProbeError(
                f"Auto-OC stopped after critical probe failure: {outcome.decision.reason}"
            )
        return candidate, outcome

    def record_outcome(
        candidate: VfCurveCandidate,
        step: AutoOcStep,
        outcome: VoltageProbeOutcome,
    ) -> bool:
        nonlocal selected_candidate, selected_probe, selected_key, power_wall_reached
        if outcome.decision.passed and outcome.raw_probe is not None:
            measured_clock = effective_q2rtx_clock_mhz(outcome.raw_probe)
            if (
                measured_clock is not None
                and float(candidate.target_mhz) - float(measured_clock)
                > AUTO_OC_WALL_SHORTFALL_TOLERANCE_MHZ
                and probe_indicates_power_saturation(
                    outcome.raw_probe,
                    power_limit_w=getattr(runner, "power_limit_w", None),
                    # Ending a climb claims the board cannot go faster, so it
                    # takes measured power at the limit or an explicit hardware
                    # power brake. A loose sw-power reason appears with hundreds
                    # of watts still to spare, and Blackwell always measures a
                    # little under its requested lock — together those would
                    # otherwise end every climb at rung one.
                    require_power_evidence=True,
                )
            ):
                # The rung is stable but its measured clock is set by the
                # board-power wall, not by the requested lock. Adopting it
                # would ship a lock target the cap cannot deliver, and every
                # higher rung would measure the same wall — the climb is over.
                power_wall_reached = True
                log_phase(
                    log,
                    "auto-oc",
                    "power-walled rung not adopted "
                    f"requested={int(candidate.target_mhz)}MHz "
                    f"measured={_format_clock(measured_clock)}",
                )
                return True
            candidate_key = auto_oc_probe_key(
                outcome.raw_probe,
                voltage_mv=int(candidate.voltage_mv),
                step_index=int(step.index),
            )
            passed_candidates.append((candidate, outcome.raw_probe))
            if candidate_key > selected_key:
                selected_candidate = candidate
                selected_probe = outcome.raw_probe
                selected_key = candidate_key
            log_phase(
                log,
                "auto-oc",
                "pass measured-clock="
                f"{_format_clock(effective_q2rtx_clock_mhz(outcome.raw_probe))}",
            )
            return True
        log_phase(log, "auto-oc", f"rejected {outcome.decision.reason}")
        return False

    def backoff_after_unsafe(step: AutoOcStep, outcome: VoltageProbeOutcome) -> None:
        nonlocal selected_candidate, selected_probe
        log_phase(
            log,
            "auto-oc",
            f"skip {outcome.decision.reason}; back off to passed {selected_candidate.target_mhz}MHz",
        )
        if selected_candidate.voltage_mv < ladder[-1].voltage_mv:
            fallback = AutoOcStep(
                index=step.index,
                voltage_mv=ladder[-1].voltage_mv,
                target_mhz=selected_candidate.target_mhz,
                ratio=step.ratio,
            )
            candidate, outcome = probe_step(fallback, action="backoff-voltage")
            if (
                record_outcome(candidate, fallback, outcome)
                and not power_wall_reached
            ):
                selected_candidate, selected_probe = candidate, outcome.raw_probe

    stop_requested = False
    for step in ladder:
        if (
            consumed_voltage_floor_mv is not None
            and int(step.voltage_mv) <= int(consumed_voltage_floor_mv)
        ):
            log_phase(
                log,
                "auto-oc",
                "skip consumed-voltage-rung "
                f"{int(step.voltage_mv)}mV@{int(step.target_mhz)}MHz "
                f"consumed-voltage={int(consumed_voltage_floor_mv)}mV",
            )
            continue
        if (
            failed_voltage_floor_mv is not None
            and int(step.voltage_mv) <= int(failed_voltage_floor_mv)
        ):
            log_phase(
                log,
                "auto-oc",
                "skip failed-voltage-rung "
                f"{int(step.voltage_mv)}mV@{int(step.target_mhz)}MHz "
                f"failed-voltage={int(failed_voltage_floor_mv)}mV",
            )
            continue
        candidate, outcome = probe_step(step, action="try")
        if outcome.decision.failure_kind is FailureKind.USER_STOP:
            break
        if outcome.decision.severity is FailureSeverity.UNSAFE:
            backoff_after_unsafe(step, outcome)
            break
        passed = record_outcome(candidate, step, outcome)
        if not passed:
            failed_voltage_floor_mv = max(
                int(failed_voltage_floor_mv or 0),
                int(candidate.voltage_mv),
            )
        if power_wall_reached:
            log_phase(
                log,
                "auto-oc",
                "stop power-wall reached; higher rungs cannot raise a "
                "capped clock",
            )
            break
        if passed:
            continue
        for retry_voltage_mv in auto_oc_retry_voltages(
            base_curve,
            failed_voltage_mv=int(candidate.voltage_mv),
            endpoint_voltage_mv=int(endpoint.voltage_mv),
        ):
            if (
                consumed_voltage_floor_mv is not None
                and int(retry_voltage_mv) <= int(consumed_voltage_floor_mv)
            ):
                continue
            retry_step = AutoOcStep(
                index=int(step.index),
                voltage_mv=int(retry_voltage_mv),
                target_mhz=int(candidate.target_mhz),
                ratio=float(step.ratio),
            )
            retry_candidate, retry_outcome = probe_step(
                retry_step,
                action=f"retry-voltage failed={int(candidate.voltage_mv)}mV",
            )
            if retry_outcome.decision.failure_kind is FailureKind.USER_STOP:
                stop_requested = True
                break
            if retry_outcome.decision.severity is FailureSeverity.UNSAFE:
                backoff_after_unsafe(retry_step, retry_outcome)
                stop_requested = True
                break
            retry_passed = record_outcome(retry_candidate, retry_step, retry_outcome)
            if retry_passed:
                consumed_voltage_floor_mv = max(
                    int(consumed_voltage_floor_mv or 0),
                    int(retry_candidate.voltage_mv),
                )
                break
            failed_voltage_floor_mv = max(
                int(failed_voltage_floor_mv or 0),
                int(retry_candidate.voltage_mv),
            )
        if stop_requested:
            break

    # A later crash can blacklist neighbouring clocks that passed earlier.
    # Carry an allowed, actually tested curve into final verification.
    unsafe = load_unsafe_voltage_blacklist()
    if unsafe_voltage_block_reason(
        unsafe,
        candidate_voltage_mv=selected_candidate.voltage_mv,
        lock_clock_mhz=selected_candidate.target_mhz,
        profile_tier=target_profile_id,
    ):
        passed_candidates.extend(
            (
                VfCurveCandidate(
                    "passed-descent-fallback", probe.candidate_voltage_mv,
                    probe.lock_clock_mhz, probe.tested_plan,
                ), probe,
            )
            for probe in probe_stable_history or []
            if probe.tested_plan is not None
            and probe.lock_clock_mhz <= start_candidate.target_mhz
        )
        eligible = [
            (candidate, probe)
            for candidate, probe in passed_candidates
            if not unsafe_voltage_block_reason(
                unsafe,
                candidate_voltage_mv=candidate.voltage_mv,
                lock_clock_mhz=candidate.target_mhz,
                profile_tier=target_profile_id,
            )
        ]
        if not eligible:
            raise AutoUvError("No passed Auto-OC candidate remains outside the unsafe band")
        selected_candidate, selected_probe = max(
            eligible,
            key=lambda item: auto_oc_probe_key(
                item[1], voltage_mv=item[0].voltage_mv, step_index=0
            ),
        )
        log_phase(
            log, "auto-oc",
            f"back off to passed {selected_candidate.voltage_mv}mV@"
            f"{selected_candidate.target_mhz}MHz outside the unsafe band",
        )

    if selected_candidate is start_candidate:
        log_phase(log, "auto-oc", "no measured-clock improvement; keeping UV candidate")
    else:
        log_phase(
            log,
            "auto-oc",
            f"selected={selected_candidate.voltage_mv}mV@{selected_candidate.target_mhz}MHz "
            f"measured-clock={_format_clock(effective_q2rtx_clock_mhz(selected_probe))}",
        )
    return AutoOcSearchResult(
        selected_candidate=selected_candidate,
        selected_probe=selected_probe,
        endpoint=endpoint,
        attempts=tuple(attempts),
    )


def auto_oc_endpoint(
    gpu_name: object | None,
    *,
    target_voltage_mv: int | None = None,
    target_clock_mhz: int | None = None,
    target_profile_id: str = AUTO_OC_TARGET_PROFILE_ID,
) -> UvTierTarget | None:
    table_target = uv_limit_profile_target_for_gpu(gpu_name, target_profile_id)
    voltage_mv = positive_int(target_voltage_mv)
    clock_mhz = positive_int(target_clock_mhz)
    if table_target is None:
        if voltage_mv is None or clock_mhz is None:
            return None
        return UvTierTarget(
            gpu_family="Custom GPU",
            profile_id="custom",
            voltage_mv=int(voltage_mv),
            clock_mhz=int(clock_mhz),
        )
    return UvTierTarget(
        gpu_family=table_target.gpu_family,
        profile_id=table_target.profile_id,
        voltage_mv=int(
            voltage_mv
            if voltage_mv is not None
            else table_target.voltage_mv
        ),
        clock_mhz=int(
            clock_mhz
            if clock_mhz is not None
            else table_target.clock_mhz
        ),
    )


def auto_oc_candidate(
    base_curve: list[dict],
    *,
    step: AutoOcStep,
    total_steps: int,
    tail_rise_bins: int,
    start_clock_mhz: int | None = None,
    endpoint_clock_mhz: int | None = None,
    measured_baseline_clock_mhz: float | int | None = None,
) -> VfCurveCandidate:
    metadata = {
        "auto_oc": True,
        "auto_oc_step": int(step.index),
        "auto_oc_steps": int(total_steps),
    }
    if measured_baseline_clock_mhz is not None:
        baseline_clock = float(measured_baseline_clock_mhz)
        endpoint_clock = (
            int(endpoint_clock_mhz)
            if endpoint_clock_mhz is not None
            else int(step.target_mhz)
        )
        limit_mhz = int(round(float(endpoint_clock) - baseline_clock))
        applied_mhz = int(round(float(step.target_mhz) - baseline_clock))
        metadata.update(
            {
                "auto_oc_baseline_clock_mhz": round(baseline_clock, 2),
                "auto_oc_target_clock_mhz": endpoint_clock,
                "auto_oc_applied_mhz": applied_mhz,
                "auto_oc_limit_mhz": limit_mhz,
            }
        )
    if start_clock_mhz is not None:
        metadata["auto_oc_start_clock_mhz"] = int(start_clock_mhz)
    return build_flattened_voltage_probe_curve(
        base_curve,
        candidate_voltage_mv=int(step.voltage_mv),
        target_clock_mhz=int(step.target_mhz),
        label=f"performance-oc {int(step.index)}/{int(total_steps)}",
        tail_rise_bins=int(tail_rise_bins),
        metadata=metadata,
    )


def retarget_clock_ceiling(
    clock_ceiling,
    *,
    candidate: VfCurveCandidate,
    log: Callable[[str], None],
) -> None:
    if clock_ceiling is None:
        return
    clock_ceiling.retarget(
        lock_clock_mhz=int(candidate.target_mhz),
        lock_voltage_mv=int(candidate.voltage_mv),
        ceiling_clock_mhz=tail_ceiling_clock_mhz(
            candidate.flattened_plan,
            fallback_clock_mhz=int(candidate.target_mhz),
            lock_voltage_mv=int(candidate.voltage_mv),
        ),
    )
    log_phase(log, "ceiling", clock_ceiling.describe())


def auto_oc_retry_voltages(
    base_curve: list[dict],
    *,
    failed_voltage_mv: int,
    endpoint_voltage_mv: int,
) -> list[int]:
    return [
        int(voltage)
        for voltage in sorted(editable_voltage_bins(base_curve))
        if int(failed_voltage_mv) < int(voltage) <= int(endpoint_voltage_mv)
    ]


def _format_clock(value: float | None) -> str:
    return "n/a" if value is None else f"{float(value):.0f}MHz"
