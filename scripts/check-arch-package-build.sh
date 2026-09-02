#!/usr/bin/env bash

# Build the Arch package from the current tree inside disposable containers,
# one scenario per real-world toolchain layout. This exists because the AUR
# package breaks when distro toolchains drift (rustc default-linker changes,
# alternative makedepend providers), not when our code changes — so it also
# runs on a weekly CI schedule against freshly updated images.
#
# Scenarios (start with Arch; sibling scripts can cover other distros later):
#   vanilla         archlinux:latest with the declared makedepends.
#   cachyos-shelly  cachyos/cachyos-v3:latest reproducing GitHub issue #65:
#                   the mingw-w64-gcc makedepend satisfied by llvm-mingw
#                   (cross compilers off-PATH under /opt/llvm-mingw) and the
#                   gcc x86_64-linux-gnu-gcc symlink absent, as on hosts whose
#                   gcc predates 16.2.1+r23 while rustc already defaults to
#                   that triple-prefixed linker name.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="${PENGUIN_BURNER_CONTAINER_ENGINE:-docker}"
# Host networking works on plain runners and on hosts with broken bridge
# egress alike; the build needs the network for pacman and crates.io.
NETWORK="${PENGUIN_BURNER_CONTAINER_NETWORK:-host}"

usage() {
    cat <<EOF
Usage: $0 [vanilla] [cachyos-shelly]

Runs the requested scenarios (default: all). Each scenario builds the package
with makepkg from a tarball of the current git HEAD using
packaging/arch/PKGBUILD, then asserts the package contains the daemon, the
NVAPI shim DLL, and the Vulkan latency layer.
EOF
}

scenario_image() {
    case "$1" in
        vanilla) echo "archlinux:latest" ;;
        cachyos-shelly) echo "cachyos/cachyos-v3:latest" ;;
        *) return 1 ;;
    esac
}

scenarios=("$@")
if [[ ${#scenarios[@]} -eq 0 ]]; then
    scenarios=(vanilla cachyos-shelly)
fi
for scenario in "${scenarios[@]}"; do
    if ! scenario_image "$scenario" >/dev/null; then
        usage >&2
        exit 1
    fi
done

if ! command -v "$ENGINE" >/dev/null 2>&1; then
    echo "missing required command: $ENGINE" >&2
    exit 1
fi

pkgver="$(sed -n 's/^pkgver=//p' "$ROOT/packaging/arch/PKGBUILD")"
if [[ -z "$pkgver" ]]; then
    echo "could not read pkgver from packaging/arch/PKGBUILD" >&2
    exit 1
fi

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

# Build from the checked-out tree, not the released tag tarball, so PKGBUILD
# and source changes are validated together before they ship.
git -C "$ROOT" archive --format=tar.gz --prefix="PenguinBurner-${pkgver}/" \
    -o "$work_dir/penguin-burner-${pkgver}.tar.gz" HEAD
sed "s|^source=.*|source=(\"penguin-burner-${pkgver}.tar.gz\")|" \
    "$ROOT/packaging/arch/PKGBUILD" > "$work_dir/PKGBUILD"

for scenario in "${scenarios[@]}"; do
    image="$(scenario_image "$scenario")"
    echo "==> scenario $scenario ($image)"
    "$ENGINE" run --rm --network "$NETWORK" \
        -v "$work_dir:/work:ro" \
        -e SCENARIO="$scenario" \
        "$image" bash -euo pipefail -c '
        pacman -Syu --noconfirm >/dev/null
        # CachyOS rolling images can ship a cmake linked against a jsoncpp
        # soname the just-synced repos no longer provide (a partial-upgrade
        # skew in the base image): cmake then dies with
        # "libjsoncpp.so.NN: cannot open shared object file" before it runs.
        # --needed would keep the broken pair, so force both to the current
        # repo build, which links cmake against the jsoncpp actually installed.
        pacman -S --noconfirm cmake jsoncpp >/dev/null
        cmake --version >/dev/null
        pacman -S --noconfirm --needed base-devel cargo python-build \
            python-installer python-setuptools python-wheel \
            vulkan-headers >/dev/null
        if [[ "$SCENARIO" == cachyos-shelly ]]; then
            pacman -S --noconfirm --needed llvm-mingw >/dev/null
            rm -f /usr/bin/x86_64-linux-gnu-gcc
            # The scenario must keep reproducing the issue #65 environment;
            # fail loudly if a base-image change quietly repairs it.
            if command -v x86_64-w64-mingw32-g++ >/dev/null; then
                echo "scenario broken: MinGW g++ reachable on PATH" >&2
                exit 1
            fi
            if command -v x86_64-linux-gnu-gcc >/dev/null; then
                echo "scenario broken: triple-prefixed gcc still present" >&2
                exit 1
            fi
        else
            pacman -S --noconfirm --needed mingw-w64-gcc >/dev/null
        fi
        useradd -m builder
        install -o builder -m 644 /work/PKGBUILD \
            "/work/penguin-burner-"*.tar.gz -t /home/builder/
        # Dependencies were installed above as root; -d skips the re-check
        # that would otherwise want the runtime depends inside the container.
        su builder -c "cd /home/builder && makepkg -fd --noconfirm"
        # The [0-9] glob skips the split penguin-burner-debug package that
        # distros with the debug makepkg option enabled produce alongside.
        pkg="$(ls /home/builder/penguin-burner-[0-9]*.pkg.tar.zst)"
        tar -tf "$pkg" > /tmp/package-contents.txt
        for artifact in usr/libexec/penguin-burnerd \
            "overlay/nvapi_shim/nvapi64.dll" \
            "overlay/native_layer/libVkLayer_penguinburner_latency.so"; do
            if ! grep -q "$artifact" /tmp/package-contents.txt; then
                echo "package is missing $artifact" >&2
                exit 1
            fi
        done
        echo "scenario $SCENARIO OK: $(basename "$pkg")"
    '
done
