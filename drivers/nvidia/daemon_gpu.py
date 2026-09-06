"""Typed NVIDIA GPU access through the root PenguinBurner daemon.

The daemon owns NVML/NVAPI and all privileged state changes.  This client turns
its JSON replies into one cached, typed view for Python callers.  Undocumented
raw NVAPI reads used by the Afterburner importer intentionally live elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from runtime.daemon_client import (
    gpu_apply_clock_offsets,
    gpu_apply_locked_core_clock,
    gpu_apply_locked_core_clock_range,
    gpu_apply_power_limit,
    gpu_apply_vf_offsets,
    gpu_capabilities,
    gpu_enable_persistence_mode,
    gpu_reset_locked_core_clocks,
    gpu_reset_locked_memory_clocks,
    gpu_telemetry,
    gpu_vf_snapshot,
    probe_power_limit_support,
)


class VfPoint(TypedDict):
    index: int
    type: int
    voltage_based: int
    freq_khz: int
    voltage_uv: int
    base_freq_khz: int
    base_voltage_uv: int
    current_offset_khz: int


@dataclass(frozen=True, slots=True)
class GpuIdentity:
    index: int
    name: str = ""
    driver_version: str = ""
    pci_bus_id: str = ""
    pci_device_id: str = ""
    uuid: str = ""


@dataclass(frozen=True, slots=True)
class GpuMemoryInfo:
    index: int
    total_bytes: int
    free_bytes: int
    used_bytes: int


@dataclass(frozen=True, slots=True)
class GpuPowerLimits:
    management_enabled: bool | None = None
    current_w: float | None = None
    enforced_w: float | None = None
    default_w: float | None = None
    minimum_w: float | None = None
    maximum_w: float | None = None


@dataclass(frozen=True, slots=True)
class GpuClocks:
    graphics_mhz: int | None = None
    sm_mhz: int | None = None
    memory_mhz: int | None = None
    video_mhz: int | None = None


@dataclass(frozen=True, slots=True)
class GpuClockOffsets:
    gpc_mhz: int | None = None
    memory_mhz: int | None = None


@dataclass(frozen=True, slots=True)
class GpuFanCapabilities:
    count: int = 0
    minimum_speed_pct: int | None = None
    maximum_speed_pct: int | None = None


@dataclass(frozen=True, slots=True)
class GpuFeatures:
    vf_curve: bool = False
    voltage: bool = False


@dataclass(frozen=True, slots=True)
class GpuVfSummary:
    active_points: int = 0
    editable_core_points: int = 0


@dataclass(frozen=True, slots=True)
class GpuCapabilities:
    gpu_index: int
    gpu_count: int
    identity: GpuIdentity
    memory: GpuMemoryInfo | None
    architecture: int | None
    power: GpuPowerLimits
    supported_memory_clocks_mhz: tuple[int, ...]
    supported_core_clocks_mhz: tuple[int, ...]
    memory_clock_offset_range_mhz: tuple[int, int] | None
    clock_offsets: GpuClockOffsets
    fan: GpuFanCapabilities
    features: GpuFeatures
    vf: GpuVfSummary


@dataclass(frozen=True, slots=True)
class GpuTelemetry:
    gpu_index: int
    updated_unix_ns: int | None
    temperature_c: float | None
    fan_speeds_pct: tuple[float, ...]
    power_draw_w: float | None
    utilization_pct: float | None
    clocks: GpuClocks
    voltage_uv: int | None
    voltage_mv: float | None
    throttle_reason_mask: int | None
    clock_offsets: GpuClockOffsets


@dataclass(frozen=True, slots=True)
class GpuSnapshot:
    capabilities: GpuCapabilities
    telemetry: GpuTelemetry


_THROTTLE_REASONS = (
    (0x001, "idle"),
    (0x002, "app-clocks"),
    (0x004, "sw-power"),
    (0x008, "hw-slowdown"),
    (0x010, "sync-boost"),
    (0x020, "sw-thermal"),
    (0x040, "hw-thermal"),
    (0x080, "hw-power-brake"),
    (0x100, "display-clock"),
)


def format_perf_cap_reason_mask(mask: int) -> str:
    value = int(mask)
    if value == 0:
        return "none"
    known = 0
    labels = []
    for bit, label in _THROTTLE_REASONS:
        known |= bit
        if value & bit:
            labels.append(label)
    unknown = value & ~known
    if unknown:
        labels.append(f"unknown-0x{unknown:x}")
    return "+".join(labels) or "none"


class DaemonGpuClient:
    """One lazy client and cache for a daemon-owned NVIDIA GPU."""

    def __init__(self, gpu_index: int = 0):
        self.gpu_index = int(gpu_index)
        self._capabilities_cache: GpuCapabilities | None = None
        self._telemetry_cache: GpuTelemetry | None = None
        self._vf_points_cache: list[VfPoint] | None = None
        self._last_raw_microvolts: int | None = None

    def capabilities(self, *, refresh: bool = False) -> GpuCapabilities:
        if refresh or self._capabilities_cache is None:
            self._capabilities_cache = _parse_capabilities(
                gpu_capabilities(self.gpu_index), self.gpu_index
            )
        return self._capabilities_cache

    def telemetry(self, *, refresh: bool = False) -> GpuTelemetry:
        if refresh or self._telemetry_cache is None:
            self._telemetry_cache = _parse_telemetry(
                gpu_telemetry(self.gpu_index), self.gpu_index
            )
        return self._telemetry_cache

    def snapshot(self, *, refresh: bool = False) -> GpuSnapshot:
        """Return stable capabilities with optionally refreshed live telemetry."""
        return GpuSnapshot(
            capabilities=self.capabilities(),
            telemetry=self.telemetry(refresh=refresh),
        )

    @classmethod
    def discover_capabilities(cls) -> list[GpuCapabilities]:
        first = cls(0).capabilities()
        count = max(0, int(first.gpu_count))
        if count == 0:
            return []
        return [first, *(cls(index).capabilities() for index in range(1, count))]

    @classmethod
    def discover_identities(cls) -> list[GpuIdentity]:
        return [item.identity for item in cls.discover_capabilities()]

    def get_supported_core_clock_steps_mhz(self) -> list[int]:
        return list(self.capabilities().supported_core_clocks_mhz)

    def get_memory_clock_offset_range_mhz(self) -> tuple[int, int] | None:
        return self.capabilities().memory_clock_offset_range_mhz

    def refresh_points(self) -> list[VfPoint]:
        raw = gpu_vf_snapshot(self.gpu_index).get("points")
        if not isinstance(raw, list):
            raise RuntimeError("PenguinBurner daemon returned an invalid V/F snapshot")
        self._vf_points_cache = [
            _parse_vf_point(point) for point in raw if isinstance(point, dict)
        ]
        return list(self._vf_points_cache)

    def editable_core_points(self) -> list[VfPoint]:
        points = (
            self.refresh_points()
            if self._vf_points_cache is None
            else self._vf_points_cache
        )
        return [
            point
            for point in points
            if point["type"] == 0 and point["voltage_based"] == 1
        ]

    def read_raw_microvolts(self) -> int | None:
        self._last_raw_microvolts = self.telemetry(refresh=True).voltage_uv
        return self._last_raw_microvolts

    def read_microvolts(self, *_ignored) -> int | None:
        voltage_uv = self.read_raw_microvolts()
        if voltage_uv is None or not 300_000 <= voltage_uv <= 1_500_000:
            return None
        return voltage_uv

    def last_raw_microvolts(self) -> int | None:
        return self._last_raw_microvolts

    def read_live_voltage_mv(self) -> float | None:
        voltage_uv = self.read_microvolts()
        return None if voltage_uv is None else float(voltage_uv) / 1000.0

    def apply_offsets_khz(self, offsets) -> None:
        gpu_apply_vf_offsets(self.gpu_index, offsets)
        self._vf_points_cache = None
        self._telemetry_cache = None

    def apply_power_limit_w(self, power_limit_w: int) -> int:
        requested = int(power_limit_w)
        result = gpu_apply_power_limit(self.gpu_index, requested)
        self._capabilities_cache = None
        self._telemetry_cache = None
        return int(result.get("applied_w", requested))

    def power_limit_set_supported(self) -> bool:
        result = probe_power_limit_support(self.gpu_index)
        if result.get("reason") == "auto-uv-scan-running":
            raise RuntimeError("power-limit support probe deferred while Auto-UV runs")
        return bool(result.get("supported"))

    def enable_persistence_mode(self) -> bool:
        gpu_enable_persistence_mode(self.gpu_index)
        self._capabilities_cache = None
        return True

    def apply_clock_offsets(
        self,
        *,
        gpc_clk_vf_offset_mhz: int | None = None,
        mem_clk_vf_offset_mhz: int | None = None,
    ) -> dict:
        if gpc_clk_vf_offset_mhz is None and mem_clk_vf_offset_mhz is None:
            return {}
        result = gpu_apply_clock_offsets(
            self.gpu_index,
            gpc_clk_vf_offset_mhz=gpc_clk_vf_offset_mhz,
            mem_clk_vf_offset_mhz=mem_clk_vf_offset_mhz,
        )
        self._capabilities_cache = None
        self._telemetry_cache = None
        keys: list[str] = []
        if gpc_clk_vf_offset_mhz is not None:
            keys.extend(("gpc_clk_vf_offset_mhz", "gpc_clk_vf_offset_readback_mhz"))
        if mem_clk_vf_offset_mhz is not None:
            keys.extend(("mem_clk_vf_offset_mhz", "mem_clk_vf_offset_readback_mhz"))
        return _selected_result(result, *keys)

    def apply_locked_core_clock_mhz(
        self,
        clock_mhz: int,
        *,
        prefer_not_above: bool = True,
        snap_to_supported: bool = True,
    ) -> dict:
        result = gpu_apply_locked_core_clock(
            self.gpu_index,
            int(clock_mhz),
            prefer_not_above=bool(prefer_not_above),
            snap_to_supported=bool(snap_to_supported),
        )
        self._telemetry_cache = None
        return _selected_result(
            result,
            "requested_clock_mhz",
            "applied_clock_mhz",
            "mode",
            "supported_steps_mhz",
        )

    def apply_locked_core_clock_range_mhz(
        self,
        min_clock_mhz: int,
        max_clock_mhz: int,
        *,
        prefer_max_not_above: bool = True,
        snap_to_supported: bool = True,
    ) -> dict:
        result = gpu_apply_locked_core_clock_range(
            self.gpu_index,
            int(min_clock_mhz),
            int(max_clock_mhz),
            prefer_max_not_above=bool(prefer_max_not_above),
            snap_to_supported=bool(snap_to_supported),
        )
        self._telemetry_cache = None
        return _selected_result(
            result,
            "requested_min_clock_mhz",
            "requested_max_clock_mhz",
            "applied_min_clock_mhz",
            "applied_max_clock_mhz",
            "min_mode",
            "max_mode",
            "supported_steps_mhz",
        )

    def reset_locked_core_clocks(self) -> bool:
        gpu_reset_locked_core_clocks(self.gpu_index)
        self._telemetry_cache = None
        return True

    def reset_locked_memory_clocks(self) -> bool:
        gpu_reset_locked_memory_clocks(self.gpu_index)
        self._telemetry_cache = None
        return True


def _parse_capabilities(raw: dict, gpu_index: int) -> GpuCapabilities:
    identity_raw = _mapping(raw.get("identity"))
    identity = GpuIdentity(
        index=_int(identity_raw.get("index"), gpu_index),
        name=str(identity_raw.get("name") or ""),
        driver_version=str(identity_raw.get("driver_version") or ""),
        pci_bus_id=str(identity_raw.get("pci_bus_id") or ""),
        pci_device_id=str(identity_raw.get("pci_device_id") or ""),
        uuid=str(identity_raw.get("uuid") or ""),
    )
    memory_raw = raw.get("memory")
    memory = None
    if isinstance(memory_raw, dict):
        try:
            memory = GpuMemoryInfo(
                index=int(memory_raw.get("index", gpu_index)),
                total_bytes=int(memory_raw["total_bytes"]),
                free_bytes=int(memory_raw["free_bytes"]),
                used_bytes=int(memory_raw["used_bytes"]),
            )
        except (KeyError, TypeError, ValueError):
            memory = None
    power_raw = _mapping(raw.get("power_limits"))
    ranges_raw = _mapping(raw.get("clock_offset_ranges_mhz"))
    offsets_raw = _mapping(raw.get("clock_offsets_mhz"))
    fan_raw = _mapping(raw.get("fan"))
    features_raw = _mapping(raw.get("features"))
    vf_raw = _mapping(raw.get("vf_summary"))
    return GpuCapabilities(
        gpu_index=_int(raw.get("gpu_index"), gpu_index),
        gpu_count=_int(raw.get("gpu_count"), 0),
        identity=identity,
        memory=memory,
        architecture=_optional_int(raw.get("architecture")),
        power=GpuPowerLimits(
            management_enabled=_optional_bool(
                power_raw.get("power_management_enabled")
            ),
            current_w=_optional_float(power_raw.get("power_limit_w")),
            enforced_w=_optional_float(power_raw.get("enforced_power_limit_w")),
            default_w=_optional_float(power_raw.get("power_limit_default_w")),
            minimum_w=_optional_float(power_raw.get("power_limit_min_w")),
            maximum_w=_optional_float(power_raw.get("power_limit_max_w")),
        ),
        supported_memory_clocks_mhz=_integer_tuple(
            raw.get("supported_memory_clock_steps_mhz")
        ),
        supported_core_clocks_mhz=_integer_tuple(
            raw.get("supported_core_clock_steps_mhz")
        ),
        memory_clock_offset_range_mhz=_integer_pair(ranges_raw.get("memory")),
        clock_offsets=GpuClockOffsets(
            gpc_mhz=_optional_int(offsets_raw.get("gpc")),
            memory_mhz=_optional_int(offsets_raw.get("memory")),
        ),
        fan=GpuFanCapabilities(
            count=_int(fan_raw.get("count"), 0),
            minimum_speed_pct=_optional_int(fan_raw.get("min_speed_pct")),
            maximum_speed_pct=_optional_int(fan_raw.get("max_speed_pct")),
        ),
        features=GpuFeatures(
            vf_curve=bool(features_raw.get("vf_curve", False)),
            voltage=bool(features_raw.get("voltage", False)),
        ),
        vf=GpuVfSummary(
            active_points=_int(vf_raw.get("active_points"), 0),
            editable_core_points=_int(vf_raw.get("editable_core_points"), 0),
        ),
    )


def _parse_telemetry(raw: dict, gpu_index: int) -> GpuTelemetry:
    clocks_raw = _mapping(raw.get("clocks_mhz"))
    offsets_raw = _mapping(raw.get("clock_offsets_mhz"))
    voltage_uv = _optional_int(raw.get("voltage_uv"))
    voltage_mv = _optional_float(raw.get("voltage_mv"))
    if voltage_mv is None and voltage_uv is not None:
        voltage_mv = float(voltage_uv) / 1000.0
    return GpuTelemetry(
        gpu_index=_int(raw.get("gpu_index"), gpu_index),
        updated_unix_ns=_optional_int(raw.get("updated_unix_ns")),
        temperature_c=_optional_float(raw.get("temperature_c")),
        fan_speeds_pct=_float_tuple(raw.get("fan_speeds_pct")),
        power_draw_w=_optional_float(raw.get("power_draw_w")),
        utilization_pct=_optional_float(raw.get("gpu_utilization_pct")),
        clocks=GpuClocks(
            graphics_mhz=_optional_int(clocks_raw.get("graphics")),
            sm_mhz=_optional_int(clocks_raw.get("sm")),
            memory_mhz=_optional_int(clocks_raw.get("memory")),
            video_mhz=_optional_int(clocks_raw.get("video")),
        ),
        voltage_uv=voltage_uv,
        voltage_mv=voltage_mv,
        throttle_reason_mask=_optional_int(raw.get("throttle_reason_mask")),
        clock_offsets=GpuClockOffsets(
            gpc_mhz=_optional_int(offsets_raw.get("gpc")),
            memory_mhz=_optional_int(offsets_raw.get("memory")),
        ),
    )


def _parse_vf_point(raw: dict) -> VfPoint:
    fields = (
        "index",
        "type",
        "voltage_based",
        "freq_khz",
        "voltage_uv",
        "base_freq_khz",
        "base_voltage_uv",
        "current_offset_khz",
    )
    try:
        return VfPoint(**{field: int(raw[field]) for field in fields})
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "PenguinBurner daemon returned an invalid V/F point"
        ) from exc


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _integer_tuple(value) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    result: set[int] = set()
    for item in value:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            pass
    return tuple(sorted(result))


def _float_tuple(value) -> tuple[float, ...]:
    if not isinstance(value, list):
        return ()
    result = []
    for item in value:
        parsed = _optional_float(item)
        if parsed is not None:
            result.append(parsed)
    return tuple(result)


def _integer_pair(value) -> tuple[int, int] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        return int(value[0]), int(value[1])
    except (TypeError, ValueError):
        return None


def _optional_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int(value, default: int) -> int:
    parsed = _optional_int(value)
    return int(default) if parsed is None else parsed


def _optional_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value) -> bool | None:
    return value if isinstance(value, bool) else None


def _selected_result(result: dict, *keys: str) -> dict:
    return {key: result[key] for key in keys if key in result}
