# Auto-UV Stop and Recovery

[Back to Auto-UV](auto-uv.md)

## Failed probes

Auto-UV retains passing candidates so one failed probe need not discard the
scan. Recovery depends on what failed:

| Failure | Next action |
| --- | --- |
| Flattened baseline is unstable | Lower clock and retry, with at most ten probes. |
| Voltage or clock candidate fails | Fall back to a passing point outside the unsafe region. |
| Requested clock cannot be reached within the voltage target | Keep a safer passing clock; do not exceed the voltage target. |
| Final verification fails | Try the next safer tested curve, including lower clocks at the same voltage. Each failed voltage/clock pair is tried at most once. |
| Later tier fails | Keep and return profiles from completed tiers. |
| Setup, required measurements, daemon access, or power-limit verification fails | Stop further probing; retain completed tier profiles. |
| No usable candidate remains | End that tier without inventing a result. |

Workload crashes, device loss, NVIDIA Xids, and CUDA computation errors reject
the candidate. The CUDA workload is built to fail softly: every launch runs
twice and the two copies are compared element for element on the GPU, the
load ramps through partial occupancy before full current, a GPU fault under
load counts as instability, and a host watchdog ends a stalled run while the
card still answers. A wrong result or a fault blacklists the candidate without
a reboot. Recovery can continue only while the GPU, daemon, and
measurements remain usable. Verification cannot guarantee stability in every
game or prevent a system freeze: a card whose edge is a bus drop can still
take the host down.

## Stopping deliberately

In an **interactive single-tier voltage sweep**, a stop after passing
checkpoints can open the final-choice dialog. Choose a passed candidate for
verification or discard it. With no passing checkpoint, there is nothing to
verify.

Stopping **All tiers**, a clock search, or final verification ends that
operation. It does not automatically retry. Clean stops, Ctrl-C, and SIGTERM
clear the active-probe marker and do not blacklist the current point.

## After a crash or reboot

Before each risky probe, Auto-UV saves its voltage and clock. If the process
ends abruptly, the next scan records that region in
`auto-uv-unsafe-voltages.json` in the user config directory. That file sits
outside `uv-result/` on purpose: clearing resumable scan state for a fresh scan
keeps what the card has already proven unsafe. A file left at the old
`uv-result/` location by an earlier release is adopted on first use.

Each recorded freeze (an abrupt exit, an NVIDIA Xid, or the CUDA workload's
hang watchdog) is also one point on the card's stability edge. The edge runs
parallel to the card's stock V/F curve, so one freeze predicts it at every
other clock. Every later descent and climb in that scan, and in later scans,
stays three voltage bins above the predicted edge; the final soak still
decides what ships. Cards that fail softly never record a freeze and keep
their full search.

The blacklist blocks the failed voltage and lower voltages at the recorded
clock band and above, including a small clock guard band. It is checked before
voltage probes, clock climbs, and final verification. A tier that already
passed its soak is invalidated only by a later failure at the same or a lower
clock and the same or a higher voltage; the guard band below a higher-clock
failure does not reach back into a verified point. A lower passing clock
may still be usable. Auto-UV never exceeds the configured voltage target to
force a higher clock.

An abrupt power loss or forced kill can leave the same marker. The record
means the probe ended abruptly; it does not prove GPU instability.

Click **Start Auto Undervolt** again with the same GPU and scan settings to
resume an unfinished scan. Opening the app never starts or resumes GPU work.
The Auto-UV tab restores its Runs table, baseline, measured results, candidate
curves, tier progress and completed tier curves. The plot returns to the last
passing candidate; failed and incomplete probes are omitted. The header
shows **Resuming**, and the restored measurements are historical, not new live
telemetry.

The scan reuses completed measurements, skips blacklisted candidates, and
long-verifies a recovery candidate one editable voltage bin above the last
passing candidate outside the blacklist. The source candidate must not sit
above the failed clock, and at the failed clock it must have passed at a
higher voltage than the one that failed: a descent holds one requested clock
while voltage falls, so the crashed probe shares its clock with every earlier
pass of that tier. This recovery voltage may exceed the original target; the
added margin is one bin above the saved candidate. When every passing
candidate of the interrupted tier lies inside the blacklisted band, as after
a crash during a clock climb, that tier runs again from its own start instead
of stopping the resume: Performance reuses the verified Balanced point and its
climb skips the cached band before touching the GPU. A resumed scan rebuilds
that Balanced hand-off from the checkpoint's verified curve, so Performance
never re-descends a ladder Balanced already proved. Power and memory settings stay unchanged. The
**Resume verification** row represents a new measurement, not a previously
verified combination. Its duration follows
the active tier (Efficiency 1 minute, Balanced 3 minutes, Performance 5 minutes,
or the configured override), using Q2RTX and CUDA. On success it completes that
tier and continues remaining tiers; a failed resume verification stops further
GPU work. No eligible recovery point means the scan stops with an explanation.
Completed tier verifications are reused only while their saved profiles remain
unchanged. A missing cached probe can require new measurements, but it does not
erase completed tiers: a verified Efficiency tier is skipped before its setup
or sweep when Balanced is unfinished. Final verification that was interrupted
must run again in full.

Progress is saved atomically in `uv-result/auto-uv-scan-checkpoint.json`.
After a reboot the stock V/F readback moves with temperature; an RTX 3080
came back 30 to 60 MHz lower across most of its curve. With the same voltage
grid, zero stock offsets, GPU, driver and scan settings, Auto-UV accepts
whole-bin drift of up to 75 MHz per point, keeps completed tiers and rebuilds
saved candidate offsets to preserve their absolute clock targets. Baselines are measured again; the
unfinished tier still requires resume verification. A larger curve change or
changed GPU, driver, scan settings, workload or algorithm starts a new scan
while preserving the blacklist. Older checkpoints without the full stock curve
snapshot require an exact curve match. The log names the changed input groups
(GPU, driver, base curve, policy, options, settings, workload or algorithm) and
reports changed profiles or malformed checkpoint data. Older checkpoints without
input fingerprints can only report a general identity mismatch. Before replacing
an unusable checkpoint, Auto-UV keeps its original bytes beside it as
`auto-uv-scan-checkpoint.json.rejected-<unique-id>.bak` and logs that path. A read
or backup failure stops the scan without replacing the original. Completed scans
clear the active checkpoint.
Older runs without this checkpoint can still offer the existing saved-candidate
recovery, but their text logs cannot reconstruct a complete resumable scan. A
rejected checkpoint cannot bypass compatibility checks through this older path.
Legacy recovery uses the interrupted tier's verification duration when the tier
is recorded, honors duration overrides, and retains compatible tested curves
from the failed run for final-verification fallback. Incompatible or blacklisted
curves are excluded; fallback still stops if the GPU or daemon is unusable.

After `--restore-stock`, you can start a CLI scan normally, including after a
reboot. Scan startup stops the daemon's active runtime profile before changing
fans or checking V/F controls. The daemon service, saved profiles and stock boot
setting are kept. If the profile cannot stop, the scan stops before GPU writes.
GUI scans retain the daemon's existing handoff and session-profile restoration.

## Clearing scan history

Use a fresh scan only when you deliberately want to forget recovery candidates
and unsafe-point history:

```bash
pburn-cli --fresh-auto-uv-scan
```

This preserves Afterburner imports and the managed Q2RTX download. For ordinary
failures, inspect `debug-logs/` and retry with the blacklist intact first.
