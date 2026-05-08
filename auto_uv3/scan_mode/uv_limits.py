"""Borrowed GPU voltage/frequency targets used by Auto-UV."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UvTierTarget:
    gpu_family: str
    profile_id: str
    voltage_mv: int
    clock_mhz: int


_UV_LIMIT_TARGETS: tuple[dict[str, object], ...] = (
    {
        "family": "RTX 4070 Ti Super",
        "patterns": ("4070 TI SUPER", "4070TI SUPER"),
        "eco": (925, 2550),
        "balanced": (940, 2640),
        "performance": (950, 2730),
        "max": (975, 2820),
    },
    {
        "family": "RTX 4070 Ti",
        "patterns": ("4070 TI", "4070TI"),
        "eco": (925, 2550),
        "balanced": (940, 2640),
        "performance": (950, 2685),
        "max": (975, 2820),
    },
    {
        "family": "RTX 4070 Super",
        "patterns": ("4070 SUPER",),
        "eco": (900, 2400),
        "balanced": (925, 2550),
        "performance": (940, 2670),
        "max": (975, 2790),
    },
    {
        "family": "RTX 5070 Ti",
        "patterns": ("5070 TI", "5070TI"),
        "eco": (850, 2500),
        "balanced": (900, 2800),
        "performance": (925, 2950),
        "max": (950, 3000),
    },
    {
        "family": "RTX 5060 Ti",
        "patterns": ("5060 TI", "5060TI", "5060"),
        "eco": (800, 2500),
        "balanced": (875, 2700),
        "performance": (925, 2900),
        "max": (975, 3000),
    },
    {
        "family": "RTX 4090",
        "patterns": ("4090",),
        "eco": (875, 2400),
        "balanced": (900, 2550),
        "performance": (925, 2670),
        "max": (950, 2745),
    },
    {
        "family": "RTX 4080",
        "patterns": ("4080",),
        "eco": (875, 2400),
        "balanced": (900, 2520),
        "performance": (925, 2640),
        "max": (950, 2700),
    },
    {
        "family": "RTX 4070",
        "patterns": ("4070",),
        "eco": (900, 2400),
        "balanced": (925, 2550),
        "performance": (940, 2670),
        "max": (975, 2790),
    },
    {
        "family": "RTX 5090",
        "patterns": ("5090",),
        "eco": (900, 2700),
        "balanced": (950, 2900),
        "performance": (975, 3000),
        "max": (975, 3100),
    },
    {
        "family": "RTX 5080",
        "patterns": ("5080",),
        "eco": (850, 2800),
        "balanced": (900, 2800),
        "performance": (925, 2980),
        "max": (975, 3150),
    },
    {
        "family": "RTX 5070",
        "patterns": ("5070",),
        "eco": (850, 2600),
        "balanced": (900, 2750),
        "performance": (940, 3000),
        "max": (975, 3150),
    },
)


def uv_limit_voltage_floor_target_for_gpu(
    gpu_name: object | None,
    auto_uv_mode: object | None = None,
    *,
    yolo: bool = False,
) -> UvTierTarget | None:
    _ = auto_uv_mode, yolo
    return uv_limit_profile_target_for_gpu(gpu_name, "eco")


def uv_limit_profile_target_for_gpu(
    gpu_name: object | None,
    profile_id: str,
) -> UvTierTarget | None:
    normalized_name = str(gpu_name or "").upper()
    if not normalized_name:
        return None

    normalized_profile = str(profile_id or "").strip().lower()
    for entry in _UV_LIMIT_TARGETS:
        patterns = tuple(str(pattern) for pattern in entry["patterns"])
        if any(pattern in normalized_name for pattern in patterns):
            if normalized_profile not in entry:
                return None
            voltage_mv, clock_mhz = entry[normalized_profile]
            return UvTierTarget(
                gpu_family=str(entry["family"]),
                profile_id=normalized_profile,
                voltage_mv=int(voltage_mv),
                clock_mhz=int(clock_mhz),
            )
    return None


def voltage_drop_pct(*, start_voltage_mv: int, floor_voltage_mv: int) -> float:
    if int(start_voltage_mv) <= 0:
        return 0.0
    drop_mv = max(0, int(start_voltage_mv) - int(floor_voltage_mv))
    return float(drop_mv) / float(start_voltage_mv) * 100.0
