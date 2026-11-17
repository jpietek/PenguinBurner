"""Build the standalone PenguinBurner UI preview from checkout-owned assets."""

import argparse
import base64
import json
import tomllib
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SCRIPTS = (
    "auto-uv-dialogs",
    "shell",
    "library",
    "profiles",
    "auto-uv",
    "overlay",
    "charts",
    "about",
    "afterburner",
    "profile-menu",
    "guide",
)
ICONS = (
    "penguin-burner",
    "tab-auto-uv",
    "tab-profiles",
    "tab-game-library",
    "tab-overlay",
    "tab-steam",
    "tab-heroic",
    "tab-lutris",
    "tab-faugus",
)
REFERENCES = (
    "game-library",
    "profiles-management",
    "auto-uv-scan",
    "overlay",
)


def image_assets() -> dict[str, str]:
    files = [ROOT / "ui/assets" / f"{name}.png" for name in ICONS]
    files += [ROOT / "docs/assets" / f"{name}.png" for name in REFERENCES]
    files += sorted((HERE / "artwork").glob("*.webp"))
    return {
        path.stem: f"data:image/{path.suffix[1:]};base64,"
        + base64.b64encode(path.read_bytes()).decode("ascii")
        for path in files
    }


def design_css(design: dict) -> str:
    roles = dict(design["colors"])
    for section, prefix in (
        ("typography", "type"),
        ("spacing", "space"),
        ("radii", "rad"),
    ):
        for key, value in design[section].items():
            roles[f"{prefix}-{key}"] = f"{value}px" if isinstance(value, int) else value
    return (
        ":root {" + "".join(f"--{key}: {value};" for key, value in roles.items()) + "}"
    )


def build_preview(output: Path | None = None) -> Path:
    """Return a self-contained file that can also be loaded in the native Qt preview."""
    output = (output or ROOT / "build/ui-preview/index.html").resolve()
    design = json.loads((HERE / "pburn-design.json").read_text())
    fixture = HERE / "fixtures/Afterburner"
    export = {
        path.relative_to(fixture).as_posix(): path.read_text()
        for path in sorted(fixture.rglob("*.cfg"))
    }
    data = {
        "appVersion": tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
            "version"
        ],
        "exampleAfterburner": export,
        "sample": json.loads((HERE / "sample.json").read_text()),
        "design": design,
        "assets": image_assets(),
    }
    # Data is embedded in a script element, so an asset label cannot close the element.
    constants = "\n".join(
        f"const {key} = " + json.dumps(value).replace("<", r"\u003c") + ";"
        for key, value in data.items()
    )
    scripts = "\n".join((HERE / f"{name}.js").read_text() for name in SCRIPTS)
    styles = "\n".join(
        [
            design_css(design),
            (HERE / "pburn.css").read_text(),
            (HERE / "auto-uv-dialogs.css").read_text(),
        ]
    )
    template = (HERE / "index.template.html").read_text()
    page = template.replace("/* PREVIEW_STYLES */", styles).replace(
        "/* PREVIEW_SCRIPT */", constants + "\n" + scripts
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, help="output file (default: build/ui-preview/index.html)"
    )
    parser.add_argument(
        "--open", action="store_true", help="open the preview in your browser"
    )
    args = parser.parse_args()
    path = build_preview(args.output)
    print(path)
    if args.open:
        webbrowser.open(path.as_uri())
