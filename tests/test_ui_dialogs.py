"""Coverage for ui/dialogs/*.

Pure helpers (final_choice ranking, error_details formatting) tested directly;
the build-then-exec dialogs are constructed with QDialog.exec monkeypatched so
their layout/wiring runs without blocking.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import ui.dialogs.about as about_dialog
import ui.dialogs.afterburner_import as ab_dialog
import ui.dialogs.error_details as error_details
import ui.dialogs.final_choice as fc
import ui.dialogs.scan_tuning as scan_tuning_dialog
import ui.dialogs.verify as verify_dialog
from ui.features.auto_uv.final_choice_ranking import candidate_fps, candidate_fpsw
from ui.qt import import_qt


# --- final_choice pure helpers ------------------------------------------------


def test_candidate_status_text() -> None:
    assert "Best FPS/W" in fc.candidate_status_text({"final_verified": True}, True)
    assert "Highest FPS" in fc.candidate_status_text(
        {}, True, auto_uv_mode="performance"
    )
    assert "Tier suggestion" in fc.candidate_status_text(
        {}, True, request_reason="adaptive-balanced"
    )
    assert fc.candidate_status_text({}, False) == "Passed short probe"


def test_candidate_number_and_numeric_sort() -> None:
    assert fc.candidate_number(2500.4, precision=0) == "2500"
    assert fc.candidate_number(12.34, precision=2) == "12.34"
    assert fc.candidate_number(None, precision=2) == ""
    assert fc.candidate_number("x", precision=2) == ""
    assert fc.numeric_sort_value("") == ""
    assert fc.numeric_sort_value(5) == 5.0
    assert fc.numeric_sort_value("nan") == ""


def test_sort_and_best_candidate() -> None:
    candidates = [
        {"candidate_id": "a", "avg_fps": 100, "efficiency_fps_per_w": 0.5, "lock_clock_mhz": 2400, "candidate_voltage_mv": 950},
        {"candidate_id": "b", "avg_fps": 120, "efficiency_fps_per_w": 0.6, "lock_clock_mhz": 2500, "candidate_voltage_mv": 900},
    ]
    perf_sorted = fc.sort_candidates_for_final_choice(candidates, "performance")
    assert perf_sorted[0]["candidate_id"] == "b"  # highest fps first
    eff_sorted = fc.sort_candidates_for_final_choice(candidates, "efficiency")
    assert eff_sorted[0]["candidate_id"] == "b"  # best fps/w first
    # best_* returns the first candidate (in the given, already-sorted order)
    # with a usable metric.
    assert fc.best_final_choice_candidate_id(perf_sorted, "performance") == "b"
    assert fc.best_final_choice_candidate_id(eff_sorted, "efficiency") == "b"
    assert fc.best_final_choice_candidate_id([], "efficiency") == ""
    assert fc.final_choice_sort_column_for_mode("performance") == fc.FINAL_CHOICE_FPS_SORT_COLUMN
    assert isinstance(fc.final_choice_sort_values(candidates[0]), list)


def test_duration_helpers_and_fps() -> None:
    assert fc.candidate_short_duration_s({"short_verification_duration_s": 45}) == 45
    assert fc.candidate_short_duration_s({}) == 30
    assert candidate_fpsw({"efficiency_fps_per_w": 0.6}) == 0.6
    assert candidate_fpsw({}) is None
    assert candidate_fps({"avg_fps": 120}) == 120.0


def test_final_choice_intro_text() -> None:
    assert "stopped" in fc.final_choice_intro_text("efficiency", request_reason="user-stop")
    assert "best FPS/W" in fc.final_choice_intro_text("efficiency")
    assert "highest FPS" in fc.final_choice_intro_text("performance")
    assert "tier's confirmed candidate" in fc.final_choice_intro_text(
        "efficiency", request_reason="adaptive-efficiency"
    )


# --- error_details pure helpers -----------------------------------------------


def test_process_failure_details() -> None:
    text = error_details.process_failure_details(
        action_label="Auto-UV scan",
        exit_code=3,
        exit_status="crash",
        extra_details="boom",
        log_tail="line1\nline2",
    )
    assert "Action: Auto-UV scan" in text
    assert "Exit code: 3" in text
    assert "Recent logs:" in text
    # defaults when blank
    assert "Unknown action" in error_details.process_failure_details(
        action_label="", exit_code=0, exit_status=""
    )


def test_qt_enum_name_and_copy_text() -> None:
    from types import SimpleNamespace

    assert error_details.qt_enum_name(SimpleNamespace(name="CrashExit")) == "CrashExit"
    assert error_details.qt_enum_name("QProcess.CrashExit") == "CrashExit"
    assert error_details.qt_enum_name("plain") == "plain"
    assert error_details.error_dialog_copy_text("T", "M", details="D").endswith("D")
    assert "T" in error_details.error_dialog_copy_text("T", "M")


# --- dialog construction (exec patched) ---------------------------------------


@pytest.fixture
def qt(qapp, monkeypatch):
    modules = import_qt()
    monkeypatch.setattr(modules[2].QDialog, "exec", lambda self: modules[2].QDialog.Rejected)
    return modules


def test_show_error_dialog_builds(qt) -> None:
    qtcore, qtgui, qtwidgets, _pg = qt
    error_details.show_error_dialog(
        QtCore=qtcore,
        QtGui=qtgui,
        QtWidgets=qtwidgets,
        parent=None,
        title="Boom",
        message="It failed",
        details="trace",
    )


def test_show_about_dialog_builds(qt) -> None:
    qtcore, qtgui, qtwidgets, _pg = qt
    about_dialog.show_about_dialog(
        QtCore=qtcore, QtGui=qtgui, QtWidgets=qtwidgets, parent=None
    )


def test_select_verify_options_rejected_returns_none(qt) -> None:
    _qtcore, _qtgui, qtwidgets, _pg = qt
    assert verify_dialog.select_verify_options(
        QtWidgets=qtwidgets, parent=None, profile_label="P1"
    ) is None


def test_select_final_candidate_paths(qt) -> None:
    qtcore, qtgui, qtwidgets, _pg = qt
    # No candidates -> discarded.
    selected, duration, action = fc.select_final_candidate(
        QtCore=qtcore, QtGui=qtgui, QtWidgets=qtwidgets, parent=None,
        candidates=[], default_candidate_id="",
    )
    assert selected is None and action == "discard"
    # With candidates + rejected exec -> still returns a 3-tuple.
    result = fc.select_final_candidate(
        QtCore=qtcore, QtGui=qtgui, QtWidgets=qtwidgets, parent=None,
        candidates=[{"candidate_id": "c1", "avg_fps": 120, "efficiency_fps_per_w": 0.6,
                     "lock_clock_mhz": 2500, "candidate_voltage_mv": 900}],
        default_candidate_id="c1", auto_uv_mode="efficiency",
    )
    assert isinstance(result, tuple) and len(result) == 3


def _tier_choice_candidates() -> list[dict]:
    # 900mV is the FPS/W best; 850mV is the tier's confirmed candidate.
    return [
        {"candidate_id": "900mv-2700mhz", "candidate_voltage_mv": 900,
         "lock_clock_mhz": 2700, "avg_fps": 150.0, "efficiency_fps_per_w": 0.75},
        {"candidate_id": "850mv-2430mhz", "candidate_voltage_mv": 850,
         "lock_clock_mhz": 2430, "avg_fps": 120.0, "efficiency_fps_per_w": 0.60},
    ]


def test_select_final_candidate_adaptive_preselects_tier_candidate(qt, monkeypatch) -> None:
    qtcore, qtgui, qtwidgets, _pg = qt
    monkeypatch.setattr(qtwidgets.QDialog, "exec", lambda self: qtwidgets.QDialog.Accepted)

    selected, _duration, action = fc.select_final_candidate(
        QtCore=qtcore, QtGui=qtgui, QtWidgets=qtwidgets, parent=None,
        candidates=_tier_choice_candidates(),
        default_candidate_id="850mv-2430mhz",
        auto_uv_mode="efficiency", request_reason="adaptive-efficiency",
    )

    assert action == "select"
    assert selected is not None and selected["candidate_id"] == "850mv-2430mhz"


def test_select_final_candidate_classic_keeps_metric_best_default(qt, monkeypatch) -> None:
    qtcore, qtgui, qtwidgets, _pg = qt
    monkeypatch.setattr(qtwidgets.QDialog, "exec", lambda self: qtwidgets.QDialog.Accepted)

    selected, _duration, action = fc.select_final_candidate(
        QtCore=qtcore, QtGui=qtgui, QtWidgets=qtwidgets, parent=None,
        candidates=_tier_choice_candidates(),
        default_candidate_id="850mv-2430mhz",
        auto_uv_mode="efficiency", request_reason="sweep-complete",
    )

    assert action == "select"
    assert selected is not None and selected["candidate_id"] == "900mv-2700mhz"


def test_select_afterburner_import_builds(qt) -> None:
    qtcore, qtgui, qtwidgets, pg = qt
    assert ab_dialog.select_afterburner_import(
        QtCore=qtcore, QtGui=qtgui, QtWidgets=qtwidgets, pg=pg, parent=None
    ) is None


def test_select_scan_tuning_builds(qt) -> None:
    qtcore, qtgui, qtwidgets, _pg = qt
    assert scan_tuning_dialog.select_scan_tuning(
        QtCore=qtcore, QtGui=qtgui, QtWidgets=qtwidgets, parent=None, gpu_index=0
    ) is None


def test_select_scan_tuning_mirrors_balanced_and_performance_memory(
    qt, monkeypatch
) -> None:
    """The full scan keeps the Balanced/Performance memory boxes identical.

    Matching offsets are what let the scan reuse the Balanced downsweep for
    Performance; Efficiency stays independent, and the single-profile scope
    releases the mirror."""
    qtcore, qtgui, qtwidgets, _pg = qt
    monkeypatch.setattr(
        scan_tuning_dialog, "memory_offset_mhz_range", lambda **_kwargs: (0, 4000)
    )

    from ui.features.tuning.gpu_selection import GpuChoice

    monkeypatch.setattr(
        scan_tuning_dialog, "gpu_choices_with_fallback",
        lambda **_: ([GpuChoice(index=0, name="RTX 5080")], 0),
    )

    checked: dict[str, bool] = {}

    def exec_and_probe(self):
        spins = [
            spin
            for spin in self.findChildren(qtwidgets.QSpinBox)
            if spin.objectName() == "memoryOffsetSpin"
        ]
        assert len(spins) == 3  # one per profile page

        def profile_of(spin):
            # findChildren order follows the stack's raise order, not the
            # profile order, so identify each memory box by the profile
            # page it sits on (each page has a distinctive extra control).
            page = spin
            while not isinstance(
                page.parentWidget(), qtwidgets.QStackedWidget
            ):
                page = page.parentWidget()
            if page.findChild(qtwidgets.QSpinBox, "efficiencyVoltageSpin"):
                return "efficiency"
            if page.findChild(qtwidgets.QSpinBox, "performanceVoltageSpin"):
                return "performance"
            return "balanced"

        by_profile = {profile_of(spin): spin for spin in spins}
        assert set(by_profile) == {"efficiency", "balanced", "performance"}
        efficiency = by_profile["efficiency"]
        balanced = by_profile["balanced"]
        performance = by_profile["performance"]
        for spin in spins:
            spin.setValue(0)
        # Full scan is the default scope: the two boxes mirror both ways.
        balanced.setValue(1500)
        checked["performance_follows_balanced"] = performance.value() == 1500
        performance.setValue(750)
        checked["balanced_follows_performance"] = balanced.value() == 750
        checked["efficiency_untouched"] = efficiency.value() == 0
        # The selected-profile scope releases the mirror.
        scope_buttons = {
            str(button.property("scopeId")): button
            for button in self.findChildren(qtwidgets.QPushButton)
            if button.objectName() == "autoUvScopeButton"
        }
        scope_buttons["selected-profile"].setChecked(True)
        balanced.setValue(2000)
        checked["independent_when_single"] = performance.value() == 750
        return qtwidgets.QDialog.Rejected

    monkeypatch.setattr(qtwidgets.QDialog, "exec", exec_and_probe)
    assert scan_tuning_dialog.select_scan_tuning(
        QtCore=qtcore, QtGui=qtgui, QtWidgets=qtwidgets, parent=None, gpu_index=0
    ) is None
    assert checked == {
        "performance_follows_balanced": True,
        "balanced_follows_performance": True,
        "efficiency_untouched": True,
        "independent_when_single": True,
    }


def test_energy_savings_formatting_uptime_and_units() -> None:
    assert about_dialog.format_total_runtime(9) == "9s"
    assert about_dialog.format_total_runtime(75) == "1m 15s"
    assert about_dialog.format_total_runtime(3 * 3600 + 62) == "3h 1m 2s"
    assert about_dialog.format_total_runtime(2 * 86400 + 3 * 3600 + 5) == (
        "2d 3h 0m 5s"
    )

    assert about_dialog.format_energy_saved(0) == "0.00 Wh"
    assert about_dialog.format_energy_saved(30.0 * 3600) == "30.00 Wh"
    assert about_dialog.format_energy_saved(150.0 * 3600) == "150.0 Wh"
    assert about_dialog.format_energy_saved(1_240 * 3600) == "1.24 kWh"
    assert about_dialog.format_energy_saved(1_020_000 * 3600) == "1.02 MWh"


def test_energy_savings_lines_from_daemon_status() -> None:
    status = {
        "energy_savings": {
            "active_seconds": 3 * 3600.0,
            "saved_watt_seconds": 30.0 * 3600 * 3,
        }
    }
    text = about_dialog.energy_savings_lines(status)
    assert text == "Total runtime: 3h 0m 0s\nEnergy saved: 90.00 Wh"

    # No counter yet, malformed payloads, missing block: no stats line.
    assert about_dialog.energy_savings_lines({}) == ""
    assert about_dialog.energy_savings_lines({"energy_savings": None}) == ""
    assert (
        about_dialog.energy_savings_lines(
            {"energy_savings": {"active_seconds": 0}}
        )
        == ""
    )
    assert (
        about_dialog.energy_savings_lines(
            {"energy_savings": {"active_seconds": "junk"}}
        )
        == ""
    )
