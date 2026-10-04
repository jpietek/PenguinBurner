#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$ROOT/packaging/flatpak/io.github.jpietek.PenguinBurner.yml"
BUILD_DIR="$ROOT/.flatpak-build/io.github.jpietek.PenguinBurner"

if ! command -v flatpak-builder >/dev/null 2>&1; then
    echo "error: flatpak-builder not found" >&2
    echo "Install it first, for example: sudo dnf install -y flatpak-builder" >&2
    exit 1
fi

flatpak install -y flathub \
    org.freedesktop.Platform//25.08 \
    org.freedesktop.Sdk//25.08 \
    org.freedesktop.Sdk.Extension.mingw-w64//25.08 \
    org.freedesktop.Sdk.Extension.rust-stable//25.08

# The manifest takes the 32-bit layer from this directory; the SDK cannot
# build one itself.
"$(dirname "${BASH_SOURCE[0]}")/build-native-layer-i386.sh"

flatpak-builder \
    --user \
    --force-clean \
    --install \
    "$BUILD_DIR" \
    "$MANIFEST"

echo
echo "Installed local Flatpak:"
echo "  flatpak run io.github.jpietek.PenguinBurner"
