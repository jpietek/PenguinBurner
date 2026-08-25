# NVAPI latency shim

> **Status 2026-07-01: VALIDATED live; merged to `main`.** The shim is the
> chosen direction. Confirmed
> end-to-end on **Talos 2** (835960) and **RE9 / Resident Evil Requiem**
> (3764200) — both DX12 + Streamline + DLSS-G frame generation, on **stock
> proton-cachyos** (no Proton/vkd3d patch).

## TL;DR

A drop-in proxy `nvapi64.dll` that gives us in-game Reflex latency markers
**without forking dxvk-nvapi** and **without its trace log**. It forwards every
NVAPI call to the real dxvk-nvapi and taps the latency-marker functions at the
NVAPI ABI — *above* vkd3d's owner-gate, so it works under frame generation where
the Vulkan layer cannot see the markers. Deployed by **fronting the prefix's
`system32\nvapi64.dll`** (generic, no per-game logic). Markers reach the existing
marker FIFO the bridge drains, so nothing downstream changed.

## Why it exists — the vkd3d owner-gate

The Vulkan layer (`overlay/native/latency_layer/`) taps `vkSetLatencyMarkerNV`,
but **markers never reach Vulkan on frame-gen titles.** The chain is:

```
game → Streamline (sl.reflex) → NvAPI_D3D_SetLatencyMarker → dxvk-nvapi
     → vkd3d d3d12_low_latency_device_SetLatencyMarker → [owner-gate] → vkSetLatencyMarkerNV
```

The gate is `third_party/vkd3d-proton/libs/vkd3d/swapchain.c` (~line 2291): when a
title has **more than one Vulkan swapchain**, vkd3d nulls its `low_latency_swapchain`
owner, and `device_vkd3d_ext.c:d3d12_low_latency_device_SetLatencyMarker` then
*drops every marker* before it becomes a Vulkan call. **DLSS-G frame generation
creates that second swapchain** (a separate presentation swapchain for the
interleaved real+generated frames), so Talos/RE9 hit the gate and the layer goes
blind. Single-swapchain titles (007 First Light, FF7 Rebirth — also Streamline+FG)
keep the owner, so the layer's tap works for them. We **cannot patch user Proton**,
so tapping above the gate at the nvapi ABI (this shim) is the only fit.

(Also: upstream PR #376 to add `DXVK_NVAPI_LATENCY_MARKER_LOG` was rejected, and
the patched-fork workaround needed rebuilding per Proton version — the shim
replaces both: build once, version-independent, no fork.)

## Architecture — a facade, not a build of nvapi

`nvapi64.dll` exports exactly one symbol, `nvapi_QueryInterface(id)`. The shim
re-exports only that and implements **zero** NVAPI functions:

```
nvapi_QueryInterface(id):
    real = real_dxvk_nvapi.nvapi_QueryInterface(id)
    switch id:
        SetLatencyMarker (0xd9984c05)          -> wrap (emit marker, tail-call real)
        D3D12_SetAsyncFrameMarker (0x13c98f73) -> wrap (emit marker, tail-call real)
        GetLatency (0x1a587f9c)                -> pass-through placeholder
        SetSleepMode (0xac1ca9e0)              -> pass-through placeholder
        default                                -> return real
```

**ABI source of truth** (verified, and confirmed live via gdb — do NOT guess):
`/home/jp/dxvk-nvapi/external/nvapi/{nvapi.h,nvapi_interface.h}`.
`NV_LATENCY_MARKER_PARAMS_V1`: `version@0 frameID@8 markerType@16` (size 88,
version word `65624`). Marker enum: SIMULATION_START=0, PRESENT_END=5,
INPUT_SAMPLE=6, OUT_OF_BAND_PRESENT_START=11, OUT_OF_BAND_PRESENT_END=12 (the 5
the bridge consumes). Built ~250 lines, MinGW, statically linked; imports only
KERNEL32 + msvcrt — it does no NVIDIA work.

### Defensive contract (it runs inside the game — must never crash/corrupt it)

- Every forward pointer is null-guarded; if the real dxvk-nvapi can't load,
  `QueryInterface` returns `nullptr` and wrappers return a benign status.
- Caller marker structs are read only after `p != null && !IsBadReadPtr(p, 24)`.
- `snprintf` return is clamped so `emit_raw` can't over-read the buffer.
- `QueryPerformanceFrequency==0` is guarded (no div-by-zero).
- It **never writes to caller/external memory** — only a local bounded buffer +
  its own fd. A genuinely wild pointer would fault in the *real* nvapi too (it
  reads the same struct), so the shim adds zero new crash surface.

## The output transport — the bug that cost the most time

The shim's wrapper **was being called all along** (gdb: Streamline's
`sl.common.dll` calls `wrap_set_latency_marker` directly with a valid
`SIMULATION_START`). The failure was purely *output*: in a GUI-subsystem game
process **msvcrt's std fd 2 is not a writable handle for a loaded DLL**, so
writing markers to stderr/fd 2 (`WriteFile(GetStdHandle)`, `fwrite(stderr)`,
`_write(2)`) silently returns `EBADF` with **no syscall** — markers vanished and
it looked like a bypass.

**Fix:** the launcher hands the shim the marker FIFO's **wine path** via
`PENGUIN_BURNER_SHIM_OUTPUT` (`_fifo_wine_path`), and the shim `_open`s a fresh,
valid fd and writes markers there directly — no dependence on the broken fd 2.
Markers reach the same `nvapi-trace.fifo` the bridge drains.

## Deployment — generic system32 fronting + re-front watcher

`overlay/shim_deploy.py:deploy_nvapi_shim(env)` fronts the running prefix's
`system32\nvapi64.dll`: parks the real dxvk-nvapi as `nvapi64-pb.dll`, drops
the shim as `nvapi64.dll`. The shim's
`load_real()` loads `system32\nvapi64-pb.dll` (or `PENGUIN_BURNER_SHIM_REAL`).
Generic because every nvapi64-loading process (bootstrappers, UE shipping exes,
Streamline's `sl.interposer` which `GetSystemDirectory`-loads nvapi64) resolves
from system32.

The prefix is found from `STEAM_COMPAT_DATA_PATH` (Steam, with the prefix under
`pfx/`) or `WINEPREFIX` (Lutris and anything else driving wine directly, with
`drive_c` at the top; umu additionally symlinks `pfx -> .`). Both roots are
tried in that order, and both layouts under each. Reading only the Steam
variable meant the shim never reached a Lutris prefix — and with no markers,
adaptive gets no pacing at all and holds whatever tier it started on.

For Lutris, set **Game → Configure → System options → Command prefix** to
`PENGUIN_BURNER --pb-overlay=1`. The launcher supplies `WINEPREFIX`, so no Steam
game identity or library discovery is required.

**Proton clobbers the shim every launch**: prefix setup unconditionally
`try_copy`s the bundled dxvk-nvapi over `system32\nvapi64.dll` (`os.remove` +
copy, no content check — the `if use_nvapi:` block in the proton script's
`setup_prefix`), *after* the wrapper's pre-exec deploy but *before* the game
loads it. So `spawn_refront_watcher` launches a detached watcher that holds an
**inotify watch on system32** and re-fronts the shim the moment a rewrite of
`nvapi64.dll` completes (`IN_CLOSE_WRITE`/`IN_MOVED_TO` only — reacting to
creation could park a half-written DLL as the forward target). It runs for the
**whole Proton session** (the wrapper execs into Proton, so the watcher's parent
*is* the session; reparenting = session over), not a fixed window — a fixed 60s
missed slow first launches (prefix creation, anticheat installs) and mid-session
re-copies (compat-config changes re-run prefix setup). Falls back to 0.25s
polling if inotify is unavailable. (Idempotent/self-healing either way.)

## Cleanup — the register of fronted prefixes

Fronting swaps a DLL inside the user's own game files, so every prefix we front
has to be findable again when PenguinBurner is removed. Steam prefixes are
discoverable (`steamapps/compatdata/*`), but a Lutris, Heroic or plain-wine
prefix lives wherever the user put it. Nothing could enumerate those, so before
this they stayed fronted forever, invisibly.

`deploy_nvapi_shim` therefore records the canonical `system32` path in
`~/.config/PenguinBurner/nvapi-shim-prefixes.json` **before** touching any DLL,
and **declines to front a prefix it could not record**. Losing a session of
marker latency is recoverable; leaving a modified DLL behind with nothing able
to undo it is not. Paths are resolved before they are stored, so umu's
`pfx -> .` symlink cannot enter the register twice.

`restore_all_nvapi_shims()` — what `penguin-burner-install-wrappers --uninstall`
calls — walks the register *and* keeps the Steam sweep. Neither alone is
enough: the register names prefixes no scan could find, while the sweep still
covers prefixes fronted by a build that predates the register. Restoring a
prefix drops it from the register.

## Integration

- `launcher.py:_configure_dxvk_nvapi_marker_output` — now just: deploy the
  shim (set `SHIM_OUTPUT` to the FIFO path) → else no marker env (layer-only).
  The marker-log auto-detection and the `DXVK_NVAPI_LOG_LEVEL=trace` escape
  are both gone (the shim is universal now).
- `launcher.py:ingame_latency_enabled` — defaults **on when the overlay is
  enabled** (`PENGUIN_BURNER_OVERLAY`/`PB_OVERLAY`); opt out with
  `PENGUIN_BURNER_INGAME_LATENCY=0`.
- Marker line format (satisfies `_MARKER_LOG_RE`):
  `0.0:<pidHex>:0:latency-marker:pb:qpcUs=<us> frameID=<id> markerType=<NAME>`.
- `overlay/telemetry/nvapi_marker_bridge.py` parses the stream unchanged, but it
  now runs as a **detached per-game drainer** spawned by the wrapper (one per
  launch, own FIFO) instead of inside the app — see the freeze-hazard section.

## Shim vs Vulkan layer — complementary, not redundant

- **Layer = durable backbone (always):** draws the overlay, FPS (all games),
  display/scanout latency (`VK_KHR_present_wait`), GPU-frame timing, frame-gen
  detection — it sits at the stable Khronos Vulkan boundary, below Proton churn.
- **Shim = a targeted marker source** for the one thing the layer can't do on
  multi-swapchain FG titles. The layer's own `vkSetLatencyMarkerNV` tap still
  covers single-swapchain / native-Vulkan Reflex games. Shipped always-on (the
  shim is a transparent proxy; disable per-game with the env if anti-tamper ever
  objects — it didn't on Capcom's RE9).

## Build & packaging

- Source: `overlay/native/nvapi_shim/` (`src/nvapi_shim.cpp`, `nvapi64.def`,
  `build.sh`). Manual build: `cd overlay/native/nvapi_shim && ./build.sh` →
  `build/nvapi64.dll` (gitignored). Toolchain `x86_64-w64-mingw32-g++`, static.
- Packaging: `setup.py:_build_nvapi_shim` cross-compiles into the wheel at
  `overlay/nvapi_shim/nvapi64.dll`; `pyproject.toml` package-data; `MANIFEST.in`
  ships the source. Set `PENGUIN_BURNER_REQUIRE_NVAPI_SHIM=1` in release builds
  so a missing toolchain fails loudly instead of silently shipping without the
  DLL. The **Flatpak** builds it via the `org.freedesktop.Sdk.Extension.mingw-w64`
  SDK extension (build-time only; see the manifest). PB is a
  **non-editable wheel** at
  `~/.local/lib/python3.14/site-packages` — Python edits need a reinstall (or copy
  the changed file in); the shim DLL needs a rebuild + copy to take effect.

## Validation (done) & how to re-check

- Launch with the overlay on (`PB_OVERLAY=1`) — latency now defaults on. The
  shim deploys; `console-linux.txt` shows `nvapi shim: installed …`.
- Live signal: the game maps both `nvapi64.dll` (the 241 KB shim) and
  `nvapi64-pb.dll`, has **2 fds** to `nvapi-trace.fifo` (game stderr + the shim's
  by-path open), and the daemon reports `quality=reflex-marker-sim-present`,
  `latency-quality=sim-to-oob-present` (~68 ms under FG; Talos emits no
  `INPUT_SAMPLE`, so it's sim→present, not full input→present).
- Diagnostic file mode: set `PENGUIN_BURNER_SHIM_OUTPUT=C:/pb-shim.log` to read
  the shim's `[pb-nvapi-shim] init … real=ok` + raw markers, decoupled from the
  bridge.

## The freeze hazard (release-plan B1) — FIXED, two independent layers

The one way the shim could *stall* the game was **blocking FIFO I/O inside the
game process**: the FIFO's kernel buffer is 64 KB, and with no drainer (the app
closed — a normal state, the wrapper lives in Steam launch options) it fills in
seconds, after which the shim's blocking `_write` froze the frame loop (and any
game stderr write could do the same). Fixed on both sides:

1. **Guaranteed drainer, game-scoped (the real fix).** The wrapper spawns the
   marker bridge as a **detached per-game process**
   (`nvapi_marker_bridge.spawn_detached_drainer`, `--session-pid` +
   `--cleanup`), spawned *before* the stderr redirect so it never holds the
   FIFO's write side. Each launch gets its **own FIFO**
   (`nvapi-trace.<sessionpid>.fifo`, pinned via `PENGUIN_BURNER_MARKER_FIFO`) so
   two concurrent games never share a pipe or steal each other's lines. The
   drainer forwards samples to the latency socket as before — the app just
   receives datagrams *whenever it happens to run*; with the app closed the
   sends fail silently and the pipe still drains. Lifetime: drains as long as
   any writer holds the FIFO (a game can outlive its launch session); exits on
   EOF once the session is gone, then unlinks its FIFO. A launch that dies
   before any writer connects is reaped by a 60 s no-writer grace
   (`_NO_WRITER_GRACE_S`) — a FIFO reader gets *no* poll event until a writer
   has connected at least once. Crash leftovers are swept at the next launch
   (`launcher._sweep_stale_marker_fifos`: `ENXIO` on a non-blocking write-only
   open = no reader = stale). The detached drainer is the only FIFO reader;
   runtime telemetry is received by the Rust daemon over the latency socket.
2. **The shim can no longer block regardless (belt and braces).** `emit_raw`
   only copies the line into a fixed in-DLL ring (256×192 B) under the lock; a
   dedicated writer thread does the blocking `_write`. If the output stalls
   anyway, the ring fills and new lines are **dropped** — lost samples beat a
   frozen game — and a `[pb-nvapi-shim] output stalled: dropped N marker lines`
   diagnostic is emitted once it drains again.

### Regression check, host-only (~1 min, no game)

```bash
f=/tmp/pb-hang-test.fifo; mkfifo $f
# launcher stand-in: holds O_RDWR like the game's fd 2, never reads
python3 -c "import os,time; os.open('$f', os.O_RDWR); time.sleep(600)" &
# shim stand-in: blocking marker-sized writes, prints a counter
python3 -c "
import os
fd = os.open('$f', os.O_WRONLY)
line = b'0.0:1a2b:0:latency-marker:pb:qpcUs=1 frameID=1 markerType=SIMULATION_START\n'
i = 0
while True:
    os.write(fd, line); i += 1
    print(i, flush=True)"
```

A raw blocking writer stalls at ~840 lines (64 KB) — that demonstrates the old
hazard mechanism. With the fix, the real FIFO always has the per-game drainer
reading (start a wrapped game and `ls ~/.cache/penguin-burner/` shows
`nvapi-trace.<pid>.fifo` plus a live `nvapi_marker_bridge --log … --session-pid …`
process), and the shim's ring would drop rather than stall even if it died.

### Regression check, in-game (Talos 2 / RE9)

1. Launch with overlay + latency **while the PB app is closed** — the primary
   B1 scenario. The game must run indefinitely; markers flow into the drainer
   and are dropped at the dead socket.
2. Start the app mid-game: LAT should populate live (drainer → socket → meter).
3. Close the app mid-game: game keeps running; LAT disappears, nothing stalls.
4. Kill the drainer mid-game (`pkill -f "nvapi_marker_bridge --log"`): the game
   must keep running — the shim's ring now drops lines instead of blocking (the
   old behavior froze ~10 s after the drain stopped). Expect the
   `output stalled: dropped N` line if a drainer is later attached.
5. Exit the game: the drainer exits by itself and its `nvapi-trace.<pid>.fifo`
   is gone.

## Env vars

- `PENGUIN_BURNER_INGAME_LATENCY` (alias `PB_INGAME_LATENCY`) — `0` opts out;
  unset defaults to the overlay-enabled state.
- `PENGUIN_BURNER_NVAPI_LATENCY_DISABLE=1` — skip NVAPI latency capture
  (layer-only). Stops re-deploying the shim; does not un-front an
  already-installed shim.
- `PENGUIN_BURNER_NVAPI_SHIM_DIR` — override the shim artifact path.
- `PENGUIN_BURNER_NVAPI_SHIM_WATCH_SECONDS` — optional hard cap on the re-front
  watcher's runtime; unset it runs for the whole Proton session.
- `PENGUIN_BURNER_SHIM_REAL` — override the forward-target DLL.
- `PENGUIN_BURNER_SHIM_OUTPUT` — set by the launcher to the FIFO wine path (the
  transport); override to a real file for diagnostics.
- `PENGUIN_BURNER_MARKER_FIFO` — set by the launcher: this launch's marker FIFO
  path (`nvapi-trace.<sessionpid>.fifo`), shared by the stderr route, the shim
  wine path and the detached drainer.

## Remaining / enhancements

- **GetLatency harvest**: the `GetLatency` wrapper is a pass-through placeholder;
  it could actively pull the driver frame-report ring (more timing tiers).
- **32-bit titles** would need a second proxy (`nvapi.dll` in syswow64).
- **Marker double-count**: on single-swapchain titles both the layer and shim
  capture the same frames' markers; benign (meter takes best quality), gate later
  if it ever shows.

## Key files

- `overlay/native/nvapi_shim/src/nvapi_shim.cpp` — the proxy DLL + hardening.
- `overlay/shim_deploy.py` — deploy + `spawn_refront_watcher`.
- `overlay/launcher.py` — selector, `_fifo_wine_path`, default-on policy.
- `overlay/telemetry/nvapi_marker_bridge.py` — marker FIFO consumer.
- `tests/test_shim_deploy.py`, `tests/test_pb_overlay_launcher.py` — tests.
- `memory/nvapi-shim-marker-source.md`, `memory/balanced-clock-drop-floor.md`.
