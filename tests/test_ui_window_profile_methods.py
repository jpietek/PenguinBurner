"""Coverage for ui/window.py profile edit/verify/export/delete methods.

Builds MainWindow offscreen and drives the profile-action methods with the
editor dialogs, command builders, store helpers, and controllers stubbed.
"""

from __future__ import annotations

import os
from pathlib import Path

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

    def is_running(self) -> bool:
        return self._running

    def start(self, *args, **kwargs) -> bool:
        self.started.append((args, kwargs))
        return True

    def stop(self) -> None:
        self._running = False


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
    # The mixin holds its own binding, and building the window syncs the boot
    # checkbox through it. Left unpatched it reached the developer's real
    # daemon socket, so the checkbox started ticked or not depending on what
    # that machine happened to have saved for boot.
    monkeypatch.setattr(
        actions_mod,
        "systemd_autostart_profile_info",
        lambda **_kwargs: {"selector": "", "silent_fan_curve": False},
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
    monkeypatch.setattr(window.profile_list, "select_profile", lambda pid: None)
    yield window, monkeypatch
    window.window.close()


PROFILE = {"profile_id": "p1", "path": "/tmp/p1.json", "final_verified": True}


def test_edit_fan_curve_no_curve_shows_info(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(actions_mod, "profile_fan_curve_points", lambda profile: [])
    shown: list = []
    monkeypatch.setattr(window.QtWidgets.QMessageBox, "information", lambda *a, **k: shown.append(a))
    window._edit_profile_fan_curve(PROFILE)
    assert shown


def test_edit_fan_curve_opens_and_saves(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(actions_mod, "profile_fan_curve_points", lambda profile: [(30, 20)])
    monkeypatch.setattr(actions_mod, "profile_fan_measurement_points", lambda profile: [])
    monkeypatch.setattr(actions_mod, "profile_fan_curve_target_point", lambda profile: None)
    monkeypatch.setattr(
        actions_mod,
        "save_edited_fan_profile",
        lambda profile, edit, original_points: (
            Path("/tmp/auto-uv-profile-x.json"),
            {"fan_curve_payload": {"points": [[35, 25]]}},
        ),
    )
    # The editor stub fires the save callback to exercise the closure.
    monkeypatch.setattr(
        actions_mod, "open_fan_curve_editor_dialog", lambda **k: k["save_callback"](object())
    )
    window._edit_profile_fan_curve(PROFILE)


def test_edit_vf_curve_no_plan_shows_info(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(actions_mod, "profile_curve_plan", lambda profile: [])
    monkeypatch.setattr(actions_mod, "editable_anchor_from_profile", lambda profile: None)
    shown: list = []
    monkeypatch.setattr(window.QtWidgets.QMessageBox, "information", lambda *a, **k: shown.append(a))
    window._edit_profile_vf_curve(PROFILE)
    assert shown


def test_edit_vf_curve_opens_and_saves(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(
        actions_mod, "profile_curve_plan",
        lambda profile: [{"index": 0, "voltage_mv": 900, "base_mhz": 2400, "target_mhz": 2500}],
    )
    monkeypatch.setattr(actions_mod, "editable_anchor_from_profile", lambda profile: (900, 2500))
    monkeypatch.setattr(actions_mod, "profile_base_curve_points", lambda profile: [(900, 2400)])
    monkeypatch.setattr(actions_mod, "_manual_curve_control_voltage_mvs", lambda manual: ())
    monkeypatch.setattr(
        actions_mod, "save_edited_curve_profile",
        lambda profile, edit, **kw: (Path("/tmp/auto-uv-profile-y.json"), {"candidate_id": "c9"}),
    )
    monkeypatch.setattr(
        actions_mod, "open_vf_curve_editor_dialog", lambda **k: k["save_callback"](object())
    )
    window._edit_profile_vf_curve(PROFILE)
    assert window.last_auto_uv_candidate_id == "c9"


def test_edit_memory_offset_no_value_shows_info(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(actions_mod, "editable_memory_offset_from_profile", lambda profile: None)
    shown: list = []
    monkeypatch.setattr(window.QtWidgets.QMessageBox, "information", lambda *a, **k: shown.append(a))
    window._edit_profile_memory_offset(PROFILE)
    assert shown


def test_edit_memory_offset_opens_and_saves(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(actions_mod, "editable_memory_offset_from_profile", lambda profile: 200)
    monkeypatch.setattr(actions_mod, "memory_offset_mhz_range", lambda gpu_index: (0, 2000))
    monkeypatch.setattr(
        actions_mod,
        "save_edited_memory_offset_profile",
        lambda profile, new_memory_offset_mhz, **kw: (
            Path("/tmp/auto-uv-profile-mem.json"),
            {"memory_offset_mhz": new_memory_offset_mhz},
        ),
    )
    # The editor stub fires the save callback to exercise the closure.
    monkeypatch.setattr(
        actions_mod, "open_memory_offset_editor_dialog", lambda **k: k["save_callback"](400)
    )
    window._edit_profile_memory_offset(PROFILE)


def test_export_lact_cancelled_and_no_gpu(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(
        window.QtWidgets.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "")
    )
    window._export_lact_profile(PROFILE)  # cancelled -> early return

    monkeypatch.setattr(
        window.QtWidgets.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "/tmp/lact")
    )
    monkeypatch.setattr(actions_mod, "detect_lact_gpu_id", lambda directory: "")
    shown: list = []
    monkeypatch.setattr(window.errors, "show", lambda title, msg: shown.append(title))
    window._export_lact_profile(PROFILE)
    assert shown  # no gpu id -> error surfaced


def test_export_lact_writes(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(
        window.QtWidgets.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "/tmp/lact")
    )
    monkeypatch.setattr(actions_mod, "detect_lact_gpu_id", lambda directory: "1002:abcd")
    monkeypatch.setattr(window.profile_list, "silent_fan_enabled", lambda: False)
    monkeypatch.setattr(
        actions_mod, "write_lact_profile_config",
        lambda profile, **kw: (Path("/tmp/lact/config.yaml"), ["a warning"]),
    )
    monkeypatch.setattr(window.QtWidgets.QMessageBox, "information", lambda *a, **k: None)
    window._export_lact_profile(PROFILE)


def test_verify_profile_guards_and_runs(win) -> None:
    window, monkeypatch = win
    # Cannot verify (no path / not afterburner) -> early return.
    window._verify_profile({"profile_id": "x"})

    monkeypatch.setattr(actions_mod, "select_verify_options", lambda **k: None)
    window._verify_profile(PROFILE)  # dialog cancelled -> return

    monkeypatch.setattr(
        actions_mod, "select_verify_options",
        lambda **k: {"duration_s": 60},
    )
    monkeypatch.setattr(
        actions_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(actions_mod, "profile_verify_command", lambda **k: ["echo", "verify"])
    fake = _FakeController()
    window.verify_controller = fake
    window._verify_profile(PROFILE)
    assert fake.started


def test_verify_profile_shows_the_static_curve_being_verified(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(actions_mod, "select_verify_options", lambda **k: {"duration_s": 60})
    monkeypatch.setattr(actions_mod, "ensure_daemon_ready_for_privileged_action", lambda **_k: True)
    monkeypatch.setattr(actions_mod, "profile_verify_command", lambda **k: ["echo", "verify"])
    monkeypatch.setattr(actions_mod, "profile_base_curve_points", lambda profile: [(900, 2400)])
    monkeypatch.setattr(
        actions_mod,
        "profile_curve_plan",
        lambda profile: [{"index": 0, "voltage_mv": 900, "base_mhz": 2400, "target_mhz": 2500}],
    )
    window.verify_controller = _FakeController()

    window._verify_profile(PROFILE)

    assert window.vf_plot._source_points == [(900.0, 2400.0)]
    assert window.vf_plot._candidate_points == [(900.0, 2500.0)]


def test_verify_profile_without_a_curve_leaves_the_plot_untouched(win) -> None:
    window, monkeypatch = win
    monkeypatch.setattr(actions_mod, "select_verify_options", lambda **k: {"duration_s": 60})
    monkeypatch.setattr(actions_mod, "ensure_daemon_ready_for_privileged_action", lambda **_k: True)
    monkeypatch.setattr(actions_mod, "profile_verify_command", lambda **k: ["echo", "verify"])
    monkeypatch.setattr(actions_mod, "profile_base_curve_points", lambda profile: [])
    monkeypatch.setattr(actions_mod, "profile_curve_plan", lambda profile: [])
    window.vf_plot.set_source_points([(800, 2000)])
    window.vf_plot.set_candidate_points([(800, 2100)], remember_previous=False)
    window.verify_controller = _FakeController()

    window._verify_profile(PROFILE)

    assert window.vf_plot._source_points == [(800.0, 2000.0)]
    assert window.vf_plot._candidate_points == [(800.0, 2100.0)]


def test_delete_selected_profiles(win) -> None:
    window, monkeypatch = win
    window.profile_summaries = [PROFILE]
    monkeypatch.setattr(window.profile_list, "selected_profile_ids", lambda: ["p1"])
    monkeypatch.setattr(window.profile_list, "selected_profile_paths", lambda: ["/tmp/p1.json"])
    monkeypatch.setattr(
        actions_mod, "profile_delete_autostart_action", lambda *a, **k: {"action": "keep"}
    )
    monkeypatch.setattr(MainWindow, "_confirm_profile_delete", lambda self, **k: True)
    deleted = []
    monkeypatch.setattr(
        actions_mod, "delete_auto_uv_profile_paths", lambda paths: deleted.extend(paths) or list(paths)
    )
    window._delete_selected_profiles()
    assert deleted == ["/tmp/p1.json"]

    # Nothing selected -> early return.
    monkeypatch.setattr(window.profile_list, "selected_profile_paths", lambda: [])
    monkeypatch.setattr(window.profile_list, "selected_profile_ids", lambda: [])
    window._delete_selected_profiles()


def test_delete_running_session_only_profile_restores_stock(win) -> None:
    # Session-only applies (Apply-on-startup unticked) leave no boot entry, so
    # deleting the actively running profile must still restore stock instead
    # of leaving an orphaned curve applied.
    window, monkeypatch = win
    window.profile_summaries = [PROFILE]
    monkeypatch.setattr(window.profile_list, "selected_profile_ids", lambda: ["p1"])
    monkeypatch.setattr(
        window.profile_list, "selected_profile_paths", lambda: ["/tmp/p1.json"]
    )
    monkeypatch.setattr(
        actions_mod,
        "systemd_autostart_profile_info",
        lambda **_kwargs: {
            "selector": "",
            "silent_fan_curve": False,
            "adaptive_auto_uv": False,
        },
    )
    monkeypatch.setattr(actions_mod, "penguin_burner_runtime_is_active", lambda: True)
    monkeypatch.setattr(
        actions_mod,
        "running_auto_uv_profile_info",
        lambda: {"selector": "p1", "silent_fan_curve": False, "adaptive_auto_uv": False},
    )
    confirmations: list[dict] = []
    monkeypatch.setattr(
        MainWindow,
        "_confirm_profile_delete",
        lambda self, **kwargs: confirmations.append(kwargs) or False,
    )

    window._delete_selected_profiles()

    assert confirmations and confirmations[0]["restore_stock"] is True


def test_delete_checks_boot_profile_for_selected_non_active_gpu(win) -> None:
    window, monkeypatch = win
    window.profile_summaries = [PROFILE]
    monkeypatch.setattr(window.profile_list, "selected_profile_ids", lambda: ["p1"])
    monkeypatch.setattr(
        window.profile_list, "selected_profile_paths", lambda: ["/tmp/p1.json"]
    )
    monkeypatch.setattr(window.profile_list, "target_gpu_uuid", lambda: "GPU-A")
    requested_gpu_uuids: list[str] = []

    def autostart_info(*, gpu_uuid: str = "") -> dict[str, object]:
        requested_gpu_uuids.append(gpu_uuid)
        return {
            "selector": "p1",
            "gpu_uuid": gpu_uuid,
            "silent_fan_curve": False,
            "adaptive_auto_uv": False,
        }

    monkeypatch.setattr(actions_mod, "systemd_autostart_profile_info", autostart_info)
    monkeypatch.setattr(actions_mod, "penguin_burner_runtime_is_active", lambda: False)
    confirmations: list[dict] = []
    monkeypatch.setattr(
        MainWindow,
        "_confirm_profile_delete",
        lambda self, **kwargs: confirmations.append(kwargs) or False,
    )

    window._delete_selected_profiles()

    assert requested_gpu_uuids == ["GPU-A"]
    assert confirmations and confirmations[0]["restore_stock"] is True


def test_unticking_boot_apply_clears_saved_boot_profile(win, monkeypatch) -> None:
    window, _mp = win
    cleared: list[str] = []
    saved: list[bool] = []
    monkeypatch.setattr(
        actions_mod, "persist_on_startup_to_runtime_config", lambda v: saved.append(bool(v))
    )
    monkeypatch.setattr(
        actions_mod,
        "clear_boot_runtime_spec",
        lambda **kwargs: cleared.append(str(kwargs.get("gpu_uuid") or "")),
    )
    monkeypatch.setattr(window.profile_list, "target_gpu_uuid", lambda: "GPU-A")

    window.profile_list.set_boot_apply_checked(True)  # blocked signals: no side effects
    assert cleared == [] and saved == []

    window.profile_list.boot_apply_checkbox.setChecked(False)
    assert saved == [False]
    assert cleared == ["GPU-A"]

    # Ticking arms boot persistence for the next Apply but clears nothing.
    window.profile_list.boot_apply_checkbox.setChecked(True)
    assert saved == [False, True]
    assert cleared == ["GPU-A"]


def test_unticking_boot_apply_failure_restores_checked_state(win, monkeypatch) -> None:
    window, _mp = win
    saved: list[bool] = []
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        actions_mod,
        "persist_on_startup_to_runtime_config",
        lambda value: saved.append(bool(value)),
    )
    monkeypatch.setattr(
        actions_mod,
        "clear_boot_runtime_spec",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("socket unavailable")),
    )
    monkeypatch.setattr(window.profile_list, "target_gpu_uuid", lambda: "GPU-A")
    monkeypatch.setattr(
        window.errors,
        "show",
        lambda title, message: shown.append((title, message)),
    )
    window._boot_apply_by_gpu = {"gpu-a": True}
    window.profile_list.set_boot_apply_checked(True)

    window.profile_list.boot_apply_checkbox.setChecked(False)

    assert window.profile_list.persist_on_startup_enabled() is True
    assert window._boot_apply_by_gpu == {"gpu-a": True}
    assert saved == []
    assert shown == [
        (
            "Apply on startup",
            "Could not clear the saved boot profile: socket unavailable",
        )
    ]


def test_boot_apply_checkbox_tracks_selected_gpu(win, monkeypatch) -> None:
    from ui.features.tuning.gpu_selection import GpuChoice

    window, _mp = win
    monkeypatch.setattr(
        window_mod,
        "gpu_choices_with_fallback",
        lambda **_kwargs: (
            [
                GpuChoice(index=0, name="Card A", uuid="GPU-A"),
                GpuChoice(index=1, name="Card B", uuid="GPU-B"),
            ],
            0,
        ),
    )
    calls: list[str] = []

    def autostart_info(*, gpu_uuid: str = "") -> dict[str, object]:
        calls.append(gpu_uuid)
        if gpu_uuid.casefold() == "gpu-a":
            return {
                "selector": "profile-a",
                "gpu_uuid": "GPU-A",
                "silent_fan_curve": False,
                "adaptive_auto_uv": False,
            }
        return {
            "selector": "",
            "silent_fan_curve": False,
            "adaptive_auto_uv": False,
        }

    monkeypatch.setattr(window_mod, "systemd_autostart_profile_info", autostart_info)
    monkeypatch.setattr(actions_mod, "systemd_autostart_profile_info", autostart_info)
    window._boot_apply_by_gpu = {}
    window.gpu_index = 0

    window._load_profiles()

    assert window.profile_list.target_gpu_uuid() == "GPU-A"
    assert window.profile_list.persist_on_startup_enabled() is True
    window.profile_list.target_gpu_combo.setCurrentIndex(2)
    assert window.profile_list.target_gpu_uuid() == "GPU-B"
    assert window.profile_list.persist_on_startup_enabled() is False
    window.profile_list.target_gpu_combo.setCurrentIndex(1)
    assert window.profile_list.persist_on_startup_enabled() is True
    assert "GPU-A" in calls and "GPU-B" in calls


def test_boot_apply_follows_the_daemon_not_the_saved_preference(
    win, monkeypatch
) -> None:
    """The observed defect: config said on, the daemon had nothing saved.

    `persist_on_startup` is written when the box is ticked but nothing rewrites
    it when the entry goes away, so a boot with nothing to apply was still
    shown as "applies at startup".
    """
    import cli.runtime_config_file as config_mod

    window, _mp = win
    # Patched at the source so any import style sees it: the saved preference
    # claims boot is armed.
    monkeypatch.setattr(
        config_mod,
        "persist_on_startup_from_runtime_config",
        lambda *_a, **_kw: True,
    )
    monkeypatch.setattr(
        actions_mod,
        "systemd_autostart_profile_info",
        lambda **_kwargs: {"selector": "", "main_gpu": False},
    )
    window._boot_apply_by_gpu = {}
    window.profile_list.set_boot_apply_checked(True)

    window._sync_boot_apply_for_target("")

    assert window.profile_list.persist_on_startup_enabled() is False


def test_a_boot_entry_that_disappears_unticks_the_box(win, monkeypatch) -> None:
    """A cached tick must not outlive the entry it described.

    Every apply sends --boot or --clear-boot from this state, so a box stuck ON
    over a daemon that has nothing is a startup promise nothing will keep.
    """
    window, _mp = win
    saved = {"selector": "profile-a", "main_gpu": False}
    monkeypatch.setattr(
        actions_mod, "systemd_autostart_profile_info", lambda **_kwargs: saved
    )
    window._boot_apply_by_gpu = {}

    window._sync_boot_apply_for_target("GPU-A")
    assert window.profile_list.persist_on_startup_enabled() is True

    saved = {"selector": "", "main_gpu": False}
    window._sync_boot_apply_for_target("GPU-A")

    assert window.profile_list.persist_on_startup_enabled() is False


def test_a_fresh_tick_survives_until_an_apply_writes_the_entry(
    win, monkeypatch
) -> None:
    """Ticking arms the next apply; the daemon has nothing to report yet."""
    window, _mp = win
    monkeypatch.setattr(
        actions_mod,
        "systemd_autostart_profile_info",
        lambda **_kwargs: {"selector": "", "main_gpu": False},
    )
    monkeypatch.setattr(
        actions_mod, "persist_on_startup_to_runtime_config", lambda _v: None
    )
    monkeypatch.setattr(window.profile_list, "target_gpu_uuid", lambda: "GPU-A")
    window._boot_apply_by_gpu = {}
    # Start from off with signals blocked, so the tick below is the only
    # user action in play.
    window.profile_list.set_boot_apply_checked(False)

    window.profile_list.boot_apply_checkbox.setChecked(True)
    assert window._boot_apply_by_gpu == {"gpu-a": True}
    window._sync_boot_apply_for_target("GPU-A")

    assert window.profile_list.persist_on_startup_enabled() is True


def test_a_confirmed_entry_drops_the_pending_tick(win, monkeypatch) -> None:
    """Once the daemon holds the entry the session marker is redundant.

    Left behind, it would keep forcing the box ON after the entry was cleared.
    """
    window, _mp = win
    monkeypatch.setattr(
        actions_mod,
        "systemd_autostart_profile_info",
        lambda **_kwargs: {"selector": "profile-a", "main_gpu": False},
    )
    window._boot_apply_by_gpu = {"gpu-a": True}

    window._sync_boot_apply_for_target("GPU-A")

    assert window._boot_apply_by_gpu == {}
    assert window.profile_list.persist_on_startup_enabled() is True


def test_main_gpu_toggle_tracks_target_and_writes_uuid(win, monkeypatch) -> None:
    from ui.features.tuning.gpu_selection import GpuChoice

    window, _mp = win
    monkeypatch.setattr(
        window_mod,
        "gpu_choices_with_fallback",
        lambda **_kwargs: (
            [
                GpuChoice(index=0, name="Card A", uuid="GPU-A"),
                GpuChoice(index=1, name="Card B", uuid="GPU-B"),
            ],
            0,
        ),
    )

    def autostart_info(*, gpu_uuid: str = "") -> dict[str, object]:
        return {
            "selector": f"profile-{gpu_uuid[-1].lower()}" if gpu_uuid else "",
            "gpu_uuid": gpu_uuid,
            "silent_fan_curve": False,
            "adaptive_auto_uv": False,
            "main_gpu": gpu_uuid == "GPU-A",
        }

    selected: list[str] = []
    monkeypatch.setattr(window_mod, "systemd_autostart_profile_info", autostart_info)
    monkeypatch.setattr(actions_mod, "systemd_autostart_profile_info", autostart_info)
    monkeypatch.setattr(
        actions_mod,
        "set_boot_main_gpu",
        lambda gpu_uuid: selected.append(gpu_uuid),
    )
    window._boot_apply_by_gpu = {}
    window.gpu_index = 0
    window._load_profiles()

    assert not window.profile_list.main_gpu_checkbox.isHidden()
    assert window.profile_list.main_gpu_checkbox.isChecked()
    window.profile_list.target_gpu_combo.setCurrentIndex(2)
    assert not window.profile_list.main_gpu_checkbox.isChecked()
    window.profile_list.main_gpu_checkbox.setChecked(True)
    window.profile_list.main_gpu_checkbox.setChecked(False)

    assert selected == ["GPU-B", ""]


def test_boot_toggle_without_multi_gpu_target_cannot_clear_all(win, monkeypatch) -> None:
    window, _mp = win
    saved: list[bool] = []
    cleared: list[str] = []
    monkeypatch.setattr(window.profile_list, "target_gpu_uuid", lambda: "")
    monkeypatch.setattr(window.profile_list, "target_selection_required", lambda: True)
    monkeypatch.setattr(
        actions_mod,
        "persist_on_startup_to_runtime_config",
        lambda value: saved.append(bool(value)),
    )
    monkeypatch.setattr(
        actions_mod,
        "clear_boot_runtime_spec",
        lambda **kwargs: cleared.append(str(kwargs.get("gpu_uuid") or "")),
    )

    window._persist_boot_apply_preference(False)

    assert saved == []
    assert cleared == []


def test_delete_boot_profile_falls_back_to_persisted_stock(win) -> None:
    window, monkeypatch = win
    restored: list[bool] = []
    monkeypatch.setattr(
        window,
        "_restore_gpu_defaults",
        lambda: restored.append(True),
    )

    handled = window._run_delete_autostart_followup(restore_stock=True)

    assert handled is True
    assert restored == [True]


def test_silent_fan_tick_survives_discarded_run(win, monkeypatch) -> None:
    # Regression: after a discarded/aborted Auto-UV run the runtime/autostart no
    # longer carry the silent-fan flag, but the user's persisted choice must keep
    # the tick checked. (The win fixture already reports both as False.)
    window, _mp = win
    monkeypatch.setattr(window_mod, "silent_fan_curve_from_runtime_config", lambda: True)
    monkeypatch.setattr(window_mod, "silent_fan_curve_to_runtime_config", lambda v: v)

    window.profile_list.silent_fan_checkbox.setChecked(False)
    window._load_profiles()

    assert window.profile_list.silent_fan_enabled() is True


def test_scan_completion_restores_pre_scan_silent_fan(win, monkeypatch) -> None:
    # A foreground scan resets the GPU to stock, so the completion reload sees
    # a fan-off running state. The auto-applied final profile must still carry
    # the silent-fan curve the user had running before the scan, without any
    # toggle change from them.
    window, _mp = win
    monkeypatch.setattr(window_mod, "silent_fan_curve_from_runtime_config", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_to_runtime_config", lambda v: v)
    monkeypatch.setattr(window_mod, "penguin_burner_runtime_is_active", lambda: True)
    monkeypatch.setattr(
        window_mod,
        "running_auto_uv_profile_info",
        lambda: {"selector": "p1", "silent_fan_curve": True, "adaptive_auto_uv": False},
    )
    monkeypatch.setattr(window_mod, "select_scan_tuning", lambda **k: {"gpu_index": 0})
    monkeypatch.setattr(window_mod, "persist_runtime_gpu_index", lambda idx: int(idx))
    monkeypatch.setattr(
        window_mod, "ensure_daemon_ready_for_privileged_action", lambda **_k: True
    )
    monkeypatch.setattr(window_mod, "scan_command", lambda options: ["echo", "scan"])
    window.scan_controller = _FakeController()

    # Start a scan: the pre-scan silent-fan intent (live running profile) is
    # captured even though the checkbox and config are both off.
    window.start_scan()
    assert window._pre_scan_silent_fan is True

    # After the scan the running profile reads as stock (fan off).
    monkeypatch.setattr(
        window_mod,
        "running_auto_uv_profile_info",
        lambda: {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False},
    )
    applied = []
    monkeypatch.setattr(
        window, "_run_runtime_action", lambda action: applied.append(action)
    )
    # Drive the completion path with a pending final result (auto-apply).
    window.pending_final_result_payload = {"candidate_id": "c1"}
    window._scan_finished(0, 0, False)
    window.QtCore.QCoreApplication.processEvents()

    assert window.profile_list.silent_fan_enabled() is True
    assert applied == ["daemonize"]


def test_silent_fan_tick_stays_unchecked_when_not_persisted(win, monkeypatch) -> None:
    window, _mp = win
    monkeypatch.setattr(window_mod, "silent_fan_curve_from_runtime_config", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_to_runtime_config", lambda v: v)

    window.profile_list.silent_fan_checkbox.setChecked(True)
    window._load_profiles()

    assert window.profile_list.silent_fan_enabled() is False


def test_apply_profile_persists_silent_fan_choice(win, monkeypatch) -> None:
    # Applying a profile with the tick on must seed the durable preference so it
    # survives a later discarded Auto-UV run even without a manual toggle.
    window, _mp = win
    saved: list[bool] = []
    monkeypatch.setattr(window_mod, "silent_fan_curve_to_runtime_config", lambda v: saved.append(bool(v)))
    monkeypatch.setattr(actions_mod, "profile_for_selector", lambda summaries, pid: dict(PROFILE))
    monkeypatch.setattr(actions_mod, "profile_can_apply", lambda p: True)
    monkeypatch.setattr(actions_mod, "sync_profile_fan_payload", lambda p: True)
    monkeypatch.setattr(
        actions_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(actions_mod, "runtime_profile_command", lambda *a, **k: ["pb"])
    monkeypatch.setattr(window.profile_list, "selected_profile_id", lambda: "p1")
    monkeypatch.setattr(window.profile_list, "silent_fan_enabled", lambda: True)
    window.profile_summaries = [PROFILE]

    window._run_runtime_action("daemonize")

    assert saved and saved[-1] is True


def test_scan_finish_leaves_exact_runtime_restoration_to_daemon(win, monkeypatch) -> None:
    # burnerd restores the exact active RuntimeSpec (which may differ from the
    # boot profile). The UI must not issue a second runtime action after abort.
    window, _mp = win
    started: list = []
    monkeypatch.setattr(
        window.command_controller, "start",
        lambda *a, **k: started.append((a, k)) or True,
    )
    window.final_choice_aborted = True

    window._scan_finished(1, 0, False)

    assert started == []
