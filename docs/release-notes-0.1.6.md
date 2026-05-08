# PenguinBurner 0.1.6 Release Notes

## GitHub Release Notes

PenguinBurner 0.1.6 focuses on Auto-UV3 guardrails, better final candidate
selection, and safer performance-mode voltage recovery.

### Highlights

- Added a borrowed GPU voltage/frequency table for RTX 50 and RTX 40 families.
- Auto-filled max voltage drop now uses the detected GPU's Eco table voltage as
  the lower sweep boundary, with a generic 15% fallback for unsupported GPUs.
- Performance-mode voltage recovery now uses the GPU's Performance table voltage
  as the ceiling. For RTX 5080, recovery can seek FPS only up to 925 mV.
- Final candidate choice now follows the selected mode: Efficiency sorts by
  FPS/W, Performance sorts by FPS.
- The final candidate modal shows relative FPS and FPS/W deltas against the
  baseline.
- If a user stops Auto-UV after stable candidates exist, the UI now offers those
  candidates for final verification instead of only marking the run stopped.
- The Auto-UV tuning modal keeps the max-voltage-drop note short and
  user-facing, ending with the detected GPU name.

### Auto-UV Behavior

- The borrowed voltage/frequency table is used as a guardrail, not as a forced
  clock target.
- Performance and YOLO clock recovery still use measured baseline and
  lower-voltage clocks; YOLO mode can use up to 175% clock recovery budget.
- Performance-mode upward voltage recovery is capped by the table Performance
  voltage and no longer climbs toward the table Max voltage.
- Final upward stabilization after a failed long verification uses the same
  performance voltage ceiling.
- NV-UV reverse-engineering findings are documented separately, with direct-mode
  V-droop compensation kept out of production core logic.

### Packaging And Local Testing

- Package metadata is prepared for version 0.1.6.
- Local wheel and source distributions should pass `twine check`.
- Ubuntu PPA upload is intentionally left for a host with the Launchpad signing
  key.
- COPR upload requires local COPR credentials and `copr-cli`.

## PyPI Release Summary

PenguinBurner 0.1.6 adds Auto-UV3 GPU table guardrails, RTX 5080 performance
voltage recovery capped at 925 mV, mode-correct final candidate sorting, stopped
scan candidate selection, and clearer Auto-UV tuning modal text.
