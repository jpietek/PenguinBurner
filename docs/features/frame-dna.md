# Frame DNA

> Feature guide — see the [README](../../README.md) for the project overview and
> [overlay.md](./overlay.md) for the in-game overlay the same telemetry feeds.

Frame DNA is a per-game telemetry fingerprint. While a PenguinBurner-enabled
game runs, the daemon records a rolling **30-minute window** of everything it
already measures — every rendered frame's frametime plus a per-second snapshot
of clock, memory clock, voltage, power, GPU/CPU load, fan, temperature,
undervolt offset, FPS, latency, and the active profile tier. The window is
summarized into a five-spoke radar you learn to recognize at a glance: how
demanding a game is, where its bottleneck lives, and how fluent its
**pre-frame-generation** frames really were.

## Where it appears

- **Steam tab** — a small fingerprint badge sits next to the **Play** button
  for the selected game. **Hover** it for a quick peek (median and 1%-low
  frametimes, power, bottleneck); **click** it to open the full detail.
- **Frame DNA tab** — the full view: the large fingerprint, a stat row, and a
  MangoHUD-style **frametime graph** in milliseconds (30-minute overview with
  red stutter needles, or a **LIVE 10 s** zoom at per-frame resolution).

A game needs at least **5 minutes** of captured play before it earns a
fingerprint; until then the badge shows a dashed "warming up" outline.

## The five spokes

| Spoke | Meaning | Normalized against |
| --- | --- | --- |
| **PWR** | median board power | the card's power limit |
| **GPU** | median GPU load | 100 % |
| **CPU** | median hottest-thread load | 100 % |
| **FPS** | median rendered FPS | the game's target FPS |
| **LOW** | 1%-low ÷ median (consistency) | 1.0 |

Latency has no spoke: it is not reliably measurable for games without Reflex
markers, so it never shapes the fingerprint. When marker latency exists it
appears as a **LATENCY** number in the tab's stat row; the 1%-low consistency
figure is likewise textual (**1%-LOW** in the stat row) rather than a spoke.

Read the spokes, not the blob's area — the shape is the signature. A fat kite
leaning into PWR/GPU is a heavyweight; a tall CPU spike with a small GPU spoke
is a CPU-bound title; full FPS/LOW spokes with everything else tiny is a light,
buttery game.

## How the data is stored

The daemon writes one ring file per running session under
`/run/penguin-burner/frame-history/` (about 0.3–0.5 MB) and, when the game
exits, archives the window trimmed to
`/var/lib/penguin-burner/frame-history/<appid>.ring` — typically well under
0.5 MB per game. Frametimes are stored log-companded at one byte per frame
(~2 % resolution, 1–250 ms range); exact per-second p50/p99/p99.9 percentiles
preserve the stutter tail. Only the newest window is kept — Frame DNA never
accumulates lifetime history.
