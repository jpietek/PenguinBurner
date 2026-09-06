# Auto-UV cookbook and RTX 5080 verification

The diagrams are editable Mermaid sources in `diagrams/*.mmd`, styled by
`diagrams/config.json` (including a fixed drawing seed). Render them with **Mermaid CLI 11.12.0** and an installed
Chrome/Chromium browser; the rendering script checks the CLI version. To install
the pinned renderer once and regenerate the SVGs and standalone page:

```sh
diagram_tools="${XDG_CACHE_HOME:-$HOME/.cache}/penguinburner-mermaid"
PUPPETEER_SKIP_DOWNLOAD=true npm install --prefix "$diagram_tools" --no-save @mermaid-js/mermaid-cli@11.12.0
python docs/pages/auto-uv-cookbook/render_diagrams.py \
  --mmdc "$diagram_tools/node_modules/.bin/mmdc" \
  --chrome /usr/bin/google-chrome
python docs/pages/auto-uv-cookbook/build_report.py
```

Use `--chrome` for your browser's path. These diagrams were rendered with Chrome
152.0.7977.82. Inspect the generated SVGs and the page at desktop and mobile
widths, including both diagram size modes. Commit the Mermaid sources and their
SVG outputs together. The page embeds the SVGs and needs no Mermaid runtime.

`measurements.json` contains the reviewed public subset of the final RTX 5080 scan, plus historical
pre-smoothing final-probe curves: requested points, final statistics, checks and
provenance. The geometry comparison is separate from current loaded measurements.
Private host paths, GPU UUIDs and runtime configuration are excluded.
The generator rejects incomplete or unsuccessful three-tier verification.
The self-contained page makes no third-party network requests.

To refresh the public data from local completed scan directories:

```sh
python docs/pages/auto-uv-cookbook/collect_measurements.py AFTER CHECKS_JSON
```

The completed scan directory must contain its `scan.log`, `results-report.json`,
and saved profiles. The checks receipt must identify the tested commit and passing checks.

Historical curves are exported from the pinned scan log and matching checkpoints:

```sh
python docs/pages/auto-uv-cookbook/collect_smoothing.py \
  --log SCAN_LOG --checkpoint-dir CHECKPOINT_DIRECTORY
```

The exporter checks source hashes, final PASS/save records, and exact curve
equality. Historical final-profile files are unavailable; these are recorded
final-probe curves matched to pre-final checkpoints. The old and current voltage
grids match, while recorded stock MHz can differ; the plot states that difference.
