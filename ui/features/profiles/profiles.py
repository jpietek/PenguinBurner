from __future__ import annotations

from pathlib import Path
import shlex
import subprocess
import time

from overlay.state import read_overlay_state
from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR
from profiles.uv.profile_store import display_signed_memory_clock
from profiles.uv.profile_store import profile_display_name
from profiles.uv.profile_store import read_auto_uv_profile_summaries
from profiles.uv.profile_tiers import available_adaptive_tiers
from profiles.uv.profile_tiers import profile_tier_label
from profiles.uv.profile_tiers import resolve_profile_tier_profiles
from runtime.daemon_client import boot_runtime_spec
from runtime.daemon_client import daemon_status
from runtime.support.runtime_service import LEGACY_PENGUIN_BURNER_UNIT_NAME
from runtime.support.runtime_service import PENGUIN_BURNER_UNIT_NAME
from runtime.support.runtime_service import SYSTEMCTL
from runtime.support.runtime_service import legacy_systemd_service_unit_path
from runtime.support.runtime_service import systemd_service_unit_path


def load_profile_summaries() -> list[dict]:
    return list(read_auto_uv_profile_summaries())


def profile_for_selector(profiles: list[dict], selector: str) -> dict | None:
    text = str(selector or "").strip()
    if not text:
        return None
    if text in {"latest", "__systemd_default__"}:
        return profiles[0] if profiles else None
    for profile in profiles:
        path = str(profile.get("path", "")).strip()
        path_names = {Path(path).name, Path(path).stem} if path else set()
        if text in {
            str(profile.get("profile_id", "")),
            str(profile.get("candidate_id", "")),
            path,
            *path_names,
        }:
            return profile
    return None


def selected_profile_ids_include_selector(
    profiles: list[dict],
    selected_ids: list[str],
    selector: str,
) -> bool:
    selected = {str(value) for value in selected_ids}
    if not selected or not str(selector or "").strip():
        return False
    profile = profile_for_selector(profiles, selector)
    return bool(profile and str(profile.get("profile_id", "")) in selected)


def profile_delete_autostart_action(
    profiles: list[dict],
    selected_ids: list[str],
    autostart_info: dict[str, object],
    *,
    include_legacy_profiles: bool = False,
) -> dict[str, str]:
    selected = {str(value).strip() for value in selected_ids if str(value).strip()}
    if not selected:
        return {"action": "keep"}
    selector = str(autostart_info.get("selector", "")).strip()
    if not selector:
        return {"action": "keep"}
    if bool(autostart_info.get("adaptive_auto_uv", False)):
        gpu_uuid = str(autostart_info.get("gpu_uuid") or "").strip()
        remaining_profiles = [
            profile
            for profile in profiles
            if str(profile.get("profile_id", "")).strip() not in selected
        ]
        resolved = resolve_profile_tier_profiles(
            remaining_profiles,
            gpu_uuid=gpu_uuid,
            include_legacy_profiles=include_legacy_profiles,
        )
        remaining_tiers = available_adaptive_tiers(resolved)
        if remaining_tiers:
            # Adaptive runtime is valid with a single remaining tier: it
            # applies that tier's curve and simply has nothing to switch to.
            return {"action": "keep"}
        return {
            "action": "restore-stock",
            "reason": "last-usable-adaptive-profile",
        }
    if selected_profile_ids_include_selector(profiles, list(selected), selector):
        return {"action": "restore-stock"}
    return {"action": "keep"}


def profile_can_apply(profile: dict) -> bool:
    return bool(profile.get("final_verified", False))


def adaptive_profile_tier_keys(
    profiles: list[dict],
    *,
    assignments: dict[str, str] | None = None,
    gpu_uuid: str = "",
    include_legacy_profiles: bool = False,
) -> list[str]:
    resolved = resolve_profile_tier_profiles(
        list(profiles),
        assignments=assignments,
        gpu_uuid=gpu_uuid,
        include_legacy_profiles=include_legacy_profiles,
    )
    return available_adaptive_tiers(resolved)


def adaptive_profile_tier_labels(
    profiles: list[dict],
    *,
    assignments: dict[str, str] | None = None,
    gpu_uuid: str = "",
    include_legacy_profiles: bool = False,
) -> list[str]:
    return [
        label
        for label in (
            profile_tier_label(tier)
            for tier in adaptive_profile_tier_keys(
                profiles,
                assignments=assignments,
                gpu_uuid=gpu_uuid,
                include_legacy_profiles=include_legacy_profiles,
            )
        )
        if label
    ]


def profile_can_verify(profile: dict) -> bool:
    return bool(str(profile.get("path", "")).strip())


def profile_verify_selector(profile: dict) -> str:
    path = str(profile.get("path", "")).strip()
    if path:
        return path
    return str(profile.get("profile_id", "")).strip()


def profile_is_deletable(profile: dict) -> bool:
    return bool(str(profile.get("path", "")).strip())


def profile_status_label(profiles: list[dict], selector: str) -> str:
    profile = profile_for_selector(profiles, selector)
    if profile is None:
        text = str(selector or "").strip()
        if text == "__systemd_default__":
            return "latest Auto-UV profile"
        return text or "unknown profile"
    display_name = str(profile.get("display_name", "")).strip()
    if display_name:
        return display_name
    text = profile_frequency_voltage(profile)
    return text or profile_display_name(profile) or str(profile.get("profile_id", ""))


def profile_frequency_voltage(profile: dict) -> str:
    clock = _status_number(profile.get("lock_clock_mhz"), precision=0)
    voltage = _status_number(profile.get("candidate_voltage_mv"), precision=0)
    if clock and voltage:
        text = f"{clock} MHz {voltage} mV"
    else:
        text = f"{clock} MHz" if clock else (f"{voltage} mV" if voltage else "")
    memory = _memory_offset_summary(profile.get("memory_offset_mhz"))
    if text and memory:
        return f"{text}, {memory}"
    return text or memory


def _memory_offset_summary(value) -> str:
    # Match the profile table / Auto-UV dialog (signed memory-clock MHz), but
    # suppress a no-op +0 MHz so the running-profile line stays clean when the
    # profile carries no memory offset.
    text = display_signed_memory_clock(value)
    if not text or text.startswith("0 "):
        return ""
    return f"mem {text}"


def runner_status_parts(
    profiles: list[dict],
    *,
    running_selector: str = "",
    running_adaptive: bool = False,
    autostart_selector: str = "",
    running_silent_fan: bool = False,
    autostart_silent_fan: bool = False,
    defaults_restored: bool = False,
    game_override: bool = False,
    standing_selector: str = "",
    standing_adaptive: bool = False,
) -> tuple[list[str], list[str]]:
    """The status split into what is running and everything behind it.

    Two groups, not one sentence. The headline -- the profile actually applied,
    which is what the bar exists to say -- kept losing the race for width: the
    whole thing runs to ~170 characters and the interesting half sits in the
    middle, so it was the first part to disappear.
    """
    running_selector = str(running_selector or "").strip()
    autostart_selector = str(autostart_selector or "").strip()
    standing_selector = str(standing_selector or "").strip()
    # Daemon running the reserved stock runtime, or a just-completed restore:
    # the GPU is at stock, so report it plainly as Default (no misleading
    # clock/voltage numbers -- those are the V/F ceiling, not the live point).
    if defaults_restored or running_selector == STOCK_PROFILE_SELECTOR:
        autostarts = "Yes" if autostart_selector else "No"
        return (
            ["Currently running profile: Default"],
            [f"Autostart: {autostarts}"],
        )
    if running_selector:
        autostarts = _profile_selectors_match(
            profiles,
            running_selector,
            autostart_selector,
        )
        running_label = profile_status_label(profiles, running_selector)
        markers = ["Adaptive"] if running_adaptive else []
        if game_override:
            # Two writers exist: the Profiles tab sets the standing/boot
            # profile, the Steam tab swaps a per-game one in while the game
            # runs. Show both layers, and judge autostart against the
            # STANDING profile -- the per-game override is transient.
            markers.append("per-game")
        if markers:
            running_label = f"{running_label} ({', '.join(markers)})"
        head = [f"Currently running profile: {running_label}"]
        parts: list[str] = []
        if game_override:
            if standing_selector and standing_selector != STOCK_PROFILE_SELECTOR:
                standing_label = profile_status_label(profiles, standing_selector)
                if standing_adaptive:
                    standing_label = f"{standing_label} (Adaptive)"
            else:
                standing_label = "Default"
            parts.append(f"Standing: {standing_label}")
            autostarts = _profile_selectors_match(
                profiles,
                standing_selector,
                autostart_selector,
            )
        parts.append(f"Autostart: {'Yes' if autostarts else 'No'}")
        parts.append(f"Silent fan curve: {_on_off(running_silent_fan)}")
        if autostart_selector and not autostarts and not game_override:
            parts.append(
                f"Autostart profile: {profile_status_label(profiles, autostart_selector)}"
            )
        return head, parts
    if autostart_selector:
        return (
            [f"Autostart profile: {profile_status_label(profiles, autostart_selector)}"],
            [
                "Autostart: Yes",
                f"Silent fan curve: {_on_off(autostart_silent_fan)}",
                "Not running now",
            ],
        )
    return ["No running/autostart profile available yet."], []


def runner_status_text(
    profiles: list[dict],
    *,
    running_selector: str = "",
    running_adaptive: bool = False,
    autostart_selector: str = "",
    running_silent_fan: bool = False,
    autostart_silent_fan: bool = False,
    defaults_restored: bool = False,
    game_override: bool = False,
    standing_selector: str = "",
    standing_adaptive: bool = False,
) -> str:
    """The same status as one sentence -- tooltips, the CLI, and old callers."""
    head, details = runner_status_parts(
        profiles,
        running_selector=running_selector,
        running_adaptive=running_adaptive,
        autostart_selector=autostart_selector,
        running_silent_fan=running_silent_fan,
        autostart_silent_fan=autostart_silent_fan,
        defaults_restored=defaults_restored,
        game_override=game_override,
        standing_selector=standing_selector,
        standing_adaptive=standing_adaptive,
    )
    text = "; ".join(head + details)
    return text if text.endswith(".") else f"{text}."


def systemd_autostart_profile_info(*, gpu_uuid: str = "") -> dict[str, object]:
    selected_gpu_uuid = str(gpu_uuid or "").strip()
    try:
        summary = boot_runtime_spec(timeout_s=1.0)
    except Exception:
        if not systemd_service_is_enabled():
            return _legacy_systemd_autostart_profile_info()
        return {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False}
    if selected_gpu_uuid:
        saved_specs = summary.get("gpus") if isinstance(summary, dict) else None
        if isinstance(saved_specs, list):
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
            "selector": "",
            "silent_fan_curve": False,
            "adaptive_auto_uv": False,
            "gpu_uuid": selected_gpu_uuid,
            "main_gpu": False,
        }
    return _profile_info_from_runtime_summary(summary, require_configured=True)


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
    return _with_override_defaults(
        {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False}
    )


def _with_override_defaults(info: dict[str, object]) -> dict[str, object]:
    info.setdefault("game_override", False)
    info.setdefault("standing_selector", "")
    info.setdefault("standing_adaptive", False)
    return info


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


def systemd_unit_entry_exists() -> bool:
    # A persistent PenguinBurner entry means the native daemon unit is installed
    # (or a legacy unit survives) -- more robust than reading the state file,
    # which is briefly absent for a stock/no-argv autostart.
    try:
        boot_runtime_spec(timeout_s=1.0)
        return True
    except Exception:
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


def penguin_burner_runtime_is_active() -> bool:
    if _daemon_runtime_profile_running():
        return True
    return _systemctl_quiet("is-active")


def delete_confirmation_text(
    names: list[str],
    *,
    restores_stock: bool = False,
    removes_last_usable_adaptive_profile: bool = False,
) -> str:
    clean_names = [str(name).strip() for name in names if str(name).strip()]
    if not clean_names:
        subject = "the selected profiles"
    elif len(clean_names) == 1:
        subject = f"Auto-UV profile {clean_names[0]}"
    else:
        subject = f"{len(clean_names)} selected profiles"
    message = f"Delete {subject}?"
    if restores_stock and removes_last_usable_adaptive_profile:
        if len(clean_names) == 1:
            message += (
                "\n\nThis is the last usable Adaptive Auto-UV profile. "
                "Deleting it will restore stock now and at boot."
            )
        else:
            message += (
                "\n\nThese are the last usable Adaptive Auto-UV profiles. "
                "Deleting them will restore stock now and at boot."
            )
    elif restores_stock:
        message += (
            "\n\nThis profile is currently persisted on startup. Deleting it will "
            "restore stock now and at boot."
        )
    return message


def _profile_selectors_match(
    profiles: list[dict],
    left_selector: str,
    right_selector: str,
) -> bool:
    left = str(left_selector or "").strip()
    right = str(right_selector or "").strip()
    if not left or not right:
        return False
    if left == right:
        return True
    left_profile = profile_for_selector(profiles, left)
    right_profile = profile_for_selector(profiles, right)
    if left_profile is None or right_profile is None:
        return False
    return str(left_profile.get("profile_id", "")) == str(
        right_profile.get("profile_id", "")
    )


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
        return {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False}
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
        return _with_override_defaults(
            {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False}
        )
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
    return _with_override_defaults(
        {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False}
    )


def _profile_info_from_runtime_summary(
    summary: dict,
    *,
    require_configured: bool = False,
) -> dict[str, object]:
    if not isinstance(summary, dict) or (
        require_configured and not bool(summary.get("configured"))
    ):
        return {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False}
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


# With the in-game HUD off, the daemon republishes runtime state on the fan
# poll cadence, which supports intervals up to 60 s. Allow one full interval
# plus scheduling margin before treating a file as left over from an old run.
LIVE_RUNTIME_STATE_MAX_AGE_S = 75.0


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


def _daemon_status_payload() -> dict[str, object]:
    try:
        payload = daemon_status(timeout_s=1.0)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _status_number(value, *, precision: int) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if precision <= 0:
        return str(int(round(number)))
    return f"{number:.{int(precision)}f}"


def _on_off(value: bool) -> str:
    return "On" if bool(value) else "Off"
