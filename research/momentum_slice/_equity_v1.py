"""One-shot equity dump for frozen mom_confirm_levelSL_tp16_t60."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import MatSimConfig, merge_summaries, simulate_session
from research.config import load_config
from research.correlated.harness import load_pair
from research.momentum_slice.backtest_v1 import level_sl
from research.momentum_slice.rules import enrich, is_momentum_v1

COMM = 0.25
TP = 16.0
TS = 60.0
OUT = Path("results/MOMENTUM_SLICE/backtest_v1")


def run(features_path: str, label: str) -> None:
    rows = [enrich(r) for r in csv.DictReader(open(features_path, encoding="utf-8"))]
    mom = [r for r in rows if is_momentum_v1(r)]
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    by: dict[str, list] = {}
    for r in mom:
        by.setdefault(r["session"], []).append(r)
    sim_cfg = MatSimConfig(
        tp_pts=TP, sl_pts=10.0, commission_pts_rt=COMM,
        signal_cooldown_sec=30, time_stop_sec=TS, trail_pts=0,
    )
    parts = []
    for sid, evs in sorted(by.items()):
        pair = load_pair(pq, sid)
        if pair is None:
            continue
        ts, side, sls, tps = [], [], [], []
        for r in evs:
            ts.append(int(float(r["confirm_ns"])))
            side.append(int(float(r["side_int"])))
            sls.append(level_sl(r, 10.0))
            tps.append(TP)
        tbl = pa.table({
            "event_ts_ns": np.array(ts, dtype=np.int64),
            "side": np.array(side, dtype=np.int8),
            "sl_pts": np.array(sls, dtype=np.float64),
            "tp_pts": np.array(tps, dtype=np.float64),
        })
        parts.append(simulate_session(
            sid, pair.nq, pair.es, sim_cfg, events=tbl, vwap_from_ns=pair.bounds[0]))
    merged = merge_summaries(parts, COMM)
    trades = sorted(merged.trades, key=lambda t: t.entry_ts_ns)
    eq = 0.0
    curve = []
    out_rows = []
    for i, tr in enumerate(trades, 1):
        net = tr.pnl - COMM
        eq += net
        curve.append(round(eq, 2))
        out_rows.append({
            "i": i,
            "session": tr.session_id,
            "entry_ts_ns": tr.entry_ts_ns,
            "side": int(tr.side),
            "pnl": round(tr.pnl, 2),
            "net": round(net, 2),
            "equity": round(eq, 2),
            "exit": getattr(tr, "exit_reason", "") or "",
        })
    out = OUT / f"equity_{label}_mom_t60.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()) if out_rows else ["i"])
        w.writeheader()
        w.writerows(out_rows)
    print(f"=== {label} mom_confirm_levelSL_tp16_t60 ===")
    print(json.dumps(merged.to_dict(), indent=2))
    print(f"n={len(curve)} final={curve[-1] if curve else 0} "
          f"min={min(curve) if curve else 0} max={max(curve) if curve else 0}")
    print(f"wrote {out}")
    print("EQUITY", ",".join(str(e) for e in curve))


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    run("results/MOMENTUM_SLICE/features/features.csv", "train")
    run("results/MOMENTUM_SLICE/holdout/features.csv", "holdout")
