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
EXP-0008 hit+10 ticks is ~0.55–0.60 vs the prior ~0.85–0.90. Treat all
momentum-based reports dated before this fix as invalid for live MAT comparison.
MAT / EXP-0034 use causal ES confirm (ES burst end ≤ NQ burst end).

## Data

- Source: Bookmap `.bmf` via Replay + Session Extractor add-on
- Feeds: `C:\Bookmap\Feeds`
- Raw extracts: `data/raw/<session_id>/`
- Parquet: `data/parquet/`
- Do not commit raw `.bmf`, NDJSON, or Parquet to git
