<p align="center">
  <img src="docs/assets/penguin-burner-logo.png" alt="PenguinBurner logo" width="160">
</p>

# PenguinBurner CLI

PenguinBurner is an NVIDIA Auto-UV tuning tool. The default app entrypoints,
`penguin-burner` and `pburn`, start the Qt GUI. The explicit CLI entrypoints,
`penguin-burner-cli` and `pburn-cli`, are for Auto-UV scans, profile
verification, and applying saved Auto-UV profiles as daemon runtime.

Privileged GPU writes are performed by the root hardware service
(`penguin-burnerd.service`, a compiled Rust daemon); the CLI itself and Auto-UV
scans run as your regular user and talk to it over a local socket. Root is
only needed to install, repair, migrate, or explicitly uninstall the root
service; run those commands as your regular user and the CLI asks for
authorization itself (pkexec/sudo). Session profile changes use the running
daemon; setting a saved profile as the boot profile from the CLI goes through
the service-install command (see Profiles And Runtime below), while
`--restore-stock` resets the boot state to stock and the GUI's **Apply on
startup** workflow persists a profile — both through the daemon socket
without a password.

## Install

```bash
python -m pip install --user --upgrade penguin-burner
```

Start the GUI:

```bash
~/.local/bin/penguin-burner
```

One-time setup: install the root hardware service so profile application and
scans can reach the GPU (the GUI offers the same setup automatically on the
first privileged action). Run it as your regular user; it asks for
authorization (pkexec/sudo) when not run as root:

```bash
~/.local/bin/penguin-burner-cli --migrate-to-daemon-service
```

Run the CLI from an installed package:

```bash
~/.local/bin/penguin-burner-cli --auto-uv-voltage-scan
```

From a checkout, use the wrapper:

```bash
./penguin_burner.sh --auto-uv-voltage-scan
```

## Auto-UV Scan

Scans are explicit because they make hardware changes. The scan runs as your
user; its GPU writes go through the root hardware service, so the service must
be installed and running (see one-time setup above). Start a scan with:

```bash
./penguin_burner.sh --auto-uv-voltage-scan
```

The CLI scan options mirror the GUI Auto-UV tuning dialog. Start with
`--auto-uv-voltage-scan`, then choose the same preset family shown in the GUI
with `--auto-uv-mode efficiency|balanced|performance`.

Common scan controls, shown for every GUI preset:

- `--gpu-index N`: select the NVIDIA GPU used for scan, verification, and runtime.
- `--auto-uv-max-clock-drop-pct N`: maximum loaded core-clock drop allowed. For known GPUs the default follows the selected preset; unknown GPUs use `12.5`. On RTX 5080 the defaults are about `11.1` for Efficiency, `9.2` for Balanced, and `6.3` for Performance.
- `--auto-uv-memory-offset-mhz N`: memory clock V/F offset applied during the scan and saved with the final profile.
- `--auto-uv-power-limit-w N`: power limit applied during the scan and saved with the final profile.

Full-scan (`--auto-uv-mode adaptive`) per-tier overrides — each tier of the
combined run can carry its own limits, mirroring the GUI's per-profile
Advanced pages (`<tier>` is `efficiency`, `balanced`, or `performance`):

- `--auto-uv-<tier>-max-clock-drop-pct N`: that tier's maximum loaded clock drop. Absent tiers fall back to `--auto-uv-max-clock-drop-pct`, then the GPU table.
- `--auto-uv-<tier>-power-limit-w N`: that tier's power limit, applied at its final verification and saved with its profile.
- `--auto-uv-<tier>-memory-offset-mhz N`: that tier's memory V/F offset, applied for its descent and saved with its profile.

Efficiency preset controls:

- `--auto-uv-mode efficiency`: use the GUI Efficiency preset path.
- `--auto-uv-min-voltage-mv N`: lowest voltage bin Auto-UV may try in Efficiency.

Balanced preset controls:

- `--auto-uv-mode balanced`: use the GUI Balanced preset path.
- `--auto-uv-tail-rise-bins 2`: the GUI Balanced preset shape. The CLI fills this in automatically when omitted.

Performance preset controls:

- `--auto-uv-mode performance`: use the GUI Performance preset path.
- `--auto-oc-target-voltage-mv N`: Performance Auto-OC voltage target.
- `--auto-oc-target-clock-mhz N`: Performance Auto-OC clock target.
- `--auto-uv-tail-rise-bins 2`: the GUI Performance preset shape. The runtime fills this in automatically when omitted.

Examples:

Balanced with GUI defaults:

```bash
./penguin_burner.sh --auto-uv-voltage-scan --auto-uv-mode balanced
```

Balanced with all common GUI knobs made explicit:

```bash
./penguin_burner.sh --auto-uv-voltage-scan \
  --auto-uv-mode balanced \
  --auto-uv-max-clock-drop-pct 9.2 \
  --auto-uv-memory-offset-mhz 500 \
  --auto-uv-power-limit-w 390
```

Balanced with the preset shape made explicit:

```bash
./penguin_burner.sh --auto-uv-voltage-scan \
  --auto-uv-mode balanced \
  --auto-uv-tail-rise-bins 2
```

Efficiency with explicit GUI knobs:

```bash
./penguin_burner.sh --auto-uv-voltage-scan \
  --auto-uv-mode efficiency \
  --auto-uv-min-voltage-mv 850 \
  --auto-uv-max-clock-drop-pct 10 \
  --auto-uv-memory-offset-mhz 500 \
  --auto-uv-power-limit-w 390
```

Performance using the detected GPU table Auto-OC target:

```bash
./penguin_burner.sh --auto-uv-voltage-scan \
  --auto-uv-mode performance
```

Performance with a custom Auto-OC target:

```bash
./penguin_burner.sh --auto-uv-voltage-scan \
  --auto-uv-mode performance \
  --auto-oc-target-voltage-mv 910 \
  --auto-oc-target-clock-mhz 2950 \
  --auto-uv-tail-rise-bins 2
```

Performance with common scan limits too:

```bash
./penguin_burner.sh --auto-uv-voltage-scan \
  --auto-uv-mode performance \
  --auto-oc-target-voltage-mv 910 \
  --auto-oc-target-clock-mhz 2950 \
  --auto-uv-max-clock-drop-pct 6.3 \
  --auto-uv-memory-offset-mhz 500 \
  --auto-uv-power-limit-w 390
```

If only one Performance Auto-OC target flag is supplied, the missing voltage or
clock value comes from the detected GPU table target. Unknown GPUs need both
custom target values for the Auto-OC ladder.

The scan stays attached to the terminal because it is actively testing voltage
stability. If the system crashes during a probe, PenguinBurner records the
in-progress voltage as unsafe on the next run and avoids that voltage unless
Auto-UV state is deliberately cleared.

## Profiles And Runtime

List saved Auto-UV profiles:

```bash
./penguin_burner.sh --list-auto-uv-profiles
```

Apply the latest saved Auto-UV profile as a daemon after a final curve exists:

```bash
./penguin_burner.sh --daemonize --auto-uv-profile latest
```

Make the latest verified Auto-UV profile the boot profile for the selected
GPU. This command always performs the full service install — it refreshes the
daemon binary at the canonical
`/var/opt/penguin-burner/libexec/penguin-burnerd` path, rewrites the systemd
unit, and restarts the service before saving the boot profile — so it needs
root:

```bash
sudo ./penguin_burner.sh --install-systemd-service --auto-uv-profile latest
```

Persist it with the saved silent fan curve too:

```bash
sudo ./penguin_burner.sh --install-systemd-service --auto-uv-profile latest --silent-fan-curve
```

Without profile options the same command reinstalls or repairs the service
and keeps any existing boot profile. Reinstalling every time is deliberate:
skipping the install when a daemon already answered used to leave a stale
daemon binary running behind a success message. To change the boot profile
without a password, tick **Apply on startup** in the GUI and then apply the
profile (the boot entry is written at apply time);
`--daemonize --auto-uv-profile latest` applies for the current session only
and never changes the boot profile.

Remove the persistent boot-time service:

```bash
sudo ./penguin_burner.sh --uninstall-systemd-service
```

Recovery — reset the GPU to stock now and make stock the boot state, keeping
saved profiles (works headless, without the GUI):

```bash
./penguin_burner.sh --restore-stock
```

By default daemon runtime applies the saved V/F curve and leaves fan control to
the GPU driver. Add `--silent-fan-curve` to opt into PenguinBurner's saved
Auto-UV fan curve:

```bash
./penguin_burner.sh --daemonize --auto-uv-profile latest --silent-fan-curve
```

Adaptive Auto-UV can switch between saved verified profile tiers:

```bash
./penguin_burner.sh --daemonize --adaptive-auto-uv
```

For persistent adaptive boot autostart (full service install, root required):

```bash
sudo ./penguin_burner.sh --install-systemd-service --adaptive-auto-uv
```

Generated Efficiency, Balanced, and Performance scans are tiered automatically.
To override existing saved profiles, copy ids from `--list-auto-uv-profiles` and
assign them explicitly:

```bash
./penguin_burner.sh --assign-auto-uv-tier <eff-profile-id> efficiency
./penguin_burner.sh --assign-auto-uv-tier <bal-profile-id> balanced
./penguin_burner.sh --assign-auto-uv-tier <perf-profile-id> performance
```

Remove a manual tier assignment:

```bash
./penguin_burner.sh --assign-auto-uv-tier <profile-id> none
```

## Overlay And Latency

For Steam games, the intended visible launch option is:

```text
PENGUIN_BURNER %command%
```

Enable the native in-game overlay for games launched through the wrapper:

```text
PB_OVERLAY=1 PENGUIN_BURNER %command%
```

Enable optional dxvk-nvapi in-game latency marker parsing:

```text
PB_INGAME_LATENCY=1 PENGUIN_BURNER %command%
```

## Debugging

Write a diagnostic log for the current operation:

```bash
./penguin_burner.sh --debug-log --auto-uv-voltage-scan
```

Follow daemon logs:

```bash
sudo journalctl -u penguin-burnerd.service --since "-4 hours" -f
```

More details:

- [Auto-UV guide](docs/features/auto-uv.md)
- [Overlay guide](docs/features/overlay.md)
