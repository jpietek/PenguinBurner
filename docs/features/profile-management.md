# Profile Management

> Feature guide — see the [README](../../README.md) for the project overview.

The **Profiles** tab lists every saved undervolt profile and is where you apply,
verify, tier, export, and clean up curves.

![Stored undervolt profiles](../assets/profiles-management.png)

## The table

Each row shows: Date, Profile name, GPU, mV, Target MHz, Effective MHz, FPS/W,
FPS, Power W, and Memory offset. Sort by any column to compare runs. Verified
profiles retain the GPU name, UUID, and PCI identity from verification, so a
saved curve cannot silently move to another card if driver indices change.

Power shows measured watts and the percentage difference from that GPU's
factory/default power limit, with lower values in green. This is a comparison
with the power cap, not measured stock power savings. If the matching GPU or its
default limit is unavailable, only watts are shown. Other metrics show absolute
values; their tooltips retain each scan's baseline comparison. Those baselines
can differ with power and memory settings and do not compare profiles or tiers.

## Actions

Top bar:

- **Apply** — run the highlighted profile now.
- **Target GPU** — filter profiles and choose the physical card for actions.
  The selector remains visible but disabled when only one GPU is detected.
- **Main GPU** — on systems with two or more NVIDIA GPUs, explicitly choose
  which saved startup GPU owns daemon monitoring after boot. Untick it to
  restore the default last-saved-GPU behavior. A startup profile must already
  be saved for the selected GPU before this toggle is available.
- **Apply on startup** — also save the applied profile for the selected GPU.
  Off by default: with it unticked, Apply changes the current session only
  and clears only that GPU's saved boot profile.
- **Silent fan curve** — use the saved fan curve with the applied profile.
- **Restore defaults** — return the GPU to stock now and at boot.

On a one-GPU system, the disabled target selector identifies the card and
**Apply** works as before. On a multi-GPU system, selecting a target filters out
profiles bound to other cards; legacy/unassigned profiles remain visible so
they can be verified and bound. Tier assignments and the startup checkbox are
kept per GPU. A legacy profile can be used directly on a one-GPU system; on a
multi-GPU system it must be verified on the intended card first.

With **Apply on startup** ticked, Apply saves the selected profile in that
GPU's boot entry. At boot, `penguin-burnerd` resolves saved UUIDs to their
current driver indices and applies the available entries serially. A missing
GPU is skipped but remains saved for a later boot. Restore defaults saves stock
for the selected GPU instead.

The Rust daemon still has one active policy engine. After serial application,
the explicitly selected **Main GPU** remains actively monitored; without that
selection, the most recently saved available GPU remains active as before. It
gets drift recovery, adaptive switching, and PenguinBurner fan control.
Earlier GPUs keep their V/F curve, memory offset, and power limit, while their
fans are released to hardware auto. Selecting and applying another GPU still
transfers the active engine to it for the current session; it does not run a
second monitoring engine.

GPU discovery for these controls comes from NVIDIA NVML. Intel and AMD PRIME
adapters are not shown and do not make a one-NVIDIA-GPU setup count as
multi-GPU.

The same preference is available without the GUI:

```bash
pburn-cli --set-main-gpu GPU-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
pburn-cli --clear-main-gpu
```

Right-click a profile:

- **Edit VF Curve** / **Edit Fan Curve** — open the editors (see
  [curve-editor.md](./curve-editor.md)).
- **Apply** / **Verify** — apply the curve, or re-run verification.
- **Export LACT** — write the curve as a LACT config.
- **Assign Tier** — Efficiency / Balanced / Performance / None.
- **Delete** — remove the profile.

Apply, Verify, and Delete go through the root hardware service
(`penguin-burnerd`), so none of them ask for your password; verification runs
as your regular user.

## Suspend/resume

While a profile runtime is active, it survives system sleep
automatically. Waking from suspend can silently reset driver state
(power limit, locked clocks, the V/F curve), so the runtime engine
detects resumes from sleeps longer than a couple of seconds, waits a few
seconds for the driver to settle, then re-asserts the applied profile:
persistence policy is re-asserted, the power limit is checked and only
rewritten when it drifted, the clock ceiling is re-locked, fan state is
re-asserted, and the V/F curve re-verifies through the engine's usual
drift guard. Detection works on any init system — it compares two kernel
clocks instead of listening to logind — and the result is logged as
`event=resume-reverify-complete` in the engine log (or
`event=resume-reverify-gave-up` if the driver kept rejecting the
recovery writes; the per-tick guards keep re-verifying from there).

This covers the runtime engine only: state that was deliberately left on
the GPU after stopping the runtime is not re-verified after a sleep —
reapply the profile if you suspend in that state.

## Where profiles live

Saved profiles are stored under the PenguinBurner user config directory in
`auto-uv-profiles/`. Only final, verified (or user-edited) curves appear here.
