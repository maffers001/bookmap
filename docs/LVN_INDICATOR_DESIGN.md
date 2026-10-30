# LVN Levels Indicator — design

Display-only Bookmap module **LVN Levels** with IRT-style profile knobs.
Auto-trade consumes the same shared state; it does not own chart LVN lines.

See also [IRT_LVN_PROFILE.md](IRT_LVN_PROFILE.md) for detector / tick-VAP
decisions.

## Goals

- Tick-built VAP for **ES and NQ** (no ES 1‑min bar-smear in production).
- Reconfigurable **Time per Profile** and **Sensitivity** (like IRT).
- Configurable **Join with Bookmap** handoff so attach time can vary.
- Plot **LVNs**, **HVNs**, and **DOP** (NY open print).
- Replay and live share one VAP + detect engine (seed file + optional live trades).

## Defaults

| Knob | ES | NQ |
|---|---|---|
| Time per Profile (days) | 90 | 30 |
| Sensitivity (price rows / ticks) | 15 | 100 |
| Join with Bookmap (UK) | 13:55 | 13:55 |
| DOP session open (UK) | 14:30 | 14:30 |
| LVNs / HVNs above & below | 3 / 3 | 3 / 3 |
| Node refresh (live) | 5 min | 5 min |

## Architecture

```text
Python morning/replay seed  →  vap_seed_{ES|NQ}/ (meta + daily + minutes)
                                      ↓
Bookmap LvnLevelsModule  →  LvnLiveVap  →  LvnNodeDetector  →  LvnLevelsState
                                      ↑
                         Bookmap trades (ts ≥ join)
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
session → frozen `DOP` line until day roll.

### Approach alert

Console knobs (Apply alert): enable, mode (**Correlated** / Either / Single ES /
Single NQ), per-market **Approach** and **Pullback** (chart pts), cooldown,
corr window, watch LVN / DOP, sound.

Rule: mid must come from **outside** the Approach band of a displayed LVN or
DOP, enter within Approach pts, and not move ≥ Pullback pts **away from the
level** during that approach. Fires on clean band entry → Bookmap log +
optional beep + console event line. Correlated mode waits for the other market
to tag within the window (needs Levels attached on both charts).

Defaults: ES Approach=5 Pullback=3, NQ Approach=20 Pullback=10, cooldown 60s,
corr window 30s, mode Correlated, sound on.

### Detector

Strict local min/max over ±S rows at 0.25 tick (`LvnNodeDetector`, mirror of
`research/irt_lvn/lvn_profile.py`). Nearest N above/below mid for LVN and HVN
separately.

## Modules

| Piece | Role |
|---|---|
| `LvnLevelsModule` | Display-only; knobs; seed load; plot lines; mid → alert |
| `LvnLevelsState` | Process-wide levels, knobs, seed bind, alert config |
| `LvnApproachAlert` | Approach/pullback band / single+correlated router |
| `LvnNodeDetector` | Valleys / peaks |
| `LvnLiveVap` | Seed + sliding window + live prints |
| `LvnVapSeedFile` | Load seed directory / CSV |
| `LvnConfluenceAutoTrade` | Trading only; reads levels from state |

## Ops (morning live)

1. Refresh IRT tick export if needed.
2. Build seed:  
   `python -m research.irt_lvn.build_vap_seed --market both --through 13:55`
3. Open Bookmap **before** Join (or rebuild seed if late).
4. Attach **LVN Levels** on ES/NQ → Load seed → Apply knobs.
5. Optionally attach AutoTrade (trading OFF until armed).

## Replay

Build the seed **for the replay day** (`--day YYYY-MM-DD`), load it in
Levels, run replay. A seed built for an earlier day leaves the days between
seed-through and the replay session out of the profile — the console warns
`STALE SEED ends … · session …`. Changing Sensitivity / window re-detects
without a new stack CSV. Optional fixed `LVN_STACK_*.csv` remains a
confluence fast-path only.
