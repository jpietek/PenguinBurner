"""Frametime algorithms behind the Perf Profile page
(ui/components/perf_profile_plot.py)."""

from ui.components.perf_profile_plot import densify_frametimes, hitch_points, live_tail


def test_densify_preserves_spikes_and_time_axis() -> None:
    frametimes = tuple(10.0 if i != 1500 else 80.0 for i in range(3000))
    # 300 frames a column, so one hitch sits beyond the column's p99.
    xs, medians, p99s, maxes = densify_frametimes(frametimes, columns=10)
    assert len(xs) == len(medians) == len(p99s) == len(maxes) == 10
    assert xs[-1] == 0.0  # newest column ends at "now"
    assert xs[0] < xs[-1] <= 0.0
    assert max(maxes) == 80.0  # the spike survives densification
    assert max(medians) == max(p99s) == 10.0  # ...while the ribbon ignores it
    assert densify_frametimes(()) == ((), (), (), ())


def test_densify_keeps_enough_frames_per_column_for_a_real_p99() -> None:
    # Five minutes at 60 fps: 300 columns would hold 60 frames each, where a
    # p99 is just the worst frame and every hitch would stretch the band.
    frametimes = tuple(16.7 if i % 500 else 120.0 for i in range(18_000))
    xs, _medians, p99s, maxes = densify_frametimes(frametimes)
    assert len(xs) == 90
    assert max(p99s) == 16.7
    assert max(maxes) == 120.0
    assert len(densify_frametimes((10.0,) * 50)[0]) == 1


def test_hitches_are_over_33_ms_and_twice_their_column_median() -> None:
    xs = (-3.0, -2.0, -1.0, 0.0)
    # 60 fps cap in a faster game, a real hitch, a 30 fps game's normal
    # frame, and that 30 fps game hitching.
    medians = (16.7, 10.0, 33.4, 33.4)
    maxes = (18.0, 45.0, 40.0, 90.0)
    assert hitch_points(xs, medians, maxes) == ((-2.0, 0.0), (45.0, 90.0))


def test_live_tail_returns_the_last_ten_seconds() -> None:
    frametimes = tuple([10.0] * 2000)  # 20 s of 100 fps
    xs, values = live_tail(frametimes)
    assert len(values) in (1000, 1001)  # ~10 s at 10 ms/frame
    assert xs[-1] == -0.01
    assert min(xs) >= -10.05
    assert live_tail(()) == ((), ())
