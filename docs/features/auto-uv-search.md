# Auto-UV Search and Verification

See [Auto-UV](auto-uv.md) for setup and [recovery](auto-uv-recovery.md) for
failed probes and interrupted scans.

## Baselines and power limits

Each tier applies and reads back its power limit before its stock baseline,
voltage sweep, and final verification. A missing or mismatched power-control
result stops further probing. Only an explicit unsupported-driver result
permits a platform-managed fixed power limit.

All tiers starts with Efficiency's baseline. Balanced and Performance can
share a baseline when power limit, memory offset, and tail settings match.
For example, a 300 W / 360 W / 360 W run needs baseline pairs at 300 W and 360 W.
Performance reuses Balanced's descent only when the remaining baseline and
measured-clock checks also pass.

## Voltage and clock search

Voltage descends through a finite set of editable bins. Passing probes keep
the requested clock even when the measured clock falls under a power limit;
that shortfall is not repeatedly subtracted from the next target. Higher
measured clocks can raise the target.

Efficiency selects the highest measured FPS/W among passing candidates,
including candidates before any clock climb. It compares unrounded values;
equal FPS/W favors higher measured clock, then lower power. Its table clock
is an upper search limit. Balanced uses the performance-and-efficiency
selection policy; Performance adds the Auto-OC ladder.

Efficiency and Balanced can reclaim clock at the already-proven voltage on a
power-limited baseline. Custom lower clocks are tested after voltage descent,
at its stable voltage. Searches have bounded steps and retain passing
candidates when further probing cannot improve the result.

## Curve shape and measured voltage

Every candidate has a gradual ramp into its selected voltage/clock anchor and
a two-bin rising tail (+30 MHz nominal). The tail is present during testing.
The saved profile retains the complete tested curve; selection does not
rebuild it or splice lower-voltage points from other candidates.

Because the tail offers boost headroom, the anchor is **not a voltage lock**.
Requested and measured voltage can differ. The size of that difference depends
on the card's curve bins and operating conditions; a bin count alone does not
establish an mV difference.

If descent cannot improve the passing baseline, Auto-UV retains that exact
baseline, including its target label and curve.

## Verification

Q2RTX and CUDA check stability, load, and FPS. There is no measured-clock-loss
percentage cutoff. A deliberately lower custom clock uses its passing
lower-clock measurement for the final FPS check.

Default final durations are 60 seconds for Efficiency, 180 for Balanced, and
300 for Performance. An explicit duration overrides the defaults. Final
verification keeps the selected curve, memory offset, and power limit intact.
A failed final check can retry the next safer tested curve.

The managed [headless Q2RTX benchmark](https://github.com/jpietek/Q2RTX-headless)
needs no display server. Resolution is 2560×1440 for GPUs with at most 8 GiB of
VRAM, otherwise 3840×2160, including when VRAM is unavailable.

`hw-power-brake` reports the board's power-delivery protection. It is recorded
in the Cap column, logs, and scan result separately from the configured power
limit. Repeated events indicate a delivery limit at that operating point.

For the complete flow and hardware evidence, see the
[illustrated cookbook](https://jpietek.github.io/PenguinBurner/auto-uv-cookbook/).
