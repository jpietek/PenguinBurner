#!/usr/bin/env python3
"""Append public recovery verification to the historical smoothing comparison."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from collect_measurements import read
from collect_smoothing import curve_metrics


def collect(root: Path, checks: dict) -> dict:
    report = read(root / "results-report.json")
    run = report["run"]
    assert run["exit_code"] == 0, "Only publish a completed successful/partial scan"
    assert report["restoration"]["active_job_matches_original"]
    assert report["restoration"]["boot_state_matches_original"]
    assert checks["all_checks_passed"]
    events, tier = [], "efficiency"
    for line in (root / "scan.log").read_text().splitlines():
        if not line.startswith("{"):
            continue
        event = json.loads(line)
        if event.get("event") == "tier_started":
            tier = event["tier"]
        events.append({**event, "observed_tier": tier})
    tiers = {}
    for item in report["profiles"]:
        profile = read(Path(item["path"]))
        key = profile["generated_profile_tier"]
        assert profile["final_verified"]
        assert item["targets_nondecreasing"] and item["plan_matches_points"]
        assert item["final_tested_curve_matches_saved_plan"]
        finals = [
            event
            for event in events
            if event.get("event") == "probe_result"
            and event.get("stage") == "final-verify"
            and event["observed_tier"] == key
        ]
        assert finals and finals[-1]["decision"] == "pass"
        final = finals[-1]
        starts = [
            event
            for event in events
            if event.get("event") == "probe_start"
            and event.get("stage") == "final-verify"
            and event["observed_tier"] == key
        ]
        points = sorted(profile["points"], key=lambda point: point["voltage_mv"])
        curve = [[point["voltage_mv"], point["target_mhz"]] for point in points]
        tiers[key] = {
            "verified": True,
            "voltage_mv": profile["candidate_voltage_mv"],
            "target_mhz": profile["lock_clock_mhz"],
            "power_limit_w": profile["power_limit_w"],
            "memory_offset_mhz": profile["memory_offset_mhz"],
            "tail_rise_bins": profile["tail_rise_bins"],
            "final_seconds": starts[-1]["target_duration_s"],
            "final": {
                "fps": profile["avg_fps"],
                "power_w": profile["avg_power_w"],
                "core_clock_mhz": profile["avg_core_clock_mhz"],
                "voltage_mv": final.get("q2rtx_measured_voltage_mv"),
                "temperature_c": profile["avg_temperature_c"],
                "cuda_core_clock_mhz": final.get("cuda_measured_clock_mhz"),
                "cuda_voltage_mv": final.get("cuda_measured_voltage_mv"),
            },
            "curve": curve,
            "monotonic": True,
            "plan_matches_points": True,
            "saved_curve_matches_final_probe": True,
            "profile_sha256": hashlib.sha256(
                Path(item["path"]).read_bytes()
            ).hexdigest(),
            **curve_metrics(curve),
        }
    assert tiers
    args = run["argv"]
    targets = {}
    for key in ("efficiency", "balanced", "performance"):
        voltage_flag, clock_flag = (
            f"--auto-uv-{key}-target-voltage-mv",
            f"--auto-uv-{key}-target-clock-mhz",
        )
        targets[key] = {
            "voltage_mv": int(args[args.index(voltage_flag) + 1]),
            "target_mhz": int(args[args.index(clock_flag) + 1]),
        }
    return {
        "commit": checks["commit"],
        "success": True,
        "gpu": "NVIDIA GeForce RTX 5080",
        "started_at": run["started_at"],
        "ended_at": run["ended_at"],
        "restored": True,
        "boot_unchanged": True,
        "settings_label": "+2000 MHz actual memory offset (CLI/NVML 4000); 300 / 360 / 390 W; two tail bins",
        "requested_targets": targets,
        "tiers": tiers,
        "tier_errors": {
            item["tier"]: item.get("error", item.get("reason", "No verified profile"))
            for item in report["skipped_tiers"]
        },
        "probe_passes": sum(
            event.get("event") == "probe_result" and event.get("decision") == "pass"
            for event in events
        ),
        "probe_failures": sum(
            event.get("event") == "probe_result" and event.get("decision") == "fail"
            for event in events
        ),
        "descent_reused": bool(report["descent_reuse"]),
        "gpu_errors": report["kernel"]["gpu_messages"],
        "log_sha256": hashlib.sha256((root / "scan.log").read_bytes()).hexdigest(),
        "summary": "Current recovery implementation, with the existing crash blacklist retained. Different Balanced and Performance power caps require separate descents.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("checks", type=Path)
    args = parser.parse_args()
    checks = read(args.checks)
    public_checks = {
        key: checks[key]
        for key in (
            "commit",
            "python_passed",
            "python_skipped",
            "all_checks_passed",
            "checks",
            "limit",
        )
    }
    data_path = Path(__file__).with_name("measurements.json")
    data = read(data_path)
    data["recovery_run"] = collect(args.evidence, public_checks)
    data["recovery_validation"] = public_checks
    payload = json.dumps(data, indent=2) + "\n"
    assert not any(private in payload for private in ("/home/", "/tmp/", "GPU-"))
    data_path.write_text(payload)
    print(data_path)


if __name__ == "__main__":
    main()
