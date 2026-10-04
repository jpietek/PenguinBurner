# Faugus Launcher in Game Library

Native and Flatpak installations share the same integration. When both have a
configured library, PenguinBurner prefers native while its executable is
available; otherwise it selects Flatpak. Library discovery, settings and Play
follow that choice. **Rescan** refreshes it after a launcher installation
changes.

[Game Library](game-library.md) lists your Faugus Launcher games beside Steam,
Lutris and Heroic. Enable **Wrap this game**, select a mode, and use **Play** or
launch from Faugus. Changes apply the next time Faugus starts the game: it
reads its library file at launch, so nothing needs restarting.

See [per-game settings](game-library.md#per-game-settings) for profiles,
Adaptive targets, GPU selection, and overlay controls. Games you have hidden in
Faugus are not listed; unhide them there to configure them here.

## Proton version

Choose **Compatibility tool** to set the game's Proton runner. The list includes
Faugus's Latest channels and locally installed builds visible to the selected
native or Flatpak installation. **UMU-Proton Latest** is Faugus's empty runner
setting; it does not inherit the default used when adding new games.
Changes apply on the next launch. Close the game's settings window in Faugus
before editing here to avoid overwriting the change. Linux-native and Steam
entries do not use this selector. Use Faugus to install additional builds, then
press **Rescan**.

## Launch arguments

PenguinBurner adds its wrapper to the end of the game's **Launch arguments**,
in `games.json`, after your existing command:

```json
"launch_arguments": "PROTON_ENABLE_WAYLAND=0 gamescope -f -- PENGUIN_BURNER --pb-overlay=1 --pb-game-id=faugus:expedition-33"
```

The ID identifies the game to PenguinBurner, qualified by the launcher that
owns it. Faugus runs the whole field as one command prefix, ahead of gamemode,
MangoHud and umu-run. Your part runs first and our wrapper sits last, next to
the game, as it does in Steam: gamescope and other wrappers you add never pick
up the overlay layer themselves. Every other field of the entry, and the order
of your library, is left exactly as it was. Disabling removes our part and
restores your own arguments. A command written by an older version, with our
wrapper first, is moved into place the next time you change a wrapper setting.

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

If a game starts without the wrapper, Game Library shows **Running**, greyed
out, rather than reporting nothing. Close that game in Faugus or in the game
itself. Those sessions are recognised by the `FAUGUSID` marker Faugus puts
in front of every game it starts — the same one its own "kill this game" uses —
so they are observed even though they carry none of our identity.

An already-running store client can start a game with the client's inherited
ID. PenguinBurner also checks the game's full executable path in its Wine
prefix to follow that handoff. Detection alone does not prove that the overlay
or Adaptive reached the game, and does not grant Stop control over the client.

## Troubleshooting

- **Running**, greyed out: the game started without the wrapper, so its
  overlay and GPU profile were not activated, and **Stop** cannot reach it.
  Close the game, then use **Play** in Game Library to launch it with the
  saved settings.
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
