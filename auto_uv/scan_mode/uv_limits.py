"""Borrowed GPU voltage/frequency targets used by Auto-UV."""

from __future__ import annotations

from dataclasses import dataclass


AUTO_UV_PERFORMANCE_OC_PROFILE_ID = "performance"

# Balanced blends the efficiency and performance clock-drop limits, weighted
# toward efficiency so the preset leans into power savings (0.6 efficiency /
# 0.4 performance) rather than sitting at the dead-center midpoint.
_BALANCED_EFFICIENCY_WEIGHT = 0.6

# Per-tier power-cap defaults. Undervolting alone still lets the card chase
# transient boost bins that cost disproportionate watts for a few MHz, so the
# efficiency preset pairs its V/F floor with a board-power cap. Balanced and
# performance both keep the stock board power budget by default: identical
# power regimes keep the balanced descent donatable to the performance tier in
# a full scan (the descent-reuse gate requires matching limits), and measured
# balanced curves are voltage-limited under gaming-class load anyway — the cap
# only bound their baseline probes. Users can still cap any tier per run from
# the scan dialog or the per-tier CLI flags. Only the efficiency cap varies by
# silicon (a weaker cut caps sooner). Percentages apply to the card's DEFAULT
# power limit (stock TGP), not the raised OC maximum.
_FULL_POWER_LIMIT_PCT = 100.0

# A fixed extra reduction applied to EVERY family's efficiency power cap. The
# per-family `efficiency_power_limit_pct` values sit close to stock TGP (the
# 5080's 88% resolves to 317W — the stock default, which caps nothing), so a
# sustained power-virus load fills the whole budget regardless of the V/F curve
# (curve shape cannot lower board power at a fixed cap; only the cap can). This
# knob pulls the efficiency default down by a fixed percentage across all GPUs
# so the tier actually undercuts stock; the driver-reported minimum power limit
# still clamps the result (e.g. the 5080 floors at its 300W hardware minimum),
# and the user can raise the cap per run.
_EFFICIENCY_POWER_LIMIT_EXTRA_REDUCTION_PCT = 12.0


@dataclass(frozen=True, slots=True)
class UvTierTarget:
    gpu_family: str
    profile_id: str
    voltage_mv: int
    clock_mhz: int


# Performance targets retain the ~1% reduction from the borrowed reference
# table (two 15 MHz clock bins on the RTX 5080). The two-bin default tail adds
# 30 MHz nominal headroom above these anchors; targets remain unchanged.
_UV_LIMIT_TARGETS: tuple[dict[str, object], ...] = (
    {
        "family": "RTX 5090",
        "patterns": ("5090",),
        "efficiency": (900, 2700),
        "balanced": (950, 2900),
        "performance": (975, 2970),
        "clock_drop_ceiling_mhz": 3100,
        "efficiency_power_limit_pct": 85,
    },
    {
        "family": "RTX 5080",
        "patterns": ("5080",),
        "efficiency": (850, 2800),
        "balanced": (900, 2800),
        # 2950 + (2 * 15) = 2980 MHz nominal with the default rising tail.
        "performance": (925, 2950),
        "clock_drop_ceiling_mhz": 3150,
        "efficiency_power_limit_pct": 88,
    },
    {
        "family": "RTX 5070 Ti",
        "patterns": ("5070 TI", "5070TI"),
        "efficiency": (850, 2500),
        "balanced": (900, 2800),
        "performance": (925, 2920),
        "clock_drop_ceiling_mhz": 3000,
        "efficiency_power_limit_pct": 83,
    },
    {
        "family": "RTX 5070",
        "patterns": ("5070",),
        "efficiency": (850, 2600),
        "balanced": (900, 2750),
        "performance": (940, 2970),
        "clock_drop_ceiling_mhz": 3150,
        "efficiency_power_limit_pct": 80,
    },
    {
        "family": "RTX 5060 Ti",
        "patterns": ("5060 TI", "5060TI"),
        "efficiency": (800, 2500),
        "balanced": (875, 2700),
        "performance": (925, 2870),
        "clock_drop_ceiling_mhz": 3000,
        "efficiency_power_limit_pct": 80,
    },
    {
        # The RTX 5060 is a cut GB206 with a much lower board-power budget than
        # the 5060 Ti, so it shares the 5060 Ti V/F targets and lets the
        # efficiency power cap (80%) hold the smaller die inside its envelope
        # rather than chasing a separate, more conservative clock ladder.
        "family": "RTX 5060",
        "patterns": ("5060",),
        "efficiency": (800, 2500),
        "balanced": (875, 2700),
        "performance": (925, 2870),
        "clock_drop_ceiling_mhz": 3000,
        "efficiency_power_limit_pct": 80,
    },
    {
        "family": "RTX 4070 Ti Super",
        "patterns": ("4070 TI SUPER", "4070TI SUPER"),
        "efficiency": (925, 2550),
        "balanced": (940, 2640),
        "performance": (950, 2705),
        "clock_drop_ceiling_mhz": 2820,
        "efficiency_power_limit_pct": 100,
    },
    {
        "family": "RTX 4070 Ti",
        "patterns": ("4070 TI", "4070TI"),
        "efficiency": (925, 2550),
        "balanced": (940, 2640),
        "performance": (950, 2660),
        "clock_drop_ceiling_mhz": 2820,
        "efficiency_power_limit_pct": 100,
    },
    {
        "family": "RTX 4070 Super",
        "patterns": ("4070 SUPER",),
        "efficiency": (900, 2400),
        "balanced": (925, 2550),
        "performance": (940, 2645),
        "clock_drop_ceiling_mhz": 2790,
        "efficiency_power_limit_pct": 100,
    },
    {
        "family": "RTX 4070",
        "patterns": ("4070",),
        "efficiency": (900, 2400),
        "balanced": (925, 2550),
        "performance": (940, 2645),
        "clock_drop_ceiling_mhz": 2790,
        "efficiency_power_limit_pct": 100,
    },
    {
        "family": "RTX 4060 Ti",
        "patterns": ("4060 TI", "4060TI"),
        "efficiency": (900, 2400),
        "balanced": (925, 2550),
        "performance": (950, 2625),
        "clock_drop_ceiling_mhz": 2750,
        "efficiency_power_limit_pct": 100,
    },
    {
        "family": "RTX 4060",
        "patterns": ("4060",),
        "efficiency": (875, 2300),
        "balanced": (900, 2450),
        "performance": (925, 2575),
        "clock_drop_ceiling_mhz": 2730,
        "efficiency_power_limit_pct": 100,
    },
    {
        "family": "RTX 4090",
        "patterns": ("4090",),
        "efficiency": (875, 2400),
        "balanced": (900, 2550),
        "performance": (925, 2645),
        "clock_drop_ceiling_mhz": 2745,
        "efficiency_power_limit_pct": 100,
    },
    {
        "family": "RTX 4080",
        "patterns": ("4080",),
        "efficiency": (875, 2400),
        "balanced": (900, 2520),
        "performance": (925, 2615),
        "clock_drop_ceiling_mhz": 2700,
        "efficiency_power_limit_pct": 100,
    },
    {
        "family": "RTX 3090 Ti",
        "patterns": ("3090 TI", "3090TI"),
        "efficiency": (825, 1700),
        "balanced": (875, 1830),
        "performance": (925, 1930),
        "clock_drop_ceiling_mhz": 2025,
        "efficiency_power_limit_pct": 80,
    },
    {
        "family": "RTX 3090",
        "patterns": ("3090",),
        "efficiency": (800, 1700),
        "balanced": (875, 1830),
        "performance": (900, 1880),
        "clock_drop_ceiling_mhz": 1965,
        "efficiency_power_limit_pct": 80,
    },
    {
        "family": "RTX 3080 Ti",
        "patterns": ("3080 TI", "3080TI"),
        "efficiency": (800, 1710),
        "balanced": (875, 1870),
        "performance": (900, 1900),
        "clock_drop_ceiling_mhz": 1980,
        "efficiency_power_limit_pct": 80,
    },
    {
        "family": "RTX 3080 12GB",
        "patterns": ("3080 12GB", "3080 12 GB", "3080-12"),
        "efficiency": (800, 1700),
        "balanced": (875, 1860),
        "performance": (900, 1900),
        "clock_drop_ceiling_mhz": 2000,
        "efficiency_power_limit_pct": 80,
    },
    {
        "family": "RTX 3080",
        "patterns": ("3080",),
        "efficiency": (800, 1750),
        "balanced": (875, 1890),
        "performance": (900, 1930),
        "clock_drop_ceiling_mhz": 2010,
        "efficiency_power_limit_pct": 80,
    },
    {
        "family": "RTX 3070 Ti",
        "patterns": ("3070 TI", "3070TI"),
        "efficiency": (825, 1770),
        "balanced": (875, 1905),
        "performance": (900, 1930),
        "clock_drop_ceiling_mhz": 1995,
        "efficiency_power_limit_pct": 80,
    },
    {
        "family": "RTX 3070",
        "patterns": ("3070",),
        "efficiency": (775, 1700),
        "balanced": (875, 1900),
        "performance": (925, 1930),
        "clock_drop_ceiling_mhz": 2010,
        "efficiency_power_limit_pct": 80,
    },
    {
        "family": "RTX 3060 Ti",
        "patterns": ("3060 TI", "3060TI"),
        "efficiency": (800, 1750),
        "balanced": (875, 1875),
        "performance": (925, 1915),
        "clock_drop_ceiling_mhz": 1980,
        "efficiency_power_limit_pct": 80,
    },
    {
        "family": "RTX 3060",
        "patterns": ("3060",),
        "efficiency": (800, 1750),
        "balanced": (850, 1840),
        "performance": (900, 1880),
        "clock_drop_ceiling_mhz": 1950,
        "efficiency_power_limit_pct": 80,
    },
)


def uv_limit_voltage_floor_target_for_gpu(
    gpu_name: object | None,
    auto_uv_mode: object | None = None,
) -> UvTierTarget | None:
    _ = auto_uv_mode
    return uv_limit_profile_target_for_gpu(gpu_name, "efficiency")


def uv_limit_clock_drop_pct_for_gpu(
    gpu_name: object | None,
    profile_id: object | None = "efficiency",
) -> float | None:
    entry = _uv_limit_entry_for_gpu(gpu_name)
    if entry is None:
        return None
    profile = str(profile_id or "efficiency").strip().lower()
    if profile == "balanced":
        # Balanced is a savings-biased blend of the efficiency and performance
        # clock-drop limits. Deriving it from a single clock ratio
        # (efficiency.clock / performance.clock) collapsed balanced toward
        # whichever neighbour the table's clock geometry sat closest to - the
        # RTX 5080 fell to ~6% (almost identical to performance) while the
        # RTX 5070 Ti rose to ~15% (almost identical to efficiency). Weighting it
        # toward efficiency keeps balanced centered-but-deeper on every GPU so it
        # actually saves power while staying short of the full efficiency drop.
        efficiency_pct = _derived_clock_drop_pct(entry, "efficiency")
        performance_pct = _derived_clock_drop_pct(entry, "performance")
        if efficiency_pct is None or performance_pct is None:
            return _derived_clock_drop_pct(entry, "balanced")
        return (
            efficiency_pct * _BALANCED_EFFICIENCY_WEIGHT
            + performance_pct * (1.0 - _BALANCED_EFFICIENCY_WEIGHT)
        )
    return _derived_clock_drop_pct(entry, profile)


def uv_limit_power_limit_pct_for_gpu(
    gpu_name: object | None,
    profile_id: object | None = "efficiency",
) -> float | None:
    """Return the default board-power cap (percent of the card's stock TGP) for a tier.

    Balanced and performance keep the full stock power budget so a full scan's
    balanced descent stays donatable to the performance tier. The efficiency
    cap is the per-family stored value less the fixed extra reduction.
    """
    entry = _uv_limit_entry_for_gpu(gpu_name)
    if entry is None:
        return None
    return _derived_power_limit_pct(entry, profile_id)


def _derived_power_limit_pct(
    entry: dict[str, object],
    profile_id: object | None,
) -> float | None:
    stored = entry.get("efficiency_power_limit_pct")
    if stored is None:
        return None
    if not isinstance(stored, (int, float, str)):
        return None
    efficiency_pct = float(stored)
    # Families that already cap efficiency get the same fixed extra reduction so
    # the default undercuts stock TGP by a real margin. A family left at full
    # power is deliberately uncapped, so it stays uncapped (you cannot lower a
    # limit that is not there).
    if efficiency_pct < _FULL_POWER_LIMIT_PCT:
        efficiency_pct = max(
            0.0,
            efficiency_pct
            * (1.0 - _EFFICIENCY_POWER_LIMIT_EXTRA_REDUCTION_PCT / 100.0),
        )
    profile = str(profile_id or "efficiency").strip().lower()
    if profile in ("performance", "balanced"):
        return _FULL_POWER_LIMIT_PCT
    return efficiency_pct


def _derived_clock_drop_pct(
    entry: dict[str, object],
    profile_id: str,
) -> float | None:
    lower_clock_mhz, upper_clock_mhz = _uv_limit_clock_drop_bounds_mhz_from_entry(
        entry,
        profile_id,
    )
    if int(lower_clock_mhz) <= 0 or int(upper_clock_mhz) <= 0:
        return None
    drop_pct = 1.0 - (float(lower_clock_mhz) / float(upper_clock_mhz))
    return max(0.0, drop_pct * 100.0)


def uv_limit_profile_target_for_gpu(
    gpu_name: object | None,
    profile_id: str,
) -> UvTierTarget | None:
    entry = _uv_limit_entry_for_gpu(gpu_name)
    if entry is None:
        return None
    return _uv_limit_profile_target_from_entry(entry, profile_id)


def uv_limit_clock_target_range_for_gpu(
    gpu_name: object | None,
    profile_id: str,
) -> tuple[int, int] | None:
    """Editable anchor-clock bounds, derived from the GPU's default tiers."""
    efficiency = uv_limit_profile_target_for_gpu(gpu_name, "efficiency")
    balanced = uv_limit_profile_target_for_gpu(gpu_name, "balanced")
    performance = uv_limit_profile_target_for_gpu(gpu_name, "performance")
    if efficiency is None or balanced is None or performance is None:
        return None
    profile = str(profile_id).strip().lower()
    if profile == "efficiency":
        return (efficiency.clock_mhz * 17 + 19) // 20, balanced.clock_mhz
    if profile == "balanced":
        return efficiency.clock_mhz, performance.clock_mhz
    if profile == "performance":
        # Round the +5% ceiling to the nearest MHz (2950 -> 3098 MHz).
        return balanced.clock_mhz, (performance.clock_mhz * 21 + 10) // 20
    return None


def _uv_limit_entry_for_gpu(gpu_name: object | None) -> dict[str, object] | None:
    normalized_name = str(gpu_name or "").upper()
    if not normalized_name:
        return None

    for entry in _UV_LIMIT_TARGETS:
        pattern_values = entry.get("patterns")
        if not isinstance(pattern_values, tuple):
            continue
        patterns = tuple(str(pattern) for pattern in pattern_values)
        if any(pattern in normalized_name for pattern in patterns):
            return entry
    return None


def _uv_limit_profile_target_from_entry(
    entry: dict[str, object],
    profile_id: str,
) -> UvTierTarget | None:
    normalized_profile = str(profile_id or "").strip().lower()
    value = entry.get(normalized_profile)
    if not isinstance(value, tuple) or len(value) != 2:
        return None
    voltage_mv, clock_mhz = value
    return UvTierTarget(
        gpu_family=str(entry["family"]),
        profile_id=normalized_profile,
        voltage_mv=int(voltage_mv),
        clock_mhz=int(clock_mhz),
    )


def _uv_limit_clock_drop_ceiling_mhz_from_entry(entry: dict[str, object]) -> int:
    value = entry.get("clock_drop_ceiling_mhz")
    if value is not None:
        if not isinstance(value, (int, float, str)):
            return 0
        return int(value)
    performance = _uv_limit_profile_target_from_entry(entry, "performance")
    if performance is None:
        return 0
    return int(performance.clock_mhz)


def _uv_limit_clock_drop_bounds_mhz_from_entry(
    entry: dict[str, object],
    profile_id: object | None,
) -> tuple[int, int]:
    profile = str(profile_id or "efficiency").strip().lower()
    efficiency = _uv_limit_profile_target_from_entry(entry, "efficiency")
    performance = _uv_limit_profile_target_from_entry(entry, "performance")
    ceiling_clock_mhz = _uv_limit_clock_drop_ceiling_mhz_from_entry(entry)
    if efficiency is None:
        return 0, 0
    if profile == "performance" and performance is not None:
        return int(performance.clock_mhz), int(ceiling_clock_mhz)
    if profile == "balanced" and performance is not None:
        return int(efficiency.clock_mhz), int(performance.clock_mhz)
    return int(efficiency.clock_mhz), int(ceiling_clock_mhz)


def voltage_drop_pct(*, start_voltage_mv: int, floor_voltage_mv: int) -> float:
    if int(start_voltage_mv) <= 0:
        return 0.0
    drop_mv = max(0, int(start_voltage_mv) - int(floor_voltage_mv))
    return float(drop_mv) / float(start_voltage_mv) * 100.0
