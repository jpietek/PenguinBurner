# Adaptive Tuning Reference

[Back to Adaptive Undervolting](adaptive-uv.md)

## Responsiveness

Set `PENGUIN_BURNER_ADAPTIVE_RESPONSIVENESS=eager` for shorter waits,
`relaxed` for longer waits, or `normal` for defaults. This scales confirmation
settings and dwell times, including cap recognition and desktop idle delay.
It leaves utilization thresholds and pacing tolerances unchanged.
An individual override takes precedence over its scaled default.

## Tuning the frame-cap and idle rules

Settings use built-in defaults, then the runtime spec, then environment
overrides. Leave them unchanged unless you need different behavior.

| What you are changing | Default | Environment override |
| --- | --- | --- |
| Maximum GPU use (%) for frame-cap recognition | 60 | `PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_ENTER_GPU_PCT` |
| GPU use (%) that releases a recognized cap | 90 | `PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_EXIT_GPU_PCT` |
| Consecutive readings required to recognize a cap | 3 | `PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_CONFIRM_WINDOWS` |
| Allowed pacing change (%) before releasing a recognized cap | 15 | `PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_EXIT_PACING_PCT` |
| Maximum GPU use (%) for desktop idle | 20 | `PENGUIN_BURNER_ADAPTIVE_DESKTOP_IDLE_GPU_PCT` |
| Idle delay before lowering a tier (seconds) | 60 | `PENGUIN_BURNER_ADAPTIVE_DESKTOP_IDLE_AFTER_S` |

For example, a stricter cap recognition with a faster idle ease-down:

```bash
PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_ENTER_GPU_PCT=40
PENGUIN_BURNER_ADAPTIVE_DESKTOP_IDLE_GPU_PCT=12
PENGUIN_BURNER_ADAPTIVE_DESKTOP_IDLE_AFTER_S=30
```

Thresholds must satisfy `desktop idle < cap enter < cap exit`; the daemon
rejects invalid ordering. Cap demotion uses the same timing as ordinary demotion.

## Promotion and demotion

Promotion and demotion use elapsed time, independent of overlay refresh.
For these settings, `N` windows means `(N − 1) × 3` seconds after the first
qualifying sample: 3 windows requires 6 more seconds. Frame-cap recognition's
`CONFIRM_WINDOWS` setting above remains a count of consecutive decision ticks.

| What you are changing | Default | Environment override |
| --- | --- | --- |
| Windows slightly behind target before promoting one tier | 3 | `PENGUIN_BURNER_ADAPTIVE_TARGET_SLOW_WINDOWS` |
| Windows clearly below target before promoting one tier | 2 | `PENGUIN_BURNER_ADAPTIVE_NEAR_SLOW_WINDOWS` |
| Comfortable windows before demoting one tier | 6 | `PENGUIN_BURNER_ADAPTIVE_COMFORT_WINDOWS` |
| Same, when demoting off the Performance tier | 10 | `PENGUIN_BURNER_ADAPTIVE_PERFORMANCE_COMFORT_WINDOWS` |
| Minimum seconds between demotions | 60 | `PENGUIN_BURNER_ADAPTIVE_DEMOTE_DWELL_S` |
| Same, when demoting off the Performance tier | 45 | `PENGUIN_BURNER_ADAPTIVE_PERFORMANCE_DEMOTE_DWELL_S` |
| Maximum GPU use (%) for the CPU-bound guard | 60 | `PENGUIN_BURNER_ADAPTIVE_CPU_BOUND_GPU_UTIL_MAX` |
| Minimum busiest-thread CPU use (%) for the CPU-bound guard | 97 | `PENGUIN_BURNER_ADAPTIVE_CPU_BOUND_PEAK_THREAD_MIN` |
| Minimum process CPU use (%) for the CPU-bound guard | 60 | `PENGUIN_BURNER_ADAPTIVE_CPU_BOUND_PROCESS_UTIL_MIN` |

A badly missed target can jump directly to the highest tier when the timing
evidence supports it and CPU-bound or frame-cap guards do not hold it back.
