from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import zipfile

import pytest

from integrations.heroic import flatpak
from overlay.wrapper_tokens import strip_penguin_burner_tokens, wrapper_present


def test_absolute_wrapper_round_trip_preserves_other_commands(tmp_path):
    command = "PENGUIN_BURNER --pb-overlay=1 --pb-game-id=heroic:game gamemoderun"
    deployed = flatpak.sandbox_command(command, tmp_path / "space home")
    assert wrapper_present(deployed)
    assert strip_penguin_burner_tokens(deployed) == "gamemoderun"
    assert str(flatpak.wrapper_path(tmp_path / "space home")) in deployed


def test_runtime_archive_is_deterministic_and_excludes_non_source(tmp_path):
    package = tmp_path / "overlay"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "launcher.py").write_text("value = 1\n")
    (package / "secret.txt").write_text("must not export")
    content = flatpak._runtime_archive(tmp_path)
    assert content == flatpak._runtime_archive(tmp_path)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.namelist() == ["overlay/__init__.py", "overlay/launcher.py"]


@pytest.fixture
def payload(monkeypatch):
    files = {
        "runtime.zip": b"runtime",
        "native_layer/VkLayer_PENGUINBURNER_latency.json": json.dumps({
            "layer": {"library_path": "./libVkLayer_penguinburner_latency.so"},
        }).encode(),
        "native_layer/libVkLayer_penguinburner_latency.so": b"layer",
        "nvapi_shim/nvapi64.dll": b"shim",
    }
    monkeypatch.setattr(flatpak, "_payload", lambda _source: files)
    return files


def test_prepare_scopes_grants_and_probes_actual_sandbox(monkeypatch, tmp_path, payload):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "ready", "")

    monkeypatch.setattr(flatpak, "run_on_host", run)
    flatpak.ensure_integration(tmp_path)
    wrapper = flatpak.wrapper_path(tmp_path)
    assert wrapper.stat().st_mode & 0o111
    assert calls[0] == [
        "flatpak", "override", "--user",
        f"--filesystem={tmp_path}/.config/PenguinBurner",
        f"--filesystem={tmp_path}/.cache/penguin-burner",
        "--filesystem=/run/penguin-burnerd.sock", flatpak.APP_ID,
    ]
    assert calls[1] == [
        "flatpak", "run", f"--command={wrapper}", flatpak.APP_ID, "--check-integration",
    ]
    manifest = next(wrapper.parent.glob("*/native_layer/*.json"))
    library = Path(json.loads(manifest.read_text())["layer"]["library_path"])
    assert library.read_bytes() == b"layer"
    flatpak.ensure_integration(tmp_path)
    assert len(list(wrapper.parent.glob("*/runtime.zip"))) == 1


def test_probe_failure_is_reported(monkeypatch, tmp_path, payload):
    monkeypatch.setattr(flatpak, "run_on_host", lambda command, **kwargs:
        subprocess.CompletedProcess(command, 1 if "run" in command else 0, "", "old daemon"))
    with pytest.raises(RuntimeError, match="old daemon"):
        flatpak.ensure_integration(tmp_path)


def test_config_location_selects_flatpak(tmp_path):
    root = tmp_path / flatpak.HEROIC_FLATPAK_DIRNAME
    root.mkdir(parents=True)
    (root / "config.json").write_text("{}")
    assert flatpak.uses_flatpak(tmp_path)
    native = tmp_path / ".config/heroic"
    native.mkdir(parents=True)
    (native / "config.json").write_text("{}")
    assert not flatpak.uses_flatpak(tmp_path)
