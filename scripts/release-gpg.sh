#!/usr/bin/env bash

# Used by dpkg-buildpackage as its signing command: never open pinentry.
set -euo pipefail

if [[ "${1:-}" == --check || "${1:-}" == --check-unattended ]]; then
    key="${2:?usage: release-gpg.sh --check KEY}"
    if [[ "$1" == --check-unattended ]]; then
        # OSTree owns Flatpak signing and cannot use our command wrapper.
        # Require unprotected signing keys so it cannot open pinentry later.
        grips="$(gpg --batch --with-colons --with-keygrip --list-secret-keys "$key" |
            awk -F: '$1 == "sec" || $1 == "ssb" { signing = $12 ~ /s/ }
                     $1 == "grp" && signing { print $10 }')"
        [[ -n "$grips" ]] || { echo "No signing key found: $key" >&2; exit 1; }
        while read -r grip; do
            protection="$(gpg-connect-agent "KEYINFO $grip" /bye |
                awk '$1 == "S" && $2 == "KEYINFO" { print $8 }')"
            [[ "$protection" == C ]] || {
                echo "Unattended release signing requires an unprotected key: $key" >&2
                exit 1
            }
        done <<<"$grips"
    fi
    if ! printf 'PenguinBurner release signing check\n' | \
        gpg --batch --yes --pinentry-mode error --local-user "$key" \
            --output /dev/null --detach-sign; then
        echo "Signing key $key is unavailable for unattended use; unlock it before releasing or configure a dedicated unattended signing key." >&2
        exit 1
    fi
else
    exec gpg --batch --yes --pinentry-mode error "$@"
fi
