from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

import saved_uv_profiles.profile_store as profile_store
import penguin_burner
import ui.curve_profiles as curve_profiles
import ui.fan_profiles as ui_app
from saved_uv_profiles import (
    archive_auto_uv_profile,
    delete_auto_uv_profile_paths,
    delete_auto_uv_profiles,
    format_profile_table,
    mark_auto_uv_profile_verified,
    profile_display_name,
    profile_summary,
    read_auto_uv_profile_summaries,
    resolve_auto_uv_profile,
)
from cli_output import format_user_duration as _format_duration_for_user
from ui.components.fan_curve_editor import (
    fan_curve_editor_shortcut_legend_rows as _fan_curve_editor_shortcut_legend_rows,
)
from ui.components.vf_curve_editor import (
    vf_curve_editor_shortcut_legend_rows as _curve_editor_shortcut_legend_rows,
)
from ui.constants import AFTERBURNER_PROFILE_ID
from ui.curve_profiles import (
    load_cached_base_curve_points as _load_cached_base_curve_points,
    profile_base_curve_points as _profile_base_curve_points,
    profile_curve_points as _profile_curve_points,
    profile_curve_tab_label as _profile_curve_tab_label,
    save_cached_base_curve_points as _save_cached_base_curve_points,
)
from ui.dialogs.error_details import error_dialog_copy_text as _error_dialog_copy_text
from ui.dialogs.error_details import process_failure_details as _process_failure_details
from ui.dialogs.final_choice import candidate_number as _candidate_number
from ui.dialogs.final_choice import candidate_status_text as _candidate_status_text
from ui.dialogs.final_choice import (
    duration_minutes_for_control as _duration_minutes_for_control,
)
from ui.dialogs.final_choice import final_choice_sort_values as _final_choice_sort_values
from ui.fan_profiles import (
    fan_curve_target_point_from_payload as _fan_curve_target_point_from_payload,
    fan_measurement_point as _fan_measurement_point,
    fan_measurement_points as _fan_measurement_points,
    fan_payload_has_silent_runtime_fields as _fan_payload_has_silent_runtime_fields,
    profile_fan_curve_points as _profile_fan_curve_points,
    profile_fan_curve_tab_label as _profile_fan_curve_tab_label,
    profile_fan_curve_target_point as _profile_fan_curve_target_point,
    profile_fan_measurement_points as _profile_fan_measurement_points,
    profile_id_from_archive_path as _profile_id_from_archive_path,
    sorted_unique_fan_points as _sorted_unique_fan_points,
)
from ui.lact_export import lact_export_output_path as _lact_export_output_path
from ui.lact_export import lact_gpu_id_from_config as _lact_gpu_id_from_config
from ui.models import candidate_id_from_payload as _candidate_id_from_result
from ui.models import event_base_points as _event_base_points
from ui.models import stage_title as _stage_title
from ui.models import status_value as _status_value
from ui.models import top_status_text as _top_status_text
from ui.profiles import delete_confirmation_text as _profile_delete_confirmation_text
from ui.profiles import final_profile_notice_text as _final_profile_notice_text
from ui.profiles import profile_info_from_command_text as _profile_info_from_command_text
from ui.profiles import profile_is_deletable as _profile_is_deletable
from ui.profiles import profile_verify_selector as _profile_verify_selector
from ui.profiles import runner_status_text as _runner_status_text
from ui.profiles import (
    selected_profile_ids_include_selector as _selected_profile_ids_include_selector,
)
from ui.verify import elapsed_from_line as _verify_elapsed_from_line
from ui.verify import progress_percent as _verify_progress_percent
from ui.components.profile_list import (
    PROFILE_SORTABLE_COLUMNS,
    ProfileList,
    _format_number,
    _format_profile_metric_delta,
    _format_profile_metric_with_delta,
    _metric_delta_percent,
    _profile_base_metric,
    _profile_metric_delta_color,
    _profile_sort_values,
    _profile_source_label,
    _promote_preferred_profile,
    _should_preserve_persist_toggle,
    _should_preserve_selection,
    _sort_value_less,
)


def test_profile_display_name_uses_clock_then_voltage() -> None:
    profile = {
        "profile_id": "20260427-120000-000000-875mv-2610mhz",
        "candidate_voltage_mv": 875,
        "lock_clock_mhz": 2610,
    }

    assert profile_display_name(profile) == "2610 MHz 875 mV"


def test_profile_table_keeps_date_separate_from_profile_name() -> None:
    profile = {
        "profile_id": "20260427-120000-000000-875mv-2610mhz",
        "profile_created_at": "2026-04-27T12:00:00+02:00",
        "candidate_voltage_mv": 875,
        "lock_clock_mhz": 2610,
        "memory_offset_mhz": 500,
        "avg_core_clock_mhz": 2605.25,
        "efficiency_fps_per_w": 0.81234,
        "profile_source": "profile-store",
    }

    rendered = format_profile_table([profile])

    assert "2026-04-27 12:00:00" in rendered
    assert "2610 MHz 875 mV" in rendered
    assert "+500" in rendered
    assert "20260427-120000-000000-875mv-2610mhz" not in rendered


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
    assert summary["base_avg_core_clock_mhz"] == 2650.0
    assert summary["base_avg_fps"] == 150.0
    assert summary["base_avg_power_w"] == 300.0
    assert summary["base_efficiency_fps_per_w"] == 0.50


def test_profile_metric_delta_text_and_color_vs_base() -> None:
    assert _metric_delta_percent(0.75, 0.50) == 50.0
    assert _format_profile_metric_with_delta(0.75, 0.50, precision=2) == (
        "0.75 (+50.00%)"
    )
    assert _profile_metric_delta_color(0.75, 0.50) == "#55d27a"

    assert _format_profile_metric_with_delta(
        875,
        1000,
        precision=0,
        lower_is_better=True,
    ) == "875 (-12.50%)"
    assert _format_profile_metric_with_delta(
        2600.0,
        2650.0,
        precision=2,
    ) == "2600.00 (-1.89%)"
    assert _format_profile_metric_with_delta(
        240.0,
        300.0,
        precision=2,
        lower_is_better=True,
    ) == "240.00 (-20.00%)"
    assert (
        _profile_metric_delta_color(240.0, 300.0, lower_is_better=True)
        == "#55d27a"
    )
    assert (
        _profile_metric_delta_color(330.0, 300.0, lower_is_better=True)
        == "#ff6b6b"
    )


def test_profile_table_headers_and_sorting_scope() -> None:
    assert ProfileList.COLUMNS[2] == "mV"
    assert ProfileList.COLUMNS[3] == "Target MHz"
    assert ProfileList.COLUMNS[4] == "Effective MHz"
    assert ProfileList.COLUMNS[5] == "FPS/W"
    assert ProfileList.COLUMNS[7] == "Power W"
    assert ProfileList.COLUMNS[8] == "Mem"
    assert ProfileList.COLUMNS[10] == "Autostart"
    assert "Voltage vs base" not in ProfileList.COLUMNS
    assert "FPS/W vs base" not in ProfileList.COLUMNS
    assert "Power vs base" not in ProfileList.COLUMNS
    assert PROFILE_SORTABLE_COLUMNS == frozenset({0, 2, 3, 4, 5, 6, 7})


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
    assert sort_values[4] == pytest.approx(2605.25)
    assert sort_values[5] == pytest.approx(0.80)
    assert sort_values[6] == pytest.approx(160.0)
    assert sort_values[7] == pytest.approx(200.0)
    assert sort_values[8] == ""
    assert sort_values[9] == ""
    assert sort_values[10] == ""


def test_profile_metric_delta_text_is_separate_from_absolute_value() -> None:
    assert _format_profile_metric_delta(0.75, 0.50) == "+50.00%"
    assert _format_profile_metric_delta(0.50, 0.50) == "ref"
    assert _format_profile_metric_delta(0.45, 0.50) == "-10.00%"
    assert _format_profile_metric_delta(
        240.0,
        300.0,
        lower_is_better=True,
    ) == "-20.00%"


def test_profile_table_keeps_regular_font_for_highlight_and_deltas() -> None:
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
        == "875 (-12.50%)"
    )
    assert (
        profile_list.table.item(0, profile_list.EFFECTIVE_MHZ_COLUMN).text()
        == "2605.25 (-1.69%)"
    )
    assert profile_list.table.item(0, profile_list.FPSW_COLUMN).text() == (
        "0.80 (+60.00%)"
    )
    assert profile_list.table.item(0, profile_list.FPS_COLUMN).text() == (
        "160.00 (+6.67%)"
    )
    assert profile_list.table.item(0, profile_list.POWER_COLUMN).text() == (
        "200.00 (-20.00%)"
    )
    assert profile_list.table.item(0, profile_list.MEMORY_OFFSET_COLUMN).text() == (
        "+1000"
    )
    for column in range(profile_list.table.columnCount()):
        item = profile_list.table.item(0, column)
        assert item is not None
        assert not item.font().bold()


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
    assert (
        _profile_base_metric(profile, "efficiency_fps_per_w")
        == 0.50
    )
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
        verification={"workload": "Q2RTX timedemo", "result_reason": "ok"},
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
    assert payload["verification"]["workload"] == "Q2RTX timedemo"
    assert payload["verification"]["result_reason"] == "ok"
    assert payload["avg_core_clock_mhz"] == 2580.0
    assert payload["avg_fps"] == 121.5
    assert payload["avg_power_w"] == 240.0
    assert payload["efficiency_fps_per_w"] == 0.50625
    assert payload["base_avg_fps"] == 100.0


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
    assert not penguin_burner._runtime_profile_selector_allows_unverified_from_argv(
        ["--auto-uv-profile", str(stored_path)]
    )
    assert penguin_burner._runtime_profile_selector_allows_unverified_from_argv(
        ["--stability-test", "--auto-uv-profile", str(stored_path)]
    )
    assert (
        resolve_auto_uv_profile(
            str(stored_path),
            allow_unverified=penguin_burner._runtime_profile_selector_allows_unverified_from_argv(
                ["--stability-test", "--auto-uv-profile", str(stored_path)]
            ),
        )
        is not None
    )


def test_profile_verification_stop_request_uses_immediate_user_stop_reason(
    tmp_path,
) -> None:
    stop_path = tmp_path / "verify.stop"
    callback = penguin_burner._stability_stop_request_abort_callback(stop_path)

    assert callback({}) is None
    stop_path.write_text("stop\n", encoding="utf-8")
    assert callback({}) == "user-stop-requested"


def test_profile_verify_selector_uses_exact_json_path() -> None:
    assert _profile_verify_selector(
        {
            "profile_id": "draft-profile",
            "profile_source": "user-edited",
            "requires_verification": True,
            "path": "/tmp/draft.json",
        }
    ) == "/tmp/draft.json"
    assert _profile_verify_selector(
        {
            "profile_id": "verified-profile",
            "profile_source": "auto-uv-final",
            "path": "/tmp/verified.json",
        }
    ) == "/tmp/verified.json"


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

    monkeypatch.setattr(
        penguin_burner,
        "load_auto_uv_final_curve",
        lambda _selector, *, allow_unverified=False: {
            "path": "/tmp/draft.json",
            "plan": plan,
            "lock_clock_mhz": 2865,
            "candidate_voltage_mv": 870,
            "memory_offset_mhz": None,
            "flatten_target": {},
        },
    )
    monkeypatch.setattr(penguin_burner, "apply_plan", lambda _reader, _plan: None)

    with pytest.raises(penguin_burner.NvmlError, match="did not match"):
        penguin_burner._apply_verify_auto_uv_profile(
            FakeReader(),
            "/tmp/draft.json",
            None,
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

    monkeypatch.setattr(penguin_burner, "apply_plan", fake_apply_plan)
    reader = FakeReader()
    policy = ResettingPolicy(reader)

    penguin_burner._apply_and_verify_profile_vf_plan(
        reader,
        plan,
        context="selected profile",
    )
    assert reader.offset_mhz == 623

    controller = penguin_burner.FlattenedClockCeilingController(
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
    assert controller.telemetry_text().startswith("clk_lock=")
    assert reader.offset_mhz == 0

    penguin_burner._apply_and_verify_profile_vf_plan(
        reader,
        plan,
        context="selected profile after clock lock",
    )
    assert reader.offset_mhz == 623


def test_profile_verification_metrics_from_q2rtx_result() -> None:
    result = SimpleNamespace(
        timedemo_runs=[
            SimpleNamespace(fps=120.0),
            SimpleNamespace(fps=126.0),
        ],
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

    metrics = penguin_burner._profile_verification_metrics_from_result(result)

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

    base_plan = penguin_burner._base_vf_plan_from_profile_plan(plan)

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

    assert penguin_burner._profile_needs_verify_baseline(str(stored_path)) is True

    mark_auto_uv_profile_verified(
        str(stored_path),
        base_metrics={
            "avg_core_clock_mhz": 2500.0,
            "avg_fps": 100.0,
            "avg_power_w": 250.0,
            "efficiency_fps_per_w": 0.4,
        },
    )

    assert penguin_burner._profile_needs_verify_baseline(str(stored_path)) is False


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
        failure={"reason": "fatal-q2rtx-output", "fatal_output_matches": ["device lost"]},
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

    class FakeReader:
        def close(self) -> None:
            pass

    class FakePolicy:
        def close(self) -> None:
            pass

    config = SimpleNamespace(abort_callback=None)
    result = SimpleNamespace(
        success=True,
        reason="ok",
        log_path=tmp_path / "verify.log",
        timedemo_runs=[SimpleNamespace(fps=120.0)],
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
    monkeypatch.setattr(
        penguin_burner,
        "stop_existing_penguin_burner_runtime",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        penguin_burner,
        "create_hidden_vf_curve_reader",
        lambda **_kwargs: FakeReader(),
    )
    monkeypatch.setattr(
        penguin_burner,
        "NvmlGpuPolicyController",
        lambda **_kwargs: FakePolicy(),
    )
    monkeypatch.setattr(
        penguin_burner,
        "backup_current_offsets",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        penguin_burner,
        "restore_offsets",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        penguin_burner,
        "_apply_verify_auto_uv_profile",
        lambda *_args, **_kwargs: ("auto-UV:2600MHz@900mV", None, plan),
    )

    def fake_baseline_probe(*_args, **kwargs):
        baseline_calls.append(kwargs)
        return {
            "avg_core_clock_mhz": 2500.0,
            "avg_fps": 100.0,
            "avg_power_w": 250.0,
            "efficiency_fps_per_w": 0.4,
        }

    monkeypatch.setattr(
        penguin_burner,
        "_run_profile_verification_baseline_probe",
        fake_baseline_probe,
    )
    monkeypatch.setattr(
        penguin_burner,
        "build_stability_config",
        lambda *_args, **_kwargs: config,
    )
    monkeypatch.setattr(
        penguin_burner,
        "build_long_stability_test_config",
        lambda stability_config, **_kwargs: stability_config,
    )
    monkeypatch.setattr(
        penguin_burner,
        "attach_stdout_progress",
        lambda _config: None,
    )
    monkeypatch.setattr(
        penguin_burner,
        "run_q2rtx_stability_test",
        lambda _config: result,
    )
    monkeypatch.setattr(
        penguin_burner,
        "print_q2rtx_stability_result",
        lambda _result: None,
    )

    penguin_burner.run_profile_verification(
        SimpleNamespace(
            auto_uv_profile=str(stored_path),
            prefer_afterburner_curve=False,
            stability_seconds=600,
            stability_stop_request_file="",
        ),
        gpu_index=0,
        config_path=tmp_path / "runtime.ini",
        afterburner_runtime_options={},
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
    assert baseline_calls
    assert baseline_calls[0]["base_plan"][0]["target_mhz"] == 2500
    assert baseline_calls[0]["base_plan"][0]["new_offset_mhz"] == 0
    assert resolve_auto_uv_profile(str(stored_path)) is not None


def test_profile_verification_voltage_abort_requires_sustained_busy_mismatch() -> None:
    callback = penguin_burner._profile_verification_voltage_abort_callback(
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
    for _ in range(penguin_burner.PROFILE_VERIFY_VOLTAGE_MISMATCH_STREAK - 1):
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


def test_profile_refresh_preserves_persist_toggle_for_same_single_selection() -> None:
    assert _should_preserve_persist_toggle(["profile-a"], ["profile-a"])
    assert not _should_preserve_persist_toggle(["profile-a"], ["profile-b"])
    assert not _should_preserve_persist_toggle(
        ["profile-a", "profile-b"],
        ["profile-a", "profile-b"],
    )
    assert not _should_preserve_persist_toggle([], [])


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


def test_profile_delete_confirmation_warns_when_systemd_entry_is_removed() -> None:
    message = _profile_delete_confirmation_text(
        ["2625 MHz 865 mV"],
        removes_systemd=True,
    )

    assert "Delete Auto-UV profile 2625 MHz 865 mV?" in message
    assert "currently persisted on startup" in message
    assert "remove the Systemd autostart entry" in message


def test_afterburner_profile_is_deletable_without_profile_path() -> None:
    assert _profile_is_deletable(
        {
            "profile_id": AFTERBURNER_PROFILE_ID,
            "runtime_source": "afterburner",
            "path": "",
        }
    )


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


def test_profile_delete_confirmation_describes_afterburner_config_entry() -> None:
    message = _profile_delete_confirmation_text(
        ["MSI Afterburner Profile1"],
        includes_afterburner=True,
    )

    assert "Delete profile MSI Afterburner Profile1?" in message
    assert "Afterburner import entries are removed" in message


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
    assert "Systemd autostart: Yes" in status
    assert "Silent fan curve: On" in status


def test_runner_status_text_has_clear_empty_state() -> None:
    assert (
        _runner_status_text([], running_selector="", autostart_selector="")
        == "No running/autostart profile available yet."
    )


def test_final_profile_notice_names_saved_profile_and_profiles_tab() -> None:
    profiles = [
        {
            "profile_id": "profile-a",
            "candidate_id": "865mv-2625mhz",
            "candidate_voltage_mv": 865,
            "lock_clock_mhz": 2625,
        }
    ]

    assert (
        _final_profile_notice_text(
            profiles,
            profile_id="profile-a",
            candidate_id="865mv-2625mhz",
            result_payload={"voltage_mv": 865, "clock_mhz": 2625},
        )
        == "Final verification complete. Profile 2625 MHz 865 mV is saved and "
        "highlighted in Profiles."
    )


def test_profile_curve_points_use_embedded_afterburner_points() -> None:
    profile = {
        "profile_id": AFTERBURNER_PROFILE_ID,
        "display_name": "MSI Afterburner Profile1 2100 MHz 900 mV",
        "curve_points": [[900, 2100], [925, 2115]],
    }

    assert _profile_curve_points(profile) == [(900.0, 2100.0), (925.0, 2115.0)]
    assert _profile_curve_tab_label(profile) == (
        "MSI Afterburner Profile1 2100 MHz 900 mV"
    )


def test_profile_curve_points_read_saved_auto_uv_profile_path(tmp_path) -> None:
    profile_path = tmp_path / "auto-uv-profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "candidate_voltage_mv": 875,
                "lock_clock_mhz": 2610,
                "points": [
                    {"voltage_mv": 875, "target_mhz": 2610},
                    {"voltage_mv": 900, "target_mhz": 2625},
                ],
            }
        ),
        encoding="utf-8",
    )

    assert _profile_curve_points({"path": str(profile_path)}) == [
        (875.0, 2610.0),
        (900.0, 2625.0),
    ]


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
    assert _profile_fan_curve_tab_label(profile) == "2715 MHz 850 mV Fan Curve"


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
        "/usr/bin/bash /opt/penguin_burner.sh --foreground "
        "--auto-uv-profile profile-a --silent-fan-curve",
        default_if_present=True,
    )

    assert info == {"selector": "profile-a", "silent_fan_curve": True}


def test_profile_info_from_command_text_parses_afterburner_preference() -> None:
    info = _profile_info_from_command_text(
        "/usr/bin/bash /opt/penguin_burner.sh --foreground "
        "--prefer-afterburner-curve --silent-fan-curve",
        default_if_present=True,
    )

    assert info == {
        "selector": AFTERBURNER_PROFILE_ID,
        "silent_fan_curve": True,
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
    assert _candidate_number(candidate["efficiency_fps_per_w"], precision=4) == "0.73"
    assert _format_number(candidate["efficiency_fps_per_w"], precision=4) == "0.73"
    assert _candidate_status_text(candidate, True) == "Best FPS/W | Passed short probe"
    assert (
        _candidate_status_text(candidate, True, auto_uv_mode="performance")
        == "Best FPS | Passed short probe"
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

    assert _final_choice_sort_values(candidate)[:7] == [
        850.0,
        2805.0,
        2647.67,
        0.6427,
        158.21,
        246.16,
        45.0,
    ]


def test_top_status_text_rounds_gui_decimals_to_two_places() -> None:
    assert _status_value(2625.12345) == "2625.12"
    assert _status_value(865.0) == "865"
    assert (
        _top_status_text(
            "candidate 865.0000mV measured=2625.123456MHz fps=178.98765"
        )
        == "candidate 865.00mV measured=2625.12MHz fps=178.99"
    )


def test_final_verification_duration_control_uses_minutes() -> None:
    assert _format_duration_for_user(600) == "10 min"
    assert _format_duration_for_user(90) == "1 min 30 sec"
    assert _duration_minutes_for_control(90) == 2
    assert _duration_minutes_for_control(3600) == 60


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
