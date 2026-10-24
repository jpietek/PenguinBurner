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

## DLSS5 neural-rendering GPU time

Enable **DLSS5 NR GPU time** in the Overlay tab, then launch or restart the
wrapped game. The overlay displays **dlss5 nr 2.20 ms**, for example. The setting
is also available as `nr_gpu_ms` in the overlay configuration's `items` list.
Changing visibility applies live; starting capture requires a game restart.

This is a three-second average of GPU elapsed time per successful NR model
evaluation. It measures GPU commands recorded by the NR model, excluding
surrounding injector processing. It is neither additional input latency nor a
measurement of DLSS Super Resolution or Frame Generation. Existing frame-latency
telemetry remains separate.

Capture currently supports 64-bit Proton/Wine games using NVIDIA's NGX D3D12
driver dispatcher (`_nvngx.dll`, feature 18) and the version-2 NR forwarder
interface (`nvngx.dll_dlssnr.dll`) used by Retoque. Capture follows these shared
interfaces rather than detecting individual OptiScaler forks. Both adapters use
the same GPU collector and telemetry path; nested calls produce one sample.
Unknown forwarder ABI versions remain untouched. Direct model-DLL calls outside
these interfaces, Vulkan NR and separate helpers that do not load the PBurn shim
are not covered. Unsupported paths and readings older than three seconds are
shown as **dlss5 nr -**; they do not display a fabricated zero.
The enabled row stays visible when NR is off, keeping the overlay layout stable.

Capture can attach after D3D12 devices and queues already exist. The NGX adapter
must observe feature creation to identify NR. The NR-only forwarder can also
recognize an existing feature at its first evaluation. Preparation uses that
device to discover the runtime's shared queue dispatch table through a temporary,
idle queue, outside collector locks. This happens once per observed feature;
prepared devices reuse the collector. Capture does not hook device creation
during startup or create a queue on every evaluation.

The optional observer uses GPU timestamp queries and asynchronous fence polling.
It sends measurements through the existing NVAPI shim FIFO and session-scoped
daemon socket, alongside Reflex records. It does not read injector logs or wait
for GPU completion on the game thread. Each device has a bounded query pool;
exhausted or unsafe replay recordings are skipped. Concurrent producers are not
averaged together: the newest producer supplies the displayed reading.
