# Faugus Launcher in Game Library

[Game Library](game-library.md) lists your Faugus Launcher games beside Steam,
Lutris and Heroic. Enable **Wrap this game**, select a mode, and use **Play** or
launch from Faugus. Changes apply the next time Faugus starts the game: it
reads its library file at launch, so nothing needs restarting.

See [per-game settings](game-library.md#per-game-settings) for profiles,
Adaptive targets, GPU selection, and overlay controls. Games you have hidden in
Faugus are not listed; unhide them there to configure them here.

## Launch arguments

PenguinBurner adds its wrapper to the game's **Launch arguments**, in
`games.json`, in front of whatever you already had there:

```json
"launch_arguments": "PENGUIN_BURNER --pb-overlay=1 --pb-game-id=faugus:expedition-33 game-performance"
```

The ID identifies the game to PenguinBurner, qualified by the launcher that
owns it. Faugus runs the whole field as one command prefix, ahead of gamemode,
MangoHud and umu-run, so our wrapper goes first and yours stays between it and
the game. Every other field of the entry, and the order of your library, is
left exactly as it was. Disabling removes our part and restores your own
arguments.

The **Command** field is editable and shows that field as Faugus stores it.
Press Enter or leave the field to save. Clearing it leaves the game with no
launch arguments, which is what Faugus's own editor writes for an empty field.

## Overlay and Adaptive

Faugus exists to run Windows games through Proton, and everything Proton runs
is translated to Vulkan, so the overlay reaches them. An entry whose runner is
**Linux-Native** is checked like any other native game: one known to use OpenGL
shows **Game not compatible with Overlay or Adaptive**, and fixed profiles and
launching remain available.

Adaptive keeps timing markers active even with the overlay hidden. See
[latency and FPS](latency-fg.md) for the sources.

## Playing and stopping

**Play** runs `faugus-launcher --game <game id>`, so Faugus starts the game
from its own entry with the runner you chose there. Wrapped sessions are found
through the game identity and session PID carried in the process environment,
and only those sessions can be stopped from PenguinBurner.

If a game starts without the wrapper, Game Library shows **Running in Faugus**
instead of waiting for the startup timeout. Close that game in Faugus or in the
game itself. Those sessions are recognised by the `FAUGUSID` marker Faugus puts
in front of every game it starts — the same one its own "kill this game" uses —
so they are observed even though they carry none of our identity.

## Sorting

Faugus records neither an install date nor a last-played time; it totals
playtime only. **Recently installed** and **Recently played** therefore place
Faugus games last, rather than at a guessed position. **Most played** and
**Alphabetical** work normally.

## Troubleshooting

- **Running in Faugus:** the game started without the wrapper, so its overlay
  and GPU profile were not activated, and **Stop** cannot reach it. Close the
  game, then use **Play** in Game Library to launch it with the saved settings.
- **Changes revert:** close the game's settings window in Faugus Launcher, then
  retry. Saving that window rewrites the whole library file over an external
  edit.
- **A newly added game is missing:** press **Rescan**. Faugus writes
  `games.json` when you finish adding a game.
- **A game is missing and you did not remove it:** check whether it is hidden
  in Faugus. Hidden games are left out.
- **Empty library:** check that `~/.local/share/faugus-launcher/games.json`
  exists and is readable.

PenguinBurner stores per-game preferences in
`~/.config/PenguinBurner/faugus-game-settings.json`. Faugus's own library is
`~/.local/share/faugus-launcher/games.json`, or
`~/.var/app/io.github.Faugus.faugus-launcher/data/faugus-launcher/games.json`
for the Flatpak build.
