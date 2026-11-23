"""What the root daemon runs now and applies at boot, as profile info dicts.

Each info dict carries ``selector`` (a profile id, the stock sentinel, or ""),
``silent_fan_curve`` and ``adaptive_auto_uv``, plus ``gpu_uuid`` and the
per-game override fields where the daemon reports them. The daemon socket is
authoritative; a legacy ``PenguinBurner.service`` unit is read only as a
fallback when the daemon does not answer.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Iterable

from overlay.state import read_overlay_state
from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR
from runtime.daemon_client import boot_runtime_spec, daemon_status
from runtime.support.runtime_service import (
    LEGACY_PENGUIN_BURNER_UNIT_NAME,
    PENGUIN_BURNER_UNIT_NAME,
    SYSTEMCTL,
    legacy_systemd_service_unit_path,
    systemd_service_unit_path,
)

# With the in-game HUD off, the daemon republishes runtime state on the fan
# poll cadence, which supports intervals up to 60 s. Allow one full interval
# plus scheduling margin before treating a file as left over from an old run.
LIVE_RUNTIME_STATE_MAX_AGE_S = 75.0


def empty_profile_info() -> dict[str, object]:
    return {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False}


# ---------------------------------------------------------------------------
# Boot (autostart) entries
# ---------------------------------------------------------------------------


def systemd_autostart_profile_info(*, gpu_uuid: str = "") -> dict[str, object]:
    """What boot applies: to ``gpu_uuid``, or on the daemon's active GPU."""
    return autostart_profile_infos([gpu_uuid])[str(gpu_uuid or "").strip()]


def autostart_profile_infos(gpu_uuids: Iterable[str]) -> dict[str, dict[str, object]]:
    """Boot entries for several GPUs from one daemon read, keyed by the
    stripped UUID ("" is the daemon's active boot entry)."""
    keys = list(dict.fromkeys(str(uuid or "").strip() for uuid in gpu_uuids))
    try:
        summary = boot_runtime_spec(timeout_s=1.0)
    except Exception:  # noqa: BLE001
        if not systemd_service_is_enabled():
            legacy = _legacy_systemd_autostart_profile_info()
            return {key: dict(legacy) for key in keys}
        return {key: empty_profile_info() for key in keys}
    return {
        key: autostart_profile_info_from_summary(summary, gpu_uuid=key) for key in keys
    }


def autostart_profile_info_from_summary(
    summary: object, *, gpu_uuid: str = ""
) -> dict[str, object]:
    """One GPU's entry in the daemon's ``boot_runtime_spec`` answer, with
    ``main_gpu``; without a UUID, the daemon's active boot entry."""
    selected_gpu_uuid = str(gpu_uuid or "").strip()
    if not selected_gpu_uuid:
        return _profile_info_from_runtime_summary(summary, require_configured=True)
    saved_specs = summary.get("gpus") if isinstance(summary, dict) else None
    if isinstance(summary, dict) and isinstance(saved_specs, list):
        for saved_spec in saved_specs:
            if not isinstance(saved_spec, dict):
                continue
            saved_uuid = str(saved_spec.get("gpu_uuid") or "").strip()
            if saved_uuid.casefold() == selected_gpu_uuid.casefold():
                info = _profile_info_from_runtime_summary(saved_spec)
                main_gpu_uuid = str(summary.get("main_gpu_uuid") or "").strip()
                info["main_gpu"] = bool(
                    main_gpu_uuid
                    and main_gpu_uuid.casefold() == selected_gpu_uuid.casefold()
                )
                return info
    return {
        **empty_profile_info(),
        "gpu_uuid": selected_gpu_uuid,
        "main_gpu": False,
    }


# ---------------------------------------------------------------------------
# Running profile
# ---------------------------------------------------------------------------


def running_auto_uv_profile_info() -> dict[str, object]:
    # Report only what is ACTUALLY applied right now (live daemon runner, or a
    # live legacy systemd unit). Deliberately no fallback to the autostart entry:
    # a configured-for-boot profile is not the same as a running one, and using
    # it here made the status show a stale profile after the runner was stopped
    # or the GPU was reset to stock. Autostart is surfaced separately.
    info = _daemon_running_profile_info()
    if str(info["selector"]):
        return info
    command = _legacy_systemd_running_exec_start()
    info = profile_info_from_command_text(command, default_if_present=True)
    if str(info["selector"]):
        return _with_override_defaults(info)
    return _with_override_defaults(empty_profile_info())


def penguin_burner_runtime_is_active() -> bool:
    if _daemon_runtime_profile_running():
        return True
    return _systemctl_quiet("is-active")


def live_runtime_profile_id(
    *,
    max_age_s: float = LIVE_RUNTIME_STATE_MAX_AGE_S,
) -> str:
    """The profile the daemon is running RIGHT NOW, or "" when unknown.

    An adaptive run switches tiers without touching the job spec it was
    started from, so ``active_job.profile_id`` keeps naming the tier it began
    on for the whole session. The published runtime state carries the tier
    actually in effect; this reads it and refuses anything stale.
    """
    try:
        state = read_overlay_state()
    except Exception:  # noqa: BLE001 - status poll must survive any read failure
        # Deliberately broad: this runs on the GUI's 2 s status poll, where an
        # unreadable state file must degrade to "unknown" rather than take the
        # whole poll down.
        return ""
    profile_id = str(state.get("profile_id") or "").strip()
    if not profile_id:
        return ""
    try:
        updated_ns = int(str(state.get("updated_unix_ns") or "").strip())
    except ValueError:
        return ""
    if updated_ns <= 0:
        return ""
    # Only staleness disqualifies it. A timestamp slightly in the future is a
    # clock detail, not evidence that the file describes an old session.
    if time.time() - updated_ns / 1_000_000_000 > float(max_age_s):
        return ""
    return profile_id


# ---------------------------------------------------------------------------
# Service unit
# ---------------------------------------------------------------------------


def systemd_unit_entry_exists() -> bool:
    # A persistent PenguinBurner entry means the native daemon unit is installed
    # (or a legacy unit survives) -- more robust than reading the state file,
    # which is briefly absent for a stock/no-argv autostart.
    try:
        boot_runtime_spec(timeout_s=1.0)
        return True
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        return (
            systemd_service_unit_path().is_file()
            or legacy_systemd_service_unit_path().is_file()
        )
    except OSError:
        return False


def systemd_service_is_enabled() -> bool:
    return _systemctl_quiet("is-enabled")


# ---------------------------------------------------------------------------
# Command-line profile info (legacy units, daemon argv jobs)
# ---------------------------------------------------------------------------


def profile_info_from_command_text(
    command_text: str,
    *,
    default_if_present: bool = False,
) -> dict[str, object]:
    parts = _command_parts(command_text)
    return profile_info_from_command_parts(parts, default_if_present=default_if_present)


def profile_info_from_command_parts(
    parts: list[str],
    *,
    default_if_present: bool = False,
) -> dict[str, object]:
    selector = _profile_selector_from_command_parts(parts)
    if not selector and default_if_present and parts:
        selector = "__systemd_default__"
    return {
        "selector": selector,
        "silent_fan_curve": "--silent-fan-curve" in parts,
        "adaptive_auto_uv": "--adaptive-auto-uv" in parts,
    }


def _with_override_defaults(info: dict[str, object]) -> dict[str, object]:
    info.setdefault("game_override", False)
    info.setdefault("standing_selector", "")
    info.setdefault("standing_adaptive", False)
    return info


def _profile_selector_from_command_parts(parts: list[str]) -> str:
    for index, part in enumerate(parts):
        if part == "--auto-uv-profile" and index + 1 < len(parts):
            return str(parts[index + 1])
        if part.startswith("--auto-uv-profile="):
            return part.split("=", 1)[1]
    return ""


def _command_parts(command_text: str) -> list[str]:
    try:
        return shlex.split(str(command_text or ""))
    except ValueError:
        return str(command_text or "").split()


def _legacy_systemd_unit_exec_start() -> str:
    try:
        text = legacy_systemd_service_unit_path().read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return ""
    service_exists = bool(text.strip())
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            return line.split("=", 1)[1]
    return "__systemd_default__" if service_exists else ""


def _legacy_systemd_running_exec_start() -> str:
    unit_name = f"{LEGACY_PENGUIN_BURNER_UNIT_NAME}.service"
    try:
        result = subprocess.run(
            [SYSTEMCTL, "show", unit_name, "--property=ExecStart", "--value"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if int(result.returncode) != 0:
        return ""
    return result.stdout.strip()


def _systemctl_quiet(action: str) -> bool:
    unit_name = f"{PENGUIN_BURNER_UNIT_NAME}.service"
    try:
        result = subprocess.run(
            [SYSTEMCTL, str(action), "--quiet", unit_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return int(result.returncode) == 0


def _legacy_systemd_autostart_profile_info() -> dict[str, object]:
    unit_name = f"{LEGACY_PENGUIN_BURNER_UNIT_NAME}.service"
    if not _systemctl_quiet_for_unit("is-enabled", unit_name):
        return empty_profile_info()
    command = _legacy_systemd_unit_exec_start()
    return profile_info_from_command_text(command, default_if_present=True)


def _systemctl_quiet_for_unit(action: str, unit_name: str) -> bool:
    try:
        result = subprocess.run(
            [SYSTEMCTL, str(action), "--quiet", unit_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return int(result.returncode) == 0


def _daemon_runtime_profile_running() -> bool:
    payload = _daemon_status_payload()
    return str(payload.get("state") or "") == "runtime_profile_running"


def _daemon_running_profile_info() -> dict[str, object]:
    payload = _daemon_status_payload()
    if str(payload.get("state") or "") != "runtime_profile_running":
        return _with_override_defaults(empty_profile_info())
    active_job = payload.get("active_job")
    if isinstance(active_job, dict) and str(active_job.get("runtime_mode") or ""):
        info = _profile_info_from_runtime_summary(active_job)
        info["gpu_uuid"] = str(active_job.get("gpu_uuid") or "")
        if bool(info.get("adaptive_auto_uv")):
            # The job spec names the tier the run STARTED on; adaptive has
            # very likely moved since. Prefer the live published tier, and
            # fall back to the spec when nothing fresh is available.
            live_profile_id = live_runtime_profile_id()
            if live_profile_id:
                info["selector"] = live_profile_id
        # The Steam tab's per-game override layer: active_job is the live
        # (possibly per-game) spec; game_runtime carries the standing one
        # that returns when the game exits.
        game_runtime = payload.get("game_runtime")
        if isinstance(game_runtime, dict) and bool(game_runtime.get("active")):
            info["game_override"] = True
            info["standing_selector"] = str(
                game_runtime.get("standing_profile_id") or ""
            )
            info["standing_adaptive"] = (
                str(game_runtime.get("standing_runtime_mode") or "") == "adaptive"
            )
        return _with_override_defaults(info)
    argv = active_job.get("argv") if isinstance(active_job, dict) else []
    if isinstance(argv, list) and all(isinstance(item, str) for item in argv):
        return _with_override_defaults(
            profile_info_from_command_parts(list(argv), default_if_present=True)
        )
    return _with_override_defaults(empty_profile_info())


def _profile_info_from_runtime_summary(
    summary: object,
    *,
    require_configured: bool = False,
) -> dict[str, object]:
    if not isinstance(summary, dict) or (
        require_configured and not bool(summary.get("configured"))
    ):
        return empty_profile_info()
    mode = str(summary.get("runtime_mode") or "").strip().lower()
    profile_id = str(summary.get("profile_id") or "").strip()
    selector = STOCK_PROFILE_SELECTOR if mode == "stock" else profile_id
    info: dict[str, object] = {
        "selector": selector,
        "silent_fan_curve": bool(summary.get("silent_fan_curve")),
        "adaptive_auto_uv": mode == "adaptive",
    }
    gpu_uuid = str(summary.get("gpu_uuid") or "").strip()
    if gpu_uuid:
        info["gpu_uuid"] = gpu_uuid
    return info


def _daemon_status_payload() -> dict[str, object]:
    try:
        payload = daemon_status(timeout_s=1.0)
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}
