"""The 32-bit companion Vulkan layer.

A 32-bit Windows game renders through wine's i386 winevulkan, so its Vulkan
instance is 32-bit and the loader can only inject a 32-bit layer into it. The
overlay ships a second layer library and manifest beside the 64-bit pair, in
the same directory, so the loader picks whichever matches the application.
"""

from __future__ import annotations

import json
from pathlib import Path

from overlay.native_layer import (
    LATENCY_LAYER_NAME,
    NATIVE_LAYER_LIBRARY,
    NATIVE_LAYER_LIBRARY_I386,
    NATIVE_LAYER_MANIFEST,
    NATIVE_LAYER_MANIFEST_I386,
)

_ROOT = Path(__file__).resolve().parent.parent
_CMAKE = _ROOT / "overlay" / "native" / "latency_layer" / "CMakeLists.txt"
_MANIFEST_TEMPLATE = (
    _ROOT / "overlay" / "native" / "latency_layer" / "VkLayer_PENGUINBURNER_latency.json.in"
)
_SETUP = _ROOT / "setup.py"


def test_the_two_variants_are_distinct_files_in_one_directory() -> None:
    # Distinct names because both live in the single directory the loader
    # scans; the 64-bit names stay the canonical present-markers.
    assert NATIVE_LAYER_LIBRARY_I386 != NATIVE_LAYER_LIBRARY
    assert NATIVE_LAYER_MANIFEST_I386 != NATIVE_LAYER_MANIFEST
    assert "i386" in NATIVE_LAYER_LIBRARY_I386
    assert NATIVE_LAYER_LIBRARY_I386.endswith(".so")
    assert NATIVE_LAYER_MANIFEST_I386.endswith(".json")


def test_the_cmake_parameterises_the_library_name_for_the_32bit_build() -> None:
    cmake = _CMAKE.read_text(encoding="utf-8")
    # One knob drives both the output library name and the manifest's
    # library_path, so the 32-bit build's manifest points at its own .so.
    assert "PB_LAYER_NAME_SUFFIX" in cmake
    assert 'OUTPUT_NAME "VkLayer_penguinburner_latency${PB_LAYER_NAME_SUFFIX}"' in cmake
    assert (
        './libVkLayer_penguinburner_latency${PB_LAYER_NAME_SUFFIX}.so' in cmake
    )
    # The arch field is derived from the pointer size, so -m32 stamps "32".
    assert 'set(PB_LAYER_LIBRARY_ARCH "32")' in cmake


def test_the_manifest_template_carries_the_arch_field() -> None:
    template = _MANIFEST_TEMPLATE.read_text(encoding="utf-8")
    assert '"library_arch": "@PB_LAYER_LIBRARY_ARCH@"' in template


def test_setup_builds_the_32bit_companion_best_effort() -> None:
    setup = _SETUP.read_text(encoding="utf-8")
    # Built with -m32 and the name suffix, gated exactly like the NVAPI shim:
    # optional by default, hard-failing only under the REQUIRE flag.
    assert "-DPB_LAYER_NAME_SUFFIX=_i386" in setup
    assert "-DCMAKE_CXX_FLAGS=-m32" in setup
    assert "PENGUIN_BURNER_BUILD_NATIVE_LAYER32" in setup
    assert "PENGUIN_BURNER_REQUIRE_NATIVE_LAYER32" in setup


def _installed_layer_dir() -> Path | None:
    """The built native_layer dir in this checkout, if a build has run."""
    for candidate in (
        _ROOT / "overlay" / "native_layer",
        *sorted((_ROOT / "build").glob("lib*/overlay/native_layer")),
    ):
        if (candidate / NATIVE_LAYER_MANIFEST_I386).is_file():
            return candidate
    return None


def test_a_built_32bit_manifest_agrees_with_the_64bit_one() -> None:
    """Runs only where the 32-bit artifact was actually built (a release build
    or a local build_py); a checkout with no build simply has nothing to check.
    """
    layer_dir = _installed_layer_dir()
    if layer_dir is None:
        return

    manifest64 = json.loads(
        (layer_dir / NATIVE_LAYER_MANIFEST).read_text(encoding="utf-8")
    )["layer"]
    manifest32 = json.loads(
        (layer_dir / NATIVE_LAYER_MANIFEST_I386).read_text(encoding="utf-8")
    )["layer"]

    # Same layer name and enable env: one VK_LOADER_LAYERS_ENABLE / PENGUIN_BURNER
    # turns on whichever arch the game needs.
    assert manifest64["name"] == manifest32["name"] == LATENCY_LAYER_NAME
    assert manifest64["enable_environment"] == manifest32["enable_environment"]
    # Different arch, each pointing at its own library.
    assert manifest64["library_arch"] == "64"
    assert manifest32["library_arch"] == "32"
    assert manifest32["library_path"].endswith(NATIVE_LAYER_LIBRARY_I386)
    assert (layer_dir / NATIVE_LAYER_LIBRARY_I386).is_file()
    for name, elf_class in ((NATIVE_LAYER_LIBRARY, 2), (NATIVE_LAYER_LIBRARY_I386, 1)):
        with (layer_dir / name).open("rb") as library:
            assert library.read(5) == b"\x7fELF" + bytes([elf_class])
