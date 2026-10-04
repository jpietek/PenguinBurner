# PenguinBurner 0.8.2

- **The overlay now reaches 32-bit games.** The published wheel ships the 32-bit Vulkan layer beside the 64-bit one, so Gorogoa and older GOG titles get the HUD, latency and Adaptive markers like everything else.

0.8.1 shipped the layer 64-bit only: it is built best effort, and the manylinux
image had no 32-bit toolchain, so the build warned and carried on. The release
now requires it and checks the wheel really contains it.

**Faugus Launcher is coming next.** Which launcher should we support after that?
Tell us what you use in
[Discussions](https://github.com/jpietek/PenguinBurner/discussions).

Everything in [0.8.1](release-notes-0.8.1.md) — the Heroic integration, the
compatibility picker and live per-game changes — is unchanged.
