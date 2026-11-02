"""Publish original NVAPI frame markers to the native graph without daemon polling."""
from __future__ import annotations

import ctypes
import mmap
import os
import tempfile
import time
from pathlib import Path

# Wine builds differ in the clock behind QueryPerformanceCounter: upstream Wine
# uses CLOCK_MONOTONIC, Proton uses CLOCK_MONOTONIC_RAW (hundreds of ms apart
# after a long uptime). A marker reaches the bridge a few ms after it was
# stamped, so the right mapping is the candidate that lands just behind now.
_FUTURE_US = 500
_WINDOW_US = 2_000_000


def monotonic_from_qpc(qpc_us: int, now_us: int, raw_to_monotonic_us: int) -> int | None:
    """Map a shim ``qpcUs`` onto CLOCK_MONOTONIC, or ``None`` if neither base fits."""
    if qpc_us <= 0:
        return None
    best: int | None = None
    for candidate in (qpc_us, qpc_us + raw_to_monotonic_us):
        if candidate > now_us + _FUTURE_US or now_us - candidate > _WINDOW_US:
            continue
        if best is None or abs(now_us - candidate) < abs(now_us - best):
            best = candidate
    return best


class BaseFramePublisher:
    def __init__(self, socket_path: Path, session: str, *, library_path: Path | None = None):
        self.path = socket_path.parent / f"base-timing.{session}.bin" if session.isdecimal() else None
        self.library_path = library_path or Path(__file__).resolve().parents[1] / "native_layer/libVkLayer_penguinburner_latency.so"
        self.mapping: mmap.mmap | None = None
        self.library = None
        self.pointer = 0
        self.identity: tuple[int, int] | None = None
        self.disabled = False

    def _open(self) -> bool:
        if self.mapping is not None:
            return True
        if self.disabled or self.path is None:
            return False
        temporary: str | None = None
        try:
            library = ctypes.CDLL(str(self.library_path))
            library.pb_base_timing_size.restype = ctypes.c_size_t
            library.pb_base_timing_publish.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint32]
            library.pb_base_timing_publish.restype = None
            size = library.pb_base_timing_size()
            if size != 8232:
                raise ValueError("unsupported native base timing ABI")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix=".base-timing-", dir=self.path.parent, delete=False) as file:
                temporary = file.name
                file.truncate(size)
                self.mapping = mmap.mmap(file.fileno(), size)
                status = os.fstat(file.fileno())
                self.identity = (status.st_dev, status.st_ino)
            self.pointer = ctypes.addressof(ctypes.c_char.from_buffer(self.mapping))
            self.library = library
            os.replace(temporary, self.path)
            return True
        except (OSError, ValueError, AttributeError):
            self.disabled = True
            self.close()
            return False
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def publish(self, frame_id: int, qpc_us: int, pid: int) -> None:
        now = time.monotonic_ns() // 1000
        raw_to_monotonic = now - time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW) // 1000
        timestamp = monotonic_from_qpc(qpc_us, now, raw_to_monotonic)
        if timestamp is None:
            return  # Unknown clock base or stale FIFO: leave the layer on its fallback graph.
        if self._open() and self.library is not None:
            self.library.pb_base_timing_publish(self.pointer, frame_id, timestamp, pid)

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
