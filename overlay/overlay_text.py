from __future__ import annotations

from .config import (
    FOOT_ROW,
    HERO_ROW,
    OVERLAY_HUD_ROWS,
    OVERLAY_NON_TEXT_ITEM_IDS,
    OVERLAY_ROW_GROUPS,
    RULE_ROW,
    OverlayConfig,
    default_overlay_config,
    normalize_overlay_config,
)

SAMPLE_OVERLAY_VALUES = {
    "present_fps": "60",
    "fps_1pct_low": "47",
    "frametime_p97": "21.3",
    "fps_session_min": "38",
    "fps_session_avg": "58",
    "fps_session_max": "61",
    # The desktop app cannot see a running game's device, so these are shaped
    # like the real thing rather than pretending to be it.
    "gpu_name": "GPU model name",
    "driver_version": "580.76",
    "resolution": "2560x1440",
    "present_mode": "fifo",
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
    "hotspot_temperature_c": "79",
    "latency_ms": "40",
    "display_latency_ms": "",
    "uv_offset_mv": "-75",
    # The memory and host numbers the daemon publishes; without them the
    # preview drops those rows whenever no game is running.
    "mem_clock_mhz": "10501",
    "vram_used_mb": "9626",
    "vram_total_mb": "16376",
    "ram_used_mb": "14540",
    "ram_total_mb": "32768",
    "cpu_temp_c": "61",
    "cpu_power_w": "88",
}


# Colour roles, mirrored in the layer. Labels are dim, a value sitting where
# you want it reads good, and the active tier gets its own accent.
CELL_NORMAL = 0
CELL_WARN = 1
CELL_ALERT = 2
CELL_DIM = 3
CELL_GOOD = 4
CELL_TIER = 5

# The temperature the mockup starts warning at. Earlier than the point a card
# throttles, because this is a tuning HUD: the useful moment is the one before
# the clocks move.
# A CPU is expected to sit hotter than a card, so the same colours arrive
# later on it. These are the points a tuner starts caring, not thermal limits.
WARN_CPU_TEMPERATURE_C = 85
ALERT_CPU_TEMPERATURE_C = 95
WARN_TEMPERATURE_C = 72
ALERT_TEMPERATURE_C = 80


def format_overlay_text(
    values: dict[str, str],
    *,
    config: OverlayConfig | None = None,
) -> str:
    """The whole readout, as the rows the overlay draws joined by newlines."""
    config = normalize_overlay_config(config or default_overlay_config(enabled=True))
    if config.layout == "hud":
        rows = _hud_text_rows(values, config)
        return "\n".join(rows) if rows else "PB waiting"
    rows = format_overlay_rows(values, config=config)
    return "\n".join(rows) if rows else "PB waiting"


def format_overlay_hud_rows(
    values: dict[str, str],
    *,
    config: OverlayConfig | None = None,
) -> list[dict]:
    """The designed layout as typed rows, the way the layer builds them.

    Each row is ``{"kind", "label", "cells"}`` where a cell is
    ``{"text", "level"}``. A row whose items are all switched off is dropped,
    and a rule with nothing after it goes with them.
    """
    config = normalize_overlay_config(config or default_overlay_config(enabled=True))
    enabled = set(config.enabled_item_ids)
    rows: list[dict] = []
    for kind, label, item_ids in OVERLAY_HUD_ROWS:
        if kind == RULE_ROW:
            if rows and rows[-1]["kind"] != RULE_ROW:
                rows.append({"kind": RULE_ROW, "label": "", "cells": []})
            continue
        cells = []
        for item_id in item_ids:
            if item_id not in enabled:
                continue
            text = format_overlay_value(item_id, values)
            if text:
                cells.append({"text": text, "level": overlay_value_level(item_id, values)})
        if not cells:
            continue
        if kind == HERO_ROW:
            # The unit is its own cell: the renderer draws the number at twice
            # the body size and drops "fps" onto the same baseline beside it.
            cells.append({"text": "fps", "level": CELL_DIM})
        if kind == FOOT_ROW:
            joined = " | ".join(cell["text"] for cell in cells)
            rows.append(
                {"kind": FOOT_ROW, "label": "", "cells": [{"text": joined, "level": CELL_DIM}]}
            )
            continue
        rows.append({"kind": kind, "label": label, "cells": cells})
    while rows and rows[-1]["kind"] == RULE_ROW:
        rows.pop()
    return rows


def overlay_value_level(item_id: str, values: dict[str, str]) -> int:
    """Which colour role a value reads in."""
    if item_id == "profile":
        return CELL_TIER
    if item_id == "uv_offset_mv":
        return CELL_GOOD
    if item_id in ("temperature_c", "cpu_temp_c"):
        # A CPU runs hotter than a GPU for the same comfort, so it gets its own
        # thresholds rather than borrowing the card's.
        warn, alert = (
            (WARN_CPU_TEMPERATURE_C, ALERT_CPU_TEMPERATURE_C)
            if item_id == "cpu_temp_c"
            else (WARN_TEMPERATURE_C, ALERT_TEMPERATURE_C)
        )
        celsius = _ms_number(values.get(item_id))
        if celsius is None:
            return CELL_NORMAL
        if celsius >= alert:
            return CELL_ALERT
        if celsius >= warn:
            return CELL_WARN
        return CELL_GOOD
    return CELL_NORMAL


def _hud_text_rows(values: dict[str, str], config: OverlayConfig) -> list[str]:
    """The designed layout flattened to text, for the preview and the harness."""
    rows = format_overlay_hud_rows(values, config=config)
    if not rows:
        return []
    label_width = max(
        (len(row["label"]) for row in rows if row["kind"] != RULE_ROW), default=0
    )
    lines: list[str] = []
    for row in rows:
        if row["kind"] == RULE_ROW:
            lines.append("")  # filled in once the width is known
            continue
        body = " ".join(cell["text"] for cell in row["cells"])
        if row["kind"] in (HERO_ROW, FOOT_ROW) or not label_width:
            lines.append(body)
        else:
            lines.append(f"{row['label']:<{label_width}} {body}".rstrip())
    width = max((len(line) for line in lines), default=0)
    return [line if line else "-" * width for line in lines]


def format_overlay_rows(
    values: dict[str, str],
    *,
    config: OverlayConfig | None = None,
) -> list[str]:
    """One string per drawn row, with columns padded so they line up.

    Rows come from OVERLAY_ROW_GROUPS, so the readout reads as a table rather
    than one long sentence. The overlay font is fixed-pitch, which is what lets
    plain space padding align a column.
    """
    config = normalize_overlay_config(config or default_overlay_config(enabled=True))
    enabled = set(config.enabled_item_ids)
    rows = []
    for group in OVERLAY_ROW_GROUPS:
        cells = [
            cell
            for item_id in group
            if item_id in enabled
            for cell in (format_overlay_item(item_id, values),)
            if cell
        ]
        if cells:
            rows.append(cells)
    return _align_overlay_columns(rows)


def _align_overlay_columns(rows: list[list[str]]) -> list[str]:
    widths: dict[int, int] = {}
    for cells in rows:
        for column, cell in enumerate(cells):
            widths[column] = max(widths.get(column, 0), len(cell))
    return [
        " ".join(
            cell.ljust(widths[column]) for column, cell in enumerate(cells)
        ).rstrip()
        for cells in rows
    ]


def short_gpu_name(value: object) -> str:
    """Drop the vendor words the driver prefixes every model name with.

    The driver reports "NVIDIA GeForce RTX 5070 Ti". In a HUD footer the first
    two words are the same on every card the overlay will ever run on, and they
    cost real width: the footer sets the panel's width, so the prefix alone
    stretched the box well past what the rows needed.
    """
    name = str(value or "").strip()
    for prefix in ("NVIDIA ", "GeForce "):
        if name.startswith(prefix):
            name = name[len(prefix):].lstrip()
    return name


def _gibibyte_pair(used: object, total: object) -> str:
    """"9.4 / 16 GiB" from two mebibyte counts.

    The used figure carries a decimal because it is what moves; the total does
    not, because a card or a board does not grow one mid-game and the extra
    digit would only cost width.
    """
    used_mb = _int_or_none(used)
    total_mb = _int_or_none(total)
    if total_mb is None or total_mb <= 0:
        return ""
    if used_mb is None:
        return ""
    return f"{used_mb / 1024.0:.1f} / {round(total_mb / 1024.0)} GiB"


def _int_or_none(value: object) -> int | None:
    try:
        return round(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def format_overlay_value(item_id: str, values: dict[str, str]) -> str:
    """One metric with no label of its own.

    The designed layout puts the label in its own dim column, so the value has
    to arrive bare: "27 ms", not "LAT 27 ms".
    """
    if item_id in OVERLAY_NON_TEXT_ITEM_IDS:
        return ""
    if item_id == "base_fps":
        return _value_or_na(_fps_display_value(values.get("present_fps")), suffix="")
    if item_id == "fg_fps":
        if not _flag_enabled(values.get("framegen_active")):
            return ""
        if _fps_number(values.get("framegen_fps")) is None:
            return ""
        return f"{_framegen_value_or_na(values.get('framegen_fps'))} fps"
    if item_id == "latency_ms":
        return _optional_value(_combined_latency_ms(values), suffix=" ms")
    if item_id == "profile":
        # The designed layout has room for the tier's real name.
        text = str(values.get("profile_tier") or "").strip()
        return text or "Default"
    if item_id == "clock_mhz":
        return _value_or_na(values.get("clock_mhz"), suffix=" MHz")
    if item_id == "voltage_mv":
        return _value_or_na(_requested_voltage_mv(values), suffix=" mV")
    if item_id == "power_w":
        return _value_or_na(values.get("power_w"), suffix=" W")
    if item_id == "temperature_c":
        return _optional_value(values.get("temperature_c"), suffix=" C")
    if item_id == "hotspot_temperature_c":
        return _optional_value(values.get("hotspot_temperature_c"), suffix=" C")
    if item_id == "gpu_util_pct":
        return _optional_value(values.get("gpu_util_pct"), suffix="%")
    if item_id == "fan_pct":
        return _optional_labeled_value("fan", values.get("fan_pct"), suffix="%")
    if item_id == "cpu_util_pct":
        return _optional_value(values.get("cpu_util_pct"), suffix="%")
    if item_id == "cpu_peak_thread_pct":
        return _optional_labeled_value(
            "peak", values.get("cpu_peak_thread_pct"), suffix="%"
        )
    if item_id == "uv_offset_mv":
        return _optional_value(_signed_value(values.get("uv_offset_mv")), suffix=" mV")
    if item_id == "mem_clock_mhz":
        return _optional_value(values.get("mem_clock_mhz"), suffix=" MHz")
    if item_id == "vram":
        return _gibibyte_pair(values.get("vram_used_mb"), values.get("vram_total_mb"))
    if item_id == "ram":
        return _gibibyte_pair(values.get("ram_used_mb"), values.get("ram_total_mb"))
    if item_id == "cpu_temp_c":
        return _optional_value(values.get("cpu_temp_c"), suffix=" C")
    if item_id == "cpu_power_w":
        return _optional_value(values.get("cpu_power_w"), suffix=" W")
    if item_id == "fps_1pct_low":
        return _optional_value(values.get("fps_1pct_low"), suffix=" fps")
    if item_id == "frametime_p97":
        return _optional_value(values.get("frametime_p97"), suffix=" ms")
    if item_id == "fps_session":
        return _session_fps_value(values).replace("min/avg/max ", "")
    if item_id == "gpu_name":
        return short_gpu_name(values.get("gpu_name"))
    if item_id == "driver_version":
        return _optional_labeled_value("driver", values.get("driver_version"), suffix="")
    if item_id == "resolution":
        return str(values.get("resolution") or "").strip()
    if item_id == "present_mode":
        return str(values.get("present_mode") or "").strip()
    return ""


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
    if values:
        # A live daemon may report an unavailable hotspot, or be an older
        # version without the field. Do not present the demo temperature as
        # a measured reading in either case.
        merged["hotspot_temperature_c"] = str(
            values.get("hotspot_temperature_c") or ""
        ).strip()
    return format_overlay_text(merged, config=config)


def _format_overlay_item(item_id: str, values: dict[str, str]) -> str:
    if item_id in OVERLAY_NON_TEXT_ITEM_IDS:
        # The layer draws these; they contribute nothing to the readout text.
        return ""
    if item_id == "base_fps":
        return _value_or_na(_fps_display_value(values.get("present_fps")), suffix=" FPS")
    if item_id == "fg_fps":
        if not _flag_enabled(values.get("framegen_active")):
            return ""
        if _fps_number(values.get("framegen_fps")) is None:
            return ""
        return f"{_framegen_value_or_na(values.get('framegen_fps'))} FG"
    if item_id == "fps_1pct_low":
        # Hidden until the layer has measured something: a 0 here would read as
        # a catastrophic stutter rather than as "no data yet".
        return _optional_labeled_value("1% low", values.get("fps_1pct_low"), suffix="")
    if item_id == "frametime_p97":
        return _optional_labeled_value("p97", values.get("frametime_p97"), suffix=" ms")
    if item_id == "fps_session":
        return _session_fps_value(values)
    if item_id == "gpu_name":
        return short_gpu_name(values.get("gpu_name"))
    if item_id == "driver_version":
        return _optional_labeled_value(
            "driver", values.get("driver_version"), suffix=""
        )
    if item_id == "resolution":
        return str(values.get("resolution") or "").strip()
    if item_id == "present_mode":
        return str(values.get("present_mode") or "").strip()
    if item_id == "clock_mhz":
        return _value_or_na(values.get("clock_mhz"), suffix=" MHz")
    if item_id == "voltage_mv":
        return _value_or_na(_requested_voltage_mv(values), suffix=" mV")
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
    if item_id == "hotspot_temperature_c":
        return _optional_labeled_value(
            "HOT", values.get("hotspot_temperature_c"), suffix=" C"
        )
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
    if item_id == "mem_clock_mhz":
        return _optional_labeled_value("MEM", values.get("mem_clock_mhz"), suffix=" MHz")
    if item_id == "vram":
        pair = _gibibyte_pair(values.get("vram_used_mb"), values.get("vram_total_mb"))
        return f"VRAM {pair}" if pair else ""
    if item_id == "ram":
        pair = _gibibyte_pair(values.get("ram_used_mb"), values.get("ram_total_mb"))
        return f"RAM {pair}" if pair else ""
    if item_id == "cpu_temp_c":
        return _optional_labeled_value("CPU", values.get("cpu_temp_c"), suffix=" C")
    if item_id == "cpu_power_w":
        return _optional_labeled_value("CPU", values.get("cpu_power_w"), suffix=" W")
    return ""


def _session_fps_value(values: dict[str, str]) -> str:
    """Whole-session slowest, average and fastest frame rate, as one cell.

    The layer keeps running totals for these; the average is taken over the
    session's total frame time rather than over per-frame rates, so a burst of
    cheap frames cannot drag it upwards.
    """
    average = str(values.get("fps_session_avg") or "").strip()
    if not average or average.lower() == "n/a":
        return ""
    slowest = str(values.get("fps_session_min") or "").strip() or "0"
    fastest = str(values.get("fps_session_max") or "").strip() or "0"
    return f"min/avg/max {slowest}/{average}/{fastest}"


def _requested_voltage_mv(values: dict[str, str]) -> str:
    """Voltage the active profile asked for, falling back to the live reading.

    The in-game layer resolves ``voltage_mv`` from the profile id whenever the
    id encodes one (``uv_925mv_2820mhz``), so the HUD shows the voltage the
    profile requested rather than whatever the card happens to be at this
    instant. The preview has to do the same or it drifts from the screen.
    """
    profile_id = str(values.get("profile_id") or "").strip().lower()
    index = profile_id.find("mv")
    while index != -1:
        start = index
        while start > 0 and profile_id[start - 1].isdigit():
            start -= 1
        if start < index:
            return profile_id[start:index]
        index = profile_id.find("mv", index + 2)
    return str(values.get("voltage_mv") or "")


def _combined_latency_ms(values: dict[str, str]) -> str:
    """Render latency = render-latency tail + present->scanout display tail.

    The two metrics are kept separate upstream (receiver/state); the overlay
    sums them into one click-to-photon number. When the display tail is absent
    (no VK_KHR_present_wait data), this collapses to the render latency alone,
    exactly as before.
    """
    render = _ms_number(values.get("latency_ms"))
    if render is None:
        return ""
    display = _ms_number(values.get("display_latency_ms"))
    total = render if display is None else render + display
    return str(total)


def _ms_number(value: object) -> int | None:
    text = str(value or "").strip()
    if not text or text.lower() == "n/a":
        return None
    if text.lower().endswith(" ms"):
        text = text[:-3].strip()
    try:
        return round(float(text))
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
        number = round(float(text))
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
