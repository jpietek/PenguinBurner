#!/usr/bin/env python3
"""Reject native libraries/wheels that silently omitted display timing."""
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

TIMING_PROCS = (
    b"vkGetPastPresentationTimingEXT\0",
    b"vkSetSwapchainPresentTimingQueueSizeEXT\0",
    b"vkGetSwapchainTimeDomainPropertiesEXT\0",
)


def check_library(payload: bytes, label: str) -> None:
    if not payload.startswith(b"\x7fELF"):
        raise ValueError(f"{label}: not an ELF library")
    missing = [name.rstrip(b"\0").decode() for name in TIMING_PROCS if name not in payload]
    if missing:
        raise ValueError(f"{label}: missing display-timing entry points: {', '.join(missing)}")


def check_path(path: Path) -> None:
    if path.suffix != ".whl":
        check_library(path.read_bytes(), str(path))
        return
    with zipfile.ZipFile(path) as archive:
        for suffix in ("", "_i386"):
            name = f"overlay/native_layer/libVkLayer_penguinburner_latency{suffix}.so"
            check_library(archive.read(name), f"{path.name}:{name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.artifacts:
        try:
            check_path(path)
        except (ValueError, KeyError, OSError, zipfile.BadZipFile) as exc:
            parser.exit(1, f"error: {exc}\n")
        print(f"verified display-timing capture: {path}")


if __name__ == "__main__":
    main()
