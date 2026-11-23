from __future__ import annotations

import json

import pytest

import profiles.uv.profile_tiers as tiers_mod
from profiles.uv.profile_tiers import (
    PROFILE_TIER_BALANCED,
    PROFILE_TIER_EFFICIENCY,
    PROFILE_TIER_PERFORMANCE,
    adaptive_tier_available,
    assigned_tier_for_profile,
    available_adaptive_tiers,
    generated_profile_tier,
    load_profile_tier_disabled_profile_ids,
    load_profile_tier_assignments,
    migrate_legacy_profile_tier_to_gpu,
    normalize_profile_tier,
    profile_tier_disabled,
    profile_tier_summary_fields,
    profile_tier_label,
    resolve_profile_tier_profiles,
    save_profile_tier_assignment,
    save_profile_tier_none_assignment,
)


def _profile(profile_id: str, tier: str, created_at: str) -> dict:
    return {
        "profile_id": profile_id,
        "candidate_id": profile_id,
        "profile_created_at": created_at,
        "final_verified": True,
        "generated_profile_tier": tier,
    }


def _measured_profile(
    profile_id: str,
    tier: str,
    created_at: str,
    *,
    fpsw: float,
    fps: float,
) -> dict:
    profile = _profile(profile_id, tier, created_at)
    profile.update({"efficiency_fps_per_w": fpsw, "avg_fps": fps})
    return profile


def test_profile_tier_infers_legacy_balanced_from_tail_shape() -> None:
    assert generated_profile_tier({"auto_uv_mode": "efficiency", "tail_rise_bins": 4}) == PROFILE_TIER_BALANCED
    assert generated_profile_tier({"auto_uv_mode": "efficiency", "tail_rise_bins": 0}) == PROFILE_TIER_EFFICIENCY
    assert profile_tier_label("perf") == "Performance"


def test_normalize_profile_tier_keeps_adaptive_unmapped() -> None:
    # 'adaptive' is a scan mode, not one of the three tiers; adaptive runs are
    # made tier-agnostic at the crash-marker boundary (auto_uv_run_profile_tier
    # and the unsafe-cache readers), not by mapping the mode to a tier here.
    assert normalize_profile_tier("adaptive") == ""


def test_profile_tier_assignment_is_one_tier_per_profile(tmp_path) -> None:
    path = tmp_path / "assignments.json"

    save_profile_tier_assignment("profile-a", "efficiency", path=path)
    assignments = save_profile_tier_assignment("profile-a", "performance", path=path)

    assert assignments == {PROFILE_TIER_PERFORMANCE: "profile-a"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["tiers"] == {"performance": "profile-a"}
    assert load_profile_tier_assignments(path) == {PROFILE_TIER_PERFORMANCE: "profile-a"}


def test_profile_tier_none_assignment_disables_generated_tier(tmp_path) -> None:
    path = tmp_path / "assignments.json"

    save_profile_tier_assignment("profile-a", "performance", path=path)
    assignments = save_profile_tier_assignment("profile-a", "none", path=path)

    assert assignments == {}
    assert load_profile_tier_assignments(path) == {}
    assert load_profile_tier_disabled_profile_ids(path) == {"profile-a"}
    fields = profile_tier_summary_fields(
        _profile("profile-a", "Performance", "2026-06-04T12:00:00+00:00"),
        assignments={},
        disabled_profile_ids={"profile-a"},
    )
    assert fields["profile_tier"] == ""
    assert fields["profile_tier_key"] == ""
    assert fields["profile_tier_disabled"] is True


def test_profile_tier_assignments_are_isolated_by_gpu_uuid(tmp_path) -> None:
    path = tmp_path / "assignments.json"

    save_profile_tier_assignment(
        "profile-a",
        "performance",
        path=path,
        gpu_uuid="GPU-a",
    )
    save_profile_tier_assignment(
        "profile-b",
        "performance",
        path=path,
        gpu_uuid="GPU-b",
    )
    save_profile_tier_none_assignment(
        "disabled-a",
        path=path,
        gpu_uuid="GPU-a",
    )

    assert load_profile_tier_assignments(path, gpu_uuid="GPU-a") == {
        PROFILE_TIER_PERFORMANCE: "profile-a"
    }
    assert load_profile_tier_assignments(path, gpu_uuid="gpu-B") == {
        PROFILE_TIER_PERFORMANCE: "profile-b"
    }
    assert load_profile_tier_disabled_profile_ids(
        path, gpu_uuid="GPU-a"
    ) == {"disabled-a"}
    assert load_profile_tier_disabled_profile_ids(path, gpu_uuid="GPU-b") == set()
    assert load_profile_tier_assignments(path) == {}


def test_binding_migrates_legacy_tier_assignment_to_gpu(tmp_path) -> None:
    path = tmp_path / "assignments.json"
    save_profile_tier_assignment("profile-a", "balanced", path=path)

    migrate_legacy_profile_tier_to_gpu("profile-a", "GPU-a", path=path)

    assert load_profile_tier_assignments(path) == {}
    assert load_profile_tier_assignments(path, gpu_uuid="GPU-a") == {
        PROFILE_TIER_BALANCED: "profile-a"
    }


def test_binding_does_not_overwrite_existing_gpu_tier_assignment(tmp_path) -> None:
    path = tmp_path / "assignments.json"
    save_profile_tier_assignment("legacy-profile", "balanced", path=path)
    save_profile_tier_assignment(
        "gpu-profile",
        "balanced",
        path=path,
        gpu_uuid="GPU-a",
    )

    migrate_legacy_profile_tier_to_gpu("legacy-profile", "GPU-a", path=path)

    assert load_profile_tier_assignments(path) == {}
    assert load_profile_tier_assignments(path, gpu_uuid="GPU-a") == {
        PROFILE_TIER_BALANCED: "gpu-profile"
    }


def test_resolve_profile_tiers_only_uses_selected_gpu_group(tmp_path) -> None:
    path = tmp_path / "assignments.json"
    profile_a = _profile("profile-a", "Performance", "2026-06-01T12:00:00+00:00")
    profile_a["gpu_identity"] = {"uuid": "GPU-a", "name": "A"}
    profile_b = _profile("profile-b", "Performance", "2026-06-02T12:00:00+00:00")
    profile_b["gpu_identity"] = {"uuid": "GPU-b", "name": "B"}
    save_profile_tier_assignment(
        "profile-a", "performance", path=path, gpu_uuid="GPU-a"
    )

    resolved = resolve_profile_tier_profiles(
        [profile_a, profile_b],
        assignments=load_profile_tier_assignments(path, gpu_uuid="GPU-a"),
        gpu_uuid="GPU-a",
    )

    assert resolved[PROFILE_TIER_PERFORMANCE] == profile_a


def test_resolve_selected_gpu_can_include_legacy_without_other_gpu(monkeypatch) -> None:
    legacy = _profile("legacy", "Balanced", "2026-06-01T12:00:00+00:00")
    profile_a = _profile("profile-a", "Performance", "2026-06-02T12:00:00+00:00")
    profile_a["gpu_identity"] = {"uuid": "GPU-a", "name": "A"}
    profile_b = _profile("profile-b", "Efficiency", "2026-06-03T12:00:00+00:00")
    profile_b["gpu_identity"] = {"uuid": "GPU-b", "name": "B"}
    monkeypatch.setattr(
        tiers_mod,
        "load_profile_tier_assignments",
        lambda *, gpu_uuid="": (
            {PROFILE_TIER_PERFORMANCE: "profile-a"}
            if gpu_uuid
            else {PROFILE_TIER_BALANCED: "legacy"}
        ),
    )
    monkeypatch.setattr(
        tiers_mod,
        "load_profile_tier_disabled_profile_ids",
        lambda *, gpu_uuid="": set(),
    )

    resolved = resolve_profile_tier_profiles(
        [legacy, profile_a, profile_b],
        gpu_uuid="GPU-a",
        include_legacy_profiles=True,
    )

    assert resolved[PROFILE_TIER_BALANCED] == legacy
    assert resolved[PROFILE_TIER_PERFORMANCE] == profile_a
    assert resolved[PROFILE_TIER_EFFICIENCY] is None


def test_adaptive_tier_available_is_scoped_to_the_game_gpu(monkeypatch) -> None:
    profile_b = _profile("profile-b", "Efficiency", "2026-06-03T12:00:00+00:00")
    profile_b["gpu_identity"] = {"uuid": "GPU-b", "name": "B"}
    unverified = _profile("draft", "Performance", "2026-06-04T12:00:00+00:00")
    unverified["final_verified"] = False
    monkeypatch.setattr(
        tiers_mod, "load_profile_tier_assignments", lambda *, gpu_uuid="": {}
    )
    monkeypatch.setattr(
        tiers_mod, "load_profile_tier_disabled_profile_ids", lambda *, gpu_uuid="": set()
    )

    assert not adaptive_tier_available([])
    assert not adaptive_tier_available([unverified])
    assert adaptive_tier_available([profile_b], gpu_uuid="GPU-b")
    assert not adaptive_tier_available([profile_b], gpu_uuid="GPU-a")


def test_resolve_profile_tier_profiles_prefers_pinned_then_latest_generated() -> None:
    profiles = [
        _profile("old-eff", "Efficiency", "2026-06-01T12:00:00+00:00"),
        _profile("new-eff", "Efficiency", "2026-06-02T12:00:00+00:00"),
        _profile("bal", "Balanced", "2026-06-03T12:00:00+00:00"),
        _profile("perf", "Performance", "2026-06-04T12:00:00+00:00"),
    ]

    resolved = resolve_profile_tier_profiles(
        profiles,
        assignments={PROFILE_TIER_EFFICIENCY: "old-eff"},
    )

    assert resolved[PROFILE_TIER_EFFICIENCY]["profile_id"] == "old-eff"
    assert resolved[PROFILE_TIER_BALANCED]["profile_id"] == "bal"
    assert resolved[PROFILE_TIER_PERFORMANCE]["profile_id"] == "perf"
    assert available_adaptive_tiers(resolved) == [
        PROFILE_TIER_EFFICIENCY,
        PROFILE_TIER_BALANCED,
        PROFILE_TIER_PERFORMANCE,
    ]


def test_resolve_profile_tier_profiles_skips_none_assignment() -> None:
    profiles = [
        _profile("eff", "Efficiency", "2026-06-02T12:00:00+00:00"),
        _profile("bal", "Balanced", "2026-06-03T12:00:00+00:00"),
        _profile("perf", "Performance", "2026-06-04T12:00:00+00:00"),
    ]

    resolved = resolve_profile_tier_profiles(
        profiles,
        assignments={},
        disabled_profile_ids={"bal"},
    )

    assert resolved[PROFILE_TIER_EFFICIENCY]["profile_id"] == "eff"
    assert resolved[PROFILE_TIER_BALANCED] is None
    assert resolved[PROFILE_TIER_PERFORMANCE]["profile_id"] == "perf"
    assert available_adaptive_tiers(resolved) == [
        PROFILE_TIER_EFFICIENCY,
        PROFILE_TIER_PERFORMANCE,
    ]


def test_resolve_profile_tier_profiles_scores_balanced_by_fpsw_and_fps() -> None:
    profiles = [
        _measured_profile(
            "balanced-efficiency",
            "Balanced",
            "2026-06-04T12:00:00+00:00",
            fpsw=0.70,
            fps=150.0,
        ),
        _measured_profile(
            "balanced-fps",
            "Balanced",
            "2026-06-05T12:00:00+00:00",
            fpsw=0.60,
            fps=170.0,
        ),
        _measured_profile(
            "balanced-blend",
            "Balanced",
            "2026-06-03T12:00:00+00:00",
            fpsw=0.67,
            fps=165.0,
        ),
    ]

    resolved = resolve_profile_tier_profiles(profiles, assignments={})

    assert resolved[PROFILE_TIER_BALANCED]["profile_id"] == "balanced-blend"


def test_resolve_profile_tier_profiles_uses_latest_for_balanced_score_tie() -> None:
    profiles = [
        _measured_profile(
            "balanced-old",
            "Balanced",
            "2026-06-03T12:00:00+00:00",
            fpsw=0.65,
            fps=160.0,
        ),
        _measured_profile(
            "balanced-new",
            "Balanced",
            "2026-06-05T12:00:00+00:00",
            fpsw=0.65,
            fps=160.0,
        ),
    ]

    resolved = resolve_profile_tier_profiles(profiles, assignments={})

    assert resolved[PROFILE_TIER_BALANCED]["profile_id"] == "balanced-new"


# --- generated_profile_tier: mode / profile_curve / tail thresholds ---


def test_generated_profile_tier_mode_performance() -> None:
    # auto_uv_mode performance with no explicit tier key -> performance (line 100-101)
    assert (
        generated_profile_tier({"auto_uv_mode": "performance"})
        == PROFILE_TIER_PERFORMANCE
    )


def test_generated_profile_tier_profile_curve_performance_sweep() -> None:
    # profile_curve performance-sweep -> performance (line 104-105)
    assert (
        generated_profile_tier({"profile_curve": "performance-sweep"})
        == PROFILE_TIER_PERFORMANCE
    )


def test_generated_profile_tier_profile_curve_auto_oc() -> None:
    # profile_curve auto-oc -> performance (line 104-105)
    assert (
        generated_profile_tier({"profile_curve": "auto-oc"})
        == PROFILE_TIER_PERFORMANCE
    )


def test_generated_profile_tier_tail_rise_bins_from_nested_flatten_target() -> None:
    # tail_rise_bins absent at top level but present in nested flatten_target dict
    # (line 109-111), and >= 6 -> performance (line 113-114)
    assert (
        generated_profile_tier({"flatten_target": {"tail_rise_bins": 6}})
        == PROFILE_TIER_PERFORMANCE
    )


def test_generated_profile_tier_tail_rise_bins_efficiency() -> None:
    # tail_rise_bins below balanced threshold -> efficiency (line 117)
    assert (
        generated_profile_tier({"tail_rise_bins": 0})
        == PROFILE_TIER_EFFICIENCY
    )


def test_generated_profile_tier_mode_efficiency_fallback() -> None:
    # No tier keys / curve / tail bins, mode efficiency -> efficiency (line 119-120)
    assert (
        generated_profile_tier({"auto_uv_mode": "efficiency"})
        == PROFILE_TIER_EFFICIENCY
    )


def test_generated_profile_tier_mode_balanced_fallback() -> None:
    # mode balanced -> balanced (line 121-122)
    assert (
        generated_profile_tier({"auto_uv_mode": "balanced"})
        == PROFILE_TIER_BALANCED
    )


# --- load_profile_tier_assignments dedup / disabled skip (line 148) ---


def test_load_profile_tier_assignments_skips_duplicate_and_disabled(tmp_path) -> None:
    path = tmp_path / "assignments.json"
    payload = {
        "format_version": 2,
        "tiers": {
            "efficiency": "shared-profile",
            "balanced": "shared-profile",  # duplicate profile_id -> skipped (line 148)
            "performance": "disabled-profile",  # disabled -> skipped (line 148)
        },
        "disabled_profile_ids": ["disabled-profile"],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    assignments = load_profile_tier_assignments(path)

    # Only the first (efficiency) wins for the shared id; balanced + performance dropped.
    assert assignments == {PROFILE_TIER_EFFICIENCY: "shared-profile"}


# --- ValueError raises (lines 169, 173, 198) ---


def test_save_profile_tier_assignment_requires_profile_id(tmp_path) -> None:
    path = tmp_path / "assignments.json"
    with pytest.raises(ValueError, match="profile_id is required"):
        save_profile_tier_assignment("   ", "efficiency", path=path)


def test_save_profile_tier_assignment_requires_known_tier(tmp_path) -> None:
    path = tmp_path / "assignments.json"
    # Non-empty profile, not a "none" alias, but tier does not normalize -> tier required.
    with pytest.raises(ValueError, match="profile tier is required"):
        save_profile_tier_assignment("profile-a", "nonsense-tier", path=path)


def test_save_profile_tier_none_assignment_requires_profile_id(tmp_path) -> None:
    path = tmp_path / "assignments.json"
    with pytest.raises(ValueError, match="profile_id is required"):
        save_profile_tier_none_assignment("", path=path)


# --- profile_tier_disabled inline flag (line 241) ---


def test_profile_tier_disabled_honors_inline_flag() -> None:
    # _truthy(profile_tier_disabled) short-circuits to True without touching disk.
    assert profile_tier_disabled(
        {"profile_id": "p1", "profile_tier_disabled": "true"},
        disabled_profile_ids=set(),
    )


# --- assigned_tier_for_profile match (line 263) ---


def test_assigned_tier_for_profile_returns_matching_tier() -> None:
    tier = assigned_tier_for_profile(
        {"profile_id": "p1"},
        assignments={PROFILE_TIER_PERFORMANCE: "p1"},
    )
    assert tier == PROFILE_TIER_PERFORMANCE


# --- available_adaptive_tiers dedup continue (line 359) ---


def test_available_adaptive_tiers_dedups_shared_profile() -> None:
    shared = {"profile_id": "shared"}
    resolved = {
        PROFILE_TIER_EFFICIENCY: shared,
        PROFILE_TIER_BALANCED: shared,  # same profile_id -> skipped (line 359)
        PROFILE_TIER_PERFORMANCE: {"profile_id": "perf"},
    }
    assert available_adaptive_tiers(resolved) == [
        PROFILE_TIER_EFFICIENCY,
        PROFILE_TIER_PERFORMANCE,
    ]


# --- _optional_float except branch (lines 468-469), exercised via balanced score ---


def test_balanced_score_tolerates_non_numeric_metrics() -> None:
    # Non-numeric efficiency_fps_per_w / avg_fps must not raise; _optional_float
    # returns None on the ValueError path, so the score falls back gracefully.
    profiles = [
        _measured_profile(
            "good",
            "Balanced",
            "2026-06-03T12:00:00+00:00",
            fpsw=0.65,
            fps=160.0,
        ),
        {
            "profile_id": "bad-metrics",
            "final_verified": True,
            "generated_profile_tier": "Balanced",
            "profile_created_at": "2026-06-05T12:00:00+00:00",
            "efficiency_fps_per_w": "not-a-number",
            "avg_fps": "also-bad",
        },
    ]

    resolved = resolve_profile_tier_profiles(profiles, assignments={})

    # The profile with valid metrics scores higher than the un-parseable one.
    assert resolved[PROFILE_TIER_BALANCED]["profile_id"] == "good"


# --- _profile_sort_time ValueError continue + OSError fallback (lines 483-489) ---


def test_profile_sort_time_falls_back_to_path_mtime(tmp_path) -> None:
    # Bad isoformat timestamps -> ValueError continue (line 483-484); then a real
    # path resolves via stat().st_mtime (line 485-487).
    real_file = tmp_path / "real-profile.json"
    real_file.write_text("{}", encoding="utf-8")
    profile_with_mtime = {
        "profile_id": "mtime-profile",
        "final_verified": True,
        "generated_profile_tier": "Efficiency",
        "profile_created_at": "not-a-timestamp",
        "verified_at": "also-not-a-timestamp",
        "path": str(real_file),
    }
    # Missing path entirely -> stat OSError -> time.time() fallback (line 488-489).
    profile_with_walltime = {
        "profile_id": "walltime-profile",
        "final_verified": True,
        "generated_profile_tier": "Efficiency",
        "profile_created_at": "bad",
        "path": str(tmp_path / "does-not-exist.json"),
    }

    resolved = resolve_profile_tier_profiles(
        [profile_with_mtime, profile_with_walltime],
        assignments={},
    )

    # Both are efficiency-tier; resolution must succeed without raising and pick one.
    assert resolved[PROFILE_TIER_EFFICIENCY] is not None
    assert resolved[PROFILE_TIER_EFFICIENCY]["profile_id"] in {
        "mtime-profile",
        "walltime-profile",
    }


def test_reassigning_a_disabled_profile_clears_the_stale_disabled_list(
    tmp_path,
) -> None:
    path = tmp_path / "assignments.json"
    save_profile_tier_none_assignment("p1", path=path)
    assert load_profile_tier_disabled_profile_ids(path) == {"p1"}

    assignments = save_profile_tier_assignment("p1", "balanced", path=path)

    assert assignments == {PROFILE_TIER_BALANCED: "p1"}
    assert load_profile_tier_assignments(path) == {PROFILE_TIER_BALANCED: "p1"}
    assert load_profile_tier_disabled_profile_ids(path) == set()
    assert "disabled_profile_ids" not in json.loads(path.read_text())


def test_migrating_a_disabled_profile_clears_the_legacy_disabled_list(
    tmp_path,
) -> None:
    path = tmp_path / "assignments.json"
    save_profile_tier_none_assignment("p1", path=path)

    migrate_legacy_profile_tier_to_gpu("p1", "GPU-a", path=path)

    assert load_profile_tier_disabled_profile_ids(path) == set()
    assert load_profile_tier_disabled_profile_ids(path, gpu_uuid="GPU-a") == {"p1"}

    save_profile_tier_assignment("p1", "balanced", path=path, gpu_uuid="GPU-a")

    assert load_profile_tier_assignments(path, gpu_uuid="GPU-a") == {
        PROFILE_TIER_BALANCED: "p1"
    }
    assert load_profile_tier_disabled_profile_ids(path, gpu_uuid="GPU-a") == set()
    # Single-GPU adaptive resolution unions the legacy list with the GPU
    # group's, so a stale legacy entry would keep excluding the profile.
    assert "disabled_profile_ids" not in json.loads(path.read_text())
