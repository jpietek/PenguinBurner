from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_native_overlay_env_true_overrides_disabled_config(tmp_path: Path) -> None:
    binary = _build_native_overlay_probe(tmp_path)
    config_path = tmp_path / "overlay.toml"
    config_path.write_text(
        "\n".join(
            [
                "version = 1",
                "enabled = false",
                (
                    'items = ["base_fps", "latency_ms", "clock_mhz", '
                    '"voltage_mv", "power_w", "profile"]'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(binary)],
        check=True,
        text=True,
        capture_output=True,
        env=_probe_env(config_path, pb_overlay="1"),
    )

    assert result.stdout.strip() == "19 FPS LAT 73 ms 1777 MHz 885 mV 54 W PERF"


def test_native_overlay_env_false_still_disables_enabled_config(tmp_path: Path) -> None:
    binary = _build_native_overlay_probe(tmp_path)
    config_path = tmp_path / "overlay.toml"
    config_path.write_text(
        "\n".join(
            [
                "version = 1",
                "enabled = true",
                'items = ["base_fps", "clock_mhz"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(binary)],
        check=True,
        text=True,
        capture_output=True,
        env=_probe_env(config_path, pb_overlay="0"),
    )

    assert result.stdout.strip() == ""


@pytest.mark.parametrize("config_enabled", [False, True, None])
def test_native_overlay_switches_on_and_off_in_same_process(tmp_path, config_enabled):
    from overlay.state import OVERLAY_OVERRIDE_ENV, write_overlay_override

    binary = _build_native_overlay_probe(tmp_path)
    config_path = tmp_path / "overlay.toml"
    if config_enabled is not None:
        config_path.write_text(
            f'enabled = {str(config_enabled).lower()}\nitems = ["base_fps"]\n'
        )
    override = tmp_path / "overlay-override"
    env = _probe_env(config_path, pb_overlay="0")
    env[OVERLAY_OVERRIDE_ENV] = str(override)
    with subprocess.Popen(
        [str(binary), "--live"], env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True,
    ) as process:
        assert process.stdout.readline().strip() == ""
        for enabled in (True, False, True):
            assert write_overlay_override(enabled, override)
            process.stdin.write("tick\n")
            process.stdin.flush()
            text = process.stdout.readline().strip()
            assert ("19 FPS" in text) is enabled
        process.stdin.close()
        assert process.wait(timeout=5) == 0


def _build_native_overlay_probe(tmp_path: Path) -> Path:
    compiler = shutil.which("c++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable")
    repo_root = Path(__file__).resolve().parents[1]
    source = tmp_path / "native_overlay_probe.cpp"
    source.write_text(
        r'''
#include <iostream>
#include <thread>
#include "latency_layer_internal.h"

namespace pblayer {
std::string build_overlay_text(
    uint64_t fps, const OverlayGpuState& state, uint64_t now_us);
}

int main(int argc, char**) {
    pblayer::OverlayGpuState state{};
    state.clock_mhz = "1777";
    state.voltage_mv = "885";
    state.power_w = "54";
    state.latency_ms = "73";
    state.profile_tier = "Performance";
    uint64_t now_us = 1000000;
    std::cout << pblayer::build_overlay_text(19, state, now_us) << std::endl;
    std::string command;
    while (argc > 1 && std::getline(std::cin, command)) {
        std::this_thread::sleep_for(std::chrono::milliseconds(1100));
        now_us += 1100000;
        std::cout << pblayer::build_overlay_text(19, state, now_us) << std::endl;
    }
    return 0;
}
''',
        encoding="utf-8",
    )
    output = tmp_path / "native_overlay_probe"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-I",
            str(repo_root / "overlay/native/latency_layer/src"),
            str(source),
            str(repo_root / "overlay/native/latency_layer/src/overlay_text.cpp"),
            str(repo_root / "overlay/native/latency_layer/src/latency_state.cpp"),
            "-o",
            str(output),
        ],
        check=True,
        cwd=repo_root,
    )
    return output


def _probe_env(config_path: Path, *, pb_overlay: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "PENGUIN_BURNER_OVERLAY_CONFIG": str(config_path),
        "PB_OVERLAY": pb_overlay,
    }
