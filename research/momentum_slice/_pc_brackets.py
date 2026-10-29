"""PC2 bracket / volatility exploration.

Stage train: grid fixed SL x TP for PC2_d10_pb>=20, plus vol-normalised
pullback rules and pitch-stop variants at selected brackets.
Stage holdout: only combos listed in --only (shortlist from train).

Usage:
  _pc_brackets --split train --mode grid
  _pc_brackets --split train --mode vol
  _pc_brackets --split holdout --mode cases --only name1,name2
"""
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
from research.momentum_slice._pc_validate2 import merge, num, P
from research.momentum_slice.backtest_v1 import level_sl
from research.momentum_slice.rules import f

OUT = Path("results/MOMENTUM_SLICE/pc_review")
COMM = 0.25


def base_pb(r, thr=20.0, delay=10):
    return (num(P(r), f"pb{delay}") or 0) >= thr and (num(P(r), f"beyond_peak{delay}") or 9) <= 1.0


def pitch_sl(r, buffer=2.0):
    p = P(r)
    peak = num(p, "peak_px")
    entry = num(p, "entry10")
    s = num(p, "side_int")
    if peak is None or entry is None or s is None:
        return 10.0
    return float(min(max(s * (peak - entry) + buffer, 4.0), 60.0))


def run(rows, fn, offset_s, tp, sl_fn, pairs, time_stop=60.0):
    sel = [r for r in rows if fn(r)]
    by = {}
    for r in sel:
        by.setdefault(r["session"], []).append(r)
    sim_cfg = MatSimConfig(tp_pts=tp, sl_pts=10.0, commission_pts_rt=COMM,
                           signal_cooldown_sec=30, time_stop_sec=time_stop, trail_pts=0)
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
            sls.append(float(sl_fn(r)))
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
    merged = merge_summaries(parts, COMM) if parts else None
    out = {"all": merged.to_dict() if merged else {"n": 0}, "n_selected": len(sel)}
    for k in ("late", "LONG", "SHORT"):
        pass
    if merged and merged.trades:
        eq, curve = 0.0, []
        for tr in sorted(merged.trades, key=lambda t: t.entry_ts_ns):
            eq += tr.pnl - COMM
            curve.append(round(eq, 2))
        out["equity"] = curve
    else:
        out["equity"] = []
    out["late"] = _rebuild_summary([tr for tr in (merged.trades if merged else [])
                                    if True], COMM).to_dict() if False else out["all"]
    # late/long/short splits recomputed via meta is skipped for grid speed; keep all
    return out


def load_split(split):
    if split == "train":
        rows = merge("results/MOMENTUM_SLICE/features/features.csv", OUT / "path_features_train.csv")
    else:
        rows = merge("results/MOMENTUM_SLICE/holdout/features.csv", OUT / "path_features_holdout.csv")
    cfg = load_config(); pq = Path(cfg["parquet_dir"])
    pairs = {}
    for sid in sorted({r["session"] for r in rows}):
        p = load_pair(pq, sid)
        if p is not None:
            pairs[sid] = p
    return rows, pairs


# Named cases for vol / pitch / shortlist validation: name -> (fn, offset, tp, sl_fn, label)
def named_cases():
    cases = {}
    # volatility-normalised pullback (pb10 relative to pre-move vol)
    for a in (0.25, 0.33, 0.4, 0.5, 0.75, 1.0):
        cases[f"VOL_range5_x{a}"] = (
            lambda r, a=a: (num(P(r), "pb10") or 0) >= a * (f(r, "range_5m_pts") or 1e9)
            and (num(P(r), "beyond_peak10") or 9) <= 1.0, 10, 10.0, lambda r: level_sl(r, 10.0))
    for a in (0.5, 0.75, 1.0, 1.25, 1.5):
        cases[f"VOL_medmin_x{a}"] = (
            lambda r, a=a: (num(P(r), "pb10") or 0) >= a * (f(r, "med_min_range_pts") or 1e9)
            and (num(P(r), "beyond_peak10") or 9) <= 1.0, 10, 10.0, lambda r: level_sl(r, 10.0))
    for a in (0.5, 0.75, 1.0, 1.5):
        cases[f"VOL_range1_x{a}"] = (
            lambda r, a=a: (num(P(r), "pb10") or 0) >= a * (f(r, "range_1m_pts") or 1e9)
            and (num(P(r), "beyond_peak10") or 9) <= 1.0, 10, 10.0, lambda r: level_sl(r, 10.0))
    # vol + absolute floor (avoid tiny-vol noise)
    cases["VOL_range5_x1_floor12"] = (
        lambda r: (num(P(r), "pb10") or 0) >= max(12.0, 1.0 * (f(r, "range_5m_pts") or 1e9))
        and (num(P(r), "beyond_peak10") or 9) <= 1.0, 10, 10.0, lambda r: level_sl(r, 10.0))
    cases["VOL_range5_x0.75_floor12"] = (
        lambda r: (num(P(r), "pb10") or 0) >= max(12.0, 0.75 * (f(r, "range_5m_pts") or 1e9))
        and (num(P(r), "beyond_peak10") or 9) <= 1.0, 10, 10.0, lambda r: level_sl(r, 10.0))
    # pitch stop variants on fixed-pb rule
    for buf, tp in ((2.0, 10.0), (2.0, 16.0), (5.0, 16.0), (2.0, 24.0)):
        cases[f"PITCHSL_buf{int(buf)}_tp{int(tp)}"] = (
            lambda r: base_pb(r, 20.0), 10, tp, lambda r, b=buf: pitch_sl(r, b))
    # fixed bracket shortlist slots are added dynamically in grid mode; named asym refs:
    for sl, tp in ((12, 32), (12, 16), (12, 12), (8, 32), (8, 20), (10, 24), (15, 32), (20, 10), (25, 10), (10, 10), (10, 16), (15, 15)):
        cases[f"FIXED_sl{sl}_tp{tp}"] = (
            lambda r: base_pb(r, 20.0), 10, float(tp), lambda r, s=float(sl): s)
    # combos: vol rule + pitch stop; long-hold asym
    cases["VOL_medmin_x0.75_PITCH_tp10"] = (
        lambda r: (num(P(r), "pb10") or 0) >= 0.75 * (f(r, "med_min_range_pts") or 1e9)
        and (num(P(r), "beyond_peak10") or 9) <= 1.0, 10, 10.0, lambda r: pitch_sl(r, 2.0))
    cases["VOL_range1_x0.75_PITCH_tp10"] = (
        lambda r: (num(P(r), "pb10") or 0) >= 0.75 * (f(r, "range_1m_pts") or 1e9)
        and (num(P(r), "beyond_peak10") or 9) <= 1.0, 10, 10.0, lambda r: pitch_sl(r, 2.0))
    cases["ASYM_sl12_tp32_t120"] = (
        lambda r: base_pb(r, 20.0), 10, 32.0, lambda r: 12.0, 120.0)
    cases["ASYM_sl12_tp32_t0"] = (
        lambda r: base_pb(r, 20.0), 10, 32.0, lambda r: 12.0, 0.0)
    cases["PITCH_tp24_t120"] = (
        lambda r: base_pb(r, 20.0), 10, 24.0, lambda r: pitch_sl(r, 2.0), 120.0)
    return cases


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="train")
    ap.add_argument("--mode", default="grid", choices=["grid", "cases"])
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    rows, pairs = load_split(args.split)
    results = {}
    if args.mode == "grid":
        for sl in (5, 8, 10, 12, 15, 20, 25, 30):
            for tp in (5, 8, 10, 12, 16, 20, 24, 32, 40):
                name = f"sl{sl}_tp{tp}"
                res = run(rows, lambda r: base_pb(r, 20.0), 10, float(tp), lambda r, s=float(sl): s, pairs)
                results[name] = res
                a = res["all"]
                print(f"{name}: n={a.get('n')} wr={a.get('wr')} pf={a.get('pf')} net={a.get('net')} dd={a.get('max_dd')}", flush=True)
    else:
        cases = named_cases()
        only = {s.strip() for s in args.only.split(",") if s.strip()}
        for name, case in cases.items():
            if only and name not in only:
                continue
            fn, off, tp, sl_fn = case[:4]
            tstop = case[4] if len(case) > 4 else 60.0
            res = run(rows, fn, off, tp, sl_fn, pairs, time_stop=tstop)
            results[name] = res
            a = res["all"]
            print(f"{name}: sel={res['n_selected']} n={a.get('n')} wr={a.get('wr')} pf={a.get('pf')} net={a.get('net')} dd={a.get('max_dd')}", flush=True)
    out_name = f"brackets_{args.mode}_{args.split}.json"
    (OUT / out_name).write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"-> {OUT / out_name}")


if __name__ == "__main__":
    main()
