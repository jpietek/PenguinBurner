from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from integrations.afterburner.import_fan_curve import write_config
from cli.runtime_config_file import load_raw_runtime_config
from common.penguin_burner_paths import default_runtime_config_path
from drivers.nvidia.daemon_gpu import DaemonGpuClient


@dataclass(frozen=True)
class GpuChoice:
    index: int
    name: str
    pci_bus_id: str = ""
    uuid: str = ""
    pci_device_id: str = ""
    power_limit_default_w: float | None = None

    @property
    def label(self) -> str:
        name = self.name.strip() or "NVIDIA GPU"
        bus = _short_bus_id(self.pci_bus_id)
        if bus:
            return f"GPU {self.index} - {name} ({bus})"
        return f"GPU {self.index} - {name}"


def detected_gpu_choices() -> list[GpuChoice]:
    try:
        capabilities = DaemonGpuClient.discover_capabilities()
    except Exception:
        return []
    defaults = {item.identity.index: item.power.default_w for item in capabilities}
    return [
        replace(choice, power_limit_default_w=defaults.get(choice.index))
        for choice in gpu_choices_from_nvml_identities(
            item.identity for item in capabilities
        )
    ]


def gpu_choices_from_nvml_identities(identities) -> list[GpuChoice]:
    seen: set[int] = set()
    choices: list[GpuChoice] = []
    for identity in identities:
        try:
            index = max(0, int(getattr(identity, "index")))
        except (TypeError, ValueError):
            continue
        if index in seen:
            continue
        seen.add(index)
        choices.append(
            GpuChoice(
                index=index,
                name=str(getattr(identity, "name", "") or ""),
                pci_bus_id=str(getattr(identity, "pci_bus_id", "") or ""),
                pci_device_id=str(getattr(identity, "pci_device_id", "") or ""),
                uuid=str(getattr(identity, "uuid", "") or ""),
            )
        )
    return choices


def runtime_gpu_index(config_path: str | Path | None = None) -> int:
    path = default_runtime_config_path() if config_path is None else Path(config_path)
    try:
        config = load_raw_runtime_config(path)
        return max(0, int(config.get("gpu", {}).get("index", 0)))
    except Exception:
        return 0


def gpu_choices_with_fallback(
    *,
    selected_index: int | None = None,
    config_path: str | Path | None = None,
    choices: list[GpuChoice] | None = None,
) -> tuple[list[GpuChoice], int]:
    """Resolve the saved selection against GPUs reported by the daemon."""
    selected = (
        runtime_gpu_index(config_path)
        if selected_index is None
        else max(0, int(selected_index))
    )
    detected = list(detected_gpu_choices() if choices is None else choices)
    if detected and selected not in {choice.index for choice in detected}:
        # A saved index is a preference, not evidence that the GPU exists.
        # Keep this correction local; opening a selector must not rewrite it.
        selected = detected[0].index
    return detected, selected


def persist_runtime_gpu_index(
    gpu_index: int,
    *,
    config_path: str | Path | None = None,
) -> int:
    selected = max(0, int(gpu_index))
    path = default_runtime_config_path() if config_path is None else Path(config_path)
    try:
        config = load_raw_runtime_config(path)
    except Exception:
        # An unreadable config must not become a destructive full rewrite:
        # continuing with {} would re-emit the file with only [gpu], silently
        # dropping every other section ([ui] persist-on-startup, [fan], ...).
        # Keep the file as-is; the selected index still applies this session.
        return selected
    updated = dict(config)
    gpu = dict(updated.get("gpu", {}))
    gpu["index"] = selected
    updated["gpu"] = gpu
    write_config(path, updated)
    return selected


def _short_bus_id(bus_id: str) -> str:
    text = str(bus_id or "").strip()
    if not text:
        return ""
    if ":" in text:
        return text.split(":", 1)[1]
    return text
