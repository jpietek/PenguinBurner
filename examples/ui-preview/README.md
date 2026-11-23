# PenguinBurner UI preview (WIP)

Run these commands from the repository root on the pull request branch. The preview uses an example RTX 5080, nine games and a 16-run Auto UV scan. Changes to profiles, launcher settings and fan curves last for the current preview session. No GPU, root service or running game is required.

## Native Qt mock

The Qt mock is the design going forward and will become the real UI. Every tab is a native widget; it needs no QtWebEngine. Install PySide6, pyqtgraph, NumPy and colorama, then run:

```sh
python3 examples/ui-preview/preview.py
```

For a fresh environment, from the checkout:

```sh
python3 -m venv .venv-preview
.venv-preview/bin/python -m pip install PySide6 pyqtgraph numpy colorama
.venv-preview/bin/python examples/ui-preview/preview.py
```

It opens on Auto UV. `--overlay` opens In-Game Overlay, `--history` the Perf Profile of the first game, and `--gpu-index 1` starts on the sample's second GPU. The checkout's own setup and final-choice dialogs, V/F plot, curve editors, Verify and memory offset dialogs, Afterburner chooser, Game Library panel and overlay settings run over sample data. New widgets live in [qt/](qt/): feature pages in `qt/features/`, reusable widgets in `qt/components/`, sample data sources in `qt/sample/`. Each page reads a small data-source object, so a runtime adapter can replace the sample one.

The footer's **Prototype controls** simulates states the sample data cannot reach: root service missing or outdated, two GPUs or none, Steam not set up or needing a restart, an empty library, dependency download, crash recovery, final-verification and process failures, the overlay's desktop, save failures and a missing vkcube. It also runs the startup checks and shows an error report.

## Frozen HTML prototype

The browser prototype (`build_preview.py`, `*.js`, `*.css`) is frozen and no longer updated. It still builds a self-contained `build/ui-preview/index.html` with `python3 examples/ui-preview/build_preview.py --open`.

## Try the workflows

- **Game Library**. Search or filter by launcher, wrapper or recording status. Play locks the other games until the session ends; a game on a tier records a live Perf Profile while it plays. **Perf Profile** beside Play shows the game's own frametime sparkline; hover it for median FPS, 1% low, power and latency, then open it. Switch the frametime plot to **Live** and pick a 10, 30 or 60 s window. **Back to game** returns to the same selection, filters and keyboard focus. Try a game that is still warming up or has capture off.
- **Auto UV**. Choose **Setup Auto Undervolt**, set the power limit, and start a full scan or a selected tier. Click a run to highlight its curve. Each tier opens its own candidate choice and final verification as soon as its descent ends; the profile is saved before the next tier starts. **Copy logs** in the log pane copies every line. **Stop** passes through Stopping before the scan ends.
- **Profiles**. Select a row, try **Silent fan curve**, then **Apply**. **Use Adaptive for a game** opens the game and FPS target picker. Right-click a profile, press the Menu key, or choose **More actions**. Both curve editors save edited copies. **Verify…** reruns final verification on Auto UV with header progress; a pass moves the profile to the top.
- **Import Afterburner**. Try the bundled sample, or choose an exported directory containing `MSIAfterburner.cfg` and `Profiles/`. Select a ready slot, inspect the V/F trace, then choose **Import**. The result and the export's available fan curve appear in the preview’s Profiles list. Imported profiles show dashes for unmeasured benchmarks.
- **In-Game Overlay**. Select a game in Game Library, then open the overlay settings. The preview uses its wide artwork behind the live HUD controls and ticks sample telemetry every second. **Keyboard shortcuts** follows the desktop chosen in Prototype controls.
- **About**. Beside the application name, About shows the version, project support and issue links. With the root service installed it shows the ticking sample lifetime runtime and energy savings; without it, a one-sentence purpose.
- **Appearance**. The palette button at the far right of the header sets the theme (Auto follows the desktop's light or dark setting, or Dark or Light), the accent colour and the font. The accent defaults to **System**, the desktop's accent from the XDG settings portal (Plasma 6, GNOME 47+, Flatpak) or the Qt palette; a desktop without one gets PenguinBurner green, today's look. Presets (PenguinBurner, Blue, Violet, Pink, Graphite) and Custom… replace it. The font is Custom (the bundled Inter unless you pick another family) or System. The Preview at the bottom shows each choice before **Apply**, which rebuilds the window on the same tab and resets the sample session.
- **Window status**. The two lines above the tabs show what runs now and at startup, a running scan with its live candidate, a playing game, or the last error, on every tab.

## Visual verification

The design roles are in [pburn-design.json](pburn-design.json): a neutral set per theme plus the accent presets. [tokens.py](tokens.py) resolves them for a theme and accent without Qt, deriving the accent roles from a seed colour, and [design.py](design.py) turns them into the Qt style sheet and chart pens. The bundled Inter faces and their SIL Open Font License are in [fonts/](fonts/). The sample export in [fixtures/Afterburner](fixtures/Afterburner) contains synthetic V/F and fan curves. The sample covers and wide backdrops are thumbnails of the corresponding Steam title artwork.

Run the regression checks, then render every tab, the Perf Profile, its peek, the Profiles menu, Prototype controls and About at 1350x900:

```sh
python3 -m pytest examples/ui-preview/tests/test_native_preview.py examples/ui-preview/tests/test_qt_*.py -q
python3 examples/ui-preview/capture_qt.py --output-dir /tmp/pburn-qt
python3 examples/ui-preview/capture_qt.py --output-dir /tmp/pburn-qt-light --theme light
```
