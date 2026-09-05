"""Resolve explicit tier targets without changing automatic scan defaults."""

from __future__ import annotations

from dataclasses import dataclass

from auto_uv.domain.types import AutoUvError
from auto_uv.scan_mode.auto_uv_mode import ADAPTIVE_TIER_MODES, adaptive_tier_option_key
from auto_uv.scan_mode.uv_limits import (
    uv_limit_clock_target_range_for_gpu,
    uv_limit_profile_target_for_gpu,
)


@dataclass(frozen=True, slots=True)
class TierTargetOverrides:
    voltage_mv: int | None = None
    clock_mhz: int | None = None

    @property
    def specified(self) -> bool:
        return self.voltage_mv is not None or self.clock_mhz is not None


def tier_target_overrides(
    runtime_options: dict,
    *,
    gpu_name: object | None,
    tier: str,
) -> TierTargetOverrides:
    values: dict[str, int | None] = {}
    for field in ("voltage_mv", "clock_mhz"):
        key = adaptive_tier_option_key(tier, f"target_{field}")
        value = runtime_options.get(key)
        if value in (None, "") and tier == "performance":
            key = f"auto_oc_target_{field}"
            value = runtime_options.get(key)
        if value in (None, ""):
            values[field] = None
            continue
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise AutoUvError(f"{key} must be a positive integer") from exc
        if number <= 0:
            raise AutoUvError(f"{key} must be a positive integer")
        values[field] = number
    clock = values["clock_mhz"]
    bounds = uv_limit_clock_target_range_for_gpu(gpu_name, tier)
    if clock is not None and bounds is not None and not bounds[0] <= clock <= bounds[1]:
        raise AutoUvError(
            f"{tier.capitalize()} clock target must be {bounds[0]}–{bounds[1]} MHz "
            f"for {gpu_name}; requested {clock} MHz"
        )
    return TierTargetOverrides(voltage_mv=values["voltage_mv"], clock_mhz=clock)


def validate_tier_target_overrides(
    runtime_options: dict, *, gpu_name: object | None
) -> None:
    for tier in ADAPTIVE_TIER_MODES:
        tier_target_overrides(runtime_options, gpu_name=gpu_name, tier=tier)


def custom_tier_target(
    overrides: TierTargetOverrides,
    *,
    gpu_name: object | None,
    tier: str,
) -> TierTargetOverrides | None:
    """Unedited table values retain the automatic tier search and scoring."""
    if not overrides.specified:
        return None
    default = uv_limit_profile_target_for_gpu(gpu_name, tier)
    if default is not None and (
        overrides.voltage_mv in (None, default.voltage_mv)
        and overrides.clock_mhz in (None, default.clock_mhz)
    ):
        return None
    if default is not None:
        return TierTargetOverrides(
            voltage_mv=overrides.voltage_mv or default.voltage_mv,
            clock_mhz=overrides.clock_mhz or default.clock_mhz,
        )
    return overrides


def custom_target_min_core_clock_pct(
    *,
    target_clock_mhz: int,
    baseline_clock_mhz: float,
    default_pct: float,
) -> float:
    """Retain the automatic loss allowance relative to a deliberately lower clock."""
    if baseline_clock_mhz <= 0:
        return default_pct
    return min(default_pct, default_pct * target_clock_mhz / baseline_clock_mhz)
