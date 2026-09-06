# Adaptive Undervolting

Adaptive switches between saved Efficiency, Balanced, and Performance profiles
to meet your game's FPS target while reducing power use when there is headroom.
Create profiles with [Auto-UV](auto-uv.md), then enable Adaptive per game in
[Game Library](game-library.md).

![Profiles with the Assign Tier menu](../assets/profiles-management.png)

## Tiers

| Tier | Goal |
| --- | --- |
| Efficiency | Best measured FPS per watt. |
| Balanced | Balance performance and power savings. |
| Performance | Higher clock target with Auto-OC. |

Auto-UV assigns tiers when saving profiles. To change one, right-click a
profile in **Profiles → Assign Tier**; **None** removes the assignment.
All three scan presets use two rising tail bins.

Adaptive needs at least two usable tiers to switch. With one tier, it runs
that profile. Verify imported or edited curves before relying on them.

## How runtime switching works

Set a game's **Auto-UV mode** to **Adaptive**. Its target defaults to the
system-wide value, initially 60 FPS. Enable **Per-game target** to override it.

- Sustained headroom lets Adaptive move toward Efficiency.
- A missed target can promote to a higher tier; a clearly severe slowdown can
  jump to the highest tier when supported by the timing evidence.
- CPU saturation can hold back promotion when more GPU clock is unlikely to help.
- A recognized frame cap lets Adaptive ease down while preserving that cadence.

Decisions use base-frame p95 timing: the slower end of recent frame times,
converted to FPS. Reflex/NVAPI markers take priority; present pacing is the
fallback. Known frame generation requires usable base timing so generated
frames do not drive promotion. See [FPS measurement details](latency-fg.md).

## Frame caps and idle periods

For a missed target with low GPU use and no CPU saturation, Adaptive waits for
three capped-looking readings before treating the game as externally capped.
It then steps down using the normal demotion delays. The cap reference is
released if pacing changes beyond its tolerance, its timing source changes,
or GPU use becomes high.

With no frame telemetry and GPU use at or below 20%, Adaptive can ease down
after 60 idle seconds. Missing utilization data holds the tier. New frames
clear idle history, so a new session is judged on its own measurements.

Promotion and demotion confirmation use elapsed time rather than overlay
refresh ticks. Longer waits between demotions prevent rapid switching.

## One-word tuning: responsiveness

Most users should keep the defaults. For faster or calmer reactions, use
`PENGUIN_BURNER_ADAPTIVE_RESPONSIVENESS=eager` or `relaxed`.
See the [tuning reference](adaptive-tuning.md) for all thresholds and timing
settings, including the exact meaning of confirmation windows.

## CLI

```bash
pburn-cli --daemonize --adaptive-auto-uv
pburn-cli --assign-auto-uv-tier <profile-id> efficiency
```

Use `balanced`, `performance`, or `none` for other assignments. Profile IDs
come from `pburn-cli --list-auto-uv-profiles`.

The system-wide target is `[adaptive] target_fps` in runtime configuration,
overridden by `PENGUIN_BURNER_ADAPTIVE_TARGET_FPS`. The Overlay tab's
**Target FPS** control shows the same setting.
