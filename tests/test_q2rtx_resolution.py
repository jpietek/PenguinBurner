from __future__ import annotations

from types import SimpleNamespace

import pytest

import stability.q2rtx.cli as q2rtx_cli
import stability.q2rtx.resolution as q2rtx_resolution


def _memory(total_bytes: int):
    return SimpleNamespace(
        total_bytes=int(total_bytes),
        free_bytes=int(total_bytes) // 2,
        used_bytes=int(total_bytes) // 2,
    )


def _patch_memory(monkeypatch, memory) -> None:
    monkeypatch.setattr(
        q2rtx_resolution,
        "DaemonGpuClient",
        lambda _gpu_index: SimpleNamespace(
            capabilities=lambda: SimpleNamespace(memory=memory)
        ),
    )


def test_auto_resolution_uses_1440p_at_8gib_or_less(monkeypatch) -> None:
    _patch_memory(monkeypatch, _memory(8 * 1024**3))

    choice = q2rtx_resolution.resolve_q2rtx_render_resolution(gpu_index=0)

    assert choice.width == 2560
    assert choice.height == 1440
    assert choice.reason == "auto-vram-le8gib"


def test_auto_resolution_uses_4k_above_8gib(monkeypatch) -> None:
    _patch_memory(monkeypatch, _memory(8 * 1024**3 + 1))

    choice = q2rtx_resolution.resolve_q2rtx_render_resolution(gpu_index=0)

    assert choice.width == 3840
    assert choice.height == 2160
    assert choice.reason == "auto-vram-gt8gib"


def test_auto_resolution_falls_back_to_4k_when_vram_unknown(monkeypatch) -> None:
    _patch_memory(monkeypatch, None)

    choice = q2rtx_resolution.resolve_q2rtx_render_resolution(gpu_index=0)

    assert choice.width == 3840
    assert choice.height == 2160
    assert choice.reason == "auto-vram-unknown"


def test_manual_resolution_overrides_vram_auto(monkeypatch) -> None:
    _patch_memory(monkeypatch, _memory(8 * 1024**3))

    choice = q2rtx_resolution.resolve_q2rtx_render_resolution(
        gpu_index=0,
        requested_width=1920,
        requested_height=1080,
    )

    assert choice.width == 1920
    assert choice.height == 1080
    assert choice.reason == "manual"


def test_standalone_q2rtx_cli_defaults_to_vram_auto(monkeypatch) -> None:
    _patch_memory(monkeypatch, _memory(8 * 1024**3))
    args = q2rtx_cli.parse_q2rtx_stability_args([])

    config = q2rtx_cli.config_from_args(args)

    assert config.width == 2560
    assert config.height == 1440


def test_standalone_q2rtx_cli_help_uses_moved_module_path(monkeypatch, capsys) -> None:
    monkeypatch.setattr(q2rtx_cli.sys, "argv", ["/tmp/__main__.py"])

    with pytest.raises(SystemExit) as exc:
        q2rtx_cli.parse_q2rtx_stability_args(["--help"])

    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "python -m stability.q2rtx" in help_text
    assert "--clean-q2rtx" in help_text
    assert "python -m auto_uv.stability.q2rtx" not in help_text


def test_negative_resolution_is_rejected() -> None:
    with pytest.raises(ValueError):
        q2rtx_resolution.resolve_q2rtx_render_resolution(
            gpu_index=0,
            requested_width=-1,
            requested_height=1080,
        )


@pytest.mark.parametrize("preset, expected", [
    ("1080p", (1920, 1080)),
    ("1440p", (2560, 1440)),
    ("4k", (3840, 2160)),
    ("auto", (3840, 2160)),
])
def test_auto_uv_cli_resolution_reaches_workload_and_final_config(
    monkeypatch, tmp_path, preset, expected,
) -> None:
    from cli.arguments import parse_arguments
    from cli.effective_runtime_options import build_effective_auto_uv_runtime_options
    from stability.q2rtx.config import build_stability_config
    from stability.q2rtx.long_stability_config import build_long_stability_test_config

    _patch_memory(monkeypatch, _memory(24 * 1024**3))
    args = parse_arguments([
        "--auto-uv-voltage-scan", "--auto-uv-q2rtx-resolution", preset,
    ])
    options = build_effective_auto_uv_runtime_options(args)
    assert options["auto_uv_q2rtx_resolution"] == preset
    config = build_stability_config(
        args, gpu_index=0, config_path=tmp_path / "config.toml",
        auto_install_q2rtx=False,
    )
    assert (config.width, config.height) == expected
    final = build_long_stability_test_config(config, total_duration_s=60)
    assert (final.width, final.height) == expected


def test_auto_uv_cli_resolution_default_and_invalid_value() -> None:
    from cli.arguments import parse_arguments
    from cli.effective_runtime_options import build_effective_auto_uv_runtime_options

    args = parse_arguments(["--auto-uv-voltage-scan"])
    assert args.auto_uv_q2rtx_resolution is None
    assert "auto_uv_q2rtx_resolution" not in build_effective_auto_uv_runtime_options(args)
    with pytest.raises(SystemExit) as exc:
        parse_arguments(["--auto-uv-q2rtx-resolution", "invalid"])
    assert exc.value.code == 2
