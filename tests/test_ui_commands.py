from __future__ import annotations

import base64
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

import ui.commands as commands
from auto_uv.domain.user_options import AUTO_UV_DEFAULTS
from auto_uv.run.crash_recovery import probe_summary_from_candidate_record
from auto_uv.run.scan_runtime_settings import (
    short_probe_base_duration_s as _short_probe_base_duration_s,
)
import ui.features.auto_uv.candidate_choice as candidate_choice
from ui.features.auto_uv.candidate_choice import (
    candidate_selection_summary as _candidate_selection_summary,
    sorted_final_choice_candidates as _sorted_backend_final_choice_candidates,
)
from ui.assets import application_version as _application_version
from ui.dialogs.about import ABOUT_LINKS_HTML
from ui.dialogs.final_choice import (
    FINAL_CHOICE_FPS_SORT_COLUMN,
    FINAL_CHOICE_FPSW_SORT_COLUMN,
    best_final_choice_candidate_id as _best_final_choice_candidate_id,
    create_final_choice_table as _create_final_choice_table,
    final_choice_shows_oc_column as _final_choice_shows_oc_column,
    final_choice_sort_column_for_mode as _final_choice_sort_column_for_mode,
    final_choice_intro_text as _final_choice_intro_text,
    sort_candidates_for_final_choice as _sort_candidates_for_final_choice,
)
from ui.main import parse_gui_args as _parse_gui_args
from ui.models import top_status_text as _top_status_text
from ui.models import probe_decision_label as _probe_decision_label
from ui.models import probe_failure_label as _probe_failure_label
from ui.styles import STYLESHEET
from ui.features.tuning.tuning import (
    AUTO_UV_PRESET_ADAPTIVE,
    AUTO_UV_PRESET_BALANCED,
    AUTO_UV_PRESET_EFFICIENCY,
    AUTO_UV_PRESET_PERFORMANCE,
    AUTO_UV_DROP_REFERENCE_VOLTAGE_MV,
    DEFAULT_AUTO_UV_BALANCED_TAIL_RISE_BINS,
    DEFAULT_AUTO_UV_MAX_CLOCK_DROP_PCT,
    DEFAULT_AUTO_UV_MAX_DROP_PCT,
    DEFAULT_AUTO_UV_PRESET,
    GPU_UNDERVOLTING_PURPOSE_TEXT,
    DEFAULT_AUTO_UV_PERFORMANCE_TAIL_RISE_BINS,
    DEFAULT_AUTO_UV_TAIL_RISE_BINS,
    auto_uv_preset as _auto_uv_preset,
    auto_uv_clock_drop_default as _auto_uv_clock_drop_default,
    auto_uv_performance_preset_label as _auto_uv_performance_preset_label,
    auto_uv_performance_target_default as _auto_uv_performance_target_default,
    auto_uv_performance_preset_tooltip as _auto_uv_performance_preset_tooltip,
    auto_uv_voltage_drop_default as _auto_uv_voltage_drop_default,
)
from ui.components.runs_table import (
    RunsTable,
    _format_duration_compact,
    _is_active_decision,
    _progress_label,
    _progress_text_color,
    _progress_time_text,
    _row_state,
)
from ui.components.scan_controls import (
    _clamped_elapsed_s as _scan_controls_clamped_elapsed_s,
)
from ui.components.curve_plot import (
    _axis_value_badge_text,
    _nearest_curve_point,
    _probe_marker_values,
)
from ui.components.table_sizing import set_header_fit_column_widths


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
    ui_source = Path("ui/commands.py").read_text(encoding="utf-8")
    runtime_source = Path(
        "runtime/support/flatpak_daemon_install.py"
    ).read_text(encoding="utf-8")

    assert "rollback_daemon_install" not in ui_source
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

    def fake_which(name: str) -> str | None:
        return {
            "pkexec": "/usr/bin/pkexec",
            "env": "/usr/bin/env",
        }.get(name)

    monkeypatch.setattr(commands.shutil, "which", fake_which)

    command = commands.daemon_migration_command()

    assert command[:2] == ["/usr/bin/pkexec", "/usr/bin/env"]
    assert "--migrate-to-daemon-service" in command


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
    assert options["auto_uv_max_clock_drop_pct"] == 10.0
    assert options["auto_oc_target_voltage_mv"] == 925
    assert options["auto_oc_target_clock_mhz"] == 2670
    assert "--power-limit-override-w" not in command
    assert options["auto_uv_power_limit_w"] == 390
    assert "--yolo" not in command
    assert "--auto-uv-efficiency-stop-streak" not in command
    assert "--auto-uv-min-efficiency-stop-drop-pct" not in command
    assert options["auto_uv_memory_offset_mhz"] == 500
    assert options["auto_uv_tail_rise_bins"] == 2


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
    assert options["auto_uv_efficiency_max_clock_drop_pct"] == 15.0
    assert options["auto_uv_efficiency_power_limit_w"] == 250
    assert options["auto_uv_efficiency_memory_offset_mhz"] == 500
    assert options["auto_uv_balanced_max_clock_drop_pct"] == 6.0
    assert options["auto_uv_balanced_power_limit_w"] == 300
    assert options["auto_uv_balanced_memory_offset_mhz"] == 0
    assert options["auto_uv_performance_max_clock_drop_pct"] == 5.4
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


def test_auto_uv_short_verification_defaults_to_10_seconds() -> None:
    assert AUTO_UV_DEFAULTS.probe_duration_s == 10
    assert _short_probe_base_duration_s() == 10


def test_gui_new_ui_argument_is_hidden_from_qt_args() -> None:
    qt_args = _parse_gui_args(["penguin-burner-ui", "--new-ui", "--style", "Fusion"])

    assert qt_args == ["penguin-burner-ui", "--style", "Fusion"]


def test_probe_failure_labels_distinguish_recoverable_and_fatal_reasons() -> None:
    assert (
        _probe_decision_label(
            {
                "decision": "fail",
                "failure_kind": "low-clock",
                "reason": "average busy core clock below floor",
            }
        )
        == "Failed"
    )
    assert (
        _probe_failure_label(
            {
                "decision": "fail",
                "failure_kind": "fps-regression",
                "reason": "single-run FPS below floor current=79 floor=80",
            }
        )
        == "Single run FPS low"
    )
    assert (
        _probe_failure_label(
            {
                "decision": "fail",
                "failure_kind": "fps-regression",
                "reason": "benchmark average FPS below floor current=89 floor=90",
            }
        )
        == "Average FPS low"
    )
    assert (
        _probe_failure_label(
            {
                "decision": "fail",
                "failure_kind": "fatal-output",
                "fatal_output_matches": ["VK_ERROR_DEVICE_LOST"],
            }
        )
        == "Vulkan device lost"
    )
    assert (
        _probe_failure_label(
            {
                "decision": "fail",
                "failure_kind": "nvidia-xid",
            }
        )
        == "Nvidia Xid fail"
    )
    assert (
        _probe_failure_label(
            {
                "decision": "fail",
                "failure_kind": "load-lost",
            }
        )
        == "GPU load too low"
    )


def test_probe_failure_severity_controls_table_row_state() -> None:
    assert (
        _row_state(
            {
                "decision": "fail",
                "failure_kind": "low-clock",
                "failure_severity": "recoverable",
                "reason": "average busy core clock below floor",
            },
            running=False,
        )
        == "warning"
    )
    assert (
        _row_state(
            {
                "decision": "fail",
                "failure_kind": "fatal-output",
                "failure_severity": "critical",
                "fatal_output_matches": ["VK_ERROR_DEVICE_LOST"],
            },
            running=False,
        )
        == "error"
    )


def test_runs_table_power_delta_keeps_raw_sign() -> None:
    table = RunsTable.__new__(RunsTable)
    table.base_baseline = {
        "stage": "base-baseline",
        "power_w": 300.0,
        "efficiency_fps_per_w": 0.5,
    }

    assert table._delta_text(225.6, "power_w") == "-24.80%"
    assert table._delta_text(0.75, "efficiency_fps_per_w") == "+50.00%"
    assert table._metric_text_with_delta(225.6, "power_w") == "225.60 (-24.80%)"


def test_runs_table_compacts_metric_delta_columns() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    table = RunsTable(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)

    assert "FPS vs base" not in RunsTable.COLUMNS
    assert "Power vs base" not in RunsTable.COLUMNS
    assert "FPS/W vs base" not in RunsTable.COLUMNS

    table.add_probe_result(
        {
            "stage": "base-baseline",
            "voltage_mv": 1020,
            "clock_mhz": 2745,
            "fps": 150.0,
            "power_w": 300.0,
            "efficiency_fps_per_w": 0.50,
            "decision": "pass",
        }
    )
    table.add_probe_result(
        {
            "stage": "candidate",
            "voltage_mv": 900,
            "clock_mhz": 2600,
            "fps": 160.0,
            "power_w": 270.0,
            "efficiency_fps_per_w": 0.75,
            "perf_cap_reason": "sw-power+hw-thermal",
            "decision": "accept",
        }
    )

    assert table.widget.columnCount() == len(RunsTable.COLUMNS)
    assert table.widget.item(0, table.FPS_COLUMN).text() == "150.00 (ref)"
    assert table.widget.item(1, table.FPS_COLUMN).text() == "160.00 (+6.67%)"
    assert table.widget.item(1, table.POWER_COLUMN).text() == "270.00 (-10.00%)"
    assert table.widget.item(1, table.PERF_CAP_COLUMN).text() == ("sw-power+hw-thermal")
    assert table.widget.item(1, table.FPSW_COLUMN).text() == "0.75 (+50.00%)"
    assert (
        table.widget.item(1, table.POWER_COLUMN)
        .toolTip()
        .startswith("Power W -10.00% vs base")
    )


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


def test_final_choice_performance_mode_sorts_by_fps() -> None:
    candidates = [
        {
            "candidate_id": "efficient",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2700,
            "avg_fps": 150.0,
            "efficiency_fps_per_w": 0.75,
        },
        {
            "candidate_id": "fast",
            "candidate_voltage_mv": 930,
            "lock_clock_mhz": 2760,
            "avg_fps": 160.0,
            "efficiency_fps_per_w": 0.65,
        },
    ]

    sorted_candidates = _sort_candidates_for_final_choice(candidates, "performance")

    assert [candidate["candidate_id"] for candidate in sorted_candidates] == [
        "fast",
        "efficient",
    ]
    assert _best_final_choice_candidate_id(sorted_candidates, "performance") == ("fast")
    assert _final_choice_sort_column_for_mode("performance") == (
        FINAL_CHOICE_FPS_SORT_COLUMN
    )


def test_final_choice_efficiency_mode_sorts_by_fps_per_w() -> None:
    candidates = [
        {
            "candidate_id": "fast",
            "candidate_voltage_mv": 930,
            "lock_clock_mhz": 2760,
            "avg_fps": 160.0,
            "efficiency_fps_per_w": 0.65,
        },
        {
            "candidate_id": "efficient",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2700,
            "avg_fps": 150.0,
            "efficiency_fps_per_w": 0.75,
        },
    ]

    sorted_candidates = _sort_candidates_for_final_choice(candidates, "efficiency")

    assert [candidate["candidate_id"] for candidate in sorted_candidates] == [
        "efficient",
        "fast",
    ]
    assert _best_final_choice_candidate_id(sorted_candidates, "efficiency") == (
        "efficient"
    )
    assert _final_choice_sort_column_for_mode("efficiency") == (
        FINAL_CHOICE_FPSW_SORT_COLUMN
    )


def test_final_choice_user_stop_intro_mentions_previous_stable_metric() -> None:
    efficiency_text = _final_choice_intro_text(
        "efficiency",
        request_reason="user-stop",
    )
    performance_text = _final_choice_intro_text(
        "performance",
        request_reason="user-stop",
    )

    assert "stopped" in efficiency_text.lower()
    assert "previously stable candidates" in efficiency_text
    assert "best FPS/W" in efficiency_text
    assert "highest FPS" in performance_text


def test_final_choice_previous_crash_intro_includes_prior_decision() -> None:
    text = _final_choice_intro_text(
        "performance",
        request_reason="previous-crash",
        recovery_decision={
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2910,
            "decision": "Device lost!",
        },
    )

    assert "resume from a safer voltage bin" in text
    assert "875mV@2910MHz" in text
    assert "Device lost!" in text


def test_final_choice_failed_final_verify_intro_mentions_safer_candidate() -> None:
    text = _final_choice_intro_text(
        "performance",
        request_reason="final-verification-failed",
        recovery_decision={
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2895,
            "decision": "fatal-q2rtx-output",
        },
    )

    assert "Final verification failed" in text
    assert "safer voltage" in text
    assert "875mV@2895MHz" in text
    assert "highest FPS" in text


def test_backend_final_choice_performance_mode_sorts_by_fps() -> None:
    candidates = [
        {
            "candidate_id": "fast",
            "candidate_voltage_mv": 930,
            "lock_clock_mhz": 2760,
            "avg_fps": 160.0,
            "efficiency_fps_per_w": 0.65,
        },
        {
            "candidate_id": "efficient",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2700,
            "avg_fps": 150.0,
            "efficiency_fps_per_w": 0.75,
        },
    ]

    sorted_candidates, sort_label = _sorted_backend_final_choice_candidates(
        candidates,
        auto_uv_mode="performance",
        base_probe=None,
    )

    assert sort_label == "fps"
    assert [candidate["candidate_id"] for candidate in sorted_candidates] == [
        "fast",
        "efficient",
    ]


def test_backend_final_choice_efficiency_mode_sorts_by_fps_per_w() -> None:
    candidates = [
        {
            "candidate_id": "efficient",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2700,
            "avg_fps": 150.0,
            "efficiency_fps_per_w": 0.75,
        },
        {
            "candidate_id": "fast",
            "candidate_voltage_mv": 930,
            "lock_clock_mhz": 2760,
            "avg_fps": 160.0,
            "efficiency_fps_per_w": 0.65,
        },
    ]

    sorted_candidates, sort_label = _sorted_backend_final_choice_candidates(
        candidates,
        auto_uv_mode="efficiency",
        base_probe=None,
    )

    assert sort_label == "fps-per-w"
    assert [candidate["candidate_id"] for candidate in sorted_candidates] == [
        "efficient",
        "fast",
    ]


def _final_choice_base_curve() -> list[dict]:
    return [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": 2200 + index * 90,
            "target_mhz": 2200 + index * 90,
            "new_offset_mhz": 0,
        }
        for index, voltage_mv in enumerate(range(850, 1025, 25))
    ]


def _final_choice_probe(voltage_mv: int, clock_mhz: int, *, fps: float, fpsw: float):
    return probe_summary_from_candidate_record(
        {
            "candidate_voltage_mv": int(voltage_mv),
            "lock_clock_mhz": int(clock_mhz),
            "avg_core_clock_mhz": float(clock_mhz),
            "avg_fps": float(fps),
            "efficiency_fps_per_w": float(fpsw),
        }
    )


def _run_backend_final_choice(tmp_path, monkeypatch, *, request_reason: str):
    request_path = tmp_path / "final-choice-request.json"
    response_path = tmp_path / "final-choice-response.json"
    monkeypatch.setattr(
        candidate_choice, "final_choice_request_path", lambda: request_path
    )
    monkeypatch.setattr(
        candidate_choice, "final_choice_response_path", lambda: response_path
    )
    # An empty UI response means the backend keeps the request's default.
    monkeypatch.setattr(
        candidate_choice, "wait_for_final_choice_response", lambda _path: {}
    )
    curve = _final_choice_base_curve()
    tier_probe = _final_choice_probe(850, 2430, fps=120.0, fpsw=0.60)
    history = [_final_choice_probe(900, 2700, fps=150.0, fpsw=0.75), tier_probe]

    (
        _plan,
        selected_voltage_mv,
        selected_lock_clock_mhz,
        _probe,
        _duration_s,
    ) = candidate_choice.choose_final_verification_candidate(
        log=lambda _message: None,
        event_callback=None,
        auto_uv_mode="efficiency",
        base_probe=None,
        stable_plan=curve,
        stable_voltage_mv=850,
        stable_lock_clock_mhz=2430,
        stable_probe=tier_probe,
        stable_history=history,
        base_curve=curve,
        final_verification_duration_s=300,
        initial_target_voltage_mv=1000,
        short_probe_base_duration_s=10,
        request_reason=request_reason,
    )
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    return payload, (int(selected_voltage_mv), int(selected_lock_clock_mhz))


def test_backend_adaptive_final_choice_defaults_to_tier_candidate(
    tmp_path, monkeypatch
) -> None:
    payload, selected = _run_backend_final_choice(
        tmp_path,
        monkeypatch,
        request_reason="adaptive-efficiency",
    )

    assert payload["request_reason"] == "adaptive-efficiency"
    assert payload["default_candidate_id"] == "850mv-2430mhz"
    assert selected == (850, 2430)


def test_backend_classic_final_choice_defaults_to_metric_best(
    tmp_path, monkeypatch
) -> None:
    payload, selected = _run_backend_final_choice(
        tmp_path,
        monkeypatch,
        request_reason="sweep-complete",
    )

    assert payload["default_candidate_id"] == "900mv-2700mhz"
    assert selected == (900, 2700)


def test_backend_final_choice_summary_includes_baseline_delta_metrics() -> None:
    summary = _candidate_selection_summary(
        {
            "candidate_id": "efficient",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2700,
            "avg_fps": 160.0,
            "efficiency_fps_per_w": 0.75,
        },
        base_probe=SimpleNamespace(
            avg_fps=150.0,
            avg_power_w=300.0,
            efficiency_fps_per_w=0.50,
        ),
    )

    assert summary["base_avg_fps"] == 150.0
    assert summary["base_avg_power_w"] == 300.0
    assert summary["base_efficiency_fps_per_w"] == 0.50


def test_backend_final_choice_summary_includes_core_oc_above_baseline() -> None:
    summary = _candidate_selection_summary(
        {
            "candidate_id": "resume",
            "candidate_voltage_mv": 885,
            "lock_clock_mhz": 2910,
            "avg_fps": 160.0,
            "efficiency_fps_per_w": 0.75,
        },
        base_probe=SimpleNamespace(
            avg_fps=150.0,
            avg_core_clock_mhz=2700.0,
            efficiency_fps_per_w=0.50,
        ),
    )

    assert summary["base_avg_core_clock_mhz"] == 2700.0
    assert summary["core_oc_mhz"] == 210


def test_final_choice_table_default_sort_and_header_toggles() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    candidates = [
        {
            "candidate_id": "efficient",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2700,
            "avg_core_clock_mhz": 2680.0,
            "avg_fps": 150.0,
            "efficiency_fps_per_w": 0.75,
            "avg_power_w": 200.0,
        },
        {
            "candidate_id": "fast",
            "candidate_voltage_mv": 930,
            "lock_clock_mhz": 2760,
            "avg_core_clock_mhz": 2740.0,
            "avg_fps": 160.0,
            "efficiency_fps_per_w": 0.65,
            "avg_power_w": 246.0,
        },
    ]

    table = _create_final_choice_table(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        candidates=candidates,
        default_candidate_id="efficient",
        default_sort_column=FINAL_CHOICE_FPS_SORT_COLUMN,
        auto_uv_mode="performance",
    )

    def row_ids() -> list[str]:
        return [
            str(table.item(row, 0).data(QtCore.Qt.UserRole))
            for row in range(table.rowCount())
        ]

    assert (
        table.horizontalHeader().sortIndicatorSection() == FINAL_CHOICE_FPS_SORT_COLUMN
    )
    assert table.horizontalHeader().sortIndicatorOrder() == QtCore.Qt.DescendingOrder
    assert row_ids() == ["fast", "efficient"]

    table.horizontalHeader().sectionClicked.emit(FINAL_CHOICE_FPSW_SORT_COLUMN)
    assert (
        table.horizontalHeader().sortIndicatorSection() == FINAL_CHOICE_FPSW_SORT_COLUMN
    )
    assert table.horizontalHeader().sortIndicatorOrder() == QtCore.Qt.DescendingOrder
    assert row_ids() == ["efficient", "fast"]

    table.horizontalHeader().sectionClicked.emit(FINAL_CHOICE_FPSW_SORT_COLUMN)
    assert table.horizontalHeader().sortIndicatorOrder() == QtCore.Qt.AscendingOrder
    assert row_ids() == ["fast", "efficient"]


def test_previous_crash_table_preserves_failed_run_order() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    candidates = [
        {
            "candidate_id": "875mv-2897mhz",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2897,
            "avg_fps": 150.0,
        },
        {
            "candidate_id": "885mv-2873mhz",
            "candidate_voltage_mv": 885,
            "lock_clock_mhz": 2873,
            "avg_fps": 180.0,
        },
        {
            "candidate_id": "900mv-2786mhz",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2786,
            "avg_fps": 170.0,
        },
    ]

    table = _create_final_choice_table(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        candidates=candidates,
        default_candidate_id="885mv-2873mhz",
        default_sort_column=None,
        auto_uv_mode="performance",
        request_reason="previous-crash",
    )

    row_ids = [
        str(table.item(row, 0).data(QtCore.Qt.UserRole))
        for row in range(table.rowCount())
    ]

    assert row_ids == ["875mv-2897mhz", "885mv-2873mhz", "900mv-2786mhz"]
    assert table.horizontalHeader().isSortIndicatorShown() is False


def test_failed_final_verify_table_uses_normal_mode_sorting() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    candidates = [
        {
            "candidate_id": "885mv-2895mhz",
            "candidate_voltage_mv": 885,
            "lock_clock_mhz": 2895,
            "avg_fps": 68.270,
        },
        {
            "candidate_id": "895mv-2925mhz",
            "candidate_voltage_mv": 895,
            "lock_clock_mhz": 2925,
            "avg_fps": 68.563,
        },
        {
            "candidate_id": "890mv-2910mhz",
            "candidate_voltage_mv": 890,
            "lock_clock_mhz": 2910,
            "avg_fps": 68.472,
        },
    ]

    table = _create_final_choice_table(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        candidates=candidates,
        default_candidate_id="895mv-2925mhz",
        default_sort_column=FINAL_CHOICE_FPS_SORT_COLUMN,
        auto_uv_mode="performance",
        request_reason="final-verification-failed",
    )

    row_ids = [
        str(table.item(row, 0).data(QtCore.Qt.UserRole))
        for row in range(table.rowCount())
    ]

    assert row_ids == ["895mv-2925mhz", "890mv-2910mhz", "885mv-2895mhz"]
    assert table.horizontalHeader().isSortIndicatorShown() is True
    assert table.item(0, 8).text().startswith("Next safer pick")


def test_final_choice_oc_column_only_visible_for_performance() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    candidates = [
        {
            "candidate_id": "resume",
            "candidate_voltage_mv": 885,
            "lock_clock_mhz": 2910,
            "core_oc_mhz": 210,
            "base_avg_core_clock_mhz": 2700.0,
            "avg_core_clock_mhz": 2890.0,
            "avg_fps": 160.0,
            "efficiency_fps_per_w": 0.75,
            "avg_power_w": 200.0,
        }
    ]

    performance_table = _create_final_choice_table(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        candidates=candidates,
        default_candidate_id="resume",
        auto_uv_mode="performance",
    )
    efficiency_table = _create_final_choice_table(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        candidates=candidates,
        default_candidate_id="resume",
        auto_uv_mode="efficiency",
    )

    assert _final_choice_shows_oc_column("performance") is True
    assert _final_choice_shows_oc_column("efficiency") is False
    assert performance_table.isColumnHidden(2) is False
    assert performance_table.item(0, 2).text() == "+210"
    assert efficiency_table.isColumnHidden(2) is True


def test_final_choice_table_uses_profile_delta_rendering_for_fps_columns() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    candidates = [
        {
            "candidate_id": "efficient",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2700,
            "avg_core_clock_mhz": 2680.0,
            "avg_fps": 160.0,
            "base_avg_fps": 150.0,
            "efficiency_fps_per_w": 0.75,
            "base_efficiency_fps_per_w": 0.50,
            "avg_power_w": 200.0,
            "base_avg_power_w": 250.0,
        },
        {
            "candidate_id": "regressed",
            "candidate_voltage_mv": 930,
            "lock_clock_mhz": 2760,
            "avg_core_clock_mhz": 2740.0,
            "avg_fps": 140.0,
            "base_avg_fps": 150.0,
            "efficiency_fps_per_w": 0.45,
            "base_efficiency_fps_per_w": 0.50,
            "avg_power_w": 270.0,
            "base_avg_power_w": 250.0,
        },
    ]

    table = _create_final_choice_table(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        candidates=candidates,
        default_candidate_id="efficient",
        default_sort_column=FINAL_CHOICE_FPSW_SORT_COLUMN,
        auto_uv_mode="performance",
    )

    def item_for(candidate_id: str, column: int):
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is not None and item.data(QtCore.Qt.UserRole) == candidate_id:
                return table.item(row, column)
        raise AssertionError(f"missing row for {candidate_id}")

    efficient_fpsw = item_for("efficient", FINAL_CHOICE_FPSW_SORT_COLUMN)
    efficient_fps = item_for("efficient", FINAL_CHOICE_FPS_SORT_COLUMN)
    efficient_power = item_for("efficient", 6)
    regressed_fpsw = item_for("regressed", FINAL_CHOICE_FPSW_SORT_COLUMN)
    regressed_fps = item_for("regressed", FINAL_CHOICE_FPS_SORT_COLUMN)
    regressed_power = item_for("regressed", 6)

    assert efficient_fpsw.text() == "0.75 (+50.00%)"
    assert efficient_fps.text() == "160.00 (+6.67%)"
    assert efficient_power.text() == "200.00 (-20.00%)"
    assert regressed_fpsw.text() == "0.45 (-10.00%)"
    assert regressed_fps.text() == "140.00 (-6.67%)"
    assert regressed_power.text() == "270.00 (+8.00%)"
    assert efficient_fpsw.foreground().color().name() == "#55d27a"
    assert efficient_fps.foreground().color().name() == "#55d27a"
    assert efficient_power.foreground().color().name() == "#55d27a"
    assert regressed_fpsw.foreground().color().name() == "#ff6b6b"
    assert regressed_fps.foreground().color().name() == "#ff6b6b"
    assert regressed_power.foreground().color().name() == "#ff6b6b"


def test_start_auto_uv_button_uses_orange_without_changing_primary_green() -> None:
    assert "QPushButton#startAutoUvButton" in STYLESHEET
    assert "background: #c4772a" in STYLESHEET
    assert "border-color: #e1a45d" in STYLESHEET
    assert "border-color: #ffc57a" in STYLESHEET
    assert "QPushButton#importAfterburnerButton" in STYLESHEET
    assert "background: #2f6f55" in STYLESHEET
    assert "QProgressBar#dependencyProgress::chunk" in STYLESHEET
    assert "background: #3d8d6d" in STYLESHEET
    assert "QToolButton#deleteProfilesButton" in STYLESHEET
    assert "background: #7f2525" in STYLESHEET
    assert "border-color: #d45d5d" in STYLESHEET


def test_single_gpu_profile_target_uses_disabled_colors() -> None:
    label_style = STYLESHEET.split(
        "QLabel#profileTargetGpuLabel:disabled {", 1
    )[1].split("}", 1)[0]
    combo_style = STYLESHEET.split(
        "QComboBox#profileTargetGpu:disabled {", 1
    )[1].split("}", 1)[0]

    assert "color: #7f8794" in label_style
    assert "color: #7f8794" in combo_style
    assert "background: #252b34" in combo_style


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


def test_auto_uv_preset_defaults_and_gpu_table_default() -> None:
    # One click, three profiles: the adaptive all-tiers scan is the default.
    assert DEFAULT_AUTO_UV_PRESET == "adaptive"
    assert DEFAULT_AUTO_UV_MAX_DROP_PCT == 10.0
    assert AUTO_UV_DROP_REFERENCE_VOLTAGE_MV == 1000
    assert DEFAULT_AUTO_UV_MAX_CLOCK_DROP_PCT == 12.5
    assert DEFAULT_AUTO_UV_TAIL_RISE_BINS == 0
    assert DEFAULT_AUTO_UV_BALANCED_TAIL_RISE_BINS == 4
    assert DEFAULT_AUTO_UV_PERFORMANCE_TAIL_RISE_BINS == 4
    efficiency = _auto_uv_preset(AUTO_UV_PRESET_EFFICIENCY)
    balanced = _auto_uv_preset(AUTO_UV_PRESET_BALANCED)
    performance = _auto_uv_preset(AUTO_UV_PRESET_PERFORMANCE)
    assert (efficiency.auto_uv_mode, efficiency.tail_rise_bins) == (
        "efficiency",
        0,
    )
    assert (balanced.auto_uv_mode, balanced.tail_rise_bins) == (
        "balanced",
        4,
    )
    assert (performance.auto_uv_mode, performance.tail_rise_bins) == (
        "performance",
        4,
    )


def test_auto_uv_performance_preset_describes_auto_oc() -> None:
    assert _auto_uv_performance_preset_label() == "Performance"
    tooltip = _auto_uv_performance_preset_tooltip()
    assert "4-bin tail curve" in tooltip
    assert "Performance Auto-OC ladder" in tooltip


def test_auto_uv_performance_target_default_uses_gpu_table_target() -> None:
    target = _auto_uv_performance_target_default(
        gpu_name="NVIDIA GeForce RTX 4090",
    )

    assert target.preset_matched is True
    assert target.gpu_family == "RTX 4090"
    assert target.voltage_mv == 925
    assert target.clock_mhz == 2645


def test_auto_uv_voltage_drop_default_uses_detected_gpu_table_floor() -> None:
    preview = _auto_uv_voltage_drop_default(gpu_name="NVIDIA GeForce RTX 5080")

    assert preview.preset_matched is True
    assert preview.gpu_family == "RTX 5080"
    assert preview.floor_voltage_mv == 850
    assert preview.value_pct == pytest.approx(15.0)


def test_auto_uv_clock_drop_default_uses_preset_aware_gpu_table_ratio() -> None:
    efficiency = _auto_uv_clock_drop_default(
        gpu_name="NVIDIA GeForce RTX 5080",
        preset_id=AUTO_UV_PRESET_EFFICIENCY,
    )
    balanced = _auto_uv_clock_drop_default(
        gpu_name="NVIDIA GeForce RTX 5080",
        preset_id=AUTO_UV_PRESET_BALANCED,
    )
    performance = _auto_uv_clock_drop_default(
        gpu_name="NVIDIA GeForce RTX 5080",
        preset_id=AUTO_UV_PRESET_PERFORMANCE,
    )

    assert efficiency.preset_matched is True
    assert efficiency.gpu_family == "RTX 5080"
    assert efficiency.value_pct == pytest.approx(11.111111111111116)
    assert balanced.value_pct == pytest.approx(
        efficiency.value_pct * 0.6 + performance.value_pct * 0.4
    )
    assert performance.value_pct == pytest.approx(6.349206349206349)


def test_auto_uv_clock_drop_default_falls_back_to_generic_when_unmatched() -> None:
    preview = _auto_uv_clock_drop_default(gpu_name="NVIDIA GeForce GTX 1080")

    assert preview.preset_matched is False
    assert preview.value_pct == pytest.approx(12.5)


def test_auto_uv_voltage_drop_default_falls_back_to_generic_when_unmatched() -> None:
    preview = _auto_uv_voltage_drop_default(gpu_name="NVIDIA GeForce GTX 1080")

    assert preview.preset_matched is False
    assert preview.value_pct == pytest.approx(10.0)
    assert preview.floor_voltage_mv is None
    assert preview.reference_voltage_mv is None


def test_auto_uv_voltage_drop_default_uses_ampere_table_for_3080() -> None:
    preview = _auto_uv_voltage_drop_default(gpu_name="NVIDIA GeForce RTX 3080")

    assert preview.preset_matched is True
    assert preview.gpu_family == "RTX 3080"
    assert preview.floor_voltage_mv == 800
    assert preview.value_pct == pytest.approx(20.0)


def test_progress_text_stays_light_until_bar_is_full() -> None:
    assert _progress_text_color("#62e887", 0.0) == "#f2f5f2"
    assert _progress_text_color("#62e887", 0.5) == "#f2f5f2"
    assert _progress_text_color("#62e887", None) == "#f2f5f2"
    assert _progress_text_color("#62e887", 1.0) == "#10140f"


def test_final_progress_label_uses_capitalized_user_text() -> None:
    assert _progress_label({"stage": "final-verification"}) == "Final verification"


def test_progress_time_uses_human_duration_text() -> None:
    assert _format_duration_compact(45) == "45s"
    assert _format_duration_compact(90) == "1min 30s"
    assert _format_duration_compact(600) == "10min"
    assert _format_duration_compact(3900) == "1h 5min"
    assert _progress_time_text(310, 600) == "5min 10s / 10min"
    assert _progress_time_text(9, 7) == "7s / 7s"
    assert _scan_controls_clamped_elapsed_s(9, 7) == 7.0


def test_running_status_without_duration_is_static_text() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    from ui.components.runs_table import RunsTable

    table = RunsTable(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    payload = {"stage": "candidate", "voltage_mv": 900, "clock_mhz": 2600}

    table.add_probe_start(payload)
    assert table.widget.cellWidget(0, table.STATUS_COLUMN) is None
    assert table.widget.item(0, table.STATUS_COLUMN).text() == "Running"


def test_overlay_tab_hides_runs_panel_and_scrolls_options(monkeypatch) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    from ui.window import MainWindow
    from overlay.config import ADVANCED_OVERLAY_ITEM_IDS
    from ui.components.overlay_config import ITEM_LABELS

    monkeypatch.setattr(MainWindow, "_load_profiles", lambda self: None)
    qt_modules = (QtCore, QtGui, QtWidgets, pytest.importorskip("pyqtgraph"))
    window = MainWindow(qt_modules)

    assert not window.table_panel.isHidden()
    window.tabs.setCurrentIndex(window.overlay_tab_index)
    assert window.table_panel.isHidden()
    assert (
        window.overlay_config.widget.findChild(
            QtWidgets.QCheckBox,
            "overlayEnableCheckbox",
        )
        is None
    )
    # The overlay tab lost its global checkbox, but the per-game switch in the
    # library tab is a different control and must survive. It is a toggle
    # switch, not a checkbox: a QAbstractButton painted from the theme.
    assert (
        window.game_library_panel.widget.findChild(
            QtWidgets.QAbstractButton,
            "gameOverlay",
        )
        is not None
    )
    assert (
        window.overlay_config.widget.findChild(
            QtWidgets.QScrollArea,
            "overlayOptionsScroll",
        )
        is not None
    )
    basic_group = window.overlay_config.widget.findChild(
        QtWidgets.QGroupBox,
        "overlayBasicOptionsGroup",
    )
    advanced_group = window.overlay_config.widget.findChild(
        QtWidgets.QGroupBox,
        "overlayAdvancedOptionsGroup",
    )
    assert basic_group is not None
    assert advanced_group is not None
    assert advanced_group.geometry().top() == basic_group.geometry().top()
    assert advanced_group.geometry().left() > basic_group.geometry().left()
    advanced_checkboxes = advanced_group.findChildren(QtWidgets.QCheckBox)
    advanced_labels = [checkbox.text() for checkbox in advanced_checkboxes]
    assert advanced_labels == [ITEM_LABELS[item] for item in ADVANCED_OVERLAY_ITEM_IDS]
    assert (
        advanced_group.findChild(
            QtWidgets.QCheckBox,
            "overlayItemCheckbox_gpu_util_pct",
        ).text()
        == "GPU %"
    )
    assert (
        advanced_group.findChild(
            QtWidgets.QCheckBox,
            "overlayItemCheckbox_cpu_util_pct",
        ).text()
        == "CPU %"
    )
    assert (
        advanced_group.findChild(
            QtWidgets.QCheckBox,
            "overlayItemCheckbox_cpu_peak_thread_pct",
        ).text()
        == "CPU-T %"
    )
    value_labels = list(window.overlay_config.item_value_labels.values())
    assert len({label.width() for label in value_labels}) == 1
    assert value_labels[0].width() >= value_labels[0].fontMetrics().horizontalAdvance(
        "UV -9999 mV"
    )

    window.tabs.setCurrentIndex(window.auto_uv_tab_index)
    assert not window.table_panel.isHidden()
    window.window.close()


def test_overlay_panel_has_no_adaptive_target_fps_control(tmp_path) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    from ui.components.overlay_config import OverlayConfigPanel

    panel = OverlayConfigPanel(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        config_path=tmp_path / "overlay.toml",
    )

    # The adaptive target FPS moved to the Steam tab's per-game settings.
    assert panel.widget.layout().contentsMargins().top() >= 16
    assert (
        panel.widget.findChild(QtWidgets.QDoubleSpinBox, "overlayTargetFpsSpin")
        is None
    )


def test_overlay_panel_saves_manual_scale(tmp_path) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    from overlay.config import load_overlay_config
    from ui.components.overlay_config import OverlayConfigPanel

    config_path = tmp_path / "overlay.toml"
    panel = OverlayConfigPanel(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        config_path=config_path,
    )

    # Defaults to the adaptive 1x option.
    assert panel.scale_combo.currentText() == "1x"

    panel.scale_combo.setCurrentIndex(2)

    assert panel.scale_combo.currentText() == "2x"
    assert load_overlay_config(config_path).scale == 2.0
    assert "scale = 2.0" in config_path.read_text(encoding="utf-8")


def test_overlay_panel_has_no_steam_launch_controls(tmp_path) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    from ui.components.overlay_config import OverlayConfigPanel

    panel = OverlayConfigPanel(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        config_path=tmp_path / "overlay.toml",
    )

    assert panel.widget.findChild(QtWidgets.QLabel, "overlaySteamLaunchHint") is None
    assert panel.widget.findChild(QtWidgets.QLineEdit, "overlaySteamLaunchLine") is None
    assert panel.widget.findChild(QtWidgets.QPushButton, "overlayCopyLaunchButton") is None


def test_running_status_with_duration_uses_seconds_progress() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    from ui.components.runs_table import RunsTable

    table = RunsTable(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    payload = {
        "stage": "candidate",
        "voltage_mv": 900,
        "clock_mhz": 2600,
        "elapsed_s": 0,
        "target_duration_s": 32,
    }

    table.add_probe_start(payload)
    widget = table.widget.cellWidget(0, table.STATUS_COLUMN)

    assert widget is not None
    assert widget.format() == "0s / 32s"
    assert table.widget.item(0, table.DECISION_COLUMN).text() == "Running"
    assert table.widget.item(0, table.STATUS_COLUMN).text() == ""

    table.update_probe_progress(
        {
            "stage": "candidate",
            "voltage_mv": 900,
            "clock_mhz": 2600,
            "elapsed_s": 12,
            "target_duration_s": 32,
        }
    )

    widget = table.widget.cellWidget(0, table.STATUS_COLUMN)
    assert widget is not None
    assert widget.format() == "12s / 32s"


def test_probe_result_reuses_running_row_and_keeps_seconds_progress() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    from ui.components.runs_table import RunsTable

    table = RunsTable(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    start_payload = {
        "stage": "candidate",
        "voltage_mv": 900,
        "clock_mhz": 2600,
        "elapsed_s": 0,
        "target_duration_s": 32,
    }
    result_payload = {
        "stage": "candidate",
        "voltage_mv": 900,
        "clock_mhz": 2600,
        "fps": 120,
        "power_w": 240,
        "decision": "pass",
    }

    table.add_probe_start(start_payload)
    table.add_probe_result(result_payload)
    widget = table.widget.cellWidget(0, table.STATUS_COLUMN)

    assert table.widget.rowCount() == 1
    assert table.widget.item(0, table.DECISION_COLUMN).text() == "Pass"
    assert table.widget.item(0, table.STATUS_COLUMN).text() == ""
    assert widget is not None
    assert widget.format() == "32s / 32s"


def test_probe_failure_result_is_generic_while_status_keeps_detail() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    from ui.components.runs_table import RunsTable

    table = RunsTable(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    table.add_probe_result(
        {
            "stage": "candidate",
            "voltage_mv": 875,
            "clock_mhz": 2880,
            "decision": "fail",
            "failure_kind": "fatal-output",
            "fatal_output_matches": ["VK_ERROR_DEVICE_LOST"],
        }
    )
    widget = table.widget.cellWidget(0, table.STATUS_COLUMN)

    assert table.widget.item(0, table.DECISION_COLUMN).text() == "Failed"
    assert widget is not None
    assert widget.format() == "Vulkan device lost 100%"


def test_auto_oc_target_mhz_stays_in_target_column() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    table = RunsTable(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    table.record_candidate_curve(
        {
            "stage": "candidate",
            "label": "performance-oc 3/10",
            "voltage_mv": 950,
            "clock_mhz": 2745,
            "auto_oc": True,
            "auto_oc_baseline_clock_mhz": 2565,
            "auto_oc_applied_mhz": 180,
            "auto_oc_limit_mhz": 180,
            "points": [
                {
                    "voltage_mv": 950,
                    "base_mhz": 2565,
                    "clock_mhz": 2745,
                    "offset_mhz": 180,
                }
            ],
        }
    )
    start_payload = {
        "stage": "candidate",
        "label": "performance-oc 3/10",
        "voltage_mv": 950,
        "clock_mhz": 2745,
        "elapsed_s": 0,
        "target_duration_s": 32,
    }
    result_payload = {
        "stage": "candidate",
        "label": "performance-oc 3/10",
        "voltage_mv": 950,
        "clock_mhz": 2745,
        "measured_clock_mhz": 2733.5,
        "decision": "pass",
    }

    table.add_probe_start(start_payload)
    table.add_probe_result(result_payload)

    assert table.widget.rowCount() == 1
    assert table.widget.item(0, table.TARGET_MHZ_COLUMN).text() == "2745"
    assert table.widget.item(0, table.OC_MHZ_COLUMN).text() == "+180 MHz"
    assert table.widget.item(0, table.MEASURED_MHZ_COLUMN).text() == "2733.50"
    assert table.widget.item(0, table.DECISION_COLUMN).text() == "Pass"


def test_candidate_curve_updates_oc_column_for_existing_run_row() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    table = RunsTable(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    table.add_probe_start(
        {
            "stage": "candidate",
            "voltage_mv": 910,
            "clock_mhz": 2890,
            "elapsed_s": 0,
            "target_duration_s": 32,
        }
    )

    assert table.widget.item(0, table.OC_MHZ_COLUMN).text() == ""

    table.record_candidate_curve(
        {
            "stage": "candidate",
            "voltage_mv": 910,
            "clock_mhz": 2890,
            "auto_oc": True,
            "auto_oc_baseline_clock_mhz": 2730,
            "points": [
                {
                    "voltage_mv": 910,
                    "base_mhz": 2400,
                    "clock_mhz": 2890,
                }
            ],
        }
    )

    assert table.widget.item(0, table.OC_MHZ_COLUMN).text() == "+160 MHz"


def test_oc_column_ignores_auto_oc_budget_without_measured_baseline() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    table = RunsTable(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    table.add_probe_start(
        {
            "stage": "candidate",
            "voltage_mv": 885,
            "clock_mhz": 2880,
            "elapsed_s": 0,
            "target_duration_s": 32,
        }
    )
    table.record_candidate_curve(
        {
            "stage": "candidate",
            "voltage_mv": 885,
            "clock_mhz": 2880,
            "auto_oc": True,
            "auto_oc_applied_mhz": 7,
            "auto_oc_limit_mhz": 107,
        }
    )

    assert table.widget.item(0, table.OC_MHZ_COLUMN).text() == ""

    table.add_probe_result(
        {
            "stage": "candidate",
            "voltage_mv": 885,
            "clock_mhz": 2880,
            "base_avg_core_clock_mhz": 2730,
            "decision": "pass",
        }
    )

    assert table.widget.item(0, table.OC_MHZ_COLUMN).text() == "+150 MHz"


def test_non_auto_oc_run_leaves_oc_progress_empty() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    table = RunsTable(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    table.add_probe_result(
        {
            "stage": "candidate",
            "voltage_mv": 900,
            "clock_mhz": 2600,
            "decision": "pass",
        }
    )

    # A row with no measured baseline leaves OC blank, not "0/0".
    assert table.widget.item(0, table.OC_MHZ_COLUMN).text() == ""


def test_run_with_measured_baseline_shows_signed_oc_progress() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    table = RunsTable(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    table.add_probe_result(
        {
            "stage": "candidate",
            "voltage_mv": 885,
            "clock_mhz": 2880,
            "base_avg_core_clock_mhz": 2730,
            "decision": "pass",
        }
    )

    assert table.widget.item(0, table.OC_MHZ_COLUMN).text() == "+150 MHz"


def test_run_with_measured_baseline_shows_negative_oc_progress() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    table = RunsTable(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    table.add_probe_result(
        {
            "stage": "candidate",
            "voltage_mv": 875,
            "clock_mhz": 2600,
            "measured_baseline_clock_mhz": 2730,
            "decision": "fail",
        }
    )

    assert table.widget.item(0, table.OC_MHZ_COLUMN).text() == "-130 MHz"


def test_runs_table_row_click_toggles_single_candidate_selection() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    table = RunsTable(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    selected: list[str | None] = []
    table.on_candidate_selection_changed = selected.append

    table.add_probe_result(
        {
            "stage": "candidate",
            "voltage_mv": 900,
            "clock_mhz": 2600,
            "decision": "accepted",
        }
    )
    table.add_probe_result(
        {
            "stage": "candidate",
            "voltage_mv": 890,
            "clock_mhz": 2610,
            "decision": "accepted",
        }
    )

    table._handle_cell_clicked(0, 0)
    assert selected == ["900mv-2600mhz"]
    assert table.selected_candidate_id() == "900mv-2600mhz"

    table._handle_cell_clicked(1, 0)
    assert selected[-1] == "890mv-2610mhz"
    assert table.selected_candidate_id() == "890mv-2610mhz"
    assert [index.row() for index in table.widget.selectionModel().selectedRows()] == [
        1
    ]

    table._handle_cell_clicked(1, 0)
    assert selected[-1] is None
    assert table.selected_candidate_id() is None
    assert table.widget.selectionModel().selectedRows() == []


def test_stopping_rows_remain_active_until_stop_is_finalized() -> None:
    assert _is_active_decision("running")
    assert _is_active_decision("stopping")
    assert not _is_active_decision("stopped")


def test_app_stylesheet_does_not_override_native_scrollbars() -> None:
    first_selector = STYLESHEET.split("{", 1)[0]

    assert "QScrollBar" not in STYLESHEET
    assert "QWidget" not in first_selector


def test_scan_controls_include_about_button_after_import_afterburner() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    from ui.components.scan_controls import ScanControls

    controls = ScanControls(QtWidgets=QtWidgets)
    buttons = controls.widget.findChildren(QtWidgets.QPushButton)

    assert [button.text() for button in buttons][-2:] == ["Import Afterburner", "About"]
    assert controls.about_button.objectName() == "aboutButton"
    assert "QPushButton#aboutButton" in STYLESHEET


def test_application_version_is_available_for_about_dialog() -> None:
    assert _application_version()


def test_gpu_undervolting_purpose_text_is_user_facing() -> None:
    assert "dead-silent fan operation" in GPU_UNDERVOLTING_PURPOSE_TEXT
    assert "lower electricity bills" in GPU_UNDERVOLTING_PURPOSE_TEXT
    assert "Nvidia GPU" in GPU_UNDERVOLTING_PURPOSE_TEXT
    assert "trial and error" in GPU_UNDERVOLTING_PURPOSE_TEXT


def test_candidate_header_detail_is_smaller_and_not_bold() -> None:
    assert "QLabel#candidateLabel" in STYLESHEET
    candidate_style = STYLESHEET.split("QLabel#candidateLabel {", 1)[1].split(
        "}",
        1,
    )[0]

    assert "font-size: 13px;" in candidate_style
    assert "font-weight: 400;" in candidate_style


def test_auto_uv_preset_control_has_breathing_room_and_autofill_note() -> None:
    assert "QGroupBox#autoUvPresetGroup" in STYLESHEET
    assert "QPushButton#autoUvPresetButton" in STYLESHEET
    assert 'presetId="efficiency"' in STYLESHEET
    assert 'presetId="balanced"' in STYLESHEET
    assert 'presetId="performance"' in STYLESHEET
    source = Path("ui/dialogs/scan_tuning.py").read_text(encoding="utf-8")
    assert "SCAN_SCOPE_FULL" in source
    assert "SCAN_SCOPE_SELECTED_PROFILE" in source
    # The full-scan label derives from the estimate table instead of
    # hardcoding a duration that silently goes stale.
    assert 'f"Full scan (~{full_minimum}-{full_maximum} min)"' in source
    assert "Full scan (up to" not in source
    assert "auto_uv_voltage_drop_default" in source
    assert "auto-filled for" not in source
    assert "efficiency floor" not in source
    assert "Max voltage drop" not in source
    assert "Base verification length" not in source
    assert '"auto_uv_short_seconds"' not in source
    assert "preset-aware from the GPU table" in source
    assert "Min voltage" in source
    assert "sync_voltage_floor_from_drop" not in source
    assert "sync_voltage_drop_from_floor" not in source
    assert "Try to maintain baseline clock" in source
    assert "AUTO_UV_PRESET_EFFICIENCY" in source
    assert "QStackedWidget" in source
    assert "preset_advanced_stack.setMinimumHeight" in source
    assert "buttonClicked.connect" in source
    assert "Auto-OC voltage target" in source
    assert "Auto-OC clock target" in source
    assert "auto_uv_performance_target_default" in source
    assert "Auto-OC table target" not in source
    assert "autoOcTargetPreview" not in source
    assert "auto_uv_performance_target_text" not in source
    assert '"auto_oc_target_voltage_mv"' in source
    assert '"auto_oc_target_clock_mhz"' in source
    assert '"power_limit_override_w"' not in source
    assert '"auto_uv_power_limit_w"' in source
    assert "powerLimitSlider" in source
    assert '"auto_uv_min_voltage_mv"' in source
    assert 'options["auto_uv_tail_rise_bins"] = int(preset.tail_rise_bins)' in source
    assert "Core ceiling MHz" not in source
    assert "Voltage ceiling mV" not in source
    assert '"auto_uv_performance_clock_ceiling_mhz"' not in source
    assert '"auto_uv_performance_voltage_ceiling_mv"' not in source
    assert '"penguin-burner-green.png"' in source
    assert '"penguin-burner.png"' in source


def test_advanced_tuning_group_has_breathing_room() -> None:
    assert "QGroupBox#advancedTuningGroup" in STYLESHEET
    assert "margin-top: 12px;" in STYLESHEET
    assert "QGroupBox#autoUvPresetGroup {\n    margin-top: 6px;" in STYLESHEET
    source = Path("ui/dialogs/scan_tuning.py").read_text(encoding="utf-8")
    assert "layout.setContentsMargins(18, 16, 18, 16)" in source
    assert "layout.setSpacing(8)" in source
    assert "preset_layout.setContentsMargins(14, 18, 14, 12)" in source
    assert "advanced_layout.setContentsMargins(18, 28, 18, 16)" in source
    assert "dialog.setMinimumWidth(860)" in source
    assert "dialog.resize(860, dialog.sizeHint().height())" in source
    assert "dialog.adjustSize()" not in source
    assert "dialog.setFixedSize" not in source
    # Row metrics moved into the shared helpers so every settings dialog gets
    # the same spacing rather than each dialog re-deciding it.
    rows = Path("ui/dialogs/form_rows.py").read_text(encoding="utf-8")
    assert "form.setHorizontalSpacing(24)" in rows
    assert "form.setVerticalSpacing(10)" in rows
    assert "label_layout.setContentsMargins(0, 2, 12, 2)" in rows


def test_scan_tuning_power_limit_controls_disabled_when_power_management_disabled():
    import ui.dialogs.scan_tuning as scan_tuning

    values = scan_tuning._power_limit_control_values(
        SimpleNamespace(
            power_management_enabled=False,
            power_limit_set_supported=True,
            power_limit_min_w=40.0,
            power_limit_max_w=80.0,
            power_limit_default_w=55.0,
            power_limit_w=55.0,
        )
    )

    assert values is None


def test_scan_tuning_power_limit_controls_require_daemon_setter_probe():
    import ui.dialogs.scan_tuning as scan_tuning

    values = scan_tuning._power_limit_control_values(
        SimpleNamespace(
            power_management_enabled=True,
            power_limit_set_supported=False,
            power_limit_min_w=40.0,
            power_limit_max_w=80.0,
            power_limit_default_w=55.0,
            power_limit_w=55.0,
        )
    )

    assert values is None


def test_scan_tuning_preset_hover_always_shows_its_tooltip(monkeypatch) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets

    import ui.dialogs.scan_tuning as scan_tuning

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    parent = QtWidgets.QDialog()
    efficiency = QtWidgets.QPushButton("Efficiency", parent)
    balanced = QtWidgets.QPushButton("Balanced", parent)
    efficiency.setToolTip("Efficiency help")
    balanced.setToolTip("Balanced help")
    efficiency.setToolTipDuration(20000)
    balanced.setToolTipDuration(20000)
    shown = []
    hidden = []
    monkeypatch.setattr(
        QtWidgets.QToolTip,
        "showText",
        lambda position, text, widget, rect, duration: shown.append(
            (position, text, widget, duration)
        ),
    )
    monkeypatch.setattr(QtWidgets.QToolTip, "hideText", lambda: hidden.append(True))

    scan_tuning._install_hover_tooltip_filter(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        parent=parent,
        widgets=(efficiency, balanced),
    )

    event_types = getattr(QtCore.QEvent, "Type", QtCore.QEvent)
    enter_type = getattr(event_types, "Enter")
    leave_type = getattr(event_types, "Leave")
    QtWidgets.QApplication.sendEvent(efficiency, QtCore.QEvent(enter_type))
    QtWidgets.QApplication.sendEvent(efficiency, QtCore.QEvent(leave_type))
    QtWidgets.QApplication.sendEvent(balanced, QtCore.QEvent(enter_type))

    assert [(text, widget, duration) for _position, text, widget, duration in shown] == [
        ("Efficiency help", efficiency, 20000),
        ("Balanced help", balanced, 20000),
    ]
    assert hidden == [True]


def test_scan_tuning_unsupported_power_limit_only_omits_power_option(
    monkeypatch,
) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    import ui.dialogs.scan_tuning as scan_tuning

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    monkeypatch.setattr(
        scan_tuning,
        "auto_uv_voltage_drop_default",
        lambda gpu_name=None, **_kwargs: SimpleNamespace(
            gpu_name="NVIDIA GeForce RTX 2050 Laptop GPU",
            value_pct=10.0,
            floor_voltage_mv=None,
        ),
    )
    monkeypatch.setattr(
        scan_tuning,
        "auto_uv_clock_drop_default",
        lambda gpu_index=None, preset_id=None, **_kwargs: SimpleNamespace(value_pct=12.5),
    )
    monkeypatch.setattr(
        scan_tuning,
        "auto_uv_power_limit_default",
        lambda max_w=None, min_w=None, default_w=None, gpu_index=None, preset_id=None: SimpleNamespace(
            watts=43,
            pct=88.0,
            preset_matched=True,
        ),
    )
    monkeypatch.setattr(
        scan_tuning,
        "auto_uv_performance_target_default",
        lambda gpu_name=None, **_kwargs: SimpleNamespace(voltage_mv=900, clock_mhz=2400),
    )
    monkeypatch.setattr(
        scan_tuning,
        "auto_uv_voltage_floor_range_mv",
        lambda gpu_index=None, **_kwargs: (800, 1250),
    )
    monkeypatch.setattr(
        scan_tuning, "memory_offset_mhz_range", lambda **_kwargs: (0, 4000)
    )
    monkeypatch.setattr(
        scan_tuning,
        "gpu_choices_with_fallback",
        lambda selected_index=None: (
            [
                SimpleNamespace(
                    index=0,
                    name="NVIDIA GeForce RTX 2050 Laptop GPU",
                    label="GPU 0 - NVIDIA GeForce RTX 2050 Laptop GPU",
                )
            ],
            0,
        ),
    )
    monkeypatch.setattr(
        scan_tuning,
        "read_auto_uv_nvml_info",
        lambda selected, **_kwargs: SimpleNamespace(
            power_draw_w=42.0,
            power_management_enabled=True,
            power_limit_set_supported=False,
            power_limit_w=43.0,
            power_limit_default_w=60.0,
            power_limit_min_w=35.0,
            power_limit_max_w=80.0,
            graphics_clock_mhz=2100,
            memory_clock_mhz=10501,
            supported_memory_clocks_mhz=(),
            supported_graphics_clock_steps_mhz=(),
        ),
    )

    def accept_without_power_limit(dialog):
        power_slider = dialog.findChild(QtWidgets.QSlider, "powerLimitSlider")
        power_spin = dialog.findChild(QtWidgets.QSpinBox, "powerLimitSpin")
        memory_spin = dialog.findChild(QtWidgets.QSpinBox, "memoryOffsetSpin")
        max_drop_spin = dialog.findChild(QtWidgets.QDoubleSpinBox, "maxClockDropSpin")
        performance_voltage = dialog.findChild(
            QtWidgets.QSpinBox,
            "performanceVoltageSpin",
        )
        performance_clock = dialog.findChild(
            QtWidgets.QSpinBox,
            "performanceClockSpin",
        )
        voltage_floor = dialog.findChild(QtWidgets.QSpinBox, "voltageFloorSpin")
        assert power_slider is not None and power_spin is not None
        assert memory_spin is not None and max_drop_spin is not None
        assert performance_voltage is not None and performance_clock is not None
        assert voltage_floor is not None
        # The mobile fixed power limit grays the power control out entirely,
        # while the other scan controls stay usable.
        assert not power_slider.isEnabled()
        assert not power_spin.isEnabled()
        assert memory_spin.isEnabled()
        assert max_drop_spin.isEnabled()
        # An unknown GPU leaves the floor automatic. The loaded baseline probe
        # will turn this into a 10% drop from its actual starting voltage.
        assert voltage_floor.specialValueText() == "Auto (-10%)"
        assert voltage_floor.value() == voltage_floor.minimum()
        memory_spin.setValue(500)
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(QtWidgets.QDialog, "exec", accept_without_power_limit)

    options = scan_tuning.select_scan_tuning(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        parent=None,
        gpu_index=0,
    )

    # No power limit is ever sent for a mobile GPU: scans run at the vanilla
    # stock limit.
    assert options is not None
    assert "auto_uv_power_limit_w" not in options
    assert "auto_uv_min_voltage_mv" not in options


def test_scan_tuning_dialog_keeps_geometry_stable_between_presets(monkeypatch) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    import ui.dialogs.scan_tuning as scan_tuning

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    dialogs = []
    monkeypatch.setattr(
        scan_tuning,
        "auto_uv_voltage_drop_default",
        lambda gpu_name=None: SimpleNamespace(
            gpu_name="NVIDIA GeForce RTX 5080",
            value_pct=15.0,
        ),
    )
    monkeypatch.setattr(
        scan_tuning,
        "auto_uv_clock_drop_default",
        lambda gpu_name=None, preset_id=None: SimpleNamespace(
            value_pct={
                AUTO_UV_PRESET_EFFICIENCY: 11.1,
                AUTO_UV_PRESET_BALANCED: 6.0,
                AUTO_UV_PRESET_PERFORMANCE: 5.4,
            }.get(preset_id, 11.1)
        ),
    )
    monkeypatch.setattr(
        scan_tuning,
        "auto_uv_power_limit_default",
        lambda max_w=None, min_w=None, default_w=None, gpu_name=None, preset_id=None: (
            SimpleNamespace(
                watts={
                    AUTO_UV_PRESET_EFFICIENCY: 383,
                    AUTO_UV_PRESET_BALANCED: 405,
                    AUTO_UV_PRESET_PERFORMANCE: 450,
                }.get(preset_id, 405),
                pct=None,
                preset_matched=True,
            )
        ),
    )
    monkeypatch.setattr(
        scan_tuning,
        "gpu_choices_with_fallback",
        lambda selected_index=None: (
            [
                SimpleNamespace(
                    index=0,
                    name="NVIDIA GeForce RTX 4090",
                    label="GPU 0 - NVIDIA GeForce RTX 4090",
                ),
                SimpleNamespace(
                    index=1,
                    name="NVIDIA GeForce RTX 5090",
                    label="GPU 1 - NVIDIA GeForce RTX 5090",
                ),
            ],
            1 if selected_index is None else selected_index,
        ),
    )
    monkeypatch.setattr(
        scan_tuning,
        "read_auto_uv_nvml_info",
        lambda selected, gpu_client=None: SimpleNamespace(
            power_draw_w=42.0,
            power_management_enabled=True,
            power_limit_set_supported=True,
            power_limit_w=320.0,
            power_limit_default_w=350.0,
            power_limit_min_w=200.0,
            power_limit_max_w=450.0,
            graphics_clock_mhz=2100,
            memory_clock_mhz=10501,
            supported_memory_clocks_mhz=(810, 5001, 10501),
            supported_graphics_clock_steps_mhz=(210, 3015),
        ),
    )
    monkeypatch.setattr(
        QtWidgets.QDialog,
        "exec",
        lambda dialog: dialogs.append(dialog) or QtWidgets.QDialog.DialogCode.Rejected,
    )

    scan_tuning.select_scan_tuning(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        parent=None,
        gpu_index=1,
    )

    dialog = dialogs[0]
    stack = dialog.findChild(QtWidgets.QStackedWidget)
    gpu_combo = dialog.findChild(QtWidgets.QComboBox, "gpuSelector")
    nvml_info = dialog.findChild(QtWidgets.QLabel, "gpuNvmlInfo")
    advanced_group = dialog.findChild(QtWidgets.QGroupBox, "advancedTuningGroup")
    preset_group = dialog.findChild(QtWidgets.QGroupBox, "autoUvPresetGroup")
    power_limit_slider = dialog.findChild(QtWidgets.QSlider, "powerLimitSlider")
    power_limit_spin = dialog.findChild(QtWidgets.QSpinBox, "powerLimitSpin")
    max_clock_drop_spin = dialog.findChild(QtWidgets.QDoubleSpinBox, "maxClockDropSpin")
    assert dialog.minimumWidth() == 860
    assert gpu_combo is not None
    assert gpu_combo.count() == 2
    assert gpu_combo.currentData() == 1
    assert nvml_info is not None
    assert "Power limit: current 320 W" in nvml_info.text()
    assert "Current draw" not in nvml_info.text()
    assert "42 W" not in nvml_info.text()
    assert "Clocks now: core 2100 MHz | memory 10501 MHz" in nvml_info.text()
    assert "RTX" not in nvml_info.text()
    assert advanced_group is not None
    assert preset_group is not None
    assert power_limit_slider is not None
    assert power_limit_spin is not None
    assert max_clock_drop_spin is not None
    assert stack is not None
    # One Advanced page per profile — no shared adaptive page.
    assert stack.count() == 3
    assert stack.minimumHeight() >= max(
        stack.widget(index).sizeHint().height() for index in range(stack.count())
    )

    def page_control(preset_index: int, object_name: str, cls):
        control = stack.widget(preset_index).findChild(cls, object_name)
        assert control is not None
        return control

    # Every profile page carries its own preset-aware defaults.
    for index, (expected_drop, expected_watts) in enumerate(
        ((11.1, 383), (6.0, 405), (5.4, 450))
    ):
        assert page_control(
            index, "maxClockDropSpin", QtWidgets.QDoubleSpinBox
        ).value() == pytest.approx(expected_drop, abs=0.05)
        power_spin_for_page = page_control(index, "powerLimitSpin", QtWidgets.QSpinBox)
        assert power_spin_for_page.minimum() == 200
        assert power_spin_for_page.maximum() == 450
        assert power_spin_for_page.value() == expected_watts
    advanced_labels = {
        label.text() for label in advanced_group.findChildren(QtWidgets.QLabel)
    }
    assert "Max loaded clock drop" in advanced_labels
    assert "Memory Offset" in advanced_labels
    assert "Power limit" in advanced_labels
    initial_size = dialog.size()
    buttons = {
        str(button.property("presetId")): button
        for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.property("presetId")
    }
    scope_buttons = {
        str(button.property("scopeId")): button
        for button in dialog.findChildren(QtWidgets.QPushButton)
        if button.property("scopeId")
    }
    scan_estimate_note = dialog.findChild(QtWidgets.QLabel, "autoUvScanEstimate")
    assert set(scope_buttons) == {
        scan_tuning.SCAN_SCOPE_FULL,
        scan_tuning.SCAN_SCOPE_SELECTED_PROFILE,
    }
    assert scope_buttons[scan_tuning.SCAN_SCOPE_FULL].isChecked()
    # Label derives from the adaptive estimate table (25-35), not a hardcoded
    # duration.
    assert "Full scan (~25-35 min)" in scope_buttons[
        scan_tuning.SCAN_SCOPE_FULL
    ].text()
    assert preset_group.isEnabled()
    assert stack.currentIndex() == 1
    assert set(buttons) == {
        AUTO_UV_PRESET_EFFICIENCY,
        AUTO_UV_PRESET_BALANCED,
        AUTO_UV_PRESET_PERFORMANCE,
    }
    assert AUTO_UV_PRESET_ADAPTIVE not in buttons
    assert "~10-20 min scan" in buttons[AUTO_UV_PRESET_EFFICIENCY].text()
    assert "~10-20 min scan" in buttons[AUTO_UV_PRESET_BALANCED].text()
    assert "~15-25 min scan" in buttons[AUTO_UV_PRESET_PERFORMANCE].text()
    assert buttons[AUTO_UV_PRESET_EFFICIENCY].text().startswith("1. Efficiency")
    assert buttons[AUTO_UV_PRESET_BALANCED].text().startswith("2. Balanced")
    assert buttons[AUTO_UV_PRESET_PERFORMANCE].text().startswith("3. Performance")
    # The profiles stay clickable in a full scan: each remains tuneable.
    assert all(button.isEnabled() for button in buttons.values())
    assert all(
        button.property("scanIncluded") == "true" for button in buttons.values()
    )
    sequence_note = dialog.findChild(QtWidgets.QLabel, "autoUvPresetSequence")
    assert sequence_note is not None
    assert sequence_note.text() == (
        "Full scan order: Efficiency → Balanced → Performance — "
        "click a profile to tune its settings."
    )
    assert scan_estimate_note is not None
    assert "exclude final verification" in scan_estimate_note.text()

    # Clicking a profile in full-scan mode switches its Advanced page while
    # every profile stays included in the scan.
    buttons[AUTO_UV_PRESET_PERFORMANCE].click()
    assert stack.currentIndex() == 2
    assert advanced_group.title() == "Advanced — Performance"
    assert dialog.size() == initial_size
    assert all(
        button.property("scanIncluded") == "true" for button in buttons.values()
    )
    performance_power_spin = page_control(2, "powerLimitSpin", QtWidgets.QSpinBox)
    efficiency_power_spin = page_control(0, "powerLimitSpin", QtWidgets.QSpinBox)
    # A manual edit latches per profile: it never bleeds into the other pages.
    performance_power_spin.setValue(420)
    assert efficiency_power_spin.value() == 383
    assert page_control(1, "powerLimitSpin", QtWidgets.QSpinBox).value() == 405
    buttons[AUTO_UV_PRESET_EFFICIENCY].click()
    assert stack.currentIndex() == 0
    assert advanced_group.title() == "Advanced — Efficiency"
    assert dialog.size() == initial_size
    assert performance_power_spin.value() == 420

    scope_buttons[scan_tuning.SCAN_SCOPE_SELECTED_PROFILE].click()
    assert preset_group.isEnabled()
    assert all(button.isEnabled() for button in buttons.values())
    assert buttons[AUTO_UV_PRESET_EFFICIENCY].property("scanIncluded") == "true"
    assert buttons[AUTO_UV_PRESET_BALANCED].property("scanIncluded") == "false"
    assert sequence_note.text() == "Choose one profile to scan."
    assert stack.currentIndex() == 0
    buttons[AUTO_UV_PRESET_BALANCED].click()
    assert buttons[AUTO_UV_PRESET_BALANCED].property("scanIncluded") == "true"
    assert buttons[AUTO_UV_PRESET_EFFICIENCY].property("scanIncluded") == "false"
    assert stack.currentIndex() == 1
    scope_buttons[scan_tuning.SCAN_SCOPE_FULL].click()
    assert preset_group.isEnabled()
    assert all(button.isEnabled() for button in buttons.values())
    assert all(
        button.property("scanIncluded") == "true" for button in buttons.values()
    )
    assert stack.currentIndex() == 1


def test_scan_tuning_enter_in_numeric_field_only_commits_value(monkeypatch) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtTest, QtWidgets

    import ui.dialogs.scan_tuning as scan_tuning

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(
        scan_tuning,
        "auto_uv_voltage_drop_default",
        lambda gpu_name=None: SimpleNamespace(
            gpu_name="NVIDIA GeForce RTX 5080",
            value_pct=15.0,
            floor_voltage_mv=850,
        ),
    )
    monkeypatch.setattr(
        scan_tuning,
        "auto_uv_clock_drop_default",
        lambda gpu_name=None, preset_id=None: SimpleNamespace(
            value_pct={
                AUTO_UV_PRESET_EFFICIENCY: 11.1,
                AUTO_UV_PRESET_BALANCED: 6.0,
                AUTO_UV_PRESET_PERFORMANCE: 5.4,
            }.get(preset_id, 6.0)
        ),
    )
    monkeypatch.setattr(
        scan_tuning,
        "gpu_choices_with_fallback",
        lambda selected_index=None: (
            [
                SimpleNamespace(
                    index=0,
                    name="NVIDIA GeForce RTX 5080",
                    label="GPU 0 - NVIDIA GeForce RTX 5080",
                )
            ],
            0,
        ),
    )
    monkeypatch.setattr(
        scan_tuning,
        "read_auto_uv_nvml_info",
        lambda selected, gpu_client=None: SimpleNamespace(
            power_draw_w=42.0,
            power_management_enabled=True,
            power_limit_set_supported=True,
            power_limit_w=360.0,
            power_limit_default_w=360.0,
            power_limit_min_w=330.0,
            power_limit_max_w=390.0,
            graphics_clock_mhz=2100,
            memory_clock_mhz=10501,
            supported_memory_clocks_mhz=(),
            supported_graphics_clock_steps_mhz=(),
        ),
    )

    def reject_after_enter_on_memory_offset(dialog):
        dialog.show()
        app.processEvents()
        accepted = []
        dialog.accepted.connect(lambda: accepted.append(True))
        buttons = {
            str(button.property("presetId")): button
            for button in dialog.findChildren(QtWidgets.QPushButton)
            if button.property("presetId")
        }
        scope_buttons = {
            str(button.property("scopeId")): button
            for button in dialog.findChildren(QtWidgets.QPushButton)
            if button.property("scopeId")
        }
        for button in buttons.values():
            assert not button.autoDefault()
            assert not button.isDefault()
        scope_buttons[scan_tuning.SCAN_SCOPE_SELECTED_PROFILE].click()
        buttons[AUTO_UV_PRESET_PERFORMANCE].click()
        assert buttons[AUTO_UV_PRESET_PERFORMANCE].isChecked()

        memory_spin = dialog.findChild(QtWidgets.QSpinBox, "memoryOffsetSpin")
        assert memory_spin is not None
        memory_spin.setValue(0)
        editor = memory_spin.lineEdit()
        editor.setFocus()
        editor.selectAll()
        app.processEvents()
        QtTest.QTest.keyClicks(editor, "1000")
        QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Return)
        app.processEvents()

        assert memory_spin.value() == 1000
        assert buttons[AUTO_UV_PRESET_PERFORMANCE].isChecked()
        assert not buttons[AUTO_UV_PRESET_EFFICIENCY].isChecked()
        assert accepted == []
        return QtWidgets.QDialog.DialogCode.Rejected

    monkeypatch.setattr(QtWidgets.QDialog, "exec", reject_after_enter_on_memory_offset)

    assert (
        scan_tuning.select_scan_tuning(
            QtCore=QtCore,
            QtGui=QtGui,
            QtWidgets=QtWidgets,
            parent=None,
            gpu_index=0,
        )
        is None
    )


def test_scan_tuning_dialog_returns_power_limit_from_slider(monkeypatch) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    import ui.dialogs.scan_tuning as scan_tuning

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    monkeypatch.setattr(
        scan_tuning,
        "auto_uv_voltage_drop_default",
        lambda gpu_name=None: SimpleNamespace(
            gpu_name="NVIDIA GeForce RTX 5080",
            value_pct=15.0,
            floor_voltage_mv=850,
        ),
    )
    monkeypatch.setattr(
        scan_tuning,
        "auto_uv_clock_drop_default",
        lambda gpu_name=None, preset_id=None: SimpleNamespace(value_pct=6.0),
    )
    monkeypatch.setattr(
        scan_tuning,
        "auto_uv_power_limit_default",
        lambda max_w=None, min_w=None, default_w=None, gpu_name=None, preset_id=None: (
            SimpleNamespace(
                watts=351,
                pct=90.0,
                preset_matched=True,
            )
        ),
    )
    monkeypatch.setattr(
        scan_tuning,
        "gpu_choices_with_fallback",
        lambda selected_index=None: (
            [
                SimpleNamespace(
                    index=0,
                    name="NVIDIA GeForce RTX 5080",
                    label="GPU 0 - NVIDIA GeForce RTX 5080",
                )
            ],
            0,
        ),
    )
    monkeypatch.setattr(
        scan_tuning,
        "read_auto_uv_nvml_info",
        lambda selected, gpu_client=None: SimpleNamespace(
            power_draw_w=42.0,
            power_management_enabled=True,
            power_limit_set_supported=True,
            power_limit_w=360.0,
            power_limit_default_w=360.0,
            power_limit_min_w=330.0,
            power_limit_max_w=390.0,
            graphics_clock_mhz=2100,
            memory_clock_mhz=10501,
            supported_memory_clocks_mhz=(),
            supported_graphics_clock_steps_mhz=(),
        ),
    )

    def accept_with_power_limit(dialog):
        stack = dialog.findChild(QtWidgets.QStackedWidget)
        assert stack is not None
        for index in range(stack.count()):
            spin = stack.widget(index).findChild(QtWidgets.QSpinBox, "powerLimitSpin")
            assert spin is not None
            assert spin.minimum() == 330
            assert spin.maximum() == 390
            # Preset default cap (patched), not the raw NVML default.
            assert spin.value() == 351
        balanced_spin = stack.widget(1).findChild(
            QtWidgets.QSpinBox, "powerLimitSpin"
        )
        balanced_spin.setValue(390)
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(QtWidgets.QDialog, "exec", accept_with_power_limit)

    options = scan_tuning.select_scan_tuning(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        parent=None,
        gpu_index=0,
    )

    assert options is not None
    assert options["auto_uv_mode"] == "adaptive"
    # Full scan: no scan-wide power key; each tier carries its own value and
    # the edited balanced page differs from the untouched siblings.
    assert "auto_uv_power_limit_w" not in options
    assert options["auto_uv_efficiency_power_limit_w"] == 351
    assert options["auto_uv_balanced_power_limit_w"] == 390
    assert options["auto_uv_performance_power_limit_w"] == 351
    assert options["auto_uv_efficiency_max_clock_drop_pct"] == pytest.approx(6.0)
    assert options["auto_uv_balanced_max_clock_drop_pct"] == pytest.approx(6.0)
    assert options["auto_uv_performance_max_clock_drop_pct"] == pytest.approx(6.0)
    assert options["auto_uv_efficiency_memory_offset_mhz"] == 0
    assert "auto_uv_max_clock_drop_pct" not in options
    assert "auto_uv_memory_offset_mhz" not in options
    assert "auto_uv_tail_rise_bins" not in options
    assert options["auto_uv_min_voltage_mv"] == 850


def test_scan_tuning_memory_offset_is_mhz_with_mt_s_shown_and_doubled(
    monkeypatch,
) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    import ui.dialogs.scan_tuning as scan_tuning

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    monkeypatch.setattr(
        scan_tuning,
        "auto_uv_voltage_drop_default",
        lambda gpu_name=None: SimpleNamespace(
            gpu_name="NVIDIA GeForce RTX 5080", value_pct=15.0, floor_voltage_mv=850
        ),
    )
    monkeypatch.setattr(
        scan_tuning,
        "auto_uv_clock_drop_default",
        lambda gpu_name=None, preset_id=None: SimpleNamespace(value_pct=6.0),
    )
    monkeypatch.setattr(
        scan_tuning,
        "auto_uv_power_limit_default",
        lambda max_w=None, min_w=None, default_w=None, gpu_name=None, preset_id=None: (
            SimpleNamespace(watts=None, pct=None, preset_matched=False)
        ),
    )
    monkeypatch.setattr(
        scan_tuning,
        "gpu_choices_with_fallback",
        lambda selected_index=None: (
            [
                SimpleNamespace(
                    index=0,
                    name="NVIDIA GeForce RTX 5080",
                    label="GPU 0 - NVIDIA GeForce RTX 5080",
                )
            ],
            0,
        ),
    )
    monkeypatch.setattr(
        scan_tuning,
        "read_auto_uv_nvml_info",
        lambda selected, gpu_client=None: SimpleNamespace(
            power_draw_w=42.0,
            power_management_enabled=True,
            power_limit_set_supported=True,
            power_limit_w=360.0,
            power_limit_default_w=360.0,
            power_limit_min_w=None,
            power_limit_max_w=None,
            graphics_clock_mhz=2100,
            memory_clock_mhz=10501,
            supported_memory_clocks_mhz=(),
            supported_graphics_clock_steps_mhz=(),
        ),
    )
    # Driver NVML offset range is MT/s; the box works in MHz (half of it).
    monkeypatch.setattr(
        scan_tuning, "memory_offset_mhz_range", lambda **_kwargs: (0, 4000)
    )

    def accept_with_memory_offset(dialog):
        scope_buttons = {
            str(button.property("scopeId")): button
            for button in dialog.findChildren(QtWidgets.QPushButton)
            if button.property("scopeId")
        }
        scope_buttons[scan_tuning.SCAN_SCOPE_SELECTED_PROFILE].click()
        # The checked profile (Balanced) owns the visible Advanced page; its
        # memory offset is the one a selected-profile scan submits.
        stack = dialog.findChild(QtWidgets.QStackedWidget)
        assert stack is not None
        page = stack.currentWidget()
        spin = page.findChild(QtWidgets.QSpinBox, "memoryOffsetSpin")
        label = page.findChild(QtWidgets.QLabel, "memoryOffsetClockLabel")
        assert spin is not None and label is not None
        assert spin.suffix() == " MHz"
        assert spin.maximum() == 2000  # 4000 MT/s range -> 2000 MHz box
        spin.setValue(500)
        # The side label recalculates the MT/s transfer rate (twice the clock).
        assert label.text() == "= +1000 MT/s transfer rate"
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(QtWidgets.QDialog, "exec", accept_with_memory_offset)

    options = scan_tuning.select_scan_tuning(
        QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, parent=None, gpu_index=0
    )

    assert options is not None
    assert options["auto_uv_mode"] == "balanced"
    # Selected 500 MHz clock -> applied NVML offset is 1000 MT/s.
    assert options["auto_uv_memory_offset_mhz"] == 1000


def test_about_dialog_preserves_project_links() -> None:
    assert "https://github.com/sponsors/jpietek" in ABOUT_LINKS_HTML
    assert "https://github.com/jpietek/PenguinBurner/issues" in ABOUT_LINKS_HTML
    assert "Having issues with PenguinBurner?" in ABOUT_LINKS_HTML


def test_top_status_text_does_not_truncate_live_temperature() -> None:
    text = (
        "Auto-UV phase=candidate-live oc-budget=2.91/4.00% "
        "candidate=990mV target=2640MHz elapsed=15.40s running=q2rtx "
        "live=975mV power=294.40W load=busy core_clock=2580MHz temp=60C fan=33%"
    )

    rendered = _top_status_text(text)

    assert rendered.endswith("temp=60C fan=33%")
    assert rendered == text


def test_status_header_candidate_detail_wraps_instead_of_hard_clipping() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    from ui.components.status_header import StatusHeader

    header = StatusHeader(QtCore=QtCore, QtWidgets=QtWidgets)

    assert header.candidate_label.wordWrap() is True


def test_column_width_includes_header_text_width() -> None:
    class FakeFontMetrics:
        def horizontalAdvance(self, text: str) -> int:
            return len(text) * 20

    class FakeHeader:
        def fontMetrics(self):
            return FakeFontMetrics()

    class FakeModel:
        def headerData(self, column, _orientation):
            return {0: "Measured MHz"}[column]

    class FakeTable:
        def __init__(self):
            self.widths = {}

        def horizontalHeader(self):
            return FakeHeader()

        def model(self):
            return FakeModel()

        def setColumnWidth(self, column, width):
            self.widths[column] = width

    class FakeQtCore:
        class Qt:
            Horizontal = 1

    table = FakeTable()

    set_header_fit_column_widths(table, {0: 80}, QtCore=FakeQtCore, padding=34)

    assert table.widths[0] == len("Measured MHz") * 20 + 34


def test_probe_marker_uses_targets_for_probe_start() -> None:
    payload = {
        "voltage_mv": 875,
        "clock_mhz": 2625,
        "measured_voltage_mv": 868.25,
        "measured_clock_mhz": 2608.5,
    }

    assert _probe_marker_values(payload, prefer_measured=False) == (875, 2625)


def test_probe_marker_uses_measured_values_for_live_samples() -> None:
    payload = {
        "voltage_mv": 875,
        "clock_mhz": 2625,
        "measured_voltage_mv": 868.25,
        "measured_clock_mhz": 2608.5,
    }

    assert _probe_marker_values(payload, prefer_measured=True) == (868.25, 2608.5)


def test_probe_marker_does_not_use_targets_for_live_samples() -> None:
    payload = {
        "voltage_mv": 875,
        "clock_mhz": 2625,
    }

    assert _probe_marker_values(payload, prefer_measured=True) == (None, None)


def test_probe_axis_badge_includes_live_value_and_units() -> None:
    assert _axis_value_badge_text(868.25, "mV") == "868 mV"
    assert _axis_value_badge_text(2608.5, "MHz") == "2608 MHz"


def test_curve_plot_nearest_point_uses_view_scaled_distance() -> None:
    point = _nearest_curve_point(
        901,
        2104,
        [(850, 1900), (900, 2100), (950, 2100)],
        [[800, 1000], [1700, 2300]],
    )

    assert point == (900.0, 2100.0)
    assert (
        _nearest_curve_point(
            760,
            1300,
            [(850, 1900), (900, 2100)],
            [[800, 1000], [1700, 2300]],
        )
        is None
    )
