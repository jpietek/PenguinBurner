"""Apply a saved game preset and register its session PID with the root daemon.

The launch wrapper execs the game, retaining the watched PID. Failures must
never block launch. Steam resolves its account-based identity separately."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from drivers.nvidia.daemon_gpu import DaemonGpuClient
from overlay.wrapper_tokens import split_game_key
from profiles.game_profile import (
    GAME_MODE_ADAPTIVE,
    GameProfileSetting,
    game_gpu_target,
    profile_argv_for_setting,
)

from .game_settings import LauncherGameSetting
from .wrapper_manager import ApplyResult


def hot_reapply_game_profile(
    game_key: str, setting: GameProfileSetting, *, mode_change: bool = False,
) -> ApplyResult | None:
    """Update this game's existing watch without taking another session's GPU."""
    from runtime.daemon_client import daemon_status, start_game_runtime_profile

    if not setting.enabled or (not mode_change and setting.mode != GAME_MODE_ADAPTIVE):
        return None
    saved = "Mode saved" if mode_change else "Target saved"
    try:
        status = daemon_status(timeout_s=1.0)
        game_runtime = status.get("game_runtime") or {}
        watch = next(
            (entry for entry in game_runtime.get("watched", [])
             if entry.get("app_id") == game_key and entry.get("pid")),
            None,
        )
        if watch is None:
            return (ApplyResult(True, "Mode saved for next launch; no active profile for this game.")
                    if mode_change else None)
        active_job = status.get("active_job") or {}
        if not game_runtime.get("active"):
            return ApplyResult(False, f"{saved}; this game's profile is not active.")
        if not mode_change and active_job.get("runtime_mode") != "adaptive":
            return ApplyResult(False, f"{saved}; this game's Adaptive profile is not active.")
        if setting.gpu_uuid and setting.gpu_uuid != active_job.get("gpu_uuid"):
            return ApplyResult(False, f"{saved}; relaunch the game to change its GPU.")
        argv = profile_argv(setting)
        if argv is None:
            return ApplyResult(False, f"{saved}; the selected profile or target GPU is unavailable.")
        result = start_game_runtime_profile(
            argv, watch_pid=int(watch["pid"]), app_id=game_key, timeout_s=45.0
        )
        if result.get("ignored") or not result.get("started", False):
            reason = result.get("reason") or "daemon did not start the profile"
            return ApplyResult(False, f"{saved}; live update skipped: {reason}")
        if mode_change:
            # A saved selection and an accepted request are not proof of the
            # active mode. Read the same game's ownership and mode back.
            current = daemon_status(timeout_s=1.0)
            runtime = current.get("game_runtime") or {}
            job = current.get("active_job") or {}
            expected = setting.mode if setting.mode in ("adaptive", "stock") else "static"
            confirmed = (
                runtime.get("active") and watch in runtime.get("watched", [])
                and job.get("runtime_mode") == expected
                and job.get("gpu_uuid") == active_job.get("gpu_uuid")
                and (expected != "static" or job.get("profile_id") == argv[1])
            )
            if not confirmed:
                actual = str(job.get("runtime_mode") or "unknown")
                return ApplyResult(False, f"{saved}; live change unconfirmed (daemon mode: {actual}).")
            return ApplyResult(True, f"{setting.mode.title()} applied to the running game; verified with daemon.")
    except Exception as error:  # noqa: BLE001 - saved setting survives a failed live apply
        return ApplyResult(False, f"{saved}; live update failed: {error}")
    return ApplyResult(True, "Adaptive target applied to the running game.")


def profile_argv(setting: GameProfileSetting) -> list[str] | None:
    """The daemon request this preset means on today's hardware, or None."""
    try:
        identities = list(DaemonGpuClient.discover_identities())
    except Exception:  # noqa: BLE001 - profile automation must not block game launch
        return None
    target = game_gpu_target(setting, identities)
    if target is None:
        return None
    gpu_uuid, gpu_index = target
    return profile_argv_for_setting(
        setting,
        gpu_index=gpu_index,
        gpu_uuid=gpu_uuid,
        include_legacy_profiles=len(identities) == 1,
    )


def send_profile(
    argv: list[str],
    *,
    app_id: str,
    watch_pid: int | None = None,
) -> bool:
    """Hand a resolved request to the daemon and have it watch this session."""
    from runtime.daemon_client import start_game_runtime_profile

    try:
        result = start_game_runtime_profile(
            argv,
            watch_pid=os.getpid() if watch_pid is None else int(watch_pid),
            app_id=str(app_id),
            timeout_s=45.0,
        )
    except Exception as error:  # noqa: BLE001 - profile automation must not block game launch
        return _skipped(error)
    if isinstance(result, dict) and (
        bool(result.get("ignored")) or not bool(result.get("started", True))
    ):
        return _skipped(result.get("reason") or "daemon did not start the profile")
    return True


def _skipped(reason: object) -> bool:
    print(f"penguin-burner: per-game profile apply skipped: {reason}", file=sys.stderr)
    return False


def game_setting(
    game_key: str,
    *,
    settings_path: str | Path | None = None,
) -> LauncherGameSetting | None:
    """The stored preset behind a "<launcher>:<game id>" key.

    Each launcher's store is imported only when a game of its own is starting:
    this runs in the launch wrapper, before the game, where importing every
    integration would be work the player waits through.
    """
    launcher_id, game_id = split_game_key(game_key)
    if launcher_id == "lutris":
        from integrations.lutris.settings import LUTRIS_GAME_SETTINGS_STORE as store
    elif launcher_id == "heroic":
        from integrations.heroic.settings import HEROIC_GAME_SETTINGS_STORE as store
    elif launcher_id == "faugus":
        from integrations.faugus.settings import FAUGUS_GAME_SETTINGS_STORE as store
    else:
        return None
    return store.get(game_id, path=settings_path)


def apply_game_key_profile(
    game_key: str,
    *,
    settings_path: str | Path | None = None,
    watch_pid: int | None = None,
) -> bool:
    """Apply the preset for the game the wrapper was told it is starting.

    The key is also the id the daemon registers the running game under, so a
    Lutris game 27 and a Steam app 27 cannot be confused for each other.
    """
    key = str(game_key or "").strip()
    setting = game_setting(key, settings_path=settings_path)
    argv = None if setting is None else profile_argv(setting)
    if argv is None:
        return False
    return send_profile(argv, app_id=key, watch_pid=watch_pid)
