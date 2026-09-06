# PR 72 curve comparison

Public URL: https://jpietek.github.io/PenguinBurner/pr72-curve-comparison/

The standalone HTML compares nine RTX 5070 Ti profiles from Ernold11's
[original attachment](https://github.com/jpietek/PenguinBurner/pull/72#issuecomment-5552597483)
and [September 6 update](https://github.com/jpietek/PenguinBurner/pull/72#issuecomment-5558750277).
The default Before reference is the oldest August pre-smoothing set; September 5
is selectable. After always uses September 6. All frequency plots use 0–3,250 MHz.

The files share a GPU and 127 voltage bins, but their stock clocks differ by up
to 15 MHz. Each run retains its own stock reference. Historical operating points
also differ: this is geometry and recorded telemetry, not a controlled benchmark.
The latest build and gameplay observations are attributed to the contributor;
the latest ZIP does not contain a scan log or embedded build identifier.

## Regenerate

Requires Python 3 and both original public ZIPs; extraction is unnecessary:

- [pb-pr72-curves.zip](https://github.com/user-attachments/files/31865984/pb-pr72-curves.zip)
  SHA-256: `f7bd154d5d7e7b425b8eb215071ca77852d055490e28df56cc954a66be799467`
- [pb-uv-tiers-20260906.zip](https://github.com/user-attachments/files/31878914/pb-uv-tiers-20260906.zip)
  SHA-256: `5fcc8dc33853beaa0da7f94fa40599d1ff58e59e479c8d18cb0c194bd67c6755`

From this directory, run:

```bash
python build_report.py \
  --archive /path/to/pb-pr72-curves.zip \
  --latest-archive /path/to/pb-uv-tiers-20260906.zip
```

The generator authenticates both archives, verifies profile identity and point
consistency, and regenerates `index.html` plus ignored JSON and CSV data. The
normalized CSV includes all nine curves; the browser CSV export compares the
selected Before reference against September 6 across all three tiers, with
separate before/after stock columns. SVG export uses the selected tier/reference.
All metrics use native adjacent points, without fitting or averaging curves.

The report has no external script or stylesheet dependencies. Check both
references, all tiers, layouts, ranges, stock/point controls, hover/keyboard
readouts and exports in desktop and mobile layouts after template changes.

The existing Pages workflow copies only `index.html` into its own subdirectory
after validating the current signed Flatpak release snapshot. The
[Auto-UV cookbook](https://jpietek.github.io/PenguinBurner/auto-uv-cookbook/)
contains the algorithm walkthrough and separate local RTX 5080 verification.
