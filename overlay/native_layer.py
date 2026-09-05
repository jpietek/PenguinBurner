from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path

LATENCY_LAYER_NAME = "VK_LAYER_PENGUINBURNER_latency"
NATIVE_LAYER_DIR_ENV = "PENGUIN_BURNER_NATIVE_LAYER_DIR"
NATIVE_LAYER_LIBRARY = "libVkLayer_penguinburner_latency.so"
NATIVE_LAYER_MANIFEST = "VkLayer_PENGUINBURNER_latency.json"
# The 32-bit companion, shipped beside the 64-bit files in the same directory.
# A 32-bit Windows game (Gorogoa, older GOG titles) renders through wine's
# i386 winevulkan, so its Vulkan instance is 32-bit and the loader can only
# inject a 32-bit layer into it. Same layer name, same enable env; the loader
# picks the manifest whose library_arch matches the application. The 64-bit
# names above stay the canonical "is the layer present" markers -- the 32-bit
# build is best effort and simply absent when no i686 toolchain is available.
NATIVE_LAYER_LIBRARY_I386 = "libVkLayer_penguinburner_latency_i386.so"
NATIVE_LAYER_MANIFEST_I386 = "VkLayer_PENGUINBURNER_latency.i386.json"

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OVERLAY_ROOT = Path(__file__).resolve().parent
_SOURCE_PROJECT_FILE = _REPO_ROOT / "pyproject.toml"
_SOURCE_NATIVE_LAYER_DIR = _OVERLAY_ROOT / "native" / "latency_layer"
_SOURCE_BUILD_LAYER_DIR = _SOURCE_NATIVE_LAYER_DIR / "build"


def native_layer_dirs(
    env: Mapping[str, str] | None = None,
    *,
    source_build_dir: Path | None = None,
) -> list[Path]:
    env = os.environ if env is None else env
    candidates: list[Path] = []
    explicit = str(env.get(NATIVE_LAYER_DIR_ENV) or "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path(__file__).resolve().parent / "native_layer")
    if source_build_dir is not None:
        candidates.append(source_build_dir)
    elif _source_checkout_present():
        candidates.append(_SOURCE_BUILD_LAYER_DIR)

    dirs: list[Path] = []
    for path in candidates:
        if _native_layer_dir_valid(path) and path not in dirs:
            dirs.append(path)
    return dirs


def _native_layer_dir_valid(path: Path) -> bool:
    return (path / NATIVE_LAYER_MANIFEST).is_file() and (
        path / NATIVE_LAYER_LIBRARY
    ).is_file()


def _source_checkout_present() -> bool:
    return _SOURCE_PROJECT_FILE.is_file() and (
        _SOURCE_NATIVE_LAYER_DIR / "CMakeLists.txt"
    ).is_file()
