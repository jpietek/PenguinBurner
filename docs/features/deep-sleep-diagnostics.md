# Laptop Deep-Sleep Diagnostics

[Back to deep sleep](deep-sleep.md)

Use this reference to interpret daemon status, identify GPU clients, and collect
park/wake evidence. The commands read state without applying a profile.

## How it works

At startup the daemon classifies the machine using only cached kernel state
(reads that never wake the GPU):

- `/proc/driver/nvidia/gpus/<pci-addr>/power` — the driver's resolved
  `Runtime D3 status`;
- `/proc/driver/nvidia/gpus/<pci-addr>/information` — the target's NVIDIA
  device minor, used to match that PCI device to its `/dev/nvidia<N>` node;
- `/sys/bus/pci/devices/<pci-addr>/power/control` — must be `auto` for the
  kernel to suspend the device;
- `/sys/bus/pci/devices/<pci-addr>/power/runtime_status` — the live power
  state. A GPU ever observed `suspended` selects Mobile handling even if the
  config files were inconclusive.

**Desktop mode** (runtime D3 disabled or unsupported) keeps the always-attached
behavior: the saved profile is applied at boot and the daemon stays attached.

**Mobile mode** (runtime D3 enabled with automatic runtime power management)
gets the deferred behavior:

- A saved profile is not applied while the GPU sleeps. The daemon watches
  `runtime_status` once per second (a cached read — no GPU traffic) and
  applies the profile when the GPU is actually in use: sustained `active`
  plus a real GPU client (a game, not a transient Vulkan capability probe).
- What counts as a real client follows the driver's runtime D3 granularity.
  Under fine-grained RTD3 (`mode: "fine-grained"`) the driver lets the GPU
  sleep even while desktop shells or monitoring tools hold `/dev/nvidia<N>`
  open, so an open handle proves nothing — the daemon asks NVML for live
  graphics/compute contexts instead. Each watcher query uses a short-lived,
  context-only NVML session and closes it before making the decision. If the
  result is idle or only root-owned `nvidia-powerd`, the watcher stops NVIDIA
  queries entirely until a changed target-device holder set (when that node is
  safely mapped), a kernel runtime-PM sleep/wake cycle, or the bounded
  awake-window re-probe described below signals a new workload. Between those
  edges it never opens NVML while waiting for suspension. Under coarse-grained
  RTD3 any holder of the
  target GPU's exact `/dev/nvidia<N>` node keeps that GPU awake, so there the
  device-handle scan is the accurate signal (another GPU's numbered node and
  auxiliary `nvidiactl`/`nvidia-uvm` handles never count). If a multi-GPU
  host does not expose the target's device minor, fd-based decisions stay
  pending rather than borrowing activity from another card. The same
  target-specific rule covers fallback when a fine-grained NVML context
  query is unavailable.
- One-off telemetry or capability queries release their GPU handles after 30
  idle seconds instead of keeping them for the daemon's lifetime.
- GPU persistence mode is never enabled (it blocks runtime D3); the profile
  is reapplied on wake instead.

After upgrading from a version that previously enabled persistence mode,
reboot once before testing deep sleep. NVIDIA persistence state can outlive a
service restart, and PenguinBurner does not make implicit hardware writes to
undo old state.

## Checking it on your machine

```
penguin-burner-cli --daemon-status
```

The `deep_sleep` block reports the verdict:

```json
"deep_sleep": {
  "state": "mobile",
  "mode": "fine-grained",
  "pci_addr": "0000:01:00.0",
  "runtime_status": "suspended",
  "runtime_active_ms": 512300,
  "runtime_suspended_ms": 7203400,
  "autostart_deferred": true,
  "suspended_observed": true,
  "parked": false,
  "daemon": {"engine_attached": false, "rpc_backends": 0}
}
```

`runtime_active_ms` / `runtime_suspended_ms` are the kernel's cumulative
runtime-PM residency counters for the dGPU since boot (wake-free reads).
They quantify how much the GPU actually sleeps: on a healthy setup the
suspended counter should dominate after some idle time, and two status
reads a few minutes apart show which state the interval was spent in.

`state: "mobile"` means the daemon treats GPU handles as ephemeral;
`"desktop"` (with a `reason`) means always-attached behavior. `"unknown"`
stays detached like Mobile mode until detection resolves. To confirm the GPU
really sleeps with the service running:

### Who is using the GPU right now

In Mobile mode the block also carries `gpu_clients` — the daemon's latest
sample of the processes it judged the park/wake decision by:

```json
"gpu_clients": {
  "source": "nvml-contexts",
  "graphics_count": 1,
  "compute_count": 0,
  "device_node_count": 3,
  "ignored_count": 1,
  "total_count": 1,
  "age_s": 0.6,
  "graphics": [{"pid": 3117, "name": "vkcube"}],
  "compute": [],
  "ignored_clients": [{"pid": 880, "name": "nvidia-powerd"}],
  "device_node_holders": [
    {"pid": 880, "name": "nvidia-powerd"},
    {"pid": 1450, "name": "plasmashell"},
    {"pid": 3117, "name": "vkcube"}
  ]
}
```

- `graphics` / `compute` list the counted processes holding a live NVML
  context of that kind; `device_node_holders` lists processes with the
  target GPU's `/dev/nvidia<N>` open (or every numbered node when a
  single-GPU host is unambiguous). The list stays empty when a multi-GPU host
  does not expose a safe target-node mapping. Each entry is `pid` plus the
  process name (`null` if it exited before the lookup).
- `ignored_clients` names positively identified infrastructure helpers that
  were observed but excluded from the fine-grained verdict. Currently the
  only exception is an exact `nvidia-powerd` process running as root. It is
  NVIDIA's Dynamic Boost controller, not a user workload; a game appearing
  alongside it remains counted normally. Missing or malformed process
  identity is counted conservatively, and coarse-grained/unknown modes never
  apply this exception.
- `source` names the decisive signal: `"nvml-contexts"` under fine-grained
  RTD3, `"device-nodes"` under coarse-grained RTD3 or when the NVML process
  query is unavailable.
- `total_count` is the number of unique clients the decision counted.
  **Parking proceeds only while it stays 0**, so if the GPU never parks,
  the named processes here are what is keeping it awake.
- Under fine-grained RTD3 the `device_node_holders` list is informational:
  a desktop shell holding the device open does not keep the GPU awake and
  does not block parking — which is why `total_count` can be 0 while the
  list is not empty.
- `age_s` is the sample's age in seconds. It grows while the GPU sleeps,
  because sampling only happens while the GPU is awake — and it also grows
  while an idle result is latched, because observing the GPU opens it and
  would keep resetting the driver's autosuspend timer. A growing `age_s` is
  the daemon deliberately not looking so the GPU can sleep; an unchanged
  holder set re-probes only after a bounded awake window (~5 minutes), so a
  latched holder that starts real work still re-materializes the profile.

### What the daemon itself is holding

The client lists above exclude the daemon's own process, so the `deep_sleep`
block carries a separate `daemon` object with the daemon's own GPU holds:

- `engine_attached: true` — the in-process profile engine is applied and
  polling the driver. This alone keeps the GPU awake; it is exactly the hold
  the park countdown removes.
- `rpc_backends` — lazily-opened GPU backends still serving socket queries
  (telemetry, capabilities). A steadily nonzero value means some client —
  typically an open PenguinBurner GUI polling telemetry — queries faster
  than the 30-second idle sweep and holds the GPU awake through the daemon,
  even though `gpu_clients` shows no counted process. Close the GUI when
  testing suspend.

### Journal events

The daemon writes every deep-sleep decision edge to its journal. All lines
are change-triggered — process starts/exits, kernel state transitions,
countdown edges — never per-tick, so an idle system stays quiet and the log
reads as a timeline. A full park-then-wake cycle looks like this:

```
deep sleep: gpu clients (nvml-contexts): counted=0 (idle), graphics=0, compute=0, ignored infrastructure helpers=1 [880 nvidia-powerd], device-node holders=3 [880 nvidia-powerd, 1450 plasmashell, 1721 kwin_wayland]
deep sleep: no GPU clients; parking the runtime in 59s unless one appears
deep sleep: no GPU clients for the idle window; parking the runtime
deep sleep: persisted runtime deferred until the GPU is in use
deep sleep: runtime parked; the GPU may suspend until its next real use
deep sleep: runtime_status active -> suspended (active for 312.4s)
deep sleep: runtime_status suspended -> active (suspended for 1841.7s)
deep sleep: gpu clients (nvml-contexts): counted=1 (GPU in use), graphics=1 [9314 game.exe], compute=0, ignored infrastructure helpers=1 [880 nvidia-powerd], device-node holders=4 [880 nvidia-powerd, 1450 plasmashell, 1721 kwin_wayland, 9314 game.exe]
deep sleep: GPU is in use; starting the deferred persisted runtime
deep sleep: runtime deferral cleared
deep sleep: parked runtime re-materialized
```

Line by line:

- **`gpu clients (...)`** — the client sample, logged whenever it changes.
  `counted` is the number the park/wake decision used (`(GPU in use)` /
  `(idle)` is the verdict); the counted, explicitly ignored, and open-holder
  lists that follow are the complete evidence.
- **`runtime_status A -> B (A for Ns)`** — the kernel's own view of the
  GPU's power state, with how long the previous state was held. This is the
  ground truth that parking actually led to a suspend, and it timestamps
  every wake. Watcher context probes have already closed before their client
  decision is logged. A separate `released N idle GPU backend(s)` line refers
  only to a one-off socket RPC backend reaching its 30-second idle TTL.
- **`no GPU clients; parking the runtime in Ns unless one appears`** — the
  park countdown started; N is the remaining time, so the park line lands N
  seconds after this one. If a client shows up first you get
  `park countdown reset after Ns: a GPU client appeared` and the sample
  line names who; if the countdown's preconditions vanish instead (a scan
  starts, the engine stops) you get `park countdown abandoned: <why>`. A
  park the supervisor cannot perform says
  `park refused (a game session owns the runtime or the engine did not
  stop); retrying every idle window` — once per idle stretch.
- **`GPU awake but no counted clients; keeping the runtime deferred
  (transient wake?)`** — the GPU came out of suspend without any counted
  client (a Vulkan capability probe from some app). A transient wake therefore does not always apply the profile. It is only
  logged for genuine wakes from suspend, so the normal post-park drain
  (the GPU stays `active` for the driver's autosuspend delay after the
  engine releases it) never produces it.

To watch it live while reproducing a park/wake problem:

```
sudo journalctl -u penguin-burnerd.service -f
```

To capture a log file to attach to a bug report (adjust the window to cover
your test):

```
sudo journalctl -u penguin-burnerd.service --since "-1 hour" --no-pager > penguin-burnerd-deep-sleep.log
```

```
cat /sys/bus/pci/devices/0000:01:00.0/power/runtime_status
```

(replace the address with your dGPU's; it should read `suspended` when no
game is running).

If `state` is `"desktop"` with reason `power/control is not 'auto'`, your
distribution has not enabled runtime power management for the dGPU — see the
NVIDIA driver README chapter on runtime D3 for the required udev rules and
module options.

When testing a fix, first confirm which build is actually running: the
daemon's startup journal line reads `penguin-burnerd <version> (<commit>)
starting`, and `--daemon-status` reports the same short commit as `build`.
The package version alone does not change between builds of a branch, so a
stale installed daemon can otherwise impersonate a fresh one.
