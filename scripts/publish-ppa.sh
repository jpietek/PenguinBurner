#!/usr/bin/env bash

set -euo pipefail

ppa="${PPA_TARGET:-ppa:jpietek/penguin-burner}"
version="${1:-}"
series_list=("${@:2}")
debian_revision="${DEBIAN_REVISION:-1}"
dput_profile_dir=""

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

if [ "${#series_list[@]}" -eq 0 ]; then
    series_list=(questing resolute)
fi

cleanup() {
    rm -f "$HOME/.dput.d/profiles/penguin-ppa.json"
    if [ -n "$dput_profile_dir" ]; then
        rm -rf "$dput_profile_dir"
    fi
}
trap cleanup EXIT

if [ "$ppa" = "ppa:jpietek/penguin-burner" ]; then
    # Fedora's distro-info data may not know future Ubuntu series such as
    # questing/resolute. Use a local dput-ng profile without the
    # supported-distribution hook while keeping checksum, suite, and GPG checks.
    dput_profile_dir="$(mktemp -d)"
    mkdir -p "$dput_profile_dir/profiles"
    cat > "$dput_profile_dir/profiles/penguin-ppa.json" <<'JSON'
{
  "fqdn": "ppa.launchpad.net",
  "incoming": "~jpietek/penguin-burner/ubuntu/",
  "login": "jpietek",
  "meta": "boring",
  "method": "sftp"
}
JSON
    mkdir -p "$HOME/.dput.d/profiles"
    cp "$dput_profile_dir/profiles/penguin-ppa.json" "$HOME/.dput.d/profiles/penguin-ppa.json"
    ppa="penguin-ppa"
fi

for series in "${series_list[@]}"; do
    scripts/build-deb-source.sh "$series" "$version" "$debian_revision"
    changes="$(find "dist/deb/${series}" -maxdepth 1 -name "*_${version}-${debian_revision}~ppa1~${series}1_source.changes" -print -quit)"
    if [ -z "$changes" ]; then
        echo "source changes file not found for ${series}" >&2
        exit 1
    fi
    dput "$ppa" "$changes"
done
