from __future__ import annotations

import contextlib
import copy
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from common.penguin_burner_errors import NvmlError
from drivers.nvidia.daemon_gpu import DaemonGpuClient
from profiles.gpu_identity import normalized_gpu_identity
from profiles.uv.profile_store import (
    mark_auto_uv_profile_verification_failed,
    mark_auto_uv_profile_verified,
)
from profiles.uv.runtime_auto_uv_profile import (
    apply_auto_uv_profile_memory_offset,
    load_auto_uv_final_curve,
)
from runtime.gpu_control.flattened_clock_ceiling import FlattenedClockCeilingController
from runtime.support.runtime_debug import log as runtime_log
from runtime.support.runtime_service import stop_existing_penguin_burner_runtime
from runtime.support.vf_curve_plan import (
    apply_plan,
    backup_current_offsets,
    restore_offsets,
)
from stability.q2rtx.config import (
    build_stability_config,
    stability_workload_label,
    stability_workload_split_label,
)
from stability.q2rtx.long_stability_config import build_long_stability_test_config
from stability.q2rtx.models import StabilityTestError
from stability.q2rtx.output import (
    attach_stdout_progress,
    attach_stdout_telemetry_events,
)
from stability.q2rtx.reporting import print_q2rtx_stability_result
from stability.q2rtx.runtime import run_q2rtx_stability_test

from .metrics import profile_verification_metrics_from_result
from .rules import (
    apply_and_verify_profile_vf_plan as verify_profile_vf_plan,
)
from .rules import (
    base_vf_plan_from_profile_plan,
    profile_needs_verify_baseline,
    profile_verification_baseline_duration_s,
    profile_verification_failure_blocks_apply,
    profile_verification_voltage_abort_callback,
    stability_stop_request_abort_callback,
    stability_stop_request_path,
)


@dataclass(slots=True)
class ProfileVerificationDependencies:
    stop_existing_penguin_burner_runtime: Callable = (
        stop_existing_penguin_burner_runtime
    )
    gpu_client_factory: Callable = DaemonGpuClient
    backup_current_offsets: Callable = backup_current_offsets
    restore_offsets: Callable = restore_offsets
    apply_plan: Callable = apply_plan
    load_auto_uv_final_curve: Callable = load_auto_uv_final_curve
    build_stability_config: Callable = build_stability_config
    build_long_stability_test_config: Callable = build_long_stability_test_config
    run_q2rtx_stability_test: Callable = run_q2rtx_stability_test
    attach_stdout_progress: Callable = attach_stdout_progress
    attach_stdout_telemetry_events: Callable = attach_stdout_telemetry_events
    print_q2rtx_stability_result: Callable = print_q2rtx_stability_result
    log: Callable[[str], None] = runtime_log


def run_profile_verification(
    args,
    *,
    gpu_index,
    config_path,
    auto_uv_runtime_options,
    dependencies: ProfileVerificationDependencies | None = None,
):
    _ = auto_uv_runtime_options
    deps = dependencies or ProfileVerificationDependencies()
    selector = str(args.auto_uv_profile or "").strip()
    if not selector:
        raise NvmlError("profile verification requires --auto-uv-profile")

    deps.stop_existing_penguin_burner_runtime(log=deps.log)
    try:
        gpu_client = deps.gpu_client_factory(gpu_index=gpu_index)
        gpu_client.refresh_points()
        gpu_identity = normalized_gpu_identity(
            gpu_client.capabilities().identity,
            index_at_verification=int(gpu_index),
        )
    except Exception as exc:
        raise NvmlError(f"could not open the daemon GPU client: {exc}") from exc
    if not str(gpu_identity.get("uuid") or "").strip():
        raise NvmlError(f"GPU {int(gpu_index)} did not report a stable UUID")
    vf_curve_reader = gpu_client
    gpu_policy_controller = gpu_client

    clock_ceiling_controller: FlattenedClockCeilingController | None = None

    def close_clock_ceiling() -> None:
        nonlocal clock_ceiling_controller
        if clock_ceiling_controller is None:
            return
        try:
            clock_ceiling_controller.close()
        except Exception as exc:  # noqa: BLE001
            deps.log(f"Warning: failed to reset verification clock lock: {exc}")
        clock_ceiling_controller = None

    with contextlib.ExitStack() as stack:
        with tempfile.NamedTemporaryFile(
            prefix="penguin-burner-verify-", suffix=".json", delete=False
        ) as backup_file:
            backup_path = Path(backup_file.name)
        deps.backup_current_offsets(
            vf_curve_reader,
            backup_path,
            policy_controller=gpu_policy_controller,
        )
        stack.callback(
            _restore_and_unlink_backup,
            deps,
            vf_curve_reader,
            backup_path,
            gpu_policy_controller,
        )
        stack.callback(close_clock_ceiling)

        label, flatten_target, verify_plan, saved_resolution = apply_verify_auto_uv_profile(
            vf_curve_reader,
            selector,
            gpu_policy_controller,
            dependencies=deps,
        )

        args = copy.copy(args)
        if getattr(args, "auto_uv_q2rtx_resolution", None) is None:
            args.auto_uv_q2rtx_resolution = saved_resolution

        if flatten_target is not None and gpu_policy_controller is not None:
            try:
                clock_ceiling_controller = FlattenedClockCeilingController(
                    flatten_target=flatten_target,
                    policy_controller=gpu_policy_controller,
                    exact_lock=True,
                )
                clock_ceiling_controller.apply()
            except Exception as exc:  # noqa: BLE001
                clock_ceiling_controller = None
                deps.log(f"Skipping verification clock lock: {exc}")
            else:
                deps.log(
                    "Configured verification clock lock: "
                    f"{clock_ceiling_controller.describe()}."
                )
                apply_and_verify_profile_vf_plan(
                    vf_curve_reader,
                    verify_plan,
                    context="selected profile after clock lock",
                    dependencies=deps,
                )
                deps.log(
                    "Re-applied profile V/F curve after verification clock lock: "
                    f"points={len(verify_plan)}."
                )

        duration_s = int(args.stability_seconds)
        workload_label = stability_workload_label()
        split_label = stability_workload_split_label(duration_s)
        deps.log(f"Profile verification workload split: {split_label}.")
        deps.log(
            "Profile verification starting: "
            f"profile={label} duration={duration_s}s workload={workload_label}."
        )
        stability_config = deps.build_stability_config(
            args,
            gpu_index=gpu_index,
            config_path=config_path,
            progress_context="Profile verification",
        )
        stability_config = deps.build_long_stability_test_config(
            stability_config,
            total_duration_s=duration_s,
        )
        if flatten_target is not None:
            stability_config.abort_callback = (
                profile_verification_voltage_abort_callback(
                    flatten_target,
                    previous_callback=stability_config.abort_callback,
                )
            )
        stop_request_path = stability_stop_request_path(args)
        if stop_request_path is not None:
            stability_config.abort_callback = stability_stop_request_abort_callback(
                stop_request_path,
                previous_callback=stability_config.abort_callback,
            )
        if flatten_target is not None:
            stability_config = deps.attach_stdout_telemetry_events(
                stability_config,
                target_voltage_mv=int(flatten_target["lock_voltage_mv"]),
                target_clock_mhz=int(flatten_target["lock_clock_mhz"]),
            )
        deps.attach_stdout_progress(stability_config)
        try:
            result = deps.run_q2rtx_stability_test(stability_config)
        except StabilityTestError as exc:
            raise NvmlError(f"stability test configuration error: {exc}") from exc
        deps.print_q2rtx_stability_result(result)
        if not result.success:
            if profile_verification_failure_blocks_apply(result.reason):
                try:
                    failed_path = mark_auto_uv_profile_verification_failed(
                        selector,
                        failure={
                            "reason": result.reason,
                            "log_path": str(result.log_path),
                            "workload": workload_label,
                            "fatal_output_matches": list(
                                getattr(result, "fatal_output_matches", []) or []
                            ),
                        },
                    )
                    if failed_path is not None:
                        deps.log(
                            "Marked profile verification failed: "
                            f"path={failed_path} reason={result.reason}"
                        )
                except Exception as exc:  # noqa: BLE001
                    deps.log(
                        f"Warning: failed to mark profile verification failed: {exc}"
                    )
            raise NvmlError(
                f"profile verification failed: {result.reason}; log={result.log_path}"
            )
        base_metrics = None
        if profile_needs_verify_baseline(selector):
            close_clock_ceiling()
            try:
                base_plan = base_vf_plan_from_profile_plan(verify_plan)
            except Exception as exc:  # noqa: BLE001
                deps.log(f"Profile verification baseline probe skipped: {exc}")
            else:
                base_metrics = run_profile_verification_baseline_probe(
                    args,
                    gpu_index=gpu_index,
                    config_path=config_path,
                    base_plan=base_plan,
                    gpu_policy_controller=gpu_policy_controller,
                    duration_s=profile_verification_baseline_duration_s(duration_s),
                    dependencies=deps,
                )
        verified_path = mark_auto_uv_profile_verified(
            selector,
            verification={
                "workload": workload_label,
                "duration_s": duration_s,
                "result_reason": result.reason,
                "log_path": str(result.log_path),
                "target_clock_mhz": (
                    flatten_target.get("lock_clock_mhz")
                    if isinstance(flatten_target, dict)
                    else None
                ),
                "target_voltage_mv": (
                    flatten_target.get("lock_voltage_mv")
                    if isinstance(flatten_target, dict)
                    else None
                ),
            },
            metrics=profile_verification_metrics_from_result(result),
            base_metrics=base_metrics,
            gpu_identity=gpu_identity,
        )
        deps.log(f"Marked profile verified: path={verified_path}")
        deps.log(f"Profile verification passed: profile={label}.")


def _restore_and_unlink_backup(
    deps: ProfileVerificationDependencies,
    vf_curve_reader,
    backup_path: Path,
    gpu_policy_controller,
) -> None:
    try:
        deps.restore_offsets(
            vf_curve_reader,
            backup_path,
            policy_controller=gpu_policy_controller,
        )
        deps.log("Restored V/F offsets after profile verification.")
    except Exception as exc:  # noqa: BLE001
        deps.log(f"Warning: failed to restore V/F offsets after verification: {exc}")
    try:
        backup_path.unlink(missing_ok=True)
    except OSError:
        pass


def apply_verify_auto_uv_profile(
    vf_curve_reader,
    selector: str,
    gpu_policy_controller,
    *,
    dependencies: ProfileVerificationDependencies | None = None,
):
    deps = dependencies or ProfileVerificationDependencies()
    auto_uv_final_curve = deps.load_auto_uv_final_curve(
        selector,
        allow_unverified=True,
    )
    if auto_uv_final_curve is None:
        raise NvmlError("Auto-UV profile not found")
    apply_and_verify_profile_vf_plan(
        vf_curve_reader,
        auto_uv_final_curve["plan"],
        context="selected profile",
        dependencies=deps,
    )
    label = (
        f"auto-UV:{auto_uv_final_curve['lock_clock_mhz']}MHz@"
        f"{auto_uv_final_curve['candidate_voltage_mv']}mV"
    )
    deps.log(
        "Applied profile for verification: "
        f"path={auto_uv_final_curve['path']} "
        f"target={auto_uv_final_curve['lock_clock_mhz']}MHz@"
        f"{auto_uv_final_curve['candidate_voltage_mv']}mV "
        f"points={len(auto_uv_final_curve['plan'])}."
    )
    memory_policy = apply_auto_uv_profile_memory_offset(
        profile_label=label,
        memory_offset_mhz=auto_uv_final_curve.get("memory_offset_mhz"),
        gpu_policy_controller=gpu_policy_controller,
    )
    if memory_policy:
        deps.log(
            "Applied profile memory offset for verification: "
            f"{int(memory_policy['mem_clk_vf_offset_mhz']):+d}MHz."
        )
    return (
        label,
        auto_uv_final_curve["flatten_target"],
        auto_uv_final_curve["plan"],
        auto_uv_final_curve.get("q2rtx_resolution"),
    )


def apply_and_verify_profile_vf_plan(
    vf_curve_reader,
    plan: list[dict],
    *,
    context: str,
    dependencies: ProfileVerificationDependencies | None = None,
) -> None:
    deps = dependencies or ProfileVerificationDependencies()
    verify_profile_vf_plan(
        vf_curve_reader,
        plan,
        context=context,
        apply_plan_fn=deps.apply_plan,
    )


def run_profile_verification_baseline_probe(
    args,
    *,
    gpu_index,
    config_path,
    base_plan: list[dict],
    gpu_policy_controller,
    duration_s: int,
    dependencies: ProfileVerificationDependencies | None = None,
) -> dict | None:
    deps = dependencies or ProfileVerificationDependencies()
    try:
        deps.log(
            "Profile verification baseline probe starting: "
            f"duration={int(duration_s)}s "
            f"{stability_workload_split_label(duration_s)}."
        )
        baseline_reader = deps.gpu_client_factory(gpu_index=gpu_index)
        baseline_reader.refresh_points()
        apply_and_verify_profile_vf_plan(
            baseline_reader,
            base_plan,
            context="baseline profile",
            dependencies=deps,
        )
        if gpu_policy_controller is not None:
            try:
                gpu_policy_controller.apply_clock_offsets(mem_clk_vf_offset_mhz=0)
            except Exception as exc:  # noqa: BLE001
                deps.log(
                    f"Warning: failed to reset memory offset for baseline probe: {exc}"
                )
        stability_config = deps.build_stability_config(
            args,
            gpu_index=gpu_index,
            config_path=config_path,
            duration_override=int(duration_s),
            progress_context="Profile baseline",
        )
        stability_config = deps.build_long_stability_test_config(
            stability_config,
            total_duration_s=int(duration_s),
        )
        stop_request_path = stability_stop_request_path(args)
        if stop_request_path is not None:
            stability_config.abort_callback = stability_stop_request_abort_callback(
                stop_request_path,
                previous_callback=stability_config.abort_callback,
            )
        deps.attach_stdout_progress(stability_config)
        result = deps.run_q2rtx_stability_test(stability_config)
        deps.print_q2rtx_stability_result(result)
        if not result.success:
            deps.log(
                "Profile verification baseline probe skipped: "
                f"{result.reason}; log={result.log_path}"
            )
            return None
        metrics = profile_verification_metrics_from_result(result)
        deps.log("Profile verification baseline probe complete.")
        return metrics
    except Exception as exc:  # noqa: BLE001
        deps.log(f"Profile verification baseline probe skipped: {exc}")
        return None
