# Steam in Game Library

Use [Game Library](features/game-library.md) to set a GPU profile, Adaptive FPS
target, and overlay for each Steam game alongside your Lutris library.

## Setup

1. Open **Game Library** and click **Scan my Steam library** if prompted.
2. Restart Steam if the tab shows **Restart Steam to finish**.
3. Select a game and enable **Wrap this game**.
4. Choose its mode and other [per-game settings](features/game-library.md#per-game-settings).

Scanning does not change launch options. Enabling a game adds the wrapper to
that game's options and preserves the existing command. Settings are stored
per Steam account in `~/.config/PenguinBurner/steam-game-settings.json`.

## When edits are available

Steam caches launch options while running. The status line shows how
PenguinBurner can update them:

| Status | Meaning |
| --- | --- |
| live apply | Steam is running and connected; edits go through Steam. |
| Steam stopped | PenguinBurner can write the saved options directly. |
| read-only until initialized | Complete the scan or restart requested beside Rescan. |

Mode, target FPS, and overlay visibility can update a running wrapped game.
Changing **Graphics card** requires a relaunch. Compatibility selection is
independent of wrapping and is disabled for Steam-confirmed native Linux games
or while live Steam control is unavailable.

## Launch and restore

Use **Play / Stop** in Game Library or launch normally from Steam. The wrapper
applies the game profile through the root daemon without an admin prompt.
On exit it restores the standing profile. For a different target GPU, it first
restores that card's saved state, or stock if none is known.

If the daemon is unavailable, the game still starts. A second concurrent game
can start too, but its runtime request does not replace the first game's
monitoring ownership.

## Manual launch options

For manually managed Steam launch options:

```text
PENGUIN_BURNER %command%
```

To show the overlay:

```text
PB_OVERLAY=1 PENGUIN_BURNER %command%
```

See [Game Library](features/game-library.md) for bulk actions and graphics-API
compatibility, or the [overlay guide](features/overlay.md) for HUD controls.
