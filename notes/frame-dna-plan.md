# Frame DNA — per-game telemetry fingerprint: implementation plan

**Status:** Stages 0–2 implemented on `steam_tweaks` (format contract + reader, hybrid UI,
burnerd recorder with per-app archival); Stage 3 live validation in progress. Two decisions
changed during implementation, superseding §2 where they conflict: **the daemon (not Python)
archives** finished sessions to `/var/lib/penguin-burner/frame-history/<app_id>.ring`
(uncompressed, trimmed — no new Rust deps), and the archive carries the app_id because the
supervisor's game watch already knows it (no pid→app_id mapping needed).
**Interactive mockup (approved UX):** https://claude.ai/code/artifact/986339c0-6fa0-4845-9cf7-c3746b6dbc35
**Owner surfaces:** `burnerd/` (recorder), `runtime/` (reader/archive), `ui/` (badge, peek, tab).

---

## 1. Summary

Every *played* Steam game gets a **telemetry fingerprint** ("Frame DNA") built purely from our own
telemetry over a rolling **30-minute** window: a six-spoke radar that says how demanding a game is,
where its bottleneck lives, and how fluent its **pre-frame-gen** frames were. A small badge sits in
the Steam tab's game header next to Play; hovering peeks the numbers; a dedicated **Frame DNA tab**
shows the full detail: a MangoHUD-style **frametime graph** (ms) and an **operating orbit**
(power × clock, colored by active profile tier).

The privileged Rust daemon `burnerd` records; the Qt UI only reads.

## 2. Locked decisions

1. **Placement — hybrid.** Small DNA badge in the Steam details header (next to title/Play).
   Hover → compact peek (rich tooltip: median / 1%-low / power / bottleneck + "click to open").
   Click → dedicated **Frame DNA tab** with the full detail. Everything-on-hover was mocked and
   rejected (too large). No DNA icons on library rows — rows already show Steam game icons.
2. **Fingerprint axes (6):** PWR, GPU, CPU, FPS, LOW, LAT (§4). Power only for demand — clock is
   recorded but not a spoke (it tracks power on the stock V/F curve; it earns its keep in the orbit).
   No thermal spoke (thermals recorded, not drawn).
3. **Frametime view — both:** densified 30-min overview *and* zoom-to-live (last ~10 s, per-frame).
4. **Frame entry encoding — log-companded u8:** `ft_ms = 2^(v/32)`, range 1–250 ms, ~2.2 % relative
   resolution everywhere (no banding at high fps, hitches not clamped at 64 ms).
5. **Metrics record — 36 B self-timed:** carries `t_rel` and `frame_count`, so the format is
   gap-proof (pauses, slow engine ticks) and frames align exactly to wall time.
6. **Live ring keyed by pid** in `/run/penguin-burner/frame-history/<pid>.ring` (mode 0644).
   The daemon needs zero Steam knowledge; two simultaneous games just work.
7. **Persistence is Python-side, per-user:** on game exit, archive to
   `${XDG_DATA_HOME:-~/.local/share}/penguin-burner/frame-history/<app_id>.ring.gz` (stdlib gzip).
   No root writes, no /var/lib, no zstd dependency. Daemon only GC's dead-pid rings.
8. **pid→app_id resolved in Python** — the mapping already exists:
   `SteamIntegrationManager._watched_running_app_ids()` reads
   `daemon_status()["game_runtime"]["watched"]`, whose entries carry both `app_id` and pid
   (see `hot_reapply`, `integrations/steam/manager.py:464-514` uses `watch_pid`).
9. **Qualify gate 5 min; window 30 min; metrics cadence 1 Hz.** Below the gate: "warming up".
10. **No cross-game plots. No cold-start estimates** (ProtonDB is a separate feature, out of scope).
11. **Build order — viz-first:** Stage 0 (format + synthetic generator) → Stage 1 (Qt UI against
    synthetic rings) → Stage 2 (Rust recorder) → Stage 3 (cutover) → Stage 4 (persistence/docs).

## 3. Binary format v1 (the contract — freeze before coding)

One file per live game session. Little-endian throughout. Two fixed-stride rings behind a 64 B
header. Fixed stride is deliberate: O(1) mmap append, random access, no compaction; delta/varint
encodings were rejected (they save ~30 KB and cost random access).

```
FILE = header(64 B) + metrics_ring[metrics_cap × 36 B] + frame_ring[frame_cap × 1 B]
```

### 3.1 Header — 64 B

| off | field | type | value / meaning |
|----:|-------|------|-----------------|
| 0 | magic | `4s` | `b"PBFH"` |
| 4 | version | `u16` | 1 |
| 6 | flags | `u16` | reserved, 0 |
| 8 | app_id | `u32` | 0 when unknown (daemon writes 0; Python maps pid→app_id) |
| 12 | pid | `u32` | game session pid |
| 16 | gpu_index | `u16` | daemon's GPU index |
| 18 | power_limit_w | `u16` | board power limit (PWR spoke denominator) |
| 20 | max_boost_mhz | `u16` | informational |
| 22 | sample_hz | `u8` | 1 |
| 23 | reserved | `u8` | 0 |
| 24 | window_s | `u16` | 1800 |
| 26 | metrics_cap | `u16` | 1800 |
| 28 | metrics_head | `u16` | next write slot |
| 30 | metrics_count | `u16` | filled records (≤ cap) — drives the 5-min gate |
| 32 | frame_cap | `u32` | default 1,048,576 (2^20 — ≥ 580 fps sustained for 30 min) |
| 36 | frame_head | `u32` | next write slot |
| 40 | frame_count | `u32` | filled entries (≤ cap) |
| 44 | started_unix | `u64` | session start, seconds |
| 52 | reserved | 12 B | 0 |

Struct format: `"<4sHHIIHHHBBHHHHIIIQ12x"` — assert `calcsize == 64` in tests.

### 3.2 Metrics record — 36 B, one per second

| field | type | scale / meaning |
|-------|------|-----------------|
| t_rel | `u16` | seconds since `started_unix` (sessions > 18 h: rebase `started_unix`) |
| frame_count | `u16` | frame-ring entries appended during this second (exact alignment) |
| clock_mhz | `u16` | |
| mem_clock_mhz | `u16` | |
| voltage_mv | `u16` | |
| power_w | `u16` | |
| gpu_util | `u8` | % |
| cpu_util | `u8` | % |
| cpu_thread | `u8` | hottest-thread %, from `cpu_peak_thread_pct` |
| fan_pct | `u8` | |
| temp_c | `u8` | |
| flags | `u8` | bit0 framegen_active · bit1 adaptive · bits2-3 fps_source (0 none, 1 marker, 2 nvapi-marker, 3 other) · bits4-7 tier nibble |
| uv_offset_mv | `i16` | signed |
| present_fps | `u16` | ×0.1 — pre-frame-gen (base) fps |
| framegen_fps | `u16` | ×0.1 — presented fps |
| latency_ms | `u16` | ×0.05 ms |
| display_latency_ms | `u16` | ×0.05 ms |
| ft_p50 / ft_p99 / ft_p999 | `u16`×3 | ×0.05 ms — exact per-second percentiles (1%-low = p99) |

Struct format: `"<HHHHHHBBBBBBhHHHHHHH"` — assert `calcsize == 36`.

**Tier nibble** (matches `PROFILE_TIERS` ordering and burnerd's `tier_index`):
`0 = none/stock · 1 = efficiency · 2 = balanced · 3 = performance`.

### 3.3 Frame entry — 1 B per rendered frame

`v: u8`, `ft_ms = 2.0 ** (v / 32.0)` → 1.0 ms (v=0) … ~250.6 ms (v=255), ~2.2 % relative step.
Encode: `v = round(32 * log2(clamp(ft_ms, 1.0, 250.6)))`. Decode via a 256-entry LUT.
Frames are **base/rendered** frametimes when markers allow separation, else raw present
(provenance in `flags.fps_source`). Exact stats are preserved per second in `ft_p50/p99/p999`.

### 3.4 Volume

56→65 KB metrics + 1 B/frame: **~0.28 MB (120 fps) … ~0.50 MB (240 fps)** live per running game;
gzip archive ≈ 0.1 MB. 20 archived games ≈ 2 MB.

## 4. Fingerprint axes & derived stats

| Spoke | Signal | Normalized against | Notes |
|------|--------|--------------------|-------|
| PWR | median `power_w` | `power_limit_w` (header) | demand |
| GPU | median `gpu_util` | 100 | saturation |
| CPU | median `cpu_thread` | 100 | single-thread bind |
| FPS | median `present_fps` | game `target_fps` → global adaptive target (60) fallback; clamp 1 | pre-frame-gen |
| LOW | window `ft_p50 / ft_p99` ratio (from frame ring or percentile series) | 0–1 | consistency |
| LAT | median `latency_ms` | 40 ms cap | responsiveness |

Bottleneck heuristic: `gpu − cpu_thread ≥ 12` → GPU-bound; `cpu_thread − gpu ≥ 12` → CPU-bound;
else "mixed". Overall tier for badge color = modal tier nibble across the window.
UI copy must note: radar **area** is not a quantity; read spokes.

## 5. Architecture

```
burnerd (root, Rust)                     Python (user)
─ latency_rx frame markers ─┐            runtime/frame_history.py  (mmap read, decode, archive)
─ engine 1 Hz metrics ──────┤                       │
  frame_history.rs (NEW) ───┴─▶ /run/penguin-burner/frame-history/<pid>.ring   (0644, tmpfs)
  · append frames + records                         │            on exit → ~/.local/share/penguin-burner/
  · per-second percentile rollup                    ▼                         frame-history/<app_id>.ring.gz
  · GC rings of dead pids            ui/components/frame_dna.py   (axes math + radar widget + badge pixmap)
                                     ui/components/frame_dna_panel.py (tab: big DNA + stats + 2 plots)
                                     ui/components/steam_panel.py  (badge in title_row, peek tooltip, click→tab)
                                     ui/window.py                  (new tab + show_frame_dna(app_id))
```

Daemon = dumb fast recorder. All interpretation (normalization, rendering) in Qt.
Recorder cadence must NOT depend on the engine tick (fan poll can legally be 60 s) — drive the
per-second rollup from the latency-rx clock; slow metrics stick to last-known (`t_rel` makes
records self-timed either way).

## 6. Repo integration facts (verified against the tree; symbols > line numbers if drifted)

- **Qt injection:** `ui/qt.py:14 import_qt()` returns `(QtCore, QtGui, QtWidgets, pg)`; `pg` may be
  `None` → placeholder widget branch (see `ui/components/curve_plot.py:52-57`). Components are plain
  Python classes exposing `.widget`; **no Qt Signals anywhere** — cross-component wiring uses plain
  callback attributes (`runs_table.py:52` + `window.py:132`).
- **Window/tabs:** `MainWindow` (`ui/window.py:57`, plain class wrapping `QMainWindow`).
  `self.tabs = QTabWidget()` at `window.py:161`; four `addTab` calls end line 186. **Insert the new
  tab after line 186 and before `setTabsClosable(True)` (187) and `CurveTabs(fixed_tab_count=...)`
  (197)** — later tabs are treated as closable/dynamic. Tab icons: `ui/assets/tab-*.png` (18×18);
  add `tab-frame-dna.png`. Programmatic switch idiom: `self.tabs.setCurrentIndex(self.<x>_tab_index)`.
  Add `MainWindow.show_frame_dna(app_id)` → `frame_dna_panel.select_app(app_id)` + setCurrentIndex.
- **Steam panel badge:** details header `title_row` built at `steam_panel.py:327-361`
  (`info_column` → `play_button` → `addStretch(1)`). Insert the badge QToolButton after
  `play_button` (before the stretch). Selection repopulates in `_sync_selected_details()`
  (`steam_panel.py:674-732`, runs under `self._syncing` guard) — refresh the badge there.
  Selected app id: `_current_app_id()` reads `Qt.UserRole`. Panel gets a new callback attribute
  `self.on_open_frame_dna: Callable[[str], None] | None`; window wires it to `show_frame_dna`.
- **Peek:** richest existing pattern is `QToolTip.showText` driven by a hover event filter
  (`ui/dialogs/scan_tuning.py:873-910`) and HTML-table tooltips (`scan_tuning.py:867-870`).
  The peek = rich HTML tooltip on the badge (name, tier, median fps+ms, 1 %-low, power, bottleneck,
  "Click to open Frame DNA"); the badge **click** opens the tab (no button-in-popover needed).
- **Theme (dark):** `ui/theme.py` — `WINDOW_BG #111418`, `SURFACE_BG #171b21`, `BORDER #2e3440`,
  `BORDER_STRONG #3a4352`, `TEXT #d8dee9`, `TEXT_MUTED #aeb7c2`, `ERROR #ff6b6b`, and **existing tier
  colors**: `TIER_CURVE_EFFICIENCY #9fe6a8`, `TIER_CURVE_BALANCED #8ecbef`,
  `TIER_CURVE_PERFORMANCE #e05c5c` (used via `[profileTier="..."]` dynamic properties,
  `ui/styles.py:87-129`, and `_TIER_CURVE_COLORS` in `window.py:875-878`). Use these everywhere.
  All QSS lives centrally in `ui/styles.py` via `Type#objectName` selectors.
- **pyqtgraph conventions:** boilerplate from `curve_plot.py:59-68` (`PlotWidget`,
  `setMenuEnabled(False)`, `setConfigOptions(antialias=True)`, `setBackground(theme.WINDOW_BG)`,
  `showGrid(alpha=0.22)`, `_disable_axis_si_prefix` both axes). Reference lines = `pg.InfiniteLine`
  (probe-line pattern `curve_plot.py:103-115`). Orbit per-point color = `pg.ScatterPlotItem` with a
  per-point brush list (new to the repo, consistent in style). Stutter color = `theme.ERROR`.
- **runtime/ conventions:** path helper = argument > env > default constant
  (`overlay/state.py:54-58`, `runtime/daemon_client.py:34-37`); readers swallow `OSError` → empty;
  writers atomic tmp+replace; binary parsing house style = `struct.unpack_from` with explicit `<`
  formats over `bytes`, pure functions, I/O at the edge (`integrations/afterburner/fan_curve.py:26-34`);
  dataclasses `frozen=True, slots=True` in runtime/ (`adaptive_profile_policy.py:38`).
  New constants: `FRAME_HISTORY_DIR = "/run/penguin-burner/frame-history"`,
  `FRAME_HISTORY_DIR_ENV = "PENGUIN_BURNER_FRAME_HISTORY_DIR"`, `FRAME_HISTORY_FORMAT_VERSION = 1`
  (mirror in Rust like `savings.rs` `SAVINGS_FORMAT_VERSION` / `*_ENV` override).
- **Tiers:** `profiles/uv/profile_tiers.py:12-25` (`PROFILE_TIERS`, `PROFILE_TIER_LABELS`,
  `PROFILE_TIER_NONE`, `normalize_profile_tier`). Target fps: `SteamGameSetting.target_fps`
  (None → global), `runtime/support/adaptive_target_fps.py` `DEFAULT_ADAPTIVE_TARGET_FPS = 60.0`.
- **Tests:** per-file `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")` before Qt imports;
  `qtbot`/`qapp` from pytest-qt (env-provided); `pytest.skip` when `pg is None`; steam-panel tests
  use `_FakeManager` + `_row()` (`tests/test_steam_panel.py:31-188`) and stop `panel._sync_timer`
  at the end. File readers tested via `tmp_path` + `monkeypatch.setenv(<ENV>, ...)`.

## 7. Modules

### New
| Path | Contents |
|------|----------|
| `runtime/frame_history.py` | format constants + struct strings; `FrameHistoryHeader` / `FrameHistoryRecord` / `FrameHistory` dataclasses; log-companding encode/decode (LUT); pure `pack_*`/`unpack_*` over bytes; `frame_history_dir(env)`; `read_frame_history(path)`; `write_frame_history(...)` (canonical Python writer — synthetic rings, tests, future tools); `summarize(history) -> FrameHistorySummary` (medians, p99, bottleneck, modal tier, minutes); archive helpers `archive_ring(src, app_id)` / `read_archived(app_id)` (gzip, XDG dir) |
| `ui/components/frame_dna.py` | `dna_axes(summary, target_fps, power_limit)` pure math; `FrameDnaWidget` (radar paintEvent, tier color, warming-up dashed state); `dna_pixmap(...)` for the badge/tab; peek tooltip HTML builder |
| `ui/components/frame_dna_panel.py` | the tab: game selector state (`select_app`), big DNA + stats row, frametime plot (overview + live-zoom toggle), orbit plot (`ScatterPlotItem`, tier brushes); `pg is None` placeholder; empty/warming states |
| `tests/test_frame_history.py` | struct sizes (64/36), pack/unpack round-trip, ring wrap, companding round-trip accuracy (≤2.3 % rel err), gate/minutes, summarize, archive round-trip, missing/corrupt file → None |
| `tests/test_frame_dna.py` | axes values on a hand-built summary; warming state; pixmap renders non-empty; peek HTML contains stats |
| `tests/test_frame_dna_panel.py` | builds with synthetic ring; plots populated; select_app switches; placeholder when `pg is None` |

### Changed
| Path | Change |
|------|--------|
| `ui/components/steam_panel.py` | badge QToolButton in `title_row`; refresh in `_sync_selected_details`; hover peek tooltip; `on_open_frame_dna` callback; click handler |
| `ui/window.py` | construct `FrameDnaPanel` (after SteamPanel, ~line 160); `addTab` before `setTabsClosable`; `show_frame_dna()`; wire `steam_panel.on_open_frame_dna` |
| `ui/styles.py` | selectors for the badge (`steamFrameDnaBadge`, tier via `[profileTier=...]`) and panel chrome |
| `ui/assets/tab-frame-dna.png` | new 18×18 tab icon (generate reproducibly, inspect before publishing) |
| `tests/test_steam_panel.py`, `tests/test_ui_window.py` | badge presence/warming, callback wiring, tab exists + switch |
| Stage 2+: `burnerd/src/profile/frame_history.rs`, `mod.rs`, `latency_rx.rs`, `telemetry.rs` | recorder (see §8 Stage 2) |

## 8. Stages & acceptance

**Stage 0 — contract.** `runtime/frame_history.py` constants/pack/unpack + synthetic-session
generator (plausible per-game sessions from summary params — powers the UI now, fixtures forever).
*Accept:* round-trip tests green; struct sizes asserted; companding error bound test.

**Stage 1 — UI against synthetic rings.** frame_dna.py, frame_dna_panel.py, steam_panel badge+peek,
window tab. *Accept:* full suite green; offscreen qtbot drive shows badge on qualified game,
warming under 5 min, peek HTML, tab switch on click; screenshot rendered offscreen for visual check;
`ruff check` clean (pyright per repo script where available).

**Stage 2 — Rust recorder.** `frame_history.rs` (same layout; mmap in `/run`, 0644; per-second
rollup from latency-rx; GC dead-pid rings; env override for tests like `savings.rs`). Wire into
`profile/mod.rs` + `latency_rx.rs`. *Accept:* live game produces a ring the Stage-0 Python reader
decodes unchanged; counts grow at 1 Hz / frame rate. **Live acceptance requires the GPU host.**

**Stage 3 — cutover.** Panel/tab read live rings via pid→app_id from
`daemon_status()["game_runtime"]["watched"]`; refresh badge/tab on `_sync_timer` ticks; archive on
exit (game-state poll already detects stops). *Accept:* launch → warming → live fingerprint.

**Stage 4 — persistence & docs.** Read archives for non-running games ("last session" badge),
eviction (keep 20 newest), user docs in `docs/` (only now), this plan updated to done.

## 9. Verification (per CLAUDE.md)

Focused tests → `python -m pytest tests/ -q` → `scripts/check-feature-static-analysis.sh`
(needs pyright/vulture/scc/cloc; on hosts missing them run at minimum
`ruff check <all project paths>` + `git diff --check`) → live daemon/GPU validation for Stage 2+3 on
the target machine. Never report unobserved live results.

## 10. Risks

- **Frame-rate writes in daemon:** preallocated mmap, 1-byte append, no syscalls hot-path.
- **Radar misread as area:** fixed axis order + UI copy.
- **Badge staleness:** `_auto_sync` only repopulates on row-signature change — badge refresh must
  hook `_sync_selected_details` *and* a periodic tick in Stage 3.
- **pyqtgraph absent:** every plot consumer implements the `pg is None` placeholder branch.
- **Permissions:** ring files must be 0644 and `/run/penguin-burner/frame-history/` 0755 —
  overlay-state precedent exists.
- **18 h+ sessions:** `t_rel u16` rebase rule (§3.2).
