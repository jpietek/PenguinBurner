"""Sample ProfilesSource: saved profiles seeded from the preview's sample.json.

Every write stays in this object. Payloads are built with the real
profile-store helpers (profile_summary, the user-edited payload builders,
the LACT renderer), so the page sees exactly the shapes the real store
returns; only the disk and the root daemon are replaced.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path

import numpy as np
from PySide6 import QtCore as C

from curve_editors.fan.fan_curve_manual_editor import (
    user_edited_fan_curve_profile_payload,
)
from curve_editors.uv.vf_curve_manual_editor import user_edited_profile_payload
from integrations.lact.export import (
    LactExportError,
    _auto_uv_fan_config,
    build_lact_nvidia_config_from_plan,
)
from profiles.gpu_identity import normalized_gpu_identity, profile_gpu_uuid
from profiles.uv.memory_offset_edit import (
    editable_memory_offset_from_profile,
    user_edited_memory_offset_profile_payload,
)
from profiles.uv.profile_store import profile_summary
from profiles.uv.profile_tiers import PROFILE_TIERS
from qt.components.profile_model import (
    STOCK,
    BootEntry,
    CommandResult,
    Done,
    GpuTarget,
    LactExport,
    RuntimeState,
)
from ui.features.curves.curve_profiles import (
    base_curve_points_from_payload,
    curve_plan_from_payload,
)
from ui.features.curves.fan_profiles import (
    embedded_fan_payload,
    fan_curve_points_from_payload,
    fan_curve_target_point_from_payload,
    fan_measurement_points_from_payload,
    fan_payload_has_silent_runtime_fields,
)
from ui.features.integrations.afterburner_import import (
    entry_curve_points,
    relative_profile_path,
)

SAMPLE_PATH = Path(__file__).resolve().parents[2] / "sample.json"
PROFILE_DIR = "~/.config/PenguinBurner/auto-uv-profiles"
SAMPLE_GPUS = (
    GpuTarget(
        index=0,
        uuid="GPU-5a0c7f1e-5080-4c1d-9e2b-0000sample0",
        name="NVIDIA GeForce RTX 5080",
        pci_bus_id="00000000:01:00.0",
        factory_power_w=360.0,
        memory_offset_range=(0, 6000),
    ),
    GpuTarget(
        index=1,
        uuid="GPU-9b3e2d40-4070-4f6a-8c11-0000sample1",
        name="NVIDIA GeForce RTX 4070",
        pci_bus_id="00000000:02:00.0",
        factory_power_w=200.0,
        memory_offset_range=(0, 3000),
    ),
)
# The example scan's stock run is the baseline of the GPU 0 profiles.
GPU0_BASE = dict(mv=1000, clock=2734.0, fps=63.4, power=341.0)
DEFAULT_FAN_CURVE = (
    (30, 30),
    (45, 35),
    (55, 42),
    (65, 51),
    (75, 66),
    (85, 85),
    (90, 100),
)
# GPU id the sample pretends LACT wrote into each suggested directory.
LACT_GPU_IDS = {"/etc/lact": "10DE:2C02-1462:5311-0000:01:00.0"}


def _identity(gpu: GpuTarget) -> dict:
    return normalized_gpu_identity(
        {"name": gpu.name, "uuid": gpu.uuid, "pci_bus_id": gpu.pci_bus_id},
        index_at_verification=gpu.index,
    )


def curve_plan(points, base_points) -> list[dict]:
    """A V/F plan (index, voltage, base and target clock) for curve points."""
    base_v = [float(v) for v, _ in base_points]
    base_c = [float(c) for _, c in base_points]

    def base_at(mv: float) -> float:
        # Linear between the stock points, flat past either end (np.interp).
        return float(np.interp(mv, base_v, base_c))

    plan = []
    for index, (mv, mhz) in enumerate(points):
        base = round(base_at(float(mv)))
        plan.append(
            {
                "index": index,
                "voltage_mv": round(float(mv)),
                "base_mhz": base,
                "target_mhz": round(float(mhz)),
                "new_offset_mhz": round(float(mhz)) - base,
            }
        )
    return plan


def silent_fan_payload(curve, *, temp: float, fan: float, mv: int, clock: int) -> dict:
    """An Auto-UV silent fan payload with the scan's measured readings."""

    def fan_at(t: float) -> float:
        pts = list(curve)
        for (t0, s0), (t1, s1) in pairwise(pts):
            if t <= t1:
                return s0 + (s1 - s0) * (max(t, t0) - t0) / max(1, t1 - t0)
        return pts[-1][1]

    measured = []
    for n in range(28):
        t = temp - 14 + n * 0.75 + math.sin(n * 2.3) * 1.2
        measured.append(
            {
                "temperature_c": round(t, 1),
                "fan_speed_pct": round(fan_at(t) + math.sin(n * 1.7) * 2.4, 1),
                "voltage_mv": mv,
                "clock_mhz": clock,
            }
        )
    return {
        "fan": {"mode": "curve", "curve": [[t, s] for t, s in curve]},
        "loaded_temperature_c": round(temp, 1),
        "observed_fan_speed_pct": round(fan, 1),
        "telemetry": {"measured_fan_points": measured},
    }


class SampleProfilesSource:
    """In-memory profile store and simulated root daemon for the preview."""

    def __init__(self, *, gpu_count: int = 1, command_delay_ms: int = 700, sample=None):
        self.sample = (
            sample if sample is not None else json.loads(SAMPLE_PATH.read_text())
        )
        self.gpu_count = max(0, min(len(SAMPLE_GPUS), int(gpu_count)))
        # Prototype controls: the next Apply, Restore or Delete fails.
        self.fail_next_command = False
        self.failure_consumed: Callable[[], None] | None = None
        self.command_delay_ms = int(command_delay_ms)
        self.daemon_available = True
        self.payloads: dict[str, dict] = {}
        self.assignments: dict[str, dict[str, str]] = {}
        self.disabled: dict[str, set[str]] = {}
        self.boot: dict[str, BootEntry] = {}
        self.main_gpu = ""
        self.state = RuntimeState()
        self.silent_fan = False
        self._listeners: list[Callable[[], None]] = []
        self._clock = datetime(2026, 7, 14, 9, 0)
        self._seed()

    # ---------------- seed ----------------

    def _seed(self) -> None:
        rows = self.sample["rows"]
        base = self.sample["base"]
        gpu0, gpu1 = (_identity(g) for g in SAMPLE_GPUS)

        def final_row(tier):
            return [r for r in rows if r["tier"] == tier and r["passed"]][-1]

        def add(
            pid,
            row,
            *,
            created,
            mode,
            identity,
            memory,
            base_metrics,
            fan=True,
            name="",
            mv=None,
            target=None,
            fps=None,
            power=None,
            meas=None,
        ):
            mv = mv if mv is not None else row["mv"]
            target = target if target is not None else row["target"]
            fps = fps if fps is not None else row["fps"]
            power = power if power is not None else row["power"]
            points = row["points"]
            payload = {
                "profile_id": pid,
                "candidate_id": f"{mode}-{mv}mv-{target}mhz",
                "display_name": name,
                "profile_created_at": created.isoformat(),
                "verified_at": created.isoformat(),
                "profile_source": "auto-uv-final",
                "path": f"{PROFILE_DIR}/auto-uv-profile-{pid}.json",
                "auto_uv_requested_mode": mode,
                "candidate_voltage_mv": mv,
                "lock_clock_mhz": target,
                "memory_offset_mhz": memory,
                "avg_core_clock_mhz": meas if meas is not None else row["meas"],
                "avg_fps": fps,
                "avg_power_w": power,
                "efficiency_fps_per_w": round(fps / power, 4),
                "base_candidate_voltage_mv": base_metrics["mv"],
                "base_avg_core_clock_mhz": base_metrics["clock"],
                "base_avg_fps": base_metrics["fps"],
                "base_avg_power_w": base_metrics["power"],
                "base_efficiency_fps_per_w": round(
                    base_metrics["fps"] / base_metrics["power"], 4
                ),
                "final_verified": True,
                "verification_status": "verified",
                "plan": curve_plan(points, base),
                "gpu_identity": identity,
            }
            if fan:
                payload["fan_curve_payload"] = silent_fan_payload(
                    DEFAULT_FAN_CURVE,
                    temp=row["temp"],
                    fan=row["fan"],
                    mv=mv,
                    clock=target,
                )
            self.payloads[pid] = payload

        add(
            "20260713-222800-efficiency",
            final_row("Efficiency"),
            created=datetime(2026, 7, 13, 22, 28),
            mode="efficiency",
            identity=gpu0,
            memory=0,
            base_metrics=GPU0_BASE,
            name="Efficiency",
        )
        add(
            "20260713-223600-balanced",
            final_row("Balanced"),
            created=datetime(2026, 7, 13, 22, 36),
            mode="balanced",
            identity=gpu0,
            memory=3000,
            base_metrics=GPU0_BASE,
            name="Balanced",
        )
        add(
            "20260714-000000-performance",
            final_row("Performance"),
            created=datetime(2026, 7, 14, 0, 0),
            mode="performance",
            identity=gpu0,
            memory=3000,
            base_metrics=GPU0_BASE,
            name="Performance",
        )
        # Bound to the second GPU: shown when the sample has two GPUs.
        add(
            "20260702-191200-balanced-4070",
            final_row("Balanced"),
            created=datetime(2026, 7, 2, 19, 12),
            mode="balanced",
            identity=gpu1,
            memory=2000,
            name="Balanced",
            mv=875,
            target=2550,
            fps=48.2,
            power=168.4,
            meas=2528.0,
            base_metrics=dict(mv=1005, clock=2640.0, fps=51.0, power=197.0),
        )
        # A 0.6 profile without a GPU binding and without a saved fan curve.
        add(
            "20260602-210400-legacy",
            final_row("Balanced"),
            created=datetime(2026, 6, 2, 21, 4),
            mode="balanced",
            identity=None,
            memory=0,
            fan=False,
            mv=880,
            target=2700,
            fps=63.9,
            power=289.6,
            meas=2681.0,
            base_metrics=GPU0_BASE,
        )
        self.payloads["20260602-210400-legacy"].pop("gpu_identity")

    # ---------------- notifications ----------------

    def subscribe(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def changed(self) -> None:
        for callback in list(self._listeners):
            callback()

    def _later(self, work: Callable[[], CommandResult], done: Done) -> None:
        """Run a command after the simulated daemon delay. ``done`` is called
        exactly once, also when the work raises (as a failed result), so the
        page never stays busy."""

        def run():
            if self.fail_next_command:
                # Prototype controls: the root service answers with an error.
                self.fail_next_command = False
                if self.failure_consumed is not None:
                    self.failure_consumed()
                result = CommandResult(
                    False,
                    "penguin-burnerd returned exit status 1: "
                    "NVML: Insufficient Permissions (nvmlDeviceSetGpuLockedClocks)",
                )
            else:
                try:
                    result = work()
                except Exception as exc:  # noqa: BLE001 - a command boundary
                    result = CommandResult(False, str(exc) or type(exc).__name__)
            self.changed()
            done(result)

        if self.command_delay_ms <= 0:
            run()
        else:
            C.QTimer.singleShot(self.command_delay_ms, run)

    def _now(self) -> datetime:
        self._clock += timedelta(minutes=1)
        return self._clock

    def _require_daemon(self) -> None:
        if not self.daemon_available:
            raise RuntimeError("the PenguinBurner root service is not available")

    # ---------------- reads ----------------

    def gpus(self) -> list[GpuTarget]:
        return list(SAMPLE_GPUS[: self.gpu_count])

    def _gpu_known(self, payload: dict) -> bool:
        uuid = profile_gpu_uuid(payload)
        return not uuid or any(g.uuid == uuid for g in self.gpus())

    def profiles(self) -> list[dict]:
        rows = []
        for payload in self.payloads.values():
            if not self._gpu_known(payload):
                continue
            uuid = profile_gpu_uuid(payload)
            # Legacy rows read the global ("") store, as the real summary does.
            assignments = dict(self.assignments.get(uuid, {}))
            summary = profile_summary(
                payload,
                tier_assignments=assignments,
                disabled_profile_tier_ids=set(self.disabled.get(uuid, set())),
            )
            if payload.get("runtime_source"):
                summary["runtime_source"] = payload["runtime_source"]
            rows.append(summary)
        rows.sort(key=lambda p: p["profile_created_at"], reverse=True)
        return rows

    def tier_assignments(self, gpu_uuid: str) -> dict[str, str]:
        return dict(self.assignments.get(gpu_uuid, {}))

    def tier_disabled(self, gpu_uuid: str) -> set[str]:
        return set(self.disabled.get(gpu_uuid, set()))

    def runtime(self) -> RuntimeState:
        return self.state

    def boot_entry(self, gpu_uuid: str) -> BootEntry | None:
        entry = self.boot.get(gpu_uuid)
        if entry is None:
            return None
        return replace(
            entry, main_gpu=bool(self.main_gpu) and self.main_gpu == gpu_uuid
        )

    def silent_fan_preference(self) -> bool:
        return self.silent_fan

    def curve_plan(self, profile_id: str) -> list[dict]:
        return curve_plan_from_payload(self.payloads.get(profile_id, {}))

    def base_curve_points(self, profile_id: str) -> list[tuple[float, float]]:
        return base_curve_points_from_payload(self.payloads.get(profile_id, {}))

    def _fan(self, profile_id: str) -> dict | None:
        return embedded_fan_payload(self.payloads.get(profile_id))

    def fan_curve(self, profile_id: str) -> list[tuple[float, float]]:
        payload = self._fan(profile_id)
        return [] if payload is None else fan_curve_points_from_payload(payload)

    def fan_measurements(self, profile_id: str) -> list[tuple[float, float]]:
        payload = self._fan(profile_id)
        return [] if payload is None else fan_measurement_points_from_payload(payload)

    def fan_target_point(self, profile_id: str) -> tuple[float, float] | None:
        payload = self._fan(profile_id)
        return None if payload is None else fan_curve_target_point_from_payload(payload)

    def fan_runtime_ready(self, profile_id: str) -> bool:
        payload = self._fan(profile_id)
        return payload is not None and fan_payload_has_silent_runtime_fields(payload)

    def lact_directories(self) -> list[str]:
        return ["/etc/lact", "~/.config/PenguinBurner"]

    # ---------------- writes ----------------

    def set_silent_fan_preference(self, enabled: bool) -> None:
        self.silent_fan = bool(enabled)

    def _bind_legacy(self, profile_id: str, gpu_uuid: str) -> None:
        payload = self.payloads.get(profile_id)
        gpu = next((g for g in SAMPLE_GPUS if g.uuid == gpu_uuid), None)
        if payload is not None and gpu is not None and not profile_gpu_uuid(payload):
            # A legacy profile binds to the GPU identity on its first apply.
            payload["gpu_identity"] = _identity(gpu)

    def apply_profile(
        self, profile_id, gpu_uuid, *, silent_fan, persist_on_startup, done
    ):
        def work():
            self._bind_legacy(profile_id, gpu_uuid)
            self.state = RuntimeState(
                running_selector=profile_id,
                running_gpu_uuid=gpu_uuid,
                running_silent_fan=bool(silent_fan),
            )
            # Every apply sends --boot or --clear-boot.
            if persist_on_startup:
                self.boot[gpu_uuid] = BootEntry(profile_id, silent_fan=bool(silent_fan))
            else:
                self.boot.pop(gpu_uuid, None)
            return CommandResult(True, "Apply selected profile complete.", profile_id)

        self._later(work, done)

    def apply_adaptive(
        self, gpu_uuid: str, *, live_tier: str, silent_fan: bool = False
    ) -> None:
        """Adaptive started from the Game Library (prototype hook)."""
        ids = self.tier_assignments(gpu_uuid)
        selector = next(iter(ids.values()), "") or next(iter(self.payloads), "")
        self.state = RuntimeState(
            running_selector=selector,
            running_gpu_uuid=gpu_uuid,
            running_adaptive=True,
            running_silent_fan=silent_fan,
            live_tier=live_tier,
        )
        self.changed()

    def set_game_session(
        self, profile_id: str | None, *, adaptive: bool = True, live_tier: str = ""
    ) -> None:
        """A per-game override from the Game Library while the game runs."""
        current = self.state
        if profile_id is None:
            if current.game_override:
                self.state = RuntimeState(
                    running_selector=current.standing_selector,
                    running_gpu_uuid=current.running_gpu_uuid,
                    running_adaptive=current.standing_adaptive,
                    running_silent_fan=current.running_silent_fan,
                )
        else:
            standing = (
                current.standing_selector
                if current.game_override
                else current.running_selector
            )
            standing_adaptive = (
                current.standing_adaptive
                if current.game_override
                else current.running_adaptive
            )
            self.state = RuntimeState(
                running_selector=profile_id,
                running_gpu_uuid=current.running_gpu_uuid or SAMPLE_GPUS[0].uuid,
                running_adaptive=adaptive,
                running_silent_fan=current.running_silent_fan,
                live_tier=live_tier,
                game_override=True,
                standing_selector=standing,
                standing_adaptive=standing_adaptive,
            )
        self.changed()

    def restore_defaults(self, gpu_uuid, *, silent_fan, done):
        def work():
            self.state = RuntimeState(
                running_selector=STOCK,
                running_gpu_uuid=gpu_uuid,
                running_silent_fan=bool(silent_fan),
                defaults_restored=True,
            )
            self.boot[gpu_uuid] = BootEntry(STOCK)
            return CommandResult(True, "Restore defaults complete.")

        self._later(work, done)

    def delete_profiles(self, profile_ids, done):
        ids = [i for i in profile_ids if i in self.payloads]

        def work():
            for pid in ids:
                self.payloads.pop(pid, None)
                for tiers in self.assignments.values():
                    for tier in [t for t, v in tiers.items() if v == pid]:
                        tiers.pop(tier)
                for disabled in self.disabled.values():
                    disabled.discard(pid)
            label = "profile" if len(ids) == 1 else "profiles"
            return CommandResult(True, f"Deleted {len(ids)} saved {label}.")

        self._later(work, done)

    def clear_boot(self, gpu_uuid: str) -> None:
        self._require_daemon()
        self.boot.pop(gpu_uuid, None)
        if self.main_gpu == gpu_uuid:
            self.main_gpu = ""
        self.changed()

    def set_main_gpu(self, gpu_uuid: str) -> None:
        self._require_daemon()
        self.main_gpu = gpu_uuid
        self.changed()

    def assign_tier(self, profile_id: str, tier: str) -> None:
        payload = self.payloads[profile_id]
        uuid = profile_gpu_uuid(payload)
        tiers = self.assignments.setdefault(uuid, {})
        disabled = self.disabled.setdefault(uuid, set())
        for key in [t for t, v in tiers.items() if v == profile_id]:
            tiers.pop(key)
        if tier in PROFILE_TIERS:
            tiers[tier] = profile_id
            disabled.discard(profile_id)
        else:
            disabled.add(profile_id)
        self.changed()

    def _store(self, payload: dict, *, suffix: str = "") -> str:
        """Save a new profile. ``suffix`` is "-edited" only for user-edited
        copies (curve, fan, memory); scan results and imports have none."""
        created = self._now()
        pid = created.strftime("%Y%m%d-%H%M%S") + suffix
        payload.update(
            profile_id=pid,
            profile_created_at=created.isoformat(),
            path=f"{PROFILE_DIR}/auto-uv-profile-{pid}.json",
        )
        if payload.get("final_verified"):
            payload["verified_at"] = created.isoformat()
        self.payloads[pid] = payload
        self.changed()
        return pid

    def save_curve_edit(self, profile_id: str, edit) -> str:
        parent = self.payloads[profile_id]
        payload = user_edited_profile_payload(
            parent,
            edit,
            original_anchor_voltage_mv=int(parent["candidate_voltage_mv"]),
            original_anchor_clock_mhz=int(parent["lock_clock_mhz"]),
        )
        return self._store(payload, suffix="-edited")

    def save_fan_edit(self, profile_id: str, edit, original_points) -> str:
        parent = self.payloads[profile_id]
        payload = user_edited_fan_curve_profile_payload(
            parent, self._fan(profile_id) or {}, edit, original_points=original_points
        )
        return self._store(payload, suffix="-edited")

    def save_memory_offset(self, profile_id: str, memory_offset: int) -> str:
        parent = self.payloads[profile_id]
        payload = user_edited_memory_offset_profile_payload(
            parent,
            int(memory_offset),
            original_memory_offset_mhz=editable_memory_offset_from_profile(parent),
        )
        return self._store(payload, suffix="-edited")

    def export_lact(
        self, profile_id: str, directory: str, *, include_fan_curve: bool
    ) -> LactExport:
        folder = str(directory).rstrip("/") or "/"
        gpu_id = LACT_GPU_IDS.get(folder, "")
        if not gpu_id:
            return LactExport(
                error="Could not detect the LACT GPU id. Start LACT once, or choose a "
                "directory that already contains config.yaml."
            )
        if include_fan_curve and not self.fan_runtime_ready(profile_id):
            return LactExport(
                error="No runtime-ready silent fan curve is available for this profile."
            )
        try:
            text, warnings = build_lact_nvidia_config_from_plan(
                gpu_id=gpu_id,
                vf_plan=self.curve_plan(profile_id),
                fan_config=_auto_uv_fan_config(self._fan(profile_id))
                if include_fan_curve
                else None,
                source_label="auto-uv",
                source_vf=self.payloads[profile_id]["path"],
                include_fan_curve=include_fan_curve,
            )
        except LactExportError as exc:
            return LactExport(error=f"LACT export failed:\n{exc}")
        return LactExport(
            path=f"{folder}/config.yaml",
            warnings=(*warnings, "Sample data: nothing was written to disk."),
            text=text,
        )

    def finish_verification(self, profile_id: str, gpu_uuid: str, passed: bool) -> None:
        payload = self.payloads.get(profile_id)
        if payload is None:
            return
        if passed:
            self._bind_legacy(profile_id, gpu_uuid)
            payload.update(
                final_verified=True,
                requires_verification=False,
                verification_status="verified",
                verified_at=self._now().isoformat(),
            )
        else:
            payload.update(verification_status="failed")
        self.changed()

    def save_afterburner_import(self, entry: dict, fan_payload: dict | None) -> dict:
        section = str(entry.get("section", "")).strip()
        if not section:
            raise ValueError("selected Afterburner profile has no section name")
        clock = int(entry["target_clock_mhz"])
        voltage = int(entry["target_voltage_mv"])
        relative = relative_profile_path(
            entry.get("afterburner_root", ""), entry.get("profile_path", "")
        )
        display_name = f"MSI Afterburner {section} {clock} MHz {voltage} mV"
        plan = curve_plan(entry_curve_points(entry), self.sample["base"])
        payload = {
            "profile_source": "MSI Afterburner",
            "display_name": display_name,
            "candidate_id": f"afterburner-{Path(relative).stem}-{section}-{voltage}mv-{clock}mhz",
            "candidate_voltage_mv": voltage,
            "lock_clock_mhz": clock,
            "final_verified": True,
            "verification_status": "imported",
            "gpu_identity": _identity(SAMPLE_GPUS[0]),
            "plan": plan,
            "points": plan,
        }
        if fan_payload is not None:
            payload["fan_curve_payload"] = fan_payload
        pid = self._store(payload)
        return {
            "profile_id": pid,
            "section": section,
            "device_profile_relative_path": relative,
            "display_name": display_name,
            "profile_path": self.payloads[pid]["path"],
        }

    def save_scan_profile(
        self, tier: str, row: dict, memory_offset_mhz: int, gpu_index: int = 0
    ) -> str:
        """A tier's verified profile from the native Auto UV scan."""
        gpu = SAMPLE_GPUS[max(0, min(len(SAMPLE_GPUS) - 1, int(gpu_index)))]
        mv, target = int(row["mv"]), int(row["target"])
        fps, power = float(row.get("fps") or 0), float(row.get("power") or 0)
        payload = {
            "candidate_id": f"{tier.lower()}-{mv}mv-{target}mhz",
            "display_name": tier,
            "profile_source": "auto-uv-final",
            "auto_uv_requested_mode": tier.lower(),
            "candidate_voltage_mv": mv,
            "lock_clock_mhz": target,
            # The scan dialog's memory clock MHz is half the stored value.
            "memory_offset_mhz": int(memory_offset_mhz) * 2,
            "avg_core_clock_mhz": row.get("meas"),
            "avg_fps": fps or None,
            "avg_power_w": power or None,
            "efficiency_fps_per_w": round(fps / power, 4) if fps and power else None,
            "base_candidate_voltage_mv": GPU0_BASE["mv"],
            "base_avg_core_clock_mhz": GPU0_BASE["clock"],
            "base_avg_fps": GPU0_BASE["fps"],
            "base_avg_power_w": GPU0_BASE["power"],
            "base_efficiency_fps_per_w": round(
                GPU0_BASE["fps"] / GPU0_BASE["power"], 4
            ),
            "final_verified": True,
            "verification_status": "verified",
            "plan": curve_plan(row["points"], self.sample["base"]),
            "gpu_identity": _identity(gpu),
        }
        if row.get("temp") is not None and row.get("fan") is not None:
            payload["fan_curve_payload"] = silent_fan_payload(
                DEFAULT_FAN_CURVE, temp=row["temp"], fan=row["fan"], mv=mv, clock=target
            )
        return self._store(payload)

    # ---------------- prototype controls ----------------

    def set_gpu_count(self, count: int) -> None:
        """0 simulates a machine with no NVIDIA GPU (nothing to target)."""
        self.gpu_count = max(0, min(len(SAMPLE_GPUS), int(count)))
        self.changed()

    def set_daemon_available(self, available: bool) -> None:
        self.daemon_available = bool(available)
