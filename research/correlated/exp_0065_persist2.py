"""EXP-0065 / H56 — CFADE second-bar persistence entry (hysteresis)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import merge_summaries, simulate_session
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.features.corr_exhaust import ExhaustParams, _Bars, _first_exhaust_bar


def exhaust_persist2(
    confirm: pa.Table,
    nq_bars: pa.Table,
    es_bars: pa.Table,
    p: ExhaustParams,
    *,
    max_wait_sec: float = 5.0,
    persist_bars: int = 2,
) -> pa.Table:
    """Like exhaust_fade_events but require ``persist_bars`` consecutive exhaust bars.

    Stamp at the close of the last bar of the run (first-crossing + hysteresis).
    """
    if confirm.num_rows == 0:
        return pa.table({})
    nq = _Bars.from_table(nq_bars)
    es = _Bars.from_table(es_bars)
    bar_ns = int(p.bar_ms * 1_000_000)
    p2 = ExhaustParams(**{**p.__dict__, "max_wait_sec": max_wait_sec})

    ev_ts = confirm.column("event_ts_ns").to_numpy().astype(np.int64)
    sides = confirm.column("side").to_numpy().astype(int)
    es_vol = (
        confirm.column("es_total_vol").to_numpy().astype(np.float64)
        if "es_total_vol" in confirm.column_names
        else np.zeros(len(ev_ts))
    )

    out_ts, out_side, waits = [], [], []
    for i in range(len(ev_ts)):
        entry = int(ev_ts[i])
        side = int(sides[i])
        hit = _first_exhaust_bar(nq, es, entry, side, float(es_vol[i]), p2)
        if hit is None:
            continue
        b0, _reason, nq_i, _es_j = hit
        # need persist_bars-1 more consecutive bars also exhausting
        ok = True
        last_i = nq_i
        for k in range(1, persist_bars):
            # advance one bar: temporarily set entry to next bar start and skip_bars=0
            next_entry = int(nq.start[nq_i + k]) if (nq_i + k) < len(nq.start) else None
            if next_entry is None:
                ok = False
                break
            # check bar nq_i+k directly
            j = nq_i + k
            if side * nq.imb[j] < p.nq_min_imbalance:
                ok = False
                break
            if p.nq_min_vol > 0 and nq.vol[j] < p.nq_min_vol:
                ok = False
                break
            if p.nq_min_trades > 0 and nq.n_tr[j] < p.nq_min_trades:
                ok = False
                break
            # matching ES bar
            es_j = int(np.searchsorted(es.start, nq.start[j], side="left"))
            has_es = es_j < len(es.start) and es.start[es_j] == nq.start[j]
            if not (p.use_imb and has_es and es.vol[es_j] > 0 and side * es.imb[es_j] <= p.es_imb_max):
                ok = False
                break
            last_i = j
        if not ok:
            continue
        stamp = int(nq.start[last_i] + bar_ns)
        out_ts.append(stamp)
        out_side.append(-side)  # fade
        waits.append(stamp - entry)

    if not out_ts:
        return pa.table({})
    return pa.table({
        "event_ts_ns": np.array(out_ts, dtype=np.int64),
        "side": np.array(out_side, dtype=np.int8),
        "wait_ms": np.array(waits, dtype=np.float64) / 1e6,
    })


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0065"
    sessions = F.clean_sessions(pq)
    gate, br, sim = F.frozen_gate(), F.frozen_bracket(), F.frozen_sim_cfg()
    p_ex = F.FROZEN_EXHAUST

    parts = {"frozen": [], "persist2": []}
    is_p = {"frozen": [], "persist2": []}
    oos_p = {"frozen": [], "persist2": []}
    n_cand = {"frozen": 0, "persist2": 0}

    for sid in sessions:
        pair = H.load_pair(pq, sid)
        if pair is None:
            continue
        nq_b, es_b = pair.bars(100)
        confirm = H.confirm_bursts(pair)
        fades = F.cfade_events(pair)
        p2 = exhaust_persist2(confirm, nq_b, es_b, p_ex, max_wait_sec=5.0, persist_bars=2)
        n_cand["frozen"] += fades.num_rows
        n_cand["persist2"] += p2.num_rows
        for k, ev in (("frozen", fades), ("persist2", p2)):
            if ev.num_rows == 0:
                continue
            s = simulate_session(sid, pair.nq, pair.es, sim, gate=gate, bracket_fn=br,
                                 vwap_from_ns=pair.vwap_from_ns, events=ev)
            parts[k].append(s)
            (is_p[k] if F.is_is(sid) else oos_p[k]).append(s)
        print(f"{sid} frozen={fades.num_rows} persist2={p2.num_rows}", flush=True)

    def pack(k):
        def one(ps):
            if not ps:
                return {"n": 0, "net": 0.0, "avg": None, "wr": None}
            s = merge_summaries(ps, F.COMMISSION)
            d = s.to_dict(); d["avg"] = round(s.net / s.n, 2) if s.n else None
            return d
        return {"all": one(parts[k]), "is": one(is_p[k]), "oos": one(oos_p[k]), "candidates": n_cand[k]}

    books = {k: pack(k) for k in ("frozen", "persist2")}
    base, p2 = books["frozen"], books["persist2"]
    retain = (p2["all"]["n"] / base["all"]["n"]) if base["all"]["n"] else 0.0
    lift = None
    if p2["oos"]["avg"] is not None and base["oos"]["avg"] is not None:
        lift = p2["oos"]["avg"] - base["oos"]["avg"]
    kill = {
        "retain_frac": round(retain, 3),
        "oos_avg_lift": None if lift is None else round(lift, 3),
        "pass": retain >= 0.70 and lift is not None and lift >= 0.5,
    }
    md = [
        "# EXP-0065 — CFADE second-bar persistence",
        "",
        "| book | candidates | n | net | avg | OOS net | OOS avg |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for k, v in books.items():
        md.append(
            f"| {k} | {v['candidates']} | {v['all']['n']} | {v['all']['net']:+.1f} | "
            f"{v['all'].get('avg')} | {v['oos']['net']:+.1f} | {v['oos'].get('avg')} |"
        )
    md += ["", f"## Verdict: **{'PASS' if kill['pass'] else 'FAIL / park'}**", str(kill), ""]
    F.write_report(out, title="EXP-0065", md_lines=md, report={"exp": "EXP-0065", "books": books, "kill": kill})


if __name__ == "__main__":
    main()
