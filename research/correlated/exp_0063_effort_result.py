"""EXP-0063 / H54 — Normalised effort/result divergence across markets (C2).

Progress per unit signed volume over W seconds; ES as control.
Pre-registered split: if ES continues (progress/effort stays high) → NQ catch-up;
if ES stalls → fade NQ. Kill: primary (fade-when-ES-stalls) beats NQ-only twin + null on OOS.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import merge_summaries, simulate_session
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.features.corr_divergence import densify_tape


def _effort_events(
    pair: H.Pair,
    *,
    window_sec: float = 10.0,
    cooldown_sec: float = 30.0,
    min_nq_vol: float = 40.0,
    nq_low_eff: float = 0.02,  # pts per unit signed vol — absorbed
    es_stall_eff: float = 0.05,
    es_cont_eff: float = 0.15,
    mode: str = "fade_es_stall",  # fade_es_stall | catchup_es_cont | nq_only_fade | null_random
) -> pa.Table:
    nq_b, es_b = pair.bars(100)
    t0, t1 = pair.bounds
    bar_ms = 100
    g_nq = densify_tape(nq_b, bar_ms=bar_ms, t0_ns=t0, t1_ns=t1)
    g_es = densify_tape(es_b, bar_ms=bar_ms, t0_ns=t0, t1_ns=t1)
    grid, nq_d, nq_v, nq_c = g_nq
    _, es_d, es_v, es_c = g_es
    if len(grid) == 0:
        return pa.table({})
    w = int(window_sec * 1000 / bar_ms)
    cool = int(cooldown_sec * 1e9)
    pts = 0.25
    events = []
    last = -10**18
    for i in range(w, len(grid)):
        nd = nq_d[i - w:i].sum()
        nv = nq_v[i - w:i].sum()
        if nv < min_nq_vol or abs(nd) < 1e-9:
            continue
        nq_prog = (nq_c[i - 1] - nq_c[i - w]) * pts if np.isfinite(nq_c[i - 1]) and np.isfinite(nq_c[i - w]) else 0.0
        # effort = progress in direction of delta / |delta|
        nq_eff = (nq_prog * np.sign(nd)) / abs(nd)
        if nq_eff > nq_low_eff:
            continue  # NQ not absorbed
        ed = es_d[i - w:i].sum()
        ev = es_v[i - w:i].sum()
        es_prog = (es_c[i - 1] - es_c[i - w]) * pts if np.isfinite(es_c[i - 1]) and np.isfinite(es_c[i - w]) else 0.0
        es_eff = (es_prog * np.sign(ed)) / abs(ed) if abs(ed) > 1e-9 and ev > 0 else 0.0

        d = int(np.sign(nd))
        if mode == "fade_es_stall":
            if es_eff > es_stall_eff:
                continue
            side = -d
        elif mode == "catchup_es_cont":
            if es_eff < es_cont_eff:
                continue
            side = d
        elif mode == "nq_only_fade":
            side = -d
        else:
            continue
        t = int(grid[i - 1] + bar_ms * 1_000_000)
        if t - last < cool:
            continue
        last = t
        events.append({"event_ts_ns": t, "side": side})
    if not events:
        return pa.table({})
    return pa.table({
        "event_ts_ns": np.array([e["event_ts_ns"] for e in events], dtype=np.int64),
        "side": np.array([e["side"] for e in events], dtype=np.int8),
    })


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0063"
    sessions = F.clean_sessions(pq)
    gate, br, sim = F.frozen_gate(), F.frozen_bracket(), F.frozen_sim_cfg()
    keys = ["fade_es_stall", "catchup_es_cont", "nq_only_fade", "null"]
    parts = {k: [] for k in keys}
    is_p = {k: [] for k in keys}
    oos_p = {k: [] for k in keys}
    n_ev = {k: 0 for k in keys}

    for i, sid in enumerate(sessions):
        pair = H.load_pair(pq, sid)
        if pair is None:
            continue
        for mode in ("fade_es_stall", "catchup_es_cont", "nq_only_fade"):
            ev = _effort_events(pair, mode=mode)
            n_ev[mode] += ev.num_rows
            if ev.num_rows == 0:
                continue
            s = simulate_session(sid, pair.nq, pair.es, sim, gate=gate, bracket_fn=br,
                                 vwap_from_ns=pair.vwap_from_ns, events=ev)
            parts[mode].append(s)
            (is_p[mode] if F.is_is(sid) else oos_p[mode]).append(s)
        nul = H.null_events(pair, max(1, n_ev["fade_es_stall"] and 5 or 5), seed=6300 + i)
        # match count to fade events this day
        fade = _effort_events(pair, mode="fade_es_stall")
        nul = H.null_events(pair, max(1, fade.num_rows), seed=6300 + i)
        n_ev["null"] += nul.num_rows
        sn = simulate_session(sid, pair.nq, pair.es, sim, gate=gate, bracket_fn=br,
                              vwap_from_ns=pair.vwap_from_ns, events=nul)
        parts["null"].append(sn)
        (is_p["null"] if F.is_is(sid) else oos_p["null"]).append(sn)
        print(f"{sid} fade_ev={fade.num_rows}", flush=True)

    def pack(k):
        def one(ps):
            if not ps:
                return {"n": 0, "net": 0.0, "avg": None, "wr": None}
            s = merge_summaries(ps, F.COMMISSION)
            d = s.to_dict(); d["avg"] = round(s.net / s.n, 2) if s.n else None
            return d
        return {"all": one(parts[k]), "is": one(is_p[k]), "oos": one(oos_p[k]), "events": n_ev[k]}

    books = {k: pack(k) for k in keys}
    P, T, N = books["fade_es_stall"], books["nq_only_fade"], books["null"]

    def beat(a, b):
        return a["oos"]["avg"] is not None and b["oos"]["avg"] is not None and a["oos"]["avg"] > b["oos"]["avg"]

    kill = {
        "min_events": n_ev["fade_es_stall"] >= 200,
        "beat_twin_oos": beat(P, T),
        "beat_null_oos": beat(P, N),
        "oos_net_pos": P["oos"]["net"] > 0,
    }
    kill["pass"] = all(kill.values())
    md = [
        "# EXP-0063 — Normalised effort/result divergence",
        "",
        "| book | events | n | net | avg | OOS net | OOS avg |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for k, v in books.items():
        md.append(
            f"| {k} | {v['events']} | {v['all']['n']} | {v['all']['net']:+.1f} | "
            f"{v['all'].get('avg')} | {v['oos']['net']:+.1f} | {v['oos'].get('avg')} |"
        )
    md += ["", f"## Verdict: **{'PASS' if kill['pass'] else 'FAIL / park'}**", str(kill), ""]
    F.write_report(out, title="EXP-0063", md_lines=md, report={"exp": "EXP-0063", "books": books, "kill": kill})


if __name__ == "__main__":
    main()
