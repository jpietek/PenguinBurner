"""Coverage for profiles/uv/profile_vf_curve.py: profile payload -> curve point/plan parsing.

Most functions are pure; the cache and save helpers use tmp files and
monkeypatched dependencies so no live GPU/profile store is touched.
"""

from __future__ import annotations

import json

import profiles.uv.profile_vf_curve as cp


# --- low-level value parsing --------------------------------------------------


def test_curve_point_from_value_forms() -> None:
    assert cp.curve_point_from_value([900, 2500]) == (900.0, 2500.0)
    assert cp.curve_point_from_value({"voltage_mv": 900, "clock_mhz": 2500}) == (
        900.0,
        2500.0,
    )
    assert cp.curve_point_from_value({"x": 900, "y": 2500}) == (900.0, 2500.0)
    assert cp.curve_point_from_value("nope") is None


def test_base_curve_point_prefers_base_mhz() -> None:
    assert cp.base_curve_point_from_value(
        {"voltage_mv": 900, "base_mhz": 2400, "clock_mhz": 2500}
    ) == (900.0, 2400.0)
    assert cp.base_curve_point_from_value(["not", "a", "dict"]) is None


def test_finite_curve_point_rejects_bad_and_infinite() -> None:
    assert cp.finite_curve_point(900, 2500) == (900.0, 2500.0)
    assert cp.finite_curve_point("x", 2500) is None
    assert cp.finite_curve_point(900, float("inf")) is None


def test_curve_point_value_skips_empty_and_missing() -> None:
    point = {"voltage": "", "mv": 900}
    assert cp.curve_point_value(point, "voltage", "mv") == 900
    assert cp.curve_point_value(point, "absent") is None


# --- list/payload aggregation -------------------------------------------------


def test_curve_points_from_values_sorts_and_filters() -> None:
    values = [{"voltage_mv": 950, "clock_mhz": 2600}, [900, 2500], "junk"]
    assert cp.curve_points_from_values(values) == [(900.0, 2500.0), (950.0, 2600.0)]
    assert cp.curve_points_from_values("not-a-list") == []


def test_base_curve_points_from_values() -> None:
    values = [{"voltage_mv": 900, "base_mhz": 2400}]
    assert cp.base_curve_points_from_values(values) == [(900.0, 2400.0)]
    assert cp.base_curve_points_from_values(None) == []


def test_curve_plan_from_values_builds_and_defaults_offset() -> None:
    values = [
        {"index": 1, "voltage_mv": 900, "base_mhz": 2400, "target_mhz": 2500},
        {"index": 0, "voltage_mv": 850, "base_mhz": 2300, "target_mhz": 2350,
         "new_offset_mhz": "bad"},
        {"missing": "fields"},
    ]
    plan = cp.curve_plan_from_values(values)
    assert [item["voltage_mv"] for item in plan] == [850, 900]
    assert plan[0]["new_offset_mhz"] == 50  # fallback target-base when value bad
    assert plan[1]["new_offset_mhz"] == 100  # 2500 - 2400
    assert cp.curve_plan_from_values("x") == []


def test_curve_plan_from_payload_and_base_points_payload() -> None:
    payload = {"plan": [{"index": 0, "voltage_mv": 900, "base_mhz": 2400, "target_mhz": 2500}]}
    assert cp.curve_plan_from_payload(payload)[0]["target_mhz"] == 2500
    assert cp.curve_plan_from_payload(
        {"materialization": {"points": [{"index": 0, "voltage_mv": 900,
                                          "base_mhz": 2400, "target_mhz": 2500}]}}
    )
    assert cp.curve_plan_from_payload("x") == []
    assert cp.base_curve_points_from_payload({"points": [{"voltage_mv": 900, "base_mhz": 2400}]})
    assert cp.base_curve_points_from_payload("x") == []


# --- profile-level helpers (with payload fallback) ----------------------------


def test_profile_curve_plan_and_base_points_fallback(monkeypatch) -> None:
    payload = {"plan": [{"index": 0, "voltage_mv": 900, "base_mhz": 2400, "target_mhz": 2500}],
               "points": [{"voltage_mv": 900, "base_mhz": 2400}]}
    monkeypatch.setattr(cp, "profile_payload_from_path", lambda profile: payload)
    assert cp.profile_curve_plan({})[0]["voltage_mv"] == 900
    assert cp.profile_base_curve_points({}) == [(900.0, 2400.0)]


# --- cache + save -------------------------------------------------------------


def test_cache_roundtrip(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "base-vf-curve.json"
    monkeypatch.setattr(cp, "base_curve_cache_path", lambda: cache)
    monkeypatch.setattr(cp, "runtime_gpu_index", lambda _path: 0)

    cp.save_cached_base_curve_points([(900.0, 2500.0), (850.0, 2400.0)])
    loaded = cp.load_cached_base_curve_points()
    assert loaded == [(850.0, 2400.0), (900.0, 2500.0)]


def test_save_cache_skips_when_no_points(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "base-vf-curve.json"
    monkeypatch.setattr(cp, "base_curve_cache_path", lambda: cache)
    monkeypatch.setattr(cp, "runtime_gpu_index", lambda _path: 0)
    cp.save_cached_base_curve_points([])
    assert not cache.exists()


def test_load_cache_missing_file_returns_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cp, "base_curve_cache_path", lambda: tmp_path / "absent.json")
    assert cp.load_cached_base_curve_points() == []


def test_load_cache_drops_other_gpu(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "base-vf-curve.json"
    cache.write_text(
        json.dumps({"gpu_index": 1, "points": [[900, 2500]]}), encoding="utf-8"
    )
    monkeypatch.setattr(cp, "base_curve_cache_path", lambda: cache)
    monkeypatch.setattr(cp, "runtime_gpu_index", lambda _path: 0)
    assert cp.load_cached_base_curve_points() == []


def test_load_cache_non_dict_payload(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "base-vf-curve.json"
    cache.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    monkeypatch.setattr(cp, "base_curve_cache_path", lambda: cache)
    assert cp.load_cached_base_curve_points() == []


def test_save_edited_curve_profile(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cp, "profile_payload_from_path", lambda profile: {"base": 1})
    monkeypatch.setattr(
        cp,
        "user_edited_profile_payload",
        lambda parent, edit, **kw: {"edited": True, "anchor": kw},
    )
    archived = tmp_path / "edited.json"
    monkeypatch.setattr(cp, "archive_auto_uv_profile", lambda payload: archived)

    path, payload = cp.save_edited_curve_profile(
        {"path": "x"},
        edit=object(),
        original_anchor_voltage_mv=900,
        original_anchor_clock_mhz=2500,
    )
    assert path == archived
    assert payload["edited"] is True


def test_save_cached_base_curve_points_writes_atomically(tmp_path, monkeypatch) -> None:
    """A crash mid-write must not leave a torn cache: the write lands via a
    temp file + rename, so no partial file and no leftover .tmp."""
    import json

    import profiles.uv.profile_vf_curve as cp

    cache = tmp_path / "base-vf-curve.json"
    monkeypatch.setattr(cp, "base_curve_cache_path", lambda: cache)
    cp.save_cached_base_curve_points([(800.0, 2000.0), (900.0, 2500.0)])

    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["points"][1] == {"voltage_mv": 900.0, "clock_mhz": 2500.0}
    # Atomic replace leaves no temporary file behind.
    assert [p.name for p in tmp_path.iterdir()] == ["base-vf-curve.json"]
