"""Apply Auto-UV V/F curve plans to the live GPU.

This is the only Auto-UV module that creates NVAPI/NVML helpers and applies curve plans.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, cast

from drivers.nvidia.daemon_gpu import DaemonGpuClient
from profiles.gpu_identity import normalized_gpu_identity
from runtime.support.vf_curve_plan import apply_plan
from runtime.support.nvidia_runtime_defaults import reset_nvidia_runtime_defaults

from auto_uv.domain.types import AutoUvError, AutoUvPowerLimitApplyError
from auto_uv.scan_mode.auto_uv_mode import (
    ADAPTIVE_TIER_MODES,
    adaptive_tier_option_key,
)
from .memory_clock_offset_user_option import auto_uv_memory_offset_mhz
from .probe_clock_ceiling import ProbeClockCeilingController
from .runtime_vf_offset_reset_check import assert_zero_runtime_vf_offsets


@dataclass(slots=True)
class LiveGpuVfCurveApplier:
    gpu_index: int
    gpu: DaemonGpuClient
    runtime_default_plan: list[dict]
    translated_gpu_policy: dict
    gpu_identity: dict[str, str | int] = field(default_factory=dict)
    power_limit_set_supported: bool = True
    baseline_power_limit_w: int | None = None
    requested_power_limit_w: int | None = None
    min_power_limit_w: int | None = None
    max_power_limit_w: int | None = None
    clock_ceiling: ProbeClockCeilingController | None = None

    @property
    def reader(self) -> DaemonGpuClient:
        return self.gpu

    @property
    def policy_controller(self) -> DaemonGpuClient:
        return self.gpu

    @property
    def live_voltage_reader(self) -> DaemonGpuClient:
        return self.gpu

    @property
    def power_limit_w(self) -> int | None:
        value = self.translated_gpu_policy.get("power_limit_w")
        return int(value) if value is not None else None

    def apply_plan(self, plan: list[dict]) -> None:
        apply_plan(self.reader, plan)
        refresh_points = getattr(self.reader, "refresh_points", None)
        if callable(refresh_points):
            refresh_points()

    def start_clock_ceiling(self, flatten_target: dict) -> None:
        self.clock_ceiling = ProbeClockCeilingController(
            flatten_target=flatten_target,
            policy_controller=self.policy_controller,
        )
        self.clock_ceiling.apply()

    def clamp_power_limit_w(self, watts: int) -> int:
        """Keep a requested board-power cap inside the card's [min, max].

        The NVML driver rejects a limit below the hardware minimum (e.g. a
        5080's 300W floor), so an efficiency tier's computed cap must clamp
        up rather than fail the apply."""
        clamped = int(watts)
        if self.min_power_limit_w is not None:
            clamped = max(clamped, int(self.min_power_limit_w))
        if self.max_power_limit_w is not None:
            clamped = min(clamped, int(self.max_power_limit_w))
        return clamped

    def apply_requested_power_limit(
        self,
        *,
        log: Callable[[str], None],
        purpose: str = "final verification",
    ) -> int | None:
        requested_w = _positive_power_limit_w(self.requested_power_limit_w)
        if requested_w is None:
            if self.power_limit_w is not None:
                verify_applied_power_limit_w(
                    self.policy_controller,
                    requested_w=self.power_limit_w,
                    reported_applied_w=self.power_limit_w,
                )
            return self.power_limit_w
        if not self.power_limit_set_supported:
            self.requested_power_limit_w = None
            self.translated_gpu_policy.pop("power_limit_w", None)
            log(
                "Auto-UV power limit: skipped unsupported fixed write for "
                f"{str(purpose)}; "
                "continuing without saved power limit"
            )
            return None
        requested_w = self.clamp_power_limit_w(int(requested_w))
        if self.power_limit_w == requested_w:
            verify_applied_power_limit_w(
                self.policy_controller,
                requested_w=requested_w,
                reported_applied_w=requested_w,
            )
            self.requested_power_limit_w = None
            return self.power_limit_w
        try:
            applied_power_limit_w = self.policy_controller.apply_power_limit_w(
                int(requested_w)
            )
        except Exception as exc:
            self.requested_power_limit_w = None
            raise AutoUvPowerLimitApplyError(
                "Auto-UV power limit: unable to establish "
                f"{int(requested_w)}W for {str(purpose)}; scan stopped before "
                f"probing under an unknown power regime: {exc}"
            ) from exc
        try:
            verified_power_limit_w = verify_applied_power_limit_w(
                self.policy_controller,
                requested_w=int(requested_w),
                reported_applied_w=int(applied_power_limit_w),
            )
        except AutoUvPowerLimitApplyError:
            self.requested_power_limit_w = None
            raise
        except Exception as exc:
            self.requested_power_limit_w = None
            raise AutoUvPowerLimitApplyError(
                "Auto-UV power limit: unable to read back "
                f"{int(requested_w)}W for {str(purpose)}; scan stopped before "
                f"probing under an unknown power regime: {exc}"
            ) from exc
        self.translated_gpu_policy["power_limit_w"] = int(verified_power_limit_w)
        # Consume the request so a subsequent tier's apply starts clean and
        # never mistakes this tier's applied cap for a fresh scan-wide one.
        self.requested_power_limit_w = None
        log(
            "Auto-UV power limit: applied "
            f"{int(verified_power_limit_w)}W for {str(purpose)}"
        )
        return int(verified_power_limit_w)

    def close(self) -> None:
        if self.clock_ceiling is not None:
            self.clock_ceiling.close()


def open_live_gpu_vf_curve_applier(
    *,
    gpu_index: int,
    runtime_options: dict,
    log: Callable[[str], None],
) -> LiveGpuVfCurveApplier:
    gpu = DaemonGpuClient(gpu_index=int(gpu_index))
    try:
        gpu.refresh_points()
        gpu_identity = normalized_gpu_identity(
            gpu.capabilities().identity,
            index_at_verification=int(gpu_index),
        )
    except Exception as exc:
        raise AutoUvError(
            "failed to create Linux NVAPI VF helper"
            f": {exc}. This driver/GPU combination may not expose editable "
            "voltage-based V/F points."
        ) from exc
    if not str(gpu_identity.get("uuid") or "").strip():
        raise AutoUvError(f"GPU {int(gpu_index)} did not report a stable UUID")

    runtime_reset = reset_nvidia_runtime_defaults(
        gpu_index=int(gpu_index),
        log=log,
    )
    runtime_default_plan = list(runtime_reset["plan"])
    # The semantic daemon reset already applied and verified this zero plan.
    # Refresh the reader created above so the scan starts from that live state
    # without issuing the same privileged V/F write a second time.
    gpu.refresh_points()
    assert_zero_runtime_vf_offsets(gpu)

    gpu_name = runtime_reset.get("gpu_name")
    pci_device_id = runtime_reset.get("pci_device_id")
    # The reset already exercised the setter and checked its readback. A
    # separate capability probe is deferred by the daemon while a scan runs;
    # that response says nothing about hardware support.
    power_limit_set_supported = runtime_reset.get("power_limit_set_supported")
    if not isinstance(power_limit_set_supported, bool):
        raise AutoUvPowerLimitApplyError(
            "Auto-UV power limit: stock reset did not establish power-limit "
            "write support; scan stopped before probing. Ensure the running "
            "penguin-burnerd daemon matches the installed application."
        )
    if not power_limit_set_supported:
        log(
            "Auto-UV power limit: driver reports fixed power-limit writes are "
            "unsupported; continuing without saved power limit"
        )
    baseline_power_limit_w = (
        _positive_power_limit_w(runtime_reset.get("power_limit_w"))
        if power_limit_set_supported
        else None
    )
    reset_power_limits = runtime_reset.get("power_limits")
    reset_power_limits = (
        reset_power_limits if isinstance(reset_power_limits, dict) else {}
    )
    min_power_limit_w = _positive_power_limit_w(
        reset_power_limits.get("power_limit_min_w")
    )
    max_power_limit_w = _positive_power_limit_w(
        reset_power_limits.get("power_limit_max_w")
    )
    translated_gpu_policy = {"gpu_name": gpu_name}
    if pci_device_id:
        translated_gpu_policy["pci_device_id"] = pci_device_id
    if baseline_power_limit_w is not None:
        translated_gpu_policy["power_limit_w"] = baseline_power_limit_w
    requested_power_limit_w = _auto_uv_power_limit_w(runtime_options)
    if requested_power_limit_w is not None and not power_limit_set_supported:
        requested_power_limit_w = None
    elif requested_power_limit_w is not None:
        if min_power_limit_w is not None:
            requested_power_limit_w = max(
                int(requested_power_limit_w), int(min_power_limit_w)
            )
        if max_power_limit_w is not None:
            requested_power_limit_w = min(
                int(requested_power_limit_w), int(max_power_limit_w)
            )
        try:
            applied_power_limit_w = gpu.apply_power_limit_w(
                int(requested_power_limit_w)
            )
            verified_power_limit_w = verify_applied_power_limit_w(
                gpu,
                requested_w=int(requested_power_limit_w),
                reported_applied_w=int(applied_power_limit_w),
            )
        except Exception as exc:
            raise AutoUvPowerLimitApplyError(
                "Auto-UV power limit: unable to establish "
                f"{int(requested_power_limit_w)}W for discovery and sweep; "
                f"scan stopped before probing: {exc}"
            ) from exc
        translated_gpu_policy["power_limit_w"] = int(verified_power_limit_w)
        log(
            "Auto-UV power limit: applied "
            f"{int(verified_power_limit_w)}W for discovery and sweep"
        )

    memory_offset_mhz, memory_offset_limit_mhz = auto_uv_memory_offset_mhz(
        runtime_options,
        policy_controller=gpu,
    )
    if memory_offset_mhz is not None:
        translated_gpu_policy["mem_clk_vf_offset_mhz"] = int(memory_offset_mhz)
        translated_gpu_policy["mem_clk_vf_offset_limit_mhz"] = int(
            memory_offset_limit_mhz
        )
        raw_memory_offset = runtime_options.get(
            "auto_uv_memory_offset_mhz",
            runtime_options.get("memory_offset_mhz"),
        )
        if raw_memory_offset not in (None, "") and int(raw_memory_offset) != int(
            memory_offset_mhz
        ):
            log(
                f"Auto-UV memory offset: requested {int(raw_memory_offset)} MHz "
                f"clamped to {int(memory_offset_mhz)} MHz "
                f"(limit {int(memory_offset_limit_mhz)} MHz)"
            )
        if int(memory_offset_mhz) != 0:
            applied_memory_offset = gpu.apply_clock_offsets(
                mem_clk_vf_offset_mhz=int(memory_offset_mhz)
            )
            readback_mhz = applied_memory_offset.get("mem_clk_vf_offset_readback_mhz")
            if readback_mhz is None:
                log(
                    f"Auto-UV memory offset: applied {int(memory_offset_mhz):+d} MHz "
                    "(driver does not support read-back)"
                )
            elif int(readback_mhz) == int(memory_offset_mhz):
                log(
                    f"Auto-UV memory offset: applied {int(memory_offset_mhz):+d} MHz, "
                    f"NVML read-back confirms {int(readback_mhz):+d} MHz"
                )
            else:
                log(
                    f"Auto-UV memory offset MISMATCH: requested "
                    f"{int(memory_offset_mhz):+d} MHz but NVML reads back "
                    f"{int(readback_mhz):+d} MHz -- the driver clamped or ignored it"
                )

    return LiveGpuVfCurveApplier(
        gpu_index=int(gpu_index),
        gpu=gpu,
        gpu_identity=gpu_identity,
        min_power_limit_w=min_power_limit_w,
        max_power_limit_w=max_power_limit_w,
        runtime_default_plan=runtime_default_plan,
        translated_gpu_policy=translated_gpu_policy,
        power_limit_set_supported=power_limit_set_supported,
        baseline_power_limit_w=baseline_power_limit_w,
        requested_power_limit_w=requested_power_limit_w,
    )


def _auto_uv_power_limit_w(runtime_options: dict) -> int | None:
    value = runtime_options.get("auto_uv_power_limit_w")
    if value in (None, ""):
        # Full scans carry per-tier power limits instead of the scan-wide
        # key. The highest tier request establishes the startup regime before
        # adaptive orchestration begins. Each tier then applies and reads back
        # its own exact cap before its stock baseline, flattened baseline,
        # descent, selection, and final verification.
        tier_values = [
            runtime_options.get(adaptive_tier_option_key(tier, "power_limit_w"))
            for tier in ADAPTIVE_TIER_MODES
        ]
        tier_values = [item for item in tier_values if item not in (None, "")]
        if not tier_values:
            return None
        value = max(float(cast(Any, item)) for item in tier_values)
    try:
        power_limit_w = int(round(float(cast(Any, value))))
    except (TypeError, ValueError) as exc:
        raise AutoUvError(f"invalid Auto-UV power limit: {value!r}") from exc
    return power_limit_w if power_limit_w > 0 else None


def _positive_power_limit_w(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        power_limit_w = int(round(float(cast(Any, value))))
    except (TypeError, ValueError):
        return None
    return power_limit_w if power_limit_w > 0 else None


def verify_applied_power_limit_w(
    policy_controller,
    *,
    requested_w: int,
    reported_applied_w: int,
) -> int:
    """Confirm the daemon's applied result against live capabilities when available."""
    requested = int(requested_w)
    reported = int(reported_applied_w)
    capabilities = getattr(policy_controller, "capabilities", None)
    if not callable(capabilities):
        if reported != requested:
            raise AutoUvPowerLimitApplyError(
                f"daemon reported {reported}W after requesting {requested}W"
            )
        return reported
    try:
        live = capabilities(refresh=True)
    except Exception as exc:
        raise AutoUvPowerLimitApplyError(
            f"unable to read back {requested}W power limit: {exc}"
        ) from exc
    power = getattr(live, "power", None)
    readbacks = (
        getattr(power, "current_w", None),
        getattr(power, "enforced_w", None),
    )
    # An independently enforced limit can be lower than the configured limit.
    # It cannot confirm that a requested setter change actually happened.
    configured = readbacks[0] if readbacks[0] is not None else readbacks[1]
    if (
        reported != requested
        or configured is None
        or not math.isfinite(float(configured))
        or abs(float(configured) - requested) > 1.0
    ):
        current_w, enforced_w = readbacks
        raise AutoUvPowerLimitApplyError(
            "power-limit read-back mismatch after requesting "
            f"{requested}W: current={current_w!r} enforced={enforced_w!r} "
            f"daemon-reported={reported}W"
        )
    return requested
