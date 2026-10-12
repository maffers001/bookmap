"""Dig EXP-0008 overlays: ES lag, CD against-trend, CD pullback combinations."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.config import load_config
from research.features.cd import build_cd_series, lookup_cd_at_events
from research.run_experiment import _filter_rth_uk_table, _load_trades, _list_sessions, _mean, _median

ROOT = Path(__file__).resolve().parents[1]


def _stats(t: pa.Table) -> dict:
    if t.num_rows == 0:
        return {"n": 0}
    return {
        "n": int(t.num_rows),
        "hit10": _mean(t.column("hit_10pts")),
        "hit20": _mean(t.column("hit_20pts")),
        "r2s": _mean(t.column("ret_pts_2000ms")),
        "r10s": _mean(t.column("ret_pts_10000ms")),
        "med_r10s": _median(t.column("ret_pts_10000ms")),
        "mfe": _mean(t.column("mfe_pts")),
        "mae": _mean(t.column("mae_pts")),
    }


def main() -> None:
    cfg = load_config()
    ev = pq.read_table(ROOT / "results/EXP-0008/events.parquet")
    parquet_dir = Path(cfg["parquet_dir"])
    sessions = sorted({x.as_py() for x in ev.column("session_id")})

    # Attach CD fields + ensure es_lag_ms present
    parts = []
    for sid in sessions:
        idx = np.where(np.array(ev.column("session_id").to_pylist()) == sid)[0]
        sub = ev.take(idx)
        nq = _load_trades(parquet_dir, sid, "NQ")
        nq = _filter_rth_uk_table(nq)
        cd = build_cd_series(nq, range_ticks=22, tick_size=1.0)
        fields = lookup_cd_at_events(cd, sub.column("event_ts_ns").to_numpy())
        out = sub
        for name, arr in fields.items():
            if name in out.column_names:
                out = out.drop(name)
            out = out.append_column(name, pa.array(arr))
        parts.append(out)
    d = pa.concat_tables(parts)

    sides = d.column("side").to_numpy()
    lag = d.column("es_lag_ms").to_numpy() if "es_lag_ms" in d.column_names else np.full(d.num_rows, np.nan)
    trend = d.column("cd_ma_trend").to_numpy()
    pb_l = d.column("cd_pullback_long").to_numpy()
    pb_s = d.column("cd_pullback_short").to_numpy()
    # with-flow: side matches trend
    with_flow = ((sides > 0) & (trend > 0)) | ((sides < 0) & (trend < 0))
    against = ((sides > 0) & (trend < 0)) | ((sides < 0) & (trend > 0))
    pullback = ((sides > 0) & (pb_l == 1)) | ((sides < 0) & (pb_s == 1))
    lag_tight = lag < 250

    def take(mask) -> pa.Table:
        idx = np.where(mask)[0]
        return d.take(idx) if len(idx) else pa.table({})

    report = {
        "all": _stats(d),
        "lag_lt_250ms": _stats(take(lag_tight)),
        "lag_ge_250ms": _stats(take(~lag_tight & np.isfinite(lag))),
        "cd_with_flow": _stats(take(with_flow)),
        "cd_against_trend": _stats(take(against)),
        "cd_pullback": _stats(take(pullback)),
        "lag_lt_250_and_pullback": _stats(take(lag_tight & pullback)),
        "lag_lt_250_and_against": _stats(take(lag_tight & against)),
        "lag_lt_250_and_with_flow": _stats(take(lag_tight & with_flow)),
        "by_session_against": {},
        "by_session_lag250": {},
    }

    sids = d.column("session_id").to_pylist()
    for sid in sessions:
        m = np.array([x == sid for x in sids])
        report["by_session_against"][sid] = _stats(take(m & against))
        report["by_session_lag250"][sid] = _stats(take(m & lag_tight))

    out = ROOT / "results/EXP-0008/overlay_dig.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print("Wrote", out)


if __name__ == "__main__":
    main()
