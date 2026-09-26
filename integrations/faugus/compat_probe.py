"""Read Faugus's runner directories without importing its migrating GUI modules.

Run inside the selected installation, so paths have the same visibility as
Faugus. The runner IDs follow Faugus's populate_combobox_with_runners contract.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

LATEST_RUNNERS = (
    "Proton-CachyOS Latest", "Proton-GE Latest", "Proton-EM Latest",
    "DW-Proton Latest", "Proton-Wineland Latest",
)


def discover(request: dict) -> list[list[str]]:
    home = Path(request["home"])
    system = request.get("system", False)
    data = Path(os.environ.get("HOST_XDG_DATA_HOME", str(home / ".local/share"))) if system else home / ".local/share"
    host_home = Path(os.environ.get("HOST_HOME", str(home))) if system else home
    roots = (data / "Steam/compatibilitytools.d",
             host_home / ".var/app/com.valvesoftware.Steam/.local/share/Steam/compatibilitytools.d")
    tools = [[name, name.replace("Proton-GE", "GE-Proton")] for name in LATEST_RUNNERS]
    if system:
        for directory in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"):
            if (Path(directory) / "steam/compatibilitytools.d/proton-cachyos-slr").is_dir():
                tools.append(["Proton-CachyOS (System)", "Proton-CachyOS (System)"])
                break
    reserved = {*LATEST_RUNNERS, "UMU-Latest", "LegacyRuntime"}
    versions: dict[str, str] = {}
    for index, root in enumerate(roots):
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if not path.is_dir():
                continue
            if path.name in reserved:
                if index == 1:
                    tools.append([str(path), f"{path.name} (Flatpak)"])
                continue
            versions.setdefault(path.name, f"{path.name} (Flatpak)" if index == 1 else path.name)
    tools.extend([name, label] for name, label in sorted(versions.items(), reverse=True))
    return tools


if __name__ == "__main__":
    print(json.dumps(discover(json.loads(sys.argv[1]))))
