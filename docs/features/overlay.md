# Performance Overlay

> Feature guide — see the [README](../../README.md) for the project overview.

The overlay shows live FPS, latency, GPU telemetry, and the active profile
while you play.

![Overlay configuration tab](../assets/overlay.png)

## Enabling the overlay

In [Game Library](game-library.md), enable **Wrap this game** and **Overlay**.
Use the Overlay tab to choose fields and appearance. For manual launch options,
see [Steam](../steam.md#manual-launch-options), [Lutris](lutris.md),
[Heroic](heroic.md) or [Faugus](faugus.md).

Top-level controls:

- **Enable overlay** — master toggle.
- **Update interval** — how often the readout refreshes (e.g. `1 s`).
- **Overlay scale** — size multiplier (`1x`, etc.).
- **Target FPS** — the reference frame rate, shared with
  [Adaptive Undervolting](./adaptive-uv.md).

The **Preview** line shows exactly what the on-screen string will look like, for
example:

```text
119 FPS  176 FG  LAT AVG ~23 ms  225 MHz  8 W  PERF  GPU 0%  CPU-T 98%
```

## Fields

Toggle each item independently. They are grouped into **Basic** and **Advanced**:

| Basic | Advanced |
| --- | --- |
| Base FPS | GPU % |
| FG FPS (frame-generation) | CPU % |
| Latency ms | CPU-T (peak thread) |
| Clock MHz | Fan % |
| Voltage mV | Temp C |
| Power W | UV offset mV |
| Profile (tier: EFF/BAL/PERF) |  |

## Pre-frame-generation FPS

The overlay shows two frame-rate numbers:

- **Base FPS** — the rendered present rate, before frame generation.
- **FG FPS** — the rate with frame generation, shown only when frame
  generation is active.

Having both makes it obvious how much of the displayed rate comes from generated
frames versus rendered ones.

See [Latency and frame-generation FPS](./latency-fg.md) for the exact p95,
raw-output, and frame-generation detection rules.

## Frame latency estimate

Enable **Frame latency ms** in **In-Game Overlay → Frame rate**. The row shows
`LAT AVG` (`lat avg` in HUD), or `LAT P95` with an older daemon. Reflex input to
GPU completion is preferred. A `~` prefix identifies estimated latency.

Without usable Reflex markers, the Vulkan layer pairs the previous Present
entry with the current frame's calibrated GPU completion. This frame-cycle
estimate includes GPU queueing, but its start is a proxy for engine/input work.
It is not measured mouse-to-screen latency. It uses the existing overlay submit
and telemetry socket; no injector-specific configuration is needed.

The fallback requires the game/runtime to enable calibrated Vulkan timestamps
and an active drawable overlay. It reads completed queries without waiting on
the GPU. Unsupported or stale measurements remain hidden. `PB_INGAME_LATENCY=0`
disables capture; `PENGUIN_BURNER_LATENCY_DISPLAY=0` disables only the independent
presentation-wait diagnostic, which is never added to the frame-latency value.
See [latency measurement](latency-fg.md) for endpoint and frame-generation limits.
