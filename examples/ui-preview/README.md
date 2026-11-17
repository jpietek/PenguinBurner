# PenguinBurner UI preview (WIP)

Run these commands from the repository root on the pull request branch. The preview uses an example RTX 5080, seven games and a 16-run Auto UV scan. Changes to profiles, launcher settings and fan curves last for the current preview session. No GPU, root service or running game is required.

## Clickable HTML

Python 3.11 or newer is sufficient.

```sh
python3 examples/ui-preview/build_preview.py --open
```

This builds a self-contained file at `build/ui-preview/index.html`. You can open it offline, move it to another computer, or share that single file. It starts in Game Library. To open a particular example, append a fragment to the file address.

| Fragment | Opens |
| --- | --- |
| `#history` | The selected game's recording, with the performance fingerprint first |
| `#auto-uv` | Auto UV before the first scan |
| `#setup` | GPU, scan scope and per-tier setup |
| `#verification` | Passing candidates and final verification duration for each tier |
| `#scan-results` | All 16 runs, the full log and three verified profiles |
| `#profiles` | Example profiles with editing, apply, startup and adaptive controls |
| `#overlay` | In-Game Overlay settings and preview |
| `#design-system` | PBurn colors, typography, spacing and interaction examples |

## Native Qt mock

In an environment with PySide6 including QtWebEngine, pyqtgraph, NumPy and colorama, run:

```sh
python3 examples/ui-preview/preview.py
```

For a fresh environment, from the checkout:

```sh
python3 -m venv .venv-preview
.venv-preview/bin/python -m pip install PySide6 pyqtgraph numpy colorama
.venv-preview/bin/python examples/ui-preview/preview.py
```

The native mock opens on Auto UV. It uses the checkout's Qt setup and final verification dialogs, V/F plot, curve editors, Afterburner chooser, Game Library and Overlay settings. Profiles and the recording detail use the embedded HTML view.

## Try the workflows

- **Game Library**. Search or filter by launcher, wrapper or recording status. On a recorded game, choose **View history**. **Back to game** returns to the same selection, filters and keyboard focus. Try a game with a short recording or with capture off.
- **Auto UV**. Choose **Setup Auto Undervolt**, set the power limit, and start a full scan or a selected tier. Click a run to highlight its curve. Once the scan finishes, choose a passing candidate and duration for each tier, start verification, then open the resulting profiles. **Copy full log** is beside the latest line and copies every line.
- **Profiles**. Select a row, try **Silent fan curve**, then **Apply**. **Use Adaptive for a game** opens the game and FPS target picker. Right-click a profile, press **Shift+F10** on its name, or choose **More actions**. Both curve editors save edited copies.
- **Import Afterburner**. Try the bundled sample, or choose an exported directory containing `MSIAfterburner.cfg` and `Profiles/`. Select a ready slot, inspect the V/F trace, then choose **Import**. The result and the export's available fan curve appear in the preview’s Profiles list. Imported profiles show dashes for unmeasured benchmarks.
- **In-Game Overlay**. Select a game in Game Library, then open the overlay settings. The preview uses its wide artwork behind the live HUD controls. The expanded preview uses the same background.
- **About**. Beside the application name, About shows the version, project support and issue links. After scan activity it shows the ticking sample lifetime runtime and energy savings.

## Visual verification

The design roles are in [pburn-design.json](pburn-design.json). The sample export in [fixtures/Afterburner](fixtures/Afterburner) contains synthetic V/F and fan curves. The [screenshots](screenshots/) show the browser and native Qt renderers. The browser's sample covers and wide backdrops are thumbnails of the corresponding Steam title artwork.

Run the focused regression checks with Node.js 22+ and the Python test dependencies:

```sh
node --test examples/ui-preview/tests/*.test.cjs
python3 -m pytest examples/ui-preview/tests/test_native_preview.py -q
```

To regenerate the browser images, install the optional `playwright` Python package and have Chrome or Chromium on your path. Pass `--browser /path/to/chromium` to select another executable. The native capture uses the same dependencies as the Qt mock.

```sh
python3 examples/ui-preview/capture_browser.py
python3 examples/ui-preview/capture_qt.py
```
