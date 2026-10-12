"""One dig: adapt ES CD bar density so dual trend/BB is observable."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.config import load_config
from research.features.cd import annotate_dual_cd_fields, build_cd_series
from research.features.momentum import (
    cross_confirm_bursts,
    momentum_burst_events,
    trades_to_tape_bars,
)
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import (
    _filter_rth_uk_table,
    _list_sessions,
    _load_trades,
    _mean,
    _median,
)


def pack(t: pa.Table, mask: np.ndarray) -> dict | None:
    idx = np.where(mask)[0]
    if len(idx) == 0:
        return None
    s = t.take(idx)
    return {
        "n": int(s.num_rows),
        "hit10": round(float(_mean(s.column("hit_10pts"))), 3),
        "ret10s": round(float(_mean(s.column("ret_pts_10000ms"))), 2),
        "med10s": round(float(_median(s.column("ret_pts_10000ms"))), 2),
    }


def run_spec(name: str, es_range: int, es_sma: int, sessions, parquet) -> dict:
    parts = []
    bar_counts = []
    burst_kw = dict(
        min_trades=6, min_vol=25.0, min_imbalance=0.55,
        min_progress_ticks=1.0, tick_size=1.0, cooldown_bars=3,
    )
    horizons = [250, 500, 1000, 2000, 5000, 10000]
    targets = [10, 20]
    for sid in sessions:
        nq = _filter_rth_uk_table(_load_trades(parquet, sid, "NQ"))
        es = _filter_rth_uk_table(_load_trades(parquet, sid, "ES"))
        if nq.num_rows == 0:
            continue
        nq_bars = trades_to_tape_bars(nq, bar_ms=100)
        es_bars = trades_to_tape_bars(es, bar_ms=100) if es.num_rows else pa.table({})
        events = cross_confirm_bursts(
            momentum_burst_events(nq_bars, **burst_kw),
            momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
            sync_ms=1500,
        )
        if events.num_rows and "es_lag_ms" in events.column_names:
            lag = np.abs(events.column("es_lag_ms").to_numpy())
            events = events.take(np.where(lag <= 250)[0])
        nq_cd = build_cd_series(nq, range_ticks=22, tick_size=1.0, sma_len=100)
        es_cd = (
            build_cd_series(es, range_ticks=es_range, tick_size=1.0, sma_len=es_sma)
            if es.num_rows
            else pa.table({})
        )
        bar_counts.append({
            "session": sid,
            "nq_cd_bars": nq_cd.num_rows,
            "es_cd_bars": es_cd.num_rows,
        })
        path_ts, path_px = build_price_path(nq)
        lab = label_events(events, path_ts, path_px, horizons, targets)
        if lab.num_rows == 0:
            continue
        lab = annotate_dual_cd_fields(lab, nq_cd, es_cd)
        parts.append(lab)
    if not parts:
        return {"name": name, "status": "empty"}
    df = pa.concat_tables(parts)
    es_tr = df.column("es_cd_ma_trend").to_numpy()
    es_pct = df.column("es_cd_bb_width_pct").to_numpy()
    return {
        "name": name,
        "es_range": es_range,
        "es_sma": es_sma,
        "bar_counts": bar_counts,
        "frac_es_trend_nz": float(np.mean(np.isfinite(es_tr) & (es_tr != 0))),
        "frac_es_pct_finite": float(np.mean(np.isfinite(es_pct))),
        "all": pack(df, np.ones(df.num_rows, dtype=bool)),
        "nq_trend": pack(df, df.column("nq_trend_aligned").to_numpy() == 1),
        "dual_trend": pack(df, df.column("dual_trend_aligned").to_numpy() == 1),
        "dual_wide": pack(df, df.column("dual_wide").to_numpy() == 1),
        "dual_same_regime": pack(df, df.column("dual_same_regime").to_numpy() == 1),
        "dual_trend_wide": pack(df, df.column("dual_trend_and_wide").to_numpy() == 1),
    }


def main() -> None:
    cfg = load_config()
    parquet = Path(cfg["parquet_dir"])
    sessions = _list_sessions(parquet, "all", cfg.get("smoke_sessions") or [])
    specs = [
        ("same_as_nq", 22, 100),
        ("es_r10_sma50", 10, 50),
        ("es_r8_sma40", 8, 40),
        ("es_r5_sma30", 5, 30),
    ]
    out = []
    for name, er, es in specs:
        print(f"spec {name} …", flush=True)
        row = run_spec(name, er, es, sessions, parquet)
        out.append(row)
        print(json.dumps({k: row[k] for k in row if k != "bar_counts"}, indent=2), flush=True)
    Path("results/EXP-0022/dig_es_adapt.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
