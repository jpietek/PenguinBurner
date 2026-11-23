"""Rows of the Auto-UV runs table: one per probe, built from scan events.

A probe_start opens a running row, load_telemetry advances its progress, and
probe_result settles it. The rules here (which row an event belongs to, its
state, the OC delta against the measured baseline, the cap reason text) are
shared by every view of the table.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .scan_events import probe_decision_label, probe_failure_label

ACTIVE_DECISIONS = frozenset({"running", "stopping"})


@dataclass(slots=True)
class ScanRun:
    """One row: the latest merged probe payload plus its live state."""

    payload: dict
    decision: str
    row_state: str
    progress: dict = field(default_factory=dict)
    oc: tuple[int, int] | None = None
    restored: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return probe_key(self.payload)

    @property
    def active(self) -> bool:
        return is_active_decision(self.decision)

    @property
    def kind(self) -> str:
        """baseline, final, resume or probe: how the row is labelled."""
        stage = str(self.payload.get("stage", "")).lower()
        if is_base_baseline(self.payload):
            return "baseline"
        if stage == "resume-verify":
            return "resume"
        if "final" in stage:
            return "final"
        return "probe"

    @property
    def candidate_id(self) -> str:
        voltage, clock = self.key
        return f"{voltage}mv-{clock}mhz" if voltage and clock else ""


class ScanRuns:
    """The table's rows in order, with the stock baseline they compare to."""

    def __init__(self) -> None:
        self.rows: list[ScanRun] = []
        self.base_baseline: dict | None = None
        self._progress_by_probe: dict[tuple[str, str], dict] = {}
        self._oc_by_probe: dict[tuple[str, str], tuple[int, int]] = {}

    def clear(self) -> None:
        self.rows.clear()
        self.base_baseline = None
        self._progress_by_probe.clear()
        self._oc_by_probe.clear()

    def probe_started(self, payload: dict) -> int:
        progress = self._merged_progress(probe_key(payload), payload_progress(payload))
        self._progress_by_probe[probe_key(payload)] = dict(progress)
        self.rows.append(
            ScanRun(
                payload=dict(payload),
                decision="Running",
                row_state="running",
                progress=progress,
                oc=self._oc_for(payload),
                restored=bool(payload.get("restored")),
            )
        )
        return len(self.rows) - 1

    def probe_result(self, payload: dict) -> int:
        if is_base_baseline(payload):
            self.base_baseline = dict(payload)
        index = self.running_row(payload)
        key = probe_key(payload)
        progress = self._merged_progress(key, payload_progress(payload))
        label = progress_label(payload)
        if label:
            progress = {**progress, "label": label}
        run = ScanRun(
            payload=dict(payload),
            decision=probe_decision_label(payload),
            row_state=row_state(payload, running=False),
            progress=progress,
            oc=self._oc_for(payload),
            restored=bool(payload.get("restored")),
        )
        if index is None:
            self.rows.append(run)
            return len(self.rows) - 1
        self.rows[index] = run
        return index

    def telemetry(self, payload: dict) -> int | None:
        index = self.running_row(payload)
        if index is None:
            return None
        key = probe_key(payload)
        progress = self._merged_progress(key, payload_progress(payload))
        self._progress_by_probe[key] = progress
        run = self.rows[index]
        run.progress = dict(progress)
        cap = payload.get("perf_cap_reason")
        if perf_cap_reason_text(cap, payload.get("hw_power_brake_samples")):
            run.payload["perf_cap_reason"] = cap
            run.payload["hw_power_brake_samples"] = payload.get("hw_power_brake_samples")
        return index

    def candidate_curve(self, payload: dict) -> int | None:
        """Record a curve's OC delta and fill it into its row, if shown."""
        key = probe_key(payload)
        if not is_base_baseline(payload):
            oc = oc_from_measured_baseline(payload, self.base_baseline)
            if oc is None:
                return None
            self._oc_by_probe[key] = oc
        index = self.row_by_key(key)
        if index is not None and not is_base_baseline(payload):
            self.rows[index].oc = self._oc_by_probe[key]
        return index

    def mark_active(self, *, decision: str, row_state: str, label: str) -> list[int]:
        """Stop/Abort/Fail every running row; the progress keeps its time."""
        changed = []
        for index, run in enumerate(self.rows):
            if not run.active:
                continue
            run.decision = decision
            run.row_state = row_state
            run.progress = {**self._progress_by_probe.get(run.key, run.progress), "label": label}
            changed.append(index)
        return changed

    def running_row(self, payload: dict) -> int | None:
        key = probe_key(payload)
        for index in range(len(self.rows) - 1, -1, -1):
            if self.rows[index].active and self.rows[index].key == key:
                return index
        return None

    def row_by_key(self, key: tuple[str, str]) -> int | None:
        for index in range(len(self.rows) - 1, -1, -1):
            if self.rows[index].key == key:
                return index
        return None

    def _oc_for(self, payload: dict) -> tuple[int, int] | None:
        measured = oc_from_measured_baseline(payload, self.base_baseline)
        if measured is not None:
            return measured
        return self._oc_by_probe.get(probe_key(payload)) or payload_oc_progress(payload)

    def _merged_progress(self, key: tuple[str, str], incoming: dict) -> dict:
        progress = dict(self._progress_by_probe.get(key, {}))
        for name in ("elapsed_s", "target_duration_s"):
            value = incoming.get(name)
            if value is not None:
                progress[name] = value
        if incoming.get("label"):
            progress["label"] = incoming["label"]
        else:
            progress.setdefault("label", "")
        progress.setdefault("elapsed_s", None)
        progress.setdefault("target_duration_s", None)
        return progress

    def delta_pct(self, run: ScanRun, metric: str) -> float | None:
        """Percent change of fps, power_w or efficiency_fps_per_w vs stock."""
        if self.base_baseline is None or not is_base_baseline(self.base_baseline):
            return None
        if run.kind == "baseline":
            return None
        baseline = to_float(self.base_baseline.get(metric))
        current = to_float(run.payload.get(metric))
        if current is None or baseline is None or baseline == 0.0:
            return None
        return (current - baseline) / baseline * 100.0


def row_state(payload: dict, *, running: bool) -> str:
    if running:
        return "running"
    stage = str(payload.get("stage", "")).lower()
    decision = str(payload.get("decision", payload.get("status", ""))).lower()
    severity = str(payload.get("failure_severity", "")).lower()
    reason = str(payload.get("reason", "")).lower()
    text = f"{decision} {reason}"
    if (
        "base" in stage or "stock" in stage or stage == "baseline"
    ) and "fail" not in text and "error" not in text:
        return "baseline"
    if severity == "recoverable":
        return "warning"
    if severity in {"critical", "unsafe"}:
        return "error"
    if any(token in text for token in ("fail", "failed", "error", "crash", "stall", "timeout")):
        return "error"
    if any(
        token in text
        for token in (
            "warn",
            "guardrail",
            "floor",
            "miss",
            "retry",
            "overclock",
            "reject",
            "budget",
        )
    ):
        return "warning"
    if decision in {"accept", "pass"}:
        return "good"
    return "neutral"


def metric_delta_tone(metric: str, delta_pct: float) -> str:
    """good/warning/error colour for a delta vs stock; "" leaves it plain."""
    if metric == "efficiency_fps_per_w":
        if delta_pct > 0.5:
            return "good"
        return "warning" if delta_pct >= -0.5 else "error"
    if abs(delta_pct) < 0.05:
        return ""
    if metric == "fps":
        if delta_pct > 0.05:
            return "good"
        return "warning" if delta_pct >= -1.5 else "error"
    if delta_pct <= -3.0:
        return "good"
    return "warning" if delta_pct <= 2.0 else "error"


def is_base_baseline(payload: dict) -> bool:
    stage = str(payload.get("stage", "")).lower()
    return stage in {"base-baseline", "stock-baseline"}


def probe_key(payload: dict) -> tuple[str, str]:
    return (format_int(payload.get("voltage_mv")), format_int(payload.get("clock_mhz")))


def is_active_decision(text: str) -> bool:
    return str(text or "").strip().lower() in ACTIVE_DECISIONS


def progress_label(payload: dict) -> str:
    stage = str(payload.get("stage", "")).lower()
    decision = str(payload.get("decision", "")).strip().lower()
    if decision in {"fail", "failed", "error"}:
        return probe_failure_label(payload)
    if "final" in stage:
        return "Final verification"
    return ""


def payload_progress(payload: dict) -> dict:
    return {
        "elapsed_s": to_float(payload.get("elapsed_s")),
        "target_duration_s": to_float(payload.get("target_duration_s")),
        "label": progress_label(payload),
    }


def payload_oc_progress(payload: dict) -> tuple[int, int] | None:
    return oc_from_measured_baseline(payload, None)


def oc_from_measured_baseline(
    payload: dict,
    baseline_payload: dict | None,
) -> tuple[int, int] | None:
    target_clock_mhz = payload_number(
        payload,
        "lock_clock_mhz",
        "clock_mhz",
        "target_clock_mhz",
        "auto_oc_target_clock_mhz",
    )
    baseline_clock_mhz = _measured_baseline_clock_mhz(payload, baseline_payload)
    if target_clock_mhz is None or baseline_clock_mhz is None:
        return None
    delta_mhz = round(float(target_clock_mhz) - float(baseline_clock_mhz))
    return (delta_mhz, delta_mhz)


def _measured_baseline_clock_mhz(
    payload: dict,
    baseline_payload: dict | None,
) -> float | None:
    baseline_clock_mhz = payload_number(
        payload,
        "auto_oc_baseline_clock_mhz",
        "measured_baseline_clock_mhz",
        "baseline_core_clock_mhz",
        "baseline_clock_mhz",
        "base_avg_core_clock_mhz",
    )
    if baseline_clock_mhz is not None:
        return baseline_clock_mhz
    if baseline_payload is None:
        return None
    return payload_number(
        baseline_payload,
        "measured_clock_mhz",
        "avg_core_clock_mhz",
    )


def progress_time_text(elapsed_s, target_s) -> str:
    elapsed = clamped_elapsed_s(elapsed_s, target_s)
    return f"{format_duration_compact(elapsed)} / {format_duration_compact(target_s)}"


def clamped_elapsed_s(elapsed_s, target_s) -> float:
    try:
        elapsed = max(0.0, float(elapsed_s))
        target = max(0.0, float(target_s))
    except (TypeError, ValueError):
        return 0.0
    if target > 0.0:
        return min(elapsed, target)
    return elapsed


def format_duration_compact(seconds) -> str:
    if seconds in (None, ""):
        return "n/a"
    try:
        total_seconds = max(0, round(float(seconds)))
    except (TypeError, ValueError):
        return "n/a"
    if total_seconds < 60:
        return f"{total_seconds}s"
    if total_seconds < 3600:
        minutes, remainder_seconds = divmod(total_seconds, 60)
        if remainder_seconds:
            return f"{minutes}min {remainder_seconds}s"
        return f"{minutes}min"
    hours, remainder_seconds = divmod(total_seconds, 3600)
    minutes = round(remainder_seconds / 60.0)
    if minutes >= 60:
        hours += 1
        minutes = 0
    if minutes:
        return f"{hours}h {minutes}min"
    return f"{hours}h"


def measured_clock_value(payload: dict):
    return payload.get("measured_clock_mhz", payload.get("avg_core_clock_mhz"))


def payload_number(payload: dict, *keys: str):
    for key in keys:
        value = to_float(payload.get(key))
        if value is not None:
            return value
    return None


def perf_cap_reason_text(value, hw_power_brake_samples: object = 0) -> str:
    text = str(value or "").strip().replace(",", "+")
    if not _brake_count(hw_power_brake_samples):
        return text
    # The summarizer may suppress a sparse power token, but a power-delivery
    # brake is never something the Cap column should quietly drop.
    if "power-brake" in text.lower():
        return text
    return f"{text}+hw-power-brake" if text and text != "none" else "hw-power-brake"


def perf_cap_reason_tooltip(value, hw_power_brake_samples: object = 0) -> str:
    text = perf_cap_reason_text(value, hw_power_brake_samples)
    if not text:
        return ""
    tooltip = f"NVML clocks throttle reason: {text}"
    brake_samples = _brake_count(hw_power_brake_samples)
    if brake_samples:
        tooltip += (
            f"\nhw-power-brake on {brake_samples} samples: the board's power "
            "delivery limited the clock, not the configured power limit"
        )
    return tooltip


def _brake_count(value: object) -> int:
    number = to_float(value)
    return max(0, int(number)) if number is not None else 0


def to_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_int(value) -> str:
    number = to_float(value)
    if number is None:
        return ""
    return str(round(number))


def format_oc_progress(value: tuple[int, int] | None) -> str:
    # Auto-OC runs only as a late stage of a Performance scan, so most rows
    # (the whole UV sweep, and every Efficiency/Balanced row) have no OC data.
    # Leave those cells blank rather than showing a zero-like placeholder.
    if value is None:
        return ""
    oc_mhz, _limit_mhz = value
    return format_signed_mhz(round(float(oc_mhz)))


def format_oc_progress_for_payload(
    payload: dict,
    value: tuple[int, int] | None,
) -> str:
    if is_base_baseline(payload):
        return "-"
    return format_oc_progress(value)


def format_signed_mhz(value: int) -> str:
    return f"{int(value):+d} MHz"


def format_float(value, *, precision: int = 2) -> str:
    number = to_float(value)
    if number is None:
        return ""
    return f"{number:.{precision}f}"
