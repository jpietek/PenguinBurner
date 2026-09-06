#!/usr/bin/env python3
"""Export the authenticated pre-smoothing RTX 5080 final-probe curves.

Run with --log SCAN_LOG --checkpoint-dir CHECKPOINT_DIRECTORY. Inputs remain
private; the output includes only curve data, capture dates and source hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from itertools import pairwise
from pathlib import Path

LOG_SHA256 = "6d0ff5e8fe1c01feb9d5b99ea3da171ab6f09ccbd7a53ac9c201112ed1c18500"
CHECKPOINTS = {
    "efficiency": (
        "auto-uv-last-stable-20260905-120708.json",
        "f028f37e6f7045973340c656877ca6aee31f6870a92e84445153bdb9bb8e767a",
    ),
    "balanced": (
        "auto-uv-last-stable-20260905-121503.json",
        "25f67758690a26b223ebd1ec51b8252443b083567e9344f4d584d787f3ae62e5",
    ),
    "performance": (
        "auto-uv-last-stable-20260905-122055.json",
        "4a38e55dbb346a3c1fbd8c5d79fd4671d263eb47674a71571db76f8514ae79a4",
    ),
}


def curve_metrics(curve: list[list[int]]) -> dict:
    """Measure native adjacent points, without interpolation or smoothing."""
    if len(curve) < 2 or any(b[0] <= a[0] for a, b in pairwise(curve)):
        raise ValueError("A curve needs at least two points with increasing voltages")
    segments = [
        {"rise": b[1] - a[1], "start": a[0], "end": b[0]}
        for a, b in pairwise(curve)
    ]
    slopes = [s["rise"] / (s["end"] - s["start"]) for s in segments]
    return {
        "max_step": max(segments, key=lambda segment: segment["rise"]),
        "peak_slope": max(slopes),
        "peak_slope_change": max(
            (abs(b - a) for a, b in pairwise(slopes)), default=0.0
        ),
        "downward_steps": sum(segment["rise"] < 0 for segment in segments),
    }


def authenticated_bytes(path: Path, expected: str) -> bytes:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError(f"Historical source hash mismatch: {path.name}")
    return raw


def collect(log: Path, checkpoint_dir: Path) -> dict:
    lines = authenticated_bytes(log, LOG_SHA256).decode().splitlines()
    if not any("initial check passed: gpu=NVIDIA GeForce RTX 5080 " in s for s in lines):
        raise ValueError("Historical log does not identify the RTX 5080")
    current_tier = ""
    final_curves, final_results, saved, completed = {}, {}, {}, set()
    for line in lines:
        if line.startswith("{"):
            event = json.loads(line)
            if event.get("event") == "tier_started":
                current_tier = event["tier"]
            if event.get("stage") == "final-verify":
                if event["event"] == "candidate_curve":
                    final_curves[current_tier] = event
                elif event["event"] == "probe_result":
                    final_results[current_tier] = event
            if event.get("event") == "tier_completed":
                completed.add(event["tier"])
        match = re.search(
            r"phase=final curve-saved=\S+ voltage=(\d+)mV target=(\d+)MHz .*status=completed",
            line,
        )
        if match:
            saved[current_tier] = tuple(map(int, match.groups()))

    tiers = {}
    for tier, (filename, sha256) in CHECKPOINTS.items():
        checkpoint = json.loads(authenticated_bytes(checkpoint_dir / filename, sha256))
        candidate = final_curves[tier]
        result = final_results[tier]
        anchor = (checkpoint["candidate_voltage_mv"], checkpoint["lock_clock_mhz"])
        if (
            checkpoint["final_verified"] is not False
            or result["decision"] != "pass"
            or tier not in completed
            or saved.get(tier) != anchor
            or (candidate["voltage_mv"], candidate["clock_mhz"]) != anchor
            or (result["voltage_mv"], result["clock_mhz"]) != anchor
        ):
            raise ValueError(f"Missing matching final pass/save evidence for {tier}")
        points = sorted(checkpoint["points"], key=lambda p: p["voltage_mv"])
        tested = sorted(candidate["points"], key=lambda p: p["voltage_mv"])
        if len(points) != 127 or [
            (p["voltage_mv"], p["base_mhz"], p["target_mhz"], p["new_offset_mhz"])
            for p in points
        ] != [
            (p["voltage_mv"], p["base_mhz"], p["clock_mhz"], p["offset_mhz"])
            for p in tested
        ]:
            raise ValueError(f"Checkpoint and final-probe curves differ for {tier}")
        curve = [[p["voltage_mv"], p["target_mhz"]] for p in points]
        tiers[tier] = {
            "curve": curve,
            "stock": [[p["voltage_mv"], p["base_mhz"]] for p in points],
            "voltage_mv": anchor[0],
            "target_mhz": anchor[1],
            "tail_rise_bins": checkpoint["tail_rise_bins"],
            "date": checkpoint["verified_at"],
            "source_sha256": sha256,
            **curve_metrics(curve),
        }
    return {
        "gpu": "NVIDIA GeForce RTX 5080",
        "label": "Before smoothing · 5 September 2026",
        "provenance": (
            "Historical final-probe curves: all 127 points match their surviving checkpoints, "
            "and the scan log records final PASS and profile saves for all three tiers. "
            "The original final-profile JSONs are unavailable; dates and per-tier hashes "
            "identify the pre-final checkpoints, which are not final-verified profiles. "
            "Tails were 2 / 4 / 4 bins, final checks 60 / 180 / 60 seconds, and power-cap "
            "writes were skipped. This is a geometry comparison, not a controlled performance "
            f"comparison. Scan-log SHA-256: {LOG_SHA256}."
        ),
        "tiers": tiers,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--checkpoint-dir", required=True, type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).with_name("smoothing-history.json")
    )
    args = parser.parse_args()
    payload = collect(args.log, args.checkpoint_dir)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
