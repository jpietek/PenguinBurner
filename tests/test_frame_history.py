import gzip
import struct
from pathlib import Path

from profiles.uv.profile_tiers import (
    PROFILE_TIER_BALANCED,
    PROFILE_TIER_EFFICIENCY,
    PROFILE_TIER_NONE,
    PROFILE_TIER_PERFORMANCE,
)
from runtime.frame_history import (
    BOTTLENECK_CPU,
    BOTTLENECK_GPU,
    BOTTLENECK_MIXED,
    FRAME_HISTORY_ARCHIVE_DIR_ENV,
    FRAME_HISTORY_LIVE_DIR_ENV,
    FRAME_HISTORY_MAGIC,
    FRAME_TIME_MAX_MS,
    HEADER_SIZE,
    RECORD_SIZE,
    FrameHistoryHeader,
    MetricsSample,
    archive_frame_history,
    decode_frametime_ms,
    encode_frametime_ms,
    frame_history_archive_dir,
    frame_history_live_dir,
    nibble_from_tier,
    pack_header,
    pack_metrics_sample,
    read_frame_history,
    read_frame_history_for_app,
    summarize,
    tier_from_nibble,
    unpack_metrics_sample,
    write_frame_history,
)


def _sample(
    t_rel_s: int = 0,
    *,
    tier: str = PROFILE_TIER_BALANCED,
    present_fps: float = 96.0,
    gpu_util_pct: int = 92,
    cpu_peak_thread_pct: int = 60,
    power_w: int = 214,
    ft_p50_ms: float = 10.4,
    ft_p99_ms: float = 14.1,
    framegen_active: bool = False,
) -> MetricsSample:
    return MetricsSample(
        t_rel_s=t_rel_s,
        frame_count=round(present_fps),
        clock_mhz=2550,
        mem_clock_mhz=10251,
        voltage_mv=965,
        power_w=power_w,
        gpu_util_pct=gpu_util_pct,
        cpu_util_pct=34,
        cpu_peak_thread_pct=cpu_peak_thread_pct,
        fan_pct=52,
        temperature_c=66,
        uv_offset_mv=-135,
        present_fps=present_fps,
        framegen_fps=present_fps * 2 if framegen_active else 0.0,
        latency_ms=22.4,
        display_latency_ms=12.1,
        framegen_active=framegen_active,
        adaptive=True,
        fps_source=2,
        tier=tier,
        ft_p50_ms=ft_p50_ms,
        ft_p99_ms=ft_p99_ms,
        ft_p999_ms=ft_p99_ms * 1.6,
    )


def test_struct_sizes_match_the_format_contract() -> None:
    assert HEADER_SIZE == 64
    assert RECORD_SIZE == 36


def test_frametime_companding_is_accurate_and_monotonic() -> None:
    for frametime in (1.0, 2.5, 4.17, 6.94, 16.7, 33.3, 63.9, 120.0, 250.0):
        decoded = decode_frametime_ms(encode_frametime_ms(frametime))
        assert abs(decoded - frametime) / frametime <= 0.023
    encoded = [encode_frametime_ms(ft / 10) for ft in range(10, 2500, 7)]
    assert encoded == sorted(encoded)


def test_frametime_companding_clamps_the_extremes() -> None:
    assert decode_frametime_ms(encode_frametime_ms(0.2)) == 1.0
    assert encode_frametime_ms(999.0) == 255
    assert abs(decode_frametime_ms(255) - FRAME_TIME_MAX_MS) < 1e-9


def test_tier_nibble_roundtrip() -> None:
    for nibble, tier in (
        (0, PROFILE_TIER_NONE),
        (1, PROFILE_TIER_EFFICIENCY),
        (2, PROFILE_TIER_BALANCED),
        (3, PROFILE_TIER_PERFORMANCE),
    ):
        assert tier_from_nibble(nibble) == tier
        assert nibble_from_tier(tier) == nibble
    assert nibble_from_tier("perf") == 3  # alias-normalized
    assert tier_from_nibble(9) == PROFILE_TIER_NONE


def test_metrics_sample_pack_unpack_roundtrip() -> None:
    sample = _sample(7, tier=PROFILE_TIER_PERFORMANCE, framegen_active=True)
    decoded = unpack_metrics_sample(pack_metrics_sample(sample), 0)
    assert decoded.t_rel_s == 7
    assert decoded.clock_mhz == 2550
    assert decoded.uv_offset_mv == -135
    assert decoded.tier == PROFILE_TIER_PERFORMANCE
    assert decoded.framegen_active is True
    assert decoded.adaptive is True
    assert decoded.fps_source == 2
    assert abs(decoded.present_fps - sample.present_fps) <= 0.05
    assert abs(decoded.latency_ms - sample.latency_ms) <= 0.025
    assert abs(decoded.ft_p99_ms - sample.ft_p99_ms) <= 0.025


def test_write_read_roundtrip_and_qualify_gate(tmp_path: Path) -> None:
    path = tmp_path / "1234.ring"
    samples = [_sample(i) for i in range(299)]
    frametimes = [10.4] * 500
    assert write_frame_history(
        path,
        samples=samples,
        frametimes_ms=frametimes,
        app_id=1234,
        pid=4242,
        power_limit_w=360,
        metrics_cap=400,
        frame_cap=1024,
        started_unix=1_784_000_000,
    )
    history = read_frame_history(path)
    assert history is not None
    assert history.header.app_id == 1234
    assert history.header.pid == 4242
    assert history.header.power_limit_w == 360
    assert history.header.started_unix == 1_784_000_000
    assert len(history.samples) == 299
    assert len(history.frametimes_ms) == 500
    assert history.samples[0].t_rel_s == 0
    assert history.samples[-1].t_rel_s == 298
    assert not history.qualified  # 299 s < 5 min

    samples.append(_sample(299))
    assert write_frame_history(
        path, samples=samples, frametimes_ms=frametimes, metrics_cap=400, frame_cap=1024
    )
    requalified = read_frame_history(path)
    assert requalified is not None and requalified.qualified


def test_writer_keeps_only_the_newest_cap_entries(tmp_path: Path) -> None:
    path = tmp_path / "trim.ring"
    assert write_frame_history(
        path,
        samples=[_sample(i) for i in range(50)],
        frametimes_ms=[float(i % 30 + 1) for i in range(100)],
        metrics_cap=20,
        frame_cap=40,
    )
    history = read_frame_history(path)
    assert history is not None
    assert [s.t_rel_s for s in history.samples] == list(range(30, 50))
    assert len(history.frametimes_ms) == 40


def test_reader_unwraps_a_wrapped_ring_chronologically(tmp_path: Path) -> None:
    cap = 6
    header = FrameHistoryHeader(
        version=1,
        app_id=1,
        pid=1,
        gpu_index=0,
        power_limit_w=360,
        max_boost_mhz=0,
        sample_hz=1,
        window_s=1800,
        metrics_cap=cap,
        metrics_head=2,  # slots 2..7%cap are oldest..newest
        metrics_count=cap,
        frame_cap=8,
        frame_head=3,
        frame_count=8,
        started_unix=0,
    )
    body = bytearray(HEADER_SIZE + cap * RECORD_SIZE + 8)
    body[:HEADER_SIZE] = pack_header(header)
    # chronological t_rel 10..15 written into ring slots starting at head=2
    for age, t_rel in enumerate(range(10, 16)):
        slot = (2 + age) % cap
        offset = HEADER_SIZE + slot * RECORD_SIZE
        body[offset : offset + RECORD_SIZE] = pack_metrics_sample(_sample(t_rel))
    frames_offset = HEADER_SIZE + cap * RECORD_SIZE
    for age in range(8):
        body[frames_offset + (3 + age) % 8] = encode_frametime_ms(1.0 + age)
    path = tmp_path / "wrap.ring"
    path.write_bytes(bytes(body))

    history = read_frame_history(path)
    assert history is not None
    assert [s.t_rel_s for s in history.samples] == list(range(10, 16))
    ft = history.frametimes_ms
    assert ft == tuple(sorted(ft))  # oldest→newest is ascending by construction


def test_reader_rejects_missing_truncated_and_corrupt_files(tmp_path: Path) -> None:
    assert read_frame_history(tmp_path / "absent.ring") is None

    good = tmp_path / "good.ring"
    assert write_frame_history(
        good, samples=[_sample(0)], frametimes_ms=[10.0], metrics_cap=4, frame_cap=8
    )
    data = good.read_bytes()

    truncated = tmp_path / "short.ring"
    truncated.write_bytes(data[: HEADER_SIZE + 10])
    assert read_frame_history(truncated) is None

    bad_magic = tmp_path / "magic.ring"
    bad_magic.write_bytes(b"XXXX" + data[4:])
    assert read_frame_history(bad_magic) is None

    bad_version = tmp_path / "version.ring"
    bad_version.write_bytes(data[:4] + struct.pack("<H", 99) + data[6:])
    assert read_frame_history(bad_version) is None

    bad_gz = tmp_path / "bad.ring.gz"
    bad_gz.write_bytes(b"not gzip at all")
    assert read_frame_history(bad_gz) is None
    assert FRAME_HISTORY_MAGIC == b"PBFH"


def test_summarize_medians_bottleneck_and_modal_tier() -> None:
    samples = (
        [_sample(i, gpu_util_pct=99, cpu_peak_thread_pct=60) for i in range(3)]
        + [
            _sample(
                3 + i,
                tier=PROFILE_TIER_PERFORMANCE,
                gpu_util_pct=99,
                cpu_peak_thread_pct=60,
            )
            for i in range(2)
        ]
    )
    frametimes = [10.0] * 99 + [20.0]
    history_header = FrameHistoryHeader(
        version=1, app_id=1, pid=1, gpu_index=0, power_limit_w=360, max_boost_mhz=0,
        sample_hz=1, window_s=1800, metrics_cap=1800, metrics_head=5, metrics_count=5,
        frame_cap=1024, frame_head=100, frame_count=100, started_unix=0,
    )
    from runtime.frame_history import FrameHistory

    history = FrameHistory(
        header=history_header,
        samples=tuple(samples),
        frametimes_ms=tuple(frametimes),
    )
    summary = summarize(history)
    assert summary is not None
    assert summary.tier == PROFILE_TIER_BALANCED  # modal: 3 of 5
    assert summary.bottleneck == BOTTLENECK_GPU
    assert summary.median_frametime_ms == 10.0
    assert summary.p99_frametime_ms == 20.0
    assert abs(summary.low_1pct_fps - 50.0) < 1e-6
    assert summary.median_power_w == 214
    assert not summary.qualified

    cpu_bound = FrameHistory(
        header=history_header,
        samples=tuple(
            _sample(i, gpu_util_pct=40, cpu_peak_thread_pct=95) for i in range(5)
        ),
        frametimes_ms=(),
    )
    cpu_summary = summarize(cpu_bound)
    assert cpu_summary is not None
    assert cpu_summary.bottleneck == BOTTLENECK_CPU
    # empty frame ring falls back to the per-second percentile medians
    assert abs(cpu_summary.median_frametime_ms - 10.4) <= 0.025

    mixed = FrameHistory(
        header=history_header,
        samples=tuple(
            _sample(i, gpu_util_pct=90, cpu_peak_thread_pct=88) for i in range(5)
        ),
        frametimes_ms=(),
    )
    mixed_summary = summarize(mixed)
    assert mixed_summary is not None
    assert mixed_summary.bottleneck == BOTTLENECK_MIXED


def test_dir_helpers_honor_env_overrides(tmp_path: Path) -> None:
    env = {
        FRAME_HISTORY_LIVE_DIR_ENV: str(tmp_path / "live"),
        FRAME_HISTORY_ARCHIVE_DIR_ENV: str(tmp_path / "arch"),
    }
    assert frame_history_live_dir(env) == tmp_path / "live"
    assert frame_history_archive_dir(env) == tmp_path / "arch"
    assert frame_history_live_dir({}) == Path("/run/penguin-burner/frame-history")
    xdg_env = {"XDG_DATA_HOME": str(tmp_path / "data")}
    assert (
        frame_history_archive_dir(xdg_env)
        == tmp_path / "data" / "penguin-burner" / "frame-history"
    )


def test_for_app_prefers_the_newest_matching_live_ring(tmp_path: Path) -> None:
    live = tmp_path / "live"
    arch = tmp_path / "arch"
    env = {
        FRAME_HISTORY_LIVE_DIR_ENV: str(live),
        FRAME_HISTORY_ARCHIVE_DIR_ENV: str(arch),
    }
    write_frame_history(
        live / "100.ring", samples=[_sample(0)], frametimes_ms=[],
        app_id=777, started_unix=1000, metrics_cap=4, frame_cap=8,
    )
    write_frame_history(
        live / "200.ring", samples=[_sample(0), _sample(1)], frametimes_ms=[],
        app_id=777, started_unix=2000, metrics_cap=4, frame_cap=8,
    )
    write_frame_history(
        live / "300.ring", samples=[_sample(0)], frametimes_ms=[],
        app_id=888, started_unix=3000, metrics_cap=4, frame_cap=8,
    )
    found = read_frame_history_for_app("777", env=env)
    assert found is not None
    assert found.header.started_unix == 2000
    assert read_frame_history_for_app("999", env=env) is None
    assert read_frame_history_for_app("not-a-number", env=env) is None


def test_for_app_falls_back_to_the_archive(tmp_path: Path) -> None:
    env = {
        FRAME_HISTORY_LIVE_DIR_ENV: str(tmp_path / "live"),
        FRAME_HISTORY_ARCHIVE_DIR_ENV: str(tmp_path / "arch"),
    }
    write_frame_history(
        tmp_path / "arch" / "555.ring.gz",
        samples=[_sample(i) for i in range(3)],
        frametimes_ms=[8.0, 9.0],
        app_id=555,
        metrics_cap=8,
        frame_cap=8,
    )
    found = read_frame_history_for_app(555, env=env)
    assert found is not None
    assert found.header.app_id == 555
    assert len(found.samples) == 3


def test_archive_frame_history_stamps_app_id_and_compresses(tmp_path: Path) -> None:
    live_ring = tmp_path / "4242.ring"
    env = {FRAME_HISTORY_ARCHIVE_DIR_ENV: str(tmp_path / "arch")}
    write_frame_history(
        live_ring,
        samples=[_sample(i) for i in range(10)],
        frametimes_ms=[12.0] * 40,
        app_id=0,  # daemon-written rings do not know the app yet
        pid=4242,
        power_limit_w=360,
        metrics_cap=16,
        frame_cap=64,
    )
    assert archive_frame_history(live_ring, 3764200, env=env)
    target = tmp_path / "arch" / "3764200.ring.gz"
    assert target.exists()
    with gzip.open(target, "rb") as fh:
        assert fh.read(4) == FRAME_HISTORY_MAGIC
    archived = read_frame_history(target)
    assert archived is not None
    assert archived.header.app_id == 3764200
    assert archived.header.pid == 4242
    assert len(archived.samples) == 10
    assert len(archived.frametimes_ms) == 40
    assert not archive_frame_history(tmp_path / "missing.ring", 1, env=env)


def test_for_app_reads_the_daemon_system_archive(tmp_path: Path) -> None:
    from runtime.frame_history import FRAME_HISTORY_SYSTEM_ARCHIVE_DIR_ENV

    env = {
        FRAME_HISTORY_LIVE_DIR_ENV: str(tmp_path / "live"),
        FRAME_HISTORY_SYSTEM_ARCHIVE_DIR_ENV: str(tmp_path / "system"),
        FRAME_HISTORY_ARCHIVE_DIR_ENV: str(tmp_path / "user"),
    }
    # An uncompressed daemon archive (the Rust side writes .ring, no gzip).
    write_frame_history(
        tmp_path / "system" / "3764200.ring",
        samples=[_sample(i) for i in range(7)],
        frametimes_ms=[12.8] * 100,
        app_id=3764200,
        metrics_cap=7,
        frame_cap=100,
    )
    found = read_frame_history_for_app(3764200, env=env)
    assert found is not None
    assert found.header.app_id == 3764200
    assert len(found.samples) == 7
    # The user archive is only the last resort.
    write_frame_history(
        tmp_path / "user" / "42.ring.gz",
        samples=[_sample(0)],
        frametimes_ms=[],
        app_id=42,
        metrics_cap=4,
        frame_cap=8,
    )
    fallback = read_frame_history_for_app(42, env=env)
    assert fallback is not None and fallback.header.app_id == 42


def test_summarize_derives_fps_from_frametimes_when_estimator_is_silent() -> None:
    from runtime.frame_history import FrameHistory

    header = FrameHistoryHeader(
        version=1, app_id=1, pid=1, gpu_index=0, power_limit_w=360, max_boost_mhz=0,
        sample_hz=1, window_s=1800, metrics_cap=1800, metrics_head=5, metrics_count=5,
        frame_cap=1024, frame_head=100, frame_count=100, started_unix=0,
    )
    silent = FrameHistory(
        header=header,
        samples=tuple(_sample(i, present_fps=0.0) for i in range(5)),
        frametimes_ms=tuple([8.33] * 100),
    )
    summary = summarize(silent)
    assert summary is not None
    assert abs(summary.median_present_fps - 1000.0 / 8.33) < 0.1


def test_reader_trims_frames_to_the_advertised_window(tmp_path: Path) -> None:
    # 20 s of 10 ms frames against a 10 s window: only the newest ~10 s of
    # frames survive, so summaries and the graph describe the same span.
    path = tmp_path / "long.ring"
    assert write_frame_history(
        path,
        samples=[_sample(i) for i in range(20)],
        frametimes_ms=[10.0] * 2000,
        window_s=10,
        metrics_cap=32,
        frame_cap=4096,
    )
    history = read_frame_history(path)
    assert history is not None
    kept_ms = sum(history.frametimes_ms)
    assert kept_ms <= 10_000.0 + 10.5
    # Companded 10 ms decodes to ~9.93 ms, so ~1006 frames fill the window.
    assert 1000 <= len(history.frametimes_ms) <= 1007
    assert len(history.frametimes_ms) < 2000  # actually trimmed
