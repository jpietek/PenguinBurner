from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from common.atomic_write import atomic_write_text
from common.penguin_burner_paths import default_user_config_dir
from overlay.telemetry.steam_launch_check import PENGUIN_BURNER_WRAPPER

OVERLAY_CONFIG_ENV = "PENGUIN_BURNER_OVERLAY_CONFIG"
STEAM_LAUNCH_OPTION = f"{PENGUIN_BURNER_WRAPPER} %command%"


def steam_launch_option(*, ingame_latency_enabled: bool = False) -> str:
    """Steam launch string for the overlay.

    ``PB_OVERLAY=1`` turns the overlay on. Flatpak installs register the native
    Vulkan layer for ``PENGUIN_BURNER=1``; the wrapper also wires the layer when
    Steam executes it directly. ``PB_INGAME_LATENCY=1`` enables dxvk-nvapi
    marker parsing for titles where native Vulkan markers are not enough.
    """
    tokens = ["PB_OVERLAY=1"]
    if ingame_latency_enabled:
        tokens.append("PB_INGAME_LATENCY=1")
    tokens.append(STEAM_LAUNCH_OPTION)
    return " ".join(tokens)


# Overlay on with latency -- the default copy string.
STEAM_LAUNCH_OPTION_OVERLAY = steam_launch_option()
# Backwards-compatible name.
STEAM_LAUNCH_OPTION_WITH_LATENCY = STEAM_LAUNCH_OPTION_OVERLAY
# Explicit dxvk-nvapi marker latency path.
STEAM_LAUNCH_OPTION_WITH_INGAME_LATENCY = steam_launch_option(
    ingame_latency_enabled=True
)
# Backwards-compatible name.
STEAM_LAUNCH_OPTION_WITH_TRACE_FALLBACK = STEAM_LAUNCH_OPTION_WITH_INGAME_LATENCY

BASIC_OVERLAY_ITEM_IDS = (
    "base_fps",
    "fg_fps",
    "latency_ms",
    "clock_mhz",
    "voltage_mv",
    "power_w",
    "profile",
)
ADVANCED_OVERLAY_ITEM_IDS = (
    "fps_1pct_low",
    "frametime_p97",
    "fps_session",
    "frametime_graph",
    "gpu_name",
    "driver_version",
    "resolution",
    "present_mode",
    "gpu_util_pct",
    "cpu_util_pct",
    "cpu_peak_thread_pct",
    "fan_pct",
    "temperature_c",
    "hotspot_temperature_c",
    "uv_offset_mv",
    "mem_clock_mhz",
    "vram",
    "ram",
    "cpu_temp_c",
    "cpu_power_w",
)
OVERLAY_ITEM_IDS = (
    "base_fps",
    "fg_fps",
    "latency_ms",
    "clock_mhz",
    "voltage_mv",
    "power_w",
    "profile",
    "fps_1pct_low",
    "frametime_p97",
    "fps_session",
    "frametime_graph",
    "gpu_name",
    "driver_version",
    "resolution",
    "present_mode",
    "gpu_util_pct",
    "cpu_util_pct",
    "cpu_peak_thread_pct",
    "fan_pct",
    "temperature_c",
    "hotspot_temperature_c",
    "uv_offset_mv",
    "mem_clock_mhz",
    "vram",
    "ram",
    "cpu_temp_c",
    "cpu_power_w",
)
DEFAULT_ENABLED_OVERLAY_ITEM_IDS = BASIC_OVERLAY_ITEM_IDS
# How the readout breaks into rows. Ten metrics on one line is a sentence, not
# a HUD; grouping them by what a tuner reads together turns it into a table.
# Every item id appears in exactly one group, and a group with nothing enabled
# in it draws no row at all. The native layer groups them the same way.
# Items the layer draws rather than writes, so they never produce a cell and
# never pull a row into the readout on their own.
OVERLAY_NON_TEXT_ITEM_IDS = ("frametime_graph",)
# How the settings tab groups the field list. Basic/Advanced above is what the
# detail presets are built from; this is what a reader scans, so it groups by
# subject the way the readout itself does. Every item id appears exactly once.
OVERLAY_ITEM_SECTIONS = (
    (
        "Frame rate",
        (
            "base_fps",
            "fg_fps",
            "latency_ms",
            "fps_1pct_low",
            "frametime_p97",
            "fps_session",
            "frametime_graph",
        ),
    ),
    (
        "GPU",
        (
            "clock_mhz",
            "voltage_mv",
            "mem_clock_mhz",
            "power_w",
            "temperature_c",
            "hotspot_temperature_c",
            "gpu_util_pct",
            "fan_pct",
            "vram",
            "uv_offset_mv",
            "profile",
        ),
    ),
    (
        "CPU and system",
        (
            "cpu_util_pct",
            "cpu_peak_thread_pct",
            "cpu_temp_c",
            "cpu_power_w",
            "ram",
        ),
    ),
    (
        "Device",
        ("gpu_name", "driver_version", "resolution", "present_mode"),
    ),
)

OVERLAY_ROW_GROUPS = (
    ("base_fps", "fg_fps", "latency_ms", "profile"),
    ("fps_1pct_low", "frametime_p97", "fps_session"),
    ("frametime_graph",),
    ("clock_mhz", "voltage_mv", "uv_offset_mv"),
    ("power_w", "temperature_c", "hotspot_temperature_c", "fan_pct"),
    ("gpu_util_pct", "cpu_util_pct", "cpu_peak_thread_pct"),
    ("mem_clock_mhz", "vram", "ram"),
    ("cpu_temp_c", "cpu_power_w"),
    ("gpu_name", "driver_version", "resolution", "present_mode"),
)
DEFAULT_OVERLAY_UPDATE_INTERVAL_S = 1
MIN_OVERLAY_UPDATE_INTERVAL_S = 1
MAX_OVERLAY_UPDATE_INTERVAL_S = 10
# Manual overlay scale multiplier applied on top of the native overlay's
# resolution-adaptive (1080p/1440p/4K) sizing. 1.0 keeps the adaptive default;
# the steps either side shrink/grow it. Values are snapped to the nearest
# offered option. The fractional steps only reach the smooth faces: the pixel
# face is magnified by whole pixels, so it uses the nearest whole size.
OVERLAY_SCALE_OPTIONS = (0.5, 0.75, 1.0, 1.5, 2.0)
# What 1x means: the layer's resolution-adaptive size times this. 0.75 makes
# 1x at 1440p draw ~16 px capitals, MangoHud's default text size; the old 1x
# read as oversized in game. Kept in step with kOverlayScaleReference in the
# layer.
OVERLAY_SCALE_REFERENCE = 0.75
DEFAULT_OVERLAY_SCALE = 1.0
# Where the HUD box sits in the game's image. The native layer understands the
# same five names; anything else falls back to the historical top-right.
OVERLAY_POSITION_IDS = (
    "top_left",
    "top_center",
    "top_right",
    "bottom_left",
    "bottom_right",
)
DEFAULT_OVERLAY_POSITION = "top_right"
# Panel opacity. Only the blended renderer can honour it: a cleared rectangle
# writes its colour straight into the image, so the clear-rect fallback always
# draws a solid box. The floor keeps a fully dragged-down slider readable
# rather than invisible.
MIN_OVERLAY_OPACITY = 0.2
MAX_OVERLAY_OPACITY = 1.0
DEFAULT_OVERLAY_OPACITY = 0.85
# "blended" uses the Vulkan pipeline where the driver gives us one, which is
# what makes translucency and rounded corners possible. "clear_rect" forces the
# original renderer, as a way out if blending upsets a particular game.
OVERLAY_RENDERERS = ("blended", "clear_rect")
DEFAULT_OVERLAY_RENDERER = "blended"
# Which face the overlay draws text in. "pixel" is the original 5x8 bitmap and
# the only one the clear-rect renderer can draw; the other two come from a
# signed-distance atlas and need the blended renderer, which falls back to
# pixel on its own when it cannot have one.
OVERLAY_FONTS = ("pixel", "mono", "sans")
# Sans by default. Every fixed-pitch face available draws a slashed or dotted
# zero, and that mark is the first thing to close up once the text is only a
# few pixels tall -- which is exactly where "2820" stopped reading as a number.
# The proportional face carries tabular figures, so the digits still hold their
# column and a counter that changes does not shuffle the row.
DEFAULT_OVERLAY_FONT = "sans"
# How the readout is arranged. "hud" is the designed layout: a headline frame
# rate, dim labels beside their values, rules between groups and a footer.
# "table" is the plain grid every field shares, which is what shipped first.
OVERLAY_LAYOUTS = ("hud", "table")
DEFAULT_OVERLAY_LAYOUT = "hud"

# The designed layout, as rows. Each entry is (kind, label, item ids); a row
# whose items are all switched off is not drawn, and nor is a rule that would
# end up next to nothing. Keeping one table here and the same one in the layer
# is what stops the two renderers drifting apart.
HERO_ROW = "hero"
RULE_ROW = "rule"
FOOT_ROW = "foot"
VALUE_ROW = "value"
OVERLAY_HUD_ROWS = (
    (HERO_ROW, "", ("base_fps",)),
    (VALUE_ROW, "fg", ("fg_fps",)),
    (VALUE_ROW, "lat", ("latency_ms",)),
    (VALUE_ROW, "tier", ("profile",)),
    (RULE_ROW, "", ()),
    (VALUE_ROW, "gpu", ("clock_mhz", "voltage_mv")),
    # A blank label continues the group above rather than repeating "gpu".
    (VALUE_ROW, "", ("power_w", "temperature_c")),
    (VALUE_ROW, "hotspot", ("hotspot_temperature_c",)),
    (VALUE_ROW, "load", ("gpu_util_pct", "fan_pct")),
    (VALUE_ROW, "mem", ("mem_clock_mhz",)),
    (VALUE_ROW, "uv", ("uv_offset_mv",)),
    (RULE_ROW, "", ()),
    (VALUE_ROW, "1% low", ("fps_1pct_low",)),
    (VALUE_ROW, "p97", ("frametime_p97",)),
    (VALUE_ROW, "session", ("fps_session",)),
    (VALUE_ROW, "vram", ("vram",)),
    (VALUE_ROW, "cpu", ("cpu_util_pct", "cpu_temp_c")),
    # Continuation of the cpu group, the way the second gpu row continues its.
    (VALUE_ROW, "", ("cpu_peak_thread_pct", "cpu_power_w")),
    (VALUE_ROW, "ram", ("ram",)),
    (FOOT_ROW, "", ("gpu_name", "driver_version", "resolution", "present_mode")),
)
# Named detail levels, so a player can go from "just the frame rate" to
# everything without visiting thirteen checkboxes. "custom" is the checkbox set
# itself and stays the default, so a config saved before detail levels existed
# keeps exactly the fields it had.
OVERLAY_DETAIL_PRESETS = {
    "minimal": ("base_fps",),
    "standard": ("base_fps", "fg_fps", "latency_ms", "profile"),
    "detailed": BASIC_OVERLAY_ITEM_IDS
    + (
        "fps_1pct_low",
        "gpu_util_pct",
        "cpu_util_pct",
        "temperature_c",
        "vram",
        "ram",
        "cpu_temp_c",
    ),
    "full": OVERLAY_ITEM_IDS,
}
OVERLAY_DETAIL_IDS = ("minimal", "standard", "detailed", "full", "custom")
CUSTOM_OVERLAY_DETAIL = "custom"
DEFAULT_OVERLAY_DETAIL = CUSTOM_OVERLAY_DETAIL


@dataclass(frozen=True, slots=True)
class OverlayConfig:
    enabled: bool = False
    enabled_item_ids: tuple[str, ...] = DEFAULT_ENABLED_OVERLAY_ITEM_IDS
    update_interval_s: int = DEFAULT_OVERLAY_UPDATE_INTERVAL_S
    scale: float = DEFAULT_OVERLAY_SCALE
    position: str = DEFAULT_OVERLAY_POSITION
    detail: str = DEFAULT_OVERLAY_DETAIL
    opacity: float = DEFAULT_OVERLAY_OPACITY
    renderer: str = DEFAULT_OVERLAY_RENDERER
    font: str = DEFAULT_OVERLAY_FONT
    layout: str = DEFAULT_OVERLAY_LAYOUT

    @property
    def latency_enabled(self) -> bool:
        return bool(self.enabled) and "latency_ms" in self.enabled_item_ids


def default_overlay_config_path(
    env: Mapping[str, str] | None = None,
) -> Path:
    resolved_env = os.environ if env is None else env
    explicit = str(resolved_env.get(OVERLAY_CONFIG_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return default_user_config_dir() / "overlay.toml"


def default_overlay_config(*, enabled: bool = False) -> OverlayConfig:
    return OverlayConfig(
        enabled=bool(enabled),
        enabled_item_ids=DEFAULT_ENABLED_OVERLAY_ITEM_IDS,
        update_interval_s=DEFAULT_OVERLAY_UPDATE_INTERVAL_S,
        scale=DEFAULT_OVERLAY_SCALE,
        position=DEFAULT_OVERLAY_POSITION,
        detail=DEFAULT_OVERLAY_DETAIL,
        opacity=DEFAULT_OVERLAY_OPACITY,
        renderer=DEFAULT_OVERLAY_RENDERER,
        font=DEFAULT_OVERLAY_FONT,
        layout=DEFAULT_OVERLAY_LAYOUT,
    )


def load_overlay_config(path: str | Path | None = None) -> OverlayConfig:
    config_path = (
        default_overlay_config_path()
        if path is None
        else Path(path).expanduser()
    )
    try:
        with config_path.open("rb") as config_file:
            payload = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError):
        return default_overlay_config()
    if not isinstance(payload, dict):
        return default_overlay_config()
    return normalize_overlay_config(
        OverlayConfig(
            enabled=_bool_value(payload.get("enabled"), default=False),
            enabled_item_ids=normalize_overlay_item_ids(
                payload.get("items", DEFAULT_ENABLED_OVERLAY_ITEM_IDS)
            ),
            update_interval_s=clamp_overlay_update_interval_s(
                payload.get("update_interval_s", DEFAULT_OVERLAY_UPDATE_INTERVAL_S)
            ),
            scale=snap_overlay_scale(payload.get("scale", DEFAULT_OVERLAY_SCALE)),
            position=normalize_overlay_position(
                payload.get("position", DEFAULT_OVERLAY_POSITION)
            ),
            detail=normalize_overlay_detail(
                payload.get("detail", DEFAULT_OVERLAY_DETAIL)
            ),
            opacity=clamp_overlay_opacity(
                payload.get("opacity", DEFAULT_OVERLAY_OPACITY)
            ),
            renderer=normalize_overlay_renderer(
                payload.get("renderer", DEFAULT_OVERLAY_RENDERER)
            ),
            font=normalize_overlay_font(payload.get("font", DEFAULT_OVERLAY_FONT)),
            layout=normalize_overlay_layout(
                payload.get("layout", DEFAULT_OVERLAY_LAYOUT)
            ),
        )
    )


def save_overlay_config(
    config: OverlayConfig,
    path: str | Path | None = None,
) -> Path:
    config = normalize_overlay_config(config)
    config_path = (
        default_overlay_config_path()
        if path is None
        else Path(path).expanduser()
    )
    text = (
        "version = 1\n"
        f"enabled = {_toml_bool(config.enabled)}\n"
        f"update_interval_s = {int(config.update_interval_s)}\n"
        f"scale = {float(config.scale)}\n"
        f'position = "{config.position}"\n'
        f'detail = "{config.detail}"\n'
        f"opacity = {float(config.opacity)}\n"
        f'renderer = "{config.renderer}"\n'
        f'font = "{config.font}"\n'
        f'layout = "{config.layout}"\n'
        "items = ["
        + ", ".join(f'"{item_id}"' for item_id in config.enabled_item_ids)
        + "]\n"
    )
    return atomic_write_text(config_path, text)


def normalize_overlay_config(config: OverlayConfig) -> OverlayConfig:
    detail = normalize_overlay_detail(config.detail)
    # A preset owns the field list while it is selected. Resolving it here
    # means overlay.toml always carries the concrete items, so the native
    # layer never has to know detail levels exist.
    items = (
        config.enabled_item_ids
        if detail == CUSTOM_OVERLAY_DETAIL
        else OVERLAY_DETAIL_PRESETS[detail]
    )
    return OverlayConfig(
        enabled=bool(config.enabled),
        enabled_item_ids=normalize_overlay_item_ids(items),
        update_interval_s=clamp_overlay_update_interval_s(config.update_interval_s),
        scale=snap_overlay_scale(config.scale),
        position=normalize_overlay_position(config.position),
        detail=detail,
        opacity=clamp_overlay_opacity(config.opacity),
        renderer=normalize_overlay_renderer(config.renderer),
        font=normalize_overlay_font(config.font),
        layout=normalize_overlay_layout(config.layout),
    )


def normalize_overlay_item_ids(value: object) -> tuple[str, ...]:
    raw_ids: list[str] = []
    if isinstance(value, (list, tuple, set)):
        raw_ids = [str(item).strip() for item in value]
    elif isinstance(value, str):
        raw_ids = [item.strip() for item in value.split(",")]
    wanted = {item for item in raw_ids if item in OVERLAY_ITEM_IDS}
    ordered = tuple(item for item in OVERLAY_ITEM_IDS if item in wanted)
    return ordered or ("base_fps",)


def set_overlay_item_enabled(
    config: OverlayConfig,
    item_id: str,
    enabled: bool,
) -> OverlayConfig:
    item_id = str(item_id).strip()
    if item_id not in OVERLAY_ITEM_IDS:
        return normalize_overlay_config(config)
    current = set(normalize_overlay_item_ids(config.enabled_item_ids))
    if enabled:
        current.add(item_id)
    elif len(current) > 1:
        current.discard(item_id)
    return normalize_overlay_config(
        replace(
            config,
            enabled_item_ids=normalize_overlay_item_ids(tuple(current)),
            detail=CUSTOM_OVERLAY_DETAIL,
        )
    )


def set_overlay_enabled(config: OverlayConfig, enabled: bool) -> OverlayConfig:
    return normalize_overlay_config(replace(config, enabled=bool(enabled)))


def set_overlay_update_interval_s(
    config: OverlayConfig,
    value: object,
) -> OverlayConfig:
    return normalize_overlay_config(
        replace(config, update_interval_s=clamp_overlay_update_interval_s(value))
    )


def set_overlay_scale(config: OverlayConfig, value: object) -> OverlayConfig:
    return normalize_overlay_config(replace(config, scale=snap_overlay_scale(value)))


def set_overlay_position(config: OverlayConfig, value: object) -> OverlayConfig:
    return normalize_overlay_config(
        replace(config, position=normalize_overlay_position(value))
    )


def set_overlay_detail(config: OverlayConfig, value: object) -> OverlayConfig:
    return normalize_overlay_config(
        replace(config, detail=normalize_overlay_detail(value))
    )


def set_overlay_opacity(config: OverlayConfig, value: object) -> OverlayConfig:
    return normalize_overlay_config(
        replace(config, opacity=clamp_overlay_opacity(value))
    )


def set_overlay_renderer(config: OverlayConfig, value: object) -> OverlayConfig:
    return normalize_overlay_config(
        replace(config, renderer=normalize_overlay_renderer(value))
    )


def clamp_overlay_opacity(value: object) -> float:
    if not isinstance(value, (int, float, str)):
        return DEFAULT_OVERLAY_OPACITY
    try:
        parsed = float(value)
    except ValueError:
        return DEFAULT_OVERLAY_OPACITY
    return max(MIN_OVERLAY_OPACITY, min(MAX_OVERLAY_OPACITY, parsed))


def set_overlay_font(config: OverlayConfig, value: object) -> OverlayConfig:
    return normalize_overlay_config(replace(config, font=normalize_overlay_font(value)))


def set_overlay_layout(config: OverlayConfig, value: object) -> OverlayConfig:
    return normalize_overlay_config(
        replace(config, layout=normalize_overlay_layout(value))
    )


def normalize_overlay_layout(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in OVERLAY_LAYOUTS else DEFAULT_OVERLAY_LAYOUT


def normalize_overlay_font(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in OVERLAY_FONTS else DEFAULT_OVERLAY_FONT


def normalize_overlay_renderer(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in OVERLAY_RENDERERS else DEFAULT_OVERLAY_RENDERER


def next_overlay_detail(value: object) -> str:
    """The detail level after this one, wrapping at the end.

    Custom is part of the cycle rather than an escape from it: it holds the
    user's own field selection, so skipping it would make that selection
    unreachable from a key binding.
    """
    current = normalize_overlay_detail(value)
    index = OVERLAY_DETAIL_IDS.index(current)
    return OVERLAY_DETAIL_IDS[(index + 1) % len(OVERLAY_DETAIL_IDS)]


def normalize_overlay_detail(value: object) -> str:
    """Snap an arbitrary detail level to a known one, else the checkbox set."""
    text = str(value or "").strip().lower()
    return text if text in OVERLAY_DETAIL_IDS else DEFAULT_OVERLAY_DETAIL


def normalize_overlay_position(value: object) -> str:
    """Snap an arbitrary position to a name the native layer also understands."""
    text = str(value or "").strip().lower()
    return text if text in OVERLAY_POSITION_IDS else DEFAULT_OVERLAY_POSITION


def snap_overlay_scale(value: object) -> float:
    """Snap an arbitrary scale to the nearest offered option.

    The steps used to be whole multiples because the bitmap face could only be
    magnified by whole pixels. The distance-field faces resample cleanly at any
    size, so the ladder now carries the sizes between them as well.
    """
    try:
        parsed = float(cast(Any, value))
    except (TypeError, ValueError):
        return DEFAULT_OVERLAY_SCALE
    if parsed <= 0:
        return DEFAULT_OVERLAY_SCALE
    return min(OVERLAY_SCALE_OPTIONS, key=lambda option: abs(option - parsed))


def clamp_overlay_update_interval_s(value: object) -> int:
    try:
        parsed = round(float(cast(Any, value)))
    except (TypeError, ValueError):
        parsed = DEFAULT_OVERLAY_UPDATE_INTERVAL_S
    return max(
        MIN_OVERLAY_UPDATE_INTERVAL_S,
        min(MAX_OVERLAY_UPDATE_INTERVAL_S, parsed),
    )


def _bool_value(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _toml_bool(value: object) -> str:
    return "true" if bool(value) else "false"
