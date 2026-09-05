"""Steam client process control for the fallback write path and init flow."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import time


_STEAM_LAUNCH_APPID_RE = re.compile(r"SteamLaunch AppId=(\d+)")


FLATPAK_INFO_PATH = Path("/.flatpak-info")
HOST_PGREP = "/usr/bin/pgrep"
HOST_SHELL = "/usr/bin/sh"
HOST_WORKING_DIRECTORY = "/tmp"


def running_in_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID", "").strip()) or FLATPAK_INFO_PATH.is_file()


def _flatpak_host_command(command: list[str]) -> list[str] | None:
    flatpak_spawn = shutil.which("flatpak-spawn")
    if not flatpak_spawn:
        return None
    # flatpak-spawn otherwise mirrors the sandbox cwd. App-only paths such as
    # /app do not exist on the host, so the portal rejects the command before
    # Steam ever sees it. A neutral host directory makes every caller
    # independent of how the Flatpak itself was launched.
    return [
        flatpak_spawn,
        "--host",
        f"--directory={HOST_WORKING_DIRECTORY}",
        *command,
    ]


def _steam_executable() -> str | None:
    if not running_in_flatpak():
        return shutil.which("steam")
    command = _flatpak_host_command([HOST_SHELL, "-c", "command -v steam"])
    if command is None:
        return None
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    candidate = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    return candidate if candidate.startswith("/") else None


def _steam_command(*args: str) -> list[str] | None:
    executable = _steam_executable()
    if executable is None:
        return None
    command = [executable, *args]
    return _flatpak_host_command(command) if running_in_flatpak() else command


def _pgrep(*args: str) -> bool:
    # Flatpak has its own PID namespace, so query the unprivileged host
    # session through flatpak-spawn instead of looking inside the sandbox.
    command = [HOST_PGREP, *args]
    if running_in_flatpak():
        command = _flatpak_host_command(command) or []
    if not command:
        return False
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def steam_running() -> bool:
    # ~/.steam/steam.pid goes stale after exit; a live process check is the
    # only reliable signal.
    return _pgrep("-x", "steam")


def running_steam_game_ids() -> frozenset[str] | None:
    """App ids of every Steam game session alive right now, in ONE subprocess.

    A single ``pgrep -af`` returns all reaper command lines; parsing them out
    means a poller checks once per tick regardless of how many games it is
    tracking, instead of one subprocess per game. The ``[S]`` class keeps the
    query's own command line (which contains the pattern) from matching.

    Returns a (possibly empty) set of app ids on success, or ``None`` when the
    check itself failed (timeout / no host bridge) so a caller can distinguish
    "no games running" from "couldn't tell" instead of misreading a stalled
    probe as every game having exited.
    """
    command = [HOST_PGREP, "-af", r"[S]teamLaunch AppId="]
    if running_in_flatpak():
        command = _flatpak_host_command(command) or []
    if not command:
        return None
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # returncode 1 = ran fine, no matches (empty set); anything but 0/1 = the
    # check failed and the result is not trustworthy.
    if result.returncode not in (0, 1):
        return None
    ids: set[str] = set()
    for line in result.stdout.splitlines():
        match = _STEAM_LAUNCH_APPID_RE.search(line)
        if match:
            ids.add(match.group(1))
    return frozenset(ids)


def steam_available() -> bool:
    return _steam_executable() is not None


def shutdown_steam(*, timeout_s: float = 30.0) -> bool:
    """Ask Steam to exit cleanly and wait until the process is gone."""
    if not steam_running():
        return True
    command = _steam_command("-shutdown")
    if command is None:
        return False
    try:
        subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not steam_running():
            return True
        time.sleep(0.5)
    return not steam_running()


def launch_steam(*, silent: bool = True) -> bool:
    command = _steam_command(*(["-silent"] if silent else []))
    if command is None:
        return False
    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


def restart_steam(*, shutdown_timeout_s: float = 30.0, silent: bool = True) -> bool:
    if not shutdown_steam(timeout_s=shutdown_timeout_s):
        return False
    return launch_steam(silent=silent)


def launch_steam_game(app_id: str) -> bool:
    """Ask the running Steam client to launch a game (detached)."""
    if not str(app_id).isdigit():
        return False
    command = _steam_command("-applaunch", str(app_id))
    if command is None:
        return False
    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True
