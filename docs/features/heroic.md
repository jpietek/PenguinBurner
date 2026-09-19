# Heroic in Game Library

[Game Library](game-library.md) lists installed Heroic games — Epic, GOG,
Amazon and sideloaded — beside Steam and Lutris. Enable **Wrap this game**,
select a mode, and use **Play** or launch from Heroic. Game Library's **Play**
refreshes saved launch settings automatically,
restarting an idle Heroic when necessary to clear its cached configuration.
If you launch directly from Heroic after editing settings in PenguinBurner,
fully exit and reopen Heroic first. Overlay visibility also updates live in an
already wrapped game.

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

An explicitly empty wrapper list stays empty after enabling and disabling
PenguinBurner; it does not start inheriting global wrappers.

The **Command** field is editable and shows those rows as one command line.
Press Enter or leave the field to save.
Invalid quoting is reported without changing the saved command. Clearing the
field resumes inheritance from Heroic's global wrappers.

## Overlay and Adaptive

The **In-Game overlay** switch shows or hides the overlay in a running wrapped
game within about one second, without restarting Heroic or the game. The
setting is also saved for future launches. If the game started without the
PenguinBurner wrapper, the GUI reports that a relaunch is needed: the overlay
must be loaded at game startup before its visibility can be changed live.

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
so Heroic starts the game with its selected runner. Wrapped sessions use the
game identity and session PID carried in the process environment, including
when no saved profile can be applied. Only those sessions can be stopped from
PenguinBurner. Helper processes are excluded from session detection and Stop.

If Heroic starts a game without the wrapper, Game Library shows **Running in
Heroic** instead of waiting for the two-minute startup timeout. Close that
game in Heroic or in the game itself. Its launch process is checked every
1.5 seconds; two successful checks confirming exit return the button to
**Play**. A configured game that started without PenguinBurner also shows
relaunch instructions: its overlay and GPU profiles were not activated.
If a tracked session cannot be inspected, its state is held until it can be
confirmed again; daemon-tracked sessions remain visible even when their
environment cannot be read. Play requires either the native Heroic command or
the installed Heroic Flatpak application.

## Troubleshooting

- **Running in Heroic:** close the game, then use **Play** in Game Library
  to launch with the saved settings. Relaunch instructions remain visible
  after the game exits and clear on a new launch.
- **Heroic is busy:** finish its game, download or other operation, then press
  **Play** again. PenguinBurner only restarts an idle launcher and never forces
  it to exit. Refreshing the launcher runs in the background so the GUI remains
  responsive.
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
