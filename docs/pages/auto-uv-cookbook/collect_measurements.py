#!/usr/bin/env python3
"""Export a public subset of completed local scan evidence; never copy host state."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from collect_smoothing import curve_metrics

TIERS = ("efficiency", "balanced", "performance")


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect(root: Path) -> dict:
    report = read(root / "results-report.json")
    run = report["run"]
    assert run["exit_code"] == 0 and not report["skipped_tiers"]
    assert {event["tier"] for event in report["completed_tiers"]} == set(TIERS)
    restored = report["restoration"]
    assert restored["active_job_matches_original"] and restored["boot_state_matches_original"]
    assert not report["kernel"]["gpu_messages"]
    probe_counts: Counter = Counter()
    final_curves = {}
    current_tier = "efficiency"
    for line in (root / "scan.log").read_text().splitlines():
        if not line.startswith("{"):
            continue
        event = json.loads(line)
        if event.get("event") == "tier_started":
            current_tier = event["tier"]
        if event.get("event") == "probe_start" and event.get("stage") != "final-verify":
            probe_counts[current_tier] += 1
        if event.get("event") == "candidate_curve" and event.get("stage") == "final-verify":
            final_curves[current_tier] = event["points"]
    profiles = {item["tier"]: item for item in report["profiles"]}
    efficiency = report["efficiency_search_uniqueness"]
    assert set(profiles) == set(TIERS)
    tiers = {}
    for key in TIERS:
        summary = profiles[key]
        path = Path(summary["path"])
        profile = read(path)
        assert profile["gpu_identity"]["name"] == "NVIDIA GeForce RTX 5080"
        assert profile["memory_offset_mhz"] == 0 and profile["tail_rise_bins"] == 2
        points = sorted(profile["points"], key=lambda point: point["voltage_mv"])
        tested = sorted(final_curves[key], key=lambda point: point["voltage_mv"])
        assert [
            (p["voltage_mv"], p["base_mhz"], p["clock_mhz"], p["offset_mhz"])
            for p in tested
        ] == [
            (p["voltage_mv"], p["base_mhz"], p["target_mhz"], p["new_offset_mhz"])
            for p in sorted(profile["plan"], key=lambda point: point["voltage_mv"])
        ]
        final = next(event for event in report["final_probes"] if event["event"] == "probe_result" and event["observed_tier"] == key)
        duration = next(event["target_duration_s"] for event in report["final_probes"] if event["event"] == "probe_start" and event["observed_tier"] == key)
        assert final["decision"] == "pass" and profile["final_verified"] and duration == 10
        tiers[key] = {
            "verified": True,
            "voltage_mv": profile["candidate_voltage_mv"],
            "target_mhz": profile["lock_clock_mhz"],
            "power_limit_w": profile["power_limit_w"],
            "memory_offset_mhz": profile["memory_offset_mhz"],
            "tail_rise_bins": profile["tail_rise_bins"],
            "final_seconds": duration,
            "final": {
                "fps": profile["avg_fps"],
                "power_w": profile["avg_power_w"],
                "core_clock_mhz": profile["avg_core_clock_mhz"],
                "voltage_mv": final["q2rtx_measured_voltage_mv"],
                "cuda_core_clock_mhz": final["cuda_measured_clock_mhz"],
                "cuda_voltage_mv": final["cuda_measured_voltage_mv"],
                "temperature_c": profile["avg_temperature_c"],
            },
            "curve": [[point["voltage_mv"], point["target_mhz"]] for point in points],
            "stock": [[point["voltage_mv"], point["base_mhz"]] for point in points],
            "monotonic": summary["targets_nondecreasing"],
            "plan_matches_points": summary["plan_matches_points"],
            "saved_curve_matches_final_probe": True,
            "max_step_mhz": summary["max_adjacent_target_jump_mhz"],
            "probe_count": probe_counts[key],
            "reused_descent": any(event["tier"] == key for event in report["descent_reuse"]),
            "profile_sha256": digest(path),
            **curve_metrics([[point["voltage_mv"], point["target_mhz"]] for point in points]),
        }
    return {
        "commit": run["commit"], "success": True,
        "gpu": "NVIDIA GeForce RTX 5080",
        "started_at": run["started_at"], "ended_at": run["ended_at"],
        "duration_seconds": (datetime.fromisoformat(run["ended_at"]) - datetime.fromisoformat(run["started_at"])).total_seconds(),
        "restored": True, "boot_unchanged": True, "gpu_errors": [],
        "efficiency_repeated_probes": efficiency["probe_count"] - efficiency["unique_pair_count"],
        "log_sha256": digest(root / "scan.log"), "tiers": tiers,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("after", type=Path)
    parser.add_argument("checks", type=Path)
    args = parser.parse_args()
    identities = {
        read(Path(profile["path"]))["gpu_identity"]["uuid"]
        for profile in read(args.after / "results-report.json")["profiles"]
    }
    assert len(identities) == 1
    run = collect(args.after)
    history = read(Path(__file__).with_name("smoothing-history.json"))
    assert history["gpu"] == run["gpu"]
    assert set(history["tiers"]) == set(TIERS)
    for tier in TIERS:
        old_stock, new_stock = history["tiers"][tier]["stock"], run["tiers"][tier]["stock"]
        assert [p[0] for p in old_stock] == [p[0] for p in new_stock]
        run["tiers"][tier]["stock_reference_max_difference_mhz"] = max(
            abs(a[1] - b[1]) for a, b in zip(old_stock, new_stock)
        )
    receipt = read(args.checks)
    public_fields = (
        "commit", "python_passed", "python_skipped", "all_checks_passed", "checks", "limit"
    )
    validation = {key: receipt[key] for key in public_fields}
    for value in [*validation["checks"], validation["limit"]]:
        assert not any(private in value for private in ("/home/", "/tmp/", "GPU-"))
    assert validation["commit"] == run["commit"]
    assert validation["python_passed"] > 0 and validation["all_checks_passed"]
    output = Path(__file__).with_name("measurements.json")
    output.write_text(json.dumps({"run": run, "before_smoothing": history, "validation": validation}, indent=2) + "\n")
    print(output)


if __name__ == "__main__":
    main()
