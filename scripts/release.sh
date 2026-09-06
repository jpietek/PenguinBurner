#!/usr/bin/env bash

# Publish a prepared release; retained artifacts and receipts make retries cheap.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ $# != 1 ]]; then
    echo "usage: $0 VERSION" >&2
    exit 2
fi
version="$1"
tag="v$version"
export GH_PROMPT_DISABLED=1 GIT_TERMINAL_PROMPT=0 PIP_NO_INPUT=1
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes}"
export PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring

for command in gh git python3 rpmbuild copr-cli makepkg cargo dpkg-buildpackage dput gpg gpg-connect-agent flatpak flatpak-builder flock; do
    command -v "$command" >/dev/null || { echo "missing required command: $command" >&2; exit 1; }
done
scripts/check-release-version.sh "$version" >/dev/null
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "commit tracked release changes before publishing" >&2
    exit 1
fi
gh auth status >/dev/null
python3 scripts/release_pypi.py --check-credentials
copr_args=()
if [[ -n "${COPR_CONFIG_FILE:-}" ]]; then copr_args+=(--config "$COPR_CONFIG_FILE"); fi
if [[ -n "${COPR_CONFIG:-}" && -z "${COPR_CONFIG_FILE:-}" ]]; then
    copr-cli --config <(printf '%s\n' "$COPR_CONFIG") whoami >/dev/null
else
    copr-cli "${copr_args[@]}" whoami >/dev/null
fi
aur_repo="${PENGUIN_BURNER_AUR_REPO:-$ROOT/../penguin-burner-aur}"
aur_repo="$(realpath -m "$aur_repo")"
aur_remote="ssh://aur@aur.archlinux.org/penguin-burner.git"
if [[ -d "$aur_repo/.git" ]]; then
    [[ -z "$(git -C "$aur_repo" status --porcelain)" ]] || {
        echo "AUR checkout has uncommitted changes: $aur_repo" >&2; exit 1;
    }
    aur_remote="$(git -C "$aur_repo" remote get-url origin)"
fi
git ls-remote "$aur_remote" >/dev/null
scripts/release-gpg.sh --check "${DEBSIGN_KEYID:-B098E377E3C3C124A009E54093CEE10EDB7F01A2}"
scripts/release-gpg.sh --check-unattended "${PENGUIN_BURNER_FLATPAK_GPG_KEY:-2800D243DB4657B3}"
git fetch origin main --tags
commit="$(git rev-parse HEAD)"
git merge-base --is-ancestor "$commit" origin/main || {
    echo "release commit must already be merged into origin/main" >&2; exit 1;
}
out="$ROOT/dist/release/$version/$commit"
mkdir -p "$out"
exec 9>"$out/lock"
flock -n 9 || { echo "this release is already running" >&2; exit 1; }
if git rev-parse --verify "$tag^{commit}" >/dev/null 2>&1; then
    [[ "$(git rev-parse "$tag^{commit}")" == "$commit" ]] || {
        echo "$tag already points to a different commit; check out that tag to resume" >&2; exit 1;
    }
else
    [[ "$commit" == "$(git rev-parse origin/main)" ]] || {
        echo "new releases must be prepared from current origin/main" >&2; exit 1;
    }
    git tag -a "$tag" -m "PenguinBurner $version"
fi

work="$out/source"
if [[ ! -d "$work/.git" ]]; then
    git clone --no-hardlinks "$ROOT" "$work"
    git -C "$work" checkout --detach "$commit"
    git -C "$work" remote set-url origin "$(git remote get-url origin)"
fi
[[ "$(git -C "$work" rev-parse HEAD)" == "$commit" ]] || {
    echo "release checkout has changed: $work" >&2; exit 1;
}
cd "$work"
[[ -z "$(git status --porcelain)" ]] || {
    echo "release checkout contains unexpected changes: $work" >&2; exit 1;
}
if [[ ! -x "$out/tools/bin/python3" ]]; then
    python3 -m venv "$out/tools"
fi
export PATH="$out/tools/bin:$PATH"
if [[ -z "${CIBW_CONTAINER_ENGINE:-}" ]] && command -v podman >/dev/null; then
    export CIBW_CONTAINER_ENGINE=podman
fi

step() {
    local name="$1"
    shift
    if [[ -f "$out/$name.done" ]]; then
        echo "$name: already completed for $commit"
        return
    fi
    # A subshell keeps errexit active inside multi-command stage functions.
    ( set -e; "$@" ) > >(tee "$out/$name.log") 2>&1
    touch "$out/$name.done"
}

publish_github() {
    git push origin "refs/tags/$tag"
    if ! gh release view "$tag" >/dev/null 2>&1; then
        gh release create "$tag" --verify-tag --title "PenguinBurner $version" \
            --notes-file "docs/release-notes-$version.md" --latest
    fi
}

python_build() { scripts/build-python-dist.sh dist/python; }
rpm_build() { scripts/build-srpm.sh dist/rpm; }

upload_asset() {
    local file="$1" name="${1##*/}"
    if gh release view "$tag" --json assets --jq '.assets[].name' | grep -Fxq "$name"; then
        mkdir -p "$out/github-check"
        gh release download "$tag" --pattern "$name" --dir "$out/github-check" --clobber
        cmp "$file" "$out/github-check/$name"
    else
        gh release upload "$tag" "$file"
    fi
}

publish_assets() {
    local file
    for file in dist/python/* dist/rpm/*; do upload_asset "$file"; done
}

publish_pypi() {
    local status=0 _attempt
    python3 scripts/release_pypi.py "$version" dist/python || status=$?
    case "$status" in
        0) return ;;
        3) python3 -m twine upload --non-interactive --skip-existing \
            --repository pypi --repository-url https://upload.pypi.org/legacy/ dist/python/* ;;
        *) return "$status" ;;
    esac
    for _attempt in {1..12}; do
        if python3 scripts/release_pypi.py "$version" dist/python; then return; fi
        sleep 5
    done
    echo "PyPI publication could not be verified" >&2
    return 1
}
publish_copr() { scripts/publish-copr.sh dist/rpm/*.src.rpm; }
publish_aur() { scripts/publish-aur.sh "$aur_repo"; }
publish_ppa() { PYTHONUNBUFFERED=1 scripts/publish-ppa.sh "$version"; }
publish_flatpak() {
    local run_url assets archive="PenguinBurner-pages-$tag.tar.gz" snapshot_dir
    snapshot_dir="${PENGUIN_BURNER_FLATPAK_PAGES_DIST:-dist/flatpak-pages}"
    assets="$(gh release view "$tag" --json assets --jq '.assets[].name')"
    if ! grep -Fxq "$archive" <<<"$assets" || ! grep -Fxq "$archive.sha256" <<<"$assets"; then
        if [[ ! -f "$snapshot_dir/$archive" || ! -f "$snapshot_dir/$archive.sha256" ]]; then
            scripts/publish-flatpak-pages.sh --prepare-only "$tag"
        fi
        upload_asset "$snapshot_dir/$archive"
        upload_asset "$snapshot_dir/$archive.sha256"
    fi
    run_url="$(gh workflow run deploy-flatpak-pages.yml --field "tag=$tag")"
    [[ "$run_url" =~ /actions/runs/([0-9]+)$ ]] || {
        echo "Could not identify the Pages deployment; inspect GitHub Actions before retrying" >&2; return 1;
    }
    gh run watch "${BASH_REMATCH[1]}" --exit-status
}

step python-build python_build
step rpm-build rpm_build
step github publish_github
step github-assets publish_assets

pids=()
for channel in pypi copr aur flatpak ppa; do
    step "$channel" "publish_$channel" &
    pids+=("$!")
done
failed=0
for pid in "${pids[@]}"; do
    wait "$pid" || failed=1
done
if ((failed)); then
    echo "Some channels failed; inspect $out/*.log and rerun the same command to resume." >&2
    exit 1
fi
echo "Released $version to GitHub, PyPI, COPR, AUR, PPA and Flatpak."
echo "Release: https://github.com/jpietek/PenguinBurner/releases/tag/$tag"
echo "Artifacts and receipts: $out"
