"""One dig: swing lookback 2m/3m/5m for EXP-0025."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.config import load_config
from research.features.momentum import (
    cross_confirm_bursts,
    momentum_burst_events,
    trades_to_tape_bars,
)
from research.features.swings import SwingConfig, annotate_burst_swing_break
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import (
    _filter_rth_uk_table,
    _list_sessions,
    _load_trades,
    _mean,
    _median,
)


def pack(t: pa.Table, mask) -> dict | None:
    idx = np.where(mask)[0]
    if len(idx) == 0:
        return None
    s = t.take(idx)
    return {
        "n": int(s.num_rows),
        "hit10": round(float(_mean(s.column("hit_10pts"))), 3),
        "ret10s": round(float(_mean(s.column("ret_pts_10000ms"))), 2),
        "med10s": round(float(_median(s.column("ret_pts_10000ms"))), 2),
        "mae": round(float(_mean(s.column("mae_pts"))), 2),
    }


def main() -> None:
    cfg = load_config()
    parquet = Path(cfg["parquet_dir"])
    sessions = _list_sessions(parquet, "all", cfg.get("smoke_sessions") or [])
    burst_kw = dict(
        min_trades=6, min_vol=25.0, min_imbalance=0.55,
        min_progress_ticks=1.0, tick_size=1.0, cooldown_bars=3,
    )
    horizons = [250, 500, 1000, 2000, 5000, 10000]
    targets = [10, 20]
    out = []
    for lb_min in (2, 3, 5):
        parts = []
        for sid in sessions:
            nq = _filter_rth_uk_table(_load_trades(parquet, sid, "NQ"))
            es = _filter_rth_uk_table(_load_trades(parquet, sid, "ES"))
            if nq.num_rows == 0:
                continue
            events = cross_confirm_bursts(
                momentum_burst_events(trades_to_tape_bars(nq, 100), **burst_kw),
                momentum_burst_events(trades_to_tape_bars(es, 100), **burst_kw)
                if es.num_rows
                else pa.table({}),
                sync_ms=1500,
            )
            if events.num_rows and "es_lag_ms" in events.column_names:
                events = events.take(
                    np.where(np.abs(events.column("es_lag_ms").to_numpy()) <= 250)[0]
                )
            sc = SwingConfig(lookback_ms=lb_min * 60_000, confirm_ms=20_000, tick_size=1.0)
            events = annotate_burst_swing_break(events, nq, sc)
            path_ts, path_px = build_price_path(nq)
            lab = label_events(events, path_ts, path_px, horizons, targets)
            if lab.num_rows:
                parts.append(lab)
            print(f"  {lb_min}m {sid}: n={lab.num_rows} break={(lab.column('swing_break').to_numpy()==1).sum() if lab.num_rows else 0}", flush=True)
        df = pa.concat_tables(parts)
        br = df.column("swing_break").to_numpy() == 1
        row = {
            "lookback_min": lb_min,
            "all": pack(df, np.ones(df.num_rows, dtype=bool)),
            "break": pack(df, br),
            "non_break": pack(df, ~br),
        }
        out.append(row)
        print(json.dumps(row, indent=2), flush=True)
    Path("results/EXP-0025").mkdir(parents=True, exist_ok=True)
    Path("results/EXP-0025/dig_lookback.json").write_text(json.dumps(out, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
