"""Applying a game's saved preset from inside the launch wrapper.

Runs in the PENGUIN_BURNER wrapper, before it execs the game: resolve the
launching game to its stored setting, then ask the root daemon to apply that
preset and watch this PID -- the wrapper's ``exec`` makes it the game session's
PID, so the daemon restores the standing profile when the game exits.

Everything here soft-fails. A daemon problem must never block a game launch.

Which launcher started the game only decides where the setting is read from,
so that is a one-line lookup rather than a module per launcher. Steam keeps its
own resolver because its identity arrives in the environment and is keyed by
account.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from drivers.nvidia.daemon_gpu import DaemonGpuClient
from overlay.wrapper_tokens import split_game_key
from profiles.game_profile import (
    GameProfileSetting,
    game_gpu_target,
    profile_argv_for_setting,
)

from .game_settings import LauncherGameSetting


def profile_argv(setting: GameProfileSetting) -> list[str] | None:
    """The daemon request this preset means on today's hardware, or None."""
    try:
        identities = list(DaemonGpuClient.discover_identities())
    except Exception:
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
    except Exception as error:
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
