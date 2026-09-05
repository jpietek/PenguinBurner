# Steam Integration

PenguinBurner's **Game Library** tab turns undervolting into per-game
automation:
discover your installed library, pick how the GPU should behave for each game,
and let PenguinBurner apply it automatically when that game launches — no manual
profile switching.

![Steam games in the Game Library tab](assets/steam-tab.png)

## Why it matters

The Game Library tab lists Steam and Lutris side by side, each game marked
with its launcher's icon, sorted by name, by when it was last played, or by
how long it has been played.

The system-wide profile is one setting for everything. Steam integration lets a
single library hold **different behavior per game**: a light indie title runs
dead-silent on Efficiency while a demanding shooter gets Performance or adapts
live. It is also the only way to get the **adaptive, per-game pre-frame-generation
FPS target** (below) and the in-game overlay, because both are delivered through
the launch wrapper.

## Setup (one scan of your library)

1. Open the **Game Library** tab. If PenguinBurner has not seen your library
   yet, click **Scan my Steam library**. This is safe and non-destructive: every
   game is listed, all left **disabled**, and no launch options are changed.
2. Restart Steam once if prompted (this connects live apply).
3. Select a game and toggle **Enable PenguinBurner per-game profiles**. Only then
   does PenguinBurner add its launch-options wrapper to that one game.

Nothing is forced. Enablement is strictly per game and per Steam account, stored
in `~/.config/PenguinBurner/steam-game-settings.json`.

Opening the Game Library or pressing **Rescan** reads Steam's live game details
together. An unavailable game's timeout does not add a separate wait for every
other game, and successful reads are kept.
On the first load, a centered spinner replaces both library panes if the scan
takes longer than half a second. It lists the detected launchers with comma
separators. Rescan keeps the existing games and settings visible, with a small
activity indicator for a slower scan; automatic background refreshes stay quiet.

## When Steam will take a change

Steam holds each game's launch options in memory while the client runs, so a
change written straight to disk behind its back would be overwritten on exit.
PenguinBurner therefore says up front whether a change can land, rather than
letting you type one and refusing it afterwards:

- The status line at the bottom of the tab carries the signed-in account and one
  of **live apply** (Steam is running and connected), **Steam stopped** (nothing
  is holding the file, so PenguinBurner writes it directly), or **read-only until
  initialized**.
- In that last state the per-game editors are grayed out and a single button
  appears beside **Rescan** with the one thing that fixes it — **Scan my Steam
  library** the first time, or **Restart Steam to finish** when Steam has been
  running since before the integration was set up.

Lutris needs none of this: its settings are files PenguinBurner owns the writing
of, so its games are always editable.

## Per-game options

The per-game editor exposes:

- **Game GPU** — shown only when two or more physical NVIDIA GPUs are detected.
  Choose the card PenguinBurner should tune for this game before enabling the
  wrapper. The setting stores the GPU UUID and resolves its current index at
  launch; PenguinBurner does not guess from the active display or GPU load.
- **Compatibility tool** — Steam's current per-game override, or its effective
  default Proton when no override was saved. PenguinBurner reads the effective
  tool from Steam's live per-app details API; it does not infer "native" from a
  missing config entry. Only games Steam explicitly reports as native Linux
  have this selector disabled and visibly grayed out.
- **Auto-UV mode** — one of:
  - **Adaptive** — starts from your newest profile and switches between saved
    tiers using live present-frame pacing (see below). Adaptive automatically
    keeps latency markers active when the overlay is hidden, so frame generation
    does not make it pace against generated presents.
  - **Efficiency / Balanced / Performance** — pin one saved tier for this game.
  - **Stock (factory GPU state)** — run this game at the factory curve while your
    system-wide profile stays tuned.
- **Adaptive target FPS** — the per-game base-present FPS the adaptive engine
  aims for (15–1000, default 60). It decides when to promote toward more clock or
  demote toward more efficiency. This is **per game**, so a 60 Hz story game and a
  144 Hz competitive shooter each get their own target.
- **Enable In-Game overlay** — the live readout (latency, pre-frame-gen FPS,
  clocks, power, tier) for this game. This controls only HUD visibility;
  Adaptive's required marker capture remains automatic in the background.
  For a running wrapped Steam game, visibility also updates live (the native
  layer checks about once per second); changing an idle game's setting does not
  affect another running game.

The tuning and overlay controls stay grayed out until the game's PenguinBurner
toggle is on. The compatibility selector is independent of that toggle; it is
grayed out only for a Steam-confirmed native Linux runtime or when live Steam
control is unavailable.

## Bulk actions ("All games")

Next to the library sort control, the **All games** menu applies an action across
your whole library in one confirmed step:

- **Enable / Disable PenguinBurner for all games** — add or remove the wrapper
  everywhere. Enabling keeps the overlay off and leaves each game's saved mode
  intact; disabling restores every game's original Steam launch options. On a
  multi-GPU host, every game needs a Game GPU before bulk enable can run.
- **Show / Hide In-Game overlay for enabled games.**

Each action confirms first and shows the game count. Directions that would change
nothing (for example "enable all" when everything is already enabled) gray out, so
the menu doubles as a state readout. Bulk enable also spells out its two side
effects: the overlay stays off, and MangoHud is disabled inside wrapped games.
Bulk writes run in the background: the list stays usable and subsequent
individual toggle changes are queued until the bulk operation finishes.
Background scans and settings writes run in sequence, so a scan cannot replace
a newly saved setting with an older value. The window remains responsive while
a change waits for a scan to finish.

## Play / Stop

The per-game editor's button is a single Steam-style control that reflects the
live session: green **Play** → **Starting…** → red **Stop** while running →
**Stopping…** → back to Play. Stop requests run in the background. Once the
launcher answers, **Stopping…** can be pressed again if the game is still
running; a failed request restores the button so it can be retried.

Lutris command edits preserve quoted text and spacing. When wrapping a game
that inherited its command prefix, disabling the wrapper resumes inheritance,
including runner/global changes made in the meantime. Explicit per-game
prefixes and externally edited commands are preserved.
If a command cannot be saved, the editor keeps its text and the current game
selected. Correct the command or resolve the reported problem, then retry. A
command save during a background scan stays pending until you retry after the
scan finishes.

## How it applies (no password prompts)

Enabling a game splices the `PENGUIN_BURNER` wrapper into its Steam launch
options. At launch the wrapper resolves the game and account, then asks the
already-root daemon to apply that game's profile over the socket — no elevation,
no per-game password. When the game exits, the daemon restores your standing
profile automatically. For a cross-GPU override it first restores that game's
target GPU to its prior saved state, then resumes the standing GPU. If no prior
state is known, it restores the game GPU to stock. If the daemon is unreachable,
the wrapper soft-fails and the game launches normally; PenguinBurner never
blocks a launch.

Adaptive mode additionally passes the per-game target FPS, and a live change to
a running game is re-applied in place without a relaunch. Changing the Game GPU
itself requires restarting the game.

The daemon has one active monitoring/adaptive/fan-control engine and one
overlay telemetry owner. The first watched Steam game therefore remains the
owner until it exits; a second concurrently launched game still starts, but its
per-game runtime request is skipped and reported in the wrapper diagnostics.

## Compatibility

- The **GPU profile** (undervolt, clocks, power, adaptive tiers) is graphics-API
  agnostic: native Linux, every Proton version, and DirectX 8–12 all work.
- The **overlay and frame-pacing telemetry** are a Vulkan layer, so they cover
  anything presenting through Vulkan (DXVK for DX8–11, vkd3d-proton for DX12,
  native Vulkan), for both 64-bit and 32-bit games — a 32-bit title renders
  through wine's 32-bit Vulkan, so the layer ships a 32-bit build beside the
  64-bit one and the loader picks the match. Native OpenGL titles get the
  profile but no overlay, and adaptive mode simply holds its initial tier when
  there is no present-pacing signal to react to.

## Manual launch options

You do not have to use the tab — the wrapper works from any launch-options
string:

```text
PENGUIN_BURNER %command%
```

Add the overlay flag to show the readout immediately:

```text
PB_OVERLAY=1 PENGUIN_BURNER %command%
```

See the [overlay guide](features/overlay.md) and
[latency / frame-generation FPS](features/latency-fg.md) for the overlay's
sources and fallbacks.
