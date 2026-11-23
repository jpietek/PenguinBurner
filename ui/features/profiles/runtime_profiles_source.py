"""The Profiles page's data source over the saved-profile store and the
root daemon: the classic Profiles tab's reads and writes, call for call.

Reads come from one snapshot taken by ``refresh()`` (saved profiles, GPUs,
what runs now and at boot), so rendering never blocks on the daemon; a
2 s poll re-reads what runs now off the GUI thread, as the classic window's
status poll does. Writes follow an explicit user action: privileged ones go
to the root daemon through ``runtime_profile_command`` /
``delete_profiles_command`` run by a QProcess, the rest write the user's
own profile and config files.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar, cast

from PySide6 import QtCore as C

from cli.runtime_config_file import (
    persist_on_startup_to_runtime_config,
    silent_fan_curve_from_runtime_config,
    silent_fan_curve_to_runtime_config,
)
from common.penguin_burner_paths import default_user_config_dir
from curve_editors.uv.vf_curve_manual_editor import editable_anchor_from_profile
from integrations.afterburner.profile_import import (
    persist_afterburner_import_selection,
)
from integrations.lact.export import LactExportRefused, export_profile_to_lact
from profiles.gpu_identity import profile_gpu_uuid
from profiles.uv.memory_offset_edit import (
    editable_memory_offset_from_profile,
    save_edited_memory_offset_profile,
)
from profiles.uv.profile_binding import bind_legacy_profile_to_gpu
from profiles.uv.profile_fan_curve import (
    fan_curve_points_from_payload,
    fan_curve_target_point_from_payload,
    fan_measurement_points_from_payload,
    fan_payload_has_silent_runtime_fields,
    profile_fan_payload,
    save_edited_fan_profile,
    sync_profile_fan_payload,
)
from profiles.uv.profile_rules import (
    NO_SILENT_FAN_CURVE,
    defaults_restored_after_status,
    legacy_profile_needs_binding,
    profile_for_selector,
    silent_fan_checked_on_load,
)
from profiles.uv.profile_store import (
    STOCK_PROFILE_SELECTOR,
    delete_auto_uv_profile_paths,
    profile_id_from_archive_path,
    read_auto_uv_profile_summaries,
)
from profiles.uv.profile_tiers import (
    PROFILE_TIERS,
    load_profile_tier_assignments,
    load_profile_tier_disabled_profile_ids,
    save_profile_tier_assignment,
    save_profile_tier_none_assignment,
)
from profiles.uv.profile_vf_curve import (
    load_cached_base_curve_points,
    profile_base_curve_points,
    profile_curve_plan,
    save_edited_curve_profile,
)
from runtime.daemon_client import clear_boot_runtime_spec, set_boot_main_gpu
from runtime.daemon_commands import delete_profiles_command, runtime_profile_command
from runtime.profile_status import live_profile_status
from ui.features.profiles.command_runner import ProfileCommandRunner
from ui.features.profiles.profile_model import (
    BootEntry,
    CommandResult,
    Done,
    GpuTarget,
    LactExport,
    RuntimeState,
)
from ui.features.tuning.gpu_selection import (
    GpuChoice,
    detected_gpu_choices,
    persist_runtime_gpu_index,
)
from ui.features.tuning.tuning import memory_offset_mhz_range

T = TypeVar("T")
STATUS_POLL_MS = 2000
STATUS_COLLECT_MS = 120


def gpu_target(choice: GpuChoice) -> GpuTarget:
    return GpuTarget(
        index=int(choice.index),
        uuid=str(choice.uuid or "").strip(),
        name=str(choice.name or "").strip() or "NVIDIA GPU",
        pci_bus_id=str(choice.pci_bus_id or ""),
        factory_power_w=choice.power_limit_default_w,
    )


def boot_entry_from_info(info: dict[str, object] | None) -> BootEntry | None:
    """A daemon boot entry (runtime.profile_status info), or None when the
    GPU has nothing saved for boot."""
    selector = str((info or {}).get("selector") or "").strip()
    if not info or not selector:
        return None
    return BootEntry(
        selector=selector,
        silent_fan=bool(info.get("silent_fan_curve")),
        adaptive=bool(info.get("adaptive_auto_uv")),
        main_gpu=bool(info.get("main_gpu")),
    )


def runtime_state_from_info(
    info: dict[str, object], *, defaults_restored: bool
) -> RuntimeState:
    return RuntimeState(
        running_selector=str(info.get("selector") or "").strip(),
        running_gpu_uuid=str(info.get("gpu_uuid") or "").strip(),
        running_adaptive=bool(info.get("adaptive_auto_uv")),
        running_silent_fan=bool(info.get("silent_fan_curve")),
        game_override=bool(info.get("game_override")),
        standing_selector=str(info.get("standing_selector") or ""),
        standing_adaptive=bool(info.get("standing_adaptive")),
        defaults_restored=bool(defaults_restored),
    )


class RuntimeProfilesSource(C.QObject):
    """ProfilesSource over the profile store and the root daemon."""

    def __init__(
        self,
        *,
        parent: C.QObject | None = None,
        ensure_daemon_ready: Callable[[str], bool] | None = None,
        poll_ms: int | None = None,
        runner: ProfileCommandRunner | None = None,
    ):
        super().__init__(parent)
        self.ensure_daemon_ready = ensure_daemon_ready or (lambda _label: True)
        self.runner = runner or ProfileCommandRunner(self)
        self._listeners: list[Callable[[], None]] = []
        # True once stock was restored and nothing has been applied since:
        # the status reads "Default" (the classic window's _defaults_restored).
        self._defaults_restored = False
        self._profiles: list[dict] = []
        self._gpus: list[GpuTarget] = []
        self._running: dict[str, object] = {}
        self._boot: dict[str, dict[str, object]] = {}
        self._cache: dict[tuple[str, str], object] = {}
        self._poll_thread: threading.Thread | None = None
        self._poll_result: tuple[dict, dict] | None = None
        self.refresh()
        poll_ms = STATUS_POLL_MS if poll_ms is None else int(poll_ms)
        self._timer = C.QTimer(self)
        self._timer.setInterval(max(0, poll_ms))
        self._timer.timeout.connect(self.poll_status)
        if poll_ms > 0:
            self._timer.start()

    # ---------------- snapshot ----------------

    def refresh(self) -> None:
        """Re-read saved profiles, GPUs and the daemon's state (the classic
        window's _load_profiles reads); listeners are not called."""
        self._profiles = list(read_auto_uv_profile_summaries())
        self._gpus = [gpu_target(choice) for choice in detected_gpu_choices()]
        self._cache.clear()
        self._set_status(*live_profile_status(self._gpu_uuids()))

    def _gpu_uuids(self) -> list[str]:
        return [gpu.uuid for gpu in self._gpus if gpu.uuid]

    def _set_status(self, running: dict, boot: dict[str, dict]) -> None:
        self._running = dict(running)
        self._boot = {str(k).casefold(): dict(v) for k, v in boot.items()}
        self._defaults_restored = defaults_restored_after_status(
            self._defaults_restored, str(self._running.get("selector") or "")
        )

    def changed(self) -> None:
        for callback in list(self._listeners):
            callback()

    def _reload_and_notify(self) -> None:
        self.refresh()
        self.changed()

    def poll_status(self) -> None:
        """Gather what runs now off the GUI thread, then apply it here. Skipped
        while a command owns the state, as the classic poll is."""
        if self.runner.is_running():
            return
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return
        uuids = self._gpu_uuids()
        self._poll_result = None

        def run() -> None:
            self._poll_result = live_profile_status(uuids)

        self._poll_thread = threading.Thread(target=run, daemon=True)
        self._poll_thread.start()
        C.QTimer.singleShot(STATUS_COLLECT_MS, self._collect_status)

    def _collect_status(self) -> None:
        thread = self._poll_thread
        if thread is not None and thread.is_alive():
            C.QTimer.singleShot(STATUS_COLLECT_MS, self._collect_status)
            return
        result, self._poll_result = self._poll_result, None
        if result is None or self.runner.is_running():
            return
        before = (self._running, self._boot, self._defaults_restored)
        self._set_status(*result)
        if (self._running, self._boot, self._defaults_restored) != before:
            self.changed()

    # ---------------- reads ----------------

    def subscribe(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def profiles(self) -> list[dict]:
        return list(self._profiles)

    def gpus(self) -> list[GpuTarget]:
        return list(self._gpus)

    def _profile(self, profile_id: str) -> dict | None:
        return profile_for_selector(self._profiles, profile_id) if profile_id else None

    def _gpu(self, gpu_uuid: str) -> GpuTarget | None:
        key = str(gpu_uuid or "").casefold()
        return next((g for g in self._gpus if g.uuid.casefold() == key), None)

    def _cached(self, kind: str, key: str, read: Callable[[], T]) -> T:
        """One read per profile store refresh (rendering asks often)."""
        if (kind, key) not in self._cache:
            self._cache[(kind, key)] = read()
        return cast(T, self._cache[(kind, key)])

    def tier_assignments(self, gpu_uuid: str) -> dict[str, str]:
        return dict(
            self._cached(
                "tiers", gpu_uuid, lambda: load_profile_tier_assignments(gpu_uuid=gpu_uuid)
            )
        )

    def tier_disabled(self, gpu_uuid: str) -> set[str]:
        return set(
            self._cached(
                "tiers-off",
                gpu_uuid,
                lambda: load_profile_tier_disabled_profile_ids(gpu_uuid=gpu_uuid),
            )
        )

    def runtime(self) -> RuntimeState:
        return runtime_state_from_info(
            self._running, defaults_restored=self._defaults_restored
        )

    def boot_entry(self, gpu_uuid: str) -> BootEntry | None:
        return boot_entry_from_info(self._boot.get(str(gpu_uuid or "").casefold()))

    def silent_fan_preference(self) -> bool:
        """The sticky load rule: the saved choice, or a running or boot
        profile that has the fan curve on."""
        return silent_fan_checked_on_load(
            silent_fan_curve_from_runtime_config(),
            self._running,
            self._boot.get("", {}),
        )

    def curve_plan(self, profile_id: str) -> list[dict]:
        profile = self._profile(profile_id)
        if profile is None:
            return []
        return list(self._cached("plan", profile_id, lambda: profile_curve_plan(profile)))

    def base_curve_points(self, profile_id: str) -> list[tuple[float, float]]:
        profile = self._profile(profile_id)
        points = [] if profile is None else profile_base_curve_points(profile)
        # The classic editor falls back to the last scan's cached stock curve.
        return points or load_cached_base_curve_points()

    def _fan_payload(self, profile_id: str) -> dict | None:
        profile = self._profile(profile_id)
        if profile is None:
            return None
        return self._cached("fan", profile_id, lambda: profile_fan_payload(profile))

    def fan_curve(self, profile_id: str) -> list[tuple[float, float]]:
        payload = self._fan_payload(profile_id)
        return [] if payload is None else fan_curve_points_from_payload(payload)

    def fan_measurements(self, profile_id: str) -> list[tuple[float, float]]:
        payload = self._fan_payload(profile_id)
        return [] if payload is None else fan_measurement_points_from_payload(payload)

    def fan_target_point(self, profile_id: str) -> tuple[float, float] | None:
        payload = self._fan_payload(profile_id)
        return None if payload is None else fan_curve_target_point_from_payload(payload)

    def fan_runtime_ready(self, profile_id: str) -> bool:
        payload = self._fan_payload(profile_id)
        return payload is not None and fan_payload_has_silent_runtime_fields(payload)

    def memory_offset_range(self, gpu_uuid: str) -> tuple[int, int]:
        """Read when the editor opens, from the GPU's driver range."""
        gpu = self._gpu(gpu_uuid)
        return memory_offset_mhz_range(gpu_index=None if gpu is None else gpu.index)

    def lact_directories(self) -> list[str]:
        return [str(default_user_config_dir())]

    # ---------------- preferences ----------------

    def set_silent_fan_preference(self, enabled: bool) -> None:
        silent_fan_curve_to_runtime_config(bool(enabled))

    def remember_apply_on_startup(self, enabled: bool) -> None:
        # Records the user's preference, not what boot will do -- the daemon's
        # boot set answers that. Kept because it is a documented setting.
        persist_on_startup_to_runtime_config(bool(enabled))

    def remember_target_gpu(self, gpu_index: int) -> None:
        persist_runtime_gpu_index(int(gpu_index))

    # ---------------- root-service commands ----------------

    def _run(
        self,
        label: str,
        kind: str,
        command: list[str],
        done: Done,
        *,
        fail_text: str,
        success_message: str = "",
        profile_id: str = "",
        on_success: Callable[[], None] | None = None,
    ) -> None:
        def finished(result: CommandResult) -> None:
            if result.ok and on_success is not None:
                on_success()
            self._reload_and_notify()
            if result.ok and profile_id:
                result = CommandResult(True, result.message, profile_id)
            done(result)

        self.runner.start(
            label,
            kind,
            command,
            finished,
            fail_text=fail_text,
            success_message=success_message,
        )

    def apply_profile(
        self,
        profile_id: str,
        gpu_uuid: str,
        *,
        silent_fan: bool,
        persist_on_startup: bool,
        done: Done,
    ) -> None:
        """Apply through the root daemon: --boot saves it for boot, --clear-boot
        makes it session-only (profile_actions._run_runtime_action)."""
        profile = self._profile(profile_id)
        gpu = self._gpu(gpu_uuid)
        if profile is None or gpu is None:
            done(CommandResult(False, "The profile or its GPU is no longer available."))
            return
        # The runtime service reads this shared artifact when the silent fan
        # curve is on; the write is durable.
        if silent_fan and not sync_profile_fan_payload(profile):
            done(CommandResult(False, NO_SILENT_FAN_CURVE))
            return
        if legacy_profile_needs_binding(profile, gpu.uuid):
            try:
                bind_legacy_profile_to_gpu(profile, gpu.index)
            except Exception as exc:  # noqa: BLE001 - reported, nothing applied
                done(
                    CommandResult(
                        False,
                        f"Could not bind this legacy profile to the selected GPU: {exc}",
                    )
                )
                return
        command = runtime_profile_command(
            "daemonize",
            profile_selector=profile_id,
            silent_fan_curve=bool(silent_fan),
            gpu_index=gpu.index,
            persist_on_startup=bool(persist_on_startup),
        )
        silent_fan_curve_to_runtime_config(bool(silent_fan))
        # A profile is now in effect, so the GPU is no longer at stock.
        self._defaults_restored = False
        self._run(
            "Apply selected profile",
            "daemonize",
            command,
            done,
            fail_text="Failed to start runtime profile action.",
            profile_id=profile_id,
        )

    def restore_defaults(self, gpu_uuid: str, *, silent_fan: bool, done: Done) -> None:
        """The daemon runs the reserved stock runtime: factory GPU settings,
        no undervolt, fan control kept. Stock is the safe state, so it is
        saved for boot regardless of Apply on startup."""
        gpu = self._gpu(gpu_uuid)
        if gpu is None:
            done(CommandResult(False, "Choose a target GPU before restoring defaults."))
            return
        command = runtime_profile_command(
            "daemonize",
            profile_selector=STOCK_PROFILE_SELECTOR,
            silent_fan_curve=bool(silent_fan),
            gpu_index=gpu.index,
            persist_on_startup=True,
        )

        def restored() -> None:
            self._defaults_restored = True

        self._run(
            "Restore defaults",
            "restore-keep-stock",
            command,
            done,
            fail_text="Failed to restore GPU to stock.",
            on_success=restored,
        )

    def delete_profiles(self, profile_ids: list[str], done: Done) -> None:
        """Delete the user's own files directly; files the user cannot remove
        go through the root daemon, which path-validates them."""
        paths = [
            str(profile.get("path") or "").strip()
            for profile in (self._profile(pid) for pid in profile_ids)
            if profile is not None and str(profile.get("path") or "").strip()
        ]
        try:
            deleted = delete_auto_uv_profile_paths(paths) if paths else []
        except PermissionError:
            if not self.ensure_daemon_ready("Deleting saved Auto-UV profiles"):
                done(CommandResult(False, cancelled=True))
                return
            self._run(
                "Delete selected profiles",
                "delete-profiles",
                delete_profiles_command(paths),
                done,
                fail_text="Failed to start Auto-UV profile delete action.",
                success_message="Selected Auto-UV profiles deleted.",
            )
            return
        except Exception as exc:  # noqa: BLE001 - reported to the user
            self._reload_and_notify()
            done(CommandResult(False, str(exc) or type(exc).__name__))
            return
        count = len(deleted)
        label = "profile" if count == 1 else "profiles"
        self._reload_and_notify()
        done(CommandResult(True, f"Deleted {count} saved {label}."))

    def clear_boot(self, gpu_uuid: str) -> None:
        """Nothing applies at boot on this GPU. Raises when the daemon refuses,
        and only then is the documented preference left alone."""
        clear_boot_runtime_spec(gpu_uuid=gpu_uuid)
        persist_on_startup_to_runtime_config(False)
        self._reload_and_notify()

    def set_main_gpu(self, gpu_uuid: str) -> None:
        set_boot_main_gpu(gpu_uuid)
        self._reload_and_notify()

    # ---------------- profile store ----------------

    def assign_tier(self, profile_id: str, tier: str) -> None:
        profile = self._profile(profile_id)
        if profile is None:
            return
        gpu_uuid = profile_gpu_uuid(profile)
        if tier in PROFILE_TIERS:
            save_profile_tier_assignment(profile_id, tier, gpu_uuid=gpu_uuid)
        else:
            save_profile_tier_none_assignment(profile_id, gpu_uuid=gpu_uuid)
        self._reload_and_notify()

    def _require_profile(self, profile_id: str) -> dict:
        profile = self._profile(profile_id)
        if profile is None:
            raise FileNotFoundError(f"Auto-UV profile not found: {profile_id}")
        return profile

    def _saved(self, path: Path) -> str:
        self._reload_and_notify()
        return profile_id_from_archive_path(path)

    def save_curve_edit(self, profile_id: str, edit) -> str:
        profile = self._require_profile(profile_id)
        anchor = editable_anchor_from_profile(profile)
        if anchor is None:
            raise ValueError("No editable V/F curve is available for this profile.")
        path, _payload = save_edited_curve_profile(
            profile,
            edit,
            original_anchor_voltage_mv=int(anchor[0]),
            original_anchor_clock_mhz=int(anchor[1]),
        )
        return self._saved(path)

    def save_fan_edit(
        self, profile_id: str, edit, original_points: list[tuple[float, float]]
    ) -> str:
        profile = self._require_profile(profile_id)
        path, _payload = save_edited_fan_profile(
            profile, edit, original_points=original_points
        )
        return self._saved(path)

    def save_memory_offset(self, profile_id: str, memory_offset: int) -> str:
        profile = self._require_profile(profile_id)
        path, _payload = save_edited_memory_offset_profile(
            profile,
            int(memory_offset),
            original_memory_offset_mhz=editable_memory_offset_from_profile(profile),
        )
        return self._saved(path)

    def export_lact(
        self, profile_id: str, directory: str, *, include_fan_curve: bool
    ) -> LactExport:
        profile = self._profile(profile_id)
        if profile is None:
            return LactExport(error="The profile is no longer available.")
        try:
            written, warnings = export_profile_to_lact(
                profile, directory, include_fan_curve=include_fan_curve
            )
        except LactExportRefused as exc:
            return LactExport(error=str(exc))
        except Exception as exc:  # noqa: BLE001 - reported to the user
            return LactExport(error=f"LACT export failed:\n{exc}")
        try:
            text = Path(written).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        return LactExport(
            path=str(written), warnings=tuple(str(w) for w in warnings), text=text
        )

    def finish_verification(self, profile_id: str, gpu_uuid: str, passed: bool) -> None:
        """The daemon's verification already marked (and bound) the profile
        file; re-read it."""
        del profile_id, gpu_uuid, passed
        self._reload_and_notify()

    def save_afterburner_import(self, entry: dict, fan_payload: dict | None) -> dict:
        """Persist the chosen Afterburner slot; the import reads the export's
        fan curve itself, so ``fan_payload`` (the preview's) is not used."""
        del fan_payload
        result = persist_afterburner_import_selection(entry)
        self._reload_and_notify()
        return result
