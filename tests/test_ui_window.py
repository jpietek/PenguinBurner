"""Coverage for ui/window.py (MainWindow) and ui/main.py launch-option parsing.

MainWindow is built under offscreen Qt with its profile-store/systemd
dependencies monkeypatched, then its data-driven handler methods are called
directly with synthetic payloads. Dialog-opening paths get QDialog.exec /
select_final_candidate stubbed so nothing blocks.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import ui.window as window_mod
from ui.main import parse_gui_args, parse_gui_launch_options
from ui.qt import import_qt
from ui.window import MainWindow


# --- ui/main.py launch-option parsing (pure) ----------------------------------


def test_parse_gui_launch_options_gpu_index() -> None:
    opts = parse_gui_launch_options(
        ["pburn", "--new-ui", "--gpu-index", "2", "extra"]
    )
    assert opts.gpu_index == 2
    assert "extra" in opts.qt_argv


def test_parse_gui_launch_options_equals_forms() -> None:
    opts = parse_gui_launch_options(["pburn", "--index=3"])
    assert opts.gpu_index == 3


def test_parse_gui_launch_options_empty_argv_defaults() -> None:
    opts = parse_gui_launch_options([])
    assert opts.qt_argv == ["penguin-burner-ui"]
    assert opts.gpu_index is None


def test_parse_gui_launch_options_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        parse_gui_launch_options(["pburn", "--gpu-index", "x"])
    with pytest.raises(ValueError):
        parse_gui_launch_options(["pburn", "--gpu-index"])


def test_parse_gui_args_returns_qt_argv() -> None:
    assert parse_gui_args(["pburn", "--gpu-index", "1", "passthrough"]) == [
        "pburn",
        "passthrough",
    ]


# --- MainWindow ---------------------------------------------------------------


@pytest.fixture
def main_window(qapp, monkeypatch):
    monkeypatch.setattr(
        window_mod,
        "load_profile_summaries",
        lambda: [{"profile_id": "p1", "display_name": "P1", "candidate_id": "c1"}],
    )
    monkeypatch.setattr(
        window_mod,
        "systemd_autostart_profile_info",
        lambda: {"selector": "active", "silent_fan_curve": False},
    )
    monkeypatch.setattr(
        window_mod,
        "running_auto_uv_profile_info",
        lambda: {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False},
    )
    monkeypatch.setattr(window_mod, "penguin_burner_runtime_is_active", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_from_runtime_config", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_to_runtime_config", lambda v: v)
    qt_modules = import_qt()
    if qt_modules[3] is None:
        pytest.skip("pyqtgraph not available")
    # Never block on a modal dialog during tests.
    monkeypatch.setattr(qt_modules[2].QDialog, "exec", lambda self: 0)
    window = MainWindow(qt_modules)
    yield window
    window.window.close()


def test_window_handles_scan_events(main_window, monkeypatch) -> None:
    win = main_window
    win._handle_scan_event({"event": "auto_uv_start"})
    win._handle_scan_event({"event": "dependency_progress", "detail": "Fetching", "percent": 40})
    win._handle_scan_event(
        {"event": "probe_start", "stage": "candidate", "voltage_mv": 900, "clock_mhz": 2500}
    )
    win._handle_scan_event(
        {"event": "probe_result", "stage": "candidate", "candidate_voltage_mv": 900,
         "lock_clock_mhz": 2500, "temp_c": 60, "fan_pct": 40}
    )
    win._handle_scan_event({"event": "load_telemetry", "candidate_voltage_mv": 900})
    win._handle_scan_event(
        {"event": "source_curve", "points": [{"voltage_mv": 800, "base_mhz": 2000}]}
    )
    win._handle_scan_event(
        {"event": "candidate_curve", "candidate_id": "c1",
         "points": [{"voltage_mv": 850, "clock_mhz": 2300}]}
    )
    win._handle_scan_event(
        {"event": "fan_curve_suggested", "points": [{"temperature_c": 40, "fan_pct": 30}]}
    )
    win._handle_scan_event({"event": "final_choice_discarded"})
    win._handle_scan_event(
        {"event": "final_result", "candidate_voltage_mv": 900, "lock_clock_mhz": 2500}
    )
    assert win.pending_final_result_payload is not None


def test_window_shows_chronological_full_scan_profile_progress(main_window) -> None:
    win = main_window
    win.auto_uv_tier_progress.start()

    win._handle_scan_event(
        {
            "event": "tier_started",
            "tier": "efficiency",
            "position": 1,
            "total": 3,
            "next_tier": "balanced",
        }
    )
    assert win.auto_uv_tier_progress.state("efficiency") == "active"
    assert win.header.stage() == "Efficiency scan (1/3)"

    win._handle_scan_event(
        {
            "event": "tier_completed",
            "tier": "efficiency",
            "position": 1,
            "total": 3,
            "next_tier": "balanced",
        }
    )
    assert win.auto_uv_tier_progress.state("efficiency") == "complete"
    assert "Continuing with Balanced" in win.controls.status_label.text()

    win._handle_scan_event(
        {
            "event": "tier_started",
            "tier": "balanced",
            "position": 2,
            "total": 3,
            "next_tier": "performance",
        }
    )
    assert win.auto_uv_tier_progress.state("balanced") == "active"
    assert win.auto_uv_tier_progress.state("performance") == "pending"


def test_window_plots_selected_performance_auto_oc_curve(main_window) -> None:
    win = main_window
    win._handle_scan_event(
        {
            "event": "tier_confirmed",
            "tier": "performance",
            "voltage_mv": 925,
            "target_mhz": 2980,
            "points": [
                {"voltage_mv": 850, "clock_mhz": 2595},
                {"voltage_mv": 925, "clock_mhz": 2980},
            ],
        }
    )

    assert win.controls.status_label.text() == (
        "Performance tier confirmed: 925mV @ 2980MHz"
    )
    x_values, y_values = win.vf_plot.comparison_curves[-1].getData()
    assert list(x_values) == [850.0, 925.0]
    assert list(y_values) == [2595.0, 2980.0]
    assert (
        win.vf_plot.comparison_curves[-1].zValue()
        > win.vf_plot.candidate_curve.zValue()
    )


def test_memory_offset_status_text_formats() -> None:
    from ui.window import _memory_offset_status_text

    assert _memory_offset_status_text(0) == "Memory offset: none"
    assert _memory_offset_status_text(500) == "Memory offset: +500 MHz memory clock"
    assert _memory_offset_status_text(-200) == "Memory offset: -200 MHz memory clock"
    assert _memory_offset_status_text(None) == "Memory offset: none"


def test_window_shows_applied_memory_offset_in_status_label(main_window) -> None:
    win = main_window
    win._handle_scan_event({"event": "memory_offset_applied", "offset_mhz": 500})
    assert win.controls.status_label.text() == "Memory offset: +500 MHz memory clock"

    # A none/zero offset reads honestly rather than a stale static string.
    win._handle_scan_event({"event": "memory_offset_applied", "offset_mhz": 0})
    assert win.controls.status_label.text() == "Memory offset: none"


def test_dependency_progress_no_longer_hijacks_the_status_label(main_window) -> None:
    win = main_window
    win._handle_scan_event({"event": "memory_offset_applied", "offset_mhz": 500})
    # The terminal "Dependencies are ready" used to sit in the status label for
    # the whole run; the download status now lives only in the progress bar.
    win._handle_scan_event(
        {
            "event": "dependency_progress",
            "detail": "Dependencies are ready",
            "percent": 100,
        }
    )
    assert win.controls.status_label.text() == "Memory offset: +500 MHz memory clock"
    assert "Dependencies are ready" not in win.controls.status_label.text()


def test_window_final_choice_request_without_response_path(main_window, monkeypatch) -> None:
    monkeypatch.setattr(
        window_mod,
        "select_final_candidate",
        lambda **kwargs: (None, 600, "select"),
    )
    # No response_path -> handler returns after running the dialog selection.
    main_window._handle_scan_event(
        {"event": "final_choice_request", "candidates": [{"candidate_id": "c1"}]}
    )


def test_window_previous_crash_close_writes_abort(main_window, monkeypatch, tmp_path) -> None:
    response_path = tmp_path / "choice.json"
    monkeypatch.setattr(
        window_mod,
        "select_final_candidate",
        lambda **kwargs: (None, 600, "abort"),
    )

    main_window._handle_scan_event(
        {
            "event": "final_choice_request",
            "request_reason": "previous-crash",
            "response_path": str(response_path),
            "candidates": [{"candidate_id": "885mv-2873mhz"}],
        }
    )

    assert '"action": "abort"' in response_path.read_text(encoding="utf-8")
    assert main_window.final_choice_discarded is True


def test_window_previous_crash_start_over_writes_discard(
    main_window,
    monkeypatch,
    tmp_path,
) -> None:
    response_path = tmp_path / "choice.json"
    monkeypatch.setattr(
        window_mod,
        "select_final_candidate",
        lambda **kwargs: (None, 600, "discard"),
    )

    main_window._handle_scan_event(
        {
            "event": "final_choice_request",
            "request_reason": "previous-crash",
            "response_path": str(response_path),
            "candidates": [{"candidate_id": "885mv-2873mhz"}],
        }
    )

    assert '"action": "discard"' in response_path.read_text(encoding="utf-8")
    assert main_window.final_choice_discarded is False


def test_window_human_lines(main_window) -> None:
    main_window._handle_human_line("Starting final verification now")
    main_window._handle_human_line("candidate 900mV under test")
    main_window._handle_human_line("Auto-UV final state reached")
    main_window._handle_human_line("unrelated chatter")


def test_window_scan_finished_branches(main_window) -> None:
    win = main_window
    # stopped-by-user
    win._scan_finished(0, 0, True)
    # pending final result -> Complete
    win.pending_final_result_payload = {"candidate_id": "c1"}
    win._scan_finished(0, 0, False)
    # discarded
    win.final_choice_discarded = True
    win._scan_finished(0, 0, False)
    # failed (errors.show_process is dialog-guarded by patched exec)
    win._scan_finished(1, 0, False)
    # plain idle
    win._scan_finished(0, 0, False)


def test_window_verify_and_command_finished(main_window) -> None:
    main_window.header.set_candidate("User edited memory offset +6000 MT/s")
    main_window._verify_finished(0, 0, False)  # success
    assert main_window.header.candidate_label.text() == ""

    main_window.header.set_candidate("stale candidate")
    main_window._verify_finished(1, 0, True)  # stopped
    assert main_window.header.candidate_label.text() == ""

    main_window.header.set_candidate("stale candidate")
    main_window._verify_finished(1, 0, False)  # failed
    assert main_window.header.candidate_label.text() == ""

    main_window._command_finished("delete", 0, 0)


def test_verify_controller_telemetry_plots_the_live_gpu_position(main_window) -> None:
    win = main_window
    win.verify_controller.on_telemetry(
        {
            "event": "load_telemetry",
            "voltage_mv": 925,
            "clock_mhz": 2950,
            "measured_voltage_mv": 923,
            "measured_clock_mhz": 2940,
        }
    )
    assert win.vf_plot._last_live_probe_values == (923.0, 2940.0)


def test_verify_finished_clears_the_live_gpu_position_marker(main_window) -> None:
    win = main_window
    win.verify_controller.on_telemetry(
        {
            "event": "load_telemetry",
            "voltage_mv": 925,
            "clock_mhz": 2950,
            "measured_voltage_mv": 923,
            "measured_clock_mhz": 2940,
        }
    )
    assert win.vf_plot._last_live_probe_values is not None

    win._verify_finished(0, 0, False)

    assert win.vf_plot._last_live_probe_values is None


def test_window_simple_helpers(main_window) -> None:
    win = main_window
    assert win._workflow_running() is False
    win._set_profile_actions_enabled(True)
    win.profile_list.set_boot_apply_checked(True)
    assert "Autostart: Yes" in win._runtime_action_start_text()
    adaptive_text = win._runtime_action_start_text(adaptive_auto_uv=True)
    assert "adaptive" in adaptive_text.lower()
    assert "Autostart: Yes" in adaptive_text
    win.profile_list.set_boot_apply_checked(False)
    assert "Autostart: No" in win._runtime_action_start_text()
    win._load_profiles()
    win.show_about()


def test_window_tab_order_and_bins_visibility(main_window) -> None:
    win = main_window
    # Tabs are Auto-UV, Profiles, the one library tab that holds every
    # launcher, then Overlay (no separate fan-curve tab).
    labels = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    assert labels == ["Auto-UV", "Profiles", "Game Library", "In-Game Overlay"]
    assert win.tabs.iconSize().width() == 18
    assert win.tabs.iconSize().height() == 18
    assert all(not win.tabs.tabIcon(i).isNull() for i in range(win.tabs.count()))
    assert not hasattr(win, "fan_plot")

    # The undervolting-runs panel shows only on the Auto-UV tab.
    win.tabs.setCurrentIndex(win.auto_uv_tab_index)
    assert not win.table_panel.isHidden()
    win.tabs.setCurrentIndex(win.profiles_tab_index)
    assert win.table_panel.isHidden()
    win.tabs.setCurrentIndex(win.overlay_tab_index)
    assert win.table_panel.isHidden()
    win.tabs.setCurrentIndex(win.game_library_tab_index)
    assert win.table_panel.isHidden()


def test_runs_table_splitter_is_draggable_with_content_derived_floors(
    main_window,
) -> None:
    win = main_window
    qt_widgets = win.QtWidgets
    split = win.auto_uv_split
    assert split.orientation() == win.QtCore.Qt.Vertical
    assert split.widget(0) is win.tabs
    assert split.widget(1) is win.table_panel
    assert not split.childrenCollapsible() or (
        not split.isCollapsible(0) and not split.isCollapsible(1)
    )

    table = win.runs_table.widget
    header_height = table.horizontalHeader().sizeHint().height()
    row_height = table.verticalHeader().defaultSectionSize()
    # The table's floor derives from its own style metrics, not pixel
    # constants: at least MIN_VISIBLE_ROWS rows plus the header/frame.
    assert win.runs_table.MIN_VISIBLE_ROWS == 7
    assert table.minimumHeight() >= (
        header_height + row_height * win.runs_table.MIN_VISIBLE_ROWS
    )
    # The tab side floors at its own content (tab bar + Auto-UV page), NOT at
    # the largest other page's hint, so the splitter default can actually
    # trade plot height for table rows.
    assert win.tabs.minimumHeight() < win.tabs.minimumSizeHint().height()
    _ = qt_widgets


def test_runs_table_follows_newest_pending_row_unless_user_scrolled_up(
    main_window,
) -> None:
    win = main_window
    win.window.show()
    table = win.runs_table.widget
    for index in range(14):
        win.runs_table.add_probe_start(
            {"stage": "candidate", "voltage_mv": 900 - index, "clock_mhz": 2500}
        )
    bar = table.verticalScrollBar()
    # The newest pending row is pinned into view (exact bottom, not one short).
    assert bar.value() == bar.maximum()
    # A user inspecting earlier runs is not yanked back by new rows...
    bar.setValue(0)
    win.runs_table.add_probe_start(
        {"stage": "candidate", "voltage_mv": 777, "clock_mhz": 2500}
    )
    assert bar.value() == 0
    # ...and returning to the tail resumes following.
    bar.setValue(bar.maximum())
    win.runs_table.add_probe_start(
        {"stage": "candidate", "voltage_mv": 776, "clock_mhz": 2500}
    )
    assert bar.value() == bar.maximum()


def test_startup_gpu_check_warns_when_no_nvidia_device(main_window, monkeypatch) -> None:
    win = main_window
    import ui.window as window_mod  # noqa: F811

    warnings = []
    monkeypatch.setattr(
        win.QtWidgets.QMessageBox,
        "warning",
        lambda *args, **kwargs: warnings.append(args),
    )
    # No NVIDIA device nodes present.
    monkeypatch.setattr(window_mod.Path, "exists", lambda self: False)
    win._check_gpu_supported_on_startup()
    assert warnings and "NVIDIA" in warnings[0][2]

    warnings.clear()
    # Device present -> no warning.
    monkeypatch.setattr(
        window_mod.Path, "exists", lambda self: str(self) == "/dev/nvidia0"
    )
    win._check_gpu_supported_on_startup()
    assert warnings == []


def test_startup_daemon_check_only_prompts_for_incompatible_running_daemon(
    main_window, monkeypatch
) -> None:
    win = main_window
    from runtime import daemon_client
    from ui import assets

    prompts = []
    compatibility_checks = []
    monkeypatch.setattr(assets, "application_version", lambda: "0.7.9")
    monkeypatch.setattr(
        window_mod,
        "ensure_daemon_ready_for_privileged_action",
        lambda **kwargs: prompts.append(kwargs.get("action_label")) or True,
    )

    # Not running -> no prompt (a new user is left alone).
    monkeypatch.setattr(
        daemon_client, "daemon_status",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no socket")),
    )
    win._check_daemon_upgrade_on_startup()
    assert prompts == []

    # Running and compatible -> no prompt.
    monkeypatch.setattr(daemon_client, "daemon_status", lambda **kwargs: {"state": "idle"})
    monkeypatch.setattr(
        daemon_client,
        "require_daemon_capabilities",
        lambda *args, **kwargs: compatibility_checks.append((args, kwargs))
        or {"state": "idle"},
    )
    win._check_daemon_upgrade_on_startup()
    assert prompts == []
    assert compatibility_checks == [((), {"expected_version": "0.7.9"})]

    # Running but incompatible (stale 0.6.x) -> prompt to update.
    def _incompatible(*a, **k):
        raise daemon_client.DaemonCompatibilityError("predates the versioned protocol")

    monkeypatch.setattr(daemon_client, "require_daemon_capabilities", _incompatible)
    win._check_daemon_upgrade_on_startup()
    assert prompts and "Updating the PenguinBurner hardware service" in prompts[0]
