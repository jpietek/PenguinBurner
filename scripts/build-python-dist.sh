#!/usr/bin/env bash

set -euo pipefail

outdir="${1:-dist/python}"

rm -rf "$outdir"
mkdir -p "$outdir"

python3 -m pip install --upgrade build cibuildwheel twine

# cibuildwheel defaults to docker; use podman when that is what the host has.
if [ -z "${CIBW_CONTAINER_ENGINE:-}" ] && ! command -v docker >/dev/null 2>&1 \
        && command -v podman >/dev/null 2>&1; then
    export CIBW_CONTAINER_ENGINE=podman
fi
PENGUIN_BURNER_REQUIRE_NATIVE_LAYER=1 \
    python3 -m build --sdist --outdir "$outdir"
# MinGW cross-compiles the NVAPI latency shim (overlay/nvapi_shim/nvapi64.dll)
# into the wheel; REQUIRE_NVAPI_SHIM makes a missing toolchain fail the build
# instead of shipping the in-game latency feature hollow (release-plan B2).
# manylinux_2_28 is AlmaLinux 8: mingw lives in EPEL, and its old MinGW needs
# static winpthreads for the -static link.
# The Rust root daemon (burnerd/ -> runtime/daemon_bin/penguin-burnerd) is built
# by REQUIRE_DAEMON: AlmaLinux 8's cargo is too old for edition 2021, so install
# a pinned stable via rustup into /opt/rust and put it on PATH for the build.
PENGUIN_BURNER_REQUIRE_NATIVE_LAYER=1 \
CIBW_ARCHS_LINUX=x86_64 \
CIBW_BUILD=cp312-manylinux_x86_64 \
CIBW_ENVIRONMENT="PENGUIN_BURNER_REQUIRE_NATIVE_LAYER=1 PENGUIN_BURNER_REQUIRE_NATIVE_LAYER32=1 PENGUIN_BURNER_REQUIRE_NVAPI_SHIM=1 PENGUIN_BURNER_REQUIRE_DAEMON=1 CARGO_HOME=/opt/rust/cargo RUSTUP_HOME=/opt/rust/rustup PATH=/opt/rust/cargo/bin:\$PATH" \
CIBW_MANYLINUX_X86_64_IMAGE=manylinux_2_28 \
CIBW_BEFORE_ALL_LINUX=$'if command -v dnf >/dev/null 2>&1; then\n  dnf install -y epel-release\n  dnf install -y vulkan-headers mingw64-gcc-c++ mingw64-winpthreads-static glibc-devel.i686 libstdc++-devel.i686\nelse\n  yum install -y epel-release\n  yum install -y vulkan-headers mingw64-gcc-c++ mingw64-winpthreads-static glibc-devel.i686 libstdc++-devel.i686\nfi\nexport RUSTUP_HOME=/opt/rust/rustup CARGO_HOME=/opt/rust/cargo\ncurl --proto \'=https\' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain 1.82.0 --no-modify-path' \
    python3 -m cibuildwheel --platform linux --output-dir "$outdir"
python3 -m twine check "$outdir"/*

# Verify the built wheel, not only pyproject metadata: PyPI installs must ship
# the Steam launch command that our launch options reference.
python3 - "$outdir" <<'PY'
import configparser
from pathlib import Path
import sys
import zipfile

wheels = sorted(Path(sys.argv[1]).glob("*.whl"))
if not wheels:
    raise SystemExit("error: no wheel produced")
for wheel in wheels:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        entry_points = [
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        ]
        if len(entry_points) != 1:
            raise SystemExit(f"error: {wheel.name} has no unique entry_points.txt")
        parser = configparser.ConfigParser()
        parser.read_string(archive.read(entry_points[0]).decode("utf-8"))
        actual = parser.get("console_scripts", "PENGUIN_BURNER", fallback="")
        if actual.strip() != "overlay.launcher:main":
            raise SystemExit(
                f"error: {wheel.name} PENGUIN_BURNER entry point is {actual!r}"
            )
        required_payloads = {
            "Vulkan layer": (
                "overlay/native_layer/libVkLayer_penguinburner_latency.so",
                b"\x7fELF",
            ),
            "NVAPI shim": ("overlay/nvapi_shim/nvapi64.dll", b"MZ"),
            "burnerd": ("runtime/daemon_bin/penguin-burnerd", b"\x7fELF"),
        }
        for label, (suffix, magic) in required_payloads.items():
            matches = [name for name in names if name.endswith(suffix)]
            if len(matches) != 1 or not archive.read(matches[0]).startswith(magic):
                raise SystemExit(f"error: {wheel.name} has no valid {label} payload")
    print(
        f"verified {wheel.name}: wrapper, Vulkan layer, NVAPI shim, and burnerd"
    )
PY
