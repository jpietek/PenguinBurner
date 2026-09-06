# Multi-GPU Profiles

[Back to profile management](profile-management.md)

With one GPU, the disabled target selector identifies the card. With multiple
GPUs, selecting a target filters out
profiles bound to other cards; legacy/unassigned profiles remain visible so
they can be verified and bound. Tier assignments and the startup checkbox are
kept per GPU. A legacy profile can be used directly on a one-GPU system; on a
multi-GPU system it must be verified on the intended card first.

With **Apply on startup** ticked, Apply saves the selected profile in that
GPU's boot entry. At boot, `penguin-burnerd` resolves saved UUIDs to their
current driver indices and applies the available entries serially. A missing
GPU is skipped but remains saved for a later boot. Restore defaults saves stock
for the selected GPU instead.

**Main GPU** requires a saved startup profile for the selected card. Untick it
to use the most recently saved available GPU.

The daemon has one active policy engine. After applying the saved entries,
the explicitly selected **Main GPU** remains actively monitored; without that
selection, the most recently saved available GPU remains active. It
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
