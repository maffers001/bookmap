# Research Rules — Orderflow Edge Hunt

## Principles

1. **Event research before strategy P&L.** First ask whether a condition predicts short-horizon NQ behavior (returns, MFE/MAE, P(reach +10/+20 pts)). Only then design entries/exits.
2. **No peeking.** Train/holdout splits are by calendar date only — never random shuffle of events across time.
3. **Primary window.** Default analysis uses UK 14:30–17:00 (Chicago RTH including the open). Other hours may be reported separately.
4. **Falsify honestly.** Null or unstable results are logged as failures; do not retune endlessly on the same holdout.
5. **Same definitions.** Feature formulas in Python must match the Java Bookmap indicator core where both exist.
6. **Costs later.** Ignore commissions/slippage until raw predictive content is established; then apply conservative costs.
7. **No live trading** in this research phase.

## Success bar (genuine edge)

- Holdout effect same direction as train
- Stable across multiple sessions/contracts
- Material lift vs unconditional / time-matched baseline
- ES confirmation either improves NQ outcomes or is explicitly falsified

## Momentum bar-end timestamps (2026-10-18)

`momentum_burst_events` / simultaneous tape-bar detectors stamp `event_ts_ns` at
**bar end** (bar_start + bar_ms). Earlier results labelled from **bar start**,
which included the burst move itself in the outcome path (look-ahead). Corrected
EXP-0008 hit+10 *ticks* was ~0.55–0.60 vs the prior ~0.85–0.90. Treat all
momentum-based reports dated before this fix as invalid for live MAT comparison.

## Units + causal stamps (2026-10-18 evening)

Three further defects invalidated most pre-fix event-study conclusions:

1. **Units.** Parquet `price` is tick units. `label_events` previously never
   scaled to chart points, so every `hit_10pts` / `ret_pts_*` was really
   **+10 ticks = 2.5 NQ pts** inside ≤10 s. `build_price_path` now multiplies by
   `pts_per_tick=0.25`. Default horizons include 20/30/60 s; targets `[5,10,20,30]`
   are true points. Pre-fix event-study numbers are not comparable.
2. **Comparator.** The old “≪ EXP-0008 85%” bar used look-ahead + tick units.
   Corrected EXP-0008 (14 days, confirm stamp, 60 s race): `p_hit_10pts` ≈ **0.44**.
3. **Depth coverage.** Depth/MBO parquet existed only for the 5 September sessions
   until full conversion (all 14 + 161026). Pre-fix H2/H3/H9/H11/H13/H15/H16
   results were 5-day-only.

**ES confirm stamps.** `cross_confirm_bursts` default is now `stamp="confirm"`
(ES within ±sync; `event_ts = max(nq, es)`). Live MAT / `mat_sim` default remains
`stamp="causal"` (ES at/before NQ). Prefer confirm-time for research entry
variants; use causal when matching the live addon.

**MAT sim** (`research/backtest/mat_sim.py`) was already in points with causal ES
and is the authoritative P&L yardstick.

## Data

- Source: Bookmap `.bmf` via Replay + Session Extractor add-on
- Feeds: `C:\Bookmap\Feeds`
- Raw extracts: `data/raw/<session_id>/`
- Parquet: `data/parquet/` (trades + depth + mbo per session)
- Do not commit raw `.bmf`, NDJSON, or Parquet to git
- Convert: `python -m research.extract.to_parquet --only depth,mbo --skip-existing`
