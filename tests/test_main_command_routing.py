from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.arguments import parse_arguments
from cli.main_command_routing import (
    MainCommandRoutingDependencies,
    route_main_command,
)
from common.penguin_burner_errors import NvmlError
from overlay.telemetry.steam_launch_check import PENGUIN_BURNER_WRAPPER


def _args(**overrides):
    values = {
        "clear_auto_uv_state": False,
        "fresh_auto_uv_scan": False,
        "auto_uv": False,
        "list_auto_uv_profiles": False,
        "json_events": False,
        "delete_auto_uv_profiles": [],
        "assign_auto_uv_tier": None,
        "set_steam_overlay_launch": None,
        "set_main_gpu": "",
        "clear_main_gpu": False,
        "config": "/tmp/config.json",
        "gpu_index": None,
        "stability_test": False,
        "auto_uv_profile": "",
        "auto_uv_require_final_choice": False,
        "auto_uv_voltage_scan": False,
        "silent_fan_curve": False,
        "auto_uv_mode": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _deps(**overrides):
    calls = {
        "logs": [],
        "prints": [],
        "clear": [],
        "profile_verification": [],
        "foreground": [],
        "stop_runtime": [],
        "debug_options": [],
        "release_fans": [],
        "tier_assignments": [],
        "steam_launch_rewrites": [],
        "main_gpu": [],
    }

    def load_config(config_path):
        return (
            {
                "gpu": {"index": 0, "enable_persistence_mode": True},
                "fan": {"poll_interval_s": 1.0},
            },
            Path(config_path),
        )

    defaults = {
        "clear_auto_uv_state": lambda **kwargs: calls["clear"].append(kwargs),
        "load_config": load_config,
        "set_boot_main_gpu": lambda gpu_uuid: calls["main_gpu"].append(gpu_uuid)
        or {"selected": bool(gpu_uuid), "main_gpu_uuid": gpu_uuid},
        "load_auto_uv_final_curve": lambda selector: {"path": "/tmp/final.json"},
        "running_under_systemd_service": lambda: False,
        "enable_stdio_capture": lambda *args, **kwargs: None,
        "stop_existing_penguin_burner_runtime": lambda **kwargs: calls[
            "stop_runtime"
        ].append(kwargs),
        "release_fans_to_hardware_auto": lambda *args, **kwargs: calls[
            "release_fans"
        ].append((args, kwargs)),
        "build_effective_auto_uv_runtime_options": lambda args: {},
        "debug_effective_runtime_options": lambda **kwargs: calls[
            "debug_options"
        ].append(kwargs),
        "run_profile_verification": lambda *args, **kwargs: calls[
            "profile_verification"
        ].append((args, kwargs)),
        "run_auto_uv_foreground_command": lambda *args, **kwargs: calls[
            "foreground"
        ].append((args, kwargs)),
        "read_auto_uv_profile_summaries": lambda: [{"id": "profile-a"}],
        "format_profile_table": lambda profiles: f"table:{profiles[0]['id']}",
        "delete_auto_uv_profiles": lambda selectors: [Path("/tmp/profile-a.json")],
        "resolve_auto_uv_profile": lambda selector, **kwargs: (
            Path("/tmp/profile-a.json"),
            {
                "profile_id": "profile-a",
                "final_verified": True,
                "gpu_identity": {"uuid": "GPU-A"},
            },
        ),
        "save_profile_tier_assignment": lambda profile_id, tier, **kwargs: calls[
            "tier_assignments"
        ].append((profile_id, tier, kwargs.get("gpu_uuid")))
        or {"balanced": profile_id},
        "rewrite_steam_launch_options": lambda **kwargs: calls[
            "steam_launch_rewrites"
        ].append(kwargs)
        or SimpleNamespace(
            app_id=kwargs["app_id"],
            config_path=Path("/tmp/localconfig.vdf"),
            requested_launch_options=kwargs["launch_options"],
            previous_launch_options="mangohud %command%",
            current_launch_options=kwargs["launch_options"],
            persisted=True,
            format_text=lambda: "persisted=True",
        ),
        "steam_launch_option": lambda **kwargs: (
            f"PB_OVERLAY=1 {PENGUIN_BURNER_WRAPPER} %command%"
        ),
        "log": calls["logs"].append,
        "print_fn": lambda *args, **kwargs: calls["prints"].append(
            (args, kwargs)
        ),
    }
    defaults.update(overrides)
    return MainCommandRoutingDependencies(**defaults), calls


@pytest.mark.parametrize(
    ("args", "expected_uuid", "expected_text"),
    [
        (_args(set_main_gpu="GPU-A"), "GPU-A", "Main GPU set to GPU-A."),
        (
            _args(clear_main_gpu=True),
            "",
            "Main GPU cleared; startup monitoring now uses the last saved GPU.",
        ),
    ],
)
def test_main_command_routing_updates_main_gpu_without_loading_config(
    args,
    expected_uuid,
    expected_text,
):
    deps, calls = _deps(
        load_config=lambda _path: (_ for _ in ()).throw(
            AssertionError("config should not be loaded")
        )
    )

    result = route_main_command(
        args=args,
        argv=[],
        explicit_cli_args=True,
        interactive=False,
        dependencies=deps,
    )

    assert result.handled is True
    assert calls["main_gpu"] == [expected_uuid]
    assert calls["prints"][0][0] == (expected_text,)


def test_main_command_routing_rejects_set_and_clear_main_gpu_together():
    deps, _calls = _deps()

    with pytest.raises(NvmlError, match="choose only one"):
        route_main_command(
            args=_args(set_main_gpu="GPU-A", clear_main_gpu=True),
            argv=[],
            explicit_cli_args=True,
            interactive=False,
            dependencies=deps,
        )


def test_main_command_routing_lists_profiles_without_loading_runtime_config():
    deps, calls = _deps(
        load_config=lambda config_path: (_ for _ in ()).throw(
            AssertionError("config should not be loaded")
        )
    )

    result = route_main_command(
        args=_args(list_auto_uv_profiles=True),
        argv=["--list-auto-uv-profiles"],
        explicit_cli_args=True,
        interactive=False,
        dependencies=deps,
    )

    assert result.handled is True
    assert calls["prints"][0][0] == ("table:profile-a",)


def test_main_command_routing_assigns_profile_tier_without_loading_runtime_config():
    deps, calls = _deps(
        load_config=lambda config_path: (_ for _ in ()).throw(
            AssertionError("config should not be loaded")
        )
    )

    result = route_main_command(
        args=_args(assign_auto_uv_tier=["profile-a", "balanced"]),
        argv=["--assign-auto-uv-tier", "profile-a", "balanced"],
        explicit_cli_args=True,
        interactive=False,
        dependencies=deps,
    )

    assert result.handled is True
    assert calls["tier_assignments"] == [("profile-a", "balanced", "GPU-A")]
    assert calls["prints"][0][0] == (
        "Assigned Auto-UV profile profile-a to Balanced tier.",
    )


def test_main_command_routing_assigns_profile_tier_none():
    deps, calls = _deps()

    result = route_main_command(
        args=_args(assign_auto_uv_tier=["profile-a", "none"]),
        argv=["--assign-auto-uv-tier", "profile-a", "none"],
        explicit_cli_args=True,
        interactive=False,
        dependencies=deps,
    )

    assert result.handled is True
    assert calls["tier_assignments"] == [("profile-a", "none", "GPU-A")]
    assert calls["prints"][0][0] == (
        "Removed adaptive tier assignment for Auto-UV profile profile-a.",
    )


def test_main_command_routing_sets_steam_overlay_launch_without_loading_config():
    deps, calls = _deps(
        load_config=lambda config_path: (_ for _ in ()).throw(
            AssertionError("config should not be loaded")
        )
    )

    result = route_main_command(
        args=_args(set_steam_overlay_launch="835960"),
        argv=["--set-steam-overlay-launch", "835960"],
        explicit_cli_args=True,
        interactive=False,
        dependencies=deps,
    )

    assert result.handled is True
    assert calls["steam_launch_rewrites"] == [
        {
            "app_id": "835960",
            "launch_options": f"PB_OVERLAY=1 {PENGUIN_BURNER_WRAPPER} %command%",
        }
    ]
    assert calls["prints"][0][0] == ("persisted=True",)


def test_main_command_routing_reports_steam_overlay_rewrite_failure():
    failed_result = SimpleNamespace(
        app_id="835960",
        config_path=Path("/tmp/localconfig.vdf"),
        requested_launch_options=f"PB_OVERLAY=1 {PENGUIN_BURNER_WRAPPER} %command%",
        previous_launch_options="mangohud %command%",
        current_launch_options="mangohud %command%",
        persisted=False,
        format_text=lambda: "persisted=False",
    )
    deps, calls = _deps(
        rewrite_steam_launch_options=lambda **kwargs: failed_result,
    )

    with pytest.raises(NvmlError, match="not persisted"):
        route_main_command(
            args=_args(set_steam_overlay_launch="835960"),
            argv=["--set-steam-overlay-launch", "835960"],
            explicit_cli_args=True,
            interactive=False,
            dependencies=deps,
        )

    assert calls["prints"][0][0] == ("persisted=False",)


def test_main_command_routing_rejects_unknown_profile_tier():
    deps, _calls = _deps()

    with pytest.raises(NvmlError, match="profile tier must be"):
        route_main_command(
            args=_args(assign_auto_uv_tier=["profile-a", "quiet"]),
            argv=["--assign-auto-uv-tier", "profile-a", "quiet"],
            explicit_cli_args=True,
            interactive=False,
            dependencies=deps,
        )


def test_main_command_routing_rejects_missing_profile_for_tier_assignment():
    deps, _calls = _deps(resolve_auto_uv_profile=lambda selector, **kwargs: None)

    with pytest.raises(NvmlError, match="not found or not final-verified"):
        route_main_command(
            args=_args(assign_auto_uv_tier=["missing", "balanced"]),
            argv=["--assign-auto-uv-tier", "missing", "balanced"],
            explicit_cli_args=True,
            interactive=False,
            dependencies=deps,
        )


def test_main_command_routing_rejects_clear_and_fresh_together():
    deps, _calls = _deps()

    with pytest.raises(NvmlError, match="choose only one"):
        route_main_command(
            args=_args(clear_auto_uv_state=True, fresh_auto_uv_scan=True),
            argv=[],
            explicit_cli_args=True,
            interactive=False,
            dependencies=deps,
        )


def test_main_command_routing_returns_runtime_inputs_for_normal_runtime():
    deps, calls = _deps(
        build_effective_auto_uv_runtime_options=lambda args: {
            "auto_uv_mode": "performance"
        },
    )
    args = _args(gpu_index=2)

    result = route_main_command(
        args=args,
        argv=["--gpu-index", "2"],
        explicit_cli_args=True,
        interactive=True,
        dependencies=deps,
    )

    assert result.handled is False
    assert result.gpu_index == 2
    assert result.gpu_config["index"] == 2
    assert result.config_path == Path("/tmp/config.json")
    assert result.auto_uv_runtime_options["auto_uv_mode"] == "performance"
    assert result.auto_uv_final_curve_available is True
    assert calls["debug_options"][0]["gpu_index"] == 2


def test_main_command_routing_rejects_no_arg_cli_without_implicit_scan_or_runtime():
    deps, calls = _deps(
        load_auto_uv_final_curve=lambda selector: None,
    )
    args = _args()

    with pytest.raises(NvmlError, match="no CLI action selected"):
        route_main_command(
            args=args,
            argv=[],
            explicit_cli_args=False,
            interactive=True,
            dependencies=deps,
        )

    assert args.auto_uv_voltage_scan is False
    assert calls["foreground"] == []
    assert calls["stop_runtime"] == []


def test_main_command_routing_allows_no_arg_systemd_runtime():
    deps, calls = _deps(
        load_auto_uv_final_curve=lambda selector: {"path": "/tmp/final.json"},
        running_under_systemd_service=lambda: True,
    )
    args = _args()

    result = route_main_command(
        args=args,
        argv=[],
        explicit_cli_args=False,
        interactive=False,
        dependencies=deps,
    )

    assert result.handled is False
    assert result.auto_uv_final_curve_available is True
    assert calls["foreground"] == []


def test_main_command_routing_accepts_parsed_auto_uv_scan_args_without_legacy_flag():
    deps, calls = _deps(
        enable_stdio_capture=lambda *args, **kwargs: Path("/tmp/auto-uv.log"),
    )
    args = parse_arguments(
        [
            "--auto-uv-voltage-scan",
            "--json-events",
            "--auto-uv-require-final-choice",
            "--auto-uv-mode",
            "efficiency",
            "--auto-uv-memory-offset-mhz",
            "0",
            "--auto-uv-tail-rise-bins",
            "0",
        ]
    )

    result = route_main_command(
        args=args,
        argv=["--auto-uv-voltage-scan"],
        explicit_cli_args=True,
        interactive=False,
        dependencies=deps,
    )

    assert result.handled is True
    assert calls["foreground"]


def test_main_command_routing_interactive_text_scan_enables_final_choice():
    deps, calls = _deps(
        build_effective_auto_uv_runtime_options=lambda args: {
            "auto_uv_require_final_choice": bool(args.auto_uv_require_final_choice),
        },
    )
    args = parse_arguments(["--auto-uv-voltage-scan"])

    result = route_main_command(
        args=args,
        argv=["--auto-uv-voltage-scan"],
        explicit_cli_args=True,
        interactive=True,
        dependencies=deps,
    )

    assert result.handled is True
    assert args.auto_uv_require_final_choice is True
    assert calls["foreground"][0][1]["auto_uv_runtime_options"] == {
        "auto_uv_require_final_choice": True,
    }


def test_main_command_routing_rejects_noninteractive_text_final_choice():
    deps, _calls = _deps()
    args = parse_arguments(
        ["--auto-uv-voltage-scan", "--auto-uv-require-final-choice"]
    )

    with pytest.raises(NvmlError, match="interactive terminal"):
        route_main_command(
            args=args,
            argv=["--auto-uv-voltage-scan"],
            explicit_cli_args=True,
            interactive=False,
            dependencies=deps,
        )


def test_main_command_routing_releases_fans_to_hardware_auto_on_scan():
    # PenguinBurner must not run its own fan control during a scan: the scan
    # stops the runtime and hands fans back to the GPU hardware-auto curve.
    deps, calls = _deps(
        enable_stdio_capture=lambda *args, **kwargs: Path("/tmp/auto-uv.log"),
    )
    args = parse_arguments(
        [
            "--auto-uv-voltage-scan",
            "--json-events",
            "--auto-uv-require-final-choice",
        ]
    )

    route_main_command(
        args=args,
        argv=["--auto-uv-voltage-scan"],
        explicit_cli_args=True,
        interactive=False,
        dependencies=deps,
    )

    assert calls["stop_runtime"]
    assert calls["release_fans"]
    # Released for the configured GPU index.
    release_args, release_kwargs = calls["release_fans"][0]
    assert release_args[0] == 0


def test_main_command_routing_rejects_plain_stability_test_without_profile():
    deps, calls = _deps(
        build_effective_auto_uv_runtime_options=lambda args: (_ for _ in ()).throw(
            AssertionError("auto-uv runtime options should not be built")
        )
    )

    with pytest.raises(NvmlError, match="requires --auto-uv-profile"):
        route_main_command(
            args=_args(stability_test=True),
            argv=["--stability-test"],
            explicit_cli_args=True,
            interactive=False,
            dependencies=deps,
        )

    assert calls["profile_verification"] == []


def test_main_command_routing_runs_profile_verification_with_profile_selector():
    deps, calls = _deps()

    result = route_main_command(
        args=_args(stability_test=True, auto_uv_profile="latest"),
        argv=["--stability-test", "--auto-uv-profile", "latest"],
        explicit_cli_args=True,
        interactive=False,
        dependencies=deps,
    )

    assert result.handled is True
    assert calls["profile_verification"][0][1]["gpu_index"] == 0
    assert calls["profile_verification"][0][1]["config_path"] == Path("/tmp/config.json")
