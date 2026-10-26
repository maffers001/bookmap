"""EXP-0064 / H55 — Flow-inflection synchrony (C3).

EMA of cumulative delta slope sign-change in both markets within 1s → trade new direction.
Null: single-market inflection. Kill: dual beats single and random on OOS.
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


def _ema(x: np.ndarray, span: int) -> np.ndarray:
    alpha = 2 / (span + 1)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def _inflections(delta: np.ndarray, span: int = 30, min_slope: float = 2.0) -> list[tuple[int, int]]:
    """Return list of (index, new_side) where EMA slope changes sign with |slope|>=min."""
    if len(delta) < span + 2:
        return []
    cd = np.cumsum(delta)
    ema = _ema(cd, span)
    slope = np.diff(ema, prepend=ema[0])
    out = []
    prev = 0
    for i in range(1, len(slope)):
        s = 1 if slope[i] > min_slope else (-1 if slope[i] < -min_slope else 0)
        if s != 0 and prev != 0 and s != prev:
            out.append((i, s))
        if s != 0:
            prev = s
    return out


def _events(pair: H.Pair, *, dual: bool, cooldown_sec: float = 30.0) -> pa.Table:
    nq_b, es_b = pair.bars(100)
    t0, t1 = pair.bounds
    grid, nq_d, _, _ = densify_tape(nq_b, bar_ms=100, t0_ns=t0, t1_ns=t1)
    _, es_d, _, _ = densify_tape(es_b, bar_ms=100, t0_ns=t0, t1_ns=t1)
    if len(grid) == 0:
        return pa.table({})
    nq_inf = _inflections(nq_d)
    es_inf = {i: s for i, s in _inflections(es_d)}
    cool = int(cooldown_sec * 1e9)
    sync = 10  # 1s = 10 bars of 100ms
    events = []
    last = -10**18
    for i, side in nq_inf:
        if dual:
            # ES inflection same side within ±1s
            ok = False
            for j in range(max(0, i - sync), min(len(grid), i + sync + 1)):
                if j in es_inf and es_inf[j] == side:
                    ok = True
                    break
            if not ok:
                continue
        t = int(grid[i] + 100_000_000)
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
    out = Path(cfg["results_dir"]) / "EXP-0064"
    sessions = F.clean_sessions(pq)
    gate, br, sim = F.frozen_gate(), F.frozen_bracket(), F.frozen_sim_cfg()
    keys = ["dual", "nq_only", "null"]
    parts = {k: [] for k in keys}
    is_p = {k: [] for k in keys}
    oos_p = {k: [] for k in keys}
    n_ev = {k: 0 for k in keys}

    for i, sid in enumerate(sessions):
        pair = H.load_pair(pq, sid)
        if pair is None:
            continue
        dual = _events(pair, dual=True)
        single = _events(pair, dual=False)
        nul = H.null_events(pair, max(1, dual.num_rows), seed=6400 + i)
        for k, ev in (("dual", dual), ("nq_only", single), ("null", nul)):
            n_ev[k] += ev.num_rows
            if ev.num_rows == 0:
                continue
            s = simulate_session(sid, pair.nq, pair.es, sim, gate=gate, bracket_fn=br,
                                 vwap_from_ns=pair.vwap_from_ns, events=ev)
            parts[k].append(s)
            (is_p[k] if F.is_is(sid) else oos_p[k]).append(s)
        print(f"{sid} dual={dual.num_rows} nq={single.num_rows}", flush=True)

    def pack(k):
        def one(ps):
            if not ps:
                return {"n": 0, "net": 0.0, "avg": None, "wr": None}
            s = merge_summaries(ps, F.COMMISSION)
            d = s.to_dict(); d["avg"] = round(s.net / s.n, 2) if s.n else None
            return d
        return {"all": one(parts[k]), "is": one(is_p[k]), "oos": one(oos_p[k]), "events": n_ev[k]}

    books = {k: pack(k) for k in keys}
    D, T, N = books["dual"], books["nq_only"], books["null"]

    def beat(a, b):
        return a["oos"]["avg"] is not None and b["oos"]["avg"] is not None and a["oos"]["avg"] > b["oos"]["avg"]

    kill = {
        "min_events": n_ev["dual"] >= 200,
        "beat_twin_oos": beat(D, T),
        "beat_null_oos": beat(D, N),
        "oos_net_pos": D["oos"]["net"] > 0,
    }
    kill["pass"] = all(kill.values())
    md = [
        "# EXP-0064 — Flow-inflection synchrony",
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
    F.write_report(out, title="EXP-0064", md_lines=md, report={"exp": "EXP-0064", "books": books, "kill": kill})


if __name__ == "__main__":
    main()
