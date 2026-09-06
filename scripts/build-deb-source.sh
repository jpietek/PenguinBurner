#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

usage() {
    echo "usage: $0 SERIES [VERSION] [DEBIAN_REVISION]" >&2
    echo "example: $0 resolute 0.7.7 1" >&2
}

if [ "$#" -lt 1 ] || [ "$#" -gt 3 ]; then
    usage
    exit 2
fi

series="$1"
version="${2:-}"
debian_revision="${3:-${DEBIAN_REVISION:-1}}"
case "$series" in
    resolute) ;;
    *)
        echo "unsupported Ubuntu series: $series" >&2
        exit 1
        ;;
esac

if [ -z "$version" ]; then
    version="$(
        python3 - <<'PY'
import tomllib
from pathlib import Path

metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
print(metadata["project"]["version"])
PY
    )"
fi

key_id="${DEBSIGN_KEYID:-B098E377E3C3C124A009E54093CEE10EDB7F01A2}"
package="penguin-burner"
debian_version="${version}-${debian_revision}~ppa1~${series}1"
outdir="${OUTDIR:-dist/deb/${series}}"
source_ref="${PPA_SOURCE_REF:-v${version}}"
orig_tarball_path="${PPA_ORIG_TARBALL:-}"
workroot="$(mktemp -d)"
source_dir="${workroot}/${package}-${version}"
orig="${workroot}/${package}_${version}.orig.tar.gz"

stage_debian_recipe() {
    mkdir -p "$1"
    git ls-files -z -- packaging/debian |
        tar --null --files-from=- --create --file=- |
        tar --extract --file=- --directory="$1" --strip-components=2
}

cleanup() {
    rm -rf "$workroot"
}
trap cleanup EXIT

for command in cargo dpkg-buildpackage git gpg python3 tar; do
    command -v "$command" >/dev/null 2>&1 \
        || { echo "missing required command: $command" >&2; exit 1; }
done

git rev-parse --verify "${source_ref}^{commit}" >/dev/null \
    || { echo "release source ref does not exist: ${source_ref}" >&2; exit 1; }

source_python_version="$(
    git show "${source_ref}:pyproject.toml" | python3 -c \
        'import sys, tomllib; print(tomllib.loads(sys.stdin.buffer.read().decode())["project"]["version"])'
)"
source_daemon_version="$(
    git show "${source_ref}:burnerd/Cargo.toml" \
        | sed -n 's/^version = "\(.*\)"/\1/p' \
        | head -1
)"
if [ "$source_python_version" != "$version" ] \
    || [ "$source_daemon_version" != "$version" ]; then
    echo "release ref versions do not match ${version}: python=${source_python_version} daemon=${source_daemon_version}" >&2
    exit 1
fi
gpg --batch --list-secret-keys "$key_id" >/dev/null 2>&1 \
    || { echo "Debian signing key not found: ${key_id}" >&2; exit 1; }
"$ROOT/scripts/release-gpg.sh" --check "$key_id"

if [ -n "$orig_tarball_path" ]; then
    [ -f "$orig_tarball_path" ] \
        || { echo "PPA orig tarball not found: $orig_tarball_path" >&2; exit 1; }
    # Preserve the immutable input before clearing OUTDIR; callers commonly
    # point PPA_ORIG_TARBALL at an artifact from the previous source build.
    cp "$orig_tarball_path" "$orig"
elif [ "$debian_revision" != "1" ]; then
    echo "PPA_ORIG_TARBALL is required for Debian revision $debian_revision" >&2
    exit 1
fi

mkdir -p "$outdir"
rm -f "$outdir"/*
if [ -n "$orig_tarball_path" ]; then
    tar --extract --gzip --file="$orig" --directory="$workroot"
    [ -d "$source_dir" ] \
        || { echo "PPA orig tarball has no ${package}-${version} root" >&2; exit 1; }
else
    mkdir -p "$source_dir"
    git archive --format=tar "$source_ref" | tar \
        --extract \
        --file=- \
        --directory="$source_dir" \
        --exclude='.github' \
        --exclude='.github/*' \
        --exclude='docs' \
        --exclude='docs/*' \
        --exclude='tests' \
        --exclude='tests/*' \
        --exclude='readme-cli.md'

    # Use the current text-only Debian recipe while keeping application sources
    # at the exact release tag. Generated crates are staged only in this
    # temporary tree and the source package; they never enter Git.
    rm -rf "$source_dir/packaging/debian"
    stage_debian_recipe "$source_dir/packaging/debian"
    mkdir -p "$source_dir/.cargo"
    (
        cd "$source_dir"
        cargo vendor \
            --locked \
            --versioned-dirs \
            --manifest-path burnerd/Cargo.toml \
            vendor > .cargo/config.toml
    )
fi

grep -Fq 'replace-with = "vendored-sources"' "$source_dir/.cargo/config.toml"
grep -Fq 'directory = "vendor"' "$source_dir/.cargo/config.toml"
(
    cd "$source_dir"
    CARGO_HOME="$workroot/cargo-home-check" cargo metadata \
        --locked \
        --offline \
        --format-version=1 \
        --manifest-path burnerd/Cargo.toml >/dev/null
)

required_source_paths=(
    ".cargo/config.toml"
    "vendor"
    "burnerd/Cargo.lock"
    "burnerd/src/main.rs"
    "integrations/steam/manager.py"
    "overlay/native/latency_layer/CMakeLists.txt"
    "overlay/native/nvapi_shim/nvapi64.def"
    "overlay/native/nvapi_shim/src/nvapi_shim.cpp"
)
for relative in "${required_source_paths[@]}"; do
    [ -e "$source_dir/$relative" ] \
        || { echo "source package staging is missing: $relative" >&2; exit 1; }
done

if [ -z "$orig_tarball_path" ]; then
    tar \
        --sort=name \
        --mtime='@0' \
        --owner=0 \
        --group=0 \
        --numeric-owner \
        --directory="$workroot" \
        --create \
        --file=- \
        "${package}-${version}" | gzip -n > "$orig"
fi

rm -rf "${source_dir}/debian"
stage_debian_recipe "${source_dir}/debian"

cat > "${source_dir}/debian/changelog" <<EOF
${package} (${debian_version}) ${series}; urgency=medium

  * PPA release for Ubuntu ${series}.

 -- Jan Pietek <jan.pietek@gmail.com>  $(date -R)
EOF

(
    cd "$source_dir"
    # This is a fresh tag export or an accepted orig archive, with no binary
    # build output to clean. Source-only packaging needs no host debhelper.
    dpkg-buildpackage --no-pre-clean --sign-backend=gpg -S -sa -d \
        -p"$ROOT/scripts/release-gpg.sh" -k"${key_id}"
)

cp "${workroot}"/*.{dsc,tar.xz,tar.gz,buildinfo,changes} "$outdir"/ 2>/dev/null || true
ls -1 "$outdir"
