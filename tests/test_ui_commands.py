from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import ui.commands as commands
from auto_uv3.auto_uv_user_options import AUTO_UV_DEFAULTS
from auto_uv3.scan_runtime_settings import (
    short_probe_base_duration_s as _short_probe_base_duration_s,
)
from auto_uv3.ui.final_verification_candidate_choice import (
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
    final_choice_sort_column_for_mode as _final_choice_sort_column_for_mode,
    final_choice_intro_text as _final_choice_intro_text,
    sort_candidates_for_final_choice as _sort_candidates_for_final_choice,
)
from ui.dialogs.scan_tuning import (
    _auto_voltage_drop_note_text,
)
from ui.main import parse_gui_args as _parse_gui_args
from ui.models import top_status_text as _top_status_text
from ui.models import probe_decision_label as _probe_decision_label
from ui.models import probe_failure_label as _probe_failure_label
from ui.styles import STYLESHEET
from ui.styles import performance_bias_slider_stylesheet
from ui.tuning import (
    AUTO_UV_DROP_REFERENCE_VOLTAGE_MV,
    DEFAULT_AUTO_UV_MAX_CLOCK_DROP_PCT,
    DEFAULT_AUTO_UV_MAX_DROP_PCT,
    DEFAULT_AUTO_UV_PERFORMANCE_BIAS_PCT,
    DEFAULT_SHORT_VERIFICATION_BASE_S,
    GENERIC_AUTO_UV_MAX_DROP_PCT,
    GPU_UNDERVOLTING_PURPOSE_TEXT,
    MAX_OVERCLOCK_BUDGET_PCT,
    PERFORMANCE_BIAS_TOOLTIP_TEXT,
    YOLO_MAX_OVERCLOCK_BUDGET_PCT,
    auto_uv_mode_for_performance_bias as _auto_uv_mode_for_performance_bias,
    auto_uv_voltage_drop_default as _auto_uv_voltage_drop_default,
    performance_bias_clock_recovery_pct as _performance_bias_clock_recovery_pct,
    performance_bias_slider_position as _performance_bias_slider_position,
    slider_value_from_click_position as _slider_value_from_click_position,
)
from ui.components.runs_table import (
    RunsTable,
    _bounce_position_for_frame,
    _budget_fill_color,
    _budget_display_values,
    _budget_recovery_display_values,
    _budget_recovery_text,
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


def test_ui_scan_command_passes_desktop_user_through_pkexec(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(commands.os, "getuid", lambda: 1000)
    monkeypatch.setattr(commands.os, "getgid", lambda: 1000)
    monkeypatch.setenv("USER", "desktop-user")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")

    def fake_which(name: str) -> str | None:
        return {
            "pkexec": "/usr/bin/pkexec",
            "sudo": "/usr/bin/sudo",
            "env": "/usr/bin/env",
        }.get(name)

    monkeypatch.setattr(commands.shutil, "which", fake_which)

    command = commands.scan_command()

    assert command[:2] == ["/usr/bin/pkexec", "/usr/bin/env"]
    assert "PENGUIN_BURNER_Q2RTX_USER=desktop-user" in command
    assert "PENGUIN_BURNER_Q2RTX_UID=1000" in command
    assert "PENGUIN_BURNER_Q2RTX_GID=1000" in command
    assert "SUDO_USER=desktop-user" in command
    assert "SUDO_UID=1000" in command
    assert "SUDO_GID=1000" in command
    assert "DISPLAY=:0" in command
    assert "XDG_RUNTIME_DIR=/run/user/1000" in command
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus" in command
    assert "--auto-uv-voltage-scan" in command
    assert "--json-events" in command
    assert "--auto-uv-require-final-choice" in command


def test_ui_scan_command_adds_auto_uv_tuning_options(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)

    command = commands.scan_command(
        {
            "auto_uv_mode": "performance",
            "auto_uv_max_drop_pct": 16.0,
            "auto_uv_max_clock_drop_pct": 10.0,
            "auto_uv_clock_bump_budget_ratio": 1.25,
            "auto_uv_yolo": True,
            "auto_uv_short_seconds": 30,
            "auto_uv_memory_offset_mhz": 500,
        }
    )

    assert "--auto-uv-mode" in command
    assert command[command.index("--auto-uv-mode") + 1] == "performance"
    assert "--auto-uv-max-drop-pct" in command
    assert command[command.index("--auto-uv-max-drop-pct") + 1] == "16"
    assert "--auto-uv-max-clock-drop-pct" in command
    assert command[command.index("--auto-uv-max-clock-drop-pct") + 1] == "10"
    assert "--auto-uv-overclock-budget-ratio" in command
    assert command[command.index("--auto-uv-overclock-budget-ratio") + 1] == "1.25"
    assert "--yolo" in command
    assert "--auto-uv-efficiency-stop-streak" not in command
    assert "--auto-uv-min-efficiency-stop-drop-pct" not in command
    assert "--auto-uv-short-seconds" in command
    assert command[command.index("--auto-uv-short-seconds") + 1] == "30"
    assert "--auto-uv-memory-offset-mhz" in command
    assert command[command.index("--auto-uv-memory-offset-mhz") + 1] == "500"


def test_ui_scan_command_includes_auto_filled_auto_uv_max_drop(
    monkeypatch,
) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)

    command = commands.scan_command(
        {
            "auto_uv_mode": "efficiency",
            "auto_uv_max_drop_pct": 15.0,
            "auto_uv_max_clock_drop_pct": 10.0,
        }
    )

    assert "--auto-uv-max-drop-pct" in command
    assert command[command.index("--auto-uv-max-drop-pct") + 1] == "15"


def test_auto_uv_short_verification_defaults_to_20_seconds() -> None:
    assert AUTO_UV_DEFAULTS.probe_duration_s == 20
    assert DEFAULT_SHORT_VERIFICATION_BASE_S == 20
    assert _short_probe_base_duration_s({}) == 20


def test_gui_yolo_argument_is_hidden_from_qt_args() -> None:
    qt_args, yolo, auto_uv3 = _parse_gui_args(
        ["penguin-burner-ui", "--style", "Fusion", "--yolo"]
    )

    assert yolo is True
    assert auto_uv3 is False
    assert qt_args == ["penguin-burner-ui", "--style", "Fusion"]


def test_gui_auto_uv3_argument_is_hidden_from_qt_args() -> None:
    qt_args, yolo, auto_uv3 = _parse_gui_args(
        ["penguin-burner-ui", "--auto-uv3", "--style", "Fusion"]
    )

    assert yolo is False
    assert auto_uv3 is True
    assert qt_args == ["penguin-burner-ui", "--style", "Fusion"]


def test_probe_failure_labels_distinguish_recoverable_and_fatal_reasons() -> None:
    assert _probe_decision_label(
        {
            "decision": "fail",
            "failure_kind": "low-clock",
            "reason": "average busy core clock below floor",
        }
    ) == "Clock too low"
    assert _probe_failure_label(
        {
            "decision": "fail",
            "failure_kind": "fps-regression",
            "reason": "timedemo single-run FPS below floor current=79 floor=80",
        }
    ) == "Single run FPS low"
    assert _probe_failure_label(
        {
            "decision": "fail",
            "failure_kind": "fps-regression",
            "reason": "timedemo average FPS below floor current=89 floor=90",
        }
    ) == "Average FPS low"
    assert _probe_failure_label(
        {
            "decision": "fail",
            "failure_kind": "fatal-output",
            "fatal_output_matches": ["VK_ERROR_DEVICE_LOST"],
        }
    ) == "Vulkan device lost"
    assert _probe_failure_label(
        {
            "decision": "fail",
            "failure_kind": "nvidia-xid",
        }
    ) == "Nvidia Xid fail"
    assert _probe_failure_label(
        {
            "decision": "fail",
            "failure_kind": "load-lost",
        }
    ) == "GPU load too low"


def test_probe_failure_severity_controls_table_row_state() -> None:
    assert _row_state(
        {
            "decision": "fail",
            "failure_kind": "low-clock",
            "failure_severity": "recoverable",
            "reason": "average busy core clock below floor",
        },
        running=False,
    ) == "warning"
    assert _row_state(
        {
            "decision": "fail",
            "failure_kind": "fatal-output",
            "failure_severity": "critical",
            "fatal_output_matches": ["VK_ERROR_DEVICE_LOST"],
        },
        running=False,
    ) == "error"


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
            "decision": "accept",
        }
    )

    assert table.widget.columnCount() == len(RunsTable.COLUMNS)
    assert table.widget.item(0, table.FPS_COLUMN).text() == "150.00 (ref)"
    assert table.widget.item(1, table.FPS_COLUMN).text() == "160.00 (+6.67%)"
    assert table.widget.item(1, table.POWER_COLUMN).text() == "270.00 (-10.00%)"
    assert table.widget.item(1, table.FPSW_COLUMN).text() == "0.75 (+50.00%)"
    assert table.widget.item(1, table.POWER_COLUMN).toolTip().startswith(
        "Power W -10.00% vs base"
    )


def test_ui_profile_delete_command_uses_privileged_launcher(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(commands.os, "getuid", lambda: 1000)
    monkeypatch.setattr(commands.os, "getgid", lambda: 1000)
    monkeypatch.setenv("USER", "desktop-user")

    def fake_which(name: str) -> str | None:
        return {
            "pkexec": "/usr/bin/pkexec",
            "env": "/usr/bin/env",
        }.get(name)

    monkeypatch.setattr(commands.shutil, "which", fake_which)

    command = commands.delete_profiles_command(["/home/user/profile.json"])

    assert command[:2] == ["/usr/bin/pkexec", "/usr/bin/env"]
    assert "--delete-auto-uv-profiles" in command
    assert "/home/user/profile.json" in command


def test_ui_runtime_command_can_prefer_afterburner_curve(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)

    command = commands.runtime_profile_command(
        "daemonize",
        prefer_afterburner_curve=True,
        silent_fan_curve=True,
    )

    assert "--daemonize" in command
    assert "--prefer-afterburner-curve" in command
    assert "--silent-fan-curve" in command
    assert "--auto-uv-profile" not in command


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
    assert _best_final_choice_candidate_id(sorted_candidates, "performance") == (
        "fast"
    )
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
    assert "highest-FPS" in performance_text


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
            efficiency_fps_per_w=0.50,
        ),
    )

    assert summary["base_avg_fps"] == 150.0
    assert summary["base_efficiency_fps_per_w"] == 0.50


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

    assert table.horizontalHeader().sortIndicatorSection() == FINAL_CHOICE_FPS_SORT_COLUMN
    assert table.horizontalHeader().sortIndicatorOrder() == QtCore.Qt.DescendingOrder
    assert row_ids() == ["fast", "efficient"]

    table.horizontalHeader().sectionClicked.emit(FINAL_CHOICE_FPSW_SORT_COLUMN)
    assert table.horizontalHeader().sortIndicatorSection() == FINAL_CHOICE_FPSW_SORT_COLUMN
    assert table.horizontalHeader().sortIndicatorOrder() == QtCore.Qt.DescendingOrder
    assert row_ids() == ["efficient", "fast"]

    table.horizontalHeader().sectionClicked.emit(FINAL_CHOICE_FPSW_SORT_COLUMN)
    assert table.horizontalHeader().sortIndicatorOrder() == QtCore.Qt.AscendingOrder
    assert row_ids() == ["fast", "efficient"]


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
            "avg_power_w": 246.0,
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
    regressed_fpsw = item_for("regressed", FINAL_CHOICE_FPSW_SORT_COLUMN)
    regressed_fps = item_for("regressed", FINAL_CHOICE_FPS_SORT_COLUMN)

    assert efficient_fpsw.text() == "0.75 (+50.00%)"
    assert efficient_fps.text() == "160.00 (+6.67%)"
    assert regressed_fpsw.text() == "0.45 (-10.00%)"
    assert regressed_fps.text() == "140.00 (-6.67%)"
    assert efficient_fpsw.foreground().color().name() == "#55d27a"
    assert efficient_fps.foreground().color().name() == "#55d27a"
    assert regressed_fpsw.foreground().color().name() == "#ff6b6b"
    assert regressed_fps.foreground().color().name() == "#ff6b6b"


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


def test_ui_profile_verify_command_uses_selected_auto_uv_profile(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)

    command = commands.profile_verify_command(
        profile_selector="profile-a",
        duration_s=600,
        stop_request_path="/tmp/verify.stop",
    )

    assert "--stability-test" in command
    assert command[command.index("--stability-seconds") + 1] == "600"
    assert command[command.index("--auto-uv-profile") + 1] == "profile-a"
    assert command[command.index("--stability-stop-request-file") + 1] == (
        "/tmp/verify.stop"
    )
    assert "--prefer-afterburner-curve" not in command


def test_ui_profile_verify_command_keeps_q2rtx_cuda_default_when_both_checked(
    monkeypatch,
) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)

    command = commands.profile_verify_command(
        profile_selector="profile-a",
        duration_s=600,
        q2rtx_enabled=True,
        cuda_enabled=True,
    )

    assert "--stability-workload" not in command


def test_ui_profile_verify_command_can_use_afterburner_profile(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)

    command = commands.profile_verify_command(
        profile_selector="ignored",
        duration_s=900,
        prefer_afterburner_curve=True,
    )

    assert "--stability-test" in command
    assert command[command.index("--stability-seconds") + 1] == "900"
    assert "--prefer-afterburner-curve" in command
    assert "--auto-uv-profile" not in command


def test_ui_profile_verify_command_can_run_q2rtx_only(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)

    command = commands.profile_verify_command(
        profile_selector="profile-a",
        duration_s=600,
        q2rtx_enabled=True,
        cuda_enabled=False,
    )

    assert command[command.index("--stability-workload") + 1] == "q2rtx"


def test_ui_profile_verify_command_can_run_cuda_only(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)

    command = commands.profile_verify_command(
        profile_selector="profile-a",
        duration_s=600,
        q2rtx_enabled=False,
        cuda_enabled=True,
    )

    assert command[command.index("--stability-workload") + 1] == "cuda"


def test_ui_profile_verify_command_rejects_empty_workload(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)

    with pytest.raises(ValueError):
        commands.profile_verify_command(
            profile_selector="profile-a",
            duration_s=600,
            q2rtx_enabled=False,
            cuda_enabled=False,
        )


def test_oc_budget_display_clamps_used_value_to_limit() -> None:
    assert _budget_display_values(4.14, 4.0) == (4.0, 4.0)
    assert _budget_display_values(2.25, 4.0) == (2.25, 4.0)
    assert _budget_display_values(1.0, 0.0) == (1.0, 0.0)


def test_clock_recovery_display_uses_drop_gap_percentages() -> None:
    payload = {
        "overclock_budget_used_of_clock_drop_pct": 130.0,
        "overclock_budget_limit_of_clock_drop_pct": 150.0,
    }

    used, limit = _budget_recovery_display_values(payload, used=13.0, limit=15.0)

    assert (used, limit) == (130.0, 150.0)
    assert _budget_recovery_text(used, limit) == "130% / 150%"


def test_auto_uv_table_clock_recovery_bar_stays_light_green() -> None:
    assert _budget_fill_color() == "#55d27a"
    assert _budget_fill_color(100.0) == "#55d27a"
    assert _budget_fill_color(105.0) == "#55d27a"
    assert _budget_fill_color(110.0) == "#55d27a"
    assert _budget_fill_color(110.1) == "#55d27a"
    assert _budget_fill_color(125.0) == "#55d27a"


def test_performance_bias_maps_physical_slider_to_clock_recovery() -> None:
    assert MAX_OVERCLOCK_BUDGET_PCT == 150.0
    assert YOLO_MAX_OVERCLOCK_BUDGET_PCT == 175.0
    assert _performance_bias_clock_recovery_pct(0) == 0.0
    assert _performance_bias_clock_recovery_pct(25) == 50.0
    assert _performance_bias_clock_recovery_pct(50) == 100.0
    assert _performance_bias_clock_recovery_pct(75) == 125.0
    assert _performance_bias_clock_recovery_pct(100) == 150.0
    assert _performance_bias_slider_position(0.0) == 0
    assert _performance_bias_slider_position(100.0) == 50
    assert _performance_bias_slider_position(150.0) == 100
    assert (
        _performance_bias_clock_recovery_pct(
            75,
            max_pct=YOLO_MAX_OVERCLOCK_BUDGET_PCT,
        )
        == 137.5
    )
    assert (
        _performance_bias_clock_recovery_pct(
            100,
            max_pct=YOLO_MAX_OVERCLOCK_BUDGET_PCT,
        )
        == 175.0
    )
    assert _performance_bias_slider_position(
        175.0,
        max_pct=YOLO_MAX_OVERCLOCK_BUDGET_PCT,
    ) == 100
    assert "might hang your system" in PERFORMANCE_BIAS_TOOLTIP_TEXT


def test_click_jump_slider_maps_click_position_to_exact_value() -> None:
    assert (
        _slider_value_from_click_position(
            position_px=0,
            width_px=101,
            minimum=0,
            maximum=100,
        )
        == 0
    )
    assert (
        _slider_value_from_click_position(
            position_px=50,
            width_px=101,
            minimum=0,
            maximum=100,
        )
        == 50
    )
    assert (
        _slider_value_from_click_position(
            position_px=100,
            width_px=101,
            minimum=0,
            maximum=100,
        )
        == 100
    )
    assert (
        _slider_value_from_click_position(
            position_px=25,
            width_px=101,
            minimum=0,
            maximum=100,
            inverted=True,
        )
        == 75
    )


def test_auto_uv_bias_defaults_mode_threshold_and_gpu_table_default() -> None:
    assert DEFAULT_AUTO_UV_PERFORMANCE_BIAS_PCT == 100.0
    assert DEFAULT_AUTO_UV_MAX_DROP_PCT == 15.0
    assert GENERIC_AUTO_UV_MAX_DROP_PCT == 15.0
    assert AUTO_UV_DROP_REFERENCE_VOLTAGE_MV == 1000
    assert DEFAULT_AUTO_UV_MAX_CLOCK_DROP_PCT == 10.0
    assert _auto_uv_mode_for_performance_bias(99.9) == "efficiency"
    assert _auto_uv_mode_for_performance_bias(100.0) == "performance"
    assert _auto_uv_mode_for_performance_bias(150.0) == "performance"


def test_auto_uv_voltage_drop_default_uses_detected_gpu_table_floor() -> None:
    preview = _auto_uv_voltage_drop_default(gpu_name="NVIDIA GeForce RTX 5080")

    assert preview.preset_matched is True
    assert preview.gpu_family == "RTX 5080"
    assert preview.floor_voltage_mv == 850
    assert preview.value_pct == pytest.approx(15.0)
    assert (
        _auto_voltage_drop_note_text(preview)
        == "Max voltage drop auto-filled for NVIDIA GeForce RTX 5080"
    )


def test_auto_uv_voltage_drop_default_falls_back_to_generic_when_unmatched() -> None:
    preview = _auto_uv_voltage_drop_default(gpu_name="NVIDIA GeForce RTX 3080")

    assert preview.preset_matched is False
    assert preview.value_pct == pytest.approx(15.0)
    assert preview.floor_voltage_mv is None
    assert (
        _auto_voltage_drop_note_text(preview)
        == "Using generic max voltage drop for NVIDIA GeForce RTX 3080"
    )


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


def test_busy_progress_bounces_with_edge_hold() -> None:
    assert [_bounce_position_for_frame(frame, steps=4) for frame in range(12)] == [
        0.0,
        0.25,
        0.5,
        0.75,
        1.0,
        1.0,
        0.75,
        0.5,
        0.25,
        0.0,
        0.0,
        0.25,
    ]


def test_busy_progress_widget_is_reused_while_scale_is_unknown() -> None:
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
    first_widget = table.widget.cellWidget(0, table.STATUS_COLUMN)
    assert first_widget is not None
    first_widget._frame = 9

    table.update_probe_progress(payload)
    second_widget = table.widget.cellWidget(0, table.STATUS_COLUMN)

    assert second_widget is first_widget
    assert second_widget._frame == 9


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
    assert [
        index.row() for index in table.widget.selectionModel().selectedRows()
    ] == [1]

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


def test_performance_bias_slider_has_breathing_room_and_autofill_note() -> None:
    assert "QGroupBox#performanceBiasGroup" in STYLESHEET
    assert "QSlider#performanceBiasSlider" in STYLESHEET
    assert "margin: 8px 0 10px 0;" in STYLESHEET
    assert "QLabel#autoVoltageDropNote" in STYLESHEET
    slider_style = performance_bias_slider_stylesheet(MAX_OVERCLOCK_BUDGET_PCT)
    assert "qlineargradient" in slider_style
    source = Path("ui/dialogs/scan_tuning.py").read_text(encoding="utf-8")
    assert "auto_uv_voltage_drop_default" in source
    assert "auto-filled for" in source
    assert "efficiency floor" not in source
    assert '"auto_uv_max_drop_pct"' in source
    assert '"penguin-burner-green.png"' in source
    assert '"penguin-burner.png"' in source


def test_advanced_tuning_group_has_breathing_room() -> None:
    assert "QGroupBox#advancedTuningGroup" in STYLESHEET
    assert "margin-top: 12px;" in STYLESHEET


def test_about_dialog_preserves_project_links() -> None:
    assert "https://github.com/sponsors/jpietek" in ABOUT_LINKS_HTML
    assert "https://github.com/jpietek/PenguinBurner/issues" in ABOUT_LINKS_HTML
    assert "Having issues with PenguinBurner?" in ABOUT_LINKS_HTML


def test_top_status_text_does_not_truncate_live_temperature() -> None:
    text = (
        "Auto-UV phase=candidate-live overclocking-budget=2.91/4.00% "
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
