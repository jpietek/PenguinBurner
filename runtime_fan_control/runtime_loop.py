from __future__ import annotations

import atexit
from dataclasses import dataclass
import signal
import sys
import time
from typing import Callable

from afterburner.vfcurve import (
    describe_afterburner_dynamic_lock,
    describe_afterburner_flatten_validation,
    describe_afterburner_profile_settings,
)
from afterburner.import_vf_curve import apply_plan
from nvml_gpu_policy import describe_translated_gpu_policy
from penguin_burner_errors import NvmlError
from runtime_debug import log as runtime_log
from runtime_gpu_control import (
    RuntimeVfCurvePolicyResult,
    detect_vf_curve_reset,
    format_vf_curve_mismatch_preview,
)

from .fan_curve_runtime_rules import (
    apply_hysteresis,
    build_effective_manual_curve,
    clamp,
    describe_fan_curve_state,
    format_curve_points,
    limit_speed_change,
    speed_for_temp,
    validate_curve,
)


@dataclass(slots=True)
class RuntimeFanLoopDependencies:
    detect_vf_curve_reset: Callable = detect_vf_curve_reset
    format_vf_curve_mismatch_preview: Callable = format_vf_curve_mismatch_preview
    apply_plan: Callable = apply_plan
    describe_translated_gpu_policy: Callable = describe_translated_gpu_policy
    log: Callable[[str], None] = runtime_log
    print_fn: Callable = print
    time_monotonic: Callable[[], float] = time.monotonic
    time_sleep: Callable[[float], None] = time.sleep
    time_strftime: Callable[[str], str] = time.strftime
    atexit_register: Callable = atexit.register
    signal_signal: Callable = signal.signal
    exit_process: Callable[[int], None] = sys.exit


@dataclass(slots=True)
class RuntimeFanSettings:
    poll_interval_s: float
    curve: list
    effective_manual_curve: list
    hysteresis_c: float
    mode: str
    min_fan_speed_pct: int
    max_fan_speed_pct: int
    effective_min_fan_speed_pct: int
    effective_max_fan_speed_pct: int
    max_step_up_pct_per_s: float
    max_step_down_pct_per_s: float
    manual_enable_temp_c: float
    auto_restore_temp_c: float
    emergency_auto_override_temp_c: float
    emergency_auto_resume_temp_c: float
    force_update_every_poll: bool


def run_runtime_fan_control_loop(
    *,
    gpu_index,
    config_path,
    fan_config: dict,
    fan_control_enabled: bool,
    enable_persistence_mode,
    prefer_afterburner_curve,
    nvml_session,
    voltage_reader,
    vf_curve_reader,
    gpu_policy_controller,
    vf_policy: RuntimeVfCurvePolicyResult | None = None,
    dependencies: RuntimeFanLoopDependencies | None = None,
    max_iterations: int | None = None,
) -> None:
    deps = dependencies or RuntimeFanLoopDependencies()
    vf_policy = vf_policy or RuntimeVfCurvePolicyResult()
    settings = _build_runtime_fan_settings(
        fan_config,
        fan_control_enabled=fan_control_enabled,
        deps=deps,
    )
    clock_ceiling_controller = vf_policy.clock_ceiling_controller
    vf_apply_result = vf_policy.vf_apply_result
    vf_expected_samples = list(vf_policy.vf_expected_samples)
    reapply_memory_offset_mhz = (
        vf_policy.auto_uv_profile_gpu_policy.get("mem_clk_vf_offset_mhz")
        if isinstance(vf_policy.auto_uv_profile_gpu_policy, dict)
        else None
    )
    last_vf_reapply_monotonic = 0.0
    vf_reapply_cooldown_s = max(float(settings.poll_interval_s), 10.0)

    fan_count = nvml_session.fan_count()
    if fan_control_enabled and fan_count == 0:
        raise NvmlError("GPU reports zero controllable fans")

    device_min_fan_speed_pct = None
    device_max_fan_speed_pct = None
    if fan_control_enabled:
        device_min_fan_speed_pct, device_max_fan_speed_pct = (
            nvml_session.fan_speed_limits()
        )
        _apply_device_fan_limits(
            settings,
            device_min_fan_speed_pct=device_min_fan_speed_pct,
            device_max_fan_speed_pct=device_max_fan_speed_pct,
        )
        settings.effective_manual_curve = build_effective_manual_curve(
            curve=settings.curve,
            manual_enable_temp_c=settings.manual_enable_temp_c,
            effective_min_fan_speed_pct=settings.effective_min_fan_speed_pct,
            effective_max_fan_speed_pct=settings.effective_max_fan_speed_pct,
            mode=settings.mode,
        )

    restored = False

    def restore_default():
        nonlocal restored
        if restored:
            return
        restored = True
        if fan_control_enabled:
            try:
                nvml_session.set_all_fans_default(fan_count)
            except Exception as exc:
                deps.log(f"Warning: failed to restore default fan speed: {exc}")
        if clock_ceiling_controller is not None:
            clock_ceiling_controller.close()
        if vf_curve_reader is not None:
            vf_curve_reader.close()
        if gpu_policy_controller is not None:
            gpu_policy_controller.close()
        nvml_session.close()

    def stop(_signum, _frame):
        restore_default()
        deps.exit_process(0)

    deps.atexit_register(restore_default)
    deps.signal_signal(signal.SIGINT, stop)
    deps.signal_signal(signal.SIGTERM, stop)

    _log_runtime_startup(
        gpu_index=gpu_index,
        config_path=config_path,
        fan_config=fan_config,
        fan_control_enabled=fan_control_enabled,
        fan_count=fan_count,
        device_min_fan_speed_pct=device_min_fan_speed_pct,
        device_max_fan_speed_pct=device_max_fan_speed_pct,
        settings=settings,
        enable_persistence_mode=enable_persistence_mode,
        vf_policy=vf_policy,
        prefer_afterburner_curve=prefer_afterburner_curve,
        vf_curve_reader=vf_curve_reader,
        deps=deps,
    )

    last_speed = None
    last_set_temp_c = None
    last_update_time = deps.time_monotonic()
    manual_mode_active = False
    hot_auto_mode_active = False
    iteration_count = 0

    def log_with_timestamp(message: str) -> None:
        deps.log(f"{deps.time_strftime('%Y-%m-%d %H:%M:%S')} {message}")

    def switch_to_hardware_auto() -> None:
        nonlocal manual_mode_active, last_speed, last_set_temp_c
        nvml_session.set_all_fans_default(fan_count)
        manual_mode_active = False
        last_speed = None
        last_set_temp_c = None

    while True:
        if max_iterations is not None and iteration_count >= int(max_iterations):
            return
        iteration_count += 1

        loop_started = deps.time_monotonic()
        current_temp_c = nvml_session.temperature_c()
        power_draw_w = nvml_session.power_draw_w()

        telemetry_text = nvml_session.format_telemetry(
            fan_count=fan_count,
            current_temp_c=current_temp_c,
            voltage_reader=voltage_reader,
            vf_curve_reader=vf_curve_reader,
            gpu_policy_controller=gpu_policy_controller,
            power_draw_w=power_draw_w,
            clock_ceiling_controller=clock_ceiling_controller,
        )
        if (
            vf_curve_reader is not None
            and vf_expected_samples
            and vf_apply_result is not None
        ):
            last_vf_reapply_monotonic = _maybe_reapply_vf_curve(
                vf_curve_reader=vf_curve_reader,
                vf_expected_samples=vf_expected_samples,
                vf_apply_result=vf_apply_result,
                telemetry_text=telemetry_text,
                loop_started=loop_started,
                last_vf_reapply_monotonic=last_vf_reapply_monotonic,
                vf_reapply_cooldown_s=vf_reapply_cooldown_s,
                deps=deps,
                gpu_policy_controller=gpu_policy_controller,
                memory_offset_mhz=reapply_memory_offset_mhz,
            )

        def fan_state_text() -> str:
            return describe_fan_curve_state(
                current_temp_c=current_temp_c,
                effective_curve=settings.effective_manual_curve,
                manual_mode_active=manual_mode_active,
                emergency_auto_mode_active=hot_auto_mode_active,
                emergency_auto_resume_temp_c=settings.emergency_auto_resume_temp_c,
            )

        if not fan_control_enabled:
            log_with_timestamp(f"{telemetry_text} fan_control=disabled")
            deps.time_sleep(settings.poll_interval_s)
            continue

        # Still in emergency override.
        if hot_auto_mode_active and current_temp_c > settings.emergency_auto_resume_temp_c:
            log_with_timestamp(
                f"{telemetry_text} {fan_state_text()} fan_mode=auto reason=emergency-override"
            )
            deps.time_sleep(settings.poll_interval_s)
            continue

        # Exit emergency override; fall through to normal decisions.
        if hot_auto_mode_active and current_temp_c <= settings.emergency_auto_resume_temp_c:
            hot_auto_mode_active = False
            log_with_timestamp(
                f"{telemetry_text} {fan_state_text()} event=emergency-override-cleared"
            )

        # Enter emergency override.
        if current_temp_c > settings.emergency_auto_override_temp_c:
            if manual_mode_active:
                switch_to_hardware_auto()
            hot_auto_mode_active = True
            log_with_timestamp(
                f"{telemetry_text} {fan_state_text()} "
                f"event=restoring-auto-mode reason=emergency-override"
            )
            deps.time_sleep(settings.poll_interval_s)
            continue

        # Still below the manual-takeover threshold.
        if not manual_mode_active and current_temp_c < settings.manual_enable_temp_c:
            log_with_timestamp(f"{telemetry_text} {fan_state_text()} fan_mode=auto")
            deps.time_sleep(settings.poll_interval_s)
            continue

        # Enter manual mode; fall through to fan computation.
        if not manual_mode_active:
            manual_mode_active = True
            last_speed = None
            last_set_temp_c = None
            last_update_time = loop_started
            log_with_timestamp(
                f"{telemetry_text} {fan_state_text()} event=entering-manual-mode"
            )

        # Hand control back to hardware once we cool down.
        if current_temp_c <= settings.auto_restore_temp_c:
            switch_to_hardware_auto()
            log_with_timestamp(
                f"{telemetry_text} {fan_state_text()} event=restoring-auto-mode"
            )
            deps.time_sleep(settings.poll_interval_s)
            continue

        raw_target_speed = clamp(
            speed_for_temp(current_temp_c, settings.curve, mode=settings.mode),
            settings.effective_min_fan_speed_pct,
            settings.effective_max_fan_speed_pct,
        )
        hysteresis_target_speed = apply_hysteresis(
            current_temp_c=current_temp_c,
            raw_target_speed=raw_target_speed,
            last_temp_c=last_set_temp_c,
            last_speed=last_speed,
            hysteresis_c=settings.hysteresis_c,
        )
        target_speed = round(
            clamp(
                limit_speed_change(
                    target_speed=hysteresis_target_speed,
                    last_speed=last_speed,
                    elapsed_s=loop_started - last_update_time,
                    max_step_up_pct_per_s=settings.max_step_up_pct_per_s,
                    max_step_down_pct_per_s=settings.max_step_down_pct_per_s,
                ),
                settings.effective_min_fan_speed_pct,
                settings.effective_max_fan_speed_pct,
            )
        )

        if settings.force_update_every_poll or target_speed != last_speed:
            nvml_session.set_all_fans_speed(fan_count, target_speed)
            last_set_temp_c = current_temp_c
            last_speed = target_speed
            last_update_time = loop_started

        log_with_timestamp(
            f"{telemetry_text} {fan_state_text()} "
            f"target={target_speed}% curve={raw_target_speed:.1f}% "
            f"hyst={hysteresis_target_speed:.1f}% fan_mode=manual"
        )

        deps.time_sleep(settings.poll_interval_s)


def _build_runtime_fan_settings(
    fan_config: dict,
    *,
    fan_control_enabled: bool,
    deps: RuntimeFanLoopDependencies,
) -> RuntimeFanSettings:
    settings = RuntimeFanSettings(
        poll_interval_s=float(fan_config["poll_interval_s"]),
        curve=[],
        effective_manual_curve=[],
        hysteresis_c=0.0,
        mode="linear",
        min_fan_speed_pct=0,
        max_fan_speed_pct=100,
        effective_min_fan_speed_pct=0,
        effective_max_fan_speed_pct=100,
        max_step_up_pct_per_s=0.0,
        max_step_down_pct_per_s=0.0,
        manual_enable_temp_c=0.0,
        auto_restore_temp_c=0.0,
        emergency_auto_override_temp_c=80.0,
        emergency_auto_resume_temp_c=75.0,
        force_update_every_poll=False,
    )
    if not fan_control_enabled:
        return settings

    settings.curve = [tuple(point) for point in fan_config["curve"]]
    validate_curve(settings.curve)
    settings.hysteresis_c = float(fan_config["hysteresis_c"])
    settings.mode = str(fan_config["mode"])
    settings.min_fan_speed_pct = int(fan_config["min_fan_speed_pct"])
    settings.max_fan_speed_pct = int(fan_config["max_fan_speed_pct"])
    settings.effective_min_fan_speed_pct = settings.min_fan_speed_pct
    settings.effective_max_fan_speed_pct = settings.max_fan_speed_pct
    settings.max_step_up_pct_per_s = float(fan_config["max_step_up_pct_per_s"])
    settings.max_step_down_pct_per_s = float(fan_config["max_step_down_pct_per_s"])
    settings.manual_enable_temp_c = float(fan_config["manual_enable_temp_c"])
    settings.auto_restore_temp_c = float(fan_config["auto_restore_temp_c"])
    settings.emergency_auto_override_temp_c = float(
        fan_config.get("emergency_auto_override_temp_c", 80.0)
    )
    settings.emergency_auto_resume_temp_c = float(
        fan_config.get("emergency_auto_resume_temp_c", 75.0)
    )
    settings.force_update_every_poll = bool(fan_config["force_update_every_poll"])
    return settings


def _apply_device_fan_limits(
    settings: RuntimeFanSettings,
    *,
    device_min_fan_speed_pct,
    device_max_fan_speed_pct,
) -> None:
    if device_min_fan_speed_pct is not None:
        settings.effective_min_fan_speed_pct = max(
            settings.effective_min_fan_speed_pct,
            device_min_fan_speed_pct,
        )
    if device_max_fan_speed_pct is not None:
        settings.effective_max_fan_speed_pct = min(
            settings.effective_max_fan_speed_pct,
            device_max_fan_speed_pct,
        )
    if settings.effective_max_fan_speed_pct < settings.effective_min_fan_speed_pct:
        raise NvmlError("effective fan speed range is invalid")


def _log_runtime_startup(
    *,
    gpu_index,
    config_path,
    fan_config,
    fan_control_enabled,
    fan_count,
    device_min_fan_speed_pct,
    device_max_fan_speed_pct,
    settings: RuntimeFanSettings,
    enable_persistence_mode,
    vf_policy: RuntimeVfCurvePolicyResult,
    prefer_afterburner_curve,
    vf_curve_reader,
    deps: RuntimeFanLoopDependencies,
) -> None:
    translated_gpu_policy = vf_policy.translated_gpu_policy
    auto_uv_profile_gpu_policy = vf_policy.auto_uv_profile_gpu_policy
    startup_power_limit_w = vf_policy.startup_power_limit_w
    active_vf_curve_source = vf_policy.active_vf_curve_source
    auto_uv_final_curve = vf_policy.auto_uv_final_curve
    afterburner_source = vf_policy.afterburner_source
    afterburner_profile_settings = vf_policy.afterburner_profile_settings
    clock_ceiling_controller = vf_policy.clock_ceiling_controller
    if fan_control_enabled:
        deps.print_fn(
            f"Controlling GPU {gpu_index} with {fan_count} fan(s), "
            f"mode={settings.mode}, hysteresis={settings.hysteresis_c} C, "
            f"manual-limits={settings.effective_min_fan_speed_pct}-{settings.effective_max_fan_speed_pct}%, "
            f"manual-enable={settings.manual_enable_temp_c} C, auto-restore={settings.auto_restore_temp_c} C, "
            f"emergency-auto={settings.emergency_auto_override_temp_c} C/{settings.emergency_auto_resume_temp_c} C. "
            "Press Ctrl-C to restore auto mode.",
            flush=True,
        )
    else:
        deps.print_fn(
            f"Running GPU {gpu_index} telemetry and V/F policy loop with fan control disabled. "
            "Use --silent-fan-curve to let PenguinBurner control fans. Press Ctrl-C to exit.",
            flush=True,
        )

    startup_gpu_policy = translated_gpu_policy or auto_uv_profile_gpu_policy or {
        "power_limit_w": startup_power_limit_w
    }
    deps.log(
        f"GPU policy: persistence={'on' if enable_persistence_mode else 'off'}, "
        f"{deps.describe_translated_gpu_policy(startup_gpu_policy)}."
    )
    deps.log(f"Config file: {config_path}")
    if active_vf_curve_source == "auto-uv-final" and auto_uv_final_curve is not None:
        deps.log(
            "Active VF curve source: auto-UV final "
            f"path={auto_uv_final_curve['path']} "
            f"target={auto_uv_final_curve['lock_clock_mhz']}MHz@"
            f"{auto_uv_final_curve['candidate_voltage_mv']}mV; "
            "Afterburner V/F import skipped."
        )
    elif active_vf_curve_source == "afterburner" and prefer_afterburner_curve:
        deps.log(
            "Active VF curve source: Afterburner import requested by --prefer-afterburner-curve."
        )
    if afterburner_source is not None:
        flatten_target = afterburner_source["section_info"].get("flatten_target")
        flatten_text = (
            describe_afterburner_dynamic_lock(flatten_target)
            if flatten_target is not None
            else "none"
        )
        deps.log(
            "Afterburner import: "
            f"root={afterburner_source['afterburner_root']} "
            f"device_profile={afterburner_source['profile_path'].name} "
            f"profile={afterburner_source['section']} "
            f"flatten-target={flatten_text}."
        )
        deps.log(
            "Afterburner flatten validation: "
            f"{describe_afterburner_flatten_validation(afterburner_source['section_info'].get('flatten_validation'))}."
        )
        if afterburner_profile_settings is not None:
            deps.log(
                "Afterburner parsed settings: "
                f"{describe_afterburner_profile_settings(afterburner_profile_settings)}."
            )
    if vf_curve_reader is not None:
        vf_summary = vf_curve_reader.summary()
        deps.log(
            f"Linux NVAPI VF curve: "
            f"active-points={vf_summary['active_points']}, "
            f"editable-core-points={vf_summary['editable_core_points']}."
        )
    if device_min_fan_speed_pct is not None and device_max_fan_speed_pct is not None:
        deps.log(
            f"Device fan limits reported by NVML: "
            f"{device_min_fan_speed_pct}-{device_max_fan_speed_pct}%."
        )
    if fan_control_enabled:
        _log_fan_curve_startup(
            fan_config=fan_config,
            settings=settings,
            deps=deps,
        )
    else:
        deps.log(
            "Fan control disabled: hardware/driver fan policy remains active; "
            "fan curve files are ignored unless --silent-fan-curve is used."
        )
    if clock_ceiling_controller is not None:
        deps.log(f"Clock ceiling policy: {clock_ceiling_controller.describe()}.")


def _log_fan_curve_startup(
    *,
    fan_config,
    settings: RuntimeFanSettings,
    deps: RuntimeFanLoopDependencies,
) -> None:
    curve_source = fan_config.get("curve_source")
    if curve_source:
        if str(curve_source) == "auto-uv":
            deps.log(
                "Fan curve source: auto-UV "
                f"path={fan_config.get('curve_source_path', 'n/a')} "
                f"generated={fan_config.get('curve_source_generated_at', 'n/a')} "
                f"target-temp={fan_config.get('curve_source_target_load_temp_c', 'n/a')}C "
                f"observed-load={fan_config.get('curve_source_loaded_temperature_c', 'n/a')}C "
                f"observed-fan={fan_config.get('curve_source_observed_fan_speed_pct', 'n/a')}%."
            )
        else:
            curve_flags_u32 = int(fan_config.get("curve_source_flags_u32", 0))
            curve_period_ms = int(
                fan_config.get(
                    "curve_source_period_ms",
                    int(round(settings.poll_interval_s * 1000)),
                )
            )
            deps.log(
                f"Fan curve source: {curve_source} "
                f"period={curve_period_ms}ms flags=0x{curve_flags_u32:08x}."
            )
    deps.log(f"Fan curve points: {format_curve_points(settings.curve)}")
    deps.log(
        "Effective manual fan curve: "
        f"{format_curve_points(settings.effective_manual_curve)}"
    )
    if fan_config.get("curve_override_zero_with_hardware_curve"):
        behavior_parts = ["zero-rpm zone uses hardware auto curve"]
        if fan_config.get("curve_hardware_auto_below_device_min"):
            behavior_parts.append("below device manual minimum uses hardware auto curve")
        takeover_temp_c = fan_config.get("curve_manual_takeover_temp_c")
        if takeover_temp_c is not None:
            behavior_parts.append(f"manual takeover near {float(takeover_temp_c):.2f}C")
        deps.log("Fan curve behavior: " + "; ".join(behavior_parts) + ".")
    deps.log(
        "Silent fan curve guardrail: "
        f"hardware auto above {float(settings.emergency_auto_override_temp_c):.0f}C, "
        f"resume manual below {float(settings.emergency_auto_resume_temp_c):.0f}C."
    )


def _maybe_reapply_vf_curve(
    *,
    vf_curve_reader,
    vf_expected_samples,
    vf_apply_result,
    telemetry_text,
    loop_started,
    last_vf_reapply_monotonic,
    vf_reapply_cooldown_s,
    deps: RuntimeFanLoopDependencies,
    gpu_policy_controller=None,
    memory_offset_mhz=None,
):
    vf_mismatches = deps.detect_vf_curve_reset(vf_curve_reader, vf_expected_samples)
    if not vf_mismatches:
        return last_vf_reapply_monotonic
    if (loop_started - last_vf_reapply_monotonic) < vf_reapply_cooldown_s:
        return last_vf_reapply_monotonic

    timestamp = deps.time_strftime("%Y-%m-%d %H:%M:%S")
    try:
        deps.apply_plan(vf_curve_reader, vf_apply_result["plan"])
        vf_curve_reader.refresh_points()
    except Exception as exc:
        deps.log(f"{timestamp} event=vf-curve-reapply-error error={exc}")
        return last_vf_reapply_monotonic

    if (
        memory_offset_mhz is not None
        and int(memory_offset_mhz) > 0
        and gpu_policy_controller is not None
    ):
        try:
            gpu_policy_controller.apply_clock_offsets(
                mem_clk_vf_offset_mhz=int(memory_offset_mhz)
            )
        except Exception as exc:
            deps.log(f"{timestamp} event=mem-offset-reapply-error error={exc}")

    mismatch_preview = deps.format_vf_curve_mismatch_preview(vf_mismatches)
    deps.log(
        f"{timestamp} {telemetry_text} "
        f"event=vf-curve-reapplied mismatches={len(vf_mismatches)} "
        f"samples={mismatch_preview}"
    )
    return loop_started
