#!/usr/bin/env bash

# Build the Ubuntu binary package from the current tree inside disposable
# containers, mirroring the Launchpad PPA pipeline: stage the source tree
# with packaging/debian as debian/, vendor the cargo crates, then run an
# offline dpkg-buildpackage the way rules does on the builders. Companion of
# check-arch-package-build.sh and check-fedora-package-build.sh;
# publish-ppa.sh runs the resolute scenario as a pre-publish gate.
#
# Scenarios:
#   resolute  ubuntu:resolute (26.04 LTS) — the PPA's supported series.
#   devel     ubuntu:devel — early warning for toolchain drift in the next
#             series (the drift class behind GitHub issue #65 on Arch).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="${PENGUIN_BURNER_CONTAINER_ENGINE:-docker}"
# Host networking works on plain runners and on hosts with broken bridge
# egress alike; the build needs the network for apt and the vendor step.
NETWORK="${PENGUIN_BURNER_CONTAINER_NETWORK:-host}"

usage() {
    cat <<EOF
Usage: $0 [resolute] [devel]

Runs the requested scenarios (default: all). Each scenario stages a source
tree from the current git HEAD with packaging/debian as its debian/
directory, vendors the cargo dependencies, builds the binary package with
dpkg-buildpackage, then asserts the .deb contains the daemon, the NVAPI shim
DLL, and the Vulkan latency layer.
EOF
}

scenario_image() {
    case "$1" in
        resolute) echo "ubuntu:resolute" ;;
        devel) echo "ubuntu:devel" ;;
        *) return 1 ;;
    esac
}

scenarios=("$@")
if [[ ${#scenarios[@]} -eq 0 ]]; then
    scenarios=(resolute devel)
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

version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"
if [[ -z "$version" ]]; then
    echo "could not read version from pyproject.toml" >&2
    exit 1
fi

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

# Build from the checked-out tree, not the released tag, so debian/ recipe
# and source changes are validated together before they ship.
git -C "$ROOT" archive --format=tar.gz --prefix="penguin-burner-${version}/" \
    -o "$work_dir/source.tar.gz" HEAD

for scenario in "${scenarios[@]}"; do
    image="$(scenario_image "$scenario")"
    echo "==> scenario $scenario ($image)"
    "$ENGINE" run --rm --network "$NETWORK" \
        -v "$work_dir:/work:ro" \
        -e SCENARIO="$scenario" -e VERSION="$version" \
        "$image" bash -euo pipefail -c '
        export DEBIAN_FRONTEND=noninteractive
        apt-get -qq update
        # ca-certificates: the minimal image cannot TLS to crates.io without
        # it, and the vendor step downloads the locked crate set.
        apt-get -qq install -y build-essential ca-certificates dpkg-dev >/dev/null
        mkdir -p /build
        tar -xzf /work/source.tar.gz -C /build
        cd "/build/penguin-burner-${VERSION}"
        cp -a packaging/debian debian
        cat > debian/changelog <<EOF
penguin-burner (${VERSION}-1~smoke1~${SCENARIO}1) ${SCENARIO}; urgency=medium

  * Containerized package build smoke test.

 -- PenguinBurner contributors <jan.pietek@gmail.com>  $(date -R)
EOF
        apt-get -qq build-dep -y ./ >/dev/null
        # The PPA source package ships a Cargo.lock-pinned vendor tree so
        # Launchpad builds offline; recreate that stage before the offline
        # build in rules.
        mkdir -p .cargo
        cargo vendor --locked --versioned-dirs \
            --manifest-path burnerd/Cargo.toml vendor > .cargo/config.toml
        dpkg-buildpackage -b -us -uc
        deb="$(ls /build/penguin-burner_*.deb)"
        dpkg-deb -c "$deb" > /tmp/package-contents.txt
        for artifact in usr/libexec/penguin-burnerd \
            "overlay/nvapi_shim/nvapi64.dll" \
            "overlay/native_layer/libVkLayer_penguinburner_latency.so" \
            "overlay/native_layer/libVkLayer_penguinburner_latency_i386.so"; do
            if ! grep -q "$artifact" /tmp/package-contents.txt; then
                echo "package is missing $artifact" >&2
                exit 1
            fi
        done
        echo "scenario $SCENARIO OK: $(basename "$deb")"
    '
done
