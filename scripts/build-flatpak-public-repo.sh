#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ID="${PENGUIN_BURNER_FLATPAK_APP_ID:-io.github.jpietek.PenguinBurner}"
MANIFEST="${PENGUIN_BURNER_FLATPAK_MANIFEST:-$ROOT/packaging/flatpak/$APP_ID.yml}"
OUT_DIR="${PENGUIN_BURNER_FLATPAK_OUT_DIR:-$ROOT/public-flatpak}"
BUILD_DIR="${PENGUIN_BURNER_FLATPAK_BUILD_DIR:-$ROOT/.flatpak-build/public/$APP_ID}"
STATE_DIR="${PENGUIN_BURNER_FLATPAK_STATE_DIR:-$(dirname "$BUILD_DIR")/.flatpak-builder-state}"
REPO_DIR="$OUT_DIR/repo"
KEY_FILE="$OUT_DIR/penguin-burner-flatpak.gpg"
FLATPAKREPO_FILE="$OUT_DIR/penguin-burner.flatpakrepo"
BUNDLE_FILE="$OUT_DIR/PenguinBurner.flatpak"
GPG_KEY="${PENGUIN_BURNER_FLATPAK_GPG_KEY:-2800D243DB4657B3}"
REPO_URL="${PENGUIN_BURNER_FLATPAK_REPO_URL:-file://$REPO_DIR}"
HOMEPAGE="${PENGUIN_BURNER_FLATPAK_HOMEPAGE:-https://github.com/jpietek/PenguinBurner}"
ICON_URL="${PENGUIN_BURNER_FLATPAK_ICON_URL:-$HOMEPAGE/raw/main/packaging/icons/hicolor/256x256/apps/penguin-burner.png}"
STATIC_DELTAS="${PENGUIN_BURNER_FLATPAK_STATIC_DELTAS:-0}"
BUILD_BUNDLE="${PENGUIN_BURNER_FLATPAK_BUILD_BUNDLE:-0}"

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "error: missing required command: $1" >&2
        exit 1
    fi
}

require_command base64
require_command flatpak
require_command flatpak-builder
require_command gpg

if ! gpg --list-secret-keys "$GPG_KEY" >/dev/null 2>&1; then
    echo "error: GPG signing key not found: $GPG_KEY" >&2
    echo "Set PENGUIN_BURNER_FLATPAK_GPG_KEY to a signing key id." >&2
    exit 1
fi

mkdir -p "$OUT_DIR"
rm -f "$OUT_DIR/install-flatpak-cli-wrappers.sh"
gpg --batch --yes --output "$KEY_FILE" --export "$GPG_KEY"

flatpak install -y flathub \
    org.freedesktop.Platform//25.08 \
    org.freedesktop.Sdk//25.08 \
    org.freedesktop.Sdk.Extension.mingw-w64//25.08 \
    org.freedesktop.Sdk.Extension.rust-stable//25.08

# The manifest takes the 32-bit layer from dist/native-layer-i386; the SDK
# cannot build one itself (see scripts/build-native-layer-i386.sh). The local
# build driver already runs this; the Pages publisher must too, or a clean
# release checkout fails at the wheel build with the prebuilt directory missing.
"$ROOT/scripts/build-native-layer-i386.sh"

# appstream 1.1 decodes icons in a bubblewrap sandbox that cannot read files
# through flatpak-builder's rofiles-fuse mount, so the final compose fails with
# icon-write-error. Build directly on the build directory instead.
flatpak-builder \
    --force-clean \
    --disable-rofiles-fuse \
    --state-dir="$STATE_DIR" \
    --repo="$REPO_DIR" \
    --gpg-sign="$GPG_KEY" \
    "$BUILD_DIR" \
    "$MANIFEST"

update_repo_args=(
    --gpg-sign="$GPG_KEY"
    --gpg-import="$KEY_FILE"
    --title="PenguinBurner"
    --comment="PenguinBurner Flatpak repository"
    --description="Self-hosted PenguinBurner Flatpak repository."
    --homepage="$HOMEPAGE"
    --icon="$ICON_URL"
    --default-branch=master
)

if [[ "$STATIC_DELTAS" == "1" ]]; then
    update_repo_args+=(--generate-static-deltas)
fi

flatpak build-update-repo "${update_repo_args[@]}" "$REPO_DIR"

gpg_key_base64="$(base64 -w0 "$KEY_FILE")"
cat >"$FLATPAKREPO_FILE" <<EOF
[Flatpak Repo]
Title=PenguinBurner
Url=$REPO_URL
Homepage=$HOMEPAGE
Comment=PenguinBurner Flatpak repository
Description=Self-hosted PenguinBurner Flatpak repository.
Icon=$ICON_URL
GPGKey=$gpg_key_base64
EOF

if [[ "$BUILD_BUNDLE" == "1" ]]; then
    flatpak build-bundle \
        --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo \
        --repo-url="$REPO_URL" \
        --gpg-keys="$KEY_FILE" \
        "$REPO_DIR" \
        "$BUNDLE_FILE" \
        "$APP_ID"
    bundle_status="$BUNDLE_FILE"
else
    rm -f "$BUNDLE_FILE"
    bundle_status="skipped; set PENGUIN_BURNER_FLATPAK_BUILD_BUNDLE=1 to build it"
fi

cat <<EOF

Built signed PenguinBurner Flatpak repository:
  Repo:        $REPO_DIR
  Remote file: $FLATPAKREPO_FILE
  Bundle:      $bundle_status
  GPG key:     $KEY_FILE

Local test:
  flatpak remote-add --user --if-not-exists penguinburner "$FLATPAKREPO_FILE"
  flatpak install --user -y penguinburner "$APP_ID"
  flatpak run "$APP_ID"

Public hosting:
  Publish through scripts/publish-flatpak-pages.sh so generated files are
  deployed as a GitHub Pages artifact instead of committed to Git.
EOF
