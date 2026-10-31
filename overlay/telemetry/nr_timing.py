"""NR GPU records on the existing shim FIFO; durations are GPU microseconds."""

from __future__ import annotations

import math
import re

_RECORD = re.compile(
    r"^pb-nr-gpu: pid=(\d+) device=([0-9a-fA-Fx]+) sample=(\d+) "
    r"gpuUs=([0-9.]+) api=(d3d12|vulkan) scope=model\s*$"
)


def parse_nr_timing(line: str, session_id: str) -> dict[str, object] | None:
    """Accept only explicitly scoped, finite measurements tied to this launch."""
    match = _RECORD.fullmatch(line)
    if not match or not session_id:
        return None
    pid, device, serial, duration, api = match.groups()
    try:
        gpu_us = float(duration)
    except ValueError:
        return None
    if not math.isfinite(gpu_us) or not 0 < gpu_us < 1_000_000:
        return None
    if int(pid) <= 0 or int(serial) <= 0:
        return None
    return {
        "v": 1,
        "type": "timing",
        "measurement": "nr-gpu",
        "source": "ngx-runtime",
        "scope": "model",
        "api": api,
        "session_id": session_id,
        "pid": int(pid),
        "device": device,
        "sample_id": int(serial),
        "nr_gpu_us": gpu_us,
    }
