"""Regenerate the offline PR 72 curve report from the extracted attachment."""

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


def summarize(path: Path, source: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text())
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
        "file": path.name,
        "relative": str(path.relative_to(source)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "tier": raw["generated_profile_tier"],
        "date": raw["profile_created_at"],
        "verified": raw["final_verified"],
        "target_voltage": raw["candidate_voltage_mv"],
        "target_clock": raw["lock_clock_mhz"],
        "loaded_voltage": raw.get("loaded_median_voltage_mv"),
        "loaded_clock": raw.get("loaded_median_core_clock_mhz"),
        "power_limit": raw.get("power_limit_w"),
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
        "--source", type=Path, required=True, help="Extracted pb-pr72-curves directory"
    )
    parser.add_argument(
        "--archive", type=Path, required=True, help="Original pb-pr72-curves.zip"
    )
    args = parser.parse_args()
    source = args.source.resolve()
    archive_bytes = args.archive.read_bytes()
    assert (
        hashlib.sha256(archive_bytes).hexdigest()
        == "f7bd154d5d7e7b425b8eb215071ca77852d055490e28df56cc954a66be799467"
    )
    data: dict[str, Any] = {
        "title": "PR 72 · RTX 5070 Ti curve comparison",
        "comment_url": "https://github.com/jpietek/PenguinBurner/pull/72#issuecomment-5552597483",
        "voltage_comment_url": "https://github.com/jpietek/PenguinBurner/pull/72#issuecomment-5552513353",
        "archive_url": "https://github.com/user-attachments/files/31865984/pb-pr72-curves.zip",
        "archive_sha256": hashlib.sha256(args.archive.read_bytes()).hexdigest(),
        "before_label": "Earlier main runs",
        "after_label": "PR 72 · 4628f82",
        "driver": "610.57.04",
        "vbios": "98.03.58.00.21",
        "tiers": {},
    }
    identities = set()
    for side in ["before", "after"]:
        for path in sorted((source / side).glob("auto-uv-profile-20*.json")):
            with ZipFile(args.archive) as archive:
                assert path.read_bytes() == archive.read(
                    f"pb-pr72-curves/{side}/{path.name}"
                )
            profile = summarize(path, source)
            tier = profile["tier"]
            assert tier in TIERS
            assert side not in data["tiers"].setdefault(tier, {})
            data["tiers"][tier][side] = profile
            identities.add(json.loads(path.read_text())["gpu_identity"]["uuid"])
    assert len(identities) == 1, "Profiles belong to different GPUs"
    for tier in TIERS:
        pair = data["tiers"][tier]
        assert [(p[0], p[2]) for p in pair["before"]["points"]] == [
            (p[0], p[2]) for p in data["tiers"]["efficiency"]["before"]["points"]
        ]
        before, after = pair["before"], pair["after"]
        assert [(p[0], p[2]) for p in before["points"]] == [
            (p[0], p[2]) for p in after["points"]
        ]
        pair["reduction"] = 100 * (
            1
            - after["metrics"]["peak_change"]["value"]
            / before["metrics"]["peak_change"]["value"]
        )
        pair["changed_points"] = sum(
            b[1] != a[1] for b, a in zip(before["points"], after["points"])
        )
    (ROOT / "comparison-data.json").write_text(json.dumps(data, indent=2) + "\n")
    with (ROOT / "curve-points.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "tier",
                "voltage_mv",
                "stock_mhz",
                "before_mhz",
                "after_mhz",
                "delta_mhz",
                "before_incoming_slope_mhz_per_mv",
                "after_incoming_slope_mhz_per_mv",
            ]
        )
        for tier in TIERS:
            pair = data["tiers"][tier]
            for i, (b, a) in enumerate(
                zip(pair["before"]["points"], pair["after"]["points"])
            ):
                slopes = [
                    pair[s]["segments"][i - 1]["slope"] if i else ""
                    for s in ["before", "after"]
                ]
                w.writerow([tier, b[0], b[2], b[1], a[1], a[1] - b[1], *slopes])
    payload = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    template = (ROOT / "report-template.html").read_text()
    assert template.count("/*__DATA__*/") == 1
    report = template.replace("/*__DATA__*/", payload)
    (ROOT / "index.html").write_text(report)
    for tier in TIERS:
        pair = data["tiers"][tier]
        print(
            tier,
            f"peak slope change -{pair['reduction']:.1f}%",
            "changed points",
            pair["changed_points"],
        )
    print(ROOT / "index.html")


if __name__ == "__main__":
    main()
