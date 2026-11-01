# Levels Indicator — design

Display-only Bookmap module **Levels** (formerly LVN Levels + Market Profile)
with IRT-style seed knobs and developing session VPOC/VAH/VAL.
Auto-trade consumes the same shared LVN state; it does not own chart lines.

See also [IRT_LVN_PROFILE.md](IRT_LVN_PROFILE.md) for detector / tick-VAP
decisions.

## Goals

- Tick-built VAP for **ES and NQ** (no ES 1‑min bar-smear in production).
- Reconfigurable **Time per Profile** and **Sensitivity** (like IRT).
- Configurable **Join with Bookmap** handoff so attach time can vary.
- Plot **LVNs**, **HVNs**, and **DOP** (UK open print).
- Developing session **Market Profile**: **VPOC**, **VAH**, **VAL** from DOP open
  (same clock as DOP; Daily Open is the DOP line — not duplicated).
- Replay and live share one VAP + detect engine (seed file + optional live trades).

## Defaults

| Knob | ES | NQ |
|---|---|---|
| Time per Profile (days) | 90 | 30 |
| Sensitivity (price rows / ticks) | 15 | 100 |
| Join with Bookmap (UK) | 14:30 | 14:30 |
| DOP session open (UK) | 14:30 | 14:30 |
| LVNs / HVNs above & below | 3 / 3 | 3 / 3 |
| Node refresh (live) | 5 min | 5 min |

## Architecture

```text
Python morning/replay seed  →  vap_seed_{ES|NQ}/ (meta + daily + minutes)
                                      ↓
Bookmap Levels (LvnLevelsModule)
   → LvnLiveVap → LvnNodeDetector → LvnLevelsState   (LVN/HVN)
   → MarketProfileTracker                             (VPOC/VAH/VAL)
                                      ↑
                         Bookmap trades (ts ≥ join / DOP open)
LvnConfluenceAutoTrade  ←── LvnLevelsState (LVN set for confluence)
```

### Join with Bookmap

- Seed builder `--through HH:MM` (UK) → `meta.through_uk`.
- Module Join knob defaults from meta; Bookmap volume accepted only for
  `ts ≥ through`. Trades before join are ignored (already in seed).
- Set Join to **when you leave IRT / finish export**, not when Bookmap opens.
- Late attach after Join without a newer seed → status notes a volume gap.

### DOP

Not a profile node. First trade at/after DOP open (default 14:30 UK) each
session → frozen `DOP` line until day roll. The line is not drawn until that
open print has happened (pre-open stays blank).

### Approach alert

Console knobs (Apply alert): enable, mode (**Correlated** / Either / Single ES /
Single NQ), per-market **Approach**, **Pullback**, and **Prox** (chart pts),
cooldown, watch LVN / DOP, sound.

Rule: mid must come from **outside** the Approach band of a displayed LVN or
DOP, enter within Approach pts, and not move ≥ Pullback pts **away from the
level** during that approach. Fires on clean band entry → Bookmap log +
loud chirp + console footer. **Correlated** (synchronised S/R): one market
clean-enters, the other is within **Prox** of a watched level on the **same
approach side** (both from-below or both from-above), both mids still within
their Prox, and neither has already traded through its level. Approach side
is armed while outside so a wick through does not flip the rotation label.

Defaults: ES Approach=5 Pullback=3 Prox=5, NQ Approach=20 Pullback=10 Prox=20,
cooldown 60s, mode Correlated, sound on.

### Detector

Strict local min/max over ±S rows at 0.25 tick (`LvnNodeDetector`, mirror of
`research/irt_lvn/lvn_profile.py`). Nearest N above/below mid for LVN and HVN
separately.

## Modules

| Piece | Role |
|---|---|
| `LvnLevelsModule` (**Levels**) | Display-only; seed LVN/HVN/DOP + developing VPOC/VAH/VAL; mid → alert |
| `LvnLevelsState` | Process-wide levels, knobs, seed bind, alert config |
| `LvnApproachAlert` | Approach/pullback band / single+correlated router |
| `LvnNodeDetector` | Valleys / peaks |
| `LvnLiveVap` | Seed + sliding window + live prints |
| `LvnVapSeedFile` | Load seed directory / CSV |
| `LvnConfluenceAutoTrade` | Trading only; reads levels from state |

## Ops (morning live)

1. Refresh IRT tick export if needed (so `build_vap_seed` has data).
2. Open Bookmap **before** Join when possible.
3. Attach **Levels** on ES/NQ — existing seeds bind immediately, then
   `build_vap_seed` for the UK session day + Join UK (default 14:30) runs
   **automatically** and reloads. Console status shows progress; the button
   re-runs the same job. Manual **Load ES/NQ seed…** still overrides.
   A red banner appears if a seed is missing/unloadable, lacks a minute slice
   for the session day, or rebuild fails.
4. Optionally attach AutoTrade (trading OFF until armed).

## Replay

Attach Levels and start replay — seed rebuild uses the feed’s UK day (a
wall-clock rebuild at attach is followed by a second pass if the replay day
differs). A stale seed that does not cover the session still shows on the
red banner until rebuild completes.

If the seed’s `through_uk` is **after** the replay day, that day exists as a
complete map in `daily.csv` but has **no** `minutes.csv` slice. Levels then
include that full daily (and skip live for the day to avoid double-count) so
nodes are EOD-shaped; the banner notes `using full daily (no minute seed)`.
For a true developing mid-session profile, rebuild with `--day` = session.

Changing Sensitivity / window re-detects without a new stack CSV. Optional
fixed `LVN_STACK_*.csv` remains a confluence fast-path only.

### ES vs IRT node parity (2026-10-07 check)

On 2026-10-07 Bookmap tick VAP keeps a stable LVN at **7813.25**. IRT
`ESZ6_ALL.csv` nearest-3 picks **7815.25** once price works lower (~13:28
IRT stamp). That ~2 pt gap is the known ES **tick vs IRT 1‑min bar-smear**
histogram difference (`IRT_LVN_PROFILE.md`), not a missed refresh. Production
stays tick for both markets.
