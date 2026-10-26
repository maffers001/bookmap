"""EXP-0062 / H53 — Synthetic-basis dislocation ± flow (C1)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import merge_summaries, simulate_session
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H


def _sec_grid(nq: pa.Table, es: pa.Table, t0: int, t1: int):
    """1s last-close returns for NQ and ES; signed NQ aggressor vol per second."""
    sec = 1_000_000_000
    g0 = (t0 // sec) * sec
    g1 = (t1 // sec) * sec
    if g1 <= g0:
        return None
    n = int((g1 - g0) // sec)
    grid = g0 + np.arange(n, dtype=np.int64) * sec

    def densify(t: pa.Table):
        close = np.full(n, np.nan)
        delta = np.zeros(n)
        if t.num_rows == 0:
            return close, delta
        ts = t.column("ts_ns").to_numpy().astype(np.int64)
        px = t.column("price").to_numpy().astype(np.float64)
        if "is_buy_aggressor" in t.column_names:
            side = np.where(t.column("is_buy_aggressor").to_numpy().astype(bool), 1, -1)
        elif "is_buy" in t.column_names:
            side = np.where(t.column("is_buy").to_numpy().astype(bool), 1, -1)
        else:
            side = np.ones(len(ts), dtype=int)
        sz = t.column("size").to_numpy().astype(np.float64) if "size" in t.column_names else np.ones(len(ts))
        idx = ((ts - g0) // sec).astype(np.int64)
        ok = (idx >= 0) & (idx < n)
        for i, p, s, z in zip(idx[ok], px[ok], side[ok], sz[ok]):
            close[i] = p
            delta[i] += s * z
        last = np.nan
        for i in range(n):
            if np.isfinite(close[i]):
                last = close[i]
            else:
                close[i] = last
        return close, delta

    nq_c, nq_d = densify(nq)
    es_c, _ = densify(es)
    if not np.isfinite(nq_c).any() or not np.isfinite(es_c).any():
        return None
    nq_r = np.diff(nq_c, prepend=nq_c[0])
    es_r = np.diff(es_c, prepend=es_c[0])
    nq_r[~np.isfinite(nq_r)] = 0.0
    es_r[~np.isfinite(es_r)] = 0.0
    return grid, nq_r, es_r, nq_d, nq_c


def _events_for_session(pair: H.Pair, *, z_entry: float = 2.0, with_flow: bool | None = True,
                        use_es: bool = True, residual_sec: int = 5, beta_sec: int = 1800,
                        cooldown_sec: int = 30, flow_min: float = 20.0) -> pa.Table:
    g = _sec_grid(pair.nq, pair.es, pair.bounds[0], pair.bounds[1])
    if g is None:
        return pa.table({})
    grid, nq_r, es_r, nq_d, _nq_c = g
    n = len(grid)
    beta_w = beta_sec
    res_w = residual_sec
    # Precompute rolling sums via cumsum
    c_nq = np.cumsum(nq_r)
    c_es = np.cumsum(es_r)
    c_fl = np.cumsum(nq_d)
    # Rolling β via expanding window sums of x^2 and xy, then sliding
    # Sample β every second using last beta_w — O(n) with running moments on a deque-like ring
    # Simple: recompute β every 30s only, hold between updates
    beta_hold = np.full(n, np.nan)
    step = 30
    for i in range(beta_w, n, step):
        y = nq_r[i - beta_w:i]
        x = es_r[i - beta_w:i]
        if use_es:
            vx = float(np.dot(x, x))
            beta = float(np.dot(x, y) / vx) if vx > 1e-12 else np.nan
        else:
            beta = 0.0
        beta_hold[i:min(i + step, n)] = beta

    # Residual series at each i (using held β)
    resid = np.full(n, np.nan)
    flow = np.zeros(n)
    for i in range(max(beta_w, res_w), n):
        b = beta_hold[i]
        if not np.isfinite(b):
            continue
        rn = c_nq[i - 1] - (c_nq[i - res_w - 1] if i - res_w - 1 >= 0 else 0.0)
        re = c_es[i - 1] - (c_es[i - res_w - 1] if i - res_w - 1 >= 0 else 0.0)
        resid[i] = rn - b * re
        flow[i] = c_fl[i - 1] - (c_fl[i - res_w - 1] if i - res_w - 1 >= 0 else 0.0)

    # Rolling mean/std of resid over 1800s via cumsum of resid and resid^2 (nan→0)
    r0 = np.nan_to_num(resid, nan=0.0)
    ok = np.isfinite(resid).astype(np.float64)
    cs = np.cumsum(r0)
    cs2 = np.cumsum(r0 * r0)
    csn = np.cumsum(ok)
    z_win = 1800
    events = []
    last_fire = -10**18
    cool = cooldown_sec * 1_000_000_000
    for i in range(max(beta_w, res_w) + z_win, n):
        r = resid[i]
        if not np.isfinite(r):
            continue
        j0 = i - z_win
        cnt = csn[i - 1] - (csn[j0 - 1] if j0 > 0 else 0.0)
        if cnt < 60:
            continue
        s1 = cs[i - 1] - (cs[j0 - 1] if j0 > 0 else 0.0)
        s2 = cs2[i - 1] - (cs2[j0 - 1] if j0 > 0 else 0.0)
        mu = s1 / cnt
        var = max(s2 / cnt - mu * mu, 0.0)
        sd = var ** 0.5
        if sd < 1e-9:
            continue
        z = (r - mu) / sd
        if abs(z) < z_entry:
            continue
        fl = float(flow[i])
        flow_with = (np.sign(fl) == np.sign(r)) and abs(fl) >= flow_min
        if with_flow is True and not flow_with:
            continue
        if with_flow is False and flow_with:
            continue
        t = int(grid[i] + 1_000_000_000)
        if t - last_fire < cool:
            continue
        last_fire = t
        if with_flow is True:
            side = 1 if r > 0 else -1
        else:
            side = -1 if r > 0 else 1
        events.append({"event_ts_ns": t, "side": side, "z": z, "beta": float(beta_hold[i]), "resid": r, "flow": fl})
    if not events:
        return pa.table({})
    return pa.table({
        "event_ts_ns": np.array([e["event_ts_ns"] for e in events], dtype=np.int64),
        "side": np.array([e["side"] for e in events], dtype=np.int8),
        "z": np.array([e["z"] for e in events], dtype=np.float64),
        "beta": np.array([e["beta"] for e in events], dtype=np.float64),
    })


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0062"
    sessions = F.clean_sessions(pq)
    gate = F.frozen_gate()
    br = F.frozen_bracket()
    sim = F.frozen_sim_cfg()

    keys = ["with_flow_cont", "without_flow_fade", "uncond_fade", "twin_nq_only_fade"]
    parts = {k: [] for k in keys}
    is_p = {k: [] for k in keys}
    oos_p = {k: [] for k in keys}
    n_ev = {k: 0 for k in keys}
    betas = []

    for sid in sessions:
        pair = H.load_pair(pq, sid)
        if pair is None:
            continue
        builders = {
            "with_flow_cont": dict(with_flow=True, use_es=True),
            "without_flow_fade": dict(with_flow=False, use_es=True),
            "uncond_fade": dict(with_flow=None, use_es=True),
            "twin_nq_only_fade": dict(with_flow=None, use_es=False),
        }
        # uncond and twin: force fade side
        line = [sid]
        for k, kw in builders.items():
            ev = _events_for_session(pair, **kw)
            # for uncond/twin force fade interpretation
            if k in ("uncond_fade", "twin_nq_only_fade") and ev.num_rows:
                # rebuild sides as fade of residual: already set when with_flow is None/False
                pass
            n_ev[k] += ev.num_rows
            if ev.num_rows and "beta" in ev.column_names:
                betas.extend(ev.column("beta").to_pylist())
            if ev.num_rows == 0:
                line.append(f"{k}=0")
                continue
            s = simulate_session(sid, pair.nq, pair.es, sim, gate=gate, bracket_fn=br,
                                 vwap_from_ns=pair.vwap_from_ns, events=ev)
            parts[k].append(s)
            (is_p[k] if F.is_is(sid) else oos_p[k]).append(s)
            line.append(f"{k} n={s.n} {s.net:+.0f}")
        print(" ".join(line), flush=True)

    def pack(k):
        def one(ps):
            if not ps:
                return {"n": 0, "net": 0.0, "avg": None, "wr": None}
            s = merge_summaries(ps, F.COMMISSION)
            d = s.to_dict()
            d["avg"] = round(s.net / s.n, 2) if s.n else None
            return d
        return {"all": one(parts[k]), "is": one(is_p[k]), "oos": one(oos_p[k]), "events": n_ev[k]}

    books = {k: pack(k) for k in keys}
    # Primary: without_flow_fade (mean-reversion when price moved without tape)
    primary = books["without_flow_fade"]
    null = books["uncond_fade"]
    twin = books["twin_nq_only_fade"]

    def beat(a, b):
        return a["oos"]["avg"] is not None and b["oos"]["avg"] is not None and a["oos"]["avg"] > b["oos"]["avg"]

    kill = {
        "min_events": n_ev["without_flow_fade"] >= 200,
        "beat_null_oos": beat(primary, null),
        "beat_twin_oos": beat(primary, twin),
        "oos_net_pos": primary["oos"]["net"] > 0,
    }
    kill["pass"] = all(kill.values())
    beta_arr = np.array(betas) if betas else np.array([])
    beta_stats = {
        "n": len(beta_arr),
        "mean": float(np.mean(beta_arr)) if len(beta_arr) else None,
        "std": float(np.std(beta_arr)) if len(beta_arr) else None,
        "p10": float(np.percentile(beta_arr, 10)) if len(beta_arr) else None,
        "p90": float(np.percentile(beta_arr, 90)) if len(beta_arr) else None,
    }

    md = [
        "# EXP-0062 — Synthetic-basis dislocation ± flow",
        "",
        f"β stats at events: {beta_stats}",
        "",
        "| book | events | n | net | avg | IS net | OOS net | OOS avg |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for k, v in books.items():
        a, o = v["all"], v["oos"]
        md.append(
            f"| {k} | {v['events']} | {a['n']} | {a['net']:+.1f} | {a.get('avg')} | "
            f"{v['is']['net']:+.1f} | {o['net']:+.1f} | {o.get('avg')} |"
        )
    md += ["", f"## Verdict: **{'PASS' if kill['pass'] else 'FAIL / park'}**", str(kill), ""]
    F.write_report(out, title="EXP-0062", md_lines=md, report={
        "exp": "EXP-0062", "books": books, "kill": kill, "beta_stats": beta_stats,
    })


if __name__ == "__main__":
    main()
