from __future__ import annotations

from drivers.nvidia.daemon_gpu import DaemonGpuClient


def _nvidia_pci_bus_id_to_dri_prime(bus_id: str) -> str:
    cleaned = str(bus_id or "").strip()
    if not cleaned:
        return ""
    try:
        domain_text, rest = cleaned.split(":", 1)
        bus_text, slot_func = rest.split(":", 1)
        slot_text, function_text = slot_func.split(".", 1)
    except ValueError:
        return ""
    try:
        domain = int(domain_text, 16) & 0xFFFF
        bus = int(bus_text, 16)
        slot = int(slot_text, 16)
        function = int(function_text, 16)
    except ValueError:
        return ""
    return f"pci-{domain:04x}_{bus:02x}_{slot:02x}_{function:x}"


def _nvidia_pci_device_id_selectors(pci_device_id: str) -> tuple[str, str]:
    cleaned = str(pci_device_id or "").strip().lower()
    cleaned = cleaned.removeprefix("0x")
    cleaned = "".join(ch for ch in cleaned if ch in "0123456789abcdef")
    if len(cleaned) < 8:
        return "", ""
    device_id = cleaned[:4]
    vendor_id = cleaned[-4:]
    return f"0x{vendor_id}:0x{device_id}", f"{vendor_id}:{device_id}"


def _query_selected_nvidia_gpu(gpu_index: int) -> dict[str, str]:
    try:
        identity = DaemonGpuClient(int(gpu_index)).capabilities().identity
    except Exception:  # noqa: BLE001
        return {}
    dri_prime = _nvidia_pci_bus_id_to_dri_prime(identity.pci_bus_id)
    vk_loader_select, mesa_vk_select = _nvidia_pci_device_id_selectors(
        identity.pci_device_id
    )
    return {
        "index": str(int(identity.index)),
        "name": identity.name,
        "pci_bus_id": identity.pci_bus_id,
        "pci_device_id": identity.pci_device_id,
        "uuid": identity.uuid,
        "dri_prime": dri_prime,
        "vk_loader_device_select": vk_loader_select,
        "mesa_vk_device_select": mesa_vk_select,
    }


def _apply_nvidia_render_offload_env(
    env: dict[str, str],
    *,
    selected_gpu: dict[str, str] | None = None,
) -> dict[str, str]:
    updated = dict(env)
    updated["__NV_PRIME_RENDER_OFFLOAD"] = "1"
    updated["__VK_LAYER_NV_optimus"] = "NVIDIA_only"
    updated["__GLX_VENDOR_LIBRARY_NAME"] = "nvidia"
    selected_gpu = selected_gpu or {}
    dri_prime = str(selected_gpu.get("dri_prime", "")).strip()
    if dri_prime:
        updated["DRI_PRIME"] = f"{dri_prime}!"
    vk_loader_select = str(selected_gpu.get("vk_loader_device_select", "")).strip()
    if vk_loader_select:
        updated["VK_LOADER_DEVICE_SELECT"] = vk_loader_select
    mesa_vk_select = str(selected_gpu.get("mesa_vk_device_select", "")).strip()
    if mesa_vk_select:
        updated["MESA_VK_DEVICE_SELECT"] = f"{mesa_vk_select}!"
        updated["MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE"] = "1"
    # The NVIDIA ICD ignores the selectors above on multi-GPU boxes, so the
    # Vulkan loader still enumerates all cards and the benchmark's "first
    # capable device" pick is arbitrary. Pass the target UUID so a patched
    # Q2RTX (Q2RTX-headless: pin-by-UUID) can select the correct device.
    # NVML reports "GPU-<hex with dashes>"; the Vulkan device UUID is the same
    # value without the prefix/dashes, so normalize to plain lowercase hex.
    target_uuid = str(selected_gpu.get("uuid", "")).strip()
    if target_uuid:
        if target_uuid.upper().startswith("GPU-"):
            target_uuid = target_uuid[4:]
        target_uuid = target_uuid.replace("-", "").lower()
    if target_uuid:
        updated["Q2RTX_TARGET_UUID"] = target_uuid
    return updated


def _selected_gpu_log_lines(
    *,
    selected_gpu: dict[str, str],
    child_env: dict[str, str],
) -> list[str]:
    lines = []
    if selected_gpu:
        lines.append(
            "# selected_nvidia_gpu="
            f"index={selected_gpu.get('index', '')} "
            f"name={selected_gpu.get('name', '')} "
            f"pci_bus_id={selected_gpu.get('pci_bus_id', '')} "
            f"pci_device_id={selected_gpu.get('pci_device_id', '')} "
            f"uuid={selected_gpu.get('uuid', '')}"
        )
    keys = [
        "__NV_PRIME_RENDER_OFFLOAD",
        "__VK_LAYER_NV_optimus",
        "__GLX_VENDOR_LIBRARY_NAME",
        "DRI_PRIME",
        "VK_LOADER_DEVICE_SELECT",
        "MESA_VK_DEVICE_SELECT",
        "MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE",
    ]
    values = [
        f"{key}={child_env[key]}" for key in keys if str(child_env.get(key, "")).strip()
    ]
    if values:
        lines.append("# q2rtx_gpu_binding_env=" + " ".join(values))
    return lines
