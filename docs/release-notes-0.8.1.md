# PenguinBurner 0.8.1

Thanks to [@Ernold11](https://github.com/Ernold11) for the **Heroic** integration
you asked for in 0.8.0, and to [@a5ehren](https://github.com/a5ehren) for the
static-analysis cleanup and daemon setup fixes.

**Heroic is done and on par with Steam and Lutris** — same per-game profiles,
with real-time Adaptive FPS target changes and Proton compatibility tool
selection.

**Faugus Launcher is coming next.** Which launcher should we support after
that? Tell us what you use in
[Discussions](https://github.com/jpietek/PenguinBurner/discussions).

- **Heroic Games Launcher** joins Game Library as a first-class launcher — Epic, GOG, Amazon and sideloaded games, native or Flatpak.
- **Compatibility tool picker** chooses the Wine/Proton build per game in Steam, Heroic and native Lutris.
- **Live changes** apply profile mode, Adaptive FPS target and overlay visibility to a running wrapped Steam, Lutris or Heroic game.
- **Bulk overlay controls** turn the HUD on or off for every enabled game across all launchers at once.
- **Event-based session tracking** replaces polling and startup timeouts, so a game is never wrongly reported as started without PenguinBurner.
- **Steadier Adaptive** ignores the frame-time spikes from alt-tabbing or switching desktops instead of jumping to Performance.
- **Recently installed** sorting orders the library by when each game was installed.

Other fixes:

- Silent fan curve survives a per-game profile change.
- Steam live connection works on both IPv4 and IPv6.
- An edited profile name follows the point it runs at.
- Flatpak launchers get a sandbox-local PenguinBurner runtime, Vulkan layers and NVAPI shim.
- Existing launch options, Lutris command prefixes and Heroic wrapper rows are preserved.
- Packaging and quality gates now cover CachyOS mirror failures and the overlay loader.
