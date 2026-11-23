from __future__ import annotations

import json

import runtime.daemon_commands as commands


def _daemon_options(command: list[str]) -> dict:
    assert command[1:4] == ["-m", "runtime.daemon_client", "start-auto-uv"]
    return json.loads(command[4])


def test_ui_scan_command_uses_auto_uv_voltage_scan(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)
    monkeypatch.setattr(commands, "runtime_gpu_index", lambda: 0)

    command = commands.scan_command()
    options = _daemon_options(command)

    assert options["gpu_index"] == 0


def test_ui_scan_command_ignores_removed_legacy_option(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)
    monkeypatch.setattr(commands, "runtime_gpu_index", lambda: 0)

    command = commands.scan_command({"auto_uv": True})
    options = _daemon_options(command)

    assert "auto_uv" not in options
    assert options["gpu_index"] == 0
