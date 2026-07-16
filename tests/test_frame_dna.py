import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from profiles.uv.profile_tiers import (
    PROFILE_TIER_BALANCED,
    PROFILE_TIER_NONE,
    PROFILE_TIER_PERFORMANCE,
)
from runtime.frame_history import (
    BOTTLENECK_CPU,
    BOTTLENECK_GPU,
    BOTTLENECK_MIXED,
    FrameHistorySummary,
)
from ui import theme
from ui.components.frame_dna import (
    FrameDnaPeek,
    bottleneck_label,
    dna_axes,
    dna_pixmap,
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


def test_dna_axes_normalization_math() -> None:
    axes = dna_axes(_summary(), target_fps=120.0, power_limit_w=360)
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


def test_dna_axes_fallbacks_and_clamping() -> None:
    fast = _summary(median_fps=300.0, latency_ms=90.0, power_w=999)
    axes = {a.code: a for a in dna_axes(fast, target_fps=None, power_limit_w=0)}
    # target None -> 60 fps fallback, clamped; power limit 0 -> 350 W fallback
    assert axes["FPS"].fraction == 1.0
    assert axes["PWR"].fraction == 1.0
    assert "LAT" not in axes


def test_tier_colors_use_the_mock_palette() -> None:
    assert tier_color(PROFILE_TIER_BALANCED) == theme.DNA_TIER_BALANCED == "#3987e5"
    assert tier_color(PROFILE_TIER_PERFORMANCE) == "#d95926"
    assert tier_color(PROFILE_TIER_NONE) == theme.DNA_TIER_NONE
    assert tier_color("garbage") == theme.DNA_TIER_NONE
    assert tier_label(PROFILE_TIER_BALANCED) == "Balanced"
    assert tier_label(PROFILE_TIER_NONE) == "Untuned"


def test_bottleneck_labels() -> None:
    assert bottleneck_label(BOTTLENECK_GPU) == "GPU-bound"
    assert bottleneck_label(BOTTLENECK_CPU) == "CPU-bound"
    assert bottleneck_label(BOTTLENECK_MIXED) == "Mixed"
    assert "5 min" in warming_text(2.4)


def _alpha_pixels(pixmap) -> int:
    image = pixmap.toImage()
    return sum(
        1
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() > 0
    )


def test_dna_pixmap_renders_fingerprint_and_warming_states(qapp) -> None:
    QtCore, QtGui, _QtWidgets, _pg = import_qt()
    axes = dna_axes(_summary(), target_fps=120.0, power_limit_w=360)
    badge = dna_pixmap(
        QtCore, QtGui, axes=axes, tier=PROFILE_TIER_BALANCED, size=40
    )
    assert not badge.isNull()
    assert badge.width() == 40
    labeled = dna_pixmap(
        QtCore,
        QtGui,
        axes=axes,
        tier=PROFILE_TIER_BALANCED,
        size=200,
        labels=True,
        device_pixel_ratio=2.0,
    )
    assert labeled.width() == 400  # hi-dpi backing store
    warming = dna_pixmap(QtCore, QtGui, axes=None, tier=PROFILE_TIER_NONE, size=40)
    badge_ink = _alpha_pixels(badge)
    warming_ink = _alpha_pixels(warming)
    assert badge_ink > 200  # a filled polygon, not a blank
    assert 0 < warming_ink < badge_ink  # dashed outline only


def test_peek_popover_populates_and_positions(qapp) -> None:
    QtCore, QtGui, QtWidgets, _pg = import_qt()
    peek = FrameDnaPeek(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    summary = _summary()
    peek.show_for(
        game_name="Baldur's Gate 3",
        summary=summary,
        target_fps=120.0,
        power_limit_w=360,
        global_pos=QtCore.QPoint(100, 100),
    )
    try:
        assert peek.widget.isVisible()
        assert peek.title.text() == "Baldur's Gate 3 · Frame DNA"
        # fps-first, matching the tab's stat row and the mock.
        assert peek._stat_values["Median"].text() == "96 fps · 10.4 ms"
        assert peek._stat_values["1%-low"].text() == "71 fps · 14.1 ms"
        assert peek._stat_values["Power"].text() == "214 W"
        assert peek._stat_values["Bottleneck"].text() == "GPU-bound"
        # A real marker latency earns the fifth row.
        assert peek._stat_values["Latency"].text() == "22 ms"
        assert peek._stat_values["Latency"].isVisibleTo(peek.widget)
        assert peek.dna_label.pixmap() is not None
        assert "Frame DNA tab" in peek.hint.text()
    finally:
        peek.hide()
    assert not peek.widget.isVisible()


def test_peek_popover_hides_latency_without_a_marker_source(qapp) -> None:
    QtCore, QtGui, QtWidgets, _pg = import_qt()
    peek = FrameDnaPeek(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
    peek.show_for(
        game_name="Hades II",
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
