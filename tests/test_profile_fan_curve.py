"""Coverage for profiles/uv/profile_fan_curve.py: fan payload/point parsing + IO helpers.

Pure parsing tested directly; afterburner/archive/ownership/path dependencies
are monkeypatched so no real GPU/profile store is touched.
"""

from __future__ import annotations

import json

import profiles.uv.profile_fan_curve as fp
import profiles.uv.profile_store as profile_store

# --- low-level point parsing --------------------------------------------------


def test_fan_point_from_values_guards() -> None:
    assert fp.fan_point_from_values(50, 40) == (50.0, 40.0)
    assert fp.fan_point_from_values("x", 40) is None
    assert fp.fan_point_from_values(50, float("inf")) is None
    assert fp.fan_point_from_values(50, 150) is None  # out of 0..100


def test_fan_point_from_sequence_or_mapping() -> None:
    assert fp.fan_point_from_sequence_or_mapping({"temperature_c": 50, "fan_pct": 40}) == (
        50.0,
        40.0,
    )
    assert fp.fan_point_from_sequence_or_mapping([50, 40]) == (50.0, 40.0)
    assert fp.fan_point_from_sequence_or_mapping("bad") is None


def test_sorted_unique_fan_points_dedupes() -> None:
    points = [(70, 60), (30, 20), (70.001, 60.0)]
    assert fp.sorted_unique_fan_points(points) == [(30.0, 20.0), (70.0, 60.0)]


def test_fan_curve_and_measurement_points_from_values() -> None:
    assert fp.fan_curve_points_from_values([[30, 20], {"temp_c": 70, "fan_pct": 60}]) == [
        (30.0, 20.0),
        (70.0, 60.0),
    ]
    assert fp.fan_curve_points_from_values("x") == []
    assert fp.fan_measurement_points(
        [{"temp_c": 40, "fan_pct": 30}, [50, 45], "junk"]
    ) == [(40.0, 30.0), (50.0, 45.0)]
    assert fp.fan_measurement_points(None) == []


def test_fan_measurement_point_keys() -> None:
    assert fp.fan_measurement_point({"avg_temperature_c": 55, "avg_fan_speed_pct": 50}) == (
        55.0,
        50.0,
    )


# --- payload-level parsing ----------------------------------------------------


def test_fan_curve_points_from_payload_priority() -> None:
    assert fp.fan_curve_points_from_payload({"fan": {"curve": [[30, 20]]}}) == [(30.0, 20.0)]
    assert fp.fan_curve_points_from_payload({"points": [[40, 30]]}) == [(40.0, 30.0)]
    assert fp.fan_curve_points_from_payload("x") == []


def test_fan_measurement_points_from_payload() -> None:
    assert fp.fan_measurement_points_from_payload(
        {"telemetry": {"measured_fan_points": [{"temp_c": 40, "fan_pct": 30}]}}
    ) == [(40.0, 30.0)]
    assert fp.fan_measurement_points_from_payload({"measured_points": [[50, 45]]}) == [
        (50.0, 45.0)
    ]
    assert fp.fan_measurement_points_from_payload("x") == []


def test_fan_curve_target_point_from_payload_sources() -> None:
    assert fp.fan_curve_target_point_from_payload(
        {"load_anchor_temperature_c": 60, "load_anchor_fan_speed_pct": 55}
    ) == (60.0, 55.0)
    assert fp.fan_curve_target_point_from_payload(
        {"telemetry": {"final": {"max_temperature_c": 70, "max_fan_speed_pct": 65}}}
    ) == (70.0, 65.0)
    # Fallback: last curve point.
    assert fp.fan_curve_target_point_from_payload({"points": [[30, 20], [70, 60]]}) == (
        70.0,
        60.0,
    )
    assert fp.fan_curve_target_point_from_payload("x") is None


def test_silent_runtime_fields_and_embedded_payload() -> None:
    assert fp.fan_payload_has_silent_runtime_fields(
        {"points": [[30, 20]], "loaded_temperature_c": 60}
    ) is True
    assert fp.fan_payload_has_silent_runtime_fields({"points": [[30, 20]]}) is False
    assert fp.fan_payload_has_silent_runtime_fields("x") is False

    assert fp.embedded_fan_payload({"fan_curve_payload": {"points": [[30, 20]]}}) == {
        "points": [[30, 20]]
    }
    assert fp.embedded_fan_payload({"points": [[30, 20]]}) == {"points": [[30, 20]]}
    assert fp.embedded_fan_payload("x") is None


# --- profile-level helpers ----------------------------------------------------


def test_profile_fan_payload_embedded(monkeypatch) -> None:
    monkeypatch.setattr(fp, "profile_payload_from_path", lambda profile: None)
    monkeypatch.setattr(fp, "matching_current_auto_uv_fan_payload", lambda profile: None)
    payload = fp.profile_fan_payload({"fan": {"curve": [[30, 20]]}})
    assert payload == {"fan": {"curve": [[30, 20]]}}

def test_profile_fan_curve_accessors(monkeypatch) -> None:
    monkeypatch.setattr(
        fp,
        "profile_fan_payload",
        lambda profile: {"points": [[30, 20], [70, 60]], "loaded_temperature_c": 60,
                         "load_anchor_temperature_c": 65, "load_anchor_fan_speed_pct": 58},
    )
    assert fp.profile_fan_curve_points({}) == [(30.0, 20.0), (70.0, 60.0)]
    assert fp.profile_fan_curve_target_point({}) == (65.0, 58.0)
    monkeypatch.setattr(fp, "profile_fan_payload", lambda profile: None)
    assert fp.profile_fan_curve_points({}) == []
    assert fp.profile_fan_measurement_points({}) == []
    assert fp.profile_fan_curve_target_point({}) is None


# --- file IO + matching -------------------------------------------------------


def test_read_json_object_and_payload_from_path(tmp_path) -> None:
    good = tmp_path / "p.json"
    good.write_text(json.dumps({"plan": [1]}), encoding="utf-8")
    assert profile_store.read_json_object(good) == {"plan": [1]}
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert profile_store.read_json_object(bad) is None
    assert profile_store.read_json_object(tmp_path / "absent.json") is None
    assert profile_store.profile_payload_from_path({"path": str(good)}) == {"plan": [1]}
    assert profile_store.profile_payload_from_path({}) is None


def test_profile_payload_has_saved_vf_curve_and_archive_id() -> None:
    assert fp.profile_payload_has_saved_vf_curve({"plan": [1]}) is True
    assert fp.profile_payload_has_saved_vf_curve({}) is False
    assert profile_store.profile_id_from_archive_path("auto-uv-profile-abc.json") == "abc"
    assert profile_store.profile_id_from_archive_path("/tmp/other.json") == "other"


def test_write_payload_and_matching(monkeypatch, tmp_path) -> None:
    target = tmp_path / "auto-uv-fan-curve.json"
    monkeypatch.setattr(fp, "auto_uv_fan_curve_payload_path", lambda: target)
    monkeypatch.setattr(
        "common.atomic_write.claim_desktop_user_ownership", lambda *a, **k: None
    )

    import pytest

    with pytest.raises(TypeError):
        fp.write_auto_uv_fan_curve_payload("not-a-dict")

    payload = {
        "telemetry": {"measured_fan_points": [{"voltage_mv": 900, "clock_mhz": 2500}]},
        "points": [[30, 20]],
    }
    fp.write_auto_uv_fan_curve_payload(payload)
    assert target.is_file()

    profile = {"candidate_voltage_mv": 900, "lock_clock_mhz": 2500}
    assert fp.fan_payload_matches_profile(payload, profile) is True
    assert fp.fan_payload_matches_profile(payload, {"candidate_voltage_mv": "x"}) is False
    assert fp.matching_current_auto_uv_fan_payload(profile) == payload
    assert fp.matching_current_auto_uv_fan_payload(
        {"candidate_voltage_mv": 1, "lock_clock_mhz": 1}
    ) is None


def test_sync_and_save_edited(monkeypatch, tmp_path) -> None:
    written = []
    monkeypatch.setattr(fp, "write_auto_uv_fan_curve_payload", lambda payload: written.append(payload) or tmp_path / "f.json")

    monkeypatch.setattr(
        fp, "profile_fan_payload", lambda profile: {"points": [[30, 20]], "loaded_temperature_c": 60}
    )
    assert fp.sync_profile_fan_payload({}) is True
    monkeypatch.setattr(fp, "profile_fan_payload", lambda profile: None)
    assert fp.sync_profile_fan_payload({}) is False

    # save_edited_fan_profile: requires a saved VF curve in the parent payload.
    monkeypatch.setattr(fp, "profile_payload_from_path", lambda profile: {"plan": [1]})
    monkeypatch.setattr(fp, "profile_fan_payload", lambda profile: {"points": [[30, 20]]})
    monkeypatch.setattr(
        fp,
        "user_edited_fan_curve_profile_payload",
        lambda parent, fan, edit, **kw: {"fan_curve_payload": {"points": [[35, 25]]}},
    )
    archived = tmp_path / "archived.json"
    monkeypatch.setattr(fp, "archive_auto_uv_profile", lambda payload: archived)
    path, payload = fp.save_edited_fan_profile({"path": "x"}, object(), [(30, 20)])
    assert path == archived
    assert written  # fan_curve_payload was written out

    import pytest

    monkeypatch.setattr(fp, "profile_payload_from_path", lambda profile: {})
    with pytest.raises(ValueError):
        fp.save_edited_fan_profile({}, object(), [])
