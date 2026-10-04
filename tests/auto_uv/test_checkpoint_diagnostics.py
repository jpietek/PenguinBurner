from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from auto_uv import main_loop as main
from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.domain.types import AutoUvCriticalProbeError, AutoUvVoltageScanResult
from auto_uv.persistence import scan_checkpoint as module
from auto_uv.persistence.scan_checkpoint import ScanCheckpoint


@pytest.mark.parametrize(
    "change",
    [
        "gpu",
        "driver",
        "base_curve",
        "policy",
        "options",
        "settings",
        "workload",
        "algorithm",
    ],
)
def test_identity_mismatch_names_component_and_preserves_original(tmp_path, change):
    identity = {
        key: {"value": "original"}
        for key in (
            "gpu",
            "driver",
            "base_curve",
            "policy",
            "settings",
            "workload",
            "algorithm",
        )
    }
    identity["options"] = {"auto_uv_mode": "adaptive"}
    path = tmp_path / "checkpoint.json"
    checkpoint = ScanCheckpoint(
        identity=identity, callback=None, log=lambda _: None, path=path
    )
    checkpoint.record("completed-work", 1)
    original = path.read_bytes()
    identity[change] = {"value": "changed"}
    messages = []

    loaded = ScanCheckpoint(
        identity=identity, callback=None, log=messages.append, path=path
    )

    assert not loaded.resuming
    assert loaded.lookup("completed-work") is None
    assert loaded.rejection_reason == f"scan inputs changed: {change}"
    archives = list(tmp_path.glob("checkpoint.json.rejected-*.bak"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == original
    assert any(
        change in message and str(archives[0]) in message for message in messages
    )


@pytest.mark.parametrize(
    "change",
    ["malformed", "version", "profiles", "records", "fields", "events", "passed"],
)
def test_invalid_checkpoint_is_archived_with_reason(tmp_path, change):
    path = tmp_path / "checkpoint.json"
    checkpoint = ScanCheckpoint(
        identity={}, callback=None, log=lambda _: None, path=path
    )
    checkpoint.record("completed-work", 1)
    payload = json.loads(path.read_text())
    if change == "malformed":
        path.write_bytes(b"\xffinvalid JSON")
    elif change == "fields":
        payload["records"] = {
            "broken": {"type": "AutoUvVoltageScanResult", "fields": "invalid"}
        }
        path.write_text(json.dumps(payload))
    else:
        key = "format_version" if change == "version" else change
        payload[key] = "invalid"
        path.write_text(json.dumps(payload))
    original = path.read_bytes()
    messages = []

    loaded = ScanCheckpoint(identity={}, callback=None, log=messages.append, path=path)

    assert loaded.rejection_reason
    assert not loaded.resuming
    assert any("checkpoint rejected" in message for message in messages)
    assert (
        next(tmp_path.glob("checkpoint.json.rejected-*.bak")).read_bytes() == original
    )


def test_matching_older_checkpoint_without_components_still_resumes(tmp_path):
    path = tmp_path / "checkpoint.json"
    checkpoint = ScanCheckpoint(
        identity={}, callback=None, log=lambda _: None, path=path
    )
    checkpoint.record("completed-work", 1)
    payload = json.loads(path.read_text())
    del payload["identity_components"]
    path.write_text(json.dumps(payload))

    loaded = ScanCheckpoint(identity={}, callback=None, log=lambda _: None, path=path)

    assert loaded.resuming
    assert loaded.lookup("completed-work") == 1
    assert loaded.rejection_reason is None
    assert not list(tmp_path.glob("*.bak"))


def test_failed_archive_does_not_overwrite_original(monkeypatch, tmp_path):
    path = tmp_path / "checkpoint.json"
    checkpoint = ScanCheckpoint(
        identity={}, callback=None, log=lambda _: None, path=path
    )
    checkpoint.record("completed-work", 1)
    original = path.read_bytes()

    def fail(_fd):
        raise OSError("disk full")

    monkeypatch.setattr(module.os, "fsync", fail)
    with pytest.raises(AutoUvCriticalProbeError, match="original left untouched"):
        ScanCheckpoint(
            identity={"gpu": "changed"}, callback=None, log=lambda _: None, path=path
        )
    assert path.read_bytes() == original


def test_unreadable_checkpoint_stops_without_replacing_it(monkeypatch, tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_bytes(b"original checkpoint")
    read_bytes = type(path).read_bytes

    def unreadable(self):
        if self == path:
            raise PermissionError("denied")
        return read_bytes(self)

    monkeypatch.setattr(type(path), "read_bytes", unreadable)
    with pytest.raises(AutoUvCriticalProbeError, match="Cannot read"):
        ScanCheckpoint(identity={}, callback=None, log=lambda _: None, path=path)
    assert read_bytes(path) == b"original checkpoint"


@pytest.mark.parametrize("base_shift", [0, 15])
def test_probe_cache_miss_keeps_completed_efficiency_and_skips_its_setup(
    monkeypatch, tmp_path, base_shift
):
    path = tmp_path / "checkpoint.json"
    identity = {
        "options": {"auto_uv_mode": "adaptive"},
        "base_curve": [{
            "index": 0, "voltage_mv": 862, "base_mhz": 1800,
            "target_mhz": 1800, "current_offset_mhz": 0,
            "new_offset_mhz": 0, "preserve_base": False,
        }],
    }
    checkpoint = ScanCheckpoint(
        identity=identity, callback=None, log=lambda _: None, path=path
    )
    completed = AutoUvVoltageScanResult(True, 862, 1800, "verified", None, [])
    checkpoint.record({"completed_tier": "efficiency"}, completed, profiles=True)
    identity["base_curve"][0].update(base_mhz=1800 + base_shift, target_mhz=1800 + base_shift)
    messages = []
    resumed = ScanCheckpoint(
        identity=identity, callback=None, log=messages.append, path=path
    )
    assert (
        resumed.probe("uncached-baseline", lambda: "new measurement")
        == "new measurement"
    )
    assert not resumed.replaying
    assert any(
        ("retaining completed tiers" if base_shift else "completed tiers remain reusable") in message
        for message in messages
    )
    setups, events = [], []

    def setup(_gpu, *, tier_mode, **_kw):
        setups.append(tier_mode)
        assert tier_mode == "balanced", "completed Efficiency must not be restarted"
        raise AutoUvCriticalProbeError("unfinished Balanced unavailable")

    monkeypatch.setattr(main, "driver_memory_offset_limit_mhz", lambda _: 0)
    monkeypatch.setattr(main, "apply_adaptive_tier_memory_offset", setup)
    result = main.run_adaptive_tier_scans(
        base_curve=[],
        gpu=SimpleNamespace(policy_controller=object()),
        configure_tier_probe_runner=lambda: pytest.fail(
            "no runner needed for completed tier"
        ),
        settings=None,
        runtime_options={},
        base_loop_settings=AutoUvScanSettings(
            start_voltage_mv=1000, min_search_voltage_mv=900
        ),
        baseline_candidate=None,
        initial_stable_outcome=None,
        stable_probe=None,
        discovery_summary=None,
        probe_history=[],
        baseline_target=None,
        unsafe_entries=[],
        finish_with_final_verification=lambda **_: pytest.fail(
            "Efficiency must not verify again"
        ),
        event_callback=lambda name, payload: events.append((name, payload)),
        log=messages.append,
        checkpoint=resumed,
    )

    assert result == completed
    assert setups == ["balanced"]
    assert [payload["tier"] for name, payload in events if name == "tier_started"] == [
        "balanced"
    ]


def test_completed_tier_still_checks_blacklist_after_replay_ends(tmp_path):
    from auto_uv.persistence.unsafe_voltage_blacklist_file import record_unsafe_voltage

    checkpoint = ScanCheckpoint(
        identity={},
        callback=None,
        log=lambda _: None,
        path=tmp_path / "checkpoint.json",
    )
    checkpoint.record(
        {"completed_tier": "efficiency"},
        AutoUvVoltageScanResult(True, 850, 1800, "verified", None, []),
        profiles=True,
    )
    record_unsafe_voltage(
        candidate_voltage_mv=850, lock_clock_mhz=1800, reason="device lost"
    )
    with pytest.raises(AutoUvCriticalProbeError, match="now blacklisted"):
        checkpoint.completed_tier("efficiency")
