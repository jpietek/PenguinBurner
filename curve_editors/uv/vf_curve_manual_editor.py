from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from auto_uv.domain.user_options import AUTO_UV_CURVE_TUNING
from auto_uv.persistence.verified_candidate_result_file import artifact_points
from profiles.uv.profile_store import user_edited_display_name

MANUAL_SMOOTH_TRIGGER_CLOCK_BINS = 5
MANUAL_SMOOTH_LEFT_VOLTAGE_BINS = 5
USER_EDITED_PROFILE_SOURCE = "user-edited"


@dataclass(frozen=True, slots=True)
class ManualCurveEdit:
    plan: list[dict]
    anchor_voltage_mv: int
    anchor_clock_mhz: int
    edit_kind: str
    selected_voltage_mv: int | None = None
    selected_clock_mhz: int | None = None
    selected_range_start_voltage_mv: int | None = None
    control_voltage_mvs: tuple[int, ...] = ()


def editable_anchor_from_profile(profile: dict) -> tuple[int, int] | None:
    try:
        voltage_mv = round(float(cast(Any, profile.get("candidate_voltage_mv"))))
        clock_mhz = round(float(cast(Any, profile.get("lock_clock_mhz"))))
    except (TypeError, ValueError):
        return None
    if voltage_mv <= 0 or clock_mhz <= 0:
        return None
    return voltage_mv, clock_mhz


def manual_edit_control_voltage_mvs(manual_edit) -> tuple[int, ...]:
    """The control-point voltages a saved manual edit was made with, in order."""
    if not isinstance(manual_edit, dict):
        return ()
    raw_values = manual_edit.get("control_voltage_mvs")
    if not isinstance(raw_values, (list, tuple)):
        return ()
    values = []
    seen = set()
    for raw_value in raw_values:
        try:
            value = round(float(raw_value))
        except (TypeError, ValueError):
            continue
        if value in seen:
            continue
        seen.add(value)
        values.append(value)
    return tuple(values)


def manual_drag_anchor_edit(
    plan: list[dict],
    *,
    anchor_voltage_mv: int,
    anchor_clock_mhz: int,
    requested_voltage_mv: float,
    requested_clock_mhz: float,
    minimum_clock_mhz: int | None = None,
    allow_lower_clock: bool = True,
    preserve_clock_on_right: bool = False,
    max_voltage_bins: int | None = None,
    max_clock_bins: int | None = None,
    control_voltage_mvs: tuple[int, ...] | list[int] | None = None,
) -> ManualCurveEdit:
    points = _sorted_plan(plan)
    anchor_index = _nearest_voltage_index(points, int(anchor_voltage_mv))
    requested_index = _nearest_voltage_index(points, round(float(requested_voltage_mv)))
    max_index = (
        len(points) - 1
        if max_voltage_bins is None
        else min(
            len(points) - 1,
            int(anchor_index) + max(0, int(max_voltage_bins)),
        )
    )
    clamped_index = _clamp(requested_index, 0, max_index)
    snapped_clock_mhz = _snap_clock_mhz(requested_clock_mhz)
    moving_right = int(clamped_index) > int(anchor_index)
    if preserve_clock_on_right and moving_right:
        snapped_clock_mhz = max(snapped_clock_mhz, int(anchor_clock_mhz))
    min_clock_mhz = (
        0
        if allow_lower_clock
        else max(
            int(anchor_clock_mhz),
            int(minimum_clock_mhz) if minimum_clock_mhz is not None else 0,
        )
    )
    max_clock_mhz = (
        None
        if max_clock_bins is None
        else int(anchor_clock_mhz)
        + max(0, int(max_clock_bins)) * _clock_step_mhz()
    )
    clamped_clock_mhz = max(int(min_clock_mhz), int(snapped_clock_mhz))
    if max_clock_mhz is not None:
        clamped_clock_mhz = min(int(max_clock_mhz), int(clamped_clock_mhz))
    return ManualCurveEdit(
        plan=_flatten_plan_from_index(
            points,
            int(clamped_index),
            int(clamped_clock_mhz),
            smooth_left=(
                not moving_right
                and int(clamped_clock_mhz)
                > int(anchor_clock_mhz)
                + MANUAL_SMOOTH_TRIGGER_CLOCK_BINS * _clock_step_mhz()
            ),
            interpolate_left=moving_right,
            interpolation_start_index=(
                max(0, int(anchor_index) - 1) if moving_right else None
            ),
        ),
        anchor_voltage_mv=int(points[int(clamped_index)]["voltage_mv"]),
        anchor_clock_mhz=int(clamped_clock_mhz),
        edit_kind="drag-anchor",
        selected_voltage_mv=int(points[int(clamped_index)]["voltage_mv"]),
        selected_clock_mhz=int(clamped_clock_mhz),
        control_voltage_mvs=_normalized_control_voltage_mvs(
            points,
            control_voltage_mvs,
            int(clamped_index),
        ),
    )


def manual_flatten_from_existing_point(
    plan: list[dict],
    *,
    anchor_voltage_mv: int,
    clicked_voltage_mv: float,
    control_voltage_mvs: tuple[int, ...] | list[int] | None = None,
) -> ManualCurveEdit:
    points = _sorted_plan(plan)
    anchor_index = _nearest_voltage_index(points, int(anchor_voltage_mv))
    clicked_index = _nearest_voltage_index(
        points,
        round(float(clicked_voltage_mv)),
    )
    flatten_index = min(int(anchor_index), int(clicked_index))
    flatten_clock_mhz = int(points[flatten_index]["target_mhz"])
    return ManualCurveEdit(
        plan=_flatten_plan_from_index(points, flatten_index, flatten_clock_mhz),
        anchor_voltage_mv=int(points[flatten_index]["voltage_mv"]),
        anchor_clock_mhz=int(flatten_clock_mhz),
        edit_kind="flatten-from-point",
        selected_voltage_mv=int(points[flatten_index]["voltage_mv"]),
        selected_clock_mhz=int(flatten_clock_mhz),
        control_voltage_mvs=_normalized_control_voltage_mvs(
            points,
            control_voltage_mvs,
            int(flatten_index),
        ),
    )


def manual_add_curve_point_edit(
    edit: ManualCurveEdit,
    *,
    requested_voltage_mv: float,
    requested_clock_mhz: float,
) -> ManualCurveEdit:
    points = _sorted_plan(edit.plan)
    anchor_index = _nearest_voltage_index(points, int(edit.anchor_voltage_mv))
    controls = _normalized_control_voltage_mvs(
        points,
        edit.control_voltage_mvs,
        int(anchor_index),
    )
    target_index = _nearest_voltage_index(
        points,
        round(float(requested_voltage_mv)),
    )
    if target_index == anchor_index:
        return ManualCurveEdit(
            plan=[dict(item) for item in points],
            anchor_voltage_mv=int(points[anchor_index]["voltage_mv"]),
            anchor_clock_mhz=int(edit.anchor_clock_mhz),
            edit_kind="add-point",
            selected_voltage_mv=int(points[anchor_index]["voltage_mv"]),
            selected_clock_mhz=int(edit.anchor_clock_mhz),
            control_voltage_mvs=controls,
        )
    target_voltage_mv = int(points[target_index]["voltage_mv"])
    if target_voltage_mv in set(controls):
        return ManualCurveEdit(
            plan=[dict(item) for item in points],
            anchor_voltage_mv=int(points[anchor_index]["voltage_mv"]),
            anchor_clock_mhz=int(edit.anchor_clock_mhz),
            edit_kind="select",
            selected_voltage_mv=int(target_voltage_mv),
            selected_clock_mhz=int(points[target_index]["target_mhz"]),
            control_voltage_mvs=controls,
        )
    if target_index < anchor_index:
        clamped_clock_mhz = _clamp(
            _snap_clock_mhz(requested_clock_mhz),
            0,
            int(edit.anchor_clock_mhz),
        )
    else:
        clamped_clock_mhz = max(
            int(edit.anchor_clock_mhz),
            _snap_clock_mhz(requested_clock_mhz),
        )
    edited = []
    for index, raw in enumerate(points):
        item = dict(raw)
        if index == target_index:
            item["target_mhz"] = int(clamped_clock_mhz)
            item["new_offset_mhz"] = (
                int(item["target_mhz"]) - int(item["base_mhz"])
            )
        edited.append(item)
    return ManualCurveEdit(
        plan=edited,
        anchor_voltage_mv=int(points[anchor_index]["voltage_mv"]),
        anchor_clock_mhz=int(edit.anchor_clock_mhz),
        edit_kind="add-point",
        selected_voltage_mv=int(target_voltage_mv),
        selected_clock_mhz=int(clamped_clock_mhz),
        control_voltage_mvs=_control_voltage_mvs_with_indexes(
            points,
            controls,
            int(anchor_index),
            add_index=int(target_index),
        ),
    )


def manual_tune_single_point_edit(
    plan: list[dict],
    *,
    anchor_voltage_mv: int,
    anchor_clock_mhz: int,
    point_voltage_mv: float,
    requested_voltage_mv: float | None = None,
    requested_clock_mhz: float,
    control_voltage_mvs: tuple[int, ...] | list[int] | None = None,
) -> ManualCurveEdit:
    points = _sorted_plan(plan)
    anchor_index = _nearest_voltage_index(points, int(anchor_voltage_mv))
    source_index = _nearest_voltage_index(
        points,
        round(float(point_voltage_mv)),
    )
    if source_index < anchor_index:
        region_first_index = 0
        region_last_index = int(anchor_index) - 1
    else:
        region_first_index = int(anchor_index) + 1
        region_last_index = len(points) - 1
    if source_index == anchor_index or region_last_index < region_first_index:
        # The anchor itself (and an empty side of the curve) has no
        # single-point edit; the anchor is moved with manual_drag_anchor_edit.
        controls = _normalized_control_voltage_mvs(
            points,
            control_voltage_mvs,
            int(anchor_index),
        )
        return ManualCurveEdit(
            plan=[dict(item) for item in points],
            anchor_voltage_mv=int(points[anchor_index]["voltage_mv"]),
            anchor_clock_mhz=int(anchor_clock_mhz),
            edit_kind="single-point",
            selected_voltage_mv=int(points[anchor_index]["voltage_mv"]),
            selected_clock_mhz=int(anchor_clock_mhz),
            control_voltage_mvs=controls,
        )
    requested_point_voltage_mv = (
        point_voltage_mv if requested_voltage_mv is None else requested_voltage_mv
    )
    target_index = _clamp(
        _nearest_voltage_index(
            points,
            round(float(requested_point_voltage_mv)),
        ),
        region_first_index,
        region_last_index,
    )
    if source_index < anchor_index:
        clamped_clock_mhz = _clamp(
            _snap_clock_mhz(requested_clock_mhz),
            0,
            int(anchor_clock_mhz),
        )
    else:
        # Bins right of the anchor form the rising tail: they may sit above
        # the locked clock but never below it.
        clamped_clock_mhz = max(
            int(anchor_clock_mhz),
            _snap_clock_mhz(requested_clock_mhz),
        )
    edited = []
    for index, raw in enumerate(points):
        item = dict(raw)
        if index == target_index:
            item["target_mhz"] = int(clamped_clock_mhz)
            item["new_offset_mhz"] = (
                int(item["target_mhz"]) - int(item["base_mhz"])
            )
        edited.append(item)
    if int(source_index) != int(target_index):
        _interpolate_single_point_voltage_move(
            edited,
            source_index=int(source_index),
            target_index=int(target_index),
            lower_bound_index=(
                0 if source_index < anchor_index else int(anchor_index)
            ),
            upper_bound_index=(
                int(anchor_index)
                if source_index < anchor_index
                else len(points) - 1
            ),
        )
    return ManualCurveEdit(
        plan=edited,
        anchor_voltage_mv=int(points[anchor_index]["voltage_mv"]),
        anchor_clock_mhz=int(anchor_clock_mhz),
        edit_kind="single-point",
        selected_voltage_mv=int(points[target_index]["voltage_mv"]),
        selected_clock_mhz=int(clamped_clock_mhz),
        control_voltage_mvs=_control_voltage_mvs_with_indexes(
            points,
            control_voltage_mvs,
            int(anchor_index),
            add_index=int(target_index),
            remove_index=(
                int(source_index)
                if int(source_index) != int(target_index)
                else None
            ),
        ),
    )


def manual_nudge_selected_frequency(
    edit: ManualCurveEdit,
    *,
    direction: int,
    clock_step_mhz: int | None = None,
) -> ManualCurveEdit:
    step_mhz = (
        _clock_step_mhz()
        if clock_step_mhz is None
        else max(1, int(clock_step_mhz))
    )
    delta_mhz = int(step_mhz) if int(direction) >= 0 else -int(step_mhz)
    if edit.selected_range_start_voltage_mv is not None:
        return _offset_selected_range_by_delta(edit, delta_mhz=int(delta_mhz))
    selected_voltage_mv = (
        int(edit.selected_voltage_mv)
        if edit.selected_voltage_mv is not None
        else int(edit.anchor_voltage_mv)
    )
    if selected_voltage_mv != int(edit.anchor_voltage_mv):
        selected_clock_mhz = _target_clock_for_voltage(
            edit.plan,
            selected_voltage_mv,
            fallback=(
                edit.selected_clock_mhz
                if edit.selected_clock_mhz is not None
                else edit.anchor_clock_mhz
            ),
        )
        return manual_tune_single_point_edit(
            edit.plan,
            anchor_voltage_mv=int(edit.anchor_voltage_mv),
            anchor_clock_mhz=int(edit.anchor_clock_mhz),
            point_voltage_mv=int(selected_voltage_mv),
            requested_clock_mhz=int(selected_clock_mhz) + int(delta_mhz),
            control_voltage_mvs=edit.control_voltage_mvs,
        )
    return manual_drag_anchor_edit(
        edit.plan,
        anchor_voltage_mv=int(edit.anchor_voltage_mv),
        anchor_clock_mhz=int(edit.anchor_clock_mhz),
        requested_voltage_mv=int(edit.anchor_voltage_mv),
        requested_clock_mhz=int(edit.anchor_clock_mhz) + int(delta_mhz),
        control_voltage_mvs=edit.control_voltage_mvs,
    )


def manual_nudge_selected_voltage(
    edit: ManualCurveEdit,
    *,
    direction: int,
) -> ManualCurveEdit:
    points = _sorted_plan(edit.plan)
    anchor_index = _nearest_voltage_index(points, int(edit.anchor_voltage_mv))
    selected_voltage_mv = (
        int(edit.selected_voltage_mv)
        if edit.selected_voltage_mv is not None
        else int(edit.anchor_voltage_mv)
    )
    selected_index = _nearest_voltage_index(points, selected_voltage_mv)
    if selected_index != anchor_index:
        if selected_index < anchor_index:
            target_index = _clamp(
                int(selected_index) + (1 if int(direction) >= 0 else -1),
                0,
                max(0, int(anchor_index) - 1),
            )
        else:
            target_index = _clamp(
                int(selected_index) + (1 if int(direction) >= 0 else -1),
                min(len(points) - 1, int(anchor_index) + 1),
                len(points) - 1,
            )
        selected_clock_mhz = _target_clock_for_voltage(
            points,
            int(points[selected_index]["voltage_mv"]),
            fallback=(
                edit.selected_clock_mhz
                if edit.selected_clock_mhz is not None
                else int(points[selected_index]["target_mhz"])
            ),
        )
        return manual_tune_single_point_edit(
            points,
            anchor_voltage_mv=int(edit.anchor_voltage_mv),
            anchor_clock_mhz=int(edit.anchor_clock_mhz),
            point_voltage_mv=int(points[selected_index]["voltage_mv"]),
            requested_voltage_mv=int(points[target_index]["voltage_mv"]),
            requested_clock_mhz=int(selected_clock_mhz),
            control_voltage_mvs=edit.control_voltage_mvs,
        )
    target_index = _clamp(
        int(anchor_index) + (1 if int(direction) >= 0 else -1),
        0,
        len(points) - 1,
    )
    return manual_drag_anchor_edit(
        points,
        anchor_voltage_mv=int(edit.anchor_voltage_mv),
        anchor_clock_mhz=int(edit.anchor_clock_mhz),
        requested_voltage_mv=int(points[target_index]["voltage_mv"]),
        requested_clock_mhz=int(edit.anchor_clock_mhz),
        control_voltage_mvs=edit.control_voltage_mvs,
    )


def manual_select_curve_point(
    edit: ManualCurveEdit,
    *,
    voltage_mv: float,
) -> ManualCurveEdit:
    points = _sorted_plan(edit.plan)
    anchor_index = _nearest_voltage_index(points, int(edit.anchor_voltage_mv))
    selected_index = _nearest_voltage_index(
        points,
        round(float(voltage_mv)),
    )
    selected_voltage_mv = int(points[selected_index]["voltage_mv"])
    selected_clock_mhz = (
        int(edit.anchor_clock_mhz)
        if selected_index == anchor_index
        else int(points[selected_index]["target_mhz"])
    )
    return ManualCurveEdit(
        plan=[dict(item) for item in points],
        anchor_voltage_mv=int(points[anchor_index]["voltage_mv"]),
        anchor_clock_mhz=int(edit.anchor_clock_mhz),
        edit_kind=str(edit.edit_kind),
        selected_voltage_mv=int(selected_voltage_mv),
        selected_clock_mhz=int(selected_clock_mhz),
        selected_range_start_voltage_mv=None,
        control_voltage_mvs=_normalized_control_voltage_mvs(
            points,
            edit.control_voltage_mvs,
            int(anchor_index),
        ),
    )


def manual_select_adjacent_point(
    edit: ManualCurveEdit,
    *,
    direction: int,
) -> ManualCurveEdit:
    points = _sorted_plan(edit.plan)
    selected_voltage_mv = (
        int(edit.selected_voltage_mv)
        if edit.selected_voltage_mv is not None
        else int(edit.anchor_voltage_mv)
    )
    selected_index = _nearest_voltage_index(points, int(selected_voltage_mv))
    target_index = _clamp(
        int(selected_index) + (1 if int(direction) >= 0 else -1),
        0,
        len(points) - 1,
    )
    return manual_select_curve_point(
        edit,
        voltage_mv=int(points[target_index]["voltage_mv"]),
    )


def manual_select_range_to_right(edit: ManualCurveEdit) -> ManualCurveEdit:
    points = _sorted_plan(edit.plan)
    anchor_index = _nearest_voltage_index(points, int(edit.anchor_voltage_mv))
    selected_voltage_mv = (
        int(edit.selected_voltage_mv)
        if edit.selected_voltage_mv is not None
        else int(edit.anchor_voltage_mv)
    )
    selected_index = min(
        int(anchor_index),
        _nearest_voltage_index(points, int(selected_voltage_mv)),
    )
    selected_voltage_mv = int(points[selected_index]["voltage_mv"])
    selected_clock_mhz = (
        int(edit.anchor_clock_mhz)
        if selected_index >= anchor_index
        else int(points[selected_index]["target_mhz"])
    )
    return ManualCurveEdit(
        plan=[dict(item) for item in points],
        anchor_voltage_mv=int(points[anchor_index]["voltage_mv"]),
        anchor_clock_mhz=int(edit.anchor_clock_mhz),
        edit_kind=str(edit.edit_kind),
        selected_voltage_mv=int(selected_voltage_mv),
        selected_clock_mhz=int(selected_clock_mhz),
        selected_range_start_voltage_mv=int(selected_voltage_mv),
        control_voltage_mvs=_normalized_control_voltage_mvs(
            points,
            edit.control_voltage_mvs,
            int(anchor_index),
        ),
    )


def manual_offset_selected_range(
    edit: ManualCurveEdit,
    *,
    reference_voltage_mv: float,
    requested_clock_mhz: float,
) -> ManualCurveEdit:
    points = _sorted_plan(edit.plan)
    anchor_index = _nearest_voltage_index(points, int(edit.anchor_voltage_mv))
    range_start_voltage_mv = (
        edit.selected_range_start_voltage_mv
        if edit.selected_range_start_voltage_mv is not None
        else edit.selected_voltage_mv
    )
    if range_start_voltage_mv is None:
        return manual_tune_single_point_edit(
            points,
            anchor_voltage_mv=int(edit.anchor_voltage_mv),
            anchor_clock_mhz=int(edit.anchor_clock_mhz),
            point_voltage_mv=float(reference_voltage_mv),
            requested_clock_mhz=float(requested_clock_mhz),
            control_voltage_mvs=edit.control_voltage_mvs,
        )
    start_index = min(
        int(anchor_index),
        _nearest_voltage_index(points, int(range_start_voltage_mv)),
    )
    reference_index = _clamp(
        _nearest_voltage_index(points, round(float(reference_voltage_mv))),
        int(start_index),
        int(anchor_index),
    )
    current_reference_clock_mhz = (
        int(edit.anchor_clock_mhz)
        if reference_index >= anchor_index
        else int(points[reference_index]["target_mhz"])
    )
    delta_mhz = _snap_clock_mhz(float(requested_clock_mhz)) - int(
        current_reference_clock_mhz
    )
    return _offset_selected_range_by_delta(
        edit,
        delta_mhz=int(delta_mhz),
        range_start_voltage_mv=int(points[start_index]["voltage_mv"]),
    )


def user_edited_profile_payload(
    parent_profile: dict,
    edit: ManualCurveEdit,
    *,
    original_anchor_voltage_mv: int | None = None,
    original_anchor_clock_mhz: int | None = None,
) -> dict:
    payload = {
        key: value
        for key, value in dict(parent_profile).items()
        if key
        not in {
            "profile_id",
            "profile_created_at",
            "path",
            "verified_at",
            "avg_core_clock_mhz",
            "avg_fps",
            "avg_power_w",
            "efficiency_fps_per_w",
            "efficiency_mhz_per_w",
            "watts_per_mhz",
            "final_validation",
            "validation",
            "verification",
        }
    }
    parent_profile_id = str(parent_profile.get("profile_id", "")).strip()
    parent_path = str(parent_profile.get("path", "")).strip()
    payload.update(
        {
            "profile_source": USER_EDITED_PROFILE_SOURCE,
            "display_name": user_edited_display_name(
                lock_clock_mhz=int(edit.anchor_clock_mhz),
                candidate_voltage_mv=int(edit.anchor_voltage_mv),
                memory_offset_mhz=parent_profile.get("memory_offset_mhz"),
            ),
            "candidate_id": (
                f"user-edited-{int(edit.anchor_voltage_mv)}mv-"
                f"{int(edit.anchor_clock_mhz)}mhz"
            ),
            "candidate_voltage_mv": int(edit.anchor_voltage_mv),
            "lock_clock_mhz": int(edit.anchor_clock_mhz),
            "final_verified": False,
            "verification_status": "unverified",
            "requires_verification": True,
            "plan": [dict(item) for item in edit.plan],
            "points": artifact_points(edit.plan),
            "manual_edit": {
                "edit_kind": str(edit.edit_kind),
                "parent_profile_id": parent_profile_id,
                "parent_path": parent_path,
                "original_anchor_voltage_mv": (
                    None
                    if original_anchor_voltage_mv is None
                    else int(original_anchor_voltage_mv)
                ),
                "original_anchor_clock_mhz": (
                    None
                    if original_anchor_clock_mhz is None
                    else int(original_anchor_clock_mhz)
                ),
                "anchor_voltage_mv": int(edit.anchor_voltage_mv),
                "anchor_clock_mhz": int(edit.anchor_clock_mhz),
                "selected_voltage_mv": edit.selected_voltage_mv,
                "selected_clock_mhz": edit.selected_clock_mhz,
                "selected_range_start_voltage_mv": (
                    edit.selected_range_start_voltage_mv
                ),
                "control_voltage_mvs": [int(value) for value in edit.control_voltage_mvs],
            },
        }
    )
    return payload


def _sorted_plan(plan: list[dict]) -> list[dict]:
    points = []
    for raw in plan:
        if not isinstance(raw, dict):
            continue
        try:
            item = dict(raw)
            item["index"] = int(item["index"])
            item["voltage_mv"] = int(item["voltage_mv"])
            item["base_mhz"] = int(item["base_mhz"])
            item["target_mhz"] = int(item["target_mhz"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid V/F curve plan point: {raw}") from exc
        item["new_offset_mhz"] = int(item["target_mhz"]) - int(item["base_mhz"])
        points.append(item)
    if not points:
        raise ValueError("V/F curve plan is empty")
    points.sort(key=lambda item: (int(item["voltage_mv"]), int(item["index"])))
    return points


def _flatten_plan_from_index(
    points: list[dict],
    flatten_index: int,
    flatten_clock_mhz: int,
    *,
    smooth_left: bool = False,
    interpolate_left: bool = False,
    interpolation_start_index: int | None = None,
) -> list[dict]:
    edited = []
    flatten_index = _clamp(int(flatten_index), 0, len(points) - 1)
    flatten_clock_mhz = int(flatten_clock_mhz)
    for index, raw in enumerate(points):
        item = dict(raw)
        target_mhz = int(item["target_mhz"])
        if index >= flatten_index or target_mhz > flatten_clock_mhz:
            target_mhz = flatten_clock_mhz
        item["target_mhz"] = int(target_mhz)
        item["new_offset_mhz"] = int(item["target_mhz"]) - int(item["base_mhz"])
        edited.append(item)
    if interpolate_left:
        _interpolate_left_side_into_flattened_clock(
            edited,
            flatten_index,
            flatten_clock_mhz,
            start_index=interpolation_start_index,
        )
    elif smooth_left:
        _smooth_left_side_into_flattened_clock(
            edited,
            flatten_index,
            flatten_clock_mhz,
        )
    return edited


def _interpolate_left_side_into_flattened_clock(
    points: list[dict],
    flatten_index: int,
    flatten_clock_mhz: int,
    *,
    start_index: int | None = None,
) -> None:
    if flatten_index <= 0:
        return
    start_index = (
        max(0, int(flatten_index) - MANUAL_SMOOTH_LEFT_VOLTAGE_BINS)
        if start_index is None
        else _clamp(int(start_index), 0, int(flatten_index) - 1)
    )
    total_intervals = int(flatten_index) - int(start_index)
    if total_intervals <= 0:
        return
    start_clock_mhz = int(points[start_index]["target_mhz"])
    clock_span_mhz = int(flatten_clock_mhz) - int(start_clock_mhz)
    if clock_span_mhz <= 0:
        return
    for index in range(start_index + 1, int(flatten_index)):
        position = int(index) - int(start_index)
        ramp_clock_mhz = _snap_clock_mhz(
            start_clock_mhz
            + (clock_span_mhz * position / float(total_intervals))
        )
        item = points[index]
        item["target_mhz"] = min(int(flatten_clock_mhz), int(ramp_clock_mhz))
        item["new_offset_mhz"] = int(item["target_mhz"]) - int(item["base_mhz"])


def _smooth_left_side_into_flattened_clock(
    points: list[dict],
    flatten_index: int,
    flatten_clock_mhz: int,
) -> None:
    if flatten_index <= 0:
        return
    clock_step_mhz = _clock_step_mhz()
    if (
        int(flatten_clock_mhz) - int(points[flatten_index - 1]["target_mhz"])
        <= MANUAL_SMOOTH_TRIGGER_CLOCK_BINS * clock_step_mhz
    ):
        return
    start_index = max(0, int(flatten_index) - MANUAL_SMOOTH_LEFT_VOLTAGE_BINS)
    total_intervals = int(flatten_index) - int(start_index)
    if total_intervals <= 0:
        return
    start_clock_mhz = int(points[start_index]["target_mhz"])
    clock_span_mhz = int(flatten_clock_mhz) - int(start_clock_mhz)
    if clock_span_mhz <= 0:
        return
    for index in range(start_index + 1, int(flatten_index)):
        position = int(index) - int(start_index)
        ramp_clock_mhz = _snap_clock_mhz(
            start_clock_mhz
            + (clock_span_mhz * position / float(total_intervals))
        )
        item = points[index]
        item["target_mhz"] = min(
            int(flatten_clock_mhz),
            max(int(item["target_mhz"]), int(ramp_clock_mhz)),
        )
        item["new_offset_mhz"] = int(item["target_mhz"]) - int(item["base_mhz"])


def _offset_selected_range_by_delta(
    edit: ManualCurveEdit,
    *,
    delta_mhz: int,
    range_start_voltage_mv: int | None = None,
) -> ManualCurveEdit:
    points = _sorted_plan(edit.plan)
    anchor_index = _nearest_voltage_index(points, int(edit.anchor_voltage_mv))
    start_voltage_mv = (
        int(range_start_voltage_mv)
        if range_start_voltage_mv is not None
        else (
            int(edit.selected_range_start_voltage_mv)
            if edit.selected_range_start_voltage_mv is not None
            else int(edit.anchor_voltage_mv)
        )
    )
    start_index = min(
        int(anchor_index),
        _nearest_voltage_index(points, int(start_voltage_mv)),
    )
    min_clock_mhz = min(
        int(edit.anchor_clock_mhz),
        *(
            int(point["target_mhz"])
            for point in points[int(start_index) : int(anchor_index)]
        ),
    )
    clamped_delta_mhz = max(-int(min_clock_mhz), int(delta_mhz))
    new_anchor_clock_mhz = max(
        0,
        _snap_clock_mhz(int(edit.anchor_clock_mhz) + int(clamped_delta_mhz)),
    )
    edited = []
    for index, raw in enumerate(points):
        item = dict(raw)
        if int(index) >= int(anchor_index):
            target_mhz = int(new_anchor_clock_mhz)
        elif int(index) >= int(start_index):
            target_mhz = max(
                0,
                _snap_clock_mhz(
                    int(item["target_mhz"]) + int(clamped_delta_mhz)
                ),
            )
        else:
            target_mhz = int(item["target_mhz"])
        item["target_mhz"] = int(target_mhz)
        item["new_offset_mhz"] = int(item["target_mhz"]) - int(item["base_mhz"])
        edited.append(item)
    selected_voltage_mv = int(points[start_index]["voltage_mv"])
    selected_clock_mhz = (
        int(new_anchor_clock_mhz)
        if start_index >= anchor_index
        else int(edited[start_index]["target_mhz"])
    )
    return ManualCurveEdit(
        plan=edited,
        anchor_voltage_mv=int(points[anchor_index]["voltage_mv"]),
        anchor_clock_mhz=int(new_anchor_clock_mhz),
        edit_kind="range-offset",
        selected_voltage_mv=int(selected_voltage_mv),
        selected_clock_mhz=int(selected_clock_mhz),
        selected_range_start_voltage_mv=int(selected_voltage_mv),
        control_voltage_mvs=_normalized_control_voltage_mvs(
            points,
            edit.control_voltage_mvs,
            int(anchor_index),
        ),
    )


def _interpolate_single_point_voltage_move(
    points: list[dict],
    *,
    source_index: int,
    target_index: int,
    lower_bound_index: int,
    upper_bound_index: int,
) -> None:
    if not points:
        return
    source_index = _clamp(int(source_index), 0, len(points) - 1)
    target_index = _clamp(int(target_index), 0, len(points) - 1)
    lower_bound_index = _clamp(int(lower_bound_index), 0, len(points) - 1)
    upper_bound_index = _clamp(int(upper_bound_index), 0, len(points) - 1)
    if int(source_index) == int(target_index):
        return
    if int(target_index) < int(source_index):
        start_index = int(target_index)
        end_index = min(int(upper_bound_index), int(source_index) + 1)
    else:
        start_index = max(int(lower_bound_index), int(source_index) - 1)
        end_index = int(target_index)
    if end_index <= start_index:
        return
    start_voltage_mv = int(points[start_index]["voltage_mv"])
    end_voltage_mv = int(points[end_index]["voltage_mv"])
    start_clock_mhz = int(points[start_index]["target_mhz"])
    end_clock_mhz = int(points[end_index]["target_mhz"])
    voltage_span = int(end_voltage_mv) - int(start_voltage_mv)
    if voltage_span <= 0:
        return
    for index in range(int(start_index) + 1, int(end_index)):
        item = points[index]
        ratio = (
            (int(item["voltage_mv"]) - int(start_voltage_mv))
            / float(voltage_span)
        )
        item["target_mhz"] = _snap_clock_mhz(
            int(start_clock_mhz)
            + (int(end_clock_mhz) - int(start_clock_mhz)) * ratio
        )
        item["new_offset_mhz"] = int(item["target_mhz"]) - int(item["base_mhz"])


def _normalized_control_voltage_mvs(
    points: list[dict],
    control_voltage_mvs: tuple[int, ...] | list[int] | None,
    anchor_index: int,
) -> tuple[int, ...]:
    if not control_voltage_mvs:
        return ()
    anchor_index = _clamp(int(anchor_index), 0, len(points) - 1)
    normalized: list[int] = []
    seen: set[int] = set()
    for raw_voltage_mv in control_voltage_mvs:
        try:
            voltage_mv = round(float(raw_voltage_mv))
        except (TypeError, ValueError):
            continue
        index = _nearest_voltage_index(points, voltage_mv)
        if int(index) >= int(anchor_index):
            continue
        snapped_voltage_mv = int(points[index]["voltage_mv"])
        if snapped_voltage_mv in seen:
            continue
        seen.add(snapped_voltage_mv)
        normalized.append(snapped_voltage_mv)
    return tuple(sorted(normalized))


def _control_voltage_mvs_with_indexes(
    points: list[dict],
    control_voltage_mvs: tuple[int, ...] | list[int] | None,
    anchor_index: int,
    *,
    add_index: int | None = None,
    remove_index: int | None = None,
) -> tuple[int, ...]:
    anchor_index = _clamp(int(anchor_index), 0, len(points) - 1)
    controls = set(
        _normalized_control_voltage_mvs(
            points,
            control_voltage_mvs,
            int(anchor_index),
        )
    )
    if remove_index is not None:
        remove_index = _clamp(int(remove_index), 0, len(points) - 1)
        controls.discard(int(points[remove_index]["voltage_mv"]))
    if add_index is not None:
        add_index = _clamp(int(add_index), 0, len(points) - 1)
        if int(add_index) < int(anchor_index):
            controls.add(int(points[add_index]["voltage_mv"]))
    return tuple(
        int(points[index]["voltage_mv"])
        for index in range(int(anchor_index))
        if int(points[index]["voltage_mv"]) in controls
    )


def _nearest_voltage_index(points: list[dict], voltage_mv: int) -> int:
    if not points:
        raise ValueError("V/F curve plan is empty")
    best_index = 0
    best_distance = None
    for index, point in enumerate(points):
        distance = abs(int(point["voltage_mv"]) - int(voltage_mv))
        if best_distance is None or distance < best_distance:
            best_index = index
            best_distance = distance
    return int(best_index)


def _clock_step_mhz() -> int:
    return max(1, int(AUTO_UV_CURVE_TUNING.clock_step_mhz))


def _snap_clock_mhz(clock_mhz: float) -> int:
    step = _clock_step_mhz()
    return int(round(float(clock_mhz) / float(step)) * step)


def _target_clock_for_voltage(
    plan: list[dict],
    voltage_mv: int,
    *,
    fallback: int,
) -> int:
    for raw in list(plan or []):
        try:
            if int(raw["voltage_mv"]) == int(voltage_mv):
                return int(raw["target_mhz"])
        except (KeyError, TypeError, ValueError):
            continue
    return int(fallback)


def _clamp(value: int, minimum: int, maximum: int) -> int:
    maximum = max(maximum, minimum)
    return max(int(minimum), min(int(maximum), int(value)))
