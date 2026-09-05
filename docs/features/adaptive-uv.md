# Adaptive Undervolting

> Feature guide — see the [README](../../README.md) for the project overview and
> [auto-uv.md](./auto-uv.md) for how the underlying profiles are produced.

Adaptive Undervolting is PenguinBurner's standout runtime feature. Instead of
locking your GPU to a single saved curve, it keeps several saved Auto-UV
profiles on hand, assigns each one a **tier**, and lets the daemon switch
between tiers automatically based on how the game is actually pacing frames.

![Profiles with the Assign Tier menu](../assets/profiles-management.png)

## Tiers

Every saved profile can be tagged with one of three tiers from the **Profiles**
tab (right-click a profile → **Assign Tier**) or from the CLI:

| Tier | Intent | Auto-UV tail-rise bins |
| --- | --- | --- |
| **Efficiency** | Lowest practical power, flat post-lock curve | `0` |
| **Balanced** | Moderate clock tail | `≥ 4` |
| **Performance** | Preserve clock, highest headroom | `≥ 6` |

Choose **None** to remove a tier assignment. The tier of a freshly generated
profile is inferred from the scan's performance-bias preset, so a normal
Efficiency / Balanced / Performance scan already lands in the matching tier.

CLI tier assignment uses profile ids from `--list-auto-uv-profiles`:

```bash
./penguin_burner.sh --assign-auto-uv-tier <eff-profile-id> efficiency
./penguin_burner.sh --assign-auto-uv-tier <bal-profile-id> balanced
./penguin_burner.sh --assign-auto-uv-tier <perf-profile-id> performance
./penguin_burner.sh --assign-auto-uv-tier <profile-id> none
```

## How runtime switching works

Enable it per game from the **Steam** tab (set the game's mode to
**Adaptive**), or on the command line with `--adaptive-auto-uv` in
runtime/daemon mode:

```bash
./penguin_burner.sh --daemonize --adaptive-auto-uv
```

For persistent boot autostart (full service install, root required):

```bash
sudo ./penguin_burner.sh --install-systemd-service --adaptive-auto-uv
```

PenguinBurner watches the **base present-frame p95 pacing** and compares it to a
target FPS. When frames are comfortably ahead of target it shifts toward a more
efficient tier (less power, quieter); when pacing falls behind it shifts toward
a higher tier to protect frame rate.

With frame generation enabled, promotion still follows the base render rate.
The median of displayed frames can include generated frames, so it is used
only for comparisons with the same measurement over time.

Reflex/NVAPI base-frame markers take priority when available. Otherwise,
Adaptive uses measured present-frame pacing from the first complete sample
window, at any framerate. A high FPS reading alone does not mean frame
generation is active and does not prevent Adaptive from stepping down.
If telemetry explicitly reports generated frames, Adaptive requires base-frame
timing or a recoverable base cadence before using that output as real FPS.

The target defaults to **60 FPS**. Override it for the service through the
environment variable (30, 50, 60, 120, etc.):

```bash
PENGUIN_BURNER_ADAPTIVE_TARGET_FPS=120
```

The same value can be set under `[adaptive] target_fps` in the runtime config
file, and the **Target FPS** control in the Overlay tab reflects it.

## When the frame rate is capped from outside

A menu locked at 60 FPS, vsync, or an in-game limiter holds the frame rate at
a number the GPU is not working for. Read only as "slower than target", that
looks identical to being short of clock — so with a target above the cap,
PenguinBurner used to climb to the top tier and stay there for the whole menu,
burning power for frames the cap would never let through.

PenguinBurner now checks whether the GPU is actually the limiter. When it is
loafing (utilisation averaged over ~8 s at or below **60%**) and the CPU is not
saturated either, nothing about the frame rate is a clock problem. One such
reading is a hint, not a diagnosis, so recognition waits for **3** consecutive
capped-looking readings — the tier is simply held while they accumulate.
Once confirmed, instead of promoting, the tier eases *down* using the same
windows and dwell as a normal comfort demotion, so the cap is held at the
cheapest tier that can hold it.

Utilisation only *starts* that recognition; it cannot end it. Each step down
makes the card work harder for the same capped frame rate, so utilisation
climbs — and judging it every tick would cancel the recognition that caused the
step, tier down and straight back up, forever. Once a cap is recognised it is
held against the pacing measured at the time, which a cap keeps steady no
matter which tier runs. The tier takes over again only when pacing actually
degrades past that reference (by more than **15%**), or when the card is flat
out (**90%** or more).

Leaving the menu reverses it: the GPU wakes up, and once the old samples age
out of the utilisation window the usual ladder applies — a clearly missed
target jumps straight to the top tier in a single window.

Releasing that latch is not on its own a reason to climb. "This reference
stopped explaining the pacing" and "the tier is the limit now" are different
claims, and only the second justifies more clock, so the loafing test runs again
on the tick a latch drops. Without it a cap released by a pacing spike promoted
straight to the top tier with the card at 58% — and a promotion takes one
window while every step back down pays its dwell.

A saturated CPU is deliberately left to its own, gentler rule: it caps the
promotion rather than stepping the tier down.

A game that starts is always judged on its own readings. Frames arriving after
a quiet stretch discard the utilisation window and every counter the previous
session accumulated — without that, the desktop's near-zero readings made the
first seconds of a launch look "capped" and held the low tier exactly when the
game needed a promotion.

Pending checks of whether a promotion improved pacing are discarded when
frame telemetry stops or the system resumes. A later session's frame rate
cannot establish whether an earlier promotion helped.

## When nothing is being played

Adaptive paces on frames, so with nothing presenting it has nothing to judge and
used to hold whichever tier the last game left behind — a tuned card sitting on
Performance for as long as the desktop stayed idle.

An idle desktop and a game we cannot measure look identical from the policy's
seat: neither reports frames. Utilisation separates them, and not narrowly. An
idle desktop with a compositor and a browser measured 3-4% here, while a game
runs 50-90% even with an external cap holding its frame rate down. The bar sits
at **20%** — roughly five times the desktop and well under half of anything
being played.

Entry is slow and exit immediate. Easing down a minute late at the desktop costs
nothing; leaving a game on a low tier costs frames in the first seconds of play,
which is when the tool is judged. Frames arriving drop the countdown outright,
so a game never pays for the desktop it interrupted. With no utilisation reading
at all the tier is held rather than lowered: absence of a measurement is not
evidence of an idle machine.

## One-word tuning: responsiveness

Most tuning wishes are just "react faster" or "be calmer", so one variable
covers them:

```bash
PENGUIN_BURNER_ADAPTIVE_RESPONSIVENESS=eager    # react in half the time
PENGUIN_BURNER_ADAPTIVE_RESPONSIVENESS=relaxed  # twice the patience
```

It scales every windows and dwell knob together — the promotion windows, the
demotion windows and dwells, the cap confirm streak, and the idle delay. The
utilisation bars and the pacing slack are judgements about a reading, not
about how long to wait, and stay put. Any knob from the tables below that you
set individually overrides its scaled value. Unset (or `normal`) changes
nothing.

## Tuning the frame-cap and idle rules

Every knob reads the same way: built-in default, then the runtime spec, then
the environment override. All are optional — the defaults are the tested
configuration.

| What you are changing | Default | Environment override |
| --- | --- | --- |
| GPU busy% at or below which a missed target counts as an external cap rather than a clock problem | 60 | `PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_ENTER_GPU_PCT` |
| GPU busy% at which the card is flat out again and a recognised cap is dropped | 90 | `PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_EXIT_GPU_PCT` |
| Consecutive capped-looking readings before the tier starts easing down | 3 | `PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_CONFIRM_WINDOWS` |
| How much worse (%) frametime may get before the cap is considered gone | 15 | `PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_EXIT_PACING_PCT` |
| GPU busy% at or below which a desktop with no game presenting counts as idle | 20 | `PENGUIN_BURNER_ADAPTIVE_DESKTOP_IDLE_GPU_PCT` |
| Seconds the desktop must stay idle before the tier eases down | 60 | `PENGUIN_BURNER_ADAPTIVE_DESKTOP_IDLE_AFTER_S` |

For example, a stricter cap recognition with a faster idle ease-down:

```bash
PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_ENTER_GPU_PCT=40
PENGUIN_BURNER_ADAPTIVE_DESKTOP_IDLE_GPU_PCT=12
PENGUIN_BURNER_ADAPTIVE_DESKTOP_IDLE_AFTER_S=30
```

The three GPU busy% bars are ordered, and the daemon enforces it: idle strictly
below cap enter, cap enter strictly below cap exit. An idle bar at or above cap
enter would call a card working under a frame cap "doing nothing" and throttle
a session being played. An exit bar at or below cap enter would let each
demotion cancel the recognition that caused it — the oscillation the latch
exists to prevent. A runtime violating either is refused with a clear error at
apply time rather than quietly clamped.

The demotion cadence under a cap is the ordinary comfort one, tuned by the
ladder knobs below.

## The ladder knobs

These predate the frame-cap rules and tune the ordinary promote/demote ladder.
A *reading* is one poll tick (roughly every 1-2 s); a *window* is one
consecutive such reading.

| What you are changing | Default | Environment override |
| --- | --- | --- |
| Consecutive slightly-behind-target readings before promoting one tier | 3 | `PENGUIN_BURNER_ADAPTIVE_TARGET_SLOW_WINDOWS` |
| Consecutive clearly-slow readings before promoting one tier | 2 | `PENGUIN_BURNER_ADAPTIVE_NEAR_SLOW_WINDOWS` |
| Consecutive comfortable readings before demoting one tier | 6 | `PENGUIN_BURNER_ADAPTIVE_COMFORT_WINDOWS` |
| Same, when demoting off the Performance tier | 10 | `PENGUIN_BURNER_ADAPTIVE_PERFORMANCE_COMFORT_WINDOWS` |
| Minimum seconds between demotions | 60 | `PENGUIN_BURNER_ADAPTIVE_DEMOTE_DWELL_S` |
| Same, when demoting off the Performance tier | 45 | `PENGUIN_BURNER_ADAPTIVE_PERFORMANCE_DEMOTE_DWELL_S` |
| GPU busy% at or below which a promotion to Performance can be held back as CPU-bound | 60 | `PENGUIN_BURNER_ADAPTIVE_CPU_BOUND_GPU_UTIL_MAX` |
| Busiest-thread CPU% at or above which the game counts as CPU-bound | 97 | `PENGUIN_BURNER_ADAPTIVE_CPU_BOUND_PEAK_THREAD_MIN` |
| Whole-process CPU% at or above which the game counts as CPU-bound | 60 | `PENGUIN_BURNER_ADAPTIVE_CPU_BOUND_PROCESS_UTIL_MIN` |

Promotions on a badly missed target are immediate and are not windowed —
protecting frame rate never waits.

## Example

Say you keep three profiles (Efficiency, Balanced, Performance) and a 60 FPS
target:

- In a menu or a light scene, frames sit well above 60, so PenguinBurner runs
  the **Efficiency** profile: lowest power, quietest fans.
- The action picks up and frames start hovering near 60, so it moves to
  **Balanced** to hold the target.
- A heavy effect pushes frames below 60, so it jumps to **Performance** for the
  extra clock until the scene clears, then eases back down.

You set it once and play; the tier follows the frame rate.

## Requirements and safety

- Adaptive UV needs **at least two usable profile tiers** to have anything to
  switch between. With a single tier it simply runs that one profile.
- Deleting a profile that would drop you below two usable tiers triggers a
  warning, because it disables adaptive switching for the autostart entry.
- All per-tier curves still went through Auto-UV's stability and final
  verification before they could be saved, so every tier the daemon can pick is
  a previously verified curve.
