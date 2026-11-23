from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from auto_uv.session.scan_session import ScanSession
from ui.components.auto_uv_tier_progress import AutoUvTierProgress
from ui.qt import import_qt

TIERS = ("efficiency", "balanced", "performance")


def _tier(session: ScanSession, event: str, tier: str) -> None:
    session.handle_event({"event": event, "tier": tier})


def test_full_scan_tier_progress_advances_in_chronological_order(qapp) -> None:
    _qtcore, _qtgui, qtwidgets, _pg = import_qt()
    progress = AutoUvTierProgress(QtWidgets=qtwidgets)
    session = ScanSession()

    session.start({"auto_uv_mode": "adaptive"})
    progress.show_states(session.tier_states)
    assert not progress.widget.isHidden()
    assert [progress.state(tier) for tier in TIERS] == ["pending"] * 3

    _tier(session, "tier_started", "efficiency")
    progress.show_states(session.tier_states)
    assert progress.state("efficiency") == "active"
    assert "Scanning" in progress.steps["efficiency"].text()

    _tier(session, "tier_completed", "efficiency")
    _tier(session, "tier_started", "balanced")
    progress.show_states(session.tier_states)
    assert progress.state("efficiency") == "complete"
    assert progress.state("balanced") == "active"
    assert progress.state("performance") == "pending"

    _tier(session, "tier_completed", "balanced")
    _tier(session, "tier_started", "performance")
    assert session.tier_states["performance"] == "active"
    _tier(session, "tier_completed", "performance")
    progress.show_states(session.tier_states)
    assert [progress.state(tier) for tier in TIERS] == ["complete"] * 3


def test_full_scan_tier_progress_marks_unfinished_profiles(qapp) -> None:
    _qtcore, _qtgui, qtwidgets, _pg = import_qt()
    progress = AutoUvTierProgress(QtWidgets=qtwidgets)
    session = ScanSession()
    session.start({"auto_uv_mode": "adaptive"})
    _tier(session, "tier_completed", "efficiency")
    _tier(session, "tier_started", "balanced")

    session.finish(1, stopped_by_user=False)
    progress.show_states(session.tier_states)

    assert progress.state("efficiency") == "complete"
    assert progress.state("balanced") == "failed"
    assert progress.state("performance") == "not-run"


def test_selected_profile_scan_hides_the_strip(qapp) -> None:
    _qtcore, _qtgui, qtwidgets, _pg = import_qt()
    progress = AutoUvTierProgress(QtWidgets=qtwidgets)
    session = ScanSession()
    session.start({"auto_uv_mode": "balanced"})

    progress.show_states(session.tier_states)

    assert progress.widget.isHidden()
