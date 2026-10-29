"""Daily/monthly equity for LONG momentum + PC2 (pitch stop) combined."""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import MatSimConfig, merge_summaries, simulate_session
from research.config import load_config
from research.correlated.harness import load_pair
from research.momentum_slice._pc_brackets import pitch_sl
from research.momentum_slice._pc_validate2 import merge, num, P
from research.momentum_slice.backtest_v1 import level_sl
from research.momentum_slice.rules import enrich, f, is_momentum_v1

OUT = Path("results/MOMENTUM_SLICE/pc_review")
COMM = 0.25


def parse_session(sid: str) -> tuple[str, str]:
    dd, mm, yy = sid[:2], sid[2:4], sid[4:6]
    return f"20{yy}-{mm}-{dd}", f"20{yy}-{mm}"


def base_pc2(r) -> bool:
    return (num(P(r), "pb10") or 0) >= 20 and (num(P(r), "beyond_peak10") or 9) <= 1.0


def collect_trades(rows, fn, offset_s, side_fn, tp, sl_fn, pairs, time_stop=60.0, tag=""):
    by = {}
    for r in rows:
        if not fn(r):
            continue
        by.setdefault(r["session"], []).append(r)
    sim_cfg = MatSimConfig(tp_pts=tp, sl_pts=10.0, commission_pts_rt=COMM,
                           signal_cooldown_sec=30, time_stop_sec=time_stop, trail_pts=0)
    parts = []
    for sid, evs in sorted(by.items()):
        pair = pairs.get(sid)
        if pair is None:
            continue
        ts, side, sls, tps = [], [], [], []
        for r in evs:
            t = int(float(r["confirm_ns"])) + int(offset_s * 1_000_000_000)
            ts.append(t)
            side.append(side_fn(r))
            sls.append(float(sl_fn(r)))
            tps.append(tp)
        tbl = pa.table({
            "event_ts_ns": np.array(ts, dtype=np.int64),
            "side": np.array(side, dtype=np.int8),
            "sl_pts": np.array(sls, dtype=np.float64),
            "tp_pts": np.array(tps, dtype=np.float64),
        })
        parts.append(simulate_session(sid, pair.nq, pair.es, sim_cfg, events=tbl,
                                      vwap_from_ns=pair.bounds[0]))
    merged = merge_summaries(parts, COMM) if parts else None
    out = []
    if not merged:
        return out
    for tr in merged.trades:
        day, month = parse_session(tr.session_id)
        out.append({
            "session": tr.session_id, "day": day, "month": month,
            "entry_ts_ns": tr.entry_ts_ns, "side": int(tr.side),
            "net": tr.pnl - COMM, "tag": tag,
        })
    return out


def summarize(trades, label):
    trades = sorted(trades, key=lambda t: t["entry_ts_ns"])
    by_day = defaultdict(list)
    by_month = defaultdict(list)
    by_tag = defaultdict(float)
    for t in trades:
        by_day[t["day"]].append(t)
        by_month[t["month"]].append(t)
        by_tag[t["tag"]] += t["net"]
    lines = [f"# {label}", "",
             f"Total n={len(trades)} net={sum(t['net'] for t in trades):+.2f}"]
    for k, v in sorted(by_tag.items()):
        lines.append(f"- {k}: {v:+.2f}")
    lines += ["", "## Monthly", "| month | n | mom_n | pc_n | net | cum | win_d | lose_d |",
              "|---|---|---|---|---|---|---|---|"]
    cum = 0.0
    month_eq = []
    for month in sorted(by_month):
        xs = by_month[month]
        net = sum(t["net"] for t in xs)
        cum += net
        month_eq.append(round(cum, 2))
        mom_n = sum(1 for t in xs if t["tag"] == "MOM")
        pc_n = sum(1 for t in xs if t["tag"] == "PC2")
        days = sorted({t["day"] for t in xs})
        day_nets = {d: sum(t["net"] for t in xs if t["day"] == d) for d in days}
        wd = sum(1 for d in days if day_nets[d] > 0)
        ld = sum(1 for d in days if day_nets[d] < 0)
        lines.append(f"| {month} | {len(xs)} | {mom_n} | {pc_n} | {net:+.2f} | {cum:+.2f} | {wd} | {ld} |")
    lines += ["", "## Daily", "| day | n | mom | pc | net | cum |",
              "|---|---|---|---|---|---|"]
    cum = 0.0
    day_eq = []
    for day in sorted(by_day):
        xs = by_day[day]
        net = sum(t["net"] for t in xs)
        cum += net
        day_eq.append(round(cum, 2))
        mom_n = sum(1 for t in xs if t["tag"] == "MOM")
        pc_n = sum(1 for t in xs if t["tag"] == "PC2")
        lines.append(f"| {day} | {len(xs)} | {mom_n} | {pc_n} | {net:+.2f} | {cum:+.2f} |")
    lines.append("")
    lines.append(f"Month equity: {','.join(str(x) for x in month_eq)}")
    lines.append(f"Day equity: {','.join(str(x) for x in day_eq)}")
    return "\n".join(lines) + "\n", day_eq, month_eq


def run_split(split):
    if split == "train":
        feat = "results/MOMENTUM_SLICE/features/features.csv"
        path = OUT / "path_features_train.csv"
    else:
        feat = "results/MOMENTUM_SLICE/holdout/features.csv"
        path = OUT / "path_features_holdout.csv"
    # mom uses features only; pc needs path join
    mom_rows = [enrich(r) for r in csv.DictReader(open(feat, encoding="utf-8"))]
    pc_rows = merge(feat, path)
    cfg = load_config(); pq = Path(cfg["parquet_dir"])
    sids = sorted({r["session"] for r in mom_rows} | {r["session"] for r in pc_rows})
    pairs = {}
    for sid in sids:
        p = load_pair(pq, sid)
        if p is not None:
            pairs[sid] = p
    print(f"{split}: sessions={len(pairs)}", flush=True)
    mom = collect_trades(
        mom_rows,
        lambda r: is_momentum_v1(r) and f(r, "side_int") == 1,
        0, lambda r: 1, 16.0, lambda r: level_sl(r, 10.0), pairs, tag="MOM")
    pc = collect_trades(
        pc_rows, base_pc2, 10,
        lambda r: -int(float(r["side_int"])),
        10.0, lambda r: pitch_sl(r, 2.0), pairs, tag="PC2")
    print(f"  mom n={len(mom)} net={sum(t['net'] for t in mom):+.2f}")
    print(f"  pc2 n={len(pc)} net={sum(t['net'] for t in pc):+.2f}")
    combo = mom + pc
    text, day_eq, month_eq = summarize(combo, f"{split} LONG-MOM + PC2 (pitch SL / TP10)")
    out = OUT / f"combo_daily_monthly_{split}.md"
    out.write_text(text, encoding="utf-8")
    # also write trade csv
    with open(OUT / f"combo_trades_{split}.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["tag", "session", "day", "month", "entry_ts_ns", "side", "net"])
        w.writeheader()
        for t in sorted(combo, key=lambda x: x["entry_ts_ns"]):
            w.writerow(t)
    print(text)
    return day_eq, month_eq


def main():
    run_split("train")
    run_split("holdout")


if __name__ == "__main__":
    main()
