from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.support import runtime_service


CANONICAL_DAEMON_BINARY = (
    "/var/opt/penguin-burner/libexec/penguin-burnerd"
)
NATIVE_PACKAGE_DAEMON_BINARY = "/usr/libexec/penguin-burnerd"
# Unit builders may receive the canonical path explicitly, but never a package
# or user-writable source path.
RUST_DAEMON_BINARY = CANONICAL_DAEMON_BINARY


FLATPAK_APP_PATH = (
    "/home/jp/.local/share/flatpak/app/io.github.jpietek.PenguinBurner/"
    "current/active/files"
)
FLATPAK_SITE_PACKAGES = f"{FLATPAK_APP_PATH}/lib/python3.13/site-packages"
FLATPAK_DEPLOYMENT_ID = "a" * 64
FLATPAK_DEPLOYMENT_APP_PATH = (
    "/home/jp/.local/share/flatpak/app/io.github.jpietek.PenguinBurner/"
    f"x86_64/master/{FLATPAK_DEPLOYMENT_ID}/files"
)
FLATPAK_ACTIVE_APP_PATH = (
    "/home/jp/.local/share/flatpak/app/io.github.jpietek.PenguinBurner/"
    "x86_64/master/active/files"
)
FLATPAK_ACTIVE_SITE_PACKAGES = (
    f"{FLATPAK_ACTIVE_APP_PATH}/lib/python3.13/site-packages"
)


@pytest.fixture(autouse=True)
def _no_live_daemon_runtime_calls(monkeypatch):
    # Lifecycle tests simulate the root-only file owner inside a user-owned
    # tmp_path. Production constants remain UID/GID 0, and a daemon already
    # installed on the test host must never become test input.
    live_daemon_binary = runtime_service.DAEMON_BINARY
    capture_managed_file = runtime_service._ManagedFileRollback.capture

    def capture_isolated_managed_file(path: Path):
        path = Path(path)
        if path == live_daemon_binary:
            return runtime_service._ManagedFileRollback(
                path=path,
                existed=False,
                backup_path=None,
            )
        return capture_managed_file(path)

    monkeypatch.setattr(runtime_service, "ROOT_UID", os.getuid())
    monkeypatch.setattr(runtime_service, "ROOT_GID", os.getgid())
    monkeypatch.setattr(
        runtime_service._ManagedFileRollback,
        "capture",
        capture_isolated_managed_file,
    )
    monkeypatch.setattr(
        runtime_service,
        "_stop_active_runtime_before_daemon_restart",
        lambda: None,
    )
    monkeypatch.setattr(
        runtime_service,
        "_apply_runtime",
        lambda _argv, **_kwargs: ({"pid": 4321}, {"format_version": 1}),
    )
    monkeypatch.setattr(
        runtime_service,
        "_apply_persistent_runtime",
        lambda _argv, **_kwargs: {"pid": 4321},
    )


def _write_fake_elf(path: Path, payload: bytes = b"daemon") -> None:
    path.write_bytes(b"\x7fELF" + payload)


def test_daemon_runtime_target_is_distinct_from_native_package_source() -> None:
    assert str(runtime_service.DAEMON_BINARY) == CANONICAL_DAEMON_BINARY
    assert (
        str(runtime_service.NATIVE_PACKAGE_DAEMON_BINARY)
        == NATIVE_PACKAGE_DAEMON_BINARY
    )

    unit = runtime_service.build_daemon_api_service_unit("/tmp/penguin_burner.py")

    assert f"ExecStart={CANONICAL_DAEMON_BINARY} " in unit
    assert f"ExecStart={NATIVE_PACKAGE_DAEMON_BINARY} " not in unit


def test_daemon_systemd_unit_uses_rust_binary_and_program_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "site-packages" / "penguin_burner.py"
    program.parent.mkdir()
    program.write_text("# program\n", encoding="utf-8")
    monkeypatch.setenv("PENGUIN_BURNER_ADAPTIVE_TARGET_FPS", "60")

    # Native unit runs the Rust daemon binary with --socket (no launcher, no
    # python --daemon-api). Autostart no longer travels as a unit env.
    unit = runtime_service.build_daemon_api_service_unit(
        program,
        binary_path=RUST_DAEMON_BINARY,
    )

    assert (
        "ExecStart=/var/opt/penguin-burner/libexec/penguin-burnerd "
        "--socket /run/penguin-burnerd.sock"
    ) in unit
    assert "Type=notify" in unit
    assert "WatchdogSec=30" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=2" in unit
    # PENGUIN_BURNER_DAEMON_PROGRAM_FILE points at the Python CLI (scan children).
    assert (
        f"Environment=PENGUIN_BURNER_DAEMON_PROGRAM_FILE={program.resolve()}" in unit
    )
    assert "PENGUIN_BURNER_ADAPTIVE_TARGET_FPS" not in unit
    assert "PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64" not in unit
    assert "--daemon-api" not in unit
    assert "penguin_burner.sh" not in unit
    assert "SyslogIdentifier=penguin-burnerd" in unit


def test_daemon_unit_never_executes_packaged_or_dev_binary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "checkout" / "penguin_burner.py"
    packaged = tmp_path / "user-site" / "penguin-burnerd"
    program.parent.mkdir()
    packaged.parent.mkdir()
    program.write_text("# program\n", encoding="utf-8")
    _write_fake_elf(packaged)
    monkeypatch.setattr(
        runtime_service, "_packaged_daemon_binary", lambda: packaged
    )

    unit = runtime_service.build_daemon_api_service_unit(program)

    assert f"ExecStart={CANONICAL_DAEMON_BINARY} " in unit
    assert str(packaged) not in unit
    with pytest.raises(RuntimeError, match="must be installed at"):
        runtime_service.build_daemon_api_service_unit(
            program,
            binary_path=packaged,
        )


def test_daemon_install_source_prefers_current_payload_over_installed_copy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    packaged = tmp_path / "packaged" / "penguin-burnerd"
    dev = tmp_path / "dev" / "penguin-burnerd"
    native_package = tmp_path / "usr" / "libexec" / "penguin-burnerd"
    installed = (
        tmp_path
        / "var"
        / "opt"
        / "penguin-burner"
        / "libexec"
        / "penguin-burnerd"
    )
    for path in (packaged, dev, native_package, installed):
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_fake_elf(path, path.parent.name.encode("ascii"))
    monkeypatch.setattr(runtime_service, "_packaged_daemon_binary", lambda: packaged)
    monkeypatch.setattr(runtime_service, "_dev_daemon_binary", lambda _program: dev)
    monkeypatch.setattr(
        runtime_service,
        "NATIVE_PACKAGE_DAEMON_BINARY",
        native_package,
    )
    monkeypatch.setattr(runtime_service, "DAEMON_BINARY", installed)

    assert runtime_service.daemon_install_source_path("program.py") == packaged
    packaged.unlink()
    assert runtime_service.daemon_install_source_path("program.py") == dev
    dev.unlink()
    assert runtime_service.daemon_install_source_path("program.py") == native_package
    native_package.unlink()
    assert runtime_service.daemon_install_source_path("program.py") == installed


def test_daemon_install_source_has_no_alternate_runtime_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    missing_packaged = tmp_path / "missing-packaged"
    missing_dev = tmp_path / "missing-dev"
    missing_native = tmp_path / "missing-native"
    missing_canonical = tmp_path / "missing-canonical"
    monkeypatch.setattr(
        runtime_service,
        "_packaged_daemon_binary",
        lambda: missing_packaged,
    )
    monkeypatch.setattr(
        runtime_service,
        "_dev_daemon_binary",
        lambda _program: missing_dev,
    )
    monkeypatch.setattr(
        runtime_service,
        "NATIVE_PACKAGE_DAEMON_BINARY",
        missing_native,
    )
    monkeypatch.setattr(runtime_service, "DAEMON_BINARY", missing_canonical)

    with pytest.raises(RuntimeError, match="install source not found") as exc_info:
        runtime_service.daemon_install_source_path("program.py")

    assert str(missing_canonical) in str(exc_info.value)
    assert "/opt/" not in str(exc_info.value)


def test_atomic_daemon_install_rejects_unsafe_existing_mode(tmp_path: Path) -> None:
    source = tmp_path / "source" / "penguin-burnerd"
    destination = tmp_path / "libexec" / "penguin-burnerd"
    source.parent.mkdir()
    _write_fake_elf(source, b"new daemon")
    owner = os.geteuid()
    group = os.getegid()

    assert runtime_service._atomic_install_daemon_binary(
        source,
        destination,
        owner_uid=owner,
        owner_gid=group,
    )
    assert destination.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755
    assert not runtime_service._atomic_install_daemon_binary(
        source,
        destination,
        owner_uid=owner,
        owner_gid=group,
    )

    destination.chmod(0o775)
    with pytest.raises(RuntimeError, match="safe ELF executable"):
        runtime_service._atomic_install_daemon_binary(
            source,
            destination,
            owner_uid=owner,
            owner_gid=group,
        )
    assert stat.S_IMODE(destination.stat().st_mode) == 0o775
    assert not list(destination.parent.glob(".penguin-burnerd.*"))


def test_daemon_install_relabels_product_tree_when_selinux_is_active(
    tmp_path: Path,
    monkeypatch,
) -> None:
    product_root = tmp_path / "penguin-burner"
    product_root.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr(runtime_service, "_selinux_is_active", lambda: True)
    monkeypatch.setattr(
        runtime_service.shutil,
        "which",
        lambda command: "/sbin/restorecon" if command == "restorecon" else None,
    )
    monkeypatch.setattr(
        runtime_service,
        "run_checked_subprocess",
        lambda args: calls.append(list(args))
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    runtime_service._restore_daemon_selinux_context(product_root)

    assert calls == [["/sbin/restorecon", "-RF", str(product_root)]]


def test_daemon_install_has_no_restorecon_dependency_without_selinux(
    tmp_path: Path,
    monkeypatch,
) -> None:
    product_root = tmp_path / "penguin-burner"
    product_root.mkdir()
    monkeypatch.setattr(runtime_service, "_selinux_is_active", lambda: False)
    monkeypatch.setattr(
        runtime_service.shutil,
        "which",
        lambda command: pytest.fail(f"looked up unexpected command: {command}"),
    )

    runtime_service._restore_daemon_selinux_context(product_root)


def test_daemon_install_rejects_noexec_target_mount(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target_dir = tmp_path / "libexec"
    target_dir.mkdir()
    monkeypatch.setattr(
        runtime_service.shutil,
        "which",
        lambda command: "/usr/bin/findmnt" if command == "findmnt" else None,
    )
    monkeypatch.setattr(
        runtime_service.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="rw,nosuid,nodev,noexec,relatime\n",
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="mounted noexec"):
        runtime_service._reject_noexec_daemon_target(target_dir)


def test_daemon_install_tree_rejects_symlinked_base(tmp_path: Path) -> None:
    real_base = tmp_path / "real-opt"
    install_base = tmp_path / "var" / "opt"
    install_base.parent.mkdir()
    real_base.mkdir()
    install_base.symlink_to(real_base, target_is_directory=True)
    destination = install_base / "penguin-burner" / "libexec" / "penguin-burnerd"

    with pytest.raises(RuntimeError, match="safe root-owned real directory"):
        runtime_service._ensure_safe_daemon_install_tree(
            destination,
            owner_uid=os.geteuid(),
        )

    assert not (real_base / "penguin-burner").exists()


def test_legacy_state_cleanup_preserves_current_boot_spec(
    tmp_path: Path,
    monkeypatch,
) -> None:
    legacy_state = tmp_path / "last-runtime.json"
    boot_state = tmp_path / "boot-runtime.json"
    legacy_state.write_text("legacy\n", encoding="utf-8")
    boot_state.write_text("boot\n", encoding="utf-8")
    monkeypatch.setattr(runtime_service, "LAST_RUNTIME_STATE_PATH", legacy_state)
    monkeypatch.setattr(runtime_service, "BOOT_RUNTIME_STATE_PATH", boot_state)

    runtime_service.clear_last_runtime_state()

    assert not legacy_state.exists()
    assert boot_state.read_text(encoding="utf-8") == "boot\n"


def test_existing_daemon_fallback_rejects_writable_mode(tmp_path: Path) -> None:
    installed = tmp_path / "libexec" / "penguin-burnerd"
    installed.parent.mkdir()
    _write_fake_elf(installed)
    installed.chmod(0o775)

    with pytest.raises(RuntimeError, match="safe ELF executable"):
        runtime_service._atomic_install_daemon_binary(
            installed,
            installed,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )

    installed.chmod(0o750)
    assert not runtime_service._atomic_install_daemon_binary(
        installed,
        installed,
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
    )


def test_atomic_daemon_install_rejects_destination_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source" / "penguin-burnerd"
    victim = tmp_path / "victim"
    destination = tmp_path / "libexec" / "penguin-burnerd"
    source.parent.mkdir()
    destination.parent.mkdir()
    _write_fake_elf(source, b"same bytes")
    victim.write_bytes(source.read_bytes())
    destination.symlink_to(victim)

    with pytest.raises(RuntimeError, match="safe ELF executable"):
        runtime_service._atomic_install_daemon_binary(
            source,
            destination,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )
    assert victim.read_bytes() == source.read_bytes()
    assert destination.is_symlink()
    assert destination.read_bytes() == source.read_bytes()


def test_atomic_daemon_install_rejects_source_symlink(tmp_path: Path) -> None:
    real_source = tmp_path / "real-source"
    source = tmp_path / "source-link"
    destination = tmp_path / "libexec" / "penguin-burnerd"
    destination.parent.mkdir()
    _write_fake_elf(real_source, b"new")
    _write_fake_elf(destination, b"old")
    destination.chmod(0o755)
    source.symlink_to(real_source)
    old_bytes = destination.read_bytes()

    with pytest.raises(RuntimeError, match="cannot safely open"):
        runtime_service._atomic_install_daemon_binary(
            source,
            destination,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )

    assert destination.read_bytes() == old_bytes


def test_atomic_daemon_install_failure_keeps_old_binary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source" / "penguin-burnerd"
    destination = tmp_path / "libexec" / "penguin-burnerd"
    source.parent.mkdir()
    destination.parent.mkdir()
    _write_fake_elf(source, b"new")
    _write_fake_elf(destination, b"old")
    destination.chmod(0o755)
    old_bytes = destination.read_bytes()

    def fail_before_replace(_descriptor: int) -> None:
        raise OSError("injected copy failure")

    monkeypatch.setattr(runtime_service.os, "fsync", fail_before_replace)
    with pytest.raises(RuntimeError, match="injected copy failure"):
        runtime_service._atomic_install_daemon_binary(
            source,
            destination,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )

    assert destination.read_bytes() == old_bytes
    assert not list(destination.parent.glob(".penguin-burnerd.*"))


def test_daemon_systemd_unit_does_not_freeze_adaptive_policy_in_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    program.write_text("# program\n", encoding="utf-8")
    monkeypatch.setenv("PENGUIN_BURNER_ADAPTIVE_TARGET_FPS", "50")

    unit = runtime_service.build_daemon_api_service_unit(
        program,
        binary_path=RUST_DAEMON_BINARY,
    )

    assert "PENGUIN_BURNER_ADAPTIVE_TARGET_FPS" not in unit


def test_daemon_api_unit_uses_daemon_socket_and_program_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    program.write_text("# program\n", encoding="utf-8")

    unit = runtime_service.build_daemon_api_service_unit(
        program,
        socket_path="/run/penguin-burnerd.sock",
        binary_path=RUST_DAEMON_BINARY,
    )

    assert (
        "ExecStart=/var/opt/penguin-burner/libexec/penguin-burnerd "
        "--socket /run/penguin-burnerd.sock"
    ) in unit
    assert (
        f"Environment=PENGUIN_BURNER_DAEMON_PROGRAM_FILE={program.resolve()}" in unit
    )
    # Boot persistence is a typed spec sent over the socket, not a unit env.
    assert "PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64" not in unit
    assert "PenguinBurner.service" not in unit


def test_daemon_api_unit_preserves_desktop_profile_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    program.write_text("# program\n", encoding="utf-8")
    monkeypatch.setenv("SUDO_USER", "jp")
    monkeypatch.setenv("PENGUIN_BURNER_HOME", "/home/jp")
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_UID", "1000")
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_GID", "1000")
    monkeypatch.setenv("PENGUIN_BURNER_ADAPTIVE_TARGET_FPS", "60")

    unit = runtime_service.build_daemon_api_service_unit(
        program,
        socket_path="/run/penguin-burnerd.sock",
        binary_path=RUST_DAEMON_BINARY,
    )

    assert "Environment=SUDO_USER=jp" in unit
    assert "Environment=PENGUIN_BURNER_HOME=/home/jp" in unit
    assert "Environment=PENGUIN_BURNER_Q2RTX_UID=1000" in unit
    assert "Environment=PENGUIN_BURNER_Q2RTX_GID=1000" in unit
    assert "PENGUIN_BURNER_ADAPTIVE_TARGET_FPS" not in unit


def test_parse_runtime_argv_from_legacy_unit() -> None:
    unit = """
[Service]
ExecStart=/usr/bin/python3 /opt/pb/penguin_burner.py --auto-uv-profile profile-a --silent-fan-curve --gpu-index 1
"""

    argv = runtime_service.parse_runtime_argv_from_unit_text(unit)

    assert argv == [
        "--auto-uv-profile",
        "profile-a",
        "--silent-fan-curve",
        "--gpu-index",
        "1",
    ]


@pytest.mark.parametrize("operation", ["install", "migrate", "daemonize"])
def test_daemon_binary_install_failure_precedes_service_mutation(
    operation: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    legacy_unit = tmp_path / "PenguinBurner.service"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    state_file = tmp_path / "last-runtime.json"
    program.write_text("# program\n", encoding="utf-8")
    legacy_unit.write_text("legacy marker\n", encoding="utf-8")
    daemon_unit.write_text("daemon marker\n", encoding="utf-8")
    state_file.write_text("state marker\n", encoding="utf-8")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime_service, "LAST_RUNTIME_STATE_PATH", state_file)
    monkeypatch.setattr(
        runtime_service, "legacy_systemd_service_unit_path", lambda: legacy_unit
    )
    monkeypatch.setattr(
        runtime_service, "daemon_systemd_service_unit_path", lambda: daemon_unit
    )
    monkeypatch.setattr(
        runtime_service,
        "read_legacy_service_state",
        lambda: {
            "exists": True,
            "enabled": True,
            "active": True,
            "runtime_argv": ["--auto-uv-profile", "profile-a"],
        },
    )

    def fail_install(*_args, **_kwargs):
        raise RuntimeError("injected daemon copy failure")

    monkeypatch.setattr(runtime_service, "install_daemon_binary", fail_install)
    monkeypatch.setattr(
        runtime_service.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("systemctl ran after copy failure"),
    )
    monkeypatch.setattr(
        runtime_service,
        "run_checked_subprocess",
        lambda *_args, **_kwargs: pytest.fail("systemctl ran after copy failure"),
    )

    with pytest.raises(RuntimeError, match="injected daemon copy failure"):
        if operation == "install":
            runtime_service.install_systemd_service(
                program,
                ["--auto-uv-profile", "profile-a"],
                journal_hours=4,
                log=lambda _message: None,
            )
        elif operation == "migrate":
            runtime_service.migrate_to_daemon_service(
                program,
                log=lambda _message: None,
            )
        else:
            runtime_service.daemonize_with_systemd(
                program,
                ["--auto-uv-profile", "profile-a"],
                journal_hours=4,
                log=lambda _message: None,
            )

    assert legacy_unit.read_text(encoding="utf-8") == "legacy marker\n"
    assert daemon_unit.read_text(encoding="utf-8") == "daemon marker\n"
    assert state_file.read_text(encoding="utf-8") == "state marker\n"


def test_install_readiness_failure_restores_previous_binary_units_and_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    daemon_binary = tmp_path / "var" / "opt" / "penguin-burner" / "libexec" / "penguin-burnerd"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    legacy_unit = tmp_path / "PenguinBurner.service"
    daemon_binary.parent.mkdir(parents=True)
    program.write_text("# program\n", encoding="utf-8")
    legacy_state = tmp_path / "last-runtime.json"
    boot_state = tmp_path / "boot-runtime.json"
    legacy_state.write_text("legacy state\n", encoding="utf-8")
    boot_state.write_text("boot state\n", encoding="utf-8")
    _write_fake_elf(daemon_binary, b"old daemon")
    daemon_binary.chmod(0o755)
    daemon_unit.write_text("old daemon unit\n", encoding="utf-8")
    legacy_unit.write_text("old legacy unit\n", encoding="utf-8")
    old_binary = daemon_binary.read_bytes()
    actions: list[list[str]] = []

    monkeypatch.setattr(runtime_service, "DAEMON_BINARY", daemon_binary)
    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime_service, "LAST_RUNTIME_STATE_PATH", legacy_state)
    monkeypatch.setattr(runtime_service, "BOOT_RUNTIME_STATE_PATH", boot_state)
    monkeypatch.setattr(
        runtime_service, "daemon_systemd_service_unit_path", lambda: daemon_unit
    )
    monkeypatch.setattr(
        runtime_service, "legacy_systemd_service_unit_path", lambda: legacy_unit
    )

    def fake_install(*_args, **_kwargs):
        staged_binary = daemon_binary.with_name("staged-penguin-burnerd")
        _write_fake_elf(staged_binary, b"broken new daemon")
        staged_binary.chmod(0o755)
        os.replace(staged_binary, daemon_binary)
        return runtime_service.DaemonBinaryRefresh(
            changed=True,
            source=Path("/package/penguin-burnerd"),
            destination=daemon_binary,
        )

    def fake_run(args, **_kwargs):
        call = list(args)
        actions.append(call)
        if call[1:3] == ["is-enabled", "penguin-burnerd.service"]:
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        if call[1:3] == ["is-active", "penguin-burnerd.service"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        if call[1:3] == ["is-enabled", "PenguinBurner.service"]:
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        if call[1:3] == ["is-active", "PenguinBurner.service"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime_service, "install_daemon_binary", fake_install)
    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)
    monkeypatch.setattr(
        runtime_service,
        "run_checked_subprocess",
        lambda args: actions.append(list(args))
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    readiness_checks = {"count": 0}

    def fake_wait(*_args):
        readiness_checks["count"] += 1
        if readiness_checks["count"] == 1:
            raise RuntimeError("new daemon failed readiness")

    monkeypatch.setattr(runtime_service, "_wait_for_daemon_status", fake_wait)

    with pytest.raises(RuntimeError, match="new daemon failed readiness"):
        runtime_service.install_systemd_service(
            program,
            [],
            journal_hours=4,
            log=lambda _message: None,
        )

    assert daemon_binary.read_bytes() == old_binary
    assert daemon_unit.read_text(encoding="utf-8") == "old daemon unit\n"
    assert legacy_unit.read_text(encoding="utf-8") == "old legacy unit\n"
    assert legacy_state.read_text(encoding="utf-8") == "legacy state\n"
    assert boot_state.read_text(encoding="utf-8") == "boot state\n"
    assert ["/bin/systemctl", "enable", "penguin-burnerd.service"] in actions
    assert ["/bin/systemctl", "start", "penguin-burnerd.service"] in actions
    assert ["/bin/systemctl", "enable", "PenguinBurner.service"] in actions
    assert ["/bin/systemctl", "start", "PenguinBurner.service"] in actions
    assert [
        "/bin/systemctl",
        "disable",
        "--now",
        "penguin-burnerd.service",
    ] in actions
    assert [
        "/bin/systemctl",
        "disable",
        "--now",
        "PenguinBurner.service",
    ] in actions
    assert readiness_checks["count"] == 2
    assert not list(daemon_binary.parent.glob("*.rollback.*"))


def test_install_transaction_reports_original_and_rollback_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime_service,
        "DAEMON_BINARY",
        tmp_path / "missing" / "penguin-burnerd",
    )
    monkeypatch.setattr(
        runtime_service,
        "daemon_systemd_service_unit_path",
        lambda: tmp_path / "missing-daemon.service",
    )
    monkeypatch.setattr(
        runtime_service,
        "legacy_systemd_service_unit_path",
        lambda: tmp_path / "missing-legacy.service",
    )
    transaction = runtime_service._DaemonServiceInstallTransaction(
        log=lambda _message: None,
    )

    def fail_rollback() -> None:
        raise RuntimeError("injected rollback failure")

    monkeypatch.setattr(transaction, "_rollback", fail_rollback)

    with pytest.raises(RuntimeError) as exc_info:
        with transaction:
            raise RuntimeError("injected readiness failure")

    message = str(exc_info.value)
    assert "injected readiness failure" in message
    assert "injected rollback failure" in message


def test_install_transaction_reports_failed_service_stop_during_rollback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime_service,
        "DAEMON_BINARY",
        tmp_path / "missing" / "penguin-burnerd",
    )
    monkeypatch.setattr(
        runtime_service,
        "daemon_systemd_service_unit_path",
        lambda: tmp_path / "missing-daemon.service",
    )
    monkeypatch.setattr(
        runtime_service,
        "legacy_systemd_service_unit_path",
        lambda: tmp_path / "missing-legacy.service",
    )
    transaction = runtime_service._DaemonServiceInstallTransaction(
        log=lambda _message: None,
    )
    transaction._states = {
        "penguin-burnerd.service": runtime_service._SystemdUnitState(
            enabled=False,
            active=False,
        ),
        "PenguinBurner.service": runtime_service._SystemdUnitState(
            enabled=False,
            active=False,
        ),
    }

    def fail_daemon_stop(args):
        if list(args) == [
            "/bin/systemctl",
            "disable",
            "--now",
            "penguin-burnerd.service",
        ]:
            raise RuntimeError("injected stop failure")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "run_checked_subprocess", fail_daemon_stop)

    with pytest.raises(RuntimeError) as exc_info:
        with transaction:
            raise RuntimeError("injected readiness failure")

    message = str(exc_info.value)
    assert "injected readiness failure" in message
    assert "could not stop and disable penguin-burnerd.service" in message
    assert "injected stop failure" in message


def test_install_systemd_service_replaces_transient_unit_before_enabling(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    legacy_unit = tmp_path / "PenguinBurner.service"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    state_file = tmp_path / "last-runtime.json"
    program.write_text("# program\n", encoding="utf-8")
    legacy_unit.write_text("old unit\n", encoding="utf-8")
    actions = []
    logs = []
    binary_paths = []
    build_unit = runtime_service.build_daemon_api_service_unit

    def capture_binary_path(*args, **kwargs):
        binary_paths.append(kwargs.get("binary_path"))
        return build_unit(*args, **kwargs)

    def fake_run(args, **_kwargs):
        actions.append(("run", list(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run_checked(args):
        actions.append(("checked", list(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("SUDO_USER", "jp")
    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "install_daemon_binary",
        lambda *_a, **_k: runtime_service.DaemonBinaryRefresh(
            changed=False, source=Path("/pkg/penguin-burnerd")
        ),
    )
    monkeypatch.setattr(
        runtime_service,
        "build_daemon_api_service_unit",
        capture_binary_path,
    )
    monkeypatch.setattr(
        runtime_service, "LAST_RUNTIME_STATE_PATH", state_file
    )
    monkeypatch.setattr(
        runtime_service,
        "clear_last_runtime_state",
        lambda: actions.append(("clear-last-runtime", [])),
    )
    monkeypatch.setattr(
        runtime_service,
        "_apply_persistent_runtime",
        lambda argv, **_kwargs: actions.append(("persist-runtime", list(argv)))
        or {"pid": 4321},
    )
    monkeypatch.setattr(
        runtime_service,
        "legacy_systemd_service_unit_path",
        lambda: legacy_unit,
    )
    monkeypatch.setattr(
        runtime_service,
        "daemon_systemd_service_unit_path",
        lambda: daemon_unit,
    )
    monkeypatch.setattr(runtime_service, "_wait_for_daemon_status", lambda *_args: None)
    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_service, "run_checked_subprocess", fake_run_checked)

    runtime_service.install_systemd_service(
        program,
        ["--auto-uv-profile", "profile-a"],
        journal_hours=4,
        log=logs.append,
    )

    assert (
        "run",
        ["/bin/systemctl", "disable", "--now", "PenguinBurner.service"],
    ) in actions
    assert (
        "checked",
        ["/bin/systemctl", "enable", "penguin-burnerd.service"],
    ) in actions
    assert (
        "checked",
        ["/bin/systemctl", "restart", "penguin-burnerd.service"],
    ) in actions
    assert actions.index(
        (
            "checked",
            ["/bin/systemctl", "restart", "penguin-burnerd.service"],
        )
    ) < actions.index(("clear-last-runtime", [])) < actions.index(
        ("persist-runtime", ["--auto-uv-profile", "profile-a"])
    )
    unit = daemon_unit.read_text(encoding="utf-8")
    assert (
        "ExecStart=/var/opt/penguin-burner/libexec/penguin-burnerd "
        "--socket /run/penguin-burnerd.sock"
    ) in unit
    assert binary_paths == [runtime_service.DAEMON_BINARY]
    assert "PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64" not in unit
    assert ("persist-runtime", ["--auto-uv-profile", "profile-a"]) in actions
    assert actions.index(
        ("checked", ["/bin/systemctl", "restart", "penguin-burnerd.service"])
    ) < actions.index(("persist-runtime", ["--auto-uv-profile", "profile-a"]))
    assert any("persistent service install" in message for message in logs)


def test_describe_daemon_binary_refresh_messages() -> None:
    updated = runtime_service.describe_daemon_binary_refresh(
        runtime_service.DaemonBinaryRefresh(
            changed=True, source=Path("/pkg/penguin-burnerd")
        )
    )
    assert "updated" in updated
    assert "/pkg/penguin-burnerd" in updated

    unchanged = runtime_service.describe_daemon_binary_refresh(
        runtime_service.DaemonBinaryRefresh(
            changed=False, source=Path("/pkg/penguin-burnerd")
        )
    )
    assert "already current" in unchanged

    # Source resolving to the installed copy itself means this install ships
    # no daemon payload: the "refresh" was a no-op against a possibly stale
    # binary and the message must say so loudly (issue #30).
    kept = runtime_service.describe_daemon_binary_refresh(
        runtime_service.DaemonBinaryRefresh(
            changed=False,
            source=runtime_service.DAEMON_BINARY,
        )
    )
    assert "KEPT" in kept
    assert "no daemon payload" in kept


def test_install_systemd_service_without_argv_preserves_boot_spec(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    program.write_text("# program\n", encoding="utf-8")
    actions = []
    logs = []

    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "install_daemon_binary",
        lambda *_a, **_k: runtime_service.DaemonBinaryRefresh(
            changed=True, source=Path("/pkg/penguin-burnerd")
        ),
    )
    monkeypatch.setattr(runtime_service, "clear_last_runtime_state", lambda: None)
    monkeypatch.setattr(
        runtime_service,
        "clear_existing_penguin_burner_unit_for_install",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        runtime_service,
        "_apply_persistent_runtime",
        lambda argv, **_kwargs: actions.append(("persist-runtime", list(argv)))
        or {"pid": 4321},
    )
    monkeypatch.setattr(
        runtime_service, "daemon_systemd_service_unit_path", lambda: daemon_unit
    )
    monkeypatch.setattr(runtime_service, "_wait_for_daemon_status", lambda *_args: None)
    monkeypatch.setattr(
        runtime_service.subprocess,
        "run",
        lambda args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        runtime_service,
        "run_checked_subprocess",
        lambda args: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    runtime_service.install_systemd_service(
        program, [], journal_hours=4, log=logs.append
    )

    # An install/repair with no profile argv must not rewrite or clear the
    # boot spec — the existing boot profile survives the reinstall.
    assert actions == []
    assert any(message.startswith("Daemon binary: updated") for message in logs)


def test_install_systemd_service_restarts_active_daemon_after_unit_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    legacy_unit = tmp_path / "PenguinBurner.service"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    state_file = tmp_path / "last-runtime.json"
    program.write_text("# program\n", encoding="utf-8")
    actions = []
    waited = []

    def fake_run(args, **_kwargs):
        actions.append(("run", list(args)))
        if list(args) == ["/bin/systemctl", "is-active", "penguin-burnerd.service"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run_checked(args):
        actions.append(("checked", list(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("SUDO_USER", "jp")
    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "install_daemon_binary",
        lambda *_a, **_k: runtime_service.DaemonBinaryRefresh(
            changed=False, source=Path("/pkg/penguin-burnerd")
        ),
    )
    monkeypatch.setattr(
        runtime_service, "LAST_RUNTIME_STATE_PATH", state_file
    )
    monkeypatch.setattr(
        runtime_service,
        "clear_last_runtime_state",
        lambda: actions.append(("clear-last-runtime", [])),
    )
    monkeypatch.setattr(
        runtime_service,
        "_apply_persistent_runtime",
        lambda argv, **_kwargs: actions.append(("persist-runtime", list(argv)))
        or {"pid": 4321},
    )
    monkeypatch.setattr(
        runtime_service,
        "legacy_systemd_service_unit_path",
        lambda: legacy_unit,
    )
    monkeypatch.setattr(
        runtime_service,
        "daemon_systemd_service_unit_path",
        lambda: daemon_unit,
    )
    monkeypatch.setattr(runtime_service, "_wait_for_daemon_status", waited.append)
    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_service, "run_checked_subprocess", fake_run_checked)

    runtime_service.install_systemd_service(
        program,
        ["--auto-uv-profile", "profile-a"],
        journal_hours=4,
        log=lambda _message: None,
    )

    assert (
        "checked",
        ["/bin/systemctl", "enable", "penguin-burnerd.service"],
    ) in actions
    assert (
        "checked",
        ["/bin/systemctl", "restart", "penguin-burnerd.service"],
    ) in actions
    assert (
        "checked",
        ["/bin/systemctl", "enable", "--now", "penguin-burnerd.service"],
    ) not in actions
    assert actions.index(
        ("checked", ["/bin/systemctl", "restart", "penguin-burnerd.service"])
    ) < actions.index(("clear-last-runtime", [])) < actions.index(
        ("persist-runtime", ["--auto-uv-profile", "profile-a"])
    )
    assert waited == [runtime_service.DEFAULT_DAEMON_SOCKET]
    assert ("persist-runtime", ["--auto-uv-profile", "profile-a"]) in actions


def test_migrate_to_daemon_service_disables_legacy_after_daemon_reachable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    legacy_unit = tmp_path / "PenguinBurner.service"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    state_file = tmp_path / "last-runtime.json"
    program.write_text("# program\n", encoding="utf-8")
    legacy_unit.write_text(
        "ExecStart=/usr/bin/python3 "
        f"{program} --auto-uv-profile profile-a --adaptive-auto-uv\n",
        encoding="utf-8",
    )
    actions = []
    logs = []
    binary_paths = []
    build_unit = runtime_service.build_daemon_api_service_unit

    def capture_binary_path(*args, **kwargs):
        binary_paths.append(kwargs.get("binary_path"))
        return build_unit(*args, **kwargs)

    def fake_run(args, **_kwargs):
        actions.append(("run", list(args)))
        if list(args) == ["/bin/systemctl", "is-enabled", "PenguinBurner.service"]:
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        if list(args) == ["/bin/systemctl", "is-active", "PenguinBurner.service"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run_checked(args):
        actions.append(("checked", list(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "install_daemon_binary",
        lambda *_a, **_k: runtime_service.DaemonBinaryRefresh(
            changed=False, source=Path("/pkg/penguin-burnerd")
        ),
    )
    monkeypatch.setattr(
        runtime_service,
        "build_daemon_api_service_unit",
        capture_binary_path,
    )
    monkeypatch.setattr(
        runtime_service, "LAST_RUNTIME_STATE_PATH", state_file
    )
    monkeypatch.setattr(
        runtime_service,
        "clear_last_runtime_state",
        lambda: actions.append(("clear-last-runtime", [])),
    )
    monkeypatch.setattr(
        runtime_service,
        "_apply_persistent_runtime",
        lambda argv, **_kwargs: actions.append(("persist-runtime", list(argv)))
        or {"pid": 4321},
    )
    monkeypatch.setattr(
        runtime_service,
        "legacy_systemd_service_unit_path",
        lambda: legacy_unit,
    )
    monkeypatch.setattr(
        runtime_service,
        "daemon_systemd_service_unit_path",
        lambda: daemon_unit,
    )
    monkeypatch.setattr(runtime_service, "daemon_status", lambda **_kwargs: {"state": "idle"})
    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_service, "run_checked_subprocess", fake_run_checked)

    runtime_service.migrate_to_daemon_service(
        program,
        socket_path="/tmp/penguin-burnerd.sock",
        log=logs.append,
    )

    assert daemon_unit.is_file()
    unit_text = daemon_unit.read_text(encoding="utf-8")
    assert (
        "ExecStart=/var/opt/penguin-burner/libexec/penguin-burnerd "
        "--socket /tmp/penguin-burnerd.sock"
    ) in unit_text
    assert "PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64" not in unit_text
    assert binary_paths == [runtime_service.DAEMON_BINARY]
    assert (
        "persist-runtime",
        ["--auto-uv-profile", "profile-a", "--adaptive-auto-uv"],
    ) in actions
    assert (
        "checked",
        ["/bin/systemctl", "enable", "penguin-burnerd.service"],
    ) in actions
    assert (
        "checked",
        ["/bin/systemctl", "restart", "penguin-burnerd.service"],
    ) in actions
    assert actions.index(
        ("checked", ["/bin/systemctl", "restart", "penguin-burnerd.service"])
    ) < actions.index(
        (
            "persist-runtime",
            ["--auto-uv-profile", "profile-a", "--adaptive-auto-uv"],
        )
    ) < actions.index(
        ("clear-last-runtime", [])
    ) < actions.index(
        ("run", ["/bin/systemctl", "disable", "--now", "PenguinBurner.service"])
    )
    assert any("Migrated enabled PenguinBurner.service" in message for message in logs)


def test_migrate_to_daemon_service_refuses_unparsed_enabled_legacy_unit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    legacy_unit = tmp_path / "PenguinBurner.service"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    program.write_text("# program\n", encoding="utf-8")
    legacy_unit.write_text("ExecStart=/usr/bin/other-tool\n", encoding="utf-8")

    def fake_run(args, **_kwargs):
        if list(args) == ["/bin/systemctl", "is-enabled", "PenguinBurner.service"]:
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        if list(args) == ["/bin/systemctl", "is-active", "PenguinBurner.service"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "legacy_systemd_service_unit_path",
        lambda: legacy_unit,
    )
    monkeypatch.setattr(
        runtime_service,
        "daemon_systemd_service_unit_path",
        lambda: daemon_unit,
    )
    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)

    try:
        runtime_service.migrate_to_daemon_service(
            program,
            socket_path="/tmp/penguin-burnerd.sock",
            log=lambda _message: None,
        )
    except RuntimeError as exc:
        assert "could not be parsed" in str(exc)
    else:
        raise AssertionError("expected migration failure")

    assert not daemon_unit.exists()


@pytest.mark.parametrize("runtime_mode", ["stock", "fixed", "adaptive"])
def test_stop_existing_runtime_does_not_disable_persistent_service(monkeypatch, runtime_mode) -> None:
    calls = []
    logs = []

    monkeypatch.setattr(runtime_service, "daemon_status", lambda **_: {
        "active_job": {"type": "runtime_profile", "runtime_mode": runtime_mode, "returncode": None}
    })
    monkeypatch.setattr(runtime_service, "stop_runtime_profile", lambda **kw: calls.append(kw))
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(runtime_service.subprocess, "run", lambda *_a, **_kw: pytest.fail("must use socket"))

    runtime_service.stop_existing_penguin_burner_runtime(log=logs.append)

    assert calls == [{"socket_path": runtime_service.DEFAULT_DAEMON_SOCKET, "timeout_s": 25.0}]
    assert any("before foreground Auto-UV scan" in message for message in logs)


@pytest.mark.parametrize("job_type, own_child", [
    ("auto_uv_scan", True), ("auto_uv_scan", False), ("profile_verification", False),
])
def test_scan_handoff_preserves_own_daemon_child_and_refuses_other_work(monkeypatch, job_type, own_child):
    monkeypatch.setattr(runtime_service, "daemon_status", lambda **_: {
        "active_job": {"type": job_type, "pid": os.getpid() if own_child else -1, "returncode": None}
    })
    monkeypatch.setattr(runtime_service, "stop_runtime_profile", lambda **_: pytest.fail("must not clear session"))
    if own_child:
        runtime_service.stop_existing_penguin_burner_runtime(log=lambda _: None)
    else:
        with pytest.raises(RuntimeError, match="another scan or profile verification"):
            runtime_service.stop_existing_penguin_burner_runtime(log=lambda _: None)


def test_scan_handoff_propagates_wedged_profile_failure(monkeypatch):
    monkeypatch.setattr(runtime_service, "daemon_status", lambda **_: {
        "active_job": {"type": "runtime_profile", "returncode": None}
    })
    def fail(**_):
        raise RuntimeError("runtime profile engine did not stop")
    monkeypatch.setattr(runtime_service, "stop_runtime_profile", fail)
    with pytest.raises(RuntimeError, match="engine did not stop"):
        runtime_service.stop_existing_penguin_burner_runtime(log=lambda _: None)


def test_daemonize_starts_daemon_service_and_runtime_profile(tmp_path, monkeypatch) -> None:
    calls = []
    logs = []
    starts = []
    daemon_unit = tmp_path / "penguin-burnerd.service"
    legacy_unit = tmp_path / "PenguinBurner.service"
    binary_paths = []
    build_unit = runtime_service.build_daemon_api_service_unit

    def capture_binary_path(*args, **kwargs):
        binary_paths.append(kwargs.get("binary_path"))
        return build_unit(*args, **kwargs)
    monkeypatch.setenv("SUDO_USER", "jp")
    monkeypatch.setenv("PENGUIN_BURNER_ADAPTIVE_TARGET_FPS", "120")
    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "install_daemon_binary",
        lambda *_a, **_k: runtime_service.DaemonBinaryRefresh(
            changed=False, source=Path("/pkg/penguin-burnerd")
        ),
    )
    monkeypatch.setattr(
        runtime_service,
        "build_daemon_api_service_unit",
        capture_binary_path,
    )
    monkeypatch.setattr(
        runtime_service,
        "daemon_systemd_service_unit_path",
        lambda: daemon_unit,
    )
    monkeypatch.setattr(
        runtime_service,
        "legacy_systemd_service_unit_path",
        lambda: legacy_unit,
    )
    monkeypatch.setattr(runtime_service, "_wait_for_daemon_status", lambda *_args: None)
    monkeypatch.setattr(
        runtime_service,
        "_apply_runtime",
        lambda argv, **_kwargs: (
            starts.append(list(argv)) or {"pid": 4321},
            {"format_version": 1},
        ),
    )

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run_checked(args):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_service, "run_checked_subprocess", fake_run_checked)

    runtime_service.daemonize_with_systemd(
        "/tmp/penguin_burner.py",
        ["--adaptive-auto-uv"],
        journal_hours=4,
        log=logs.append,
    )

    assert ["/bin/systemctl", "enable", "penguin-burnerd.service"] in calls
    assert ["/bin/systemctl", "restart", "penguin-burnerd.service"] in calls
    assert ["/bin/systemctl", "start", "penguin-burnerd.service"] not in calls
    assert not any("systemd-run" in " ".join(call) for call in calls)
    assert starts == [["--adaptive-auto-uv"]]
    assert binary_paths == [runtime_service.DAEMON_BINARY]
    unit = daemon_unit.read_text(encoding="utf-8")
    assert "PENGUIN_BURNER_ADAPTIVE_TARGET_FPS" not in unit
    assert any(message.startswith("Daemon binary:") for message in logs)
    assert "Started runtime profile through penguin-burnerd.service" in "\n".join(logs)


@pytest.mark.parametrize(
    ("binary_changed", "expected_verb"),
    [(True, "restart"), (False, "start")],
)
def test_daemonize_restarts_only_when_existing_binary_changed(
    binary_changed: bool,
    expected_verb: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    program.write_text("# program\n", encoding="utf-8")
    daemon_unit.write_text(
        runtime_service.build_daemon_api_service_unit(program),
        encoding="utf-8",
    )
    checked_calls = []
    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "install_daemon_binary",
        lambda *_args, **_kwargs: runtime_service.DaemonBinaryRefresh(
            changed=binary_changed, source=Path("/pkg/penguin-burnerd")
        ),
    )
    monkeypatch.setattr(
        runtime_service,
        "clear_existing_penguin_burner_unit_for_daemonize",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        runtime_service, "daemon_systemd_service_unit_path", lambda: daemon_unit
    )
    monkeypatch.setattr(runtime_service, "_wait_for_daemon_status", lambda *_args: None)
    monkeypatch.setattr(
        runtime_service,
        "_apply_runtime",
        lambda *_args, **_kwargs: ({"pid": 4321}, {"format_version": 1}),
    )
    monkeypatch.setattr(
        runtime_service.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )
    monkeypatch.setattr(
        runtime_service,
        "run_checked_subprocess",
        lambda args: checked_calls.append(list(args))
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    runtime_service.daemonize_with_systemd(
        program,
        ["--auto-uv-profile", "profile-a"],
        journal_hours=4,
        log=lambda _message: None,
    )

    expected = [
        "/bin/systemctl",
        expected_verb,
        "penguin-burnerd.service",
    ]
    assert expected in checked_calls
    assert ["/bin/systemctl", "enable", "penguin-burnerd.service"] not in checked_calls
    assert ["/bin/systemctl", "daemon-reload"] not in checked_calls
    other_verb = "start" if expected_verb == "restart" else "restart"
    assert [
        "/bin/systemctl",
        other_verb,
        "penguin-burnerd.service",
    ] not in checked_calls


def test_daemonize_preserves_pkexec_desktop_user_env(tmp_path, monkeypatch) -> None:
    calls = []
    logs = []
    daemon_unit = tmp_path / "penguin-burnerd.service"
    legacy_unit = tmp_path / "PenguinBurner.service"
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("PENGUIN_BURNER_HOME", "/home/jp")
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_USER", "jp")
    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "install_daemon_binary",
        lambda *_a, **_k: runtime_service.DaemonBinaryRefresh(
            changed=False, source=Path("/pkg/penguin-burnerd")
        ),
    )
    monkeypatch.setattr(
        runtime_service,
        "daemon_systemd_service_unit_path",
        lambda: daemon_unit,
    )
    monkeypatch.setattr(
        runtime_service,
        "legacy_systemd_service_unit_path",
        lambda: legacy_unit,
    )
    monkeypatch.setattr(runtime_service, "_wait_for_daemon_status", lambda *_args: None)
    monkeypatch.setattr(
        runtime_service,
        "_apply_runtime",
        lambda argv, **_kwargs: ({"pid": 4321}, {"format_version": 1}),
    )

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run_checked(args):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_service, "run_checked_subprocess", fake_run_checked)

    runtime_service.daemonize_with_systemd(
        "/tmp/penguin_burner.py",
        ["--auto-uv-profile", "850mv-2762mhz"],
        journal_hours=4,
        log=logs.append,
    )

    unit = daemon_unit.read_text(encoding="utf-8")
    assert "Environment=SUDO_USER=jp" in unit
    assert "Environment=PENGUIN_BURNER_HOME=/home/jp" in unit
    assert "Environment=PENGUIN_BURNER_Q2RTX_USER=jp" in unit


def test_flatpak_site_packages_stabilizes_deployment_id_to_active(monkeypatch) -> None:
    # The Python daemon unit is no longer built inside the Flatpak sandbox
    # (Option A -- the install path is gated in ui/commands.py), but the surviving
    # flatpak path helper still rewrites a concrete deployment-id path to the
    # stable "active" symlink so a re-sync does not strand PYTHONPATH.
    monkeypatch.setenv("FLATPAK_ID", "io.github.jpietek.PenguinBurner")
    monkeypatch.setenv("PENGUIN_BURNER_FLATPAK_APP_PATH", FLATPAK_DEPLOYMENT_APP_PATH)
    monkeypatch.delenv("PENGUIN_BURNER_FLATPAK_SITE_PACKAGES", raising=False)

    site_packages = str(
        runtime_service.flatpak_host_site_packages_path(
            "/app/lib/python3.13/site-packages/penguin_burner.py"
        )
    )

    assert FLATPAK_DEPLOYMENT_ID not in site_packages
    assert site_packages == FLATPAK_ACTIVE_SITE_PACKAGES


def test_flatpak_site_packages_falls_back_to_local_app_relative_path(
    monkeypatch,
) -> None:
    host_app_path = Path("/home/jp/.local/share/flatpak/app/appid/current/active/files")
    monkeypatch.setenv("FLATPAK_ID", "io.github.jpietek.PenguinBurner")
    monkeypatch.setenv("PENGUIN_BURNER_FLATPAK_APP_PATH", str(host_app_path))
    monkeypatch.delenv("PENGUIN_BURNER_FLATPAK_SITE_PACKAGES", raising=False)
    monkeypatch.setattr(
        runtime_service,
        "_flatpak_local_site_packages_relative_path",
        lambda: Path("lib/python3.13/site-packages"),
    )

    assert (
        runtime_service.flatpak_host_site_packages_path()
        == host_app_path / "lib/python3.13/site-packages"
    )


def test_read_legacy_last_runtime_argv_recovers_profile_not_stock(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    state_file = tmp_path / "last-runtime.json"
    monkeypatch.setattr(runtime_service, "LAST_RUNTIME_STATE_PATH", state_file)

    # A 0.6.x apply-on-startup profile is recovered verbatim.
    state_file.write_text(
        json.dumps(
            {
                "argv": ["--auto-uv-profile", "profile-x", "--silent-fan-curve"],
                "program_file": "/x/penguin_burner.py",
            }
        ),
        encoding="utf-8",
    )
    assert runtime_service.read_legacy_last_runtime_argv() == [
        "--auto-uv-profile",
        "profile-x",
        "--silent-fan-curve",
    ]

    # A stock/reset action is NOT worth persisting as a boot profile.
    from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR

    state_file.write_text(
        json.dumps({"argv": ["--auto-uv-profile", STOCK_PROFILE_SELECTOR]}),
        encoding="utf-8",
    )
    assert runtime_service.read_legacy_last_runtime_argv() == []

    # An argv with no profile selector, junk, or a missing file -> nothing.
    state_file.write_text(json.dumps({"argv": ["--gpu-index", "0"]}), encoding="utf-8")
    assert runtime_service.read_legacy_last_runtime_argv() == []
    state_file.unlink()
    assert runtime_service.read_legacy_last_runtime_argv() == []


def test_migrate_recovers_066_boot_profile_without_legacy_capitalized_unit(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    program = tmp_path / "penguin_burner.py"
    program.write_text("# program\n", encoding="utf-8")
    daemon_unit = tmp_path / "penguin-burnerd.service"
    missing_legacy = tmp_path / "PenguinBurner.service"  # pre-0.6, absent
    state_file = tmp_path / "last-runtime.json"
    state_file.write_text(
        json.dumps(
            {"argv": ["--auto-uv-profile", "profile-066", "--adaptive-auto-uv"]}
        ),
        encoding="utf-8",
    )
    actions = []

    def fake_run(args, **_kwargs):
        actions.append(("run", list(args)))
        return SimpleNamespace(returncode=1, stdout="", stderr="")  # unit absent

    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "install_daemon_binary",
        lambda *_a, **_k: runtime_service.DaemonBinaryRefresh(
            changed=False, source=Path("/pkg/penguin-burnerd")
        ),
    )
    monkeypatch.setattr(runtime_service, "LAST_RUNTIME_STATE_PATH", state_file)
    monkeypatch.setattr(
        runtime_service, "clear_last_runtime_state", lambda: None
    )
    monkeypatch.setattr(
        runtime_service,
        "_apply_persistent_runtime",
        lambda argv, **_kwargs: actions.append(("persist-runtime", list(argv)))
        or {"pid": 1},
    )
    monkeypatch.setattr(
        runtime_service, "legacy_systemd_service_unit_path", lambda: missing_legacy
    )
    monkeypatch.setattr(
        runtime_service, "daemon_systemd_service_unit_path", lambda: daemon_unit
    )
    monkeypatch.setattr(runtime_service, "daemon_status", lambda **_kwargs: {"state": "idle"})
    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)
    monkeypatch.setattr(
        runtime_service, "run_checked_subprocess",
        lambda args: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    logs = []
    runtime_service.migrate_to_daemon_service(
        program, socket_path="/tmp/s.sock", log=logs.append
    )

    # The 0.6.x boot profile survives the upgrade instead of falling to stock.
    assert (
        "persist-runtime",
        ["--auto-uv-profile", "profile-066", "--adaptive-auto-uv"],
    ) in actions
    assert any("Recovered apply-on-startup" in line for line in logs)


def test_migrate_preserves_existing_boot_spec_when_nothing_recovered(
    tmp_path: Path, monkeypatch
) -> None:
    program = tmp_path / "penguin_burner.py"
    program.write_text("# program\n", encoding="utf-8")
    daemon_unit = tmp_path / "penguin-burnerd.service"
    missing_legacy = tmp_path / "PenguinBurner.service"
    missing_state = tmp_path / "last-runtime.json"  # nothing to recover
    actions = []

    def fake_run(args, **_kwargs):
        actions.append(("run", list(args)))
        return SimpleNamespace(returncode=1, stdout="", stderr="")  # unit absent

    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "install_daemon_binary",
        lambda *_a, **_k: runtime_service.DaemonBinaryRefresh(
            changed=False, source=Path("/pkg/penguin-burnerd")
        ),
    )
    monkeypatch.setattr(runtime_service, "LAST_RUNTIME_STATE_PATH", missing_state)
    monkeypatch.setattr(runtime_service, "clear_last_runtime_state", lambda: None)
    monkeypatch.setattr(
        runtime_service,
        "_apply_persistent_runtime",
        lambda argv, **_kwargs: actions.append(("persist-runtime", list(argv)))
        or {"pid": 1},
    )
    monkeypatch.setattr(
        runtime_service, "legacy_systemd_service_unit_path", lambda: missing_legacy
    )
    monkeypatch.setattr(
        runtime_service, "daemon_systemd_service_unit_path", lambda: daemon_unit
    )
    monkeypatch.setattr(runtime_service, "daemon_status", lambda **_kwargs: {"state": "idle"})
    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)
    monkeypatch.setattr(
        runtime_service, "run_checked_subprocess",
        lambda args: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    logs = []
    runtime_service.migrate_to_daemon_service(
        program, socket_path="/tmp/s.sock", log=logs.append
    )

    # A repair/reinstall must not wipe an existing 0.7 boot profile.
    assert not any(action[0] == "persist-runtime" for action in actions)


def test_registered_daemon_program_file_parses_unit(tmp_path: Path) -> None:
    unit = tmp_path / "penguin-burnerd.service"
    unit.write_text(
        "[Service]\n"
        "Environment=SUDO_USER=jp\n"
        "Environment=PENGUIN_BURNER_DAEMON_PROGRAM_FILE=/opt/app/penguin_burner.py\n"
        "ExecStart=/var/opt/penguin-burner/libexec/penguin-burnerd\n",
        encoding="utf-8",
    )
    assert runtime_service.registered_daemon_program_file(unit) == Path(
        "/opt/app/penguin_burner.py"
    )
    # No unit installed (or unreadable): nothing to validate.
    assert (
        runtime_service.registered_daemon_program_file(tmp_path / "missing.service")
        is None
    )
    # A unit that registers no worker path also validates as None.
    bare = tmp_path / "bare.service"
    bare.write_text(
        "[Service]\n"
        "ExecStart=/var/opt/penguin-burner/libexec/penguin-burnerd\n"
    )
    assert runtime_service.registered_daemon_program_file(bare) is None


def test_daemon_worker_registration_error_detects_medium_switch(
    tmp_path: Path,
) -> None:
    """A healthy daemon registered to another install must fail readiness.

    The flatpak->pip switch shape (2026-07-14): the daemon answers every
    capability check, but its unit spawns scan workers from a deleted
    deployment and the first scan dies with 'daemon worker ... is not
    accessible'."""
    current = tmp_path / "site-packages" / "penguin_burner.py"
    current.parent.mkdir()
    current.write_text("# worker\n", encoding="utf-8")
    unit = tmp_path / "penguin-burnerd.service"

    def write_unit(worker: Path) -> None:
        unit.write_text(
            f"Environment=PENGUIN_BURNER_DAEMON_PROGRAM_FILE={worker}\n",
            encoding="utf-8",
        )

    # Registration matches this install: ready.
    write_unit(current.resolve())
    assert (
        runtime_service.daemon_worker_registration_error(current, unit_path=unit)
        is None
    )

    # Registered worker no longer exists (uninstalled flatpak deployment).
    gone = tmp_path / "flatpak" / "penguin_burner.py"
    write_unit(gone)
    error = runtime_service.daemon_worker_registration_error(current, unit_path=unit)
    assert error is not None and "no longer exists" in error

    # Registered worker exists but belongs to a different install
    # (e.g. an old python's site-packages after a distro upgrade).
    other = tmp_path / "old-site-packages" / "penguin_burner.py"
    other.parent.mkdir()
    other.write_text("# stale worker\n", encoding="utf-8")
    write_unit(other.resolve())
    error = runtime_service.daemon_worker_registration_error(current, unit_path=unit)
    assert error is not None and "this PenguinBurner runs from" in error

    # No unit at all: nothing to validate, never blocks.
    assert (
        runtime_service.daemon_worker_registration_error(
            current, unit_path=tmp_path / "absent.service"
        )
        is None
    )


def _capture_reexec_command(monkeypatch, returncode: int = 0) -> list:
    commands: list = []
    monkeypatch.setattr(
        runtime_service.subprocess,
        "run",
        lambda command, check: commands.append((list(command), check))
        or SimpleNamespace(returncode=returncode),
    )
    return commands


def test_reexec_daemon_lifecycle_builds_pkexec_command(monkeypatch) -> None:
    monkeypatch.setattr(runtime_service, "running_in_flatpak", lambda: False)
    monkeypatch.setattr(
        runtime_service,
        "env_command_prefix",
        lambda: ["/usr/bin/env"],
    )
    monkeypatch.setattr(
        runtime_service.shutil,
        "which",
        lambda name: "/usr/bin/pkexec" if name == "pkexec" else None,
    )
    entry_script = "/home/user/.local/bin/penguin-burner-cli"
    monkeypatch.setattr(runtime_service.sys, "argv", [entry_script])
    monkeypatch.setenv("SUDO_USER", "tester")
    import_root = str(Path(runtime_service.__file__).resolve().parents[2])
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(["/extra/site", import_root, "/extra/site"]),
    )
    commands = _capture_reexec_command(monkeypatch, returncode=42)

    exit_code = runtime_service.reexec_daemon_lifecycle_with_root(
        ["--install-systemd-service", "--adaptive-auto-uv"]
    )

    assert exit_code == 42
    assert commands == [
        (
            [
                "/usr/bin/pkexec",
                "/usr/bin/env",
                "SUDO_USER=tester",
                "PYTHONPATH=" + os.pathsep.join([import_root, "/extra/site"]),
                runtime_service.sys.executable,
                str(Path(entry_script).resolve()),
                "--install-systemd-service",
                "--adaptive-auto-uv",
            ],
            False,
        )
    ]


def test_reexec_daemon_lifecycle_dispatches_uutils_env(monkeypatch) -> None:
    monkeypatch.setattr(runtime_service, "running_in_flatpak", lambda: False)
    monkeypatch.setattr(
        runtime_service,
        "env_command_prefix",
        lambda: ["/usr/bin/uu-coreutils", "env"],
    )
    monkeypatch.setattr(
        runtime_service.shutil,
        "which",
        lambda name: "/usr/bin/pkexec" if name == "pkexec" else None,
    )
    monkeypatch.setattr(runtime_service.sys, "argv", ["/opt/pb/penguin-burner-cli"])
    commands = _capture_reexec_command(monkeypatch)

    runtime_service.reexec_daemon_lifecycle_with_root(
        ["--install-systemd-service"]
    )

    command, _check = commands[0]
    assert command[:3] == ["/usr/bin/pkexec", "/usr/bin/uu-coreutils", "env"]


def test_reexec_daemon_lifecycle_falls_back_to_sudo(monkeypatch) -> None:
    monkeypatch.setattr(runtime_service, "running_in_flatpak", lambda: False)
    monkeypatch.setattr(
        runtime_service,
        "env_command_prefix",
        lambda: ["/usr/bin/env"],
    )
    monkeypatch.setattr(
        runtime_service.shutil,
        "which",
        lambda name: "/usr/bin/sudo" if name == "sudo" else None,
    )
    monkeypatch.setattr(runtime_service.sys, "argv", ["/opt/pb/penguin-burner-cli"])
    monkeypatch.setenv("SUDO_USER", "tester")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    commands = _capture_reexec_command(monkeypatch)

    exit_code = runtime_service.reexec_daemon_lifecycle_with_root(
        ["--uninstall-systemd-service"]
    )

    assert exit_code == 0
    command, check = commands[0]
    assert command[0] == "/usr/bin/sudo"
    assert command[1] == "/usr/bin/env"
    assert command[-1] == "--uninstall-systemd-service"
    assert check is False


def test_reexec_daemon_lifecycle_requires_an_escalator(monkeypatch) -> None:
    monkeypatch.setattr(runtime_service, "running_in_flatpak", lambda: False)
    monkeypatch.setattr(runtime_service.shutil, "which", lambda _name: None)
    commands = _capture_reexec_command(monkeypatch)

    with pytest.raises(RuntimeError, match="neither pkexec nor sudo"):
        runtime_service.reexec_daemon_lifecycle_with_root(
            ["--install-systemd-service"]
        )
    assert commands == []


def test_reexec_daemon_lifecycle_refuses_flatpak_sandbox(monkeypatch) -> None:
    monkeypatch.setattr(runtime_service, "running_in_flatpak", lambda: True)
    commands = _capture_reexec_command(monkeypatch)

    with pytest.raises(RuntimeError, match="PenguinBurner GUI"):
        runtime_service.reexec_daemon_lifecycle_with_root(
            ["--install-systemd-service"]
        )
    assert commands == []
