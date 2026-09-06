# Documentation Images

Capture Game Library from the current Qt widget and installed games:

```bash
python scripts/render-game-library.py --select Shelter
```

Run from the repository root with PySide6 installed. The script uses an offscreen
window and defaults to the first Lutris game when `--select` is omitted. It reads
the library without changing game settings or launching a game. Inspect the PNG
before publishing; available games and artwork depend on the host.
