from __future__ import annotations

from types import SimpleNamespace

import pytest

from cli.runtime_config_file import default_runtime_config
from runtime import daemon_client
from runtime import runtime_spec


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
    assert spec["policy"]["enable_persistence_mode"] is False
    assert spec["overlay"] == {"enabled": True, "update_interval_s": 2}


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
    spec = {"format_version": 1, "mode": "profile"}
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
        lambda _profiles: {
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
        lambda _profiles: {"balanced": {"profile_id": "balanced-only"}},
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
        lambda _profiles: {
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
        lambda _profiles: {"balanced": {"profile_id": "balanced-only"}},
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
