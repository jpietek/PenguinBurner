from __future__ import annotations

import json
from dataclasses import fields

import pytest

from auto_uv.curve.flattened_voltage_probe_curve import (
    build_flattened_voltage_probe_curve,
)
from auto_uv.domain.types import AutoUvProbeSummary, AutoUvVoltageScanResult
from auto_uv.persistence.checkpoint_curve import compatible_stock_curves
from auto_uv.persistence.scan_checkpoint import ScanCheckpoint


def stock_curve():
    return [
        {
            "index": index,
            "voltage_mv": voltage,
            "base_mhz": clock,
            "target_mhz": clock,
            "current_offset_mhz": 0,
            "new_offset_mhz": 0,
            "preserve_base": False,
        }
        for index, (voltage, clock) in enumerate(
            [(800, 1605), (825, 1650), (850, 1695), (875, 1740), (900, 1800)]
        )
    ]


def shifted_curve(curve):
    return [
        {
            **point,
            "base_mhz": point["base_mhz"] + shift,
            "target_mhz": point["target_mhz"] + shift,
        }
        for point, shift in zip(curve, (0, 15, -15, 15, 0), strict=True)
    ]


@pytest.mark.parametrize(
    "change",
    [
        "two_bins",
        "fractional_bin",
        "voltage",
        "index",
        "missing",
        "reordered",
        "duplicate",
        "offset",
        "target",
        "preserve",
        "malformed",
    ],
)
def test_stock_drift_does_not_relax_other_curve_constraints(change):
    curve = stock_curve()
    changed = shifted_curve(curve)
    if change == "two_bins":
        # Beyond the drift tolerance (not merely more than one bin).
        changed[0].update(base_mhz=1605 + 90, target_mhz=1605 + 90)
    elif change == "fractional_bin":
        changed[0].update(base_mhz=1606, target_mhz=1606)
    elif change in {"voltage", "index", "offset", "target", "preserve"}:
        key = {
            "voltage": "voltage_mv",
            "index": "index",
            "offset": "current_offset_mhz",
            "target": "target_mhz",
            "preserve": "preserve_base",
        }[change]
        changed[0][key] += 1
    elif change == "missing":
        changed.pop()
    elif change == "reordered":
        changed.reverse()
    elif change == "duplicate":
        curve[1] = curve[0].copy()
        changed[1] = changed[0].copy()
    else:
        changed[0].pop("base_mhz")
    assert not compatible_stock_curves(curve, changed)


def test_reboot_keeps_completed_tier_and_rebases_history_before_recovery(tmp_path):
    curve = stock_curve()
    current = shifted_curve(curve)
    identity = {"base_curve": curve, "options": {"auto_uv_mode": "adaptive"}}
    path = tmp_path / "checkpoint.json"
    old = ScanCheckpoint(
        identity=identity, callback=None, log=lambda _: None, path=path
    )
    completed = AutoUvVoltageScanResult(True, 850, 1755, "verified", None, [])
    old.record({"completed_tier": "efficiency"}, completed, profiles=True)
    old.record("old-stock-measurement", "must not replay")
    old.event("tier_started", {"tier": "efficiency"})
    old.event("tier_completed", {"tier": "efficiency"})
    old.event("tier_started", {"tier": "balanced"})
    point = build_flattened_voltage_probe_curve(
        curve,
        candidate_voltage_mv=850,
        target_clock_mhz=1800,
        tail_rise_bins=2,
        label="passed",
        metadata={"tail_rise_bins": 2},
    )
    probe = AutoUvProbeSummary(
        **{field.name: None for field in fields(AutoUvProbeSummary)}
    )
    probe.candidate_voltage_mv, probe.lock_clock_mhz = 850, 1800
    probe.avg_fps = 110.0
    probe.tested_plan = point.flattened_plan
    old.note_pass(point, probe)

    messages, restored = [], []
    resumed = ScanCheckpoint(
        identity={**identity, "base_curve": current},
        callback=lambda event, payload: restored.append((event, payload)),
        log=messages.append,
        path=path,
    )
    assert resumed.resuming and resumed.base_curve_rebased and not resumed.replaying
    assert resumed.rejection_reason is None
    assert resumed.completed_tier("efficiency") == completed
    assert resumed.lookup("old-stock-measurement") is None
    history = resumed.history_for("balanced")
    assert history[0].avg_fps == 110.0
    for before, after, base in zip(
        point.flattened_plan, history[0].tested_plan, current, strict=True
    ):
        assert after["target_mhz"] == before["target_mhz"]
        assert after["new_offset_mhz"] == before["target_mhz"] - base["base_mhz"]
        assert after["base_mhz"] == base["base_mhz"]

    resumed.prepare_recovery(current, [], {"lock_clock_mhz": 1830})
    recovery = resumed.recovery_for("balanced")
    assert recovery is not None
    assert (recovery.voltage_mv, recovery.target_mhz) == (875, 1800)
    assert recovery.metadata["resume_recovery"]
    assert recovery.metadata["tail_rise_bins"] == 2
    assert resumed.recovery_for("efficiency") is None
    resumed.restore_ui()
    assert restored[0][0] == "scan_resumed"
    assert any("15MHz" in message for message in messages)

    # Another interruption cannot resurrect the old cache using the new identity.
    again = ScanCheckpoint(
        identity={**identity, "base_curve": current},
        callback=None,
        log=lambda _: None,
        path=path,
    )
    assert again.resuming
    assert again.lookup("old-stock-measurement") is None
    assert again.completed_tier("efficiency") == completed


@pytest.mark.parametrize("snapshot", ["missing", "tampered"])
def test_drift_requires_the_original_fingerprinted_stock_snapshot(tmp_path, snapshot):
    curve = stock_curve()
    path = tmp_path / "checkpoint.json"
    old = ScanCheckpoint(
        identity={"base_curve": curve}, callback=None, log=lambda _: None, path=path
    )
    old.record("measurement", 1)
    payload = json.loads(path.read_text())
    if snapshot == "missing":
        payload.pop("base_curve")
    else:
        payload["base_curve"] = shifted_curve(curve)
    path.write_text(json.dumps(payload))
    resumed = ScanCheckpoint(
        identity={"base_curve": shifted_curve(curve)},
        callback=None,
        log=lambda _: None,
        path=path,
    )
    assert not resumed.resuming
    assert resumed.rejection_reason == "scan inputs changed: base_curve"
