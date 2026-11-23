"""Read and write the PenguinBurner runtime TOML config used by CLI commands.

The module owns config defaults and small persistence updates; runtime behavior stays elsewhere.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from common.penguin_burner_paths import default_runtime_config_path
from integrations.afterburner.import_fan_curve import (
    write_config as write_runtime_config,
)
from runtime.support.adaptive_target_fps import (
    ADAPTIVE_TARGET_FPS_CONFIG_KEY,
    ADAPTIVE_TARGET_FPS_CONFIG_SECTION,
    DEFAULT_ADAPTIVE_TARGET_FPS,
    parse_adaptive_target_fps,
)

UI_CONFIG_SECTION = "ui"
PERSIST_ON_STARTUP_CONFIG_KEY = "persist_on_startup"
SILENT_FAN_CURVE_CONFIG_KEY = "silent_fan_curve"


def default_config_path():
    return default_runtime_config_path()


def default_runtime_config() -> dict:
    return {
        "gpu": {
            "index": 0,
            "enable_persistence_mode": True,
        },
        "fan": {
            "poll_interval_s": 2,
            "hysteresis_c": 2.0,
            "mode": "linear",
            "min_fan_speed_pct": 20,
            "max_fan_speed_pct": 100,
            "max_step_up_pct_per_s": 25.0,
            "max_step_down_pct_per_s": 15.0,
            "manual_enable_temp_c": 55.0,
            "auto_restore_temp_c": 50.0,
            "emergency_auto_override_temp_c": 80.0,
            "emergency_auto_resume_temp_c": 75.0,
            "force_update_every_poll": False,
            "curve": [
                [55, 30],
                [65, 35],
                [70, 40],
                [80, 45],
            ],
        },
        "stability": {},
        "adaptive": {
            "target_fps": DEFAULT_ADAPTIVE_TARGET_FPS,
        },
        UI_CONFIG_SECTION: {
            # Safe default: applying a profile changes the current session
            # only. The user opts into boot persistence explicitly.
            PERSIST_ON_STARTUP_CONFIG_KEY: False,
            SILENT_FAN_CURVE_CONFIG_KEY: False,
        },
    }


def load_runtime_config(config_path=None):
    path = default_config_path() if config_path is None else Path(config_path).expanduser()
    config = default_runtime_config()
    if not path.exists():
        return config, path

    loaded = load_raw_runtime_config(path, tolerant=True)
    for section in ("gpu", "fan", "stability", "adaptive", UI_CONFIG_SECTION):
        values = loaded.get(section)
        if isinstance(values, dict):
            config[section].update(values)
    return config, path


def runtime_gpu_index(config_path: str | Path | None = None) -> int:
    """The GPU the runtime targets, from the runtime config; 0 when unset
    or unreadable."""
    path = default_runtime_config_path() if config_path is None else Path(config_path)
    try:
        config = load_raw_runtime_config(path)
        return max(0, int(config.get("gpu", {}).get("index", 0)))
    except Exception:  # noqa: BLE001
        return 0


def load_raw_runtime_config(config_path, *, tolerant: bool = False):
    """Read the raw TOML config.

    Read-only paths pass ``tolerant=True`` so a hand-corrupted config
    degrades to defaults — --restore-stock especially is a recovery command
    and has to work when user state is broken. Read-modify-write persisters
    must stay strict: swallowing the error there would re-emit the file with
    a single section and silently drop the rest of the user's settings.
    """
    path = Path(config_path).expanduser()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as config_file:
            return tomllib.load(config_file)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        if not tolerant:
            raise
        print(
            f"warning: unreadable runtime config {path} ({exc}); "
            "continuing with defaults",
            file=sys.stderr,
            flush=True,
        )
        return {}


def persist_adaptive_target_fps_to_runtime_config(
    target_fps,
    config_path=None,
) -> float:
    path = (
        default_config_path()
        if config_path is None
        else Path(config_path).expanduser()
    )
    config = load_raw_runtime_config(path)
    raw_adaptive = config.get(ADAPTIVE_TARGET_FPS_CONFIG_SECTION, {})
    adaptive = dict(raw_adaptive) if isinstance(raw_adaptive, dict) else {}
    normalized = parse_adaptive_target_fps(target_fps)
    adaptive[ADAPTIVE_TARGET_FPS_CONFIG_KEY] = normalized
    config[ADAPTIVE_TARGET_FPS_CONFIG_SECTION] = adaptive
    write_runtime_config(path, config)
    return normalized


def _ui_bool_from_runtime_config(
    key: str,
    config_path=None,
    *,
    default: bool = False,
) -> bool:
    path = (
        default_config_path()
        if config_path is None
        else Path(config_path).expanduser()
    )
    # Tolerant read: GUI startup and recovery paths must degrade to the
    # default on a corrupt config instead of failing.
    config = load_raw_runtime_config(path, tolerant=True)
    raw_ui = config.get(UI_CONFIG_SECTION, {})
    if not isinstance(raw_ui, dict):
        return bool(default)
    value = raw_ui.get(key)
    if value is None:
        return bool(default)
    return _config_bool(value, default=default)


def _ui_bool_to_runtime_config(
    key: str,
    enabled,
    config_path=None,
) -> bool:
    path = (
        default_config_path()
        if config_path is None
        else Path(config_path).expanduser()
    )
    # Strict read: a corrupt config must abort this read-modify-write instead
    # of being re-emitted with only the [ui] section.
    config = load_raw_runtime_config(path)
    raw_ui = config.get(UI_CONFIG_SECTION, {})
    ui = dict(raw_ui) if isinstance(raw_ui, dict) else {}
    normalized = bool(enabled)
    ui[key] = normalized
    config[UI_CONFIG_SECTION] = ui
    write_runtime_config(path, config)
    return normalized


def ui_strings_from_runtime_config(keys, config_path=None) -> dict[str, str]:
    """The ``[ui]`` string values among ``keys``; missing or non-string
    values are left out, and a corrupt config reads as empty."""
    path = (
        default_config_path()
        if config_path is None
        else Path(config_path).expanduser()
    )
    raw_ui = load_raw_runtime_config(path, tolerant=True).get(UI_CONFIG_SECTION, {})
    if not isinstance(raw_ui, dict):
        return {}
    return {key: raw_ui[key] for key in keys if isinstance(raw_ui.get(key), str)}


def ui_strings_to_runtime_config(values: dict[str, str], config_path=None) -> None:
    """Merge string ``values`` into ``[ui]``, keeping every other setting."""
    path = (
        default_config_path()
        if config_path is None
        else Path(config_path).expanduser()
    )
    # Strict read, as _ui_bool_to_runtime_config: never re-emit a corrupt
    # config with only the [ui] section.
    config = load_raw_runtime_config(path)
    raw_ui = config.get(UI_CONFIG_SECTION, {})
    ui = dict(raw_ui) if isinstance(raw_ui, dict) else {}
    ui.update({key: str(value) for key, value in values.items()})
    config[UI_CONFIG_SECTION] = ui
    write_runtime_config(path, config)


def persist_on_startup_from_runtime_config(
    config_path=None,
    *,
    default: bool = False,
) -> bool:
    return _ui_bool_from_runtime_config(
        PERSIST_ON_STARTUP_CONFIG_KEY, config_path, default=default
    )


def persist_on_startup_to_runtime_config(
    enabled,
    config_path=None,
) -> bool:
    return _ui_bool_to_runtime_config(
        PERSIST_ON_STARTUP_CONFIG_KEY, enabled, config_path
    )


def silent_fan_curve_from_runtime_config(
    config_path=None,
    *,
    default: bool = False,
) -> bool:
    return _ui_bool_from_runtime_config(
        SILENT_FAN_CURVE_CONFIG_KEY, config_path, default=default
    )


def silent_fan_curve_to_runtime_config(
    enabled,
    config_path=None,
) -> bool:
    return _ui_bool_to_runtime_config(
        SILENT_FAN_CURVE_CONFIG_KEY, enabled, config_path
    )


def _config_bool(value, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)
