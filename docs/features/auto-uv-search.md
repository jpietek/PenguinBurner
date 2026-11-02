# Auto-UV Search and Verification

See [Auto-UV](auto-uv.md) for setup and [recovery](auto-uv-recovery.md) for
failed probes and interrupted scans.

## V/F curve compatibility

On drivers that omit the optional V/F base tuple, including affected Turing
GPUs, missing base frequency or voltage values fall back to the reported curve
point. Populated base values are preserved. Auto-UV still rejects invalid
reported points and requires enough plausible points before scanning.

## Boost shape and freeze retries

The GUI always uses **Standard Boost**. On the CLI, choose the rising tail
independently of Efficiency, Balanced, Performance, or a full adaptive scan:

| CLI option | Rising tail | Nominal headroom above the undervolt point |
| --- | --- | --- |
| Omitted, or `--auto-uv-boost standard` | 2 bins | +30 MHz |
| `--auto-uv-boost aggressive` | 4 bins | +60 MHz |

Every scanned tier tests its selected tail during the search and final
verification, then saves that tested shape. A full adaptive scan applies the
choice to all three tiers, including Balanced-to-Performance reuse and resume.
Aggressive Boost permits more headroom; it does not promise higher measured
clocks or FPS and may increase power use or instability.

The count refers to successive **editable voltage points above the anchor**.
The anchor itself stays at the selected clock. Each following point adds
15 MHz until the chosen number of steps is reached; higher editable points
then keep that ceiling. With an illustrative 2800 MHz anchor:

| Curve point | Standard Boost | Aggressive Boost |
| --- | --- | --- |
| Anchor | 2800 MHz | 2800 MHz |
| First editable voltage point above it | 2815 MHz | 2815 MHz |
| Second | 2830 MHz | 2830 MHz |
| Third | 2830 MHz | 2845 MHz |
| Fourth and later | 2830 MHz | 2860 MHz |

These are requested curve targets, before driver clock-step constraints.
Aggressive Boost adds **30 MHz more nominal headroom than Standard**, not
60 MHz more. If fewer than four editable voltage points remain above the
anchor, the generated tail cannot reach all four steps. Voltage spacing is
GPU-dependent: four points do not imply a fixed mV increase. The GPU may use
higher voltage to reach the tail; the anchor is not a voltage lock.

`--auto-uv-tail-rise-bins N` remains the custom numeric alternative (0 makes a
flat tail; the supported range is 0–8). It cannot be combined with
`--auto-uv-boost`. Neither boost setting changes the freeze blacklist,
predicted stability margin, floor caution, or Performance voltage headroom.

`--auto-uv-retry-after-freeze` separately permits another clock-climb attempt
at higher voltage after an explicitly resumed scan. It defaults off, is
CLI-only, and can require further reboots. See [freeze-retry behavior](auto-uv-recovery.md#cli-only-retry-after-a-hard-freeze)
for the off/on rules and checkpoint requirements.

## Monitoring the requested and observed boost

Keep three kinds of evidence separate when reading a scan:

| Evidence | What it establishes |
| --- | --- |
| Requested curve | The anchor, tail-bin count and complete target plan describe the intended shape. Saved profiles retain the tested plan and tail-bin count. |
| Applied controls | Before a probe workload starts, the scan refreshes driver V/F offsets and checks each planned point. A mismatch stops the scan. The temporary clock ceiling is selected from supported driver steps at or below the requested ceiling; Auto-OC and final verification log requested-to-applied ceiling changes. This is a control setting, not a measured running clock. |
| Workload telemetry | Live samples and probe summaries report measured clock and voltage, power, temperature, FPS and limiting reasons. Loaded median and 90th-percentile clocks, loaded median voltage, and qualified sample counts help distinguish sustained behavior from occasional boost. Missing measurements remain unknown. |

For the example above, `2860MHz` describes the requested Aggressive Boost
ceiling. If the driver applies a lower ceiling, report that separately. If the
loaded clock stays below either ceiling, report the measured value rather
than claiming +60 MHz of observed boost. A saved 4-bin curve or higher FPS alone
does not prove that the extra tail headroom was used.

The full curve is present during probing and final verification, but the
workload chooses which voltage points the GPU visits. Current monitoring does
**not** certify that every tail point was exercised, or that the top of the
tail was sustained. A passing run establishes stability only for the tested
workload and duration; it is not a guarantee for every game.

To compare Standard and Aggressive Boost, record the anchor and curve shape,
requested and applied ceilings, measured clocks and voltage, average/maximum
power, temperature, FPS, performance-cap reason and hardware-brake samples.
Use the same GPU, anchor, power limit, memory offset, workload/resolution,
probe duration and comparable thermal conditions. Separate Q2RTX and CUDA
measurements, and do not compare a short probe with a longer final soak as if
they were equivalent. Two independently optimized scans may choose different
anchors; their difference cannot be attributed to the tail setting alone.

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

When a descent runs out of voltage bins with every probe passing, the card
never showed its edge and the long soak would be the first real test of the
deepest point. The sweep then keeps the pass one step above the floor for
verification and sets the floor pass aside. A descent that ended on a failed
or blocked probe is unaffected.

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
Auto-OC can increase clocks toward the Performance clock target and retry
blocked rungs up to six editable voltage bins above that proven voltage. An edited voltage target keeps its requested
bound; the scan dialog sends only edited targets, so an unchanged table
default never becomes that bound. Balanced and Performance retain their shared power budget and can
still converge if no higher stable measured clock is found.

A cached unsafe voltage/clock band is skipped before touching the GPU. Auto-OC
can test the same clock at a higher editable voltage outside that band, within
the applicable voltage cap. After a successful voltage retry, remaining clock steps
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
a rising tail: two bins (+30 MHz nominal) by default, or four bins (+60 MHz)
with CLI Aggressive Boost. The selected tail is present during testing.
The saved profile retains the complete tested curve; selection does not
rebuild it or splice lower-voltage points from other candidates.

Because the tail offers boost headroom, the anchor is **not a voltage lock**.
Requested and measured voltage can differ. The size of that difference depends
on the card's curve bins and operating conditions; a bin count alone does not
establish an mV difference.

If descent cannot improve the passing baseline, Auto-UV retains that exact
baseline, including its target label and curve.

## Verification

Performance rechecks a contradictory near-tie before final selection: if its
short-probe FPS leader uses more power and measures a lower core clock than
another passed curve within 2% FPS, those two exact curves run in A/B/B/A order.
Each run uses 30 seconds of Q2RTX and 5 seconds of CUDA at the same scan settings.
The higher two-run mean FPS becomes the recommendation; the 2% window only
triggers measurement and never suppresses a small measured gain. The dialog and
CLI label the comparison winner and averaged rows; other candidates remain
selectable. This adds about 140 seconds plus workload startup only when this
case occurs. Efficiency, Balanced, custom targets, interrupted-run recovery and
user-stop selection do not add this comparison.

Standard and Aggressive Boost retain their tested two-bin and four-bin curves.
No voltage bounds, unsafe-point rules or final verification durations are relaxed.
A comparison failure stops the scan through the normal probe safety path rather
than recommending a stale pass. The comparison winner still needs the usual
long final verification; repeated short probes are not a stability guarantee.

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

Q2RTX startup, including shader compilation, does not count toward verification
progress or measured telemetry. Its first-render `measure_start` event begins
measurement and load checks; there is no fixed startup grace period. Concurrent
CUDA work starts at that event too. A separate startup timeout reports a launch
failure without marking the voltage unsafe. Stop and fatal-error handling remain
active throughout startup.

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
