#!/usr/bin/env bash

# The Flatpak's one pkexec elevation runs product code on the HOST python:
# the generated install transaction (runtime/support/flatpak_daemon_install.py)
# executes `/usr/bin/python3 -m runtime.daemon_client apply-runtime-intent`
# with PYTHONPATH pointing at the Flatpak deployment's site-packages. Compiled
# extensions in that site-packages only import under the sandbox's python
# version, so the reachable import closure must stay pure-python/stdlib AND
# parse/run under every host python users pair the Flatpak with.
#
# Each scenario proves, on a bare distro python (no third-party packages
# installed, so any compiled/third-party dependency creeping into the closure
# fails the import step):
#   - `python3 -m runtime.daemon_client --help` runs;
#   - the lazily imported elevated-path modules import;
#   - the owner packages in the closure byte-compile (parse coverage for
#     branches the import step does not reach);
#   - the generated /bin/sh install transaction passes `sh -n` and its inline
#     socket-probe heredoc byte-compiles.
#
# The repo tree stands in for the Flatpak site-packages: the manifest pip
# installs this same pure-python tree, so parse/import compatibility is
# identical. Scenario floor tracks requires-python (>=3.11): debian-12 ships
# python 3.11.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="${PENGUIN_BURNER_CONTAINER_ENGINE:-docker}"
NETWORK="${PENGUIN_BURNER_CONTAINER_NETWORK:-host}"

CLOSURE_PACKAGES=(runtime auto_uv cli common drivers overlay profiles curve_editors)

if ! command -v "$ENGINE" >/dev/null 2>&1; then
    echo "missing required command: $ENGINE" >&2
    exit 1
fi

scenario_image() {
    case "$1" in
        debian-12) echo "debian:12" ;;
        ubuntu-lts) echo "ubuntu:24.04" ;;
        fedora) echo "fedora:latest" ;;
        arch) echo "archlinux:latest" ;;
        *)
            echo "unknown scenario: $1 (expected debian-12, ubuntu-lts, fedora, or arch)" >&2
            exit 2
            ;;
    esac
}

scenario_bootstrap() {
    case "$1" in
        debian-12|ubuntu-lts)
            echo "apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -qq -y python3 >/dev/null"
            ;;
        fedora)
            echo "dnf -q install -y python3 >/dev/null"
            ;;
        arch)
            echo "pacman -Syu --noconfirm --needed python >/dev/null"
            ;;
    esac
}

run_scenario() {
    local scenario="$1"
    local image bootstrap
    image="$(scenario_image "$scenario")"
    bootstrap="$(scenario_bootstrap "$scenario")"

    echo "==> flatpak host-python compatibility ($scenario scenario, $image)"
    "$ENGINE" run --rm --network "$NETWORK" \
        -v "$ROOT:/src:ro" \
        -w /src \
        -e "CLOSURE_PACKAGES=${CLOSURE_PACKAGES[*]}" \
        "$image" \
        bash -euo pipefail -c '
            '"$bootstrap"'
            export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/src
            echo "host python: $(python3 --version)"

            python3 -m runtime.daemon_client --help >/dev/null
            python3 -c "
import runtime.runtime_spec
import runtime.support.runtime_service
import runtime.support.flatpak_daemon_install
"

            # compileall writes __pycache__, so parse a writable copy of the
            # closure-owning packages.
            mkdir /tmp/closure
            for pkg in $CLOSURE_PACKAGES; do
                cp -a "/src/$pkg" /tmp/closure/
            done
            python3 -m compileall -q /tmp/closure

            # The elevated transaction runs under `/bin/sh -eu -c` on the
            # host; render every variant, syntax-check it with the host sh,
            # and byte-compile the inline socket-probe heredoc.
            python3 - <<"PY"
from pathlib import Path
from runtime.support.flatpak_daemon_install import (
    build_flatpak_daemon_install_script,
)

for action in ("apply-intent", "migrate-legacy", "none"):
    script = build_flatpak_daemon_install_script(runtime_action=action)
    Path(f"/tmp/install-{action}.sh").write_text(script, encoding="utf-8")
PY
            for rendered in /tmp/install-*.sh; do
                sh -n "$rendered"
                sed -n "/<<'\''PY'\''/,/^PY$/p" "$rendered" | sed "1d;\$d" \
                    > /tmp/socket-probe.py
                test -s /tmp/socket-probe.py
                python3 -m py_compile /tmp/socket-probe.py
            done

            echo "host-python compatibility OK"
        '
}

scenarios=("$@")
if [[ ${#scenarios[@]} -eq 0 ]]; then
    scenarios=(debian-12 ubuntu-lts fedora arch)
fi

for scenario in "${scenarios[@]}"; do
    scenario_image "$scenario" >/dev/null
done
for scenario in "${scenarios[@]}"; do
    run_scenario "$scenario"
done
