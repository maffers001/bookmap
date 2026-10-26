"""EXP-0066 / H57 — Tier D count-first book/MBO ideas.

Counts dual near-touch size drops (co-pull proxy) and BBO-pressure divergence
on depth sessions. If n>=200, stratify frozen CFADE/MAT by state at trigger.
Iceberg: MBO coverage only (detector not built until counts justify).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import merge_summaries
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H


def _bbo_size_series(depth: pa.Table, sample_ms: int = 100):
    if depth.num_rows == 0:
        return np.zeros(0, np.int64), np.zeros(0), np.zeros(0), np.zeros(0), np.zeros(0)
    d_ts = depth.column("ts_ns").to_numpy().astype(np.int64)
    d_bid = depth.column("is_bid").to_numpy().astype(bool)
    d_px = depth.column("price_ticks").to_numpy().astype(np.int64)
    d_sz = depth.column("size").to_numpy().astype(np.int64)
    order = np.argsort(d_ts, kind="mergesort")
    d_ts, d_bid, d_px, d_sz = d_ts[order], d_bid[order], d_px[order], d_sz[order]
    bids: dict[int, int] = {}
    asks: dict[int, int] = {}
    bb, ba = -10**18, 10**18
    sample_ns = sample_ms * 1_000_000
    next_s = int(d_ts[0]) + sample_ns
    out = []
    for i in range(len(d_ts)):
        is_bid, px, sz = bool(d_bid[i]), int(d_px[i]), int(d_sz[i])
        book = bids if is_bid else asks
        if sz <= 0:
            book.pop(px, None)
            if is_bid and px == bb:
                bb = max(book) if book else -10**18
            elif (not is_bid) and px == ba:
                ba = min(book) if book else 10**18
        else:
            book[px] = sz
            if is_bid and px > bb:
                bb = px
            if (not is_bid) and px < ba:
                ba = px
        ts = int(d_ts[i])
        if ts < next_s:
            continue
        next_s = ts + sample_ns
        if bb <= -10**17 or ba >= 10**17 or bb >= ba:
            continue
        bbs = float(bids.get(bb, 0))
        bas = float(asks.get(ba, 0))
        imb = (bas - bbs) / (bas + bbs) if (bas + bbs) > 0 else 0.0
        out.append((ts, bb, ba, bbs, bas, imb))
    if not out:
        return np.zeros(0, np.int64), np.zeros(0), np.zeros(0), np.zeros(0), np.zeros(0)
    arr = np.array(out)
    return arr[:, 0].astype(np.int64), arr[:, 3], arr[:, 4], arr[:, 5], arr[:, 1]


def _co_pull_count(nq_s, es_s, *, drop_frac: float = 0.4, sync_ms: int = 1000) -> int:
    """Count times both books lose >=drop_frac of bid OR ask size within sync window."""
    nq_ts, nq_bbs, nq_bas, _, _ = nq_s
    es_ts, es_bbs, es_bas, _, _ = es_s
    if len(nq_ts) < 2 or len(es_ts) < 2:
        return 0
    sync_ns = sync_ms * 1_000_000
    count = 0
    # NQ pull events
    for i in range(1, len(nq_ts)):
        bid_drop = nq_bbs[i - 1] > 0 and nq_bbs[i] <= (1 - drop_frac) * nq_bbs[i - 1]
        ask_drop = nq_bas[i - 1] > 0 and nq_bas[i] <= (1 - drop_frac) * nq_bas[i - 1]
        if not (bid_drop or ask_drop):
            continue
        t = int(nq_ts[i])
        side_bid = bid_drop
        # find ES sample near t
        j = int(np.searchsorted(es_ts, t, side="left"))
        ok = False
        for jj in (j - 1, j, j + 1):
            if 0 <= jj < len(es_ts) and abs(int(es_ts[jj]) - t) <= sync_ns:
                if jj == 0:
                    continue
                if side_bid:
                    if es_bbs[jj - 1] > 0 and es_bbs[jj] <= (1 - drop_frac) * es_bbs[jj - 1]:
                        ok = True
                else:
                    if es_bas[jj - 1] > 0 and es_bas[jj] <= (1 - drop_frac) * es_bas[jj - 1]:
                        ok = True
        if ok:
            count += 1
    return count


def _pressure_div_at(ts_arr, imb, t0: int, thr: float = 0.25) -> str:
    if len(ts_arr) == 0:
        return "unknown"
    i = int(np.searchsorted(ts_arr, t0, side="right") - 1)
    if i < 0:
        return "unknown"
    return "lean_ask" if imb[i] >= thr else ("lean_bid" if imb[i] <= -thr else "neutral")


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0066"
    sessions = F.clean_sessions(pq)
    cov = F.depth_mbo_coverage(pq, sessions)

    total_copull = 0
    depth_used = 0
    # CFADE stratification by NQ vs ES pressure disagreement at trigger
    strata_cfade = {"agree": [], "disagree": [], "neutral": [], "unknown": []}
    strata_mat = {"agree": [], "disagree": [], "neutral": [], "unknown": []}

    for sid in sessions:
        if sid not in cov["depth_sessions"]:
            continue
        pair = H.load_pair(pq, sid)
        if pair is None:
            continue
        nq_d = H.load_depth_window(pq, sid, "NQ", pair.bounds)
        es_d = H.load_depth_window(pq, sid, "ES", pair.bounds)
        if nq_d.num_rows == 0 or es_d.num_rows == 0:
            continue
        depth_used += 1
        nq_s = _bbo_size_series(nq_d)
        es_s = _bbo_size_series(es_d)
        cp = _co_pull_count(nq_s, es_s)
        total_copull += cp

        # Stratify frozen CFADE / MAT
        fades = F.cfade_events(pair)
        sc = F.simulate_cfade(pair, events=fades)
        sm = F.simulate_mat(pair)
        nq_ts, _, _, nq_imb, _ = nq_s
        es_ts, _, _, es_imb, _ = es_s

        def classify(t0: int) -> str:
            nq_st = _pressure_div_at(nq_ts, nq_imb, t0)
            es_st = _pressure_div_at(es_ts, es_imb, t0)
            if nq_st == "unknown" or es_st == "unknown":
                return "unknown"
            if nq_st == "neutral" or es_st == "neutral":
                return "neutral"
            return "agree" if nq_st == es_st else "disagree"

        for t in sc.trades:
            strata_cfade[classify(t.entry_ts_ns)].append(t)
        for t in sm.trades:
            strata_mat[classify(t.entry_ts_ns)].append(t)
        print(f"{sid} copull={cp} cfade_n={sc.n} mat_n={sm.n}", flush=True)

    def stratum_stats(trades):
        if not trades:
            return {"n": 0, "net": 0.0, "avg": None, "oos_n": 0, "oos_net": 0.0, "oos_avg": None}
        nets = [t.pnl - F.COMMISSION for t in trades]
        oos = [t for t in trades if not F.is_is(t.session_id)]
        oos_nets = [t.pnl - F.COMMISSION for t in oos]
        return {
            "n": len(trades),
            "net": round(sum(nets), 2),
            "avg": round(float(np.mean(nets)), 2),
            "oos_n": len(oos),
            "oos_net": round(sum(oos_nets), 2) if oos else 0.0,
            "oos_avg": round(float(np.mean(oos_nets)), 2) if oos else None,
        }

    cfade_s = {k: stratum_stats(v) for k, v in strata_cfade.items()}
    mat_s = {k: stratum_stats(v) for k, v in strata_mat.items()}
    book_avg_cfade = stratum_stats([t for v in strata_cfade.values() for t in v])

    promote = False
    promote_notes = []
    if total_copull >= 200:
        promote_notes.append(f"co-pull n={total_copull} >= 200 — eligible for filter dig (not auto-promoted)")
    else:
        promote_notes.append(f"co-pull n={total_copull} < 200 — no filter dig")

    # disagree as skip filter on CFADE?
    dis = cfade_s["disagree"]
    if dis["oos_n"] >= 20 and dis["oos_avg"] is not None and book_avg_cfade["oos_avg"] is not None:
        lift = dis["oos_avg"] - book_avg_cfade["oos_avg"]
        if lift >= 1.0:
            promote = True
            promote_notes.append(f"disagree stratum OOS lift {lift:+.2f} — PASS filter bar")
        else:
            promote_notes.append(f"disagree stratum OOS lift {lift:+.2f} < +1.0 — FAIL")
    else:
        promote_notes.append("disagree stratum underpowered or empty OOS")

    md = [
        "# EXP-0066 — Tier D count-first",
        "",
        f"Depth sessions used: {depth_used}/{cov['n_depth']} · MBO sessions: {cov['n_mbo']}",
        f"Co-pull proxy events (dual near-touch size drop ≥40% within 1s): **{total_copull}**",
        "",
        "## CFADE stratified by NQ/ES BBO-pressure state at entry",
        "",
        "| state | n | net | avg | OOS n | OOS net | OOS avg |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for k, v in cfade_s.items():
        md.append(
            f"| {k} | {v['n']} | {v['net']:+.1f} | {v.get('avg')} | "
            f"{v['oos_n']} | {v['oos_net']:+.1f} | {v.get('oos_avg')} |"
        )
    md += [
        "",
        "## MAT stratified (descriptive)",
        "",
        "| state | n | net | avg | OOS n | OOS avg |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for k, v in mat_s.items():
        md.append(
            f"| {k} | {v['n']} | {v['net']:+.1f} | {v.get('avg')} | {v['oos_n']} | {v.get('oos_avg')} |"
        )
    md += [
        "",
        "## Iceberg / paired-large",
        "",
        f"MBO coverage {cov['n_mbo']}/{cov['n_sessions']} — detector deferred (count-first gate for "
        "paired events not yet built; no standalone entry).",
        "",
        "## Verdict",
        "",
        f"**{'PROMOTE filter dig' if promote else 'PARK'}**",
        *[f"- {n}" for n in promote_notes],
        "",
    ]
    F.write_report(out, title="EXP-0066", md_lines=md, report={
        "exp": "EXP-0066",
        "coverage": cov,
        "depth_used": depth_used,
        "co_pull_events": total_copull,
        "cfade_strata": cfade_s,
        "mat_strata": mat_s,
        "promote": promote,
        "notes": promote_notes,
    })


if __name__ == "__main__":
    main()
