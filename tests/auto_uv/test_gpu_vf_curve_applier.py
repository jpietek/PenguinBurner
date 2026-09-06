from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from auto_uv.domain.types import AutoUvPowerLimitApplyError
from auto_uv.gpu import gpu_vf_curve_applier
from runtime.support import nvidia_runtime_defaults as runtime_defaults


_POWER_LIMIT_ERROR = (
    "nvmlDeviceSetPowerManagementLimit failed with NVML error 3: Not Supported"
)


def _patch_applier_environment(monkeypatch, gpu_client_type) -> None:
    if not hasattr(gpu_client_type, "capabilities"):
        def fake_capabilities(self, *, refresh: bool = False):
            _ = refresh
            applied_w = (
                self.power_limit_calls[-1]
                if getattr(self, "power_limit_calls", [])
                else 360
            )
            return SimpleNamespace(
                identity=SimpleNamespace(
                    index=self.gpu_index,
                    uuid=f"GPU-test-{self.gpu_index}",
                    name="NVIDIA GeForce RTX Test",
                    pci_bus_id=f"0000:{self.gpu_index + 1:02x}:00.0",
                    pci_device_id="0x1234",
                ),
                power=SimpleNamespace(
                    current_w=applied_w,
                    enforced_w=applied_w,
                ),
            )

        monkeypatch.setattr(
            gpu_client_type,
            "capabilities",
            fake_capabilities,
            raising=False,
        )
    monkeypatch.setattr(gpu_vf_curve_applier, "DaemonGpuClient", gpu_client_type)
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "reset_nvidia_runtime_defaults",
        lambda **_kwargs: {
            "plan": [{"index": 0, "voltage_mv": 900, "target_mhz": 2500}],
            "gpu_name": "NVIDIA GeForce RTX 5080",
            "power_limit_w": 360,
            "power_limit_set_supported": True,
        },
    )
    monkeypatch.setattr(gpu_vf_curve_applier, "apply_plan", lambda *_args: None)
    monkeypatch.setattr(
        gpu_vf_curve_applier, "assert_zero_runtime_vf_offsets", lambda *_args: None
    )


def _patch_power_environment(
    monkeypatch,
    *,
    error: str | None = None,
    power_limit_supported: bool = True,
    power_limit_probe_error: Exception | None = None,
):
    clients = []

    class FakeGpuClient:
        def __init__(self, *, gpu_index: int) -> None:
            self.gpu_index = int(gpu_index)
            self.power_limit_calls: list[int] = []
            self.power_limit_probe_calls = 0
            clients.append(self)

        def refresh_points(self) -> None:
            pass

        def apply_power_limit_w(self, power_limit_w):
            self.power_limit_calls.append(int(power_limit_w))
            if error is not None:
                raise RuntimeError(error)
            return int(power_limit_w)

        def power_limit_set_supported(self) -> bool:
            self.power_limit_probe_calls += 1
            if power_limit_probe_error is not None:
                raise power_limit_probe_error
            return bool(power_limit_supported)

    _patch_applier_environment(monkeypatch, FakeGpuClient)
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "auto_uv_memory_offset_mhz",
        lambda *_args, **_kwargs: (None, None),
    )
    return clients


def test_open_live_gpu_applier_applies_raised_auto_uv_power_limit(monkeypatch) -> None:
    logs: list[str] = []
    controllers = _patch_power_environment(monkeypatch)

    applier = gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={"auto_uv_power_limit_w": 390},
        log=logs.append,
    )

    assert controllers[0].power_limit_calls == [390]
    assert applier.power_limit_w == 390
    assert applier.baseline_power_limit_w == 360
    assert applier.requested_power_limit_w == 390
    assert applier.translated_gpu_policy["power_limit_w"] == 390
    assert logs == [
        "Auto-UV power limit: applied 390W for discovery and sweep"
    ]

    applier.apply_requested_power_limit(log=logs.append)

    assert controllers[0].power_limit_calls == [390]
    assert applier.power_limit_w == 390
    assert applier.translated_gpu_policy["power_limit_w"] == 390
    assert logs == [
        "Auto-UV power limit: applied 390W for discovery and sweep"
    ]


def test_open_live_gpu_applier_applies_reduced_auto_uv_power_limit_for_scan(
    monkeypatch,
) -> None:
    logs: list[str] = []
    controllers = _patch_power_environment(monkeypatch)

    applier = gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={"auto_uv_power_limit_w": 319},
        log=logs.append,
    )

    assert controllers[0].power_limit_calls == [319]
    assert applier.power_limit_w == 319
    assert applier.baseline_power_limit_w == 360
    assert applier.requested_power_limit_w == 319
    assert applier.translated_gpu_policy["power_limit_w"] == 319
    assert logs == [
        "Auto-UV power limit: applied 319W for discovery and sweep"
    ]

    applier.apply_requested_power_limit(log=logs.append)

    assert controllers[0].power_limit_calls == [319]
    assert applier.power_limit_w == 319
    assert applier.translated_gpu_policy["power_limit_w"] == 319
    assert logs == [
        "Auto-UV power limit: applied 319W for discovery and sweep"
    ]


def test_open_live_gpu_applier_stops_when_raised_power_limit_is_rejected(
    monkeypatch,
) -> None:
    logs: list[str] = []
    controllers = _patch_power_environment(monkeypatch, error=_POWER_LIMIT_ERROR)

    with pytest.raises(
        AutoUvPowerLimitApplyError,
        match="scan stopped before probing",
    ):
        gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
            gpu_index=0,
            runtime_options={"auto_uv_power_limit_w": 390},
            log=logs.append,
        )

    assert controllers[0].power_limit_calls == [390]
    assert logs == []


def test_unsupported_power_limit_is_platform_managed_and_not_saved(
    monkeypatch,
) -> None:
    logs: list[str] = []
    controllers = _patch_power_environment(
        monkeypatch,
        power_limit_supported=False,
    )
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "reset_nvidia_runtime_defaults",
        lambda **_kwargs: {
            "plan": [{"index": 0, "voltage_mv": 900, "target_mhz": 2490}],
            "gpu_name": "NVIDIA GeForce RTX 4090 Laptop GPU",
            "power_limit_w": 150,
            "power_limit_set_supported": False,
            "power_limits": {
                "power_limit_default_w": 150,
                "power_limit_min_w": 5,
                "power_limit_max_w": 175,
            },
        },
    )
    applier = gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={},
        log=logs.append,
    )

    assert controllers[0].power_limit_calls == []
    assert applier.power_limit_w is None
    assert applier.baseline_power_limit_w is None
    assert applier.requested_power_limit_w is None
    assert "power_limit_w" not in applier.translated_gpu_policy
    assert logs == [
        "Auto-UV power limit: driver reports fixed power-limit writes are unsupported; "
        "continuing without saved power limit"
    ]

    applier.requested_power_limit_w = 150
    assert applier.apply_requested_power_limit(log=logs.append) is None
    assert controllers[0].power_limit_calls == []
    assert logs[-1] == (
        "Auto-UV power limit: skipped unsupported fixed write for final verification; "
        "continuing without saved power limit"
    )


def test_successful_probe_is_authoritative_even_for_mobile_identity_strings(
    monkeypatch,
) -> None:
    logs: list[str] = []
    controllers = _patch_power_environment(monkeypatch)
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "reset_nvidia_runtime_defaults",
        lambda **_kwargs: {
            "plan": [{"index": 0, "voltage_mv": 900, "target_mhz": 2490}],
            "gpu_name": "NVIDIA GeForce RTX 5090",
            "pci_device_id": "0x2C1810DE",
            "power_limit_w": 150,
            "power_limit_set_supported": True,
            "power_limits": {
                "power_limit_default_w": 150,
                "power_limit_min_w": 5,
                "power_limit_max_w": 175,
            },
        },
    )

    applier = gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={"auto_uv_power_limit_w": 175},
        log=logs.append,
    )

    assert controllers[0].power_limit_calls == [175]
    assert applier.power_limit_w == 175
    assert applier.baseline_power_limit_w == 150
    assert applier.requested_power_limit_w == 175
    assert applier.translated_gpu_policy["pci_device_id"] == "0x2C1810DE"
    assert applier.translated_gpu_policy["power_limit_w"] == 175
    assert logs == ["Auto-UV power limit: applied 175W for discovery and sweep"]


def test_unrecognized_rtx_2050_with_unsupported_setter_never_saves_power_limit(
    monkeypatch,
) -> None:
    logs: list[str] = []
    controllers = _patch_power_environment(
        monkeypatch,
        power_limit_supported=False,
    )
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "reset_nvidia_runtime_defaults",
        lambda **_kwargs: {
            "plan": [{"index": 0, "voltage_mv": 900, "target_mhz": 1620}],
            "gpu_name": "NVIDIA GeForce RTX 2050",
            "pci_device_id": "0x25B810DE",
            "power_limit_w": 35,
            "power_limit_set_supported": False,
            "power_limits": {
                "power_limit_default_w": 35,
                "power_limit_min_w": 35,
                "power_limit_max_w": 55,
            },
        },
    )
    applier = gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={},
        log=logs.append,
    )

    assert controllers[0].power_limit_probe_calls == 0
    assert controllers[0].power_limit_calls == []
    assert applier.power_limit_w is None
    assert applier.baseline_power_limit_w is None
    assert "power_limit_w" not in applier.translated_gpu_policy
    assert logs == [
        "Auto-UV power limit: driver reports fixed power-limit writes are unsupported; "
        "continuing without saved power limit"
    ]


@pytest.mark.parametrize("support", [None, "false", 0])
def test_missing_or_invalid_reset_power_support_stops_scan(monkeypatch, support) -> None:
    controllers = _patch_power_environment(monkeypatch)
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "reset_nvidia_runtime_defaults",
        lambda **_kwargs: {
            "plan": [{"index": 0, "voltage_mv": 900, "target_mhz": 2490}],
            "gpu_name": "NVIDIA GeForce RTX 5080",
            "power_limit_w": 360,
            "power_limit_set_supported": support,
        },
    )
    with pytest.raises(AutoUvPowerLimitApplyError, match="did not establish"):
        gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
            gpu_index=0, runtime_options={"auto_uv_power_limit_w": 300},
            log=lambda _message: None,
        )
    assert controllers[0].power_limit_calls == []
    assert controllers[0].power_limit_probe_calls == 0


def test_scan_uses_reset_setter_result_even_when_support_probe_is_deferred(monkeypatch) -> None:
    controllers = _patch_power_environment(
        monkeypatch, power_limit_probe_error=RuntimeError("auto-uv-scan-running")
    )
    applier = gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0, runtime_options={"auto_uv_power_limit_w": 300},
        log=lambda _message: None,
    )
    assert controllers[0].power_limit_probe_calls == 0
    assert controllers[0].power_limit_calls == [300]
    assert applier.power_limit_w == 300


def test_tier_power_limit_rejection_is_fatal_and_preserves_known_cap() -> None:
    logs: list[str] = []

    class FakePolicyController:
        def __init__(self) -> None:
            self.power_limit_calls: list[int] = []

        def apply_power_limit_w(self, power_limit_w):
            self.power_limit_calls.append(int(power_limit_w))
            raise RuntimeError(
                "nvmlDeviceSetPowerManagementLimit failed with NVML error 3: "
                "Not Supported"
            )

    policy_controller = FakePolicyController()
    applier = gpu_vf_curve_applier.LiveGpuVfCurveApplier(
        gpu_index=0,
        gpu=cast(Any, policy_controller),
        runtime_default_plan=[],
        translated_gpu_policy={"power_limit_w": 360},
        baseline_power_limit_w=360,
        requested_power_limit_w=43,
    )

    with pytest.raises(
        AutoUvPowerLimitApplyError,
        match="probing under an unknown power regime",
    ):
        applier.apply_requested_power_limit(log=logs.append)

    assert policy_controller.power_limit_calls == [43]
    assert applier.power_limit_w == 360
    assert applier.requested_power_limit_w is None
    assert applier.translated_gpu_policy["power_limit_w"] == 360
    assert logs == []


def test_power_limit_readback_mismatch_stops_before_policy_is_updated() -> None:
    class FakePolicyController:
        def apply_power_limit_w(self, power_limit_w):
            return int(power_limit_w)

        def capabilities(self, *, refresh):
            assert refresh
            return cast(
                Any,
                type(
                    "Caps",
                    (),
                    {
                        "power": type(
                            "Power",
                            (),
                            {"current_w": 300.0, "enforced_w": 300.0},
                        )()
                    },
                )(),
            )

    applier = gpu_vf_curve_applier.LiveGpuVfCurveApplier(
        gpu_index=0,
        gpu=cast(Any, FakePolicyController()),
        runtime_default_plan=[],
        translated_gpu_policy={"power_limit_w": 360},
        baseline_power_limit_w=360,
        requested_power_limit_w=320,
    )

    with pytest.raises(AutoUvPowerLimitApplyError, match="read-back mismatch"):
        applier.apply_requested_power_limit(log=lambda _message: None)

    assert applier.power_limit_w == 360
    assert applier.requested_power_limit_w is None


def test_power_limit_readback_error_stops_before_policy_is_updated() -> None:
    class FakePolicyController:
        def apply_power_limit_w(self, power_limit_w):
            return int(power_limit_w)

        def capabilities(self, *, refresh):
            assert refresh
            raise RuntimeError("daemon read-back unavailable")

    applier = gpu_vf_curve_applier.LiveGpuVfCurveApplier(
        gpu_index=0,
        gpu=cast(Any, FakePolicyController()),
        runtime_default_plan=[],
        translated_gpu_policy={"power_limit_w": 360},
        baseline_power_limit_w=360,
        requested_power_limit_w=320,
    )

    with pytest.raises(AutoUvPowerLimitApplyError, match="unable to read back"):
        applier.apply_requested_power_limit(log=lambda _message: None)

    assert applier.power_limit_w == 360
    assert applier.requested_power_limit_w is None


@pytest.mark.parametrize("pending", [None, 300])
def test_cached_power_limit_must_still_match_live_state(pending) -> None:
    controller = SimpleNamespace(capabilities=lambda **_: SimpleNamespace(
        power=SimpleNamespace(current_w=360, enforced_w=300)
    ))
    applier = gpu_vf_curve_applier.LiveGpuVfCurveApplier(
        gpu_index=0, gpu=cast(Any, controller), runtime_default_plan=[],
        translated_gpu_policy={"power_limit_w": 300}, requested_power_limit_w=pending,
    )
    with pytest.raises(AutoUvPowerLimitApplyError, match="read-back mismatch"):
        applier.apply_requested_power_limit(log=lambda _: None)


@pytest.mark.parametrize("current,enforced,passes", [
    (360, 300, False), (300, 280, True), (None, 300, True),
    (None, None, False), (float("nan"), 300, False),
])
def test_power_readback_prefers_configured_limit(current, enforced, passes) -> None:
    controller = SimpleNamespace(capabilities=lambda **_: SimpleNamespace(
        power=SimpleNamespace(current_w=current, enforced_w=enforced)
    ))
    if passes:
        assert gpu_vf_curve_applier.verify_applied_power_limit_w(
            controller, requested_w=300, reported_applied_w=300
        ) == 300
    else:
        with pytest.raises(AutoUvPowerLimitApplyError, match="read-back mismatch"):
            gpu_vf_curve_applier.verify_applied_power_limit_w(
                controller, requested_w=300, reported_applied_w=300
            )


@pytest.mark.parametrize("drift_during_probe", [False, True])
def test_each_probe_stops_on_a_wrong_or_lost_power_cap(monkeypatch, drift_during_probe) -> None:
    from auto_uv.probes import voltage_probe
    from stability.q2rtx.models import Q2RTXStabilityConfig

    power = SimpleNamespace(current_w=300 if drift_during_probe else 360, enforced_w=300)
    reader = SimpleNamespace(
        capabilities=lambda **_: SimpleNamespace(power=power),
        refresh_points=lambda: None,
    )
    calls = []

    def run(*_args, **_kwargs):
        calls.append("workload")
        power.current_w = 360
        return SimpleNamespace(reason="ok")

    monkeypatch.setattr(voltage_probe, "run_probe_with_hang_confirmation", run)
    monkeypatch.setattr(voltage_probe, "handle_probe_result_logging_and_blacklist", lambda *_a, **_k: None)
    monkeypatch.setattr(voltage_probe, "log_probe_start", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(voltage_probe, "apply_plan", lambda *_: None)
    with pytest.raises(AutoUvPowerLimitApplyError, match="read-back mismatch"):
        voltage_probe.probe_voltage_candidate(
            reader=reader, candidate_plan=[], candidate_voltage_mv=850,
            lock_clock_mhz=2700, q2rtx_config=Q2RTXStabilityConfig(), stable_history=[],
            initial_probe_clock_mhz=2700,
            nvml_session=SimpleNamespace(read_live_voltage_mv=lambda: 850),
            log=lambda _: None, phase_label="candidate", power_limit_w=300,
        )
    assert calls == (["workload"] if drift_during_probe else [])


@pytest.mark.parametrize("offset", [None, 0, 100_000, 105_000])
def test_requested_curve_must_match_driver_readback(offset) -> None:
    from auto_uv.domain.types import AutoUvError
    from auto_uv.gpu.runtime_vf_offset_reset_check import assert_runtime_vf_offsets_match_plan

    reader = SimpleNamespace(editable_core_points=lambda: (
        [] if offset is None else [{"index": 12, "current_offset_khz": offset}]
    ))
    plan = [{"index": 12, "new_offset_mhz": 105}]
    if offset == 105_000:
        assert_runtime_vf_offsets_match_plan(reader, plan)
    else:
        with pytest.raises(AutoUvError, match="V/F curve read-back mismatch"):
            assert_runtime_vf_offsets_match_plan(reader, plan)


class _MemoryOffsetPolicyController:
    readback_mhz: int | None = None

    def __init__(self, *, gpu_index: int) -> None:
        self.gpu_index = int(gpu_index)
        self.clock_offset_calls: list[dict] = []

    def get_memory_clock_offset_range_mhz(self):
        return (-2000, 6000)

    def refresh_points(self) -> None:
        return None

    def power_limit_set_supported(self) -> bool:
        return True

    def apply_clock_offsets(self, **kwargs):
        self.clock_offset_calls.append(kwargs)
        applied = dict(kwargs)
        applied["mem_clk_vf_offset_readback_mhz"] = self.readback_mhz
        return applied


def test_open_live_gpu_applier_logs_memory_offset_clamp_and_readback(
    monkeypatch,
) -> None:
    logs: list[str] = []

    class ConfirmingController(_MemoryOffsetPolicyController):
        readback_mhz = 6000

    _patch_applier_environment(monkeypatch, ConfirmingController)

    applier = gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={"auto_uv_memory_offset_mhz": 8000},
        log=logs.append,
    )

    # The driver-reported max (6000) is the clamp authority, not the static
    # 2000 fallback cap.
    assert applier.translated_gpu_policy["mem_clk_vf_offset_mhz"] == 6000
    assert cast(Any, applier.policy_controller).clock_offset_calls == [
        {"mem_clk_vf_offset_mhz": 6000}
    ]
    assert (
        "Auto-UV memory offset: requested 8000 MHz clamped to 6000 MHz (limit 6000 MHz)"
    ) in logs
    assert (
        "Auto-UV memory offset: applied +6000 MHz, NVML read-back confirms +6000 MHz"
    ) in logs


def test_open_live_gpu_applier_logs_memory_offset_readback_mismatch(
    monkeypatch,
) -> None:
    logs: list[str] = []

    class ClampingController(_MemoryOffsetPolicyController):
        readback_mhz = 500

    _patch_applier_environment(monkeypatch, ClampingController)

    gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={"auto_uv_memory_offset_mhz": 1000},
        log=logs.append,
    )

    assert (
        "Auto-UV memory offset MISMATCH: requested +1000 MHz but NVML reads "
        "back +500 MHz -- the driver clamped or ignored it"
    ) in logs


def test_open_live_gpu_applier_logs_memory_offset_readback_unsupported(
    monkeypatch,
) -> None:
    logs: list[str] = []

    class NoReadbackController(_MemoryOffsetPolicyController):
        readback_mhz = None

    _patch_applier_environment(monkeypatch, NoReadbackController)

    gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={"auto_uv_memory_offset_mhz": 1000},
        log=logs.append,
    )

    assert (
        "Auto-UV memory offset: applied +1000 MHz (driver does not support read-back)"
    ) in logs


def test_runtime_defaults_are_derived_from_semantic_daemon_reset(monkeypatch) -> None:
    logs: list[str] = []
    monkeypatch.setattr(
        runtime_defaults,
        "gpu_reset_defaults",
        lambda gpu_index: {
            "reset": True,
            "power_limit_set_supported": True,
            "gpu_name": "NVIDIA GeForce RTX 5080",
            "power_limits": {"power_limit_default_w": 360},
            "points": [
                {
                    "index": 12,
                    "type": 0,
                    "voltage_based": 1,
                    "voltage_uv": 900_000,
                    "base_freq_khz": 2_680_000,
                    "current_offset_khz": 0,
                }
            ],
        },
    )

    result = runtime_defaults.reset_nvidia_runtime_defaults(
        gpu_index=0,
        log=logs.append,
    )

    assert result["power_limit_w"] == 360
    assert result["power_limit_set_supported"] is True
    assert result["plan"] == [
        {
            "index": 12,
            "voltage_mv": 900,
            "base_mhz": 2680,
            "target_mhz": 2680,
            "current_offset_mhz": 0,
            "new_offset_mhz": 0,
            "preserve_base": False,
        }
    ]
    assert logs == [
        "Reset defaults through penguin-burnerd: "
        "gpu=NVIDIA GeForce RTX 5080 points=1 power_limit=360W"
    ]


def test_runtime_defaults_reject_daemon_reset_without_editable_points(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime_defaults,
        "gpu_reset_defaults",
        lambda gpu_index: {"reset": True, "points": []},
    )

    try:
        runtime_defaults.reset_nvidia_runtime_defaults(gpu_index=0)
    except runtime_defaults.NvidiaRuntimeDefaultsError as exc:
        assert "no editable V/F points" in str(exc)
    else:
        raise AssertionError("missing editable V/F points must fail Auto-UV startup")
