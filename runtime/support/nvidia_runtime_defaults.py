"""Build Auto-UV's baseline plan after one semantic daemon stock reset."""

from __future__ import annotations

from typing import Callable

from runtime.daemon_client import gpu_reset_defaults


class NvidiaRuntimeDefaultsError(RuntimeError):
    pass


def build_runtime_default_plan(points: list[dict]) -> list[dict]:
    return [
        {
            "index": int(point["index"]),
            "voltage_mv": int(point["voltage_uv"]) // 1000,
            "base_mhz": int(point["base_freq_khz"]) // 1000,
            "target_mhz": int(point["base_freq_khz"]) // 1000,
            "current_offset_mhz": int(point["current_offset_khz"]) // 1000,
            "new_offset_mhz": 0,
            "preserve_base": False,
        }
        for point in points
        if int(point.get("type", -1)) == 0
        and int(point.get("voltage_based", 0)) == 1
    ]


def reset_nvidia_runtime_defaults(
    *,
    gpu_index: int,
    log: Callable[[str], None] = print,
) -> dict:
    try:
        result = gpu_reset_defaults(int(gpu_index))
        raw_points = result.get("points")
        if not isinstance(raw_points, list):
            raise NvidiaRuntimeDefaultsError(
                "daemon stock reset returned no V/F snapshot"
            )
        points = [point for point in raw_points if isinstance(point, dict)]
        plan = build_runtime_default_plan(points)
        if not plan:
            raise NvidiaRuntimeDefaultsError(
                "daemon stock reset returned no editable V/F points"
            )
    except NvidiaRuntimeDefaultsError:
        raise
    except Exception as exc:
        raise NvidiaRuntimeDefaultsError(
            f"failed to reset GPU defaults before Auto-UV: {exc}"
        ) from exc

    gpu_name = str(result.get("gpu_name") or "").strip() or None
    pci_device_id = str(result.get("pci_device_id") or "").strip() or None
    power_limits = result.get("power_limits")
    power_limits = power_limits if isinstance(power_limits, dict) else {}
    default_power_limit_w = power_limits.get("power_limit_default_w")
    power_limit_w = (
        int(default_power_limit_w) if default_power_limit_w is not None else None
    )
    log(
        "Reset defaults through penguin-burnerd: "
        f"gpu={gpu_name or int(gpu_index)} points={len(plan)} "
        f"power_limit={f'{power_limit_w}W' if power_limit_w is not None else 'n/a'}"
    )
    return {
        "plan": plan,
        "gpu_name": gpu_name,
        "pci_device_id": pci_device_id,
        "power_limits": power_limits,
        "power_limit_w": power_limit_w,
        "power_limit_set_supported": result.get("power_limit_set_supported"),
        "source": "runtime-defaults",
    }
