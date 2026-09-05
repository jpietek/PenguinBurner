"""Run one live Q2RTX/CUDA probe for an Auto-UV curve candidate.

It converts the raw workload result into the strict Auto-UV stability decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from stability.q2rtx.models import Q2RTXStabilityConfig

from auto_uv.domain.types import AutoUvProbeSummary, VfCurveCandidate
from .stability_decision import StabilityThresholds, evaluate_stable_run
from .voltage_probe import probe_voltage_candidate
from .voltage_probe import companion_duration_s_from_command
from .config import (
    q2rtx_cuda_probe_config_for_voltage_band,
    q2rtx_only_probe_config_for_voltage_band,
    reference_discovery_q2rtx_probe_config,
)
from auto_uv.domain.events import AutoUvEventCallback
from .events import (
    emit_voltage_probe_finished,
    emit_voltage_probe_started,
)
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome


@dataclass(frozen=True, slots=True)
class AutoUvProbeRunner:
    reader: object
    live_voltage_reader: object
    q2rtx_config: Q2RTXStabilityConfig
    runtime_default_plan: list[dict]
    power_limit_w: int | None
    start_voltage_mv: int
    baseline_clock_mhz: float | None
    min_performance_core_clock_pct: float
    short_probe_base_duration_s: int
    log: Callable[[str], None]
    marker_details: dict | None = None
    event_callback: AutoUvEventCallback | None = None

    def probe_default_curve(
        self,
        *,
        base_curve: list[dict],
        label_voltage_mv: int,
        label_clock_mhz: int,
    ) -> tuple[AutoUvProbeSummary, object]:
        return probe_voltage_candidate(
            reader=self.reader,
            candidate_plan=base_curve,
            candidate_voltage_mv=int(label_voltage_mv),
            lock_clock_mhz=int(label_clock_mhz),
            q2rtx_config=reference_discovery_q2rtx_probe_config(
                self.q2rtx_config,
                base_duration_s=int(self.short_probe_base_duration_s),
            ),
            stable_history=[],
            initial_probe_clock_mhz=None,
            nvml_session=self.live_voltage_reader,
            log=self.log,
            phase_label="discover",
            power_limit_w=self.power_limit_w,
            enforce_target_core_clock_floor=False,
            show_candidate_target=False,
            summarize_saturated_tail=True,
            use_power_limit_floor=True,
            reset_plan=self.runtime_default_plan,
            marker_details=self.marker_details,
            event_callback=self.event_callback,
        )

    def probe_baseline_candidate(
        self,
        candidate: VfCurveCandidate,
    ) -> VoltageProbeOutcome:
        return self.probe_candidate(
            candidate,
            stable_history=[],
            phase_label="baseline",
            enforce_target_core_clock_floor=False,
            summarize_saturated_tail=True,
            use_power_limit_floor=True,
            use_companion_load=False,
        )

    def probe_sweep_candidate(
        self,
        candidate: VfCurveCandidate,
        *,
        stable_history: list[AutoUvProbeSummary],
        phase_label: str = "candidate",
        enforce_target_core_clock_floor: bool = True,
    ) -> VoltageProbeOutcome:
        return self.probe_candidate(
            candidate,
            stable_history=stable_history,
            phase_label=phase_label,
            enforce_target_core_clock_floor=bool(enforce_target_core_clock_floor),
            summarize_saturated_tail=False,
            use_power_limit_floor=False,
            use_companion_load=True,
        )

    def probe_candidate(
        self,
        candidate: VfCurveCandidate,
        *,
        stable_history: list[AutoUvProbeSummary],
        phase_label: str,
        enforce_target_core_clock_floor: bool,
        summarize_saturated_tail: bool,
        use_power_limit_floor: bool,
        use_companion_load: bool,
    ) -> VoltageProbeOutcome:
        config_builder = (
            q2rtx_cuda_probe_config_for_voltage_band
            if bool(use_companion_load)
            else q2rtx_only_probe_config_for_voltage_band
        )
        config = config_builder(
            self.q2rtx_config,
            initial_target_voltage_mv=int(self.start_voltage_mv),
            candidate_voltage_mv=int(candidate.voltage_mv),
            base_duration_s=int(self.short_probe_base_duration_s),
        )
        emit_voltage_probe_started(
            self.event_callback,
            candidate,
            stage=str(phase_label),
            max_clock_drop_pct=self.max_clock_drop_pct,
            target_duration_s=probe_ui_target_duration_s(config),
        )
        summary, result = probe_voltage_candidate(
            reader=self.reader,
            candidate_plan=candidate.flattened_plan,
            candidate_voltage_mv=int(candidate.voltage_mv),
            lock_clock_mhz=int(candidate.target_mhz),
            q2rtx_config=config,
            stable_history=stable_history,
            initial_probe_clock_mhz=self.baseline_clock_mhz,
            nvml_session=self.live_voltage_reader,
            log=self.log,
            phase_label=str(phase_label),
            initial_target_voltage_mv=int(self.start_voltage_mv),
            power_limit_w=self.power_limit_w,
            enforce_target_core_clock_floor=bool(enforce_target_core_clock_floor),
            summarize_saturated_tail=bool(summarize_saturated_tail),
            use_power_limit_floor=bool(use_power_limit_floor),
            min_performance_core_clock_pct=float(self.min_performance_core_clock_pct),
            reset_plan=self.runtime_default_plan,
            marker_details=probe_runner_marker_details(self.marker_details, candidate),
            event_callback=self.event_callback,
        )
        outcome = self.outcome_from_probe_result(
            summary,
            result,
            stable_history=stable_history,
            q2rtx_config=config,
            enforce_core_clock_floor=bool(enforce_target_core_clock_floor),
        )
        emit_voltage_probe_finished(
            self.event_callback,
            candidate,
            outcome,
            stage=str(phase_label),
            max_clock_drop_pct=self.max_clock_drop_pct,
        )
        return outcome

    @property
    def max_clock_drop_pct(self) -> float:
        return max(0.0, 100.0 - float(self.min_performance_core_clock_pct))

    def outcome_from_probe_result(
        self,
        summary: AutoUvProbeSummary,
        result: object,
        *,
        stable_history: list[AutoUvProbeSummary],
        q2rtx_config: Q2RTXStabilityConfig | None = None,
        enforce_core_clock_floor: bool = True,
    ) -> VoltageProbeOutcome:
        baseline_probe = stable_history[0] if stable_history else None
        baseline_core_clock_mhz = self.baseline_clock_mhz
        if baseline_core_clock_mhz is None and baseline_probe is not None:
            baseline_core_clock_mhz = baseline_probe.avg_core_clock_mhz
        effective_config = q2rtx_config or self.q2rtx_config
        cuda_required = bool(getattr(effective_config, "companion_command", None))
        decision = evaluate_stable_run(
            result,
            baseline_fps=(
                float(baseline_probe.avg_fps)
                if baseline_probe is not None and baseline_probe.avg_fps is not None
                else None
            ),
            baseline_power_w=(
                float(baseline_probe.avg_power_w)
                if baseline_probe is not None
                and baseline_probe.avg_power_w is not None
                else None
            ),
            baseline_core_clock_mhz=(
                float(baseline_core_clock_mhz)
                if baseline_core_clock_mhz is not None
                else None
            ),
            power_limit_w=self.power_limit_w,
            cuda_required=cuda_required,
            companion_result={"success": True} if cuda_required else None,
            fatal_output_found=bool(getattr(result, "fatal_output_matches", [])),
            xid_found=bool(getattr(result, "xid_messages", [])),
            thresholds=StabilityThresholds(
                min_core_clock_pct=(
                    float(self.min_performance_core_clock_pct)
                    if bool(enforce_core_clock_floor)
                    else 0.0
                )
            ),
        )
        return VoltageProbeOutcome(
            decision=decision,
            measured_core_clock_mhz=summary.avg_core_clock_mhz,
            measured_voltage_mv=summary.avg_voltage_mv,
            raw_probe=summary,
            raw_result=result,
        )


def probe_ui_target_duration_s(config: Q2RTXStabilityConfig) -> float | None:
    q2rtx_duration_s = (
        float(config.duration_s)
        if int(getattr(config, "duration_s", 0) or 0) > 0
        else None
    )
    if q2rtx_duration_s is None:
        return None
    return max(
        1.0,
        float(q2rtx_duration_s)
        + companion_duration_s_from_command(getattr(config, "companion_command", None)),
    )


def probe_runner_marker_details(
    base_details: dict | None,
    candidate: VfCurveCandidate,
) -> dict:
    details = dict(base_details or {})
    metadata = getattr(candidate, "metadata", {}) or {}
    if isinstance(metadata, dict):
        for key in (
            "custom_target",
            "auto_oc",
            "tail_rise_bins",
            "auto_uv_mode",
            "auto_uv_requested_mode",
            "generated_profile_tier",
        ):
            if key in metadata and key not in details:
                details[key] = metadata[key]
    return details
