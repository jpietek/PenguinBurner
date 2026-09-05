from __future__ import annotations

from dataclasses import dataclass

from auto_uv.domain.user_options import AUTO_UV_DEFAULTS
from auto_uv.scan_mode.auto_uv_mode import AUTO_UV_MODE_ADAPTIVE
from auto_uv.scan_mode.auto_uv_mode import AUTO_UV_MODE_BALANCED
from auto_uv.scan_mode.auto_uv_mode import AUTO_UV_MODE_EFFICIENCY
from auto_uv.scan_mode.auto_uv_mode import AUTO_UV_MODE_PERFORMANCE
from auto_uv.scan_mode.uv_limits import (
    AUTO_UV_PERFORMANCE_OC_PROFILE_ID,
    uv_limit_power_limit_pct_for_gpu,
    uv_limit_profile_target_for_gpu,
)
from common.penguin_burner_paths import default_runtime_config_path
from drivers.nvidia.daemon_gpu import DaemonGpuClient

from ui.features.tuning.gpu_selection import runtime_gpu_index


DEFAULT_AUTO_UV_TAIL_RISE_BINS = AUTO_UV_DEFAULTS.tail_rise_bins
DEFAULT_AUTO_UV_BALANCED_TAIL_RISE_BINS = AUTO_UV_DEFAULTS.balanced_tail_rise_bins
DEFAULT_AUTO_UV_PERFORMANCE_TAIL_RISE_BINS = AUTO_UV_DEFAULTS.performance_tail_rise_bins
AUTO_UV_PRESET_EFFICIENCY = "efficiency"
AUTO_UV_PRESET_BALANCED = "balanced"
AUTO_UV_PRESET_PERFORMANCE = "performance"
AUTO_UV_PRESET_ADAPTIVE = "adaptive"
# One click, three profiles: the adaptive all-tiers scan is the default.
DEFAULT_AUTO_UV_PRESET = AUTO_UV_PRESET_ADAPTIVE
# Wall-clock ranges cover discovery and descent only. Final verification is
# excluded because the user chooses its duration after the sweep. The exact
# scan length still depends on the GPU's voltage bins and stability retries.
AUTO_UV_SCAN_ESTIMATE_MINUTES = {
    AUTO_UV_PRESET_EFFICIENCY: (10, 20),
    AUTO_UV_PRESET_BALANCED: (10, 20),
    AUTO_UV_PRESET_PERFORMANCE: (15, 25),
    AUTO_UV_PRESET_ADAPTIVE: (25, 35),
}
GPU_UNDERVOLTING_PURPOSE_TEXT = (
    "GPU undervolting is meant to make your graphics card consume significantly "
    "less power while giving up as little performance as possible. The practical "
    "result can be dead-silent fan operation, lower temperatures, and lower "
    "electricity bills. PenguinBurner automatically searches for the operating "
    "sweet spot of your Nvidia GPU, so you do not have to resort to trial and "
    "error or risk introducing avoidable system instability."
)


@dataclass(frozen=True, slots=True)
class AutoUvTargetDefault:
    gpu_name: str | None
    gpu_family: str | None
    voltage_mv: int | None
    clock_mhz: int | None
    profile_id: str
    preset_matched: bool


@dataclass(frozen=True, slots=True)
class AutoUvPowerLimitDefault:
    watts: int | None
    pct: float | None
    gpu_name: str | None
    gpu_family: str | None
    preset_matched: bool


@dataclass(frozen=True, slots=True)
class AutoUvPreset:
    preset_id: str
    label: str
    auto_uv_mode: str
    tail_rise_bins: int


@dataclass(frozen=True, slots=True)
class AutoUvNvmlInfo:
    power_draw_w: float | None = None
    power_management_enabled: bool | None = None
    power_limit_set_supported: bool | None = None
    power_limit_w: float | None = None
    power_limit_default_w: float | None = None
    power_limit_min_w: float | None = None
    power_limit_max_w: float | None = None
    graphics_clock_mhz: int | None = None
    memory_clock_mhz: int | None = None
    supported_memory_clocks_mhz: tuple[int, ...] = ()
    supported_graphics_clock_steps_mhz: tuple[int, ...] = ()


def auto_uv_preset(preset_id: object) -> AutoUvPreset:
    normalized = str(preset_id or DEFAULT_AUTO_UV_PRESET).strip().lower()
    if normalized == AUTO_UV_PRESET_EFFICIENCY:
        return AutoUvPreset(
            preset_id=AUTO_UV_PRESET_EFFICIENCY,
            label="Efficiency",
            auto_uv_mode=AUTO_UV_MODE_EFFICIENCY,
            tail_rise_bins=DEFAULT_AUTO_UV_TAIL_RISE_BINS,
        )
    if normalized == AUTO_UV_PRESET_PERFORMANCE:
        return AutoUvPreset(
            preset_id=AUTO_UV_PRESET_PERFORMANCE,
            label="Performance",
            auto_uv_mode=AUTO_UV_MODE_PERFORMANCE,
            tail_rise_bins=DEFAULT_AUTO_UV_PERFORMANCE_TAIL_RISE_BINS,
        )
    if normalized == AUTO_UV_PRESET_ADAPTIVE:
        # One full run generates all three tier profiles. Each per-tier descent
        # carries its own rising tail, so the preset itself has none to configure.
        return AutoUvPreset(
            preset_id=AUTO_UV_PRESET_ADAPTIVE,
            label="All tiers (adaptive)",
            auto_uv_mode=AUTO_UV_MODE_ADAPTIVE,
            tail_rise_bins=DEFAULT_AUTO_UV_TAIL_RISE_BINS,
        )
    return AutoUvPreset(
        preset_id=AUTO_UV_PRESET_BALANCED,
        label="Balanced",
        auto_uv_mode=AUTO_UV_MODE_BALANCED,
        tail_rise_bins=DEFAULT_AUTO_UV_BALANCED_TAIL_RISE_BINS,
    )


def auto_uv_scan_estimate_minutes(preset_id: object) -> tuple[int, int]:
    preset = auto_uv_preset(preset_id)
    return AUTO_UV_SCAN_ESTIMATE_MINUTES[preset.preset_id]


def auto_uv_scan_estimate_text(preset_id: object) -> str:
    minimum, maximum = auto_uv_scan_estimate_minutes(preset_id)
    return f"about {minimum}-{maximum} minutes"


def auto_uv_power_limit_default(
    *,
    max_w: float | None,
    min_w: float | None = None,
    default_w: float | None = None,
    gpu_name: object | None = None,
    gpu_index: int | None = None,
    preset_id: object | None = AUTO_UV_PRESET_EFFICIENCY,
) -> AutoUvPowerLimitDefault:
    """Preset-aware default board-power cap in watts.

    The efficiency preset pairs its V/F floor with a fraction of the card's
    stock power budget (the driver default limit, not the raised OC maximum);
    balanced and performance (and any GPU not covered by the tier table) keep
    the stock board power budget — matching regimes keep the full scan's
    balanced descent donatable to the performance tier.
    """
    detected_name = str(gpu_name).strip() if gpu_name else _query_gpu_name(gpu_index)
    preset = auto_uv_preset(preset_id)
    max_watts = _positive_float(max_w)
    base_watts = _positive_float(default_w) or max_watts
    if base_watts is None:
        return AutoUvPowerLimitDefault(
            watts=None,
            pct=None,
            gpu_name=detected_name or None,
            gpu_family=None,
            preset_matched=False,
        )
    # Callers pass a real tier id (the dialog keeps one page per profile);
    # a plain "adaptive" request falls back to the balanced budget.
    pct = uv_limit_power_limit_pct_for_gpu(
        detected_name,
        profile_id=_defaults_profile_id(preset, AUTO_UV_PRESET_BALANCED),
    )
    if pct is None:
        return AutoUvPowerLimitDefault(
            watts=int(round(base_watts)),
            pct=100.0,
            gpu_name=detected_name or None,
            gpu_family=None,
            preset_matched=False,
        )
    watts = int(round(base_watts * (float(pct) / 100.0)))
    floor_watts = _positive_float(min_w)
    if floor_watts is not None:
        watts = max(int(round(floor_watts)), watts)
    if max_watts is not None:
        watts = min(int(round(max_watts)), watts)
    return AutoUvPowerLimitDefault(
        watts=watts,
        pct=float(pct),
        gpu_name=detected_name or None,
        gpu_family=None,
        preset_matched=True,
    )


def _defaults_profile_id(preset: AutoUvPreset, adaptive_fallback: str) -> str:
    if preset.preset_id == AUTO_UV_PRESET_ADAPTIVE:
        return adaptive_fallback
    return preset.preset_id


def _positive_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0.0 else None


def auto_uv_target_default(
    *,
    gpu_name: object | None = None,
    gpu_index: int | None = None,
    profile_id: str = AUTO_UV_PERFORMANCE_OC_PROFILE_ID,
) -> AutoUvTargetDefault:
    detected_name = str(gpu_name).strip() if gpu_name else _query_gpu_name(gpu_index)
    target = uv_limit_profile_target_for_gpu(detected_name, profile_id)
    if target is None:
        return AutoUvTargetDefault(
            gpu_name=detected_name or None,
            gpu_family=None,
            voltage_mv=None,
            clock_mhz=None,
            profile_id=str(profile_id),
            preset_matched=False,
        )
    return AutoUvTargetDefault(
        gpu_name=detected_name or None,
        gpu_family=str(target.gpu_family),
        voltage_mv=int(target.voltage_mv),
        clock_mhz=int(target.clock_mhz),
        profile_id=str(target.profile_id),
        preset_matched=True,
    )


def auto_uv_performance_preset_label(_preview=None) -> str:
    return "Performance"


def auto_uv_performance_preset_tooltip(_preview=None) -> str:
    return (
        f"Use the {DEFAULT_AUTO_UV_PERFORMANCE_TAIL_RISE_BINS}-bin tail "
        "curve, then run the Performance Auto-OC ladder toward the "
        "configured voltage and clock targets."
    )


def read_auto_uv_nvml_info(
    gpu_index: int,
    *,
    gpu_client: DaemonGpuClient | None = None,
) -> AutoUvNvmlInfo:
    client = gpu_client
    try:
        client = client or DaemonGpuClient(int(gpu_index))
        snapshot = client.snapshot(refresh=True)
    except Exception:
        snapshot = None

    capabilities = snapshot.capabilities if snapshot is not None else None
    telemetry = snapshot.telemetry if snapshot is not None else None
    power = capabilities.power if capabilities is not None else None
    clocks = telemetry.clocks if telemetry is not None else None

    power_limit_set_supported_value = (
        _power_limit_set_supported(client)
        if client is not None and _power_limit_set_probe_applicable(power)
        else None
    )
    return AutoUvNvmlInfo(
        power_draw_w=getattr(telemetry, "power_draw_w", None),
        power_management_enabled=getattr(power, "management_enabled", None),
        power_limit_set_supported=power_limit_set_supported_value,
        power_limit_w=getattr(power, "current_w", None),
        power_limit_default_w=getattr(power, "default_w", None),
        power_limit_min_w=getattr(power, "minimum_w", None),
        power_limit_max_w=getattr(power, "maximum_w", None),
        graphics_clock_mhz=getattr(clocks, "graphics_mhz", None),
        memory_clock_mhz=getattr(clocks, "memory_mhz", None),
        supported_memory_clocks_mhz=(
            capabilities.supported_memory_clocks_mhz if capabilities else ()
        ),
        supported_graphics_clock_steps_mhz=(
            capabilities.supported_core_clocks_mhz if capabilities else ()
        ),
    )


def _power_limit_set_probe_applicable(power: object | None) -> bool:
    if power is None:
        return False
    if getattr(power, "management_enabled", None) is False:
        return False
    return (
        getattr(power, "minimum_w", None) is not None
        and getattr(power, "maximum_w", None) is not None
    )


def _power_limit_set_supported(gpu_client: DaemonGpuClient) -> bool:
    try:
        return gpu_client.power_limit_set_supported()
    except Exception:
        return False


_NVML_INFO_UNAVAILABLE_TEXT = (
    "GPU limits unavailable: the PenguinBurner background hardware service "
    "is not responding. Close this dialog and click Setup Auto Undervolt "
    "again to install or repair it (one admin prompt)."
)


def auto_uv_nvml_info_text(info: AutoUvNvmlInfo | None) -> str:
    if info is None:
        return _NVML_INFO_UNAVAILABLE_TEXT

    rows = [
        _power_limit_text(info),
        _current_clocks_text(info),
        _supported_memory_clocks_text(info.supported_memory_clocks_mhz),
        _supported_core_range_text(info.supported_graphics_clock_steps_mhz),
        _power_management_text(info.power_management_enabled),
        _power_limit_set_text(info.power_limit_set_supported),
    ]
    text = "\n".join(row for row in rows if row)
    return text or _NVML_INFO_UNAVAILABLE_TEXT


def memory_offset_mhz_range(
    gpu_index: int | None = None,
    *,
    gpu_client: DaemonGpuClient | None = None,
) -> tuple[int, int]:
    fallback = (0, 2000)
    try:
        index = (
            runtime_gpu_index(default_runtime_config_path())
            if gpu_index is None
            else int(gpu_index)
        )
        client = gpu_client or DaemonGpuClient(gpu_index=index)
        driver_range = client.capabilities().memory_clock_offset_range_mhz
    except Exception:
        return fallback
    if not driver_range:
        return fallback
    _driver_min, driver_max = driver_range
    try:
        max_mhz = int(driver_max)
    except (TypeError, ValueError):
        return fallback
    # The driver-reported max is the real authority for this GPU; the static
    # fallback cap only applies when NVML exposes no range.
    return 0, max(0, max_mhz)


def auto_uv_voltage_floor_range_mv(
    gpu_index: int | None = None,
    *,
    gpu_client: DaemonGpuClient | None = None,
) -> tuple[int, int] | None:
    """Settable voltage-floor range (mV), derived from the live V/F curve.

    Lower bound = the curve "knee": the lowest voltage that still holds a real
    boost clock (at/above half the card's max base clock). Below the knee the
    curve is the flat idle shelf (e.g. 180 MHz on Blackwell), so those voltages
    are unreachable-under-load and pointless as a floor. Upper bound = the
    curve's max voltage point. Returns None when the live curve
    cannot be read, so the dialog keeps the voltage target automatic.
    """
    index = (
        runtime_gpu_index(default_runtime_config_path())
        if gpu_index is None
        else int(gpu_index)
    )
    try:
        client = gpu_client or DaemonGpuClient(gpu_index=index)
        points = [
            (int(p["voltage_uv"]) // 1000, int(p["base_freq_khz"]) // 1000)
            for p in client.editable_core_points()
        ]
    except Exception:
        return None
    points = [(v, c) for v, c in points if v > 0 and c > 0]
    if not points:
        return None
    max_clock = max(c for _, c in points)
    max_voltage = max(v for v, _ in points)
    # Knee: lowest voltage that reaches at least half the max base clock. Snap to
    # the 5 mV spinbox step so the bound sits on a settable value.
    useful = [v for v, c in points if c * 2 >= max_clock]
    knee = min(useful) if useful else min(v for v, _ in points)
    knee = int(round(knee / 5.0) * 5)
    return (knee, int(max_voltage))


def _query_gpu_name(gpu_index: int | None = None) -> str | None:
    try:
        index = (
            int(gpu_index)
            if gpu_index is not None
            else runtime_gpu_index(default_runtime_config_path())
        )
        name = DaemonGpuClient(gpu_index=index).capabilities().identity.name
    except Exception:
        return None
    return str(name).strip() if name else None


def _power_limit_text(info: AutoUvNvmlInfo) -> str:
    parts = []
    if info.power_limit_w is not None:
        parts.append(f"current {_watts_text(info.power_limit_w)}")
    if info.power_limit_default_w is not None:
        parts.append(f"default {_watts_text(info.power_limit_default_w)}")
    if info.power_limit_min_w is not None and info.power_limit_max_w is not None:
        parts.append(
            f"range {_watts_number_text(info.power_limit_min_w)}-"
            f"{_watts_number_text(info.power_limit_max_w)} W"
        )
    return f"Power limit: {' | '.join(parts)}" if parts else ""


def _current_clocks_text(info: AutoUvNvmlInfo) -> str:
    parts = []
    if info.graphics_clock_mhz is not None:
        parts.append(f"core {int(info.graphics_clock_mhz)} MHz")
    if info.memory_clock_mhz is not None:
        parts.append(f"memory {int(info.memory_clock_mhz)} MHz")
    return f"Clocks now: {' | '.join(parts)}" if parts else ""


def _supported_memory_clocks_text(clocks_mhz: tuple[int, ...]) -> str:
    text = _clock_list_text(clocks_mhz)
    return f"Supported memory clocks: {text}" if text else ""


def _supported_core_range_text(clocks_mhz: tuple[int, ...]) -> str:
    if not clocks_mhz:
        return ""
    low = min(clocks_mhz)
    high = max(clocks_mhz)
    count = len(set(clocks_mhz))
    return f"Supported core range: {low}-{high} MHz ({count} steps)"


def _power_management_text(enabled: bool | None) -> str:
    if enabled is None:
        return ""
    return f"Power management: {'enabled' if enabled else 'disabled'}"


def _power_limit_set_text(supported: bool | None) -> str:
    if supported is None:
        return ""
    return f"Fixed power-limit writes: {'supported' if supported else 'unavailable'}"


def _clock_list_text(clocks_mhz: tuple[int, ...]) -> str:
    clocks = tuple(sorted({int(clock) for clock in clocks_mhz}))
    if not clocks:
        return ""
    if len(clocks) <= 5:
        return ", ".join(str(clock) for clock in clocks) + " MHz"
    return f"{clocks[0]}-{clocks[-1]} MHz ({len(clocks)} steps)"


def _watts_text(value: float) -> str:
    return f"{_watts_number_text(value)} W"


def _watts_number_text(value: float) -> str:
    rounded = round(float(value), 1)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.1f}"
