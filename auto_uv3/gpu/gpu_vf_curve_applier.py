"""Apply Auto-UV3 V/F curve plans to the live GPU.

This is the only Auto-UV3 module that creates NVAPI/NVML helpers and applies curve plans.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from afterburner.import_vf_curve import apply_plan
from hidden_nvapi_vf import (
    create_hidden_vf_curve_reader,
    get_hidden_vf_curve_reader_last_error,
)
from nvml_gpu_policy import NvmlGpuPolicyController
from nvidia_runtime_defaults import reset_nvidia_runtime_defaults

from ..auto_uv_types import AutoUvError
from .live_nvml_voltage_reader import LiveNvmlVoltageReader
from .memory_clock_offset_user_option import auto_uv_memory_offset_mhz
from .probe_clock_ceiling import ProbeClockCeilingController
from .runtime_vf_offset_reset_check import assert_zero_runtime_vf_offsets


@dataclass(slots=True)
class LiveGpuVfCurveApplier:
    gpu_index: int
    reader: object
    policy_controller: NvmlGpuPolicyController
    live_voltage_reader: LiveNvmlVoltageReader
    runtime_default_plan: list[dict]
    translated_gpu_policy: dict
    clock_ceiling: ProbeClockCeilingController | None = None

    @property
    def power_limit_w(self) -> int | None:
        value = self.translated_gpu_policy.get("power_limit_w")
        return int(value) if value is not None else None

    def apply_plan(self, plan: list[dict]) -> None:
        apply_plan(self.reader, plan)
        self.reader.refresh_points()

    def start_clock_ceiling(self, flatten_target: dict) -> None:
        self.clock_ceiling = ProbeClockCeilingController(
            flatten_target=flatten_target,
            policy_controller=self.policy_controller,
        )
        self.clock_ceiling.apply()

    def close(self) -> None:
        if self.clock_ceiling is not None:
            self.clock_ceiling.close()
        self.live_voltage_reader.close()


def open_live_gpu_vf_curve_applier(
    *,
    gpu_index: int,
    runtime_options: dict,
    log: Callable[[str], None],
) -> LiveGpuVfCurveApplier:
    reader = create_hidden_vf_curve_reader(gpu_index=int(gpu_index))
    if reader is None:
        last_error = get_hidden_vf_curve_reader_last_error()
        detail = f": {last_error}" if last_error is not None else ""
        raise AutoUvError(
            "failed to create Linux NVAPI VF helper"
            f"{detail}. This driver/GPU combination may not expose editable voltage-based V/F points."
        )

    policy_controller = NvmlGpuPolicyController(gpu_index=int(gpu_index))
    live_voltage_reader = LiveNvmlVoltageReader(gpu_index=int(gpu_index))
    runtime_reset = reset_nvidia_runtime_defaults(
        gpu_index=int(gpu_index),
        power_limit_override_w=runtime_options.get("power_limit_override_w"),
        log=log,
    )
    runtime_default_plan = list(runtime_reset["plan"])
    apply_plan(reader, runtime_default_plan)
    assert_zero_runtime_vf_offsets(reader)

    translated_gpu_policy = {
        "gpu_name": runtime_reset.get("gpu_name"),
        "power_limit_w": runtime_reset.get("power_limit_w"),
    }
    memory_offset_mhz, memory_offset_limit_mhz = auto_uv_memory_offset_mhz(
        runtime_options,
        policy_controller=policy_controller,
    )
    if memory_offset_mhz is not None:
        translated_gpu_policy["mem_clk_vf_offset_mhz"] = int(memory_offset_mhz)
        translated_gpu_policy["mem_clk_vf_offset_limit_mhz"] = int(
            memory_offset_limit_mhz
        )
        if int(memory_offset_mhz) != 0:
            policy_controller.apply_clock_offsets(
                mem_clk_vf_offset_mhz=int(memory_offset_mhz)
            )

    return LiveGpuVfCurveApplier(
        gpu_index=int(gpu_index),
        reader=reader,
        policy_controller=policy_controller,
        live_voltage_reader=live_voltage_reader,
        runtime_default_plan=runtime_default_plan,
        translated_gpu_policy=translated_gpu_policy,
    )
