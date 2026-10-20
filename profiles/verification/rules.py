from __future__ import annotations

from pathlib import Path

from common.penguin_burner_errors import NvmlError
from profiles.uv.profile_store import resolve_auto_uv_profile
from runtime.gpu_control.vf_curve_reset_guard import (
    detect_vf_curve_reset,
    format_vf_curve_mismatch_preview,
    select_expected_vf_samples,
)

PROFILE_VERIFY_VOLTAGE_TOLERANCE_MV = 50
PROFILE_VERIFY_VOLTAGE_MISMATCH_STREAK = 5
PROFILE_VERIFY_VOLTAGE_WARMUP_S = 8.0
PROFILE_VERIFY_BASELINE_DURATION_S = 60
PROFILE_VERIFY_BASELINE_MIN_DURATION_S = 10


def apply_and_verify_profile_vf_plan(
    vf_curve_reader,
    plan: list[dict],
    *,
    context: str,
    apply_plan_fn,
) -> None:
    apply_plan_fn(vf_curve_reader, plan)
    vf_curve_reader.refresh_points()
    expected_samples = select_expected_vf_samples(plan)
    vf_mismatches = detect_vf_curve_reset(vf_curve_reader, expected_samples)
    if vf_mismatches:
        mismatch_preview = format_vf_curve_mismatch_preview(vf_mismatches)
        raise NvmlError(
            f"Profile verification V/F curve did not match the {context} "
            f"after apply: mismatches={len(vf_mismatches)} "
            f"samples={mismatch_preview}"
        )


def profile_verification_baseline_duration_s(duration_s: int) -> int:
    return max(
        PROFILE_VERIFY_BASELINE_MIN_DURATION_S,
        min(
            PROFILE_VERIFY_BASELINE_DURATION_S,
            max(1, int(duration_s)) // 10,
        ),
    )


def base_vf_plan_from_profile_plan(plan: list[dict]) -> list[dict]:
    base_plan = []
    for raw in list(plan or []):
        item = dict(raw)
        try:
            base_mhz = round(float(item["base_mhz"]))
            item["target_mhz"] = base_mhz
            item["new_offset_mhz"] = 0
        except (KeyError, TypeError, ValueError) as exc:
            raise NvmlError(f"profile baseline V/F point is invalid: {raw}") from exc
        base_plan.append(item)
    if not base_plan:
        raise NvmlError("profile baseline V/F plan is empty")
    return base_plan


def stability_stop_request_path(args) -> Path | None:
    text = str(getattr(args, "stability_stop_request_file", "") or "").strip()
    if not text:
        return None
    return Path(text).expanduser()


def stability_stop_request_abort_callback(
    stop_request_path: Path,
    *,
    previous_callback=None,
):
    def abort_callback(state: dict) -> str | None:
        if previous_callback is not None:
            reason = previous_callback(state)
            if reason:
                return str(reason)
        try:
            if Path(stop_request_path).exists():
                return "user-stop-requested"
        except OSError:
            return None
        return None

    return abort_callback


def profile_verification_voltage_abort_callback(
    flatten_target: dict,
    *,
    previous_callback=None,
):
    # Only sustained loaded drift fails; warmup, idle samples, and isolated spikes are ignored.
    target_voltage_mv = _coerce_positive_int(
        dict(flatten_target).get("lock_voltage_mv")
    )
    if target_voltage_mv is None:
        return previous_callback
    tolerance_mv = PROFILE_VERIFY_VOLTAGE_TOLERANCE_MV
    state = {"high_voltage_streak": 0}

    def abort_callback(progress_state: dict) -> str | None:
        if previous_callback is not None:
            reason = previous_callback(progress_state)
            if reason:
                return str(reason)
        if progress_state.get("running") == "q2rtx":
            warming_up = progress_state.get("benchmark_phase") != "measure"
        else:
            warming_up = _progress_elapsed_s(progress_state) < PROFILE_VERIFY_VOLTAGE_WARMUP_S
        if warming_up:
            state["high_voltage_streak"] = 0
            return None
        sample = progress_state.get("latest_sample")
        voltage_mv = _float_or_none(getattr(sample, "voltage_mv", None))
        if voltage_mv is None:
            state["high_voltage_streak"] = 0
            return None
        gpu_util_pct = _float_or_none(getattr(sample, "gpu_util_pct", None))
        if gpu_util_pct is not None and gpu_util_pct < 50.0:
            state["high_voltage_streak"] = 0
            return None
        if voltage_mv <= float(target_voltage_mv + tolerance_mv):
            state["high_voltage_streak"] = 0
            return None
        state["high_voltage_streak"] += 1
        if state["high_voltage_streak"] < PROFILE_VERIFY_VOLTAGE_MISMATCH_STREAK:
            return None
        return (
            "profile-verification-voltage-mismatch "
            f"current={voltage_mv:.0f}mV "
            f"target={target_voltage_mv}mV "
            f"tolerance={tolerance_mv}mV "
            f"streak={state['high_voltage_streak']}"
        )

    return abort_callback


def profile_needs_verify_baseline(selector: str) -> bool:
    # Only user-edited drafts missing base metrics need this probe.
    try:
        resolved = resolve_auto_uv_profile(str(selector), allow_unverified=True)
    except Exception:  # noqa: BLE001
        return False
    if resolved is None:
        return False
    _path, profile = resolved
    if str(profile.get("profile_source", "")).strip() != "user-edited":
        return False
    return any(
        profile.get(key) in (None, "")
        for key in (
            "base_avg_core_clock_mhz",
            "base_avg_fps",
            "base_avg_power_w",
            "base_efficiency_fps_per_w",
        )
    )


def profile_verification_failure_blocks_apply(reason: str) -> bool:
    text = str(reason or "").strip()
    if not text:
        return False
    return not text.startswith("user-stop-requested")


def _progress_elapsed_s(progress_state: dict) -> float:
    return _float_or_none(
        progress_state.get("progress_elapsed_s", progress_state.get("elapsed_s", 0.0))
    ) or 0.0


def _coerce_positive_int(value) -> int | None:
    number = _float_or_none(value)
    if number is None:
        return None
    integer = round(number)
    return integer if integer > 0 else None


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
