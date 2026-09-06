from __future__ import annotations

import pytest

from stability.q2rtx.models import Q2RTXStabilityConfig

from auto_uv.probes.config import cuda_companion_enabled_for_voltage_band
from auto_uv.probes.config import q2rtx_cuda_probe_config_for_voltage_band
from auto_uv.probes.config import q2rtx_only_probe_config_for_voltage_band
from auto_uv.probes.config import reference_discovery_q2rtx_probe_config
from auto_uv.probes.config import short_q2rtx_probe_config
from auto_uv.probes.config import tiered_cuda_probe_duration_s
from auto_uv.probes.config import tiered_q2rtx_probe_duration_s
from auto_uv.run.scan_runtime_settings import read_scan_runtime_settings


def test_short_probe_config_uses_exact_duration_benchmark() -> None:
    config = short_q2rtx_probe_config(
        Q2RTXStabilityConfig(duration_s=60, single_pass_timeout_s=999.0),
        target_duration_s=20,
    )

    assert config.duration_s == 20
    assert config.single_pass_timeout_s == 120.0


def test_reference_discovery_probe_runs_double_q2rtx_without_cuda() -> None:
    config = reference_discovery_q2rtx_probe_config(
        Q2RTXStabilityConfig(companion_command=("cuda",)),
        base_duration_s=10,
    )

    assert config.duration_s == 20
    assert config.companion_command is None


def test_q2rtx_only_voltage_band_probe_clears_cuda() -> None:
    config = q2rtx_only_probe_config_for_voltage_band(
        Q2RTXStabilityConfig(companion_command=("cuda",)),
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=900,
        base_duration_s=10,
    )

    # Moderate sweep: the mid/deep band soaks 1.5x base (15s), not 2x — the
    # 300s final does the real verification.
    assert config.duration_s == 15
    assert config.companion_command is None


def test_voltage_band_probe_skips_cuda_inside_first_five_percent_drop() -> None:
    q2rtx_s = tiered_q2rtx_probe_duration_s(
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=980,
        base_duration_s=10,
    )
    cuda_s = tiered_cuda_probe_duration_s(base_duration_s=10)

    assert q2rtx_s == 10
    assert cuda_s == 5
    assert cuda_s < q2rtx_s
    assert (
        cuda_companion_enabled_for_voltage_band(
            initial_target_voltage_mv=1000,
            candidate_voltage_mv=950,
        )
        is False
    )

    config = q2rtx_cuda_probe_config_for_voltage_band(
        Q2RTXStabilityConfig(gpu_index=2),
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=980,
        base_duration_s=10,
    )

    assert config.duration_s == 10
    assert config.companion_command is None


def test_voltage_band_probe_adds_cuda_after_five_percent_drop() -> None:
    config = q2rtx_cuda_probe_config_for_voltage_band(
        Q2RTXStabilityConfig(gpu_index=2),
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=940,
        base_duration_s=10,
    )

    # Moderate sweep: 15s q2rtx (1.5x base) with the 5s CUDA companion.
    assert config.duration_s == 15
    assert _companion_duration_s(config.companion_command) == 5


def test_moderate_sweep_mid_and_deep_bands_soak_fifteen_seconds() -> None:
    # Moderate sweep: both the mid (>=90%) and deep (<90%) bands run a 15s
    # q2rtx probe with a 5s CUDA companion. (Previously the deep band shifted
    # up to 25s q2rtx + 10s CUDA for a full soak at every rung; that soak is
    # now the job of the 300s final verification, so the sweep is uniform.)
    medium_cuda_s = tiered_cuda_probe_duration_s(
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=920,
        base_duration_s=10,
    )
    deep_cuda_s = tiered_cuda_probe_duration_s(
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=850,
        base_duration_s=10,
    )
    assert (
        tiered_q2rtx_probe_duration_s(
            initial_target_voltage_mv=1000,
            candidate_voltage_mv=920,
            base_duration_s=10,
        ),
        medium_cuda_s,
    ) == (15, 5)
    assert (
        tiered_q2rtx_probe_duration_s(
            initial_target_voltage_mv=1000,
            candidate_voltage_mv=850,
            base_duration_s=10,
        ),
        deep_cuda_s,
    ) == (15, 5)

    config = q2rtx_cuda_probe_config_for_voltage_band(
        Q2RTXStabilityConfig(gpu_index=2),
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=850,
        base_duration_s=10,
    )

    assert config.duration_s == 15
    assert _companion_duration_s(config.companion_command) == 5


def test_scan_runtime_settings_keep_duration_config() -> None:
    source_config = Q2RTXStabilityConfig(duration_s=600, single_pass_timeout_s=999.0)

    settings = read_scan_runtime_settings(
        {},
        source_config,
        gpu_name="NVIDIA GeForce RTX 5080",
    )

    assert settings.q2rtx_config is source_config
    assert settings.q2rtx_config.duration_s == 600
    assert settings.q2rtx_config.single_pass_timeout_s == 999.0
    # Empty options resolve to the efficiency mode, whose per-tier final
    # verification default is 60 s.
    assert settings.final_verification_duration_s == 60
    assert settings.derive_efficiency_stop_streak is True


def test_scan_runtime_settings_ignores_removed_efficiency_stop_override() -> None:
    source_config = Q2RTXStabilityConfig(duration_s=600)

    settings = read_scan_runtime_settings(
        {"auto_uv_efficiency_stop_streak": 4},
        source_config,
    )

    assert settings.efficiency_stop_streak == 2
    assert settings.derive_efficiency_stop_streak is True


def test_scan_runtime_tail_rise_defaults_follow_auto_uv_mode() -> None:
    source_config = Q2RTXStabilityConfig(duration_s=600)

    efficiency = read_scan_runtime_settings({"auto_uv_mode": "efficiency"}, source_config)
    balanced = read_scan_runtime_settings({"auto_uv_mode": "balanced"}, source_config)
    performance = read_scan_runtime_settings({"auto_uv_mode": "performance"}, source_config)
    overridden = read_scan_runtime_settings(
        {"auto_uv_mode": "performance", "auto_uv_tail_rise_bins": 4},
        source_config,
    )

    assert efficiency.tail_rise_bins == 2
    assert balanced.tail_rise_bins == 2
    assert performance.tail_rise_bins == 2
    assert overridden.tail_rise_bins == 4


@pytest.mark.parametrize("mode", ["efficiency", "balanced", "performance"])
@pytest.mark.parametrize("override", [None, 0, 4])
def test_cli_tail_defaults_and_overrides_preserve_tier(mode, override) -> None:
    from cli.arguments import parse_arguments
    from cli.effective_runtime_options import build_effective_auto_uv_runtime_options
    from profiles.uv.profile_tiers import generated_profile_tier

    argv = ["--auto-uv-voltage-scan", "--auto-uv-mode", mode]
    if override is not None:
        argv += ["--auto-uv-tail-rise-bins", str(override)]
    options = build_effective_auto_uv_runtime_options(parse_arguments(argv))
    settings = read_scan_runtime_settings(options, Q2RTXStabilityConfig(duration_s=60))

    assert settings.tail_rise_bins == (2 if override is None else override)
    profile = {**options, "tail_rise_bins": settings.tail_rise_bins}
    assert generated_profile_tier(profile) == mode


def _companion_duration_s(command: tuple[str, ...] | None) -> int | None:
    if command is None:
        return None
    parts = [str(part) for part in command]
    for index, part in enumerate(parts[:-1]):
        if part == "--duration-seconds":
            return int(parts[index + 1])
    return None


def test_final_verification_duration_prefers_scan_option(monkeypatch) -> None:
    from auto_uv.run import scan_runtime_settings as srs

    monkeypatch.delenv("PENGUIN_BURNER_AUTO_UV_FINAL_SECONDS", raising=False)
    assert srs.final_verification_duration_s({"auto_uv_final_verification_s": 60}) == 60
    assert srs.final_verification_duration_s({}) == 300
    assert srs.final_verification_duration_s(None) == 300
    monkeypatch.setenv("PENGUIN_BURNER_AUTO_UV_FINAL_SECONDS", "45")
    assert srs.final_verification_duration_s({}) == 45
    # The explicit per-scan argument outranks the developer env override.
    assert srs.final_verification_duration_s({"auto_uv_final_verification_s": 60}) == 60
