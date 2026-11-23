from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable

from runtime.daemon_client import DaemonCompatibilityError, require_daemon_capabilities
from runtime.daemon_commands import daemon_migration_command
from runtime.support.runtime_service import daemon_worker_registration_error
from ui.assets import application_version


def _daemon_status_and_worker_check(required_capabilities: tuple[str, ...]) -> dict:
    status = require_daemon_capabilities(
        *required_capabilities,
        expected_version=application_version(),
    )
    # A healthy, protocol-compatible daemon can still be registered to spawn
    # scan workers from a different install (flatpak<->pip switch, or a
    # Python upgrade that moved site-packages). Capabilities cannot see
    # that; validate the unit's worker registration so the mismatch lands
    # in the repair prompt instead of failing the first scan launch.
    registration_error = daemon_worker_registration_error()
    if registration_error:
        raise RuntimeError(registration_error)
    return status


def ensure_daemon_ready_for_privileged_action(
    *,
    QtWidgets,
    parent,
    log,
    action_label: str,
    required_capabilities: tuple[str, ...] = (),
    status_check: Callable[[], dict] | None = None,
    command_factory: Callable[[], list[str]] = daemon_migration_command,
    run_command: Callable = subprocess.run,
) -> bool:
    # Reachability alone is not readiness: a daemon left running from a
    # previous install still answers status but speaks an older protocol, and
    # the action would then fail mid-flight with a raw daemon error. Check the
    # protocol handshake (plus the action's capabilities) up front so a stale
    # daemon lands in the repair prompt instead.
    check = status_check or (
        lambda: _daemon_status_and_worker_check(required_capabilities)
    )

    try:
        check()
        return True
    except DaemonCompatibilityError as exc:
        unavailable_reason = str(exc)
        prompt = (
            f"{action_label} needs a matching PenguinBurner root hardware "
            "service.\n\n"
            f"({unavailable_reason})\n\n"
            "PenguinBurner can update and restart it now. This may ask for "
            "your administrator password once."
        )
    except Exception as exc:  # noqa: BLE001
        unavailable_reason = str(exc)
        prompt = (
            f"{action_label} requires the PenguinBurner root hardware service.\n\n"
            f"({unavailable_reason})\n\n"
            "PenguinBurner can install or repair it now. If an older "
            "PenguinBurner.service exists, it will be migrated once. This may "
            "ask for your administrator password once."
        )

    answer = QtWidgets.QMessageBox.question(
        parent,
        "PenguinBurner Hardware Service",
        prompt,
        QtWidgets.QMessageBox.StandardButton.Yes
        | QtWidgets.QMessageBox.StandardButton.No,
        QtWidgets.QMessageBox.StandardButton.Yes,
    )
    if answer != QtWidgets.QMessageBox.StandardButton.Yes:
        log(
            "\nPenguinBurner hardware service setup was cancelled. "
            f"Previous status: {unavailable_reason}\n"
        )
        return False

    command = command_factory()
    log("\n$ " + " ".join(shlex.quote(part) for part in command) + "\n")
    result = run_command(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = "\n".join(
        part.strip()
        for part in (getattr(result, "stdout", ""), getattr(result, "stderr", ""))
        if str(part or "").strip()
    )
    if output:
        log(output + "\n")
    if int(getattr(result, "returncode", 1)) != 0:
        QtWidgets.QMessageBox.critical(
            parent,
            "PenguinBurner Hardware Service",
            "PenguinBurner could not install or repair the hardware service.\n\n"
            + (output or f"Exit code: {getattr(result, 'returncode', 1)}"),
        )
        return False

    try:
        check()
    except Exception as exc:  # noqa: BLE001
        QtWidgets.QMessageBox.critical(
            parent,
            "PenguinBurner Hardware Service",
            "The hardware service command finished, but the daemon is not "
            "ready (unreachable or still incompatible).\n\n"
            f"{exc}",
        )
        return False
    return True
