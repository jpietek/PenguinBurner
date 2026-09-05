"""Wrapper-side per-game profile apply.

Runs inside the PENGUIN_BURNER launch wrapper, before it execs the game:
resolve the launching game (``SteamAppId``) and account (``SteamUser``) to
the stored preset, then ask the root daemon to apply it and watch this PID —
the wrapper's ``exec`` makes it the game session's PID, so the daemon can
restore the standing profile when the game exits. Everything here soft-fails:
a daemon problem must never block a game launch.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from drivers.nvidia.daemon_gpu import DaemonGpuClient
from profiles.game_profile import game_gpu_target, profile_argv_for_setting

from .settings import steam_game_setting
from .users import list_steam_users


APP_ID_ENV_VARS = ("SteamAppId", "STEAM_COMPAT_APP_ID", "SteamGameId")
ACCOUNT_NAME_ENV_VARS = ("SteamUser", "SteamAppUser")


def game_app_id(env: dict[str, str]) -> str:
    for key in APP_ID_ENV_VARS:
        value = str(env.get(key) or "").strip()
        if value.isdigit():
            return value
    return ""


def game_account_id(env: dict[str, str], *, home: Path | None = None) -> str:
    """The launching Steam account: match the login name Steam puts in env."""
    users = list_steam_users(home)
    for key in ACCOUNT_NAME_ENV_VARS:
        name = str(env.get(key) or "").strip()
        if not name:
            continue
        for user in users:
            if user.account_name == name:
                return user.account_id
    return users[0].account_id if users else ""








def game_runtime_profile_argv(
    env: dict[str, str],
    *,
    home: Path | None = None,
    settings_path: str | Path | None = None,
) -> tuple[list[str], str] | None:
    app_id = game_app_id(env)
    if not app_id:
        return None
    account_id = game_account_id(env, home=home)
    if not account_id:
        return None
    setting = steam_game_setting(account_id, app_id, path=settings_path)
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
    return argv, app_id


def apply_game_runtime_profile(
    env: dict[str, str],
    *,
    home: Path | None = None,
    settings_path: str | Path | None = None,
    watch_pid: int | None = None,
) -> bool:
    resolved = game_runtime_profile_argv(env, home=home, settings_path=settings_path)
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


def main(argv: list[str] | None = None) -> int:
    """Apply a Steam game profile for a host wrapper PID.

    The Flatpak-generated host wrapper runs this module inside the sandbox
    before it execs Steam's real game command. The root daemon watches the
    host wrapper PID, which remains stable across that exec and therefore
    identifies the complete Proton/game session.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--watch-pid", required=True, type=int)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--account-name", default="")
    args = parser.parse_args(argv)
    if args.watch_pid <= 0 or not str(args.app_id).isdigit():
        print(
            "penguin-burner: invalid Steam game runtime request",
            file=sys.stderr,
        )
        return 2

    env = dict(os.environ)
    env["SteamAppId"] = str(args.app_id)
    if args.account_name:
        env["SteamUser"] = str(args.account_name)
    try:
        apply_game_runtime_profile(env, watch_pid=args.watch_pid)
    except Exception as error:
        # The wrapper must never trade a game launch for profile automation.
        print(
            f"penguin-burner: per-game profile apply skipped: {error}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
