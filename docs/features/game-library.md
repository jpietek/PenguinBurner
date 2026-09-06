# Game Library

Manage **Steam and Lutris** games in one list. Choose a GPU profile, Adaptive
FPS target, and overlay settings for each game, then launch it here or through
its usual launcher.

![Steam and Lutris games in Game Library](../assets/game-library.png)

## Setup

1. Open **Game Library** and let it discover installed games.
2. Select a game and enable **Wrap this game**.
3. Choose **Adaptive**, **Efficiency**, **Balanced**, **Performance**, or **Stock**.
4. Use **Play** to start the game; **Stop** appears while it runs.

Steam may require a one-time library scan or restart before edits are available.
See [Steam setup](../steam.md) or [Lutris setup](lutris.md).
Discovery reads the library; wrapping a game changes its launch command.

## Per-game settings

| Setting | Effect |
| --- | --- |
| Wrap this game | Add the PenguinBurner wrapper; disabling it restores the original launch command. |
| Graphics card | Choose the NVIDIA GPU to tune; shown only on multi-GPU systems. |
| Auto-UV mode | Switch tiers with Adaptive, pin one tier, or use stock settings. |
| Per-game target | Override the system-wide Adaptive FPS target; off follows that target. |
| Overlay | Show the in-game HUD. Adaptive keeps required timing markers active when the HUD is hidden. |
| Command | View or edit the launch command or Lutris prefix. |

Existing launch options and command prefixes are preserved. Lutris changes
apply on the next launch. Steam supports live mode, target, and overlay updates
for a running wrapped game; changing its GPU requires a relaunch.

## Library controls

Sort by launcher, name, recently played, or most played. **Rescan** refreshes
discovery without clearing settings. Scans and bulk writes run in the background.

The **All games** menu enables or disables wrapping, or shows or hides overlays
for enabled games. Bulk enable keeps the overlay off and preserves saved modes.
Each action shows the affected count before confirmation.

## Runtime behavior

The wrapper asks `penguin-burnerd` to apply the game profile, then restores your
standing profile when the game exits. If the daemon is unavailable, the game
still launches. Only one game can own the daemon's active monitoring and
Adaptive engine at a time; concurrent games do not replace that owner.

Fixed profiles work independently of the game's graphics API. Overlay and
Adaptive frame telemetry require Vulkan, including DXVK and vkd3d-proton.
Known native OpenGL games have those controls disabled; fixed profiles and
launching remain available.

See [Adaptive](adaptive-uv.md), [overlay controls](overlay.md), and
[latency and frame-generation FPS](latency-fg.md) for measurement details.
