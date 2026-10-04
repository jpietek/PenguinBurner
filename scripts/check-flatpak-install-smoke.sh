#!/usr/bin/env bash

set -euo pipefail

APP_ID="io.github.jpietek.PenguinBurner"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$ROOT/packaging/flatpak/$APP_ID.yml"
BUILD_DIR="${PENGUIN_BURNER_FLATPAK_SMOKE_BUILD_DIR:-}"
STATE_DIR="${PENGUIN_BURNER_FLATPAK_SMOKE_STATE_DIR:-}"
WORK_DIR="${PENGUIN_BURNER_FLATPAK_SMOKE_WORK_DIR:-}"
RUNTIME_BRANCH="${PENGUIN_BURNER_FLATPAK_RUNTIME_BRANCH:-25.08}"
CONTAINER_IMAGE="${PENGUIN_BURNER_FLATPAK_SMOKE_IMAGE:-}"
CONTAINER_NETWORK="${PENGUIN_BURNER_CONTAINER_NETWORK:-host}"

usage() {
    cat <<EOF
Usage: $0 [--host] [--container [SCENARIO]]

Build and install the PenguinBurner Flatpak in an isolated user profile, then
prove that:
  - native host commands such as penguin-burner and pburn do not leak in;
  - Flatpak exports the app-id launcher;
  - the packaged /app/bin entry points exist inside the sandbox;
  - the non-GUI CLI entry points run --help successfully.

The flatpak-builder/bubblewrap/ostree stack the build runs on comes from the
host distro, so the container mode takes a host scenario:
  fedora      fedora:latest      (default)
  ubuntu-lts  ubuntu:24.04
  arch        archlinux:latest

Options:
  --host                  run the smoke test directly on this host (default)
  --container [SCENARIO]  run the same smoke test in a disposable container
                          for the given host scenario
EOF
}

die() {
    echo "error: $*" >&2
    exit 1
}

cleanup_work_dir() {
    if [[ -z "${WORK_DIR:-}" || ! -d "$WORK_DIR" ]]; then
        return
    fi

    if command -v fusermount3 >/dev/null 2>&1; then
        find "$WORK_DIR" -mindepth 1 -maxdepth 4 -type d -name 'rofiles-*' \
            -exec fusermount3 -u {} \; >/dev/null 2>&1 || true
    fi
    find "$WORK_DIR" -mindepth 1 -maxdepth 4 -type d -name 'rofiles-*' \
        -exec umount -l {} \; >/dev/null 2>&1 || true
    rm -rf "$WORK_DIR" >/dev/null 2>&1 || true
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

container_engine() {
    if [[ -n "${PENGUIN_BURNER_CONTAINER_ENGINE:-}" ]]; then
        echo "$PENGUIN_BURNER_CONTAINER_ENGINE"
    elif command -v docker >/dev/null 2>&1; then
        echo docker
    elif command -v podman >/dev/null 2>&1; then
        echo podman
    else
        die "missing container engine: install docker or podman"
    fi
}

scenario_image() {
    case "$1" in
        fedora) echo "fedora:latest" ;;
        ubuntu-lts) echo "ubuntu:24.04" ;;
        arch) echo "archlinux:latest" ;;
        *) die "unknown container scenario: $1 (expected fedora, ubuntu-lts, or arch)" ;;
    esac
}

scenario_bootstrap() {
    case "$1" in
        fedora)
            echo "dnf -q install -y flatpak flatpak-builder dbus-daemon binutils >/dev/null"
            ;;
        ubuntu-lts)
            echo "apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -qq -y flatpak flatpak-builder ca-certificates dbus binutils >/dev/null"
            ;;
        arch)
            echo "pacman -Sy --noconfirm --needed flatpak flatpak-builder dbus binutils >/dev/null"
            ;;
        *)
            die "unknown container scenario: $1 (expected fedora, ubuntu-lts, or arch)"
            ;;
    esac
}

# Older flatpak versions (ubuntu-lts ships 1.14) refuse `flatpak run` when the
# system D-Bus is unreachable; distro containers boot without one, so start it.
# Containers also lack systemd, so bus activation of AccountsService (which
# flatpak's parental-controls support queries on `flatpak run`) can only fail;
# where the distro ships it (arch), that failure is a fatal spawn error, so
# drop the activation file to get the clean no-such-service = no-parental-
# controls path every real host without AccountsService takes.
CONTAINER_SYSTEM_BUS_SNIPPET='
if [ ! -S /run/dbus/system_bus_socket ] && command -v dbus-daemon >/dev/null 2>&1; then
    mkdir -p /run/dbus
    dbus-daemon --system --fork
fi
rm -f /usr/share/dbus-1/system-services/org.freedesktop.Accounts.service
'

run_container() {
    local scenario="$1"
    local engine image bootstrap
    engine="$(container_engine)"
    image="${CONTAINER_IMAGE:-$(scenario_image "$scenario")}"
    bootstrap="$(scenario_bootstrap "$scenario")"

    # The manifest takes the 32-bit layer from dist/, and the SDK cannot build
    # one. /src is mounted read-only below, so it has to exist before the
    # container starts.
    ensure_native_layer_i386

    echo "==> flatpak install smoke ($scenario scenario, $image)"
    "$engine" run --rm --privileged --security-opt label=disable \
        --network "$CONTAINER_NETWORK" \
        -v "$ROOT:/src:ro" \
        -w /src \
        "$image" \
        bash -c "
            set -euo pipefail
            $bootstrap
            $CONTAINER_SYSTEM_BUS_SNIPPET
            /src/scripts/check-flatpak-install-smoke.sh --host
        "
}

ensure_native_layer_i386() {
    local layer_dir="$ROOT/dist/native-layer-i386"
    if [[ -f "$layer_dir/libVkLayer_penguinburner_latency_i386.so" ]]; then
        return
    fi
    "$ROOT/scripts/build-native-layer-i386.sh" "$layer_dir"
}

flatpak_ref_exists() {
    local ref="$1"
    flatpak info "$ref" >/dev/null 2>&1
}

ensure_runtime() {
    # The manifest's build-time sdk-extensions (MinGW for the NVAPI shim,
    # rust-stable for the daemon) must be present alongside the runtime and
    # SDK, or flatpak-builder aborts at build-dir initialization.
    local refs=(
        "org.freedesktop.Platform/x86_64/$RUNTIME_BRANCH"
        "org.freedesktop.Sdk/x86_64/$RUNTIME_BRANCH"
        "org.freedesktop.Sdk.Extension.mingw-w64/x86_64/$RUNTIME_BRANCH"
        "org.freedesktop.Sdk.Extension.rust-stable/x86_64/$RUNTIME_BRANCH"
    )
    local missing=()
    local ref

    for ref in "${refs[@]}"; do
        flatpak_ref_exists "$ref" || missing+=("$ref")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        return
    fi

    flatpak --user remote-add --if-not-exists \
        flathub https://dl.flathub.org/repo/flathub.flatpakrepo
    flatpak --user install -y flathub "${missing[@]}"
}

assert_no_host_short_commands() {
    local path_copy="$PATH"
    export PATH="/usr/bin:/bin"

    for bin in penguin-burner pburn penguin-burner-cli pburn-cli; do
        if command -v "$bin" >/dev/null 2>&1; then
            die "host command leaked into smoke test PATH: $bin"
        fi
    done

    export PATH="$path_copy"
}

assert_flatpak_exports() {
    local export_bin="$XDG_DATA_HOME/flatpak/exports/bin/$APP_ID"

    test -x "$export_bin" || die "Flatpak app-id launcher was not exported: $export_bin"

    if PATH="$XDG_DATA_HOME/flatpak/exports/bin:/usr/bin:/bin" command -v penguin-burner >/dev/null 2>&1; then
        die "Flatpak unexpectedly exported native-style penguin-burner host command"
    fi

    PATH="$XDG_DATA_HOME/flatpak/exports/bin:/usr/bin:/bin" command -v "$APP_ID" >/dev/null
}

# The daemon and the Vulkan latency layer are built against the freedesktop
# runtime but execute on the HOST (daemon from /var/opt, layer inside host
# game processes), so each runtime bump can silently raise the minimum host
# glibc. Fail when the built artifacts exceed the accepted floor so the bump
# becomes a deliberate decision (update the floor and docs/flatpak.md
# together).
assert_host_glibc_floor() {
    local floor="${PENGUIN_BURNER_FLATPAK_HOST_GLIBC_FLOOR:-2.39}"
    local files="$XDG_DATA_HOME/flatpak/app/$APP_ID/current/active/files"
    local daemon="$files/libexec/penguin-burnerd"
    local layer requirement
    layer="$(find "$files" -name 'libVkLayer_penguinburner_latency.so' -print -quit)"

    test -f "$daemon" || die "built Flatpak is missing $daemon"
    test -n "$layer" || die "built Flatpak is missing the Vulkan latency layer"
    require_command objdump

    requirement="$( { objdump -T "$daemon"; objdump -T "$layer"; } \
        | grep -o 'GLIBC_[0-9.]*' | sed 's/^GLIBC_//' | sort -uV | tail -1)"
    test -n "$requirement" || die "could not read GLIBC requirements from $daemon"
    echo "host-side binaries require glibc >= $requirement (accepted floor: $floor)"
    if [[ "$(printf '%s\n%s\n' "$floor" "$requirement" | sort -V | tail -1)" != "$floor" ]]; then
        die "host-side binaries now require glibc $requirement, above the accepted floor $floor. A runtime bump raised the host requirement: update docs/flatpak.md and PENGUIN_BURNER_FLATPAK_HOST_GLIBC_FLOOR deliberately."
    fi
}

assert_sandbox_entrypoints() {
    flatpak run --user --command=bash "$APP_ID" -c '
        set -euo pipefail
        for bin in penguin-burner pburn penguin-burner-cli pburn-cli PENGUIN_BURNER penguin-burner-install-wrappers; do
            test -x "/app/bin/$bin"
            resolved="$(command -v "$bin")"
            test "$resolved" = "/app/bin/$bin"
        done
    '

    flatpak run --user --command=penguin-burner-cli "$APP_ID" --help >/dev/null
    flatpak run --user --command=pburn-cli "$APP_ID" --help >/dev/null
    flatpak run --user --command=penguin-burner-install-wrappers "$APP_ID" \
        --bin-dir "$HOME/.local/bin"
    for bin in penguin-burner pburn penguin-burner-cli pburn-cli PENGUIN_BURNER; do
        test -x "$HOME/.local/bin/$bin"
        grep -Fq "Generated by PenguinBurner Flatpak wrapper installer" \
            "$HOME/.local/bin/$bin"
    done
}

run_host() {
    require_command flatpak
    require_command flatpak-builder

    test -f "$MANIFEST" || die "missing Flatpak manifest: $MANIFEST"

    if [[ -z "$WORK_DIR" ]]; then
        WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/penguin-burner-flatpak-smoke.XXXXXX")"
    else
        mkdir -p "$WORK_DIR"
    fi
    trap cleanup_work_dir EXIT

    mkdir -p "$WORK_DIR"/{home,xdg-data,xdg-config,xdg-cache,xdg-state}
    export HOME="$WORK_DIR/home"
    export XDG_DATA_HOME="$WORK_DIR/xdg-data"
    export XDG_CONFIG_HOME="$WORK_DIR/xdg-config"
    export XDG_CACHE_HOME="$WORK_DIR/xdg-cache"
    export XDG_STATE_HOME="$WORK_DIR/xdg-state"

    BUILD_DIR="${BUILD_DIR:-$WORK_DIR/build/$APP_ID}"
    STATE_DIR="${STATE_DIR:-$WORK_DIR/state}"

    assert_no_host_short_commands
    ensure_runtime

    flatpak-builder \
        --user \
        --force-clean \
        --state-dir="$STATE_DIR" \
        --install \
        "$BUILD_DIR" \
        "$MANIFEST"

    flatpak --user info "$APP_ID" >/dev/null
    assert_flatpak_exports
    assert_host_glibc_floor
    assert_sandbox_entrypoints

    cat <<EOF
Flatpak smoke test passed.
  Isolated HOME: $HOME
  Exported launcher: $XDG_DATA_HOME/flatpak/exports/bin/$APP_ID
  GUI launch: flatpak run $APP_ID
  CLI launch: flatpak run --command=pburn-cli $APP_ID --help
  Wrapper install: flatpak run --command=penguin-burner-install-wrappers $APP_ID
EOF
}

mode="host"
scenario="fedora"
while (($#)); do
    case "$1" in
        --container)
            mode="container"
            if [[ $# -gt 1 && "$2" != -* ]]; then
                scenario="$2"
                shift
            fi
            ;;
        --host)
            mode="host"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ "$mode" == "container" ]]; then
    run_container "$scenario"
else
    run_host
fi
