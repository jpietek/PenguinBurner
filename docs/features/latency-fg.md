# Latency And Frame-Generation FPS

> Feature guide - see the [Performance Overlay](./overlay.md) guide for the
> overlay controls.

PenguinBurner can show a latency number and a pre-frame-generation FPS number
when the game exposes enough timing information. These fields are optional
signals, not normal GPU counters. Clocks, voltage, power, temperature, and GPU
load come from GPU telemetry. Latency and base FPS come from the game/render
path.

## Launch Paths

### Steam

Add this to the game's launch options:

```text
PB_OVERLAY=1 PENGUIN_BURNER %command%
```

### Lutris and other Wine launchers

In Lutris, open **Game → Configure → System options** and set
**Command prefix** to:

```text
PENGUIN_BURNER --pb-overlay=1
```

The `PENGUIN_BURNER` command must be installed and available on the launcher's
`PATH`. This path also works for other launchers that expose the active prefix
through `WINEPREFIX`; it does not depend on Steam game identity.

In-game latency turns on with the overlay — no extra flag. The NVAPI shim is
deployed into the Wine or Proton prefix automatically and streams Reflex markers
to the wrapper's FIFO; native and prefix-less games fall back to the Vulkan
layer's own marker tap. Opt out with `PB_INGAME_LATENCY=0`.

#### Markers without the overlay

For launcher-managed games, selecting **Adaptive** keeps marker capture enabled
automatically even when the overlay is off. The overlay switch controls the HUD;
it does not take Adaptive's base-frame pacing signal away. Fixed tiers and Stock
do not capture hidden markers when their overlay is off.

For a wrapper command managed by hand, request the same headless marker mode
explicitly:

```text
env PB_INGAME_LATENCY=1 PENGUIN_BURNER --pb-overlay=0
```

The leading `env` is required. Lutris execs the command prefix directly rather
than through a shell, so a bare `PB_INGAME_LATENCY=1` would be treated as the
program to run instead of an assignment.

## What PenguinBurner Can Force

PenguinBurner can load its Vulkan layer, expose the Proton/NVAPI environment,
capture present pacing, and parse marker records when they exist.

PenguinBurner cannot force a game engine to emit real latency markers. The game
has to call the relevant APIs at meaningful points: input sample, simulation,
render submit, present, and, for frame generation, out-of-band present. If a
title never emits those markers, PenguinBurner should omit `LAT` instead of
inventing a fake PC-latency value.

That is why one game can show `LAT` while another game in the same Steam library
does not. Games such as FF7 Rebirth or 007 can expose usable Reflex/NVAPI marker
data through Proton, while another title can expose only present pacing or no
marker stream at all.

## Latency Sources

### NVAPI Shim (default, Wine and Proton games)

The default source is the drop-in NVAPI shim. It taps the Reflex markers above
vkd3d's owner-gate — so it still works under frame generation — and streams them
to the wrapper's FIFO. The bridge pairs markers by frame ID and emits
`marker-proxy` timing samples; no dxvk-nvapi fork, trace, or marker log is
involved.

### Native Vulkan Markers (fallback)

Games without a Wine or Proton prefix (and the single-swapchain / non-FG case)
fall back to PenguinBurner's native Vulkan implicit layer, which observes
`vkSetLatencyMarkerNV` / `vkGetLatencyTimingsNV` inside the game process and
publishes samples such as:

- `input_to_present_us`
- `sim_to_present_us`
- `sim_to_oob_present_us`
- `submit_to_present_us`
- `render_submit_us`

Either way the daemon aggregates recent samples and publishes `latency_p95_ms`,
which the overlay renders as `LAT`.

### Display Tail

The render latency and display tail are kept separate internally.

- `latency_p95_ms` is the selected render/game latency proxy.
- `display_latency_p95_ms` is the present-to-scanout tail when
  `VK_KHR_present_wait` and `VK_KHR_present_id` are available.

The overlay sums those two values into the single `LAT` number. If the display
tail is unavailable, `LAT` falls back to render/game latency alone.

Set this to hide the display tail:

```text
PENGUIN_BURNER_LATENCY_DISPLAY=0
```

## Latency Quality

PenguinBurner uses the best usable p95 latency tier available in the current
sample window. From most complete to weaker proxy, the important tiers are:

- `input-to-oob-present` - input marker to displayed present, including the
  frame-generation hold.
- `sim-to-oob-present` - simulation start to displayed present, including the
  frame-generation hold.
- `input-to-present` / `marker-input-to-present` - input marker to application
  present.
- `sim-to-present` - simulation start to application present.
- `submit-to-present` - render submit to present.
- `render-submit` - render-submit marker span only.

If frame generation is active, PenguinBurner prefers the out-of-band present
span when available because application present can happen before generated
frames are paced to display.

## Why LAT Can Be Missing

Missing `LAT` is expected for some games. Common causes:

- The game does not implement Reflex/NVAPI/Vulkan latency markers.
- The game implements them on Windows but not on the Proton path being used.
- The game exposes no Reflex/NVAPI markers for the shim to tap and no native
  Vulkan low-latency calls for the layer to read.
- The game emits only partial markers, for example render-submit without input
  sample or present markers.
- `VK_NV_low_latency2` or `vkGetLatencyTimingsNV` is unavailable or returns an
  empty/stale timing ring.
- Display-tail timing is unavailable because `VK_KHR_present_wait` or
  `VK_KHR_present_id` is missing.
- The game was already running when the native layer was rebuilt, so the old
  `.so` is still mapped in that process.

When this happens, PenguinBurner still shows the normal overlay fields and can
still show FPS/present pacing. It should not label present pacing as real PC
latency.

## FPS Fields

PenguinBurner tracks two different frame-rate concepts.

### Raw Output FPS

The native layer observes `vkQueuePresentKHR` and records the time between
presents. This is display/output cadence. With frame generation active, this
often includes generated frames.

The daemon tracks the arithmetic mean output FPS over the recent present
intervals (the raw present average).

The overlay's `FG` value is based on that raw present average. It is shown only
when PenguinBurner has enough evidence that frame generation is active.

### Base / Pre-FG FPS

The overlay's main `FPS` number is `present_fps`. This is intended to be the
rendered/base cadence before frame generation.

PenguinBurner gets it in this order:

1. Prefer base-frame marker pacing from the game marker stream.
2. Use marker families in this priority order: `oob-present-start`,
   `present-start`, then `rendersubmit-start`.
3. Reject impossible marker streams whose base-marker FPS is faster than raw
   present cadence by more than the guard threshold.
4. If marker pacing is unavailable, use present-pacing p95.
5. Only deinterlace a multiplied output cadence when there is a marker stream
   and a previous base cadence to anchor the split.

`present_fps` is p95-based. It is not the arithmetic mean. PenguinBurner takes
the p95 frametime from the current window and converts it to FPS, so the number
leans toward the slow-tail rendered cadence instead of a spiky average.

This distinction matters:

- `present-fps=40` means PenguinBurner's selected base/pre-FG cadence is about
  40 FPS from p95 frametime.
- `raw-present-fps-avg=120` means the output present stream averaged about
  120 FPS, which can include generated frames.

## Frame-Generation Detection

PenguinBurner is conservative about showing `FG`.

Out-of-band present markers alone are not enough, because Reflex can emit them
even with frame generation off. The overlay reports frame generation when the
output cadence clearly exceeds the base cadence, or when an explicit frame-gen
sample says it is active.

The current cadence rule requires output FPS to be at least 1.5x the selected
base FPS before `FG` is shown.

Examples:

```text
40 FPS  120 FG
```

The base/pre-FG cadence is about 40 FPS and output cadence is about 120 FPS.

```text
58 FPS
```

The output cadence is not clearly frame-generated, or PenguinBurner does not
have enough evidence to split base and output rates.

## Diagnostics

The live overlay state is written under:

```text
/run/user/<uid>/penguin-burner/overlay-state.txt
```

## About low_latency_layer

[low_latency_layer](https://github.com/Korthos-Software/low_latency_layer) is a
useful design reference for reducing latency. It implements Vulkan-side Reflex
and Anti-Lag style behavior, tracks queue submissions by present ID, waits for
work completion, and signals Reflex sleep semaphores. That can reduce queue
depth in games that already call the latency APIs.

It does not solve PenguinBurner's missing-`LAT` problem by itself. In its Reflex
mode it is primarily a latency-control layer, not a telemetry layer: it does not
produce the input/simulation/present marker timings PenguinBurner needs for the
overlay meter. It can help a game run with lower latency, but it cannot make a
game emit real input or simulation markers if the game never calls them.

Do not stack extra implicit layers blindly. Multiple layers that intercept
`VK_NV_low_latency2`, queue submit, and present calls can affect ordering,
timing, or compatibility. Treat this as a separate experiment from the
PenguinBurner `LAT` meter.
