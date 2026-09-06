"""Test-only power-governor simulation for power-bound scan scenarios.

Closes the loop the plain probe fakes cannot express: given an APPLIED V/F
plan, a power model, and a board-power limit, compute where the governor
settles and synthesize the telemetry a probe would collect there. This is
what lets the suite state "measured clock is below target BECAUSE of the
power cap" instead of merely asserting a number.

Model: P(f, V) = idle_w + k * f * (V/1000)^2, plus an explicit synthetic
spike-reserve margin. Tests exercise several coefficient/margin combinations
and assert only regime invariants; this helper is not fitted or presented as
a hardware prediction for any particular 5090.

Beyond the settled point the model also expresses the three telemetry
behaviors the live 2026-08-04 Blackwell log shows and flat fakes cannot:
load V-droop (the card operates a bin below the requested lock, so measured
clock trails the request even with power to spare), per-sample jitter around
the settled point, and an intermittent perf-cap reason duty cycle. Those are
what the scan's saturation evidence rules actually read, so scenarios can
distinguish "power wall" from "Blackwell always measures a little low".

``GovernorProbeHarness`` wires the model into the real probe seams: it
synthesizes telemetry, then runs the product's own summarizer and stability
decision over it. Scenarios therefore exercise the shipped algorithms
end to end and only the hardware is simulated.

Test infrastructure only — never product code (the scan must not depend on
a governor model; it reads real telemetry).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from auto_uv.domain.types import (
    AutoUvProbeSummary,
    FailureKind,
    FailureSeverity,
    StableRunDecision,
)
from auto_uv.probes.stability_decision import (
    StabilityThresholds,
    evaluate_loaded_telemetry,
)
from auto_uv.probes.summary import summarize_q2rtx_cuda_probe
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome
from stability.q2rtx.models import Q2RTXBenchmarkSummary


@dataclass(frozen=True, slots=True)
class GovernorPowerModel:
    dynamic_w_per_mhz_v2: float
    idle_w: float = 40.0
    spike_margin_mv: float = 15.0
    # Load V-droop: the card operates this far below the requested lock
    # voltage, so its measured clock trails the request even with power
    # headroom to spare (observed -5 to -25mV across the live Blackwell log).
    vdroop_mv: float = 0.0
    # Per-sample telemetry spread around the settled point.
    clock_jitter_mhz: float = 0.0
    power_jitter_w: float = 0.0
    # Fraction of busy samples that report the power cap while saturated. A
    # duty below 1.0 models the driver naming the cap only intermittently.
    cap_reason_duty: float = 1.0
    # Reason reported on samples that are not naming the power cap. "none"
    # is reason-bearing telemetry; None means the sample carries no reason.
    idle_cap_reason: str | None = "none"
    # A second, non-power cap reason mixed into capped samples (thermal), so
    # scenarios can prove a thermal cap never buys a power-wall exemption.
    secondary_cap_reason: str | None = None
    # Board power-delivery protection (EDPp/OCP): fraction of busy samples on
    # which the hardware brake asserts. Deliberately independent of the
    # software cap — a board trips its brake on transients while aggregate
    # draw sits well under the configured limit — and typically sparse, which
    # is what makes it disappear from a summarized perf-cap reason.
    brake_duty: float = 0.0
    # Report the power cap even when the board is not at its wall. Blackwell
    # does this: the 2026-08-04 log summarizes sw-power on a probe drawing
    # 287W under a 319W cap, so the reason token alone is weaker evidence
    # than the sample-power it comes with.
    reports_power_cap_off_the_wall: bool = False
    # Genuine V/F instability floor: candidates at or below this voltage
    # crash rather than merely clocking low.
    unstable_below_mv: int | None = None


def scenario_5090_power_models() -> tuple[GovernorPowerModel, ...]:
    """Distinct synthetic response curves used to test invariant behavior."""
    return (
        GovernorPowerModel(
            dynamic_w_per_mhz_v2=0.18,
            idle_w=30.0,
            spike_margin_mv=0.0,
        ),
        GovernorPowerModel(
            dynamic_w_per_mhz_v2=0.20,
            idle_w=40.0,
            spike_margin_mv=15.0,
        ),
        GovernorPowerModel(
            dynamic_w_per_mhz_v2=0.22,
            idle_w=50.0,
            spike_margin_mv=25.0,
        ),
    )


def scenario_5090_blackwell_models() -> tuple[GovernorPowerModel, ...]:
    """Response curves carrying the observed Blackwell telemetry behaviors.

    Each varies droop, jitter, and how often the driver actually names the
    power cap, so a scan rule that only works against clean, always-labeled
    telemetry fails here.
    """
    return (
        GovernorPowerModel(
            dynamic_w_per_mhz_v2=0.18,
            idle_w=30.0,
            spike_margin_mv=0.0,
            vdroop_mv=5.0,
            clock_jitter_mhz=12.0,
            power_jitter_w=8.0,
            cap_reason_duty=1.0,
        ),
        GovernorPowerModel(
            dynamic_w_per_mhz_v2=0.20,
            idle_w=40.0,
            spike_margin_mv=15.0,
            vdroop_mv=12.0,
            clock_jitter_mhz=25.0,
            power_jitter_w=18.0,
            cap_reason_duty=0.75,
        ),
        GovernorPowerModel(
            dynamic_w_per_mhz_v2=0.22,
            idle_w=50.0,
            spike_margin_mv=25.0,
            vdroop_mv=25.0,
            clock_jitter_mhz=40.0,
            power_jitter_w=25.0,
            cap_reason_duty=0.6,
            secondary_cap_reason="hw-thermal",
        ),
    )


def point_power_w(
    model: GovernorPowerModel,
    *,
    clock_mhz: float,
    voltage_mv: float,
) -> float:
    volts = float(voltage_mv) / 1000.0
    return model.idle_w + model.dynamic_w_per_mhz_v2 * float(clock_mhz) * volts * volts


def sustainable_clock_mhz(
    model: GovernorPowerModel,
    *,
    voltage_mv: float,
    power_limit_w: float,
) -> float:
    """The highest clock ``power_limit_w`` sustains at ``voltage_mv``.

    This is the power wall a reclaim climb converges on. It rises as the
    voltage descends, which is why undervolting is the remedy for a power
    wall and clock reduction is not.
    """
    volts = float(voltage_mv) / 1000.0
    dynamic_budget_w = float(power_limit_w) - float(model.idle_w)
    if dynamic_budget_w <= 0.0 or volts <= 0.0:
        return 0.0
    return dynamic_budget_w / (model.dynamic_w_per_mhz_v2 * volts * volts)


def settle_operating_point(
    plan: list[dict],
    *,
    model: GovernorPowerModel,
    power_limit_w: float,
) -> dict:
    """Where the governor parks on ``plan`` under ``power_limit_w``.

    The governor targets the highest clock whose power fits the limit, backing
    off by the model's spike margin when the limit binds, and delivers it at
    the LOWEST curve voltage that reaches that clock — which is what makes a
    flattened plan operate at its lock voltage rather than at the top of its
    flat. Load V-droop then pushes the operating point one or more bins below
    that, onto the plan's ramp, so the measured clock trails the requested
    lock exactly the way the live Blackwell log shows.

    Returns voltage_mv/clock_mhz/power_w plus power_capped.
    """
    points = sorted(
        (
            (int(point["voltage_mv"]), int(point["target_mhz"]))
            for point in plan
            if point.get("voltage_mv") is not None
            and point.get("target_mhz") is not None
        ),
        key=lambda item: item[0],
    )
    if not points:
        raise ValueError("plan has no usable V/F points")

    def cheapest_delivery(clock_mhz: int) -> tuple[int, int]:
        """The lowest-voltage point that delivers ``clock_mhz``."""
        for point in points:
            if point[1] >= clock_mhz:
                return point
        return points[-1]

    def fits(point: tuple[int, int]) -> bool:
        voltage_mv, clock_mhz = point
        return (
            point_power_w(model, clock_mhz=clock_mhz, voltage_mv=voltage_mv)
            <= float(power_limit_w)
        )

    requested_clock_mhz = max(clock for _voltage, clock in points)
    settle_voltage_mv, settle_clock_mhz = points[0]
    for clock_mhz in sorted({clock for _voltage, clock in points}, reverse=True):
        delivery = cheapest_delivery(clock_mhz)
        if fits(delivery):
            settle_voltage_mv, settle_clock_mhz = delivery
            break
    # Power-bound means the governor could not reach the clock the plan asks
    # for — not merely that some unvisited point on the curve is unaffordable.
    power_capped = settle_clock_mhz < requested_clock_mhz
    if power_capped and model.spike_margin_mv > 0.0:
        margin_ceiling_mv = float(settle_voltage_mv) - float(model.spike_margin_mv)
        margined = [point for point in points if point[0] <= margin_ceiling_mv]
        if margined:
            settle_voltage_mv, settle_clock_mhz = margined[-1]
    if model.vdroop_mv > 0.0:
        droop_ceiling_mv = float(settle_voltage_mv) - float(model.vdroop_mv)
        drooped = [point for point in points if point[0] <= droop_ceiling_mv]
        if drooped:
            settle_voltage_mv, settle_clock_mhz = drooped[-1]
    return {
        "voltage_mv": int(settle_voltage_mv),
        "clock_mhz": int(settle_clock_mhz),
        "power_w": point_power_w(
            model,
            clock_mhz=settle_clock_mhz,
            voltage_mv=settle_voltage_mv,
        ),
        "power_capped": bool(power_capped),
    }


def synthesize_busy_samples(
    operating_point: dict,
    *,
    count: int = 8,
    first_elapsed_s: float = 6.0,
    model: GovernorPowerModel | None = None,
) -> list[dict]:
    """Busy telemetry a probe would collect at the settled operating point.

    Power-capped points carry the driver's sw-power perf-cap reason — the
    same evidence the real scan reads to distinguish governor descent from
    silent reliability demotion. Passing ``model`` adds that model's jitter
    and perf-cap duty cycle; without it the samples are flat and always
    labeled, which is the well-behaved control case.

    A model's ``brake_duty`` mixes in the board's hw-power-brake bit on its
    own schedule, so a scenario can express a protection brake firing while
    the software cap is nowhere near reached.
    """
    jitter_clock = float(getattr(model, "clock_jitter_mhz", 0.0) or 0.0)
    jitter_power = float(getattr(model, "power_jitter_w", 0.0) or 0.0)
    duty = float(getattr(model, "cap_reason_duty", 1.0) or 1.0)
    brake_duty = float(getattr(model, "brake_duty", 0.0) or 0.0)
    idle_reason = getattr(model, "idle_cap_reason", "none") if model else "none"
    secondary = getattr(model, "secondary_cap_reason", None) if model else None
    samples: list[dict] = []
    for index in range(int(count)):
        wobble = math.sin(float(index) * 1.7)
        names_cap = bool(operating_point["power_capped"]) or bool(
            getattr(model, "reports_power_cap_off_the_wall", False)
        )
        capped = names_cap and _duty_cycle_hit(index, duty)
        tokens: list[str] = []
        if capped:
            tokens.append("sw-power")
            if secondary:
                tokens.append(str(secondary))
        if _duty_cycle_hit(index, brake_duty):
            tokens.append("hw-power-brake")
        reason = "+".join(tokens) if tokens else idle_reason
        samples.append(
            {
                "elapsed_s": float(first_elapsed_s) + float(index),
                "gpu_util_pct": 99.0,
                "power_w": max(
                    0.0, float(operating_point["power_w"]) + wobble * jitter_power
                ),
                "core_clock_mhz": max(
                    0.0,
                    float(operating_point["clock_mhz"]) + wobble * jitter_clock,
                ),
                "voltage_mv": float(operating_point["voltage_mv"]),
                "perf_cap_reason": reason,
            }
        )
    return samples


def synthesize_probe_samples(
    operating_point: dict,
    *,
    model: GovernorPowerModel | None = None,
    busy_count: int = 12,
    ramp_count: int = 3,
) -> list[dict]:
    """A full probe's telemetry: the idle/ramp head plus the busy window.

    The scan drops the ramp before deciding anything, so a scenario that
    omits it cannot prove the busy filter and the saturation evidence rules
    agree about which samples speak for the run.
    """
    ramp = [
        {
            "elapsed_s": 1.0 + float(index),
            "gpu_util_pct": 20.0 + 20.0 * float(index),
            "power_w": float(model.idle_w if model else 40.0) + 15.0 * float(index),
            "core_clock_mhz": 800.0 + 400.0 * float(index),
            "voltage_mv": 700.0,
            "perf_cap_reason": "none",
        }
        for index in range(max(0, int(ramp_count)))
    ]
    busy = synthesize_busy_samples(
        operating_point,
        count=int(busy_count),
        first_elapsed_s=1.0 + float(max(0, int(ramp_count))),
        model=model,
    )
    return [*ramp, *busy]


def _duty_cycle_hit(index: int, duty: float) -> bool:
    """Deterministically spread ``duty`` reason-bearing samples over a run."""
    if duty >= 1.0:
        return True
    if duty <= 0.0:
        return False
    return math.floor((index + 1) * duty) > math.floor(index * duty)


@dataclass
class GovernorProbeHarness:
    """Probe a candidate against the simulated governor using real decisions.

    Only the telemetry is synthetic: the summary and the pass/fail verdict
    come from the shipped ``summarize_q2rtx_cuda_probe`` and
    ``evaluate_loaded_telemetry``, so a scenario proves what the product
    would actually decide on this hardware behavior.
    """

    base_curve: list[dict]
    model: GovernorPowerModel
    power_limit_w: int
    baseline_core_clock_mhz: float
    baseline_power_w: float
    baseline_fps: float
    probes: list[dict] = field(default_factory=list)

    def operating_point(self, plan: list[dict]) -> dict:
        return settle_operating_point(
            plan,
            model=self.model,
            power_limit_w=float(self.power_limit_w),
        )

    def probe_candidate(self, candidate, **_kwargs) -> VoltageProbeOutcome:
        """Auto-OC / sweep runner seam: same probe, runner-shaped signature."""
        return self.probe(candidate)

    def probe(self, candidate) -> VoltageProbeOutcome:
        unstable_below_mv = self.model.unstable_below_mv
        if unstable_below_mv is not None and int(candidate.voltage_mv) <= int(
            unstable_below_mv
        ):
            self.probes.append(
                {
                    "voltage_mv": int(candidate.voltage_mv),
                    "requested_mhz": int(candidate.target_mhz),
                    "measured_mhz": None,
                    "power_w": None,
                    "power_capped": False,
                    "perf_cap_reason": None,
                    "passed": False,
                    "crashed": True,
                }
            )
            return VoltageProbeOutcome(
                decision=StableRunDecision(
                    False,
                    FailureKind.Q2RTX_FAILED,
                    FailureSeverity.CRITICAL,
                    "q2rtx crashed",
                ),
            )
        settled = self.operating_point(list(candidate.flattened_plan))
        samples = synthesize_probe_samples(settled, model=self.model)
        fps = float(self.baseline_fps) * (
            float(settled["clock_mhz"]) / float(self.baseline_core_clock_mhz)
        )
        result = SimpleNamespace(
            telemetry_samples=samples,
            companion_telemetry_samples=[],
            telemetry_summary=lambda: {},
            benchmark_summary=_benchmark_summary(fps),
            reason="ok",
            success=True,
            log_path=Path("/tmp/q2rtx.log"),
        )
        summary = summarize_q2rtx_cuda_probe(
            candidate_voltage_mv=int(candidate.voltage_mv),
            lock_clock_mhz=int(candidate.target_mhz),
            live_voltage_before_mv=int(candidate.voltage_mv),
            live_voltage_after_mv=int(candidate.voltage_mv),
            used_companion_load=True,
            power_limit_w=int(self.power_limit_w),
            result=result,
        )
        decision = evaluate_loaded_telemetry(
            samples,
            baseline_power_w=float(self.baseline_power_w),
            power_limit_w=int(self.power_limit_w),
            thresholds=StabilityThresholds(
            ),
            log_path=None,
        )
        self.probes.append(
            {
                "voltage_mv": int(candidate.voltage_mv),
                "requested_mhz": int(candidate.target_mhz),
                "measured_mhz": float(settled["clock_mhz"]),
                "power_w": float(settled["power_w"]),
                "power_capped": bool(settled["power_capped"]),
                "perf_cap_reason": summary.perf_cap_reason,
                "passed": bool(decision.passed),
                "crashed": False,
            }
        )
        return VoltageProbeOutcome(
            decision=decision,
            measured_core_clock_mhz=summary.avg_core_clock_mhz,
            measured_voltage_mv=summary.avg_voltage_mv,
            raw_probe=summary,
            raw_result=result,
        )


def probe_summary_at(
    harness: GovernorProbeHarness,
    candidate,
) -> AutoUvProbeSummary:
    outcome = harness.probe(candidate)
    assert outcome.raw_probe is not None
    return outcome.raw_probe


def _benchmark_summary(fps: float) -> Q2RTXBenchmarkSummary:
    return Q2RTXBenchmarkSummary(
        reason="target",
        loops=1,
        loops_started=1,
        demo_frames=631,
        render_frames=int(float(fps) * 45.0),
        target_s=45.0,
        measured_s=45.0,
        render_s=45.0,
        drain_s=0.0,
        loop_fps_mean=float(fps),
        fps_avg=float(fps),
        fps_min=float(fps) * 0.9,
        fps_max=float(fps) * 1.1,
        fps_mean=float(fps),
        frame_ms_min=None,
        frame_ms_max=None,
        frame_ms_mean=None,
    )
