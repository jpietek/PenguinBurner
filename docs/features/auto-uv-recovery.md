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

When compatible checkpoints exist for the requested tier, the GUI offers a
recovery candidate before repeating discovery. Resuming skips completed voltage
work and proceeds to remaining clock tuning and final verification.
**Start From Scratch** repeats the scan while retaining unsafe-point history.

## Clearing scan history

Use a fresh scan only when you deliberately want to forget recovery candidates
and unsafe-point history:

```bash
pburn-cli --fresh-auto-uv-scan
```

This preserves Afterburner imports and the managed Q2RTX download. For ordinary
failures, inspect `debug-logs/` and retry with the blacklist intact first.
