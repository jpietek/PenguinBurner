import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from profiles.uv.profile_tiers import (
    PROFILE_TIER_BALANCED,
    PROFILE_TIER_NONE,
    PROFILE_TIER_PERFORMANCE,
)
from runtime.frame_history import (
    BOTTLENECK_GPU,
    FrameHistorySummary,
)
from ui import theme
from ui.components.game_perf_profile import (
    AXIS_HIGHER_IS_BETTER,
    AXIS_LOWER_IS_CHEAPER,
    GamePerfProfilePeek,
    axis_label_lines,
    axis_point_color,
    perf_profile_axes,
    perf_profile_pixmap,
    tier_color,
    tier_label,
    warming_text,
)
from ui.qt import import_qt


def _summary(
    *,
    tier: str = PROFILE_TIER_BALANCED,
    bottleneck: str = BOTTLENECK_GPU,
    median_fps: float = 96.0,
    low_fps: float = 71.0,
    power_w: int = 214,
    gpu: int = 92,
    cpu: int = 60,
    latency_ms: float = 22.0,
    display_latency_ms: float = 12.0,
    minutes: float = 30.0,
) -> FrameHistorySummary:
    return FrameHistorySummary(
        minutes=minutes,
        qualified=minutes >= 5.0,
        tier=tier,
        bottleneck=bottleneck,
        median_present_fps=median_fps,
        low_1pct_fps=low_fps,
        median_frametime_ms=1000.0 / median_fps,
        p99_frametime_ms=1000.0 / low_fps,
        median_power_w=power_w,
        median_clock_mhz=2550,
        gpu_util_pct=gpu,
        cpu_peak_thread_pct=cpu,
        latency_ms=latency_ms,
        median_display_latency_ms=display_latency_ms,
        framegen_seen=False,
    )


def test_perf_profile_axes_normalization_math() -> None:
    axes = perf_profile_axes(_summary(), target_fps=120.0, power_limit_w=360)
    by_code = {axis.code: axis for axis in axes}
    # Five spokes — PWR crown, load pair, experience pair; latency stays
    # textual only.
    assert [axis.code for axis in axes] == ["PWR", "GPU", "FPS", "LOW", "CPU"]
    assert abs(by_code["PWR"].fraction - 214 / 360) < 1e-9
    assert by_code["PWR"].text == "214 W"
    assert abs(by_code["GPU"].fraction - 0.92) < 1e-9
    assert abs(by_code["CPU"].fraction - 0.60) < 1e-9
    assert abs(by_code["FPS"].fraction - 96 / 120) < 1e-9
    assert abs(by_code["LOW"].fraction - 71 / 96) < 1e-9
    assert by_code["LOW"].text == "74% of median"


def test_perf_profile_axes_fallbacks_and_clamping() -> None:
    fast = _summary(median_fps=300.0, latency_ms=90.0, power_w=999)
    axes = {a.code: a for a in perf_profile_axes(fast, target_fps=None, power_limit_w=0)}
    # target None -> 60 fps fallback, clamped; power limit 0 -> 350 W fallback
    assert axes["FPS"].fraction == 1.0
    assert axes["PWR"].fraction == 1.0
    assert "LAT" not in axes


def test_perf_profile_axes_carry_reading_direction_and_fine_band() -> None:
    axes = {a.code: a for a in perf_profile_axes(_summary(), target_fps=120.0, power_limit_w=360)}
    # Experience spokes read outward, cost spokes inward; GPU load has no
    # single good reading, so it carries neither.
    assert axes["FPS"].direction == AXIS_HIGHER_IS_BETTER
    assert axes["LOW"].direction == AXIS_HIGHER_IS_BETTER
    assert axes["PWR"].direction == AXIS_LOWER_IS_CHEAPER
    assert axes["CPU"].direction == AXIS_LOWER_IS_CHEAPER
    assert axes["GPU"].direction == ""
    assert axes["GPU"].good_band is None
    assert axes["FPS"].caption == "FPS ↑"
    assert axes["PWR"].caption == "PWR ↓"
    assert axes["GPU"].caption == "GPU"


def test_perf_profile_fine_band_membership_drives_point_color() -> None:
    # A capped 60 fps game with stutter: on target, cheap, but uneven.
    capped = _summary(median_fps=59.0, low_fps=42.0, power_w=116, gpu=77, cpu=19)
    axes = {a.code: a for a in perf_profile_axes(capped, target_fps=None, power_limit_w=0)}
    assert axes["FPS"].in_good_band is True
    assert axes["PWR"].in_good_band is True
    assert axes["CPU"].in_good_band is True
    assert axes["LOW"].in_good_band is False
    assert axes["GPU"].in_good_band is None
    assert axis_point_color(axes["FPS"]) == theme.PERF_PROFILE_POINT_GOOD
    assert axis_point_color(axes["LOW"]) == theme.PERF_PROFILE_POINT_OUTSIDE
    assert axis_point_color(axes["GPU"]) == theme.PERF_PROFILE_POINT_NEUTRAL

    # A CPU-bound title misses its target and pins its hottest thread.
    cpu_bound = _summary(median_fps=48.0, low_fps=40.0, gpu=62, cpu=97)
    axes = {a.code: a for a in perf_profile_axes(cpu_bound, target_fps=60.0, power_limit_w=360)}
    assert axes["FPS"].in_good_band is False
    assert axes["CPU"].in_good_band is False


def test_axis_label_lines_add_the_percentage_only_when_detailed() -> None:
    axes = {a.code: a for a in perf_profile_axes(_summary(), target_fps=120.0, power_limit_w=360)}
    assert axis_label_lines(axes["LOW"], detailed=False) == ("LOW ↑",)
    assert axis_label_lines(axes["LOW"], detailed=True) == ("LOW ↑", "74%")
    assert axis_label_lines(axes["GPU"], detailed=True) == ("GPU", "92%")


def test_tier_colors_use_the_mock_palette() -> None:
    assert tier_color(PROFILE_TIER_BALANCED) == theme.PERF_PROFILE_TIER_BALANCED == "#3987e5"
    assert tier_color(PROFILE_TIER_PERFORMANCE) == "#d95926"
    assert tier_color(PROFILE_TIER_NONE) == theme.PERF_PROFILE_TIER_NONE
    assert tier_color("garbage") == theme.PERF_PROFILE_TIER_NONE
    assert tier_label(PROFILE_TIER_BALANCED) == "Balanced"
    assert tier_label(PROFILE_TIER_NONE) == "Untuned"


def test_warming_text_names_the_gate() -> None:
    assert "5 min" in warming_text(2.4)
    assert "4 of 5 min" in warming_text(4.6)


def _alpha_pixels(pixmap) -> int:
    image = pixmap.toImage()
    return sum(
        1
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() > 0
    )


def test_perf_profile_pixmap_renders_fingerprint_and_warming_states(qapp) -> None:
    QtCore, QtGui, _QtWidgets, _pg = import_qt()
    axes = perf_profile_axes(_summary(), target_fps=120.0, power_limit_w=360)
    badge = perf_profile_pixmap(
        QtCore, QtGui, axes=axes, tier=PROFILE_TIER_BALANCED, size=40
    )
    assert not badge.isNull()
    assert badge.width() == 40
    labeled = perf_profile_pixmap(
        QtCore,
        QtGui,
        axes=axes,
        tier=PROFILE_TIER_BALANCED,
        size=200,
        labels=True,
        device_pixel_ratio=2.0,
    )
    assert labeled.width() == 400  # hi-dpi backing store
    # The labeled variant colors vertex dots by fine-band membership.
    image = labeled.toImage()
    good = QtGui.QColor(theme.PERF_PROFILE_POINT_GOOD)
    assert any(
        image.pixelColor(x, y).green() > 150
        and abs(image.pixelColor(x, y).red() - good.red()) < 40
        and image.pixelColor(x, y).blue() < 160
        for x in range(0, image.width(), 2)
        for y in range(0, image.height(), 2)
    )
    peek_size = perf_profile_pixmap(
        QtCore, QtGui, axes=axes, tier=PROFILE_TIER_BALANCED, size=124, labels=True
    )
    assert not peek_size.isNull()
    warming = perf_profile_pixmap(QtCore, QtGui, axes=None, tier=PROFILE_TIER_NONE, size=40)
    badge_ink = _alpha_pixels(badge)
    warming_ink = _alpha_pixels(warming)
    assert badge_ink > 200  # a filled polygon, not a blank
    assert 0 < warming_ink < badge_ink  # dashed outline only


def test_peek_popover_populates_and_positions(qapp) -> None:
    QtCore, QtGui, QtWidgets, _pg = import_qt()
    peek = GamePerfProfilePeek(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    summary = _summary()
    peek.show_for(
        summary=summary,
        target_fps=120.0,
        power_limit_w=360,
        global_pos=QtCore.QPoint(100, 100),
    )
    try:
        assert peek.widget.isVisible()
        # No title: the badge it hangs off already sits beside the game name.
        assert not hasattr(peek, "title")
        # fps-first, matching the tab's stat row and the mock.
        assert peek._stat_values["Median"].text() == "96 fps · 10.4 ms"
        assert peek._stat_values["1%-low"].text() == "71 fps · 14.1 ms"
        assert peek._stat_values["Power"].text() == "214 W"
        assert "Bottleneck" not in peek._stat_values  # dropped as confusing
        # A real marker latency earns the fifth row.
        assert peek._stat_values["Latency"].text() == "22 ms"
        assert peek._stat_values["Latency"].isVisibleTo(peek.widget)
        assert peek.profile_label.pixmap() is not None
        assert "Game Perf Profile tab" in peek.hint.text()
    finally:
        peek.hide()
    assert not peek.widget.isVisible()


def test_peek_popover_hides_latency_without_a_marker_source(qapp) -> None:
    QtCore, QtGui, QtWidgets, _pg = import_qt()
    peek = GamePerfProfilePeek(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    peek.show_for(
        summary=_summary(latency_ms=0.0, display_latency_ms=8.0),
        target_fps=144.0,
        power_limit_w=360,
        global_pos=QtCore.QPoint(100, 100),
    )
    try:
        # No marker latency -> no latency row at all, never a placeholder.
        assert not peek._stat_values["Latency"].isVisibleTo(peek.widget)
        assert not peek._stat_keys["Latency"].isVisibleTo(peek.widget)
    finally:
        peek.hide()


def test_peek_opens_leftward_when_right_aligned(qapp) -> None:
    QtCore, QtGui, QtWidgets, _pg = import_qt()
    peek = GamePerfProfilePeek(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    anchor = QtCore.QPoint(900, 200)
    peek.show_for(
        summary=_summary(),
        target_fps=144.0,
        power_limit_w=360,
        global_pos=anchor,
        align_right=True,
    )
    try:
        # The popover's right edge meets the anchor: it grows leftward from
        # the corner badge instead of off the window edge.
        right_edge = peek.widget.x() + peek.widget.sizeHint().width()
        assert peek.widget.x() < anchor.x()
        assert abs(right_edge - anchor.x()) <= 8
    finally:
        peek.hide()
