"""EXP-0051 / H42 — volume-ratio shock gate on frozen CFADE.

Stage 0: coverage / causality / power on DIG4 23d (park if the shock or
         non-shock candidate stratum has n < 20 — counts are the result).
Stage 1: discovery P&L (comparator = ER60 CFADE; gated = keep z >= 1.0
         only; random-keep placebo; MAT stratum descriptive).
Stage 2: May read once only if Stage 1 passes.
Java is out of this runner — conditional after a May pass.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    combine_gates,
    filter_summary,
    make_daytype_skip,
    make_trend_mode,
    make_vol_bracket,
    make_vol_bracket_rr,
    make_vwap_side,
    simulate_session,
)
from research.correlated import harness as H
from research.correlated.exp_0036_fade_dig import _concentration
from research.correlated.exp_0049_divergence import DIG4_23
from research.features.corr_exhaust import ExhaustParams, exhaust_fade_events
from research.features.vol_ratio import (
    VolRatioParams,
    VolRatioSeries,
    vol_ratio_at,
    vol_ratio_handcheck,
    vol_ratio_series,
)
from research.run_mat_may import MAY


def _sim(
    pairs: list[H.Pair],
    by_sid: dict[str, pa.Table],
    *,
    gate=None,
    bracket_fn=None,
    slip: float = 1.0,
    cooldown: float = 5.0,
) -> SimSummary:
    cfg = MatSimConfig(
        slip_ticks=slip,
        commission_pts_rt=H.COMMISSION,
        tp_pts=10,
        sl_pts=10,
        signal_cooldown_sec=cooldown,
        es_stamp="confirm",
    )
    parts = [
        simulate_session(
            p.sid, p.nq, p.es, cfg, gate=gate, bracket_fn=bracket_fn,
            vwap_from_ns=p.vwap_from_ns, events=by_sid[p.sid],
        )
        for p in pairs if p.sid in by_sid
    ]
    return H.merge(parts) if parts else SimSummary()


def _filter_events(ev: pa.Table, mask: np.ndarray) -> pa.Table:
    if ev.num_rows == 0:
        return ev
    idx = np.where(mask)[0]
    return ev.take(idx) if len(idx) else ev.slice(0, 0)


def _annotate(ev: pa.Table, series: VolRatioSeries) -> pa.Table:
    if ev.num_rows == 0:
        return ev
    ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
    vals = vol_ratio_at(series, ts)
    for col, key in (
        ("vr_z", "z"), ("vr_ratio", "ratio"),
        ("vr_vol_nq", "vol_nq"), ("vr_vol_es", "vol_es"),
        ("vr_nq_spike", "nq_spike"),
    ):
        if col in ev.column_names:
            ev = ev.drop([col])
        ev = ev.append_column(col, pa.array(np.asarray(vals[key], dtype=np.float64)))
    return ev


def _truncation_check(pair: H.Pair, p: VolRatioParams, *, frac: float = 0.7) -> dict:
    """Values at completed seconds <= T on full session match trunc-at-T series."""
    t0, t1 = pair.bounds
    sec_ns = int(p.grid_sec) * 1_000_000_000
    T_raw = t0 + int(frac * (t1 - t0))
    T = (T_raw // sec_ns) * sec_ns
    full = vol_ratio_series(pair.nq, pair.es, t0_ns=t0, t1_ns=t1, p=p)

    def _cut(t: pa.Table) -> pa.Table:
        if t.num_rows == 0:
            return t
        ts = t.column("ts_ns").to_numpy().astype(np.int64)
        k = ts < (T + sec_ns)
        return t.take(np.where(k)[0]) if k.any() else t.slice(0, 0)

    trunc = vol_ratio_series(_cut(pair.nq), _cut(pair.es), t0_ns=t0, t1_ns=T + sec_ns, p=p)
    trunc_map = {int(e): j for j, e in enumerate(trunc.end_ns.tolist())}
    diffs = []
    n_cmp = 0
    for i in np.where(full.end_ns <= T)[0]:
        e = int(full.end_ns[i])
        j = trunc_map.get(e)
        if j is None:
            diffs.append(("missing_in_trunc", e))
            break
        n_cmp += 1
        for a, b in ((full.z[i], trunc.z[j]), (full.ratio[i], trunc.ratio[j])):
            both_nan = (not np.isfinite(a)) and (not np.isfinite(b))
            if both_nan:
                continue
            if not (np.isfinite(a) and np.isfinite(b) and abs(float(a) - float(b)) < 1e-9):
                diffs.append((e, float(a) if np.isfinite(a) else None,
                              float(b) if np.isfinite(b) else None))
                break
        if len(diffs) > 5:
            break
    return {
        "session": pair.sid, "T_frac": frac, "T_ns": int(T),
        "n_compared": n_cmp, "n_diff": len(diffs),
        "match": len(diffs) == 0, "diffs_sample": diffs[:3],
    }


def _placebo_p90(trades, *, keep_n: int, draws: int, seed: int, commission: float) -> dict:
    """Trade-level random-keep null: keep ``keep_n`` comparator trades at random."""
    pnls = np.asarray([t.pnl - commission for t in trades], dtype=np.float64)
    n = len(pnls)
    if n == 0 or keep_n <= 0:
        return {"p90_avg": float("nan"), "p50_avg": float("nan"), "n": 0, "keep_n": keep_n}
    k = min(keep_n, n)
    rng = np.random.default_rng(seed)
    avgs = np.empty(draws, dtype=np.float64)
    for d in range(draws):
        avgs[d] = float(pnls[rng.choice(n, size=k, replace=False)].mean())
    return {
        "p90_avg": float(np.quantile(avgs, 0.90)),
        "p50_avg": float(np.median(avgs)),
        "p10_avg": float(np.quantile(avgs, 0.10)),
        "n": n, "keep_n": k, "draws": draws,
    }


def _avg(r: dict) -> float:
    n = r.get("n") or 0
    return (r["net"] / n) if n else float("nan")


def _label(z: float, cuts: list[float], shock: float) -> str:
    if not np.isfinite(z):
        return "warmup"
    if z >= shock:
        return "shock"
    if z < cuts[0]:
        return "low"
    return "mid"


def _cohort_avg_ok(sim_comp, sim_gated, cohort_name: str) -> bool:
    def avg_of(sim) -> float:
        sub = filter_summary(sim, lambda t: H.cohort(t.session_id) == cohort_name, H.COMMISSION)
        return (sub.net / sub.n) if sub.n else float("nan")

    a_c, a_g = avg_of(sim_comp), avg_of(sim_gated)
    if not np.isfinite(a_c):
        return True
    if not np.isfinite(a_g):
        return False
    return a_g + 1e-9 >= a_c


def run(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    parquet_dir = Path(cfg["parquet_dir"])
    results_dir = Path(cfg["results_dir"]) / exp["name"]
    exclude = set(str(s) for s in exp.get("exclude_sessions") or [])
    slip = float(exp.get("slip_ticks", 1.0))
    cooldown = float(exp.get("signal_cooldown_sec", 5.0))
    kill = dict(exp.get("kill") or {})
    raw = dict(exp.get("vol_ratio") or {})
    placebo_draws = int(raw.get("placebo_draws", 1000))
    placebo_seed = int(raw.get("placebo_seed", 51))
    vp = VolRatioParams(
        grid_sec=int(raw.get("grid_sec", 1)),
        window_sec=int(raw.get("window_sec", 10)),
        baseline_lookback_min=int(raw.get("baseline_lookback_min", 30)),
        baseline_warmup_min=int(raw.get("baseline_warmup_min", 5)),
        shock_z=float(raw.get("shock_z", 1.0)),
        nq_spike_mult=float(raw.get("nq_spike_mult", 2.0)),
    )
    strata_cuts = [float(x) for x in (raw.get("strata_z") or [0.0, 1.0])]
    er_max = float(exp.get("cfade_er_max", 0.30))
    fade_wait = float(exp.get("cfade_max_wait_sec", 5.0))
    p_ex = ExhaustParams(**(exp.get("cfade_exhaust") or {}))

    discovery_ids = [s for s in DIG4_23 if s not in exclude]
    pairs: list[H.Pair] = []
    for sid in discovery_ids:
        p = H.load_pair(parquet_dir, sid)
        if p is not None:
            pairs.append(p)
    sids = [p.sid for p in pairs]
    print(f"[0051] discovery {len(pairs)}/{len(DIG4_23)} DIG4 sessions", flush=True)

    br = make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=4.0, cap=40.0)
    gate_er = make_daytype_skip(max_er=er_max)

    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        exp.get("description", "").strip(),
        "",
        f"Discovery={len(sids)} DIG4 sessions · UK 14:30–17:00 · {slip}-tick · "
        f"{H.COMMISSION} pt/RT · window={vp.window_sec}s baseline={vp.baseline_lookback_min}m "
        f"shock_z={vp.shock_z} · CFADE ER≤{er_max} · RR SL1/TP2",
        "",
        f"train={len([s for s in sids if H.cohort(s)=='train'])} "
        f"hold={len([s for s in sids if H.cohort(s)=='hold'])} "
        f"new={len([s for s in sids if H.cohort(s)=='new'])}",
        "",
    ]
    report: dict = {
        "status": "ok", "sessions": sids,
        "vol_ratio": asdict(vp), "kill": kill, "stages": {},
    }

    # ---------------------------------------------------------------- emit fades + ratio
    by_sid_all: dict[str, pa.Table] = {}
    series_by_sid: dict[str, VolRatioSeries] = {}
    for pair in pairs:
        nq_b, es_b = pair.bars(100)
        bursts = H.confirm_bursts(pair)
        fades = exhaust_fade_events(bursts, nq_b, es_b, p_ex, max_wait_sec=fade_wait)
        series = vol_ratio_series(pair.nq, pair.es, t0_ns=pair.bounds[0], t1_ns=pair.bounds[1], p=vp)
        series_by_sid[pair.sid] = series
        if fades.num_rows:
            fades = H._with_session(fades, pair.sid)
            by_sid_all[pair.sid] = _annotate(fades, series)
        print(
            f"  {pair.sid}: fades={fades.num_rows} "
            f"z_finite={int(np.isfinite(series.z).sum())}/{len(series.z)}",
            flush=True,
        )

    # ---------------------------------------------------------------- Stage 0
    print("[0051] Stage 0 — coverage / causality / power", flush=True)
    all_z = (
        np.concatenate([ev.column("vr_z").to_numpy().astype(np.float64)
                        for ev in by_sid_all.values()])
        if by_sid_all else np.zeros(0)
    )
    n_cand = len(all_z)
    n_warm = int((~np.isfinite(all_z)).sum())
    n_low = int((np.isfinite(all_z) & (all_z < strata_cuts[0])).sum())
    n_mid = int((np.isfinite(all_z) & (all_z >= strata_cuts[0]) & (all_z < vp.shock_z)).sum())
    n_shock = int((np.isfinite(all_z) & (all_z >= vp.shock_z)).sum())
    n_nonshock = n_low + n_mid
    finite_ratio = (
        np.concatenate([ev.column("vr_ratio").to_numpy().astype(np.float64)
                        for ev in by_sid_all.values()])
        if by_sid_all else np.zeros(0)
    )
    finite_ratio = finite_ratio[np.isfinite(finite_ratio)]
    coverage = {
        "n_candidates": n_cand, "warmup": n_warm, "low": n_low, "mid": n_mid,
        "shock": n_shock, "nonshock_finite": n_nonshock,
        "ratio_median": round(float(np.median(finite_ratio)), 4) if len(finite_ratio) else None,
        "ratio_p90": round(float(np.quantile(finite_ratio, 0.90)), 4) if len(finite_ratio) else None,
    }
    report["stages"]["0_coverage"] = coverage
    md += [
        "## Stage 0 — coverage, causality & power",
        "",
        f"Fade candidates n=**{n_cand}** · warmup={n_warm} · low(z<{strata_cuts[0]:g})={n_low} · "
        f"mid={n_mid} · shock(z>={vp.shock_z:g})={n_shock} · non-shock finite={n_nonshock}",
        f"Candidate ratio median={coverage['ratio_median']} p90={coverage['ratio_p90']}",
        "",
    ]

    trunc_rows = [_truncation_check(pair, vp) for pair in pairs[:3]]
    trunc_ok = all(r["match"] for r in trunc_rows) if trunc_rows else False
    report["stages"]["0_truncation"] = trunc_rows
    report["causality_truncation_ok"] = trunc_ok
    md.append(f"Truncation invariance (3 sessions, frac=0.7): **{'PASS' if trunc_ok else 'FAIL'}**")
    for r in trunc_rows:
        md.append(f"- {r['session']}: compared={r['n_compared']} diffs={r['n_diff']} match={r['match']}")
    md.append("")

    hand = []
    for pair in pairs:
        ev = by_sid_all.get(pair.sid)
        if ev is None or ev.num_rows == 0:
            continue
        z = ev.column("vr_z").to_numpy().astype(np.float64)
        ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        for i in np.where(np.isfinite(z))[0][:1]:
            h = vol_ratio_handcheck(pair.nq, pair.es, t0_ns=pair.bounds[0],
                                    event_ts_ns=int(ts[i]), p=vp)
            h["session"] = pair.sid
            h["event_ts_ns"] = int(ts[i])
            hand.append(h)
        if len(hand) >= 3:
            break
    hand_ok = all(h["match"] for h in hand) if hand else False
    report["stages"]["0_handcheck"] = hand
    report["causality_handcheck_ok"] = hand_ok
    md.append(f"Hand-check vs raw-trade window sums ({len(hand)} samples): **{'PASS' if hand_ok else 'FAIL'}**")
    for h in hand:
        md.append(
            f"- {h['session']}: ratio_at={h['ratio_at']:.4f} direct={h['ratio_direct']:.4f} "
            f"z={h['z_at']:+.3f} match={h['match']}"
        )
    md.append("")

    if not trunc_ok or not hand_ok:
        report["verdict"] = "reject — Stage 0 causality failed"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        return {"summary": report}

    min_shock_c = int(exp.get("min_shock_candidates", 20))
    power_ok = n_shock >= min_shock_c and n_nonshock >= min_shock_c
    report["stages"]["0_power"] = {
        "min_shock_candidates": min_shock_c, "shock": n_shock,
        "nonshock_finite": n_nonshock, "pass": power_ok,
    }
    md.append(
        f"Stage 0 power (shock >= {min_shock_c} and non-shock >= {min_shock_c}): "
        f"**{'PASS' if power_ok else 'FAIL'}**"
    )
    md.append("")
    if not power_ok:
        report["verdict"] = (
            "reject / park — Stage 0 underpowered (counts are the result; no P&L, no May, no retune)"
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        return {"summary": report}

    # ---------------------------------------------------------------- Stage 1
    print("[0051] Stage 1 — discovery P&L", flush=True)
    sim_comp = _sim(pairs, by_sid_all, gate=gate_er, bracket_fn=br, slip=slip, cooldown=cooldown)
    r_comp = H.pack_pnl(sim_comp, sids)
    conc_comp = _concentration(sim_comp.trades, sids)
    avg_comp = _avg(r_comp)

    z_lookup: dict[tuple[str, int], float] = {}
    spike_lookup: dict[tuple[str, int], float] = {}
    for sid, ev in by_sid_all.items():
        ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        zz = ev.column("vr_z").to_numpy().astype(np.float64)
        sp = ev.column("vr_nq_spike").to_numpy().astype(np.float64)
        for t, v, s in zip(ts, zz, sp):
            z_lookup[(sid, int(t))] = float(v)
            spike_lookup[(sid, int(t))] = float(s)
    kept_trades = [
        t for t in sim_comp.trades
        if np.isfinite(z_lookup.get((t.session_id, t.entry_ts_ns), float("nan")))
        and z_lookup[(t.session_id, t.entry_ts_ns)] >= vp.shock_z
    ]
    kept_ids = {id(t) for t in kept_trades}
    dropped_trades = [t for t in sim_comp.trades if id(t) not in kept_ids]
    n_kept_joined, n_dropped = len(kept_trades), len(dropped_trades)

    by_sid_gated: dict[str, pa.Table] = {}
    n_drop_warm = n_drop_nonshock = 0
    for sid, ev in by_sid_all.items():
        z = ev.column("vr_z").to_numpy().astype(np.float64)
        keep = np.isfinite(z) & (z >= vp.shock_z)
        n_drop_warm += int((~np.isfinite(z)).sum())
        n_drop_nonshock += int((np.isfinite(z) & (z < vp.shock_z)).sum())
        fe = _filter_events(ev, keep)
        if fe.num_rows:
            by_sid_gated[sid] = fe
    sim_gated = _sim(pairs, by_sid_gated, gate=gate_er, bracket_fn=br, slip=slip, cooldown=cooldown)
    r_gated = H.pack_pnl(sim_gated, sids)
    conc_gated = _concentration(sim_gated.trades, sids)
    avg_gated = _avg(r_gated)

    stratum_books = {}
    for name, mask_fn in (
        ("low", lambda z: np.isfinite(z) & (z < strata_cuts[0])),
        ("mid", lambda z: np.isfinite(z) & (z >= strata_cuts[0]) & (z < vp.shock_z)),
        ("shock", lambda z: np.isfinite(z) & (z >= vp.shock_z)),
        ("warmup", lambda z: ~np.isfinite(z)),
    ):
        by = {}
        for sid, ev in by_sid_all.items():
            z = ev.column("vr_z").to_numpy().astype(np.float64)
            fe = _filter_events(ev, mask_fn(z))
            if fe.num_rows:
                by[sid] = fe
        sim = _sim(pairs, by, gate=gate_er, bracket_fn=br, slip=slip, cooldown=cooldown)
        rr = H.pack_pnl(sim, sids)
        stratum_books[name] = {**rr, "avg": _avg(rr),
                               "n_events": sum(t.num_rows for t in by.values())}

    # Descriptive NQ-spike x shock split on comparator trades
    spike_cells: dict[str, dict] = {}
    for cell, pred in (
        ("spike+shock", lambda sp, z: sp == 1.0 and np.isfinite(z) and z >= vp.shock_z),
        ("spike+nonshock", lambda sp, z: sp == 1.0 and np.isfinite(z) and z < vp.shock_z),
        ("nospike+shock", lambda sp, z: sp == 0.0 and np.isfinite(z) and z >= vp.shock_z),
        ("nospike+nonshock", lambda sp, z: sp == 0.0 and np.isfinite(z) and z < vp.shock_z),
        ("warmup_or_unknown", lambda sp, z: not np.isfinite(z) or not np.isfinite(sp)),
    ):
        pnls = [
            t.pnl - H.COMMISSION for t in sim_comp.trades
            if pred(spike_lookup.get((t.session_id, t.entry_ts_ns), float("nan")),
                    z_lookup.get((t.session_id, t.entry_ts_ns), float("nan")))
        ]
        arr = np.asarray(pnls, dtype=np.float64)
        spike_cells[cell] = {
            "n": int(len(arr)),
            "net": round(float(arr.sum()), 2) if len(arr) else 0.0,
            "avg": round(float(arr.mean()), 3) if len(arr) else None,
        }

    placebo = _placebo_p90(sim_comp.trades, keep_n=r_gated["n"], draws=placebo_draws,
                           seed=placebo_seed, commission=H.COMMISSION)

    # MAT secondary stratum
    print("[0051] MAT secondary stratum …", flush=True)
    mat_cfg = MatSimConfig(slip_ticks=slip, commission_pts_rt=H.COMMISSION, es_stamp="causal")
    mat_gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    mat_parts = [
        simulate_session(p.sid, p.nq, p.es, mat_cfg, gate=mat_gate,
                         bracket_fn=make_vol_bracket(1.0), vwap_from_ns=p.vwap_from_ns)
        for p in pairs
    ]
    mat_all = H.merge(mat_parts)
    mat_by: dict[str, list[float]] = {"warmup": [], "low": [], "mid": [], "shock": []}
    for tr in mat_all.trades:
        series = series_by_sid.get(tr.session_id)
        v = vol_ratio_at(series, tr.entry_ts_ns)["z"] if series is not None else float("nan")
        mat_by[_label(float(v), strata_cuts, vp.shock_z)].append(tr.pnl - H.COMMISSION)
    mat_stratum = {}
    for lab, pnls in mat_by.items():
        arr = np.asarray(pnls, dtype=np.float64)
        mat_stratum[lab] = {
            "n": int(len(arr)),
            "net": round(float(arr.sum()), 2) if len(arr) else 0.0,
            "avg": round(float(arr.mean()), 3) if len(arr) else None,
        }

    md += [
        "## Stage 1 — discovery P&L (DIG4 23d)",
        "",
        "Comparator = CFADE against_m04 + ER60≤0.30 + RR SL1/TP2. "
        "Gated = same, candidates pre-filtered to z≥shock (warmup dropped).",
        "Stratum books are report-only (not a partition of comparator trades).",
        "",
        *H.PNL_HDR,
        H.pnl_row("comparator (ER60)", r_comp),
        H.pnl_row("gated (keep shock)", r_gated),
    ]
    for name in ("low", "mid", "shock", "warmup"):
        md.append(H.pnl_row(f"stratum {name}", stratum_books[name]))
    md += [
        "",
        f"Comparator avg={avg_comp:.3f} (n={r_comp['n']}) · "
        f"gated avg={avg_gated:.3f} (n={r_gated['n']}) · Δavg={avg_gated - avg_comp:+.3f}",
        f"Candidates dropped: warmup={n_drop_warm} nonshock={n_drop_nonshock} · "
        f"comparator trades joined kept (shock) n={n_kept_joined} dropped n={n_dropped}",
        f"Placebo (trade-level random-keep, keep n={placebo['keep_n']}, {placebo_draws} draws): "
        f"p50_avg={placebo['p50_avg']:+.3f} p90_avg={placebo['p90_avg']:+.3f}",
        f"Concentration comparator days+={conc_comp['days_pos']}/{len(sids)} "
        f"top3={conc_comp['top3_share_of_pos']} · gated days+={conc_gated['days_pos']} "
        f"top3={conc_gated['top3_share_of_pos']}",
        "",
        "### NQ-spike × ratio on comparator trades (descriptive)",
        "",
        "| cell | n | net | avg |",
        "|---|---:|---:|---:|",
    ]
    for cell, c in spike_cells.items():
        md.append(f"| {cell} | {c['n']} | {c['net']:+.1f} | {c['avg'] if c['avg'] is not None else '–'} |")
    md += [
        "",
        "### MAT secondary stratum (descriptive — no ship)",
        "",
        "| stratum | n | net | avg |",
        "|---|---:|---:|---:|",
    ]
    for lab in ("warmup", "low", "mid", "shock"):
        m = mat_stratum[lab]
        md.append(f"| {lab} | {m['n']} | {m['net']:+.1f} | {m['avg'] if m['avg'] is not None else '–'} |")
    md.append("")

    min_kept = int(kill.get("min_kept_n", 20))
    min_dropped = int(kill.get("min_dropped_n", 20))
    min_lift = float(kill.get("min_avg_lift_pts", 1.0))
    min_ret = float(kill.get("min_net_retained_frac", 0.60))
    checks = {
        "kept_n_ok": r_gated["n"] >= min_kept,
        "dropped_n_ok": n_dropped >= min_dropped,
        "avg_lift_ok": bool(np.isfinite(avg_gated) and np.isfinite(avg_comp)
                            and avg_gated >= avg_comp + min_lift),
        "net_retained_ok": bool(r_gated["net"] >= min_ret * r_comp["net"]) if r_comp["net"] > 0
                           else bool(r_gated["net"] >= r_comp["net"]),
        "train_avg_ok": _cohort_avg_ok(sim_comp, sim_gated, "train"),
        "hold_avg_ok": _cohort_avg_ok(sim_comp, sim_gated, "hold"),
        "maxdd_ok": r_gated["max_dd"] <= r_comp["max_dd"] + 1e-9,
        "placebo_ok": bool(np.isfinite(avg_gated) and np.isfinite(placebo["p90_avg"])
                           and avg_gated > placebo["p90_avg"]),
    }
    stage1_pass = all(checks.values())
    report["stages"]["1"] = {
        "comparator": {**r_comp, "avg": avg_comp, "concentration": conc_comp},
        "gated": {**r_gated, "avg": avg_gated, "concentration": conc_gated},
        "stratum_books": stratum_books,
        "n_kept_joined": n_kept_joined, "n_dropped_joined": n_dropped,
        "spike_cells": spike_cells, "placebo": placebo,
        "mat_stratum": mat_stratum, "checks": checks, "pass": stage1_pass,
    }
    md += [f"**Stage 1 checks:** {checks}", f"**Stage 1: {'PASS' if stage1_pass else 'FAIL'}**", ""]

    if not stage1_pass:
        report["verdict"] = "reject / park — Stage 1 discovery fail (no May, no retune)"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        return {"summary": report}

    # ---------------------------------------------------------------- Stage 2 May
    print("[0051] Stage 1 passed — freezing and reading May once", flush=True)
    report["frozen"] = True
    md += ["## Freeze", "", "Stage 1 passed. Thresholds unchanged. Reading May 2026 once.", ""]
    may_pairs: list[H.Pair] = []
    for sid in MAY:
        if not (parquet_dir / f"session_id={sid}" / "trades.parquet").exists():
            continue
        p = H.load_pair(parquet_dir, sid)
        if p is not None:
            may_pairs.append(p)
    may_sids = [p.sid for p in may_pairs]
    by_may_all: dict[str, pa.Table] = {}
    by_may_gated: dict[str, pa.Table] = {}
    for pair in may_pairs:
        nq_b, es_b = pair.bars(100)
        bursts = H.confirm_bursts(pair)
        fades = exhaust_fade_events(bursts, nq_b, es_b, p_ex, max_wait_sec=fade_wait)
        series = vol_ratio_series(pair.nq, pair.es, t0_ns=pair.bounds[0], t1_ns=pair.bounds[1], p=vp)
        if fades.num_rows:
            fades = H._with_session(fades, pair.sid)
            fades = _annotate(fades, series)
            by_may_all[pair.sid] = fades
            z = fades.column("vr_z").to_numpy().astype(np.float64)
            fe = _filter_events(fades, np.isfinite(z) & (z >= vp.shock_z))
            if fe.num_rows:
                by_may_gated[pair.sid] = fe
        print(f"  MAY {pair.sid}: fades={fades.num_rows}", flush=True)

    may_comp = _sim(may_pairs, by_may_all, gate=gate_er, bracket_fn=br, slip=slip, cooldown=cooldown)
    may_gat = _sim(may_pairs, by_may_gated, gate=gate_er, bracket_fn=br, slip=slip, cooldown=cooldown)
    r_mc, r_mg = H.pack_pnl(may_comp, may_sids), H.pack_pnl(may_gat, may_sids)
    avg_mc, avg_mg = _avg(r_mc), _avg(r_mg)
    may_pass = bool(np.isfinite(avg_mg) and np.isfinite(avg_mc) and avg_mg > avg_mc and r_mg["net"] > 0)
    report["stages"]["2"] = {
        "sessions": may_sids,
        "comparator": {**r_mc, "avg": avg_mc},
        "gated": {**r_mg, "avg": avg_mg},
        "pass": may_pass,
    }
    md += [
        "## Stage 2 — May 2026 OOS (once)",
        "",
        *H.PNL_HDR,
        H.pnl_row("May comparator (ER60)", r_mc),
        H.pnl_row("May gated (keep shock)", r_mg),
        "",
        f"May avg comparator={avg_mc:.3f} gated={avg_mg:.3f} · **{'PASS' if may_pass else 'FAIL'}**",
        "",
    ]
    report["verdict"] = (
        "PASS May — eligible for Java volume-ratio gate ship (default OFF until parity). "
        "No MAT ship from this experiment."
        if may_pass else "reject / park — Stage 2 May fail (no retune)"
    )
    md += ["## Verdict", "", f"**{report['verdict']}**", ""]
    H.write_report(results_dir, report, md)
    return {"summary": report}
