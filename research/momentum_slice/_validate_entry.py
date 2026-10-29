"""mat_sim validation for entry-criteria finalists from _sweep_entry.

Frozen brackets only (no re-fit):
  mom: level SL clamp 4-40, TP16, t60, entry per case
  pc : level SL clamp 4-40, TP10, t60, fade at confirm+5s

Usage:
  .venv\\Scripts\\python.exe -m research.momentum_slice._validate_entry --split train
  .venv\\Scripts\\python.exe -m research.momentum_slice._validate_entry --split holdout --only NAME,...
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
from research.momentum_slice.backtest_v1 import level_sl
from research.momentum_slice.rules import enrich, f, is_momentum_v1, is_pc_v1

OUT = Path("results/MOMENTUM_SLICE/entry_sweep")
COMM = 0.25


def g(r, k, default=None):
    v = f(r, k)
    return default if v is None else v


def base_mom(r):
    return is_momentum_v1(r)


def base_pc(r):
    return is_pc_v1(r)


CASES = [
    # name, family, filter, entry_offset_s, fade, tp
    ("MOM_V1_baseline", "mom", base_mom, 0, False, 16.0),
    ("MOM_LONG_only", "mom", lambda r: base_mom(r) and f(r, "side_int") == 1, 0, False, 16.0),
    ("MOM_trend>=10", "mom", lambda r: base_mom(r) and g(r, "trend_5m_pts", -1e9) >= 10, 0, False, 16.0),
    ("MOM_low_pullback<=0.3", "mom", lambda r: base_mom(r) and g(r, "nq_pullback_ratio_confirm", 9) <= 0.3, 0, False, 16.0),
    ("MOM_left_c2s_entry+2s", "mom", lambda r: f(r, "nq_left_behind_c2s") == 1 and g(r, "trend_5m_pts", -1e9) >= 0 and g(r, "nq_dom_vs_med", 0) >= 4, 2, False, 16.0),
    ("MOM_left_c5s_entry+5s", "mom", lambda r: f(r, "nq_left_behind_c5s") == 1 and g(r, "trend_5m_pts", -1e9) >= 0 and g(r, "nq_dom_vs_med", 0) >= 4, 5, False, 16.0),
    ("MOM_left_c5s_LONG_entry+5s", "mom", lambda r: f(r, "nq_left_behind_c5s") == 1 and g(r, "trend_5m_pts", -1e9) >= 0 and g(r, "nq_dom_vs_med", 0) >= 4 and f(r, "side_int") == 1, 5, False, 16.0),
    ("MOM_left_c5s_lowpb_entry+5s", "mom", lambda r: f(r, "nq_left_behind_c5s") == 1 and g(r, "trend_5m_pts", -1e9) >= 0 and g(r, "nq_dom_vs_med", 0) >= 4 and g(r, "nq_pullback_ratio_confirm", 9) <= 0.3, 5, False, 16.0),
    ("MOM_no_trend_gate", "mom", lambda r: f(r, "nq_left_behind_confirm") == 1 and g(r, "nq_dom_vs_med", 0) >= 4, 0, False, 16.0),
    ("PC_V1_baseline", "pc", base_pc, 5, True, 10.0),
    ("PC_strong_stall", "pc", lambda r: base_pc(r) and g(r, "nq_pullback_c5s_pts", 0) >= 8 and g(r, "nq_pullback_ratio_c5s", 0) >= 0.9 and g(r, "nq_post5_slow_ratio", 9) <= 0.7, 5, True, 10.0),
    ("PC_pullback>=6", "pc", lambda r: base_pc(r) and g(r, "nq_pullback_c5s_pts", 0) >= 6, 5, True, 10.0),
    ("PC_high_dom>=4", "pc", lambda r: base_pc(r) and g(r, "nq_dom_vs_med", 0) >= 4, 5, True, 10.0),
    ("PC_no_slow_gate", "pc", lambda r: f(r, "nq_left_behind_c5s") == 0 and g(r, "nq_pullback_c5s_pts", 0) >= 4 and g(r, "nq_pullback_ratio_c5s", 0) >= 0.70 and g(r, "nq_post5_net_pts", 9) <= 1.0, 5, True, 10.0),
    ("PC_loose_pull>=2_ratio>=0.5", "pc", lambda r: f(r, "nq_left_behind_c5s") == 0 and g(r, "nq_pullback_c5s_pts", 0) >= 2 and g(r, "nq_pullback_ratio_c5s", 0) >= 0.5, 5, True, 10.0),
    ("PC_fade_after_LONGslice", "pc", lambda r: base_pc(r) and f(r, "side_int") == 1, 5, True, 10.0),
    ("PC_fade_after_SHORTslice", "pc", lambda r: base_pc(r) and f(r, "side_int") == -1, 5, True, 10.0),
    # Round 2: logical combos of train winners (exploratory; holdout now seen)
    ("MOM_LONG_lowpb<=0.3", "mom", lambda r: base_mom(r) and f(r, "side_int") == 1 and g(r, "nq_pullback_ratio_confirm", 9) <= 0.3, 0, False, 16.0),
    ("MOM_LONG_trend>=10", "mom", lambda r: base_mom(r) and f(r, "side_int") == 1 and g(r, "trend_5m_pts", -1e9) >= 10, 0, False, 16.0),
    ("MOM_left_c5s_late_entry+5s", "mom", lambda r: f(r, "nq_left_behind_c5s") == 1 and g(r, "trend_5m_pts", -1e9) >= 0 and g(r, "nq_dom_vs_med", 0) >= 4 and r.get("tod") == "late", 5, False, 16.0),
    ("MOM_lowpb_late", "mom", lambda r: base_mom(r) and g(r, "nq_pullback_ratio_confirm", 9) <= 0.3 and r.get("tod") == "late", 0, False, 16.0),
    ("MOM_LONG_c5s_lowpb_entry+5s", "mom", lambda r: f(r, "nq_left_behind_c5s") == 1 and g(r, "trend_5m_pts", -1e9) >= 0 and g(r, "nq_dom_vs_med", 0) >= 4 and f(r, "side_int") == 1 and g(r, "nq_pullback_ratio_confirm", 9) <= 0.3, 5, False, 16.0),
    ("PC_loose_after_LONGslice", "pc", lambda r: f(r, "nq_left_behind_c5s") == 0 and g(r, "nq_pullback_c5s_pts", 0) >= 2 and g(r, "nq_pullback_ratio_c5s", 0) >= 0.5 and f(r, "side_int") == 1, 5, True, 10.0),
    ("PC_loose_late", "pc", lambda r: f(r, "nq_left_behind_c5s") == 0 and g(r, "nq_pullback_c5s_pts", 0) >= 2 and g(r, "nq_pullback_ratio_c5s", 0) >= 0.5 and r.get("tod") == "late", 5, True, 10.0),
]


def run_case(rows, fn, offset_s, fade, tp, pairs):
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
            s = int(float(r["side_int"]))
            if fade:
                s = -s
            ts.append(t); side.append(s); sls.append(level_sl(r, 10.0)); tps.append(tp)
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
    # equity curve (chronological)
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
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    feats = ("results/MOMENTUM_SLICE/features/features.csv" if args.split == "train"
             else "results/MOMENTUM_SLICE/holdout/features.csv")
    rows = [enrich(r) for r in csv.DictReader(open(feats, encoding="utf-8"))]
    cfg = load_config(); pq = Path(cfg["parquet_dir"])
    # preload pairs for sessions that any case might use
    sids = sorted({r["session"] for r in rows})
    print(f"loading {len(sids)} sessions...", flush=True)
    pairs = {}
    for sid in sids:
        p = load_pair(pq, sid)
        if p is not None:
            pairs[sid] = p
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    report = {"split": args.split, "cases": {}}
    lines = [f"# Entry validation ({args.split}) — frozen brackets", ""]
    for name, fam, fn, off, fade, tp in CASES:
        if only and name not in only:
            continue
        print(f"running {name}", flush=True)
        res = run_case(rows, fn, off, fade, tp, pairs)
        report["cases"][name] = res
        a = res["all"]
        lines.append(f"## {name} [{fam}]")
        lines.append(f"- sel={res['n_selected']} all: n={a.get('n')} wr={a.get('wr')} pf={a.get('pf')} net={a.get('net')} maxDD={a.get('max_dd')}")
        for sub in ("late", "open", "LONG", "SHORT"):
            s = res.get(sub, {})
            if s.get("n"):
                lines.append(f"- {sub}: n={s.get('n')} wr={s.get('wr')} pf={s.get('pf')} net={s.get('net')}")
        lines.append(f"- equity: {','.join(str(x) for x in res.get('equity', []))}")
        lines.append("")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"validate_{args.split}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (OUT / f"validate_{args.split}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
