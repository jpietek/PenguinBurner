"""Exercise NVAPI probe/unload lifetime with real Windows loader semantics."""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _shipped_shim_flags() -> list[str]:
    # The probe must load the shim exactly as packaged (static, stripped).
    setup = Path(__file__).resolve().parents[1] / "setup.py"
    for node in ast.parse(setup.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "NVAPI_SHIM_CXXFLAGS"
            for target in node.targets
        ):
            return list(ast.literal_eval(node.value))
    raise AssertionError("setup.py defines no NVAPI_SHIM_CXXFLAGS")


def _shim_sources(shim_dir: Path) -> list[Path]:
    # Mirror setup.py: every shim translation unit, plus MinHook when vendored.
    minhook = shim_dir / "third_party" / "minhook" / "src"
    vendored = [
        minhook / n for n in ("buffer.c", "hook.c", "trampoline.c", "hde/hde64.c")
    ]
    return [
        *sorted((shim_dir / "src").glob("*.cpp")),
        *(path for path in vendored if path.exists()),
        shim_dir / "nvapi64.def",
    ]


@pytest.fixture(scope="module")
def shim_probe(tmp_path_factory: pytest.TempPathFactory):
    compiler = shutil.which("x86_64-w64-mingw32-g++")
    wine = shutil.which("wine")
    wineserver = shutil.which("wineserver")
    if not compiler or not wine or not wineserver:
        pytest.skip("NVAPI lifetime probe requires MinGW and Wine")
    root = tmp_path_factory.mktemp("nvapi-lifetime")
    source = Path(__file__).resolve().parents[1] / "overlay/native/nvapi_shim"
    for name, text in _SOURCES.items():
        (root / name).write_text(text, encoding="utf-8")
    native = Path(__file__).resolve().parent / "native"
    probe_flags = [
        "-O2",
        "-std=c++17",
        "-static",
        "-static-libgcc",
        "-static-libstdc++",
    ]
    for output, sources, flags in (
        ("nvapi64.dll", _shim_sources(source), [*_shipped_shim_flags(), "-lkernel32"]),
        ("real.dll", [root / "real.cpp"], [*probe_flags, "-shared"]),
        ("probe.dll", [root / "probe.cpp"], [*probe_flags, "-shared"]),
        ("probe.exe", [root / "main.cpp"], probe_flags),
        ("_nvngx.dll", [native / "nr_ngx_stub.cpp"], [*probe_flags, "-shared"]),
        (
            "nvngx.dll_dlssnr.dll",
            [native / "nr_ngx_stub.cpp", native / "nr_forwarder_stub.cpp"],
            [*probe_flags, "-shared"],
        ),
        ("nr-probe.exe", [native / "nr_gpu_probe.cpp"], probe_flags),
    ):
        subprocess.run(
            [
                compiler,
                *flags,
                *(str(path) for path in sources),
                "-o",
                str(root / output),
                *(
                    ["-ld3d12", "-ldxgi", "-ldxguid"]
                    if output == "nr-probe.exe"
                    else []
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    env = dict(os.environ)
    env.update(
        WINEPREFIX=str(root / "prefix"),
        WINEDEBUG="-all",
        WINEDLLOVERRIDES="nvapi64=n;mscoree,mshtml=",
        DISPLAY="",
        WAYLAND_DISPLAY="",
        PENGUIN_BURNER_SHIM_REAL="Z:" + str(root / "real.dll"),
        PENGUIN_BURNER_SHIM_OUTPUT="Z:" + str(root / "markers.log"),
    )
    try:
        yield wine, root, env
    finally:
        subprocess.run(
            [wineserver, "-k"], env=env, check=False, timeout=10, capture_output=True
        )


@pytest.mark.parametrize("loader_lock", [False, True])
def test_nvapi_probe_can_release_its_handle_without_unmapping_writer(
    shim_probe,
    loader_lock: bool,
) -> None:
    wine, root, env = shim_probe
    result = subprocess.run(
        [wine, str(root / "probe.exe"), str(int(loader_lock))],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "probe released; forwarding and writer survived" in result.stdout
    assert "real=ok" in (root / "markers.log").read_text(encoding="utf-8")


_SOURCES = {
    "real.cpp": r"""
#include <windows.h>
extern "C" int __cdecl ping() { return 123; }
extern "C" __declspec(dllexport) void* __cdecl nvapi_QueryInterface(unsigned id) {
    return id == 1 ? reinterpret_cast<void*>(&ping) : nullptr;
}
""",
    "probe.cpp": r"""
#include <windows.h>
using Query = void* (__cdecl*)(unsigned);
using Ping = int (__cdecl*)();
BOOL WINAPI DllMain(HINSTANCE, DWORD reason, LPVOID) {
    if (reason != DLL_PROCESS_ATTACH) return TRUE;
    HMODULE shim = LoadLibraryA("nvapi64.dll");
    if (!shim) return FALSE;
    auto query = reinterpret_cast<Query>(GetProcAddress(shim, "nvapi_QueryInterface"));
    auto ping = query ? reinterpret_cast<Ping>(query(1)) : nullptr;
    bool ok = ping && ping() == 123;
    // The writer cannot start until this DLL_PROCESS_ATTACH releases the
    // loader lock. This is OptiScaler's GPU detection sequence.
    FreeLibrary(shim);
    return ok;
}
""",
    "main.cpp": r"""
#include <windows.h>
#include <cstdio>
using Query = void* (__cdecl*)(unsigned);
using Ping = int (__cdecl*)();
int main(int argc, char** argv) {
    bool loader_lock = argc > 1 && argv[1][0] == '1';
    HMODULE handle = LoadLibraryA(loader_lock ? "probe.dll" : "nvapi64.dll");
    if (!handle) return 10;
    if (!loader_lock) {
        auto query = reinterpret_cast<Query>(GetProcAddress(handle, "nvapi_QueryInterface"));
        auto ping = query ? reinterpret_cast<Ping>(query(1)) : nullptr;
        if (!ping || ping() != 123) return 11;
    }
    FreeLibrary(handle);
    HMODULE shim = GetModuleHandleA("nvapi64.dll");
    if (!shim) { std::puts("writer DLL was unloaded"); return 12; }
    Sleep(250);
    auto query = reinterpret_cast<Query>(GetProcAddress(shim, "nvapi_QueryInterface"));
    auto ping = query ? reinterpret_cast<Ping>(query(1)) : nullptr;
    if (!ping || ping() != 123) return 13;
    std::puts("probe released; forwarding and writer survived");
    return 0;
}
""",
}


@pytest.mark.parametrize(
    "backend",
    [
        "ngx",
        "forwarder",
        "unknown-forwarder",
        "late-forwarder",
        "nested-forwarder",
        "refused-forwarder",
    ],
)
@pytest.mark.parametrize(
    "enabled,feature,failed",
    [(True, 18, False), (False, 18, False), (True, 1, False), (True, 18, True)],
)
def test_nr_gpu_timestamps_survive_repeated_recordings(
    shim_probe, enabled, feature, failed, backend
):
    from overlay.telemetry.nr_timing import parse_nr_timing

    wine, root, base_env = shim_probe
    runtime = os.environ.get("PB_TEST_D3D12_RUNTIME")
    if runtime:
        for name in ("d3d12.dll", "d3d12core.dll", "dxgi.dll"):
            shutil.copyfile(Path(runtime) / name, root / name)
    output = root / f"nr-{enabled}-{feature}-{failed}-{backend}.log"
    env = dict(
        base_env,
        PENGUIN_BURNER_NR_TIMING="1" if enabled else "0",
        PENGUIN_BURNER_SHIM_OUTPUT="Z:" + str(output),
    )
    env["WINEDLLOVERRIDES"] += ";_nvngx=n" + (
        ";d3d12=n;d3d12core=n;dxgi=n" if runtime else ""
    )
    if failed:
        env["PB_TEST_NGX_FAIL"] = "1"
    if backend != "ngx":
        env["PB_TEST_NR_FORWARDER"] = "1"
        env["WINEDLLOVERRIDES"] += ";nvngx.dll_dlssnr=n"
    if backend == "unknown-forwarder":
        env["PB_TEST_NR_UNKNOWN_ABI"] = "1"
    for mode in ("late", "nested", "refused"):
        if backend == f"{mode}-forwarder":
            env[f"PB_TEST_NR_{mode.upper()}"] = "1"
    result = subprocess.run(
        [wine, str(root / "nr-probe.exe"), str(feature)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode == 77:
        pytest.skip(
            "No D3D12 GPU; set PB_TEST_D3D12_RUNTIME to a vkd3d-proton DLL directory"
        )
    assert result.returncode == 0, result.stdout + result.stderr
    samples = [
        sample
        for line in output.read_text().splitlines()
        if (sample := parse_nr_timing(line, "700")) is not None
    ]
    if (
        enabled
        and (feature == 18 or backend != "ngx")
        and not failed
        and backend not in ("unknown-forwarder", "refused-forwarder")
    ):
        # More recordings than the 64-slot query pool verifies fence-safe reuse.
        expected = 100 if feature == 18 else 4
        assert len(samples) == expected
        assert len({sample["sample_id"] for sample in samples}) == expected
        assert all(0 < float(sample["nr_gpu_us"]) < 40_000 for sample in samples)
    else:
        assert samples == []
