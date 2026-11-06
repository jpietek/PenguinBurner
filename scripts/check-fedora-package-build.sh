#!/usr/bin/env bash

# Build the Fedora package from the current tree inside disposable
# containers, mirroring the COPR pipeline: pack an SRPM from git HEAD, then
# dnf builddep + rpmbuild --rebuild it. Companion of
# check-arch-package-build.sh; publish-copr.sh runs the supported scenarios as
# a pre-publish gate.
#
# Scenarios:
#   fedora-43, fedora-44, fedora-45
#                         the supported Fedora releases and COPR chroots.
#   rawhide               fedora:rawhide — early warning for toolchain
#                         drift; new rustc, gcc, and mingw land there first
#                         (the drift class behind GitHub issue #65 on Arch).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="${PENGUIN_BURNER_CONTAINER_ENGINE:-docker}"
# Host networking works on plain runners and on hosts with broken bridge
# egress alike; the build needs the network for dnf and crates.io.
NETWORK="${PENGUIN_BURNER_CONTAINER_NETWORK:-host}"

usage() {
    cat <<EOF
Usage: $0 [fedora-43] [fedora-44] [fedora-45] [rawhide]

Runs the requested scenarios (default: all). Each scenario builds an SRPM
from a tarball of the current git HEAD using packaging/rpm/penguin-burner.spec,
rebuilds it the way COPR does, then asserts the binary RPM contains the
daemon, the NVAPI shim DLL, and the Vulkan latency layer.
EOF
}

scenario_image() {
    case "$1" in
        fedora-43) echo "fedora:43" ;;
        fedora-44) echo "fedora:44" ;;
        fedora-45) echo "fedora:45" ;;
        rawhide) echo "fedora:rawhide" ;;
        *) return 1 ;;
    esac
}

scenarios=("$@")
if [[ ${#scenarios[@]} -eq 0 ]]; then
    scenarios=(fedora-43 fedora-44 fedora-45 rawhide)
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

version="$(sed -n 's/^Version:[[:space:]]*//p' "$ROOT/packaging/rpm/penguin-burner.spec")"
if [[ -z "$version" ]]; then
    echo "could not read Version from packaging/rpm/penguin-burner.spec" >&2
    exit 1
fi

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

# Build from the checked-out tree, not the released tag tarball, so spec and
# source changes are validated together before they ship.
git -C "$ROOT" archive --format=tar.gz --prefix="penguin-burner-${version}/" \
    -o "$work_dir/penguin-burner-${version}.tar.gz" HEAD
cp "$ROOT/packaging/rpm/penguin-burner.spec" "$work_dir/"

for scenario in "${scenarios[@]}"; do
    image="$(scenario_image "$scenario")"
    echo "==> scenario $scenario ($image)"
    # The build script expands inside the container. Relabel only its temporary inputs.
    # shellcheck disable=SC2016
    "$ENGINE" run --rm --network "$NETWORK" \
        -v "$work_dir:/work:ro,Z" \
        "$image" bash -euo pipefail -c '
        dnf -y -q install rpm-build >/dev/null
        # builddep lives in a plugin package whose name differs between
        # dnf5 (current Fedora) and dnf4.
        dnf -y -q install dnf5-plugins >/dev/null 2>&1 ||
            dnf -y -q install dnf-plugins-core >/dev/null
        rpmbuild -bs /work/penguin-burner.spec \
            --define "_sourcedir /work" --define "_srcrpmdir /tmp/srpm"
        dnf -y builddep /tmp/srpm/penguin-burner-*.src.rpm >/dev/null
        rpmbuild --rebuild /tmp/srpm/penguin-burner-*.src.rpm \
            --define "_topdir /tmp/rpmbuild"
        rpm -qlp /tmp/rpmbuild/RPMS/x86_64/penguin-burner-[0-9]*.rpm \
            > /tmp/package-contents.txt
        for artifact in /usr/libexec/penguin-burnerd \
            "overlay/nvapi_shim/nvapi64.dll" \
            "overlay/native_layer/libVkLayer_penguinburner_latency.so"; do
            if ! grep -q "$artifact" /tmp/package-contents.txt; then
                echo "package is missing $artifact" >&2
                exit 1
            fi
        done
        echo "scenario OK: $(basename /tmp/rpmbuild/RPMS/x86_64/penguin-burner-[0-9]*.rpm)"
    '
done
