"""Publish original NVAPI frame markers to the native graph without daemon polling."""
from __future__ import annotations

import mmap
import os
import struct
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# Fixed little-endian layout shared with the reader in
# overlay/native/latency_layer/src/base_timing.cpp (BaseTimingBuffer). The
# sequence word is a seqlock: odd while a publish is in progress, even once
# the payload is complete, so the reader can retry a torn snapshot.
BUFFER_MAGIC = 0x50424231  # "PBB1"
RING_SLOTS = 512
_HEADER = struct.Struct("<6I2Q")  # sequence, magic, pid, index, count, reserved, frame_id, last_us
_SEQUENCE = struct.Struct("<I")
_SAMPLE = struct.Struct("<Q")
SAMPLES_OFFSET = _HEADER.size
TIMES_OFFSET = SAMPLES_OFFSET + _SAMPLE.size * RING_SLOTS
BUFFER_SIZE = TIMES_OFFSET + _SAMPLE.size * RING_SLOTS
_GAP_US = 2_000_000
_U32 = 0xFFFF_FFFF
_U64 = 0xFFFF_FFFF_FFFF_FFFF


def write_base_frame(buffer, frame_id: int, time_us: int, pid: int) -> bool:
    """Append one simulation-start timestamp to a base-timing buffer.

    Mirrors pb_base_timing_publish: the first publish, or a gap longer than
    two seconds since the previous one, rebinds the buffer to ``pid`` with an
    empty ring; otherwise a sample from another pid, a non-advancing frame id
    or a non-advancing timestamp is dropped. Returns whether anything was
    written. ``buffer`` is any writable byte buffer of at least BUFFER_SIZE.
    """
    if not 0 < frame_id <= _U64 or not 0 < time_us <= _U64 or not 0 < pid <= _U32:
        return False
    sequence, magic, bound_pid, index, count, _reserved, stored_frame, last_us = _HEADER.unpack_from(buffer)
    reset = magic != BUFFER_MAGIC or (time_us > last_us and time_us - last_us > _GAP_US)
    if not reset and (bound_pid != pid or frame_id <= stored_frame or time_us <= last_us):
        return False
    odd = ((sequence + 1) | 1) & _U32
    _SEQUENCE.pack_into(buffer, 0, odd)
    if reset:
        bound_pid, index, count, last_us = pid, 0, 0, 0
    if last_us:
        _SAMPLE.pack_into(buffer, SAMPLES_OFFSET + _SAMPLE.size * index, time_us - last_us)
        _SAMPLE.pack_into(buffer, TIMES_OFFSET + _SAMPLE.size * index, time_us)
        index = (index + 1) % RING_SLOTS
        count = min(count + 1, RING_SLOTS)
    _HEADER.pack_into(buffer, 0, odd, BUFFER_MAGIC, bound_pid, index, count, 0, frame_id, time_us)
    _SEQUENCE.pack_into(buffer, 0, (odd + 1) & _U32)
    return True


# Wine builds differ in the clock behind QueryPerformanceCounter: upstream Wine
# uses CLOCK_MONOTONIC, Proton uses CLOCK_MONOTONIC_RAW (hundreds of ms apart
# after a long uptime). A marker reaches the bridge a few ms after it was
# stamped, so the right mapping is the candidate that lands just behind now.
# The base is decided once per game process: picking per sample would flap
# between the two whenever the clocks are only a millisecond or two apart.
_FUTURE_US = 500
_WINDOW_US = 2_000_000
_REDECIDE_MISSES = 8
_MAX_TRACKED_PIDS = 16


def monotonic_from_qpc(qpc_us: int, now_us: int, raw_to_monotonic_us: int, *, raw: bool) -> int | None:
    """Map a shim ``qpcUs`` onto CLOCK_MONOTONIC from the given base, or ``None`` when it misses the window."""
    if qpc_us <= 0:
        return None
    candidate = qpc_us + raw_to_monotonic_us if raw else qpc_us
    if candidate > now_us + _FUTURE_US or now_us - candidate > _WINDOW_US:
        return None
    return candidate


def choose_clock_base(qpc_us: int, now_us: int, raw_to_monotonic_us: int) -> bool | None:
    """Pick the Wine clock base whose candidate lands in the window closest to now.

    Returns ``True`` for CLOCK_MONOTONIC_RAW, ``False`` for CLOCK_MONOTONIC and
    ``None`` when neither base fits.
    """
    best: bool | None = None
    best_distance = 0
    for raw in (False, True):
        candidate = monotonic_from_qpc(qpc_us, now_us, raw_to_monotonic_us, raw=raw)
        if candidate is None:
            continue
        distance = abs(now_us - candidate)
        if best is None or distance < best_distance:
            best, best_distance = raw, distance
    return best


@dataclass
class _ClockBase:
    raw: bool
    misses: int = 0


class QpcClockMapper:
    """Remember, per game pid, which Wine clock backs QueryPerformanceCounter."""

    def __init__(self) -> None:
        self._bases: dict[int, _ClockBase] = {}

    def base(self, pid: int) -> bool | None:
        state = self._bases.get(pid)
        return None if state is None else state.raw

    def map(self, pid: int, qpc_us: int, now_us: int, raw_to_monotonic_us: int) -> int | None:
        """Map ``qpc_us`` with the pid's sticky base, deciding or re-deciding it when needed."""
        if qpc_us <= 0:
            return None
        state = self._bases.get(pid)
        if state is not None:
            mapped = monotonic_from_qpc(qpc_us, now_us, raw_to_monotonic_us, raw=state.raw)
            if mapped is not None:
                state.misses = 0
                return mapped
            state.misses += 1
            if state.misses < _REDECIDE_MISSES:
                return None
            # A new process reused the pid, or the clocks jumped: decide afresh.
            del self._bases[pid]
        raw = choose_clock_base(qpc_us, now_us, raw_to_monotonic_us)
        if raw is None:
            return None
        while len(self._bases) >= _MAX_TRACKED_PIDS:
            del self._bases[next(iter(self._bases))]
        self._bases[pid] = _ClockBase(raw)
        return monotonic_from_qpc(qpc_us, now_us, raw_to_monotonic_us, raw=raw)


class BaseFramePublisher:
    def __init__(self, socket_path: Path, session: str):
        self.path = socket_path.parent / f"base-timing.{session}.bin" if session.isdecimal() else None
        self.mapping: mmap.mmap | None = None
        self.identity: tuple[int, int] | None = None
        self.disabled = False
        self.clock = QpcClockMapper()

    def _open(self) -> bool:
        if self.mapping is not None:
            return True
        if self.disabled or self.path is None:
            return False
        temporary: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix=".base-timing-", dir=self.path.parent, delete=False) as file:
                temporary = file.name
                file.truncate(BUFFER_SIZE)
                self.mapping = mmap.mmap(file.fileno(), BUFFER_SIZE)
                status = os.fstat(file.fileno())
                self.identity = (status.st_dev, status.st_ino)
            os.replace(temporary, self.path)
            return True
        except OSError:
            self.disabled = True
            self.close()
            return False
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def publish(self, frame_id: int, qpc_us: int, pid: int) -> None:
        now = time.monotonic_ns() // 1000
        raw_to_monotonic = now - time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW) // 1000
        timestamp = self.clock.map(pid, qpc_us, now, raw_to_monotonic)
        if timestamp is None:
            return  # Unknown clock base or stale FIFO: leave the layer on its fallback graph.
        if self._open() and self.mapping is not None:
            write_base_frame(self.mapping, frame_id, timestamp, pid)

    def close(self) -> None:
        if self.mapping is not None:
            self.mapping.close()
            self.mapping = None
        if self.path is not None and self.identity is not None:
            try:
                status = self.path.lstat()
                if (status.st_dev, status.st_ino) == self.identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass
        self.identity = None
