"""EXP-0036 — correlated exhaustion as an exit (and as a standalone fade).

Part A (exit overlay): rerun the frozen MAT base(s) with ``exit_fn`` =
first 100 ms bar after entry where NQ is still with-trade but ES has stopped.
Yardstick: P&L of the same base with brackets only (same sessions, same
gates). Reported on the full run and on matched entries (trades that exist in
both runs) so flat-only drift does not blur the per-trade effect.

Part B (fade event): after every confirm burst, the first exhaustion bar within
``fade_max_wait_sec``; side = against the burst. Event study in points vs a
random null and vs the corrected EXP-0008 bursts themselves; P&L via the MAT sim
with the events override.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    combine_gates,
    make_trend_mode,
    make_vol_bracket,
    make_vwap_side,
    simulate_session,
)
from research.correlated import harness as H
from research.backtest.mat_sim import filter_summary
from research.features.corr_exhaust import (
    ExhaustParams,
    build_exhaust_exit_fn,
    exhaust_fade_events,
)


def _base_cfg(exp: dict, base: dict) -> tuple[MatSimConfig, object, object]:
    cfg = MatSimConfig(
        bar_ms=int(exp.get("bar_ms", 100)),
        min_trades=int(exp.get("min_trades", 6)),
        min_vol=float(exp.get("min_vol", 25)),
        min_imbalance=float(exp.get("min_imbalance", 0.55)),
        min_progress_ticks=float(exp.get("min_progress_ticks", 1.0)),
        sync_ms=int(exp.get("sync_ms", 1500)),
        max_es_lag_ms=float(base.get("max_es_lag_ms", exp.get("max_es_lag_ms", 250))),
        signal_cooldown_sec=float(exp.get("signal_cooldown_sec", 5)),
        slip_ticks=float(exp.get("slip_ticks", 1)),
        commission_pts_rt=float(exp.get("commission_pts_rt", H.COMMISSION)),
        es_stamp=str(base.get("es_stamp", "causal")),
        tp_pts=float(base.get("tp_pts", 10)),
        sl_pts=float(base.get("sl_pts", 10)),
    )
    gates = []
    if base.get("trend", "with") != "off":
        gates.append(make_trend_mode(str(base.get("trend", "with"))))
    if base.get("vwap", True):
        gates.append(make_vwap_side(True))
    gate = combine_gates(*gates) if gates else None
    br = None
    if base.get("vol_k"):
        br = make_vol_bracket(float(base["vol_k"]))
    return cfg, gate, br


def _matched(base_trades, var_trades) -> dict:
    b = {(t.session_id, t.entry_ts_ns): t for t in base_trades}
    v = {(t.session_id, t.entry_ts_ns): t for t in var_trades}
    common = sorted(set(b) & set(v))
    fired = [k for k in common if v[k].reason.startswith("exhaust")]
    d_all = sum(v[k].pnl - b[k].pnl for k in common)
    d_fired = sum(v[k].pnl - b[k].pnl for k in fired)
    base_on_fired = sum(b[k].pnl for k in fired)
    var_on_fired = sum(v[k].pnl for k in fired)
    # what the bracket would have done on fired trades
    base_tp = sum(1 for k in fired if b[k].reason == "tp")
    base_sl = sum(1 for k in fired if b[k].reason == "sl")
    held = [v[k].bars_held for k in fired]
    return {
        "n_common": len(common),
        "n_base_only": len(set(b) - set(v)),
        "n_var_only": len(set(v) - set(b)),
        "n_fired": len(fired),
        "fire_rate": round(len(fired) / len(common), 3) if common else None,
        "delta_gross_common": round(d_all, 2),
        "delta_gross_fired": round(d_fired, 2),
        "fired_base_gross": round(base_on_fired, 2),
        "fired_var_gross": round(var_on_fired, 2),
        "fired_base_tp": base_tp,
        "fired_base_sl": base_sl,
        "fired_median_prints_held": float(np.median(held)) if held else None,
    }


def run(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    parquet_dir = Path(cfg["parquet_dir"])
    results_dir = Path(cfg["results_dir"]) / exp["name"]
    horizons = list(exp.get("horizons_ms") or cfg["outcome_horizons_ms"])
    targets = list(exp.get("point_targets") or cfg["nq_point_targets"])
    exclude = set(str(s) for s in exp.get("exclude_sessions") or [])
    bar_ms = int(exp.get("bar_ms", 100))

    pairs: list[H.Pair] = []
    for sid in session_ids:
        if sid in exclude:
            continue
        p = H.load_pair(parquet_dir, sid)
        if p is not None:
            pairs.append(p)
    sids = [p.sid for p in pairs]
    print(f"[0036] {len(pairs)} sessions: {sids}", flush=True)

    default_p = ExhaustParams(bar_ms=bar_ms, **(exp.get("exhaust") or {}))
    variants: dict[str, ExhaustParams] = {"default": default_p}
    for name, over in (exp.get("variants") or {}).items():
        variants[name] = ExhaustParams(**{**asdict(default_p), **over})

    # ------------------------------------------------------------ Part A
    skip_exits = bool(exp.get("skip_exit_overlay")) or not (exp.get("bases") or [])
    pnl_report: dict[str, dict] = {}
    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        exp.get("description", "").strip(),
        "",
        "**Pre-registered bar (exits):** net lift vs the same base on train, hold "
        "AND new cohorts, with maxDD not worse. **Bar (fade event):** hit+10 and "
        "@60s above both null and EXP-0008 confirm bursts on old AND new, n ≥ 50.",
        "",
        f"Sessions={len(sids)} · UK 14:30–17:00 · 1-tick slip · {H.COMMISSION} pt/RT · "
        f"train={len([s for s in sids if H.cohort(s)=='train'])} "
        f"hold={len([s for s in sids if H.cohort(s)=='hold'])} "
        f"new={len([s for s in sids if H.cohort(s)=='new'])}",
        "",
        "## Exhaustion rule parameters",
        "",
        "| variant | NQ imb≥ | NQ vol≥ | NQ n≥ | ES imb≤ | ES min vol | ES vol< frac·burst | empty ES | skip | max wait |",
        "|---|---:|---:|---:|---:|---:|---:|---|---:|---:|",
    ]
    for name, p in variants.items():
        md.append(
            f"| {name} | {p.nq_min_imbalance} | {p.nq_min_vol} | {p.nq_min_trades} | "
            f"{p.es_imb_max if p.use_imb else 'off'} | {p.es_min_vol or 'off'} | "
            f"{p.es_vol_frac if p.use_vol else 'off'} | {p.es_vol_counts_empty} | "
            f"{p.skip_bars} | {p.max_wait_sec or 'EOD'} |"
        )

    if skip_exits:
        md += ["", "## Part A — exit overlay", "", "Skipped (`skip_exit_overlay` / empty `bases`).", ""]
    else:
        md += ["", "## Part A — exit overlay on MAT bases", ""]

    for base in ([] if skip_exits else (exp.get("bases") or [])):
        bname = base["name"]
        sim_cfg, gate, br = _base_cfg(exp, base)
        print(f"[0036] base {bname}", flush=True)
        base_parts = [
            simulate_session(p.sid, p.nq, p.es, sim_cfg, gate=gate, bracket_fn=br, vwap_from_ns=p.vwap_from_ns)
            for p in pairs
        ]
        base_all = H.merge(base_parts)
        rows = {"bracket_only": H.pack_pnl(base_all, sids)}
        matched = {}
        for vname, p_ex in variants.items():
            parts = []
            for p in pairs:
                nq_b, es_b = p.bars(bar_ms)
                parts.append(
                    simulate_session(
                        p.sid, p.nq, p.es, sim_cfg, gate=gate, bracket_fn=br,
                        vwap_from_ns=p.vwap_from_ns,
                        exit_fn=build_exhaust_exit_fn(nq_b, es_b, p_ex),
                    )
                )
            var_all = H.merge(parts)
            rows[vname] = H.pack_pnl(var_all, sids)
            matched[vname] = _matched(base_all.trades, var_all.trades)
            r = rows[vname]
            print(
                f"  {vname:14s} n={r['n']:4d} net={r['net']:+8.1f} (base {rows['bracket_only']['net']:+.1f}) "
                f"train={r['train_net']:+.1f} hold={r['hold_net']:+.1f} new={r['new_net']:+.1f} "
                f"fired={matched[vname]['n_fired']}/{matched[vname]['n_common']} "
                f"d_fired={matched[vname]['delta_gross_fired']:+.1f}",
                flush=True,
            )
        pnl_report[bname] = {"base": base, "rows": rows, "matched": matched}

        brk_desc = (
            f"vol k={base['vol_k']}" if base.get("vol_k")
            else f"TP {base.get('tp_pts')} / SL {base.get('sl_pts')}"
        )
        md += [
            f"### {bname}  (stamp={base.get('es_stamp', 'causal')}, {brk_desc}, "
            f"trend={base.get('trend', 'with')}, vwap={base.get('vwap', True)})",
            "",
            *H.PNL_HDR,
        ]
        for vname, r in rows.items():
            md.append(H.pnl_row(vname, r))
        md += [
            "",
            "Matched entries (same trade in both runs):",
            "",
            "| variant | common | fired | fire% | Δgross all | Δgross fired | base on fired (tp/sl) | exit reasons |",
            "|---|---:|---:|---:|---:|---:|---|---|",
        ]
        for vname, m in matched.items():
            reasons = {k: v["n"] for k, v in rows[vname]["by_reason"].items()}
            md.append(
                f"| {vname} | {m['n_common']} | {m['n_fired']} | "
                f"{(m['fire_rate'] or 0)*100:.0f}% | {m['delta_gross_common']:+.1f} | {m['delta_gross_fired']:+.1f} | "
                f"{m['fired_base_gross']:+.1f} ({m['fired_base_tp']}/{m['fired_base_sl']}) | {reasons} |"
            )
        md.append("")

    # ------------------------------------------------------------ Part B
    print("[0036] fade event study", flush=True)
    fade_wait = float(exp.get("fade_max_wait_sec", 5.0))
    fade_keys = list(exp.get("fade_variants") or ["default"])
    fade_variants = {k: variants[k] for k in fade_keys if k in variants}
    if not fade_variants and "default" in variants:
        fade_variants = {"default": variants["default"]}
    gate_modes = list(exp.get("fade_pnl_gates") or ["none"])
    label_legs = list(exp.get("fade_label_legs") or ["NQ"])

    ev_report: dict = {}
    fade_tables: dict[str, dict[str, list[tuple[str, pa.Table]]]] = {
        leg: {k: [] for k in fade_variants} for leg in label_legs
    }
    fade_raw: dict[str, list[pa.Table]] = {k: [] for k in fade_variants}
    null_parts: dict[str, list[tuple[str, pa.Table]]] = {leg: [] for leg in label_legs}
    comp_parts: dict[str, list[tuple[str, pa.Table]]] = {leg: [] for leg in label_legs}

    for p in pairs:
        bursts = H.confirm_bursts(p)
        nq_b, es_b = p.bars(bar_ms)
        n_fade = 0
        for k, p_ex in fade_variants.items():
            fe = exhaust_fade_events(bursts, nq_b, es_b, p_ex, max_wait_sec=fade_wait)
            if fe.num_rows:
                fe = H._with_session(fe, p.sid)
                fade_raw[k].append(fe)
            for leg in label_legs:
                fade_tables[leg][k].append(
                    (p.sid, H.label_in_points(fe, p, horizons, targets, role=leg))
                )
            n_fade = max(n_fade, fe.num_rows)
        for leg in label_legs:
            # Sample null from the same tape we label against (ES null from ES trades).
            null_src = p if leg == "NQ" else type(p)(p.sid, p.es, p.es, p.bounds)
            null_parts[leg].append(
                (p.sid, H.label_in_points(
                    H.null_events(null_src, max(n_fade, 1), seed=42 + (0 if leg == "NQ" else 7)),
                    p, horizons, targets, role=leg,
                ))
            )
            if leg == "NQ" or (leg == "ES" and p.es.num_rows):
                comp_parts[leg].append(
                    (p.sid, H.label_in_points(bursts, p, horizons, targets, role=leg))
                )

    for leg in label_legs:
        ev_report[f"null_{leg}"] = H.summarize_by_cohort(null_parts[leg], horizons, targets)
        ev_report[f"exp0008_confirm_{leg}"] = H.summarize_by_cohort(comp_parts[leg], horizons, targets)
        for k in fade_variants:
            ev_report[f"fade_{k}_{leg}"] = H.summarize_by_cohort(fade_tables[leg][k], horizons, targets)

    md += ["## Part B — standalone fade event (side = against the burst)", ""]
    for leg in label_legs:
        md += [f"### Outcomes on {leg} path", ""]
        for coh in ("all", "old", "new"):
            md += [f"**{coh}**", "", *H.EVENT_HDR]
            md.append(H.event_row("null", ev_report[f"null_{leg}"][coh]))
            md.append(H.event_row("EXP-0008 confirm bursts", ev_report[f"exp0008_confirm_{leg}"][coh]))
            for k in fade_variants:
                md.append(H.event_row(f"fade {k}", ev_report[f"fade_{k}_{leg}"][coh]))
            md.append("")

    # Fade P&L via events override (± trend/VWAP gate)
    fade_pnl: dict = {}
    per_session_pnl: dict = {}
    want_per_session = bool(exp.get("per_session_pnl"))
    bracket_specs = list(exp.get("pnl_brackets") or ["vol_k1", "fixed_10_10"])
    brackets = []
    for bname in bracket_specs:
        if bname == "vol_k1":
            brackets.append(("vol_k1", make_vol_bracket(1.0)))
        elif bname == "fixed_10_10":
            brackets.append(("fixed_10_10", None))
        else:
            print(f"  skip unknown pnl_bracket {bname!r}", flush=True)
    gate_fns = {
        "none": None,
        "trend_vwap": combine_gates(make_trend_mode("with"), make_vwap_side(True)),
        "trend": make_trend_mode("with"),
        "vwap": make_vwap_side(True),
    }
    md += ["Fade P&L (events override on NQ; MAT flat-only/cooldown apply):", "", *H.PNL_HDR]
    for k in fade_variants:
        by_sid = {t.column("session_id")[0].as_py(): t for t in fade_raw[k] if t.num_rows}
        for gname in gate_modes:
            gate = gate_fns.get(gname)
            if gname not in gate_fns:
                print(f"  skip unknown fade_pnl_gate {gname!r}", flush=True)
                continue
            for bname, brk in brackets:
                sim_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=H.COMMISSION, tp_pts=10, sl_pts=10)
                parts = [
                    simulate_session(
                        p.sid, p.nq, p.es, sim_cfg, gate=gate, bracket_fn=brk,
                        vwap_from_ns=p.vwap_from_ns, events=by_sid[p.sid],
                    )
                    for p in pairs if p.sid in by_sid
                ]
                key = f"{k}_{gname}_{bname}"
                merged = H.merge(parts) if parts else None
                r = H.pack_pnl(merged, sids) if merged else None
                fade_pnl[key] = r
                if r:
                    md.append(H.pnl_row(f"fade {k} · {gname} · {bname}", r))
                    print(
                        f"  fade {k:18s} {gname:11s} {bname:12s} n={r['n']:4d} "
                        f"net={r['net']:+8.1f} train={r['train_net']:+7.1f} "
                        f"hold={r['hold_net']:+7.1f} new={r['new_net']:+7.1f}",
                        flush=True,
                    )
                if want_per_session and merged is not None:
                    day = []
                    for sid in sids:
                        sub = filter_summary(merged, lambda t, x=sid: t.session_id == x, H.COMMISSION)
                        day.append({
                            "session_id": sid,
                            "cohort": H.cohort(sid),
                            "n": sub.n,
                            "net": round(sub.net, 2),
                            "gross": round(sub.gross, 2),
                            "wr": None if sub.n == 0 else round(sub.wr, 4),
                        })
                    nets = np.array([d["net"] for d in day], dtype=float)
                    pos = nets[nets > 0]
                    neg = nets[nets < 0]
                    top3 = sorted(day, key=lambda d: d["net"], reverse=True)[:3]
                    bot3 = sorted(day, key=lambda d: d["net"])[:3]
                    total_pos = float(pos.sum()) if len(pos) else 0.0
                    conc = {
                        "days_pos": int((nets > 0).sum()),
                        "days_neg": int((nets < 0).sum()),
                        "days_flat": int((nets == 0).sum()),
                        "top3_net": round(sum(d["net"] for d in top3), 2),
                        "top3_share_of_pos": (
                            round(sum(d["net"] for d in top3) / total_pos, 3) if total_pos > 0 else None
                        ),
                        "worst3_net": round(sum(d["net"] for d in bot3), 2),
                        "top3": top3,
                        "worst3": bot3,
                    }
                    per_session_pnl[key] = {"days": day, "concentration": conc}
                    md += [
                        f"",
                        f"#### Per-session · {k} · {gname} · {bname}",
                        f"",
                        f"days+ {conc['days_pos']}/{len(sids)} · days− {conc['days_neg']} · "
                        f"top3 share of positive net = {conc['top3_share_of_pos']} "
                        f"(top3 {conc['top3_net']:+.1f}; worst3 {conc['worst3_net']:+.1f})",
                        "",
                        "| session | cohort | n | WR | net |",
                        "|---|---|---:|---:|---:|",
                    ]
                    for d in day:
                        wr = "–" if d["wr"] is None else f"{d['wr']:.2f}"
                        md.append(
                            f"| {d['session_id']} | {d['cohort']} | {d['n']} | {wr} | {d['net']:+.1f} |"
                        )
                    md.append("")
    md.append("")

    # wait-time distribution for first fade variant
    first_key = next(iter(fade_variants), None)
    if first_key and fade_raw.get(first_key):
        w = np.concatenate([t.column("wait_ms").to_numpy() for t in fade_raw[first_key]])
        reasons = pa.concat_tables(fade_raw[first_key]).column("exhaust_reason").to_pylist()
        rc = {r: reasons.count(r) for r in set(reasons)}
        md += [
            f"Fade `{first_key}`: n={len(w)}, wait ms p25/50/75 = "
            f"{np.percentile(w,25):.0f}/{np.percentile(w,50):.0f}/{np.percentile(w,75):.0f}; reasons {rc}",
            "",
        ]
        ev_report[f"fade_{first_key}_wait_ms_pct"] = [float(x) for x in np.percentile(w, [25, 50, 75])]
        ev_report[f"fade_{first_key}_reasons"] = rc

    report = {
        "status": "ok",
        "sessions": sids,
        "variants": {k: asdict(v) for k, v in variants.items()},
        "pnl": pnl_report,
        "events": ev_report,
        "fade_pnl": fade_pnl,
        "per_session_pnl": per_session_pnl,
        "skip_exit_overlay": skip_exits,
    }
    H.write_report(results_dir, report, md)
    # Prefer the best-documented dig fade for events.parquet, else first available
    pick = "against_m03" if "against_m03" in fade_raw else (first_key or "")
    all_fade = [t for t in fade_raw.get(pick, []) if t.num_rows]
    out = {"summary": report}
    if all_fade:
        out["events"] = pa.concat_tables(all_fade, promote_options="default")
    return out
