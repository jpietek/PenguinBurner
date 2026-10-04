import configparser
import dataclasses
import json
import os
import pwd
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Self

from common.privileged_env import env_command_prefix
from common.subprocess_locale import stable_subprocess_env
from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR
from runtime.daemon_client import (
    DEFAULT_DAEMON_SOCKET,
    apply_runtime_spec,
    daemon_status,
    set_boot_runtime_spec,
    stop_runtime_profile,
)
from runtime.runtime_spec import (
    build_runtime_spec_from_intent,
    runtime_intent_from_argv,
)

SYSTEMCTL = shutil.which("systemctl") or "systemctl"
ROOT_UID = 0
ROOT_GID = 0
LEGACY_PENGUIN_BURNER_UNIT_NAME = "PenguinBurner"
PENGUIN_BURNER_DAEMON_UNIT_NAME = "penguin-burnerd"
PENGUIN_BURNER_UNIT_NAME = PENGUIN_BURNER_DAEMON_UNIT_NAME
PENGUIN_BURNER_FOREGROUND_ENV = "PENGUIN_BURNER_FOREGROUND"
ALLOWED_UID_ENV = "PENGUIN_BURNER_DAEMON_ALLOWED_UID"
AUTOSTART_PROGRAM_FILE_ENV = "PENGUIN_BURNER_DAEMON_PROGRAM_FILE"

# Legacy cleanup path from pre-RuntimeSpec releases.
LAST_RUNTIME_STATE_PATH = Path("/var/lib/penguin-burner/last-runtime.json")
BOOT_RUNTIME_STATE_PATH = Path("/var/lib/penguin-burner/boot-runtime.json")
DEFAULT_JOURNAL_HOURS = 4
FLATPAK_ID_ENV = "FLATPAK_ID"
FLATPAK_INFO_PATH = Path("/.flatpak-info")
DESKTOP_RUNTIME_ENV_NAMES = (
    "PENGUIN_BURNER_HOME",
    "PENGUIN_BURNER_Q2RTX_USER",
    "PENGUIN_BURNER_Q2RTX_UID",
    "PENGUIN_BURNER_Q2RTX_GID",
)
# The only executable path permitted in the root systemd unit. Wheel, checkout,
# Flatpak, and native-package copies are installation sources, never persistent
# root execution paths.
DAEMON_INSTALL_BASE = Path("/var/opt")
DAEMON_PRODUCT_DIRECTORY = DAEMON_INSTALL_BASE / "penguin-burner"
DAEMON_LIBEXEC_DIRECTORY = DAEMON_PRODUCT_DIRECTORY / "libexec"
DAEMON_BINARY = DAEMON_LIBEXEC_DIRECTORY / "penguin-burnerd"
NATIVE_PACKAGE_DAEMON_BINARY = Path("/usr/libexec/penguin-burnerd")
SELINUX_ENFORCE_PATH = Path("/sys/fs/selinux/enforce")


def parse_runtime_flags(argv, *, default_journal_hours=DEFAULT_JOURNAL_HOURS):
    daemonize = False
    install_systemd_service = False
    uninstall_systemd_service = False
    migrate_to_daemon = False
    daemon_status_requested = False
    restore_stock = False
    journal_hours = default_journal_hours
    passthrough = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--daemonize":
            daemonize = True
            index += 1
            continue
        if arg == "--install-systemd-service":
            install_systemd_service = True
            index += 1
            continue
        if arg in ("--uninstall-systemd-service", "--deinstall-systemd-service"):
            uninstall_systemd_service = True
            index += 1
            continue
        if arg == "--migrate-to-daemon-service":
            migrate_to_daemon = True
            index += 1
            continue
        if arg == "--daemon-status":
            daemon_status_requested = True
            index += 1
            continue
        if arg == "--restore-stock":
            restore_stock = True
            index += 1
            continue
        if arg == "--journal-hours":
            if index + 1 >= len(argv):
                raise RuntimeError("--journal-hours requires a value")
            index += 1
            arg = f"--journal-hours={argv[index]}"
        if arg.startswith("--journal-hours="):
            value = arg.split("=", 1)[1].strip()
            if not value:
                raise RuntimeError("--journal-hours requires a value")
            journal_hours = max(1, int(float(value)))
            index += 1
            continue
        passthrough.append(arg)
        index += 1
    if install_systemd_service and uninstall_systemd_service:
        raise RuntimeError(
            "choose either --install-systemd-service or --uninstall-systemd-service"
        )
    if restore_stock and (
        daemonize or install_systemd_service or uninstall_systemd_service or migrate_to_daemon
    ):
        raise RuntimeError(
            "--restore-stock is a standalone recovery command; do not combine "
            "it with --daemonize or service install/uninstall/migrate flags"
        )
    return {
        "daemonize": daemonize,
        "install_systemd_service": install_systemd_service,
        "uninstall_systemd_service": uninstall_systemd_service,
        "migrate_to_daemon": migrate_to_daemon,
        "daemon_status": daemon_status_requested,
        "restore_stock": restore_stock,
        "journal_hours": journal_hours,
        "passthrough": passthrough,
    }


def running_under_systemd_service():
    return os.environ.get(PENGUIN_BURNER_FOREGROUND_ENV) == "1"


def systemd_is_available():
    return (
        Path("/run/systemd/system").exists() and shutil.which("systemctl") is not None
    )


def journalctl_follow_command(hours):
    return f'journalctl -u {PENGUIN_BURNER_UNIT_NAME}.service --since "-{int(hours)} hours" -f'


def systemd_service_unit_path():
    return daemon_systemd_service_unit_path()


def legacy_systemd_service_unit_path():
    return Path("/etc/systemd/system") / f"{LEGACY_PENGUIN_BURNER_UNIT_NAME}.service"


def daemon_systemd_service_unit_path():
    return Path("/etc/systemd/system") / f"{PENGUIN_BURNER_DAEMON_UNIT_NAME}.service"


def _invoking_user_name():
    for env_name in ("SUDO_USER", "PENGUIN_BURNER_Q2RTX_USER"):
        user = os.environ.get(env_name, "").strip()
        if user:
            return user
    return pwd.getpwuid(os.getuid()).pw_name


def desktop_runtime_env_assignments() -> list[str]:
    assignments = [f"SUDO_USER={_invoking_user_name()}"]
    for env_name in DESKTOP_RUNTIME_ENV_NAMES:
        value = os.environ.get(env_name, "").strip()
        if value:
            assignments.append(f"{env_name}={value}")
    if running_in_flatpak():
        assignments.extend(_flatpak_host_user_env_assignments())
    return assignments


def running_in_flatpak() -> bool:
    return bool(os.environ.get(FLATPAK_ID_ENV, "").strip()) or Path(
        "/.flatpak-info"
    ).is_file()


def flatpak_host_app_path() -> Path:
    override = os.environ.get("PENGUIN_BURNER_FLATPAK_APP_PATH", "").strip()
    if override:
        return _stable_flatpak_deployment_path(Path(override).expanduser())
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(FLATPAK_INFO_PATH, encoding="utf-8")
    except OSError:
        return Path("/app")
    app_path = parser.get("Instance", "app-path", fallback="").strip()
    return _stable_flatpak_deployment_path(Path(app_path)) if app_path else Path("/app")


def _stable_flatpak_deployment_path(path: str | Path) -> Path:
    item = Path(path).expanduser()
    parts = item.parts
    for index, part in enumerate(parts):
        if part != "flatpak":
            continue
        if index + 6 > len(parts):
            continue
        if parts[index + 1] != "app":
            continue
        deploy_index = index + 5
        deployment = parts[deploy_index]
        if deployment == "active":
            return item
        if _looks_like_flatpak_deployment_id(deployment):
            return Path(*parts[:deploy_index], "active", *parts[deploy_index + 1 :])
    return item


def _looks_like_flatpak_deployment_id(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


def _flatpak_host_path_for_app_path(path: str | Path) -> Path:
    item = Path(str(path))
    if item.is_absolute() and len(item.parts) >= 2 and item.parts[1] == "app":
        return _stable_flatpak_deployment_path(
            flatpak_host_app_path().joinpath(*item.parts[2:])
        )
    return _stable_flatpak_deployment_path(item)


def flatpak_host_site_packages_path(program_file: str | Path | None = None) -> Path:
    override = os.environ.get("PENGUIN_BURNER_FLATPAK_SITE_PACKAGES", "").strip()
    if override:
        return _stable_flatpak_deployment_path(Path(override).expanduser())
    if program_file is not None:
        mapped_program = _flatpak_host_path_for_app_path(program_file)
        if mapped_program.parent.name == "site-packages":
            return mapped_program.parent
    app_path = flatpak_host_app_path()
    candidates = sorted((app_path / "lib").glob("python*/site-packages"), reverse=True)
    if candidates:
        return candidates[0]
    local_relative = _flatpak_local_site_packages_relative_path()
    if local_relative is not None:
        return app_path / local_relative
    if program_file is not None:
        mapped_program = _flatpak_host_path_for_app_path(program_file)
        if mapped_program.name == "penguin_burner.py":
            return mapped_program.parent
    raise RuntimeError(f"could not locate Flatpak Python site-packages under {app_path}")


def _flatpak_local_site_packages_relative_path() -> Path | None:
    candidates = sorted(Path("/app/lib").glob("python*/site-packages"), reverse=True)
    if not candidates:
        return None
    try:
        return candidates[0].relative_to("/app")
    except ValueError:
        return None


def flatpak_host_cli_program_file(program_file: str | Path | None = None) -> Path:
    return flatpak_host_site_packages_path(program_file) / "penguin_burner.py"


def _desktop_user_home() -> str:
    override = os.environ.get("PENGUIN_BURNER_HOME", "").strip()
    if override:
        return str(Path(override).expanduser())
    user = _invoking_user_name()
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


def _flatpak_host_user_env_assignments() -> list[str]:
    home = _desktop_user_home()
    if not home:
        return []
    return [
        f"HOME={home}",
        f"XDG_DATA_HOME={home}/.local/share",
    ]


def daemon_allowed_uid_assignment() -> str:
    uid = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_UID", "").strip()
        or os.environ.get("SUDO_UID", "").strip()
    )
    if not uid and os.getuid() != 0:
        uid = str(os.getuid())
    return f"{ALLOWED_UID_ENV}={uid}" if uid else ""


def _format_systemd_exec(args):
    rendered = []
    for arg in args:
        text = str(arg).replace("%", "%%")
        rendered.append(shlex.quote(text))
    return " ".join(rendered)


def runtime_python_env_assignments(program_file) -> list[str]:
    if not running_in_flatpak():
        return []
    return [
        "PYTHONNOUSERSITE=1",
        "PYTHONDONTWRITEBYTECODE=1",
        f"PYTHONPATH={flatpak_host_site_packages_path(program_file)}",
    ]


def run_checked_subprocess(args):
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    if result.returncode != 0:
        output = (result.stdout.strip() or result.stderr.strip()).strip()
        command_text = " ".join(shlex.quote(str(arg)) for arg in args)
        raise RuntimeError(f"{command_text} failed: {output or result.returncode}")
    return result


def _packaged_daemon_binary() -> Path:
    """penguin-burnerd bundled inside the installed wheel / site-packages.

    setup.py's build_py cargo-builds the daemon and stages it at
    ``runtime/daemon_bin/penguin-burnerd`` (package data). This module lives at
    ``runtime/support/runtime_service.py``, so the sibling ``daemon_bin`` dir is
    one level up from ``support``. This is the *source* copy the elevated install
    step reads from to populate the root-owned canonical target.
    """
    return Path(__file__).resolve().parent.parent / "daemon_bin" / "penguin-burnerd"


def _dev_daemon_binary(program_file) -> Path:
    """Cargo release build sitting next to a dev checkout's sources."""
    return (
        Path(program_file).resolve().parent
        / "burnerd"
        / "target"
        / "release"
        / "penguin-burnerd"
    )


def daemon_binary_path(program_file, *, binary_path=None) -> str:
    """Return the sole root-service executable path.

    Lifecycle callers pass the destination returned by the install step; it
    must still name the same fixed host path. A unit can never point into
    site-packages or a checkout.
    """
    del program_file
    if binary_path is not None and Path(binary_path) != DAEMON_BINARY:
        raise RuntimeError(
            "penguin-burnerd service binary must be installed at "
            f"{DAEMON_BINARY}, not {binary_path}"
        )
    return str(DAEMON_BINARY)


def daemon_install_source_path(program_file, *, source_path=None) -> Path:
    """Select bytes for the privileged copy into the canonical target.

    Prefer the current wheel/dev payload over an older installed daemon so a
    repair also performs an update. A safe existing libexec binary is the final
    fallback for distro packages that do not bundle a second copy.
    """
    candidates = (
        [Path(source_path)]
        if source_path is not None
        else [
            _packaged_daemon_binary(),
            _dev_daemon_binary(program_file),
            NATIVE_PACKAGE_DAEMON_BINARY,
            DAEMON_BINARY,
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise RuntimeError(
        "penguin-burnerd install source not found (looked in: "
        f"{searched}). Install a daemon-bearing package or build it with "
        "`cargo build --release` in burnerd/."
    )


def _daemon_metadata_is_safe(metadata, *, owner_uid=0) -> bool:
    mode = metadata.st_mode
    return (
        stat.S_ISREG(mode)
        and metadata.st_uid == owner_uid
        and mode & 0o100 != 0
        and mode & 0o022 == 0
    )


def _installed_daemon_is_safe(path: Path, *, owner_uid=0) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return _daemon_metadata_is_safe(metadata, owner_uid=owner_uid)


def _ensure_safe_daemon_install_tree(
    destination: Path,
    *,
    owner_uid=0,
) -> None:
    """Create and validate the base/product/libexec directory chain."""
    destination = Path(destination)
    product_directory = destination.parent.parent
    install_base = product_directory.parent
    for directory in (install_base, product_directory, destination.parent):
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            try:
                directory.mkdir(mode=0o755)
                metadata = directory.lstat()
            except OSError as exc:
                raise RuntimeError(
                    f"cannot create daemon install directory {directory}: {exc}"
                ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"cannot inspect daemon install directory {directory}: {exc}"
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != owner_uid
            or metadata.st_mode & 0o022
        ):
            raise RuntimeError(
                "daemon install directory is not a safe root-owned real "
                f"directory: {directory}"
            )


def _reject_noexec_daemon_target(target_directory: Path) -> None:
    """Fail early with a useful error when the target mount is ``noexec``."""
    findmnt = shutil.which("findmnt")
    if not findmnt:
        return
    result = subprocess.run(
        [findmnt, "-no", "OPTIONS", "--target", str(target_directory)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    if result.returncode != 0:
        return
    options = {option.strip() for option in result.stdout.strip().split(",")}
    if "noexec" in options:
        raise RuntimeError(
            "PenguinBurner cannot install its hardware service because "
            f"{target_directory} is mounted noexec"
        )


def _selinux_is_active() -> bool:
    if SELINUX_ENFORCE_PATH.exists():
        return True
    selinuxenabled = shutil.which("selinuxenabled")
    if not selinuxenabled:
        return False
    result = subprocess.run(
        [selinuxenabled],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    return result.returncode == 0


def _restore_daemon_selinux_context(product_directory: Path) -> None:
    if not _selinux_is_active():
        return
    restorecon = shutil.which("restorecon")
    if not restorecon:
        raise RuntimeError(
            "SELinux is active, but restorecon is unavailable; cannot safely "
            "label the PenguinBurner hardware service"
        )
    run_checked_subprocess([restorecon, "-RF", str(product_directory)])


def _read_daemon_install_source(source: Path) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise RuntimeError(
            f"cannot safely open daemon install source {source}: {exc}"
        ) from exc
    payload = b""
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"daemon install source is not a regular file: {source}")
        with os.fdopen(descriptor, "rb") as source_file:
            descriptor = -1
            payload = source_file.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not payload.startswith(b"\x7fELF"):
        raise RuntimeError(f"daemon install source is not an ELF binary: {source}")
    return payload


def _atomic_install_daemon_binary(
    source: Path,
    destination: Path,
    *,
    owner_uid=0,
    owner_gid=0,
) -> bool:
    """Install ``source`` atomically; return whether destination changed."""
    source = Path(source)
    destination = Path(destination)
    payload = _read_daemon_install_source(source)
    parent = destination.parent
    try:
        parent.mkdir(parents=True, mode=0o755, exist_ok=True)
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise RuntimeError(f"cannot create daemon install directory {parent}: {exc}") from exc
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != owner_uid
        or parent_metadata.st_mode & 0o022
    ):
        raise RuntimeError(f"daemon install directory is not safely owned: {parent}")

    temporary_fd = -1
    temporary_path = None
    try:
        try:
            destination_metadata = destination.lstat()
        except FileNotFoundError:
            destination_metadata = None
        if destination_metadata is not None and not _daemon_metadata_is_safe(
            destination_metadata,
            owner_uid=owner_uid,
        ):
            raise RuntimeError(
                f"installed daemon is not a safe ELF executable: {destination}"
            )

        if source == destination:
            if not _installed_daemon_is_safe(
                destination, owner_uid=owner_uid
            ):
                raise RuntimeError(
                    f"installed daemon is not a safe ELF executable: {destination}"
                )
            return False

        if _installed_daemon_is_safe(
            destination, owner_uid=owner_uid
        ) and destination.read_bytes() == payload:
            return False

        temporary_fd, temp_path = tempfile.mkstemp(
            prefix=".penguin-burnerd.", dir=parent
        )
        temporary_path = Path(temp_path)
        with os.fdopen(temporary_fd, "wb") as temporary_file:
            temporary_fd = -1
            temporary_file.write(payload)
            temporary_file.flush()
            os.fchown(temporary_file.fileno(), owner_uid, owner_gid)
            os.fchmod(temporary_file.fileno(), 0o755)
            os.fsync(temporary_file.fileno())

        os.replace(temporary_path, destination)
        temporary_path = None
        return True
    except OSError as exc:
        raise RuntimeError(
            f"could not atomically install {source} at {destination}: {exc}"
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
        if temporary_fd >= 0:
            try:
                os.close(temporary_fd)
            except OSError:
                pass


@dataclasses.dataclass(frozen=True)
class DaemonBinaryRefresh:
    """Outcome of one privileged daemon-binary install attempt."""

    changed: bool
    source: Path
    destination: Path = DAEMON_BINARY


def describe_daemon_binary_refresh(refresh: DaemonBinaryRefresh) -> str:
    """One mandatory user-facing line per install: what happened to the binary.

    A stale root daemon with success-looking output cost issue #30 three test
    cycles; every install action must now say whether the canonical target was
    refreshed, and from where.
    """
    if refresh.source == refresh.destination:
        return (
            f"Daemon binary: KEPT the existing {refresh.destination} — this "
            "install carries no daemon payload. Rebuild with cargo or "
            "reinstall a daemon-bearing package, then re-run the install."
        )
    if refresh.changed:
        return (
            f"Daemon binary: updated {refresh.destination} from {refresh.source}."
        )
    return (
        f"Daemon binary: already current at {refresh.destination} "
        f"(matches {refresh.source})."
    )


def install_daemon_binary(program_file, *, source_path=None) -> DaemonBinaryRefresh:
    if os.geteuid() != 0:
        raise RuntimeError("installing penguin-burnerd requires root privileges")
    source = daemon_install_source_path(program_file, source_path=source_path)
    _ensure_safe_daemon_install_tree(DAEMON_BINARY)
    _reject_noexec_daemon_target(DAEMON_BINARY.parent)
    changed = _atomic_install_daemon_binary(source, DAEMON_BINARY)
    _restore_daemon_selinux_context(DAEMON_BINARY.parent.parent)
    return DaemonBinaryRefresh(
        changed=changed,
        source=source,
        destination=DAEMON_BINARY,
    )


def _daemon_program_file_for_unit(program_file) -> Path:
    """The Python CLI the daemon re-launches for Auto-UV scan children.

    Flatpak maps it to the host deployment path; otherwise it is the resolved
    program file. This is what the unit's PENGUIN_BURNER_DAEMON_PROGRAM_FILE env
    and daemon-spawned scan children use.
    """
    if running_in_flatpak():
        return flatpak_host_cli_program_file(program_file)
    return Path(program_file).resolve()


def read_legacy_last_runtime_argv() -> list[str]:
    """Recover a 0.6.x apply-on-startup intent before migration wipes it.

    The 0.6.x Python daemon persisted the last runtime action as an argv list
    in ``/var/lib/penguin-burner/last-runtime.json`` (``{"argv": [...]}``); the
    0.7 Rust daemon reads a typed spec from a different file and never consults
    it, so a straight migration silently drops the user's boot profile. This
    reads that argv so the migration can rebuild it as the boot spec. Only an
    argv that actually applies a profile is returned — a stock/reset argv (or
    a bare autostart with no ``--auto-uv-profile``) is not worth persisting.
    """
    try:
        data = json.loads(
            LAST_RUNTIME_STATE_PATH.read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, ValueError):
        return []
    argv = data.get("argv") if isinstance(data, dict) else None
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        return []
    argv = [str(item) for item in argv]
    try:
        selector = argv[argv.index("--auto-uv-profile") + 1]
    except (ValueError, IndexError):
        return []
    if not selector or selector == STOCK_PROFILE_SELECTOR:
        return []
    return argv


def clear_last_runtime_state() -> None:
    """Remove only the obsolete pre-RuntimeSpec state file."""
    try:
        LAST_RUNTIME_STATE_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def clear_all_runtime_state() -> None:
    """Remove legacy and current boot state during explicit uninstall."""
    for path in (LAST_RUNTIME_STATE_PATH, BOOT_RUNTIME_STATE_PATH):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _stop_active_runtime_before_daemon_restart() -> None:
    try:
        stop_runtime_profile(socket_path=DEFAULT_DAEMON_SOCKET, timeout_s=3)
    except Exception:  # noqa: BLE001, S110
        pass


def _apply_runtime(argv, *, socket_path=DEFAULT_DAEMON_SOCKET) -> tuple[dict, dict]:
    spec = build_runtime_spec_from_intent(
        runtime_intent_from_argv(argv),
        socket_path=socket_path,
    )
    result = apply_runtime_spec(spec, socket_path=socket_path, timeout_s=45)
    return result, spec


def _apply_persistent_runtime(argv, *, socket_path=DEFAULT_DAEMON_SOCKET) -> dict:
    result, spec = _apply_runtime(argv, socket_path=socket_path)
    set_boot_runtime_spec(spec, socket_path=socket_path, timeout_s=10)
    return result


def registered_daemon_program_file(unit_path=None) -> Path | None:
    """The scan-worker path the installed daemon unit registers, or None.

    None means there is nothing to validate: no unit installed, or the unit
    is unreadable, or it predates the program-file registration."""
    path = Path(unit_path or daemon_systemd_service_unit_path())
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    prefix = f"Environment={AUTOSTART_PROGRAM_FILE_ENV}="
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
            return Path(value) if value else None
    return None


def _current_install_program_file() -> Path:
    # runtime/support/runtime_service.py sits two levels below the install
    # root (a site-packages dir or the source checkout), where the
    # penguin_burner.py worker entry lives — the same layout cli_base_command
    # resolves against.
    return Path(__file__).resolve().parents[2] / "penguin_burner.py"


def daemon_worker_registration_error(program_file=None, *, unit_path=None) -> str | None:
    """Why the installed daemon cannot spawn scan workers for THIS install.

    The unit bakes in the worker path of whichever install medium set the
    daemon up. After a flatpak<->pip switch — or a distro Python upgrade
    moving site-packages — the daemon stays healthy and answers every status
    check, but each scan launch dies with "daemon worker ... is not
    accessible" (2026-07-14). Surfacing the mismatch from the readiness check
    routes the user into the existing install-or-repair prompt, whose
    migration command regenerates the unit for the current install. Returns
    None when the unit registers no worker (nothing to validate) or the
    registration matches this install.
    """
    registered = registered_daemon_program_file(unit_path)
    if registered is None:
        return None
    expected = _daemon_program_file_for_unit(
        program_file if program_file is not None else _current_install_program_file()
    )
    if not registered.exists():
        return (
            "the hardware service is registered to a scan worker that no "
            f"longer exists: {registered}"
        )
    if registered != Path(expected):
        return (
            f"the hardware service spawns scan workers from {registered}, "
            f"but this PenguinBurner runs from {expected}"
        )
    return None


def build_daemon_api_service_unit(
    program_file,
    *,
    socket_path=DEFAULT_DAEMON_SOCKET,
    binary_path=None,
) -> str:
    allowed_uid = daemon_allowed_uid_assignment()
    allowed_uid_env = f"Environment={allowed_uid}\n" if allowed_uid else ""
    runtime_env = "".join(
        f"Environment={assignment}\n"
        for assignment in [
            *desktop_runtime_env_assignments(),
            *runtime_python_env_assignments(program_file),
        ]
    )

    # The compiled Rust penguin-burnerd binary. Boot intent is stored separately
    # through the typed daemon API. Type=notify + WatchdogSec: the daemon sends
    # READY=1 and heartbeats WATCHDOG=1. Lifecycle callers pass the installed
    # destination explicitly, but every unit is constrained to that same
    # root-owned target.
    binary = daemon_binary_path(program_file, binary_path=binary_path)
    exec_start = _format_systemd_exec([binary, "--socket", str(socket_path)])
    program_file_env = (
        f"Environment={AUTOSTART_PROGRAM_FILE_ENV}="
        f"{_daemon_program_file_for_unit(program_file)}\n"
    )
    return (
        "[Unit]\n"
        "Description=PenguinBurner hardware daemon\n"
        "After=multi-user.target\n"
        "\n"
        "[Service]\n"
        "Type=notify\n"
        "WorkingDirectory=/\n"
        "WatchdogSec=30\n"
        f"{runtime_env}"
        f"{allowed_uid_env}"
        f"{program_file_env}"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        f"SyslogIdentifier={PENGUIN_BURNER_DAEMON_UNIT_NAME}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _atomic_write_service_unit(path: Path, text: str) -> None:
    path = Path(path)
    temporary_fd = -1
    temporary_path = None
    try:
        temporary_fd, temp_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        temporary_path = Path(temp_path)
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as temporary_file:
            temporary_fd = -1
            temporary_file.write(text)
            temporary_file.flush()
            os.fchown(temporary_file.fileno(), ROOT_UID, ROOT_GID)
            os.fchmod(temporary_file.fileno(), 0o644)
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise RuntimeError(f"could not atomically install systemd unit {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
        if temporary_fd >= 0:
            try:
                os.close(temporary_fd)
            except OSError:
                pass


@dataclasses.dataclass
class _ManagedFileRollback:
    path: Path
    existed: bool
    backup_path: Path | None

    @classmethod
    def capture(cls, path: Path) -> "_ManagedFileRollback":
        path = Path(path)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return cls(path=path, existed=False, backup_path=None)
        except OSError as exc:
            raise RuntimeError(f"cannot inspect rollback source {path}: {exc}") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != ROOT_UID
            or metadata.st_mode & 0o022
        ):
            raise RuntimeError(
                f"cannot replace unsafe managed file during daemon setup: {path}"
            )
        backup_fd = -1
        backup_path = None
        try:
            backup_fd, backup_name = tempfile.mkstemp(
                prefix=f".{path.name}.rollback.",
                dir=path.parent,
            )
            backup_path = Path(backup_name)
            os.close(backup_fd)
            backup_fd = -1
            backup_path.unlink()
            os.link(path, backup_path)
            return cls(path=path, existed=True, backup_path=backup_path)
        except OSError as exc:
            if backup_path is not None:
                try:
                    backup_path.unlink()
                except OSError:
                    pass
            raise RuntimeError(f"cannot preserve {path} for rollback: {exc}") from exc
        finally:
            if backup_fd >= 0:
                os.close(backup_fd)

    def restore(self) -> None:
        if self.existed:
            if self.backup_path is None:
                raise RuntimeError(f"rollback copy for {self.path} is unavailable")
            os.replace(self.backup_path, self.path)
            self.backup_path = None
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def discard(self) -> None:
        if self.backup_path is None:
            return
        try:
            self.backup_path.unlink()
        except FileNotFoundError:
            pass
        self.backup_path = None


@dataclasses.dataclass(frozen=True)
class _SystemdUnitState:
    enabled: bool
    active: bool


class _DaemonServiceInstallTransaction:
    def __init__(self, *, log, socket_path=DEFAULT_DAEMON_SOCKET):
        self._log = log
        self._socket_path = socket_path
        self._committed = False
        self._states: dict[str, _SystemdUnitState] | None = None
        self._snapshots: list[_ManagedFileRollback] = []
        try:
            self._snapshots = [
                _ManagedFileRollback.capture(DAEMON_BINARY),
                _ManagedFileRollback.capture(daemon_systemd_service_unit_path()),
                _ManagedFileRollback.capture(legacy_systemd_service_unit_path()),
            ]
        except Exception:
            for snapshot in self._snapshots:
                snapshot.discard()
            raise

    def __enter__(self) -> Self:
        return self

    def capture_service_state(self) -> None:
        self._states = {
            f"{PENGUIN_BURNER_DAEMON_UNIT_NAME}.service": _SystemdUnitState(
                enabled=_systemd_unit_is_enabled(
                    f"{PENGUIN_BURNER_DAEMON_UNIT_NAME}.service"
                ),
                active=_systemd_unit_is_active(
                    f"{PENGUIN_BURNER_DAEMON_UNIT_NAME}.service"
                ),
            ),
            f"{LEGACY_PENGUIN_BURNER_UNIT_NAME}.service": _SystemdUnitState(
                enabled=_systemd_unit_is_enabled(
                    f"{LEGACY_PENGUIN_BURNER_UNIT_NAME}.service"
                ),
                active=_systemd_unit_is_active(
                    f"{LEGACY_PENGUIN_BURNER_UNIT_NAME}.service"
                ),
            ),
        }

    def commit(self) -> None:
        self._committed = True
        for snapshot in self._snapshots:
            snapshot.discard()

    def _rollback(self) -> None:
        states = self._states or {}
        rollback_errors: list[str] = []

        def attempt(description: str, operation) -> None:
            try:
                operation()
            except Exception as exc:  # noqa: BLE001
                rollback_errors.append(f"{description}: {exc}")

        if states:
            for unit_name in states:
                attempt(
                    f"could not stop and disable {unit_name}",
                    lambda unit_name=unit_name: run_checked_subprocess(
                        [SYSTEMCTL, "disable", "--now", unit_name]
                    ),
                )
        for snapshot in self._snapshots:
            attempt(
                f"could not restore {snapshot.path}",
                snapshot.restore,
            )
        if not states:
            if rollback_errors:
                raise RuntimeError("; ".join(rollback_errors))
            return
        attempt(
            "could not reload restored systemd units",
            lambda: run_checked_subprocess([SYSTEMCTL, "daemon-reload"]),
        )
        for unit_name, state in states.items():
            if state.enabled:
                attempt(
                    f"could not re-enable {unit_name}",
                    lambda unit_name=unit_name: run_checked_subprocess(
                        [SYSTEMCTL, "enable", unit_name]
                    ),
                )
            if state.active:
                attempt(
                    f"could not restart {unit_name}",
                    lambda unit_name=unit_name: run_checked_subprocess(
                        [SYSTEMCTL, "start", unit_name]
                    ),
                )
        daemon_state = states[f"{PENGUIN_BURNER_DAEMON_UNIT_NAME}.service"]
        if daemon_state.active:
            attempt(
                "restored hardware service did not become ready",
                lambda: _wait_for_daemon_status(self._socket_path),
            )
        if rollback_errors:
            raise RuntimeError("; ".join(rollback_errors))
        self._log("Restored the previous PenguinBurner hardware service after setup failed.")

    def __exit__(self, exc_type, exc, traceback) -> bool:
        del exc_type, traceback
        if exc is None or self._committed:
            for snapshot in self._snapshots:
                snapshot.discard()
            return False
        try:
            self._rollback()
        except Exception as rollback_error:  # noqa: BLE001
            raise RuntimeError(
                f"{exc}\nRollback of the previous hardware service also failed: "
                f"{rollback_error}"
            ) from exc
        return False


def reexec_daemon_lifecycle_with_root(argv: list[str]) -> int:
    """Re-run this CLI invocation as root for one daemon-lifecycle step.

    pip --user installs live where only the invoking user's Python looks, so a
    bare ``sudo penguin-burner-cli`` dies on import before any product code
    runs (sudo resets HOME and root's interpreter never searches the user
    site). Mirror the GUI's single elevated lifecycle step instead: escalate
    through pkexec (or sudo) with the invoking user's import root injected so
    the root process resolves the same installed code.
    """
    if running_in_flatpak():
        # In the sandbox pkexec is unusable, sys.executable is /app/bin/python,
        # and the injected PYTHONPATH would name sandbox paths that mean
        # nothing on the host. The flatpak daemon lifecycle runs host-side
        # through the GUI's flatpak-spawn flow instead.
        raise RuntimeError(
            "this daemon service command cannot self-elevate inside the "
            "flatpak sandbox; run the daemon setup from the PenguinBurner "
            "GUI, which performs the host-side elevated install"
        )
    escalator = shutil.which("pkexec") or shutil.which("sudo")
    if escalator is None:
        raise RuntimeError(
            "this daemon service command requires root privileges and "
            "neither pkexec nor sudo is available; re-run as root"
        )
    import_root = str(Path(__file__).resolve().parents[2])
    pythonpath_entries = [import_root]
    for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        entry = entry.strip()
        if entry and entry not in pythonpath_entries:
            pythonpath_entries.append(entry)
    command = [
        escalator,
        *env_command_prefix(),
        f"SUDO_USER={_invoking_user_name()}",
        "PYTHONPATH=" + os.pathsep.join(pythonpath_entries),
        sys.executable,
        str(Path(sys.argv[0]).resolve()),
        *argv,
    ]
    return subprocess.run(command, check=False).returncode


def install_systemd_service(program_file, argv, *, journal_hours, log):
    if not systemd_is_available():
        raise RuntimeError("systemd service install is unavailable on this system.")
    if os.geteuid() != 0:
        raise RuntimeError(
            "systemd service install requires root privileges. Re-run with sudo."
        )

    unit_path = daemon_systemd_service_unit_path()
    with _DaemonServiceInstallTransaction(log=log) as transaction:
        refresh = install_daemon_binary(program_file)
        log(describe_daemon_binary_refresh(refresh))
        transaction.capture_service_state()
        _stop_active_runtime_before_daemon_restart()
        clear_existing_penguin_burner_unit_for_install(log=log)
        _atomic_write_service_unit(
            unit_path,
            build_daemon_api_service_unit(
                program_file,
                binary_path=refresh.destination,
            ),
        )
        run_checked_subprocess([SYSTEMCTL, "daemon-reload"])
        subprocess.run(
            [SYSTEMCTL, "reset-failed", unit_path.name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=stable_subprocess_env(),
            check=False,
        )
        _enable_and_start_or_restart_daemon_unit(unit_path.name)
        _wait_for_daemon_status(DEFAULT_DAEMON_SOCKET)
        transaction.commit()
    clear_last_runtime_state()
    if argv:
        _apply_persistent_runtime(argv)
    # With no profile argv this is an install/repair, not a boot-profile
    # change: an existing boot spec is preserved as-is (the daemon re-read it
    # on the restart above), matching migrate_to_daemon_service. Wiping the
    # user's boot profile on a reinstall was never the intent.
    log(f"Installed and enabled {unit_path.name} at {unit_path}.")
    log(f"Follow the journal with: {journalctl_follow_command(journal_hours)}")


def migrate_to_daemon_service(program_file, *, socket_path=DEFAULT_DAEMON_SOCKET, log):
    if not systemd_is_available():
        raise RuntimeError(
            "PenguinBurner daemon service install is unavailable on this system."
        )
    if os.geteuid() != 0:
        raise RuntimeError(
            "PenguinBurner daemon service migration requires root privileges. "
            "Re-run with sudo."
        )

    legacy_state = read_legacy_service_state()
    raw_argv = (
        legacy_state["runtime_argv"]
        if legacy_state["exists"] and legacy_state["enabled"]
        else []
    )
    autostart_argv = [str(arg) for arg in raw_argv] if isinstance(raw_argv, list) else []
    if legacy_state["exists"] and legacy_state["enabled"] and not autostart_argv:
        raise RuntimeError(
            "existing enabled PenguinBurner.service could not be parsed; "
            "leaving the legacy service unchanged"
        )
    # No pre-0.6 capitalized unit to inherit from? A 0.6.x install used this
    # same unit name and persisted its apply-on-startup intent as argv in
    # last-runtime.json — recover it (before clear_last_runtime_state wipes
    # the file) so an upgrading user keeps their boot profile.
    if not autostart_argv:
        recovered = read_legacy_last_runtime_argv()
        if recovered:
            autostart_argv = recovered
            log(
                "Recovered apply-on-startup profile from the previous "
                f"PenguinBurner version: {shlex.join(recovered)}"
            )
    unit_path = daemon_systemd_service_unit_path()
    with _DaemonServiceInstallTransaction(
        log=log,
        socket_path=socket_path,
    ) as transaction:
        refresh = install_daemon_binary(program_file)
        log(describe_daemon_binary_refresh(refresh))
        transaction.capture_service_state()
        _stop_active_runtime_before_daemon_restart()
        _atomic_write_service_unit(
            unit_path,
            build_daemon_api_service_unit(
                program_file,
                socket_path=socket_path,
                binary_path=refresh.destination,
            ),
        )
        run_checked_subprocess([SYSTEMCTL, "daemon-reload"])
        subprocess.run(
            [SYSTEMCTL, "reset-failed", unit_path.name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=stable_subprocess_env(),
            check=False,
        )
        _enable_and_start_or_restart_daemon_unit(unit_path.name)
        _wait_for_daemon_status(socket_path)
        if autostart_argv:
            _apply_persistent_runtime(autostart_argv, socket_path=socket_path)
        transaction.commit()
    clear_last_runtime_state()
    # With nothing recovered, an existing 0.7 boot spec is preserved as-is
    # (a repair/reinstall must not wipe the user's boot profile); the daemon
    # re-read it on the restart above.
    log(f"Installed and started {unit_path.name} at {unit_path}.")

    if legacy_state["exists"]:
        # The new daemon answered status above; the legacy unit is stopped,
        # disabled, AND its file removed so nothing can ever start it again.
        _clear_existing_penguin_burner_unit(log=log, reason="daemon migration")
        if legacy_state["enabled"] and autostart_argv:
            log(
                "Migrated enabled PenguinBurner.service autostart intent to "
                f"{unit_path.name}: {shlex.join(autostart_argv)}"
            )
        else:
            log("Migrated existing PenguinBurner.service to penguin-burnerd.service.")
    else:
        log("No existing PenguinBurner.service found; daemon service is ready.")


def read_legacy_service_state() -> dict[str, object]:
    unit_path = legacy_systemd_service_unit_path()
    text = ""
    exists = unit_path.is_file()
    if exists:
        text = unit_path.read_text(encoding="utf-8", errors="replace")
    return {
        "exists": exists,
        "enabled": _systemd_unit_is_enabled(unit_path.name) if exists else False,
        "active": _systemd_unit_is_active(unit_path.name) if exists else False,
        "runtime_argv": parse_runtime_argv_from_unit_text(text),
    }


def parse_runtime_argv_from_unit_text(text: str) -> list[str]:
    exec_start = ""
    for line in str(text).splitlines():
        line = line.strip()
        if line.startswith("ExecStart="):
            exec_start = line.split("=", 1)[1].strip()
            break
    if not exec_start:
        return []
    try:
        parts = shlex.split(exec_start.replace("%%", "%"))
    except ValueError:
        return []
    for index, part in enumerate(parts):
        if Path(part).name == "penguin_burner.py":
            return parts[index + 1 :]
        if Path(part).name == "penguin_burner.sh":
            return parts[index + 1 :]
    return []


def _systemd_unit_is_enabled(unit_name: str) -> bool:
    result = subprocess.run(
        [SYSTEMCTL, "is-enabled", unit_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "enabled"


def _systemd_unit_is_active(unit_name: str) -> bool:
    result = subprocess.run(
        [SYSTEMCTL, "is-active", unit_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "active"


def _enable_and_start_or_restart_daemon_unit(unit_name: str) -> None:
    run_checked_subprocess([SYSTEMCTL, "enable", unit_name])
    run_checked_subprocess([SYSTEMCTL, "restart", unit_name])


def _wait_for_daemon_status(socket_path) -> None:
    last_error = None
    for _attempt in range(30):
        try:
            daemon_status(socket_path=socket_path, timeout_s=1)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"PenguinBurner daemon did not become reachable: {last_error}")


def uninstall_systemd_service(*, log):
    if not systemd_is_available():
        raise RuntimeError("systemd service uninstall is unavailable on this system.")
    if os.geteuid() != 0:
        raise RuntimeError(
            "systemd service uninstall requires root privileges. Re-run with sudo."
        )

    _stop_active_runtime_before_daemon_restart()
    clear_all_runtime_state()  # nothing to re-run once the service is gone
    unit_paths = (
        daemon_systemd_service_unit_path(),
        legacy_systemd_service_unit_path(),
    )
    for unit_path in unit_paths:
        subprocess.run(
            [SYSTEMCTL, "disable", "--now", unit_path.name],
            env=stable_subprocess_env(),
            check=False,
        )
        if unit_path.exists():
            unit_path.unlink()
    run_checked_subprocess([SYSTEMCTL, "daemon-reload"])
    for unit_path in unit_paths:
        subprocess.run(
            [SYSTEMCTL, "reset-failed", unit_path.name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=stable_subprocess_env(),
            check=False,
        )
    log("Removed penguin-burnerd.service and legacy PenguinBurner.service.")


def _clear_existing_penguin_burner_unit(*, log, reason):
    unit_name = f"{LEGACY_PENGUIN_BURNER_UNIT_NAME}.service"
    unit_path = legacy_systemd_service_unit_path()

    subprocess.run(
        [SYSTEMCTL, "disable", "--now", unit_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    if unit_path.exists():
        unit_path.unlink()
        log(f"Removed existing static {unit_name} before {reason}.")
    run_checked_subprocess([SYSTEMCTL, "daemon-reload"])
    subprocess.run(
        [SYSTEMCTL, "reset-failed", unit_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )


def clear_existing_penguin_burner_unit_for_daemonize(*, log):
    _clear_existing_penguin_burner_unit(
        log=log,
        reason="transient daemon start",
    )


def clear_existing_penguin_burner_unit_for_install(*, log):
    _clear_existing_penguin_burner_unit(
        log=log,
        reason="persistent service install",
    )


def stop_existing_penguin_burner_runtime(*, log):
    """Release profile control before CLI scan writes, keeping the boot intent."""
    status = daemon_status(socket_path=DEFAULT_DAEMON_SOCKET)
    job = status.get("active_job") or {}
    if job.get("type") in {"auto_uv_scan", "profile_verification"} and job.get("returncode") is None:
        if job.get("type") == "auto_uv_scan" and job.get("pid") == os.getpid():
            # The GUI's daemon-owned child already has exclusive control.
            # Stopping here would erase the session profile restored on exit.
            return
        raise RuntimeError("Cannot start Auto-UV: another scan or profile verification is running")
    if job.get("type") == "runtime_profile":
        # Works for stock too, including a stock profile restored after boot.
        # A failed/wedged stop is fatal before fans or V/F checks write anything.
        stop_runtime_profile(socket_path=DEFAULT_DAEMON_SOCKET, timeout_s=25.0)
        log("Stopped runtime profile before foreground Auto-UV scan; boot settings were kept.")


def daemonize_with_systemd(program_file, argv, *, journal_hours, log):
    if not systemd_is_available():
        raise RuntimeError(
            "systemd background mode is unavailable on this system. "
            "Run PenguinBurner directly or use a systemd-based system."
        )
    if os.geteuid() != 0:
        raise RuntimeError(
            "automatic systemd daemon mode requires root privileges. "
            "Re-run PenguinBurner with sudo."
        )

    unit_changed = False
    with _DaemonServiceInstallTransaction(log=log) as transaction:
        refresh = install_daemon_binary(program_file)
        log(describe_daemon_binary_refresh(refresh))
        transaction.capture_service_state()
        clear_existing_penguin_burner_unit_for_daemonize(log=log)
        unit_changed = _ensure_daemon_service_started(
            program_file,
            socket_path=DEFAULT_DAEMON_SOCKET,
            binary_changed=refresh.changed,
            binary_path=refresh.destination,
            log=log,
        )
        transaction.commit()
    if unit_changed:
        clear_last_runtime_state()
    result, _spec = _apply_runtime(argv, socket_path=DEFAULT_DAEMON_SOCKET)
    log(
        "Started runtime profile through "
        f"{PENGUIN_BURNER_DAEMON_UNIT_NAME}.service"
        + (
            f" (pid {result.get('pid')})."
            if str(result.get("pid") or "").strip()
            else "."
        )
    )
    log(f"Follow the journal with: {journalctl_follow_command(journal_hours)}")


def _ensure_daemon_service_started(
    program_file, *, socket_path, binary_changed, binary_path, log
) -> bool:
    unit_path = daemon_systemd_service_unit_path()
    unit_text = build_daemon_api_service_unit(
        program_file,
        socket_path=socket_path,
        binary_path=binary_path,
    )
    wrote_unit = False
    current_text = (
        unit_path.read_text(encoding="utf-8", errors="replace")
        if unit_path.exists()
        else ""
    )
    if current_text != unit_text:
        _atomic_write_service_unit(unit_path, unit_text)
        wrote_unit = True
        verb = "Updated" if current_text else "Installed"
        log(f"{verb} {unit_path.name} at {unit_path}.")
    if wrote_unit:
        run_checked_subprocess([SYSTEMCTL, "daemon-reload"])
    subprocess.run(
        [SYSTEMCTL, "reset-failed", unit_path.name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    if wrote_unit:
        run_checked_subprocess([SYSTEMCTL, "enable", unit_path.name])
    if wrote_unit or binary_changed:
        run_checked_subprocess([SYSTEMCTL, "restart", unit_path.name])
    else:
        run_checked_subprocess([SYSTEMCTL, "start", unit_path.name])
    _wait_for_daemon_status(socket_path)
    return wrote_unit
