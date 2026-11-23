from __future__ import annotations

from pathlib import Path

import profiles.uv.memory_offset_edit as mop
from profiles.uv.profile_store import profile_presentation_name
from profiles.uv.memory_offset_edit import (
    editable_memory_offset_from_profile,
    user_edited_memory_offset_profile_payload,
)


def test_editable_memory_offset_from_profile_valid() -> None:
    assert editable_memory_offset_from_profile({"memory_offset_mhz": 400}) == 400
    assert editable_memory_offset_from_profile({"memory_offset_mhz": "400"}) == 400


def test_editable_memory_offset_from_profile_invalid() -> None:
    assert editable_memory_offset_from_profile({}) is None
    assert editable_memory_offset_from_profile({"memory_offset_mhz": None}) is None
    assert editable_memory_offset_from_profile({"memory_offset_mhz": "not-a-number"}) is None


def test_user_edited_memory_offset_profile_payload_requires_verification() -> None:
    payload = user_edited_memory_offset_profile_payload(
        {
            "profile_id": "parent",
            "path": "/tmp/parent.json",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2550,
            "memory_offset_mhz": 200,
            "plan": [{"index": 0, "voltage_mv": 900, "base_mhz": 2400, "target_mhz": 2500}],
            "points": [{"voltage_mv": 900, "clock_mhz": 2500}],
            "avg_fps": 120.0,
            "final_verified": True,
            "verification_status": "verified",
        },
        400,
        original_memory_offset_mhz=200,
    )

    assert payload["profile_source"] == "user-edited"
    assert payload["memory_offset_mhz"] == 400
    # Named like the running-profile line: the tuning point, memory offset in
    # memory-clock MHz (half the stored MT/s), not "memory offset +400 MT/s".
    assert payload["display_name"] == "User edited 2550 MHz 900 mV, mem +200 MHz"
    assert payload["final_verified"] is False
    assert payload["verification_status"] == "unverified"
    assert payload["requires_verification"] is True
    # Unrelated V/F curve and identity fields must pass through untouched.
    assert payload["candidate_voltage_mv"] == 900
    assert payload["lock_clock_mhz"] == 2550
    assert payload["plan"] == [
        {"index": 0, "voltage_mv": 900, "base_mhz": 2400, "target_mhz": 2500}
    ]
    assert "avg_fps" not in payload
    assert "profile_id" not in payload
    assert payload["manual_edit"] == {
        "edit_kind": "memory-offset",
        "parent_profile_id": "parent",
        "parent_path": "/tmp/parent.json",
        "original_memory_offset_mhz": 200,
        "new_memory_offset_mhz": 400,
    }


def test_user_edited_memory_offset_profile_payload_without_original() -> None:
    payload = user_edited_memory_offset_profile_payload(
        {"profile_id": "parent", "path": "/tmp/parent.json"},
        150,
    )
    assert payload["manual_edit"]["original_memory_offset_mhz"] is None
    assert payload["manual_edit"]["new_memory_offset_mhz"] == 150


def test_user_edited_memory_offset_profile_payload_names_bare_edit() -> None:
    payload = user_edited_memory_offset_profile_payload({}, 0)
    assert payload["display_name"] == "User edited profile"


def test_profile_presentation_name_refreshes_legacy_memory_offset_name() -> None:
    # Saved before the rename: the file still says MT/s, the UI must not.
    assert (
        profile_presentation_name(
            {
                "display_name": "User edited memory offset +6000 MT/s",
                "lock_clock_mhz": 2500,
                "candidate_voltage_mv": 850,
                "memory_offset_mhz": 6000,
            }
        )
        == "User edited 2500 MHz 850 mV, mem +3000 MHz"
    )


def test_profile_presentation_name_refreshes_legacy_curve_name() -> None:
    assert (
        profile_presentation_name(
            {
                "display_name": "User edited 2500 MHz 850 mV",
                "lock_clock_mhz": 2500,
                "candidate_voltage_mv": 850,
                "memory_offset_mhz": 6000,
            }
        )
        == "User edited 2500 MHz 850 mV, mem +3000 MHz"
    )


def test_profile_presentation_name_keeps_other_saved_names() -> None:
    # An edited fan curve, an Afterburner import and anything hand-picked keep
    # the name in their file.
    for saved in (
        "User edited fan curve 2500 MHz 850 mV",
        "MSI Afterburner Curve 2500 MHz 850 mV",
        "My quiet profile",
    ):
        assert (
            profile_presentation_name(
                {
                    "display_name": saved,
                    "lock_clock_mhz": 2500,
                    "candidate_voltage_mv": 850,
                    "memory_offset_mhz": 6000,
                }
            )
            == saved
        )


def test_profile_presentation_name_falls_back_without_a_saved_name() -> None:
    assert (
        profile_presentation_name({"lock_clock_mhz": 2500, "candidate_voltage_mv": 850})
        == "2500 MHz 850 mV"
    )
    # A legacy name with nothing to recompute from stays as saved.
    assert (
        profile_presentation_name(
            {"display_name": "User edited memory offset +0 MT/s"}
        )
        == "User edited memory offset +0 MT/s"
    )


# Saving an edit: dependencies are monkeypatched so no live store is touched.


def test_save_edited_memory_offset_profile_reads_from_disk_and_archives(monkeypatch) -> None:
    monkeypatch.setattr(mop, "profile_payload_from_path", lambda profile: {"base": 1})
    monkeypatch.setattr(
        mop,
        "user_edited_memory_offset_profile_payload",
        lambda parent, new_memory_offset_mhz, **kw: {
            "edited": True,
            "parent": parent,
            "new_memory_offset_mhz": new_memory_offset_mhz,
            "kw": kw,
        },
    )
    archived = Path("/tmp/auto-uv-profile-mem-edit.json")
    monkeypatch.setattr(mop, "archive_auto_uv_profile", lambda payload: archived)

    path, payload = mop.save_edited_memory_offset_profile(
        {"path": "x"},
        400,
        original_memory_offset_mhz=200,
    )

    assert path == archived
    assert payload["edited"] is True
    # The parent payload came from the re-read (disk), not the trimmed summary dict.
    assert payload["parent"] == {"base": 1}
    assert payload["new_memory_offset_mhz"] == 400
    assert payload["kw"] == {"original_memory_offset_mhz": 200}


def test_save_edited_memory_offset_profile_falls_back_to_profile_dict(monkeypatch) -> None:
    monkeypatch.setattr(mop, "profile_payload_from_path", lambda profile: None)
    captured = {}

    def fake_builder(parent, new_memory_offset_mhz, **kw):
        captured["parent"] = parent
        return {"edited": True}

    monkeypatch.setattr(mop, "user_edited_memory_offset_profile_payload", fake_builder)
    monkeypatch.setattr(mop, "archive_auto_uv_profile", lambda payload: Path("/tmp/x.json"))

    mop.save_edited_memory_offset_profile({"path": "", "memory_offset_mhz": 100}, 250)

    assert captured["parent"] == {"path": "", "memory_offset_mhz": 100}
