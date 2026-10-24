"""EXP-0054 / H45 — BBO/top-3 imbalance agreement at frozen triggers.

STRATUM ONLY (Tier A / A4; see EXP-0054.yaml for the frozen design):
- Book replay per market from depth.parquet (seeded pre-window), top-3
  imbalance per completed second, trailing-5 s mean at each trigger.
- Stage 0: coverage (defined-state share per trigger set; uncovered
  sessions reported) + causality (truncation invariance; chunked vs
  brute-force replay hand-check).
- Stage 1: joined frozen-book trades by side-relative state
  (agree_with / agree_against / other / warmup) for MAT and CFADE.
  A state matters on a book iff agree_with n >= 20, avg >= rest + 1.0,
  and the direction holds in both train and hold. Nothing ships from
  this experiment even on a matters verdict.
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
    detect_signals,
    make_daytype_skip,
    make_trend_mode,
    make_vol_bracket,
    make_vol_bracket_rr,
    make_vwap_side,
    simulate_session,
)
from research.correlated import harness as H
from research.correlated.exp_0049_divergence import DIG4_23
from research.features.book_agreement import (
    BookAgreementParams,
    absolute_state,
    brute_imbalance_at,
    lean,
    market_event_imbalance,
    replay_imbalance_samples,
    side_state,
)
from research.features.corr_exhaust import ExhaustParams, exhaust_fade_events
from research.irt_lvn.run_hold_break_of import load_depth_window_with_book


def _filter_events(ev: pa.Table, mask: np.ndarray) -> pa.Table:
    if ev.num_rows == 0:
        return ev
    idx = np.where(mask)[0]
    return ev.take(idx) if len(idx) else ev.slice(0, 0)


def _annotate(ev: pa.Table, nq_imb: np.ndarray, es_imb: np.ndarray, cut: float) -> pa.Table:
    if ev.num_rows == 0:
        return ev
    sides = ev.column("side").to_numpy().astype(int)
    abs_states, side_states = [], []
    for a, b, s in zip(nq_imb, es_imb, sides):
        la, lb = lean(float(a), cut), lean(float(b), cut)
        abs_states.append(absolute_state(la, lb))
        side_states.append(side_state(la, lb, int(s)))
    for col, arr, typ in (
        ("bi_nq_imb", np.asarray(nq_imb, dtype=np.float64), pa.float64()),
        ("bi_es_imb", np.asarray(es_imb, dtype=np.float64), pa.float64()),
        ("bi_abs_state", np.array(abs_states, dtype=object), pa.string()),
        ("bi_side_state", np.array(side_states, dtype=object), pa.string()),
    ):
        if col in ev.column_names:
            ev = ev.drop([col])
        ev = ev.append_column(col, pa.array(arr, type=typ))
    return ev


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


def _pack_pnls(pnls: list[float]) -> dict:
    arr = np.asarray(pnls, dtype=np.float64)
    return {"n": int(len(arr)),
            "net": round(float(arr.sum()), 2) if len(arr) else 0.0,
            "avg": round(float(arr.mean()), 3) if len(arr) else None}


def _strata(trades, lookup: dict, cohorts: bool = False) -> dict:
    out: dict = {}
    names = ("agree_with", "agree_against", "other", "warmup")
    for name in names:
        sel = [t for t in trades if lookup.get((t.session_id, t.entry_ts_ns), "warmup") == name]
        out[name] = _pack_pnls([t.pnl - H.COMMISSION for t in sel])
        if cohorts:
            for coh in ("train", "hold", "new"):
                sub = [t for t in sel if H.cohort(t.session_id) == coh]
                out[name][coh] = _pack_pnls([t.pnl - H.COMMISSION for t in sub])
    rest = [t for t in trades
            if lookup.get((t.session_id, t.entry_ts_ns), "warmup") in ("agree_against", "other")]
    out["rest_defined"] = _pack_pnls([t.pnl - H.COMMISSION for t in rest])
    if cohorts:
        for coh in ("train", "hold", "new"):
            sub = [t for t in rest if H.cohort(t.session_id) == coh]
            out["rest_defined"][coh] = _pack_pnls([t.pnl - H.COMMISSION for t in sub])
    return out


def _matters(strata: dict, *, min_n: int, lift: float) -> tuple[bool, dict]:
    w, r = strata["agree_with"], strata["rest_defined"]
    def _dir(coh: str) -> bool:
        a, b = w.get(coh) or {}, r.get(coh) or {}
        return bool(a.get("n") and b.get("n") and a.get("avg") is not None
                    and b.get("avg") is not None and a["avg"] > b["avg"])
    checks = {
        "n_ok": w["n"] >= min_n,
        "avg_lift_ok": bool(w["avg"] is not None and r["avg"] is not None
                            and w["avg"] >= r["avg"] + lift),
        "train_dir_ok": _dir("train"),
        "hold_dir_ok": _dir("hold"),
    }
    return all(checks.values()), checks


def run(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    parquet_dir = Path(cfg["parquet_dir"])
    results_dir = Path(cfg["results_dir"]) / exp["name"]
    exclude = set(str(s) for s in exp.get("exclude_sessions") or [])
    slip = float(exp.get("slip_ticks", 1.0))
    cooldown = float(exp.get("signal_cooldown_sec", 5.0))
    raw = dict(exp.get("book_agreement") or {})
    bp = BookAgreementParams(
        window_sec=int(raw.get("window_sec", 5)),
        top_levels=int(raw.get("top_levels", 3)),
        lean_cut=float(raw.get("lean_cut", 0.20)),
        min_defined_sec=int(raw.get("min_defined_sec", 3)),
    )
    min_state_n = int(exp.get("min_state_trades", 20))
    lift = float(exp.get("mde_avg_lift_pts", 1.0))
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
    print(f"[0054] discovery {len(pairs)}/{len(DIG4_23)} DIG4 sessions", flush=True)

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
        "## Methodology (as frozen in EXP-0054.yaml)",
        "",
        f"- Book: depth.parquet role rows, seeded from pre-window rows "
        f"(`load_depth_window_with_book`), replayed with last-update-per-price chunking "
        f"(proven identical to brute force in Stage 0).",
        f"- Feature: top-{bp.top_levels} imbalance averaged over trailing {bp.window_sec} "
        f"completed seconds; lean cut |imb| >= {bp.lean_cut}; defined if >= "
        f"{bp.min_defined_sec} of {bp.window_sec} seconds defined.",
        "- Primary read: side-relative agree_with vs rest_defined (agree_against + other; "
        "warmup excluded from both), per book. Absolute states reported as counts.",
        f"- Matters bar (per book): agree_with n >= {min_state_n}, avg >= rest + {lift} pt, "
        "same avg direction in train AND hold. Stratum only: a matters verdict ships "
        "nothing; it only entitles a future Tier B execution-timer preregistration.",
        "- No re-simulated books, no May read, no retune. Books are the frozen MAT/CFADE "
        "books exactly as in EXP-0050..0053; reads are joined trades.",
        "",
        f"Discovery={len(sids)} DIG4 sessions · UK 14:30–17:00 · {slip}-tick · {H.COMMISSION} pt/RT",
        "",
    ]
    report: dict = {"status": "ok", "sessions": sids,
                    "book_agreement": asdict(bp), "stages": {}}

    # ---------------------------------------------------------------- triggers
    mat_raw: dict[str, pa.Table] = {}
    cfade_raw: dict[str, pa.Table] = {}
    for pair in pairs:
        signals = detect_signals(pair.nq, pair.es, mat_cfg)
        if signals.num_rows:
            mat_raw[pair.sid] = signals
        nq_b, es_b = pair.bars(100)
        bursts = H.confirm_bursts(pair)
        fades = exhaust_fade_events(bursts, nq_b, es_b, p_ex, max_wait_sec=fade_wait)
        if fades.num_rows:
            cfade_raw[pair.sid] = H._with_session(fades, pair.sid)

    # ---------------------------------------------------------------- depth + annotate
    mat_by_sid: dict[str, pa.Table] = {}
    cfade_by_sid: dict[str, pa.Table] = {}
    depth_info: dict[str, dict] = {}
    trunc_data = {}  # sid -> (depth_nq, ib, ia, depth_es, eb, ea, sample_events)
    for pair in pairs:
        ev_tables = [t for t in (mat_raw.get(pair.sid), cfade_raw.get(pair.sid))
                     if t is not None and t.num_rows]
        all_ts = np.concatenate(
            [t.column("event_ts_ns").to_numpy().astype(np.int64) for t in ev_tables]
        ) if ev_tables else np.zeros(0, dtype=np.int64)
        d_nq, ib_n, ia_n = load_depth_window_with_book(parquet_dir, pair.sid, "NQ", pair.bounds)
        d_es, ib_e, ia_e = load_depth_window_with_book(parquet_dir, pair.sid, "ES", pair.bounds)
        usable = d_nq.num_rows > 0 and d_es.num_rows > 0
        depth_info[pair.sid] = {
            "depth_nq_rows": int(d_nq.num_rows), "depth_es_rows": int(d_es.num_rows),
            "init_nq_levels": [len(ib_n), len(ia_n)], "init_es_levels": [len(ib_e), len(ia_e)],
            "usable": bool(usable), "n_triggers": int(len(all_ts)),
            "n_defined": 0,
        }
        if len(all_ts) and usable:
            nq_imb, _ = market_event_imbalance(d_nq, ib_n, ia_n, all_ts, bp)
            es_imb, _ = market_event_imbalance(d_es, ib_e, ia_e, all_ts, bp)
            defined = np.isfinite(nq_imb) & np.isfinite(es_imb)
            depth_info[pair.sid]["n_defined"] = int(defined.sum())
            # Split back per table in order
            pos = 0
            for src, dest in ((mat_raw.get(pair.sid), mat_by_sid),
                              (cfade_raw.get(pair.sid), cfade_by_sid)):
                if src is None or src.num_rows == 0:
                    continue
                k = src.num_rows
                dest[pair.sid] = _annotate(src, nq_imb[pos:pos + k], es_imb[pos:pos + k], bp.lean_cut)
                pos += k
            if pair.sid in (discovery_ids[:2] if len(discovery_ids) >= 2 else discovery_ids):
                trunc_data[pair.sid] = (d_nq, ib_n, ia_n, all_ts.copy())
        else:
            # Keep triggers as warmup so denominators stay honest
            for src, dest in ((mat_raw.get(pair.sid), mat_by_sid),
                              (cfade_raw.get(pair.sid), cfade_by_sid)):
                if src is not None and src.num_rows:
                    nan = np.full(src.num_rows, np.nan)
                    dest[pair.sid] = _annotate(src, nan, nan, bp.lean_cut)
        print(
            f"  {pair.sid}: triggers={depth_info[pair.sid]['n_triggers']} "
            f"defined={depth_info[pair.sid]['n_defined']} usable={usable}",
            flush=True,
        )

    # ---------------------------------------------------------------- Stage 0
    print("[0054] Stage 0 — coverage / causality", flush=True)
    def _coverage(by_sid: dict[str, pa.Table]) -> dict:
        total = defined = 0
        abs_counts: dict[str, int] = {}
        for ev in by_sid.values():
            total += ev.num_rows
            st = ev.column("bi_side_state").to_pylist()
            defined += sum(1 for s in st if s != "warmup")
            for s in ev.column("bi_abs_state").to_pylist():
                abs_counts[s] = abs_counts.get(s, 0) + 1
        return {"n_triggers": total, "n_defined": defined,
                "frac_defined": round(defined / total, 3) if total else None,
                "abs_states": abs_counts}

    cov_mat = _coverage(mat_by_sid)
    cov_cf = _coverage(cfade_by_sid)
    uncovered = [s for s, d in depth_info.items() if not d["usable"]]
    report["stages"]["0_coverage"] = {
        "mat": cov_mat, "cfade": cov_cf, "depth_info": depth_info,
        "uncovered_sessions": uncovered,
    }
    md += [
        "## Stage 0 — coverage & causality",
        "",
        f"MAT triggers: n={cov_mat['n_triggers']} defined={cov_mat['n_defined']} "
        f"({cov_mat['frac_defined']}) · absolute states {cov_mat['abs_states']}",
        f"CFADE triggers: n={cov_cf['n_triggers']} defined={cov_cf['n_defined']} "
        f"({cov_cf['frac_defined']}) · absolute states {cov_cf['abs_states']}",
        f"Sessions without usable depth: {uncovered if uncovered else 'none'}",
        "",
        "| session | depth NQ rows | depth ES rows | triggers | defined |",
        "|---|---:|---:|---:|---:|",
    ]
    for sid in sids:
        d = depth_info[sid]
        md.append(f"| {sid} | {d['depth_nq_rows']} | {d['depth_es_rows']} | "
                  f"{d['n_triggers']} | {d['n_defined']} |")
    md.append("")

    # Truncation: event imbalances for events <= T identical on depth cut at T
    trunc_rows = []
    sec_ns = 1_000_000_000
    for sid, (d_nq, ib_n, ia_n, all_ts) in trunc_data.items():
        pair = next(p for p in pairs if p.sid == sid)
        t0, t1 = pair.bounds
        T = ((t0 + int(0.7 * (t1 - t0))) // sec_ns) * sec_ns
        ev_sel = all_ts[all_ts <= T]
        if len(ev_sel) == 0:
            continue
        full_imb, _ = market_event_imbalance(d_nq, ib_n, ia_n, ev_sel, bp)
        ts = d_nq.column("ts_ns").to_numpy().astype(np.int64)
        k = ts <= T
        d_cut = d_nq.take(np.where(k)[0]) if k.any() else d_nq.slice(0, 0)
        cut_imb, _ = market_event_imbalance(d_cut, ib_n, ia_n, ev_sel, bp)
        both_nan = ~np.isfinite(full_imb) & ~np.isfinite(cut_imb)
        diff = np.where(np.isfinite(full_imb) & np.isfinite(cut_imb),
                        np.abs(full_imb - cut_imb), 0.0)
        n_diff = int(((diff > 1e-9) | (np.isfinite(full_imb) != np.isfinite(cut_imb))).sum())
        trunc_rows.append({"session": sid, "T_ns": int(T), "n_compared": int(len(ev_sel)),
                           "n_diff": n_diff, "match": n_diff == 0})
    trunc_ok = all(r["match"] for r in trunc_rows) if trunc_rows else False
    report["stages"]["0_truncation"] = trunc_rows
    report["causality_truncation_ok"] = trunc_ok
    md.append(f"Truncation invariance (NQ imb, events <= T, {len(trunc_rows)} sessions): "
              f"**{'PASS' if trunc_ok else 'FAIL'}**")
    for r in trunc_rows:
        md.append(f"- {r['session']}: compared={r['n_compared']} diffs={r['n_diff']} match={r['match']}")
    md.append("")

    # Hand-check: chunked vs brute-force at one sample end per market (first usable session)
    hand = []
    for pair in pairs:
        info = depth_info[pair.sid]
        if not info["usable"]:
            continue
        d_nq, ib_n, ia_n = load_depth_window_with_book(parquet_dir, pair.sid, "NQ", pair.bounds)
        d_es, ib_e, ia_e = load_depth_window_with_book(parquet_dir, pair.sid, "ES", pair.bounds)
        t_end = pair.bounds[0] + 1800 * sec_ns
        for role, d, ib, ia in (("NQ", d_nq, ib_n, ia_n), ("ES", d_es, ib_e, ia_e)):
            chunked = float(replay_imbalance_samples(
                d, ib, ia, np.array([t_end], dtype=np.int64), top_levels=bp.top_levels)[0])
            brute = float(brute_imbalance_at(d, ib, ia, t_end, top_levels=bp.top_levels))
            both_nan = (not np.isfinite(chunked)) and (not np.isfinite(brute))
            match = both_nan or (np.isfinite(chunked) and np.isfinite(brute)
                                 and abs(chunked - brute) < 1e-12)
            hand.append({"session": pair.sid, "market": role, "t_end": int(t_end),
                         "chunked": chunked, "brute": brute, "match": bool(match)})
        break
    hand_ok = all(h["match"] for h in hand) if hand else False
    report["stages"]["0_handcheck"] = hand
    report["causality_handcheck_ok"] = hand_ok
    md.append(f"Hand-check chunked vs brute-force replay ({len(hand)} markets, first usable "
              f"session): **{'PASS' if hand_ok else 'FAIL'}**")
    for h in hand:
        md.append(f"- {h['session']} {h['market']}: chunked={h['chunked']:+.6f} "
                  f"brute={h['brute']:+.6f} match={h['match']}")
    md.append("")

    if not trunc_ok or not hand_ok:
        report["verdict"] = "reject — Stage 0 causality failed"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        return {"summary": report}

    # ---------------------------------------------------------------- Stage 1 strata
    print("[0054] Stage 1 — joined strata (MAT + CFADE)", flush=True)
    mat_gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    cfade_br = make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=4.0, cap=40.0)
    sim_mat = _mat_sim(pairs, mat_by_sid, gate=mat_gate, bracket_fn=make_vol_bracket(1.0),
                       slip=slip, cooldown=cooldown)
    r_mat = H.pack_pnl(sim_mat, sids)
    sim_cf = _cfade_sim(pairs, cfade_by_sid, gate=make_daytype_skip(max_er=er_max),
                        bracket_fn=cfade_br, slip=slip, cooldown=cooldown)
    r_cf = H.pack_pnl(sim_cf, sids)

    def _lookup(by_sid: dict[str, pa.Table]) -> dict:
        out = {}
        for sid, ev in by_sid.items():
            ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
            for t, s in zip(ts, ev.column("bi_side_state").to_pylist()):
                out[(sid, int(t))] = s
        return out

    mat_strata = _strata(sim_mat.trades, _lookup(mat_by_sid), cohorts=True)
    cf_strata = _strata(sim_cf.trades, _lookup(cfade_by_sid), cohorts=True)
    mat_matters, mat_checks = _matters(mat_strata, min_n=min_state_n, lift=lift)
    cf_matters, cf_checks = _matters(cf_strata, min_n=min_state_n, lift=lift)

    md += [
        "## Stage 1 — joined strata (descriptive; stratum-only experiment)",
        "",
        f"Frozen books reproduced on the depth-annotated trigger sets: "
        f"MAT n={r_mat['n']} net={r_mat['net']:+.1f} · CFADE n={r_cf['n']} net={r_cf['net']:+.1f} "
        f"(compare EXP-0050..0053: MAT +779 / CFADE +663 on identical books).",
        "",
        "| book | stratum | n | net | avg | train avg (n) | hold avg (n) | new avg (n) |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for book, strata, r_all in (("MAT", mat_strata, r_mat), ("CFADE", cf_strata, r_cf)):
        for name in ("agree_with", "agree_against", "other", "warmup", "rest_defined"):
            s = strata[name]
            def _c(coh):
                c = s.get(coh) or {}
                return f"{c.get('avg')} ({c.get('n')})"
            md.append(f"| {book} | {name} | {s['n']} | {s['net']:+.1f} | {s['avg']} | "
                      f"{_c('train')} | {_c('hold')} | {_c('new')} |")
    md += [
        "",
        f"MAT matters checks: {mat_checks} -> **{'MATTERS' if mat_matters else 'does not matter'}**",
        f"CFADE matters checks: {cf_checks} -> **{'MATTERS' if cf_matters else 'does not matter'}**",
        "",
    ]

    report["stages"]["1"] = {
        "mat_book": r_mat, "cfade_book": r_cf,
        "mat_strata": mat_strata, "cfade_strata": cf_strata,
        "mat_matters": mat_matters, "mat_checks": mat_checks,
        "cfade_matters": cf_matters, "cfade_checks": cf_checks,
    }
    matters_books = [b for b, m in (("MAT", mat_matters), ("CFADE", cf_matters)) if m]
    if matters_books:
        report["verdict"] = (
            f"state matters on {', '.join(matters_books)} — entitled only to a future "
            "Tier B execution-timer preregistration; nothing ships from EXP-0054"
        )
    else:
        report["verdict"] = (
            "reject / park — imbalance-agreement state matters on neither book "
            "(stratum-only; no gate, no May, no retune)"
        )
    md += ["## Verdict", "", f"**{report['verdict']}**", ""]
    H.write_report(results_dir, report, md)
    return {"summary": report}
