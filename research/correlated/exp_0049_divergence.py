"""EXP-0049 / H40 — cumulative ES/NQ tape divergence (dose-response CFADE).

Stages A→B→C on the 23d discovery sample; Stage D (May) only if C passes.
Kill bars are sequential — first failure stops and parks.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    make_daytype_skip,
    make_vol_bracket,
    make_vol_bracket_rr,
    simulate_session,
)
from research.correlated import harness as H
from research.correlated.exp_0036_fade_dig import _concentration
from research.features.corr_divergence import (
    DivergenceParams,
    divergence_fade_events,
    make_params,
)
from research.features.corr_exhaust import ExhaustParams, exhaust_fade_events

# CFADE DIG4 freeze sample (23d) — discovery only; May is Stage D.
DIG4_23 = [
    "011026", "021026", "030826", "040826", "050826", "051026", "061026",
    "071026", "081026", "091026", "121026", "131026", "141026", "151026",
    "160926", "161026", "170926", "180926", "210926", "220926", "260326",
    "290926", "300926",
]


def _loo(trades, sids: list[str], commission: float = H.COMMISSION) -> tuple[list[dict], int]:
    base_net = sum(t.pnl - commission for t in trades)
    rows = []
    for sid in sids:
        net = sum(t.pnl - commission for t in trades if t.session_id != sid)
        rows.append({
            "drop": sid,
            "cohort": H.cohort(sid),
            "net": round(net, 2),
            "delta": round(net - base_net, 2),
        })
    n_neg = sum(1 for r in rows if r["net"] < 0)
    return rows, n_neg


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
    )
    parts = [
        simulate_session(
            p.sid, p.nq, p.es, cfg, gate=gate, bracket_fn=bracket_fn,
            vwap_from_ns=p.vwap_from_ns, events=by_sid[p.sid],
        )
        for p in pairs if p.sid in by_sid
    ]
    return H.merge(parts) if parts else SimSummary()


def _events_for_pair(
    pair: H.Pair,
    p: DivergenceParams,
    bar_ms: int,
) -> pa.Table:
    nq_b, es_b = pair.bars(bar_ms)
    fe = divergence_fade_events(
        nq_b, es_b, t0_ns=pair.bounds[0], t1_ns=pair.bounds[1], p=p,
    )
    if fe.num_rows:
        fe = H._with_session(fe, pair.sid)
    return fe


def _concat_raw(parts: list[pa.Table]) -> pa.Table | None:
    parts = [t for t in parts if t.num_rows > 0]
    if not parts:
        return None
    return pa.concat_tables(parts, promote_options="default")


def _tercile_masks(x: np.ndarray) -> dict[str, np.ndarray]:
    """Terciles of |x| among finite values; returns lo/mid/hi boolean masks."""
    out = {
        "lo": np.zeros(len(x), dtype=bool),
        "mid": np.zeros(len(x), dtype=bool),
        "hi": np.zeros(len(x), dtype=bool),
    }
    ok = np.isfinite(x)
    if ok.sum() < 3:
        return out
    ax = np.abs(x[ok])
    q1, q2 = np.quantile(ax, [1 / 3, 2 / 3])
    vals = np.abs(x)
    out["lo"] = ok & (vals <= q1)
    out["mid"] = ok & (vals > q1) & (vals <= q2)
    out["hi"] = ok & (vals > q2)
    return out


def _monotone_means(means: list[float | None]) -> bool:
    """True if non-decreasing among finite values (allow flat)."""
    prev = None
    for m in means:
        if m is None or not np.isfinite(m):
            continue
        if prev is not None and m + 1e-9 < prev:
            return False
        prev = m
    return prev is not None


def truncation_invariance_check(
    pair: H.Pair,
    p: DivergenceParams,
    bar_ms: int,
    *,
    frac: float = 0.7,
) -> dict:
    """Events on trades truncated at T must match full-session events stamped ≤ T."""
    t0, t1 = pair.bounds
    span = t1 - t0
    T = t0 + int(frac * span)
    # Full
    full = _events_for_pair(pair, p, bar_ms)
    # Truncated trades
    def _cut(t: pa.Table) -> pa.Table:
        if t.num_rows == 0:
            return t
        ts = t.column("ts_ns").to_numpy().astype(np.int64)
        k = ts <= T
        return t.take(np.where(k)[0]) if k.any() else t.slice(0, 0)

    trunc_pair = H.Pair(pair.sid, _cut(pair.nq), _cut(pair.es), (t0, min(t1, T + 1)))
    trunc = _events_for_pair(trunc_pair, p, bar_ms)

    full_ts = (
        full.column("event_ts_ns").to_numpy().astype(np.int64)
        if full.num_rows else np.zeros(0, dtype=np.int64)
    )
    full_le = full_ts[full_ts <= T]
    trunc_ts = (
        trunc.column("event_ts_ns").to_numpy().astype(np.int64)
        if trunc.num_rows else np.zeros(0, dtype=np.int64)
    )
    # Truncated window end is T; events stamped at bar end ≤ T should match
    match = np.array_equal(full_le, trunc_ts)
    return {
        "session": pair.sid,
        "T_frac": frac,
        "full_le_n": int(len(full_le)),
        "trunc_n": int(len(trunc_ts)),
        "match": bool(match),
        "full_le_ts": full_le.tolist()[:20],
        "trunc_ts": trunc_ts.tolist()[:20],
    }


def alignment_spot_check(
    pair: H.Pair,
    p: DivergenceParams,
    bar_ms: int,
    *,
    n_show: int = 3,
) -> list[dict]:
    """Hand-readable dump of a few events vs underlying window stats."""
    fe = _events_for_pair(pair, p, bar_ms)
    if fe.num_rows == 0:
        return []
    rows = []
    n = min(n_show, fe.num_rows)
    for i in range(n):
        rows.append({
            "session": pair.sid,
            "event_ts_ns": int(fe.column("event_ts_ns")[i].as_py()),
            "side": int(fe.column("side")[i].as_py()),
            "push_side": int(fe.column("push_side")[i].as_py()),
            "nq_imb_w": float(fe.column("nq_imb_w")[i].as_py()),
            "es_imb_w": float(fe.column("es_imb_w")[i].as_py()),
            "D": float(fe.column("D")[i].as_py()),
            "nq_progress_pts": float(fe.column("nq_progress_pts")[i].as_py()),
            "nq_vol_w": float(fe.column("nq_vol_w")[i].as_py()),
            "es_vol_w": float(fe.column("es_vol_w")[i].as_py()),
        })
    return rows


def run(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    parquet_dir = Path(cfg["parquet_dir"])
    results_dir = Path(cfg["results_dir"]) / exp["name"]
    horizons = list(exp.get("horizons_ms") or cfg["outcome_horizons_ms"])
    targets = list(exp.get("point_targets") or cfg["nq_point_targets"])
    exclude = set(str(s) for s in exp.get("exclude_sessions") or [])
    bar_ms = int(exp.get("bar_ms", 100))
    kill = dict(exp.get("kill") or {})
    run_may = bool(exp.get("run_may", False))
    slip = float(exp.get("slip_ticks", 1.0))
    cooldown = float(exp.get("signal_cooldown_sec", 5.0))

    from research.run_mat_may import MAY

    # Discovery = DIG4 23d freeze sample only (not every parquet session).
    discovery_ids = [s for s in DIG4_23 if s not in exclude]
    missing = [s for s in discovery_ids if s not in set(session_ids)]
    if missing:
        print(f"[0049] WARN missing parquet for discovery sids: {missing}", flush=True)

    pairs: list[H.Pair] = []
    for sid in discovery_ids:
        if sid not in set(session_ids) and missing:
            # still try load — session_ids from CLI may be "all"
            pass
        p = H.load_pair(parquet_dir, sid)
        if p is not None:
            pairs.append(p)
    sids = [p.sid for p in pairs]
    print(f"[0049] discovery {len(pairs)}/{len(DIG4_23)} DIG4 sessions: {sids}", flush=True)

    div_kw = dict(exp.get("divergence") or {})
    div_kw["bar_ms"] = bar_ms  # YAML + top-level agree; top-level wins
    base = DivergenceParams(**div_kw)
    cells: dict[str, DivergenceParams] = {"primary": base}
    for name, over in (exp.get("neighbours") or {}).items():
        cells[name] = make_params(base, **over)

    # Comparator param variants (same W/persist as primary)
    comps: dict[str, DivergenceParams] = {
        "nq_only": make_params(base, require_es=False),
        "no_progress": make_params(base, require_progress=False),
        "shifted_es": make_params(base, es_shift_sec=60.0),
    }

    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        exp.get("description", "").strip(),
        "",
        f"Sessions(discovery)={len(sids)} · UK 14:30–17:00 · {slip}-tick slip · "
        f"{H.COMMISSION} pt/RT · train={len([s for s in sids if H.cohort(s)=='train'])} "
        f"hold={len([s for s in sids if H.cohort(s)=='hold'])} "
        f"new={len([s for s in sids if H.cohort(s)=='new'])}",
        "",
        "## Pre-registered cells",
        "",
        "| cell | W s | persist s | nq_imb≥ | es_imb≤ | D≥ | progress≥ | role |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name, p in cells.items():
        role = "PRIMARY" if name == "primary" else "neighbour (robustness)"
        md.append(
            f"| {name} | {p.window_sec:g} | {p.persist_sec:g} | {p.nq_min_imb} | "
            f"{p.es_imb_max} | {p.min_D} | {p.nq_min_progress_pts} | {role} |"
        )
    md.append("")

    report: dict = {
        "status": "ok",
        "sessions": sids,
        "primary": asdict(base),
        "kill": kill,
        "stages": {},
    }

    # ------------------------------------------------------------------ causality
    print("[0049] causality checks …", flush=True)
    # Use persist=0 for a non-vacuous truncation check (primary is sparse by design).
    trunc_params = make_params(base, persist_sec=0.0)
    causality = {"truncation": [], "truncation_primary": [], "alignment": []}
    for p in pairs[:3]:
        causality["truncation"].append(truncation_invariance_check(p, trunc_params, bar_ms))
        causality["truncation_primary"].append(truncation_invariance_check(p, base, bar_ms))
        # Prefer denser cell for alignment readability
        spots = alignment_spot_check(p, trunc_params, bar_ms, n_show=2)
        if not spots:
            spots = alignment_spot_check(p, base, bar_ms, n_show=2)
        causality["alignment"].extend(spots)
    trunc_ok = all(c["match"] for c in causality["truncation"]) if causality["truncation"] else False
    trunc_n = sum(c["full_le_n"] for c in causality["truncation"])
    report["causality"] = causality
    report["causality_truncation_ok"] = trunc_ok
    md += [
        "## Causality",
        "",
        f"Truncation invariance (persist=0, first 3 sessions, frac=0.7, "
        f"n_events_checked={trunc_n}): **{'PASS' if trunc_ok else 'FAIL'}**",
        "",
    ]
    for c in causality["truncation"]:
        md.append(
            f"- {c['session']}: full_le={c['full_le_n']} trunc={c['trunc_n']} "
            f"match={c['match']}"
        )
    md += ["", "Alignment spot-check (sample events):", ""]
    for a in causality["alignment"][:6]:
        md.append(
            f"- {a['session']} side={a['side']} push={a['push_side']} "
            f"nq_imb={a['nq_imb_w']:+.2f} es_imb={a['es_imb_w']:+.2f} "
            f"D={a['D']:+.2f} prog={a['nq_progress_pts']:+.1f}pt "
            f"vols nq/es={a['nq_vol_w']:.0f}/{a['es_vol_w']:.0f}"
        )
    md.append("")
    if not trunc_ok:
        report["verdict"] = "reject — truncation invariance failed"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        return {"summary": report}

    # ------------------------------------------------------------------ emit events
    raw_by_cell: dict[str, list[pa.Table]] = {k: [] for k in cells}
    raw_by_comp: dict[str, list[pa.Table]] = {k: [] for k in comps}
    by_sid_primary: dict[str, pa.Table] = {}

    for pair in pairs:
        print(f"[0049] emit {pair.sid}", flush=True)
        for name, p in cells.items():
            fe = _events_for_pair(pair, p, bar_ms)
            if fe.num_rows:
                raw_by_cell[name].append(fe)
                if name == "primary":
                    by_sid_primary[pair.sid] = fe
        for name, p in comps.items():
            fe = _events_for_pair(pair, p, bar_ms)
            if fe.num_rows:
                raw_by_comp[name].append(fe)

    # CFADE comparator events
    p_ex = ExhaustParams(**(exp.get("cfade_exhaust") or {}))
    fade_wait = float(exp.get("cfade_max_wait_sec", 5.0))
    cfade_raw: list[pa.Table] = []
    by_sid_cfade: dict[str, pa.Table] = {}
    for pair in pairs:
        nq_b, es_b = pair.bars(bar_ms)
        bursts = H.confirm_bursts(pair)
        fe = exhaust_fade_events(bursts, nq_b, es_b, p_ex, max_wait_sec=fade_wait)
        if fe.num_rows:
            fe = H._with_session(fe, pair.sid)
            cfade_raw.append(fe)
            by_sid_cfade[pair.sid] = fe

    # ------------------------------------------------------------------ Stage A
    print("[0049] Stage A — counts + dose-response", flush=True)
    stage_a: dict = {"counts": {}, "dose": {}}
    md += ["## Stage A — counts + dose-response", ""]

    primary_all = _concat_raw(raw_by_cell["primary"])
    n_primary = 0 if primary_all is None else primary_all.num_rows
    stage_a["counts"]["primary"] = n_primary
    for name in cells:
        t = _concat_raw(raw_by_cell[name])
        stage_a["counts"][name] = 0 if t is None else t.num_rows
    for name in comps:
        t = _concat_raw(raw_by_comp[name])
        stage_a["counts"][name] = 0 if t is None else t.num_rows
    stage_a["counts"]["cfade"] = sum(t.num_rows for t in cfade_raw)

    md += [
        "| set | n |",
        "|---|---:|",
    ]
    for k, v in stage_a["counts"].items():
        md.append(f"| {k} | {v} |")
    md.append("")

    min_n = int(kill.get("stage_a_min_n", 200))
    stage_a_pass_n = n_primary >= min_n

    # Dose-response: label primary, stratify by |D| tercile and by persist neighbours
    label_primary_parts: list[tuple[str, pa.Table]] = []
    for pair in pairs:
        fe = by_sid_primary.get(pair.sid)
        if fe is None or fe.num_rows == 0:
            label_primary_parts.append((pair.sid, pa.table({})))
            continue
        label_primary_parts.append(
            (pair.sid, H.label_in_points(fe, pair, horizons, targets))
        )
    labeled_primary = H.summarize_by_cohort(label_primary_parts, horizons, targets)

    # |D| terciles on concatenated labelled table
    lab_parts = [t for _, t in label_primary_parts if t.num_rows > 0]
    dose_D = {"lo": None, "mid": None, "hi": None}
    dose_persist = {}
    if lab_parts:
        lab = pa.concat_tables(lab_parts, promote_options="default")
        D = lab.column("D").to_numpy().astype(np.float64)
        masks = _tercile_masks(D)
        for key, m in masks.items():
            sub = lab.take(np.where(m)[0]) if m.any() else pa.table({})
            s = H.summarize([sub], horizons, targets) if sub.num_rows else {"n_events": 0}
            dose_D[key] = {
                "n": s.get("n_events", 0),
                "hit10": s.get("p_hit_10pts"),
                "ret60": s.get("mean_ret_pts_60000ms"),
            }
        stage_a["dose"]["D_tercile"] = dose_D

    # Persistence strata via neighbour cells labeled
    for pname in ("persist0", "primary", "persist10"):
        src = raw_by_cell.get("primary" if pname == "primary" else pname, [])
        if pname == "primary":
            s = labeled_primary["all"]
        else:
            parts = []
            by = {t.column("session_id")[0].as_py(): t for t in src if t.num_rows}
            for pair in pairs:
                fe = by.get(pair.sid)
                if fe is None:
                    continue
                parts.append((pair.sid, H.label_in_points(fe, pair, horizons, targets)))
            s = H.summarize([t for _, t in parts], horizons, targets) if parts else {"n_events": 0}
        dose_persist[pname] = {
            "n": s.get("n_events", 0),
            "hit10": s.get("p_hit_10pts"),
            "ret60": s.get("mean_ret_pts_60000ms"),
        }
    stage_a["dose"]["persist"] = dose_persist

    md += [
        "### Dose-response by |D| tercile (primary)",
        "",
        "| tercile | n | hit10 | @60s |",
        "|---|---:|---:|---:|",
    ]
    for key in ("lo", "mid", "hi"):
        d = dose_D.get(key) or {}
        md.append(
            f"| {key} | {d.get('n', 0)} | "
            f"{d.get('hit10') if d.get('hit10') is not None else '–'} | "
            f"{H.fmt(d.get('ret60'), 2)} |"
        )
    md += [
        "",
        "### Dose-response by persistence (neighbours)",
        "",
        "| persist | n | hit10 | @60s |",
        "|---|---:|---:|---:|",
    ]
    for key in ("persist0", "primary", "persist10"):
        d = dose_persist.get(key) or {}
        label = {"persist0": "0s", "primary": "5s (primary)", "persist10": "10s"}[key]
        md.append(
            f"| {label} | {d.get('n', 0)} | "
            f"{d.get('hit10') if d.get('hit10') is not None else '–'} | "
            f"{H.fmt(d.get('ret60'), 2)} |"
        )
    md.append("")

    means_D = [((dose_D.get(k) or {}).get("ret60")) for k in ("lo", "mid", "hi")]
    means_P = [((dose_persist.get(k) or {}).get("ret60")) for k in ("persist0", "primary", "persist10")]
    mono_D = _monotone_means(means_D)
    mono_P = _monotone_means(means_P)
    # Plan: "fade outcome not monotone increasing in |D| and persistence"
    # Require both axes monotone (or at least |D|; persistence neighbours may be thin)
    stage_a_pass_mono = mono_D and mono_P
    stage_a_pass = stage_a_pass_n and stage_a_pass_mono
    stage_a["pass_n"] = stage_a_pass_n
    stage_a["pass_mono_D"] = mono_D
    stage_a["pass_mono_persist"] = mono_P
    stage_a["pass"] = stage_a_pass
    report["stages"]["A"] = stage_a
    md += [
        f"**Stage A kill:** n≥{min_n} → {stage_a_pass_n} (n={n_primary}); "
        f"monotone |D| → {mono_D}; monotone persist → {mono_P}. "
        f"**{'PASS' if stage_a_pass else 'FAIL'}**",
        "",
    ]
    if not stage_a_pass:
        report["verdict"] = (
            f"reject / park — Stage A fail "
            f"(n={n_primary}>={min_n}? {stage_a_pass_n}; "
            f"mono_D={mono_D}; mono_P={mono_P})"
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        out: dict = {"summary": report}
        if primary_all is not None:
            out["events"] = primary_all
        return out

    # ------------------------------------------------------------------ Stage B
    print("[0049] Stage B — event study vs nulls", flush=True)
    stage_b: dict = {}
    md += ["## Stage B — event study", ""]

    # Label all comparators
    def _label_raw(raw_list: list[pa.Table]) -> list[tuple[str, pa.Table]]:
        by = {}
        for t in raw_list:
            if t.num_rows == 0:
                continue
            sid = t.column("session_id")[0].as_py()
            by[sid] = t
        parts = []
        for pair in pairs:
            fe = by.get(pair.sid)
            if fe is None:
                parts.append((pair.sid, pa.table({})))
            else:
                parts.append((pair.sid, H.label_in_points(fe, pair, horizons, targets)))
        return parts

    null_parts = []
    for pair in pairs:
        n_ev = by_sid_primary.get(pair.sid)
        n = n_ev.num_rows if n_ev is not None else 1
        null_parts.append(
            (pair.sid, H.label_in_points(H.null_events(pair, max(n, 1), seed=49), pair, horizons, targets))
        )

    sets = {
        "primary": label_primary_parts,
        "nq_only": _label_raw(raw_by_comp["nq_only"]),
        "no_progress": _label_raw(raw_by_comp["no_progress"]),
        "shifted_es": _label_raw(raw_by_comp["shifted_es"]),
        "null": null_parts,
    }
    ev_sum = {k: H.summarize_by_cohort(v, horizons, targets) for k, v in sets.items()}
    stage_b["event_study"] = {k: v["all"] for k, v in ev_sum.items()}
    stage_b["by_cohort"] = {k: {c: v[c] for c in ("train", "hold", "new", "old")} for k, v in ev_sum.items()}

    md += ["**all**", "", *H.EVENT_HDR]
    for k in ("null", "nq_only", "no_progress", "shifted_es", "primary"):
        md.append(H.event_row(k, ev_sum[k]["all"]))
    md.append("")
    for coh in ("train", "hold", "new"):
        md += [f"**{coh}**", "", *H.EVENT_HDR]
        for k in ("null", "nq_only", "primary"):
            md.append(H.event_row(k, ev_sum[k][coh]))
        md.append("")

    prim = ev_sum["primary"]["all"]
    nq_o = ev_sum["nq_only"]["all"]
    nop = ev_sum["no_progress"]["all"]
    nul = ev_sum["null"]["all"]
    ret60 = prim.get("mean_ret_pts_60000ms")
    hit10 = prim.get("p_hit_10pts")
    nq_hit = nq_o.get("p_hit_10pts")
    min_ret = float(kill.get("stage_b_min_ret60", 1.5))
    min_lift = float(kill.get("stage_b_min_hit10_lift_vs_nq_only_pp", 5.0)) / 100.0

    ret_ok = ret60 is not None and np.isfinite(ret60) and ret60 >= min_ret
    lift = (hit10 - nq_hit) if (hit10 is not None and nq_hit is not None) else None
    lift_ok = lift is not None and lift >= min_lift
    above_null = (
        ret60 is not None and nul.get("mean_ret_pts_60000ms") is not None
        and ret60 > nul["mean_ret_pts_60000ms"]
        and hit10 is not None and nul.get("p_hit_10pts") is not None
        and hit10 > nul["p_hit_10pts"]
    )
    above_nop = (
        ret60 is not None and nop.get("mean_ret_pts_60000ms") is not None
        and ret60 > nop["mean_ret_pts_60000ms"]
    )
    stage_b_pass = bool(ret_ok and lift_ok and above_null and above_nop)
    stage_b.update({
        "ret60": ret60,
        "hit10": hit10,
        "lift_vs_nq_only": lift,
        "ret_ok": ret_ok,
        "lift_ok": lift_ok,
        "above_null": above_null,
        "above_no_progress": above_nop,
        "pass": stage_b_pass,
    })
    report["stages"]["B"] = stage_b
    md += [
        f"**Stage B kill:** @60s≥{min_ret} → {ret_ok} ({H.fmt(ret60, 2)}); "
        f"hit10 lift vs NQ-only ≥{min_lift*100:.0f}pp → {lift_ok} "
        f"(lift={None if lift is None else f'{lift*100:.1f}pp'}); "
        f"above null → {above_null}; above no-progress → {above_nop}. "
        f"**{'PASS' if stage_b_pass else 'FAIL'}**",
        "",
    ]
    if not stage_b_pass:
        report["verdict"] = "reject / park — Stage B fail (event study)"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        out = {"summary": report}
        if primary_all is not None:
            out["events"] = primary_all
        return out

    # ------------------------------------------------------------------ Stage C
    print("[0049] Stage C — P&L", flush=True)
    stage_c: dict = {}
    md += ["## Stage C — P&L (discovery)", "", *H.PNL_HDR]

    br_rr = make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=4.0, cap=40.0)
    br_sym = make_vol_bracket(1.0)
    gate_er = make_daytype_skip(max_er=0.3)

    # CFADE yardstick on same sessions
    cfade_sim = _sim(pairs, by_sid_cfade, gate=gate_er, bracket_fn=br_rr, slip=slip, cooldown=cooldown)
    cfade_r = H.pack_pnl(cfade_sim, sids)
    cfade_avg = (cfade_r["net"] / cfade_r["n"]) if cfade_r["n"] else 0.0
    stage_c["cfade_rr"] = cfade_r
    md.append(H.pnl_row("CFADE against_m04 · ER0.3 · RR1/2", cfade_r))

    specs = [
        ("primary_rr", None, br_rr),
        ("primary_sym", None, br_sym),
        ("primary_rr_er_stratum", gate_er, br_rr),  # stratum report only
        ("primary_rr_slip2", None, br_rr),
    ]
    for name, gate, br in specs:
        sl_use = 2.0 if "slip2" in name else slip
        sim = _sim(pairs, by_sid_primary, gate=gate, bracket_fn=br, slip=sl_use, cooldown=cooldown)
        r = H.pack_pnl(sim, sids)
        conc = _concentration(sim.trades, sids)
        loo_rows, loo_neg = _loo(sim.trades, sids)
        avg = (r["net"] / r["n"]) if r["n"] else 0.0
        stage_c[name] = {
            **r,
            "avg": round(avg, 3),
            "concentration": conc,
            "loo_neg": loo_neg,
            "loo": loo_rows,
        }
        md.append(H.pnl_row(name, r))
        print(
            f"  {name:28s} n={r['n']:4d} net={r['net']:+8.1f} "
            f"train={r['train_net']:+7.1f} hold={r['hold_net']:+7.1f} new={r['new_net']:+7.1f} "
            f"days+={conc['days_pos']} top3={conc['top3_share_of_pos']} loo_neg={loo_neg}",
            flush=True,
        )
    md.append("")

    prim_c = stage_c["primary_rr"]
    conc = prim_c["concentration"]
    min_days = int(kill.get("stage_c_min_days_pos", 14))
    max_top3 = float(kill.get("stage_c_max_top3_share", 0.55))
    max_loo = int(kill.get("stage_c_max_loo_neg", 2))
    min_avg_frac = float(kill.get("stage_c_min_avg_frac_of_cfade", 0.50))
    avg_ok = (
        cfade_avg > 0 and prim_c["avg"] >= min_avg_frac * cfade_avg
    ) or (cfade_avg <= 0 and prim_c["avg"] > 0)

    checks = {
        "net_ok": prim_c["net"] > 0,
        "train_ok": prim_c["train_net"] > 0,
        "hold_ok": prim_c["hold_net"] > 0,
        "days_ok": conc["days_pos"] >= min_days,
        "top3_ok": (conc["top3_share_of_pos"] or 1.0) <= max_top3,
        "loo_ok": prim_c["loo_neg"] <= max_loo,
        "avg_ok": bool(avg_ok),
    }
    stage_c_pass = all(checks.values())
    stage_c["checks"] = checks
    stage_c["pass"] = stage_c_pass
    stage_c["cfade_avg"] = cfade_avg
    report["stages"]["C"] = stage_c
    md += [
        f"Primary RR checks: {checks}",
        f"avg={prim_c['avg']:.3f} vs 50% of CFADE avg={cfade_avg:.3f}",
        f"days+={conc['days_pos']}/{len(sids)} top3_share={conc['top3_share_of_pos']} "
        f"loo_neg={prim_c['loo_neg']}",
        f"**Stage C: {'PASS' if stage_c_pass else 'FAIL'}**",
        "",
    ]
    if not stage_c_pass:
        report["verdict"] = "reject / park — Stage C fail (P&L discovery)"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        H.write_report(results_dir, report, md)
        out = {"summary": report}
        if primary_all is not None:
            out["events"] = primary_all
        return out

    # ------------------------------------------------------------------ Stage D (May) — only if C passed AND run_may
    # Auto-enable May once C passes (plan: freeze then read May once)
    print("[0049] Stage C passed — freezing and reading May once", flush=True)
    report["frozen"] = True
    md += [
        "## Freeze",
        "",
        "Stage C passed. Primary cell thresholds frozen. Reading May 2026 once.",
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
    by_sid_may: dict[str, pa.Table] = {}
    may_raw: list[pa.Table] = []
    for pair in may_pairs:
        fe = _events_for_pair(pair, base, bar_ms)
        if fe.num_rows:
            by_sid_may[pair.sid] = fe
            may_raw.append(fe)
        print(f"  MAY {pair.sid}: n={fe.num_rows}", flush=True)

    may_sim = _sim(may_pairs, by_sid_may, gate=None, bracket_fn=br_rr, slip=slip, cooldown=cooldown)
    may_r = H.pack_pnl(may_sim, may_sids)
    may_avg = (may_r["net"] / may_r["n"]) if may_r["n"] else 0.0
    may_pass = may_r["net"] > 0 and may_avg > 0 and prim_c["net"] > 0
    stage_d = {
        "sessions": may_sids,
        "n_events": sum(t.num_rows for t in may_raw),
        "pnl": may_r,
        "avg": round(may_avg, 3),
        "pass": may_pass,
        "target_n": int(exp.get("target_n_may", 80)),
    }
    report["stages"]["D"] = stage_d
    md += [
        "## Stage D — May 2026 OOS (once)",
        "",
        *H.PNL_HDR,
        H.pnl_row("May primary RR", may_r),
        "",
        f"avg={may_avg:.3f} · events={stage_d['n_events']} · "
        f"**{'PASS' if may_pass else 'FAIL'}**",
        "",
    ]
    if may_pass:
        report["verdict"] = (
            "DIG / soft-confirm — Stage D May positive; "
            "do not ship without further months; no Java yet"
        )
    else:
        report["verdict"] = "reject / park — Stage D May fail (no retune)"
    md += ["## Verdict", "", f"**{report['verdict']}**", ""]

    H.write_report(results_dir, report, md)
    out = {"summary": report}
    if primary_all is not None:
        out["events"] = primary_all
    return out
