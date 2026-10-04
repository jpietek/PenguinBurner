#!/usr/bin/env bash

# Build the 32-bit companion Vulkan layer outside any sandbox, for builds whose
# own toolchain cannot produce it.
#
# The Flatpak SDK is the case in point: its gcc has no multilib support
# (`gcc -print-multi-lib` reports only the default), and no 32-bit libgcc exists
# in the SDK or in Sdk.Compat.i386, which carries runtime libraries rather than
# a toolchain -- compiling -m32 there succeeds, linking cannot, and flathub has
# no i386 runtime for 25.08 to build in instead. So the layer is built here, in
# the same manylinux image the PyPI wheel uses, and handed to that build through
# PENGUIN_BURNER_NATIVE_LAYER32_PREBUILT.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
outdir="${1:-$ROOT/dist/native-layer-i386}"
image="${PENGUIN_BURNER_MANYLINUX_IMAGE:-quay.io/pypa/manylinux_2_28_x86_64}"

engine="${PENGUIN_BURNER_CONTAINER_ENGINE:-}"
if [ -z "$engine" ]; then
    if command -v podman >/dev/null 2>&1; then
        engine=podman
    elif command -v docker >/dev/null 2>&1; then
        engine=docker
    else
        echo "missing a container engine (podman or docker)" >&2
        exit 1
    fi
fi

rm -rf "$outdir"
mkdir -p "$outdir"

# manylinux_2_28 compiles with gcc-toolset-14, whose libstdc++_nonshared.a is
# x86_64-only, so -m32 needs the toolset's own 32-bit libstdc++ on top of the
# base i686 runtime packages.
"$engine" run --rm \
    -v "$ROOT/overlay/native/latency_layer:/src:ro,Z" \
    -v "$outdir:/out:Z" \
    "$image" bash -euo pipefail -c '
        dnf install -y vulkan-headers glibc-devel.i686 libstdc++-devel.i686 \
            libgcc.i686 gcc-toolset-14-libstdc++-devel.i686 >/dev/null
        cmake -S /src -B /tmp/build -DCMAKE_BUILD_TYPE=Release \
            -DCMAKE_C_FLAGS=-m32 -DCMAKE_CXX_FLAGS=-m32 \
            -DPB_LAYER_NAME_SUFFIX=_i386 >/dev/null
        cmake --build /tmp/build --config Release >/dev/null
        cp /tmp/build/libVkLayer_penguinburner_latency_i386.so /out/
        # The 32-bit build stamps library_arch 32 and names its own .so; it is
        # copied under the i386 manifest name so both sit in one directory.
        cp /tmp/build/VkLayer_PENGUINBURNER_latency.json \
            /out/VkLayer_PENGUINBURNER_latency.i386.json
    '

file "$outdir/libVkLayer_penguinburner_latency_i386.so" | grep -q "ELF 32-bit" || {
    echo "built layer is not a 32-bit ELF" >&2
    exit 1
}
echo "32-bit layer ready in $outdir"
