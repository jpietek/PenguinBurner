#!/usr/bin/env bash

# Run inside the manylinux build container after installing its MinGW RPMs.
# EPEL 8's compiler works for the shim, but its headers predate Direct3D 12.
set -euo pipefail

compiler=x86_64-w64-mingw32-g++
probe() {
    printf '#include <d3d12.h>\nint main() { return sizeof(D3D12_QUERY_HEAP_DESC); }\n' |
        "$compiler" -std=c++17 -x c++ -fsyntax-only -
}
if probe >/dev/null 2>&1; then
    exit 0
fi

sysroot="$("$compiler" -print-sysroot)"
test -n "$sysroot" && test -d "$sysroot/mingw/include"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
curl --fail --location --retry 3 \
    https://codeload.github.com/mingw-w64/mingw-w64/tar.gz/refs/tags/v12.0.0 \
    -o "$work/headers.tar.gz"
printf '%s  %s\n' \
    fa41d462041ed72e29a5101049e5991d4aea5c552fd75f7ac0074989fc54ceb7 \
    "$work/headers.tar.gz" | sha256sum --check -
tar -xzf "$work/headers.tar.gz" -C "$work"
cd "$work/mingw-w64-12.0.0/mingw-w64-headers"
./configure --host=x86_64-w64-mingw32 --prefix="$sysroot/mingw" \
    --with-default-msvcrt=msvcrt
make install
probe
