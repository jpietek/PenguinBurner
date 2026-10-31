"""Shared native overlay probe compilation; binaries still run in isolated processes."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

TEXT_SOURCES = (
    "overlay_text.cpp", "latency_state.cpp", "frame_timing.cpp",
    "overlay_render.cpp", "overlay_worker.cpp",
)
LAYER_SOURCES = (*TEXT_SOURCES, "display_timing.cpp", "latency_layer.cpp",
                 "telemetry_events.cpp", "marker_timing.cpp")


class OverlayProbeBuilder:
    def __init__(self, root: Path, objects: Path):
        self.source_dir = root / "overlay/native/latency_layer/src"
        self.objects = objects
        self.compiler = shutil.which("c++") or shutil.which("g++")
        if self.compiler is None:
            pytest.skip("C++ compiler unavailable")
        self.flags = [self.compiler, "-std=c++17", "-O2", "-pthread", "-I", str(self.source_dir)]
        headers = subprocess.run(
            [*self.flags, "-fsyntax-only", "-x", "c++", "-"],
            input="#include <vulkan/vk_layer.h>\n", text=True, capture_output=True,
        )
        if headers.returncode:
            pytest.skip("Vulkan headers unavailable (install the vulkan-headers package)")

    def __call__(self, source: Path, output: Path, *, full_layer=False, embedded=()) -> Path:
        objects = []
        for name in LAYER_SOURCES if full_layer else TEXT_SOURCES:
            if name in embedded:
                continue
            obj = self.objects / (name + ".o")
            if not obj.exists():
                subprocess.run([*self.flags, "-c", str(self.source_dir / name), "-o", str(obj)], check=True)
            objects.append(str(obj))
        subprocess.run([*self.flags, str(source), *objects, "-o", str(output)], check=True)
        return output
