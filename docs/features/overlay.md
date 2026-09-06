# Performance Overlay

> Feature guide — see the [README](../../README.md) for the project overview.

The overlay shows live FPS, latency, GPU telemetry, and the active profile
while you play.

![Overlay configuration tab](../assets/overlay.png)

## Enabling the overlay

In [Game Library](game-library.md), enable **Wrap this game** and **Overlay**.
Use the Overlay tab to choose fields and appearance. For manual launch options,
see [Steam](../steam.md#manual-launch-options) or [Lutris](lutris.md).

Top-level controls:

- **Enable overlay** — master toggle.
- **Update interval** — how often the readout refreshes (e.g. `1 s`).
- **Overlay scale** — size multiplier (`1x`, etc.).
- **Target FPS** — the reference frame rate, shared with
  [Adaptive Undervolting](./adaptive-uv.md).

The **Preview** line shows exactly what the on-screen string will look like, for
example:

```text
119 FPS  176 FG  LAT 23 ms  225 MHz  8 W  PERF  GPU 0%  CPU-T 98%
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

## PC latency meter

The **LAT** field shows PC latency in milliseconds: the render tail plus the
present-to-scanout display tail when that tail is supported. Where the display
tail isn't available, it shows render latency alone. It turns on with the
overlay, sourced from the NVAPI shim on Proton games and the native Vulkan layer
otherwise.

- Set `PENGUIN_BURNER_LATENCY_DISPLAY=0` to show render latency only.
- Set `PB_INGAME_LATENCY=0` to turn the latency field off while keeping the
  overlay.

Without usable latency markers, the overlay omits `LAT` and keeps showing FPS
and GPU telemetry. See [latency and FPS details](latency-fg.md).
