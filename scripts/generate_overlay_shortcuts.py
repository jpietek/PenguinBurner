#!/usr/bin/env python3
"""Generate desktop shortcut actions from overlay.shortcuts; --check detects drift."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from overlay.shortcuts import SHORTCUTS  # noqa: E402


def desktop_entry(text: str) -> str:
    # Preserve package-specific identity and launcher metadata before the actions.
    header = text.split("[Desktop Action ", 1)[0].rstrip()
    actions = ";".join(item.desktop_id for item in SHORTCUTS) + ";"
    header = re.sub(r"^Actions=.*$", "Actions=" + actions, header, flags=re.MULTILINE)
    blocks = [header]
    for item in SHORTCUTS:
        blocks.append(f"[Desktop Action {item.desktop_id}]\nName={item.label}\n"
                      f"Exec=penguin-burner-cli {item.flag}\nX-KDE-Shortcuts=Alt+{item.key}")
    return "\n\n".join(blocks) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for package in ("linux", "flatpak"):
        path = ROOT / "packaging" / package / "io.github.jpietek.PenguinBurner.desktop"
        text = path.read_text()
        generated = desktop_entry(text)
        if args.check:
            if text != generated:
                raise SystemExit(f"Stale desktop actions: {path.relative_to(ROOT)}")
        else:
            path.write_text(generated)


if __name__ == "__main__":
    main()
