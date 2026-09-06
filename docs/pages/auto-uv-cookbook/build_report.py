#!/usr/bin/env python3
"""Build the standalone cookbook from reviewed, public measurement data."""

from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
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
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    template = (root / "template.html").read_text()
    for name in ("scan", "voltage", "clock"):
        template = template.replace(
            f"__{name.upper()}_SVG__", (root / "diagrams" / f"{name}.svg").read_text()
        )
    assert template.count("__MEASUREMENTS__") == 1
    (root / "index.html").write_text(template.replace("__MEASUREMENTS__", payload))
    print(root / "index.html")


if __name__ == "__main__":
    main()
