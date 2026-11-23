"""The checks both windows run after they are shown (ui/startup_checks.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import ui.startup_checks as checks
from runtime import daemon_client
from ui import assets


@pytest.fixture
def shown(monkeypatch):
    """Run the checks with a fake QMessageBox; returns warnings and prompts."""
    result = {"warnings": [], "prompts": [], "updated": 0}
    box = SimpleNamespace(
        warning=lambda parent, title, text: result["warnings"].append((title, text))
    )
    monkeypatch.setattr(
        checks,
        "ensure_daemon_ready_for_privileged_action",
        lambda **kwargs: result["prompts"].append(kwargs["action_label"]) or True,
    )

    def run():
        def updated():
            result["updated"] += 1

        checks.run_startup_checks(
            QtWidgets=SimpleNamespace(QMessageBox=box),
            parent=None,
            log=lambda _text: None,
            on_daemon_updated=updated,
        )
        return result

    return run


def test_warns_only_without_an_nvidia_device(shown, monkeypatch) -> None:
    monkeypatch.setattr(checks, "running_daemon_needs_update", lambda: False)
    monkeypatch.setattr(checks.Path, "exists", lambda self: False)
    result = shown()
    assert result["warnings"][0][0] == "No NVIDIA GPU detected"
    assert "NVIDIA" in result["warnings"][0][1]

    result["warnings"].clear()
    monkeypatch.setattr(checks.Path, "exists", lambda self: str(self) == "/dev/nvidia0")
    assert shown()["warnings"] == []


def test_only_an_incompatible_running_daemon_is_offered_an_update(
    shown, monkeypatch
) -> None:
    monkeypatch.setattr(checks, "nvidia_driver_present", lambda: True)
    monkeypatch.setattr(assets, "application_version", lambda: "0.7.9")
    compatibility_checks = []

    # Not running -> no prompt (a new user is left alone).
    monkeypatch.setattr(
        daemon_client,
        "daemon_status",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no socket")),
    )
    assert shown()["prompts"] == []

    # Running and compatible -> no prompt.
    monkeypatch.setattr(daemon_client, "daemon_status", lambda **kwargs: {"state": "idle"})
    monkeypatch.setattr(
        daemon_client,
        "require_daemon_capabilities",
        lambda *args, **kwargs: compatibility_checks.append((args, kwargs))
        or {"state": "idle"},
    )
    assert shown()["prompts"] == []
    assert compatibility_checks == [((), {"expected_version": "0.7.9"})]

    # Running but incompatible (stale 0.6.x) -> prompt, then the window reloads.
    def incompatible(*_args, **_kwargs):
        raise daemon_client.DaemonCompatibilityError("predates the versioned protocol")

    monkeypatch.setattr(daemon_client, "require_daemon_capabilities", incompatible)
    result = shown()
    assert result["prompts"] == ["Updating the PenguinBurner hardware service"]
    assert result["updated"] == 1

    # Any other capability error is not an upgrade case.
    monkeypatch.setattr(
        daemon_client,
        "require_daemon_capabilities",
        lambda **kwargs: (_ for _ in ()).throw(OSError("socket reset")),
    )
    assert checks.running_daemon_needs_update() is False
