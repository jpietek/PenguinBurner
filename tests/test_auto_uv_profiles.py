from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

import profiles.uv.profile_store as profile_store
import penguin_burner
import profiles.verification.metrics as profile_verification_metrics
import profiles.verification.rules as profile_verification_rules
import profiles.verification.runner as profile_verification_runner
from cli.runtime_profile_argument import (
    runtime_profile_selector_allows_unverified_from_argv as allow_unverified_from_argv,
)
import ui.features.curves.curve_profiles as curve_profiles
import ui.features.curves.fan_profiles as ui_app
from runtime.gpu_control.flattened_clock_ceiling import FlattenedClockCeilingController
from profiles.uv.profile_store import (
    archive_auto_uv_profile,
    bind_auto_uv_profile_gpu_identity,
    delete_auto_uv_profile_paths,
    delete_auto_uv_profiles,
    format_profile_table,
    mark_auto_uv_profile_verified,
    profile_display_name,
    profile_summary,
    read_auto_uv_profile_summaries,
    resolve_auto_uv_profile,
)
from common.cli_output import format_user_duration as _format_duration_for_user
from ui.components.fan_curve_editor import (
    fan_curve_editor_shortcut_legend_rows as _fan_curve_editor_shortcut_legend_rows,
)
from ui.components.vf_curve_editor import (
    vf_curve_editor_shortcut_legend_rows as _curve_editor_shortcut_legend_rows,
)
from ui.features.curves.curve_profiles import (
    load_cached_base_curve_points as _load_cached_base_curve_points,
    profile_base_curve_points as _profile_base_curve_points,
    save_cached_base_curve_points as _save_cached_base_curve_points,
)
from ui.dialogs.error_details import error_dialog_copy_text as _error_dialog_copy_text
from ui.dialogs.error_details import process_failure_details as _process_failure_details
from ui.dialogs.final_choice import candidate_number as _candidate_number
from ui.dialogs.final_choice import candidate_oc_text as _candidate_oc_text
from ui.dialogs.final_choice import candidate_status_text as _candidate_status_text
from ui.dialogs.final_choice import (
    final_choice_sort_values as _final_choice_sort_values,
)
from ui.features.curves.fan_profiles import (
    fan_curve_target_point_from_payload as _fan_curve_target_point_from_payload,
    fan_measurement_point as _fan_measurement_point,
    fan_measurement_points as _fan_measurement_points,
    fan_payload_has_silent_runtime_fields as _fan_payload_has_silent_runtime_fields,
    profile_fan_curve_points as _profile_fan_curve_points,
    profile_fan_curve_target_point as _profile_fan_curve_target_point,
    profile_fan_measurement_points as _profile_fan_measurement_points,
    profile_id_from_archive_path as _profile_id_from_archive_path,
    sorted_unique_fan_points as _sorted_unique_fan_points,
)
from ui.features.integrations.lact_export import (
    lact_export_output_path as _lact_export_output_path,
)
from ui.features.integrations.lact_export import (
    lact_gpu_id_from_config as _lact_gpu_id_from_config,
)
from ui.models import candidate_id_from_payload as _candidate_id_from_result
from ui.models import event_base_points as _event_base_points
from ui.models import stage_title as _stage_title
from ui.models import status_value as _status_value
from ui.models import top_status_text as _top_status_text
from ui.features.profiles.profiles import (
    delete_confirmation_text as _profile_delete_confirmation_text,
)
from ui.features.profiles.profiles import (
    adaptive_profile_tier_labels as _adaptive_profile_tier_labels,
)
from ui.features.profiles.profiles import (
    profile_info_from_command_text as _profile_info_from_command_text,
)
from ui.features.profiles.profiles import (
    profile_delete_autostart_action as _profile_delete_autostart_action,
)
from ui.features.profiles.profiles import (
    profile_verify_selector as _profile_verify_selector,
)
from ui.features.profiles.profiles import runner_status_text as _runner_status_text
from ui.features.profiles.profiles import (
    selected_profile_ids_include_selector as _selected_profile_ids_include_selector,
)
from ui.features.tuning.verify import elapsed_from_line as _verify_elapsed_from_line
from ui.features.tuning.verify import progress_percent as _verify_progress_percent
from ui.features.tuning.gpu_selection import GpuChoice
from ui.components.profile_list import (
    PROFILE_SORTABLE_COLUMNS,
    ProfileList,
    _format_number,
    _metric_delta_percent,
    _profile_base_metric,
    _profile_metric_tooltip,
    _profile_sort_values,
    _profile_source_label,
    _profile_tier_label,
    _promote_preferred_profile,
    _resolved_tier_winner_ids,
    _should_preserve_selection,
    _should_preserve_single_selection_toggle,
    _sort_value_less,
)


def test_profile_display_name_uses_clock_then_voltage() -> None:
    profile = {
        "profile_id": "20260427-120000-000000-875mv-2610mhz",
        "candidate_voltage_mv": 875,
        "lock_clock_mhz": 2610,
    }

    assert profile_display_name(profile) == "2610 MHz 875 mV"


def test_profile_table_shows_copyable_id_and_keeps_date_separate_from_name() -> None:
    profile = {
        "profile_id": "20260427-120000-000000-875mv-2610mhz",
        "profile_created_at": "2026-04-27T12:00:00+02:00",
        "profile_tier": "Balanced",
        "candidate_voltage_mv": 875,
        "lock_clock_mhz": 2610,
        "memory_offset_mhz": 500,
        "avg_core_clock_mhz": 2605.25,
        "efficiency_fps_per_w": 0.81234,
        "profile_source": "profile-store",
        "gpu_identity": {
            "name": "RTX 5090",
            "uuid": "GPU-A",
            "pci_bus_id": "00000000:01:00.0",
        },
    }

    rendered = format_profile_table([profile])

    assert "2026-04-27 12:00:00" in rendered
    assert "20260427-120000-000000-875mv-2610mhz" in rendered
    assert "Balanced" in rendered
    assert "RTX 5090 (01:00.0)" in rendered
    assert "2610 MHz 875 mV" in rendered
    # 500 MT/s transfer-rate offset -> 250 MHz realized memory clock.
    assert "+250 MHz" in rendered


def test_curve_editor_shortcut_legend_mentions_core_actions() -> None:
    rows = dict(_curve_editor_shortcut_legend_rows())

    assert rows["Click"] == "select bin"
    assert rows["Ctrl+Click"] == "new point"
    assert rows["Shift+Right"] == "select range"
    assert rows["Ctrl+L"] == "flatten here"
    assert rows["Ctrl+Z / Ctrl+Y"] == "undo / redo"


def test_fan_curve_editor_shortcut_legend_excludes_vf_only_actions() -> None:
    rows = dict(_fan_curve_editor_shortcut_legend_rows())

    assert rows["Click"] == "select dot"
    assert rows["Ctrl+Click"] == "new point"
    assert rows["Drag"] == "move dot"
    assert rows["Up/Down"] == "fan speed"
    assert rows["Left/Right"] == "temperature"
    assert rows["Tab / Shift+Tab"] == "next / previous"
    assert rows["Ctrl+Z / Ctrl+Y"] == "undo / redo"
    assert "Ctrl+L" not in rows
    assert "Shift+Right" not in rows


def test_profile_summary_keeps_base_metrics_for_profile_table_delta() -> None:
    summary = profile_summary(
        {
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2610,
            "memory_offset_mhz": 750,
            "power_limit_w": 360,
            "avg_core_clock_mhz": 2605.0,
            "avg_power_w": 240.0,
            "efficiency_fps_per_w": 0.70,
            "base_candidate_voltage_mv": 1000,
            "base_lock_clock_mhz": 2700,
            "base_avg_core_clock_mhz": 2650.0,
            "base_avg_fps": 150.0,
            "base_avg_power_w": 300.0,
            "base_efficiency_fps_per_w": 0.50,
            "final_verified": True,
        }
    )

    assert summary["base_candidate_voltage_mv"] == 1000
    assert summary["base_lock_clock_mhz"] == 2700
    assert summary["memory_offset_mhz"] == 750
    assert summary["power_limit_w"] == 360
    assert summary["base_avg_core_clock_mhz"] == 2650.0
    assert summary["base_avg_fps"] == 150.0
    assert summary["base_avg_power_w"] == 300.0
    assert summary["base_efficiency_fps_per_w"] == 0.50


def test_profile_metric_tooltip_identifies_its_scan_baseline() -> None:
    assert _metric_delta_percent(0.75, 0.50) == 50.0
    assert _profile_metric_tooltip(240.0, 300.0, label="Power W") == (
        "Power W: 240.00\n"
        "-20.00% vs this scan's baseline: 300.00.\n"
        "Baselines differ between scans; these percentages do not compare profiles."
    )
    assert "+4.57% vs this scan's baseline: 64.23" in _profile_metric_tooltip(
        67.161, 64.228, label="FPS"
    )
    assert "baseline: 0.2043" in _profile_metric_tooltip(
        0.242340, 0.204326, label="FPS/W", precision=4
    )
    for current, baseline in ((None, 300), (240, None), (240, 0)):
        assert _profile_metric_tooltip(current, baseline, label="Power W") == ""


def test_profile_table_headers_and_sorting_scope() -> None:
    assert ProfileList.COLUMNS[2] == "GPU"
    assert ProfileList.COLUMNS[3] == "mV"
    assert ProfileList.COLUMNS[4] == "Target MHz"
    assert ProfileList.COLUMNS[5] == "Effective MHz"
    assert ProfileList.COLUMNS[6] == "FPS/W"
    assert ProfileList.COLUMNS[8] == "Power W"
    assert ProfileList.COLUMNS[9] == "Mem"
    assert ProfileList.COLUMNS[10] == "Tier"
    assert "Autostart" not in ProfileList.COLUMNS
    assert "Voltage vs base" not in ProfileList.COLUMNS
    assert "FPS/W vs base" not in ProfileList.COLUMNS
    assert "Power vs base" not in ProfileList.COLUMNS
    assert PROFILE_SORTABLE_COLUMNS == frozenset({0, 2, 3, 4, 5, 6, 7, 8})


def test_existing_profile_uses_matching_latest_long_verification_clock(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    profile_path = archive_auto_uv_profile(
        {
            "candidate_id": "925mv-2980mhz",
            "candidate_voltage_mv": 925,
            "lock_clock_mhz": 2980,
            "avg_core_clock_mhz": 2830.0,
            "avg_fps": 63.456,
            "avg_power_w": 299.53,
            "efficiency_fps_per_w": 0.21185,
            "base_avg_core_clock_mhz": 2743.6,
            "base_avg_fps": 64.228,
            "base_avg_power_w": 300.0,
            "base_efficiency_fps_per_w": 0.21409,
            "final_verified": True,
            "points": [
                {
                    "index": 0,
                    "voltage_mv": 925,
                    "base_mhz": 2475,
                    "target_mhz": 2980,
                    "new_offset_mhz": 505,
                }
            ],
        }
    )
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    result_dir = tmp_path / "uv-result"
    result_dir.mkdir(parents=True)
    (result_dir / "auto-uv-latest-verified.json").write_text(
        json.dumps(
            {
                "candidate_id": "925mv-2980mhz",
                "candidate_voltage_mv": 925,
                "lock_clock_mhz": 2980,
                "avg_core_clock_mhz": 2888.06,
                "avg_fps": 67.161,
                "avg_power_w": 317.098,
                "efficiency_fps_per_w": 0.211799,
                "verified_at": profile["profile_created_at"],
            }
        ),
        encoding="utf-8",
    )

    loaded = profile_store.read_auto_uv_profile_summaries()

    assert loaded[0]["avg_core_clock_mhz"] == 2888.06
    assert loaded[0]["avg_fps"] == 67.161
    assert loaded[0]["avg_power_w"] == 317.098
    assert loaded[0]["efficiency_fps_per_w"] == 0.211799
    assert loaded[0]["base_avg_core_clock_mhz"] == 2743.6
    assert loaded[0]["base_avg_fps"] == 64.228
    assert loaded[0]["base_avg_power_w"] == 300.0
    assert loaded[0]["base_efficiency_fps_per_w"] == 0.21409
    assert json.loads(profile_path.read_text(encoding="utf-8"))[
        "avg_core_clock_mhz"
    ] == 2830.0


def test_post_fix_profile_keeps_its_own_long_verification_metrics(
    tmp_path,
    monkeypatch,
) -> None:
    """The repair must not touch profiles that embed long metrics at write time.

    A post-fix profile's final_q2rtx_avg_core_clock_mhz is the Q2RTX-window
    average; the repair file only carries the blended probe average and would
    silently degrade it."""
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    profile_path = archive_auto_uv_profile(
        {
            "candidate_id": "925mv-2980mhz",
            "candidate_voltage_mv": 925,
            "lock_clock_mhz": 2980,
            "avg_core_clock_mhz": 2888.06,
            "final_q2rtx_avg_core_clock_mhz": 2910.0,
            "final_verification_metrics": True,
            "final_verified": True,
            "points": [
                {
                    "index": 0,
                    "voltage_mv": 925,
                    "base_mhz": 2475,
                    "target_mhz": 2980,
                    "new_offset_mhz": 505,
                }
            ],
        }
    )
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    result_dir = tmp_path / "uv-result"
    result_dir.mkdir(parents=True)
    (result_dir / "auto-uv-latest-verified.json").write_text(
        json.dumps(
            {
                "candidate_id": "925mv-2980mhz",
                "candidate_voltage_mv": 925,
                "lock_clock_mhz": 2980,
                "avg_core_clock_mhz": 2860.0,
                "verified_at": profile["profile_created_at"],
            }
        ),
        encoding="utf-8",
    )

    loaded = profile_store.read_auto_uv_profiles()

    assert loaded[0]["final_q2rtx_avg_core_clock_mhz"] == 2910.0
    assert loaded[0]["avg_core_clock_mhz"] == 2888.06


def test_profile_non_sort_columns_have_no_sort_keys() -> None:
    sort_values = _profile_sort_values(
        {
            "profile_created_at": "2026-04-27T12:00:00+02:00",
            "display_name": "2610 MHz 875 mV",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2610,
            "avg_core_clock_mhz": 2605.25,
            "efficiency_fps_per_w": 0.80,
            "base_efficiency_fps_per_w": 0.50,
            "avg_fps": 160.0,
            "base_avg_fps": 150.0,
            "avg_power_w": 200.0,
            "base_avg_power_w": 250.0,
            "profile_source": "auto-uv-final",
        }
    )

    assert sort_values[1] == ""
    assert sort_values[2] == "unassigned (legacy)"
    assert sort_values[5] == pytest.approx(2605.25)
    assert sort_values[6] == pytest.approx(0.80)
    assert sort_values[7] == pytest.approx(160.0)
    assert sort_values[8] == pytest.approx(200.0)
    assert sort_values[9] == ""
    assert sort_values[10] == ""
    assert sort_values[11] == ""


def test_profile_table_keeps_plain_metrics_and_regular_font_for_highlight() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    profile_list = ProfileList(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    header = profile_list.table.horizontalHeader()
    assert not header.highlightSections()
    assert not header.font().bold()
    profile_list.set_profiles(
        [
            {
                "profile_id": "profile-a",
                "candidate_id": "875mv-2610mhz",
                "profile_created_at": "2026-04-27T12:00:00+02:00",
                "candidate_voltage_mv": 875,
                "base_candidate_voltage_mv": 1000,
                "lock_clock_mhz": 2610,
                "avg_core_clock_mhz": 2605.25,
                "base_avg_core_clock_mhz": 2650.0,
                "efficiency_fps_per_w": 0.80,
                "base_efficiency_fps_per_w": 0.50,
                "avg_fps": 160.0,
                "base_avg_fps": 150.0,
                "avg_power_w": 200.0,
                "base_avg_power_w": 250.0,
                "memory_offset_mhz": 1000,
            }
        ],
        preferred_candidate_id="875mv-2610mhz",
        select_preferred=True,
    )

    assert (
        profile_list.table.item(0, profile_list.VOLTAGE_COLUMN).text()
        == "875"
    )
    assert (
        profile_list.table.item(0, profile_list.EFFECTIVE_MHZ_COLUMN).text()
        == "2605.25"
    )
    assert profile_list.table.item(0, profile_list.FPSW_COLUMN).text() == (
        "0.8000"
    )
    assert profile_list.table.item(0, profile_list.FPS_COLUMN).text() == (
        "160.00"
    )
    assert profile_list.table.item(0, profile_list.POWER_COLUMN).text() == (
        "200.00"
    )
    # The stored offset is an NVML transfer-rate value (MT/s); the table shows
    # the realized memory clock in MHz (half of it) with a unit suffix.
    assert profile_list.table.item(0, profile_list.MEMORY_OFFSET_COLUMN).text() == (
        "+500 MHz"
    )
    for column in range(profile_list.table.columnCount()):
        item = profile_list.table.item(0, column)
        assert item is not None
        assert not item.font().bold()
        assert item.foreground().style() == QtCore.Qt.BrushStyle.NoBrush


def test_profile_table_keeps_scan_deltas_in_tooltips_and_sorts_absolute_values() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    profile_list = ProfileList(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    profile_list.set_profiles(
        [
            {
                "avg_core_clock_mhz": 2888.06,
                "base_avg_core_clock_mhz": 2743.59,
                "efficiency_fps_per_w": 0.211799,
                "base_efficiency_fps_per_w": 0.183359,
                "avg_fps": 67.161,
                "base_avg_fps": 64.228,
                "avg_power_w": 317.098,
                "base_avg_power_w": 350.286,
            }
        ]
    )

    expected = {
        profile_list.EFFECTIVE_MHZ_COLUMN: (
            "2888.06",
            "+5.27%",
        ),
        profile_list.FPSW_COLUMN: (
            "0.2118",
            "+15.51%",
        ),
        profile_list.FPS_COLUMN: (
            "67.16",
            "+4.57%",
        ),
    }
    for column, (text, delta) in expected.items():
        item = profile_list.table.item(0, column)
        assert item.text() == text
        assert delta in item.toolTip()
        assert "vs this scan's baseline:" in item.toolTip()
        assert item.foreground().style() == QtCore.Qt.BrushStyle.NoBrush
    power_item = profile_list.table.item(0, profile_list.POWER_COLUMN)
    assert power_item.text() == "317.10"
    assert "unavailable" in power_item.toolTip()
    profile_list.set_profiles(
        [
            {
                "profile_id": "efficiency",
                "gpu_identity": {"uuid": "GPU-A"},
                "avg_power_w": 247.895,
                "base_avg_power_w": 300.129,
                "avg_fps": 60.075,
                "base_avg_fps": 61.324,
            },
            {
                "profile_id": "balanced",
                "gpu_identity": {"uuid": "GPU-A"},
                "avg_power_w": 252.228,
                "base_avg_power_w": 354.089,
                "avg_fps": 61.933,
                "base_avg_fps": 63.018,
            },
        ],
        default_power_limits_w={"gpu-a": 360.0},
    )
    profile_list.table.sortItems(
        profile_list.POWER_COLUMN, QtCore.Qt.SortOrder.AscendingOrder
    )
    power_items = [profile_list.table.item(row, profile_list.POWER_COLUMN) for row in range(2)]
    assert [item.text() for item in power_items] == ["247.90 (-31.14%)", "252.23 (-29.94%)"]
    for item in power_items:
        assert "factory/default power limit (360.00 W)" in item.toolTip()
        assert "not measured stock power savings" in item.toolTip()
        assert "scan's baseline" not in item.toolTip()
        assert item.foreground().color().name() == "#55d27a"
    profile_list.table.sortItems(
        profile_list.FPS_COLUMN, QtCore.Qt.SortOrder.DescendingOrder
    )
    assert [
        profile_list.table.item(row, profile_list.FPS_COLUMN).text() for row in range(2)
    ] == ["61.93", "60.08"]


def test_profile_table_defaults_to_newest_date_first() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    profile_list = ProfileList(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    profile_list.set_profiles(
        [
            {
                "profile_id": "old",
                "display_name": "Old",
                "profile_created_at": "2026-04-25T12:00:00+02:00",
            },
            {
                "profile_id": "new",
                "display_name": "New",
                "profile_created_at": "2026-04-27T12:00:00+02:00",
            },
            {
                "profile_id": "middle",
                "display_name": "Middle",
                "profile_created_at": "2026-04-26T12:00:00+02:00",
            },
        ],
    )

    assert profile_list.table.horizontalHeader().sortIndicatorSection() == 0
    assert (
        profile_list.table.horizontalHeader().sortIndicatorOrder()
        == QtCore.Qt.DescendingOrder
    )
    assert [
        profile_list.table.item(row, profile_list.PROFILE_COLUMN).text()
        for row in range(profile_list.table.rowCount())
    ] == ["New", "Middle", "Old"]


def test_profile_list_syncs_silent_fan_toggle_from_runtime_state() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    profile_list = ProfileList(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)

    profile_list.set_profiles(
        [{"profile_id": "profile-a", "final_verified": True}],
        silent_fan_checked=True,
    )

    assert profile_list.silent_fan_enabled() is True


def test_profile_list_preserves_silent_fan_toggle_for_same_selection_refresh() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    profile_list = ProfileList(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    profiles = [{"profile_id": "profile-a", "final_verified": True}]
    profile_list.set_profiles(profiles)
    profile_list.select_profile("profile-a")
    profile_list.silent_fan_checkbox.setChecked(True)

    profile_list.set_profiles(profiles, silent_fan_checked=False)

    assert profile_list.selected_profile_id() == "profile-a"
    assert profile_list.silent_fan_enabled() is True


def test_profile_list_uses_one_apply_button() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    profile_list = ProfileList(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)

    profile_list.set_profiles(
        [{"profile_id": "profile-a", "final_verified": True}],
    )

    assert profile_list.daemonize_button.text() == "Apply"
    assert not hasattr(profile_list, "adaptive_button")
    assert not hasattr(profile_list, "adaptive_checkbox")
    assert not profile_list.daemonize_button.isEnabled()
    assert "Apply on startup" in profile_list.daemonize_button.toolTip()
    assert not hasattr(profile_list, "remove_button")
    restore_tooltip = profile_list.restore_defaults_button.toolTip()
    assert "core and memory offsets" in restore_tooltip
    assert "default power limit" in restore_tooltip

    profile_list.select_profile("profile-a")
    assert profile_list.daemonize_button.isEnabled()


def test_profile_list_single_gpu_keeps_clean_apply_and_legacy_profile() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    profile_list = ProfileList(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    profiles = [{"profile_id": "legacy", "final_verified": True}]
    profile_list.set_profiles(profiles)
    profile_list.configure_gpu_targets(
        profiles,
        [GpuChoice(0, "NVIDIA RTX 5090", "00000000:01:00.0", "GPU-A")],
    )
    profile_list.select_profile("legacy")

    assert not profile_list.target_gpu_combo.isHidden()
    assert not profile_list.target_gpu_label.isEnabled()
    assert not profile_list.target_gpu_combo.isEnabled()
    assert profile_list.target_gpu_combo.count() == 1
    assert profile_list.target_gpu_combo.currentData() == "GPU-A"
    assert profile_list.daemonize_button.text() == "Apply"
    assert profile_list.daemonize_button.isEnabled()
    assert profile_list.main_gpu_checkbox.isHidden()
    assert profile_list.target_gpu_index() == 0
    assert profile_list.table.item(0, profile_list.GPU_COLUMN).text() == (
        "Unassigned (legacy)"
    )


def test_profile_list_stale_configured_index_does_not_fake_multiple_gpus() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    profile_list = ProfileList(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    profiles = [{"profile_id": "legacy", "final_verified": True}]
    profile_list.set_profiles(profiles)
    profile_list.configure_gpu_targets(
        profiles,
        [
            GpuChoice(0, "NVIDIA RTX 5090", "00000000:01:00.0", "GPU-A"),
            GpuChoice(5, "NVIDIA GPU"),
        ],
        preferred_index=5,
    )

    assert profile_list.target_gpu_combo.count() == 1
    assert profile_list.target_gpu_combo.currentData() == "GPU-A"
    assert profile_list.target_gpu_index() == 0
    assert not profile_list.target_gpu_label.isEnabled()
    assert not profile_list.target_gpu_combo.isEnabled()


def test_profile_list_multiple_profile_gpus_requires_explicit_target() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    profile_list = ProfileList(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    profiles = [
        {
            "profile_id": "profile-a",
            "final_verified": True,
            "gpu_identity": {"name": "RTX 5090", "uuid": "GPU-A"},
        },
        {
            "profile_id": "profile-b",
            "final_verified": True,
            "gpu_identity": {"name": "RTX 5090", "uuid": "GPU-B"},
        },
    ]
    choices = [
        GpuChoice(0, "RTX 5090", "00000000:01:00.0", "GPU-A"),
        GpuChoice(1, "RTX 5090", "00000000:02:00.0", "GPU-B"),
    ]
    profile_list.set_profiles(profiles)
    profile_list.configure_gpu_targets(profiles, choices)
    profile_list.select_profile("profile-b")

    assert not profile_list.target_gpu_combo.isHidden()
    assert profile_list.target_gpu_label.isEnabled()
    assert profile_list.target_gpu_combo.isEnabled()
    assert profile_list.target_gpu_index() is None
    assert not profile_list.daemonize_button.isEnabled()
    assert not profile_list.boot_apply_checkbox.isEnabled()
    assert not profile_list.main_gpu_checkbox.isHidden()
    assert not profile_list.main_gpu_checkbox.isEnabled()

    profile_list.target_gpu_combo.setCurrentIndex(2)

    assert profile_list.target_gpu_uuid() == "GPU-B"
    assert profile_list.target_gpu_index() == 1
    assert profile_list.daemonize_button.text() == "Apply to GPU 1"
    assert profile_list.daemonize_button.isEnabled()
    assert profile_list.boot_apply_checkbox.isEnabled()
    assert not profile_list.main_gpu_checkbox.isEnabled()
    assert profile_list.table.isRowHidden(0)
    assert not profile_list.table.isRowHidden(1)

    profile_list.set_main_gpu_state(checked=True, has_boot_profile=True)

    assert profile_list.main_gpu_checkbox.isEnabled()
    assert profile_list.main_gpu_checkbox.isChecked()


def test_profile_list_multiple_hardware_gpus_one_profile_group_keeps_selector_visible() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    profile_list = ProfileList(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    profiles = [
        {
            "profile_id": "profile-b",
            "final_verified": True,
            "gpu_identity": {"name": "RTX 5090", "uuid": "GPU-B"},
        }
    ]
    choices = [
        GpuChoice(0, "RTX 4090", "00000000:01:00.0", "GPU-A"),
        GpuChoice(1, "RTX 5090", "00000000:02:00.0", "GPU-B"),
    ]
    profile_list.set_profiles(profiles)
    profile_list.configure_gpu_targets(profiles, choices)
    profile_list.select_profile("profile-b")

    assert not profile_list.target_gpu_combo.isHidden()
    assert profile_list.target_gpu_combo.isEnabled()
    assert profile_list.target_gpu_index() is None
    assert not profile_list.daemonize_button.isEnabled()

    profile_list.target_gpu_combo.setCurrentIndex(2)

    assert profile_list.target_gpu_index() == 1
    assert profile_list.daemonize_button.text() == "Apply to GPU 1"
    assert profile_list.daemonize_button.isEnabled()


def test_profile_list_target_filter_keeps_legacy_profiles_visible() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    profile_list = ProfileList(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    profiles = [
        {
            "profile_id": "profile-a",
            "final_verified": True,
            "gpu_identity": {"name": "RTX 4090", "uuid": "GPU-A"},
        },
        {
            "profile_id": "profile-b",
            "final_verified": True,
            "gpu_identity": {"name": "RTX 5090", "uuid": "GPU-B"},
        },
        {
            "profile_id": "legacy",
            "final_verified": True,
            # Profile summaries normalize missing legacy identity to an empty
            # mapping. It must not become the literal UUID string "None".
            "gpu_identity": {},
        },
    ]
    choices = [
        GpuChoice(0, "RTX 4090", "00000000:01:00.0", "GPU-A"),
        GpuChoice(1, "RTX 5090", "00000000:02:00.0", "GPU-B"),
    ]
    profile_list.set_profiles(profiles)
    profile_list.configure_gpu_targets(profiles, choices)

    profile_list.target_gpu_combo.setCurrentIndex(1)

    assert not profile_list.table.isRowHidden(0)
    assert profile_list.table.isRowHidden(1)
    assert not profile_list.table.isRowHidden(2)


def test_profile_filter_deselects_rows_hidden_by_gpu_switch() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    profile_list = ProfileList(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    profiles = [
        {
            "profile_id": "profile-a",
            "final_verified": True,
            "gpu_identity": {"uuid": "GPU-A"},
        },
        {
            "profile_id": "profile-b",
            "final_verified": True,
            "gpu_identity": {"uuid": "GPU-B"},
        },
    ]
    choices = [
        GpuChoice(0, "RTX A", uuid="GPU-A"),
        GpuChoice(1, "RTX B", uuid="GPU-B"),
    ]
    profile_list.set_profiles(profiles)
    profile_list.configure_gpu_targets(profiles, choices)
    profile_list.target_gpu_combo.setCurrentIndex(1)
    profile_list.select_profile("profile-a")
    assert profile_list.selected_profile_ids() == ["profile-a"]

    profile_list.target_gpu_combo.setCurrentIndex(2)

    assert profile_list.selected_profile_ids() == []


def test_profile_list_boot_toggle_defaults_off_and_survives_reloads() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    profile_list = ProfileList(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)

    # Opt-in boot persistence: unticked until the config (or a pre-existing
    # boot entry seeding the one-time default) says otherwise.
    assert not profile_list.persist_on_startup_enabled()

    # Profile reloads must never flip the user's choice — the config key is
    # the single source of truth (the old toggle was removed over resync
    # glitches; this pins the fix).
    profile_list.set_boot_apply_checked(True)
    profile_list.set_profiles(
        [{"profile_id": "profile-a", "final_verified": True}],
    )
    assert profile_list.persist_on_startup_enabled()
    profile_list.set_boot_apply_checked(False)
    profile_list.set_profiles([])
    assert not profile_list.persist_on_startup_enabled()


def test_adaptive_profile_tier_labels_need_distinct_verified_tiers() -> None:
    profiles = [
        {
            "profile_id": "eff-a",
            "final_verified": True,
            "profile_tier": "Efficiency",
        },
        {
            "profile_id": "eff-b",
            "final_verified": True,
            "profile_tier": "Efficiency",
        },
        {
            "profile_id": "perf-draft",
            "final_verified": False,
            "profile_tier": "Performance",
        },
        {
            "profile_id": "unverified-import",
            "final_verified": False,
            "profile_tier": "Performance",
        },
    ]

    assert _adaptive_profile_tier_labels(profiles, assignments={}) == ["Efficiency"]

    profiles.append(
        {
            "profile_id": "perf-a",
            "final_verified": True,
            "profile_tier": "Performance",
        }
    )

    assert _adaptive_profile_tier_labels(profiles, assignments={}) == [
        "Efficiency",
        "Performance",
    ]


def test_profile_list_tier_label_hides_none_assignment() -> None:
    assert (
        _profile_tier_label(
            {
                "profile_tier_disabled": True,
                "profile_tier": "",
                "generated_profile_tier": "Performance",
            }
        )
        == ""
    )


def test_profile_list_tier_column_is_unique_per_tier() -> None:
    # Two verified performance profiles must not both show "Performance" -- only
    # the one adaptive mode would resolve (the newest) keeps the tier label.
    perf_old = {
        "profile_id": "perf-old",
        "final_verified": True,
        "profile_tier": "Performance",
        "profile_created_at": "2026-01-01T00:00:00",
    }
    perf_new = {
        "profile_id": "perf-new",
        "final_verified": True,
        "profile_tier": "Performance",
        "profile_created_at": "2026-02-01T00:00:00",
    }
    eff = {
        "profile_id": "eff-a",
        "final_verified": True,
        "profile_tier": "Efficiency",
        "profile_created_at": "2026-01-15T00:00:00",
    }
    profiles = [perf_old, perf_new, eff]

    winner_ids = _resolved_tier_winner_ids(profiles)
    assert winner_ids["performance"] == "perf-new"
    assert winner_ids["efficiency"] == "eff-a"

    assert _profile_tier_label(perf_new, winner_ids) == "Performance"
    assert _profile_tier_label(perf_old, winner_ids) == ""
    assert _profile_tier_label(eff, winner_ids) == "Efficiency"


def test_profile_list_tier_label_without_winners_keeps_legacy_label() -> None:
    # Backward compatibility: with no resolved-winner map, every profile still
    # reports its own tier (no uniqueness collapse).
    perf_old = {
        "profile_id": "perf-old",
        "final_verified": True,
        "profile_tier": "Performance",
    }
    assert _profile_tier_label(perf_old) == "Performance"
    assert _profile_tier_label(perf_old, {}) == "Performance"


def test_process_error_details_are_copy_friendly() -> None:
    details = _process_failure_details(
        action_label="Auto-UV process",
        exit_code=1,
        exit_status="CrashExit",
        extra_details="Auto-UV exited without reporting a final result.",
        log_tail="[2026-04-27 16:30:06] unexpected traceback",
    )
    copy_text = _error_dialog_copy_text(
        "Auto-UV failed",
        "Auto-UV process stopped unexpectedly.",
        details=details,
    )

    assert "Auto-UV failed" in copy_text
    assert "Auto-UV process stopped unexpectedly." in copy_text
    assert "Action: Auto-UV process" in copy_text
    assert "Exit code: 1" in copy_text
    assert "Exit status: CrashExit" in copy_text
    assert "without reporting a final result" in copy_text
    assert "Recent logs:" in copy_text
    assert "unexpected traceback" in copy_text


def test_profile_source_label_uses_user_facing_auto_uv_name() -> None:
    assert _profile_source_label({"profile_source": "auto-uv-final"}) == "Auto UV"
    assert _profile_source_label({"profile_source": "user-edited"}) == "User edited"
    assert _profile_source_label({"profile_source": "afterburner"}) == "afterburner"


def test_profile_sort_keeps_empty_metrics_at_bottom() -> None:
    assert _sort_value_less(1.0, "")
    assert not _sort_value_less("", 1.0)
    assert _sort_value_less("", 1.0, descending=True)
    assert not _sort_value_less(1.0, "", descending=True)


def test_profile_base_metric_reads_saved_base_fields() -> None:
    profile = {
        "base_candidate_voltage_mv": 1000,
        "base_avg_core_clock_mhz": 2650.0,
        "base_efficiency_fps_per_w": 0.50,
        "base_avg_power_w": 300.0,
    }

    assert _profile_base_metric(profile, "candidate_voltage_mv") == 1000
    assert _profile_base_metric(profile, "avg_core_clock_mhz") == 2650.0
    assert _profile_base_metric(profile, "efficiency_fps_per_w") == 0.50
    assert _profile_base_metric(profile, "avg_power_w") == 300.0


def test_profile_store_keeps_multiple_final_verified_profiles(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)

    archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "avg_core_clock_mhz": 2595.0,
            "efficiency_fps_per_w": 0.70,
            "final_verified": True,
            "points": [{"voltage_mv": 900, "target_mhz": 2600}],
        }
    )
    archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2580,
            "avg_core_clock_mhz": 2575.0,
            "efficiency_fps_per_w": 0.78,
            "final_verified": True,
            "points": [{"voltage_mv": 875, "target_mhz": 2580}],
        }
    )

    summaries = read_auto_uv_profile_summaries()

    assert len(summaries) == 2
    assert {summary["candidate_id"] for summary in summaries} == {
        "900mv-2600mhz",
        "875mv-2580mhz",
    }


def test_profile_store_ignores_short_verified_candidates(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)

    archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "avg_core_clock_mhz": 2595.0,
            "profile_source": "verified-candidate",
            "final_verified": False,
            "points": [{"voltage_mv": 900, "target_mhz": 2600}],
        }
    )

    assert read_auto_uv_profile_summaries() == []


def test_profile_store_lists_user_edited_drafts_but_resolver_blocks_apply(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)

    stored_path = archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "profile_source": "user-edited",
            "final_verified": False,
            "requires_verification": True,
            "points": [
                {
                    "index": 0,
                    "voltage_mv": 900,
                    "base_mhz": 2500,
                    "target_mhz": 2600,
                    "new_offset_mhz": 100,
                }
            ],
        }
    )

    summaries = read_auto_uv_profile_summaries()

    assert len(summaries) == 1
    assert summaries[0]["profile_source"] == "user-edited"
    assert summaries[0]["final_verified"] is False
    assert summaries[0]["requires_verification"] is True
    assert resolve_auto_uv_profile(str(stored_path)) is None
    assert resolve_auto_uv_profile(str(stored_path), allow_unverified=True) is not None


def test_mark_auto_uv_profile_verified_promotes_user_edited_draft(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)

    stored_path = archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "profile_source": "user-edited",
            "final_verified": False,
            "requires_verification": True,
            "verification_status": "unverified",
            "base_avg_fps": 100.0,
            "points": [
                {
                    "index": 0,
                    "voltage_mv": 900,
                    "base_mhz": 2500,
                    "target_mhz": 2600,
                    "new_offset_mhz": 100,
                }
            ],
        }
    )

    assert resolve_auto_uv_profile(str(stored_path)) is None

    marked_path = mark_auto_uv_profile_verified(
        str(stored_path),
        verification={"workload": "Q2RTX benchmark", "result_reason": "ok"},
        metrics={
            "avg_core_clock_mhz": 2580.0,
            "avg_fps": 121.5,
            "avg_power_w": 240.0,
            "efficiency_fps_per_w": 0.50625,
        },
    )

    assert marked_path == stored_path
    resolved = resolve_auto_uv_profile(str(stored_path))
    assert resolved is not None
    payload = json.loads(stored_path.read_text(encoding="utf-8"))
    assert payload["profile_source"] == "user-edited"
    assert payload["final_verified"] is True
    assert payload["requires_verification"] is False
    assert payload["verification_status"] == "verified"
    assert payload["verification"]["workload"] == "Q2RTX benchmark"
    assert payload["verification"]["result_reason"] == "ok"
    assert payload["avg_core_clock_mhz"] == 2580.0
    assert payload["avg_fps"] == 121.5
    assert payload["avg_power_w"] == 240.0
    assert payload["efficiency_fps_per_w"] == 0.50625
    assert payload["base_avg_fps"] == 100.0


def test_bind_legacy_profile_identity_preserves_verified_payload(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    stored_path = archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "final_verified": True,
            "points": [],
        }
    )

    bind_auto_uv_profile_gpu_identity(
        stored_path,
        {
            "name": "NVIDIA RTX 5090",
            "uuid": "GPU-A",
            "pci_bus_id": "00000000:01:00.0",
            "pci_device_id": "0x2B8510DE",
            "index": 0,
        },
    )

    payload = json.loads(stored_path.read_text(encoding="utf-8"))
    assert payload["final_verified"] is True
    assert payload["gpu_identity"]["uuid"] == "GPU-A"
    assert payload["gpu_identity"]["pci_bus_id"] == "00000000:01:00.0"


def test_binding_cannot_silently_move_profile_to_another_gpu(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    stored_path = archive_auto_uv_profile(
        {
            "final_verified": True,
            "gpu_identity": {"uuid": "GPU-A", "name": "RTX 5090"},
        }
    )

    with pytest.raises(ValueError, match="already bound"):
        bind_auto_uv_profile_gpu_identity(stored_path, {"uuid": "GPU-B"})


def test_mark_auto_uv_profile_verified_preserves_existing_metrics(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)

    stored_path = archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "profile_source": "user-edited",
            "final_verified": False,
            "requires_verification": True,
            "avg_fps": 119.0,
            "points": [
                {
                    "index": 0,
                    "voltage_mv": 900,
                    "base_mhz": 2500,
                    "target_mhz": 2600,
                    "new_offset_mhz": 100,
                }
            ],
        }
    )

    mark_auto_uv_profile_verified(
        str(stored_path),
        metrics={"avg_fps": 121.5, "avg_power_w": 240.0},
        base_metrics={
            "avg_fps": 150.0,
            "avg_power_w": 300.0,
            "efficiency_fps_per_w": 0.5,
        },
    )

    payload = json.loads(stored_path.read_text(encoding="utf-8"))
    assert payload["avg_fps"] == 119.0
    assert payload["avg_power_w"] == 240.0
    assert payload["base_avg_fps"] == 150.0
    assert payload["base_avg_power_w"] == 300.0
    assert payload["base_efficiency_fps_per_w"] == 0.5


def test_runtime_profile_precheck_allows_user_edited_drafts_for_verification(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)

    stored_path = archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "profile_source": "user-edited",
            "final_verified": False,
            "requires_verification": True,
            "points": [
                {
                    "index": 0,
                    "voltage_mv": 900,
                    "base_mhz": 2500,
                    "target_mhz": 2600,
                    "new_offset_mhz": 100,
                }
            ],
        }
    )

    assert resolve_auto_uv_profile(str(stored_path)) is None
    assert not allow_unverified_from_argv(["--auto-uv-profile", str(stored_path)])
    assert allow_unverified_from_argv(
        ["--stability-test", "--auto-uv-profile", str(stored_path)]
    )
    assert (
        resolve_auto_uv_profile(
            str(stored_path),
            allow_unverified=allow_unverified_from_argv(
                ["--stability-test", "--auto-uv-profile", str(stored_path)]
            ),
        )
        is not None
    )


def test_profile_verification_stop_request_uses_immediate_user_stop_reason(
    tmp_path,
) -> None:
    stop_path = tmp_path / "verify.stop"
    callback = profile_verification_rules.stability_stop_request_abort_callback(
        stop_path
    )

    assert callback({}) is None
    stop_path.write_text("stop\n", encoding="utf-8")
    assert callback({}) == "user-stop-requested"


def test_profile_verify_selector_uses_exact_json_path() -> None:
    assert (
        _profile_verify_selector(
            {
                "profile_id": "draft-profile",
                "profile_source": "user-edited",
                "requires_verification": True,
                "path": "/tmp/draft.json",
            }
        )
        == "/tmp/draft.json"
    )
    assert (
        _profile_verify_selector(
            {
                "profile_id": "verified-profile",
                "profile_source": "auto-uv-final",
                "path": "/tmp/verified.json",
            }
        )
        == "/tmp/verified.json"
    )


def test_profile_id_from_archive_path_strips_store_filename() -> None:
    assert (
        _profile_id_from_archive_path(
            "/tmp/auto-uv-profile-20260428-user-edited-870mv-2865mhz.json"
        )
        == "20260428-user-edited-870mv-2865mhz"
    )


def test_profile_verification_rejects_vf_curve_apply_mismatch(monkeypatch) -> None:
    plan = [
        {
            "index": 4,
            "voltage_mv": 870,
            "base_mhz": 2242,
            "target_mhz": 2865,
            "new_offset_mhz": 623,
        }
    ]

    class FakeReader:
        def refresh_points(self) -> None:
            pass

        def editable_core_points(self) -> list[dict]:
            return [{"index": 4, "current_offset_khz": 0}]

    deps = profile_verification_runner.ProfileVerificationDependencies(
        load_auto_uv_final_curve=lambda _s, *, allow_unverified=False: {
            "path": "/tmp/draft.json",
            "plan": plan,
            "lock_clock_mhz": 2865,
            "candidate_voltage_mv": 870,
            "memory_offset_mhz": None,
            "flatten_target": {},
        },
        apply_plan=lambda _reader, _plan: None,
    )
    with pytest.raises(penguin_burner.NvmlError, match="did not match"):
        profile_verification_runner.apply_verify_auto_uv_profile(
            FakeReader(),
            "/tmp/draft.json",
            None,
            dependencies=deps,
        )


def test_profile_verification_can_reapply_curve_after_clock_lock(monkeypatch) -> None:
    plan = [
        {
            "index": 4,
            "voltage_mv": 870,
            "base_mhz": 2242,
            "target_mhz": 2865,
            "new_offset_mhz": 623,
        }
    ]

    class FakeReader:
        offset_mhz = 0

        def refresh_points(self) -> None:
            pass

        def editable_core_points(self) -> list[dict]:
            return [{"index": 4, "current_offset_khz": int(self.offset_mhz) * 1000}]

    class ResettingPolicy:
        def __init__(self, reader: FakeReader) -> None:
            self.reader = reader
            self.exact_calls = []

        def apply_locked_core_clock_mhz(self, clock_mhz, **kwargs) -> dict:
            self.exact_calls.append((int(clock_mhz), dict(kwargs)))
            self.reader.offset_mhz = 0
            return {
                "requested_clock_mhz": int(clock_mhz),
                "applied_clock_mhz": int(clock_mhz),
                "mode": "exact",
                "supported_steps_mhz": [180, int(clock_mhz)],
            }

        def reset_locked_core_clocks(self) -> None:
            pass

    def fake_apply_plan(reader: FakeReader, applied_plan: list[dict]) -> None:
        reader.offset_mhz = int(applied_plan[0]["new_offset_mhz"])

    reader = FakeReader()
    policy = ResettingPolicy(reader)
    deps = profile_verification_runner.ProfileVerificationDependencies(
        apply_plan=fake_apply_plan,
    )

    profile_verification_runner.apply_and_verify_profile_vf_plan(
        reader,
        plan,
        context="selected profile",
        dependencies=deps,
    )
    assert reader.offset_mhz == 623

    controller = FlattenedClockCeilingController(
        {
            "source": "auto-uv-final",
            "lock_clock_mhz": 2865,
            "lock_voltage_mv": 870,
        },
        policy,
        exact_lock=True,
    )
    controller.apply()
    assert policy.exact_calls[0][0] == 2865
    assert reader.offset_mhz == 0

    profile_verification_runner.apply_and_verify_profile_vf_plan(
        reader,
        plan,
        context="selected profile after clock lock",
        dependencies=deps,
    )
    assert reader.offset_mhz == 623


def test_runtime_clock_ceiling_uses_saved_rising_tail_ceiling() -> None:
    class RangePolicy:
        def __init__(self) -> None:
            self.range_calls = []

        def get_supported_core_clock_steps_mhz(self) -> list[int]:
            return [210, 2600, 2630]

        def apply_locked_core_clock_range_mhz(
            self,
            min_clock_mhz,
            max_clock_mhz,
            **kwargs,
        ):
            self.range_calls.append(
                (int(min_clock_mhz), int(max_clock_mhz), dict(kwargs))
            )
            return {
                "requested_min_clock_mhz": int(min_clock_mhz),
                "requested_max_clock_mhz": int(max_clock_mhz),
                "applied_min_clock_mhz": int(min_clock_mhz),
                "applied_max_clock_mhz": int(max_clock_mhz),
                "min_mode": "exact",
                "max_mode": "exact",
                "supported_steps_mhz": [210, 2600, 2630],
            }

        def reset_locked_core_clocks(self) -> None:
            pass

    policy = RangePolicy()
    controller = FlattenedClockCeilingController(
        {
            "source": "auto-uv-final",
            "lock_clock_mhz": 2600,
            "lock_voltage_mv": 900,
            "ceiling_clock_mhz": 2630,
        },
        policy,
    )

    controller.apply()

    assert policy.range_calls[0][1] == 2630


def test_profile_verification_metrics_from_q2rtx_result() -> None:
    result = SimpleNamespace(
        benchmark_summary=SimpleNamespace(fps_avg=123.0),
        telemetry_summary=lambda: {
            "core_clock_avg": 2580.0,
            "power_avg": 240.0,
            "power_max": 260.0,
            "voltage_avg": 890.0,
            "voltage_max": 895.0,
            "temperature_avg": 60.0,
            "temperature_max": 66.0,
            "fan_avg": 32.0,
            "fan_max": 45.0,
        },
    )

    metrics = profile_verification_metrics.profile_verification_metrics_from_result(
        result
    )

    assert metrics["avg_fps"] == 123.0
    assert metrics["avg_core_clock_mhz"] == 2580.0
    assert metrics["avg_power_w"] == 240.0
    assert metrics["max_power_w"] == 260.0
    assert metrics["avg_voltage_mv"] == 890.0
    assert metrics["max_voltage_mv"] == 895.0
    assert metrics["avg_temperature_c"] == 60.0
    assert metrics["max_temperature_c"] == 66.0
    assert metrics["avg_fan_speed_pct"] == 32.0
    assert metrics["max_fan_speed_pct"] == 45.0
    assert metrics["efficiency_fps_per_w"] == pytest.approx(123.0 / 240.0)
    assert metrics["efficiency_mhz_per_w"] == pytest.approx(2580.0 / 240.0)
    assert metrics["watts_per_mhz"] == pytest.approx(240.0 / 2580.0)


def test_profile_baseline_plan_restores_base_offsets() -> None:
    plan = [
        {
            "index": 4,
            "voltage_mv": 870,
            "base_mhz": 2242,
            "target_mhz": 2865,
            "new_offset_mhz": 623,
        }
    ]

    base_plan = profile_verification_rules.base_vf_plan_from_profile_plan(plan)

    assert base_plan == [
        {
            "index": 4,
            "voltage_mv": 870,
            "base_mhz": 2242,
            "target_mhz": 2242,
            "new_offset_mhz": 0,
        }
    ]


def test_user_edited_profile_with_missing_base_metrics_needs_baseline(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    stored_path = archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "profile_source": "user-edited",
            "final_verified": False,
            "requires_verification": True,
            "points": [
                {
                    "index": 0,
                    "voltage_mv": 900,
                    "base_mhz": 2500,
                    "target_mhz": 2600,
                    "new_offset_mhz": 100,
                }
            ],
        }
    )

    assert (
        profile_verification_rules.profile_needs_verify_baseline(str(stored_path))
        is True
    )

    mark_auto_uv_profile_verified(
        str(stored_path),
        base_metrics={
            "avg_core_clock_mhz": 2500.0,
            "avg_fps": 100.0,
            "avg_power_w": 250.0,
            "efficiency_fps_per_w": 0.4,
        },
    )

    assert (
        profile_verification_rules.profile_needs_verify_baseline(str(stored_path))
        is False
    )


def test_mark_auto_uv_profile_verification_failed_blocks_user_edited_apply(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    stored_path = archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "profile_source": "user-edited",
            "final_verified": True,
            "requires_verification": False,
            "verification_status": "verified",
            "points": [
                {
                    "index": 0,
                    "voltage_mv": 900,
                    "base_mhz": 2500,
                    "target_mhz": 2600,
                    "new_offset_mhz": 100,
                }
            ],
        }
    )

    marked_path = profile_store.mark_auto_uv_profile_verification_failed(
        str(stored_path),
        failure={
            "reason": "fatal-q2rtx-output",
            "fatal_output_matches": ["device lost"],
        },
    )

    assert marked_path == stored_path
    assert resolve_auto_uv_profile(str(stored_path)) is None
    assert resolve_auto_uv_profile(str(stored_path), allow_unverified=True) is not None
    payload = json.loads(stored_path.read_text(encoding="utf-8"))
    assert payload["final_verified"] is False
    assert payload["requires_verification"] is True
    assert payload["verification_status"] == "failed"
    assert payload["verification"]["failure"]["reason"] == "fatal-q2rtx-output"


def test_profile_verification_promotes_verified_profile(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    stored_path = archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "profile_source": "user-edited",
            "final_verified": False,
            "requires_verification": True,
            "points": [
                {
                    "index": 0,
                    "voltage_mv": 900,
                    "base_mhz": 2500,
                    "target_mhz": 2600,
                    "new_offset_mhz": 100,
                }
            ],
        }
    )

    class FakeGpuClient:
        def refresh_points(self) -> None:
            pass

        def capabilities(self):
            return SimpleNamespace(
                identity=SimpleNamespace(
                    index=0,
                    name="NVIDIA RTX 5090",
                    uuid="GPU-verify-a",
                    pci_bus_id="00000000:01:00.0",
                    pci_device_id="0x2B8510DE",
                )
            )

    config = SimpleNamespace(abort_callback=None)
    result = SimpleNamespace(
        success=True,
        reason="ok",
        log_path=tmp_path / "verify.log",
        benchmark_summary=SimpleNamespace(fps_avg=120.0),
        telemetry_summary=lambda: {
            "core_clock_avg": 2580.0,
            "power_avg": 240.0,
        },
    )
    plan = [
        {
            "index": 0,
            "voltage_mv": 900,
            "base_mhz": 2500,
            "target_mhz": 2600,
            "new_offset_mhz": 100,
        }
    ]
    baseline_calls = []

    def fake_baseline_probe(*_args, **kwargs):
        baseline_calls.append(kwargs)
        return {
            "avg_core_clock_mhz": 2500.0,
            "avg_fps": 100.0,
            "avg_power_w": 250.0,
            "efficiency_fps_per_w": 0.4,
        }

    monkeypatch.setattr(
        profile_verification_runner,
        "apply_verify_auto_uv_profile",
        lambda *_args, **_kwargs: ("auto-UV:2600MHz@900mV", None, plan),
    )
    monkeypatch.setattr(
        profile_verification_runner,
        "run_profile_verification_baseline_probe",
        fake_baseline_probe,
    )

    deps = profile_verification_runner.ProfileVerificationDependencies(
        stop_existing_penguin_burner_runtime=lambda **_kwargs: None,
        gpu_client_factory=lambda **_kwargs: FakeGpuClient(),
        backup_current_offsets=lambda *_args, **_kwargs: None,
        restore_offsets=lambda *_args, **_kwargs: None,
        build_stability_config=lambda *_args, **_kwargs: config,
        build_long_stability_test_config=lambda stability_config, **_kwargs: (
            stability_config
        ),
        attach_stdout_progress=lambda _config: None,
        run_q2rtx_stability_test=lambda _config: result,
        print_q2rtx_stability_result=lambda _result: None,
    )

    profile_verification_runner.run_profile_verification(
        SimpleNamespace(
            auto_uv_profile=str(stored_path),
            stability_seconds=600,
            stability_stop_request_file="",
        ),
        gpu_index=0,
        config_path=tmp_path / "runtime.ini",
        auto_uv_runtime_options={},
        dependencies=deps,
    )

    payload = json.loads(stored_path.read_text(encoding="utf-8"))
    assert payload["final_verified"] is True
    assert payload["requires_verification"] is False
    assert payload["avg_fps"] == 120.0
    assert payload["avg_core_clock_mhz"] == 2580.0
    assert payload["avg_power_w"] == 240.0
    assert payload["efficiency_fps_per_w"] == 0.5
    assert payload["base_avg_core_clock_mhz"] == 2500.0
    assert payload["base_avg_fps"] == 100.0
    assert payload["base_avg_power_w"] == 250.0
    assert payload["base_efficiency_fps_per_w"] == 0.4
    assert payload["gpu_identity"] == {
        "name": "NVIDIA RTX 5090",
        "uuid": "GPU-verify-a",
        "pci_bus_id": "00000000:01:00.0",
        "pci_device_id": "0x2B8510DE",
        "index_at_verification": 0,
    }
    assert baseline_calls
    assert baseline_calls[0]["base_plan"][0]["target_mhz"] == 2500
    assert baseline_calls[0]["base_plan"][0]["new_offset_mhz"] == 0
    assert resolve_auto_uv_profile(str(stored_path)) is not None


def test_profile_verification_wires_live_telemetry_events_when_target_known(
    tmp_path,
    monkeypatch,
) -> None:
    # A GUI listening on stdout can only plot the live GPU position if this
    # is wired with the profile's own target voltage/clock, not left unset.
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    stored_path = archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "profile_source": "user-edited",
            "final_verified": False,
            "requires_verification": True,
            "base_avg_core_clock_mhz": 2500.0,
            "base_avg_fps": 100.0,
            "base_avg_power_w": 250.0,
            "base_efficiency_fps_per_w": 0.4,
            "points": [
                {
                    "index": 0,
                    "voltage_mv": 900,
                    "base_mhz": 2500,
                    "target_mhz": 2600,
                    "new_offset_mhz": 100,
                }
            ],
        }
    )

    class FakeGpuClient:
        def refresh_points(self) -> None:
            pass

        def capabilities(self):
            return SimpleNamespace(
                identity=SimpleNamespace(
                    index=0,
                    name="NVIDIA RTX 5090",
                    uuid="GPU-verify-a",
                    pci_bus_id="00000000:01:00.0",
                    pci_device_id="0x2B8510DE",
                )
            )

    config = SimpleNamespace(abort_callback=None)
    result = SimpleNamespace(
        success=True,
        reason="ok",
        log_path=tmp_path / "verify.log",
        benchmark_summary=SimpleNamespace(fps_avg=120.0),
        telemetry_summary=lambda: {"core_clock_avg": 2580.0, "power_avg": 240.0},
    )
    plan = [
        {
            "index": 0,
            "voltage_mv": 900,
            "base_mhz": 2500,
            "target_mhz": 2600,
            "new_offset_mhz": 100,
        }
    ]
    monkeypatch.setattr(
        profile_verification_runner,
        "apply_verify_auto_uv_profile",
        lambda *_args, **_kwargs: (
            "auto-UV:2600MHz@900mV",
            {"lock_voltage_mv": 900, "lock_clock_mhz": 2600},
            plan,
        ),
    )

    telemetry_calls = []

    def fake_attach_telemetry(stability_config, *, target_voltage_mv, target_clock_mhz):
        telemetry_calls.append(
            {"target_voltage_mv": target_voltage_mv, "target_clock_mhz": target_clock_mhz}
        )
        return stability_config

    deps = profile_verification_runner.ProfileVerificationDependencies(
        stop_existing_penguin_burner_runtime=lambda **_kwargs: None,
        gpu_client_factory=lambda **_kwargs: FakeGpuClient(),
        backup_current_offsets=lambda *_args, **_kwargs: None,
        restore_offsets=lambda *_args, **_kwargs: None,
        build_stability_config=lambda *_args, **_kwargs: config,
        build_long_stability_test_config=lambda stability_config, **_kwargs: (
            stability_config
        ),
        attach_stdout_progress=lambda _config: None,
        attach_stdout_telemetry_events=fake_attach_telemetry,
        run_q2rtx_stability_test=lambda _config: result,
        print_q2rtx_stability_result=lambda _result: None,
    )

    profile_verification_runner.run_profile_verification(
        SimpleNamespace(
            auto_uv_profile=str(stored_path),
            stability_seconds=600,
            stability_stop_request_file="",
        ),
        gpu_index=0,
        config_path=tmp_path / "runtime.ini",
        auto_uv_runtime_options={},
        dependencies=deps,
    )

    assert telemetry_calls == [{"target_voltage_mv": 900, "target_clock_mhz": 2600}]


def test_profile_verification_voltage_abort_requires_sustained_busy_mismatch() -> None:
    callback = profile_verification_rules.profile_verification_voltage_abort_callback(
        {"lock_voltage_mv": 870}
    )
    high_voltage_sample = SimpleNamespace(voltage_mv=1025, gpu_util_pct=99)

    assert (
        callback({"progress_elapsed_s": 2.0, "latest_sample": high_voltage_sample})
        is None
    )
    assert (
        callback(
            {
                "progress_elapsed_s": 10.0,
                "latest_sample": SimpleNamespace(voltage_mv=1025, gpu_util_pct=10),
            }
        )
        is None
    )
    for _ in range(
        profile_verification_rules.PROFILE_VERIFY_VOLTAGE_MISMATCH_STREAK - 1
    ):
        assert (
            callback({"progress_elapsed_s": 10.0, "latest_sample": high_voltage_sample})
            is None
        )

    reason = callback(
        {"progress_elapsed_s": 10.0, "latest_sample": high_voltage_sample}
    )

    assert reason is not None
    assert reason.startswith("profile-verification-voltage-mismatch")
    assert "target=870mV" in reason


def test_profile_summary_uses_real_file_path_not_payload_path(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)

    stored_path = archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2580,
            "points": [{"voltage_mv": 875, "target_mhz": 2580}],
            "final_verified": True,
            "path": str(tmp_path / "stale.json"),
        }
    )

    summaries = read_auto_uv_profile_summaries()

    assert summaries[0]["path"] == str(stored_path)


def test_delete_auto_uv_profiles_removes_only_profile_store_files(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    stored_path = archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2580,
            "points": [{"voltage_mv": 875, "target_mhz": 2580}],
            "final_verified": True,
        }
    )
    active_path = tmp_path / "auto-uv-final-curve.json"
    active_path.write_text(
        json.dumps(
            {
                "candidate_voltage_mv": 900,
                "lock_clock_mhz": 2600,
                "final_verified": True,
                "points": [{"voltage_mv": 900, "target_mhz": 2600}],
            }
        ),
        encoding="utf-8",
    )
    outside_path = tmp_path / "outside-profile.json"
    outside_path.write_text("{}", encoding="utf-8")

    deleted = delete_auto_uv_profile_paths([stored_path, active_path, outside_path])

    assert {path.name for path in deleted} == {stored_path.name}
    assert not stored_path.exists()
    assert active_path.exists()
    assert outside_path.exists()


def test_delete_auto_uv_profiles_accepts_profile_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    stored_path = archive_auto_uv_profile(
        {
            "profile_id": "profile-a",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2580,
            "final_verified": True,
            "points": [{"voltage_mv": 875, "target_mhz": 2580}],
        }
    )

    deleted = delete_auto_uv_profiles(["profile-a"])

    assert deleted == [stored_path.resolve()]
    assert read_auto_uv_profile_summaries() == []


def test_profile_list_ignores_legacy_saved_uv_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    legacy_dir = tmp_path / "saved-uv"
    legacy_dir.mkdir()
    (legacy_dir / "auto-uv-best-undervolt-old.json").write_text(
        json.dumps(
            {
                "candidate_voltage_mv": 865,
                "lock_clock_mhz": 2610,
                "points": [{"voltage_mv": 865, "target_mhz": 2610}],
            }
        ),
        encoding="utf-8",
    )

    assert read_auto_uv_profile_summaries() == []


def test_profile_list_ignores_legacy_active_final_curve_file(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    (tmp_path / "auto-uv-final-curve.json").write_text(
        json.dumps(
            {
                "candidate_voltage_mv": 865,
                "lock_clock_mhz": 2610,
                "final_verified": True,
                "points": [{"voltage_mv": 865, "target_mhz": 2610}],
            }
        ),
        encoding="utf-8",
    )

    assert read_auto_uv_profile_summaries() == []


def test_promote_preferred_profile_moves_one_matching_candidate_to_top() -> None:
    profiles = [
        {"profile_id": "old", "candidate_id": "900mv-2500mhz"},
        {"profile_id": "chosen", "candidate_id": "875mv-2610mhz"},
        {"profile_id": "duplicate", "candidate_id": "875mv-2610mhz"},
    ]

    promoted = _promote_preferred_profile(
        profiles,
        preferred_candidate_id="875mv-2610mhz",
    )

    assert [profile["profile_id"] for profile in promoted] == [
        "chosen",
        "old",
        "duplicate",
    ]


def test_profile_refresh_preserves_user_selection_over_preferred_profile() -> None:
    assert _should_preserve_selection(
        ["profile-a", "profile-b", "profile-c"],
        preferred_profile_id="profile-a",
    )
    assert _should_preserve_selection(
        ["profile-a"],
        preferred_profile_id="profile-b",
    )
    assert not _should_preserve_selection(
        [],
        preferred_profile_id="profile-b",
    )


def test_profile_refresh_preserves_single_selection_toggle_for_same_selection() -> None:
    assert _should_preserve_single_selection_toggle(["profile-a"], ["profile-a"])
    assert not _should_preserve_single_selection_toggle(["profile-a"], ["profile-b"])
    assert not _should_preserve_single_selection_toggle(
        ["profile-a", "profile-b"],
        ["profile-a", "profile-b"],
    )
    assert not _should_preserve_single_selection_toggle([], [])


def test_selected_profile_ids_include_persisted_selector() -> None:
    profiles = [
        {"profile_id": "profile-a", "candidate_id": "875mv-2610mhz"},
        {"profile_id": "profile-b", "candidate_id": "865mv-2625mhz"},
    ]

    assert _selected_profile_ids_include_selector(
        profiles,
        ["profile-b"],
        "865mv-2625mhz",
    )
    assert _selected_profile_ids_include_selector(
        profiles,
        ["profile-a"],
        "latest",
    )
    assert not _selected_profile_ids_include_selector(
        profiles,
        ["profile-b"],
        "latest",
    )


def test_adaptive_profile_delete_keeps_systemd_when_two_tiers_remain() -> None:
    profiles = [
        {
            "profile_id": "eff",
            "final_verified": True,
            "profile_tier": "Efficiency",
        },
        {
            "profile_id": "bal",
            "final_verified": True,
            "profile_tier": "Balanced",
        },
        {
            "profile_id": "perf",
            "final_verified": True,
            "profile_tier": "Performance",
        },
    ]
    autostart_info = {
        "selector": "__systemd_default__",
        "adaptive_auto_uv": True,
    }

    assert _profile_delete_autostart_action(profiles, ["bal"], autostart_info) == {
        "action": "keep",
    }


def test_adaptive_profile_delete_keeps_adaptive_when_one_profile_remains() -> None:
    profiles = [
        {
            "profile_id": "eff",
            "final_verified": True,
            "profile_tier": "Efficiency",
        },
        {
            "profile_id": "bal",
            "final_verified": True,
            "profile_tier": "Balanced",
        },
        {
            "profile_id": "perf",
            "final_verified": True,
            "profile_tier": "Performance",
        },
    ]
    autostart_info = {
        "selector": "__systemd_default__",
        "adaptive_auto_uv": True,
    }

    assert _profile_delete_autostart_action(
        profiles,
        ["bal", "perf"],
        autostart_info,
    ) == {
        "action": "keep",
    }


def test_adaptive_profile_delete_keeps_adaptive_when_remaining_profiles_share_one_tier() -> (
    None
):
    profiles = [
        {
            "profile_id": "bal",
            "final_verified": True,
            "profile_tier": "Balanced",
        },
        {
            "profile_id": "perf-old",
            "final_verified": True,
            "profile_tier": "Performance",
            "profile_created_at": "2026-06-01T12:00:00+00:00",
        },
        {
            "profile_id": "perf-new",
            "final_verified": True,
            "profile_tier": "Performance",
            "profile_created_at": "2026-06-02T12:00:00+00:00",
        },
    ]

    assert _profile_delete_autostart_action(
        profiles,
        ["bal"],
        {"selector": "__systemd_default__", "adaptive_auto_uv": True},
    ) == {
        "action": "keep",
    }


def test_adaptive_profile_delete_restores_stock_when_no_profile_remains() -> None:
    profiles = [
        {
            "profile_id": "eff",
            "final_verified": True,
            "profile_tier": "Efficiency",
        },
        {
            "profile_id": "perf",
            "final_verified": True,
            "profile_tier": "Performance",
        },
    ]
    autostart_info = {
        "selector": "__systemd_default__",
        "adaptive_auto_uv": True,
    }

    assert _profile_delete_autostart_action(
        profiles,
        ["eff", "perf"],
        autostart_info,
    ) == {
        "action": "restore-stock",
        "reason": "last-usable-adaptive-profile",
    }


def test_non_adaptive_profile_delete_restores_stock_for_selected_startup_profile() -> (
    None
):
    profiles = [
        {"profile_id": "profile-a", "candidate_id": "875mv-2610mhz"},
        {"profile_id": "profile-b", "candidate_id": "865mv-2625mhz"},
    ]

    assert _profile_delete_autostart_action(
        profiles,
        ["profile-b"],
        {"selector": "865mv-2625mhz", "adaptive_auto_uv": False},
    ) == {"action": "restore-stock"}
    assert _profile_delete_autostart_action(
        profiles,
        ["profile-a"],
        {"selector": "865mv-2625mhz", "adaptive_auto_uv": False},
    ) == {"action": "keep"}


def test_profile_delete_confirmation_warns_when_stock_will_be_restored() -> None:
    message = _profile_delete_confirmation_text(
        ["2625 MHz 865 mV"],
        restores_stock=True,
    )

    assert "Delete Auto-UV profile 2625 MHz 865 mV?" in message
    assert "currently persisted on startup" in message
    assert "restore stock now and at boot" in message


def test_profile_delete_confirmation_warns_for_last_usable_adaptive_profile() -> None:
    message = _profile_delete_confirmation_text(
        ["2625 MHz 865 mV"],
        restores_stock=True,
        removes_last_usable_adaptive_profile=True,
    )

    assert "Delete Auto-UV profile 2625 MHz 865 mV?" in message
    assert "last usable Adaptive Auto-UV profile" in message
    assert "restore stock now and at boot" in message


def test_lact_export_output_uses_lact_config_filename(tmp_path) -> None:
    assert _lact_export_output_path(tmp_path) == tmp_path / "config.yaml"


def test_lact_gpu_id_parser_reads_first_gpu_key(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join(
            [
                "daemon:",
                "  log_level: info",
                "gpus:",
                "  10DE:2C02-10DE:2095-0000:2b:00.0:",
                "    fan_control_enabled: false",
                "profiles: {}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert _lact_gpu_id_from_config(path) == "10DE:2C02-10DE:2095-0000:2b:00.0"


def test_verify_progress_parses_stability_live_elapsed() -> None:
    assert (
        _verify_elapsed_from_line(
            "Stability live: demo=q2demo1 elapsed=123.4s power=250.0W"
        )
        == 123.4
    )
    assert _verify_elapsed_from_line("Stability test: PASS") is None
    assert _verify_progress_percent(150, 600) == 25
    assert _verify_progress_percent(700, 600) == 100


def test_runner_status_text_shows_running_profile_and_autostart_state() -> None:
    profiles = [
        {
            "profile_id": "profile-a",
            "candidate_id": "875mv-2610mhz",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2610,
        }
    ]

    status = _runner_status_text(
        profiles,
        running_selector="profile-a",
        autostart_selector="profile-a",
        running_silent_fan=True,
        autostart_silent_fan=True,
    )

    assert "Currently running profile: 2610 MHz 875 mV" in status
    assert "Autostart: Yes" in status
    assert "Silent fan curve: On" in status


def test_runner_status_text_marks_adaptive_running_profile() -> None:
    profiles = [
        {
            "profile_id": "profile-a",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2610,
        }
    ]

    status = _runner_status_text(
        profiles,
        running_selector="profile-a",
        running_adaptive=True,
    )

    assert "Currently running profile: 2610 MHz 875 mV (Adaptive)" in status


def test_runner_status_text_has_clear_empty_state() -> None:
    assert (
        _runner_status_text([], running_selector="", autostart_selector="")
        == "No running/autostart profile available yet."
    )


def test_profile_base_curve_points_read_saved_auto_uv_profile_path(tmp_path) -> None:
    profile_path = tmp_path / "auto-uv-profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "points": [
                    {"voltage_mv": 875, "base_mhz": 2550, "target_mhz": 2610},
                    {"voltage_mv": 900, "base_mhz": 2580, "target_mhz": 2625},
                ],
            }
        ),
        encoding="utf-8",
    )

    assert _profile_base_curve_points({"path": str(profile_path)}) == [
        (875.0, 2550.0),
        (900.0, 2580.0),
    ]


def test_profile_fan_curve_points_use_embedded_auto_uv_payload() -> None:
    profile = {
        "display_name": "2715 MHz 850 mV",
        "fan_curve_payload": {
            "fan": {"curve": [[45.0, 0.0], [60.0, 30.0], [75.0, 42.7]]},
            "telemetry": {
                "measured_fan_points": [
                    {
                        "temperature_c": 58.0,
                        "fan_speed_pct": 34.0,
                        "voltage_mv": 850,
                        "clock_mhz": 2715,
                    }
                ]
            },
            "load_anchor_temperature_c": 75.0,
            "load_anchor_fan_speed_pct": 42.7,
        },
    }

    assert _profile_fan_curve_points(profile) == [
        (45.0, 0.0),
        (60.0, 30.0),
        (75.0, 42.7),
    ]
    assert _profile_fan_measurement_points(profile) == [(58.0, 34.0)]
    assert _profile_fan_curve_target_point(profile) == (75.0, 42.7)


def test_saved_fan_curve_payload_helpers_detect_runtime_ready_payload() -> None:
    payload = {
        "loaded_temperature_c": 75.0,
        "observed_fan_speed_pct": 42.7,
        "fan": {"curve": [[45.0, 0.0], [60.0, 30.0], [75.0, 42.7]]},
    }

    assert _fan_payload_has_silent_runtime_fields(payload)
    assert _fan_curve_target_point_from_payload(payload) == (75.0, 42.7)
    assert not _fan_payload_has_silent_runtime_fields(
        {"fan": {"curve": [[45.0, 0.0], [60.0, 30.0]]}}
    )


def test_profile_fan_curve_points_fall_back_to_matching_current_artifact(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ui_app, "default_user_config_dir", lambda: tmp_path)
    (tmp_path / "auto-uv-fan-curve.json").write_text(
        json.dumps(
            {
                "fan": {"curve": [[45.0, 0.0], [70.0, 40.0]]},
                "telemetry": {
                    "measured_fan_points": [
                        {
                            "temperature_c": 66.0,
                            "fan_speed_pct": 35.0,
                            "voltage_mv": 850,
                            "clock_mhz": 2715,
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    matching_profile = {"candidate_voltage_mv": 850, "lock_clock_mhz": 2715}
    other_profile = {"candidate_voltage_mv": 875, "lock_clock_mhz": 2715}

    assert _profile_fan_curve_points(matching_profile) == [
        (45.0, 0.0),
        (70.0, 40.0),
    ]
    assert _profile_fan_measurement_points(matching_profile) == [(66.0, 35.0)]
    assert _profile_fan_curve_points(other_profile) == []


def test_event_base_points_prefer_base_clock_over_target_clock() -> None:
    assert _event_base_points(
        {
            "points": [
                {"voltage_mv": 875, "base_mhz": 2550, "clock_mhz": 2610},
                {"voltage_mv": 900, "base_mhz": 2580, "clock_mhz": 2625},
            ]
        }
    ) == [(875.0, 2550.0), (900.0, 2580.0)]


def test_cached_base_curve_points_roundtrip_for_current_gpu(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(curve_profiles, "default_user_config_dir", lambda: tmp_path)
    monkeypatch.setattr(curve_profiles, "runtime_gpu_index", lambda _path: 0)

    _save_cached_base_curve_points([(900, 2100), (925, 2115)])

    assert _load_cached_base_curve_points() == [
        (900.0, 2100.0),
        (925.0, 2115.0),
    ]


def test_profile_info_from_command_text_parses_selector_and_silent_fan_flag() -> None:
    info = _profile_info_from_command_text(
        "/usr/bin/bash /opt/penguin_burner.sh "
        "--auto-uv-profile profile-a --silent-fan-curve",
        default_if_present=True,
    )

    assert info == {
        "selector": "profile-a",
        "silent_fan_curve": True,
        "adaptive_auto_uv": False,
    }


def test_candidate_id_from_final_result_uses_voltage_and_clock() -> None:
    assert (
        _candidate_id_from_result({"voltage_mv": 875.0, "clock_mhz": 2610.0})
        == "875mv-2610mhz"
    )


def test_final_choice_candidate_table_helpers_show_metrics_and_default() -> None:
    candidate = {
        "candidate_voltage_mv": 865,
        "lock_clock_mhz": 2625,
        "efficiency_fps_per_w": 0.73123,
    }

    assert _candidate_number(candidate["candidate_voltage_mv"], precision=0) == "865"
    assert _candidate_number(candidate["efficiency_fps_per_w"], precision=4) == "0.7312"
    assert _format_number(candidate["efficiency_fps_per_w"], precision=4) == "0.7312"
    assert _candidate_status_text(candidate, True) == "Best FPS/W | Passed short probe"
    assert (
        _candidate_status_text(candidate, True, auto_uv_mode="performance")
        == "Highest FPS | Passed short probe"
    )


def test_final_choice_candidate_sort_values_use_numeric_metrics() -> None:
    candidate = {
        "candidate_voltage_mv": "850",
        "lock_clock_mhz": "2805",
        "avg_core_clock_mhz": "2647.67",
        "efficiency_fps_per_w": "0.6427",
        "avg_fps": "158.21",
        "avg_power_w": "246.16",
        "short_verification_duration_s": "45",
    }

    assert _final_choice_sort_values(candidate)[:8] == [
        850.0,
        2805.0,
        "",
        2647.67,
        0.6427,
        158.21,
        246.16,
        45.0,
    ]


def test_final_choice_oc_uses_target_minus_measured_baseline() -> None:
    candidate = {
        "candidate_voltage_mv": "885",
        "lock_clock_mhz": "2880",
        "core_oc_mhz": "7",
        "base_avg_core_clock_mhz": "2730",
        "avg_core_clock_mhz": "2868.0",
        "efficiency_fps_per_w": "0.6427",
        "avg_fps": "158.21",
        "avg_power_w": "246.16",
        "short_verification_duration_s": "45",
    }

    assert _candidate_oc_text(candidate) == "+150"
    assert _final_choice_sort_values(candidate)[:3] == [885.0, 2880.0, 150.0]


def test_final_choice_oc_can_be_negative_against_measured_baseline() -> None:
    candidate = {
        "candidate_voltage_mv": "875",
        "lock_clock_mhz": "2600",
        "measured_baseline_clock_mhz": "2730",
    }

    assert _candidate_oc_text(candidate) == "-130"
    assert _final_choice_sort_values(candidate)[:3] == [875.0, 2600.0, -130.0]


def test_top_status_text_rounds_gui_decimals_to_two_places() -> None:
    assert _status_value(2625.12345) == "2625.12"
    assert _status_value(865.0) == "865"
    assert (
        _top_status_text("candidate 865.0000mV measured=2625.123456MHz fps=178.98765")
        == "candidate 865.00mV measured=2625.12MHz fps=178.99"
    )


def test_final_verification_duration_control_uses_minutes() -> None:
    assert _format_duration_for_user(600) == "10 min"
    assert _format_duration_for_user(90) == "1 min 30 sec"


def test_stage_title_simplifies_base_baseline() -> None:
    assert _stage_title("base-baseline") == "Baseline"
    assert _stage_title("stock-baseline") == "Baseline"


def test_fan_measurement_helpers_read_probe_result_payloads() -> None:
    assert _fan_measurement_point({"temp_c": 62.4, "fan_pct": 34.2}) == (
        62.4,
        34.2,
    )
    assert _fan_measurement_point({"temp_c": 62.4, "fan_pct": None}) is None
    assert _fan_measurement_points(
        [
            {"temperature_c": 63.1, "fan_speed_pct": 35.0},
            [64.0, 36.0],
            {"temperature_c": 65.0, "fan_speed_pct": 110.0},
        ]
    ) == [(63.1, 35.0), (64.0, 36.0)]


def test_fan_measurement_points_are_sorted_and_deduplicated_for_plotting() -> None:
    points = _sorted_unique_fan_points(
        [(64.0, 35.0), (62.0, 34.0), (64.0, 35.0), (63.0, 34.5)]
    )

    assert points == [(62.0, 34.0), (63.0, 34.5), (64.0, 35.0)]


# --- profile_store branch coverage -----------------------------------------


def _archive_in(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    return archive_auto_uv_profile(payload)


def test_file_time_iso_falls_back_to_now_on_stat_error(monkeypatch) -> None:
    sentinel = "2099-01-01T00:00:00+00:00"
    monkeypatch.setattr(profile_store, "_now_iso", lambda: sentinel)

    class _BadPath:
        def stat(self):
            raise OSError("no stat")

    assert profile_store._file_time_iso(_BadPath()) == sentinel


def test_candidate_id_prefers_explicit_candidate_id() -> None:
    assert profile_store._candidate_id({"candidate_id": "My Cand!"}) == "My-Cand"


def test_profile_sort_time_skips_empty_and_invalid_then_uses_mtime(
    tmp_path,
) -> None:
    target = tmp_path / "p.json"
    target.write_text("{}", encoding="utf-8")
    profile = {
        "profile_created_at": "",  # skipped (empty)
        "verified_at": "not-a-date",  # ValueError -> continue
        "created_at": "also bad",  # ValueError -> continue
        "path": str(target),
    }
    assert profile_store._profile_sort_time(profile) == pytest.approx(
        target.stat().st_mtime
    )


def test_profile_sort_time_returns_zero_when_path_missing() -> None:
    profile = {"path": "/nonexistent/path/should/not/exist.json"}
    assert profile_store._profile_sort_time(profile) == 0.0


def test_profile_sort_time_parses_iso_timestamp() -> None:
    profile = {"profile_created_at": "2026-06-13T10:00:00+00:00"}
    assert profile_store._profile_sort_time(profile) > 0.0


def test_mark_verified_raises_when_profile_not_found(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    with pytest.raises(FileNotFoundError):
        mark_auto_uv_profile_verified("does-not-exist")


def test_mark_verified_raises_when_profile_unreadable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    missing = tmp_path / "ghost.json"
    # Resolver succeeds but the subsequent _read_json(path) returns None.
    monkeypatch.setattr(
        profile_store,
        "resolve_auto_uv_profile",
        lambda *a, **k: (missing, {}),
    )
    with pytest.raises(FileNotFoundError):
        mark_auto_uv_profile_verified("anything")


def test_mark_verified_merges_existing_verification_dict(tmp_path, monkeypatch) -> None:
    stored = _archive_in(
        tmp_path,
        monkeypatch,
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "final_verified": True,
            "verification": {"prior_key": "prior_value"},
            "points": [{"voltage_mv": 900, "target_mhz": 2600}],
        },
    )
    mark_auto_uv_profile_verified(str(stored))
    payload = json.loads(stored.read_text(encoding="utf-8"))
    assert payload["verification"]["prior_key"] == "prior_value"
    assert "verified_at" in payload["verification"]


def test_mark_verification_failed_raises_when_not_found(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    with pytest.raises(FileNotFoundError):
        profile_store.mark_auto_uv_profile_verification_failed("missing")


def test_mark_verification_failed_raises_when_unreadable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    missing = tmp_path / "ghost.json"
    monkeypatch.setattr(
        profile_store,
        "resolve_auto_uv_profile",
        lambda *a, **k: (missing, {}),
    )
    with pytest.raises(FileNotFoundError):
        profile_store.mark_auto_uv_profile_verification_failed(
            "anything", failure={"reason": "x"}
        )


def test_mark_verification_failed_returns_none_for_non_user_edited(
    tmp_path, monkeypatch
) -> None:
    stored = _archive_in(
        tmp_path,
        monkeypatch,
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "final_verified": True,
            "profile_source": "auto-uv-final",
            "points": [{"voltage_mv": 900, "target_mhz": 2600}],
        },
    )
    assert (
        profile_store.mark_auto_uv_profile_verification_failed(
            str(stored), failure={"reason": "x"}
        )
        is None
    )


def test_mark_verification_failed_merges_existing_dict_and_failure(
    tmp_path, monkeypatch
) -> None:
    stored = _archive_in(
        tmp_path,
        monkeypatch,
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "profile_source": "user-edited",
            "requires_verification": True,
            "verification": {"prior": "kept"},
            "points": [{"voltage_mv": 900, "target_mhz": 2600}],
        },
    )
    result = profile_store.mark_auto_uv_profile_verification_failed(
        str(stored), failure={"reason": "crash", "empty": ""}
    )
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["verification"]["prior"] == "kept"
    assert payload["verification"]["failed_at"]
    assert payload["verification"]["failure"] == {"reason": "crash"}
    assert payload["verification_status"] == "failed"


def test_normalize_returns_none_without_points_or_plan() -> None:
    assert (
        profile_store._normalize_profile_payload(
            {"candidate_voltage_mv": 900, "lock_clock_mhz": 2600},
            path=profile_store.Path("/tmp/x.json"),
            source="profile-store",
        )
        is None
    )


def test_normalize_returns_none_without_voltage_or_clock() -> None:
    assert (
        profile_store._normalize_profile_payload(
            {"points": [{"voltage_mv": 900}], "lock_clock_mhz": 2600},
            path=profile_store.Path("/tmp/x.json"),
            source="profile-store",
        )
        is None
    )


def test_load_profile_files_skips_unreadable(tmp_path) -> None:
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps(
            {
                "candidate_voltage_mv": 900,
                "lock_clock_mhz": 2600,
                "final_verified": True,
                "points": [{"voltage_mv": 900, "target_mhz": 2600}],
            }
        ),
        encoding="utf-8",
    )
    bad = tmp_path / "bad.json"
    bad.write_text("{ broken", encoding="utf-8")
    profiles = profile_store._load_profile_files([bad, good], source="profile-store")
    assert len(profiles) == 1
    assert profiles[0]["lock_clock_mhz"] == 2600


def test_profile_display_name_clock_only() -> None:
    assert profile_display_name({"lock_clock_mhz": 2600}) == "2600 MHz"


def test_profile_display_name_voltage_only() -> None:
    assert profile_display_name({"candidate_voltage_mv": 900}) == "900 mV"


def test_profile_display_name_falls_back_to_candidate_id() -> None:
    assert profile_display_name({"candidate_id": "fallback-id"}) == "fallback-id"


def test_display_date_empty_returns_empty() -> None:
    assert profile_store._display_date("") == ""
    assert profile_store._display_date(None) == ""


def test_display_date_parses_iso() -> None:
    assert (
        profile_store._display_date("2026-06-13T10:11:12+00:00")
        == "2026-06-13 10:11:12"
    )


def test_display_date_falls_back_on_unparseable_value() -> None:
    assert profile_store._display_date("2026-06-13Tgarbage") == "2026-06-13 garbage"


def test_delete_auto_uv_profiles_skips_blank_and_uses_path_fallback(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    # Blank selector is skipped; an unresolved selector falls back to a raw Path
    # (which is then rejected by the deletable-path guard since it is outside the dir).
    deleted = delete_auto_uv_profiles(["", "  ", "/tmp/not-a-profile.json"])
    assert deleted == []


def test_delete_auto_uv_profiles_resolves_real_profile(tmp_path, monkeypatch) -> None:
    stored = _archive_in(
        tmp_path,
        monkeypatch,
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "final_verified": True,
            "points": [{"voltage_mv": 900, "target_mhz": 2600}],
        },
    )
    deleted = delete_auto_uv_profiles([str(stored)])
    assert deleted == [stored.resolve()]
    assert not stored.exists()


def test_delete_paths_skips_unresolvable_path(monkeypatch) -> None:
    # Simulate path.resolve() raising OSError -> the path is skipped.
    base = profile_store.Path

    class _FlakyResolvePath(type(base())):
        def resolve(self, *args, **kwargs):
            raise OSError("resolve failed")

    monkeypatch.setattr(profile_store, "Path", _FlakyResolvePath)
    assert delete_auto_uv_profile_paths(["/tmp/boom.json"]) == []


def test_delete_paths_dedupes_repeated_paths(tmp_path, monkeypatch) -> None:
    stored = _archive_in(
        tmp_path,
        monkeypatch,
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "final_verified": True,
            "points": [{"voltage_mv": 900, "target_mhz": 2600}],
        },
    )
    # Same path listed twice: second occurrence hits the "already seen" branch.
    deleted = delete_auto_uv_profile_paths([str(stored), str(stored)])
    assert deleted == [stored.resolve()]


def test_delete_paths_skips_file_that_vanishes(tmp_path, monkeypatch) -> None:
    stored = _archive_in(
        tmp_path,
        monkeypatch,
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "final_verified": True,
            "points": [{"voltage_mv": 900, "target_mhz": 2600}],
        },
    )
    base = profile_store.Path

    class _VanishingUnlinkPath(type(base())):
        def unlink(self, *args, **kwargs):
            raise FileNotFoundError(str(self))

    monkeypatch.setattr(profile_store, "Path", _VanishingUnlinkPath)
    assert delete_auto_uv_profile_paths([str(stored)]) == []


def test_resolve_returns_none_for_blank_selector() -> None:
    assert resolve_auto_uv_profile("   ") is None


def test_resolve_active_returns_none_when_no_profiles(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    assert resolve_auto_uv_profile("latest") is None


def test_resolve_active_returns_latest_profile(tmp_path, monkeypatch) -> None:
    stored = _archive_in(
        tmp_path,
        monkeypatch,
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "final_verified": True,
            "points": [{"voltage_mv": 900, "target_mhz": 2600}],
        },
    )
    resolved = resolve_auto_uv_profile("active")
    assert resolved is not None
    assert resolved[0] == stored


def test_resolve_file_returns_none_when_unreadable(tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{ broken", encoding="utf-8")
    assert resolve_auto_uv_profile(str(bad)) is None


def test_resolve_matches_by_candidate_id(tmp_path, monkeypatch) -> None:
    _archive_in(
        tmp_path,
        monkeypatch,
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "final_verified": True,
            "points": [{"voltage_mv": 900, "target_mhz": 2600}],
        },
    )
    resolved = resolve_auto_uv_profile("900mv-2600mhz")
    assert resolved is not None
    assert resolved[1]["candidate_id"] == "900mv-2600mhz"


def test_deletable_path_rejects_non_json(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    profiles_dir = profile_store.auto_uv_profiles_dir()
    profiles_dir.mkdir(parents=True, exist_ok=True)
    txt = profiles_dir / "note.txt"
    txt.write_text("x", encoding="utf-8")
    assert profile_store._is_deletable_auto_uv_profile_path(txt) is False


def test_display_number_empty_and_non_numeric() -> None:
    assert profile_store._display_number(None, precision=0) == ""
    assert profile_store._display_number("", precision=0) == ""
    assert profile_store._display_number(object(), precision=0) == ""


def test_display_signed_number_variants() -> None:
    assert profile_store._display_signed_number("", precision=0) == ""
    assert profile_store._display_signed_number(0.1, precision=0) == "0"
    assert profile_store._display_signed_number(5, precision=0) == "+5"
    assert profile_store._display_signed_number(-5, precision=0) == "-5"


def test_display_signed_number_returns_text_when_value_non_numeric(
    monkeypatch,
) -> None:
    # _display_number yields non-empty text, but the float() reparse raises
    # -> the formatted text is returned unsigned.
    monkeypatch.setattr(
        profile_store, "_display_number", lambda value, *, precision: "7"
    )

    class _Weird:
        def __float__(self):
            raise ValueError("not a number")

    assert profile_store._display_signed_number(_Weird(), precision=0) == "7"
