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

## Data

- Source: Bookmap `.bmf` via Replay + Session Extractor add-on
- Feeds: `C:\Bookmap\Feeds`
- Raw extracts: `data/raw/<session_id>/`
- Parquet: `data/parquet/`
- Do not commit raw `.bmf`, NDJSON, or Parquet to git
