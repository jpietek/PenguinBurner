"""Command lines the desktop app runs through the root daemon (runtime.daemon_commands)."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import runtime.daemon_commands as commands

FLATPAK_APP_PATH = (
    "/home/desktop-user/.local/share/flatpak/app/"
    "io.github.jpietek.PenguinBurner/current/active/files"
)
FLATPAK_SITE_PACKAGES = f"{FLATPAK_APP_PATH}/lib/python3.13/site-packages"


def _scan_daemon_options(command: list[str]) -> dict:
    assert command[1:4] == ["-m", "runtime.daemon_client", "start-auto-uv"]
    return json.loads(command[4])


def _runtime_profile_daemon_intent(command: list[str]) -> dict:
    assert command[1:4] == ["-m", "runtime.daemon_client", "apply-runtime-intent"]
    return json.loads(command[-1])


def _assert_flatpak_daemon_script_waits_for_api(
    script: str,
    *,
    success_message: str,
    clears_last_runtime: bool = True,
) -> None:
    assert "daemon_socket=/run/penguin-burnerd.sock" in script
    assert 'client.sendall(b\'{"method":"status"}\\n\')' in script
    assert 'rm -f "$daemon_socket"' in script
    state_clear = 'rm -f "/var/lib/penguin-burner/last-runtime.json"'
    if clears_last_runtime:
        assert state_clear in script
        assert script.rindex("restart_penguin_burnerd") < script.rindex(
            state_clear
        ) < script.rindex("commit_daemon_install")
    else:
        # Migration consumes the legacy 0.6.x state via
        # migrate-legacy-boot-intent instead of deleting it up front.
        assert state_clear not in script
    # The user's 0.7 boot profile must survive a repair/reinstall.
    assert "boot-runtime.json" not in script
    assert "restart_penguin_burnerd" in script
    assert "systemctl status --no-pager penguin-burnerd.service" in script
    assert "journalctl -u penguin-burnerd.service -n 80 --no-pager" in script
    assert script.rindex("restart_penguin_burnerd") < script.index(success_message)


def _assert_flatpak_daemon_binary_installed_atomically(script: str) -> None:
    stage = 'daemon_tmp="$(mktemp "$daemon_dir/.penguin-burnerd.XXXXXX")"'
    copy = 'dd if="$PENGUIN_BURNER_DAEMON_BINARY_SRC" of="$daemon_tmp"'
    commit = 'mv -f -- "$daemon_tmp" "$daemon_target"'
    legacy_mutation = "systemctl disable --now PenguinBurner.service"
    assert stage in script
    assert copy in script
    assert "iflag=nofollow,fullblock" in script
    assert "nonblock" in script
    assert "oflag=nofollow" in script
    assert "conv=fsync" in script
    assert '[ ! -f "$PENGUIN_BURNER_DAEMON_BINARY_SRC" ]' in script
    assert '[ -L "$PENGUIN_BURNER_DAEMON_BINARY_SRC" ]' in script
    assert "7f454c46" in script
    assert 'chown root:root "$daemon_tmp"' in script
    assert 'chmod 0755 "$daemon_tmp"' in script
    assert commit in script
    assert "install -Dm0755" not in script
    assert "trap finish_daemon_install EXIT" in script
    assert "trap 'exit 1' HUP INT TERM" in script
    # The staged binary must prove it can execute on this host (the Flatpak
    # daemon links against the freedesktop runtime's glibc floor) before any
    # installed state is touched.
    preflight = '"$daemon_tmp" --version'
    assert preflight in script
    assert "cannot run on this host" in script
    assert (
        script.index('chmod 0755 "$daemon_tmp"')
        < script.index(preflight)
        < script.index(commit)
    )
    # Rollback must tolerate units that do not exist: a bare `systemctl
    # disable` of the usually-absent legacy unit reported every rollback as
    # failed.
    assert 'systemctl cat -- "$1"' in script
    assert "disable_present_service penguin-burnerd.service || rollback_failed=1" in script
    assert "disable_present_service PenguinBurner.service || rollback_failed=1" in script
    assert script.index(stage) < script.index(copy) < script.index(commit)
    assert script.index(commit) < script.rindex(legacy_mutation)


def test_flatpak_systemd_transaction_is_owned_by_runtime_support() -> None:
    commands_source = Path("runtime/daemon_commands.py").read_text(encoding="utf-8")
    runtime_source = Path(
        "runtime/support/flatpak_daemon_install.py"
    ).read_text(encoding="utf-8")

    assert "rollback_daemon_install" not in commands_source
    assert "def build_flatpak_daemon_install_script" in runtime_source
    assert "rollback_daemon_install" in runtime_source


def test_ui_scan_command_uses_daemon_client_without_pkexec(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(commands.os, "getuid", lambda: 1000)
    monkeypatch.setattr(commands.os, "getgid", lambda: 1000)
    monkeypatch.setenv("USER", "desktop-user")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    monkeypatch.setattr(commands, "runtime_gpu_index", lambda: 0)

    def fake_which(name: str) -> str | None:
        return {
            "pkexec": "/usr/bin/pkexec",
            "sudo": "/usr/bin/sudo",
            "env": "/usr/bin/env",
        }.get(name)

    monkeypatch.setattr(commands.shutil, "which", fake_which)

    command = commands.scan_command()
    options = _scan_daemon_options(command)

    assert "/usr/bin/pkexec" not in command
    assert "/usr/bin/sudo" not in command
    assert options["gpu_index"] == commands.runtime_gpu_index()


def test_ui_scan_command_uses_flatpak_host_privilege(
    monkeypatch,
    tmp_path,
) -> None:
    flatpak_info = tmp_path / ".flatpak-info"
    flatpak_info.write_text("[Application]\n", encoding="utf-8")
    monkeypatch.setattr(commands, "FLATPAK_INFO_PATH", flatpak_info)
    monkeypatch.setattr(commands.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(commands.os, "getuid", lambda: 1000)
    monkeypatch.setattr(commands.os, "getgid", lambda: 1000)
    monkeypatch.setenv("USER", "desktop-user")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/flatpak/bus")

    def fake_which(name: str) -> str | None:
        return {
            "flatpak-spawn": "/usr/bin/flatpak-spawn",
        }.get(name)

    monkeypatch.setattr(commands.shutil, "which", fake_which)

    command = commands.scan_command({"gpu_index": 0})
    options = _scan_daemon_options(command)

    assert "/usr/bin/flatpak-spawn" not in command
    assert "/usr/bin/pkexec" not in command
    assert options["gpu_index"] == 0


def _flatpak_daemon_install_env(monkeypatch, tmp_path) -> None:
    """Environment for building the elevated flatpak daemon-install command.

    The host home this command carries is meant to resolve from USER through
    the getpwnam stub below. PENGUIN_BURNER_HOME outranks both, so the helper
    clears it and states the environment under test outright, instead of
    depending on whichever home the run happened to inherit.
    """
    monkeypatch.delenv("PENGUIN_BURNER_HOME", raising=False)
    flatpak_info = tmp_path / ".flatpak-info"
    flatpak_info.write_text("[Application]\n", encoding="utf-8")
    monkeypatch.setattr(commands, "FLATPAK_INFO_PATH", flatpak_info)
    monkeypatch.setattr(commands.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(commands.os, "getuid", lambda: 1000)
    monkeypatch.setattr(commands.os, "getgid", lambda: 1000)
    monkeypatch.setenv("FLATPAK_ID", "io.github.jpietek.PenguinBurner")
    monkeypatch.setenv("PENGUIN_BURNER_FLATPAK_APP_PATH", FLATPAK_APP_PATH)
    monkeypatch.setenv("PENGUIN_BURNER_FLATPAK_SITE_PACKAGES", FLATPAK_SITE_PACKAGES)
    monkeypatch.setenv("USER", "desktop-user")
    monkeypatch.setattr(
        commands.pwd,
        "getpwnam",
        lambda user: SimpleNamespace(pw_dir=f"/home/{user}"),
    )

    def fake_which(name: str) -> str | None:
        return {
            "flatpak-spawn": "/usr/bin/flatpak-spawn",
            "flatpak": "/usr/bin/flatpak",
        }.get(name)

    monkeypatch.setattr(commands.shutil, "which", fake_which)


def _command_env_value(command: list[str], name: str) -> str:
    prefix = f"{name}="
    values = [part[len(prefix) :] for part in command if part.startswith(prefix)]
    assert len(values) == 1, f"expected exactly one {name} assignment"
    return values[0]


def test_daemon_migration_command_installs_flatpak_daemon(
    monkeypatch, tmp_path
) -> None:
    # The sandbox-built /app/libexec source is copied to the one canonical host
    # runtime path by the explicit pkexec repair transaction.
    _flatpak_daemon_install_env(monkeypatch, tmp_path)

    command = commands.daemon_migration_command()

    assert command[:4] == [
        "/usr/bin/flatpak-spawn",
        "--host",
        "/usr/bin/pkexec",
        "/usr/bin/env",
    ]
    assert _command_env_value(command, "PENGUIN_BURNER_DAEMON_BINARY_SRC") == (
        f"{FLATPAK_APP_PATH}/libexec/penguin-burnerd"
    )
    script = command[command.index("-c") + 1]
    syntax = subprocess.run(
        ["/bin/sh", "-n"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr
    assert "/usr/bin/flatpak" not in command
    assert command[-5:] == [
        "/bin/sh",
        "-eu",
        "-c",
        command[-2],
        "penguin-burner-daemon-install",
    ]
    assert "systemctl is-active --quiet penguin-burnerd.service" in command[-2]
    assert "systemctl enable penguin-burnerd.service" in command[-2]
    assert "systemctl restart penguin-burnerd.service" in command[-2]
    assert "systemctl enable --now penguin-burnerd.service" not in command[-2]
    _assert_flatpak_daemon_script_waits_for_api(
        command[-2],
        success_message='echo "Installed and enabled penguin-burnerd.service at $unit."',
        clears_last_runtime=False,
    )
    _assert_flatpak_daemon_binary_installed_atomically(script)
    assert "daemon_dir=/var/opt/penguin-burner/libexec" in script
    assert (
        'daemon_target="/var/opt/penguin-burner/libexec/penguin-burnerd"'
        in script
    )
    assert "PENGUIN_BURNER_DAEMON_BINARY_TARGET" not in " ".join(command)
    assert "findmnt" in script and "noexec" in script
    assert "selinuxenabled" in script and "restorecon" in script
    assert "rollback_daemon_install" in script
    # Rollback disables only units that exist; a bare `systemctl disable` of
    # the usually-absent legacy unit reported every rollback as failed.
    assert "disable_present_service penguin-burnerd.service || rollback_failed=1" in script
    assert "disable_present_service PenguinBurner.service || rollback_failed=1" in script
    assert script.index("restart_penguin_burnerd\n") < script.rindex(
        "commit_daemon_install"
    )
    assert "systemctl enable penguin-burnerd.service" in script
    # Migration replays a 0.6.x boot profile host-side after the new daemon
    # is up; it passes no client-built intent of its own.
    assert "migrate-legacy-boot-intent" in script
    assert script.index("restart_penguin_burnerd\n") < script.index(
        "migrate-legacy-boot-intent"
    )
    assert "PENGUIN_BURNER_RUNTIME_INTENT_B64" not in " ".join(command)
    unit = base64.b64decode(
        _command_env_value(command, "PENGUIN_BURNER_SYSTEMD_UNIT_B64")
    ).decode("utf-8")
    assert "/usr/bin/flatpak" not in unit
    assert f"Environment=PYTHONPATH={FLATPAK_SITE_PACKAGES}" in unit
    assert (
        "ExecStart=/var/opt/penguin-burner/libexec/penguin-burnerd "
        "--socket /run/penguin-burnerd.sock"
        in unit
    )


def test_daemon_migration_command_replays_legacy_boot_intent_host_side(
    monkeypatch, tmp_path
) -> None:
    """The 0.6.x last-runtime.json lives under host /var/lib, which the
    sandbox cannot read: migration must recover it host-side (elevated),
    after the new daemon is reachable, with the host env the intent bridge
    needs."""
    _flatpak_daemon_install_env(monkeypatch, tmp_path)

    command = commands.daemon_migration_command()

    script = command[-2]
    replay = (
        'env PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 '
        'PYTHONPATH="$PENGUIN_BURNER_RUNTIME_PYTHONPATH" '
        'PENGUIN_BURNER_HOME="$PENGUIN_BURNER_RUNTIME_HOME" '
        "/usr/bin/python3 -m runtime.daemon_client migrate-legacy-boot-intent"
    )
    assert replay in script
    assert _command_env_value(command, "PENGUIN_BURNER_RUNTIME_PYTHONPATH") == (
        FLATPAK_SITE_PACKAGES
    )
    assert _command_env_value(command, "PENGUIN_BURNER_RUNTIME_HOME") == (
        "/home/desktop-user"
    )
    # The legacy file is consumed by the replay helper, never rm'd up front,
    # and an existing 0.7 boot spec is preserved.
    assert "last-runtime.json" not in script
    assert "boot-runtime.json" not in script


def test_flatpak_runtime_profile_daemonize_uses_daemon_client(
    monkeypatch,
    tmp_path,
) -> None:
    flatpak_info = tmp_path / ".flatpak-info"
    flatpak_info.write_text("[Application]\n", encoding="utf-8")
    monkeypatch.setattr(commands, "FLATPAK_INFO_PATH", flatpak_info)
    monkeypatch.setattr(commands.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("FLATPAK_ID", "io.github.jpietek.PenguinBurner")
    monkeypatch.setenv("PENGUIN_BURNER_FLATPAK_APP_PATH", FLATPAK_APP_PATH)
    monkeypatch.setenv("PENGUIN_BURNER_FLATPAK_SITE_PACKAGES", FLATPAK_SITE_PACKAGES)
    monkeypatch.setenv("USER", "desktop-user")
    monkeypatch.setattr(
        commands.pwd,
        "getpwnam",
        lambda user: SimpleNamespace(pw_dir=f"/home/{user}"),
    )

    def fake_which(name: str) -> str | None:
        return {
            "flatpak-spawn": "/usr/bin/flatpak-spawn",
            "flatpak": "/usr/bin/flatpak",
        }.get(name)

    monkeypatch.setattr(commands.shutil, "which", fake_which)

    command = commands.runtime_profile_command(
        "daemonize",
        profile_selector="profile-a",
        silent_fan_curve=True,
        gpu_index=0,
    persist_on_startup=True,
    )
    intent = _runtime_profile_daemon_intent(command)

    assert "/usr/bin/systemd-run" not in command
    assert "PenguinBurner" not in command
    assert "/usr/bin/flatpak" not in command
    assert intent == {
        "profile_selector": "profile-a",
        "silent_fan_curve": True,
        "adaptive_auto_uv": False,
        "gpu_index": 0,
    }


def test_daemon_migration_command_uses_privileged_cli(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(commands, "env_command_prefix", lambda: ["/usr/bin/env"])

    def fake_which(name: str) -> str | None:
        return {
            "pkexec": "/usr/bin/pkexec",
            "env": "/usr/bin/env",
        }.get(name)

    monkeypatch.setattr(commands.shutil, "which", fake_which)

    command = commands.daemon_migration_command()

    assert command[:2] == ["/usr/bin/pkexec", "/usr/bin/env"]
    assert "--migrate-to-daemon-service" in command


def test_daemon_migration_command_dispatches_uutils_env(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        commands,
        "env_command_prefix",
        lambda: ["/usr/bin/uu-coreutils", "env"],
    )
    monkeypatch.setattr(
        commands.shutil,
        "which",
        lambda name: "/usr/bin/pkexec" if name == "pkexec" else None,
    )

    command = commands.daemon_migration_command()

    assert command[:3] == ["/usr/bin/pkexec", "/usr/bin/uu-coreutils", "env"]


def test_desktop_session_env_infers_xauthority_for_x11_forwarding(
    monkeypatch,
    tmp_path,
) -> None:
    xauthority = tmp_path / ".Xauthority"
    xauthority.write_text("cookie", encoding="utf-8")
    monkeypatch.setenv("USER", "desktop-user")
    monkeypatch.setenv("DISPLAY", "localhost:10.0")
    monkeypatch.delenv("XAUTHORITY", raising=False)
    monkeypatch.setattr(
        commands.pwd,
        "getpwnam",
        lambda user: SimpleNamespace(pw_dir=str(tmp_path)),
    )

    env = commands.desktop_session_env()

    assert "DISPLAY=localhost:10.0" in env
    assert f"XAUTHORITY={xauthority}" in env


def test_ui_scan_command_adds_auto_uv_tuning_options(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)
    monkeypatch.setattr(commands, "runtime_gpu_index", lambda: 2)

    command = commands.scan_command(
        {
            "auto_uv_mode": "performance",
            "auto_uv_min_voltage_mv": 850,
            "auto_uv_max_clock_drop_pct": 10.0,
            "auto_uv_memory_offset_mhz": 500,
            "auto_uv_power_limit_w": 390,
            "auto_uv_tail_rise_bins": 2,
            "auto_oc_target_voltage_mv": 925,
            "auto_oc_target_clock_mhz": 2670,
        }
    )
    options = _scan_daemon_options(command)

    assert options["gpu_index"] == 2
    assert options["auto_uv_mode"] == "performance"
    assert options["auto_uv_min_voltage_mv"] == 850
    assert "auto_uv_max_clock_drop_pct" not in options
    assert options["auto_oc_target_voltage_mv"] == 925
    assert options["auto_oc_target_clock_mhz"] == 2670
    assert "--power-limit-override-w" not in command
    assert options["auto_uv_power_limit_w"] == 390
    assert "--yolo" not in command
    assert "--auto-uv-efficiency-stop-streak" not in command
    assert "--auto-uv-min-efficiency-stop-drop-pct" not in command
    assert options["auto_uv_memory_offset_mhz"] == 500
    assert options["auto_uv_tail_rise_bins"] == 2


def test_ui_scan_command_does_not_forward_cli_only_recovery_options(monkeypatch) -> None:
    monkeypatch.setattr(commands, "runtime_gpu_index", lambda: 0)
    options = _scan_daemon_options(commands.scan_command({
        "auto_uv_mode": "adaptive",
        "auto_uv_tuning_mode": "aggressive",
        "auto_uv_retry_after_freeze": True,
    }))
    assert "auto_uv_tuning_mode" not in options
    assert "auto_uv_retry_after_freeze" not in options


def test_ui_scan_command_passes_per_tier_full_scan_options(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)
    monkeypatch.setattr(commands, "runtime_gpu_index", lambda: 0)

    command = commands.scan_command(
        {
            "auto_uv_mode": "adaptive",
            "auto_uv_min_voltage_mv": 850,
            "auto_oc_target_voltage_mv": 925,
            "auto_oc_target_clock_mhz": 2670,
            "auto_uv_efficiency_max_clock_drop_pct": 15.0,
            "auto_uv_efficiency_power_limit_w": 250,
            "auto_uv_efficiency_memory_offset_mhz": 500,
            "auto_uv_balanced_max_clock_drop_pct": 6.0,
            "auto_uv_balanced_power_limit_w": 300,
            "auto_uv_balanced_memory_offset_mhz": 0,
            "auto_uv_performance_max_clock_drop_pct": 5.4,
            "auto_uv_performance_power_limit_w": 360,
            "auto_uv_performance_memory_offset_mhz": 1000,
        }
    )
    options = _scan_daemon_options(command)

    assert options["auto_uv_mode"] == "adaptive"
    assert "auto_uv_efficiency_max_clock_drop_pct" not in options
    assert options["auto_uv_efficiency_power_limit_w"] == 250
    assert options["auto_uv_efficiency_memory_offset_mhz"] == 500
    assert "auto_uv_balanced_max_clock_drop_pct" not in options
    assert options["auto_uv_balanced_power_limit_w"] == 300
    assert options["auto_uv_balanced_memory_offset_mhz"] == 0
    assert "auto_uv_performance_max_clock_drop_pct" not in options
    assert options["auto_uv_performance_power_limit_w"] == 360
    assert options["auto_uv_performance_memory_offset_mhz"] == 1000
    # The full scan carries no scan-wide tuning keys.
    assert "auto_uv_max_clock_drop_pct" not in options
    assert "auto_uv_memory_offset_mhz" not in options
    assert "auto_uv_power_limit_w" not in options


def test_ui_scan_command_can_override_runtime_gpu_index(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)
    monkeypatch.setattr(commands, "runtime_gpu_index", lambda: 2)

    command = commands.scan_command({"gpu_index": 1})
    options = _scan_daemon_options(command)

    assert options["gpu_index"] == 1


def test_ui_profile_delete_command_uses_daemon_client(monkeypatch) -> None:
    # Deletion routes through the already-root daemon: no pkexec, no elevation.
    monkeypatch.setattr(commands.os, "geteuid", lambda: 1000)

    command = commands.delete_profiles_command(["/home/user/profile.json"])

    assert command[1:4] == [
        "-m",
        "runtime.daemon_client",
        "delete-auto-uv-profiles",
    ]
    assert json.loads(command[4]) == ["/home/user/profile.json"]
    assert "pkexec" not in " ".join(command)


def test_ui_runtime_command_uses_auto_uv_profile_without_afterburner_flag(
    monkeypatch,
) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)

    command = commands.runtime_profile_command(
        "daemonize",
        profile_selector="profile-a",
        silent_fan_curve=True,
        gpu_index=1,
    persist_on_startup=True,
    )
    intent = _runtime_profile_daemon_intent(command)

    assert "--daemonize" not in command
    assert "--prefer-afterburner-curve" not in command
    assert intent["silent_fan_curve"] is True
    assert intent["profile_selector"] == "profile-a"
    assert intent["gpu_index"] == 1


def test_ui_runtime_command_adds_adaptive(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)

    command = commands.runtime_profile_command(
        "daemonize",
        adaptive_auto_uv=True,
    persist_on_startup=True,
    )

    assert _runtime_profile_daemon_intent(command)["adaptive_auto_uv"] is True
    assert _runtime_profile_daemon_intent(command)["profile_selector"] == ""


def test_ui_adaptive_boot_apply_uses_daemon_without_pkexec(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 1000)

    command = commands.runtime_profile_command(
        "daemonize",
        adaptive_auto_uv=True,
        gpu_index=0,
    persist_on_startup=True,
    )

    assert command[1:5] == [
        "-m",
        "runtime.daemon_client",
        "apply-runtime-intent",
        "--boot",
    ]
    assert "pkexec" not in " ".join(command)
    assert "sudo" not in " ".join(command)
    assert _runtime_profile_daemon_intent(command) == {
        "profile_selector": "",
        "silent_fan_curve": False,
        "adaptive_auto_uv": True,
        "gpu_index": 0,
    }


def test_session_only_apply_clears_the_boot_profile(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 1000)

    command = commands.runtime_profile_command(
        "daemonize",
        profile_selector="perf",
        gpu_index=0,
        persist_on_startup=False,
    )

    assert command[1:5] == [
        "-m",
        "runtime.daemon_client",
        "apply-runtime-intent",
        "--clear-boot",
    ]
    assert "--boot" not in command


def test_flatpak_normal_operations_never_request_elevation(
    monkeypatch,
    tmp_path,
) -> None:
    flatpak_info = tmp_path / ".flatpak-info"
    flatpak_info.write_text("[Application]\n", encoding="utf-8")
    monkeypatch.setattr(commands, "FLATPAK_INFO_PATH", flatpak_info)
    monkeypatch.setattr(commands.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        commands.shutil,
        "which",
        lambda name: "/usr/bin/flatpak-spawn" if name == "flatpak-spawn" else None,
    )

    normal_commands = [
        commands.scan_command({"gpu_index": 0}),
        commands.runtime_profile_command(
            "daemonize",
            profile_selector="profile-a",
            gpu_index=0,
        persist_on_startup=True,
        ),
        commands.runtime_profile_command(
            "daemonize",
            adaptive_auto_uv=True,
            gpu_index=0,
        persist_on_startup=True,
        ),
        commands.runtime_profile_command(
            "daemonize",
            profile_selector="__stock__",
            gpu_index=0,
        persist_on_startup=True,
        ),
        commands.profile_verify_command(
            profile_selector="profile-a",
            gpu_index=0,
        ),
        commands.delete_profiles_command(["/home/user/profile.json"]),
    ]

    for command in normal_commands:
        joined = " ".join(command)
        assert "pkexec" not in joined
        assert "sudo" not in joined
        assert "flatpak-spawn" not in joined
        assert "systemctl" not in joined


def _verify_daemon_options(command: list[str]) -> dict:
    assert command[1:4] == [
        "-m",
        "runtime.daemon_client",
        "start-profile-verification",
    ]
    return json.loads(command[4])


def test_ui_profile_verify_command_uses_selected_auto_uv_profile(monkeypatch) -> None:
    # Verification streams through the root daemon (no pkexec); the daemon owns
    # the stop-request marker, so stop_request_path is not forwarded.
    monkeypatch.setattr(commands.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(commands, "runtime_gpu_index", lambda: 2)

    command = commands.profile_verify_command(
        profile_selector="profile-a",
        duration_s=600,
        stop_request_path="/tmp/verify.stop",
    )
    options = _verify_daemon_options(command)

    assert "pkexec" not in " ".join(command)
    assert options == {
        "stability_seconds": 600,
        "gpu_index": 2,
        "auto_uv_profile": "profile-a",
    }
    assert "/tmp/verify.stop" not in " ".join(command)


def test_ui_profile_verify_command_never_reenters_flatpak_or_pkexec(monkeypatch) -> None:
    """Bazzite regression: verification is one daemon RPC, not nested bwrap."""
    monkeypatch.setenv("FLATPAK_ID", "io.github.jpietek.PenguinBurner")
    monkeypatch.setattr(commands.os, "geteuid", lambda: 1000)

    command = commands.profile_verify_command(
        profile_selector="profile-a",
        duration_s=300,
        stop_request_path="/tmp/verify.stop",
    )

    joined = " ".join(command)
    assert command[1:4] == [
        "-m",
        "runtime.daemon_client",
        "start-profile-verification",
    ]
    assert "flatpak-spawn" not in joined
    assert "flatpak run" not in joined
    assert "pkexec" not in joined
    assert "/proc/self/fd" not in joined


def test_ui_profile_verify_command_uses_fixed_q2rtx_cuda_workload(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 1000)

    command = commands.profile_verify_command(
        profile_selector="profile-a",
        duration_s=600,
    )
    options = _verify_daemon_options(command)

    assert "stability_workload" not in options


def test_ui_profile_verify_command_can_override_runtime_gpu_index(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(commands, "runtime_gpu_index", lambda: 2)

    command = commands.profile_verify_command(
        profile_selector="profile-a",
        duration_s=600,
        gpu_index=1,
    )

    assert _verify_daemon_options(command)["gpu_index"] == 1
