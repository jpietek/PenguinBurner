#!/usr/bin/env python3
"""Verify release artifacts on PyPI; exit 3 when matching files are missing."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def publication_complete(expected: dict[str, str], published: dict[str, str]) -> bool:
    if not expected:
        raise ValueError("No local Python artifacts to publish")
    if any(expected.get(name) != digest for name, digest in published.items()):
        raise ValueError(
            "PyPI files differ from this release; refusing to replace them"
        )
    return published == expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?")
    parser.add_argument("directory", nargs="?", type=Path)
    parser.add_argument("--check-credentials", action="store_true")
    args = parser.parse_args()
    if args.check_credentials:
        config = configparser.ConfigParser(interpolation=None)
        config.read(Path(os.environ.get("TWINE_CONFIG_FILE", "~/.pypirc")).expanduser())
        if not (
            os.environ.get("TWINE_PASSWORD")
            or config.get("pypi", "password", fallback="")
        ):
            parser.error(
                "configure TWINE_PASSWORD or the pypi password in the Twine config before releasing"
            )
        return 0
    if not args.version or args.directory is None:
        parser.error("VERSION and DIRECTORY are required for artifact verification")
    expected = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in args.directory.iterdir()
        if p.is_file()
    }
    request = Request(
        f"https://pypi.org/pypi/penguin-burner/{args.version}/json",
        headers={"Cache-Control": "no-cache"},
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310
            published = json.load(response)
    except HTTPError as error:
        if error.code != 404:
            raise
        published = {"urls": []}
    actual = {p["filename"]: p["digests"]["sha256"] for p in published["urls"]}
    if not publication_complete(expected, actual):
        return 3
    print("PyPI artifact names and SHA256 hashes match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
