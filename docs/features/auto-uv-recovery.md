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
the candidate. Recovery can continue only while the GPU, daemon, and
measurements remain usable. Verification cannot guarantee stability in every
game or prevent a system freeze.

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
`uv-result/auto-uv-unsafe-voltages.json`.

The blacklist blocks the failed voltage and lower voltages at the recorded
clock band and above, including a small clock guard band. It is checked before
voltage probes, clock climbs, and final verification. A lower passing clock
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
long-verifies a recovery candidate at a previously passing lower clock outside
the blacklist and one editable voltage bin above its measured voltage. This
recovery voltage may exceed the original target; the added margin is one bin
above the saved candidate. Power and memory settings stay unchanged. The
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

Progress is saved atomically in `uv-result/auto-uv-scan-checkpoint.json`. A
changed GPU, driver, base curve, scan settings, workload or algorithm starts a
new scan while preserving the blacklist. The log names the changed input groups
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

## Clearing scan history

Use a fresh scan only when you deliberately want to forget recovery candidates
and unsafe-point history:

```bash
pburn-cli --fresh-auto-uv-scan
```

This preserves Afterburner imports and the managed Q2RTX download. For ordinary
failures, inspect `debug-logs/` and retry with the blacklist intact first.
