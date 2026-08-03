import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from profiles.uv.profile_tiers import (
    PROFILE_TIER_EFFICIENCY,
    PROFILE_TIER_PERFORMANCE,
)
from runtime.frame_history import (
    FRAME_HISTORY_ARCHIVE_DIR_ENV,
    FRAME_HISTORY_LIVE_DIR_ENV,
    FRAME_HISTORY_SYSTEM_ARCHIVE_DIR_ENV,
    MetricsSample,
    write_frame_history,
)
from ui import theme
from ui.components.game_perf_profile_panel import (
    GamePerfProfilePanel,
    densify_frametimes,
    live_tail,
)
from ui.qt import import_qt


def _sample(
    t_rel_s: int,
    *,
    tier: str = PROFILE_TIER_PERFORMANCE,
    latency_ms: float = 34.0,
    display_latency_ms: float = 19.0,
) -> MetricsSample:
    return MetricsSample(
        t_rel_s=t_rel_s,
        frame_count=78,
        clock_mhz=2610,
        mem_clock_mhz=10251,
        voltage_mv=1010,
        power_w=298,
        gpu_util_pct=99,
        cpu_util_pct=40,
        cpu_peak_thread_pct=66,
        fan_pct=63,
        temperature_c=71,
        uv_offset_mv=-120,
        present_fps=78.0,
        framegen_fps=156.0,
        latency_ms=latency_ms,
        display_latency_ms=display_latency_ms,
        framegen_active=True,
        adaptive=False,
        fps_source=2,
        tier=tier,
        ft_p50_ms=12.8,
        ft_p99_ms=19.2,
        ft_p999_ms=42.0,
    )


def _write_ring(
    tmp_path: Path,
    app_id: int,
    *,
    seconds: int = 360,
    latency_ms: float = 34.0,
    display_latency_ms: float = 19.0,
    live: bool = True,
) -> dict[str, str]:
    env = {
        FRAME_HISTORY_LIVE_DIR_ENV: str(tmp_path / "live"),
        FRAME_HISTORY_ARCHIVE_DIR_ENV: str(tmp_path / "arch"),
        FRAME_HISTORY_SYSTEM_ARCHIVE_DIR_ENV: str(tmp_path / "sys-arch"),
    }
    frametimes = []
    for index in range(seconds * 78):
        frametimes.append(30.0 if index % 50 == 49 else 12.8)  # 2% stutter spikes
    target = (
        tmp_path / "live" / f"{app_id}.ring" if live
        else tmp_path / "arch" / f"{app_id}.ring.gz"
    )
    assert write_frame_history(
        target,
        samples=[
            _sample(
                i,
                latency_ms=latency_ms,
                display_latency_ms=display_latency_ms,
            )
            for i in range(seconds)
        ],
        frametimes_ms=frametimes,
        app_id=app_id,
        pid=4242,
        power_limit_w=360,
        started_unix=1_784_000_000,
        frame_cap=1 << 16,
    )
    return env


def test_densify_preserves_spikes_and_time_axis() -> None:
    frametimes = tuple(10.0 if i != 1500 else 80.0 for i in range(3000))
    xs, means, maxes = densify_frametimes(frametimes, columns=100)
    assert len(xs) == len(means) == len(maxes) == 100
    assert xs[-1] == 0.0  # newest column ends at "now"
    assert xs[0] < xs[-1] <= 0.0
    assert max(maxes) == 80.0  # the spike survives densification
    assert max(means) < 80.0  # ...while the mean smooths it
    assert densify_frametimes(()) == ((), (), ())


def test_live_tail_returns_the_last_ten_seconds() -> None:
    frametimes = tuple([10.0] * 2000)  # 20 s of 100 fps
    xs, values = live_tail(frametimes)
    assert len(values) in (1000, 1001)  # ~10 s at 10 ms/frame
    assert xs[-1] == -0.01
    assert min(xs) >= -10.05
    assert live_tail(()) == ((), ())


def test_panel_placeholder_without_pyqtgraph(qapp) -> None:
    QtCore, QtGui, QtWidgets, _pg = import_qt()
    panel = GamePerfProfilePanel(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, pg=None)
    text = panel.widget.findChild(QtWidgets.QPlainTextEdit)
    assert text is not None and "pyqtgraph" in text.toPlainText()
    panel.refresh()  # must be a no-op, not a crash


def _make_panel(env):
    QtCore, QtGui, QtWidgets, pg = import_qt()
    if pg is None:
        pytest.skip("pyqtgraph not available")
    return GamePerfProfilePanel(
        QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, pg=pg, history_env=env
    )


def test_panel_empty_and_missing_states(qtbot, tmp_path: Path) -> None:
    env = {
        FRAME_HISTORY_LIVE_DIR_ENV: str(tmp_path / "live"),
        FRAME_HISTORY_ARCHIVE_DIR_ENV: str(tmp_path / "arch"),
        FRAME_HISTORY_SYSTEM_ARCHIVE_DIR_ENV: str(tmp_path / "sys-arch"),
    }
    panel = _make_panel(env)
    qtbot.addWidget(panel.widget)
    try:
        assert panel._stack.currentIndex() == 0
        panel.refresh()
        assert "Select a game" in panel.empty_label.text()
        panel.select_app("999", game_name="Ghost Game")
        assert panel._stack.currentIndex() == 0
        assert "No telemetry captured for Ghost Game" in panel.empty_label.text()
    finally:
        panel._refresh_timer.stop()


def test_panel_warming_state_under_five_minutes(qtbot, tmp_path: Path) -> None:
    # A short *archived* session is over: no more data is coming, so the gate
    # still says the fingerprint never settled.
    env = _write_ring(tmp_path, 4242, seconds=120, live=False)
    panel = _make_panel(env)
    qtbot.addWidget(panel.widget)
    try:
        panel.select_app("4242", game_name="INDIKA")
        assert panel._stack.currentIndex() == 0
        assert "Warming up" in panel.empty_label.text()
        assert "2 of 5 min" in panel.empty_label.text()
    finally:
        panel._refresh_timer.stop()


def test_panel_shows_a_live_session_before_the_qualify_gate(
    qtbot, tmp_path: Path
) -> None:
    # The game is on right now: show the frametimes as they land instead of
    # hiding five minutes of real play behind "Warming up".
    env = _write_ring(tmp_path, 4242, seconds=120)
    panel = _make_panel(env)
    qtbot.addWidget(panel.widget)
    try:
        panel.select_app("4242", game_name="INDIKA")
        assert panel._stack.currentIndex() == 1  # detail, not the gate message
        assert panel.frametime_plot is not None
        meta = panel.meta_label.text()
        assert "● REC" in meta
        assert "2.0 min captured" in meta
        assert "fingerprint at 5 min" in meta  # honest about the short window
    finally:
        panel._refresh_timer.stop()


def test_live_zoom_buys_1hz_and_the_overview_stays_at_2s(
    qtbot, tmp_path: Path
) -> None:
    # Each refresh decodes the whole window, so the faster tick is only worth
    # it where a second of play is visible.
    env = _write_ring(tmp_path, 4242, seconds=120)
    panel = _make_panel(env)
    qtbot.addWidget(panel.widget)
    try:
        panel.select_app("4242", game_name="INDIKA")
        assert panel._refresh_timer.interval() == 2000
        panel.live_toggle.setChecked(True)
        assert panel._refresh_timer.interval() == 1000
        panel.live_toggle.setChecked(False)
        assert panel._refresh_timer.interval() == 2000
    finally:
        panel._refresh_timer.stop()


def test_archived_session_never_buys_the_fast_tick(qtbot, tmp_path: Path) -> None:
    env = _write_ring(tmp_path, 4242, seconds=360, live=False)
    panel = _make_panel(env)
    qtbot.addWidget(panel.widget)
    try:
        panel.select_app("4242", game_name="INDIKA")
        panel.live_toggle.setChecked(True)
        # Dead data cannot advance; 1 Hz would redraw the same picture.
        assert panel._refresh_timer.interval() == 2000
    finally:
        panel._refresh_timer.stop()


def test_panel_populates_detail_from_a_qualified_ring(qtbot, tmp_path: Path) -> None:
    env = _write_ring(tmp_path, 3764200, seconds=360)
    panel = _make_panel(env)
    qtbot.addWidget(panel.widget)
    try:
        panel.select_app("3764200", game_name="Resident Evil 9", target_fps=120.0)
        assert panel._stack.currentIndex() == 1
        assert panel.game_title.text() == "Resident Evil 9"
        assert panel.tier_chip.text() == "PERFORMANCE"
        assert "6.0 min captured" in panel.meta_label.text()
        assert "fingerprint at 5 min" not in panel.meta_label.text()
        assert "frame-gen seen" in panel.meta_label.text()
        assert panel._stat_values["POWER"].text() == "298 W"
        assert panel._stat_values["GPU / CPU-THREAD"].text() == "99% / 66%"
        assert panel._stat_values["CLOCK"].text() == "2610 MHz"
        assert panel._stat_values["TEMP"].text() == "71 °C"
        assert panel._stat_values["FAN"].text() == "63%"
        assert "BOTTLENECK" not in panel._stat_values  # dropped as confusing
        assert "78 fps" in panel._stat_values["MEDIAN"].text()
        assert panel.profile_label.pixmap() is not None

        # overview plot has densified data + stutter needles + reference lines
        xs, _ys = panel.frametime_curve.getData()
        assert xs is not None and len(xs) > 100
        assert panel.stutter_bars.opts["x"] is not None
        assert len(panel.stutter_bars.opts["x"]) > 0
        assert abs(panel.median_line.value() - 12.8) < 0.2
        assert panel.p99_line.value() > panel.median_line.value()

        # the trace stays balanced-blue even though this ring is performance
        assert (
            panel.frametime_curve.opts["pen"].color().name()
            == theme.PERF_PROFILE_TIER_BALANCED
        )
        # the latency cell reports the measured marker latency
        assert panel._latency_cell.isVisibleTo(panel.widget)
        assert panel._latency_key.text() == "LATENCY"
        assert panel._stat_values["LATENCY"].text() == "34 ms"
        # the y axis is pinned to a zero floor
        assert panel.frametime_plot.getViewBox().viewRange()[1][0] == 0.0

        # live zoom re-renders at per-frame resolution
        panel.live_toggle.setChecked(True)
        live_xs, _live_ys = panel.frametime_curve.getData()
        assert live_xs is not None
        assert min(live_xs) >= -10.5  # seconds, not minutes
        panel.live_toggle.setChecked(False)
    finally:
        panel._refresh_timer.stop()


def test_panel_latency_cell_never_shows_the_display_tail(
    qtbot, tmp_path: Path
) -> None:
    # A recorded present→scanout tail with no marker latency stays data-only:
    # a bare scanout time displayed as latency reads as garbage.
    env = _write_ring(tmp_path, 3764200, latency_ms=0.0, display_latency_ms=19.0)
    panel = _make_panel(env)
    qtbot.addWidget(panel.widget)
    try:
        panel.select_app("3764200", game_name="Resident Evil 9", target_fps=120.0)
        assert panel._stack.currentIndex() == 1
        assert not panel._latency_cell.isVisibleTo(panel.widget)
    finally:
        panel._refresh_timer.stop()


def test_panel_latency_cell_hidden_without_any_source(qtbot, tmp_path: Path) -> None:
    env = _write_ring(tmp_path, 3764200, latency_ms=0.0, display_latency_ms=0.0)
    panel = _make_panel(env)
    qtbot.addWidget(panel.widget)
    try:
        panel.select_app("3764200", game_name="Resident Evil 9", target_fps=120.0)
        assert panel._stack.currentIndex() == 1
        assert not panel._latency_cell.isVisibleTo(panel.widget)
    finally:
        panel._refresh_timer.stop()


def test_panel_reads_archived_rings_too(qtbot, tmp_path: Path) -> None:
    env = {
        FRAME_HISTORY_LIVE_DIR_ENV: str(tmp_path / "live"),
        FRAME_HISTORY_ARCHIVE_DIR_ENV: str(tmp_path / "arch"),
        FRAME_HISTORY_SYSTEM_ARCHIVE_DIR_ENV: str(tmp_path / "sys-arch"),
    }
    assert write_frame_history(
        tmp_path / "arch" / "1089130.ring.gz",
        samples=[_sample(i, tier=PROFILE_TIER_EFFICIENCY) for i in range(400)],
        frametimes_ms=[12.8] * 2000,
        app_id=1089130,
        power_limit_w=360,
        frame_cap=1 << 12,
    )
    panel = _make_panel(env)
    qtbot.addWidget(panel.widget)
    try:
        panel.select_app("1089130", game_name="Quake II RTX")
        assert panel._stack.currentIndex() == 1
        assert panel.tier_chip.text() == "EFFICIENCY"
    finally:
        panel._refresh_timer.stop()


def test_live_window_dropdown_switches_the_tail_span(qtbot, tmp_path: Path) -> None:
    env = _write_ring(tmp_path, 3764200, seconds=360)
    panel = _make_panel(env)
    qtbot.addWidget(panel.widget)
    try:
        panel.select_app("3764200", game_name="Resident Evil 9", target_fps=120.0)
        assert [
            panel.live_window_combo.itemData(i)
            for i in range(panel.live_window_combo.count())
        ] == [10, 30, 60]

        panel.live_toggle.setChecked(True)
        xs, _ys = panel.frametime_curve.getData()
        assert min(xs) >= -10.5  # default 10 s tail

        panel.live_window_combo.setCurrentIndex(
            panel.live_window_combo.findData(60)
        )
        xs, _ys = panel.frametime_curve.getData()
        assert min(xs) >= -60.5
        assert min(xs) < -30.0  # genuinely widened beyond the old span
        panel.live_toggle.setChecked(False)
    finally:
        panel._refresh_timer.stop()
