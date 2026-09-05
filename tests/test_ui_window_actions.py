"""Coverage for ui/window.py action methods: scan start/stop and runtime actions.

Builds MainWindow offscreen, then drives the dialog/process-backed methods with
the dialog functions, command builders, and controllers stubbed so nothing
blocks or launches a real process.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import ui.features.profiles.profile_actions as actions_mod
import ui.window as window_mod
from ui.qt import import_qt
from ui.features.tuning.gpu_selection import GpuChoice
from ui.window import MainWindow


class _FakeController:
    def __init__(self) -> None:
        self._running = False
        self.started: list = []
        self.stopped = 0

    def is_running(self) -> bool:
        return self._running

    def start(self, *args, **kwargs) -> bool:
        self.started.append((args, kwargs))
        return True

    def stop(self) -> None:
        self.stopped += 1


@pytest.fixture
def win(qapp, monkeypatch):
    monkeypatch.setattr(
        window_mod, "gpu_choices_with_fallback",
        lambda **_: ([GpuChoice(index=0, name="RTX 5080", uuid="GPU-test")], 0),
    )
    monkeypatch.setattr(window_mod, "load_profile_summaries", lambda: [])
    monkeypatch.setattr(
        window_mod, "systemd_autostart_profile_info", lambda: {"selector": "", "silent_fan_curve": False}
    )
    monkeypatch.setattr(
        window_mod, "running_auto_uv_profile_info",
        lambda: {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False},
    )
    monkeypatch.setattr(window_mod, "penguin_burner_runtime_is_active", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_from_runtime_config", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_to_runtime_config", lambda v: v)
    modules = import_qt()
    if modules[3] is None:
        pytest.skip("pyqtgraph not available")
    monkeypatch.setattr(modules[2].QDialog, "exec", lambda self: 0)
    window = MainWindow(modules)
    yield window, monkeypatch
    window.window.close()


def test_start_scan_cancelled(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(
        window_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(window_mod, "select_scan_tuning", lambda **k: None)
    fake = _FakeController()
    window.scan_controller = fake
    window.start_scan()
    assert fake.started == []  # dialog cancelled -> nothing started


def test_start_scan_runs(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(window_mod, "select_scan_tuning", lambda **k: {"gpu_index": 0})
    monkeypatch.setattr(window_mod, "persist_runtime_gpu_index", lambda idx: int(idx))
    monkeypatch.setattr(
        window_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(window_mod, "scan_command", lambda options: ["echo", "scan"])
    fake = _FakeController()
    window.scan_controller = fake
    window.start_scan()
    assert fake.started  # scan command launched


def test_full_scan_shows_tier_progress_but_selected_profile_scan_hides_it(win) -> None:
    window, monkeypatch = win
    selected_options = {"gpu_index": 0, "auto_uv_mode": "adaptive"}
    monkeypatch.setattr(
        window_mod, "select_scan_tuning", lambda **_kwargs: dict(selected_options)
    )
    monkeypatch.setattr(window_mod, "persist_runtime_gpu_index", lambda idx: int(idx))
    monkeypatch.setattr(
        window_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(window_mod, "scan_command", lambda options: ["echo", "scan"])
    window.scan_controller = _FakeController()

    window.start_scan()
    assert not window.auto_uv_tier_progress.widget.isHidden()
    assert window.auto_uv_tier_progress.state("efficiency") == "pending"

    selected_options["auto_uv_mode"] = "balanced"
    window.start_scan()
    assert window.auto_uv_tier_progress.widget.isHidden()


def test_start_scan_runs_daemon_gate_before_the_setup_dialog(win) -> None:
    # The setup dialog reads GPU identity/limits through the daemon: the
    # install/update prompt must fire BEFORE the dialog opens, or a fresh
    # install shows a misleading generic GPU with no limits.
    window, monkeypatch = win
    order = []

    def fake_gate(**kwargs):
        order.append(("gate", kwargs["action_label"]))
        return True

    def fake_dialog(**_kwargs):
        order.append(("dialog", None))
        return {"gpu_index": 0}

    monkeypatch.setattr(
        window_mod,
        "ensure_daemon_ready_for_privileged_action",
        fake_gate,
    )
    monkeypatch.setattr(window_mod, "select_scan_tuning", fake_dialog)
    monkeypatch.setattr(window_mod, "persist_runtime_gpu_index", lambda idx: int(idx))
    monkeypatch.setattr(window_mod, "scan_command", lambda options: ["echo", "scan"])
    fake = _FakeController()
    window.scan_controller = fake

    window.start_scan()

    assert order[0] == ("gate", "Setting up Auto-UV")
    assert order[1] == ("dialog", None)
    assert fake.started


def test_start_scan_blocks_when_daemon_migration_cancelled(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(window_mod, "select_scan_tuning", lambda **k: {"gpu_index": 0})
    monkeypatch.setattr(window_mod, "persist_runtime_gpu_index", lambda idx: int(idx))
    monkeypatch.setattr(
        window_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(window_mod, "scan_command", lambda options: ["echo", "scan"])
    fake = _FakeController()
    window.scan_controller = fake

    window.start_scan()

    assert fake.started == []


def test_start_scan_switches_to_auto_uv_tab(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(window_mod, "select_scan_tuning", lambda **k: {"gpu_index": 0})
    monkeypatch.setattr(window_mod, "persist_runtime_gpu_index", lambda idx: int(idx))
    monkeypatch.setattr(
        window_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(window_mod, "scan_command", lambda options: ["echo", "scan"])
    window.scan_controller = _FakeController()
    # Start from a different tab; the scan must pull the user to Auto-UV.
    window.tabs.setCurrentIndex(window.profiles_tab_index)
    assert window.tabs.currentIndex() != window.auto_uv_tab_index
    window.start_scan()
    assert window.tabs.currentIndex() == window.auto_uv_tab_index


def test_start_scan_gpu_persist_error(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(
        window_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(window_mod, "select_scan_tuning", lambda **k: {"gpu_index": 0})

    def _boom(_idx):
        raise RuntimeError("cannot save")

    monkeypatch.setattr(window_mod, "persist_runtime_gpu_index", _boom)
    fake = _FakeController()
    window.scan_controller = fake
    window.start_scan()
    assert fake.started == []  # aborted on persist error


def test_stop_scan_paths(win) -> None:
    window, _monkeypatch = win
    verify = _FakeController()
    scan = _FakeController()
    window.verify_controller = verify
    window.scan_controller = scan

    # Nothing running -> no-op.
    window.stop_scan()
    assert verify.stopped == 0 and scan.stopped == 0

    # Verify running takes priority.
    verify._running = True
    window.stop_scan()
    assert verify.stopped == 1

    # Otherwise the scan controller is stopped.
    verify._running = False
    scan._running = True
    window.stop_scan()
    assert scan.stopped == 1


def test_run_runtime_action_no_profile(win) -> None:
    window, monkeypatch = win
    window.profile_summaries = []
    monkeypatch.setattr(window.profile_list, "selected_profile_id", lambda: "")
    monkeypatch.setattr(window.profile_list, "silent_fan_enabled", lambda: False)
    fake = _FakeController()
    window.command_controller = fake
    window._run_runtime_action("daemonize")
    assert fake.started == []  # no profile -> nothing launched


def test_run_runtime_action_launches(win) -> None:
    window, monkeypatch = win
    window.profile_summaries = [
        {"profile_id": "p1", "final_verified": True, "path": "/tmp/p1.json"}
    ]
    monkeypatch.setattr(window.profile_list, "selected_profile_id", lambda: "p1")
    monkeypatch.setattr(window.profile_list, "silent_fan_enabled", lambda: False)
    monkeypatch.setattr(window.profile_list, "set_runtime_actions_enabled", lambda enabled: None)
    monkeypatch.setattr(
        actions_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(actions_mod, "runtime_profile_command", lambda *a, **k: ["echo", "run"])
    fake = _FakeController()
    window.command_controller = fake
    window._run_runtime_action("daemonize")
    assert fake.started  # runtime command launched


def test_apply_selected_profile_with_persistence_stays_on_daemon_path(win) -> None:
    window, monkeypatch = win
    captured: list[tuple[tuple, dict]] = []
    window.profile_summaries = [
        {"profile_id": "eff", "final_verified": True, "profile_tier": "Efficiency"},
        {"profile_id": "perf", "final_verified": True, "profile_tier": "Performance"},
    ]
    monkeypatch.setattr(window.profile_list, "silent_fan_enabled", lambda: False)
    monkeypatch.setattr(window.profile_list, "selected_profile_id", lambda: "perf")
    monkeypatch.setattr(
        actions_mod,
        "profile_for_selector",
        lambda _profiles, _selector: window.profile_summaries[-1],
    )
    monkeypatch.setattr(
        actions_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        actions_mod,
        "runtime_profile_command",
        lambda *args, **kwargs: captured.append((args, kwargs)) or ["daemon-client"],
    )
    window.command_controller = _FakeController()

    window.profile_list.set_boot_apply_checked(True)
    window._run_profiles()

    assert captured
    args, kwargs = captured[0]
    assert args == ("daemonize",)
    assert kwargs["adaptive_auto_uv"] is False
    assert kwargs["profile_selector"] == "perf"
    assert kwargs["persist_on_startup"] is True


def test_apply_without_boot_toggle_is_session_only(win) -> None:
    window, monkeypatch = win
    captured: list[tuple[tuple, dict]] = []
    window.profile_summaries = [
        {"profile_id": "perf", "final_verified": True, "profile_tier": "Performance"},
    ]
    monkeypatch.setattr(window.profile_list, "silent_fan_enabled", lambda: False)
    monkeypatch.setattr(window.profile_list, "selected_profile_id", lambda: "perf")
    monkeypatch.setattr(
        actions_mod,
        "profile_for_selector",
        lambda _profiles, _selector: window.profile_summaries[-1],
    )
    monkeypatch.setattr(
        actions_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        actions_mod,
        "runtime_profile_command",
        lambda *args, **kwargs: captured.append((args, kwargs)) or ["daemon-client"],
    )
    window.command_controller = _FakeController()

    window.profile_list.set_boot_apply_checked(False)
    window._run_profiles()

    assert captured
    _args, kwargs = captured[0]
    assert kwargs["persist_on_startup"] is False
    assert "Autostart: No" in window.controls.status_label.text()


def test_apply_bound_profile_targets_non_display_gpu(win) -> None:
    window, monkeypatch = win
    captured: list[dict] = []
    profile = {
        "profile_id": "gpu-b-profile",
        "final_verified": True,
        "gpu_identity": {"uuid": "GPU-B"},
    }
    window.profile_summaries = [profile]
    monkeypatch.setattr(
        window.profile_list,
        "selected_profile_id",
        lambda: "gpu-b-profile",
    )
    monkeypatch.setattr(window.profile_list, "target_gpu_index", lambda: 1)
    monkeypatch.setattr(window.profile_list, "target_gpu_uuid", lambda: "GPU-B")
    monkeypatch.setattr(window.profile_list, "profile_matches_target", lambda _p: True)
    monkeypatch.setattr(window.profile_list, "silent_fan_enabled", lambda: False)
    monkeypatch.setattr(
        actions_mod,
        "running_auto_uv_profile_info",
        lambda: {"gpu_uuid": ""},
    )
    monkeypatch.setattr(
        actions_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        actions_mod,
        "runtime_profile_command",
        lambda *args, **kwargs: captured.append(kwargs) or ["daemon-client"],
    )
    window.command_controller = _FakeController()
    window.profile_list.set_boot_apply_checked(True)

    window._run_profiles()

    assert captured[0]["gpu_index"] == 1
    assert captured[0]["profile_selector"] == "gpu-b-profile"
    assert captured[0]["persist_on_startup"] is True


def test_restore_defaults_persists_stock_now_and_at_boot(win) -> None:
    window, monkeypatch = win
    captured: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(window.profile_list, "silent_fan_enabled", lambda: False)
    monkeypatch.setattr(
        actions_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        actions_mod,
        "runtime_profile_command",
        lambda *args, **kwargs: captured.append((args, kwargs)) or ["daemon-client"],
    )
    window.command_controller = _FakeController()

    window._restore_gpu_defaults()

    assert captured
    args, kwargs = captured[0]
    assert args == ("daemonize",)
    assert kwargs["profile_selector"] == "__stock__"
    assert "stock now and at boot" in window.controls.status_label.text()


def test_run_runtime_action_runs_daemon_migration_gate_before_apply(win) -> None:
    window, monkeypatch = win
    gate_calls = []
    window.profile_summaries = [
        {"profile_id": "p1", "final_verified": True, "path": "/tmp/p1.json"}
    ]
    monkeypatch.setattr(window.profile_list, "selected_profile_id", lambda: "p1")
    monkeypatch.setattr(window.profile_list, "silent_fan_enabled", lambda: False)
    monkeypatch.setattr(window.profile_list, "set_runtime_actions_enabled", lambda enabled: None)

    def fake_gate(**kwargs):
        gate_calls.append(kwargs)
        return True

    monkeypatch.setattr(
        actions_mod,
        "ensure_daemon_ready_for_privileged_action",
        fake_gate,
    )
    monkeypatch.setattr(actions_mod, "runtime_profile_command", lambda *a, **k: ["echo", "run"])
    fake = _FakeController()
    window.command_controller = fake

    window._run_runtime_action("daemonize")

    assert gate_calls
    assert gate_calls[0]["action_label"] == "Applying runtime profile"
    assert fake.started


def test_run_runtime_action_blocks_when_daemon_migration_cancelled(win) -> None:
    window, monkeypatch = win
    window.profile_summaries = [
        {"profile_id": "p1", "final_verified": True, "path": "/tmp/p1.json"}
    ]
    monkeypatch.setattr(window.profile_list, "selected_profile_id", lambda: "p1")
    monkeypatch.setattr(window.profile_list, "silent_fan_enabled", lambda: False)
    monkeypatch.setattr(
        actions_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(actions_mod, "runtime_profile_command", lambda *a, **k: ["echo", "run"])
    fake = _FakeController()
    window.command_controller = fake

    window._run_runtime_action("daemonize")

    assert fake.started == []


def test_run_runtime_action_blocked_when_busy(win) -> None:
    window, _monkeypatch = win
    busy = _FakeController()
    busy._running = True
    window.command_controller = busy
    window._run_runtime_action("daemonize")
    # _workflow_running() short-circuits; start was never called again.
    assert busy.started == []


def test_run_adaptive_requires_at_least_one_tier(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(
        actions_mod,
        "adaptive_profile_tier_labels",
        lambda profs, **kwargs: [],
    )
    shown: list = []
    monkeypatch.setattr(window.errors, "show", lambda title, msg: shown.append(title))
    fake = _FakeController()
    window.command_controller = fake
    window._run_runtime_action("daemonize", adaptive_auto_uv=True)
    assert shown  # error surfaced, nothing launched
    assert fake.started == []
