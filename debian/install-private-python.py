#!/usr/bin/env python3

from __future__ import annotations

import os
import shutil
from pathlib import Path


PACKAGE_DIR = Path("debian/penguin-burner")
APP_DIR = PACKAGE_DIR / "usr/lib/penguin-burner"
BIN_DIR = PACKAGE_DIR / "usr/bin"


def merge_tree(source: Path, target: Path) -> None:
    if not source.exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        destination = target / child.name
        if child.is_dir() and destination.exists():
            merge_tree(child, destination)
            shutil.rmtree(child)
        else:
            if destination.exists():
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            shutil.move(str(child), str(destination))


def normalize_legacy_local_install() -> None:
    local_dir = PACKAGE_DIR / "usr/local"
    if not local_dir.exists():
        return
    merge_tree(local_dir / "bin", APP_DIR / "bin")
    merge_tree(local_dir / "lib", APP_DIR / "lib")
    shutil.rmtree(local_dir)


def normalize_shared_data() -> None:
    merge_tree(APP_DIR / "share", PACKAGE_DIR / "usr/share")


def write_wrapper(script: Path) -> None:
    wrapper = BIN_DIR / script.name
    wrapper.write_text(
        f"""#!/bin/sh
PB_SITE_PACKAGES="$(find /usr/lib/penguin-burner/lib -type d \\( -name dist-packages -o -name site-packages \\) -print -quit 2>/dev/null)"
if [ -n "$PB_SITE_PACKAGES" ]; then
    export PYTHONPATH="$PB_SITE_PACKAGES${{PYTHONPATH:+:$PYTHONPATH}}"
fi
exec python3 "/usr/lib/penguin-burner/bin/{script.name}" "$@"
""",
        encoding="utf-8",
    )
    os.chmod(wrapper, 0o755)


def move_generated_scripts_private() -> None:
    private_bin = APP_DIR / "bin"
    private_bin.mkdir(parents=True, exist_ok=True)
    if not BIN_DIR.exists():
        return
    for script in sorted(BIN_DIR.iterdir()):
        if script.is_file():
            shutil.move(str(script), str(private_bin / script.name))


def main() -> None:
    normalize_legacy_local_install()
    normalize_shared_data()
    move_generated_scripts_private()
    private_bin = APP_DIR / "bin"
    if not private_bin.is_dir():
        raise SystemExit(f"private script directory not found: {private_bin}")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    for script in sorted(private_bin.iterdir()):
        if script.is_file():
            write_wrapper(script)


if __name__ == "__main__":
    main()
