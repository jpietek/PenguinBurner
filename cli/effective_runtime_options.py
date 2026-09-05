from __future__ import annotations

from auto_uv.domain.user_options import AUTO_UV_DEFAULTS
from auto_uv.scan_mode.auto_uv_mode import (
    ADAPTIVE_TIER_MODES,
    AUTO_UV_MODE_BALANCED,
    adaptive_tier_option_key,
    normalize_auto_uv_mode,
)


def _positive_or_none(coerce):
    def fn(value):
        v = coerce(value)
        return v if v > coerce(0) else None
    return fn


_INT_POS = _positive_or_none(int)


def _memory_offset(value):
    # No static cap here: the Auto-UV apply path clamps against the
    # driver-reported NVML limit for the actual GPU and logs the clamp.
    return max(0, int(value))


# (arg_name, runtime_key, transform). transform is called only when args.<arg_name> is not None.
_AUTO_UV_NUMERIC_OPTIONS = [
    ("auto_uv_min_voltage_mv", "auto_uv_min_voltage_mv", _INT_POS),
    ("auto_uv_memory_offset_mhz", "auto_uv_memory_offset_mhz", _memory_offset),
    ("auto_uv_power_limit_w", "auto_uv_power_limit_w", _INT_POS),
    (
        "auto_uv_tail_rise_bins",
        "auto_uv_tail_rise_bins",
        lambda v: max(0, min(int(AUTO_UV_DEFAULTS.max_tail_rise_bins), int(v))),
    ),
    ("auto_oc_target_voltage_mv", "auto_oc_target_voltage_mv", _INT_POS),
    ("auto_oc_target_clock_mhz", "auto_oc_target_clock_mhz", _INT_POS),
    ("auto_uv_final_verification_s", "auto_uv_final_verification_s", _INT_POS),
] + [
    # Per-tier full-scan overrides: each adaptive tier's
    # board-power cap and memory offset (same units/semantics as the
    # scan-wide keys above), keys derived from the canonical tier constants.
    (key, key, transform)
    for tier in ADAPTIVE_TIER_MODES
    for key, transform in (
        (adaptive_tier_option_key(tier, "power_limit_w"), _INT_POS),
        (adaptive_tier_option_key(tier, "memory_offset_mhz"), _memory_offset),
        (adaptive_tier_option_key(tier, "target_voltage_mv"), int),
        (adaptive_tier_option_key(tier, "target_clock_mhz"), int),
    )
]


def build_effective_auto_uv_runtime_options(args) -> dict:
    runtime_options: dict = {}
    for arg_name, key, transform in _AUTO_UV_NUMERIC_OPTIONS:
        value = getattr(args, arg_name, None)
        if value is not None:
            runtime_options[key] = transform(value)

    if getattr(args, "auto_uv_mode", None) is not None:
        requested_auto_uv_mode = str(args.auto_uv_mode).strip().lower()
        runtime_options["auto_uv_requested_mode"] = requested_auto_uv_mode
        runtime_options["auto_uv_mode"] = normalize_auto_uv_mode(args.auto_uv_mode)
        if (
            requested_auto_uv_mode == AUTO_UV_MODE_BALANCED
            and getattr(args, "auto_uv_tail_rise_bins") is None
        ):
            runtime_options["auto_uv_tail_rise_bins"] = (
                AUTO_UV_DEFAULTS.balanced_tail_rise_bins
            )
    if getattr(args, "auto_uv_require_final_choice", False):
        runtime_options["auto_uv_require_final_choice"] = True
    return runtime_options
