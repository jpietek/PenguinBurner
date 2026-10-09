"""The one scan-wide knob for how far a scan may walk toward a card's edge."""

from __future__ import annotations

from auto_uv.domain.types import AutoUvError

AUTO_UV_TUNING_MODE_CAREFUL = "careful"
AUTO_UV_TUNING_MODE_AGGRESSIVE = "aggressive"
AUTO_UV_TUNING_MODES = (AUTO_UV_TUNING_MODE_CAREFUL, AUTO_UV_TUNING_MODE_AGGRESSIVE)
AUTO_UV_TUNING_MODE_OPTION = "auto_uv_tuning_mode"


def normalize_auto_uv_tuning_mode(value: object | None) -> str:
    text = str(value or AUTO_UV_TUNING_MODE_CAREFUL).strip().lower()
    if text not in AUTO_UV_TUNING_MODES:
        raise AutoUvError(
            f"{AUTO_UV_TUNING_MODE_OPTION} must be one of {', '.join(AUTO_UV_TUNING_MODES)}; "
            f"got {value!r}"
        )
    return text


def tuning_mode_from_runtime_options(runtime_options: dict | None) -> str:
    options = runtime_options if isinstance(runtime_options, dict) else {}
    return normalize_auto_uv_tuning_mode(options.get(AUTO_UV_TUNING_MODE_OPTION))
