# Laptop Deep Sleep

On hybrid laptops, the NVIDIA GPU can power down when idle using runtime D3
(RTD3 / D3cold). PenguinBurner releases idle GPU handles so its daemon can stay
running without keeping the card awake.

The NVIDIA driver and kernel must already support and enable runtime power
management. PenguinBurner does not enable RTD3 itself.

## How it works

On supported laptops, the daemon defers a saved profile while the GPU sleeps.
It applies the profile when sustained GPU activity and a real GPU client are
detected. After 60 seconds without a counted client, it parks the runtime,
releases GPU handles, returns fans to hardware control, and releases the clock
lock. The profile remains saved for the next workload.

Desktop GPUs keep their normal always-attached runtime. Detection uses the
selected GPU's kernel power-management state. See the
[diagnostic reference](deep-sleep-diagnostics.md#how-it-works) for detection
rules and fine-grained versus coarse-grained RTD3.

## Checking it on your machine

```bash
pburn-cli --daemon-status
```

Look for these fields under `deep_sleep`:

| Field | Meaning |
| --- | --- |
| `state: mobile` | The daemon uses laptop power-management behavior. |
| `runtime_status: suspended` | The kernel reports the GPU asleep. |
| `parked: true` | The profile runtime has released its GPU handles. |
| `autostart_deferred: true` | The saved profile waits for GPU use. |
| `gpu_clients` | Processes counted as GPU users. |
| `daemon` | GPU handles still held by the daemon itself. |

Close telemetry windows while testing; frequent queries can keep the GPU awake.
For client lists, journal examples, and bug-report commands, see
[deep-sleep diagnostics](deep-sleep-diagnostics.md#checking-it-on-your-machine).

## Applied profiles and deep sleep coexist

A parked tuned profile reapplies when real GPU use resumes. A parked stock
runtime has nothing to enforce and stays detached. After a scan or verification,
profile application waits for real GPU use so the idle card can sleep.

## Current limitations

A runtime you explicitly stop stays stopped. If upgrading from a version that
enabled persistence mode, reboot once before testing: NVIDIA persistence state
can survive a service restart.
