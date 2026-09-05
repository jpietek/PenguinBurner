from __future__ import annotations

from pathlib import Path
import shlex

from cli.runtime_config_file import (
    silent_fan_curve_from_runtime_config,
    silent_fan_curve_to_runtime_config,
)
from common.penguin_burner_paths import default_user_config_dir

from ui.features.integrations.afterburner_workflow import AfterburnerImportWorkflow
from ui.assets import asset_image_path
from ui.commands import scan_command
from ui.components.auto_uv_tier_progress import AutoUvTierProgress
from ui.components.curve_plot import CurvePlot
from ui.components.log_view import LogView
from ui.components.overlay_config import OverlayConfigPanel
from ui.components.profile_list import ProfileList
from ui.components.runs_table import RunsTable
from ui.components.scan_controls import ScanControls
from ui.components.status_header import StatusHeader
from ui.components.game_library_panel import GameLibraryPanel
from ui.features.profiles.profiles import adaptive_profile_tier_labels
from ui.constants import APP_DISPLAY_NAME
from .controllers.command import CommandController
from .controllers.scan import ScanController
from .controllers.verify import VerifyController
from ui.features.curves.curve_tabs import CurveTabs
from ui.dialogs.about import show_about_dialog
from ui.dialogs.final_choice import select_final_candidate
from ui.dialogs.scan_tuning import select_scan_tuning
from .error_reporting import ErrorReporter
from ui.features.tuning.gpu_selection import (
    detected_gpu_choices,
    gpu_choices_with_fallback,
    persist_runtime_gpu_index,
)
from ui.daemon_setup import ensure_daemon_ready_for_privileged_action
from ui.features.tuning.final_choice_controller import handle_final_choice_request
from . import theme
from .models import candidate_id_from_payload
from .models import event_base_points
from .models import event_points
from .models import stage_title
from .models import status_value
from .models import top_status_text
from ui.features.profiles.profiles import load_profile_summaries
from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR
from ui.features.profiles.profiles import penguin_burner_runtime_is_active
from ui.features.profiles.profiles import runner_status_parts
from ui.features.profiles.profiles import running_auto_uv_profile_info
from ui.features.profiles.profiles import systemd_autostart_profile_info
from ui.features.tuning.verify import stop_request_path as verify_stop_request_path
from ui.features.profiles.profile_actions import ProfileActionsMixin
from .styles import STYLESHEET


class MainWindow(ProfileActionsMixin):
    def __init__(
        self,
        qt_modules,
        *,
        gpu_index: int | None = None,
    ):
        self.QtCore, self.QtGui, self.QtWidgets, self.pg = qt_modules
        self.gpu_index = None if gpu_index is None else max(0, int(gpu_index))
        self.profile_summaries: list[dict] = []
        self.pending_final_result_payload: dict | None = None
        self.final_choice_discarded = False
        self.final_choice_aborted = False
        self.last_auto_uv_candidate_id = ""
        # True once the user restored stock GPU settings and nothing has been
        # applied since; drives the "Currently running profile: default" status.
        self._defaults_restored = False
        self._delete_restore_stock = False
        self._delete_switch_systemd_profile_id = ""
        self._boot_apply_by_gpu: dict[str, bool] = {}

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
        self.verify_controller.on_telemetry = self.vf_plot.set_live_load_marker
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
        self.runs_table = RunsTable(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
        )
        self.auto_uv_tier_progress = AutoUvTierProgress(QtWidgets=self.QtWidgets)
        self.overlay_config = OverlayConfigPanel(
            QtCore=self.QtCore,
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
        self.profile_list.on_target_gpu_changed = self._profile_target_gpu_changed
        self.log_view = LogView(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
        )

        auto_uv_view = self.QtWidgets.QSplitter(self.QtCore.Qt.Horizontal)
        auto_uv_view.addWidget(self.vf_plot.widget)
        auto_uv_view.addWidget(self.log_view.widget)
        auto_uv_view.setSizes([760, 440])

        self.game_library_panel = GameLibraryPanel(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            adaptive_available=lambda gpu_uuid="", include_legacy_profiles=False: len(
                adaptive_profile_tier_labels(
                    self.profile_summaries,
                    gpu_uuid=gpu_uuid,
                    include_legacy_profiles=include_legacy_profiles,
                )
            )
            >= 1,
            gpu_choices=detected_gpu_choices,
        )

        self.tabs = self.QtWidgets.QTabWidget()
        self.tabs.setIconSize(self.QtCore.QSize(18, 18))

        def tab_icon(filename: str):
            return self.QtGui.QIcon(str(asset_image_path(filename)))

        self.auto_uv_tab_index = self.tabs.addTab(
            auto_uv_view,
            tab_icon("tab-auto-uv.png"),
            "Auto-UV",
        )
        self.profiles_tab_index = self.tabs.addTab(
            self.profile_list.widget,
            tab_icon("tab-profiles.png"),
            "Profiles",
        )
        self.game_library_tab_index = self.tabs.addTab(
            self.game_library_panel.widget,
            tab_icon("tab-game-library.png"),
            "Game Library",
        )
        self.overlay_tab_index = self.tabs.addTab(
            self.overlay_config.widget,
            tab_icon("tab-overlay.png"),
            "In-Game Overlay",
        )
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

        self.table_panel = self.QtWidgets.QGroupBox("Undervolting runs")
        table_layout = self.QtWidgets.QVBoxLayout(self.table_panel)
        table_layout.setContentsMargins(10, 18, 10, 10)
        table_layout.addWidget(self.auto_uv_tier_progress.widget)
        table_layout.addWidget(self.runs_table.widget)

        # The plot/tabs area and the runs table share a draggable vertical
        # splitter; window resizes grow only the plot side. No pixel
        # constants: the table side floors at the runs table's own
        # MIN_VISIBLE_ROWS content minimum, and the tab side floors at the
        # tab bar plus the Auto-UV page's content minimum. An explicit tab
        # minimum is required because the splitter would otherwise honor the
        # LARGEST page's minimum-size hint (overlay/Steam), pinning the tab
        # area tall; the table panel only shows on the Auto-UV tab.
        self.auto_uv_split = self.QtWidgets.QSplitter(self.QtCore.Qt.Vertical)
        self.auto_uv_split.setObjectName("autoUvVerticalSplit")
        self.tabs.setMinimumHeight(
            self.tabs.tabBar().sizeHint().height()
            + auto_uv_view.minimumSizeHint().height()
        )
        self.auto_uv_split.addWidget(self.tabs)
        self.auto_uv_split.addWidget(self.table_panel)
        self.auto_uv_split.setStretchFactor(0, 1)
        self.auto_uv_split.setStretchFactor(1, 0)
        self.auto_uv_split.setCollapsible(0, False)
        self.auto_uv_split.setCollapsible(1, False)

        layout.addWidget(self.header.widget)
        layout.addWidget(self.controls.widget)
        layout.addWidget(self.auto_uv_split, 1)

        self.controls.start_button.clicked.connect(self.start_scan)
        self.controls.stop_button.clicked.connect(self.stop_scan)
        self.controls.about_button.clicked.connect(self.show_about)
        self.controls.import_afterburner_button.clicked.connect(self.afterburner_import.run)
        self.profile_list.daemonize_button.clicked.connect(self._run_profiles)
        self.profile_list.delete_button.clicked.connect(self._delete_selected_profiles)
        self.profile_list.silent_fan_checkbox.toggled.connect(
            self._persist_silent_fan_preference
        )
        self.profile_list.boot_apply_checkbox.toggled.connect(
            self._persist_boot_apply_preference
        )
        self.profile_list.main_gpu_checkbox.toggled.connect(
            self._persist_main_gpu_preference
        )
        self.profile_list.restore_defaults_button.clicked.connect(
            self._restore_gpu_defaults
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
        self.tabs.currentChanged.connect(self._sync_selected_tab_layout)
        self._sync_selected_tab_layout(self.tabs.currentIndex())
        self.window.setCentralWidget(root)
        self.window.setStyleSheet(STYLESHEET)
        # Keep the "Currently running profile" line current even when the
        # daemon/GPU state changes outside the UI (external CLI, a profile that
        # exited, reboot). Lightweight: recomputes only the status text.
        self._status_timer = self.QtCore.QTimer(self.window)
        self._status_timer.setInterval(2000)
        self._status_timer.timeout.connect(self._poll_running_status)
        self._status_timer.start()
        self._status_poll_thread = None
        self._status_poll_result = None

    def show(self) -> None:
        self.window.show()
        # Run environment checks after the window paints, so a first launch or
        # a post-upgrade launch guides the user instead of failing later.
        self.QtCore.QTimer.singleShot(0, self._run_startup_checks)

    def _run_startup_checks(self) -> None:
        self._check_gpu_supported_on_startup()
        self._check_daemon_upgrade_on_startup()

    def _check_gpu_supported_on_startup(self) -> None:
        """Warn immediately when no usable NVIDIA GPU is present.

        Cheap, unprivileged, daemon-free: the NVIDIA kernel driver exposes
        /dev/nvidia0. Catching "no NVIDIA GPU / driver" here means an
        unsupported box gets a plain explanation up front instead of a cryptic
        scan failure after a pointless root-service install. The deeper
        architecture/driver validation still runs at scan start.
        """
        if Path("/dev/nvidia0").exists() or Path("/dev/nvidiactl").exists():
            return
        self.QtWidgets.QMessageBox.warning(
            self.window,
            "No NVIDIA GPU detected",
            "PenguinBurner tunes NVIDIA GPUs and could not find one on this "
            "system (no NVIDIA kernel driver is loaded).\n\n"
            "Make sure a supported NVIDIA card and the proprietary driver are "
            "installed, then restart PenguinBurner. Undervolt scans and profile "
            "applies will not work until then.",
        )

    def _check_daemon_upgrade_on_startup(self) -> None:
        """Offer to update a running daemon from another incompatible release.

        Only fires when a daemon is actually running and its protocol or release
        differs from this application. A brand-new user with no daemon installed
        is left alone: the install prompt appears when they first do something
        privileged, not on launch.
        """
        from runtime.daemon_client import (
            DaemonCompatibilityError,
            daemon_status,
            require_daemon_capabilities,
        )
        from ui.assets import application_version

        try:
            daemon_status(timeout_s=1.0)
        except Exception:
            return  # not running / not installed — do not nag a new user
        try:
            require_daemon_capabilities(expected_version=application_version())
            return  # running and compatible — nothing to do
        except DaemonCompatibilityError:
            pass
        except Exception:
            return
        ensure_daemon_ready_for_privileged_action(
            QtWidgets=self.QtWidgets,
            parent=self.window,
            log=self.log_view.append,
            action_label="Updating the PenguinBurner hardware service",
        )
        self._load_profiles()

    def _sync_selected_tab_layout(self, index: int) -> None:
        if index == self.game_library_tab_index:
            # Scanned on first entry, not while the window is being built: a
            # library read is filesystem work nobody has asked for until the
            # tab is looked at. Idempotent, so switching back costs nothing.
            self.game_library_panel.ensure_scanned()
        # The undervolting-runs table belongs to the Auto-UV workflow only.
        self.table_panel.setVisible(index == self.auto_uv_tab_index)

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
        # The setup dialog reads GPU identity and limits through the daemon,
        # so the service must be installed/updated BEFORE the dialog opens —
        # otherwise a fresh or upgraded install shows a misleading generic
        # "NVIDIA GPU" with no limits (the exact state that scares users off
        # the one action that would fix it).
        if not ensure_daemon_ready_for_privileged_action(
            QtWidgets=self.QtWidgets,
            parent=self.window,
            log=self.log_view.append,
            action_label="Setting up Auto-UV",
            # The scan streams through the daemon; a stale daemon without this
            # capability must land in the repair prompt, not fail at scan start.
            required_capabilities=("scan-stream-v1",),
        ):
            return
        options = select_scan_tuning(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            parent=self.window,
            gpu_index=self.gpu_index,
        )
        if options is None:
            return
        try:
            self.gpu_index = persist_runtime_gpu_index(options.get("gpu_index", 0))
        except Exception as exc:
            self.errors.show(
                "GPU selection",
                f"Could not save selected GPU index: {exc}",
            )
            return
        options = {**options, "gpu_index": int(self.gpu_index)}
        # Remember the silent-fan intent BEFORE the scan: a foreground scan
        # resets the GPU to stock, so mid-scan the "running" profile reads as
        # fan-off. Without this, the completion auto-apply recomputes the
        # checkbox from that stock state and silently drops a fan curve the
        # user had running. Capture the checkbox OR the live running profile.
        self._pre_scan_silent_fan = bool(
            self.profile_list.silent_fan_enabled()
            or (
                penguin_burner_runtime_is_active()
                and running_auto_uv_profile_info().get("silent_fan_curve")
            )
        )
        # Bring the scan into view: the live runs/curve are on the Auto-UV tab.
        self.tabs.setCurrentIndex(self.auto_uv_tab_index)
        command = scan_command(options)
        self.runs_table.clear()
        if str(options.get("auto_uv_mode") or "") == "adaptive":
            self.auto_uv_tier_progress.start()
        else:
            self.auto_uv_tier_progress.clear()
        self.vf_plot.clear()
        self.pending_final_result_payload = None
        self.final_choice_discarded = False
        self.final_choice_aborted = False
        self.last_auto_uv_candidate_id = ""
        # A scan applies its own curves, so the GPU is no longer at stock.
        self._defaults_restored = False
        self.controls.hide_dependency_progress()
        self.log_view.append("$ " + " ".join(shlex.quote(part) for part in command) + "\n")
        self.header.set_stage("Starting")
        self.header.set_candidate("Writing to main Auto-UV profile store")
        # Show the requested memory offset immediately; the scan later confirms
        # the actually-applied (post-clamp) value via a memory_offset_applied
        # event. Full scans carry per-tier offsets; the efficiency (first)
        # tier's offset is what the scan opens with. NVML offsets are
        # transfer-rate units (MT/s); the memory clock moves by half.
        requested_memory_offset_mt_s = options.get("auto_uv_memory_offset_mhz")
        if requested_memory_offset_mt_s in (None, ""):
            requested_memory_offset_mt_s = options.get(
                "auto_uv_efficiency_memory_offset_mhz"
            )
        requested_memory_offset_mhz = int(requested_memory_offset_mt_s or 0) // 2
        self.controls.set_status_text(
            _memory_offset_status_text(requested_memory_offset_mhz)
        )
        self.controls.set_running(True)
        self.profile_list.set_runtime_actions_enabled(False)
        if not self.scan_controller.start(command):
            self.auto_uv_tier_progress.mark_unfinished("failed")
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
        elif event == "load_telemetry":
            self.runs_table.update_probe_progress(payload)
            self.vf_plot.set_live_load_marker(payload)
        elif event in {"source_curve", "base_curve"}:
            self.controls.hide_dependency_progress()
            points = event_base_points(payload)
            self.vf_plot.set_source_points(points)
            self.curve_tabs.set_base_points(points)
        elif event == "candidate_curve":
            self.runs_table.record_candidate_curve(payload)
            self.vf_plot.set_candidate_points(
                event_points(payload),
                curve_id=candidate_id_from_payload(payload),
            )
        elif event == "memory_offset_applied":
            self.controls.set_status_text(
                _memory_offset_status_text(payload.get("offset_mhz"))
            )
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
        elif event == "tier_started":
            tier_name = str(payload.get("tier", ""))
            tier_position = _tier_position_text(payload)
            self.auto_uv_tier_progress.set_active(tier_name)
            self.header.set_stage(f"{tier_name.title()} scan{tier_position}")
            self.controls.set_status_text(
                f"Scanning {tier_name.title()} profile{tier_position}."
            )
        elif event == "tier_descent_reused":
            tier_name = str(payload.get("tier", ""))
            source_tier = str(payload.get("source_tier", ""))
            self.controls.set_status_text(
                f"{tier_name.title()} reuses the {source_tier.title()} "
                f"downsweep ({payload.get('voltage_mv')}mV @ "
                f"{payload.get('target_mhz')}MHz) — starting the Auto-OC climb."
            )
        elif event == "tier_confirmed":
            tier_name = str(payload.get("tier", ""))
            self.controls.set_status_text(
                f"{tier_name.title()} tier confirmed: "
                f"{payload.get('voltage_mv')}mV @ {payload.get('target_mhz')}MHz"
            )
            tier_points = event_points(payload)
            tier_color = _TIER_CURVE_COLORS.get(tier_name)
            if tier_points and tier_color:
                self.vf_plot.add_comparison_points(
                    tier_points,
                    name=f"{tier_name.title()} tier",
                    color=tier_color,
                    alpha=220,
                    width=2,
                    # The final tier is also the live candidate. Keep the
                    # persistent tier-colored trace above that identical
                    # green curve so Performance remains visibly red.
                    z_value=5,
                )
        elif event == "tier_completed":
            tier_name = str(payload.get("tier", ""))
            next_tier = str(payload.get("next_tier", ""))
            self.auto_uv_tier_progress.set_completed(tier_name)
            self.header.set_stage(
                f"{tier_name.title()} complete{_tier_position_text(payload)}"
            )
            self.controls.set_status_text(
                f"{tier_name.title()} profile complete."
                + (
                    f" Continuing with {next_tier.title()}."
                    if next_tier
                    else " All selected profiles are complete."
                )
            )
        elif event == "tier_skipped":
            tier_name = str(payload.get("tier", ""))
            next_tier = str(payload.get("next_tier", ""))
            self.auto_uv_tier_progress.set_skipped(tier_name)
            self.controls.set_status_text(
                f"{tier_name.title()} tier skipped: "
                f"{payload.get('reason') or 'no stable candidate found'}"
                + (f". Continuing with {next_tier.title()}." if next_tier else "")
            )

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
        # The download status lives in the progress bar; the status label keeps
        # showing the applied memory offset so a terminal "Dependencies are
        # ready" no longer sits in it for the whole run.
        detail = str(payload.get("detail") or "Downloading dependencies").strip()
        percent = payload.get("percent", 0)
        self.header.set_stage("Downloading dependencies")
        self.header.set_candidate("")
        self.controls.set_dependency_progress(percent, detail=detail)

    def _handle_final_choice_request(self, payload: dict) -> None:
        result = handle_final_choice_request(
            payload,
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            parent=self.window,
            scan_controller=self.scan_controller,
            log_view=self.log_view,
            select_final_candidate_fn=select_final_candidate,
        )
        if result.discarded:
            self.final_choice_discarded = True
        if result.aborted:
            self.final_choice_aborted = True
        if result.selected_candidate_id:
            self.last_auto_uv_candidate_id = result.selected_candidate_id

    def _scan_finished(self, exit_code, exit_status, stopped_by_user: bool) -> None:
        apply_final_profile = False
        status_name = "finished" if int(exit_code) == 0 else "stopped"
        self.log_view.append(f"\nAuto-UV process {status_name}: exit_code={exit_code}\n")
        failed = int(exit_code) != 0 and not stopped_by_user
        if self.final_choice_aborted:
            # A user-requested abort is not a failure even though the scan
            # process exits non-zero: label the run "Aborted", never "Failed".
            self.header.set_stage("Aborted")
            self.runs_table.mark_running_rows_stopped(
                label="Aborted", row_state="warning"
            )
            self.controls.set_status_text("Auto-UV aborted by user.")
            self.auto_uv_tier_progress.mark_unfinished("stopped")
        elif failed:
            self.header.set_stage("Error")
            self.runs_table.mark_running_rows_stopped(label="Failed")
            self.controls.set_status_text("Auto-UV failed.")
            self.auto_uv_tier_progress.mark_unfinished("failed")
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
            apply_final_profile = True
        elif stopped_by_user:
            self.header.set_stage("Stopped")
            self.runs_table.mark_running_rows_stopped(label="Stopped")
            self.auto_uv_tier_progress.mark_unfinished("stopped")
        else:
            self.header.set_stage("Idle")
        self.controls.set_running(False)
        self.controls.hide_dependency_progress()
        self.profile_list.set_runtime_actions_enabled(False)
        self.pending_final_result_payload = None
        self.final_choice_discarded = False
        self.final_choice_aborted = False
        # The root daemon suspended the exact active RuntimeSpec before starting
        # the scan and restores it before the streaming command exits. That spec
        # may be a transient selected profile or Adaptive mode and can differ from
        # the boot/autostart profile, so the UI must not apply a second fallback.
        self._load_profiles()
        if apply_final_profile:
            # Restore the pre-scan silent-fan intent: the reload above may have
            # unticked the checkbox from the mid-scan stock state, and the
            # auto-apply reads the checkbox. A scan must never turn off a fan
            # curve the user had running.
            if getattr(self, "_pre_scan_silent_fan", False):
                self.profile_list.set_silent_fan_checked(True)
            self._pre_scan_silent_fan = False
            # Apply the freshly verified profile now (and for boot when the
            # "Apply on startup" toggle is ticked).
            # Deferred one event-loop turn so the scan controller has fully
            # released before the apply command starts. One static preset:
            # adaptivity is a per-game (Steam tab) choice, not a standing one.
            self.QtCore.QTimer.singleShot(
                0, lambda: self._run_runtime_action("daemonize")
            )

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
        # The live GPU-position marker only means something while this
        # profile's own verification is running; leaving it up afterward
        # would misleadingly suggest it still reflects the current state.
        self.vf_plot.clear_load_markers()
        # The header's candidate label is only ever set by a running scan or
        # verify; clear it here too, or it keeps showing the just-verified
        # profile's name indefinitely, including through unrelated later
        # Apply actions that never touch this label themselves.
        self.header.set_candidate("")
        self.controls.set_running(False)
        self._load_profiles()

    def _load_profiles(self) -> None:
        self.profile_summaries = load_profile_summaries()
        autostart_info = systemd_autostart_profile_info()
        running_info = (
            running_auto_uv_profile_info()
            if penguin_burner_runtime_is_active()
            else {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False}
        )
        self.profile_list.set_profiles(
            self.profile_summaries,
            preferred_candidate_id=self.last_auto_uv_candidate_id,
            select_preferred=bool(self.last_auto_uv_candidate_id),
            # The silent-fan tick is sticky: the user's persisted choice is
            # authoritative and survives discarded/aborted Auto-UV runs and
            # reloads. We OR in the live runtime / autostart flag only so an
            # already-applied silent-fan profile still shows checked on a fresh
            # install where nothing has been toggled yet.
            silent_fan_checked=(
                silent_fan_curve_from_runtime_config()
                or bool(running_info["silent_fan_curve"])
                or bool(autostart_info["silent_fan_curve"])
            ),
        )
        gpu_choices, _selected_gpu_index = gpu_choices_with_fallback(
            selected_index=self.gpu_index
        )
        self.profile_list.configure_gpu_targets(
            self.profile_summaries,
            gpu_choices,
            preferred_index=self.gpu_index,
        )
        self._sync_boot_apply_for_target(self.profile_list.target_gpu_uuid())
        self._set_profile_actions_enabled(not self._workflow_running())
        self._refresh_running_status(running_info, autostart_info)

    def _profile_target_gpu_changed(
        self,
        gpu_index: int | None,
        _gpu_uuid: str,
    ) -> None:
        self._sync_boot_apply_for_target(_gpu_uuid)
        if gpu_index is None:
            return
        try:
            self.gpu_index = persist_runtime_gpu_index(int(gpu_index))
        except Exception as exc:
            self.errors.show(
                "GPU selection",
                f"Could not save selected GPU index: {exc}",
            )

    def _poll_running_status(self) -> None:
        """Gather live daemon/systemd status OFF the GUI thread, then render.

        The status gathering makes daemon socket calls and up to two systemctl
        subprocesses; doing that on the GUI thread every 2 s freezes the window
        whenever the daemon wedges. A worker fetches the two info dicts and the
        cheap render is applied back on the GUI thread.
        """
        if self._workflow_running() or self.command_controller.is_running():
            return
        thread = self._status_poll_thread
        if thread is not None and thread.is_alive():
            return
        self._status_poll_result = None

        def run() -> None:
            running = (
                running_auto_uv_profile_info()
                if penguin_burner_runtime_is_active()
                else {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False}
            )
            self._status_poll_result = (running, systemd_autostart_profile_info())

        import threading

        self._status_poll_thread = threading.Thread(target=run, daemon=True)
        self._status_poll_thread.start()
        self._collect_status_poll()

    def _collect_status_poll(self) -> None:
        thread = self._status_poll_thread
        if thread is not None and thread.is_alive():
            self.QtCore.QTimer.singleShot(120, self._collect_status_poll)
            return
        result = self._status_poll_result
        if result is not None:
            self._refresh_running_status(result[0], result[1])

    def _refresh_running_status(
        self,
        running_info: dict | None = None,
        autostart_info: dict | None = None,
    ) -> None:
        """Recompute the 'Currently running profile' line from LIVE state.

        Skipped while a scan/command owns the status line. When called with no
        info dicts (rare — internal callers pass them), it falls back to a
        synchronous gather; the periodic path uses ``_poll_running_status`` to
        keep the daemon/systemctl I/O off the GUI thread.
        """
        if self._workflow_running() or self.command_controller.is_running():
            return
        if running_info is None:
            running_info = (
                running_auto_uv_profile_info()
                if penguin_burner_runtime_is_active()
                else {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False}
            )
        if autostart_info is None:
            autostart_info = systemd_autostart_profile_info()
        # A live running profile always wins: it means the flag is stale, so
        # clear it. This keeps the status honest if a profile is (re)applied
        # outside the UI after a restore.
        live_selector = str(running_info["selector"]).strip()
        if live_selector and live_selector != STOCK_PROFILE_SELECTOR:
            # A real profile is live -> the restore flag is stale; clear it. The
            # stock sentinel is NOT a real profile, so it keeps "Default".
            self._defaults_restored = False
        head, detail = runner_status_parts(
            self.profile_summaries,
            running_selector=str(running_info["selector"]),
            running_adaptive=bool(running_info["adaptive_auto_uv"]),
            autostart_selector=str(autostart_info["selector"]),
            running_silent_fan=bool(running_info["silent_fan_curve"]),
            autostart_silent_fan=bool(autostart_info["silent_fan_curve"]),
            defaults_restored=self._defaults_restored,
            game_override=bool(running_info.get("game_override")),
            standing_selector=str(running_info.get("standing_selector") or ""),
            standing_adaptive=bool(running_info.get("standing_adaptive")),
        )
        # Middle dots, not semicolons: these are separate facts, not clauses.
        self.controls.set_status_text(" · ".join(head), " · ".join(detail))

    def _persist_silent_fan_preference(self, checked: bool) -> None:
        # Remember the silent-fan choice durably so the "latest profile setup"
        # restores it after an aborted Auto-UV run, profile reload, or restart.
        silent_fan_curve_to_runtime_config(bool(checked))

    def _workflow_running(self) -> bool:
        return (
            self.scan_controller.is_running()
            or self.command_controller.is_running()
            or self.verify_controller.is_running()
        )

    def _set_profile_actions_enabled(self, enabled: bool) -> None:
        self.profile_list.set_runtime_actions_enabled(bool(enabled))

def _probe_text(payload: dict) -> str:
    voltage = status_value(payload.get("voltage_mv") or payload.get("candidate_voltage_mv"))
    clock = status_value(payload.get("clock_mhz") or payload.get("lock_clock_mhz"))
    return f"{voltage or 'n/a'} mV @ {clock or 'n/a'} MHz"


_TIER_CURVE_COLORS = {
    "efficiency": theme.TIER_CURVE_EFFICIENCY,
    "balanced": theme.TIER_CURVE_BALANCED,
    "performance": theme.TIER_CURVE_PERFORMANCE,
}


def _memory_offset_status_text(offset_mhz) -> str:
    """Status-bar text for the memory offset applied during the scan (MHz)."""
    try:
        mhz = int(offset_mhz)
    except (TypeError, ValueError):
        mhz = 0
    if mhz == 0:
        return "Memory offset: none"
    return f"Memory offset: {mhz:+d} MHz memory clock"


def _tier_position_text(payload: dict) -> str:
    try:
        position = int(payload.get("position") or 0)
        total = int(payload.get("total") or 0)
    except (TypeError, ValueError):
        return ""
    if position <= 0 or total <= 0:
        return ""
    return f" ({position}/{total})"


def _stop_request_path() -> Path:
    return default_user_config_dir() / "auto-uv-stop-requested"
