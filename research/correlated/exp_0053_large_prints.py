"""EXP-0053 / H44 — cross-market large-print clustering on frozen books.

Detailed design record (also in EXP-0053.yaml):
- Large print per market: size >= causal p99 of that market's own print
  sizes over trailing 30 completed minutes (whole-session p99 rejected
  a priori as non-causal). Burst = >= 2 large prints in trailing 5 s.
  Strata: both / nq_only / es_only / none / warmup.
- Stage 0: causality (truncation invariance + direct-count hand-check),
  stratum coverage on MAT signal events and CFADE candidates, and power
  (both-burst among MAT signals >= 20, else park with counts as result).
- Stage 1: PRIMARY MAT keep-gate (keep "both" signals) vs frozen MAT
  (vol k=1, trend+VWAP, causal) + random-keep placebo. SECONDARY CFADE
  joined comparator-trade net/avg by stratum (descriptive, no ship).
- Stage 2: May MAT books read once only if Stage 1 passes. Java only
  after a May pass (separate phase).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    combine_gates,
    detect_signals,
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
from research.features.large_prints import (
    STRATA,
    LargePrintParams,
    LargePrintSeries,
    large_print_at,
    large_print_handcheck,
    large_print_series,
)
from research.run_mat_may import MAY


def _filter_events(ev: pa.Table, mask: np.ndarray) -> pa.Table:
    if ev.num_rows == 0:
        return ev
    idx = np.where(mask)[0]
    return ev.take(idx) if len(idx) else ev.slice(0, 0)


def _annotate(ev: pa.Table, series: LargePrintSeries) -> pa.Table:
    if ev.num_rows == 0:
        return ev
    ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
    vals = large_print_at(series, ts)
    for col in ("lp_stratum", "lp_nq_cnt", "lp_es_cnt", "lp_nq_thr", "lp_es_thr"):
        if col in ev.column_names:
            ev = ev.drop([col])
        ev = ev.append_column(col, pa.array(np.asarray(vals[col], dtype=np.float64)))
    return ev


def _is_both(ev: pa.Table) -> np.ndarray:
    return ev.column("lp_stratum").to_numpy() == 1


def _stratum_counts(ev: pa.Table | None) -> Counter:
    c: Counter = Counter()
    if ev is None or ev.num_rows == 0:
        return c
    for v in ev.column("lp_stratum").to_numpy():
        code = int(v) if np.isfinite(v) else 0
        c[STRATA.get(code, "warmup")] += 1
    return c


def _mat_sim(pairs, by_sid, *, gate, bracket_fn, slip, cooldown=5.0) -> SimSummary:
    cfg = MatSimConfig(slip_ticks=slip, commission_pts_rt=H.COMMISSION,
                       es_stamp="causal", signal_cooldown_sec=cooldown)
    parts = [
        simulate_session(p.sid, p.nq, p.es, cfg, gate=gate, bracket_fn=bracket_fn,
                         vwap_from_ns=p.vwap_from_ns, events=by_sid[p.sid])
        for p in pairs if p.sid in by_sid
    ]
    return H.merge(parts) if parts else SimSummary()


def _cfade_sim(pairs, by_sid, *, gate, bracket_fn, slip, cooldown=5.0) -> SimSummary:
    cfg = MatSimConfig(slip_ticks=slip, commission_pts_rt=H.COMMISSION,
                       tp_pts=10, sl_pts=10, signal_cooldown_sec=cooldown, es_stamp="confirm")
    parts = [
        simulate_session(p.sid, p.nq, p.es, cfg, gate=gate, bracket_fn=bracket_fn,
                         vwap_from_ns=p.vwap_from_ns, events=by_sid[p.sid])
        for p in pairs if p.sid in by_sid
    ]
    return H.merge(parts) if parts else SimSummary()


def _placebo_p90(trades, *, keep_n, draws, seed, commission) -> dict:
    pnls = np.asarray([t.pnl - commission for t in trades], dtype=np.float64)
    n = len(pnls)
    if n == 0 or keep_n <= 0:
        return {"p90_avg": float("nan"), "p50_avg": float("nan"), "n": 0, "keep_n": keep_n}
    k = min(keep_n, n)
    rng = np.random.default_rng(seed)
    avgs = np.empty(draws)
    for d in range(draws):
        avgs[d] = float(pnls[rng.choice(n, size=k, replace=False)].mean())
    return {"p90_avg": float(np.quantile(avgs, 0.90)),
            "p50_avg": float(np.median(avgs)),
            "p10_avg": float(np.quantile(avgs, 0.10)),
            "n": n, "keep_n": k, "draws": draws}


def _avg(r: dict) -> float:
    n = r.get("n") or 0
    return (r["net"] / n) if n else float("nan")


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


def _truncation_check(pair: H.Pair, p: LargePrintParams, *, frac: float = 0.7) -> dict:
    t0, t1 = pair.bounds
    sec_ns = int(p.grid_sec) * 1_000_000_000
    T_raw = t0 + int(frac * (t1 - t0))
    T = (T_raw // sec_ns) * sec_ns
    full = large_print_series(pair.nq, pair.es, t0_ns=t0, t1_ns=t1, p=p)

    def _cut(t: pa.Table) -> pa.Table:
        if t.num_rows == 0:
            return t
        ts = t.column("ts_ns").to_numpy().astype(np.int64)
        k = ts < (T + sec_ns)
        return t.take(np.where(k)[0]) if k.any() else t.slice(0, 0)

    trunc = large_print_series(_cut(pair.nq), _cut(pair.es), t0_ns=t0, t1_ns=T + sec_ns, p=p)
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
        for a, b in (
            (full.nq_cnt[i], trunc.nq_cnt[j]), (full.es_cnt[i], trunc.es_cnt[j]),
            (full.nq_thr[i], trunc.nq_thr[j]), (full.es_thr[i], trunc.es_thr[j]),
            (float(full.stratum[i]), float(trunc.stratum[j])),
        ):
            both_nan = (not np.isfinite(a)) and (not np.isfinite(b))
            if both_nan:
                continue
            if not (np.isfinite(a) and np.isfinite(b) and abs(float(a) - float(b)) < 1e-9):
                diffs.append((e, float(a) if np.isfinite(a) else None,
                              float(b) if np.isfinite(b) else None))
                break
        if len(diffs) > 5:
            break
    return {"session": pair.sid, "T_frac": frac, "T_ns": int(T),
            "n_compared": n_cmp, "n_diff": len(diffs),
            "match": len(diffs) == 0, "diffs_sample": diffs[:3]}


def run(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    parquet_dir = Path(cfg["parquet_dir"])
    results_dir = Path(cfg["results_dir"]) / exp["name"]
    exclude = set(str(s) for s in exp.get("exclude_sessions") or [])
    slip = float(exp.get("slip_ticks", 1.0))
    cooldown = float(exp.get("signal_cooldown_sec", 5.0))
    kill = dict(exp.get("kill") or {})
    raw = dict(exp.get("large_prints") or {})
    placebo_draws = int(raw.get("placebo_draws", 1000))
    placebo_seed = int(raw.get("placebo_seed", 53))
    lp = LargePrintParams(
        grid_sec=int(raw.get("grid_sec", 1)),
        window_sec=int(raw.get("window_sec", 5)),
        large_q=float(raw.get("large_q", 0.99)),
        baseline_lookback_min=int(raw.get("baseline_lookback_min", 30)),
        baseline_warmup_min=int(raw.get("baseline_warmup_min", 5)),
        min_baseline_prints=int(raw.get("min_baseline_prints", 100)),
        burst_min_count=int(raw.get("burst_min_count", 2)),
    )
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
    print(f"[0053] discovery {len(pairs)}/{len(DIG4_23)} DIG4 sessions", flush=True)

    mat_gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    mat_br = make_vol_bracket(1.0)
    cfade_br = make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=4.0, cap=40.0)
    gate_er = make_daytype_skip(max_er=er_max)
    mat_cfg = MatSimConfig(slip_ticks=slip, commission_pts_rt=H.COMMISSION, es_stamp="causal")

    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        exp.get("description", "").strip(),
        "",
        "## Methodology (as frozen in EXP-0053.yaml)",
        "",
        f"- Large print: size >= causal p{int(lp.large_q * 100)} of own-market print sizes, "
        f"trailing {lp.baseline_lookback_min} completed minutes (warmup "
        f"{lp.baseline_warmup_min} min / {lp.min_baseline_prints} prints). "
        "Whole-session p99 was rejected a priori as non-causal.",
        f"- Burst: >= {lp.burst_min_count} large prints in trailing {lp.window_sec} s of "
        "completed 1 s seconds. Strata: both / nq_only / es_only / none / warmup.",
        "- Primary: MAT keep-gate on `both` vs frozen MAT (vol k=1, trend+VWAP, causal). "
        "Secondary: CFADE joined-trade strata (descriptive, no ship).",
        "- Sequential decisions: Stage 0 causality+power -> Stage 1 gate bar -> "
        "Stage 2 May (MAT) once. No retune; no stratum swap after P&L.",
        f"- Costs/books: {slip}-tick slip, {H.COMMISSION} pt/RT, {cooldown:g} s cooldown; "
        f"CFADE comparator ER<={er_max} + RR SL1/TP2 (as EXP-0050/0051).",
        "",
        f"Discovery={len(sids)} DIG4 sessions · UK 14:30–17:00",
        f"train={len([s for s in sids if H.cohort(s)=='train'])} "
        f"hold={len([s for s in sids if H.cohort(s)=='hold'])} "
        f"new={len([s for s in sids if H.cohort(s)=='new'])}",
        "",
    ]
    report: dict = {"status": "ok", "sessions": sids,
                    "large_prints": asdict(lp), "kill": kill, "stages": {}}

    # ---------------------------------------------------------------- emit triggers + strata
    series_by_sid: dict[str, LargePrintSeries] = {}
    mat_by_sid: dict[str, pa.Table] = {}
    cfade_by_sid: dict[str, pa.Table] = {}
    for pair in pairs:
        series = large_print_series(pair.nq, pair.es, t0_ns=pair.bounds[0], t1_ns=pair.bounds[1], p=lp)
        series_by_sid[pair.sid] = series
        signals = detect_signals(pair.nq, pair.es, mat_cfg)
        if signals.num_rows:
            mat_by_sid[pair.sid] = _annotate(signals, series)
        nq_b, es_b = pair.bars(100)
        bursts = H.confirm_bursts(pair)
        fades = exhaust_fade_events(bursts, nq_b, es_b, p_ex, max_wait_sec=fade_wait)
        if fades.num_rows:
            fades = H._with_session(fades, pair.sid)
            cfade_by_sid[pair.sid] = _annotate(fades, series)
        print(f"  {pair.sid}: mat_signals={signals.num_rows} fades={fades.num_rows}", flush=True)

    # ---------------------------------------------------------------- Stage 0
    print("[0053] Stage 0 — coverage / causality / power", flush=True)
    mat_counts: Counter = Counter()
    for ev in mat_by_sid.values():
        mat_counts.update(_stratum_counts(ev))
    cfade_counts: Counter = Counter()
    for ev in cfade_by_sid.values():
        cfade_counts.update(_stratum_counts(ev))
    n_mat_sig = sum(mat_counts.values())
    n_cfade = sum(cfade_counts.values())
    both_mat = mat_counts.get("both", 0)
    # Candidate threshold summary (medians across annotated triggers)
    thr_nq = np.concatenate([ev.column("lp_nq_thr").to_numpy() for ev in mat_by_sid.values()]) \
        if mat_by_sid else np.zeros(0)
    thr_es = np.concatenate([ev.column("lp_es_thr").to_numpy() for ev in mat_by_sid.values()]) \
        if mat_by_sid else np.zeros(0)
    coverage = {
        "mat_signals": int(n_mat_sig), "cfade_candidates": int(n_cfade),
        "mat_strata": dict(sorted(mat_counts.items())),
        "cfade_strata": dict(sorted(cfade_counts.items())),
        "both_mat_signals": int(both_mat),
        "thr_nq_median": round(float(np.nanmedian(thr_nq)), 3) if len(thr_nq) else None,
        "thr_es_median": round(float(np.nanmedian(thr_es)), 3) if len(thr_es) else None,
    }
    report["stages"]["0_coverage"] = coverage
    md += [
        "## Stage 0 — coverage, causality & power",
        "",
        f"MAT signal events n=**{n_mat_sig}** · CFADE candidates n=**{n_cfade}**",
        f"Median causal large thresholds at MAT signals: NQ size >= {coverage['thr_nq_median']}, "
        f"ES size >= {coverage['thr_es_median']}",
        "",
        "| stratum | MAT signals | CFADE candidates |",
        "|---|---:|---:|",
    ]
    for name in ("both", "nq_only", "es_only", "none", "warmup"):
        md.append(f"| {name} | {mat_counts.get(name, 0)} | {cfade_counts.get(name, 0)} |")
    md.append("")

    trunc_rows = [_truncation_check(pair, lp) for pair in pairs[:3]]
    trunc_ok = all(r["match"] for r in trunc_rows) if trunc_rows else False
    report["stages"]["0_truncation"] = trunc_rows
    report["causality_truncation_ok"] = trunc_ok
    md.append(f"Truncation invariance (3 sessions, frac=0.7): **{'PASS' if trunc_ok else 'FAIL'}**")
    for r in trunc_rows:
        md.append(f"- {r['session']}: compared={r['n_compared']} diffs={r['n_diff']} match={r['match']}")
    md.append("")

    hand = []
    for pair in pairs:
        ev = cfade_by_sid.get(pair.sid)
        if ev is None or ev.num_rows == 0:
            continue
        st = ev.column("lp_stratum").to_numpy()
        ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        idxs = np.where(np.isfinite(st) & (st > 0))[0]
        for i in idxs[:1]:
            h = large_print_handcheck(pair.nq, pair.es, t0_ns=pair.bounds[0],
                                      event_ts_ns=int(ts[i]), p=lp)
            h["session"] = pair.sid
            h["event_ts_ns"] = int(ts[i])
            hand.append(h)
        if len(hand) >= 3:
            break
    hand_ok = all(h["match"] for h in hand) if hand else False
    report["stages"]["0_handcheck"] = hand
    report["causality_handcheck_ok"] = hand_ok
    md.append(f"Hand-check vs direct large-print counts ({len(hand)} samples): "
              f"**{'PASS' if hand_ok else 'FAIL'}**")
    for h in hand:
        md.append(
            f"- {h['session']}: nq_cnt {h['nq_cnt_at']:.0f} vs {h['nq_cnt_direct']} "
            f"(thr {h['nq_thr']:.0f}) · es_cnt {h['es_cnt_at']:.0f} vs {h['es_cnt_direct']} "
            f"(thr {h['es_thr']:.0f}) · match={h['match']}"
        )
    md.append("")

    if not trunc_ok or not hand_ok:
        report["verdict"] = "reject — Stage 0 causality failed"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        return {"summary": report}

    min_both = int(exp.get("min_both_signals", 20))
    power_ok = both_mat >= min_both
    report["stages"]["0_power"] = {"min_both_signals": min_both,
                                   "both_mat_signals": int(both_mat), "pass": power_ok}
    md.append(f"Stage 0 power (both-burst among MAT signals >= {min_both}): "
              f"**{'PASS' if power_ok else 'FAIL'}**")
    md.append("")
    if not power_ok:
        report["verdict"] = ("reject / park — Stage 0 underpowered (counts are the result; "
                              "no P&L, no May, no retune)")
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        return {"summary": report}

    # ---------------------------------------------------------------- Stage 1
    print("[0053] Stage 1 — MAT both-burst gate + CFADE strata", flush=True)
    sim_comp = _mat_sim(pairs, mat_by_sid, gate=mat_gate, bracket_fn=mat_br, slip=slip, cooldown=cooldown)
    r_comp = H.pack_pnl(sim_comp, sids)
    conc_comp = _concentration(sim_comp.trades, sids)
    avg_comp = _avg(r_comp)

    both_lookup: dict[tuple[str, int], bool] = {}
    for sid, ev in mat_by_sid.items():
        ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        for t, b in zip(ts, _is_both(ev)):
            both_lookup[(sid, int(t))] = bool(b)
    kept_trades = [t for t in sim_comp.trades if both_lookup.get((t.session_id, t.entry_ts_ns), False)]
    kept_ids = {id(t) for t in kept_trades}
    n_dropped = len([t for t in sim_comp.trades if id(t) not in kept_ids])

    mat_gated_by: dict[str, pa.Table] = {}
    for sid, ev in mat_by_sid.items():
        fe = _filter_events(ev, _is_both(ev))
        if fe.num_rows:
            mat_gated_by[sid] = fe
    sim_gated = _mat_sim(pairs, mat_gated_by, gate=mat_gate, bracket_fn=mat_br, slip=slip, cooldown=cooldown)
    r_gated = H.pack_pnl(sim_gated, sids)
    conc_gated = _concentration(sim_gated.trades, sids)
    avg_gated = _avg(r_gated)

    placebo = _placebo_p90(sim_comp.trades, keep_n=r_gated["n"], draws=placebo_draws,
                           seed=placebo_seed, commission=H.COMMISSION)

    # CFADE secondary strata (joined trades)
    cfade_comp = _cfade_sim(pairs, cfade_by_sid, gate=gate_er, bracket_fn=cfade_br,
                            slip=slip, cooldown=cooldown)
    r_cf = H.pack_pnl(cfade_comp, sids)
    stratum_lookup: dict[tuple[str, int], str] = {}
    for sid, ev in cfade_by_sid.items():
        ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        st = ev.column("lp_stratum").to_numpy()
        for t, v in zip(ts, st):
            stratum_lookup[(sid, int(t))] = STRATA.get(int(v) if np.isfinite(v) else 0, "warmup")
    cfade_strata: dict[str, dict] = {}
    for name in ("both", "nq_only", "es_only", "none", "warmup"):
        pnls = [t.pnl - H.COMMISSION for t in cfade_comp.trades
                if stratum_lookup.get((t.session_id, t.entry_ts_ns), "warmup") == name]
        arr = np.asarray(pnls, dtype=np.float64)
        cfade_strata[name] = {"n": int(len(arr)),
                              "net": round(float(arr.sum()), 2) if len(arr) else 0.0,
                              "avg": round(float(arr.mean()), 3) if len(arr) else None}
    # Same joined strata on MAT comparator trades (report context for the gate)
    mat_strata: dict[str, dict] = {}
    mat_lookup: dict[tuple[str, int], str] = {}
    for sid, ev in mat_by_sid.items():
        ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        st = ev.column("lp_stratum").to_numpy()
        for t, v in zip(ts, st):
            mat_lookup[(sid, int(t))] = STRATA.get(int(v) if np.isfinite(v) else 0, "warmup")
    for name in ("both", "nq_only", "es_only", "none", "warmup"):
        pnls = [t.pnl - H.COMMISSION for t in sim_comp.trades
                if mat_lookup.get((t.session_id, t.entry_ts_ns), "warmup") == name]
        arr = np.asarray(pnls, dtype=np.float64)
        mat_strata[name] = {"n": int(len(arr)),
                            "net": round(float(arr.sum()), 2) if len(arr) else 0.0,
                            "avg": round(float(arr.mean()), 3) if len(arr) else None}

    md += [
        "## Stage 1 — discovery (DIG4 23d)",
        "",
        "PRIMARY: MAT comparator = frozen MAT (vol k=1, trend+VWAP, causal). "
        "Gated = same sim, signal events pre-filtered to both-burst stratum.",
        "Note: gated-book trades are a re-simulation (flat-only/cooldown paths differ), "
        "so gated n need not equal the joined both-stratum trade count below.",
        "",
        *H.PNL_HDR,
        H.pnl_row("MAT comparator", r_comp),
        H.pnl_row("MAT gated (keep both)", r_gated),
        "",
        f"Comparator avg={avg_comp:.3f} (n={r_comp['n']}) · gated avg={avg_gated:.3f} "
        f"(n={r_gated['n']}) · Δavg={avg_gated - avg_comp:+.3f}",
        f"Comparator trades joined both n={len(kept_trades)} dropped n={n_dropped}",
        f"Placebo (random-keep, keep n={placebo['keep_n']}, {placebo_draws} draws): "
        f"p50_avg={placebo['p50_avg']:+.3f} p90_avg={placebo['p90_avg']:+.3f}",
        f"Concentration comparator days+={conc_comp['days_pos']}/{len(sids)} "
        f"top3={conc_comp['top3_share_of_pos']} · gated days+={conc_gated['days_pos']} "
        f"top3={conc_gated['top3_share_of_pos']}",
        "",
        "### Joined comparator-trade strata (descriptive; not re-simulated books)",
        "",
        "| stratum | MAT trades n | MAT net | MAT avg | CFADE trades n | CFADE net | CFADE avg |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("both", "nq_only", "es_only", "none", "warmup"):
        m, c = mat_strata[name], cfade_strata[name]
        md.append(
            f"| {name} | {m['n']} | {m['net']:+.1f} | {m['avg']} | "
            f"{c['n']} | {c['net']:+.1f} | {c['avg']} |"
        )
    md += [
        "",
        f"CFADE comparator book (secondary context): n={r_cf['n']} net={r_cf['net']:+.1f}",
        "",
    ]

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
        "mat_comparator": {**r_comp, "avg": avg_comp, "concentration": conc_comp},
        "mat_gated": {**r_gated, "avg": avg_gated, "concentration": conc_gated},
        "n_kept_joined": len(kept_trades), "n_dropped_joined": n_dropped,
        "placebo": placebo, "mat_strata_joined": mat_strata,
        "cfade_comparator": r_cf, "cfade_strata_joined": cfade_strata,
        "checks": checks, "pass": stage1_pass,
    }
    md += [f"**Stage 1 checks:** {checks}", f"**Stage 1: {'PASS' if stage1_pass else 'FAIL'}**", ""]

    if not stage1_pass:
        report["verdict"] = "reject / park — Stage 1 discovery fail (no May, no retune)"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        return {"summary": report}

    # ---------------------------------------------------------------- Stage 2 May (MAT)
    print("[0053] Stage 1 passed — freezing and reading May MAT once", flush=True)
    report["frozen"] = True
    md += ["## Freeze", "", "Stage 1 passed. Thresholds unchanged. Reading May 2026 MAT once.", ""]
    may_pairs: list[H.Pair] = []
    for sid in MAY:
        if not (parquet_dir / f"session_id={sid}" / "trades.parquet").exists():
            continue
        p = H.load_pair(parquet_dir, sid)
        if p is not None:
            may_pairs.append(p)
    may_sids = [p.sid for p in may_pairs]
    may_all: dict[str, pa.Table] = {}
    may_gated: dict[str, pa.Table] = {}
    for pair in may_pairs:
        series = large_print_series(pair.nq, pair.es, t0_ns=pair.bounds[0], t1_ns=pair.bounds[1], p=lp)
        signals = detect_signals(pair.nq, pair.es, mat_cfg)
        if signals.num_rows:
            signals = _annotate(signals, series)
            may_all[pair.sid] = signals
            fe = _filter_events(signals, _is_both(signals))
            if fe.num_rows:
                may_gated[pair.sid] = fe
        print(f"  MAY {pair.sid}: mat_signals={signals.num_rows}", flush=True)

    may_comp = _mat_sim(may_pairs, may_all, gate=mat_gate, bracket_fn=mat_br, slip=slip, cooldown=cooldown)
    may_gat = _mat_sim(may_pairs, may_gated, gate=mat_gate, bracket_fn=mat_br, slip=slip, cooldown=cooldown)
    r_mc, r_mg = H.pack_pnl(may_comp, may_sids), H.pack_pnl(may_gat, may_sids)
    avg_mc, avg_mg = _avg(r_mc), _avg(r_mg)
    may_pass = bool(np.isfinite(avg_mg) and np.isfinite(avg_mc) and avg_mg > avg_mc and r_mg["net"] > 0)
    report["stages"]["2"] = {"sessions": may_sids,
                             "comparator": {**r_mc, "avg": avg_mc},
                             "gated": {**r_mg, "avg": avg_mg}, "pass": may_pass}
    md += [
        "## Stage 2 — May 2026 MAT OOS (once)",
        "",
        *H.PNL_HDR,
        H.pnl_row("May MAT comparator", r_mc),
        H.pnl_row("May MAT gated (keep both)", r_mg),
        "",
        f"May avg comparator={avg_mc:.3f} gated={avg_mg:.3f} · **{'PASS' if may_pass else 'FAIL'}**",
        "",
    ]
    report["verdict"] = (
        "PASS May — eligible for Java MAT large-print gate ship (default OFF until parity). "
        "No CFADE ship from this experiment."
        if may_pass else "reject / park — Stage 2 May fail (no retune)"
    )
    md += ["## Verdict", "", f"**{report['verdict']}**", ""]
    H.write_report(results_dir, report, md)
    return {"summary": report}
