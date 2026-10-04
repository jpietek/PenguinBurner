# Documentation Images

Render the animated README demo with the current Qt UI and its deterministic
sample data:

```bash
python scripts/render-auto-uv-qt-demo.py
```

To show the installed Steam, Lutris and Heroic library instead, use the
read-only live
library option and select a game visible on the capture host:

```bash
python scripts/render-auto-uv-qt-demo.py \
  --live-game-library --select-game Shelter
```

The Auto-UV scan and overlay telemetry remain simulated in both modes, and the
renderer does not contact the hardware daemon. Rendering requires PySide6,
pyqtgraph, and `ffmpeg`. The committed GIF uses the live-library command above;
its games and artwork reflect that capture host. Inspect the GIF and generated
poster before publishing.

Capture Game Library from the current Qt widget and installed games:

```bash
python scripts/render-game-library.py --select Shelter
python scripts/render-game-library.py --select "The First Tree" \
  --output docs/assets/game-library-heroic.png
```

The first is the shared library shot used by the README and the Game Library
guide; the second shows a Heroic game's own settings for the Heroic guide.

Run from the repository root with PySide6 installed. The script uses an offscreen
window and defaults to the first Lutris game when `--select` is omitted. It reads
the library without changing game settings or launching a game. Inspect the PNG
before publishing; available games and artwork depend on the host.
