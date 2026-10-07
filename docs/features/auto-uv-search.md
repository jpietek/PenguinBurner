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
measured-clock checks also pass. It starts from Balanced's final verified
curve, including any clock reclaim or safer fallback selected during final
verification, and keeps the passed descent points for recovery.

## Voltage and clock search

Voltage descends through a finite set of editable bins while keeping the
previous passing candidate's requested clock. Higher or lower measured clocks
do not change the next target: rising-tail boost is not added again at each
step, and a power-limited shortfall is not repeatedly subtracted. The configured
rising tail and its clock headroom are preserved. Measured clocks remain available
for evaluation; clock increases are tested separately at a fixed voltage.

Each tier's table clock is the upper limit of its search. When the stock
curve loads above that clock under the tier's power budget (an RTX 3080 at
380 W runs about 1920 MHz against an 1885 MHz Balanced target), the loaded
baseline locks at the highest 15 MHz step at or below the table clock and the
voltage descent runs there; the measured stock clock stays the comparison
reference. A power-bound baseline below the table clock is left where the
card runs and can climb later. An edited clock target replaces the table clock
as this limit.

Efficiency selects the highest measured FPS/W among passing candidates,
including candidates before any clock climb. It compares unrounded values;
equal FPS/W favors higher measured clock, then lower power. Balanced uses the
performance-and-efficiency selection policy; Performance adds the Auto-OC
ladder.

All reference target clocks use 5 MHz increments. Selected RTX 30-series
Efficiency/Balanced targets include small fixed reductions, and Performance
retains its roughly 1% reduction. RTX 3080 10GB uses 800 mV / 1750 MHz,
875 mV / 1885 MHz, and 900 mV / 1930 MHz. RTX 3060 uses 800 mV / 1555 MHz,
850 mV / 1680 MHz, and 900 mV / 1780 MHz. These guide the search; the measured
baseline and stability checks determine the saved result. A 5 MHz target
change does not necessarily change the clock bin held by the GPU.

If the proven Performance starting voltage exceeds its default table target,
Auto-OC can still increase clocks at that same voltage up to the existing
Performance clock target. An edited voltage target keeps its requested
bound; the scan dialog sends only edited targets, so an unchanged table
default never becomes that bound. Balanced and Performance retain their shared power budget and can
still converge if no higher stable measured clock is found.

A cached unsafe voltage/clock band is skipped before touching the GPU. Auto-OC
can test the same clock at a higher editable voltage outside that band, within
the voltage target. After a successful voltage retry, remaining clock steps
continue from at least that voltage, even if the retry already reached the
voltage target. Reusing Balanced therefore does not cap Performance at an early
clock step. A new unsafe probe or a measured power wall still ends the climb;
reaching the configured clock target depends on the actual probe results.

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

Q2RTX and CUDA check stability, load, and FPS. The CUDA companion runs an
integer stress twice per launch and compares the copies on the GPU, ramping
from an eighth of the grid to the full grid so a marginal voltage shows up as
a wrong result before full current can hang the card. There is no
measured-clock-loss percentage cutoff. A deliberately lower custom clock uses its passing
lower-clock measurement for the final FPS check.

Default final durations are 60 seconds for Efficiency, 180 for Balanced, and
300 for Performance. An explicit duration overrides the defaults. Final
verification keeps the selected curve, memory offset, and power limit intact.
A failed final check can retry the next safer tested curve.

The managed [headless Q2RTX benchmark](https://github.com/jpietek/Q2RTX-headless)
needs no display server. Automatic resolution is 3840×2160 (4K) on desktop
RTX 4080, 4080 Super, 4090, 5070 Ti, 5080, and 5090, plus RTX 4090 Laptop and
5090 Laptop. Every other GPU uses 1920×1080, including RTX 3080/3090,
RTX 5080 Laptop, RTX 5070 Ti Laptop, other laptop models, unrecognized models,
and unavailable GPU identification. Selection uses the GPU model, not VRAM size.
This keeps the heavier instability workload on the supported high-end cards
without letting 4K establish an unnecessarily low scan baseline on other GPUs.

For a manual CLI comparison, select `auto`, `1080p`, `1440p`, or `4k` with
`--auto-uv-q2rtx-resolution`. For example:

```bash
penguin-burner-cli --auto-uv-voltage-scan --gpu-index 0 \
  --auto-uv-mode performance --auto-uv-q2rtx-resolution 1080p
```

The selected resolution applies to the baseline, every scanned tier and final
Q2RTX verification; CUDA is unchanged. New profiles record the resolved Q2RTX
resolution and reuse it for later verification. Older profiles retain automatic
resolution selection. The GUI keeps its automatic default. Compare FPS and
FPS/W only between runs at the same resolution; this override does not establish
that a lower resolution improves tuning on a particular GPU.


`hw-power-brake` reports the board's power-delivery protection. It is recorded
in the Cap column, logs, and scan result separately from the configured power
limit. Repeated events indicate a delivery limit at that operating point.

For the complete flow and hardware evidence, see the
[illustrated cookbook](https://jpietek.github.io/PenguinBurner/auto-uv-cookbook/).
