from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, cast

from stability.q2rtx.models import Q2RTXStabilityConfig

from auto_uv.scan_mode.auto_uv_mode import (
    ADAPTIVE_TIER_MODES,
    AUTO_UV_MODE_BALANCED,
    AUTO_UV_MODE_EFFICIENCY,
    AUTO_UV_MODE_PERFORMANCE,
    adaptive_tier_option_key,
    normalize_auto_uv_mode,
)
from auto_uv.domain.types import AutoUvError
from auto_uv.domain.user_options import (
    AUTO_UV_DEFAULTS,
    AUTO_UV_METRIC_TUNING,
)
from auto_uv.scan_mode.target_overrides import validate_tier_target_overrides


@dataclass(frozen=True, slots=True)
class ScanRuntimeSettings:
    q2rtx_config: Q2RTXStabilityConfig
    auto_uv_mode: str
    configured_min_voltage_mv: int | None
    configured_max_drop_pct: float
    final_verification_duration_s: int
    short_probe_base_duration_s: int
    efficiency_stop_streak: int
    derive_efficiency_stop_streak: bool
    min_efficiency_stop_voltage_drop_pct: float
    tail_rise_bins: int


def read_scan_runtime_settings(
    runtime_options: dict,
    q2rtx_config: Q2RTXStabilityConfig,
    gpu_name: object | None = None,
) -> ScanRuntimeSettings:
    if int(q2rtx_config.duration_s) <= 0:
        raise AutoUvError("auto-UV voltage scan needs positive benchmark duration")

    validate_tier_target_overrides(runtime_options, gpu_name=gpu_name)
    auto_uv_mode = normalize_auto_uv_mode(runtime_options.get("auto_uv_mode"))
    return ScanRuntimeSettings(
        q2rtx_config=q2rtx_config,
        auto_uv_mode=auto_uv_mode,
        configured_min_voltage_mv=optional_int(
            runtime_options.get("auto_uv_min_voltage_mv")
        ),
        configured_max_drop_pct=max_drop_pct(),
        final_verification_duration_s=final_verification_duration_s(
            runtime_options, auto_uv_mode=auto_uv_mode
        ),
        short_probe_base_duration_s=short_probe_base_duration_s(),
        efficiency_stop_streak=efficiency_stop_streak(),
        derive_efficiency_stop_streak=derive_efficiency_stop_streak(),
        min_efficiency_stop_voltage_drop_pct=min_efficiency_stop_voltage_drop_pct(),
        tail_rise_bins=tail_rise_bins(runtime_options, auto_uv_mode=auto_uv_mode),
    )


def adaptive_tier_option(
    runtime_options: dict,
    *,
    tier_mode: str,
    option: str,
) -> object | None:
    """A per-tier scan option (``auto_uv_<tier>_<option>``), or None.

    The full (adaptive) scan carries tier-specific values for options that a
    single-profile scan expresses with one scan-wide key; empty values count
    as absent so the caller can fall through to the scan-wide key.
    """
    tier = str(tier_mode).strip().lower()
    if tier not in ADAPTIVE_TIER_MODES:
        return None
    value = runtime_options.get(adaptive_tier_option_key(tier, option))
    return None if value in (None, "") else value


def max_drop_pct() -> float:
    return max(0.0, float(AUTO_UV_DEFAULTS.max_drop_pct))


def explicit_final_verification_duration_s(
    runtime_options: dict | None = None,
) -> int | None:
    """A scan-wide final-verification duration the user explicitly requested,
    or None to fall back to the per-tier defaults.

    The --auto-uv-final-verification-s option wins, then the developer env
    override; both apply to every tier of a scan."""
    requested = (runtime_options or {}).get("auto_uv_final_verification_s")
    if requested is not None:
        try:
            return max(1, int(requested))
        except (TypeError, ValueError):
            pass
    override = os.environ.get("PENGUIN_BURNER_AUTO_UV_FINAL_SECONDS")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    return None


_PER_TIER_FINAL_DURATION_S = {
    AUTO_UV_MODE_EFFICIENCY: AUTO_UV_DEFAULTS.efficiency_final_duration_s,
    AUTO_UV_MODE_BALANCED: AUTO_UV_DEFAULTS.balanced_final_duration_s,
    AUTO_UV_MODE_PERFORMANCE: AUTO_UV_DEFAULTS.performance_final_duration_s,
}


def final_verification_duration_s(
    runtime_options: dict | None = None,
    *,
    auto_uv_mode: str | None = None,
) -> int:
    # An explicit scan-wide request applies to every tier; otherwise the
    # per-tier default (efficiency 60s, balanced 180s, performance 300s), or
    # the plain 300s default for non-adaptive/unknown modes.
    explicit = explicit_final_verification_duration_s(runtime_options)
    if explicit is not None:
        return explicit
    per_tier = _PER_TIER_FINAL_DURATION_S.get(str(auto_uv_mode or ""))
    if per_tier is not None:
        return max(1, int(per_tier))
    return max(1, int(AUTO_UV_DEFAULTS.final_duration_s))


def short_probe_base_duration_s() -> int:
    return max(10, min(60, int(AUTO_UV_DEFAULTS.probe_duration_s)))


def efficiency_stop_streak() -> int:
    return max(0, int(AUTO_UV_DEFAULTS.efficiency_stop_streak))


def derive_efficiency_stop_streak() -> bool:
    return True


def min_efficiency_stop_voltage_drop_pct() -> float:
    return max(0.0, float(AUTO_UV_METRIC_TUNING.min_efficiency_stop_voltage_drop_pct))


def tail_rise_bins(runtime_options: dict, *, auto_uv_mode: str) -> int:
    value = runtime_options.get("auto_uv_tail_rise_bins")
    if value is None:
        if auto_uv_mode == AUTO_UV_MODE_PERFORMANCE:
            value = AUTO_UV_DEFAULTS.performance_tail_rise_bins
        elif auto_uv_mode == AUTO_UV_MODE_BALANCED:
            value = AUTO_UV_DEFAULTS.balanced_tail_rise_bins
        else:
            value = AUTO_UV_DEFAULTS.tail_rise_bins
    return max(0, min(int(AUTO_UV_DEFAULTS.max_tail_rise_bins), int(value)))


def optional_int(value: object) -> int | None:
    return None if value is None else int(cast(Any, value))
