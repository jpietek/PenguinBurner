# Troubleshooting & FAQ

> See the [feature guides](./README.md) for how each part works.

### The daemon fills the journal with GPU statistics

Periodic `telemetry` and `status` lines are disabled by default. Monitoring,
overlay updates, fan control, and adaptive switching continue normally. Startup,
profile changes, warnings, and errors are still logged.

To collect these statistics for debugging, run
`systemctl edit penguin-burnerd.service` with administrator privileges and add:

```ini
[Service]
Environment=PENGUIN_BURNERD_DEBUG=1
```

Restart `penguin-burnerd.service` with administrator privileges, then read the
logs with `journalctl -u penguin-burnerd.service -b`. Restarting interrupts the
active runtime profile; the daemon replays its saved boot configuration.

After debugging, remove that environment line from the override (preserving any
other overrides), reload systemd, and restart the service again. Alternatively,
set `PENGUIN_BURNERD_DEBUG=0` to disable it. Values `true`, `yes`, and `on` also
enable debugging; an unset variable leaves it off.

For a manually launched daemon, `penguin-burnerd serve --debug` enables the same
logging. The application's `--debug-log` option is separate and does not change
the running root daemon's journal output.

### The scan stops early

Read the latest scan log in `~/.config/PenguinBurner/debug-logs/`.
Candidate failures normally retry safer settings. A scan can end when no usable
candidate remains, required measurements are missing, the daemon is unavailable,
or its power limit cannot be verified. Completed tiers remain saved.

Retry with crash history intact. See [Auto-UV recovery](auto-uv-recovery.md)
for fallback behavior and deliberate history resets.

### I stopped Auto-UV before it finished

An interactive single-tier voltage sweep can offer passing checkpoints for
final verification. Stopping All tiers, a clock search, or final verification
ends that operation. Clean stops do not blacklist the current point.

### A Q2RTX window appears

The managed Q2RTX benchmark binary is headless and should not create an X11 or
Wayland window. If a window appears, check that the managed PenguinBurner Q2RTX
install is being used.

### Q2RTX installation ran out of disk space

Retry Auto-UV after freeing disk space. PenguinBurner detects an incomplete
managed Q2RTX install and rebuilds it automatically. To remove both the install
and any complete or partial cached downloads first, run this as your regular
user:

```bash
python3 -m stability.q2rtx --clean-q2rtx
```

The cleanup preserves PenguinBurner profiles, settings, scan history, and logs.
If the module command is unavailable in an older installation, use:

```bash
rm -rf -- "$HOME/.local/share/PenguinBurner/q2rtx" \
           "$HOME/.cache/PenguinBurner/q2rtx"
```

Then retry Auto-UV to download and install Q2RTX again. Do not use `sudo` for
either recovery command.

### Running on a headless server (no display)

Install the normal NVIDIA Vulkan driver stack. The managed Q2RTX benchmark path
uses the [headless Q2RTX fork](https://github.com/jpietek/Q2RTX-headless), so it
does not need Steam, a desktop display server, or a compositor wrapper.

### I have more than one GPU

Select the card in the Auto-UV dialog or with `--gpu-index N`. In Profiles,
**Target GPU** selects the card for profile actions; **Main GPU** selects the
startup card that owns monitoring. See [multi-GPU profiles](profile-multi-gpu.md)
for the full behavior.

For a boot-recovery issue, collect the daemon journal and its saved/replay
summary before changing the configuration:

```bash
journalctl -u penguin-burnerd.service -b --no-pager
python3 -m runtime.daemon_client boot-runtime-spec
python3 -m runtime.daemon_client status
```

The boot summary lists every saved GPU UUID and a replay outcome such as
`applied`, `active`, `stock-skipped`, `gpu-not-detected`, or `stock-fallback`.
Include this output when reporting missing cards or boot recovery problems.

### The game closed but PenguinBurner still shows Stop or Running

A wrapped Battle.net, Ubisoft Connect, EA app or other store launcher may still
be running. Fully exit it, including its tray process, to end the session and
return to stopped/Play. See [launcher sessions](game-library.md#games-that-start-another-launcher).

### Adaptive switching isn't doing anything

Adaptive needs at least two profiles with different tiers assigned. With one
tier it just runs that profile. Assign tiers from the Profiles tab or with
`--assign-auto-uv-tier`.

### The `penguin-burner` command isn't found

Make sure `~/.local/bin` is on your `PATH`.

### Voltage shows `n/a`

Voltage telemetry isn't available on that driver/GPU. The scan still runs using
its other safety checks. Use a recent driver and a supported card (RTX 30 / 40 /
50).

### Why does it need root?

The root service `penguin-burnerd` handles GPU writes. Installing it needs one
admin prompt; the GUI, CLI, and scans then use its socket as your regular user.

### Resetting user data

For Auto-UV recovery, prefer a [scan reset](auto-uv-recovery.md#clearing-scan-history).
A full reset deletes **all profiles, settings, logs, and cached downloads**.
Close PenguinBurner and back up anything you need before running:

```bash
rm -rf ~/.config/PenguinBurner ~/.local/share/PenguinBurner ~/.cache/PenguinBurner
```

This does not uninstall the daemon or restore GPU state. Use **Restore defaults**
first if you also want stock settings.
