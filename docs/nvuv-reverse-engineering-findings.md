# NV-UV Reverse Engineering Findings

This note records what we learned from the local NV-UV binaries and which parts
are safe to borrow for Penguin Burner.

## What We Should Reverse

Reverse or leave reverted:

- Adaptive V-droop compensation in the scan loop. NV-UV mutates a direct-mode
  compensation value from measured loaded voltage and clamps it to 0-25 mV.
  That is coupled to its private NVAPI direct writer and should not be copied
  into our normal core logic yet.
- NVAPI direct strict-lock curve writing. NV-UV writes private VF control tables,
  folds points above the lock point to a low penalty clock, and retries batch
  writes. This is not a portable runtime behavior for us.
- NV-UV's broader downward optimize search. It probes down in 5 mV steps to
  `max(700, originalVoltageMV - 100)`, which is a separate policy from our
  current scanner and was the kind of core algorithm change that made results
  worse.
- Any recovery above the borrowed Performance preset voltage. In performance
  mode, the recovery ladder may seek FPS only up to the table's Performance
  voltage for that GPU, not the Max preset.

Keep:

- The borrowed GPU voltage/frequency preset table.
- The UI sorting fixes: Performance sorts by FPS, Efficiency sorts by FPS/W,
  with relative FPS and FPS/W deltas shown to the user.
- The max-voltage-drop auto-fill from the borrowed Eco voltage floor.
- The modal note as one short line ending with the GPU name.
- The performance-mode recovery voltage ceiling from the borrowed Performance
  preset voltage.

## Borrowed Table Policy

Use the table as bounds, not as a replacement scan algorithm.

- Eco preset voltage: lower sweep boundary for the automatic max voltage drop.
- Performance preset voltage: upper voltage recovery ceiling in performance
  mode.
- Max preset voltage: informational only for now; do not use it for automatic
  recovery.

For RTX 5080 the table says:

| Tier | Voltage | Clock |
| --- | ---: | ---: |
| Eco | 850 mV | 2800 MHz |
| Balanced | 900 mV | 2800 MHz |
| Performance | 925 mV | 2980 MHz |
| Max | 975 mV | 3150 MHz |

So RTX 5080 performance-mode voltage recovery must stop at 925 mV.

## Modal Copy Rule

Before starting Auto OC, the scan tuning modal may show only a short note:

```text
Max voltage drop auto-filled for NVIDIA GeForce RTX 5080
```

Do not mention "efficiency floor" in the modal. The technical reason can live in
the tooltip or docs, not in the visible note.

## Evidence From NV-UV

The local `NV-UV.exe` is a .NET single-file bundle. The bundle contained
`NVUV.Core.dll`, `NV-UV.dll`, `NV-UV.r2r.dll`, dependency metadata, and runtime
config. The core service type is `NVUV.Core.Services.AutoUVService`.

Relevant managed behavior found in `NVUV.Core.dll`:

- `TryEnterDirectMode` uses NVAPI direct mode when it can read the stock VF
  curve; otherwise it falls back to the Afterburner path.
- `ApplyViaNvapi(freqMHz, voltageMV, applyComp)` chooses a VF point within
  about 15 mV of the requested voltage plus any active V-droop compensation.
  It writes a strict lock at that point and pushes higher points to a penalty
  clock.
- `AdaptVDroopCompFromSamples(requestedMv, samples)` computes loaded median
  voltage and adjusts compensation by the difference, clamped to 0-25 mV.
- Load-qualified voltage and clock sampling ignores the first 5 seconds, keeps
  samples at or above 60% of max observed power, requires at least 5 qualified
  samples, and uses medians.
- `RunVoltageSearchInline` first tries a pre-resolved mapping if present, then
  probes downward in 5 mV steps until instability, crash, cancellation, or the
  voltage floor.

Relevant native bridge behavior found in `NvApiNative.dll`:

- Exports include `NvApiDirect_Init`, `ReadCurve`, `ReadOffsets`, `SetPoint`,
  `SetBatch`, `ResetAll`, and `GetLastError`.
- `SetBatch` writes active point offsets through a private NVAPI VF control call,
  verifies offsets, retries, and falls back to per-point writes when needed.
- The native code clamps offsets to +/-1,000,000 kHz before writing.

## Practical Decision

For Penguin Burner, the useful part is the preset table as a conservative guard
rail:

- Auto-fill max voltage drop from the Eco floor.
- In performance mode, recover voltage upward only through the Performance
  voltage ceiling for the detected GPU.
- Keep the existing scanner's stability rules and candidate selection policy.

The NV-UV direct-mode and adaptive V-droop ideas are worth keeping as research
notes, but they should stay out of production until we can test them as an
explicit backend with separate safety gates.
