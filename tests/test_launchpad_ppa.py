from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_module() -> ModuleType:
    path = Path("scripts/launchpad_ppa.py")
    spec = importlib.util.spec_from_file_location("launchpad_ppa", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PPA = _load_module()


def _source(series: str, version: str, *, status: str = "Published") -> dict[str, Any]:
    return {
        "source_package_name": "penguin-burner",
        "source_package_version": version,
        "distro_series_link": f"https://api.launchpad.net/1.0/ubuntu/{series}",
        "self_link": f"https://api.launchpad.net/1.0/source/{series}",
        "status": status,
    }


def _build(state: str, series: str = "questing") -> dict[str, str]:
    return {
        "buildstate": state,
        "web_link": f"https://launchpad.net/build/{series}",
    }


def test_ppa_version_is_exact_and_validated() -> None:
    assert PPA.ppa_version("0.7.2", "1", "questing") == "0.7.2-1~ppa1~questing1"

    for args in (
        ("0.7.2;bad", "1", "questing"),
        ("0.7.2", "1/2", "questing"),
        ("0.7.2", "1", "Questing"),
    ):
        with pytest.raises(PPA.LaunchpadError):
            PPA.ppa_version(*args)


def test_find_source_requires_exact_version_and_series() -> None:
    payload = {
        "entries": [
            _source("resolute", "0.7.2-1~ppa1~resolute1"),
            _source("questing", "0.7.2-1~ppa1~questing1"),
        ]
    }

    found = PPA.find_source(payload, "0.7.2-1~ppa1~questing1", "questing")

    assert found is payload["entries"][1]
    assert PPA.find_source(payload, "0.7.2-2~ppa1~questing1", "questing") is None


@pytest.mark.parametrize(
    ("builds", "expected"),
    [
        ([], "pending"),
        ([_build("Needs building")], "pending"),
        ([_build("Successfully built")], "success"),
        ([_build("Failed to build")], "failure"),
    ],
)
def test_build_outcome(builds: list[dict[str, str]], expected: str) -> None:
    outcome, _detail = PPA.build_outcome(builds)

    assert outcome == expected


def test_check_available_rejects_duplicate_source_version() -> None:
    expected = "0.7.2-1~ppa1~questing1"

    with pytest.raises(PPA.LaunchpadError, match="already exists"):
        PPA.check_available(
            "0.7.2",
            "1",
            ["questing"],
            fetch_json=lambda _url: {"entries": [_source("questing", expected)]},
        )


def test_check_available_accepts_unpublished_versions(capsys: pytest.CaptureFixture[str]) -> None:
    PPA.check_available(
        "0.7.2",
        "1",
        ["questing", "resolute"],
        fetch_json=lambda _url: {"entries": []},
    )

    output = capsys.readouterr().out
    assert "0.7.2-1~ppa1~questing1" in output
    assert "0.7.2-1~ppa1~resolute1" in output


def test_wait_for_builds_follows_source_to_successful_build() -> None:
    expected = "0.7.2-1~ppa1~questing1"
    responses: Iterator[dict[str, Any]] = iter(
        (
            {"entries": []},
            {"entries": [_source("questing", expected)]},
            {"entries": [_build("Needs building")]},
            {"entries": [_source("questing", expected)]},
            {"entries": [_build("Successfully built")]},
        )
    )
    clock = iter((0.0, 1.0, 2.0, 3.0))

    completed = PPA.wait_for_builds(
        "0.7.2",
        "1",
        ["questing"],
        timeout_s=10,
        poll_s=1,
        fetch_json=lambda _url: next(responses),
        monotonic=lambda: next(clock),
        sleep=lambda _seconds: None,
    )

    assert completed == {"questing": ["https://launchpad.net/build/questing"]}


@pytest.mark.parametrize(
    "payload",
    [
        {"entries": [_source("questing", "0.7.2-1~ppa1~questing1", status="Deleted")]},
        {"entries": [_source("questing", "0.7.2-1~ppa1~questing1")]},
    ],
)
def test_wait_for_builds_fails_on_terminal_publication_state(
    payload: dict[str, Any],
) -> None:
    responses: Iterator[dict[str, Any]]
    if payload["entries"][0]["status"] == "Deleted":
        responses = iter((payload,))
    else:
        responses = iter((payload, {"entries": [_build("Failed to build")]}))

    with pytest.raises(PPA.PublicationError):
        PPA.wait_for_builds(
            "0.7.2",
            "1",
            ["questing"],
            timeout_s=10,
            poll_s=1,
            fetch_json=lambda _url: next(responses),
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
        )


def test_wait_for_builds_times_out_without_source() -> None:
    clock = iter((0.0, 1.0, 2.0))

    with pytest.raises(PPA.LaunchpadError, match="timed out"):
        PPA.wait_for_builds(
            "0.7.2",
            "1",
            ["questing"],
            timeout_s=2,
            poll_s=1,
            fetch_json=lambda _url: {"entries": []},
            monotonic=lambda: next(clock),
            sleep=lambda _seconds: None,
        )
