from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_uv.domain.user_options import AUTO_UV_DEFAULTS
from auto_uv.run.scan_runtime_settings import (
    short_probe_base_duration_s as _short_probe_base_duration_s,
)
from auto_uv.session.scan_events import probe_decision_label as _probe_decision_label
from auto_uv.session.scan_events import probe_failure_label as _probe_failure_label
from auto_uv.session.scan_events import top_status_text as _top_status_text
from auto_uv.session.scan_runs import (
    clamped_elapsed_s as _scan_controls_clamped_elapsed_s,
)
from auto_uv.session.scan_runs import (
    format_duration_compact as _format_duration_compact,
)
from auto_uv.session.scan_runs import (
    is_active_decision as _is_active_decision,
)
from auto_uv.session.scan_runs import (
    progress_label as _progress_label,
)
from auto_uv.session.scan_runs import (
    progress_time_text as _progress_time_text,
)
from auto_uv.session.scan_runs import (
    row_state as _row_state,
)
from ui.assets import application_version as _application_version
from ui.components.curve_plot import (
    _axis_value_badge_text,
    _nearest_curve_point,
    _probe_marker_values,
)
from ui.components.table_sizing import set_header_fit_column_widths
from ui.dialogs.about import ABOUT_LINKS_HTML
from ui.dialogs.final_choice import (
    FINAL_CHOICE_FPS_SORT_COLUMN,
    FINAL_CHOICE_FPSW_SORT_COLUMN,
)
from ui.dialogs.final_choice import (
    best_final_choice_candidate_id as _best_final_choice_candidate_id,
)
from ui.dialogs.final_choice import (
    create_final_choice_table as _create_final_choice_table,
)
from ui.dialogs.final_choice import (
    final_choice_intro_text as _final_choice_intro_text,
)
from ui.dialogs.final_choice import (
    final_choice_shows_oc_column as _final_choice_shows_oc_column,
)
from ui.dialogs.final_choice import (
    final_choice_sort_column_for_mode as _final_choice_sort_column_for_mode,
)
from ui.dialogs.final_choice import (
    sort_candidates_for_final_choice as _sort_candidates_for_final_choice,
)
from ui.features.tuning.tuning import (
    AUTO_UV_PRESET_ADAPTIVE,
    AUTO_UV_PRESET_BALANCED,
    AUTO_UV_PRESET_EFFICIENCY,
    AUTO_UV_PRESET_PERFORMANCE,
    DEFAULT_AUTO_UV_BALANCED_TAIL_RISE_BINS,
    DEFAULT_AUTO_UV_PERFORMANCE_TAIL_RISE_BINS,
    DEFAULT_AUTO_UV_PRESET,
    DEFAULT_AUTO_UV_TAIL_RISE_BINS,
    GPU_UNDERVOLTING_PURPOSE_TEXT,
)
from ui.features.tuning.tuning import (
    auto_uv_performance_preset_label as _auto_uv_performance_preset_label,
)
from ui.features.tuning.tuning import (
    auto_uv_performance_preset_tooltip as _auto_uv_performance_preset_tooltip,
)
from ui.features.tuning.tuning import (
    auto_uv_preset as _auto_uv_preset,
)
from ui.features.tuning.tuning import (
    auto_uv_target_default as _auto_uv_target_default,
)
from ui.main import parse_gui_args as _parse_gui_args
from ui.styles import STYLESHEET


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

    assert efficient_fpsw.text() == "0.7500 (+50.00%)"
    assert efficient_fps.text() == "160.00 (+6.67%)"
    assert efficient_power.text() == "200.00 (-20.00%)"
    assert regressed_fpsw.text() == "0.4500 (-10.00%)"
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


def test_auto_uv_preset_defaults_and_gpu_table_default() -> None:
    # One click, three profiles: the adaptive all-tiers scan is the default.
    assert DEFAULT_AUTO_UV_PRESET == "adaptive"
    assert DEFAULT_AUTO_UV_TAIL_RISE_BINS == 2
    assert DEFAULT_AUTO_UV_BALANCED_TAIL_RISE_BINS == 2
    assert DEFAULT_AUTO_UV_PERFORMANCE_TAIL_RISE_BINS == 2
    efficiency = _auto_uv_preset(AUTO_UV_PRESET_EFFICIENCY)
    balanced = _auto_uv_preset(AUTO_UV_PRESET_BALANCED)
    performance = _auto_uv_preset(AUTO_UV_PRESET_PERFORMANCE)
    assert (efficiency.auto_uv_mode, efficiency.tail_rise_bins) == (
        "efficiency",
        2,
    )
    assert (balanced.auto_uv_mode, balanced.tail_rise_bins) == (
        "balanced",
        2,
    )
    assert (performance.auto_uv_mode, performance.tail_rise_bins) == (
        "performance",
        2,
    )


def test_auto_uv_performance_preset_describes_auto_oc() -> None:
    assert _auto_uv_performance_preset_label() == "Performance"
    tooltip = _auto_uv_performance_preset_tooltip()
    assert "2-bin tail curve" in tooltip
    assert "Performance Auto-OC ladder" in tooltip


def test_auto_uv_target_default_uses_gpu_table_target() -> None:
    target = _auto_uv_target_default(
        gpu_name="NVIDIA GeForce RTX 4090",
    )

    assert target.preset_matched is True
    assert target.gpu_family == "RTX 4090"
    assert target.voltage_mv == 925
    assert target.clock_mhz == 2645






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






def test_overlay_panel_has_no_adaptive_target_fps_control(tmp_path, qtbot) -> None:
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
    qtbot.addWidget(panel.widget)

    # The adaptive target FPS moved to the Steam tab's per-game settings.
    assert panel.widget.layout().contentsMargins().top() >= 16
    assert (
        panel.widget.findChild(QtWidgets.QDoubleSpinBox, "overlayTargetFpsSpin")
        is None
    )


def test_overlay_panel_saves_manual_scale(tmp_path, qtbot) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app

    from overlay.config import (
        load_overlay_config,
    )
    from overlay.style import (
        OVERLAY_SCALE_OPTIONS,
    )
    from ui.components.overlay_config import OverlayConfigPanel

    config_path = tmp_path / "overlay.toml"
    panel = OverlayConfigPanel(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        config_path=config_path,
    )
    qtbot.addWidget(panel.widget)

    # The slider uses the existing adaptive scale steps, displayed as percentages.
    assert panel.scale_slider.value() == OVERLAY_SCALE_OPTIONS.index(1.0)
    assert panel.scale_value_label.text() == "100%"

    panel.scale_slider.setValue(OVERLAY_SCALE_OPTIONS.index(2.0))

    assert panel.scale_value_label.text() == "200%"
    assert load_overlay_config(config_path).scale == 2.0
    assert "scale = 2.0" in config_path.read_text(encoding="utf-8")

    panel.scale_slider.setValue(OVERLAY_SCALE_OPTIONS.index(0.75))

    assert load_overlay_config(config_path).scale == 0.75
    assert "scale = 0.75" in config_path.read_text(encoding="utf-8")


def test_overlay_panel_has_no_steam_launch_controls(tmp_path, qtbot) -> None:
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
    qtbot.addWidget(panel.widget)

    assert panel.widget.findChild(QtWidgets.QLabel, "overlaySteamLaunchHint") is None
    assert panel.widget.findChild(QtWidgets.QLineEdit, "overlaySteamLaunchLine") is None
    assert panel.widget.findChild(QtWidgets.QPushButton, "overlayCopyLaunchButton") is None






















def test_stopping_rows_remain_active_until_stop_is_finalized() -> None:
    assert _is_active_decision("running")
    assert _is_active_decision("stopping")
    assert not _is_active_decision("stopped")


def test_app_stylesheet_keeps_custom_scrollbars_inside_overlay() -> None:
    first_selector = STYLESHEET.split("{", 1)[0]
    scrollbar_selectors = [
        selector.strip()
        for rule in STYLESHEET.split("}")
        for selector in rule.split("{", 1)[0].split(",")
        if "QScrollBar" in selector
    ]

    assert all(
        selector.startswith("QWidget#overlayConfigPanel QScrollBar")
        for selector in scrollbar_selectors
    )
    assert "QWidget" not in first_selector




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
    assert "uv_limit_clock_target_range_for_gpu" in source
    assert "auto-filled for" not in source
    assert "efficiency floor" not in source
    assert "Max voltage drop" not in source
    assert "Base verification length" not in source
    assert '"auto_uv_short_seconds"' not in source
    assert "sync_voltage_floor_from_drop" not in source
    assert "sync_voltage_drop_from_floor" not in source
    assert "Try to maintain baseline clock" in source
    assert "AUTO_UV_PRESET_EFFICIENCY" in source
    assert "QStackedWidget" in source
    assert "preset_advanced_stack.setMinimumHeight" in source
    assert "buttonClicked.connect" in source
    assert "Voltage target" in source
    assert "Core clock target" in source
    assert "auto_uv_target_default" in source
    assert "Auto-OC table target" not in source
    assert "autoOcTargetPreview" not in source
    assert "auto_uv_performance_target_text" not in source
    assert 'f"target_{field}"' in source
    assert '"power_limit_override_w"' not in source
    assert '"auto_uv_power_limit_w"' in source
    assert "powerLimitSlider" in source
    assert '"auto_uv_min_voltage_mv"' not in source
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
    from ui.dialogs import scan_tuning

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
    from ui.dialogs import scan_tuning

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

    from ui.dialogs import scan_tuning

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
    enter_type = event_types.Enter
    leave_type = event_types.Leave
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

    from ui.dialogs import scan_tuning

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
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
        "auto_uv_target_default",
        lambda gpu_name=None, **_kwargs: SimpleNamespace(voltage_mv=None, clock_mhz=None),
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
        voltage_floor = dialog.findChild(QtWidgets.QSpinBox, "efficiencyVoltageSpin")
        assert power_slider is not None and power_spin is not None
        assert memory_spin is not None
        assert max_drop_spin is None
        assert performance_voltage is not None and performance_clock is not None
        assert voltage_floor is not None
        # The mobile fixed power limit grays the power control out entirely,
        # while the other scan controls stay usable.
        assert not power_slider.isEnabled()
        assert not power_spin.isEnabled()
        assert memory_spin.isEnabled()
        # An unknown GPU leaves the floor automatic. The loaded baseline probe
        # will turn this into a 10% drop from its actual starting voltage.
        assert voltage_floor.specialValueText() == "Auto"
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

    from ui.dialogs import scan_tuning

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    dialogs = []
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
    assert max_clock_drop_spin is None
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
    for index, expected_watts in enumerate((383, 405, 450)):
        assert stack.widget(index).findChild(
            QtWidgets.QDoubleSpinBox, "maxClockDropSpin"
        ) is None
        power_spin_for_page = page_control(index, "powerLimitSpin", QtWidgets.QSpinBox)
        assert power_spin_for_page.minimum() == 200
        assert power_spin_for_page.maximum() == 450
        assert power_spin_for_page.value() == expected_watts
    advanced_labels = {
        label.text() for label in advanced_group.findChildren(QtWidgets.QLabel)
    }
    assert "Max loaded clock drop" not in advanced_labels
    assert "Voltage target" in advanced_labels
    assert "Core clock target" in advanced_labels
    caution = dialog.findChild(QtWidgets.QLabel, "autoUvTargetCaution")
    assert caution is not None and caution.wordWrap()
    assert "optimized for most GPUs" in caution.text()
    assert "only if you understand" in caution.text()
    assert "Memory offset" in advanced_labels
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

    from ui.dialogs import scan_tuning

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
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

    from ui.dialogs import scan_tuning

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
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

    monkeypatch.setattr(
        scan_tuning, "auto_uv_voltage_floor_range_mv", lambda **_: (800, 1100)
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
    assert "auto_uv_efficiency_max_clock_drop_pct" not in options
    assert "auto_uv_balanced_max_clock_drop_pct" not in options
    assert "auto_uv_performance_max_clock_drop_pct" not in options
    assert options["auto_uv_efficiency_memory_offset_mhz"] == 0
    assert "auto_uv_max_clock_drop_pct" not in options
    assert "auto_uv_memory_offset_mhz" not in options
    assert "auto_uv_tail_rise_bins" not in options
    # Unedited table defaults (850/900/925 mV on the 5080) stay automatic:
    # sent as explicit targets they became hard Auto-OC bounds (issue #109).
    assert not any("_target_" in key for key in options)


def test_scan_tuning_memory_offset_is_mhz_with_mt_s_shown_and_doubled(
    monkeypatch,
) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    from ui.dialogs import scan_tuning

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
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

    # The live line is summarised for the window, temperature and fan kept.
    assert rendered.endswith("60 °C · fan 33%")
    assert rendered == (
        "Testing · 990 mV @ 2640 MHz · 0:15 · Q2RTX · 294 W · 2580 MHz · 60 °C · fan 33%"
    )




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


@pytest.mark.parametrize("gpu_name,hardware_ranges", [
    ("NVIDIA GeForce RTX 5080", True), ("Unknown GPU", True), ("Unknown GPU", False),
])
def test_tier_target_controls_validate_ranges_and_auto(monkeypatch, gpu_name, hardware_ranges):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    import ui.dialogs.scan_tuning as tuning

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(tuning, "DaemonGpuClient", lambda index: SimpleNamespace(
        capabilities=lambda: SimpleNamespace(identity=SimpleNamespace(name=gpu_name))))
    monkeypatch.setattr(tuning, "gpu_choices_with_fallback", lambda **_: (
        [SimpleNamespace(index=0, label=gpu_name, name=gpu_name)], 0))
    monkeypatch.setattr(tuning, "auto_uv_voltage_floor_range_mv", lambda **_: (
        (800, 1250) if hardware_ranges else None))
    monkeypatch.setattr(tuning, "memory_offset_mhz_range", lambda **_: (0, 2000))
    monkeypatch.setattr(tuning, "read_auto_uv_nvml_info", lambda *_, **__: SimpleNamespace(
        power_management_enabled=False, power_limit_set_supported=False,
        power_draw_w=None, graphics_clock_mhz=None, memory_clock_mhz=None,
        power_limit_w=None, power_limit_default_w=None,
        power_limit_min_w=None, power_limit_max_w=None,
        supported_memory_clocks_mhz=(),
        supported_graphics_clock_steps_mhz=(210, 3200) if hardware_ranges else (),
    ))
    expected = {"efficiency": (2380, 2800, 850), "balanced": (2800, 2950, 900),
                "performance": (2800, 3098, 925)}
    def accept(dialog):
        dialog.show()
        app.processEvents()
        assert dialog.findChild(QtWidgets.QDoubleSpinBox, "maxClockDropSpin") is None
        for tier, (low, high, voltage) in expected.items():
            clock = dialog.findChild(QtWidgets.QSpinBox, f"{tier}ClockSpin")
            volts = dialog.findChild(QtWidgets.QSpinBox, f"{tier}VoltageSpin")
            assert clock.suffix() == " MHz" and volts.suffix() == " mV"
            form = clock.parentWidget().layout()
            labels = []
            for row in range(form.rowCount()):
                item = form.itemAt(row, QtWidgets.QFormLayout.LabelRole)
                if item is not None:
                    labels.append(item.widget().findChild(QtWidgets.QLabel).text())
            assert labels == ["Voltage target", "Core clock target", "Memory offset", "Power limit"]
            if gpu_name.startswith("NVIDIA"):
                assert (clock.minimum(), clock.maximum()) == (low, high)
                assert volts.value() == voltage
                clock.setValue(low - 500)
                assert clock.value() == low
            else:
                for spin in (clock, volts):
                    assert spin.specialValueText() == "Auto"
                    assert spin.value() == spin.minimum()
                    assert spin.isEnabled() is hardware_ranges
        dialog.hide()
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(QtWidgets.QDialog, "exec", accept)
    options = tuning.select_scan_tuning(QtCore=QtCore, QtGui=QtGui,
                                       QtWidgets=QtWidgets, parent=None, gpu_index=0)
    assert options is not None
    if gpu_name.startswith("NVIDIA"):
        # The clock boxes were edited down to their minimum; the voltage boxes
        # still hold the table default, which is not an override. Balanced and
        # Performance minimums equal their table clocks, so those stay automatic.
        defaults = {"efficiency": 2800, "balanced": 2800, "performance": 2950}
        for tier, (low, _, _voltage) in expected.items():
            if low != defaults[tier]:
                assert options[f"auto_uv_{tier}_target_clock_mhz"] == low
            else:
                assert f"auto_uv_{tier}_target_clock_mhz" not in options
            assert f"auto_uv_{tier}_target_voltage_mv" not in options
    else:
        assert not any("target_" in key for key in options)
