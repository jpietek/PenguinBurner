from __future__ import annotations

from types import SimpleNamespace

import pytest

import stability.q2rtx.cli as q2rtx_cli
import stability.q2rtx.resolution as q2rtx_resolution


def _patch_gpu(monkeypatch, name: str) -> None:
    monkeypatch.setattr(
        q2rtx_resolution,
        "DaemonGpuClient",
        lambda _gpu_index: SimpleNamespace(
            capabilities=lambda: SimpleNamespace(identity=SimpleNamespace(name=name))
        ),
    )


@pytest.mark.parametrize("name", [
    "NVIDIA GeForce RTX 4080",
    "NVIDIA GeForce RTX 4080 SUPER",
    "NVIDIA GeForce RTX 4090",
    "NVIDIA GeForce RTX 5070 Ti",
    "NVIDIA GeForce RTX 5080",
    "NVIDIA GeForce RTX 5090",
    " GeForce RTX 5070Ti ",
    "nvidia geforce rtx 4080super",
    "RTX 5080",
])
def test_auto_resolution_uses_4k_only_for_supported_desktop_models(monkeypatch, name) -> None:
    # Only identity is supplied: resolution no longer reads VRAM capacity.
    _patch_gpu(monkeypatch, name)

    choice = q2rtx_resolution.resolve_q2rtx_render_resolution(gpu_index=0)

    assert (choice.width, choice.height) == (3840, 2160)
    assert choice.reason == "auto-gpu-4k"
    assert choice.auto_selected
    assert name in q2rtx_resolution.format_q2rtx_resolution_choice(choice)


@pytest.mark.parametrize("name", [
    "NVIDIA GeForce RTX 2080 Ti",
    "NVIDIA GeForce RTX 3060",
    "NVIDIA GeForce RTX 3080",
    "NVIDIA GeForce RTX 3080 Ti",
    "NVIDIA GeForce RTX 3090",
    "NVIDIA GeForce RTX 3090 Ti",
    "NVIDIA GeForce RTX 4060 Ti",
    "NVIDIA GeForce RTX 4070 Ti",
    "NVIDIA GeForce RTX 4070 Ti SUPER",
    "NVIDIA GeForce RTX 5060 Ti",
    "NVIDIA GeForce RTX 5070",
    "NVIDIA GeForce RTX 4080 Laptop GPU",
    "NVIDIA GeForce RTX 4090 Laptop GPU",
    "NVIDIA GeForce RTX 5070 Ti Laptop GPU",
    "NVIDIA GeForce RTX 5080 Laptop GPU",
    "NVIDIA GeForce RTX 5090 Laptop GPU",
    "NVIDIA GeForce RTX 5080 Max-Q",
    "NVIDIA RTX A6000",
    "NVIDIA RTX PRO 6000 Blackwell",
    "NVIDIA GeForce RTX 5090 Ti",
    "NVIDIA GeForce RTX 50900",
    "NVIDIA GeForce RTX 6090",
    "unknown",
    "",
])
def test_all_other_and_unknown_gpus_use_1080p(monkeypatch, name) -> None:
    _patch_gpu(monkeypatch, name)

    choice = q2rtx_resolution.resolve_q2rtx_render_resolution(gpu_index=0)

    assert (choice.width, choice.height) == (1920, 1080)
    assert choice.reason == "auto-gpu-1080p"
    assert choice.auto_selected


def test_auto_resolution_falls_back_to_1080p_when_identification_fails(monkeypatch) -> None:
    def fail(_gpu_index):
        raise RuntimeError("daemon unavailable")

    monkeypatch.setattr(q2rtx_resolution, "DaemonGpuClient", fail)

    choice = q2rtx_resolution.resolve_q2rtx_render_resolution(gpu_index=0)

    assert (choice.width, choice.height) == (1920, 1080)
    assert "gpu=unknown" in q2rtx_resolution.format_q2rtx_resolution_choice(choice)


@pytest.mark.parametrize("width, height, expected", [
    (1920, 1080, (1920, 1080)),
    (2560, 1440, (2560, 1440)),
    (3840, 2160, (3840, 2160)),
    (2560, None, (2560, 1440)),
    (None, 2160, (3840, 2160)),
])
def test_manual_resolution_bypasses_gpu_identification(monkeypatch, width, height, expected) -> None:
    def unexpected_lookup(_gpu_index):
        pytest.fail("manual resolution must not query the GPU")

    monkeypatch.setattr(q2rtx_resolution, "DaemonGpuClient", unexpected_lookup)

    choice = q2rtx_resolution.resolve_q2rtx_render_resolution(
        gpu_index=0,
        requested_width=width,
        requested_height=height,
    )

    assert (choice.width, choice.height) == expected
    assert choice.reason.startswith("manual")
    assert not choice.auto_selected


@pytest.mark.parametrize("gpu_index, expected", [(0, (3840, 2160)), (1, (1920, 1080))])
def test_standalone_q2rtx_cli_uses_selected_gpu(monkeypatch, gpu_index, expected) -> None:
    names = ["NVIDIA GeForce RTX 5080", "NVIDIA GeForce RTX 3080"]
    monkeypatch.setattr(
        q2rtx_resolution, "DaemonGpuClient",
        lambda index: SimpleNamespace(
            capabilities=lambda: SimpleNamespace(identity=SimpleNamespace(name=names[index]))
        ),
    )
    args = q2rtx_cli.parse_q2rtx_stability_args(["--gpu-index", str(gpu_index)])

    config = q2rtx_cli.config_from_args(args)

    assert (config.width, config.height) == expected


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
    ("auto", (1920, 1080)),
])
def test_auto_uv_cli_resolution_reaches_workload_and_final_config(
    monkeypatch, tmp_path, preset, expected,
) -> None:
    from cli.arguments import parse_arguments
    from cli.effective_runtime_options import build_effective_auto_uv_runtime_options
    from stability.q2rtx.config import build_stability_config
    from stability.q2rtx.long_stability_config import build_long_stability_test_config

    _patch_gpu(monkeypatch, "NVIDIA GeForce RTX 3080")
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


@pytest.mark.parametrize("name, expected", [
    ("NVIDIA GeForce RTX 5070 Ti", (3840, 2160)),
    ("NVIDIA GeForce RTX 3080", (1920, 1080)),
    ("unknown", (1920, 1080)),
])
def test_normal_scan_config_uses_gpu_rules_through_final_verification(
    monkeypatch, tmp_path, name, expected,
) -> None:
    from cli.arguments import parse_arguments
    from stability.q2rtx.config import build_stability_config
    from stability.q2rtx.long_stability_config import build_long_stability_test_config

    _patch_gpu(monkeypatch, name)
    args = parse_arguments(["--auto-uv-voltage-scan"])
    config = build_stability_config(
        args, gpu_index=0, config_path=tmp_path / "config.toml",
        auto_install_q2rtx=False,
    )
    final = build_long_stability_test_config(config, total_duration_s=60)

    assert (config.width, config.height) == expected
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
