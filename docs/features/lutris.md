# Lutris

> Feature guide — see the [README](../../README.md) for the project overview,
> [adaptive-uv.md](./adaptive-uv.md) for what the tiers do, and
> [overlay.md](./overlay.md) for the in-game HUD.

The **Game Library** tab lists your Lutris library beside every other
launcher's, and lets you decide, per game,
what PenguinBurner does when Lutris starts it: which Auto-UV profile applies,
whether Adaptive aims at a different frame rate than the system-wide one, and
whether the overlay is drawn.

The Game Library loading state covers every detected launcher. Its centered
spinner appears after half a second on the first load, with launcher names
separated by commas. Rescan keeps the existing list and selected game visible.

The Game Library tab can start and stop a Lutris game, through Lutris's own
CLI (`lutris:rungameid/<id>`), so the game launches from its stored config and
the wrapper below is already in the line Lutris builds. Stopping signals the
game's `lutris-wrapper`, which takes its whole process tree down the way Lutris
does it itself.

PenguinBurner still does not **reconfigure** a running game, so a setting
changed here takes effect **the next time you launch it**.

## What it writes

Enabling a game puts the PenguinBurner wrapper into that game's
`system.prefix_command`, in `games/<configpath>.yml` under Lutris's own
config directory — `~/.config/lutris` where that directory exists (Lutris
still prefers it), otherwise `~/.local/share/lutris`:

```yaml
system:
  prefix_command: PENGUIN_BURNER --pb-overlay=1 --pb-lutris-id=27 game-performance
```

Three things are worth knowing about that line:

- **Your own prefix is kept.** Anything already in `prefix_command` —
  `game-performance`, `gamemoderun`, whatever — stays, and stays between the
  wrapper and the game.
- **`--pb-lutris-id` is how the wrapper knows which game this is.** Lutris
  regenerates `LUTRIS_GAME_UUID` on every launch and publishes nothing stable,
  so unlike Steam there is no app id to read from the environment. The tab
  writes the id it stores your settings under.
- **Nothing else in the config is touched.** Every other key is preserved, the
  write is atomic, and a config that does not parse is reported rather than
  overwritten.

Disabling a game removes the wrapper and restores the `prefix_command` you had
before — or removes the key entirely if you had none.

## The settings

| Setting | What it does |
| --- | --- |
| **PenguinBurner for this game** | Whether the wrapper is in the game's `prefix_command` at all. Off, Lutris launches the game untouched. |
| **Auto-UV mode** | `Adaptive` switches tiers from live frame pacing; a named tier pins that profile; `Stock` pins the factory GPU state for this game while your standing profile stays tuned. |
| **Adaptive target** | The base present-frame rate Adaptive aims for in this game. At its lowest position the game follows the system-wide target from the Adaptive window. |
| **Graphics card** | Which GPU the profile applies to. Hidden on a single-GPU machine, which resolves it itself. |
| **In-game overlay** | Whether the PenguinBurner overlay draws in this game. |
| **Command prefix** | The line as Lutris has it. Editable — press Enter to write it back. |

Settings persist in `~/.config/PenguinBurner/lutris-game-settings.json`, keyed
by Lutris game id. There is no account layer — unlike Steam, a Lutris library
belongs to one user.

## Adaptive markers without the overlay

For native Linux games detected as OpenGL, the library shows **Game not
compatible with Overlay or Adaptive** and greys out Overlay, the Adaptive
choice, and the FPS target controls. Fixed GPU profiles and launching remain
available, and saved preferences are preserved. Rescan after changing a native
game's renderer. Unknown renderers and Wine/Proton games keep their controls.
The local wheel can include both 32-bit and 64-bit Vulkan overlay libraries;
32-bit DXVK games need the 32-bit companion.

Adaptive automatically keeps latency markers running when the overlay is off.
This lets it pace against base rendered frames under frame generation without
making the user coordinate a second switch. The only visible in-game setting is
the overlay: it decides whether the HUD is drawn, not whether Adaptive receives
the pacing signal it needs.

The Adaptive launch prefix carries the opt-in at the front of the line:

```
env PB_INGAME_LATENCY=1 PENGUIN_BURNER --pb-overlay=0 --pb-lutris-id=27 game-performance
```

`env` introduces it because Lutris runs `prefix_command` as a command list with
no shell, where a bare `PB_INGAME_LATENCY=1` would be read as the program to
run rather than as a variable.

With the overlay on, the wrapper already enables markers and omits the redundant
explicit assignment. Selecting a fixed tier or Stock removes headless marker
capture; those modes do not adapt from live pacing.

## Editing the command prefix yourself

The toggles compose that line for you, but the field itself is editable: type
into it and PenguinBurner writes exactly what you left there — on Enter, or as
soon as you click away — then re-reads the toggles from it. Clear the wrapper out by hand and the switch
turns itself off — after a hand edit the config is the truth, not what the tab
remembered.
If saving fails, PenguinBurner keeps the edited text and the game selected so
you can correct or retry the command. If a library scan is still running, wait
for it to finish and retry the save.

## Things to watch for

**Lutris's own configuration window wins.** Lutris keeps a game's config in
memory while its configuration dialog is open and rewrites the whole file when
you press Save. PenguinBurner reads every write back and tells you if Lutris
undid it — close that dialog in Lutris and try again.

**MangoHud.** Lutris applies its MangoHud option outside this wrapper, so the
PenguinBurner overlay and MangoHud can end up drawn on the same swapchain. If
they collide, turn MangoHud off for the game, or globally in Lutris's
`system.yml` (in the same config directory as the game configs).

**Press Refresh after changing a game in Lutris.** The tab reads each game's
live `prefix_command` when it refreshes; it does not watch the files.

**Wine/Proton games get the most.** The NVAPI shim and in-game latency
telemetry need a wine prefix. A native Linux runner still gets the GPU profile,
but not the shim-based readouts.

## If the list is empty

PenguinBurner reads `~/.local/share/lutris/pga.db`. If that file is missing the
tab says so; if Lutris is installed but you have no games installed yet, it
says that instead. A locked or unreadable database also reads as an empty
library rather than failing — Lutris may be running and writing to it.
