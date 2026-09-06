# Lutris in Game Library

[Game Library](game-library.md) lists installed Lutris games beside Steam.
Enable **Wrap this game**, select a mode, and use **Play** or launch from Lutris.
Changes take effect on the next launch.

![Game Library with a Lutris game's settings](../assets/game-library.png)

See [per-game settings](game-library.md#per-game-settings) for profiles,
Adaptive targets, GPU selection, and overlay controls.

## Command prefix

PenguinBurner adds its wrapper to the game's `system.prefix_command`, preserving
your existing command. For example:

```yaml
system:
  prefix_command: PENGUIN_BURNER --pb-overlay=1 --pb-lutris-id=27 game-performance
```

The ID identifies the game to PenguinBurner. Other configuration keys remain
unchanged. Disabling wrapping restores an explicit per-game prefix, or resumes
runner/global inheritance when the game originally inherited its prefix.

The **Command** field is editable. Press Enter or leave the field to save.
If saving fails, the text remains so you can correct it and retry. Wait for a
running library scan to finish before retrying a pending command save.

## Overlay and Adaptive

Adaptive keeps timing markers active even with the overlay hidden. Native games
known to use OpenGL show **Game not compatible with Overlay or Adaptive**;
fixed profiles and launching remain available. Rescan after changing a renderer.

Wine/Proton games can use the NVAPI latency shim. Native games use Vulkan timing
where available. See [latency and FPS](latency-fg.md) for sources and manual
wrapper commands.

## Troubleshooting

- **Changes revert:** close Lutris's game configuration dialog, then retry.
  Saving that dialog can overwrite an external edit.
- **Overlay conflict:** disable MangoHud for the game in Lutris if both HUDs collide.
- **Stale settings:** press **Rescan** after editing a game in Lutris.
- **Empty library:** check that `~/.local/share/lutris/pga.db` exists and is
  readable. A missing, locked, or unreadable database can leave the list empty.

PenguinBurner stores per-game preferences in
`~/.config/PenguinBurner/lutris-game-settings.json`. Lutris game configuration
uses `~/.config/lutris` when present, otherwise `~/.local/share/lutris`.
