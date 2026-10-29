"""mat_sim validation for PC round-2 finalists (pullback delay / second push)."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import MatSimConfig, _rebuild_summary, merge_summaries, simulate_session
from research.config import load_config
from research.correlated.harness import load_pair
from research.momentum_slice.backtest_v1 import level_sl
from research.momentum_slice.rules import enrich, f, is_pc_v1

OUT = Path("results/MOMENTUM_SLICE/pc_review")
COMM = 0.25


def num(d, k):
    v = d.get(k) if d else None
    if v in ("", "nan", None):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def merge(feats_path, path_path):
    paths = {}
    for p in csv.DictReader(open(path_path, encoding="utf-8")):
        paths[(p["session"], int(float(p["side_int"])), int(float(p["ignition_ns"])))] = p
    rows = []
    for r in csv.DictReader(open(feats_path, encoding="utf-8")):
        enrich(r)
        key = (r["session"], int(float(r["side_int"])), int(float(r["ignition_ns"])))
        p = paths.get(key)
        if p is None:
            continue
        r["_path"] = p
        rows.append(r)
    return rows


def P(r):
    return r["_path"]


CASES = [
    ("PC_V1_baseline_tp10", lambda r: is_pc_v1(r), 5, 10.0),
    ("PC2_d10_pb>=20_tp10", lambda r: (num(P(r), "pb10") or 0) >= 20 and (num(P(r), "beyond_peak10") or 9) <= 1.0, 10, 10.0),
    ("PC2_d10_pb>=25_tp10", lambda r: (num(P(r), "pb10") or 0) >= 25 and (num(P(r), "beyond_peak10") or 9) <= 1.0, 10, 10.0),
    ("PC2_d10_pb>=25_tp16", lambda r: (num(P(r), "pb10") or 0) >= 25 and (num(P(r), "beyond_peak10") or 9) <= 1.0, 10, 16.0),
    ("PC2_d10_pb25-40_tp16", lambda r: 25 <= (num(P(r), "pb10") or -1) <= 40 and (num(P(r), "beyond_peak10") or 9) <= 1.0, 10, 16.0),
    ("PC2_d20_pb>=20_pushfail_reclaim<=0.5_tp10", lambda r: (num(P(r), "pb20") or 0) >= 20 and (num(P(r), "beyond_peak20") or 9) <= 1.0 and num(P(r), "push20_fails") == 1 and (num(P(r), "push20_reclaim_frac") or 9) <= 0.5, 20, 10.0),
    ("PC2_d20_pb>=20_pushfail_reclaim<=0.5_tp16", lambda r: (num(P(r), "pb20") or 0) >= 20 and (num(P(r), "beyond_peak20") or 9) <= 1.0 and num(P(r), "push20_fails") == 1 and (num(P(r), "push20_reclaim_frac") or 9) <= 0.5, 20, 16.0),
    ("PC2_d20_pb>=20_pushfail_lowvol<=0.5_tp16", lambda r: (num(P(r), "pb20") or 0) >= 20 and (num(P(r), "beyond_peak20") or 9) <= 1.0 and num(P(r), "push20_fails") == 1 and (num(P(r), "push20_vol_ratio") or 9) <= 0.5, 20, 16.0),
    ("PC2_d20_pb>=15_pushfail_reclaim<=0.5_tp16", lambda r: (num(P(r), "pb20") or 0) >= 15 and (num(P(r), "beyond_peak20") or 9) <= 1.0 and num(P(r), "push20_fails") == 1 and (num(P(r), "push20_reclaim_frac") or 9) <= 0.5, 20, 16.0),
    ("PC2_d20_pb>=20_nopushtest_tp16", lambda r: (num(P(r), "pb20") or 0) >= 20 and (num(P(r), "beyond_peak20") or 9) <= 1.0, 20, 16.0),
]


def run_case(rows, fn, offset_s, tp, pairs):
    sel = [r for r in rows if fn(r)]
    by = {}
    for r in sel:
        by.setdefault(r["session"], []).append(r)
    sim_cfg = MatSimConfig(tp_pts=tp, sl_pts=10.0, commission_pts_rt=COMM,
                           signal_cooldown_sec=30, time_stop_sec=60.0, trail_pts=0)
    parts, splits = [], {"late": [], "open": [], "LONG": [], "SHORT": []}
    for sid, evs in sorted(by.items()):
        pair = pairs.get(sid)
        if pair is None:
            continue
        ts, side, sls, tps, meta = [], [], [], [], {}
        for r in evs:
            t = int(float(r["confirm_ns"])) + int(offset_s * 1_000_000_000)
            ts.append(t)
            side.append(-int(float(r["side_int"])))
            sls.append(level_sl(r, 10.0))
            tps.append(tp)
            meta[t] = r
        tbl = pa.table({
            "event_ts_ns": np.array(ts, dtype=np.int64),
            "side": np.array(side, dtype=np.int8),
            "sl_pts": np.array(sls, dtype=np.float64),
            "tp_pts": np.array(tps, dtype=np.float64),
        })
        summ = simulate_session(sid, pair.nq, pair.es, sim_cfg, events=tbl, vwap_from_ns=pair.bounds[0])
        parts.append(summ)
        for tr in summ.trades:
            r = meta.get(tr.entry_ts_ns)
            if r is None:
                for k, rr in meta.items():
                    if abs(k - tr.entry_ts_ns) < 1_000_000:
                        r = rr; break
            if r is None:
                continue
            splits[r.get("tod", "late")].append(tr)
            splits[r.get("side", "LONG")].append(tr)
    merged = merge_summaries(parts, COMM) if parts else None
    out = {"all": merged.to_dict() if merged else {"n": 0}, "n_selected": len(sel)}
    for k, trs in splits.items():
        out[k] = _rebuild_summary(trs, COMM).to_dict()
    if merged and merged.trades:
        eq, curve = 0.0, []
        for tr in sorted(merged.trades, key=lambda t: t.entry_ts_ns):
            eq += tr.pnl - COMM
            curve.append(round(eq, 2))
        out["equity"] = curve
    else:
        out["equity"] = []
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="train")
    args = ap.parse_args()
    if args.split == "train":
        rows = merge("results/MOMENTUM_SLICE/features/features.csv", OUT / "path_features_train.csv")
    else:
        rows = merge("results/MOMENTUM_SLICE/holdout/features.csv", OUT / "path_features_holdout.csv")
    cfg = load_config(); pq = Path(cfg["parquet_dir"])
    pairs = {}
    for sid in sorted({r["session"] for r in rows}):
        p = load_pair(pq, sid)
        if p is not None:
            pairs[sid] = p
    report = {"split": args.split, "cases": {}}
    lines = [f"# PC round-2 validation ({args.split})", ""]
    for name, fn, off, tp in CASES:
        print(f"running {name}", flush=True)
        res = run_case(rows, fn, off, tp, pairs)
        report["cases"][name] = res
        a = res["all"]
        lines.append(f"## {name}")
        lines.append(f"- sel={res['n_selected']} all: n={a.get('n')} wr={a.get('wr')} pf={a.get('pf')} net={a.get('net')} maxDD={a.get('max_dd')}")
        for sub in ("late", "open", "LONG", "SHORT"):
            s = res.get(sub, {})
            if s.get("n"):
                lines.append(f"- {sub}: n={s.get('n')} wr={s.get('wr')} pf={s.get('pf')} net={s.get('net')}")
        lines.append(f"- equity: {','.join(str(x) for x in res.get('equity', []))}")
        lines.append("")
    (OUT / f"validate2_{args.split}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (OUT / f"validate2_{args.split}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
