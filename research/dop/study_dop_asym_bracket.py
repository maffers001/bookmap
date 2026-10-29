"""All-month DOP break backtest: enter at DOP±offset, fixed asym brackets.

Default (NQ): entry first print at DOP + side*5 pts after the break;
SL 20 / TP 40. Shorts mirrored. ES scaled 2 / 8 / 16.

Usage:
  python -m research.dop.study_dop_asym_bracket
  python -m research.dop.study_dop_asym_bracket --nq-offset 5 --nq-sl 20 --nq-tp 40
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.backtest.mat_sim import MatSimConfig, SimSummary, simulate_session
from research.config import load_config
from research.correlated import harness as H
from research.dop.exp_0078_dop import (
    _month,
    all_sessions,
    compute_break_history,
    load_pair_full,
)
from research.features.daily_open import DopBreak


def _date_key(sid: str) -> tuple[int, int, int]:
    return (int(sid[4:6]), int(sid[2:4]), int(sid[0:2]))


def find_offset_entry(
    trades: pa.Table,
    bk: DopBreak,
    *,
    offset_pts: float,
    window_end_ns: int,
    pts_per_tick: float = 0.25,
) -> tuple[int, float] | None:
    """First print at/beyond DOP + side*offset after the break."""
    if trades.num_rows == 0:
        return None
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64) * pts_per_tick
    o = np.argsort(ts, kind="stable")
    ts, px = ts[o], px[o]
    k = (ts >= bk.event_ts_ns) & (ts <= window_end_ns)
    if not k.any():
        return None
    ts_w, px_w = ts[k], px[k]
    target = bk.dop_pts + bk.side * float(offset_pts)
    if bk.side > 0:
        hit = px_w >= target
    else:
        hit = px_w <= target
    if not hit.any():
        return None
    i = int(np.argmax(hit))
    return int(ts_w[i]), float(px_w[i])


def sim_from_trades(trades: list) -> SimSummary:
    from research.backtest.mat_sim import _rebuild_summary
    return _rebuild_summary(list(trades), H.COMMISSION)


def _avg(s: SimSummary) -> float:
    return (s.net / s.n) if s.n else float("nan")


def run(
    *,
    nq_offset: float = 5.0,
    nq_sl: float = 20.0,
    nq_tp: float = 40.0,
    es_offset: float = 2.0,
    es_sl: float = 8.0,
    es_tp: float = 16.0,
) -> dict:
    cfg = load_config()
    parquet_dir = Path(cfg["parquet_dir"])
    out_dir = Path(cfg["results_dir"]) / "DOP_ASYM_BRACKET"
    out_dir.mkdir(parents=True, exist_ok=True)

    sids = all_sessions(parquet_dir, {"smoke_synthetic"})
    sids = sorted(sids, key=_date_key)
    print(f"[asym] {len(sids)} sessions; building DOP breaks", flush=True)
    hist = compute_break_history(
        parquet_dir, sids,
        {"grace_min": 5, "pierce_ticks": 1, "prior_sessions": 14,
         "max_first_print_delay_sec": 60},
    )

    brackets = {
        "NQ": (nq_offset, nq_sl, nq_tp),
        "ES": (es_offset, es_sl, es_tp),
    }
    all_trades: dict[str, list] = {"NQ": [], "ES": [], "BOTH": []}
    event_rows: list[dict] = []
    skip: dict[str, int] = defaultdict(int)

    for sid in sids:
        pair = load_pair_full(parquet_dir, sid)
        if pair is None:
            skip["no_pair"] += 1
            continue
        open_ns, win_end = pair.bounds
        sh = hist.get(sid) or {}
        for mkt in ("NQ", "ES"):
            st = sh.get(mkt) or {}
            bk: DopBreak | None = st.get("break")
            if bk is None:
                skip[f"{mkt}_no_break"] += 1
                continue
            offset, sl, tp = brackets[mkt]
            tr = pair.nq if mkt == "NQ" else pair.es
            if tr.num_rows == 0:
                skip[f"{mkt}_empty"] += 1
                continue
            entry = find_offset_entry(tr, bk, offset_pts=offset, window_end_ns=win_end)
            if entry is None:
                skip[f"{mkt}_no_offset"] += 1
                continue
            ets, epx = entry
            ev = pa.table({
                "event_ts_ns": pa.array([ets], pa.int64()),
                "side": pa.array([int(bk.side)], pa.int8()),
                "tp_pts": pa.array([float(tp)], pa.float64()),
                "sl_pts": pa.array([float(sl)], pa.float64()),
                "dop_pts": pa.array([float(bk.dop_pts)], pa.float64()),
                "break_ts_ns": pa.array([int(bk.event_ts_ns)], pa.int64()),
                "entry_offset_pts": pa.array([float(offset)], pa.float64()),
            })
            sim_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=H.COMMISSION)
            s = simulate_session(
                sid, tr, pair.es, sim_cfg,
                vwap_from_ns=open_ns, events=ev,
            )
            all_trades[mkt].extend(s.trades)
            all_trades["BOTH"].extend(s.trades)
            event_rows.append({
                "session_id": sid, "market": mkt, "month": _month(sid),
                "side": int(bk.side), "break_ts_ns": int(bk.event_ts_ns),
                "event_ts_ns": ets, "entry_px_pts": epx, "dop_pts": bk.dop_pts,
                "offset_pts": offset, "sl_pts": sl, "tp_pts": tp,
                "lag_break_to_entry_sec": (ets - bk.event_ts_ns) / 1e9,
                "pnl": float(s.trades[0].pnl) if s.trades else float("nan"),
                "net": float(s.trades[0].pnl - H.COMMISSION) if s.trades else float("nan"),
                "reason": s.trades[0].reason if s.trades else "",
            })
        print(f"  [asym] {sid}", flush=True)

    # write events
    if event_rows:
        cols = {k: [r[k] for r in event_rows] for k in event_rows[0]}
        pq.write_table(pa.table({
            "session_id": cols["session_id"],
            "market": cols["market"],
            "month": cols["month"],
            "side": pa.array(cols["side"], pa.int8()),
            "break_ts_ns": pa.array(cols["break_ts_ns"], pa.int64()),
            "event_ts_ns": pa.array(cols["event_ts_ns"], pa.int64()),
            "entry_px_pts": pa.array(cols["entry_px_pts"], pa.float64()),
            "dop_pts": pa.array(cols["dop_pts"], pa.float64()),
            "offset_pts": pa.array(cols["offset_pts"], pa.float64()),
            "sl_pts": pa.array(cols["sl_pts"], pa.float64()),
            "tp_pts": pa.array(cols["tp_pts"], pa.float64()),
            "lag_break_to_entry_sec": pa.array(cols["lag_break_to_entry_sec"], pa.float64()),
            "pnl": pa.array(cols["pnl"], pa.float64()),
            "net": pa.array(cols["net"], pa.float64()),
            "reason": cols["reason"],
        }), out_dir / "trades.parquet", compression="zstd")

    report: dict = {
        "brackets": {"NQ": {"offset": nq_offset, "sl": nq_sl, "tp": nq_tp},
                     "ES": {"offset": es_offset, "sl": es_sl, "tp": es_tp}},
        "skip": dict(skip),
        "books": {},
    }
    md = [
        "# DOP asym bracket — all months",
        "",
        f"Entry: first print at DOP ± offset after DOP break (grace 5 min, 1-tick pierce). "
        f"NQ offset/SL/TP = {nq_offset}/{nq_sl}/{nq_tp}; "
        f"ES = {es_offset}/{es_sl}/{es_tp}. "
        f"1-tick slip, 0.25 pt/RT, UK 14:30–21:00, flat-only.",
        "",
        f"Skips: {dict(skip)}",
        "",
        "## Overall",
        "",
        "| book | n | WR | net | avg | maxDD |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for book in ("NQ", "ES", "BOTH"):
        sim = sim_from_trades(all_trades[book])
        avg = _avg(sim)
        report["books"][book] = {**sim.to_dict(), "avg": avg}
        md.append(
            f"| {book} | {sim.n} | {sim.wr if sim.n else float('nan'):.3f} | "
            f"{sim.net:+.1f} | {avg:+.2f} | {sim.max_dd:.1f} |"
        )

    # by month / side / reason
    md += ["", "## By month (NQ)", "",
           "| month | n | WR | net | avg |", "|---|---:|---:|---:|---:|"]
    by_m: dict[str, list] = defaultdict(list)
    for t in all_trades["NQ"]:
        by_m[_month(t.session_id)].append(t)
    for m in sorted(by_m):
        s = sim_from_trades(by_m[m])
        md.append(f"| {m} | {s.n} | {s.wr:.3f} | {s.net:+.1f} | {_avg(s):+.2f} |")
        report.setdefault("nq_by_month", {})[m] = {**s.to_dict(), "avg": _avg(s)}

    md += ["", "## By month (ES)", "",
           "| month | n | WR | net | avg |", "|---|---:|---:|---:|---:|"]
    by_m = defaultdict(list)
    for t in all_trades["ES"]:
        by_m[_month(t.session_id)].append(t)
    for m in sorted(by_m):
        s = sim_from_trades(by_m[m])
        md.append(f"| {m} | {s.n} | {s.wr:.3f} | {s.net:+.1f} | {_avg(s):+.2f} |")
        report.setdefault("es_by_month", {})[m] = {**s.to_dict(), "avg": _avg(s)}

    for book in ("NQ", "ES"):
        longs = [t for t in all_trades[book] if t.side > 0]
        shorts = [t for t in all_trades[book] if t.side < 0]
        sl = sim_from_trades(longs)
        ss = sim_from_trades(shorts)
        md += [
            "", f"## Side split ({book})", "",
            f"Long: n={sl.n} net={sl.net:+.1f} avg={_avg(sl):+.2f}",
            f"Short: n={ss.n} net={ss.net:+.1f} avg={_avg(ss):+.2f}",
        ]
        reasons: dict[str, int] = defaultdict(int)
        for t in all_trades[book]:
            reasons[t.reason] += 1
        md.append(f"Exit reasons: {dict(reasons)}")
        report.setdefault("side", {})[book] = {
            "long": {**sl.to_dict(), "avg": _avg(sl)},
            "short": {**ss.to_dict(), "avg": _avg(ss)},
            "reasons": dict(reasons),
        }

    # lag stats
    if event_rows:
        lags = np.array([r["lag_break_to_entry_sec"] for r in event_rows], dtype=float)
        md += [
            "", "## Entry lag (break → offset fill)", "",
            f"n={len(lags)} · median={np.median(lags):.1f}s · p90={np.percentile(lags, 90):.1f}s · "
            f"mean={np.mean(lags):.1f}s",
        ]
        report["entry_lag_sec"] = {
            "median": float(np.median(lags)),
            "p90": float(np.percentile(lags, 90)),
            "mean": float(np.mean(lags)),
        }

    H.write_report(out_dir, report, md)
    print(md[md.index("## Overall"):md.index("## Overall") + 8], flush=True)
    print(f"Wrote {out_dir}", flush=True)
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nq-offset", type=float, default=5.0)
    ap.add_argument("--nq-sl", type=float, default=20.0)
    ap.add_argument("--nq-tp", type=float, default=40.0)
    ap.add_argument("--es-offset", type=float, default=2.0)
    ap.add_argument("--es-sl", type=float, default=8.0)
    ap.add_argument("--es-tp", type=float, default=16.0)
    args = ap.parse_args()
    run(
        nq_offset=args.nq_offset, nq_sl=args.nq_sl, nq_tp=args.nq_tp,
        es_offset=args.es_offset, es_sl=args.es_sl, es_tp=args.es_tp,
    )


if __name__ == "__main__":
    main()
