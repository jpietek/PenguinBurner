# PenguinBurner Feature Guides

User documentation for each PenguinBurner feature, in priority order. See the
[project README](../../README.md) for the overview, install, and the LACT
comparison.

## Core

1. **[Automatic Tuning](./auto-uv.md)** — the managed headless Q2RTX + CUDA
   undervolt sweep that can stop/resume and final-verify a stable, efficient
   V/F curve.
2. **[Adaptive Undervolting](./adaptive-uv.md)** — switch between tiered profiles
   at runtime based on frame-rate pacing.
3. **[Performance Overlay](./overlay.md)** — in-game FPS, pre-frame-gen FPS,
   PC latency meter, clocks, power, and active tier.
4. **[Latency & Frame-Generation FPS](./latency-fg.md)** — how LAT, base FPS,
   output FPS, and frame-generation detection are measured.
5. **[Game Perf Profile](./game-perf-profile.md)** — the per-game telemetry fingerprint:
   badge and peek in the Steam tab, plus a MangoHUD-style frametime graph
   over the last 30 minutes of play.

## Secondary

- **[Profile Management](./profile-management.md)** — apply, verify, tier,
  export, and clean up saved curves.
- **[Curve Editors](./curve-editor.md)** — manual V/F and fan curve editing.
- **[Silent Fan Curve](./silent-fan-curve.md)** — auto-generated quiet fan curve.

## Help

- **[Troubleshooting & FAQ](./troubleshooting.md)**

## Roadmap (planned, not shipped)

- Power limit control (GPU board power cap)
- Historical data plotting (power / clocks / FPS over time)
- Steam library discovery
- Per-game tuning profiles
