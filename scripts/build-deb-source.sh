#!/usr/bin/env bash

set -euo pipefail

usage() {
    echo "usage: $0 SERIES [VERSION] [DEBIAN_REVISION]" >&2
    echo "example: $0 questing 0.1.6 1" >&2
}

if [ "$#" -lt 1 ] || [ "$#" -gt 3 ]; then
    usage
    exit 2
fi

series="$1"
version="${2:-}"
debian_revision="${3:-${DEBIAN_REVISION:-1}}"
case "$series" in
    questing|resolute) ;;
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
workroot="$(mktemp -d)"
source_dir="${workroot}/${package}-${version}"
orig="${workroot}/${package}_${version}.orig.tar.gz"

cleanup() {
    rm -rf "$workroot"
}
trap cleanup EXIT

mkdir -p "$outdir"
rm -f "$outdir"/*

git ls-files -z | tar \
    --null \
    --exclude='.copr' \
    --exclude='.copr/*' \
    --exclude='.github' \
    --exclude='.github/*' \
    --exclude='dist' \
    --exclude='dist/*' \
    --exclude='build' \
    --exclude='build/*' \
    --exclude='*.egg-info' \
    --exclude='*.egg-info/*' \
    --exclude='docs' \
    --exclude='docs/*' \
    --exclude='tests' \
    --exclude='tests/*' \
    --exclude='readme-cli.md' \
    --sort=name \
    --mtime='@0' \
    --owner=0 \
    --group=0 \
    --numeric-owner \
    --transform "s,^,${package}-${version}/," \
    --files-from=- \
    -cf - | gzip -n > "$orig"

tar -xzf "$orig" -C "$workroot"
rm -rf "${source_dir}/debian"
cp -a packaging/debian "${source_dir}/debian"

cat > "${source_dir}/debian/changelog" <<EOF
${package} (${debian_version}) ${series}; urgency=medium

  * PPA release for Ubuntu ${series}.

 -- Jan Pietek <jan.pietek@gmail.com>  $(date -R)
EOF

(
    cd "$source_dir"
    dpkg-buildpackage -S -sa -d -k"${key_id}"
)

cp "${workroot}"/*.{dsc,tar.xz,tar.gz,buildinfo,changes} "$outdir"/ 2>/dev/null || true
ls -1 "$outdir"
