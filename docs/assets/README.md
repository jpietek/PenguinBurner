# Documentation Images

Render the animated README demo with the current Qt UI and its deterministic
sample data:

```bash
python scripts/render-auto-uv-qt-demo.py
```

To show the installed Steam, Lutris, Heroic and Faugus libraries, select a game
visible on the capture host. The committed GIF selects Faugus's Diablo III:

```bash
python scripts/render-auto-uv-qt-demo.py \
  --live-game-library --select-game "Diablo III" --screenshots-dir docs/assets
```

The README demo was refreshed from the installed **0.8.4** release. To reproduce
that boundary after installing the released wheel, run:

```bash
python scripts/render-auto-uv-qt-demo.py --installed \
  --live-game-library --select-game "Diablo III"
```

`--installed` loads the installed distribution's Qt UI and prints its version
and module path. Without that flag, the renderer uses the checkout. The tour
includes the redesigned overlay settings, DLSS 5 NR timing, hotspot temperature,
and both frame-timing graphs, with a longer hold on the overlay screen.

The scan and overlay telemetry are simulated; profiles use fixed example
results. No scan or GPU write occurs. `--screenshots-dir` also refreshes the
scan, profiles and overlay PNGs. Rendering requires PySide6, pyqtgraph and
`ffmpeg`. Inspect the GIF, its generated poster and PNGs before publishing.

Capture Game Library from the current Qt widget and installed games:

```bash
QT_SCALE_FACTOR=2 python scripts/render-game-library.py --select Shelter
QT_SCALE_FACTOR=2 python scripts/render-game-library.py --select "The First Tree" \
  --output docs/assets/game-library-heroic.png
QT_SCALE_FACTOR=2 python scripts/render-game-library.py --select "Diablo III" \
  --output docs/assets/game-library-faugus.png
```

These show Lutris, Heroic and Faugus settings respectively. The README and
Game Library guide use the Faugus image. Games and artwork depend on the host;
choose another installed title if necessary.

Refresh setup, curve editors and Afterburner import using fixed examples:

```bash
python scripts/render-auto-uv-setup.py
python scripts/render-doc-dialogs.py
```

The real dialogs are captured and cancelled; nothing is saved or applied.
Example curves illustrate the interface, not recommended settings or new
hardware measurements.

Run from the repository root with PySide6 and Noto Sans installed. Captures use
offscreen Qt windows. The library renderer defaults to the first Lutris game
when `--select` is omitted; it does not change settings or launch games.
