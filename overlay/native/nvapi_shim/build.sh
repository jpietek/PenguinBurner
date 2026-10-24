#!/usr/bin/env bash
# Build the PenguinBurner NVAPI latency shim (drop-in proxy nvapi64.dll).
#
# Output: build/nvapi64.dll — a PE x86_64 DLL exporting only nvapi_QueryInterface,
# forwarding to the real (dxvk-nvapi) nvapi64.dll and tapping the Reflex latency
# markers. Statically linked so it has no libgcc/libstdc++/winpthread runtime
# dependency to resolve inside the Wine prefix.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

CXX="${CXX:-x86_64-w64-mingw32-g++}"
out="build/nvapi64.dll"
mkdir -p build

"$CXX" \
    -O2 -shared -std=c++17 \
    -fno-exceptions -fno-rtti \
    -ffunction-sections -fdata-sections \
    -static -static-libgcc -static-libstdc++ \
    -s -Wl,--gc-sections \
    -Wall -Wextra \
    -o "$out" \
    src/*.cpp third_party/minhook/src/{buffer,hook,trampoline}.c \
    third_party/minhook/src/hde/hde64.c nvapi64.def \
    -lkernel32

echo "built: $out"
x86_64-w64-mingw32-objdump -p "$out" | sed -n '/Export Address Table/,/^$/p' || true
