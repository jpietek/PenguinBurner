#!/usr/bin/env python3
"""Build the standalone cookbook from reviewed, public measurement data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write a preview to this path instead of index.html")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    data = json.loads((root / "measurements.json").read_text())
    run = data["run"]
    assert data["validation"]["commit"] == run["commit"]
    assert data["validation"]["all_checks_passed"]
    assert run["success"] and set(run["tiers"]) == {"efficiency", "balanced", "performance"}
    assert run["restored"] and run["boot_unchanged"] and not run["gpu_errors"]
    assert data["before_smoothing"]["gpu"] == run["gpu"]
    for key, tier in run["tiers"].items():
        assert tier["verified"] and tier["final_seconds"] == 10
        assert tier["monotonic"] and tier["plan_matches_points"]
        assert tier["saved_curve_matches_final_probe"]
        assert [p[0] for p in data["before_smoothing"]["tiers"][key]["curve"]] == [p[0] for p in tier["curve"]]
    recovery = data.get("recovery_run")
    if recovery is not None:
        receipt = data["recovery_validation"]
        assert receipt["commit"] == recovery["commit"] and receipt["all_checks_passed"]
        assert set(recovery["tiers"]) <= {"efficiency", "balanced", "performance"}
        for tier in recovery["tiers"].values():
            if tier.get("verified"):
                assert tier["final_seconds"] > 0
                assert tier["monotonic"] and tier["plan_matches_points"]
                assert tier["saved_curve_matches_final_probe"]
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    template = (root / "template.html").read_text()
    for name in ("recovery", "scan", "baseline", "voltage", "custom", "clock", "final"):
        template = template.replace(
            f"__{name.upper()}_SVG__", (root / "diagrams" / f"{name}.svg").read_text()
        )
    assert template.count("__MEASUREMENTS__") == 1
    output = args.output or root / "index.html"
    output.write_text(template.replace("__MEASUREMENTS__", payload))
    print(output)


if __name__ == "__main__":
    main()
