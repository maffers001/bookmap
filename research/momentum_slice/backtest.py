"""Exploratory bracket backtest of LST events via mat_sim (confirm-time entry).

  .venv\\Scripts\\python.exe -m research.momentum_slice.backtest --rule R1_tierA
Rules imported from analyse_labels.RULES. Sep-Oct is TRAIN here; treat all
numbers as in-sample until the plan's holdout gate passes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import MatSimConfig, merge_summaries, simulate_session
from research.config import load_config
from research.correlated.harness import load_pair
from research.momentum_slice.analyse_labels import RULES, load
from research.momentum_slice.scan import bst

VARIANTS = {
    "b8_16": MatSimConfig(tp_pts=16, sl_pts=8, commission_pts_rt=0.25, signal_cooldown_sec=30),
    "b10_10": MatSimConfig(tp_pts=10, sl_pts=10, commission_pts_rt=0.25, signal_cooldown_sec=30),
    "b10_10_t60": MatSimConfig(tp_pts=10, sl_pts=10, commission_pts_rt=0.25,
                               signal_cooldown_sec=30, time_stop_sec=60),
    "b16_24": MatSimConfig(tp_pts=24, sl_pts=16, commission_pts_rt=0.25, signal_cooldown_sec=30),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", default="ALL", choices=["ALL", *RULES])
    ap.add_argument("--features", default="results/MOMENTUM_SLICE/features/features.csv")
    ap.add_argument("--out", default="results/MOMENTUM_SLICE/backtest")
    args = ap.parse_args()
    rows = load(Path(args.features))
    if args.rule != "ALL":
        rows = [r for r in rows if RULES[args.rule](r)]
    cfg = load_config(); pq_dir = Path(cfg["parquet_dir"])
    by_session: dict[str, list] = {}
    for r in rows:
        by_session.setdefault(r["session"], []).append(r)
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    report = {"rule": args.rule, "n_events": len(rows), "variants": {}}
    for name, sim_cfg in VARIANTS.items():
        parts, tod_trades = [], {"open": [], "late": []}
        for sid, evs in sorted(by_session.items()):
            pair = load_pair(pq_dir, sid)
            if pair is None:
                continue
            tbl = pa.table({"event_ts_ns": np.array([int(float(r["confirm_ns"])) for r in evs], dtype=np.int64),
                            "side": np.array([int(float(r["side_int"])) for r in evs], dtype=np.int8)})
            s = simulate_session(sid, pair.nq, pair.es, sim_cfg, events=tbl,
                                 vwap_from_ns=pair.bounds[0])
            parts.append(s)
            tod_by_ts = {int(float(r["confirm_ns"])): r.get("tod", "late") for r in evs}
            for tr in s.trades:
                tod_trades[tod_by_ts.get(tr.entry_ts_ns, "late")].append(tr)
        merged = merge_summaries(parts, 0.25) if parts else None
        entry = {"all": merged.to_dict() if merged else {"n": 0}}
        from research.backtest.mat_sim import _rebuild_summary
        for tod, trs in tod_trades.items():
            entry[tod] = _rebuild_summary(trs, 0.25).to_dict()
        report["variants"][name] = entry
        print(name, entry)
    (out / f"backtest_{args.rule}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"-> {out}/backtest_{args.rule}.json")


if __name__ == "__main__":
    main()
