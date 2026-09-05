# Automatic Tuning (Auto-UV)

> Feature guide — see the [README](../../README.md) for the project overview and
> the [other feature pages](./) for adaptive UV, the overlay, profiles, the curve
> editors, and the silent fan curve.

Auto-UV is the core PenguinBurner feature. It tests your GPU under real load,
finds the most efficient stable voltage/frequency curve, and saves it for later
terminal or daemon use.

![Auto-UV candidate sweep](../assets/auto-uv-scan.png)

The Auto-UV tab shows the live V/F scatter (base vs accepted candidate curve), a
streaming event log, and the per-step **Undervolting runs** table (mV, clocks,
FPS, power, temp, fan, FPS/W).

> ⚠️ Auto-UV makes real hardware changes. A bad voltage point can hang the GPU,
> crash the driver, or force a reboot. Auto-UV records each risky voltage before
> probing it and marks it unsafe after a crash, so later runs avoid it.

## Run a scan

```bash
./penguin_burner.sh --auto-uv-voltage-scan   # start a scan explicitly
./penguin_burner.sh --fresh-auto-uv-scan     # forget previous results, start clean
```

The scan runs as your regular user; its privileged GPU writes go through the
root hardware service (`penguin-burnerd`), which must be installed and running.
The GUI offers that one-time setup automatically; from the CLI install it with
`sudo ./penguin_burner.sh --migrate-to-daemon-service`.

## What happens

- Resets clocks, offsets, power policy, and fan control before measuring.
- Runs **Q2RTX** (PenguinBurner's headless Quake II RTX benchmark) plus a
  **CUDA** companion load for a real, GPU-bound workload. Q2RTX is downloaded
  automatically if missing.
- Walks voltage down step by step, verifying each candidate before accepting it.
- Stops before unsafe points, excessive clock loss, crashes, or NVIDIA Xid errors.
- Saves stable checkpoints as it goes, then runs a longer final verification
  (default `300s`) before publishing the curve.
- If you stop the scan after stable checkpoints exist, PenguinBurner offers those
  previously stable candidates for final verification instead of throwing the
  work away.

All three tiers build their candidate curves with a gradual transition into
the selected V/F point. The exact curve applied during each probe is kept with
its measurements. Selection and final verification preserve every point,
including the lower ramp and rising tail; no final rewrite introduces a jump.
Performance keeps the selected Auto-OC candidate intact as well; it does not
combine lower-voltage points from other candidates into a new curve.

Final verification runs Q2RTX and CUDA on that complete curve, preserving its
selected voltage, clock, tail, and power limit throughout the soak and save.
It uses the tier's normal clock-loss rules against its measured baseline.
For a deliberately lower custom clock, the clock guard accounts for that
reduction and the final FPS check uses the passed lower-clock measurement.
It does not replace the curve with separate low-clock transition sweeps.
Existing saved profiles are unchanged.

When a tier includes a board-power limit, Auto-UV applies and reads back
that exact limit before the tier's stock baseline, voltage sweep, and final
verification. It stops instead of probing if the limit cannot be established.

Full scans start directly with Efficiency's power limit and reuse that initial
stock/flattened baseline for its search. Balanced and Performance share a second
baseline when their power limit, memory offset, and tail settings match; each
still enforces its own clock-loss allowance. For a 300 W / 360 W / 360 W scan,
this means one baseline pair at 300 W and one shared pair at 360 W.

On a genuinely power-limited card, the lower measured clock is treated as a
governor operating point only while sustained cap evidence is present; ordinary
clock regressions still fail. Efficiency and Balanced may then probe a bounded
clock climb at the already-proven voltage, without raising the voltage or
loosening the stability checks.

Efficiency chooses the highest measured FPS/W among passed candidates inside
its clock-loss allowance, including candidates from before the climb. Comparisons
use unrounded measurements; the tables show four FPS/W decimal places. Equal
FPS/W favors the higher measured clock, then lower power. Its table clock is an upper
search limit, not a required final clock. Balanced keeps the faster stable choice
within its own clock-loss allowance; Performance pursues its higher Auto-OC target.
All three tiers remain available even when two happen to find the same best point.
Lower measured watts break ties at equal measured clock and FPS/W. Driver V/F
offsets must read back as requested before a probe starts.

All three tiers keep two rising tail bins (+30 MHz nominal) throughout the
search and final verification. The tail is tested with each candidate, never
added after a successful probe. It provides boost headroom above the selected
point, so the voltage target is an anchor, not a strict operating-voltage limit.
Balanced and Performance retain matching tails so Performance can reuse a
passed Balanced descent when power, memory, baseline, and clock-floor checks
also permit it.

During voltage descent, passing probes keep their requested clock target even
when the measured clock is lower. A measured shortfall is not repeatedly
subtracted from the next target after power limiting clears. Higher measured
clocks can still raise the target, and failed clock or stability checks still
reject a candidate.

Power-control support comes from the daemon's verified stock-reset setter result.
Only an explicit driver rejection as unsupported permits platform-managed mobile
power. A missing result stops the scan. Configured caps are read back before and
after probes, including the final soak; a mismatch stops the scan instead of saving
a profile under an unverified limit.

`hw-power-brake` is the board's own power-delivery protection, not the power
limit you configured. A probe that trips it still counts as power-limited
rather than unstable, so it is not failed on clock alone — but the event is
always reported: the run's Cap column names it, the probe log line and JSON
event carry the sample count, and the saved scan result records it. Repeated
brakes on a candidate mean that board is hitting its delivery limit at that
operating point.

By default Q2RTX runs through PenguinBurner's managed
[headless benchmark binary](https://github.com/jpietek/Q2RTX-headless), so no
desktop display server or compositor wrapper is needed. Render resolution is
selected from the chosen GPU's NVML VRAM total: `2560x1440` for GPUs with
`<=8 GiB`, otherwise `3840x2160` when VRAM is larger or unavailable.

## Stop, choose, or resume

You can stop Auto-UV from the GUI while it is scanning. After at least one stable
candidate exists, the stop request is handled as a controlled stop: PenguinBurner
opens the final-choice dialog with the already-passed candidates, and you can
choose which voltage/clock target should receive final verification. This does
not mark the current voltage unsafe. It only turns the completed checkpoints
into final-verification options.

If you stop before any stable checkpoint exists, there is no candidate to
verify, so the scan just stops.

Auto-UV writes an active-probe marker before each risky candidate or final
verification run. Normal exits, Ctrl-C, and SIGTERM remove that marker. If the
machine hangs, reboots, loses power, or the process is killed during a probe,
the next Auto-UV run consumes the stale marker, records that voltage/clock band
in `uv-result/auto-uv-unsafe-voltages.json`, and avoids repeating it.

The blacklist is checked before applying a climb or final-verification curve.
It blocks the failed voltage and lower voltages at the recorded clock band and
above, including a small clock guard band. If a climb reaches a cached unsafe
point, Auto-UV backs off to a passing clock and can test that clock at the
configured voltage target. It never exceeds that voltage target to force a
higher clock. A new critical GPU or workload error aborts the scan instead of
triggering higher-voltage retries. Explicit lower-clock targets also retain
their crash markers across abrupt exits.

When stable checkpoints exist for the same requested tier, the GUI shows a
previous-crash recovery dialog before starting discovery again. The default
choice is the next safer saved candidate above the failed voltage. Accepting a
candidate resumes from the saved baseline and candidate metrics, skips the
completed lower-voltage sweep, and goes straight to any remaining Performance
Auto-OC work plus final verification. Choosing **Start From Scratch** runs a new
scan instead, but the unsafe-voltage cache still applies.

Use `--fresh-auto-uv-scan` only when you deliberately want to forget the saved
Auto-UV state, including unsafe-voltage history and recovery candidates.

## Presets / tiers

![Auto-UV setup with the same Advanced controls for every tier](../assets/auto-uv-setup.png)

Presets share a two-bin rising tail and differ in power policy, clock-loss
allowance, and search targets. They map directly to
[adaptive UV tiers](./adaptive-uv.md):

| Preset | Tail-rise bins | Extra |
| --- | --- | --- |
| Efficiency | `2` | lowest tier power; bounded fixed-voltage clock reclaim on power-bound baselines |
| Balanced | `2` | balances performance and power savings within its clock-loss allowance |
| Performance | `2` | adds an Auto-OC ladder (raises V+clock to targets) |

## GPU selection and telemetry

PenguinBurner reads GPU identity, PCI bus id, driver version, and VRAM directly
through NVML (`libnvidia-ml.so.1`). It does not shell out to `nvidia-smi` for the
GPU picker or Q2RTX resolution choice.

The picker lists only detected NVIDIA GPUs. If a saved GPU index is no longer
present, it selects an available card for you to review before starting; opening
the dialog does not rewrite the saved setting. With no detected GPU, the dialog
explains the problem and disables Start Auto Undervolt.

The selected `--gpu-index` is used consistently for NVML/NVAPI control,
telemetry, Q2RTX, CUDA, profile verification, and runtime profile application.
On multi-GPU systems, pick the card in the tuning dialog or pass `--gpu-index N`
so the benchmark and the curve writer target the same physical GPU.

Clock-loss thresholds are automatic for each GPU and tier, with a 12.5% fallback
for unknown GPUs. The scan still checks loaded clocks during probing and final
verification, accounting for sustained power-limit evidence. These thresholds
are not editable.

## Custom tier targets

Each tier's Advanced page exposes voltage and clock targets. Default targets
are optimized for most GPUs. Change them only if you understand GPU
voltage/frequency tuning and the risks of instability or crashes.

All three pages use the same order: voltage target (mV), core clock target
(MHz), memory offset (MHz), and power limit (W).

Clock ranges use the GPU table's default targets, independently of edits to
other tiers:

| Tier | Minimum clock | Maximum clock |
| --- | --- | --- |
| Efficiency | Default Efficiency minus 15% | Default Balanced |
| Balanced | Default Efficiency | Default Performance |
| Performance | Default Balanced | Default Performance plus 5%, rounded to the nearest MHz |

These are allowed tuning ranges, not guaranteed stable operating points.
For an RTX 5080 they are 2380–2800, 2800–2950, and 2800–3098 MHz.
Voltage inputs use the editable live curve's range. The rising tail can operate
above the target anchor.

The normal voltage sweep runs first. When a custom clock target is lower than
its result, Auto-UV tests decreasing clocks at that result's stable voltage,
then verifies the selected combination. It does not run another voltage-down
sweep. If the original sweep stopped above the requested voltage target, the
scan reports that it retained the stable voltage. Higher clock targets use the
existing tested Auto-OC climb toward the voltage and clock targets.
Unchanged defaults preserve automatic tier selection and compatible
Balanced-to-Performance sweep reuse.

GPUs without table entries show **Auto**. Custom clock ranges use driver-reported
supported clocks when available; if no range can be read, the clock remains
Auto. An unavailable editable voltage range likewise keeps voltage Auto.
No desktop GPU target is substituted for an unknown card.

## Useful flags

| Flag | Purpose |
| --- | --- |
| `--auto-uv-voltage-scan` | start the Auto-UV scan explicitly |
| `--auto-uv-mode efficiency\|balanced\|performance` | select the same preset family as the GUI |
| `--gpu-index N` | select one NVIDIA GPU on multi-GPU systems |
| `--auto-uv-min-voltage-mv N` | explicit lowest voltage bin |
| `--auto-uv-memory-offset-mhz N` | memory clock V/F offset saved with the profile |
| `--auto-uv-power-limit-w N` | power limit applied during the scan and saved with the profile |
| `--auto-uv-tail-rise-bins N` | bins above lock point that may rise (`0` = flat) |
| `--auto-uv-<tier>-target-voltage-mv N` / `--auto-uv-<tier>-target-clock-mhz N` | tier targets for single or full scans |
| `--auto-oc-target-voltage-mv N` / `--auto-oc-target-clock-mhz N` | legacy Performance target options, used when per-tier targets are absent |
| `--auto-uv-<tier>-power-limit-w N` | full-scan per-tier power limit |
| `--auto-uv-<tier>-memory-offset-mhz N` | full-scan per-tier memory offset |

Tier target flags mirror the GUI tuning modal. `<tier>` is `efficiency`,
`balanced`, or `performance`. The older `--auto-uv-min-voltage-mv` option
continues to control the initial voltage sweep floor.

## After the scan

Runtime and daemon mode prefer the saved curve automatically:

```bash
./penguin_burner.sh --daemonize --auto-uv-profile latest
./penguin_burner.sh --daemonize --auto-uv-profile latest --silent-fan-curve
```

Make the latest verified profile the boot profile. The command performs the
full service install (refresh the daemon binary, rewrite the unit, restart the
service) before saving the boot profile, so it needs root:

```bash
sudo ./penguin_burner.sh --install-systemd-service --auto-uv-profile latest
sudo ./penguin_burner.sh --install-systemd-service --auto-uv-profile latest --silent-fan-curve
```

To set the boot profile without a password, tick **Apply on startup** in the
GUI and then apply the profile — the boot entry is written at apply time.

Export a saved curve to [LACT](https://github.com/ilya-zlobintsev/LACT) from the
GUI Profiles view. Tick **Silent fan curve** in the Profiles tab before
exporting when you want LACT to manage fan settings too. Review the generated file, then:

```bash
sudo install -m 0644 lact-config.yaml /etc/lact/config.yaml
sudo systemctl restart lactd
```

## State and logs

Under the PenguinBurner user config directory:

- `debug-logs/` — per-scan stdout/stderr
- `uv-result/` — scan results and the unsafe-voltage crash cache
- `auto-uv-profiles/` — final profiles shown in the GUI

## Troubleshooting

If a scan stops early, read the latest log in `debug-logs/` first — common
causes are an unsafe-voltage history entry, a clock guardrail, a Q2RTX/CUDA
failure, or interrupted final verification. To wipe history and rerun clean:

```bash
./penguin_burner.sh --fresh-auto-uv-scan
```

This keeps Afterburner imports and the Q2RTX download intact.
