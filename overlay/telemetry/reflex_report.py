"""Decode driver reports carried by the existing NVAPI marker FIFO."""

from __future__ import annotations

import json

REPORT_PREFIX = "[pb-reflex-report] "
_TIME_FIELDS = (
    "input_sample_us", "sim_start_us", "sim_end_us",
    "render_submit_start_us", "render_submit_end_us",
    "present_start_us", "present_end_us", "driver_start_us", "driver_end_us",
    "os_render_queue_start_us", "os_render_queue_end_us",
    "gpu_render_start_us", "gpu_render_end_us",
)


def parse_reflex_report(line: str, session_id: str) -> dict[str, object] | None:
    if not line.startswith(REPORT_PREFIX):
        return None
    try:
        data = json.loads(line[len(REPORT_PREFIX):])
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("pid", "present_id", *_TIME_FIELDS):
        value = data.get(key)
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            return None
    device = data.get("device")
    if not data["pid"] or not data["present_id"] or not isinstance(device, str):
        return None
    if not device or len(device) > 16 or any(c not in "0123456789abcdef" for c in device):
        return None
    sample: dict[str, object] = {key: data[key] for key in ("pid", "present_id", *_TIME_FIELDS)}
    sample.update(v=1, type="timing", measurement="driver-report",
                  source="nvapi-driver-report", device=device)
    if session_id:
        sample["session_id"] = session_id
    # Keep missing/invalid timing endpoints as reported. The daemon validates
    # ordering and selects the available scope; no synthetic input timestamp.
    return sample
