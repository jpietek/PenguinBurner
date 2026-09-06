from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import pwd
import shutil
import sys
from typing import Mapping

from auto_uv.scan_mode.auto_uv_mode import (
    ADAPTIVE_TIER_MODES,
    ADAPTIVE_TIER_OPTION_SUFFIXES,
    adaptive_tier_option_key,
)
from ui.constants import DEFAULT_FINAL_VERIFICATION_DURATION_S
from ui.features.tuning.gpu_selection import runtime_gpu_index


FLATPAK_INFO_PATH = Path("/.flatpak-info")
FLATPAK_APP_ID = "io.github.jpietek.PenguinBurner"


def running_in_flatpak() -> bool:
    return FLATPAK_INFO_PATH.is_file()


def cli_base_command() -> list[str]:
    script_path = Path(__file__).resolve().parents[1] / "penguin_burner.py"
    if script_path.is_file():
        return [sys.executable, str(script_path)]
    return ["penguin-burner-cli"]


def host_cli_base_command() -> list[str]:
    override = os.environ.get("PENGUIN_BURNER_HOST_CLI", "").strip()
    if override:
        return [override]
    if running_in_flatpak():
        flatpak = shutil.which("flatpak") or "/usr/bin/flatpak"
        return [
            flatpak,
            "run",
            "--user",
            "--command=penguin-burner-cli",
            os.environ.get("FLATPAK_ID", "").strip() or FLATPAK_APP_ID,
        ]
    return ["penguin-burner-cli"]


def _desktop_user_name() -> str:
    return (
        os.environ.get("PENGUIN_BURNER_Q2RTX_USER", "").strip()
        or os.environ.get("SUDO_USER", "").strip()
        or os.environ.get("USER", "").strip()
        or os.environ.get("LOGNAME", "").strip()
    )


def desktop_user_env() -> list[str]:
    user = _desktop_user_name()
    uid = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_UID", "").strip()
        or os.environ.get("SUDO_UID", "").strip()
    )
    gid = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_GID", "").strip()
        or os.environ.get("SUDO_GID", "").strip()
    )
    if not user and os.getuid() != 0:
        try:
            user = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            user = ""
    if not uid and os.getuid() != 0:
        uid = str(os.getuid())
    if not gid and os.getgid() != 0:
        gid = str(os.getgid())

    values = []
    if user:
        values.append(f"PENGUIN_BURNER_Q2RTX_USER={user}")
        values.append(f"SUDO_USER={user}")
    if uid:
        values.append(f"PENGUIN_BURNER_Q2RTX_UID={uid}")
        values.append(f"SUDO_UID={uid}")
    if gid:
        values.append(f"PENGUIN_BURNER_Q2RTX_GID={gid}")
        values.append(f"SUDO_GID={gid}")
    return values


def desktop_session_env() -> list[str]:
    names = [
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_CURRENT_DESKTOP",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    ]
    values = []
    for name in names:
        if running_in_flatpak() and name == "DBUS_SESSION_BUS_ADDRESS":
            value = _host_session_bus_address()
        else:
            value = os.environ.get(name, "").strip()
        if value:
            values.append(f"{name}={value}")
    if os.environ.get("DISPLAY", "").strip() and not os.environ.get(
        "XAUTHORITY", ""
    ).strip():
        xauthority = _default_xauthority_path()
        if xauthority:
            values.append(f"XAUTHORITY={xauthority}")
    return values


def _default_xauthority_path() -> str:
    user = _desktop_user_name()
    home = ""
    if user:
        try:
            home = pwd.getpwnam(user).pw_dir
        except KeyError:
            home = ""
    if not home:
        return ""
    path = Path(home) / ".Xauthority"
    return str(path) if path.is_file() else ""


def _host_session_bus_address() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir.startswith("/run/user/"):
        return f"unix:path={runtime_dir}/bus"
    uid = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_UID", "").strip()
        or os.environ.get("SUDO_UID", "").strip()
    )
    if not uid and os.getuid() != 0:
        uid = str(os.getuid())
    return f"unix:path=/run/user/{uid}/bus" if uid else ""


def _privileged_command_base() -> list[str] | None:
    if os.geteuid() == 0:
        return []
    if running_in_flatpak():
        flatpak_spawn = shutil.which("flatpak-spawn")
        if flatpak_spawn:
            return [flatpak_spawn, "--host", "/usr/bin/pkexec", "/usr/bin/env"]
        return None
    escalator = shutil.which("pkexec") or shutil.which("sudo")
    if not escalator:
        return None
    env = shutil.which("env") or "/usr/bin/env"
    return [escalator, env]


def _privileged_env() -> list[str]:
    values = []
    if running_in_flatpak():
        values.extend(_host_flatpak_user_env())
        values.append(_host_path_assignment())
        pythonpath = _host_pythonpath_assignment()
        if pythonpath:
            values.append(pythonpath)
    return [*values, *desktop_user_env(), *desktop_session_env()]


def _host_flatpak_user_env() -> list[str]:
    home = _desktop_user_home()
    if not home:
        return []
    return [
        f"HOME={home}",
        f"XDG_DATA_HOME={home}/.local/share",
    ]


def _desktop_user_home() -> str:
    override = os.environ.get("PENGUIN_BURNER_HOME", "").strip()
    if override:
        return str(Path(override).expanduser())
    user = _desktop_user_name()
    if user:
        try:
            return pwd.getpwnam(user).pw_dir
        except KeyError:
            pass
    uid = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_UID", "").strip()
        or os.environ.get("SUDO_UID", "").strip()
    )
    if uid:
        try:
            return pwd.getpwuid(int(uid)).pw_dir
        except (KeyError, ValueError):
            pass
    home = str(Path.home())
    return home if home and home != "/" else ""


def _host_path_assignment() -> str:
    entries = []
    home = (_desktop_user_home() if running_in_flatpak() else str(Path.home())).strip()
    if home and home != "/":
        entries.append(str(Path(home) / ".local" / "bin"))
    entries.extend(["/usr/local/bin", "/usr/bin", "/bin"])
    for item in os.environ.get("PATH", "").split(os.pathsep):
        item = item.strip()
        if item and not item.startswith("/app") and item not in entries:
            entries.append(item)
    return "PATH=" + os.pathsep.join(entries)


def _host_pythonpath_assignment() -> str:
    entries = []
    home = Path.home()
    user_lib = home / ".local" / "lib"
    if user_lib.is_dir():
        entries.extend(
            str(path)
            for path in sorted(user_lib.glob("python*/site-packages"), reverse=True)
            if path.is_dir()
        )
    for item in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        item = item.strip()
        if item and not item.startswith("/app") and item not in entries:
            entries.append(item)
    return "PYTHONPATH=" + os.pathsep.join(entries) if entries else ""


def _privileged_command(command: list[str]) -> list[str]:
    base = _privileged_command_base()
    if base is None:
        return list(command)
    if not base:
        return list(command)
    return [*base, *_privileged_env(), *command]


def scan_command(auto_uv_options: Mapping[str, object] | None = None) -> list[str]:
    options = auto_uv_options or {}
    payload = {
        "gpu_index": options.get("gpu_index", runtime_gpu_index()),
    }
    option_keys = (
        "auto_uv_mode",
        "auto_uv_min_voltage_mv",
        "auto_uv_memory_offset_mhz",
        "auto_uv_power_limit_w",
        "auto_uv_tail_rise_bins",
        "auto_oc_target_voltage_mv",
        "auto_oc_target_clock_mhz",
        # Per-tier full-scan overrides (adaptive mode only), derived from the
        # canonical tier/option constants so a new option cannot be silently
        # dropped from the daemon payload.
        *(
            adaptive_tier_option_key(tier, suffix)
            for tier in ADAPTIVE_TIER_MODES
            for suffix in ADAPTIVE_TIER_OPTION_SUFFIXES
        ),
    )
    for key in option_keys:
        value = options.get(key)
        if value in (None, ""):
            continue
        payload[key] = value
    return [
        sys.executable,
        "-m",
        "runtime.daemon_client",
        "start-auto-uv",
        json.dumps(payload, separators=(",", ":")),
    ]


def privileged_command(command: list[str]) -> list[str]:
    if running_in_flatpak():
        command = _host_equivalent_command(command)
    return _privileged_command(command)


def _host_equivalent_command(command: list[str]) -> list[str]:
    command = list(command)
    local_base = cli_base_command()
    if command[: len(local_base)] == local_base:
        return [*host_cli_base_command(), *command[len(local_base) :]]
    return command


def daemon_migration_command() -> list[str]:
    if running_in_flatpak():
        return _flatpak_daemon_install_command(autostart_intent=None)
    return privileged_command([*cli_base_command(), "--migrate-to-daemon-service"])


def runtime_profile_command(
    action: str,
    *,
    profile_selector: str = "",
    silent_fan_curve: bool = False,
    adaptive_auto_uv: bool = False,
    gpu_index: int | None = None,
    persist_on_startup: bool,
) -> list[str]:
    # Apply always targets the already-root daemon. Boot persistence follows
    # the user's "Apply on startup" toggle: on, the applied runtime is saved
    # as the boot profile; off, the apply is session-only and any saved boot
    # profile is cleared so boot state always matches the visible toggle.
    # Restore defaults keeps persisting the stock runtime for boot.
    if action != "daemonize":
        raise ValueError(f"unknown runtime profile action: {action}")
    intent = {
        "profile_selector": str(profile_selector or "").strip(),
        "silent_fan_curve": bool(silent_fan_curve),
        "adaptive_auto_uv": bool(adaptive_auto_uv),
        "gpu_index": None if gpu_index is None else max(0, int(gpu_index)),
    }
    return [
        sys.executable,
        "-m",
        "runtime.daemon_client",
        "apply-runtime-intent",
        "--boot" if persist_on_startup else "--clear-boot",
        json.dumps(intent, separators=(",", ":")),
    ]


def _flatpak_daemon_install_command(
    *, autostart_intent: dict | None
) -> list[str]:
    """Elevated host-side install of the Rust daemon from a Flatpak sandbox.

    The manifest builds penguin-burnerd into /app/libexec (host-visible inside
    the flatpak deployment dir); the one pkexec elevation copies it onto the
    canonical root-owned host path, drops the generated unit into
    /etc/systemd/system, and enables+restarts it. The source deployment is
    never an ExecStart target.

    ``autostart_intent`` is None for migration/repair, an empty dict for no boot
    runtime, or a semantic intent that the newly installed daemon resolves and
    persists through its typed API.
    """
    from runtime.support.flatpak_daemon_install import (
        build_flatpak_daemon_install_script,
    )
    from runtime.support.runtime_service import (
        build_daemon_api_service_unit,
        flatpak_host_app_path,
        flatpak_host_cli_program_file,
        flatpak_host_site_packages_path,
    )

    program_file = flatpak_host_cli_program_file()
    unit = build_daemon_api_service_unit(program_file)
    encoded_unit = base64.b64encode(unit.encode("utf-8")).decode("ascii")
    daemon_binary_src = flatpak_host_app_path() / "libexec" / "penguin-burnerd"

    environment = [
        f"PENGUIN_BURNER_DAEMON_BINARY_SRC={daemon_binary_src}",
        f"PENGUIN_BURNER_SYSTEMD_UNIT_B64={encoded_unit}",
        f"PENGUIN_BURNER_RUNTIME_PYTHONPATH={flatpak_host_site_packages_path()}",
        f"PENGUIN_BURNER_RUNTIME_HOME={_desktop_user_home()}",
    ]
    runtime_action = "none"
    if autostart_intent:
        intent_json = json.dumps(autostart_intent, separators=(",", ":"))
        environment.append(
            "PENGUIN_BURNER_RUNTIME_INTENT_B64="
            + base64.b64encode(intent_json.encode("utf-8")).decode("ascii")
        )
        runtime_action = "apply-intent"
    elif autostart_intent is None:
        runtime_action = "migrate-legacy"

    script = build_flatpak_daemon_install_script(
        runtime_action=runtime_action,
    )
    return _privileged_command(
        [
            *environment,
            "/bin/sh",
            "-eu",
            "-c",
            script,
            "penguin-burner-daemon-install",
        ]
    )


def profile_verify_command(
    *,
    profile_selector: str = "",
    duration_s: int = DEFAULT_FINAL_VERIFICATION_DURATION_S,
    stop_request_path: str | Path = "",
    gpu_index: int | None = None,
) -> list[str]:
    # Verification runs inside the already-root daemon (streaming socket RPC,
    # no pkexec). The daemon owns the child's --stability-stop-request-file
    # marker (same <config>/profile-verify-stop-requested path the UI writes
    # for a cooperative stop), so stop_request_path is accepted for caller
    # compatibility but not forwarded.
    del stop_request_path
    payload: dict[str, object] = {
        "stability_seconds": max(1, int(duration_s)),
        "gpu_index": runtime_gpu_index() if gpu_index is None else max(0, int(gpu_index)),
    }
    if profile_selector:
        payload["auto_uv_profile"] = str(profile_selector)
    return [
        sys.executable,
        "-m",
        "runtime.daemon_client",
        "start-profile-verification",
        json.dumps(payload, separators=(",", ":")),
    ]


def delete_profiles_command(profile_paths: list[str]) -> list[str]:
    # Profile deletion goes through the root daemon (unary RPC, no pkexec);
    # the daemon path-validates against the saved-profiles dir before deleting.
    return [
        sys.executable,
        "-m",
        "runtime.daemon_client",
        "delete-auto-uv-profiles",
        json.dumps([str(path) for path in profile_paths], separators=(",", ":")),
    ]
