#!/usr/bin/env bash

set -euo pipefail

aur_repo="${1:-../penguin-burner-aur}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Smoke-build the package in the containerized Arch scenarios first so a
# broken toolchain layout fails here, not on user machines after the push.
# PENGUIN_BURNER_SKIP_PACKAGE_SMOKE=1 skips the gate (e.g. right after the
# same scenarios passed in CI for this commit).
if [ "${PENGUIN_BURNER_SKIP_PACKAGE_SMOKE:-0}" != 1 ]; then
    "$script_dir/check-arch-package-build.sh"
fi

if ! command -v makepkg >/dev/null 2>&1; then
    echo "missing required command: makepkg" >&2
    exit 1
fi

if [ ! -d "$aur_repo/.git" ]; then
    git clone ssh://aur@aur.archlinux.org/penguin-burner.git "$aur_repo"
fi

cp packaging/arch/PKGBUILD "$aur_repo/PKGBUILD"

(
    cd "$aur_repo"
    makepkg --ignorearch --printsrcinfo > .SRCINFO
    git add PKGBUILD .SRCINFO
    if git diff --cached --quiet; then
        echo "AUR package is already up to date."
    else
        pkgver="$(awk -F ' = ' '$1 == "\tpkgver" { print $2; exit }' .SRCINFO)"
        pkgrel="$(awk -F ' = ' '$1 == "\tpkgrel" { print $2; exit }' .SRCINFO)"
        git commit -m "Update to ${pkgver}-${pkgrel}"
    fi
    # A previous run may have committed successfully but failed to push.
    git push
)
