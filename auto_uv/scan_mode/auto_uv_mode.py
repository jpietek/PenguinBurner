"""Normalize the user-visible Auto-UV preset/mode name."""

from __future__ import annotations


AUTO_UV_MODE_EFFICIENCY = "efficiency"
AUTO_UV_MODE_BALANCED = "balanced"
AUTO_UV_MODE_PERFORMANCE = "performance"
# One scan discovering all three tier profiles at once.
AUTO_UV_MODE_ADAPTIVE = "adaptive"
AUTO_UV_MODES = (
    AUTO_UV_MODE_EFFICIENCY,
    AUTO_UV_MODE_BALANCED,
    AUTO_UV_MODE_PERFORMANCE,
    AUTO_UV_MODE_ADAPTIVE,
)

# The three profile tiers one full (adaptive) scan produces, in scan order.
ADAPTIVE_TIER_MODES = (
    AUTO_UV_MODE_EFFICIENCY,
    AUTO_UV_MODE_BALANCED,
    AUTO_UV_MODE_PERFORMANCE,
)
# Per-tier full-scan option suffixes; the canonical runtime-option key is
# adaptive_tier_option_key(tier, suffix). Every layer (dialog, UI command
# payload, daemon whitelist, worker CLI) derives its key list from these so
# a new option cannot silently go missing in one layer.
ADAPTIVE_TIER_OPTION_SUFFIXES = (
    "power_limit_w",
    "memory_offset_mhz",
    "target_voltage_mv",
    "target_clock_mhz",
)


def adaptive_tier_option_key(tier_mode: str, option_suffix: str) -> str:
    return f"auto_uv_{tier_mode}_{option_suffix}"


_AUTO_UV_MODE_ALIASES = {
    "": AUTO_UV_MODE_EFFICIENCY,
    "balanced": AUTO_UV_MODE_BALANCED,
    "efficiency": AUTO_UV_MODE_EFFICIENCY,
    "aggressive": AUTO_UV_MODE_PERFORMANCE,
    "performance": AUTO_UV_MODE_PERFORMANCE,
    "adaptive": AUTO_UV_MODE_ADAPTIVE,
    "all": AUTO_UV_MODE_ADAPTIVE,
}


def normalize_auto_uv_mode(value: object | None) -> str:
    normalized = str(value or "").strip().lower()
    return _AUTO_UV_MODE_ALIASES.get(normalized, AUTO_UV_MODE_EFFICIENCY)
