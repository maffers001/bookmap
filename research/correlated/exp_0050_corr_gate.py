"""EXP-0050 / H41 — correlation-regime gate on frozen CFADE.

Stage 0: sanity / causality on DIG4 23d.
Stage 1: discovery P&L (comparator = ER60 CFADE; gated = + rho≥0.30;
         random-skip placebo; MAT stratum descriptive).
Stage 2: May read once only if Stage 1 passes.
Stage 3 (Java) is out of this runner — conditional after May pass.
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
from research.features.cross_regime import (
    CorrGateParams,
    RhoSeries,
    rho_at,
    rho_handcheck,
    rho_series,
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
    es_stamp: str = "confirm",
    trend_vwap: bool = False,
) -> SimSummary:
    cfg = MatSimConfig(
        slip_ticks=slip,
        commission_pts_rt=H.COMMISSION,
        tp_pts=10,
        sl_pts=10,
        signal_cooldown_sec=cooldown,
        es_stamp=es_stamp,
    )
    g = gate
    if trend_vwap:
        tv = combine_gates(make_trend_mode("with"), make_vwap_side(True))
        g = combine_gates(tv, gate) if gate is not None else tv
    parts = [
        simulate_session(
            p.sid, p.nq, p.es, cfg, gate=g, bracket_fn=bracket_fn,
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


def _annotate_rho(ev: pa.Table, series: RhoSeries) -> pa.Table:
    if ev.num_rows == 0:
        return ev
    ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
    r = rho_at(series, ts)
    if "rho" in ev.column_names:
        ev = ev.drop(["rho"])
    return ev.append_column("rho", pa.array(np.asarray(r, dtype=np.float64)))


def _truncation_check(
    pair: H.Pair,
    p: CorrGateParams,
    *,
    frac: float = 0.7,
) -> dict:
    """Rho at seconds ≤ T on full session must match trunc-at-T series.

    ``T`` is aligned to a completed second boundary. Trunc densify uses
    ``t1 = T + grid_sec`` so the second starting at T is included (half-open
    densify interval), matching the full series at that stamp.
    """
    t0, t1 = pair.bounds
    sec_ns = int(p.grid_sec) * 1_000_000_000
    T_raw = t0 + int(frac * (t1 - t0))
    T = (T_raw // sec_ns) * sec_ns  # last completed second start ≤ T_raw
    full = rho_series(
        pair.nq, pair.es, t0_ns=t0, t1_ns=t1,
        window_sec=p.window_sec, min_obs=p.min_obs, grid_sec=p.grid_sec,
    )

    def _cut(t: pa.Table) -> pa.Table:
        if t.num_rows == 0:
            return t
        ts = t.column("ts_ns").to_numpy().astype(np.int64)
        # Include prints through the end of second T (exclusive upper = T+sec)
        k = ts < (T + sec_ns)
        return t.take(np.where(k)[0]) if k.any() else t.slice(0, 0)

    trunc = rho_series(
        _cut(pair.nq), _cut(pair.es), t0_ns=t0, t1_ns=T + sec_ns,
        window_sec=p.window_sec, min_obs=p.min_obs, grid_sec=p.grid_sec,
    )
    # Compare seconds with end_ns ≤ T present in both
    full_idx = np.where(full.end_ns <= T)[0]
    trunc_map = {int(e): j for j, e in enumerate(trunc.end_ns.tolist())}
    diffs = []
    n_cmp = 0
    for i in full_idx:
        e = int(full.end_ns[i])
        j = trunc_map.get(e)
        if j is None:
            diffs.append(("missing_in_trunc", e))
            if len(diffs) > 5:
                break
            continue
        a, b = full.rho[i], trunc.rho[j]
        n_cmp += 1
        both_nan = (not np.isfinite(a)) and (not np.isfinite(b))
        if both_nan:
            continue
        if not (np.isfinite(a) and np.isfinite(b) and abs(float(a) - float(b)) < 1e-9):
            diffs.append((e, float(a) if np.isfinite(a) else None,
                          float(b) if np.isfinite(b) else None))
            if len(diffs) > 5:
                break
    return {
        "session": pair.sid,
        "T_frac": frac,
        "T_ns": int(T),
        "n_compared": n_cmp,
        "n_diff": len(diffs),
        "match": len(diffs) == 0,
        "diffs_sample": diffs[:3],
    }


def _placebo_p90(
    trades,
    *,
    keep_n: int,
    draws: int,
    seed: int,
    commission: float,
) -> dict:
    """Trade-level random-skip null: keep ``keep_n`` of comparator trades at random."""
    pnls = np.asarray([t.pnl - commission for t in trades], dtype=np.float64)
    n = len(pnls)
    if n == 0 or keep_n <= 0:
        return {"p90_avg": float("nan"), "p50_avg": float("nan"), "n": 0, "keep_n": keep_n}
    k = min(keep_n, n)
    rng = np.random.default_rng(seed)
    avgs = np.empty(draws, dtype=np.float64)
    for d in range(draws):
        idx = rng.choice(n, size=k, replace=False)
        avgs[d] = float(pnls[idx].mean())
    return {
        "p90_avg": float(np.quantile(avgs, 0.90)),
        "p50_avg": float(np.median(avgs)),
        "p10_avg": float(np.quantile(avgs, 0.10)),
        "n": n,
        "keep_n": k,
        "draws": draws,
    }


def _avg(r: dict) -> float:
    n = r.get("n") or 0
    return (r["net"] / n) if n else float("nan")


def _stratum_label(rho: float, lo: float, mid: float) -> str:
    if not np.isfinite(rho):
        return "warmup"
    if rho < lo:
        return "lo"
    if rho < mid:
        return "mid"
    return "hi"


def run(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    parquet_dir = Path(cfg["parquet_dir"])
    results_dir = Path(cfg["results_dir"]) / exp["name"]
    exclude = set(str(s) for s in exp.get("exclude_sessions") or [])
    slip = float(exp.get("slip_ticks", 1.0))
    cooldown = float(exp.get("signal_cooldown_sec", 5.0))
    kill = dict(exp.get("kill") or {})
    raw_cg = dict(exp.get("corr_gate") or {})
    placebo_draws = int(raw_cg.get("placebo_draws", 1000))
    placebo_seed = int(raw_cg.get("placebo_seed", 50))
    cg = CorrGateParams(
        grid_sec=int(raw_cg.get("grid_sec", 1)),
        window_sec=int(raw_cg.get("window_sec", 300)),
        min_obs=int(raw_cg.get("min_obs", 120)),
        skip_below=float(raw_cg.get("skip_below", 0.30)),
    )
    strata_cuts = list(raw_cg.get("strata") or [0.30, 0.60])
    lo_cut, mid_cut = float(strata_cuts[0]), float(strata_cuts[1])
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
    print(f"[0050] discovery {len(pairs)}/{len(DIG4_23)} DIG4 sessions", flush=True)

    br = make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=4.0, cap=40.0)
    gate_er = make_daytype_skip(max_er=er_max)

    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        exp.get("description", "").strip(),
        "",
        f"Discovery={len(sids)} DIG4 sessions · UK 14:30–17:00 · {slip}-tick · "
        f"{H.COMMISSION} pt/RT · rho window={cg.window_sec}s min_obs={cg.min_obs} "
        f"skip_below={cg.skip_below} · CFADE ER≤{er_max} · RR SL1/TP2",
        "",
        f"train={len([s for s in sids if H.cohort(s)=='train'])} "
        f"hold={len([s for s in sids if H.cohort(s)=='hold'])} "
        f"new={len([s for s in sids if H.cohort(s)=='new'])}",
        "",
    ]
    report: dict = {
        "status": "ok",
        "sessions": sids,
        "corr_gate": asdict(cg),
        "kill": kill,
        "stages": {},
    }

    # ------------------------------------------------------------------ emit fades + rho
    by_sid_all: dict[str, pa.Table] = {}
    rho_by_sid: dict[str, RhoSeries] = {}
    for pair in pairs:
        nq_b, es_b = pair.bars(100)
        bursts = H.confirm_bursts(pair)
        fades = exhaust_fade_events(bursts, nq_b, es_b, p_ex, max_wait_sec=fade_wait)
        series = rho_series(
            pair.nq, pair.es, t0_ns=pair.bounds[0], t1_ns=pair.bounds[1],
            window_sec=cg.window_sec, min_obs=cg.min_obs, grid_sec=cg.grid_sec,
        )
        rho_by_sid[pair.sid] = series
        if fades.num_rows:
            fades = H._with_session(fades, pair.sid)
            fades = _annotate_rho(fades, series)
            by_sid_all[pair.sid] = fades
        print(
            f"  {pair.sid}: fades={fades.num_rows} "
            f"rho_finite={int(np.isfinite(series.rho).sum())}/{len(series.rho)}",
            flush=True,
        )

    # ------------------------------------------------------------------ Stage 0
    print("[0050] Stage 0 — sanity / causality", flush=True)
    # Coverage across all fade candidates
    all_rho = []
    for sid, ev in by_sid_all.items():
        all_rho.append(ev.column("rho").to_numpy().astype(np.float64))
    if all_rho:
        R = np.concatenate(all_rho)
    else:
        R = np.zeros(0)
    n_cand = len(R)
    n_warm = int((~np.isfinite(R)).sum())
    n_lo = int((np.isfinite(R) & (R < lo_cut)).sum())
    n_mid = int((np.isfinite(R) & (R >= lo_cut) & (R < mid_cut)).sum())
    n_hi = int((np.isfinite(R) & (R >= mid_cut)).sum())
    coverage = {
        "n_candidates": n_cand,
        "warmup": n_warm,
        "lo": n_lo,
        "mid": n_mid,
        "hi": n_hi,
        "frac_warmup": round(n_warm / n_cand, 3) if n_cand else None,
        "frac_lo": round(n_lo / n_cand, 3) if n_cand else None,
    }
    report["stages"]["0_coverage"] = coverage
    md += [
        "## Stage 0 — coverage & causality",
        "",
        f"Fade candidates n=**{n_cand}** · warmup={n_warm} ({coverage['frac_warmup']}) · "
        f"rho<{lo_cut:g}={n_lo} · {lo_cut:g}–{mid_cut:g}={n_mid} · ≥{mid_cut:g}={n_hi}",
        "",
    ]

    # Truncation
    trunc_rows = []
    for pair in pairs[:3]:
        trunc_rows.append(_truncation_check(pair, cg))
    trunc_ok = all(r["match"] for r in trunc_rows) if trunc_rows else False
    report["stages"]["0_truncation"] = trunc_rows
    report["causality_truncation_ok"] = trunc_ok
    md.append(f"Truncation invariance (3 sessions, frac=0.7): **{'PASS' if trunc_ok else 'FAIL'}**")
    for r in trunc_rows:
        md.append(
            f"- {r['session']}: compared={r['n_compared']} diffs={r['n_diff']} match={r['match']}"
        )
    md.append("")

    # Hand-check: sample up to 3 finite-rho fades
    hand = []
    for pair in pairs:
        ev = by_sid_all.get(pair.sid)
        if ev is None or ev.num_rows == 0:
            continue
        rho = ev.column("rho").to_numpy().astype(np.float64)
        ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        for i in np.where(np.isfinite(rho))[0][:1]:
            h = rho_handcheck(
                pair.nq, pair.es, t0_ns=pair.bounds[0], event_ts_ns=int(ts[i]),
                window_sec=cg.window_sec, min_obs=cg.min_obs,
            )
            h["session"] = pair.sid
            h["event_ts_ns"] = int(ts[i])
            hand.append(h)
        if len(hand) >= 3:
            break
    hand_ok = all(h["match"] for h in hand) if hand else False
    report["stages"]["0_handcheck"] = hand
    report["causality_handcheck_ok"] = hand_ok
    md.append(f"Hand-check vs np.corrcoef ({len(hand)} samples): **{'PASS' if hand_ok else 'FAIL'}**")
    for h in hand:
        md.append(
            f"- {h['session']}: rho_at={h['rho_at']:+.4f} corrcoef={h['corrcoef']:+.4f} "
            f"n_paired={h['n_paired']} match={h['match']}"
        )
    md.append("")

    if not trunc_ok or not hand_ok:
        report["verdict"] = "reject — Stage 0 causality failed"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        return {"summary": report}

    # ------------------------------------------------------------------ Stage 1
    print("[0050] Stage 1 — discovery P&L", flush=True)

    # Comparator: all candidates, ER60 gate inside sim
    sim_comp = _sim(pairs, by_sid_all, gate=gate_er, bracket_fn=br, slip=slip, cooldown=cooldown)
    r_comp = H.pack_pnl(sim_comp, sids)
    conc_comp = _concentration(sim_comp.trades, sids)
    avg_comp = _avg(r_comp)

    # Join comparator trades to rho for skipped-stratum count
    # Build (sid, entry_ts) -> rho from candidates
    rho_lookup: dict[tuple[str, int], float] = {}
    for sid, ev in by_sid_all.items():
        ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        rr = ev.column("rho").to_numpy().astype(np.float64)
        for t, v in zip(ts, rr):
            rho_lookup[(sid, int(t))] = float(v)
    skipped_trades = [
        t for t in sim_comp.trades
        if np.isfinite(rho_lookup.get((t.session_id, t.entry_ts_ns), float("nan")))
        and rho_lookup[(t.session_id, t.entry_ts_ns)] < cg.skip_below
    ]
    n_skipped = len(skipped_trades)

    # Gated: pre-filter candidates to rho >= skip_below (drop warmup)
    by_sid_gated: dict[str, pa.Table] = {}
    n_drop_warm = 0
    n_drop_lo = 0
    for sid, ev in by_sid_all.items():
        rho = ev.column("rho").to_numpy().astype(np.float64)
        warm = ~np.isfinite(rho)
        lo = np.isfinite(rho) & (rho < cg.skip_below)
        keep = np.isfinite(rho) & (rho >= cg.skip_below)
        n_drop_warm += int(warm.sum())
        n_drop_lo += int(lo.sum())
        fe = _filter_events(ev, keep)
        if fe.num_rows:
            by_sid_gated[sid] = fe
    sim_gated = _sim(pairs, by_sid_gated, gate=gate_er, bracket_fn=br, slip=slip, cooldown=cooldown)
    r_gated = H.pack_pnl(sim_gated, sids)
    conc_gated = _concentration(sim_gated.trades, sids)
    avg_gated = _avg(r_gated)

    # Stratum books (report only)
    stratum_books = {}
    for name, mask_fn in (
        ("lo", lambda r: np.isfinite(r) & (r < lo_cut)),
        ("mid", lambda r: np.isfinite(r) & (r >= lo_cut) & (r < mid_cut)),
        ("hi", lambda r: np.isfinite(r) & (r >= mid_cut)),
        ("warmup", lambda r: ~np.isfinite(r)),
    ):
        by = {}
        for sid, ev in by_sid_all.items():
            rho = ev.column("rho").to_numpy().astype(np.float64)
            fe = _filter_events(ev, mask_fn(rho))
            if fe.num_rows:
                by[sid] = fe
        sim = _sim(pairs, by, gate=gate_er, bracket_fn=br, slip=slip, cooldown=cooldown)
        rr = H.pack_pnl(sim, sids)
        stratum_books[name] = {**rr, "avg": _avg(rr), "n_events": sum(t.num_rows for t in by.values())}

    # Placebo
    placebo = _placebo_p90(
        sim_comp.trades, keep_n=r_gated["n"], draws=placebo_draws,
        seed=placebo_seed, commission=H.COMMISSION,
    )

    # MAT secondary stratum
    print("[0050] MAT secondary stratum …", flush=True)
    mat_cfg = MatSimConfig(slip_ticks=slip, commission_pts_rt=H.COMMISSION, es_stamp="causal")
    mat_gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    mat_br = make_vol_bracket(1.0)
    mat_parts = [
        simulate_session(
            p.sid, p.nq, p.es, mat_cfg, gate=mat_gate, bracket_fn=mat_br,
            vwap_from_ns=p.vwap_from_ns,
        )
        for p in pairs
    ]
    mat_all = H.merge(mat_parts)
    mat_by = {"warmup": [], "lo": [], "mid": [], "hi": []}
    for tr in mat_all.trades:
        series = rho_by_sid.get(tr.session_id)
        if series is None:
            lab = "warmup"
        else:
            v = rho_at(series, tr.entry_ts_ns)
            lab = _stratum_label(float(v) if np.isfinite(v) else float("nan"), lo_cut, mid_cut)
        mat_by[lab].append(tr.pnl - H.COMMISSION)
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
        "Gated = same, candidates pre-filtered to rho≥skip_below (warmup dropped).",
        "Stratum books are report-only (not a partition of comparator trades).",
        "",
        *H.PNL_HDR,
        H.pnl_row("comparator (ER60)", r_comp),
        H.pnl_row("gated (rho≥skip)", r_gated),
    ]
    for name in ("lo", "mid", "hi", "warmup"):
        md.append(H.pnl_row(f"stratum {name}", stratum_books[name]))
    md += [
        "",
        f"Comparator avg={avg_comp:.3f} (n={r_comp['n']}) · "
        f"gated avg={avg_gated:.3f} (n={r_gated['n']}) · "
        f"Δavg={avg_gated - avg_comp:+.3f}",
        f"Candidates dropped: warmup={n_drop_warm} lo={n_drop_lo} · "
        f"comparator trades with rho<skip (skipped stratum) n=**{n_skipped}**",
        f"Placebo (trade-level, keep n={placebo['keep_n']}, {placebo_draws} draws): "
        f"p50_avg={placebo['p50_avg']:+.3f} p90_avg={placebo['p90_avg']:+.3f}",
        f"Concentration comparator days+={conc_comp['days_pos']}/{len(sids)} "
        f"top3={conc_comp['top3_share_of_pos']} · gated days+={conc_gated['days_pos']} "
        f"top3={conc_gated['top3_share_of_pos']}",
        "",
        "### MAT secondary stratum (descriptive — no ship)",
        "",
        "| stratum | n | net | avg |",
        "|---|---:|---:|---:|",
    ]
    for lab in ("warmup", "lo", "mid", "hi"):
        m = mat_stratum[lab]
        md.append(f"| {lab} | {m['n']} | {m['net']:+.1f} | {m['avg'] if m['avg'] is not None else '–'} |")
    md.append("")

    # Kill bar
    min_skipped = int(kill.get("min_skipped_n", 20))
    min_lift = float(kill.get("min_avg_lift_pts", 1.0))
    min_ret_frac = float(kill.get("min_net_retained_frac", 0.60))
    checks = {
        "skipped_n_ok": n_skipped >= min_skipped,
        "avg_lift_ok": bool(
            np.isfinite(avg_gated) and np.isfinite(avg_comp)
            and avg_gated >= avg_comp + min_lift
        ),
        "net_retained_ok": bool(
            r_comp["net"] == 0
            or (r_comp["net"] > 0 and r_gated["net"] >= min_ret_frac * r_comp["net"])
            or (r_comp["net"] < 0 and r_gated["net"] >= r_comp["net"])  # less negative counts
        ),
        "train_avg_ok": _cohort_avg_ok(sim_comp, sim_gated, sids, "train"),
        "hold_avg_ok": _cohort_avg_ok(sim_comp, sim_gated, sids, "hold"),
        "maxdd_ok": r_gated["max_dd"] <= r_comp["max_dd"] + 1e-9,
        "placebo_ok": bool(
            np.isfinite(avg_gated) and np.isfinite(placebo["p90_avg"])
            and avg_gated > placebo["p90_avg"]
        ),
    }
    # Fix net_retained for positive comparator: require >= 60%
    if r_comp["net"] > 0:
        checks["net_retained_ok"] = r_gated["net"] >= min_ret_frac * r_comp["net"]
    stage1_pass = all(checks.values())
    stage1 = {
        "comparator": {**r_comp, "avg": avg_comp, "concentration": conc_comp},
        "gated": {**r_gated, "avg": avg_gated, "concentration": conc_gated},
        "stratum_books": stratum_books,
        "n_skipped_comparator_trades": n_skipped,
        "n_drop_warm": n_drop_warm,
        "n_drop_lo": n_drop_lo,
        "placebo": placebo,
        "mat_stratum": mat_stratum,
        "checks": checks,
        "pass": stage1_pass,
    }
    report["stages"]["1"] = stage1
    md += [
        f"**Stage 1 checks:** {checks}",
        f"**Stage 1: {'PASS' if stage1_pass else 'FAIL'}**",
        "",
    ]

    if not stage1_pass:
        report["verdict"] = "reject / park — Stage 1 discovery fail (no May, no retune)"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        return {"summary": report}

    # ------------------------------------------------------------------ Stage 2 May
    print("[0050] Stage 1 passed — freezing and reading May once", flush=True)
    report["frozen"] = True
    md += [
        "## Freeze",
        "",
        "Stage 1 passed. Thresholds unchanged. Reading May 2026 once.",
        "",
    ]

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
        series = rho_series(
            pair.nq, pair.es, t0_ns=pair.bounds[0], t1_ns=pair.bounds[1],
            window_sec=cg.window_sec, min_obs=cg.min_obs, grid_sec=cg.grid_sec,
        )
        if fades.num_rows:
            fades = H._with_session(fades, pair.sid)
            fades = _annotate_rho(fades, series)
            by_may_all[pair.sid] = fades
            rho = fades.column("rho").to_numpy().astype(np.float64)
            keep = np.isfinite(rho) & (rho >= cg.skip_below)
            fe = _filter_events(fades, keep)
            if fe.num_rows:
                by_may_gated[pair.sid] = fe
        print(f"  MAY {pair.sid}: fades={fades.num_rows}", flush=True)

    may_comp = _sim(may_pairs, by_may_all, gate=gate_er, bracket_fn=br, slip=slip, cooldown=cooldown)
    may_gat = _sim(may_pairs, by_may_gated, gate=gate_er, bracket_fn=br, slip=slip, cooldown=cooldown)
    r_mc = H.pack_pnl(may_comp, may_sids)
    r_mg = H.pack_pnl(may_gat, may_sids)
    avg_mc, avg_mg = _avg(r_mc), _avg(r_mg)
    may_pass = bool(
        np.isfinite(avg_mg) and np.isfinite(avg_mc)
        and avg_mg > avg_mc and r_mg["net"] > 0
    )
    stage2 = {
        "sessions": may_sids,
        "comparator": {**r_mc, "avg": avg_mc},
        "gated": {**r_mg, "avg": avg_mg},
        "pass": may_pass,
    }
    report["stages"]["2"] = stage2
    md += [
        "## Stage 2 — May 2026 OOS (once)",
        "",
        *H.PNL_HDR,
        H.pnl_row("May comparator (ER60)", r_mc),
        H.pnl_row("May gated (rho≥skip)", r_mg),
        "",
        f"May avg comparator={avg_mc:.3f} gated={avg_mg:.3f} · "
        f"**{'PASS' if may_pass else 'FAIL'}**",
        "",
    ]
    if may_pass:
        report["verdict"] = (
            "PASS May — eligible for Java CrossMarketCorrTracker ship "
            "(default OFF until parity). No MAT ship from this experiment."
        )
    else:
        report["verdict"] = "reject / park — Stage 2 May fail (no retune)"
    md += ["## Verdict", "", f"**{report['verdict']}**", ""]
    H.write_report(results_dir, report, md)
    return {"summary": report}


def _cohort_avg_ok(
    sim_comp: SimSummary,
    sim_gated: SimSummary,
    sids: list[str],
    cohort_name: str,
) -> bool:
    """Train/hold avg of gated must not be worse than comparator."""

    def avg_of(sim: SimSummary) -> float:
        sub = filter_summary(
            sim,
            lambda t, c=cohort_name: H.cohort(t.session_id) == c,
            H.COMMISSION,
        )
        return (sub.net / sub.n) if sub.n else float("nan")

    a_c = avg_of(sim_comp)
    a_g = avg_of(sim_gated)
    if not np.isfinite(a_c):
        return True  # no comparator trades in cohort — vacuously ok
    if not np.isfinite(a_g):
        return False
    return a_g + 1e-9 >= a_c
