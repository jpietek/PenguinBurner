#!/usr/bin/env python3
"""Preflight and monitor PenguinBurner Launchpad PPA publications."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ARCHIVE_API = (
    "https://api.launchpad.net/1.0/~jpietek/+archive/ubuntu/penguin-burner"
)
PACKAGE_NAME = "penguin-burner"
SUCCESS_STATE = "Successfully built"
FAILURE_STATES = frozenset(
    {
        "Cancelled",
        "Chroot problem",
        "Failed to build",
        "Failed to upload",
        "Superseded",
    }
)
SOURCE_FAILURE_STATES = frozenset({"Deleted", "Obsolete", "Superseded"})
VERSION_PATTERN = re.compile(r"[0-9][0-9A-Za-z.+~-]*\Z")
SERIES_PATTERN = re.compile(r"[a-z][a-z0-9-]*\Z")

JsonObject = dict[str, Any]
JsonFetcher = Callable[[str], JsonObject]


class LaunchpadError(RuntimeError):
    """Raised when publication state is invalid or Launchpad is unavailable."""


class PublicationError(LaunchpadError):
    """Raised when an accepted publication cannot complete successfully."""


def ppa_version(version: str, revision: str, series: str) -> str:
    """Return the exact Debian version published for one Ubuntu series."""

    if not VERSION_PATTERN.fullmatch(version):
        raise LaunchpadError(f"invalid package version: {version}")
    if not VERSION_PATTERN.fullmatch(revision):
        raise LaunchpadError(f"invalid Debian revision: {revision}")
    if not SERIES_PATTERN.fullmatch(series):
        raise LaunchpadError(f"invalid Ubuntu series: {series}")
    return f"{version}-{revision}~ppa1~{series}1"


def _fetch_json(url: str) -> JsonObject:
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "PenguinBurner-PPA/1"},
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise LaunchpadError(f"Launchpad request failed for {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise LaunchpadError(f"Launchpad returned a non-object for {url}")
    return payload


def published_sources_url(archive_api: str = ARCHIVE_API) -> str:
    query = urlencode(
        {
            "ws.op": "getPublishedSources",
            "source_name": PACKAGE_NAME,
            "exact_match": "true",
            "order_by_date": "true",
        }
    )
    return f"{archive_api}?{query}"


def _entries(payload: JsonObject) -> list[JsonObject]:
    entries = payload.get("entries", [])
    if not isinstance(entries, list) or not all(
        isinstance(entry, dict) for entry in entries
    ):
        raise LaunchpadError("Launchpad collection has invalid entries")
    return entries


def find_source(
    payload: JsonObject, expected_version: str, series: str
) -> JsonObject | None:
    """Find an exact version/series source publication in a collection."""

    suffix = f"/ubuntu/{series}"
    for entry in _entries(payload):
        if (
            entry.get("source_package_name") == PACKAGE_NAME
            and entry.get("source_package_version") == expected_version
            and str(entry.get("distro_series_link", "")).endswith(suffix)
        ):
            return entry
    return None


def build_outcome(builds: list[JsonObject]) -> tuple[str, str]:
    """Classify Launchpad build records as pending, success, or failure."""

    if not builds:
        return "pending", "waiting for Launchpad to create an amd64 build"
    states = [str(build.get("buildstate", "unknown")) for build in builds]
    failures = [state for state in states if state in FAILURE_STATES]
    if failures:
        return "failure", ", ".join(failures)
    if all(state == SUCCESS_STATE for state in states):
        return "success", ", ".join(states)
    return "pending", ", ".join(states)


def check_available(
    version: str,
    revision: str,
    series_list: list[str],
    *,
    fetch_json: JsonFetcher = _fetch_json,
    archive_api: str = ARCHIVE_API,
) -> None:
    payload = fetch_json(published_sources_url(archive_api))
    for series in series_list:
        expected = ppa_version(version, revision, series)
        existing = find_source(payload, expected, series)
        if existing is not None:
            raise LaunchpadError(
                f"PPA source version already exists for {series}: {expected} "
                f"({existing.get('status', 'unknown')})"
            )
        print(f"PPA version is available for {series}: {expected}")


def wait_for_builds(
    version: str,
    revision: str,
    series_list: list[str],
    *,
    timeout_s: float,
    poll_s: float,
    fetch_json: JsonFetcher = _fetch_json,
    archive_api: str = ARCHIVE_API,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, list[str]]:
    deadline = monotonic() + timeout_s
    last_messages: dict[str, str] = {}
    completed: dict[str, list[str]] = {}

    while len(completed) != len(series_list):
        if monotonic() >= deadline:
            pending = sorted(set(series_list) - set(completed))
            raise LaunchpadError(
                f"timed out waiting for Launchpad builds: {', '.join(pending)}"
            )
        try:
            sources = fetch_json(published_sources_url(archive_api))
            for series in series_list:
                if series in completed:
                    continue
                expected = ppa_version(version, revision, series)
                source = find_source(sources, expected, series)
                if source is None:
                    message = f"{series}: waiting for source acceptance ({expected})"
                else:
                    source_status = str(source.get("status", "unknown"))
                    if source_status in SOURCE_FAILURE_STATES:
                        raise PublicationError(
                            f"Launchpad source failed for {series}: {source_status}"
                        )
                    self_link = source.get("self_link")
                    if not isinstance(self_link, str) or not self_link.startswith(
                        "https://api.launchpad.net/"
                    ):
                        raise PublicationError(
                            f"Launchpad source has no valid API link for {series}"
                        )
                    builds_payload = fetch_json(f"{self_link}?ws.op=getBuilds")
                    builds = _entries(builds_payload)
                    outcome, detail = build_outcome(builds)
                    urls = [
                        str(build["web_link"])
                        for build in builds
                        if isinstance(build.get("web_link"), str)
                    ]
                    message = f"{series}: {source_status}; {detail}"
                    if outcome == "failure":
                        raise PublicationError(f"Launchpad build failed: {message}")
                    if outcome == "success":
                        completed[series] = urls
                        print(message)
                        for url in urls:
                            print(f"  {url}")
                        continue
                if last_messages.get(series) != message:
                    print(message)
                    last_messages[series] = message
        except PublicationError:
            raise
        except LaunchpadError as exc:
            message = f"Launchpad poll warning: {exc}"
            if last_messages.get("__api__") != message:
                print(message, file=sys.stderr)
                last_messages["__api__"] = message
        if len(completed) != len(series_list):
            sleep(poll_s)
    return completed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "wait"):
        command = commands.add_parser(name)
        command.add_argument("version")
        command.add_argument("revision")
        command.add_argument("series", nargs="+")
        if name == "wait":
            command.add_argument("--timeout", type=float, default=10_800)
            command.add_argument("--poll", type=float, default=30)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "check":
            check_available(args.version, args.revision, args.series)
        elif args.command == "wait":
            if args.timeout <= 0 or args.poll <= 0:
                raise LaunchpadError("timeout and poll interval must be positive")
            wait_for_builds(
                args.version,
                args.revision,
                args.series,
                timeout_s=args.timeout,
                poll_s=args.poll,
            )
        else:  # pragma: no cover
            raise LaunchpadError(f"unsupported command: {args.command}")
    except LaunchpadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
