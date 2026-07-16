from __future__ import annotations

from .config import OverlayConfig
from .config import default_overlay_config
from .config import normalize_overlay_config


SAMPLE_OVERLAY_VALUES = {
    "present_fps": "60",
    "framegen_fps": "120",
    "framegen_active": "1",
    "clock_mhz": "2820",
    "voltage_mv": "925",
    "power_w": "318",
    "profile_tier": "Balanced",
    "gpu_util_pct": "97",
    "cpu_util_pct": "32",
    "cpu_peak_thread_pct": "98",
    "fan_pct": "62",
    "temperature_c": "67",
    "latency_ms": "40",
    "display_latency_ms": "",
    "uv_offset_mv": "-75",
}


def format_overlay_text(
    values: dict[str, str],
    *,
    config: OverlayConfig | None = None,
) -> str:
    config = normalize_overlay_config(config or default_overlay_config(enabled=True))
    parts = []
    for item_id in config.enabled_item_ids:
        part = format_overlay_item(item_id, values)
        if part:
            parts.append(part)
    return " ".join(parts) if parts else "PB waiting"


def format_overlay_item(item_id: str, values: dict[str, str]) -> str:
    """Format one overlay metric using current telemetry values."""
    return _format_overlay_item(item_id, values)


def preview_overlay_text(
    values: dict[str, str] | None = None,
    *,
    config: OverlayConfig | None = None,
) -> str:
    merged = dict(SAMPLE_OVERLAY_VALUES)
    for key, value in (values or {}).items():
        text = str(value or "").strip()
        if key == "framegen_active" and not _flag_enabled(text):
            continue
        if text:
            merged[key] = text
    return format_overlay_text(merged, config=config)


def _format_overlay_item(item_id: str, values: dict[str, str]) -> str:
    if item_id == "base_fps":
        return _value_or_na(_fps_display_value(values.get("present_fps")), suffix=" FPS")
    if item_id == "fg_fps":
        if not _flag_enabled(values.get("framegen_active")):
            return ""
        if _fps_number(values.get("framegen_fps")) is None:
            return ""
        return f"{_framegen_value_or_na(values.get('framegen_fps'))} FG"
    if item_id == "clock_mhz":
        return _value_or_na(values.get("clock_mhz"), suffix=" MHz")
    if item_id == "voltage_mv":
        return _value_or_na(values.get("voltage_mv"), suffix=" mV")
    if item_id == "power_w":
        return _value_or_na(values.get("power_w"), suffix=" W")
    if item_id == "profile":
        return _compact_tier(values.get("profile_tier"))
    if item_id == "gpu_util_pct":
        return _optional_labeled_value("GPU", values.get("gpu_util_pct"), suffix="%")
    if item_id == "cpu_util_pct":
        return _labeled_value_or_placeholder(
            "CPU",
            values.get("cpu_util_pct"),
            suffix="%",
            placeholder="--%",
        )
    if item_id == "cpu_peak_thread_pct":
        return _labeled_value_or_placeholder(
            "CPU-T",
            values.get("cpu_peak_thread_pct"),
            suffix="%",
            placeholder="--%",
        )
    if item_id == "fan_pct":
        return _optional_labeled_value("Fan", values.get("fan_pct"), suffix="%")
    if item_id == "temperature_c":
        return _optional_labeled_value("T", values.get("temperature_c"), suffix=" C")
    if item_id == "latency_ms":
        return _optional_labeled_value(
            "LAT", _combined_latency_ms(values), suffix=" ms"
        )
    if item_id == "uv_offset_mv":
        return _optional_labeled_value(
            "UV",
            _signed_value(values.get("uv_offset_mv")),
            suffix=" mV",
        )
    return ""


def _combined_latency_ms(values: dict[str, str]) -> str:
    """Render latency = render-latency tail + present->scanout display tail.

    The two metrics are kept separate upstream (receiver/state); the overlay
    sums them into one click-to-photon number. When the display tail is absent
    (no VK_KHR_present_wait data), this collapses to the render latency alone.
    For non-Reflex games with no marker latency at all, the display tail is
    still a real measurement and is shown on its own rather than nothing.
    """
    render = _ms_number(values.get("latency_ms"))
    display = _ms_number(values.get("display_latency_ms"))
    if render is None:
        return "" if display is None else str(display)
    total = render if display is None else render + display
    return str(total)


def _ms_number(value: object) -> int | None:
    text = str(value or "").strip()
    if not text or text.lower() == "n/a":
        return None
    if text.lower().endswith(" ms"):
        text = text[:-3].strip()
    try:
        return int(round(float(text)))
    except ValueError:
        return None


def _value_or_na(value: object, *, suffix: str) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "n/a":
        return f"n/a{suffix}"
    if text.endswith(suffix):
        return text
    return f"{text}{suffix}"


def _optional_value(value: object, *, suffix: str) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "n/a":
        return ""
    if text.endswith(suffix):
        return text
    return f"{text}{suffix}"


def _optional_labeled_value(label: str, value: object, *, suffix: str) -> str:
    text = _optional_value(value, suffix=suffix)
    if not text:
        return ""
    return f"{label} {text}"


def _labeled_value_or_placeholder(
    label: str,
    value: object,
    *,
    suffix: str,
    placeholder: str,
) -> str:
    text = _optional_value(value, suffix=suffix)
    return f"{label} {text or placeholder}"


def _signed_value(value: object) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "n/a":
        return ""
    lower = text.lower()
    for suffix in (" mv", "mv"):
        if lower.endswith(suffix):
            text = text[: -len(suffix)].strip()
            break
    try:
        number = int(round(float(text)))
    except ValueError:
        return text
    return f"{number:+d}"


def _framegen_value_or_na(value: object) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "n/a":
        return "n/a"
    lower = text.lower()
    if lower.endswith(" fps"):
        return text[:-4].strip()
    if lower.endswith("fps"):
        return text[:-3].strip()
    return text


def _fps_number(value: object) -> float | None:
    text = str(value or "").strip().lower()
    if not text or text == "n/a":
        return None
    if text.endswith("fps"):
        text = text[:-3].strip()
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number > 0 else None


def _fps_display_value(value: object) -> str:
    return str(value or "").strip() if _fps_number(value) is not None else ""


def _flag_enabled(value: object) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _compact_tier(value: object) -> str:
    # No tier means stock/default GPU state (Restore Defaults, per-game
    # Stock/Default): show DEF, never masquerade as a tuned tier.
    text = str(value or "").strip()
    lower = text.lower()
    if not text or lower in ("stock", "default"):
        return "DEF"
    if lower == "balanced":
        return "BAL"
    if lower == "efficiency":
        return "EFF"
    if lower == "performance":
        return "PERF"
    return text[:6].upper()
