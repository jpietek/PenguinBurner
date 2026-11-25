"""The Auto UV tab: run the undervolt scan and profile verification.

The scan runs as the daemon-streamed subprocess the classic window starts
(``runtime.daemon_commands.scan_command``); ``ScanSession`` turns its JSON
events into what this page shows. ``AutoUvEngine`` owns the processes, the
session and the log for the whole app, so rebuilding the window for a new
Appearance never kills a running scan: the new page attaches to the same
engine and renders its state.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from auto_uv.persistence.auto_uv_persisted_json_files import auto_uv_stop_request_path
from auto_uv.session.scan_events import probe_reason_tooltip
from auto_uv.session.scan_runs import (
    ScanRun,
    ScanRuns,
    format_float,
    format_int,
    format_oc_progress,
    measured_clock_value,
    metric_delta_tone,
    perf_cap_reason_text,
    perf_cap_reason_tooltip,
    progress_time_text,
    to_float,
)
from auto_uv.session.scan_session import (
    ScanSession,
    ScanUpdate,
    normalized_tier,
    pre_scan_silent_fan,
)
from cli.runtime_config_file import silent_fan_curve_from_runtime_config
from profiles.uv.profile_rules import profile_verify_selector
from profiles.uv.profile_vf_curve import (
    curve_points_from_values,
    profile_base_curve_points,
    profile_curve_plan,
)
from runtime.daemon_commands import profile_verify_command, scan_command
from runtime.profile_status import (
    penguin_burner_runtime_is_active,
    running_auto_uv_profile_info,
)
from ui.components.scan_progress import TIERS
from ui.components.scan_runs_table import (
    RESULT_COLORS,
    MeasurementLanes,
    RunCells,
    write_run_cells,
)
from ui.controllers.scan import ScanController
from ui.controllers.verify import VerifyController
from ui.daemon_setup import ensure_daemon_ready_for_privileged_action
from ui.design.style import TIER_COLORS, glyph_icon
from ui.dialogs.error_details import show_error_dialog
from ui.dialogs.final_choice import select_final_candidate
from ui.dialogs.scan_tuning import select_scan_tuning
from ui.error_reporting import show_process_failure
from ui.features.tuning.auto_uv_view import (
    EMPTY_RUNS_META,
    IDLE_DETAIL,
    IDLE_TITLE,
    LOG_LIMIT,
    AutoUvView,
    VisibleWhileEnabled,
    fit_choice_dialog,
)
from ui.features.tuning.final_choice_controller import handle_final_choice_request
from ui.features.tuning.gpu_selection import (
    gpu_choices_with_fallback,
    persist_runtime_gpu_index,
)
from ui.features.tuning.verify import stop_request_path as verify_stop_request_path
from ui.features.tuning.verify import workload_label

# Widest values the measurement lanes must fit.
LANE_VALUES = {5: ["999.99"], 6: ["999.99"], 10: ["0.9999"]}
LANE_DELTAS = {col: ["-99.9%", "+99.9%"] for col in (5, 6, 10)}
METRICS = {5: "fps", 6: "power_w", 10: "efficiency_fps_per_w"}
UNMEASURED = "—"
# metric_delta_tone -> the delta lane's colour token.
DELTA_TONES = {"good": "gain", "warning": "warning", "error": "loss"}


class AutoUvEngine(C.QObject):
    """The scan and verification processes, their session and log.

    One per application (see ``scan_engine``). Pages attach to it; the engine
    forwards each event to the attached page and keeps the session current
    even while no page is attached.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = ScanSession()
        self.scan_controller = ScanController(
            QtCore=C, parent=self, stop_request_path=auto_uv_stop_request_path()
        )
        self.verify_controller = VerifyController(
            QtCore=C, parent=self, stop_request_path=verify_stop_request_path()
        )
        self.log_lines: list[str] = []
        self.gpu_index: int | None = None
        self.pre_scan_silent_fan = False
        self.verifying_profile_id = ""
        self.choice_open = False
        self.page: AutoUvPage | None = None
        self.scan_controller.on_output = self.append
        self.scan_controller.on_event = self._scan_event
        self.scan_controller.on_human_line = self._human_line
        self.scan_controller.on_finished = self._scan_finished
        self.verify_controller.on_output = self.append
        self.verify_controller.on_telemetry = self._verify_telemetry
        self.verify_controller.on_progress = self._verify_progress
        self.verify_controller.on_finished = self._verify_finished

    def busy(self) -> bool:
        return self.scan_controller.is_running() or self.verify_controller.is_running()

    def append(self, text: str) -> None:
        """Process output, one timestamped line per non-empty line."""
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        for line in str(text or "").splitlines():
            if line.strip():
                entry = f"[{stamp}] {line}"
                self.log_lines.append(entry)
                if self.page is not None:
                    self.page.append_log(entry)
        del self.log_lines[:-LOG_LIMIT]

    def _scan_event(self, payload: dict) -> None:
        update = self.session.handle_event(payload)
        if self.page is not None:
            self.page.scan_update(update)

    def _human_line(self, line: str) -> None:
        update = self.session.handle_human_line(line)
        if self.page is not None:
            self.page.scan_update(update)

    def _scan_finished(self, exit_code, exit_status, stopped_by_user: bool) -> None:
        finish, update = self.session.finish(exit_code, stopped_by_user)
        self.append(finish.log_text)
        if self.page is not None:
            self.page.on_scan_finished(finish, update, exit_code, exit_status)

    def _verify_telemetry(self, payload: dict) -> None:
        if self.page is not None:
            self.page.plot.set_live_load_marker(payload)

    def _verify_progress(self, percent, *, elapsed_s=None, target_s=None, detail="") -> None:
        self.session.verification_progress(
            percent, elapsed_s=elapsed_s, target_s=target_s, detail=detail
        )
        if self.page is not None:
            self.page.refresh_view()

    def _verify_finished(self, exit_code, exit_status, stopped_by_user: bool) -> None:
        finish = self.session.finish_verification(exit_code, stopped_by_user)
        self.append(finish.log_text)
        profile_id, self.verifying_profile_id = self.verifying_profile_id, ""
        if self.page is not None:
            self.page.verification_finished(profile_id, finish, exit_code, exit_status)


ENGINE: dict[str, AutoUvEngine | None] = {"engine": None}


def scan_engine() -> AutoUvEngine:
    """The app's one engine, parented to the application so a window
    rebuild never takes a running scan with it."""
    engine = ENGINE["engine"]
    if engine is None:
        engine = AutoUvEngine(W.QApplication.instance())
        ENGINE["engine"] = engine
    return engine


class AutoUvPage(AutoUvView):
    """Auto UV over the real scan.

    Signals other pages connect to:
      show_scan()                 bring this tab forward (scan or verify starts)
      show_profiles()             a scan finished with a saved profile
      import_requested()          header Import Afterburner
      status_changed(dict|None)   window status, source "scan"
      progress_changed(str|None, int, str)  header progress
      busy_changed(bool)          scan or verification running (lock Profiles)
      profile_saved(dict)         final_result: a tier's profile was saved
      scan_finished(dict)         any scan end: reload profiles
      scan_completed(dict)        apply the new profile (candidate_id,
                                  gpu_index, silent_fan)
      profile_verified(str, bool) profile id, passed
    """

    show_scan = C.Signal()
    show_profiles = C.Signal()
    status_changed = C.Signal(object)
    progress_changed = C.Signal(object, int, str)
    busy_changed = C.Signal(bool)
    profile_saved = C.Signal(object)
    scan_finished = C.Signal(object)
    scan_completed = C.Signal(object)
    profile_verified = C.Signal(str, bool, bool)  # profile id, passed, stopped

    def __init__(self, *, gpu_index: int | None = None, engine: AutoUvEngine | None = None):
        super().__init__(MeasurementLanes(LANE_VALUES, LANE_DELTAS))
        self.engine = engine or scan_engine()
        if self.engine.gpu_index is None and gpu_index is not None:
            self.engine.gpu_index = max(0, int(gpu_index))
        # Another page's command (Apply, Delete, ...) blocks a new scan.
        self.other_busy = False
        # The Silent fan curve choice the completion apply keeps; Profiles
        # wires its checkbox here, the persisted choice is the fallback.
        self.silent_fan_checked: Callable[[], bool] = silent_fan_curve_from_runtime_config
        self.tier_traces: dict[str, Any] = {}
        self.last_busy: bool | None = None
        self.stop_button.setIcon(glyph_icon("stop", "loss"))
        self.import_button.setIcon(glyph_icon("import", "secondary"))
        for action in (self.setup_button, self.stop_button, self.import_button):
            action.setIconSize(C.QSize(16, 16))
        self.stop_visibility = VisibleWhileEnabled(self.stop_button)
        self.engine.page = self
        page_id = id(self)
        engine_ref = self.engine
        self.destroyed.connect(lambda *_: _detach(engine_ref, page_id))
        self.render_all()
        C.QTimer.singleShot(0, self, self.refresh_live_state)

    @property
    def gpu_index(self) -> int | None:
        return self.engine.gpu_index

    @property
    def session(self) -> ScanSession:
        return self.engine.session

    # ---------- shell ----------

    def attach_to_shell(self, shell) -> None:
        """Header actions, window status, header progress and tab switches."""
        for action in (self.setup_button, self.stop_button, self.import_button):
            shell.add_header_action(action)
        self.status_changed.connect(lambda status: shell.set_global_status(status, "scan"))
        self.progress_changed.connect(shell.set_header_progress)
        self.show_scan.connect(lambda: shell.tabs.setCurrentIndex(0))
        self.show_profiles.connect(lambda: shell.tabs.setCurrentIndex(1))
        shell.tabs.currentChanged.connect(self.style_setup_action)
        self.style_setup_action(shell.tabs.currentIndex())
        self.publish_status()

    def style_setup_action(self, tab_index: int) -> None:
        """Setup is the filled primary only on Auto UV."""
        setup = self.setup_button
        setup.setProperty("headerRole", "" if tab_index == 0 else "secondary")
        setup.setIcon(glyph_icon("auto-uv", "onPrimary" if tab_index == 0 else "ink"))
        setup.style().unpolish(setup)
        setup.style().polish(setup)

    def refresh_live_state(self) -> None:
        """GPU caption from the daemon (after startup or a service update)."""
        try:
            choices, selected = gpu_choices_with_fallback(selected_index=self.gpu_index)
        except Exception:  # noqa: BLE001 - the caption is informational
            choices, selected = [], self.gpu_index or 0
        name = next((c.name for c in choices if c.index == selected), "")
        self.gpu_caption.setText(f"GPU {selected} · {name}" if name else f"GPU {selected}")
        self.gpu_caption.setVisible(True)

    def blocks_rebuild(self) -> bool:
        """A final-candidate choice is open: the window must stay as is."""
        return self.engine.choice_open

    def set_other_busy(self, busy: bool) -> None:
        self.other_busy = bool(busy)
        self.refresh_view()

    def busy(self) -> bool:
        return self.engine.busy()

    # ---------- scan ----------

    def setup(self):
        if self.busy() or self.other_busy:
            return
        # The setup dialog reads GPU identity and limits through the daemon,
        # so the service must be installed or updated before it opens; the
        # scan streams through it, so a daemon without scan-stream-v1 lands
        # in the repair prompt instead of failing at scan start.
        if not ensure_daemon_ready_for_privileged_action(
            QtWidgets=W,
            parent=self,
            log=self.engine.append,
            action_label="Setting up Auto-UV",
            required_capabilities=("scan-stream-v1",),
        ):
            return
        options = select_scan_tuning(
            QtCore=C, QtGui=G, QtWidgets=W, parent=self, gpu_index=self.gpu_index
        )
        if options is None:
            return
        try:
            self.engine.gpu_index = persist_runtime_gpu_index(options.get("gpu_index", 0))
        except Exception as exc:  # noqa: BLE001
            self.show_error("GPU selection", f"Could not save selected GPU index: {exc}")
            return
        options = {**options, "gpu_index": int(self.engine.gpu_index)}
        self.engine.pre_scan_silent_fan = pre_scan_silent_fan(
            self.silent_fan_checked(),
            running_auto_uv_profile_info() if penguin_burner_runtime_is_active() else None,
        )
        self.refresh_live_state()
        self.show_scan.emit()
        command = scan_command(options)
        self.session.start(options)
        self.engine.append("$ " + " ".join(shlex.quote(part) for part in command))
        self.render_all()
        started = self.engine.scan_controller.start(command)
        if not started:
            self.session.start_failed()
        self.refresh_view()

    def stop(self):
        engine = self.engine
        if engine.verify_controller.is_running():
            self.session.request_verification_stop()
            self.refresh_view()
            engine.verify_controller.stop()
            return
        if not engine.scan_controller.is_running():
            return
        update = self.session.request_stop()
        self.scan_update(update)
        engine.scan_controller.stop()

    def scan_update(self, update: ScanUpdate) -> None:
        self.apply_update(update)
        self.refresh_view()
        if update.final_result is not None:
            self.profile_saved.emit({**update.final_result, "gpu_index": self.gpu_index,
                                     "candidate_id": self.session.last_candidate_id})
        if update.final_choice_request is not None:
            self.choose_final_candidate(update.final_choice_request)

    def apply_update(self, update: ScanUpdate) -> None:
        """Rows and plot for one update (and the events a resume replays)."""
        if update.reset:
            self.table.setRowCount(0)
            self.clear_plot()
        for restored in update.restored:
            self.apply_update(restored)
        for index in update.runs:
            self.write_run(index)
        plot = self.plot
        if update.base_points is not None:
            plot.set_source_points(update.base_points)
        if update.candidate_curve is not None:
            curve_id, points = update.candidate_curve
            plot.set_candidate_points(points, curve_id=curve_id)
        if update.tier_curve is not None:
            self.show_tier_curve(*update.tier_curve)
        if update.probe_marker is not None:
            plot.set_probe_marker(update.probe_marker)
        if update.load_marker is not None:
            plot.set_load_markers(update.load_marker)
        if update.live_marker is not None:
            plot.set_live_load_marker(update.live_marker)

    def choose_final_candidate(self, payload: dict) -> None:
        """The backend asks which candidate to verify; answer atomically."""
        self.engine.choice_open = True
        C.QTimer.singleShot(0, fit_choice_dialog)
        try:
            result = handle_final_choice_request(
                payload,
                QtCore=C,
                QtGui=G,
                QtWidgets=W,
                parent=self,
                scan_controller=self.engine.scan_controller,
                log_view=self.engine,
                select_final_candidate_fn=select_final_candidate,
            )
        finally:
            self.engine.choice_open = False
        self.session.record_final_choice(
            discarded=result.discarded,
            aborted=result.aborted,
            selected_candidate_id=result.selected_candidate_id,
        )

    def on_scan_finished(self, finish, update: ScanUpdate, exit_code, exit_status) -> None:
        self.apply_update(update)
        # Nothing is live any more; the curves stay for review.
        self.plot.clear_load_markers()
        self.refresh_view()
        if finish.show_error:
            show_process_failure(
                QtCore=C,
                QtGui=G,
                QtWidgets=W,
                parent=self,
                title="Auto-UV failed",
                action_label="Auto-UV scan",
                exit_code=exit_code,
                exit_status=exit_status,
                log_text="\n".join(self.engine.log_lines),
            )
        summary = {
            "outcome": finish.outcome,
            "candidate_id": finish.candidate_id,
            "gpu_index": self.gpu_index,
        }
        # The daemon restores the exact runtime it suspended for the scan;
        # Profiles only reloads, and applies the new profile on completion.
        self.scan_finished.emit(summary)
        if finish.apply_profile:
            silent_fan, self.engine.pre_scan_silent_fan = self.engine.pre_scan_silent_fan, False
            self.show_profiles.emit()
            self.scan_completed.emit({**summary, "silent_fan": silent_fan})

    # ---------- profile verification (Profiles → Verify) ----------

    def start_profile_verification(
        self, profile: dict, *, label: str, duration_s: int, gpu_index: int | None
    ) -> bool:
        """Verify a saved profile. Profiles has already checked the GPU,
        asked for the duration and made sure the root service is ready."""
        if self.busy():
            return False
        engine = self.engine
        duration_s = max(1, int(duration_s))
        command = profile_verify_command(
            profile_selector=profile_verify_selector(profile),
            duration_s=duration_s,
            stop_request_path=verify_stop_request_path(),
            gpu_index=None if gpu_index is None else int(gpu_index),
        )
        engine.verifying_profile_id = str(profile.get("profile_id") or "")
        self.session.start_verification(
            label=label, workload=workload_label(), duration_s=duration_s
        )
        # Verification tests a known curve: show it instead of a scan's.
        self.clear_plot()
        base_points = profile_base_curve_points(profile)
        if base_points:
            self.plot.set_source_points(base_points)
        candidate_points = curve_points_from_values(profile_curve_plan(profile))
        if candidate_points:
            self.plot.set_candidate_points(candidate_points, remember_previous=False)
        engine.append("$ " + " ".join(shlex.quote(part) for part in command))
        self.show_scan.emit()
        engine.verify_controller.start(command, duration_s=duration_s)
        self.refresh_view()
        return True

    def verification_finished(self, profile_id: str, finish, exit_code, exit_status) -> None:
        self.plot.clear_load_markers()
        if finish.passed:
            C.QTimer.singleShot(2500, self, self.hide_finished_progress)
        self.refresh_view()
        if finish.show_error:
            show_process_failure(
                QtCore=C,
                QtGui=G,
                QtWidgets=W,
                parent=self,
                title="Profile verification failed",
                action_label="Profile verification",
                exit_code=exit_code,
                exit_status=exit_status,
                log_text="\n".join(self.engine.log_lines),
            )
        self.profile_verified.emit(profile_id, finish.passed, finish.stopped)

    def hide_finished_progress(self) -> None:
        if not self.busy():
            self.session.progress = None
            self.refresh_view()

    # ---------- rendering ----------

    def render_all(self) -> None:
        """Everything from the session: after a rebuild or a new scan."""
        self.log_view.clear()
        self.log_lines = []
        for line in self.engine.log_lines:
            self.append_log(line)
        self.table.setRowCount(0)
        for index in range(len(self.session.runs.rows)):
            self.write_run(index)
        self.clear_plot()
        session = self.session
        if session.base_points:
            self.plot.set_source_points(session.base_points)
        for curve_id, points in session.candidate_curves:
            self.plot.set_candidate_points(points, curve_id=curve_id)
        for tier, points in session.tier_curves.items():
            self.show_tier_curve(tier, points)
        if session.probe_marker is not None:
            self.plot.set_probe_marker(session.probe_marker)
        if session.load_marker is not None:
            self.plot.set_load_markers(session.load_marker)
        self.refresh_view()

    def clear_plot(self) -> None:
        self.plot.clear()
        self.tier_traces.clear()

    def show_tier_curve(self, tier: str, points) -> None:
        title = tier.title()
        if title in self.tier_traces:
            voltages, clocks = zip(*points, strict=True)
            self.tier_traces[title].setData(voltages, clocks)
            return
        self.plot.add_comparison_points(
            points,
            name=f"{title} tier",
            color=TIER_COLORS[title],
            alpha=255,
            width=3,
            z_value=2,
        )
        self.tier_traces[title] = self.plot.comparison_curves[-1]

    def write_run(self, index: int) -> None:
        runs = self.session.runs
        if 0 <= index < len(runs.rows):
            write_run_cells(self.table, index, run_cells(index, runs.rows[index], runs))

    def refresh_view(self) -> None:
        session = self.session
        busy = self.busy()
        idle = not busy and not session.stage and not self.engine.log_lines
        self.show_stage(
            title=IDLE_TITLE if idle else session.stage or "Idle",
            tone=stage_tone(session.stage, busy=busy),
            candidate=session.candidate,
            detail=IDLE_DETAIL if idle else session.status,
            detail_tooltip=session.status,
        )
        self.tier_strip.setVisible(bool(session.tier_states))
        self.tier_strip.set_states(
            {tier.title(): state for tier, state in session.tier_states.items()},
            final_tier=self.final_tier(),
        )
        self.show_actions(
            busy=busy, stopping=session.stage == "Stopping", import_ready=not self.other_busy
        )
        if self.other_busy:
            self.setup_button.setEnabled(False)
        self.show_log_state()
        rows = session.runs.rows
        finished = sum(not run.active for run in rows)
        self.show_layout(
            idle=idle,
            has_runs=bool(rows),
            runs_meta=EMPTY_RUNS_META
            if not rows
            else f"{'Full scan' if session.full_scan else 'Selected profile'} · "
            f"{finished} completed run{'s' if finished != 1 else ''}",
        )
        self.update_legend()
        self.publish_status()

    def publish_status(self) -> None:
        session, busy = self.session, self.busy()
        if busy:
            verifying = self.engine.verify_controller.is_running()
            self.status_changed.emit(
                {
                    "running": session.stage if verifying else f"Auto UV · {session.stage}",
                    "detail": " · ".join(filter(None, (session.candidate, session.status))),
                }
            )
        else:
            self.status_changed.emit(None)
        progress = session.progress
        if progress is None:
            self.progress_changed.emit(None, 0, "")
        else:
            self.progress_changed.emit(progress.text, progress.percent, progress.detail)
        if busy != self.last_busy:
            self.last_busy = busy
            self.busy_changed.emit(busy)

    def final_tier(self) -> str | None:
        """The tier whose final verification is running, for the strip."""
        rows = self.session.runs.rows
        running = next((run for run in reversed(rows) if run.active), None)
        if running is None or running.kind not in ("final", "resume"):
            return None
        active = next(
            (tier for tier, state in self.session.tier_states.items() if state == "active"),
            None,
        )
        return active.title() if active else None

    def highlight(self, row, *_):
        rows = self.session.runs.rows
        run = rows[row] if 0 <= row < len(rows) else None
        if run is None or run.kind == "baseline":
            self.plot.set_selection_color(None)
            self.plot.set_highlighted_curve(None)
        else:
            tier = run_tier(run)
            self.plot.set_selection_color(TIER_COLORS.get(tier) if tier else None)
            self.plot.set_highlighted_curve(run.candidate_id)
        self.update_legend()

    def update_legend(self):
        items = []
        if self.session.base_points:
            items.append(("Stock", "baseCurve", "dashed"))
        if self.plot.previous_curves:
            items.append(("Previous runs", "curveHistory", "solid"))
        for tier in TIERS:
            if tier in self.tier_traces:
                items.append((tier + " tier", TIER_COLORS[tier], "solid"))
        if self.engine.verify_controller.is_running() or any(
            run.active for run in self.session.runs.rows
        ):
            items.append(("Live candidate", "candidateCurve", "dot"))
        row = self.table.currentRow()
        rows = self.session.runs.rows
        if 0 <= row < len(rows) and rows[row].kind != "baseline":
            tier = run_tier(rows[row])
            items.append(
                (
                    f"Run {row + 1} (selected)",
                    TIER_COLORS.get(tier, "candidateCurve") if tier else "candidateCurve",
                    "selected",
                )
            )
        if [tuple(i) for i in self.legend.items] != items:
            self.legend.set_items(items)

    def show_error(self, title: str, message: str) -> None:
        self.engine.append(f"{title}: {message}")
        show_error_dialog(
            QtCore=C, QtGui=G, QtWidgets=W, parent=self, title=title, message=message
        )


def _detach(engine: AutoUvEngine, page_id: int) -> None:
    if engine.page is not None and id(engine.page) == page_id:
        engine.page = None


def stage_tone(stage: str, *, busy: bool) -> str:
    if busy:
        return "warning" if stage == "Stopping" else "running"
    if stage == "Error":
        return "error"
    if stage in ("Stopped", "Aborted"):
        return "warning"
    if stage == "Complete":
        return "good"
    return "idle"


def run_tier(run: ScanRun) -> str | None:
    """Title-case tier of a run, from its stage (efficiency-candidate...)."""
    stage = str(run.payload.get("stage", "")).lower()
    tier = normalized_tier(stage.split("-", 1)[0])
    return tier.title() if tier else None


def run_cells(index: int, run: ScanRun, runs: ScanRuns) -> RunCells:
    """One scan run as the runs table shows it."""
    payload = run.payload
    base = run.kind == "baseline"
    # Result rows carry the probe's measurements; a running, stopped or
    # failed-process row only has what its probe_start announced.
    measured = "decision" in payload and not run.active
    cap = perf_cap_reason_text(
        payload.get("perf_cap_reason"), payload.get("hw_power_brake_samples")
    )
    texts = [
        str(index + 1),
        format_int(payload.get("voltage_mv")),
        format_int(payload.get("clock_mhz")),
        "-" if base else format_oc_progress(run.oc).removesuffix(" MHz"),
        format_float(measured_clock_value(payload)),
        format_float(payload.get("fps")),
        format_float(payload.get("power_w")),
        format_float(payload.get("temp_c")),
        format_float(payload.get("fan_pct")),
        cap,
        format_float(payload.get("efficiency_fps_per_w"), precision=4),
        run.decision,
        status_text(run),
    ]
    if not measured:
        texts[4:11] = [UNMEASURED] * 7
        texts[9] = cap or UNMEASURED
    deltas, delta_tones, tooltips = {}, {}, {}
    for col, metric in METRICS.items():
        change = runs.delta_pct(run, metric) if measured else None
        deltas[col] = change
        if change is not None:
            tone = metric_delta_tone(metric, change)
            delta_tones[col] = DELTA_TONES.get(tone, "quiet")
            reference = (runs.base_baseline or {}).get(metric)
            tooltips[col] = f"{texts[col]} · {change:+.2f}% vs stock baseline {reference}"
    reason = probe_reason_tooltip(payload)
    tooltips[0] = "Baseline reference run" if base else texts[0]
    if run.oc is not None and not base:
        tooltips[3] = (
            f"Target clock delta from measured baseline: {format_oc_progress(run.oc)}"
        )
    cap_tooltip = perf_cap_reason_tooltip(
        payload.get("perf_cap_reason"), payload.get("hw_power_brake_samples")
    )
    if cap_tooltip:
        tooltips[9] = cap_tooltip
    if base and measured:
        tooltips[10] = "Base FPS/W reference"
    stage = {"final": "Final verification", "resume": "Resume verification"}
    tooltips[11] = reason or run.decision
    tooltips[12] = "\n".join(
        filter(None, (stage.get(run.kind, ""), reason or status_text(run)))
    )
    result_tone = (
        "warning"
        if run.decision == "Failed" and run.row_state == "warning"
        else RESULT_COLORS.get(run.decision, "loss" if run.decision else "secondary")
    )
    return RunCells(
        texts=texts,
        tooltips=tooltips,
        deltas=deltas,
        delta_tones=delta_tones,
        tone=run.row_state,
        result_tone=result_tone,
        progress=progress_ratio(run),
        final=run.kind in ("final", "resume"),
        muted=run.restored,
    )


def progress_ratio(run: ScanRun) -> float:
    elapsed = to_float(run.progress.get("elapsed_s"))
    target = to_float(run.progress.get("target_duration_s"))
    if not run.active and run.row_state in ("good", "baseline", "neutral"):
        return 1.0
    if target is None or target <= 0:
        return 0.0 if run.active else 1.0
    return max(0.0, min(1.0, float(elapsed or 0.0) / target))


def status_text(run: ScanRun) -> str:
    """Status column: failure label, else stage and elapsed / target."""
    label = str(run.progress.get("label") or "").strip()
    if run.decision == "Failed" and label and label != "Failed":
        return label
    target = to_float(run.progress.get("target_duration_s"))
    prefix = {"final": "Final · ", "resume": "Resume · "}.get(run.kind, "")
    if label and label not in ("Final verification",):
        prefix = f"{label} · "
    if target:
        elapsed = run.progress.get("elapsed_s")
        if not run.active and run.row_state in ("good", "baseline", "neutral"):
            elapsed = target
        return prefix + progress_time_text(elapsed or 0.0, target)
    if run.active:
        return label or "Running"
    return label or run.decision or "Done"

