# Heroic in Game Library

[Game Library](game-library.md) lists installed Heroic games — Epic, GOG,
Amazon and sideloaded — beside Steam and Lutris. Enable **Wrap this game**,
select a mode, and use **Play** or launch from Heroic. Changes take effect on
the next launch.

See [per-game settings](game-library.md#per-game-settings) for profiles,
Adaptive targets, GPU selection, and overlay controls. DLC entries are not
listed: Heroic shows them beside their game but never launches them.

## Wrapper command

PenguinBurner adds its wrapper to the game's `wrapperOptions`, in
`GamesConfig/<app name>.json`, preserving your existing rows:

```json
"wrapperOptions": [
  { "exe": "PENGUIN_BURNER", "args": "--pb-overlay=1 --pb-game-id=heroic:Turkey" },
  { "exe": "game-performance", "args": "" }
]
```

The ID identifies the game to PenguinBurner, qualified by the launcher that
owns it. Heroic replaces rather than merges, so a game's own rows win outright
over the global ones in Settings → Advanced — whatever you had is written along
with ours and stays between the wrapper and the game, in the same rows Heroic's
settings table shows. Other settings in the file are untouched. Disabling
removes our row, restoring the game's own wrappers or removing the key entirely
so the global ones apply again.

The **Command** field is editable and shows those rows as one command line.
Press Enter or leave the field to save.

## Overlay and Adaptive

Heroic games run under Proton unless the store shipped a Linux build, and
everything Proton runs is translated to Vulkan, so the overlay reaches them. A
native game known to use OpenGL shows **Game not compatible with Overlay or
Adaptive**; fixed profiles and launching remain available.

Adaptive keeps timing markers active even with the overlay hidden. The opt-in
has to be set before the wrapper runs, so it rides an `env` prefix
(`{ "exe": "env", "args": "PB_INGAME_LATENCY=1 PENGUIN_BURNER …" }`) — Heroic
spawns wrapper rows as plain argv, where a bare assignment would be read as the
program to run. See [latency and FPS](latency-fg.md) for the sources.

## Playing and stopping

**Play** goes through Heroic's own `heroic://launch/<runner>/<app name>` link,
so the game starts from its stored configuration with the wrapper already in
it. The running state is read off the wrapper's own command line, so only games
PenguinBurner wraps are tracked here.

## Troubleshooting

- **Changes revert:** close the game's settings page in Heroic, then retry.
  Saving that page rewrites the whole file over an external edit.
- **A newly installed game is missing:** press **Rescan**. Discovery reads each
  store's installed list, which Heroic writes when the install finishes.
- **Empty library:** check that `~/.config/heroic/config.json` exists and is
  readable.

PenguinBurner stores per-game preferences in
`~/.config/PenguinBurner/heroic-game-settings.json`. Heroic's own configuration
is `~/.config/heroic`, or
`~/.var/app/com.heroicgameslauncher.hgl/config/heroic` for the Flatpak build.
