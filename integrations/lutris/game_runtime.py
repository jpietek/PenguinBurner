"""Wrapper-side per-game profile apply for Lutris launches.

Runs inside the PENGUIN_BURNER wrapper, before it execs the game. The Steam
counterpart sniffs the launching game out of the environment; here the identity
arrives as the ``--pb-lutris-id`` flag the Lutris tab wrote into
``prefix_command``, because Lutris regenerates its own per-launch UUID and
publishes nothing stable.

Everything here soft-fails: a daemon problem must never block a game launch.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from drivers.nvidia.daemon_gpu import DaemonGpuClient
from overlay.wrapper_tokens import LUTRIS_GAME_ID_ENV
from profiles.game_profile import game_gpu_target, profile_argv_for_setting

from .settings import lutris_game_setting

# Steam app ids and Lutris game ids are both bare integers, and the daemon
# keys the running-game registry by one opaque string. Namespacing keeps
# Lutris game 27 from colliding with Steam app 27.
LUTRIS_APP_ID_PREFIX = "lutris:"


def lutris_app_id(game_id: str) -> str:
    """The daemon-facing id for a Lutris game."""
    value = str(game_id or "").strip()
    return f"{LUTRIS_APP_ID_PREFIX}{value}" if value else ""


def game_id_from_env(env: dict[str, str]) -> str:
    value = str(env.get(LUTRIS_GAME_ID_ENV) or "").strip()
    return value if value.isdigit() else ""


def lutris_runtime_profile_argv(
    env: dict[str, str],
    *,
    settings_path: str | Path | None = None,
) -> tuple[list[str], str] | None:
    game_id = game_id_from_env(env)
    if not game_id:
        return None
    setting = lutris_game_setting(game_id, path=settings_path)
    if setting is None:
        return None
    try:
        identities = list(DaemonGpuClient.discover_identities())
    except Exception:
        return None
    target = game_gpu_target(setting, identities)
    if target is None:
        return None
    gpu_uuid, gpu_index = target
    argv = profile_argv_for_setting(
        setting,
        gpu_index=gpu_index,
        gpu_uuid=gpu_uuid,
        include_legacy_profiles=len(identities) == 1,
    )
    if argv is None:
        return None
    return argv, lutris_app_id(game_id)


def apply_lutris_game_runtime_profile(
    env: dict[str, str],
    *,
    settings_path: str | Path | None = None,
    watch_pid: int | None = None,
) -> bool:
    resolved = lutris_runtime_profile_argv(env, settings_path=settings_path)
    if resolved is None:
        return False
    argv, app_id = resolved
    from runtime.daemon_client import start_game_runtime_profile

    try:
        result = start_game_runtime_profile(
            argv,
            watch_pid=os.getpid() if watch_pid is None else int(watch_pid),
            app_id=app_id,
            timeout_s=45.0,
        )
    except Exception as error:
        print(
            f"penguin-burner: per-game profile apply skipped: {error}",
            file=sys.stderr,
        )
        return False
    if isinstance(result, dict) and (
        bool(result.get("ignored")) or not bool(result.get("started", True))
    ):
        reason = str(result.get("reason") or "daemon did not start the profile")
        print(
            f"penguin-burner: per-game profile apply skipped: {reason}",
            file=sys.stderr,
        )
        return False
    return True
