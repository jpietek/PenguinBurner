from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "provider", ["native", "native-with-rustup", "symlink", "hardlink"]
)
@pytest.mark.parametrize("selected", [None, "1.98.0"])
def test_pkgbuild_prepares_both_cargo_providers_before_building(
    tmp_path, provider, selected
):
    result, calls = run_build(tmp_path, provider=provider, selected=selected)
    assert result.returncode == 0, result.stderr
    proxy = provider in {"symlink", "hardlink"}
    if proxy:
        assert calls.pop(0)["argv"] == [
            "rustup",
            "toolchain",
            "install",
            selected or "stable",
            "--profile",
            "minimal",
            "--no-self-update",
        ]
    assert [call["argv"][:2] for call in calls] == [
        ["cargo", "--version"],
        ["rustc", "--version"],
        ["python", "-m"],
        ["cargo", "build"],
    ]
    for call in calls:
        assert call["toolchain"] == (selected or ("stable" if proxy else None))
    for call in calls[2:]:
        assert call["require_daemon"] == "1"
        assert call["linker"] == "gcc"


def test_pkgbuild_stops_before_wheel_when_toolchain_install_fails(tmp_path):
    result, calls = run_build(tmp_path, provider="symlink", fail_install=True)
    assert result.returncode == 7
    assert len(calls) == 1 and calls[0]["argv"][0] == "rustup"


def run_build(tmp_path, *, provider, selected=None, fail_install=False):
    """Execute the actual recipe, substituting toolchain/build commands only."""
    recipe = Path(__file__).resolve().parents[1] / "packaging/arch/PKGBUILD"
    version = re.search(r"^pkgver=(.+)$", recipe.read_text(), re.MULTILINE)
    assert version is not None
    (tmp_path / f"PenguinBurner-{version[1]}").mkdir()
    programs = tmp_path / "bin"
    programs.mkdir()
    trace = tmp_path / "calls.jsonl"
    program = (
        f"#!{sys.executable}\n"
        + """
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
with open(os.environ["BUILD_TRACE"], "a") as handle:
    handle.write(json.dumps({
        "argv": [name, *sys.argv[1:]],
        "toolchain": os.environ.get("RUSTUP_TOOLCHAIN"),
        "require_daemon": os.environ.get("PENGUIN_BURNER_REQUIRE_DAEMON"),
        "linker": os.environ.get("CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER"),
    }) + "\\n")
ready = Path(os.environ["TOOLCHAIN_READY"])
if name == "rustup":
    assert sys.argv[1:3] == ["toolchain", "install"]
    if os.environ["FAIL_INSTALL"] == "1":
        sys.exit(7)
    ready.touch()
elif name in {"cargo", "rustc"}:
    if os.environ["CARGO_PROVIDER"] in {"symlink", "hardlink"} and not ready.exists():
        sys.exit("rustup has no toolchain")
"""
    )
    for name in ("cargo", "rustc", "python", "rustup"):
        if name == "rustup" and provider == "native":
            continue
        path = programs / name
        path.write_text(program)
        path.chmod(0o755)
    if provider in {"symlink", "hardlink"}:
        (programs / "cargo").unlink()
        if provider == "symlink":
            (programs / "cargo").symlink_to("rustup")
        else:
            (programs / "cargo").hardlink_to(programs / "rustup")
    env = {key: value for key, value in os.environ.items() if key != "RUSTUP_TOOLCHAIN"}
    env.update(
        PATH=str(programs),
        BUILD_TRACE=str(trace),
        CARGO_PROVIDER=provider,
        TOOLCHAIN_READY=str(tmp_path / "ready"),
        FAIL_INSTALL=str(int(fail_install)),
    )
    if selected:
        env["RUSTUP_TOOLCHAIN"] = selected
    result = subprocess.run(
        [
            "/bin/bash",
            "-euo",
            "pipefail",
            "-c",
            'source "$1"; build',
            "build-test",
            str(recipe),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result, [json.loads(line) for line in trace.read_text().splitlines()]
