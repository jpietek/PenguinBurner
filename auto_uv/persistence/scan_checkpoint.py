"""Durable scan measurements and UI history, reused only by an explicit new scan.

Replaying measurements rebuilds the existing search state without repeating GPU
work. The normal blacklist and final-verification paths still own safety.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import asdict, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from auto_uv.domain.events import AutoUvEventCallback
from auto_uv.domain.types import (
    AutoUvCriticalProbeError,
    AutoUvError,
    AutoUvProbeSummary,
    AutoUvVoltageScanResult,
    VfCurveCandidate,
)
from auto_uv.run.scan_resume import completed_ui_events, recovery_candidate
from auto_uv.scan_mode.auto_uv_mode import normalize_auto_uv_mode
from auto_uv.shared.positive_int import positive_int
from profiles.uv.profile_store import auto_uv_profiles_dir
from stability.q2rtx.models import (
    Q2RTXBenchmarkSummary,
    Q2RTXStabilityResult,
    TelemetrySample,
)

from .auto_uv_persisted_json_files import auto_uv_user_config_dir, safe_json_write
from .checkpoint_curve import (
    compatible_stock_curves,
    rebase_measured_plan,
    stock_curve_drift_mhz,
)
from .unsafe_voltage_blacklist_file import load_unsafe_voltage_blacklist
from .unsafe_voltage_cache import unsafe_voltage_block_reason

T = TypeVar("T")
_FORMAT_VERSION = 1
_RESULT_TYPES = {
    cls.__name__: cls
    for cls in (
        AutoUvProbeSummary,
        AutoUvVoltageScanResult,
        VfCurveCandidate,
        Q2RTXStabilityResult,
        Q2RTXBenchmarkSummary,
        TelemetrySample,
    )
}
_UI_EVENTS = {
    "base_curve",
    "candidate_curve",
    "probe_start",
    "probe_result",
    "memory_offset_applied",
    "derived_defaults",
    "tier_started",
    "tier_descent_reused",
    "tier_confirmed",
    "tier_completed",
    "tier_skipped",
}


def scan_checkpoint_path() -> Path:
    return auto_uv_user_config_dir() / "uv-result" / "auto-uv-scan-checkpoint.json"


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"unsupported checkpoint value: {type(value).__name__}")


def checkpoint_key(value: object) -> str:
    payload = json.dumps(value, default=_json_default, sort_keys=True, allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _encode(value: Any) -> Any:
    if isinstance(value, Path):
        return {"path": str(value), "type": "Path"}
    if type(value).__name__ in _RESULT_TYPES and is_dataclass(value):
        return {
            "type": type(value).__name__,
            "fields": {
                field.name: _encode(getattr(value, field.name))
                for field in fields(value)
            },
        }
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        if value.get("type") == "Path":
            return Path(value["path"])
        if value.get("type") in _RESULT_TYPES:
            cls = _RESULT_TYPES[value["type"]]
            result_fields = value["fields"]
            if not isinstance(result_fields, dict):
                raise ValueError(f"invalid fields for {value['type']}")
            return cls(**{key: _decode(item) for key, item in result_fields.items()})
        return {key: _decode(item) for key, item in value.items()}
    return value


def _profile_receipts() -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in auto_uv_profiles_dir().glob("*.json")
    }


class ScanCheckpoint:
    def __init__(
        self,
        *,
        identity: dict,
        callback: AutoUvEventCallback | None,
        log: Callable[[str], None],
        path: Path | None = None,
    ):
        self.path = path or scan_checkpoint_path()
        self.callback = callback
        self.log = log
        self.identity = checkpoint_key(identity)
        self.identity_components = {
            key: checkpoint_key(value) for key, value in identity.items()
        }
        self.base_curve = identity.get("base_curve")
        self.base_curve_rebased = False
        self.records: dict[str, Any] = {}
        self.events: list[dict] = []
        self.pending: list[dict] = []
        self.profiles: dict[str, str] = {}
        self.mode = normalize_auto_uv_mode(identity.get("options", {}).get("auto_uv_mode"))
        self.current_tier = "efficiency" if self.mode == "adaptive" else self.mode
        self.passed: list[dict] = []
        self.recovery: VfCurveCandidate | None = None
        self.recovery_tier = ""
        self.resuming = False
        self.replaying = False
        self.rejection_reason: str | None = None
        try:
            current_profiles = _profile_receipts()
        except OSError as exc:
            raise AutoUvCriticalProbeError(f"Cannot check saved profiles for resume: {exc}") from exc
        try:
            saved_bytes = self.path.read_bytes()
        except FileNotFoundError:
            self.log("Auto-UV: no saved scan checkpoint; starting a new scan.")
        except OSError as exc:
            raise AutoUvCriticalProbeError(f"Cannot read Auto-UV resume checkpoint: {exc}") from exc
        else:
            try:
                self._restore(json.loads(saved_bytes), current_profiles)
            except (ValueError, TypeError, KeyError) as exc:
                self.rejection_reason = str(exc) or type(exc).__name__
                backup = self._archive_rejected(saved_bytes)
                self.log(
                    f"Auto-UV: checkpoint rejected: {self.rejection_reason}. "
                    f"Preserved at {backup}; starting a new scan without saved-candidate recovery."
                )
        if not self.resuming:
            self.profiles = current_profiles
        self._save()

    def _restore(self, payload: Any, current_profiles: dict[str, str]) -> None:
        if not isinstance(payload, dict):
            raise TypeError("invalid checkpoint object")
        if payload.get("format_version") != _FORMAT_VERSION:
            raise ValueError(f"unsupported format version {payload.get('format_version')!r}")
        rebase = False
        if payload.get("identity") != self.identity:
            components = payload.get("identity_components")
            if isinstance(components, dict):
                changed = sorted(
                    key for key in set(components) | set(self.identity_components)
                    if components.get(key) != self.identity_components.get(key)
                )
                detail = ", ".join(changed) or "identity hash"
                rebase = (
                    changed == ["base_curve"]
                    and checkpoint_key(payload.get("base_curve")) == components.get("base_curve")
                    and compatible_stock_curves(payload.get("base_curve"), self.base_curve)
                )
            else:
                detail = "older checkpoint has no component fingerprints"
            if not rebase:
                raise ValueError(f"scan inputs changed: {detail}")
        if payload.get("profiles") != current_profiles:
            raise ValueError("saved profiles changed, were added, or were removed")
        records = _decode(payload["records"])
        events = payload["events"]
        if not isinstance(records, dict) or not isinstance(events, list):
            raise TypeError("invalid checkpoint records or events")
        if not all(
            isinstance(e, dict)
            and e.get("event") in _UI_EVENTS
            and isinstance(e.get("payload"), dict)
            for e in events
        ):
            raise ValueError("invalid checkpoint events")
        passed = _decode(payload["passed"])
        if not isinstance(passed, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("candidate"), VfCurveCandidate)
            and isinstance(item.get("probe"), AutoUvProbeSummary)
            and isinstance(item.get("tier"), str)
            for item in passed
        ):
            raise ValueError("invalid passed candidates")
        if rebase:
            assert isinstance(self.base_curve, list)  # Validated by compatible_stock_curves.
            # Old probe hashes include the old base and offsets. Keep completed
            # tiers, but remeasure baselines rather than replay stale stock data.
            records = {
                key: value for key, value in records.items()
                if key in {
                    checkpoint_key({"completed_tier": tier})
                    for tier in ("efficiency", "balanced", "performance")
                }
            }
            for item in passed:
                item["candidate"] = replace(
                    item["candidate"],
                    flattened_plan=rebase_measured_plan(
                        item["candidate"].flattened_plan, self.base_curve
                    ),
                )
                probe = item["probe"]
                if probe.tested_plan is not None:
                    probe.tested_plan = rebase_measured_plan(probe.tested_plan, self.base_curve)
            self.log(
                "Auto-UV: stock base clocks shifted by up to "
                f"{stock_curve_drift_mhz(payload.get('base_curve'), self.base_curve)}MHz "
                "(same voltage grid); retaining completed tiers and absolute "
                "candidate targets, remeasuring baselines before resume verification."
            )
        self.passed = passed
        self.records, self.events = records, events
        self.profiles = current_profiles
        self.base_curve_rebased = rebase
        self.resuming = bool(records or passed)
        self.replaying = self.resuming and not rebase
        if not self.resuming:
            self.log("Auto-UV: saved checkpoint has no completed measurements; starting a new scan.")

    def _archive_rejected(self, saved_bytes: bytes) -> Path:
        backup = self.path.with_name(f"{self.path.name}.rejected-{uuid4().hex}.bak")
        try:
            with backup.open("xb") as handle:
                handle.write(saved_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise AutoUvCriticalProbeError(
                f"Cannot preserve rejected checkpoint; original left untouched: {exc}"
            ) from exc
        return backup

    def prepare_recovery(
        self, base_curve: list[dict], unsafe: list[dict], interrupted: dict | None
    ) -> None:
        if not self.resuming:
            return
        tier = self.current_tier
        completed = set()
        failed_clock = failed_voltage = None
        for item in self.events:
            event, payload = item["event"], item["payload"]
            if event == "tier_started":
                tier = str(payload["tier"])
                failed_clock = failed_voltage = None
            elif event == "tier_completed":
                completed.add(str(payload["tier"]))
            elif event == "probe_result" and payload.get("decision") == "fail":
                failed_clock = int(payload["clock_mhz"])
                failed_voltage = positive_int(payload.get("voltage_mv"))
        if interrupted:
            failed_clock = int(interrupted["lock_clock_mhz"])
            failed_voltage = positive_int(interrupted.get("candidate_voltage_mv"))
        if tier not in completed and any(item["tier"] == tier for item in self.passed):
            try:
                self.recovery = recovery_candidate(
                    base_curve,
                    self.passed,
                    tier=tier,
                    unsafe=unsafe,
                    failed_clock_mhz=failed_clock,
                    failed_voltage_mv=failed_voltage,
                )
                self.recovery_tier = tier
            except AutoUvError:
                # A crash during a clock climb (Performance Auto-OC, the
                # Efficiency/Balanced reclaim) blacklists the rungs that passed
                # just below it. Nothing in that tier is left to verify, so the
                # tier simply runs again from its own start: Performance reuses
                # the verified Balanced point and the climb skips the cached
                # band before touching the GPU. Aborting the whole resume here
                # cost the tester a reboot's worth of progress (issue #109).
                self.log(
                    f"Auto-UV: every passed {tier} candidate lies inside the cached "
                    f"unsafe band; {tier} restarts from its baseline with that band "
                    "skipped. Completed tiers are kept."
                )
        self.events = completed_ui_events(self.events, unsafe, self.mode)
        self._save()

    def recovery_for(self, tier: str) -> VfCurveCandidate | None:
        return self.recovery if self.recovery_tier == tier else None

    def completed_tier(self, tier: str) -> AutoUvVoltageScanResult | None:
        # Completed profiles remain valid when a probe cache miss ends replay.
        result = self.records.get(checkpoint_key({"completed_tier": tier}))
        if not isinstance(result, AutoUvVoltageScanResult):
            return None
        if unsafe_voltage_block_reason(
            load_unsafe_voltage_blacklist(),
            candidate_voltage_mv=result.final_voltage_mv,
            lock_clock_mhz=result.lock_clock_mhz,
            profile_tier=tier,
        ):
            raise AutoUvCriticalProbeError(
                f"Cannot reuse completed {tier} tier: its point is now blacklisted"
            )
        return _decode(_encode(result))

    def history_for(self, tier: str) -> list[AutoUvProbeSummary]:
        unsafe = load_unsafe_voltage_blacklist()
        return [
            item["probe"]
            for item in self.passed
            if item["tier"] == tier
            and not unsafe_voltage_block_reason(
                unsafe,
                candidate_voltage_mv=item["candidate"].voltage_mv,
                lock_clock_mhz=item["candidate"].target_mhz,
                profile_tier=tier,
            )
        ]

    def note_pass(self, candidate: VfCurveCandidate, probe: AutoUvProbeSummary) -> None:
        if not self.replaying:
            self.passed.append(
                {"tier": self.current_tier, "candidate": candidate, "probe": probe}
            )
            self._save()

    def restore_ui(self) -> None:
        if self.resuming:
            self.log("Auto-UV: resuming saved scan; restoring measurements and curves.")
            if self.callback:
                self.callback("scan_resumed", {"events": self.events})

    def event(self, event: str, payload: dict) -> None:
        if event == "tier_started":
            self.current_tier = str(payload["tier"])
        item = {"event": event, "payload": payload}
        if self.replaying:
            self.pending.append(item)
            return
        if event in _UI_EVENTS:
            self.events.append(item)
            self._save()
        if self.callback:
            self.callback(event, payload)

    def lookup(self, key: object) -> Any:
        value = self.records.get(checkpoint_key(key)) if self.replaying else None
        if value is not None:
            self.pending.clear()
            # Search functions may append to histories: never mutate the saved copy.
            return _decode(_encode(value))
        return None

    def continue_live(self) -> None:
        if not self.replaying:
            return
        self.replaying = False
        pending, self.pending = self.pending, []
        for item in pending:
            if item not in self.events:
                self.event(item["event"], item["payload"])
        self.log("Auto-UV: saved progress restored; continuing uncompleted work.")

    def record(self, key: object, value: object, *, profiles: bool = False) -> None:
        self.records[checkpoint_key(key)] = value
        if profiles:
            self.profiles = _profile_receipts()
        self._save()

    def probe(self, key: object, run: Callable[[], T]) -> T:
        cached = self.lookup(key)
        if cached is not None:
            return cached
        if self.replaying:
            self.log("Auto-UV: saved probe unavailable; completed tiers remain reusable.")
        self.continue_live()
        result = run()
        self.record(key, result)
        return result

    def finish(self, result: T, *, adaptive: bool = False) -> T:
        completed = {
            item["payload"].get("tier")
            for item in self.events
            if item["event"] == "tier_completed"
            or (
                item["event"] == "tier_skipped"
                and item["payload"].get("reason") == "discarded"
            )
        }
        if not adaptive or completed >= {"efficiency", "balanced", "performance"}:
            self.discard()
        return result

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)

    def _save(self) -> None:
        try:
            safe_json_write(
                self.path,
                {
                    "format_version": _FORMAT_VERSION,
                    "identity": self.identity,
                    "identity_components": self.identity_components,
                    "base_curve": self.base_curve,
                    "records": _encode(self.records),
                    "events": self.events,
                    "profiles": self.profiles,
                    "passed": _encode(self.passed),
                },
            )
        except (OSError, ValueError, TypeError) as exc:
            raise AutoUvCriticalProbeError(f"Cannot save Auto-UV resume checkpoint: {exc}") from exc


def scan_checkpoint_identity(
    gpu, runtime_options: dict, q2rtx_config, settings
) -> dict:
    """Bind measurements to this card, driver, algorithm, workload and settings."""
    config = asdict(q2rtx_config)
    for key in ("progress_callback", "abort_callback", "log_dir"):
        config.pop(key, None)
    # Source changes can alter search decisions without a package version bump.
    owner = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for root in (owner, owner.parent / "stability"):
        for path in sorted(root.rglob("*.py")):
            digest.update(path.relative_to(owner.parent).as_posix().encode())
            digest.update(path.read_bytes())
    return {
        "gpu": gpu.gpu_identity,
        "driver": gpu.reader.capabilities().identity.driver_version,
        "base_curve": gpu.runtime_default_plan,
        "policy": gpu.translated_gpu_policy,
        "options": runtime_options,
        "settings": {
            field.name: getattr(settings, field.name)
            for field in fields(settings)
            if field.name != "q2rtx_config"
        },
        "workload": config,
        "algorithm": digest.hexdigest(),
    }
