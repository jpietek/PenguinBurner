"""What an Auto-UV scan or profile verification shows while it runs.

The scan subprocess streams JSON events; ``ScanSession`` turns each one into
state (stage, candidate, status, tier progress, runs, curves) and returns a
``ScanUpdate`` naming what changed, so every window renders the same scan
from the same rules. It never touches Qt, files or the GPU: answering a
final-choice request and applying the finished profile stay with the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from auto_uv.scan_mode.auto_uv_mode import ADAPTIVE_TIER_MODES, adaptive_tier_option_key

from .scan_events import (
    candidate_id_from_payload,
    event_base_points,
    event_points,
    stage_title,
    status_value,
    top_status_text,
)
from .scan_runs import ScanRuns, clamped_elapsed_s, format_duration_compact

TIER_ORDER = ADAPTIVE_TIER_MODES
STARTING_CANDIDATE = "Writing to main Auto-UV profile store"
DISCARDED_STATUS = "Final verification discarded. No Auto-UV profile was saved."
RESTORED_STATUS = (
    "Restored previous scan measurements and curves. Continuing from saved progress."
)
VERIFY_STAGE = "Profile verification"


@dataclass(frozen=True, slots=True)
class ScanProgress:
    """The header progress bar: dependency download or profile verification."""

    percent: int
    text: str
    detail: str


@dataclass(slots=True)
class ScanUpdate:
    """What one event changed. None means unchanged."""

    event: str = ""
    payload: dict = field(default_factory=dict)
    stage: str | None = None
    candidate: str | None = None
    status: str | None = None
    progress: ScanProgress | None = None
    hide_progress: bool = False
    tiers_changed: bool = False
    runs: list[int] = field(default_factory=list)
    reset: bool = False
    restored: list[ScanUpdate] = field(default_factory=list)
    base_points: list[tuple[float, float]] | None = None
    candidate_curve: tuple[str, list[tuple[float, float]]] | None = None
    tier_curve: tuple[str, list[tuple[float, float]]] | None = None
    probe_marker: dict | None = None
    load_marker: dict | None = None
    live_marker: dict | None = None
    final_choice_request: dict | None = None
    final_result: dict | None = None


@dataclass(frozen=True, slots=True)
class ScanFinish:
    """How a scan process ended and what the window does next."""

    outcome: str  # aborted, failed, discarded, complete, stopped or idle
    log_text: str
    show_error: bool
    apply_profile: bool
    candidate_id: str


@dataclass(frozen=True, slots=True)
class VerifyFinish:
    passed: bool
    stopped: bool
    show_error: bool
    log_text: str


class ScanSession:
    """State of the current (or last) Auto-UV scan and profile verification."""

    def __init__(self) -> None:
        self.runs = ScanRuns()
        self.options: dict = {}
        self.full_scan = False
        self.tier_states: dict[str, str] = {}
        self.stage = ""
        self.candidate = ""
        self.status = ""
        self.progress: ScanProgress | None = None
        self.base_points: list[tuple[float, float]] = []
        # Candidate curves in arrival order; the last one is live.
        self.candidate_curves: list[tuple[str, list[tuple[float, float]]]] = []
        self.tier_curves: dict[str, list[tuple[float, float]]] = {}
        self.probe_marker: dict | None = None
        self.load_marker: dict | None = None
        self.final_result: dict | None = None
        self.discarded = False
        self.aborted = False
        self.last_candidate_id = ""
        self.verify_target_s = 0

    # ---------- scan lifecycle ----------

    def start(self, options: dict) -> None:
        """A new scan with ``options`` from the setup dialog."""
        self.runs.clear()
        self.options = dict(options)
        self.full_scan = str(options.get("auto_uv_mode") or "") == "adaptive"
        self.tier_states = dict.fromkeys(TIER_ORDER, "pending") if self.full_scan else {}
        self._clear_curves()
        self.final_result = None
        self.discarded = self.aborted = False
        self.last_candidate_id = ""
        self.progress = None
        self.stage = "Starting"
        self.candidate = STARTING_CANDIDATE
        self.status = memory_offset_status_text(requested_memory_offset_mhz(options))

    def start_failed(self) -> None:
        self._mark_tiers_unfinished("failed")

    def request_stop(self) -> ScanUpdate:
        update = ScanUpdate(event="stop", stage="Stopping", status="Stopping Auto-UV.")
        update.runs = self.runs.mark_active(
            decision="Stopping", row_state="warning", label="Stopping"
        )
        return self._apply(update)

    def record_final_choice(
        self, *, discarded: bool, aborted: bool, selected_candidate_id: str
    ) -> None:
        if discarded:
            self.discarded = True
        if aborted:
            self.aborted = True
        if selected_candidate_id:
            self.last_candidate_id = selected_candidate_id

    def finish(self, exit_code, stopped_by_user: bool) -> tuple[ScanFinish, ScanUpdate]:
        code = int(exit_code)
        status_name = "finished" if code == 0 else "stopped"
        update = ScanUpdate(event="finished", hide_progress=True)
        failed = code != 0 and not stopped_by_user
        if self.aborted:
            # A user-requested abort exits non-zero but is not a failure.
            outcome = "aborted"
            update.stage, update.status = "Aborted", "Auto-UV aborted by user."
            update.runs = self.runs.mark_active(
                decision="Aborted", row_state="warning", label="Aborted"
            )
            self._mark_tiers_unfinished("stopped")
        elif failed:
            outcome = "failed"
            update.stage, update.status = "Error", "Auto-UV failed."
            update.runs = self.runs.mark_active(
                decision="Failed", row_state="error", label="Failed"
            )
            self._mark_tiers_unfinished("failed")
        elif self.discarded:
            outcome = "discarded"
            update.stage = "Discarded"
            update.runs = self.runs.mark_active(
                decision="Discarded", row_state="error", label="Discarded"
            )
        elif self.final_result is not None:
            outcome = "complete"
            update.stage, update.status = "Complete", "Final verification complete."
        elif stopped_by_user:
            outcome = "stopped"
            update.stage = "Stopped"
            update.runs = self.runs.mark_active(
                decision="Stopped", row_state="error", label="Stopped"
            )
            self._mark_tiers_unfinished("stopped")
        else:
            outcome = "idle"
            update.stage = "Idle"
        update.tiers_changed = True
        finish = ScanFinish(
            outcome=outcome,
            log_text=f"\nAuto-UV process {status_name}: exit_code={exit_code}\n",
            show_error=failed and not self.aborted,
            apply_profile=outcome == "complete",
            candidate_id=self.last_candidate_id,
        )
        self.final_result = None
        self.discarded = self.aborted = False
        return finish, self._apply(update)

    # ---------- events ----------

    def handle_event(self, payload: dict) -> ScanUpdate:
        event = str(payload.get("event", ""))
        update = ScanUpdate(event=event, payload=payload)
        if event == "auto_uv_start":
            update.stage = "Scanning"
        elif event == "scan_resumed":
            self._restore(payload, update)
        elif event == "dependency_progress":
            detail = str(payload.get("detail") or "Downloading dependencies").strip()
            update.stage, update.candidate = "Downloading dependencies", ""
            update.progress = dependency_progress(payload.get("percent", 0), detail)
        elif event == "probe_start":
            update.hide_progress = True
            update.stage = stage_title(payload.get("stage", "Probe"))
            update.candidate = probe_text(payload)
            update.runs = [self.runs.probe_started(payload)]
            update.probe_marker = payload
        elif event == "probe_result":
            update.stage = stage_title(payload.get("stage", "Probe"))
            update.runs = [self.runs.probe_result(payload)]
            update.load_marker = payload
        elif event == "load_telemetry":
            index = self.runs.telemetry(payload)
            update.runs = [] if index is None else [index]
            update.live_marker = payload
        elif event in {"source_curve", "base_curve"}:
            update.hide_progress = True
            update.base_points = event_base_points(payload)
        elif event == "candidate_curve":
            index = self.runs.candidate_curve(payload)
            update.runs = [] if index is None else [index]
            update.candidate_curve = (
                candidate_id_from_payload(payload),
                event_points(payload),
            )
        elif event == "memory_offset_applied":
            update.status = memory_offset_status_text(payload.get("offset_mhz"))
        elif event == "final_choice_request":
            update.final_choice_request = payload
        elif event == "final_choice_discarded":
            self.discarded = True
            update.stage, update.candidate = "Discarded", ""
            update.status = DISCARDED_STATUS
        elif event == "final_result":
            self.final_result = dict(payload)
            self.last_candidate_id = candidate_id_from_payload(payload)
            update.stage, update.candidate = "Complete", probe_text(payload)
            update.final_result = self.final_result
        elif event.startswith("tier_"):
            self._tier_event(event, payload, update)
        return self._apply(update)

    def handle_human_line(self, line: str) -> ScanUpdate:
        lower = line.lower()
        update = ScanUpdate(event="line")
        if "final verification" in lower:
            update.stage = "Final verification"
        elif "candidate" in lower and "mv" in lower:
            update.stage = "Undervolting Candidates Sweep"
            update.candidate = top_status_text(line)
        elif "auto-uv final state" in lower:
            update.stage = "Complete"
            update.candidate = top_status_text(line)
        return self._apply(update)

    def _tier_event(self, event: str, payload: dict, update: ScanUpdate) -> None:
        tier = str(payload.get("tier", ""))
        name = tier.title()
        position = tier_position_text(payload)
        next_tier = str(payload.get("next_tier", ""))
        if event == "tier_started":
            self._set_tier_active(tier)
            update.tiers_changed = True
            update.stage = f"{name} scan{position}"
            update.status = f"Scanning {name} profile{position}."
        elif event == "tier_descent_reused":
            source = str(payload.get("source_tier", "")).title()
            update.status = (
                f"{name} reuses the {source} downsweep ({payload.get('voltage_mv')}mV @ "
                f"{payload.get('target_mhz')}MHz) — starting the Auto-OC climb."
            )
        elif event == "tier_confirmed":
            update.status = (
                f"{name} tier confirmed: "
                f"{payload.get('voltage_mv')}mV @ {payload.get('target_mhz')}MHz"
            )
            points = event_points(payload)
            if points and tier in TIER_ORDER:
                self.tier_curves[tier] = points
                update.tier_curve = (tier, points)
        elif event == "tier_completed":
            update.tiers_changed = self._set_tier_state(tier, "complete")
            update.stage = f"{name} complete{position}"
            update.status = f"{name} profile complete." + (
                f" Continuing with {next_tier.title()}."
                if next_tier
                else " All selected profiles are complete."
            )
        elif event == "tier_skipped":
            update.tiers_changed = self._set_tier_state(tier, "skipped")
            update.status = (
                f"{name} tier skipped: "
                f"{payload.get('reason') or 'no stable candidate found'}"
                + (f". Continuing with {next_tier.title()}." if next_tier else "")
            )

    def _restore(self, payload: dict, update: ScanUpdate) -> None:
        """scan_resumed: replay the saved events as saved measurements."""
        events = [
            item
            for item in payload.get("events", [])
            if isinstance(item, dict) and isinstance(item.get("payload"), dict)
        ]
        self.runs.clear()
        self._clear_curves()
        self.tier_states = (
            dict.fromkeys(TIER_ORDER, "pending")
            if any(item.get("event") == "tier_started" for item in events)
            else {}
        )
        update.reset = update.tiers_changed = True
        safe_probe = safe_curve = candidate_curve = None
        for item in events:
            event, data = str(item["event"]), item["payload"]
            restored = {"event": event, **data, "restored": True}
            if event == "probe_result":
                restored["reason"] = "Saved measurement: " + str(
                    data.get("reason") or "passed"
                )
            update.restored.append(self.handle_event(restored))
            if event == "candidate_curve":
                candidate_curve = data
            elif event == "probe_result" and data.get("decision") == "pass":
                safe_probe = data
                if candidate_curve and all(
                    candidate_curve.get(key) == data.get(key)
                    for key in ("voltage_mv", "clock_mhz")
                ):
                    safe_curve = candidate_curve
        if safe_curve:
            update.restored.append(
                self.handle_event({"event": "candidate_curve", **safe_curve, "restored": True})
            )
        if safe_probe:
            update.probe_marker = update.load_marker = safe_probe
            update.candidate = (
                f"{safe_probe.get('voltage_mv')}mV @ {safe_probe.get('clock_mhz')}MHz (saved)"
            )
        update.stage = "Resuming"
        update.hide_progress = True
        update.status = RESTORED_STATUS

    # ---------- profile verification ----------

    def start_verification(self, *, label: str, workload: str, duration_s: int) -> None:
        self.verify_target_s = max(1, int(duration_s))
        self.stage, self.candidate = VERIFY_STAGE, label
        self.status = f"Verifying {label} with {workload}."
        self.progress = ScanProgress(
            0, verify_progress_text(0, self.verify_target_s), self.status
        )

    def verification_progress(self, percent, *, elapsed_s, target_s, detail: str) -> None:
        self.progress = ScanProgress(
            _percent(percent),
            verify_progress_text(elapsed_s, target_s),
            str(detail or "Verifying profile"),
        )

    def request_verification_stop(self) -> None:
        self.stage, self.status = "Stopping", "Stopping profile verification."

    def finish_verification(self, exit_code, stopped_by_user: bool) -> VerifyFinish:
        passed = int(exit_code) == 0
        if passed:
            target = self.verify_target_s
            self.status = "Profile verification complete."
            self.stage = "Idle"
            self.progress = ScanProgress(
                100, verify_progress_text(target, target), self.status
            )
        else:
            self.progress = None
            self.status = (
                "Profile verification stopped."
                if stopped_by_user
                else "Profile verification failed."
            )
            self.stage = "Idle" if stopped_by_user else "Error"
        # The candidate line only ever names what a scan or verification runs.
        self.candidate = ""
        self.load_marker = None
        return VerifyFinish(
            passed=passed,
            stopped=bool(stopped_by_user) and not passed,
            show_error=not passed and not stopped_by_user,
            log_text=f"\nProfile verification finished: exit_code={exit_code}\n",
        )

    # ---------- state ----------

    def _apply(self, update: ScanUpdate) -> ScanUpdate:
        if update.stage is not None:
            self.stage = update.stage
        if update.candidate is not None:
            self.candidate = update.candidate
        if update.status is not None:
            self.status = update.status
        if update.progress is not None:
            self.progress = update.progress
        elif update.hide_progress:
            self.progress = None
        if update.base_points is not None:
            self.base_points = list(update.base_points)
        if update.candidate_curve is not None:
            self.candidate_curves.append(update.candidate_curve)
        if update.probe_marker is not None:
            self.probe_marker = update.probe_marker
        if update.load_marker is not None:
            self.load_marker = update.load_marker
        if update.live_marker is not None:
            self.load_marker = update.live_marker
        return update

    def _clear_curves(self) -> None:
        self.base_points = []
        self.candidate_curves = []
        self.tier_curves = {}
        self.probe_marker = self.load_marker = None

    def _set_tier_active(self, tier: str) -> None:
        normalized = normalized_tier(tier)
        if normalized is None:
            return
        if not self.tier_states:
            self.tier_states = dict.fromkeys(TIER_ORDER, "pending")
        for previous in TIER_ORDER[: TIER_ORDER.index(normalized)]:
            if self.tier_states.get(previous) in {"pending", "active"}:
                self.tier_states[previous] = "complete"
        self.tier_states[normalized] = "active"

    def _set_tier_state(self, tier: str, state: str) -> bool:
        normalized = normalized_tier(tier)
        if normalized is None:
            return False
        if not self.tier_states:
            self.tier_states = dict.fromkeys(TIER_ORDER, "pending")
        self.tier_states[normalized] = state
        return True

    def _mark_tiers_unfinished(self, state: str) -> None:
        terminal = state if state in {"stopped", "failed"} else "stopped"
        for tier, current in self.tier_states.items():
            if current == "active":
                self.tier_states[tier] = terminal
            elif current == "pending":
                self.tier_states[tier] = "not-run"


def normalized_tier(value: object) -> str | None:
    tier = str(value or "").strip().lower()
    return tier if tier in TIER_ORDER else None


def probe_text(payload: dict) -> str:
    voltage = status_value(payload.get("voltage_mv") or payload.get("candidate_voltage_mv"))
    clock = status_value(payload.get("clock_mhz") or payload.get("lock_clock_mhz"))
    return f"{voltage or 'n/a'} mV @ {clock or 'n/a'} MHz"


def tier_position_text(payload: dict) -> str:
    try:
        position = int(payload.get("position") or 0)
        total = int(payload.get("total") or 0)
    except (TypeError, ValueError):
        return ""
    if position <= 0 or total <= 0:
        return ""
    return f" ({position}/{total})"


def requested_memory_offset_mhz(options: dict) -> int:
    """The memory-clock offset a scan opens with, before the scan confirms it.

    Full scans carry per-tier offsets and open on Efficiency's. NVML offsets
    are transfer rate (MT/s); the memory clock moves by half.
    """
    requested = options.get("auto_uv_memory_offset_mhz")
    if requested in (None, ""):
        requested = options.get(
            adaptive_tier_option_key(TIER_ORDER[0], "memory_offset_mhz")
        )
    return int(requested or 0) // 2


def memory_offset_status_text(offset_mhz) -> str:
    """Status text for the memory offset applied during the scan (MHz)."""
    try:
        mhz = int(offset_mhz)
    except (TypeError, ValueError):
        mhz = 0
    if mhz == 0:
        return "Memory offset: none"
    return f"Memory offset: {mhz:+d} MHz memory clock"


def dependency_progress(percent, detail: str) -> ScanProgress:
    value = _percent(percent)
    return ScanProgress(value, f"Downloading dependencies {value}%", detail)


def verify_progress_text(elapsed_s, target_s) -> str:
    shown = clamped_elapsed_s(elapsed_s, target_s)
    return (
        f"Verifying profile {format_duration_compact(shown)} / "
        f"{format_duration_compact(target_s)}"
    )


def pre_scan_silent_fan(checkbox_checked: bool, running_info: dict | None) -> bool:
    """The silent-fan intent to restore when the scan's profile is applied.

    A foreground scan resets the GPU to stock, so mid-scan the running
    profile reads as fan-off. Capture the Silent fan curve choice OR the live
    running profile's flag before the scan starts; the completion apply uses
    it so a scan never drops a fan curve the user had running.
    """
    return bool(checkbox_checked or (running_info or {}).get("silent_fan_curve"))


def _percent(value) -> int:
    try:
        number = round(float(value))
    except (TypeError, ValueError):
        number = 0
    return max(0, min(100, number))
