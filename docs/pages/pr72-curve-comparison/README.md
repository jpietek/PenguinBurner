# PR 72 curve comparison

Public URL: https://jpietek.github.io/PenguinBurner/pr72-curve-comparison/

The self-contained HTML compares the six RTX 5070 Ti profiles in
[Ernold11’s attachment](https://github.com/jpietek/PenguinBurner/pull/72#issuecomment-5552597483).
It measures saved curve geometry. Historical runs and changed operating points
mean this does not establish a controlled performance or game frametime gain.

## Regenerate

Requires Python 3 and the original public attachment. Download
[pb-pr72-curves.zip](https://github.com/user-attachments/files/31865984/pb-pr72-curves.zip)
and verify its SHA-256 before extracting it into a temporary directory:

```text
f7bd154d5d7e7b425b8eb215071ca77852d055490e28df56cc954a66be799467
```

From this directory, run:

```bash
python build_report.py --source /path/to/extracted/pb-pr72-curves --archive /path/to/pb-pr72-curves.zip
```

This regenerates `index.html`, plus ignored normalized JSON and CSV files. All
metrics are calculated from the native adjacent voltage points. The report has
no external script or stylesheet dependencies; CSV and SVG exports work offline.

Open `index.html` in a browser to inspect all three tiers, layout and range
controls, hover/keyboard readouts, and exports. Check desktop and mobile layouts
after changing `report-template.html`.

The existing Pages workflow copies only `index.html` into its own subdirectory
after validating the current signed Flatpak release snapshot.
