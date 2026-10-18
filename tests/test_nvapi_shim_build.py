"""The NVAPI shim is built stripped.

The shim is statically linked against libstdc++ so it needs no MinGW runtime in
the Wine prefix. Without stripping, that static runtime drags megabytes of
DWARF into a DLL that is copied into every fronted Proton prefix.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SETUP = _ROOT / "setup.py"
_BUILD_SH = _ROOT / "overlay" / "native" / "nvapi_shim" / "build.sh"
_SHIM_DIR = _ROOT / "overlay" / "native" / "nvapi_shim"


def _setup_shim_flags() -> tuple[str, ...]:
    # Read the constant without executing setup.py (which calls setup()).
    tree = ast.parse(_SETUP.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "NVAPI_SHIM_CXXFLAGS"
            for target in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("setup.py defines no NVAPI_SHIM_CXXFLAGS")


def _shim_sources(shim_dir: Path) -> list[Path]:
    # Mirror setup.py: every shim translation unit, plus MinHook when vendored.
    minhook = shim_dir / "third_party" / "minhook" / "src"
    vendored = [minhook / n for n in ("buffer.c", "hook.c", "trampoline.c", "hde/hde64.c")]
    return [
        *sorted((shim_dir / "src").glob("*.cpp")),
        *(path for path in vendored if path.exists()),
        shim_dir / "nvapi64.def",
    ]


def test_setup_and_build_script_strip_the_static_shim() -> None:
    flags = _setup_shim_flags()
    assert "-s" in flags
    assert "-Wl,--gc-sections" in flags
    assert "-static-libstdc++" in flags
    script = _BUILD_SH.read_text(encoding="utf-8")
    assert "-s -Wl,--gc-sections" in script
    assert "-ffunction-sections -fdata-sections" in script


def test_shim_built_with_setup_flags_has_no_debug_sections(tmp_path: Path) -> None:
    compiler = shutil.which("x86_64-w64-mingw32-g++")
    if compiler is None:
        pytest.skip("MinGW cross compiler unavailable")
    output = tmp_path / "nvapi64.dll"
    subprocess.run(
        [
            compiler,
            *_setup_shim_flags(),
            "-o",
            str(output),
            *(str(path) for path in _shim_sources(_SHIM_DIR)),
            "-lkernel32",
        ],
        check=True,
        capture_output=True,
    )
    data = output.read_bytes()
    assert data.startswith(b"MZ")
    assert b".debug_info" not in data
    assert b"nvapi_QueryInterface" in data
