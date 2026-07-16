"""Frame-history ring files: the per-game telemetry window behind Frame DNA.

`burnerd` records one ring file per running game session (keyed by pid) under
``/run/penguin-burner/frame-history/``: a 64-byte header, a fixed-stride ring
of one 36-byte metrics record per second, and a ring of one log-companded byte
per rendered frame. This module is the Python side of that format contract —
pure pack/unpack helpers over ``bytes``, an edge reader that swallows I/O and
corruption into ``None``, a canonical writer (synthetic rings, tests, tools),
and the per-user archive that outlives the tmpfs ring.

Format constants here and in ``burnerd/src/profile/frame_history.rs`` must
stay in lockstep; ``FRAME_HISTORY_FORMAT_VERSION`` gates both sides.
"""

from __future__ import annotations

import gzip
import math
import os
import statistics
import struct
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from profiles.uv.profile_tiers import (
    PROFILE_TIER_NONE,
    PROFILE_TIERS,
    normalize_profile_tier,
)

FRAME_HISTORY_MAGIC = b"PBFH"
FRAME_HISTORY_FORMAT_VERSION = 1

# Live rings are written by the daemon on tmpfs; tests and tools pin the
# directories through the env overrides (argument > env > default, matching
# overlay_state_path / daemon socket resolution).
FRAME_HISTORY_LIVE_DIR = "/run/penguin-burner/frame-history"
FRAME_HISTORY_LIVE_DIR_ENV = "PENGUIN_BURNER_FRAME_HISTORY_DIR"
# The daemon archives a finished session per app id, world-readable.
FRAME_HISTORY_SYSTEM_ARCHIVE_DIR = "/var/lib/penguin-burner/frame-history"
FRAME_HISTORY_SYSTEM_ARCHIVE_DIR_ENV = "PENGUIN_BURNER_FRAME_HISTORY_SYSTEM_DIR"
# Optional per-user archives written by this process (tools/tests).
FRAME_HISTORY_ARCHIVE_DIR_ENV = "PENGUIN_BURNER_FRAME_HISTORY_ARCHIVE_DIR"

_HEADER_FORMAT = "<4sHHIIHHHBBHHHHIIIQ12x"
_RECORD_FORMAT = "<HHHHHHBBBBBBhHHHHHHH2x"
HEADER_SIZE = struct.calcsize(_HEADER_FORMAT)
RECORD_SIZE = struct.calcsize(_RECORD_FORMAT)

DEFAULT_WINDOW_S = 1800
DEFAULT_METRICS_CAP = 1800
DEFAULT_FRAME_CAP = 1 << 20
QUALIFY_MINUTES = 5.0

# Frame entries are log2-companded to one byte: ft_ms = 2 ** (value / 32).
# 1 ms .. ~250.6 ms at ~2.2% relative resolution; exact per-second p50/p99/
# p99.9 in the metrics record preserve the tails the companding rounds.
_FT_LOG_STEP = 32.0
FRAME_TIME_MIN_MS = 1.0
FRAME_TIME_MAX_MS = 2.0 ** (255 / _FT_LOG_STEP)
_FT_DECODE_MS = tuple(2.0 ** (value / _FT_LOG_STEP) for value in range(256))

_FPS_SCALE = 10.0  # u16 fields storing fps * 10
_MS_SCALE = 20.0  # u16 fields storing milliseconds * 20 (0.05 ms steps)
_U16_MAX = 0xFFFF

FLAG_FRAMEGEN_ACTIVE = 0x01
FLAG_ADAPTIVE = 0x02
_FPS_SOURCE_SHIFT = 2
_FPS_SOURCE_MASK = 0x03
_TIER_SHIFT = 4
_TIER_MASK = 0x0F

FPS_SOURCE_NONE = 0
FPS_SOURCE_MARKER = 1
FPS_SOURCE_NVAPI_MARKER = 2
FPS_SOURCE_OTHER = 3

# Nibble 0 is "no tier"; N>0 equals PROFILE_TIERS[N-1] so ordering comparisons
# match the daemon's tier_index semantics.
_TIER_NIBBLE_KEYS = (PROFILE_TIER_NONE, *PROFILE_TIERS)

BOTTLENECK_GPU = "gpu"
BOTTLENECK_CPU = "cpu"
BOTTLENECK_MIXED = "mixed"
_BOTTLENECK_MARGIN_PCT = 12


def encode_frametime_ms(frametime_ms: float) -> int:
    """One companded byte for a frametime; clamps into 1..~250.6 ms."""
    clamped = min(max(float(frametime_ms), FRAME_TIME_MIN_MS), FRAME_TIME_MAX_MS)
    return min(255, max(0, round(_FT_LOG_STEP * math.log2(clamped))))


def decode_frametime_ms(value: int) -> float:
    return _FT_DECODE_MS[int(value) & 0xFF]


def tier_from_nibble(nibble: int) -> str:
    if 0 <= nibble < len(_TIER_NIBBLE_KEYS):
        return _TIER_NIBBLE_KEYS[nibble]
    return PROFILE_TIER_NONE


def nibble_from_tier(tier: str) -> int:
    key = normalize_profile_tier(tier)
    if key in PROFILE_TIERS:
        return PROFILE_TIERS.index(key) + 1
    return 0


@dataclass(frozen=True, slots=True)
class FrameHistoryHeader:
    version: int
    app_id: int
    pid: int
    gpu_index: int
    power_limit_w: int
    max_boost_mhz: int
    sample_hz: int
    window_s: int
    metrics_cap: int
    metrics_head: int
    metrics_count: int
    frame_cap: int
    frame_head: int
    frame_count: int
    started_unix: int


@dataclass(frozen=True, slots=True)
class MetricsSample:
    """One decoded per-second record, in natural units."""

    t_rel_s: int
    frame_count: int
    clock_mhz: int
    mem_clock_mhz: int
    voltage_mv: int
    power_w: int
    gpu_util_pct: int
    cpu_util_pct: int
    cpu_peak_thread_pct: int
    fan_pct: int
    temperature_c: int
    uv_offset_mv: int
    present_fps: float
    framegen_fps: float
    latency_ms: float
    display_latency_ms: float
    framegen_active: bool
    adaptive: bool
    fps_source: int
    tier: str
    ft_p50_ms: float
    ft_p99_ms: float
    ft_p999_ms: float


@dataclass(frozen=True, slots=True)
class FrameHistory:
    """A fully decoded ring, unwrapped oldest-to-newest."""

    header: FrameHistoryHeader
    samples: tuple[MetricsSample, ...]
    frametimes_ms: tuple[float, ...]

    @property
    def minutes(self) -> float:
        hz = max(1, self.header.sample_hz)
        return len(self.samples) / (60.0 * hz)

    @property
    def qualified(self) -> bool:
        return self.minutes >= QUALIFY_MINUTES


@dataclass(frozen=True, slots=True)
class FrameHistorySummary:
    """The rolled-up numbers the fingerprint and peek are drawn from."""

    minutes: float
    qualified: bool
    tier: str
    bottleneck: str
    median_present_fps: float
    low_1pct_fps: float
    median_frametime_ms: float
    p99_frametime_ms: float
    median_power_w: int
    median_clock_mhz: int
    gpu_util_pct: int
    cpu_peak_thread_pct: int
    latency_ms: float
    framegen_seen: bool
    # Present->scanout tail (VK_KHR_present_wait) — a different metric from
    # marker render latency; non-Reflex games may have only this one.
    median_display_latency_ms: float = 0.0


def frame_history_live_dir(env: Mapping[str, str] | None = None) -> Path:
    resolved_env = os.environ if env is None else env
    explicit = str(resolved_env.get(FRAME_HISTORY_LIVE_DIR_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return Path(FRAME_HISTORY_LIVE_DIR)


def frame_history_system_archive_dir(env: Mapping[str, str] | None = None) -> Path:
    resolved_env = os.environ if env is None else env
    explicit = str(resolved_env.get(FRAME_HISTORY_SYSTEM_ARCHIVE_DIR_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return Path(FRAME_HISTORY_SYSTEM_ARCHIVE_DIR)


def frame_history_archive_dir(env: Mapping[str, str] | None = None) -> Path:
    resolved_env = os.environ if env is None else env
    explicit = str(resolved_env.get(FRAME_HISTORY_ARCHIVE_DIR_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    data_home = str(resolved_env.get("XDG_DATA_HOME") or "").strip()
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local/share"
    return base / "penguin-burner" / "frame-history"


def _pack_u16(value: float) -> int:
    return min(_U16_MAX, max(0, round(value)))


def pack_header(header: FrameHistoryHeader) -> bytes:
    return struct.pack(
        _HEADER_FORMAT,
        FRAME_HISTORY_MAGIC,
        header.version,
        0,
        header.app_id,
        header.pid,
        header.gpu_index,
        header.power_limit_w,
        header.max_boost_mhz,
        header.sample_hz,
        0,
        header.window_s,
        header.metrics_cap,
        header.metrics_head,
        header.metrics_count,
        header.frame_cap,
        header.frame_head,
        header.frame_count,
        header.started_unix,
    )


def unpack_header(data: bytes) -> FrameHistoryHeader | None:
    if len(data) < HEADER_SIZE:
        return None
    fields = struct.unpack_from(_HEADER_FORMAT, data, 0)
    (magic, version, _, app_id, pid, gpu_index, power_limit_w, max_boost_mhz,
     sample_hz, _, window_s, metrics_cap, metrics_head, metrics_count,
     frame_cap, frame_head, frame_count, started_unix) = fields
    if magic != FRAME_HISTORY_MAGIC or version != FRAME_HISTORY_FORMAT_VERSION:
        return None
    return FrameHistoryHeader(
        version=version,
        app_id=app_id,
        pid=pid,
        gpu_index=gpu_index,
        power_limit_w=power_limit_w,
        max_boost_mhz=max_boost_mhz,
        sample_hz=sample_hz,
        window_s=window_s,
        metrics_cap=metrics_cap,
        metrics_head=metrics_head,
        metrics_count=metrics_count,
        frame_cap=frame_cap,
        frame_head=frame_head,
        frame_count=frame_count,
        started_unix=started_unix,
    )


def pack_metrics_sample(sample: MetricsSample) -> bytes:
    flags = (
        (FLAG_FRAMEGEN_ACTIVE if sample.framegen_active else 0)
        | (FLAG_ADAPTIVE if sample.adaptive else 0)
        | ((sample.fps_source & _FPS_SOURCE_MASK) << _FPS_SOURCE_SHIFT)
        | ((nibble_from_tier(sample.tier) & _TIER_MASK) << _TIER_SHIFT)
    )
    return struct.pack(
        _RECORD_FORMAT,
        _pack_u16(sample.t_rel_s),
        _pack_u16(sample.frame_count),
        _pack_u16(sample.clock_mhz),
        _pack_u16(sample.mem_clock_mhz),
        _pack_u16(sample.voltage_mv),
        _pack_u16(sample.power_w),
        min(255, max(0, sample.gpu_util_pct)),
        min(255, max(0, sample.cpu_util_pct)),
        min(255, max(0, sample.cpu_peak_thread_pct)),
        min(255, max(0, sample.fan_pct)),
        min(255, max(0, sample.temperature_c)),
        flags,
        min(32767, max(-32768, sample.uv_offset_mv)),
        _pack_u16(sample.present_fps * _FPS_SCALE),
        _pack_u16(sample.framegen_fps * _FPS_SCALE),
        _pack_u16(sample.latency_ms * _MS_SCALE),
        _pack_u16(sample.display_latency_ms * _MS_SCALE),
        _pack_u16(sample.ft_p50_ms * _MS_SCALE),
        _pack_u16(sample.ft_p99_ms * _MS_SCALE),
        _pack_u16(sample.ft_p999_ms * _MS_SCALE),
    )


def unpack_metrics_sample(data: bytes, offset: int) -> MetricsSample:
    (t_rel, frame_count, clock, mem_clock, voltage, power, gpu_util, cpu_util,
     cpu_thread, fan, temp, flags, uv_offset, present_fps, framegen_fps,
     latency, display_latency, ft_p50, ft_p99, ft_p999) = struct.unpack_from(
        _RECORD_FORMAT, data, offset
    )
    return MetricsSample(
        t_rel_s=t_rel,
        frame_count=frame_count,
        clock_mhz=clock,
        mem_clock_mhz=mem_clock,
        voltage_mv=voltage,
        power_w=power,
        gpu_util_pct=gpu_util,
        cpu_util_pct=cpu_util,
        cpu_peak_thread_pct=cpu_thread,
        fan_pct=fan,
        temperature_c=temp,
        uv_offset_mv=uv_offset,
        present_fps=present_fps / _FPS_SCALE,
        framegen_fps=framegen_fps / _FPS_SCALE,
        latency_ms=latency / _MS_SCALE,
        display_latency_ms=display_latency / _MS_SCALE,
        framegen_active=bool(flags & FLAG_FRAMEGEN_ACTIVE),
        adaptive=bool(flags & FLAG_ADAPTIVE),
        fps_source=(flags >> _FPS_SOURCE_SHIFT) & _FPS_SOURCE_MASK,
        tier=tier_from_nibble((flags >> _TIER_SHIFT) & _TIER_MASK),
        ft_p50_ms=ft_p50 / _MS_SCALE,
        ft_p99_ms=ft_p99 / _MS_SCALE,
        ft_p999_ms=ft_p999 / _MS_SCALE,
    )


def _unwrap(count: int, cap: int, head: int) -> range:
    """Slot indices oldest-to-newest for a ring with `count` filled entries."""
    if count < cap:
        return range(count)
    start = head % cap
    return range(start, start + cap)


def read_frame_history(path: str | Path) -> FrameHistory | None:
    """Decode a ring file (live ``.ring`` or archived ``.ring.gz``).

    Returns ``None`` for missing, truncated, or version-mismatched files —
    callers treat that as "no telemetry yet".
    """
    file_path = Path(path).expanduser()
    try:
        data = file_path.read_bytes()
    except OSError:
        return None
    if file_path.name.endswith(".gz"):
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError, gzip.BadGzipFile):
            return None
    header = unpack_header(data)
    if header is None:
        return None
    if header.metrics_cap <= 0 or header.frame_cap <= 0:
        return None
    metrics_end = HEADER_SIZE + header.metrics_cap * RECORD_SIZE
    frames_end = metrics_end + header.frame_cap
    if len(data) < frames_end:
        return None
    metrics_count = min(header.metrics_count, header.metrics_cap)
    samples = tuple(
        unpack_metrics_sample(
            data, HEADER_SIZE + (slot % header.metrics_cap) * RECORD_SIZE
        )
        for slot in _unwrap(metrics_count, header.metrics_cap, header.metrics_head)
    )
    frame_count = min(header.frame_count, header.frame_cap)
    frametimes = tuple(
        decode_frametime_ms(data[metrics_end + (slot % header.frame_cap)])
        for slot in _unwrap(frame_count, header.frame_cap, header.frame_head)
    )
    # The frame ring's byte capacity outlives the metrics window on long
    # sessions; enforce the advertised rolling window on the frame data too,
    # so summaries and the frametime graph describe the same span.
    frametimes = _trim_frames_to_window(frametimes, header.window_s)
    return FrameHistory(header=header, samples=samples, frametimes_ms=frametimes)


def _trim_frames_to_window(
    frametimes_ms: tuple[float, ...], window_s: int
) -> tuple[float, ...]:
    budget_ms = float(window_s) * 1000.0
    if budget_ms <= 0.0:
        return frametimes_ms
    total = 0.0
    start = len(frametimes_ms)
    for index in range(len(frametimes_ms) - 1, -1, -1):
        total += frametimes_ms[index]
        if total > budget_ms:
            break
        start = index
    return frametimes_ms[start:]


def write_frame_history(
    path: str | Path,
    *,
    samples: Sequence[MetricsSample],
    frametimes_ms: Sequence[float],
    app_id: int = 0,
    pid: int = 0,
    gpu_index: int = 0,
    power_limit_w: int = 0,
    max_boost_mhz: int = 0,
    sample_hz: int = 1,
    window_s: int = DEFAULT_WINDOW_S,
    metrics_cap: int | None = None,
    frame_cap: int | None = None,
    started_unix: int = 0,
) -> bool:
    """Write a complete ring file; the canonical Python writer.

    Keeps the newest ``cap`` entries when given more, laying them out
    unwrapped from slot 0 (head = count % cap), which is a valid ring state.
    Writes atomically; gzip-compresses when the target name ends in ``.gz``.
    """
    resolved_metrics_cap = metrics_cap if metrics_cap else DEFAULT_METRICS_CAP
    resolved_frame_cap = frame_cap if frame_cap else DEFAULT_FRAME_CAP
    kept_samples = list(samples)[-resolved_metrics_cap:]
    kept_frames = [encode_frametime_ms(ft) for ft in frametimes_ms]
    kept_frames = kept_frames[-resolved_frame_cap:]
    header = FrameHistoryHeader(
        version=FRAME_HISTORY_FORMAT_VERSION,
        app_id=app_id,
        pid=pid,
        gpu_index=gpu_index,
        power_limit_w=power_limit_w,
        max_boost_mhz=max_boost_mhz,
        sample_hz=sample_hz,
        window_s=window_s,
        metrics_cap=resolved_metrics_cap,
        metrics_head=len(kept_samples) % resolved_metrics_cap,
        metrics_count=len(kept_samples),
        frame_cap=resolved_frame_cap,
        frame_head=len(kept_frames) % resolved_frame_cap,
        frame_count=len(kept_frames),
        started_unix=started_unix,
    )
    body = bytearray(HEADER_SIZE + resolved_metrics_cap * RECORD_SIZE + resolved_frame_cap)
    body[:HEADER_SIZE] = pack_header(header)
    for index, sample in enumerate(kept_samples):
        offset = HEADER_SIZE + index * RECORD_SIZE
        body[offset : offset + RECORD_SIZE] = pack_metrics_sample(sample)
    frames_offset = HEADER_SIZE + resolved_metrics_cap * RECORD_SIZE
    body[frames_offset : frames_offset + len(kept_frames)] = bytes(kept_frames)
    payload = bytes(body)
    target = Path(path).expanduser()
    if target.name.endswith(".gz"):
        payload = gzip.compress(payload, compresslevel=6)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        temp.write_bytes(payload)
        temp.replace(target)
    except OSError:
        return False
    return True


def archive_frame_history(
    live_path: str | Path,
    app_id: int,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Archive a live ring per-user as ``<app_id>.ring.gz``, stamping app_id.

    The daemon records app_id as 0 (it only knows the pid); the archive is
    where the association becomes durable.
    """
    history = read_frame_history(live_path)
    if history is None or not history.samples:
        return False
    header = replace(history.header, app_id=int(app_id))
    target = frame_history_archive_dir(env) / f"{int(app_id)}.ring.gz"
    return write_frame_history(
        target,
        samples=history.samples,
        frametimes_ms=history.frametimes_ms,
        app_id=header.app_id,
        pid=header.pid,
        gpu_index=header.gpu_index,
        power_limit_w=header.power_limit_w,
        max_boost_mhz=header.max_boost_mhz,
        sample_hz=header.sample_hz,
        window_s=header.window_s,
        metrics_cap=header.metrics_cap,
        frame_cap=header.frame_cap,
        started_unix=header.started_unix,
    )


def read_frame_history_for_app(
    app_id: str | int,
    *,
    env: Mapping[str, str] | None = None,
) -> FrameHistory | None:
    """The freshest history for a game: live ring, then archives.

    Live rings are matched on ``header.app_id`` (the daemon stamps it at
    session start). Fallbacks: the daemon's system archive under /var/lib,
    then the per-user archive.
    """
    try:
        wanted = int(str(app_id).strip())
    except ValueError:
        return None
    if wanted <= 0:
        return None
    best: FrameHistory | None = None
    try:
        candidates = sorted(frame_history_live_dir(env).glob("*.ring"))
    except OSError:
        candidates = []
    for candidate in candidates:
        history = read_frame_history(candidate)
        if history is None or history.header.app_id != wanted:
            continue
        if best is None or history.header.started_unix >= best.header.started_unix:
            best = history
    if best is not None:
        return best
    system_dir = frame_history_system_archive_dir(env)
    for name in (f"{wanted}.ring", f"{wanted}.ring.gz"):
        history = read_frame_history(system_dir / name)
        if history is not None:
            return history
    return read_frame_history(frame_history_archive_dir(env) / f"{wanted}.ring.gz")


def _percentile(values: Sequence[float], fraction: float) -> float:
    """Stutter-biased percentile: the value `fraction` of samples stay under.

    Uses floor indexing so the tail is included — the p99 of 100 frames is the
    single worst frame, matching the gaming "1%-low" convention.
    """
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, math.floor(fraction * len(ordered))))
    return ordered[index]


def summarize(history: FrameHistory) -> FrameHistorySummary | None:
    """Roll a decoded window up into the numbers the UI draws."""
    samples = history.samples
    if not samples:
        return None
    if history.frametimes_ms:
        median_ft = _percentile(history.frametimes_ms, 0.50)
        p99_ft = _percentile(history.frametimes_ms, 0.99)
    else:
        median_ft = statistics.median(s.ft_p50_ms for s in samples)
        p99_ft = statistics.median(s.ft_p99_ms for s in samples)
    gpu_util = round(statistics.median(s.gpu_util_pct for s in samples))
    cpu_thread = round(statistics.median(s.cpu_peak_thread_pct for s in samples))
    if gpu_util - cpu_thread >= _BOTTLENECK_MARGIN_PCT:
        bottleneck = BOTTLENECK_GPU
    elif cpu_thread - gpu_util >= _BOTTLENECK_MARGIN_PCT:
        bottleneck = BOTTLENECK_CPU
    else:
        bottleneck = BOTTLENECK_MIXED
    tier_counts = Counter(s.tier for s in samples if s.tier != PROFILE_TIER_NONE)
    tier = tier_counts.most_common(1)[0][0] if tier_counts else PROFILE_TIER_NONE
    median_fps = statistics.median(s.present_fps for s in samples)
    if median_fps <= 0.0 and median_ft > 0.0:
        # The daemon's fps estimator stays silent when no marker source ever
        # elects an owner session; the recorded frametimes still carry the
        # true present cadence.
        median_fps = 1000.0 / median_ft
    return FrameHistorySummary(
        minutes=history.minutes,
        qualified=history.qualified,
        tier=tier,
        bottleneck=bottleneck,
        median_present_fps=median_fps,
        low_1pct_fps=(1000.0 / p99_ft) if p99_ft > 0 else 0.0,
        median_frametime_ms=median_ft,
        p99_frametime_ms=p99_ft,
        median_power_w=round(statistics.median(s.power_w for s in samples)),
        median_clock_mhz=round(statistics.median(s.clock_mhz for s in samples)),
        gpu_util_pct=gpu_util,
        cpu_peak_thread_pct=cpu_thread,
        latency_ms=statistics.median(s.latency_ms for s in samples),
        median_display_latency_ms=statistics.median(
            s.display_latency_ms for s in samples
        ),
        framegen_seen=any(s.framegen_active for s in samples),
    )
