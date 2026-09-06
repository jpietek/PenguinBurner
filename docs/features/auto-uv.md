# Automatic Tuning (Auto-UV)

Auto-UV tests your GPU with Q2RTX and CUDA, finds stable voltage/frequency
settings, and saves verified profiles. Choose one tier or **All tiers** for
an Efficiency, Balanced, and Performance set.

![Auto-UV candidate sweep](../assets/auto-uv-scan.png)

> GPU tuning can crash the driver or freeze the system. Auto-UV records
> interrupted probes so later scans avoid the same voltage/clock region.

## Run a scan

1. Close games and other demanding GPU workloads.
2. Click **Setup Auto Undervolt**, select the NVIDIA GPU and tier, then start.
3. Review the live curve, measurements, and log. The GUI applies the verified
   result when the scan finishes.

The GUI offers to install the root hardware service once. Scans then run as
your regular user, with GPU writes handled by `penguin-burnerd`.

From the CLI:

```bash
pburn-cli --auto-uv-voltage-scan --auto-uv-mode adaptive  # All tiers
pburn-cli --auto-uv-voltage-scan --auto-uv-mode efficiency
```

See the [CLI reference](../../readme-cli.md#auto-uv-scan) for per-tier overrides.

## What happens

1. Apply and read back the tier's power limit and measure its baseline.
2. Lower voltage in steps, testing each candidate with Q2RTX and CUDA.
3. Adjust clocks for the selected tier or custom target.
4. Run final verification on the exact selected curve, then save it.

Failures fall back to safer settings where possible. Final verification can
retry another tested curve; a failed later tier preserves completed profiles.
Stopping the scan remains a user-controlled action.

All tiers use a smooth ramp and **two rising tail bins** (+30 MHz nominal).
The voltage target is a curve anchor: GPU boost can use higher voltage bins.
Power limits can also reduce the loaded clock below the requested target.
There is no fixed percentage cutoff for measured clock loss; workload stability,
load, and FPS checks still apply.

See [search and verification details](auto-uv-search.md),
[stop and recovery behavior](auto-uv-recovery.md), or the
[illustrated algorithm and RTX 5080 report](https://jpietek.github.io/PenguinBurner/auto-uv-cookbook/).
The [RTX 5070 Ti report](https://jpietek.github.io/PenguinBurner/pr72-curve-comparison/)
compares curve shapes before and after smoothing.

## Presets / tiers

| Tier | Goal |
| --- | --- |
| Efficiency | Best measured FPS per watt, with a lower default power budget. |
| Balanced | Balance measured performance and power savings. |
| Performance | Undervolt, then raise voltage and clock toward the Auto-OC targets. |

Two tiers can legitimately select the same point. All tiers reuses scan work
only when power, memory, curve shape, and measurement evidence are compatible.

## Custom tier targets

![Auto-UV setup with Advanced controls for every tier](../assets/auto-uv-setup.png)

Every tier has the same controls: **voltage target (mV), core clock target
(MHz), memory offset (MHz), and power limit (W)**.

**Defaults suit most GPUs. Change them only if you understand voltage/frequency
tuning and the risk of instability.** Allowed clock ranges use the GPU table's
defaults, independently of edits to other tiers:

| Tier | Minimum clock | Maximum clock |
| --- | --- | --- |
| Efficiency | Default Efficiency minus 15% | Default Balanced |
| Balanced | Default Efficiency | Default Performance |
| Performance | Default Balanced | Default Performance plus 5%, rounded to the nearest MHz |

For an RTX 5080 these ranges are 2380–2800, 2800–2950, and 2800–3098 MHz.
They are input limits, not guaranteed stable settings. Voltage inputs use the
editable live curve's range.

A lower custom clock is tested **after voltage descent**, at the stable voltage
already found. There is no second voltage-down sweep. If descent stopped above
the requested voltage, Auto-UV reports the retained stable voltage. Higher
clock targets use the tested clock climb toward the configured targets.

GPUs without table entries show **Auto**. Custom ranges use driver-reported
clocks and editable voltage bins where available; missing ranges remain Auto.
No other GPU's preset is substituted. Fixed-power laptop GPUs use the stock
limit with the power control disabled.

## GPU selection and telemetry

Select the card in the setup dialog or pass `--gpu-index N`. The same card is
used for tuning, telemetry, Q2RTX, CUDA, and verification. The picker lists
NVIDIA GPUs only; with none detected, Start is disabled.

The scan table shows requested and measured clocks separately, alongside
voltage, FPS, power, temperature, fan speed, and FPS/W.

## Stop, choose, or resume

During an interactive single-tier voltage sweep, stopping can offer passing
candidates for final verification. Stopping All tiers, a clock search, or final
verification ends that operation. See [recovery details](auto-uv-recovery.md)
for crash recovery, saved checkpoints, and deliberate resets.

## After the scan

Use **Profiles → Apply** to select a saved curve. Enable **Apply on startup**
before applying to save it for boot; enable **Silent fan curve** for quieter
cooling. See [profile management](profile-management.md) for verification,
export, and multi-GPU behavior.

Apply the latest profile from the CLI:

```bash
pburn-cli --daemonize --auto-uv-profile latest
pburn-cli --daemonize --auto-uv-profile latest --silent-fan-curve
```

## State and logs

Under the PenguinBurner user config directory:

- `debug-logs/` — scan logs.
- `uv-result/` — checkpoints, results, and unsafe-point history.
- `auto-uv-profiles/` — saved profiles shown in the GUI.

If a scan cannot finish, read its latest log and follow the
[troubleshooting guide](troubleshooting.md#the-scan-stops-early).
