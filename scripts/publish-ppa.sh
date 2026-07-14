#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ppa="${PPA_TARGET:-ppa:jpietek/penguin-burner}"
version="${1:-}"
series_list=("${@:2}")
debian_revision="${DEBIAN_REVISION:-1}"
dput_profile_dir=""
dput_profile_backup=""
status_helper="$ROOT/scripts/launchpad_ppa.py"

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

for command in cargo dpkg-buildpackage dput git gpg python3; do
    command -v "$command" >/dev/null 2>&1 \
        || { echo "missing required command: $command" >&2; exit 1; }
done
scripts/check-release-version.sh "$version" >/dev/null
git rev-parse --verify "v${version}^{commit}" >/dev/null \
    || { echo "release tag does not exist: v${version}" >&2; exit 1; }
if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
    echo "PPA publication requires a clean checkout" >&2
    git status --short >&2
    exit 1
fi
git check-ignore -q dist/deb \
    || { echo "dist/deb must remain ignored by Git" >&2; exit 1; }

tracked_artifacts="$(
    git ls-files | grep -E '(^|/)(vendor|target|dist|build)/|\.(deb|dsc|changes|buildinfo|whl|so|dll|src\.rpm)$' || true
)"
if [ -n "$tracked_artifacts" ]; then
    echo "generated build artifacts must not be tracked by Git:" >&2
    printf '%s\n' "$tracked_artifacts" >&2
    exit 1
fi

python3 "$status_helper" check \
    "$version" \
    "$debian_revision" \
    "${series_list[@]}"

cleanup() {
    profile="$HOME/.dput.d/profiles/penguin-ppa.json"
    if [ -n "$dput_profile_backup" ]; then
        cp "$dput_profile_backup" "$profile"
    else
        rm -f "$profile"
    fi
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
  "incoming": "~jpietek/ubuntu/penguin-burner/",
  "login": "anonymous",
  "meta": "boring",
  "method": "ftp",
  "passive_ftp": true
}
JSON
    mkdir -p "$HOME/.dput.d/profiles"
    if [ -f "$HOME/.dput.d/profiles/penguin-ppa.json" ]; then
        dput_profile_backup="$dput_profile_dir/original-penguin-ppa.json"
        cp "$HOME/.dput.d/profiles/penguin-ppa.json" "$dput_profile_backup"
    fi
    cp "$dput_profile_dir/profiles/penguin-ppa.json" "$HOME/.dput.d/profiles/penguin-ppa.json"
    ppa="penguin-ppa"
fi

changes_files=()
for series in "${series_list[@]}"; do
    scripts/build-deb-source.sh "$series" "$version" "$debian_revision"
    changes="$(find "dist/deb/${series}" -maxdepth 1 -name "*_${version}-${debian_revision}~ppa1~${series}1_source.changes" -print -quit)"
    if [ -z "$changes" ]; then
        echo "source changes file not found for ${series}" >&2
        exit 1
    fi
    changes_files+=("$changes")
done

if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
    echo "source packaging unexpectedly changed tracked or unignored files" >&2
    git status --short >&2
    exit 1
fi

for changes in "${changes_files[@]}"; do
    dput "$ppa" "$changes"
done

python3 "$status_helper" wait \
    "$version" \
    "$debian_revision" \
    "${series_list[@]}" \
    --timeout "${PPA_WAIT_TIMEOUT_S:-10800}" \
    --poll "${PPA_POLL_INTERVAL_S:-30}"
