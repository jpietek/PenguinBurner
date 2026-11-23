"""Deterministic sample frame rings for the Perf Profile preview.

Implements ``PerfProfileSource`` from ``qt.components.perf_profile_plot`` for
the sample Game Library (keys are ``<launcher>:<id>``, as
``native_library.GAMES`` and ``ui.components.game_library_panel.game_key``
build them). The generator follows the browser prototype's
``history.js`` sample rings: a jittered body, a 1% tail tuned to the game's
1% low, mild 20-30 ms stutters only the Live view singles out, 35-90 ms
stalls, an optional capped menu stretch and one clipped stall.

Covered cases:
- ``steam:1091500`` Cyberpunk 2077: 24 min archived, Balanced, hitches, a
  182 ms clipped stall, frame-gen seen, measured latency.
- ``steam:2531310`` The Last of Us Part II: 3.6 min archived (warming:
  "settling · fingerprint at 5 min").
- ``steam:782330`` DOOM Eternal: 158 fps with a 45 s menu capped at 60 fps,
  Performance, estimated latency, a per-game target.
- ``heroic:control`` Control: still recording (● REC); the ring grows by
  one second of frames per wall-clock second.
- ``steam:1888930`` The Last of Us Part I: Capture telemetry off, no ring.
- Every other game: no ring yet.
"""

from __future__ import annotations

import math
import random
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field

from qt.components.perf_profile_plot import (
    QUALIFY_MINUTES,
    PerfRecording,
    PerfSummary,
    percentile,
)

# runtime/frame_history.py (PR #137): GPU vs busiest CPU thread margin.
BOTTLENECK_MARGIN_PCT = 12
RING_MINUTES = 30
SYSTEM_TARGET_FPS = 60.0
POWER_LIMIT_W = 360
# One call never records more than this much catch-up play.
MAX_CATCH_UP_S = 120

GAME_NAMES = {
    "steam:1091500": "Cyberpunk 2077",
    "steam:2531310": "The Last of Us Part II Remastered",
    "steam:1888930": "The Last of Us Part I",
    "steam:782330": "DOOM Eternal",
    "heroic:control": "Control",
    "lutris:dishonored": "Dishonored",
    "faugus:hollow-knight": "Hollow Knight",
    "steam:388880": "Oxenfree",
    "steam:1145350": "Hades II",
}
CAPTURE_OFF = frozenset({"steam:1888930"})
LIVE_KEY = "heroic:control"


@dataclass(frozen=True)
class PerfHardware:
    """The sample's per-second readings (window medians)."""

    power_w: int
    clock_mhz: int
    gpu_util_pct: int
    cpu_thread_pct: int
    temperature_c: float
    fan_pct: float


@dataclass(frozen=True)
class RingSpec:
    """How a sample ring sounds. ``menu`` is (start_s, length_s, capped_fps);
    ``big_hitch`` is (at_s, ms); ``early_hitches`` are seconds."""

    tier: str
    minutes: float
    fps: float
    low_fps: float
    hardware: PerfHardware
    drift: float = 0.05
    hitch_every: float = 60.0
    hitch_ms: tuple[float, float] = (35.0, 60.0)
    stutter_every: float = 0.0
    menu: tuple[float, float, float] | None = None
    big_hitch: tuple[float, float] | None = None
    early_hitches: tuple[float, ...] = ()
    framegen: bool = False
    latency_ms: float = 0.0
    latency_estimated: bool = False
    target_fps: float = 0.0
    live: bool = False


SAMPLE_SPECS: dict[str, RingSpec] = {
    "steam:1091500": RingSpec(
        tier="Balanced",
        minutes=24,
        fps=87,
        low_fps=61,
        drift=0.05,
        hitch_every=88,
        hitch_ms=(35, 96),
        big_hitch=(610, 182),
        stutter_every=9,
        framegen=True,
        latency_ms=28,
        hardware=PerfHardware(224, 2640, 96, 72, 64, 42),
    ),
    "steam:2531310": RingSpec(
        tier="Efficiency",
        minutes=3.6,
        fps=72,
        low_fps=54,
        drift=0.04,
        hitch_every=70,
        hitch_ms=(38, 58),
        early_hitches=(4, 9, 15, 31),
        stutter_every=14,
        hardware=PerfHardware(168, 2385, 91, 64, 58, 34),
    ),
    "steam:782330": RingSpec(
        tier="Performance",
        minutes=7.4,
        fps=158,
        low_fps=118,
        drift=0.06,
        menu=(150, 45, 60),
        hitch_every=160,
        hitch_ms=(34, 46),
        stutter_every=6,
        latency_ms=22,
        latency_estimated=True,
        target_fps=144,
        hardware=PerfHardware(288, 2925, 98, 58, 69, 55),
    ),
    LIVE_KEY: RingSpec(
        tier="Balanced",
        minutes=11.5,
        fps=96,
        low_fps=68,
        drift=0.05,
        hitch_every=75,
        hitch_ms=(36, 70),
        stutter_every=7,
        latency_ms=31,
        hardware=PerfHardware(236, 2700, 97, 61, 66, 46),
        live=True,
    ),
}
_TIER_POWER = {"Efficiency": 172, "Balanced": 224, "Performance": 292}


def _fresh_spec(game_key: str, tier: str) -> RingSpec:
    """A new recording for a game the user starts in the preview."""
    seed = zlib.crc32(game_key.encode())
    fps = 70 + seed % 70
    return RingSpec(
        tier=tier,
        minutes=0,
        fps=fps,
        low_fps=round(fps * 0.72),
        hitch_every=45,
        hitch_ms=(36, 60),
        stutter_every=8,
        live=True,
        hardware=PerfHardware(
            _TIER_POWER.get(tier, 250),
            2500 + seed % 300,
            88 + seed % 11,
            50 + seed % 30,
            60,
            38,
        ),
    )


@dataclass
class _Ring:
    key: str
    spec: RingSpec
    rng: random.Random
    frames: list[float] = field(default_factory=list)
    total_ms: float = 0.0
    clock_ms: float = 0.0
    spike_hi: float = 1.2
    next_hitch: float = 0.0
    next_stutter: float = 1e9
    early: list[float] = field(default_factory=list)
    big_done: bool = True
    burst: int = 0
    burst_ms: float = 0.0
    live: bool = False
    tier: str = ""
    version: int = 0
    started_at: float = 0.0
    recorded_s: int = 0
    snapshot: PerfRecording | None = None

    def next_frame(self) -> float:
        s, r, t = self.spec, self.rng.random, self.clock_ms / 1000.0
        if self.burst > 0:
            self.burst -= 1
            return self.burst_ms * (0.7 + 0.3 * r())
        if s.menu and s.menu[0] <= t < s.menu[0] + s.menu[1]:
            # A capped menu is smooth; stalls due inside it move past its end.
            end = s.menu[0] + s.menu[1]
            self.next_hitch = max(self.next_hitch, end + s.hitch_every * 0.5)
            self.next_stutter = max(self.next_stutter, end + 2)
            return 1000.0 / s.menu[2] + (r() - 0.5) * 0.3
        if not self.big_done and s.big_hitch and t >= s.big_hitch[0]:
            self.big_done = True
            return s.big_hitch[1]
        if self.early and t >= self.early[0]:
            self.early.pop(0)
            return s.hitch_ms[0] + r() * (s.hitch_ms[1] - s.hitch_ms[0])
        if t >= self.next_hitch:
            self.next_hitch = t + s.hitch_every * (0.5 + r())
            ms = s.hitch_ms[0] + r() * r() * (s.hitch_ms[1] - s.hitch_ms[0])
            if r() < 0.35:
                self.burst, self.burst_ms = 1, ms * 0.6
            return ms
        if t >= self.next_stutter:
            self.next_stutter = t + s.stutter_every * (0.5 + r())
            return 19 + r() * 12
        base = (1000.0 / s.fps) * (
            1 + s.drift * math.sin(t / 53) + s.drift * 0.5 * math.sin(t / 11.3)
        )
        if r() < 0.025:
            return base * (1.12 + r() * (self.spike_hi - 1.12))
        return base * (1 + (r() + r() + r() - 1.5) * 0.07)

    def append(self, duration_ms: float) -> None:
        end = self.clock_ms + duration_ms
        frames = self.frames
        while self.clock_ms < end:
            ms = self.next_frame()
            frames.append(ms)
            self.total_ms += ms
            self.clock_ms += ms
        cap = RING_MINUTES * 60_000.0
        if self.total_ms > cap:
            drop, dropped = 0, 0.0
            while self.total_ms - dropped > cap:
                dropped += frames[drop]
                drop += 1
            del frames[:drop]
            self.total_ms -= dropped
        self.version += 1

    def hardware(self) -> PerfHardware:
        hw = self.spec.hardware
        if not self.live:
            return hw
        n = self.clock_ms / 1000.0

        def wobble(period: float, amp: float) -> int:
            return round(amp * math.sin(n / period))

        return PerfHardware(
            hw.power_w + wobble(17, 4),
            hw.clock_mhz + wobble(23, 15),
            min(100, hw.gpu_util_pct + wobble(13, 1)),
            hw.cpu_thread_pct + wobble(9, 3),
            hw.temperature_c + wobble(41, 1),
            hw.fan_pct + wobble(37, 1),
        )


def _new_ring(key: str, spec: RingSpec) -> _Ring:
    rng = random.Random(zlib.crc32(key.encode()))
    ratio = spec.fps / spec.low_fps
    ring = _Ring(
        key=key,
        spec=spec,
        rng=rng,
        # Tail spikes U(lo, hi) at 2.5% so the 1% low lands near low_fps.
        spike_hi=max(1.2, (ratio - 1.12) / 0.6 + 1.12),
        next_hitch=spec.hitch_every * (0.4 + rng.random()),
        next_stutter=(spec.stutter_every or 1e9) * (0.5 + rng.random()),
        early=list(spec.early_hitches),
        big_done=spec.big_hitch is None,
        live=spec.live,
        tier=spec.tier,
    )
    ring.append(spec.minutes * 60_000.0)
    return ring


def summarize_ring(
    frames: tuple[float, ...], spec: RingSpec, tier: str, hw: PerfHardware
) -> PerfSummary | None:
    """What the daemon's ``frame_history.summarize`` reports for a ring:
    floor-index percentiles over the frames, the 5-minute qualify gate and the
    GPU-vs-CPU bottleneck rule. The sample's medians are its readings."""
    if not frames:
        return None
    ordered = sorted(frames)
    median_ft = percentile(ordered, 0.50)
    p99_ft = percentile(ordered, 0.99)
    minutes = sum(frames) / 60_000.0
    gpu, cpu = hw.gpu_util_pct, hw.cpu_thread_pct
    bottleneck = (
        "gpu"
        if gpu - cpu >= BOTTLENECK_MARGIN_PCT
        else "cpu"
        if cpu - gpu >= BOTTLENECK_MARGIN_PCT
        else "mixed"
    )
    return PerfSummary(
        minutes=minutes,
        qualified=minutes >= QUALIFY_MINUTES,
        tier=tier,
        bottleneck=bottleneck,
        median_present_fps=1000.0 / median_ft if median_ft > 0 else 0.0,
        low_1pct_fps=1000.0 / p99_ft if p99_ft > 0 else 0.0,
        median_frametime_ms=median_ft,
        p99_frametime_ms=p99_ft,
        median_power_w=hw.power_w,
        median_clock_mhz=hw.clock_mhz,
        gpu_util_pct=gpu,
        cpu_peak_thread_pct=cpu,
        latency_ms=spec.latency_ms,
        framegen_seen=spec.framegen,
        median_temperature_c=hw.temperature_c,
        median_fan_pct=hw.fan_pct,
        latency_estimated=spec.latency_estimated,
    )


class SamplePerfSource:
    """The preview's ``PerfProfileSource``: rings generated on first read.

    ``clock`` is injectable so tests can advance a live ring by whole seconds;
    a live ring records one second of play per elapsed second, read lazily
    whenever ``record`` is called.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        specs: dict[str, RingSpec] | None = None,
        names: dict[str, str] | None = None,
        capture_off: frozenset[str] = CAPTURE_OFF,
        system_target_fps: float = SYSTEM_TARGET_FPS,
        power_limit_w: int = POWER_LIMIT_W,
    ) -> None:
        self._clock = clock
        self._specs = dict(SAMPLE_SPECS if specs is None else specs)
        self._names = dict(GAME_NAMES if names is None else names)
        self._capture_off = set(capture_off)
        self._rings: dict[str, _Ring] = {}
        self.system_target_fps = system_target_fps
        self.power_limit_w = power_limit_w

    # ----- PerfProfileSource -----
    def game_name(self, game_key: str) -> str:
        return self._names.get(game_key, game_key)

    def capture_enabled(self, game_key: str) -> bool:
        return game_key not in self._capture_off

    def record(self, game_key: str) -> PerfRecording | None:
        ring = self._ring(game_key)
        if ring is None:
            return None
        if ring.live:
            self._catch_up(ring)
        if ring.snapshot is None or ring.snapshot.version != ring.version:
            spec = ring.spec
            target = spec.target_fps or self.system_target_fps
            frames = tuple(ring.frames)
            ring.snapshot = PerfRecording(
                game_key=game_key,
                frametimes_ms=frames,
                live=ring.live,
                tier=ring.tier,
                target_fps=target,
                target_is_game=bool(spec.target_fps),
                power_limit_w=self.power_limit_w,
                summary=summarize_ring(frames, spec, ring.tier, ring.hardware()),
                version=ring.version,
            )
        return ring.snapshot

    # ----- preview controls -----
    def keys(self) -> list[str]:
        return list(self._names)

    def has_recording(self, game_key: str) -> bool:
        return game_key in self._rings or game_key in self._specs

    def set_capture(self, game_key: str, enabled: bool) -> None:
        """The library's Capture telemetry switch. Turning it off keeps an
        existing ring; a game without one then explains capture is off."""
        if enabled:
            self._capture_off.discard(game_key)
        else:
            self._capture_off.add(game_key)

    def set_running(self, game_key: str, running: bool, *, tier: str = "") -> None:
        """Tie a ring to the library's launch lifecycle.

        A session records only when it runs on a tier (``tier`` is empty for
        Stock, an unwrapped game or Capture telemetry off) and capture is on
        for the game; stopping archives the ring. The recording takes the
        session's tier.
        """
        ring = self._ring(game_key)
        if not running:
            if ring is not None and ring.live:
                self._catch_up(ring)
                ring.live = False
                ring.version += 1
            return
        if tier not in _TIER_POWER or not self.capture_enabled(game_key):
            return
        if ring is None:
            ring = self._rings[game_key] = _new_ring(
                game_key, _fresh_spec(game_key, tier)
            )
            # A fresh spec is born live; the session clock starts now.
            ring.live = False
        if not ring.live:
            ring.live = True
            ring.started_at, ring.recorded_s = self._clock(), 0
        ring.tier = tier
        ring.version += 1

    def _ring(self, game_key: str) -> _Ring | None:
        ring = self._rings.get(game_key)
        if ring is None and game_key in self._specs:
            ring = self._rings[game_key] = _new_ring(game_key, self._specs[game_key])
            ring.started_at = self._clock()
        return ring

    def _catch_up(self, ring: _Ring) -> None:
        due = int(self._clock() - ring.started_at) - ring.recorded_s
        if due <= 0:
            return
        ring.recorded_s += due
        ring.append(min(due, MAX_CATCH_UP_S) * 1000.0)
