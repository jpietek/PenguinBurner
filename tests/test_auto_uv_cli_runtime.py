from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from auto_uv.domain.types import AutoUvError, AutoUvFinalChoiceDiscarded
from auto_uv.run.cli_runtime import (
    AutoUvForegroundDependencies,
    format_auto_uv_final_state,
    run_auto_uv_foreground_command,
    run_auto_uv_voltage_scan,
)
from cli.final_choice_text import format_cli_final_choice_request
from common.penguin_burner_errors import NvmlError
from overlay.telemetry.steam_launch_check import PENGUIN_BURNER_WRAPPER


def test_auto_uv_voltage_scan_wires_json_events_and_final_result() -> None:
    emitted = []
    logs = []
    checks = []
    build_calls = []
    stop_clears = []

    args = SimpleNamespace(json_events=True)
    q2rtx_config = object()

    def fake_build_stability_config(*call_args, **kwargs):
        build_calls.append((call_args, kwargs))
        callback = kwargs["dependency_progress_callback"]
        callback({"step": "download"})
        return q2rtx_config

    def fake_runner(**kwargs):
        assert kwargs["q2rtx_config"] is q2rtx_config
        assert callable(kwargs["event_callback"])
        kwargs["event_callback"]("probe_progress", {"candidate_voltage_mv": 900})
        return SimpleNamespace(
            final_voltage_mv=875,
            lock_clock_mhz=2700,
            final_power_w=210.5,
            final_temperature_c=61.0,
            final_fan_speed_pct=40,
            stop_reason="verified",
            failed_candidate_voltage_mv=850,
        )

    run_auto_uv_voltage_scan(
        args,
        gpu_index=1,
        config_path="/tmp/config.toml",
        auto_uv_runtime_options={"auto_uv_mode": "performance"},
        dependencies=AutoUvForegroundDependencies(
            require_auto_uv_initial_check=lambda **kwargs: checks.append(kwargs),
            build_stability_config=fake_build_stability_config,
            run_voltage_frequency_undervolt_main_loop=fake_runner,
            emit_json_event=lambda enabled, event, **payload: emitted.append(
                (enabled, event, payload)
            ),
            clear_auto_uv_stop_request=lambda: stop_clears.append(None),
            log=logs.append,
        ),
    )

    assert stop_clears == [None]
    assert checks[0]["gpu_index"] == 1
    assert callable(checks[0]["log"])
    assert build_calls[0][1]["auto_install_q2rtx"] is True
    assert build_calls[0][1]["progress_context"] == "Auto-UV"
    assert build_calls[0][1]["dependency_text_progress"] is False
    assert emitted[0] == (
        True,
        "auto_uv_start",
        {"gpu_index": 1, "algorithm": "auto_uv"},
    )
    assert (True, "dependency_progress", {"step": "download"}) in emitted
    assert (True, "probe_progress", {"candidate_voltage_mv": 900}) in emitted
    assert emitted[-1] == (
        True,
        "final_result",
        {
            "voltage_mv": 875,
            "clock_mhz": 2700,
            "power_w": 210.5,
            "temperature_c": 61.0,
            "fan_pct": 40,
            "stop_reason": "verified",
            "failed_candidate_voltage_mv": 850,
        },
    )
    assert logs[-1].startswith("Auto-UV final state: 2700MHz@875mV")


def test_auto_uv_voltage_scan_handles_interactive_final_choice_request(tmp_path) -> None:
    logs = []
    response_path = tmp_path / "choice.json"
    q2rtx_config = object()

    args = SimpleNamespace(json_events=False)

    payload = {
        "auto_uv_mode": "performance",
        "request_reason": "final-verification-failed",
        "response_path": str(response_path),
        "default_candidate_id": "best",
        "final_verification_duration_s": 600,
        "recovery_decision": {
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2895,
            "decision": "final verification failed",
        },
        "candidates": [
            {
                "candidate_id": "slower",
                "candidate_voltage_mv": 890,
                "lock_clock_mhz": 2910,
                "avg_core_clock_mhz": 2890.0,
                "avg_fps": 68.0,
                "efficiency_fps_per_w": 0.31,
                "avg_power_w": 218.0,
                "base_avg_core_clock_mhz": 2700.0,
                "short_verification_duration_s": 90,
            },
            {
                "candidate_id": "best",
                "candidate_voltage_mv": 895,
                "lock_clock_mhz": 2925,
                "avg_core_clock_mhz": 2910.0,
                "avg_fps": 69.0,
                "efficiency_fps_per_w": 0.30,
                "avg_power_w": 230.0,
                "base_avg_core_clock_mhz": 2700.0,
                "short_verification_duration_s": 90,
            },
        ],
    }

    def fake_build_stability_config(*_call_args, **kwargs):
        assert kwargs["dependency_progress_callback"] is None
        assert kwargs["dependency_text_progress"] is True
        return q2rtx_config

    def fake_runner(**kwargs):
        assert kwargs["q2rtx_config"] is q2rtx_config
        assert callable(kwargs["event_callback"])
        kwargs["event_callback"]("final_choice_request", payload)
        response = json.loads(response_path.read_text(encoding="utf-8"))
        assert response["candidate_id"] == "best"
        assert response["final_verification_duration_s"] == 600
        return SimpleNamespace(
            final_voltage_mv=895,
            lock_clock_mhz=2925,
            final_power_w=225.0,
            final_temperature_c=62.0,
            final_fan_speed_pct=42,
            stop_reason="verified",
            failed_candidate_voltage_mv=875,
        )

    run_auto_uv_voltage_scan(
        args,
        gpu_index=0,
        config_path="/tmp/config.toml",
        auto_uv_runtime_options={"auto_uv_require_final_choice": True},
        interactive=True,
        dependencies=AutoUvForegroundDependencies(
            require_auto_uv_initial_check=lambda **_kwargs: None,
            build_stability_config=fake_build_stability_config,
            run_voltage_frequency_undervolt_main_loop=fake_runner,
            final_choice_input=lambda _prompt: "",
            log=logs.append,
        ),
    )

    output = "\n".join(logs)
    assert "Choose Safer Auto-UV Candidate" in output
    assert "Blacklisted failed point: 875mV@2895MHz" in output
    assert "Next safer pick | Passed short probe" in output
    assert output.index("895") < output.index("890")


def test_cli_final_choice_text_uses_balanced_fpsw_sort_and_hides_oc_column(tmp_path) -> None:
    payload = {
        "auto_uv_mode": "balanced",
        "request_reason": "sweep-complete",
        "response_path": str(tmp_path / "choice.json"),
        "default_candidate_id": "efficient",
        "final_verification_duration_s": 300,
        "candidates": [
            {
                "candidate_id": "fast",
                "candidate_voltage_mv": 900,
                "lock_clock_mhz": 2800,
                "avg_core_clock_mhz": 2790.0,
                "avg_fps": 100.0,
                "efficiency_fps_per_w": 0.40,
                "avg_power_w": 250.0,
                "base_avg_core_clock_mhz": 2700.0,
            },
            {
                "candidate_id": "efficient",
                "candidate_voltage_mv": 890,
                "lock_clock_mhz": 2750,
                "avg_core_clock_mhz": 2740.0,
                "avg_fps": 96.0,
                "efficiency_fps_per_w": 0.48,
                "avg_power_w": 200.0,
                "base_avg_core_clock_mhz": 2700.0,
            },
        ],
    }

    output = "\n".join(format_cli_final_choice_request(payload))

    assert "OC MHz" not in output
    assert "Best FPS/W | Passed short probe" in output
    assert output.index("890") < output.index("900")


def test_auto_uv_foreground_command_translates_auto_uv_error() -> None:
    args = SimpleNamespace(
        auto_uv_voltage_scan=True,
        json_events=False,
    )

    def fake_runner(**_kwargs):
        raise AutoUvError("driver rejected curve")

    with pytest.raises(NvmlError, match="driver rejected curve"):
        run_auto_uv_foreground_command(
            args,
            gpu_index=0,
            config_path="/tmp/config.toml",
            auto_uv_runtime_options={},
            interactive=False,
            dependencies=AutoUvForegroundDependencies(
                require_auto_uv_initial_check=lambda **_kwargs: None,
                build_stability_config=lambda *_args, **_kwargs: object(),
                run_voltage_frequency_undervolt_main_loop=fake_runner,
            ),
        )


def test_auto_uv_foreground_command_logs_discarded_final_choice() -> None:
    args = SimpleNamespace(
        auto_uv_voltage_scan=True,
        json_events=False,
    )
    logs = []

    def fake_runner(**_kwargs):
        raise AutoUvFinalChoiceDiscarded("discarded")

    run_auto_uv_foreground_command(
        args,
        gpu_index=0,
        config_path="/tmp/config.toml",
        auto_uv_runtime_options={},
        interactive=False,
        dependencies=AutoUvForegroundDependencies(
            require_auto_uv_initial_check=lambda **_kwargs: None,
            build_stability_config=lambda *_args, **_kwargs: object(),
            run_voltage_frequency_undervolt_main_loop=fake_runner,
            log=logs.append,
        ),
    )

    assert logs == [
        "Auto-UV: running the voltage-frequency undervolt main loop.",
        "discarded",
    ]


def test_auto_uv_foreground_command_prompts_to_start_and_install_single_profile() -> None:
    args = SimpleNamespace(
        auto_uv_voltage_scan=True,
        json_events=False,
    )
    prompts = []
    applied = []

    def fake_runner(**_kwargs):
        return SimpleNamespace(
            success=True,
            final_voltage_mv=850,
            lock_clock_mhz=2762,
            final_power_w=278.8,
            final_temperature_c=65.8,
            final_fan_speed_pct=41.4,
            stop_reason="verified",
            failed_candidate_voltage_mv=None,
        )

    def prompt_yes_no(prompt, *, default):
        prompts.append((prompt, default))
        return True

    run_auto_uv_foreground_command(
        args,
        gpu_index=0,
        config_path="/tmp/config.toml",
        auto_uv_runtime_options={},
        interactive=True,
        program_file="/tmp/penguin_burner.py",
        journal_hours=6,
        prompt_yes_no=prompt_yes_no,
        dependencies=AutoUvForegroundDependencies(
            require_auto_uv_initial_check=lambda **_kwargs: None,
            build_stability_config=lambda *_args, **_kwargs: object(),
            run_voltage_frequency_undervolt_main_loop=fake_runner,
            read_auto_uv_profiles=lambda: [],
            apply_runtime_intent=lambda intent, **kwargs: applied.append(
                (intent, kwargs)
            ),
            log=lambda *_args: None,
        ),
    )

    assert prompts == [
        ("Start PenguinBurner runtime now with single profile 850mv-2762mhz?", True),
        ("Persist single profile 850mv-2762mhz for startup?", False),
    ]
    assert applied == [
        (
            {
                "profile_selector": "850mv-2762mhz",
                "silent_fan_curve": False,
                "adaptive_auto_uv": False,
                "adaptive_target_fps": None,
                "game_telemetry": True,
                "gpu_index": None,
            },
            {"persist_on_startup": False},
        ),
        (
            {
                "profile_selector": "850mv-2762mhz",
                "silent_fan_curve": False,
                "adaptive_auto_uv": False,
                "adaptive_target_fps": None,
                "game_telemetry": True,
                "gpu_index": None,
            },
            {"persist_on_startup": True},
        ),
    ]


def test_auto_uv_foreground_command_offers_optional_adaptive_when_two_tiers_exist() -> None:
    args = SimpleNamespace(
        auto_uv_voltage_scan=True,
        json_events=False,
    )
    logs = []
    prompts = []
    applied = []
    answers = iter([True, True, False])

    def fake_runner(**_kwargs):
        return SimpleNamespace(
            success=True,
            final_voltage_mv=900,
            lock_clock_mhz=2800,
            final_power_w=None,
            final_temperature_c=None,
            final_fan_speed_pct=None,
            stop_reason="verified",
            failed_candidate_voltage_mv=None,
        )

    def prompt_yes_no(prompt, *, default):
        prompts.append((prompt, default))
        return next(answers)

    run_auto_uv_foreground_command(
        args,
        gpu_index=0,
        config_path="/tmp/config.toml",
        auto_uv_runtime_options={},
        interactive=True,
        program_file="/tmp/penguin_burner.py",
        journal_hours=4,
        prompt_yes_no=prompt_yes_no,
        dependencies=AutoUvForegroundDependencies(
            require_auto_uv_initial_check=lambda **_kwargs: None,
            build_stability_config=lambda *_args, **_kwargs: object(),
            run_voltage_frequency_undervolt_main_loop=fake_runner,
            read_auto_uv_profiles=lambda: [{"profile_id": "eff"}, {"profile_id": "perf"}],
            resolve_profile_tier_profiles=lambda profiles: {
                "efficiency": profiles[0],
                "balanced": None,
                "performance": profiles[1],
            },
            available_adaptive_tiers=lambda _resolved: ["efficiency", "performance"],
            profile_tier_label=lambda tier: str(tier).title(),
            apply_runtime_intent=lambda intent, **kwargs: applied.append(
                (intent, kwargs)
            ),
            log=logs.append,
        ),
    )

    assert prompts[0] == (
        "Use adaptive Auto-UV instead of the single discovered profile "
        "for runtime/autostart?",
        False,
    )
    assert prompts[1] == ("Start PenguinBurner runtime now with adaptive Auto-UV?", True)
    assert prompts[2] == ("Persist adaptive Auto-UV for startup?", False)
    assert applied[0][0]["adaptive_auto_uv"] is True
    assert applied[0][1]["persist_on_startup"] is False
    output = "\n".join(logs)
    assert "Adaptive Auto-UV is also available" in output
    assert f"{PENGUIN_BURNER_WRAPPER} %command%" in output


def test_auto_uv_foreground_command_offers_optional_adaptive_when_one_tier_exists() -> None:
    args = SimpleNamespace(
        auto_uv_voltage_scan=True,
        json_events=False,
    )
    logs = []
    prompts = []
    applied = []
    answers = iter([True, True, False])

    def fake_runner(**_kwargs):
        return SimpleNamespace(
            success=True,
            final_voltage_mv=900,
            lock_clock_mhz=2800,
            final_power_w=None,
            final_temperature_c=None,
            final_fan_speed_pct=None,
            stop_reason="verified",
            failed_candidate_voltage_mv=None,
        )

    def prompt_yes_no(prompt, *, default):
        prompts.append((prompt, default))
        return next(answers)

    run_auto_uv_foreground_command(
        args,
        gpu_index=0,
        config_path="/tmp/config.toml",
        auto_uv_runtime_options={},
        interactive=True,
        program_file="/tmp/penguin_burner.py",
        journal_hours=4,
        prompt_yes_no=prompt_yes_no,
        dependencies=AutoUvForegroundDependencies(
            require_auto_uv_initial_check=lambda **_kwargs: None,
            build_stability_config=lambda *_args, **_kwargs: object(),
            run_voltage_frequency_undervolt_main_loop=fake_runner,
            read_auto_uv_profiles=lambda: [{"profile_id": "bal"}],
            resolve_profile_tier_profiles=lambda profiles: {"balanced": profiles[0]},
            available_adaptive_tiers=lambda _resolved: ["balanced"],
            profile_tier_label=lambda tier: str(tier).title(),
            apply_runtime_intent=lambda intent, **kwargs: applied.append(
                (intent, kwargs)
            ),
            log=logs.append,
        ),
    )

    assert prompts[0] == (
        "Use adaptive Auto-UV instead of the single discovered profile "
        "for runtime/autostart?",
        False,
    )
    assert applied[0][0]["adaptive_auto_uv"] is True
    assert "Adaptive Auto-UV is also available" in "\n".join(logs)


def test_auto_uv_foreground_command_applies_through_daemon_without_root() -> None:
    args = SimpleNamespace(
        auto_uv_voltage_scan=True,
        json_events=False,
    )
    logs = []
    applied = []

    def fake_runner(**_kwargs):
        return SimpleNamespace(
            success=True,
            final_voltage_mv=875,
            lock_clock_mhz=2700,
            final_power_w=None,
            final_temperature_c=None,
            final_fan_speed_pct=None,
            stop_reason="verified",
            failed_candidate_voltage_mv=None,
        )

    run_auto_uv_foreground_command(
        args,
        gpu_index=0,
        config_path="/tmp/config.toml",
        auto_uv_runtime_options={},
        interactive=True,
        program_file="/tmp/penguin_burner.py",
        prompt_yes_no=lambda _prompt, *, default: default,
        dependencies=AutoUvForegroundDependencies(
            require_auto_uv_initial_check=lambda **_kwargs: None,
            build_stability_config=lambda *_args, **_kwargs: object(),
            run_voltage_frequency_undervolt_main_loop=fake_runner,
            read_auto_uv_profiles=lambda: [],
            apply_runtime_intent=lambda intent, **kwargs: applied.append(
                (intent, kwargs)
            ),
            log=logs.append,
        ),
    )

    assert applied[0][0]["profile_selector"] == "875mv-2700mhz"
    assert applied[0][1]["persist_on_startup"] is False
    assert "pkexec" not in "\n".join(logs)


def test_auto_uv_optional_runtime_failure_mentions_burnerd() -> None:
    logs = []
    dependencies = AutoUvForegroundDependencies(
        apply_runtime_intent=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("socket unavailable")
        ),
        log=logs.append,
    )

    from auto_uv.run.cli_runtime import _run_optional_runtime_action

    _run_optional_runtime_action(
        persist_on_startup=False,
        runtime_argv=["--auto-uv-profile", "profile-a"],
        deps=dependencies,
    )

    assert logs == [
        "Could not apply optional runtime action through burnerd: socket unavailable"
    ]


def test_format_auto_uv_final_state_uses_na_for_missing_metrics() -> None:
    text = format_auto_uv_final_state(
        SimpleNamespace(
            lock_clock_mhz=2700,
            final_voltage_mv=875,
            final_power_w=None,
            final_temperature_c=None,
            final_fan_speed_pct=None,
            stop_reason="verified",
            failed_candidate_voltage_mv=None,
        )
    )

    assert text == (
        "Auto-UV final state: 2700MHz@875mV "
        "power=n/aW temp=n/aC fan=n/a% stop_reason=verified "
        "failed_candidate=none"
    )
