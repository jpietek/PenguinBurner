from __future__ import annotations

from pathlib import Path
import shlex

from manual_uv_curve_editor import editable_anchor_from_profile
from saved_uv_profiles import delete_auto_uv_profile_paths
from penguin_burner_paths import default_user_config_dir

from .afterburner_import import delete_afterburner_import_config
from .afterburner_workflow import AfterburnerImportWorkflow
from .commands import delete_profiles_command
from .commands import profile_verify_command
from .commands import runtime_profile_command
from .commands import scan_command
from .components import CurvePlot
from .components import LogView
from .components import ProfileList
from .components import RunsTable
from .components import ScanControls
from .components import StatusHeader
from .constants import APP_DISPLAY_NAME
from .constants import DEFAULT_FINAL_VERIFICATION_DURATION_S
from .constants import MAX_FINAL_VERIFICATION_DURATION_S
from .controllers import CommandController
from .controllers import VerifyController
from .controllers import ScanController
from .curve_profiles import profile_base_curve_points
from .curve_profiles import profile_curve_plan
from .curve_profiles import save_edited_curve_profile
from .curve_tabs import CurveTabs
from .dialogs import select_final_candidate
from .dialogs import select_verify_options
from .dialogs import select_scan_tuning
from .dialogs import show_about_dialog
from .error_reporting import ErrorReporter
from .components.fan_curve_editor import open_fan_curve_editor_dialog
from .components.vf_curve_editor import open_vf_curve_editor_dialog
from .fan_profiles import fan_curve_points_from_payload
from .fan_profiles import fan_curve_target_point_from_payload
from .fan_profiles import fan_measurement_points_from_payload
from .fan_profiles import profile_fan_curve_points
from .fan_profiles import profile_fan_curve_target_point
from .fan_profiles import profile_fan_measurement_points
from .fan_profiles import profile_id_from_archive_path
from .fan_profiles import save_edited_fan_profile
from .fan_profiles import sync_profile_fan_payload
from .lact_export import detect_lact_gpu_id
from .lact_export import lact_export_output_path
from .lact_export import write_lact_profile_config
from .models import candidate_id_from_payload
from .models import event_base_points
from .models import event_points
from .models import fan_measurement_point
from .models import fan_points
from .models import sorted_unique_points
from .models import stage_title
from .models import status_value
from .models import top_status_text
from .profiles import delete_confirmation_text
from .profiles import load_profile_summaries
from .profiles import penguin_burner_runtime_is_active
from .profiles import profile_can_apply
from .profiles import profile_can_verify
from .profiles import profile_for_selector
from .profiles import profile_is_afterburner
from .profiles import profile_is_deletable
from .profiles import profile_status_label
from .profiles import profile_verify_selector
from .profiles import profiles_for_selectors
from .profiles import runner_status_text
from .profiles import running_auto_uv_profile_info
from .profiles import selected_profile_ids_include_selector
from .profiles import systemd_autostart_profile_info
from .profiles import systemd_unit_entry_exists
from .verify import stop_request_path as verify_stop_request_path
from .verify import workload_label
from .styles import STYLESHEET


class MainWindow:
    def __init__(self, qt_modules, *, yolo: bool = False, auto_uv3: bool = False):
        self.QtCore, self.QtGui, self.QtWidgets, self.pg = qt_modules
        self.auto_uv_yolo = bool(yolo)
        self.auto_uv3 = bool(auto_uv3)
        self.profile_summaries: list[dict] = []
        self.fan_measured_points: list[tuple[float, float]] = []
        self.pending_final_result_payload: dict | None = None
        self.final_choice_discarded = False
        self.last_auto_uv_candidate_id = ""
        self._delete_remove_systemd = False
        self._delete_selected_ids: set[str] = set()

        self.window = self.QtWidgets.QMainWindow()
        self.window.setWindowTitle(APP_DISPLAY_NAME)
        self.window.resize(1220, 820)
        self._build_ui()
        self.scan_controller = ScanController(
            QtCore=self.QtCore,
            parent=self.window,
            stop_request_path=_stop_request_path(),
        )
        self.scan_controller.on_output = self.log_view.append
        self.scan_controller.on_event = self._handle_scan_event
        self.scan_controller.on_human_line = self._handle_human_line
        self.scan_controller.on_finished = self._scan_finished
        self.command_controller = CommandController(
            QtCore=self.QtCore,
            parent=self.window,
        )
        self.command_controller.on_output = self.log_view.append
        self.command_controller.on_finished = self._command_finished
        self.verify_controller = VerifyController(
            QtCore=self.QtCore,
            parent=self.window,
            stop_request_path=verify_stop_request_path(),
        )
        self.verify_controller.on_output = self.log_view.append
        self.verify_controller.on_progress = self.controls.set_verify_progress
        self.verify_controller.on_finished = self._verify_finished
        self._load_profiles()

    def _build_ui(self) -> None:
        root = self.QtWidgets.QWidget()
        layout = self.QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self.header = StatusHeader(QtCore=self.QtCore, QtWidgets=self.QtWidgets)
        self.controls = ScanControls(QtWidgets=self.QtWidgets)
        self.vf_plot = CurvePlot(
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            x_label="Voltage",
            x_units="mV",
            y_label="Clock",
            y_units="MHz",
        )
        self.fan_plot = CurvePlot(
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            x_label="Temperature",
            x_units="C",
            y_label="Fan",
            y_units="%",
            x_range=(35, 95),
            y_range=(0, 100),
        )
        self.runs_table = RunsTable(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
        )
        self.runs_table.on_candidate_selection_changed = (
            self.vf_plot.set_highlighted_curve
        )
        self.profile_list = ProfileList(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
        )
        self.log_view = LogView(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
        )

        auto_uv_view = self.QtWidgets.QSplitter(self.QtCore.Qt.Horizontal)
        auto_uv_view.addWidget(self.vf_plot.widget)
        auto_uv_view.addWidget(self.log_view.widget)
        auto_uv_view.setSizes([760, 440])

        self.tabs = self.QtWidgets.QTabWidget()
        self.auto_uv_tab_index = self.tabs.addTab(auto_uv_view, "Auto-UV")
        self.tabs.addTab(self.fan_plot.widget, "Silent Fan Curve")
        self.profiles_tab_index = self.tabs.addTab(self.profile_list.widget, "Profiles")
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_dynamic_tab)
        self.errors = ErrorReporter(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            parent=self.window,
            controls=self.controls,
            log_view=self.log_view,
        )
        self.curve_tabs = CurveTabs(
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            tabs=self.tabs,
            fixed_tab_count=self.tabs.count(),
            show_error=self.errors.show,
        )
        self.afterburner_import = AfterburnerImportWorkflow(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            parent=self.window,
            tabs=self.tabs,
            profiles_tab_index=self.profiles_tab_index,
            profile_list=self.profile_list,
            controls=self.controls,
            log_view=self.log_view,
            workflow_running=self._workflow_running,
            load_profiles=self._load_profiles,
            show_error=self.errors.show,
        )

        table_panel = self.QtWidgets.QGroupBox("Undervolting runs")
        table_panel.setMinimumHeight(220)
        table_layout = self.QtWidgets.QVBoxLayout(table_panel)
        table_layout.setContentsMargins(10, 18, 10, 10)
        table_layout.addWidget(self.runs_table.widget)

        layout.addWidget(self.header.widget)
        layout.addWidget(self.controls.widget)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(table_panel)

        self.controls.start_button.clicked.connect(self.start_scan)
        self.controls.stop_button.clicked.connect(self.stop_scan)
        self.controls.about_button.clicked.connect(self.show_about)
        self.controls.import_afterburner_button.clicked.connect(self.afterburner_import.run)
        self.profile_list.daemonize_button.clicked.connect(self._run_selected_profile)
        self.profile_list.delete_button.clicked.connect(self._delete_selected_profiles)
        self.profile_list.remove_button.clicked.connect(
            lambda: self._run_runtime_action("uninstall-systemd")
        )
        context_menu_policy = getattr(
            getattr(self.QtCore.Qt, "ContextMenuPolicy", self.QtCore.Qt),
            "CustomContextMenu",
        )
        self.profile_list.table.setContextMenuPolicy(context_menu_policy)
        self.profile_list.table.customContextMenuRequested.connect(
            self._show_profile_context_menu
        )
        self.profile_list.set_runtime_actions_enabled(False)
        self.window.setCentralWidget(root)
        self.window.setStyleSheet(STYLESHEET)

    def show(self) -> None:
        self.window.show()

    def show_about(self) -> None:
        show_about_dialog(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            parent=self.window,
        )

    def start_scan(self) -> None:
        if self._workflow_running():
            return
        options = select_scan_tuning(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            parent=self.window,
            yolo=self.auto_uv_yolo,
        )
        if options is None:
            return
        options["auto_uv3"] = bool(self.auto_uv3)
        command = scan_command(options)
        self.runs_table.clear()
        self.vf_plot.clear()
        self.fan_plot.clear()
        self.fan_measured_points = []
        self.pending_final_result_payload = None
        self.final_choice_discarded = False
        self.last_auto_uv_candidate_id = ""
        self.controls.hide_dependency_progress()
        self.log_view.append("$ " + " ".join(shlex.quote(part) for part in command) + "\n")
        self.header.set_stage("Starting")
        self.header.set_candidate("Writing to main Auto-UV profile store")
        self.controls.set_running(True)
        self.profile_list.set_runtime_actions_enabled(False)
        if not self.scan_controller.start(command):
            self.controls.set_running(False)
            self._set_profile_actions_enabled(True)

    def stop_scan(self) -> None:
        if self.verify_controller.is_running():
            self.header.set_stage("Stopping")
            self.controls.set_status_text("Stopping profile verification.")
            self.verify_controller.stop()
            return
        if not self.scan_controller.is_running():
            return
        self.header.set_stage("Stopping")
        self.runs_table.mark_running_rows_stopping()
        self.controls.set_status_text("Stopping Auto-UV.")
        self.scan_controller.stop()

    def _handle_scan_event(self, payload: dict) -> None:
        event = str(payload.get("event", ""))
        if event == "auto_uv_start":
            self.header.set_stage("Scanning")
        elif event == "dependency_progress":
            self._handle_dependency_progress(payload)
        elif event == "probe_start":
            self.controls.hide_dependency_progress()
            self.header.set_stage(stage_title(payload.get("stage", "Probe")))
            self.header.set_candidate(_probe_text(payload))
            self.runs_table.add_probe_start(payload)
            self.vf_plot.set_probe_marker(payload)
        elif event == "probe_result":
            self.header.set_stage(stage_title(payload.get("stage", "Probe")))
            self.runs_table.add_probe_result(payload)
            self.vf_plot.set_load_markers(payload)
            self._record_fan_measurement(payload)
        elif event == "load_telemetry":
            self.runs_table.update_probe_progress(payload)
            self.vf_plot.set_live_load_marker(payload)
        elif event in {"source_curve", "base_curve"}:
            self.controls.hide_dependency_progress()
            points = event_base_points(payload)
            self.vf_plot.set_source_points(points)
            self.curve_tabs.set_base_points(points)
        elif event == "candidate_curve":
            self.vf_plot.set_candidate_points(
                event_points(payload),
                curve_id=candidate_id_from_payload(payload),
            )
        elif event == "fan_curve_suggested":
            points = fan_points(payload)
            if points:
                self.fan_plot.set_candidate_points(points)
        elif event == "final_choice_request":
            self._handle_final_choice_request(payload)
        elif event == "final_choice_discarded":
            self.final_choice_discarded = True
            self.header.set_stage("Discarded")
            self.header.set_candidate("")
            self.controls.set_status_text(
                "Final verification discarded. No Auto-UV profile was saved."
            )
        elif event == "final_result":
            self.pending_final_result_payload = dict(payload)
            self.last_auto_uv_candidate_id = candidate_id_from_payload(payload)
            self.header.set_stage("Complete")
            self.header.set_candidate(_probe_text(payload))
            self._load_profiles()

    def _handle_human_line(self, line: str) -> None:
        lower = line.lower()
        if "final verification" in lower:
            self.header.set_stage("Final verification")
        elif "candidate" in lower and "mv" in lower:
            self.header.set_stage("Undervolting Candidates Sweep")
            self.header.set_candidate(top_status_text(line))
        elif "auto-uv final state" in lower:
            self.header.set_stage("Complete")
            self.header.set_candidate(top_status_text(line))

    def _handle_dependency_progress(self, payload: dict) -> None:
        detail = str(payload.get("detail") or "Downloading dependencies").strip()
        percent = payload.get("percent", 0)
        self.header.set_stage("Downloading dependencies")
        self.header.set_candidate("")
        self.controls.set_status_text(detail)
        self.controls.set_dependency_progress(percent, detail=detail)

    def _handle_final_choice_request(self, payload: dict) -> None:
        candidates = [
            dict(candidate)
            for candidate in payload.get("candidates", [])
            if isinstance(candidate, dict)
        ]
        auto_uv_mode = str(payload.get("auto_uv_mode", "")).strip()
        default_id = str(payload.get("default_candidate_id", "")).strip()
        default_duration_s = _duration_seconds(
            payload.get("final_verification_duration_s"),
            DEFAULT_FINAL_VERIFICATION_DURATION_S,
        )
        max_duration_s = _duration_seconds(
            payload.get("max_final_verification_duration_s"),
            MAX_FINAL_VERIFICATION_DURATION_S,
        )
        request_reason = str(payload.get("request_reason", "")).strip()
        selected, duration_s, discarded = select_final_candidate(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            parent=self.window,
            candidates=candidates,
            default_candidate_id=default_id,
            default_duration_s=default_duration_s,
            max_duration_s=max_duration_s,
            auto_uv_mode=auto_uv_mode,
            request_reason=request_reason,
        )
        response_path = str(payload.get("response_path", "")).strip()
        if not response_path:
            return
        if discarded:
            self.final_choice_discarded = True
            self.scan_controller.write_json_response(response_path, {"action": "discard"})
            self.log_view.append("Final verification discarded by user.\n")
            return
        selected_id = (
            str(selected.get("candidate_id", "")).strip()
            if selected is not None
            else default_id
        )
        self.last_auto_uv_candidate_id = selected_id
        self.scan_controller.write_json_response(
            response_path,
            {
                "candidate_id": selected_id,
                "final_verification_duration_s": int(duration_s),
            },
        )
        self.log_view.append(
            "Selected Final verification candidate: "
            f"{selected_id}; duration: {int(duration_s)}s\n"
        )

    def _scan_finished(self, exit_code, exit_status, stopped_by_user: bool) -> None:
        status_name = "finished" if int(exit_code) == 0 else "stopped"
        self.log_view.append(f"\nAuto-UV process {status_name}: exit_code={exit_code}\n")
        failed = int(exit_code) != 0 and not stopped_by_user
        if failed:
            self.header.set_stage("Error")
            self.runs_table.mark_running_rows_stopped(label="Failed")
            self.controls.set_status_text("Auto-UV failed.")
            self.errors.show_process(
                title="Auto-UV failed",
                action_label="Auto-UV scan",
                exit_code=exit_code,
                exit_status=exit_status,
            )
        elif self.final_choice_discarded:
            self.header.set_stage("Discarded")
            self.runs_table.mark_running_rows_stopped(label="Discarded")
        elif self.pending_final_result_payload is not None:
            self.header.set_stage("Complete")
            self.controls.set_status_text("Final verification complete.")
            self.tabs.setCurrentIndex(self.profiles_tab_index)
        elif stopped_by_user:
            self.header.set_stage("Stopped")
            self.runs_table.mark_running_rows_stopped(label="Stopped")
        else:
            self.header.set_stage("Idle")
        self.controls.set_running(False)
        self.controls.hide_dependency_progress()
        self.profile_list.set_runtime_actions_enabled(False)
        self.pending_final_result_payload = None
        self.final_choice_discarded = False
        self._load_profiles()

    def _run_selected_profile(self) -> None:
        action = (
            "install-systemd"
            if self.profile_list.persist_on_startup_enabled()
            else "daemonize"
        )
        self._run_runtime_action(action)

    def _run_runtime_action(self, action: str) -> None:
        if self._workflow_running():
            return
        profile_id = self.profile_list.selected_profile_id()
        if action != "uninstall-systemd" and not profile_id:
            self.log_view.append("\nNo profile selected.\n")
            return
        selected_profile = profile_for_selector(self.profile_summaries, profile_id)
        if action != "uninstall-systemd" and not profile_can_apply(
            selected_profile or {}
        ):
            message = "This edited profile must be verified before it can be applied."
            self.controls.set_status_text(message)
            self.log_view.append(f"\n{message}\n")
            return
        if (
            action != "uninstall-systemd"
            and selected_profile
            and self.profile_list.silent_fan_enabled()
            and not profile_is_afterburner(selected_profile)
            and not sync_profile_fan_payload(selected_profile)
        ):
            # Runtime fan apply needs a saved fan payload before the CLI starts.
            self.controls.set_status_text("No runtime-ready silent fan curve is available.")
            self.log_view.append("\nNo runtime-ready silent fan curve is available.\n")
            return
        prefer_afterburner_curve = bool(
            selected_profile and profile_is_afterburner(selected_profile)
        )
        command = runtime_profile_command(
            action,
            profile_selector=(
                ""
                if action == "uninstall-systemd" or prefer_afterburner_curve
                else profile_id
            ),
            silent_fan_curve=self.profile_list.silent_fan_enabled(),
            prefer_afterburner_curve=prefer_afterburner_curve,
        )
        self.controls.set_status_text(self._runtime_action_start_text(action))
        self._set_profile_actions_enabled(False)
        self.controls.start_button.setEnabled(False)
        self.command_controller.start(
            action,
            command,
            fail_text="Failed to start runtime profile action.",
        )

    def _edit_profile_fan_curve(self, profile: dict) -> None:
        curve_points = profile_fan_curve_points(profile)
        if self.pg is None or not curve_points:
            self.QtWidgets.QMessageBox.information(
                self.window,
                "Edit Fan Curve",
                "No editable fan curve is available for this profile.",
            )
            return

        def save_edit(edit) -> str:
            path, payload = save_edited_fan_profile(
                profile,
                edit,
                original_points=curve_points,
            )
            profile_id = profile_id_from_archive_path(path)
            if profile_id:
                self.profile_list.select_profile(profile_id)
            fan_payload = payload.get("fan_curve_payload")
            if isinstance(fan_payload, dict):
                self._populate_fan_plot_from_payload(fan_payload)
            self._load_profiles()
            return f"Saved edited fan curve: {path.name}."

        open_fan_curve_editor_dialog(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            parent=self.window,
            curve_points=curve_points,
            measured_points=profile_fan_measurement_points(profile),
            target_point=profile_fan_curve_target_point(profile),
            save_callback=save_edit,
        )

    def _edit_profile_vf_curve(self, profile: dict) -> None:
        plan = profile_curve_plan(profile)
        anchor = editable_anchor_from_profile(profile)
        if not plan or anchor is None:
            self.QtWidgets.QMessageBox.information(
                self.window,
                "Edit VF Curve",
                "No editable V/F curve is available for this profile.",
            )
            return
        manual_edit = profile.get("manual_edit")
        control_voltage_mvs = _manual_curve_control_voltage_mvs(manual_edit)

        def save_edit(edit) -> str:
            path, payload = save_edited_curve_profile(
                profile,
                edit,
                original_anchor_voltage_mv=int(anchor[0]),
                original_anchor_clock_mhz=int(anchor[1]),
            )
            self.last_auto_uv_candidate_id = str(payload.get("candidate_id", "")).strip()
            profile_id = profile_id_from_archive_path(path)
            self.controls.set_status_text(
                f"Saved edited curve draft: {path.name}. Verify it before Apply."
            )
            self._load_profiles()
            if profile_id:
                self.profile_list.select_profile(profile_id)
            return f"Saved edited curve draft: {path.name}. Verify it before Apply."

        open_vf_curve_editor_dialog(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            parent=self.window,
            plan=plan,
            base_points=profile_base_curve_points(profile)
            or self.curve_tabs.base_curve_points,
            anchor=anchor,
            save_callback=save_edit,
            control_voltage_mvs=control_voltage_mvs,
        )

    def _export_lact_profile(self, profile: dict) -> None:
        directory = self.QtWidgets.QFileDialog.getExistingDirectory(
            self.window,
            "Choose LACT Export Directory",
            str(default_user_config_dir()),
        )
        if not directory:
            return
        output_path = lact_export_output_path(directory)
        gpu_id = detect_lact_gpu_id(output_path.parent)
        if not gpu_id:
            self.errors.show(
                "Export LACT",
                "Could not detect the LACT GPU id. Start LACT once, or choose a "
                "directory that already contains config.yaml.",
            )
            return
        include_fan_curve = self.profile_list.silent_fan_enabled()
        if include_fan_curve and not sync_profile_fan_payload(profile):
            self.errors.show(
                "Export LACT",
                "No runtime-ready silent fan curve is available for this profile.",
            )
            return
        try:
            written_path, warnings = write_lact_profile_config(
                profile,
                output_path=output_path,
                gpu_id=gpu_id,
                include_fan_curve=include_fan_curve,
            )
        except Exception as exc:
            self.errors.show("Export LACT", f"LACT export failed:\n{exc}")
            return
        message = f"LACT profile successfully written:\n{written_path}"
        if warnings:
            message += "\n\nWarnings:\n" + "\n".join(str(item) for item in warnings)
        self.controls.set_status_text(f"LACT profile written: {written_path}")
        self.log_view.append("\n" + message + "\n")
        self.QtWidgets.QMessageBox.information(self.window, "Export LACT", message)

    def _verify_profile(self, profile: dict) -> None:
        if self._workflow_running() or not profile_can_verify(profile):
            return
        label = profile_status_label(
            self.profile_summaries,
            str(profile.get("profile_id", "")),
        )
        options = select_verify_options(
            QtWidgets=self.QtWidgets,
            parent=self.window,
            profile_label=label,
        )
        if options is None:
            return
        duration_s = int(options["duration_s"])
        q2rtx_enabled = bool(options["q2rtx_enabled"])
        cuda_enabled = bool(options["cuda_enabled"])
        # The UI says verify; the backend flag name is kept for compatibility.
        prefer_afterburner_curve = profile_is_afterburner(profile)
        command = profile_verify_command(
            profile_selector="" if prefer_afterburner_curve else profile_verify_selector(profile),
            duration_s=duration_s,
            prefer_afterburner_curve=prefer_afterburner_curve,
            stop_request_path=verify_stop_request_path(),
            q2rtx_enabled=q2rtx_enabled,
            cuda_enabled=cuda_enabled,
        )
        workload = workload_label(
            q2rtx_enabled=q2rtx_enabled,
            cuda_enabled=cuda_enabled,
        )
        self.header.set_stage("Profile verification")
        self.header.set_candidate(label)
        self.controls.set_status_text(f"Verifying {label} with {workload}.")
        self.tabs.setCurrentIndex(self.auto_uv_tab_index)
        self.controls.set_verify_progress(
            0,
            elapsed_s=0,
            target_s=duration_s,
            detail=f"Verifying {label} with {workload}.",
        )
        self.log_view.append(
            "\n$ " + " ".join(shlex.quote(part) for part in command) + "\n"
        )
        self.controls.set_running(True)
        self._set_profile_actions_enabled(False)
        self.verify_controller.start(command, duration_s=duration_s)

    def _delete_selected_profiles(self) -> None:
        if self._workflow_running():
            return
        selected_ids = set(self.profile_list.selected_profile_ids())
        selected_profiles = profiles_for_selectors(
            self.profile_summaries,
            list(selected_ids),
        )
        delete_afterburner_import = any(
            profile_is_afterburner(profile) for profile in selected_profiles
        )
        selected_paths = self.profile_list.selected_profile_paths()
        if not selected_paths and not delete_afterburner_import:
            return
        autostart_selector = str(systemd_autostart_profile_info()["selector"])
        remove_systemd = selected_profile_ids_include_selector(
            self.profile_summaries,
            list(selected_ids),
            autostart_selector,
        )
        if not self._confirm_profile_delete(
            remove_systemd=remove_systemd,
            includes_afterburner=delete_afterburner_import,
        ):
            return
        afterburner_deleted = False
        if delete_afterburner_import:
            try:
                afterburner_deleted = delete_afterburner_import_config()
            except Exception as exc:
                self.errors.show(
                    "Delete Profiles",
                    f"Afterburner import deletion failed:\n{exc}",
                )
                return
        try:
            deleted = delete_auto_uv_profile_paths(selected_paths) if selected_paths else []
        except PermissionError:
            self._run_privileged_profile_delete(
                selected_paths,
                selected_ids,
                remove_systemd=remove_systemd,
            )
            return
        count = len(deleted) + (1 if afterburner_deleted else 0)
        label = "profile" if count == 1 else "profiles"
        self.log_view.append(f"\nDeleted {count} saved {label}.\n")
        self.controls.set_status_text(f"Deleted {count} saved {label}.")
        self._load_profiles()
        if remove_systemd:
            self._run_runtime_action("uninstall-systemd")

    def _run_privileged_profile_delete(
        self,
        selected_paths: list[str],
        selected_ids: set[str],
        *,
        remove_systemd: bool,
    ) -> None:
        self._delete_selected_ids = set(selected_ids)
        self._delete_remove_systemd = bool(remove_systemd)
        self._set_profile_actions_enabled(False)
        self.controls.start_button.setEnabled(False)
        self.controls.set_status_text("Deleting selected Auto-UV profiles.")
        self.command_controller.start(
            "delete-profiles",
            delete_profiles_command(selected_paths),
            fail_text="Failed to start Auto-UV profile delete action.",
        )

    def _command_finished(self, kind: str, exit_code, exit_status) -> None:
        label = _runtime_action_label(kind)
        self.log_view.append(f"\n{label} finished: exit_code={exit_code}\n")
        success = int(exit_code) == 0
        self.controls.start_button.setEnabled(not self.scan_controller.is_running())
        self._set_profile_actions_enabled(not self.scan_controller.is_running())
        if kind == "delete-profiles":
            if success:
                self.controls.set_status_text("Selected Auto-UV profiles deleted.")
                self._load_profiles()
                if self._delete_remove_systemd:
                    self._delete_remove_systemd = False
                    self._run_runtime_action("uninstall-systemd")
                    return
            else:
                self.controls.set_status_text("Auto-UV profile deletion failed.")
                self.errors.show_process(
                    title="Profile deletion failed",
                    action_label="Delete selected profiles",
                    exit_code=exit_code,
                    exit_status=exit_status,
                )
            self._delete_selected_ids = set()
            self._delete_remove_systemd = False
            self._load_profiles()
            return
        if success:
            self.controls.set_status_text(f"{label} complete.")
        else:
            self.controls.set_status_text(f"{label} failed.")
            self.errors.show_process(
                title=f"{label} failed",
                action_label=label,
                exit_code=exit_code,
                exit_status=exit_status,
            )
        self._load_profiles()

    def _confirm_profile_delete(
        self,
        *,
        remove_systemd: bool,
        includes_afterburner: bool = False,
    ) -> bool:
        buttons = (
            self.QtWidgets.QMessageBox.StandardButton.Yes
            | self.QtWidgets.QMessageBox.StandardButton.No
        )
        answer = self.QtWidgets.QMessageBox.question(
            self.window,
            "Delete Profiles",
            delete_confirmation_text(
                self.profile_list.selected_profile_names(),
                removes_systemd=remove_systemd,
                includes_afterburner=includes_afterburner,
            ),
            buttons,
            self.QtWidgets.QMessageBox.StandardButton.No,
        )
        return answer == self.QtWidgets.QMessageBox.StandardButton.Yes

    def _show_profile_context_menu(self, position) -> None:
        table = self.profile_list.table
        index = table.indexAt(position)
        if not index.isValid():
            return
        table.selectRow(int(index.row()))
        profile = self._profile_from_row(int(index.row()))
        if profile is None:
            return
        menu = self.QtWidgets.QMenu(table)
        edit_curve_action = menu.addAction("Edit VF Curve")
        edit_curve_action.setEnabled(bool(profile_curve_plan(profile)))
        fan_action = menu.addAction("Edit Fan Curve")
        fan_action.setEnabled(bool(profile_fan_curve_points(profile)))
        apply_action = menu.addAction("Apply")
        apply_action.setEnabled(not self._workflow_running() and profile_can_apply(profile))
        verify_action = menu.addAction("Verify")
        verify_action.setEnabled(
            not self._workflow_running() and profile_can_verify(profile)
        )
        export_action = menu.addAction("Export LACT")
        export_action.setEnabled(not self._workflow_running() and profile_can_apply(profile))
        menu.addSeparator()
        delete_action = menu.addAction("Delete")
        delete_action.setEnabled(
            not self._workflow_running() and profile_is_deletable(profile)
        )
        chosen = menu.exec(table.viewport().mapToGlobal(position))
        if chosen == edit_curve_action:
            self._edit_profile_vf_curve(profile)
        elif chosen == fan_action:
            self._edit_profile_fan_curve(profile)
        elif chosen == apply_action:
            self.profile_list.install_button.setChecked(True)
            self._run_runtime_action("install-systemd")
        elif chosen == verify_action:
            self._verify_profile(profile)
        elif chosen == export_action:
            self._export_lact_profile(profile)
        elif chosen == delete_action:
            self._delete_selected_profiles()

    def _profile_from_row(self, row: int) -> dict | None:
        item = self.profile_list.table.item(int(row), 0)
        if item is None:
            return None
        profile_id = str(item.data(self.profile_list.PROFILE_ID_ROLE) or "")
        return profile_for_selector(self.profile_summaries, profile_id)

    def _close_dynamic_tab(self, index: int) -> None:
        self.curve_tabs.close_tab(index)

    def _verify_finished(self, exit_code, exit_status, stopped_by_user: bool) -> None:
        success = int(exit_code) == 0
        self.log_view.append(f"\nProfile verification finished: exit_code={exit_code}\n")
        if success:
            target = int(self.verify_controller.target_duration_s or 0)
            self.controls.set_verify_progress(
                100,
                elapsed_s=target,
                target_s=target,
                detail="Profile verification complete.",
            )
            self.controls.set_status_text("Profile verification complete.")
            self.header.set_stage("Idle")
            self.QtCore.QTimer.singleShot(2500, self.controls.hide_dependency_progress)
        else:
            self.controls.hide_dependency_progress()
            self.controls.set_status_text(
                "Profile verification stopped."
                if stopped_by_user
                else "Profile verification failed."
            )
            self.header.set_stage("Idle" if stopped_by_user else "Error")
            if not stopped_by_user:
                self.errors.show_process(
                    title="Profile verification failed",
                    action_label="Profile verification",
                    exit_code=exit_code,
                    exit_status=exit_status,
                )
        self.controls.set_running(False)
        self._load_profiles()

    def _record_fan_measurement(self, payload: dict) -> None:
        point = fan_measurement_point(payload)
        if point is None:
            return
        self.fan_measured_points.append(point)
        self.fan_plot.set_source_points(sorted_unique_points(self.fan_measured_points))

    def _populate_fan_plot_from_payload(self, payload: dict) -> None:
        points = fan_curve_points_from_payload(payload)
        if not points:
            return
        measured_points = fan_measurement_points_from_payload(payload)
        if measured_points:
            self.fan_measured_points = list(measured_points)
            self.fan_plot.set_source_points(measured_points)
        self.fan_plot.set_candidate_points(points, remember_previous=False)
        target = fan_curve_target_point_from_payload(payload)
        if target is not None:
            self.fan_plot.set_selected_point(target[0], target[1])

    def _load_profiles(self) -> None:
        self.profile_summaries = load_profile_summaries()
        autostart_info = systemd_autostart_profile_info()
        systemd_selector = str(autostart_info["selector"])
        if systemd_selector in {"active", "latest", "__systemd_default__"}:
            systemd_selector = str(
                (self.profile_summaries[0] if self.profile_summaries else {}).get(
                    "profile_id",
                    "",
                )
            )
        self.profile_list.set_profiles(
            self.profile_summaries,
            systemd_selector=systemd_selector,
            has_systemd_entry=systemd_unit_entry_exists(),
            preferred_candidate_id=self.last_auto_uv_candidate_id,
            select_preferred=bool(self.last_auto_uv_candidate_id),
        )
        self._set_profile_actions_enabled(not self._workflow_running())
        running_info = (
            running_auto_uv_profile_info()
            if penguin_burner_runtime_is_active()
            else {"selector": "", "silent_fan_curve": False}
        )
        self.controls.set_status_text(
            runner_status_text(
                self.profile_summaries,
                running_selector=str(running_info["selector"]),
                autostart_selector=str(autostart_info["selector"]),
                running_silent_fan=bool(running_info["silent_fan_curve"]),
                autostart_silent_fan=bool(autostart_info["silent_fan_curve"]),
            )
        )

    def _workflow_running(self) -> bool:
        return (
            self.scan_controller.is_running()
            or self.command_controller.is_running()
            or self.verify_controller.is_running()
        )

    def _set_profile_actions_enabled(self, enabled: bool) -> None:
        self.profile_list.set_runtime_actions_enabled(bool(enabled))

    def _runtime_action_start_text(self, action: str) -> str:
        selected = self.profile_list.selected_profile_name() or "none"
        if action == "install-systemd":
            return f"Starting profile: {selected}; Systemd autostart: Yes."
        if action == "uninstall-systemd":
            return "Removing Systemd autostart entry."
        return f"Starting profile: {selected}; Systemd autostart: No."


def _probe_text(payload: dict) -> str:
    voltage = status_value(payload.get("voltage_mv") or payload.get("candidate_voltage_mv"))
    clock = status_value(payload.get("clock_mhz") or payload.get("lock_clock_mhz"))
    return f"{voltage or 'n/a'} mV @ {clock or 'n/a'} MHz"


def _duration_seconds(value, default_s: int) -> int:
    try:
        return max(1, int(round(float(value))))
    except (TypeError, ValueError):
        return int(default_s)


def _manual_curve_control_voltage_mvs(manual_edit) -> tuple[int, ...]:
    if not isinstance(manual_edit, dict):
        return ()
    raw_values = manual_edit.get("control_voltage_mvs")
    if not isinstance(raw_values, (list, tuple)):
        return ()
    values = []
    seen = set()
    for raw_value in raw_values:
        try:
            value = int(round(float(raw_value)))
        except (TypeError, ValueError):
            continue
        if value in seen:
            continue
        seen.add(value)
        values.append(value)
    return tuple(values)


def _stop_request_path() -> Path:
    return default_user_config_dir() / "auto-uv-stop-requested"


def _runtime_action_label(action: str) -> str:
    labels = {
        "daemonize": "Apply selected profile",
        "install-systemd": "Install startup profile",
        "uninstall-systemd": "Remove autostart entry",
        "delete-profiles": "Delete selected profiles",
    }
    return labels.get(str(action), str(action) or "Profile action")
