from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.dialogs import final_choice as fc
from ui.qt import import_qt


def _tier_choice_candidates():
    return [
        {"candidate_id": "900mv-2700mhz", "candidate_voltage_mv": 900, "lock_clock_mhz": 2700, "avg_fps": 150.0},
        {"candidate_id": "850mv-2430mhz", "candidate_voltage_mv": 850, "lock_clock_mhz": 2430, "avg_fps": 120.0},
    ]


def test_comparison_recommendation_survives_real_dialog_accept(qapp) -> None:
    qtcore, qtgui, qtwidgets, _pg = import_qt()
    candidates = _tier_choice_candidates()
    candidates[1].update(comparison_runs=2, short_verification_duration_s=70)
    observed = []

    def accept_recommendation():
        dialog = qapp.activeModalWidget()
        if dialog is None:
            return
        table = dialog.findChild(qtwidgets.QTableWidget)
        observed.append(table.item(table.currentRow(), 0).data(qtcore.Qt.UserRole))
        dialog.accept()

    timer = qtcore.QTimer()
    timer.timeout.connect(accept_recommendation)
    timer.start(10)
    try:
        selected, duration, action = fc.select_final_candidate(
            QtCore=qtcore, QtGui=qtgui, QtWidgets=qtwidgets, parent=None,
            candidates=candidates, default_candidate_id="850mv-2430mhz",
            default_duration_s=300, auto_uv_mode="performance",
            request_reason="performance-comparison",
        )
    finally:
        timer.stop()
    assert observed == ["850mv-2430mhz"]
    assert selected is not None and selected["candidate_id"] == "850mv-2430mhz"
    assert (duration, action) == (300, "select")
    assert "Comparison winner" in fc.candidate_status_text(
        selected, True, auto_uv_mode="performance", request_reason="performance-comparison",
    )
