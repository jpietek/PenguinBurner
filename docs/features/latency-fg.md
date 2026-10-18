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

### Lutris, Heroic, Faugus and other Wine launchers

In Lutris, open **Game → Configure → System options** and set
**Command prefix** to:

```text
PENGUIN_BURNER --pb-overlay=1
```

In Heroic, the equivalent field is **Settings → Advanced → Wrapper commands**.
In Faugus, use **Launch arguments**, or enable wrapping in
[Game Library](faugus.md).

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

PenguinBurner cannot force an engine to emit real input or simulation markers.
When they are absent, it can estimate a frame-cycle span using Vulkan GPU
completion timestamps. The overlay marks that fallback with `~`; it does not
claim to measure engine input or physical mouse-to-screen latency.

## Latency Sources

### Driver completion (preferred)

When valid `vkGetLatencyTimingsNV` driver reports are available, `LAT AVG` shows the
arithmetic mean frame latency over the last three seconds ending at GPU
completion. Endpoints come from the same report: input sample to GPU completion,
simulation start to GPU completion, or render submission to GPU completion,
in that order of preference. This includes the GPU queue and rendering work
that can continue after a CPU
`Present()` call returns.

When the input-sample marker is missing, the simulation/submission span is
prefixed with `~`: it is an estimate of responsiveness, not a measured input
latency. The published `latency_source` identifies the selected endpoints.

These driver-reported spans are experimental under Proton and frame generation.
Timestamp ordering checks do not establish that the reported GPU completion is
the completion of the image the player saw. This is not full mouse-to-screen
latency: input sampling waits, presentation, and physical display response are
not fully covered. Neither the average nor p95 proves perceived input delay.

### NVAPI Shim (default, Wine and Proton games)

The marker fallback uses the drop-in NVAPI shim. It taps the Reflex markers above
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

The daemon computes the average and p95 from the same selected sample stream.
It publishes the average as `latency_avg_ms`, rendered as `LAT AVG`. The legacy
`latency_ms` field retains p95 for diagnostics and older readers. If the daemon
does not publish the average, the overlay labels its fallback `LAT P95`.
Repeated driver frame reports are counted only once.

### Marker estimates

If valid driver-completion timestamps are unavailable, the daemon uses Reflex
input/simulation-to-present markers, preferring the out-of-band present span
when frame-generation markers are present. The overlay prefixes this shorter
estimate with `~`, for example `LAT AVG ~25 ms`. An older daemon that does not
publish source information is also shown as an estimate.

Application present and frame-generation present markers are software events;
they do not establish when the corresponding image reached the monitor. A low
marker estimate can coexist with a long GPU queue. Missing markers are not
replaced with an invented latency derived from FPS.

### No-marker frame-cycle estimate

With no usable marker spans, `frame-cycle-to-gpu` pairs the previous
`vkQueuePresentKHR` entry with GPU completion of the current presented frame.
The GPU timestamp is written after the overlay draws, behind the application's
render-finished semaphore waits. `VK_KHR_calibrated_timestamps` or its EXT alias
maps it to the CPU monotonic clock. Reading the query when its image is reused
adds no readback delay to the measured duration and never waits on the GPU.

This is a frame-cycle proxy plus the remaining GPU queue/render work, not
`1000 / FPS`. It works independently of which DLSS injector is loaded. It
requires a drawable overlay and an enabled calibration extension with a
monotonic CPU time domain. Invalid calibration, failed presents, long pauses,
and stale samples are discarded. Reflex sources always take precedence.

CPU work before that frame-cycle boundary is not measured. Asynchronous
presentation or frame generation can separate a displayed frame from the
engine's input-bearing frame, so this estimate can understate or overstate
input latency. The overlay's small drawing cost is included. Display scanout
and pixel response are excluded.

### Presentation wait

`display_latency_p95_ms` remains a separate diagnostic for the
`VK_KHR_present_wait` completion wait. It is not added to `LAT AVG`: the independent
measurements are not paired by frame, may overlap the measured render span, and
do not establish physical pixel-response time.

Set `PENGUIN_BURNER_LATENCY_DISPLAY=0` to disable that diagnostic capture.

## Why LAT Can Be Missing

Without Reflex, the estimate remains unavailable if the game/runtime did not
enable calibrated timestamps, the timestamp clock cannot be calibrated reliably,
or the overlay cannot draw on the presenting queue/swapchain. An old running
game must be restarted to load the updated native layer. Unsupported and stale
readings are hidden; pacing alone is not reported as measured PC latency.

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

Adaptive uses the same slowdown protection for base-frame markers and ordinary
present pacing. Before immediately jumping to Performance, it checks that the
median frame time also exceeds the target and at least half the measured frames
miss the badly-slow deadline. A short slow tail, such as during a desktop switch,
instead goes through the normal sustained-slowdown confirmation. Returning to a
lower tier still requires sustained headroom.

Generated output is not a base-frame sample set. When only an inferred base FPS
is available, the controller keeps its existing fallback; it does not use the
faster displayed-frame median or miss ratio to veto a needed promotion.

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
