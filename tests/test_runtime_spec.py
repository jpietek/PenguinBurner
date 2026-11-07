from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cli.runtime_config_file import default_runtime_config
from profiles.uv.runtime_auto_uv_profile import load_auto_uv_final_curve
from runtime import daemon_client, runtime_spec


def test_build_runtime_spec_rejects_non_mapping_intent_as_type_error() -> None:
    with pytest.raises(TypeError, match="runtime intent JSON must be an object"):
        runtime_spec.build_runtime_spec_from_intent([])  # type: ignore[arg-type]


def _curve(profile_id: str, tier: str = "balanced") -> dict:
    return {
        "path": f"/tmp/auto-uv-profile-{profile_id}.json",
        "profile_id": profile_id,
        "profile_tier": tier.title(),
        "profile_tier_key": tier,
        "plan": [
            {
                "index": 12,
                "voltage_mv": 900,
                "base_mhz": 2700,
                "target_mhz": 2800,
                "new_offset_mhz": 100,
            }
        ],
        "lock_clock_mhz": 2800,
        "candidate_voltage_mv": 900,
        "memory_offset_mhz": 1500,
        "power_limit_w": 320,
        "flatten_target": {
            "source": "auto-uv-final",
            "lock_clock_mhz": 2800,
            "lock_voltage_mv": 900,
            "end_voltage_mv": 900,
            "tail_point_count": 1,
        },
    }


def _stub_runtime_sources(monkeypatch, *, curve=None) -> None:
    config = default_runtime_config()
    config["gpu"]["index"] = 2
    config["gpu"]["enable_persistence_mode"] = False
    monkeypatch.setattr(runtime_spec, "load_runtime_config", lambda: (config, None))
    monkeypatch.setattr(
        runtime_spec,
        "require_daemon_capabilities",
        lambda *required, **kwargs: {"capabilities": list(required)},
    )
    monkeypatch.setattr(
        runtime_spec,
        "gpu_capabilities",
        lambda index, **kwargs: {
            "identity": {
                "uuid": f"GPU-test-{index}",
                "pci_bus_id": "0000:02:00.0",
                "name": "Test GPU",
            }
        },
    )
    monkeypatch.setattr(
        runtime_spec,
        "load_auto_uv_final_curve",
        lambda _selector="": curve,
    )
    monkeypatch.setattr(
        runtime_spec,
        "load_overlay_config",
        lambda: SimpleNamespace(enabled=True, update_interval_s=2),
    )


def test_build_static_runtime_spec_resolves_gpu_and_profile(monkeypatch) -> None:
    _stub_runtime_sources(monkeypatch, curve=_curve("balanced-new"))

    spec = runtime_spec.build_runtime_spec(profile_selector="balanced-new")

    assert spec["mode"] == "static"
    assert spec["gpu"] == {
        "uuid": "GPU-test-2",
        "index_at_resolution": 2,
        "pci_bus_id": "0000:02:00.0",
        "name": "Test GPU",
    }
    assert spec["static_profile"]["profile_id"] == "balanced-new"
    assert spec["static_profile"]["plan"][0]["new_offset_mhz"] == 100
    assert spec["static_profile"]["power_limit_w"] == 320
    assert spec["policy"]["enable_persistence_mode"] is False
    assert spec["overlay"] == {"enabled": True, "update_interval_s": 2}


def test_bound_profile_resolves_current_index_from_uuid_after_reordering(
    monkeypatch,
) -> None:
    curve = _curve("gpu-a-profile")
    curve["gpu_identity"] = {
        "name": "Test GPU",
        "uuid": "GPU-bound-a",
        "pci_bus_id": "0000:01:00.0",
        "pci_device_id": "0x123410DE",
        "index_at_verification": 0,
    }
    _stub_runtime_sources(monkeypatch, curve=curve)
    monkeypatch.setattr(
        runtime_spec.DaemonGpuClient,
        "discover_identities",
        lambda: [
            SimpleNamespace(index=0, uuid="GPU-other"),
            SimpleNamespace(index=1, uuid="GPU-bound-a"),
        ],
    )
    monkeypatch.setattr(
        runtime_spec,
        "gpu_capabilities",
        lambda index, **_kwargs: {
            "identity": {
                "uuid": "GPU-bound-a" if int(index) == 1 else "GPU-other",
                "pci_bus_id": "0000:01:00.0",
                "name": "Test GPU",
            }
        },
    )

    spec = runtime_spec.build_runtime_spec(profile_selector="gpu-a-profile")

    assert spec["gpu"]["uuid"] == "GPU-bound-a"
    assert spec["gpu"]["index_at_resolution"] == 1


def test_explicit_gpu_index_cannot_override_bound_profile_uuid(monkeypatch) -> None:
    curve = _curve("gpu-a-profile")
    curve["gpu_identity"] = {"uuid": "GPU-bound-a", "name": "Test GPU"}
    _stub_runtime_sources(monkeypatch, curve=curve)
    monkeypatch.setattr(
        runtime_spec.DaemonGpuClient,
        "discover_identities",
        lambda: [
            SimpleNamespace(index=0, uuid="GPU-bound-a"),
            SimpleNamespace(index=1, uuid="GPU-other"),
        ],
    )

    with pytest.raises(RuntimeError, match="not requested GPU 1"):
        runtime_spec.build_runtime_spec(
            profile_selector="gpu-a-profile",
            gpu_index=1,
        )


@pytest.mark.parametrize(
    ("bound_uuid", "gpu_index", "expected_index", "error"),
    [
        ("GPU-test-1", None, 1, None),
        ("GPU-test-1", 1, 1, None),
        ("GPU-test-1", 0, None, "not requested GPU 0"),
        ("GPU-missing", None, None, "profile GPU GPU-missing is not currently detected"),
        (None, None, 2, None),
    ],
)
def test_saved_profile_gpu_binding_reaches_runtime_spec(
    monkeypatch, tmp_path, bound_uuid, gpu_index, expected_index, error
) -> None:
    payload = _curve("saved-profile")
    payload["final_verified"] = True
    if bound_uuid is not None:
        payload["gpu_identity"] = {
            "uuid": bound_uuid,
            "index_at_verification": 0,
        }
    path = tmp_path / "saved-profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    _stub_runtime_sources(monkeypatch)
    # Keep the real resolver and loader: mocking their output hid the lost binding.
    monkeypatch.setattr(runtime_spec, "load_auto_uv_final_curve", load_auto_uv_final_curve)
    monkeypatch.setattr(
        runtime_spec.DaemonGpuClient,
        "discover_identities",
        lambda: [SimpleNamespace(index=i, uuid=f"GPU-test-{i}") for i in range(3)],
    )

    if error is not None:
        with pytest.raises(RuntimeError, match=error):
            runtime_spec.build_runtime_spec(profile_selector=str(path), gpu_index=gpu_index)
    else:
        spec = runtime_spec.build_runtime_spec(
            profile_selector=str(path), gpu_index=gpu_index
        )
        assert spec["gpu"]["uuid"] == f"GPU-test-{expected_index}"
        assert spec["gpu"]["index_at_resolution"] == expected_index
        assert spec["static_profile"]["profile_id"] == "saved-profile"


def test_single_gpu_adaptive_runtime_includes_legacy_tiers(monkeypatch) -> None:
    curve = _curve("legacy-balanced")
    _stub_runtime_sources(monkeypatch, curve=curve)
    monkeypatch.setattr(
        runtime_spec.DaemonGpuClient,
        "discover_identities",
        lambda: [SimpleNamespace(index=0, uuid="GPU-test-0")],
    )
    calls: list[dict[str, object]] = []

    def adaptive(selected_curve, **kwargs):
        calls.append({"selected_curve": selected_curve, **kwargs})
        return {"initial_tier": "balanced"}

    monkeypatch.setattr(runtime_spec, "_adaptive_spec", adaptive)

    spec = runtime_spec.build_runtime_spec(
        profile_selector="legacy-balanced",
        adaptive_auto_uv=True,
        gpu_index=0,
    )

    assert spec["mode"] == "adaptive"
    assert calls[0]["include_legacy_profiles"] is True


def test_runtime_spec_leaves_power_capability_decision_to_daemon(monkeypatch) -> None:
    legacy_curve = _curve("legacy-laptop")
    legacy_curve["power_limit_w"] = 150
    _stub_runtime_sources(monkeypatch, curve=legacy_curve)
    monkeypatch.setattr(
        runtime_spec,
        "gpu_capabilities",
        lambda index, **kwargs: {
            "identity": {
                "uuid": f"GPU-laptop-{index}",
                "pci_bus_id": "0000:01:00.0",
                "pci_device_id": "0x271710DE",
                "name": "NVIDIA GeForce RTX 4090 Laptop GPU",
            },
            "power_limits": {
                "power_limit_w": None,
                "power_limit_default_w": 150,
                "power_limit_min_w": 5,
                "power_limit_max_w": 175,
            },
        },
    )
    spec = runtime_spec.build_runtime_spec(profile_selector="legacy-laptop")

    # Resolution preserves the saved intent. The daemon attempts the write and
    # skips only NVML_ERROR_NOT_SUPPORTED, without guessing from this name.
    assert spec["static_profile"]["power_limit_w"] == 150
    assert spec["static_profile"]["memory_offset_mhz"] == 1500
    assert spec["static_profile"]["plan"][0]["new_offset_mhz"] == 100

def test_build_static_runtime_spec_resolves_latest_profile_for_empty_selector(
    monkeypatch,
) -> None:
    _stub_runtime_sources(monkeypatch, curve=_curve("latest-profile"))

    spec = runtime_spec.build_runtime_spec(profile_selector="")

    assert spec["mode"] == "static"
    assert spec["static_profile"]["profile_id"] == "latest-profile"


def test_build_runtime_spec_uses_requested_daemon_socket(monkeypatch) -> None:
    _stub_runtime_sources(monkeypatch, curve=None)
    calls: list[tuple[str, object]] = []

    def fake_require(*required, socket_path=None, **kwargs):
        calls.append(("require", socket_path))
        return {"capabilities": list(required)}

    def fake_capabilities(index, *, socket_path=None, **kwargs):
        calls.append(("gpu", socket_path))
        return {
            "identity": {
                "uuid": f"GPU-test-{index}",
                "pci_bus_id": "0000:02:00.0",
                "name": "Test GPU",
            }
        }

    monkeypatch.setattr(runtime_spec, "require_daemon_capabilities", fake_require)
    monkeypatch.setattr(runtime_spec, "gpu_capabilities", fake_capabilities)

    runtime_spec.build_runtime_spec(socket_path="/tmp/test-burnerd.sock")

    assert calls == [
        ("require", "/tmp/test-burnerd.sock"),
        ("gpu", "/tmp/test-burnerd.sock"),
    ]


def test_apply_runtime_intent_uses_daemon_for_apply_and_boot(monkeypatch) -> None:
    calls = []
    spec = {"format_version": 1, "mode": "stock"}
    monkeypatch.setattr(
        runtime_spec,
        "build_runtime_spec_from_intent",
        lambda intent, **kwargs: calls.append(("resolve", intent, kwargs)) or spec,
    )
    monkeypatch.setattr(
        daemon_client,
        "apply_runtime_spec",
        lambda payload, **kwargs: calls.append(("apply", payload, kwargs))
        or {"started": True},
    )
    monkeypatch.setattr(
        daemon_client,
        "set_boot_runtime_spec",
        lambda payload, **kwargs: calls.append(("boot", payload, kwargs))
        or {"saved": True},
    )

    result = daemon_client.apply_runtime_intent(
        {"profile_selector": "__stock__"},
        persist_on_startup=True,
        socket_path="/tmp/burnerd.sock",
    )

    assert result == {"started": True}
    assert [call[0] for call in calls] == ["resolve", "apply", "boot"]
    assert all(call[2]["socket_path"] == "/tmp/burnerd.sock" for call in calls)


def test_apply_runtime_intent_session_only_clears_boot(monkeypatch) -> None:
    calls = []
    spec = {
        "format_version": 1,
        "mode": "profile",
        "gpu": {"uuid": "GPU-B", "index_at_resolution": 1},
    }
    monkeypatch.setattr(
        runtime_spec,
        "build_runtime_spec_from_intent",
        lambda intent, **kwargs: calls.append(("resolve", intent, kwargs)) or spec,
    )
    monkeypatch.setattr(
        daemon_client,
        "apply_runtime_spec",
        lambda payload, **kwargs: calls.append(("apply", payload, kwargs))
        or {"started": True},
    )
    monkeypatch.setattr(
        daemon_client,
        "set_boot_runtime_spec",
        lambda payload, **kwargs: calls.append(("boot", payload, kwargs))
        or {"saved": True},
    )
    monkeypatch.setattr(
        daemon_client,
        "clear_boot_runtime_spec",
        lambda **kwargs: calls.append(("clear-boot", None, kwargs))
        or {"cleared": True},
    )

    result = daemon_client.apply_runtime_intent(
        {"profile_selector": "perf"},
        persist_on_startup=False,
        clear_boot=True,
        socket_path="/tmp/burnerd.sock",
    )

    assert result == {"started": True}
    assert [call[0] for call in calls] == ["resolve", "apply", "clear-boot"]
    assert calls[-1][2]["gpu_uuid"] == "GPU-B"


def test_daemon_client_boot_diagnostics_and_targeted_clear(monkeypatch, capsys) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        daemon_client,
        "boot_runtime_spec",
        lambda **kwargs: {
            "configured": True,
            "replay": [{"gpu_uuid": "GPU-A", "outcome": "active"}],
        },
    )
    monkeypatch.setattr(
        daemon_client,
        "clear_boot_runtime_spec",
        lambda *, gpu_uuid="", **kwargs: calls.append(("clear", gpu_uuid))
        or {"cleared": True, "gpu_uuid": gpu_uuid},
    )

    assert daemon_client.main(["boot-runtime-spec"]) == 0
    assert '"outcome": "active"' in capsys.readouterr().out
    assert (
        daemon_client.main(
            ["clear-boot-runtime-spec", "--gpu-uuid", "GPU-A"]
        )
        == 0
    )
    assert calls == [("clear", "GPU-A")]


def test_daemon_client_sets_and_clears_main_gpu(monkeypatch, capsys) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        daemon_client,
        "set_boot_main_gpu",
        lambda gpu_uuid, **_kwargs: calls.append(gpu_uuid)
        or {"selected": bool(gpu_uuid), "main_gpu_uuid": gpu_uuid},
    )

    assert (
        daemon_client.main(["set-boot-main-gpu", "--gpu-uuid", "GPU-A"])
        == 0
    )
    assert '"main_gpu_uuid": "GPU-A"' in capsys.readouterr().out
    assert daemon_client.main(["set-boot-main-gpu"]) == 0
    assert calls == ["GPU-A", ""]


def test_adaptive_runtime_keeps_explicit_old_profile_as_initial_tier(monkeypatch) -> None:
    selected = _curve("balanced-old")
    curves = {
        "balanced-old": selected,
        "balanced-new": _curve("balanced-new"),
        "eff-new": _curve("eff-new", "efficiency"),
    }
    _stub_runtime_sources(monkeypatch, curve=selected)
    monkeypatch.setattr(runtime_spec, "read_auto_uv_profiles", lambda: [{}])
    monkeypatch.setattr(
        runtime_spec,
        "resolve_profile_tier_profiles",
        lambda _profiles, **_kwargs: {
            "efficiency": {"profile_id": "eff-new"},
            "balanced": {"profile_id": "balanced-new"},
            "performance": None,
        },
    )
    monkeypatch.setattr(
        runtime_spec,
        "available_adaptive_tiers",
        lambda _resolved: ["efficiency", "balanced"],
    )
    monkeypatch.setattr(
        runtime_spec,
        "load_auto_uv_final_curve",
        lambda selector="": curves.get(selector, selected),
    )

    spec = runtime_spec.build_runtime_spec(
        profile_selector="balanced-old",
        adaptive_auto_uv=True,
    )

    assert spec["mode"] == "adaptive"
    assert spec["adaptive"]["initial_tier"] == "balanced"
    assert spec["adaptive"]["profiles"]["balanced"]["profile_id"] == "balanced-old"
    assert spec["adaptive"]["profiles"]["efficiency"]["profile_id"] == "eff-new"


def test_adaptive_runtime_accepts_one_profile_without_switching(monkeypatch) -> None:
    only_curve = _curve("balanced-only")
    _stub_runtime_sources(monkeypatch, curve=only_curve)
    monkeypatch.setattr(runtime_spec, "read_auto_uv_profiles", lambda: [{}])
    monkeypatch.setattr(
        runtime_spec,
        "resolve_profile_tier_profiles",
        lambda _profiles, **_kwargs: {"balanced": {"profile_id": "balanced-only"}},
    )
    monkeypatch.setattr(
        runtime_spec,
        "available_adaptive_tiers",
        lambda _resolved: ["balanced"],
    )

    spec = runtime_spec.build_runtime_spec(
        profile_selector="balanced-only",
        adaptive_auto_uv=True,
    )

    assert spec["mode"] == "adaptive"
    assert spec["adaptive"]["initial_tier"] == "balanced"
    assert list(spec["adaptive"]["profiles"]) == ["balanced"]
    assert spec["adaptive"]["profiles"]["balanced"]["profile_id"] == "balanced-only"


@pytest.mark.parametrize(
    ("tiers", "expected_initial"),
    [
        (["efficiency", "balanced", "performance"], "performance"),
        (["efficiency", "balanced"], "balanced"),
        (["efficiency"], "efficiency"),
    ],
)
def test_adaptive_without_explicit_profile_starts_at_fastest_available_tier(
    monkeypatch,
    tiers,
    expected_initial,
) -> None:
    curves = {tier: _curve(f"{tier}-profile", tier) for tier in tiers}
    _stub_runtime_sources(monkeypatch)
    monkeypatch.setattr(runtime_spec, "read_auto_uv_profiles", lambda: [{}])
    monkeypatch.setattr(
        runtime_spec,
        "resolve_profile_tier_profiles",
        lambda _profiles, **_kwargs: {
            tier: {"profile_id": f"{tier}-profile"} for tier in tiers
        },
    )
    monkeypatch.setattr(
        runtime_spec,
        "available_adaptive_tiers",
        lambda _resolved: tiers,
    )
    monkeypatch.setattr(
        runtime_spec,
        "load_auto_uv_final_curve",
        lambda selector="": curves.get(selector.removesuffix("-profile")),
    )

    spec = runtime_spec.build_runtime_spec(
        profile_selector="",
        adaptive_auto_uv=True,
    )

    assert spec["mode"] == "adaptive"
    assert spec["adaptive"]["initial_tier"] == expected_initial


def test_adaptive_runtime_uses_per_game_target_fps_override(monkeypatch) -> None:
    only_curve = _curve("balanced-only")
    _stub_runtime_sources(monkeypatch, curve=only_curve)
    monkeypatch.setattr(runtime_spec, "read_auto_uv_profiles", lambda: [{}])
    monkeypatch.setattr(
        runtime_spec,
        "resolve_profile_tier_profiles",
        lambda _profiles, **_kwargs: {"balanced": {"profile_id": "balanced-only"}},
    )
    monkeypatch.setattr(
        runtime_spec,
        "available_adaptive_tiers",
        lambda _resolved: ["balanced"],
    )

    spec = runtime_spec.build_runtime_spec(
        profile_selector="balanced-only",
        adaptive_auto_uv=True,
        adaptive_target_fps=120.0,
    )

    assert spec["mode"] == "adaptive"
    assert spec["adaptive"]["policy"]["target_fps"] == 120.0


def _adaptive_policy_payload(monkeypatch) -> dict:
    only_curve = _curve("balanced-only")
    _stub_runtime_sources(monkeypatch, curve=only_curve)
    monkeypatch.setattr(runtime_spec, "read_auto_uv_profiles", lambda: [{}])
    monkeypatch.setattr(
        runtime_spec,
        "resolve_profile_tier_profiles",
        lambda _profiles, **_kwargs: {"balanced": {"profile_id": "balanced-only"}},
    )
    monkeypatch.setattr(
        runtime_spec,
        "available_adaptive_tiers",
        lambda _resolved: ["balanced"],
    )
    spec = runtime_spec.build_runtime_spec(
        profile_selector="balanced-only",
        adaptive_auto_uv=True,
    )
    return spec["adaptive"]["policy"]


def test_default_frame_cap_and_idle_knobs_are_omitted_from_the_payload(
    monkeypatch,
) -> None:
    """Knobs at their defaults stay out of the wire spec.

    The daemon fills in identical defaults, and a still-running 0.7.x daemon
    (deny_unknown_fields) must keep accepting a default-config runtime until
    its binary is restarted after an upgrade.
    """
    for env in (
        "PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_ENTER_GPU_PCT",
        "PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_EXIT_GPU_PCT",
        "PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_CONFIRM_WINDOWS",
        "PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_EXIT_PACING_PCT",
        "PENGUIN_BURNER_ADAPTIVE_DESKTOP_IDLE_GPU_PCT",
        "PENGUIN_BURNER_ADAPTIVE_DESKTOP_IDLE_AFTER_S",
    ):
        monkeypatch.delenv(env, raising=False)

    policy = _adaptive_policy_payload(monkeypatch)

    assert "frame_cap_enter_gpu_pct" not in policy
    assert "frame_cap_exit_gpu_pct" not in policy
    assert "frame_cap_confirm_windows" not in policy
    assert "frame_cap_exit_pacing_pct" not in policy
    assert "desktop_idle_gpu_pct" not in policy
    assert "desktop_idle_after_s" not in policy


def test_overridden_frame_cap_knob_is_sent_and_the_rest_stay_omitted(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_ENTER_GPU_PCT", "45")

    policy = _adaptive_policy_payload(monkeypatch)

    assert policy["frame_cap_enter_gpu_pct"] == 45.0
    assert "frame_cap_exit_gpu_pct" not in policy
    assert "desktop_idle_after_s" not in policy


def test_runtime_intent_from_argv_parses_adaptive_target_fps() -> None:
    intent = runtime_spec.runtime_intent_from_argv(
        [
            "--auto-uv-profile",
            "latest",
            "--adaptive-auto-uv",
            "--adaptive-target-fps",
            "120",
        ]
    )

    assert intent["adaptive_auto_uv"] is True
    assert intent["adaptive_target_fps"] == 120.0
    assert runtime_spec.runtime_intent_from_argv(
        ["--adaptive-auto-uv", "--adaptive-target-fps=90"]
    )["adaptive_target_fps"] == 90.0
    assert (
        runtime_spec.runtime_intent_from_argv(["--adaptive-auto-uv"])[
            "adaptive_target_fps"
        ]
        is None
    )


def test_runtime_intent_from_argv_parses_the_telemetry_opt_out() -> None:
    # Capture is the default; the wrapper passes the flag only to opt out.
    assert runtime_spec.runtime_intent_from_argv([])["game_telemetry"] is True
    assert (
        runtime_spec.runtime_intent_from_argv(["--no-game-telemetry"])[
            "game_telemetry"
        ]
        is False
    )


def test_saved_fan_curve_is_resolved_before_daemon_apply(monkeypatch, tmp_path) -> None:
    path = tmp_path / "auto-uv-fan-curve.json"
    path.write_text(
        """{
          "loaded_temperature_c": 70,
          "fan": {
            "curve": [[45, 0], [60, 30], [75, 60], [80, 75], [90, 100]],
            "poll_interval_s": 1
          }
        }""",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_spec, "auto_uv_fan_curve_payload_path", lambda: path)

    fan = runtime_spec._fan_spec(default_runtime_config(), enabled=True)

    assert fan["enabled"] is True
    assert fan["config"]["curve"][0] == [45.0, 0.0]
    assert fan["config"]["curve_source_path"] == str(path)


def test_blocked_fan_curve_becomes_explicit_disabled_notice(monkeypatch, tmp_path) -> None:
    path = tmp_path / "auto-uv-fan-curve.json"
    path.write_text(
        '{"fan_curve_blocked":true,"block_reason":"too-hot"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_spec, "auto_uv_fan_curve_payload_path", lambda: path)

    fan = runtime_spec._fan_spec(default_runtime_config(), enabled=True)

    assert fan["enabled"] is False
    assert "too-hot" in fan["notice"]


def test_runtime_intent_argv_bridge_is_python_only_and_strict() -> None:
    assert runtime_spec.runtime_intent_from_argv(
        [
            "--auto-uv-profile=profile-a",
            "--silent-fan-curve",
            "--adaptive-auto-uv",
            "--gpu-index",
            "3",
        ]
    ) == {
        "profile_selector": "profile-a",
        "silent_fan_curve": True,
        "adaptive_auto_uv": True,
        "adaptive_target_fps": None,
        "gpu_index": 3,
        "game_telemetry": True,
    }
    with pytest.raises(RuntimeError, match="unsupported runtime profile argument"):
        runtime_spec.runtime_intent_from_argv(["--daemon-api"])


def test_profile_spec_carries_power_metrics_only_when_present(monkeypatch) -> None:
    """The daemon's energy-saved accounting rate rides in the spec; profiles
    without scan power metrics keep the exact old spec shape."""
    with_power = _curve("with-power")
    with_power["avg_power_w"] = 309.15
    with_power["base_avg_power_w"] = 338.64
    _stub_runtime_sources(monkeypatch, curve=with_power)
    spec = runtime_spec.build_runtime_spec(profile_selector="with-power")
    assert spec["static_profile"]["avg_power_w"] == 309.15
    assert spec["static_profile"]["base_avg_power_w"] == 338.64

    _stub_runtime_sources(monkeypatch, curve=_curve("no-power"))
    spec = runtime_spec.build_runtime_spec(profile_selector="no-power")
    assert "avg_power_w" not in spec["static_profile"]
    assert "base_avg_power_w" not in spec["static_profile"]

    # Junk values (zero, negative, non-numeric) are dropped, not sent.
    junk = _curve("junk-power")
    junk["avg_power_w"] = 0
    junk["base_avg_power_w"] = "n/a"
    _stub_runtime_sources(monkeypatch, curve=junk)
    spec = runtime_spec.build_runtime_spec(profile_selector="junk-power")
    assert "avg_power_w" not in spec["static_profile"]
    assert "base_avg_power_w" not in spec["static_profile"]


def test_adaptive_without_explicit_profile_includes_legacy_tiers_on_single_gpu(
    monkeypatch,
) -> None:
    _stub_runtime_sources(monkeypatch)
    monkeypatch.setattr(
        runtime_spec.DaemonGpuClient,
        "discover_identities",
        lambda: [SimpleNamespace(index=2, uuid="GPU-test-2")],
    )
    calls: list[dict[str, object]] = []

    def adaptive(selected_curve, **kwargs):
        calls.append({"selected_curve": selected_curve, **kwargs})
        return {"initial_tier": "balanced"}

    monkeypatch.setattr(runtime_spec, "_adaptive_spec", adaptive)

    spec = runtime_spec.build_runtime_spec(
        profile_selector="",
        adaptive_auto_uv=True,
    )

    assert spec["mode"] == "adaptive"
    assert calls[0]["selected_curve"] is None
    assert calls[0]["include_legacy_profiles"] is True


def test_adaptive_request_fails_closed_instead_of_degrading_to_stock(
    monkeypatch,
) -> None:
    _stub_runtime_sources(monkeypatch)
    monkeypatch.setattr(
        runtime_spec.DaemonGpuClient,
        "discover_identities",
        lambda: [SimpleNamespace(index=2, uuid="GPU-test-2")],
    )
    monkeypatch.setattr(runtime_spec, "_adaptive_spec", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="adaptive Auto-UV was requested"):
        runtime_spec.build_runtime_spec(
            profile_selector="",
            adaptive_auto_uv=True,
        )
