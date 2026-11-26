# Documentation Images

The animated README demo (`auto-uv-full-scan-demo.gif`) shows the 0.8.4
window. Its renderer drove that window, so it lives with the release; to
reproduce the published GIF, install the 0.8.4 wheel and run it from the tag:

```bash
git show v0.8.4:scripts/render-auto-uv-qt-demo.py > /tmp/render-demo.py
python /tmp/render-demo.py --installed \
  --live-game-library --select-game "Diablo III"
```

The scan and overlay telemetry are simulated; profiles use fixed example
results. No scan or GPU write occurs. Rendering requires PySide6, pyqtgraph and
`ffmpeg`. Inspect the GIF and its poster before publishing.

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
