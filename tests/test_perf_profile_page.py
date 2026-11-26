"""Native Perf Profile page, library entry and sample rings (PR #137 port)."""

import os
import random

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W
from PySide6.QtTest import QTest
from ui_samples.perf_rings import GAME_NAMES, LIVE_KEY, SamplePerfSource

from ui.components.perf_entry import (
    PERF_CAPTURE_OFF,
    PERF_UNAVAILABLE,
    PerfEntryButton,
)
from ui.components.perf_fingerprint import perf_profile_axes
from ui.components.perf_profile_plot import (
    LIVE_CAPTION,
    LIVE_REFRESH_MS,
    OVERVIEW_CAPTION,
    REFRESH_MS,
    densify_frametimes,
    frametime_ceiling,
    hitch_points,
    live_tail,
    meta_text,
    percentile,
    y_tick_step,
)
from ui.design.style import install_theme
from ui.features.library.perf_profile_page import (
    EMPTY_HINT,
    PerfProfilePage,
    capture_off_text,
    column_mode,
    no_recording_text,
)
from ui.qt import apply_dark_palette

CYBERPUNK, TLOU2, TLOU1, DOOM = (
    "steam:1091500",
    "steam:2531310",
    "steam:1888930",
    "steam:782330",
)
# The offscreen platform reports what it cannot do; that is not our widget.
PLATFORM_NOISE = ("This plugin does not support",)


# ---------- PR #137 reference (origin/build-pr-137 @ 611e6d3, verbatim) ----------
def _pr137_densify(frametimes_ms, columns=300):
    if not frametimes_ms:
        return (), (), (), ()
    total_ms = sum(frametimes_ms)
    bucket = max(1, len(frametimes_ms) // columns, 200)
    xs, medians, p99s, maxes = [], [], [], []
    elapsed = 0.0
    for start in range(0, len(frametimes_ms), bucket):
        chunk = sorted(frametimes_ms[start : start + bucket])
        elapsed += sum(chunk)
        xs.append((elapsed - total_ms) / 60_000.0)
        medians.append(chunk[len(chunk) // 2])
        p99s.append(chunk[min(len(chunk) - 1, int(len(chunk) * 0.99))])
        maxes.append(chunk[-1])
    return tuple(xs), tuple(medians), tuple(p99s), tuple(maxes)


def _pr137_hitch_points(xs, medians, maxes):
    points = [
        (x, worst)
        for x, median, worst in zip(xs, medians, maxes, strict=True)
        if worst >= max(33.3, median * 2.0)
    ]
    return tuple(x for x, _ in points), tuple(worst for _, worst in points)


def _pr137_live_tail(frametimes_ms, window_ms=10_000.0):
    xs, values = [], []
    elapsed = 0.0
    for frametime in reversed(frametimes_ms):
        elapsed += frametime
        if elapsed > window_ms and values:
            break
        xs.append(-elapsed / 1000.0)
        values.append(frametime)
    xs.reverse()
    values.reverse()
    return tuple(xs), tuple(values)


@pytest.fixture(scope="module")
def app():
    instance = W.QApplication.instance() or W.QApplication([])
    apply_dark_palette(instance, G)
    install_theme(instance)
    return instance


@pytest.fixture
def qt_messages():
    messages = []

    def handler(_mode, _context, message):
        if not message.startswith(PLATFORM_NOISE):
            messages.append(message)

    previous = C.qInstallMessageHandler(handler)
    yield messages
    C.qInstallMessageHandler(previous)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def source(clock):
    return SamplePerfSource(clock=clock)


# ---------- Algorithms ----------
def _random_rings():
    rng = random.Random(137)
    yield ()
    yield (16.7,) * 50
    for count in (199, 200, 201, 18_000, 61_234):
        yield tuple(5 + rng.random() * 40 for _ in range(count))


def test_densify_hitch_and_tail_match_pr137():
    for ring in _random_rings():
        assert densify_frametimes(ring) == _pr137_densify(ring)
        assert densify_frametimes(ring, columns=22) == _pr137_densify(ring, columns=22)
        xs, medians, _p99s, maxes = densify_frametimes(ring)
        assert hitch_points(xs, medians, maxes) == _pr137_hitch_points(
            xs, medians, maxes
        )
        for window in (10_000.0, 30_000.0, 60_000.0):
            assert live_tail(ring, window) == _pr137_live_tail(ring, window)


def test_densify_column_sizes():
    # 30 min at 144 fps: 864 frames a column, 300 columns, newest at x = 0.
    xs, *_ = densify_frametimes((1000 / 144,) * (30 * 60 * 144))
    assert len(xs) == 300
    assert xs[-1] == pytest.approx(0.0)
    # 5 min at 60 fps hits the 200-frame floor: 90 columns.
    assert len(densify_frametimes((1000 / 60,) * (5 * 60 * 60))[0]) == 90
    assert len(densify_frametimes((10.0,) * 50)[0]) == 1
    assert densify_frametimes(()) == ((), (), (), ())


def test_hitch_rule_uses_the_column_median():
    xs = (-3.0, -2.0, -1.0, 0.0)
    medians = (16.7, 10.0, 33.4, 33.4)
    maxes = (18.0, 45.0, 40.0, 90.0)
    # A 60 fps cap or a steady 30 fps game is not a hitch.
    assert hitch_points(xs, medians, maxes) == ((-2.0, 0.0), (45.0, 90.0))


def test_live_tail_window_edge():
    xs, values = live_tail((10.0,) * 2000, 10_000.0)
    assert len(values) in (1000, 1001)
    assert xs[-1] == pytest.approx(-0.01)
    # One frame longer than the window is still shown.
    assert live_tail((250.0,), 100.0) == ((-0.25,), (250.0,))


def test_ceiling_ticks_and_percentile():
    assert frametime_ceiling(11.5, 16.8) == 35.0
    assert frametime_ceiling(6.4, 8.0) == 20.0
    assert [y_tick_step(c) for c in (20, 25, 35, 50, 60)] == [5, 5, 10, 10, 20]
    # Floor indexing: the p99 of 100 frames is the single worst frame.
    assert percentile(sorted(range(100)), 0.99) == 99
    assert percentile([], 0.5) == 0.0


# ---------- Sample source ----------
def test_sample_rings_cover_the_cases(source):
    assert set(GAME_NAMES) >= {CYBERPUNK, TLOU2, TLOU1, DOOM, LIVE_KEY}
    cyberpunk = source.record(CYBERPUNK)
    assert cyberpunk is not None and cyberpunk.summary is not None
    assert meta_text(cyberpunk, cyberpunk.summary) == (
        f"24 min captured · {len(cyberpunk.frametimes_ms):,} frames · frame-gen seen"
    )
    tlou2 = source.record(TLOU2)
    assert tlou2 is not None and tlou2.summary is not None
    assert "settling · fingerprint at 5 min" in meta_text(tlou2, tlou2.summary)
    live = source.record(LIVE_KEY)
    assert live is not None and live.summary is not None
    assert meta_text(live, live.summary).startswith("● REC · ")
    doom = source.record(DOOM)
    assert doom is not None and doom.target_is_game
    # The 60 fps menu stretch in a 158 fps game: the 1% low is the cap.
    assert doom.summary is not None and doom.summary.low_1pct_fps == pytest.approx(
        60, abs=2
    )
    assert source.record(TLOU1) is None and not source.capture_enabled(TLOU1)
    assert source.record("lutris:dishonored") is None


def test_live_ring_grows_one_second_per_second(source, clock):
    first = source.record(LIVE_KEY)
    archived = source.record(CYBERPUNK)
    assert first is not None and archived is not None
    assert source.record(LIVE_KEY) is first
    clock.now += 3.2
    grown = source.record(LIVE_KEY)
    assert grown is not None and grown.version > first.version
    added_ms = sum(grown.frametimes_ms) - sum(first.frametimes_ms)
    assert 3000 <= added_ms < 3100
    assert source.record(CYBERPUNK) is archived
    source.set_running(LIVE_KEY, False)
    stopped = source.record(LIVE_KEY)
    assert stopped is not None and not stopped.live
    clock.now += 5
    assert source.record(LIVE_KEY) is stopped


def test_started_game_records_a_fresh_ring(source, clock):
    assert source.record("lutris:dishonored") is None
    source.set_running("lutris:dishonored", True, tier="Efficiency")
    clock.now += 2
    ring = source.record("lutris:dishonored")
    assert ring is not None and ring.live and ring.tier == "Efficiency"
    source.set_running(TLOU1, True)
    assert source.record(TLOU1) is None


def test_fresh_ring_starts_empty_on_a_late_clock(source, clock):
    # The session clock starts at Play: no catch-up of the machine's uptime.
    source.set_running("lutris:dishonored", True, tier="Balanced")
    clock.now += 1.5
    ring = source.record("lutris:dishonored")
    assert ring is not None and ring.live
    assert sum(ring.frametimes_ms) < 2_500


def test_only_a_tier_session_with_capture_on_resumes_a_ring(source, clock):
    archived = source.record(CYBERPUNK)
    assert archived is not None and not archived.live
    source.set_running(CYBERPUNK, True, tier="")  # Stock or unwrapped
    clock.now += 30
    assert source.record(CYBERPUNK) is archived
    source.set_capture(CYBERPUNK, False)
    source.set_running(CYBERPUNK, True, tier="Balanced")
    assert not source.record(CYBERPUNK).live
    source.set_capture(CYBERPUNK, True)
    source.set_running(CYBERPUNK, True, tier="Performance")
    live = source.record(CYBERPUNK)
    assert live.live and live.tier == "Performance"


# ---------- Page ----------
def _page(app, source, width=1350, height=900):
    page = PerfProfilePage(source, on_back=lambda: None)
    page.resize(width, height)
    page.show()
    QTest.qWait(20)
    return page


def test_page_builds_for_every_sample_game(app, source, qt_messages):
    page = _page(app, source)
    try:
        for key in [*GAME_NAMES, ""]:
            page.show_game(key)
            page.figure.set_live(True)
            page.figure.set_live(False)
            QTest.qWait(5)
            recording = source.record(key) if key else None
            if not key:
                assert page.state == "nogame" and page.message.text() == EMPTY_HINT
            elif recording is None and not source.capture_enabled(key):
                assert page.state == "captureoff"
                assert page.message.text() == capture_off_text(GAME_NAMES[key])
            elif recording is None:
                assert page.state == "norecord"
                assert page.message.text() == no_recording_text(GAME_NAMES[key])
            else:
                assert page.state == "detail"
                assert page.title.text() == GAME_NAMES[key]
        assert qt_messages == []
    finally:
        page.close()


def test_overview_figure_and_cards(app, source):
    page = _page(app, source)
    try:
        page.show_game(CYBERPUNK)
        figure = page.figure
        assert figure.mode == "overview"
        assert (
            figure.legend.caption
            == f"{OVERVIEW_CAPTION} · worst frame 182 ms (clipped)"
        )
        assert figure.hitch_count > 5
        assert page.tier_chip.text() == "Balanced"
        assert "28 ms" in page.latency_row.value.text() and page.latency_row.isVisible()
        assert page.fingerprint.axes is not None and page.dot_key.isVisible()
        assert "Power draw: 224 W" in page.fingerprint.toolTip()
        # An archived ring under 5 minutes keeps the dashed outline.
        page.show_game(TLOU2)
        assert page.fingerprint.axes is None
        assert page.fingerprint.toolTip() == "Warming up · 3 of 5 min captured"
        assert not page.latency_row.isVisible()
        # A live ring draws its fingerprint early.
        page.show_game(LIVE_KEY)
        assert page.fingerprint.axes is not None
        assert "● REC" in page.meta.text()
    finally:
        page.close()


def test_live_mode_window_and_cadence(app, source, clock):
    page = _page(app, source)
    try:
        page.show_game(LIVE_KEY)
        assert page.timer.isActive() and page.timer.interval() == REFRESH_MS
        assert not page.figure.window_combo.isVisible()
        page.figure.live_switch.click()
        assert page.figure.mode == "live" and page.figure.legend.caption.startswith(
            LIVE_CAPTION
        )
        assert page.figure.window_combo.isVisible()
        assert page.timer.interval() == LIVE_REFRESH_MS
        frames = len(page.recording.frametimes_ms) if page.recording else 0
        clock.now += 2
        page._tick()
        assert page.recording is not None and len(page.recording.frametimes_ms) > frames
        page.figure.set_window(60)
        assert page.figure.window_s == 60
        # An archived ring never gets the fast tick.
        page.show_game(CYBERPUNK)
        assert not page.figure.live
        page.figure.set_live(True)
        assert page.timer.interval() == REFRESH_MS
        page.hide()
        assert not page.timer.isActive()
    finally:
        page.close()


def test_probe_reads_values_by_keyboard(app, source):
    page = _page(app, source)
    try:
        page.show_game(CYBERPUNK)
        figure = page.figure
        figure._probe_key("end")
        assert figure.probe_text().startswith("0.0 min ago · median ")
        figure._probe_key("home")
        assert figure.probe_text() != ""
        figure._probe_key("escape")
        assert figure.probe_text() == ""
    finally:
        page.close()


def _position(grid, index) -> tuple[int, int, int, int]:
    """(row, column, row span, column span); PySide types it as object."""
    row, column, rows, columns = grid.getItemPosition(index)  # type: ignore[misc]
    return int(row), int(column), int(rows), int(columns)


def test_cards_reflow_three_two_one(app, source):
    assert [column_mode(w) for w in (1300, 960, 959, 720, 719)] == [3, 3, 2, 2, 1]
    page = _page(app, source)
    try:
        page.show_game(CYBERPUNK)
        grid = page.grid
        for width, expected in ((1300, (1, 1)), (800, (2, 1)), (600, (3, 1))):
            page.reflow(width)
            index = grid.indexOf(page.figure_card)
            row = _position(grid, index)[0]
            assert (row, grid.rowStretch(row)) == expected
        page.reflow(800)
        assert _position(grid, grid.indexOf(page.fingerprint_card))[2] == 2
    finally:
        page.close()


def test_back_runs_the_callback(app, source):
    calls = []
    page = PerfProfilePage(source, on_back=lambda: calls.append(1))
    page.show_game(CYBERPUNK)
    page.back_button.click()
    assert calls == [1]
    page.close()


def test_axes_bands():
    source = SamplePerfSource()
    recording = source.record(CYBERPUNK)
    assert recording is not None and recording.summary is not None
    axes = perf_profile_axes(recording.summary, target_fps=60, power_limit_w=360)
    assert [a.caption for a in axes] == ["PWR ↓", "GPU", "FPS ↑", "LOW ↑", "CPU ↓"]
    assert [a.in_good_band for a in axes] == [False, None, True, False, True]
    assert axes[3].spoke_text == "69%"


# ---------- Library entry ----------
def _pixels(pixmap):
    image = pixmap.toImage().convertToFormat(G.QImage.Format.Format_ARGB32)
    return bytes(image.constBits())




def _focus(widget, reason):
    """Deliver focus with a reason; offscreen windows are never activated."""
    W.QApplication.sendEvent(widget, G.QFocusEvent(C.QEvent.Type.FocusOut, reason))
    W.QApplication.sendEvent(widget, G.QFocusEvent(C.QEvent.Type.FocusIn, reason))


def test_entry_button_states_peek_and_open(app, source):
    host = W.QWidget()
    layout = W.QVBoxLayout(host)
    button = PerfEntryButton(source)
    layout.addWidget(button)
    other = W.QPushButton("Play")
    layout.addWidget(other)
    host.show()
    opened = []
    button.open_requested.connect(opened.append)
    try:
        assert not button.isVisible()
        button.sync("lutris:dishonored")
        assert button.property("perfState") == "empty" and button.isEnabled()
        assert button.accessibleDescription() == PERF_UNAVAILABLE
        button.click()
        assert opened == []
        button.sync(TLOU1)
        assert button.accessibleDescription() == PERF_CAPTURE_OFF
        button.show_peek()
        assert (
            button.peek.isVisible() and button.peek.message.text() == PERF_CAPTURE_OFF
        )
        assert not button.peek.body.isVisible()
        button.hide_peek()

        button.sync(CYBERPUNK)
        assert button.property("perfState") == "ready"
        assert button.iconSize().width() == 16  # the chart glyph
        # Keyboard focus peeks; Esc closes it.
        _focus(button, C.Qt.FocusReason.TabFocusReason)
        assert button.peek.isVisible()
        assert button.peek.values["Median"].text().endswith("ms")
        assert button.peek.keys["Latency"].isVisibleTo(button.peek)
        QTest.keyClick(button, C.Qt.Key.Key_Escape)
        assert not button.peek.isVisible()
        # Focus restored after Back does not peek or ring.
        _focus(button, C.Qt.FocusReason.OtherFocusReason)
        assert not button.peek.isVisible() and button.property("keyboardFocus") is False
        button.click()
        assert opened == [CYBERPUNK]

        button.sync(TLOU2)
        button.show_peek()
        assert button.peek.message.text() == "Warming up · 3 of 5 min captured"
        assert not button.peek.keys["Latency"].isVisibleTo(button.peek)
        button.sync(LIVE_KEY)
        assert "Recording now." in button.accessibleDescription()
    finally:
        button.hide_peek()
        host.close()
