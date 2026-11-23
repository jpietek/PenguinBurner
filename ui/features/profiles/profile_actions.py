from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from cli.runtime_config_file import persist_on_startup_to_runtime_config
from common.penguin_burner_paths import default_user_config_dir
from curve_editors.uv.vf_curve_manual_editor import editable_anchor_from_profile
from drivers.nvidia.daemon_gpu import DaemonGpuClient
from profiles.gpu_identity import (
    GPU_COMPATIBILITY_LEGACY,
    GPU_COMPATIBILITY_MATCH,
    normalized_gpu_identity,
    profile_gpu_compatibility,
    profile_gpu_uuid,
)
from profiles.uv.memory_offset_edit import editable_memory_offset_from_profile
from profiles.uv.profile_store import (
    STOCK_PROFILE_SELECTOR,
    bind_auto_uv_profile_gpu_identity,
    delete_auto_uv_profile_paths,
)
from profiles.uv.profile_tiers import (
    save_profile_tier_assignment,
    save_profile_tier_none_assignment,
)
from runtime.daemon_client import clear_boot_runtime_spec, set_boot_main_gpu
from runtime.daemon_commands import (
    delete_profiles_command,
    profile_verify_command,
    runtime_profile_command,
)
from ui.components.fan_curve_editor import open_fan_curve_editor_dialog
from ui.components.memory_offset_editor import open_memory_offset_editor_dialog
from ui.components.vf_curve_editor import open_vf_curve_editor_dialog
from ui.daemon_setup import ensure_daemon_ready_for_privileged_action
from ui.dialogs.verify import select_verify_options
from ui.features.curves.curve_profiles import (
    curve_points_from_values,
    profile_base_curve_points,
    profile_curve_plan,
    save_edited_curve_profile,
)
from ui.features.curves.fan_profiles import (
    profile_fan_curve_points,
    profile_fan_curve_target_point,
    profile_fan_measurement_points,
    profile_id_from_archive_path,
    save_edited_fan_profile,
    sync_profile_fan_payload,
)
from ui.features.curves.memory_offset_profiles import save_edited_memory_offset_profile
from ui.features.integrations.lact_export import (
    detect_lact_gpu_id,
    lact_export_output_path,
    write_lact_profile_config,
)
from ui.features.profiles.profiles import (
    adaptive_profile_tier_labels,
    delete_confirmation_text,
    penguin_burner_runtime_is_active,
    profile_can_apply,
    profile_can_verify,
    profile_delete_autostart_action,
    profile_for_selector,
    profile_is_deletable,
    profile_status_label,
    profile_verify_selector,
    running_auto_uv_profile_info,
    systemd_autostart_profile_info,
)
from ui.features.tuning.tuning import memory_offset_mhz_range
from ui.features.tuning.verify import stop_request_path as verify_stop_request_path
from ui.features.tuning.verify import workload_label


class ProfileActionsMixin:
    QtCore: Any
    QtGui: Any
    QtWidgets: Any
    pg: Any
    window: Any
    tabs: Any
    auto_uv_tab_index: int
    profile_list: Any
    profile_summaries: list[dict]
    log_view: Any
    controls: Any
    errors: Any
    header: Any
    curve_tabs: Any
    vf_plot: Any
    scan_controller: Any
    command_controller: Any
    verify_controller: Any
    gpu_index: int | None
    last_auto_uv_candidate_id: str
    _boot_apply_by_gpu: dict[str, bool]

    def _workflow_running(self) -> bool:
        raise NotImplementedError

    def _load_profiles(self) -> None:
        raise NotImplementedError

    def _persist_boot_apply_preference(self, checked: bool) -> None:
        gpu_uuid = self.profile_list.target_gpu_uuid()
        if self.profile_list.target_selection_required() and not gpu_uuid:
            return
        if checked:
            if gpu_uuid:
                # Intent only: the boot entry itself is written by the next
                # apply, and _sync_boot_apply_for_target drops this the moment
                # the daemon reports the entry.
                self._boot_apply_by_gpu[gpu_uuid.casefold()] = True
            # Records the user's preference, not what boot will do -- the
            # daemon's boot set answers that, and this is never read back for
            # it. Kept because it is a documented setting in existing configs.
            persist_on_startup_to_runtime_config(True)
            return
        # Unticking means "nothing applies at boot": clear the selected GPU's
        # saved entry immediately. Do not commit the visible/cache preference
        # until the daemon confirms the write; otherwise a socket failure makes
        # the UI claim boot is disabled while the old profile still applies.
        try:
            clear_boot_runtime_spec(gpu_uuid=gpu_uuid)
        except Exception as exc:  # noqa: BLE001
            message = f"Could not clear the saved boot profile: {exc}"
            if gpu_uuid:
                self._boot_apply_by_gpu[gpu_uuid.casefold()] = True
            self.profile_list.set_boot_apply_checked(True)
            self.log_view.append(f"\n{message}\n")
            self.errors.show("Apply on startup", message)
            return
        if gpu_uuid:
            self._boot_apply_by_gpu.pop(gpu_uuid.casefold(), None)
        persist_on_startup_to_runtime_config(False)

    def _sync_boot_apply_for_target(self, gpu_uuid: str) -> None:
        """Show what boot will actually do, as the daemon currently has it.

        The daemon's boot set is the only thing that decides what applies at
        startup, so it is the only thing this reads. The saved preference is
        written when the box is ticked but nothing rewrites it when the entry
        goes away, so consulting it here promised a startup apply for a boot
        that had nothing saved to apply.

        The session cache carries only intent the daemon has not been told
        about yet -- a freshly ticked box, whose entry the next apply writes.
        """
        selected_uuid = str(gpu_uuid or "").strip()
        if not selected_uuid:
            # Without a UUID the daemon still answers for its selected GPU.
            info = systemd_autostart_profile_info()
            self.profile_list.set_boot_apply_checked(bool(info.get("selector")))
            self.profile_list.set_main_gpu_state(
                checked=False,
                has_boot_profile=False,
            )
            return
        key = selected_uuid.casefold()
        info = systemd_autostart_profile_info(gpu_uuid=selected_uuid)
        saved = bool(info.get("selector"))
        if saved:
            # Confirmed on the daemon: there is nothing left pending.
            self._boot_apply_by_gpu.pop(key, None)
        # OR rather than either alone, which keeps one invariant: the box is
        # never shown OFF while the daemon holds an entry. Every apply sends
        # --boot or --clear-boot from this state, so showing OFF over a saved
        # entry is what let an unrelated apply silently clear a boot profile.
        self.profile_list.set_boot_apply_checked(
            saved or self._boot_apply_by_gpu.get(key, False)
        )
        self.profile_list.set_main_gpu_state(
            checked=bool(info.get("main_gpu")),
            has_boot_profile=saved,
        )

    def _persist_main_gpu_preference(self, checked: bool) -> None:
        gpu_uuid = self.profile_list.target_gpu_uuid()
        if not gpu_uuid or not self.profile_list.target_selection_required():
            self.profile_list.set_main_gpu_state(
                checked=False,
                has_boot_profile=False,
            )
            return
        has_boot_profile = self.profile_list.main_gpu_target_has_boot_profile()
        if checked and not has_boot_profile:
            self.profile_list.set_main_gpu_state(
                checked=False,
                has_boot_profile=False,
            )
            return
        try:
            set_boot_main_gpu(gpu_uuid if checked else "")
        except Exception as exc:  # noqa: BLE001
            self.profile_list.set_main_gpu_state(
                checked=not checked,
                has_boot_profile=has_boot_profile,
            )
            self.errors.show(
                "Main GPU",
                f"Could not update the daemon's main GPU: {exc}",
            )
            return
        self.profile_list.set_main_gpu_state(
            checked=checked,
            has_boot_profile=has_boot_profile,
        )

    def _persist_silent_fan_preference(self, checked: bool) -> None:
        raise NotImplementedError

    def _set_profile_actions_enabled(self, enabled: bool) -> None:
        raise NotImplementedError

    def _apply_profile_action(self) -> str:
        # Apply always targets the already-root daemon. Persistence is a daemon
        # API flag on that request; checking for the host unit from a Flatpak is
        # unreliable because the sandbox cannot see the host's /etc/systemd.
        # If the daemon is genuinely unavailable, the migration gate performs
        # the one allowed systemd lifecycle elevation before this request runs.
        return "daemonize"

    def _run_profiles(self) -> None:
        self._run_runtime_action(self._apply_profile_action())

    def _run_runtime_action(
        self,
        action: str,
        *,
        adaptive_auto_uv: bool = False,
        profile_selector: str = "",
    ) -> None:
        if self._workflow_running():
            return
        target_gpu_index = self.profile_list.target_gpu_index()
        target_gpu_uuid = self.profile_list.target_gpu_uuid()
        if target_gpu_index is None:
            message = "Choose a target GPU before applying this profile."
            self.controls.set_status_text(message)
            self.log_view.append(f"\n{message}\n")
            return
        profile_id = str(profile_selector or "").strip()
        selected_profile = None
        if adaptive_auto_uv:
            tiers = adaptive_profile_tier_labels(
                self.profile_summaries,
                gpu_uuid=target_gpu_uuid,
                include_legacy_profiles=self.profile_list.single_physical_gpu(),
            )
            if not tiers:
                available = ", ".join(tiers) if tiers else "none"
                self.errors.show(
                    "Adaptive Auto-UV unavailable",
                    "Adaptive Auto-UV requires at least one verified Auto-UV "
                    f"profile. Available tiers: {available}.",
                )
                return
        else:
            profile_id = profile_id or self.profile_list.selected_profile_id()
            if not profile_id:
                self.log_view.append("\nNo profile selected.\n")
                return
            selected_profile = profile_for_selector(self.profile_summaries, profile_id)
            if not profile_can_apply(selected_profile or {}):
                message = "This edited profile must be verified before it can be applied."
                self.controls.set_status_text(message)
                self.log_view.append(f"\n{message}\n")
                return
            if not self.profile_list.profile_matches_target(selected_profile or {}):
                compatibility = profile_gpu_compatibility(
                    selected_profile or {}, target_gpu_uuid
                )
                message = (
                    "This legacy profile must be verified on the selected GPU "
                    "before it can be applied."
                    if compatibility == GPU_COMPATIBILITY_LEGACY
                    else "This profile belongs to a different GPU. Choose its GPU target."
                )
                self.controls.set_status_text(message)
                self.log_view.append(f"\n{message}\n")
                return
        if (
            selected_profile
            and self.profile_list.silent_fan_enabled()
            and not sync_profile_fan_payload(selected_profile)
        ):
            self.controls.set_status_text("No runtime-ready silent fan curve is available.")
            self.log_view.append("\nNo runtime-ready silent fan curve is available.\n")
            return
        if not ensure_daemon_ready_for_privileged_action(
            QtWidgets=self.QtWidgets,
            parent=self.window,
            log=self.log_view.append,
            action_label=(
                "Applying adaptive Auto-UV"
                if adaptive_auto_uv
                else "Applying runtime profile"
            ),
        ):
            return
        if not self._confirm_runtime_gpu_switch(target_gpu_uuid):
            return
        if (
            selected_profile
            and profile_gpu_compatibility(selected_profile, target_gpu_uuid)
            == GPU_COMPATIBILITY_LEGACY
            and Path(str(selected_profile.get("path") or "")).is_file()
        ):
            try:
                identity = normalized_gpu_identity(
                    DaemonGpuClient(int(target_gpu_index)).capabilities().identity,
                    index_at_verification=int(target_gpu_index),
                )
                bind_auto_uv_profile_gpu_identity(
                    str(selected_profile.get("path") or profile_id),
                    identity,
                )
                selected_profile["gpu_identity"] = identity
            except Exception as exc:  # noqa: BLE001
                self.errors.show(
                    "GPU profile binding",
                    f"Could not bind this legacy profile to the selected GPU: {exc}",
                )
                return
        # Boot persistence follows the "Apply on startup" toggle: ticked saves
        # the applied profile for boot, unticked applies session-only and
        # clears any saved boot profile. Restore defaults still persists the
        # stock runtime.
        persist_on_startup = self.profile_list.persist_on_startup_enabled()
        command = runtime_profile_command(
            action,
            profile_selector=profile_id,
            silent_fan_curve=self.profile_list.silent_fan_enabled(),
            adaptive_auto_uv=adaptive_auto_uv,
            gpu_index=int(target_gpu_index),
            persist_on_startup=persist_on_startup,
        )
        self._persist_silent_fan_preference(self.profile_list.silent_fan_enabled())
        # A profile is now in effect, so the GPU is no longer at stock.
        self._defaults_restored = False
        self.controls.set_status_text(
            self._runtime_action_start_text(adaptive_auto_uv=adaptive_auto_uv)
        )
        self._set_profile_actions_enabled(False)
        self.controls.start_button.setEnabled(False)
        self.command_controller.start(
            f"adaptive-{action}" if adaptive_auto_uv else action,
            command,
            fail_text="Failed to start runtime profile action.",
        )

    def _restore_gpu_defaults(self) -> None:
        if self._workflow_running():
            return
        target_gpu_index = self.profile_list.target_gpu_index()
        if target_gpu_index is None:
            self.errors.show(
                "Restore defaults",
                "Choose a target GPU before restoring defaults.",
            )
            return
        # Elevated work goes through the already-root daemon (NO pkexec): ask it
        # to run the reserved stock runtime. The daemon stops the current profile,
        # resets the GPU to factory, applies no undervolt, and keeps running for
        # fan control. See CLAUDE.md: privileged ops route through the daemon.
        if not ensure_daemon_ready_for_privileged_action(
            QtWidgets=self.QtWidgets,
            parent=self.window,
            log=self.log_view.append,
            action_label="Restoring GPU to stock",
        ):
            return
        command = runtime_profile_command(
            "daemonize",
            profile_selector=STOCK_PROFILE_SELECTOR,
            silent_fan_curve=self.profile_list.silent_fan_enabled(),
            gpu_index=int(target_gpu_index),
            # Stock is the safe state: restoring persists it for boot
            # regardless of the Apply-on-startup toggle.
            persist_on_startup=True,
        )
        self.controls.set_status_text(
            "Restoring GPU to stock now and at boot (daemon keeps running)."
        )
        self._set_profile_actions_enabled(False)
        self.controls.start_button.setEnabled(False)
        self.command_controller.start(
            "restore-keep-stock",
            command,
            fail_text="Failed to restore GPU to stock.",
        )

    def _run_delete_autostart_followup(self, *, restore_stock: bool) -> bool:
        self._delete_restore_stock = False
        if restore_stock:
            self._restore_gpu_defaults()
            return True
        return False

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
            path, _payload = save_edited_fan_profile(
                profile,
                edit,
                original_points=curve_points,
            )
            profile_id = profile_id_from_archive_path(path)
            if profile_id:
                self.profile_list.select_profile(profile_id)
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

    def _edit_profile_memory_offset(self, profile: dict) -> None:
        current = editable_memory_offset_from_profile(profile)
        if current is None:
            self.QtWidgets.QMessageBox.information(
                self.window,
                "Edit Memory Offset",
                "No editable memory offset is available for this profile.",
            )
            return
        target_gpu_index = self.profile_list.target_gpu_index()
        if target_gpu_index is None:
            self.errors.show(
                "Edit Memory Offset",
                "Choose this profile's GPU before editing its memory offset.",
            )
            return
        min_mhz, max_mhz = memory_offset_mhz_range(
            gpu_index=int(target_gpu_index)
        )

        def save_edit(new_memory_offset_mhz: int) -> str:
            path, _payload = save_edited_memory_offset_profile(
                profile,
                new_memory_offset_mhz,
                original_memory_offset_mhz=current,
            )
            profile_id = profile_id_from_archive_path(path)
            message = (
                f"Saved edited memory offset draft: {path.name}. "
                "Verify it before Apply."
            )
            self.controls.set_status_text(message)
            self._load_profiles()
            if profile_id:
                self.profile_list.select_profile(profile_id)
            return message

        open_memory_offset_editor_dialog(
            QtWidgets=self.QtWidgets,
            parent=self.window,
            current_memory_offset_mhz=current,
            min_mhz=min_mhz,
            max_mhz=max_mhz,
            save_callback=save_edit,
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
        except Exception as exc:  # noqa: BLE001
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
        target_gpu_index = self.profile_list.target_gpu_index()
        target_gpu_uuid = self.profile_list.target_gpu_uuid()
        if target_gpu_index is None:
            self.errors.show(
                "Profile verification",
                "Choose a target GPU before verifying this profile.",
            )
            return
        compatibility = profile_gpu_compatibility(profile, target_gpu_uuid)
        if compatibility not in {GPU_COMPATIBILITY_MATCH, GPU_COMPATIBILITY_LEGACY}:
            self.errors.show(
                "Profile verification",
                "This profile belongs to a different GPU. Choose its GPU target.",
            )
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
        if not ensure_daemon_ready_for_privileged_action(
            QtWidgets=self.QtWidgets,
            parent=self.window,
            log=self.log_view.append,
            action_label="Verifying an Auto-UV profile",
        ):
            return
        duration_s = int(options["duration_s"])
        command = profile_verify_command(
            profile_selector=profile_verify_selector(profile),
            duration_s=duration_s,
            stop_request_path=verify_stop_request_path(),
            gpu_index=int(target_gpu_index),
        )
        workload = workload_label()
        self.header.set_stage("Profile verification")
        self.header.set_candidate(label)
        self.controls.set_status_text(f"Verifying {label} with {workload}.")
        self.tabs.setCurrentIndex(self.auto_uv_tab_index)
        # Verify tests an already-known curve rather than discovering one, so
        # there's no live candidate-curve event stream to plot from — just
        # show the static curve being verified instead of leaving the plot
        # showing whatever it last had.
        base_points = profile_base_curve_points(profile)
        if base_points:
            self.vf_plot.set_source_points(base_points)
        candidate_points = curve_points_from_values(profile_curve_plan(profile))
        if candidate_points:
            self.vf_plot.set_candidate_points(candidate_points, remember_previous=False)
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
        selected_paths = self.profile_list.selected_profile_paths()
        if not selected_paths:
            return
        target_gpu_uuid = self.profile_list.target_gpu_uuid()
        autostart_info = systemd_autostart_profile_info(gpu_uuid=target_gpu_uuid)
        autostart_action = profile_delete_autostart_action(
            self.profile_summaries,
            list(selected_ids),
            autostart_info,
            include_legacy_profiles=self.profile_list.single_physical_gpu(),
        )
        # Session-only applies (Apply-on-startup unticked) leave no boot
        # entry, so also check the actively running profile — deleting it
        # must restore stock instead of leaving an orphaned curve applied.
        running_action = {"action": "keep"}
        if penguin_burner_runtime_is_active():
            running_action = profile_delete_autostart_action(
                self.profile_summaries,
                list(selected_ids),
                running_auto_uv_profile_info(),
                include_legacy_profiles=self.profile_list.single_physical_gpu(),
            )
        restore_stock = "restore-stock" in (
            autostart_action.get("action"),
            running_action.get("action"),
        )
        if not self._confirm_profile_delete(
            restore_stock=restore_stock,
            removes_last_usable_adaptive_profile=(
                "last-usable-adaptive-profile"
                in (
                    autostart_action.get("reason"),
                    running_action.get("reason"),
                )
            ),
        ):
            return
        try:
            deleted = delete_auto_uv_profile_paths(selected_paths) if selected_paths else []
        except PermissionError:
            self._run_privileged_profile_delete(
                selected_paths,
                selected_ids,
                restore_stock=restore_stock,
            )
            return
        count = len(deleted)
        label = "profile" if count == 1 else "profiles"
        self.log_view.append(f"\nDeleted {count} saved {label}.\n")
        self.controls.set_status_text(f"Deleted {count} saved {label}.")
        self._load_profiles()
        self._run_delete_autostart_followup(restore_stock=restore_stock)

    def _run_privileged_profile_delete(
        self,
        selected_paths: list[str],
        selected_ids: set[str],
        *,
        restore_stock: bool,
    ) -> None:
        if not ensure_daemon_ready_for_privileged_action(
            QtWidgets=self.QtWidgets,
            parent=self.window,
            log=self.log_view.append,
            action_label="Deleting saved Auto-UV profiles",
        ):
            return
        self._delete_restore_stock = bool(restore_stock)
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
                if self._run_delete_autostart_followup(
                    restore_stock=self._delete_restore_stock,
                ):
                    return
            else:
                self.controls.set_status_text("Auto-UV profile deletion failed.")
                self.errors.show_process(
                    title="Profile deletion failed",
                    action_label="Delete selected profiles",
                    exit_code=exit_code,
                    exit_status=exit_status,
                )
            self._delete_restore_stock = False
            self._load_profiles()
            return
        if kind == "restore-keep-stock" and success:
            # Daemon now runs the stock runtime (GPU at factory, no undervolt);
            # reflect that as Default in the status line. Live daemon detection
            # (running selector == stock sentinel) also drives this.
            self._defaults_restored = True
            target_gpu_uuid = self.profile_list.target_gpu_uuid()
            if target_gpu_uuid:
                self._boot_apply_by_gpu[target_gpu_uuid.casefold()] = True
            self.profile_list.set_boot_apply_checked(True)
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
        restore_stock: bool,
        removes_last_usable_adaptive_profile: bool = False,
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
                restores_stock=restore_stock,
                removes_last_usable_adaptive_profile=(
                    removes_last_usable_adaptive_profile
                ),
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
        # Right-clicking inside a batch must not collapse it to one profile.
        selection = table.selectionModel()
        if selection is None:
            return
        if not selection.isRowSelected(index.row(), index.parent()):
            flags = self.QtCore.QItemSelectionModel.SelectionFlag
            selection.select(index, flags.ClearAndSelect | flags.Rows)
            selection.setCurrentIndex(index, flags.NoUpdate)
        selected_count = len(self.profile_list.selected_profile_ids())
        if selected_count > 1:
            menu = self.QtWidgets.QMenu(table)
            delete_action = menu.addAction(f"Delete {selected_count} selected profiles")
            delete_action.setEnabled(
                not self._workflow_running() and self.profile_list.delete_button.isEnabled()
            )
            chosen = menu.exec(table.viewport().mapToGlobal(position))
            if chosen == delete_action:
                self._delete_selected_profiles()
            return
        profile = self._profile_from_row(int(index.row()))
        if profile is None:
            return
        menu = self.QtWidgets.QMenu(table)
        edit_curve_action = menu.addAction("Edit VF Curve")
        edit_curve_action.setEnabled(bool(profile_curve_plan(profile)))
        fan_action = menu.addAction("Edit Fan Curve")
        fan_action.setEnabled(bool(profile_fan_curve_points(profile)))
        memory_offset_action = menu.addAction("Edit Memory Offset")
        memory_offset_action.setEnabled(
            editable_memory_offset_from_profile(profile) is not None
        )
        apply_action = menu.addAction("Apply")
        apply_action.setEnabled(
            not self._workflow_running()
            and profile_can_apply(profile)
            and self.profile_list.profile_matches_target(profile)
        )
        verify_action = menu.addAction("Verify")
        verify_action.setEnabled(
            not self._workflow_running() and profile_can_verify(profile)
        )
        export_action = menu.addAction("Export LACT")
        export_action.setEnabled(not self._workflow_running() and profile_can_apply(profile))
        tier_menu = menu.addMenu("Assign Tier")
        tier_actions = {
            "efficiency": tier_menu.addAction("Efficiency"),
            "balanced": tier_menu.addAction("Balanced"),
            "performance": tier_menu.addAction("Performance"),
        }
        tier_menu.addSeparator()
        none_tier_action = tier_menu.addAction("None")
        can_assign_tier = (
            not self._workflow_running()
            and profile_can_apply(profile)
            and bool(str(profile.get("profile_id") or "").strip())
        )
        tier_menu.setEnabled(can_assign_tier)
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
        elif chosen == memory_offset_action:
            self._edit_profile_memory_offset(profile)
        elif chosen == apply_action:
            self._run_runtime_action("daemonize")
        elif chosen == verify_action:
            self._verify_profile(profile)
        elif chosen == export_action:
            self._export_lact_profile(profile)
        elif chosen in set(tier_actions.values()):
            profile_id = str(profile.get("profile_id") or "").strip()
            for tier, action in tier_actions.items():
                if chosen == action:
                    save_profile_tier_assignment(
                        profile_id,
                        tier,
                        gpu_uuid=profile_gpu_uuid(profile),
                    )
                    self._load_profiles()
                    break
        elif chosen == none_tier_action:
            save_profile_tier_none_assignment(
                str(profile.get("profile_id") or ""),
                gpu_uuid=profile_gpu_uuid(profile),
            )
            self._load_profiles()
        elif chosen == delete_action:
            self._delete_selected_profiles()

    def _profile_from_row(self, row: int) -> dict | None:
        item = self.profile_list.table.item(int(row), 0)
        if item is None:
            return None
        profile_id = str(item.data(self.profile_list.PROFILE_ID_ROLE) or "")
        return profile_for_selector(self.profile_summaries, profile_id)

    def _runtime_action_start_text(self, *, adaptive_auto_uv: bool = False) -> str:
        autostart = "Yes" if self.profile_list.persist_on_startup_enabled() else "No"
        if adaptive_auto_uv:
            return f"Starting adaptive Auto-UV; Autostart: {autostart}."
        selected = self.profile_list.selected_profile_name() or "none"
        return f"Starting profile: {selected}; Autostart: {autostart}."

    def _confirm_runtime_gpu_switch(self, target_gpu_uuid: str) -> bool:
        running_info = running_auto_uv_profile_info()
        running_uuid = str(running_info.get("gpu_uuid") or "").strip()
        selected_uuid = str(target_gpu_uuid or "").strip()
        if not running_uuid or not selected_uuid or running_uuid.casefold() == selected_uuid.casefold():
            return True
        buttons = (
            self.QtWidgets.QMessageBox.StandardButton.Yes
            | self.QtWidgets.QMessageBox.StandardButton.No
        )
        answer = self.QtWidgets.QMessageBox.question(
            self.window,
            "Switch managed GPU",
            "Another GPU is currently managed. Switching stops monitoring that GPU. "
            "Its applied curve, memory offset, or power limit may remain until you "
            "restore it or the driver resets it.\n\nSwitch to the selected GPU?",
            buttons,
            self.QtWidgets.QMessageBox.StandardButton.No,
        )
        return answer == self.QtWidgets.QMessageBox.StandardButton.Yes


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
            value = round(float(raw_value))
        except (TypeError, ValueError):
            continue
        if value in seen:
            continue
        seen.add(value)
        values.append(value)
    return tuple(values)


def _runtime_action_label(action: str) -> str:
    labels = {
        "daemonize": "Apply selected profile",
        "adaptive-daemonize": "Apply adaptive Auto-UV",
        "delete-profiles": "Delete selected profiles",
        "restore-defaults": "Restore GPU defaults",
        "restore-keep-stock": "Keep GPU at stock",
    }
    return labels.get(str(action), str(action) or "Profile action")
