# Game Perf Profile

> Feature guide — see the [README](../../README.md) for the project overview and
> [overlay.md](./overlay.md) for the in-game overlay the same telemetry feeds.

Game Perf Profile is a per-game telemetry fingerprint. While a PenguinBurner-enabled
game runs, the daemon records a rolling **30-minute window** of everything it
already measures — every rendered frame's frametime plus a per-second snapshot
of clock, memory clock, voltage, power, GPU/CPU load, fan, temperature,
undervolt offset, FPS, latency, and the active profile tier. The window is
summarized into a five-spoke radar you learn to recognize at a glance: how
demanding a game is, where its bottleneck lives, and how fluent its
**pre-frame-generation** frames really were.

## Where it appears

- **Game Library tab** — a **Perf Profile** button sits beside **Play** in the
  header of the selected game, whichever launcher it comes from (Steam, Lutris,
  Heroic, Faugus). **Hover** it for a quick peek (fingerprint, median and 1%-low
  frametimes, power draw, latency); **click** it to open the full detail. Until
  the game has recorded telemetry the button is greyed out and its tooltip says
  how to record some. Each game has a
  **Capture telemetry** switch under *In game* (on by default); turning it off
  stops new capture from the next launch.
- **Game Perf Profile tab** — the full view: the large fingerprint, a stat row, and a
  MangoHUD-style **frametime graph** in milliseconds (30-minute overview with
  red stutter needles, or a **LIVE 10 s** zoom at per-frame resolution).

A game needs at least **5 minutes** of captured play before it earns a
fingerprint; until then the fingerprint is a dashed "warming up" outline, while
the stat row and frametime graph already show what was recorded. A session that
is still running draws its fingerprint early and labels it as settling.

Frametimes are **pre-frame-generation** when the game sends base-frame markers.
Menus and loading screens usually send none, so for those stretches the graph
and stats fall back to the raw present cadence — the frames that actually
reached the screen.

## The five spokes

| Spoke | Meaning | Normalized against | Reads | Fine band |
| --- | --- | --- | --- | --- |
| **PWR** | median board power | the card's power limit | ↓ lower is cheaper | under 60 % |
| **GPU** | median GPU load | 100 % | — | none |
| **CPU** | median hottest-thread load | 100 % | ↓ lower is cheaper | under 80 % |
| **FPS** | median rendered FPS | the game's target FPS | ↑ higher is better | 95 % of target or more |
| **LOW** | 1%-low ÷ median (consistency) | 1.0 | ↑ higher is better | 85 % or more |

In the large fingerprint each vertex dot is green when the value sits inside
its spoke's fine band and amber outside it. The rings
mark 25, 50, 75 and 100 % of the spoke scale and sit exactly where those values
land. FPS and LOW belong at the outer ring; PWR and CPU are costs, so closer to
the center means more headroom. GPU load has no band: high load is a
bottleneck when FPS misses its target and simply full use when it does not, so
its dot stays neutral.

Latency has no spoke: it is not reliably measurable for games without Reflex
markers, so it never shapes the fingerprint. When the overlay has a latency
reading it appears as a **LATENCY** number in the tab's stat row: the session
median of the same average the in-game `LAT` shows. Like `LAT` it carries a
`~` when any of the session's readings was an estimate rather than an
input-to-GPU measurement, as for games without Reflex markers (see
[Latency and frame-generation FPS](latency-fg.md)). The 1%-low consistency
figure is likewise textual (**1%-LOW** in the stat row) rather than a spoke.

Read the spokes, not the blob's area — the shape is the signature. A fat kite
leaning into PWR/GPU is a heavyweight; a tall CPU spike with a small GPU spoke
is a CPU-bound title; full FPS/LOW spokes with everything else tiny is a light,
buttery game.

## How the data is stored

Recording runs while the game's per-game PenguinBurner profile is active (a
wrapped game whose mode is not *Default*), because that is when the daemon
watches the game. A game launched unwrapped or in *Default* mode records
nothing. The daemon writes one ring file per running session under
`/run/penguin-burner/frame-history/` (about 0.3–0.5 MB) and, when the game
exits, archives the window trimmed to
`/var/lib/penguin-burner/frame-history/<game>.ring` — typically well under
0.5 MB per game. `<game>` is the game's `launcher:id` key with unsafe
characters escaped, e.g. `steam%3A620.ring` or `heroic%3AFall%20Guys.ring`. Frametimes are stored log-companded at one byte per frame
(~2 % resolution, 1–250 ms range); exact per-second p50/p99/p99.9 percentiles
preserve the stutter tail. Only the newest window is kept — Game Perf Profile never
accumulates lifetime history.

Ring files belong to the user who ran the game and are readable only by them.
The archive directory cannot be listed, so other local users cannot see which
games were played.
