#!/usr/bin/env python3

from __future__ import annotations

import shutil
import subprocess
from typing import Callable

from hidden_nvapi_vf import create_hidden_vf_curve_reader
from afterburner.import_vf_curve import apply_plan
from nvml_gpu_policy import NvmlGpuPolicyController
from subprocess_locale import stable_subprocess_env


class NvidiaRuntimeDefaultsError(RuntimeError):
    pass


def build_runtime_default_plan(reader) -> list[dict]:
    plan = []
    for point in reader.editable_core_points():
        voltage_mv = int(point["voltage_uv"] // 1000)
        base_mhz = int(point["base_freq_khz"] // 1000)
        current_offset_mhz = int(point["current_offset_khz"] // 1000)
        plan.append(
            {
                "index": int(point["index"]),
                "voltage_mv": voltage_mv,
                "base_mhz": base_mhz,
                "target_mhz": base_mhz,
                "current_offset_mhz": current_offset_mhz,
                "new_offset_mhz": 0,
                "preserve_base": False,
            }
        )
    return plan


def _run_nvidia_smi_reset(args: list[str]) -> tuple[bool, str]:
    binary = shutil.which("nvidia-smi")
    if not binary:
        return False, "nvidia-smi not found"
    try:
        completed = subprocess.run(
            [binary, *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=stable_subprocess_env(),
        )
    except Exception as exc:
        return False, str(exc)
    if completed.returncode == 0:
        return True, ""
    stderr = (completed.stderr or completed.stdout or "").strip()
    return False, stderr or f"exit={completed.returncode}"


def _format_clock_offsets(offsets: dict) -> str:
    def _value(key: str) -> str:
        value = offsets.get(key)
        return "n/a" if value is None else f"{int(value):+d}MHz"

    return (
        f"gpc-vf-offset={_value('gpc_clk_vf_offset_mhz')} "
        f"mem-vf-offset={_value('mem_clk_vf_offset_mhz')}"
    )


def _assert_zero_readable_clock_offsets(offsets: dict) -> None:
    stale = []
    for key, label in (
        ("gpc_clk_vf_offset_mhz", "GPU GPC VF offset"),
        ("mem_clk_vf_offset_mhz", "memory VF offset"),
    ):
        value = offsets.get(key)
        if value is not None and int(value) != 0:
            stale.append(f"{label}={int(value):+d}MHz")
    if stale:
        raise NvidiaRuntimeDefaultsError(
            "failed to clear global clock offsets before Auto-UV: " + ", ".join(stale)
        )


def reset_nvidia_runtime_defaults(
    *,
    gpu_index: int,
    power_limit_override_w: int | None = None,
    log: Callable[[str], None] = print,
) -> dict:
    reader = create_hidden_vf_curve_reader(gpu_index=gpu_index)
    if reader is None:
        raise NvidiaRuntimeDefaultsError("failed to create Linux NVAPI VF helper")

    policy_controller = None
    try:
        try:
            policy_controller = NvmlGpuPolicyController(gpu_index=gpu_index)
        except Exception as exc:
            raise NvidiaRuntimeDefaultsError(
                f"GPU policy helper unavailable: {exc}"
            ) from exc

        power_limits = policy_controller.query_power_limits()
        default_power_limit_w = power_limits.get("power_limit_default_w")
        try:
            gpu_name = policy_controller.query_gpu_name()
            if gpu_name:
                log(f"Reset defaults: GPU name {gpu_name}")
        except Exception as exc:
            gpu_name = None
            log(f"Reset defaults: GPU name read skipped: {exc}")

        try:
            before_offsets = policy_controller.get_clock_offsets()
            log(
                f"Reset defaults: clock offsets before reset {_format_clock_offsets(before_offsets)}"
            )
        except Exception as exc:
            before_offsets = {}
            log(f"Reset defaults: clock offset read before reset skipped: {exc}")

        try:
            policy_controller.reset_locked_core_clocks()
            log("Reset defaults: cleared locked GPU clocks")
        except Exception as exc:
            log(f"Reset defaults: locked GPU clock reset skipped: {exc}")

        ok, reason = _run_nvidia_smi_reset(["-i", str(int(gpu_index)), "-rgc"])
        if ok:
            log("Reset defaults: cleared locked GPU clocks with nvidia-smi")
        else:
            log(f"Reset defaults: nvidia-smi locked GPU clock reset skipped: {reason}")

        ok, reason = _run_nvidia_smi_reset(["-i", str(int(gpu_index)), "-rmc"])
        if ok:
            log("Reset defaults: cleared locked memory clocks")
        else:
            log(f"Reset defaults: locked memory clock reset skipped: {reason}")

        try:
            applied_offsets = policy_controller.apply_clock_offsets(
                gpc_clk_vf_offset_mhz=0,
                mem_clk_vf_offset_mhz=0,
            )
            cleared = []
            if applied_offsets.get("gpc_clk_vf_offset_mhz") is not None:
                cleared.append("GPU GPC VF offset")
            if applied_offsets.get("mem_clk_vf_offset_mhz") is not None:
                cleared.append("memory VF offset")
            if cleared:
                log(f"Reset defaults: cleared {' and '.join(cleared)}")
            else:
                log("Reset defaults: no global VF offsets were applied")
        except Exception as exc:
            raise NvidiaRuntimeDefaultsError(
                f"failed to clear global core/memory VF offsets before Auto-UV: {exc}"
            ) from exc

        try:
            after_offsets = policy_controller.get_clock_offsets()
            log(
                f"Reset defaults: clock offsets after reset {_format_clock_offsets(after_offsets)}"
            )
            _assert_zero_readable_clock_offsets(after_offsets)
        except NvidiaRuntimeDefaultsError:
            raise
        except Exception as exc:
            log(f"Reset defaults: clock offset read after reset skipped: {exc}")

        # The VF reader snapshots point status/control at construction time.
        # Refresh after clearing global offsets so the zero-offset plan is built
        # from the live post-reset curve, not stale pre-reset data.
        reader.refresh_points()
        plan = build_runtime_default_plan(reader)
        apply_plan(reader, plan)
        reader.refresh_points()
        changed_points = sum(
            1 for item in plan if int(item.get("current_offset_mhz", 0)) != 0
        )
        log(
            "Reset defaults: cleared GPU VF offsets "
            f"matched={len(plan)} changed={changed_points}"
        )

        target_power_limit_w = (
            int(power_limit_override_w)
            if power_limit_override_w is not None and int(power_limit_override_w) > 0
            else (
                int(default_power_limit_w)
                if default_power_limit_w is not None
                else None
            )
        )
        if target_power_limit_w is not None:
            try:
                policy_controller.apply_power_limit_w(target_power_limit_w)
                if default_power_limit_w is not None and int(
                    target_power_limit_w
                ) == int(default_power_limit_w):
                    log(
                        "Reset defaults: restored power limit "
                        f"to default {int(target_power_limit_w)}W"
                    )
                else:
                    log(
                        "Reset defaults: applied power limit "
                        f"{int(target_power_limit_w)}W"
                    )
            except Exception as exc:
                log(f"Reset defaults: power-limit reset skipped: {exc}")

        return {
            "plan": plan,
            "gpu_name": gpu_name,
            "power_limits": power_limits,
            "power_limit_w": target_power_limit_w,
            "source": "runtime-defaults",
        }
    finally:
        reader.close()
        if policy_controller is not None:
            policy_controller.close()
