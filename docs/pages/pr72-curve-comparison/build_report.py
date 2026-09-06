"""Regenerate the offline PR 72 curve report from the two original attachments."""

import argparse
import csv
import hashlib
import itertools
import json
from pathlib import Path
from typing import Any
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent
TIERS = ["efficiency", "balanced", "performance"]


def summarize(content: bytes, member: str) -> dict[str, Any]:
    raw = json.loads(content)
    points = sorted(raw["plan"], key=lambda p: p["voltage_mv"])
    assert len(points) == 127
    assert len({p["voltage_mv"] for p in points}) == len(points)
    assert all(p["target_mhz"] == p["base_mhz"] + p["new_offset_mhz"] for p in points)
    saved = {p["voltage_mv"]: p["target_mhz"] for p in raw["points"]}
    assert saved == {p["voltage_mv"]: p["target_mhz"] for p in points}
    segments = [
        {
            "start": a["voltage_mv"],
            "end": b["voltage_mv"],
            "start_mhz": a["target_mhz"],
            "end_mhz": b["target_mhz"],
            "rise": b["target_mhz"] - a["target_mhz"],
            "slope": (b["target_mhz"] - a["target_mhz"])
            / (b["voltage_mv"] - a["voltage_mv"]),
        }
        for a, b in itertools.pairwise(points)
    ]
    changes = [
        {"voltage": left["end"], "value": abs(right["slope"] - left["slope"])}
        for left, right in itertools.pairwise(segments)
    ]
    return {
        "file": Path(member).name,
        "relative": member,
        "sha256": hashlib.sha256(content).hexdigest(),
        "tier": raw["generated_profile_tier"],
        "date": raw["profile_created_at"],
        "verified": raw["final_verified"],
        "target_voltage": raw["candidate_voltage_mv"],
        "target_clock": raw["lock_clock_mhz"],
        "loaded_voltage": raw.get("loaded_median_voltage_mv"),
        "loaded_clock": raw.get("loaded_median_core_clock_mhz"),
        "power_limit": raw.get("power_limit_w"),
        "avg_clock": raw.get("avg_core_clock_mhz"),
        "avg_power": raw.get("avg_power_w"),
        "avg_fps": raw.get("avg_fps"),
        "memory_offset": raw.get("memory_offset_mhz"),
        "tail_bins": raw.get("tail_rise_bins"),
        "gpu": raw["gpu_identity"]["name"],
        "points": [[p["voltage_mv"], p["target_mhz"], p["base_mhz"]] for p in points],
        "segments": segments,
        "metrics": {
            "peak_slope": max(s["slope"] for s in segments),
            "peak_change": max(changes, key=lambda p: p["value"]),
            "largest_step": max(segments, key=lambda s: s["rise"]),
            "downward_steps": sum(s["rise"] < 0 for s in segments),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive", type=Path, required=True, help="pb-pr72-curves.zip"
    )
    parser.add_argument(
        "--latest-archive", type=Path, required=True, help="pb-uv-tiers-20260906.zip"
    )
    args = parser.parse_args()
    archives = [
        {
            "file": "pb-pr72-curves.zip",
            "sha256": "f7bd154d5d7e7b425b8eb215071ca77852d055490e28df56cc954a66be799467",
            "url": "https://github.com/user-attachments/files/31865984/pb-pr72-curves.zip",
        },
        {
            "file": "pb-uv-tiers-20260906.zip",
            "sha256": "5fcc8dc33853beaa0da7f94fa40599d1ff58e59e479c8d18cb0c194bd67c6755",
            "url": "https://github.com/user-attachments/files/31878914/pb-uv-tiers-20260906.zip",
        },
    ]
    data: dict[str, Any] = {
        "title": "PR 72 · RTX 5070 Ti curve comparison",
        "comment_url": "https://github.com/jpietek/PenguinBurner/pull/72#issuecomment-5552597483",
        "latest_comment_url": "https://github.com/jpietek/PenguinBurner/pull/72#issuecomment-5558750277",
        "gameplay_comment_url": "https://github.com/jpietek/PenguinBurner/pull/72#issuecomment-5558810521",
        "archives": archives,
        "labels": {
            "before": "Oldest · August / pre-smoothing",
            "previous": "September 5 · 4628f82",
            "after": "September 6 · 1027c01",
        },
        "driver": "610.57.04",
        "tiers": {},
    }
    identities = set()
    for path, metadata in zip([args.archive, args.latest_archive], archives):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata["sha256"]
        with ZipFile(path) as archive:
            for member in sorted(archive.namelist()):
                if not Path(member).name.startswith("auto-uv-profile-20"):
                    continue
                content = archive.read(member)
                profile = summarize(content, member)
                side = (
                    "after"
                    if path == args.latest_archive
                    else "before"
                    if "/before/" in member
                    else "previous"
                )
                tier = profile["tier"]
                assert tier in TIERS
                assert side not in data["tiers"].setdefault(tier, {})
                assert profile["verified"] is True
                data["tiers"][tier][side] = profile
                identities.add(json.loads(content)["gpu_identity"]["uuid"])
    assert len(identities) == 1, "Profiles belong to different GPUs"
    baseline = data["tiers"]["efficiency"]["before"]["points"]
    stock_deltas = []
    for tier in TIERS:
        profiles = data["tiers"][tier]
        assert set(profiles) == {"before", "previous", "after"}
        for profile in profiles.values():
            assert [p[0] for p in profile["points"]] == [p[0] for p in baseline]
            stock_deltas.extend(
                abs(p[2] - b[2]) for p, b in zip(profile["points"], baseline)
            )
    data["max_stock_delta_mhz"] = max(stock_deltas)
    (ROOT / "comparison-data.json").write_text(json.dumps(data, indent=2) + "\n")
    with (ROOT / "curve-points.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["tier", "run", "voltage_mv", "target_mhz", "stock_mhz"])
        for tier in TIERS:
            for side, profile in data["tiers"][tier].items():
                writer.writerows([tier, side, *point] for point in profile["points"])
    payload = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    template = (ROOT / "report-template.html").read_text()
    assert template.count("/*__DATA__*/") == 1
    (ROOT / "index.html").write_text(template.replace("/*__DATA__*/", payload))
    for tier in TIERS:
        print(
            tier,
            {
                side: profile["metrics"]["largest_step"]["rise"]
                for side, profile in data["tiers"][tier].items()
            },
        )
    print(ROOT / "index.html")


if __name__ == "__main__":
    main()
