"""Checks every PenguinBurner window runs once it is shown."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from runtime import daemon_client
from ui import assets
from ui.daemon_setup import ensure_daemon_ready_for_privileged_action

NO_NVIDIA_TITLE = "No NVIDIA GPU detected"
NO_NVIDIA_TEXT = (
    "PenguinBurner tunes NVIDIA GPUs and could not find one on this "
    "system (no NVIDIA kernel driver is loaded).\n\n"
    "Make sure a supported NVIDIA card and the proprietary driver are "
    "installed, then restart PenguinBurner. Undervolt scans and profile "
    "applies will not work until then."
)
DAEMON_UPDATE_LABEL = "Updating the PenguinBurner hardware service"


def nvidia_driver_present() -> bool:
    """Cheap, unprivileged, daemon-free: the NVIDIA kernel driver exposes
    /dev/nvidia0. The deeper architecture/driver validation runs at scan
    start."""
    return Path("/dev/nvidia0").exists() or Path("/dev/nvidiactl").exists()


def running_daemon_needs_update() -> bool:
    """True only when a daemon is running and its protocol or release differs
    from this application. A new user with no daemon installed is left
    alone: the install prompt appears on their first privileged action."""
    try:
        daemon_client.daemon_status(timeout_s=1.0)
    except Exception:  # noqa: BLE001 - not running / not installed
        return False
    try:
        daemon_client.require_daemon_capabilities(
            expected_version=assets.application_version()
        )
    except daemon_client.DaemonCompatibilityError:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def run_startup_checks(
    *,
    QtWidgets,
    parent,
    log: Callable[[str], None],
    on_daemon_updated: Callable[[], None],
) -> None:
    """Warn when no NVIDIA GPU is usable, then offer to update a running
    daemon from another incompatible release."""
    if not nvidia_driver_present():
        # An unsupported box gets a plain explanation up front instead of a
        # cryptic scan failure after a pointless root-service install.
        QtWidgets.QMessageBox.warning(parent, NO_NVIDIA_TITLE, NO_NVIDIA_TEXT)
    if not running_daemon_needs_update():
        return
    ensure_daemon_ready_for_privileged_action(
        QtWidgets=QtWidgets,
        parent=parent,
        log=log,
        action_label=DAEMON_UPDATE_LABEL,
    )
    on_daemon_updated()
