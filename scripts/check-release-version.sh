#!/usr/bin/env bash

set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "usage: $0 VERSION" >&2
    exit 2
fi

requested_version="$1"
python3 - "$requested_version" <<'PY'
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

version = sys.argv[1]
def check(path, actual):
    if actual != version:
        sys.exit(f"release version does not match {path}: requested={version} actual={actual}")

for path, section in (("pyproject.toml", "project"), ("burnerd/Cargo.toml", "package")):
    check(path, tomllib.loads(Path(path).read_text())[section]["version"])

lock_path = "burnerd/Cargo.lock"
for package in tomllib.loads(Path(lock_path).read_text())["package"]:
    if package["name"] == "penguin-burnerd":
        check(lock_path, package["version"])
        break
else:
    sys.exit(f"penguin-burnerd missing from {lock_path}")

for path, pattern in (
    ("packaging/arch/PKGBUILD", r"^pkgver=(.+)$"),
    ("packaging/rpm/penguin-burner.spec", r"^Version:\s*(.+)$"),
):
    match = re.search(pattern, Path(path).read_text(), re.M)
    check(path, match.group(1) if match else None)

appstream = "packaging/flatpak/io.github.jpietek.PenguinBurner.metainfo.xml"
release = ET.parse(appstream).find("releases/release")
check(appstream, release.get("version") if release is not None else None)
PY

if [ ! -f "docs/release-notes-$requested_version.md" ]; then
    echo "missing docs/release-notes-$requested_version.md" >&2
    exit 1
fi

echo "$requested_version"
