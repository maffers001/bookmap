"""EXP-0052 / H43 — aggressor count vs size states on frozen books.

Stage 0: causality + 16-cell counts (CFADE candidates, MAT signals) +
         power (FLB among MAT signals >= 20, else park with counts).
Stage 1: PRIMARY MAT FLB keep-gate vs frozen MAT + random-keep placebo;
         SECONDARY CFADE MSQ stratum (descriptive only, no ship).
Stage 2: May MAT books read once only if Stage 1 passes.
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
from research.features.aggr_flow import (
    STATE_NAMES,
    AggrFlowParams,
    AggrSeries,
    aggr_at,
    aggr_handcheck,
    aggr_series,
    combined_cell,
)
from research.features.corr_exhaust import ExhaustParams, exhaust_fade_events
from research.run_mat_may import MAY


def _filter_events(ev: pa.Table, mask: np.ndarray) -> pa.Table:
    if ev.num_rows == 0:
        return ev
    idx = np.where(mask)[0]
    return ev.take(idx) if len(idx) else ev.slice(0, 0)


def _annotate(ev: pa.Table, series: AggrSeries) -> pa.Table:
    if ev.num_rows == 0:
        return ev
    ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
    vals = aggr_at(series, ts)
    for col, key in (
        ("af_nq_state", "nq_state"), ("af_es_state", "es_state"),
        ("af_nq_cnt", "nq_cnt"), ("af_es_cnt", "es_cnt"),
        ("af_es_vol_le_med", "es_vol_le_med"),
    ):
        if col in ev.column_names:
            ev = ev.drop([col])
        ev = ev.append_column(col, pa.array(np.asarray(vals[key], dtype=np.float64)))
    return ev


def _is_flb(ev: pa.Table) -> np.ndarray:
    return (
        (ev.column("af_nq_state").to_numpy() == 2)
        & (ev.column("af_es_state").to_numpy() == 2)
    )


def _is_msq(ev: pa.Table) -> np.ndarray:
    return (
        (ev.column("af_nq_state").to_numpy() == 3)
        & (ev.column("af_es_vol_le_med").to_numpy() == 1)
    )


def _cell_counts(ev: pa.Table | None) -> Counter:
    c: Counter = Counter()
    if ev is None or ev.num_rows == 0:
        return c
    nq = ev.column("af_nq_state").to_numpy()
    es = ev.column("af_es_state").to_numpy()
    le = ev.column("af_es_vol_le_med").to_numpy()
    for a, b, l in zip(nq, es, le):
        ai = int(a) if np.isfinite(a) else 0
        bi = int(b) if np.isfinite(b) else 0
        c[combined_cell(ai, bi, bool(l == 1))] += 1
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


def _truncation_check(pair: H.Pair, p: AggrFlowParams, *, frac: float = 0.7) -> dict:
    t0, t1 = pair.bounds
    sec_ns = int(p.grid_sec) * 1_000_000_000
    T_raw = t0 + int(frac * (t1 - t0))
    T = (T_raw // sec_ns) * sec_ns
    full = aggr_series(pair.nq, pair.es, t0_ns=t0, t1_ns=t1, p=p)

    def _cut(t: pa.Table) -> pa.Table:
        if t.num_rows == 0:
            return t
        ts = t.column("ts_ns").to_numpy().astype(np.int64)
        k = ts < (T + sec_ns)
        return t.take(np.where(k)[0]) if k.any() else t.slice(0, 0)

    trunc = aggr_series(_cut(pair.nq), _cut(pair.es), t0_ns=t0, t1_ns=T + sec_ns, p=p)
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
        pairs_cmp = (
            (full.nq_cnt[i], trunc.nq_cnt[j]), (full.es_cnt[i], trunc.es_cnt[j]),
            (full.nq_mean[i], trunc.nq_mean[j]), (full.es_mean[i], trunc.es_mean[j]),
            (float(full.nq_state[i]), float(trunc.nq_state[j])),
            (float(full.es_state[i]), float(trunc.es_state[j])),
        )
        for a, b in pairs_cmp:
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
    raw = dict(exp.get("aggr_flow") or {})
    placebo_draws = int(raw.get("placebo_draws", 1000))
    placebo_seed = int(raw.get("placebo_seed", 52))
    ap = AggrFlowParams(
        grid_sec=int(raw.get("grid_sec", 1)),
        window_sec=int(raw.get("window_sec", 5)),
        baseline_lookback_min=int(raw.get("baseline_lookback_min", 30)),
        baseline_warmup_min=int(raw.get("baseline_warmup_min", 5)),
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
    print(f"[0052] discovery {len(pairs)}/{len(DIG4_23)} DIG4 sessions", flush=True)

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
        f"Discovery={len(sids)} DIG4 sessions · UK 14:30–17:00 · {slip}-tick · "
        f"{H.COMMISSION} pt/RT · window={ap.window_sec}s baseline={ap.baseline_lookback_min}m · "
        f"MAT vol k=1 trend+VWAP causal · CFADE ER≤{er_max} RR SL1/TP2",
        "",
        f"train={len([s for s in sids if H.cohort(s)=='train'])} "
        f"hold={len([s for s in sids if H.cohort(s)=='hold'])} "
        f"new={len([s for s in sids if H.cohort(s)=='new'])}",
        "",
    ]
    report: dict = {"status": "ok", "sessions": sids,
                    "aggr_flow": asdict(ap), "kill": kill, "stages": {}}

    # ---------------------------------------------------------------- emit triggers + states
    series_by_sid: dict[str, AggrSeries] = {}
    mat_by_sid: dict[str, pa.Table] = {}
    cfade_by_sid: dict[str, pa.Table] = {}
    for pair in pairs:
        series = aggr_series(pair.nq, pair.es, t0_ns=pair.bounds[0], t1_ns=pair.bounds[1], p=ap)
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
    print("[0052] Stage 0 — 16-cell counts / causality / power", flush=True)
    mat_cells: Counter = Counter()
    for ev in mat_by_sid.values():
        mat_cells.update(_cell_counts(ev))
    cfade_cells: Counter = Counter()
    for ev in cfade_by_sid.values():
        cfade_cells.update(_cell_counts(ev))
    n_mat_sig = sum(mat_cells.values())
    n_cfade_cand = sum(cfade_cells.values())
    flb_mat = mat_cells.get("few_large__few_large", 0)
    flb_cfade = cfade_cells.get("few_large__few_large", 0)
    msq_cfade = sum(
        _is_msq(ev).sum() for ev in cfade_by_sid.values()
    ) if cfade_by_sid else 0
    coverage = {
        "mat_signals": int(n_mat_sig), "cfade_candidates": int(n_cfade_cand),
        "mat_cells": dict(sorted(mat_cells.items())),
        "cfade_cells": dict(sorted(cfade_cells.items())),
        "flb_mat_signals": int(flb_mat), "flb_cfade_candidates": int(flb_cfade),
        "msq_cfade_candidates": int(msq_cfade),
    }
    report["stages"]["0_coverage"] = coverage
    md += [
        "## Stage 0 — 16-cell counts, causality & power",
        "",
        f"MAT signal events n=**{n_mat_sig}** · CFADE candidates n=**{n_cfade_cand}**",
        f"Carried cells: FLB among MAT signals = **{flb_mat}** · FLB among CFADE = {flb_cfade} · "
        f"MSQ among CFADE = {int(msq_cfade)}",
        "",
        "| combined cell | MAT signals | CFADE candidates |",
        "|---|---:|---:|",
    ]
    all_cells = sorted(set(mat_cells) | set(cfade_cells))
    for cell in all_cells:
        md.append(f"| {cell} | {mat_cells.get(cell, 0)} | {cfade_cells.get(cell, 0)} |")
    md.append("")

    trunc_rows = [_truncation_check(pair, ap) for pair in pairs[:3]]
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
        st = ev.column("af_nq_state").to_numpy()
        ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        for i in np.where(st > 0)[0][:1]:
            h = aggr_handcheck(pair.nq, pair.es, t0_ns=pair.bounds[0],
                               event_ts_ns=int(ts[i]), p=ap)
            h["session"] = pair.sid
            h["event_ts_ns"] = int(ts[i])
            hand.append(h)
        if len(hand) >= 3:
            break
    hand_ok = all(h["match"] for h in hand) if hand else False
    report["stages"]["0_handcheck"] = hand
    report["causality_handcheck_ok"] = hand_ok
    md.append(f"Hand-check vs raw-trade window distinct counts ({len(hand)} samples): "
              f"**{'PASS' if hand_ok else 'FAIL'}**")
    for h in hand:
        md.append(
            f"- {h['session']}: nq_cnt {h['nq_cnt_at']:.0f} vs {h['nq_cnt_direct']} · "
            f"es_cnt {h['es_cnt_at']:.0f} vs {h['es_cnt_direct']} · match={h['match']}"
        )
    md.append("")

    if not trunc_ok or not hand_ok:
        report["verdict"] = "reject — Stage 0 causality failed"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        return {"summary": report}

    min_flb = int(exp.get("min_flb_signals", 20))
    power_ok = flb_mat >= min_flb
    report["stages"]["0_power"] = {"min_flb_signals": min_flb, "flb_mat_signals": int(flb_mat),
                                  "pass": power_ok}
    md.append(f"Stage 0 power (FLB among MAT signals >= {min_flb}): "
              f"**{'PASS' if power_ok else 'FAIL'}**")
    md.append("")
    if not power_ok:
        report["verdict"] = ("reject / park — Stage 0 underpowered (counts are the result; "
                              "no P&L, no May, no retune)")
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        return {"summary": report}

    # ---------------------------------------------------------------- Stage 1
    print("[0052] Stage 1 — MAT FLB gate + CFADE MSQ stratum", flush=True)
    sim_comp = _mat_sim(pairs, mat_by_sid, gate=mat_gate, bracket_fn=mat_br, slip=slip, cooldown=cooldown)
    r_comp = H.pack_pnl(sim_comp, sids)
    conc_comp = _concentration(sim_comp.trades, sids)
    avg_comp = _avg(r_comp)

    flb_lookup: dict[tuple[str, int], bool] = {}
    msq_lookup: dict[tuple[str, int], bool] = {}
    for sid, ev in mat_by_sid.items():
        ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        for t, f in zip(ts, _is_flb(ev)):
            flb_lookup[(sid, int(t))] = bool(f)
    kept_trades = [t for t in sim_comp.trades if flb_lookup.get((t.session_id, t.entry_ts_ns), False)]
    kept_ids = {id(t) for t in kept_trades}
    n_dropped = len([t for t in sim_comp.trades if id(t) not in kept_ids])

    mat_gated_by: dict[str, pa.Table] = {}
    for sid, ev in mat_by_sid.items():
        fe = _filter_events(ev, _is_flb(ev))
        if fe.num_rows:
            mat_gated_by[sid] = fe
    sim_gated = _mat_sim(pairs, mat_gated_by, gate=mat_gate, bracket_fn=mat_br, slip=slip, cooldown=cooldown)
    r_gated = H.pack_pnl(sim_gated, sids)
    conc_gated = _concentration(sim_gated.trades, sids)
    avg_gated = _avg(r_gated)

    placebo = _placebo_p90(sim_comp.trades, keep_n=r_gated["n"], draws=placebo_draws,
                           seed=placebo_seed, commission=H.COMMISSION)

    # CFADE secondary: comparator book, joined MSQ vs rest; stratum books report-only
    cfade_comp = _cfade_sim(pairs, cfade_by_sid, gate=gate_er, bracket_fn=cfade_br,
                            slip=slip, cooldown=cooldown)
    r_cf = H.pack_pnl(cfade_comp, sids)
    for sid, ev in cfade_by_sid.items():
        ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        for t, m in zip(ts, _is_msq(ev)):
            msq_lookup[(sid, int(t))] = bool(m)

    def _joined(trades, lookup):
        inn, out = [], []
        for t in trades:
            (inn if lookup.get((t.session_id, t.entry_ts_ns), False) else out).append(
                t.pnl - H.COMMISSION)
        def _pack(a):
            arr = np.asarray(a, dtype=np.float64)
            return {"n": int(len(arr)), "net": round(float(arr.sum()), 2) if len(arr) else 0.0,
                    "avg": round(float(arr.mean()), 3) if len(arr) else None}
        return _pack(inn), _pack(out)

    msq_in, msq_out = _joined(cfade_comp.trades, msq_lookup)
    flb_cf_in, flb_cf_out = _joined(
        cfade_comp.trades,
        {k: v for k, v in (
            ((sid, int(t)), bool(f))
            for sid, ev in cfade_by_sid.items()
            for t, f in zip(ev.column("event_ts_ns").to_numpy().astype(np.int64), _is_flb(ev))
        )},
    )
    msq_signal = bool(
        msq_in["n"] >= 20 and msq_in["avg"] is not None and msq_out["avg"] is not None
        and msq_in["avg"] >= msq_out["avg"] + 1.0
    )

    md += [
        "## Stage 1 — discovery (DIG4 23d)",
        "",
        "PRIMARY: MAT comparator = frozen MAT (vol k=1, trend+VWAP, causal). "
        "Gated = same sim, signal events pre-filtered to few-large-both (FLB).",
        "",
        *H.PNL_HDR,
        H.pnl_row("MAT comparator", r_comp),
        H.pnl_row("MAT gated (keep FLB)", r_gated),
        "",
        f"Comparator avg={avg_comp:.3f} (n={r_comp['n']}) · gated avg={avg_gated:.3f} "
        f"(n={r_gated['n']}) · Δavg={avg_gated - avg_comp:+.3f}",
        f"Comparator trades joined FLB n={len(kept_trades)} dropped n={n_dropped}",
        f"Placebo (random-keep, keep n={placebo['keep_n']}, {placebo_draws} draws): "
        f"p50_avg={placebo['p50_avg']:+.3f} p90_avg={placebo['p90_avg']:+.3f}",
        f"Concentration comparator days+={conc_comp['days_pos']}/{len(sids)} "
        f"top3={conc_comp['top3_share_of_pos']} · gated days+={conc_gated['days_pos']} "
        f"top3={conc_gated['top3_share_of_pos']}",
        "",
        "### SECONDARY: CFADE MSQ stratum (descriptive — no ship from EXP-0052)",
        "",
        f"CFADE comparator book: n={r_cf['n']} net={r_cf['net']:+.1f}",
        "",
        "| stratum | n | net | avg |",
        "|---|---:|---:|---:|",
        f"| MSQ (many-small NQ + ES quiet) | {msq_in['n']} | {msq_in['net']:+.1f} | {msq_in['avg']} |",
        f"| rest | {msq_out['n']} | {msq_out['net']:+.1f} | {msq_out['avg']} |",
        f"| FLB on CFADE (report) | {flb_cf_in['n']} | {flb_cf_in['net']:+.1f} | {flb_cf_in['avg']} |",
        "",
        f"MSQ secondary signal (n>=20 and avg >= rest + 1.0): **{msq_signal}** "
        f"(motivates a separate preregistration only; cannot promote here)",
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
        "placebo": placebo,
        "cfade_comparator": r_cf,
        "cfade_msq": msq_in, "cfade_rest": msq_out,
        "cfade_flb_report": flb_cf_in, "msq_secondary_signal": msq_signal,
        "checks": checks, "pass": stage1_pass,
    }
    md += [f"**Stage 1 checks:** {checks}", f"**Stage 1: {'PASS' if stage1_pass else 'FAIL'}**", ""]

    if not stage1_pass:
        report["verdict"] = "reject / park — Stage 1 discovery fail (no May, no retune)"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        return {"summary": report}

    # ---------------------------------------------------------------- Stage 2 May (MAT)
    print("[0052] Stage 1 passed — freezing and reading May MAT once", flush=True)
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
        series = aggr_series(pair.nq, pair.es, t0_ns=pair.bounds[0], t1_ns=pair.bounds[1], p=ap)
        signals = detect_signals(pair.nq, pair.es, mat_cfg)
        if signals.num_rows:
            signals = _annotate(signals, series)
            may_all[pair.sid] = signals
            fe = _filter_events(signals, _is_flb(signals))
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
        H.pnl_row("May MAT gated (keep FLB)", r_mg),
        "",
        f"May avg comparator={avg_mc:.3f} gated={avg_mg:.3f} · **{'PASS' if may_pass else 'FAIL'}**",
        "",
    ]
    report["verdict"] = (
        "PASS May — eligible for Java MAT FLB gate ship (default OFF until parity). "
        "No CFADE ship from this experiment."
        if may_pass else "reject / park — Stage 2 May fail (no retune)"
    )
    md += ["## Verdict", "", f"**{report['verdict']}**", ""]
    H.write_report(results_dir, report, md)
    return {"summary": report}
