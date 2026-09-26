# Lutris in Game Library

Native and Flatpak installations share the same integration. When both have a
configured library, PenguinBurner prefers native while its executable is
available; otherwise it selects Flatpak. Library discovery, settings, Play and
compatibility tools follow that choice. **Rescan** refreshes it after a launcher
installation changes. Flatpak games receive a sandbox-local PenguinBurner runtime;
restart the launcher after first enabling wrapping so it receives filesystem access.

Flatpak Lutris uses `~/.var/app/net.lutris.Lutris/data/lutris/pga.db`, with
configuration under that app's `config/lutris` directory when present, otherwise
its data directory. Its compatibility picker queries Lutris inside that sandbox.

[Game Library](game-library.md) lists installed Lutris games beside Steam and
Heroic.
Enable **Wrap this game**, select a mode, and use **Play** or launch from Lutris.
Wrapping and compatibility-tool changes take effect on the next launch.
Profile modes, Adaptive targets and overlay visibility also apply live to an
already wrapped game, with failures reported alongside the saved preference.

![Game Library with a Lutris game's settings](../assets/game-library.png)

See [per-game settings](game-library.md#per-game-settings) for profiles,
Adaptive targets, GPU selection, and overlay controls.

## Command prefix

PenguinBurner adds its wrapper to the end of the game's `system.prefix_command`,
after your existing command, so it runs next to the game as it does in Steam.
Wrappers such as gamescope stay outside it. For example:

```yaml
system:
  prefix_command: game-performance PENGUIN_BURNER --pb-overlay=1 --pb-game-id=lutris:27
```

The ID identifies the game to PenguinBurner, qualified by the launcher that
owns it. Prefixes written by earlier versions carry `--pb-lutris-id=27`
instead and keep working; they are rewritten the next time you change a
setting, which also moves a wrapper that earlier versions put first. Other configuration keys remain unchanged. Disabling wrapping restores an explicit per-game prefix, or resumes
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

If the preferences file is ever damaged and cannot be read, PenguinBurner keeps
it as `lutris-game-settings.json.corrupt-<timestamp>` beside it and starts a
new one, naming the copy in the status line; the old presets stay recoverable.
