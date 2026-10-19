"""EXP-0035 — correlated absorption fade.

Per market, an absorption bar is heavy one-sided aggression that fails to move
price (thresholds are per-market rolling quantiles — see
``features/corr_absorb.py`` for why ES and NQ must not share fixed numbers).
Dual event = NQ and ES absorption bars with the same aggressor side within
``sync_ms``; trade = fade the aggressor on NQ.

Yardsticks (points, 60 s): null, corrected EXP-0008 confirm bursts, and —
to test the *correlated* claim directly — NQ-only absorption bars from the same
rule. Strata: depth level contact on the NQ bar (15 sessions with depth), ES
lead/lag sign. P&L via MAT sim events override, vol k=1 and fixed 10/10, with
and without the frozen trend+VWAP gate.
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
from research.features.corr_absorb import (
    AbsorbParams,
    absorption_bars,
    level_contact_flags,
)


def _sub(t: pa.Table, mask: np.ndarray) -> pa.Table:
    if t.num_rows == 0:
        return t
    idx = np.where(mask)[0]
    return t.take(idx) if len(idx) else t.slice(0, 0)


def _strata_rows(label_parts: list[tuple[str, pa.Table]], col: str, values: dict[str, object]) -> dict:
    """Cohort summaries of labelled tables split on ``col`` == value."""
    out = {}
    for name, val in values.items():
        parts = []
        for sid, t in label_parts:
            if t.num_rows == 0 or col not in t.column_names:
                continue
            v = t.column(col).to_numpy()
            parts.append((sid, _sub(t, v == val)))
        out[name] = parts
    return out


def run(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    parquet_dir = Path(cfg["parquet_dir"])
    results_root = Path(cfg["results_dir"])
    results_dir = results_root / exp["name"]
    horizons = list(exp.get("horizons_ms") or cfg["outcome_horizons_ms"])
    targets = list(exp.get("point_targets") or cfg["nq_point_targets"])
    exclude = set(str(s) for s in exp.get("exclude_sessions") or [])
    use_depth = bool(exp.get("depth_stratum", True))
    radius = int(exp.get("level_radius_ticks", 8))
    gate_modes = list(exp.get("pnl_gates") or ["none", "trend_vwap"])

    pairs: list[H.Pair] = []
    for sid in session_ids:
        if sid in exclude:
            continue
        p = H.load_pair(parquet_dir, sid)
        if p is not None:
            pairs.append(p)
    sids = [p.sid for p in pairs]
    print(f"[0035] {len(pairs)} sessions: {sids}", flush=True)

    default_p = AbsorbParams(**(exp.get("absorb") or {}))
    variants: dict[str, AbsorbParams] = {"default": default_p}
    for name, over in (exp.get("variants") or {}).items():
        variants[name] = AbsorbParams(**{**asdict(default_p), **over})

    # Depth contacts (cached) — only sessions with depth
    contacts: dict[str, pa.Table | None] = {}
    if use_depth:
        for p in pairs:
            print(f"[0035] level contacts {p.sid} …", flush=True)
            contacts[p.sid] = H.level_contacts(parquet_dir, p, results_root)
        n_depth = sum(1 for v in contacts.values() if v is not None)
        print(f"[0035] depth available for {n_depth}/{len(pairs)} sessions", flush=True)

    # Comparators (independent of variant)
    null_parts: dict[str, list[tuple[str, pa.Table]]] = {}
    comp_parts: list[tuple[str, pa.Table]] = []
    bursts_by_sid: dict[str, pa.Table] = {}
    for p in pairs:
        b = H.confirm_bursts(p)
        bursts_by_sid[p.sid] = b
        comp_parts.append((p.sid, H.label_in_points(b, p, horizons, targets)))

    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        exp.get("description", "").strip(),
        "",
        "**Pre-registered bar:** dual-absorption fade hit+10 and @60s above **both** "
        "null and EXP-0008 confirm bursts on old AND new, n ≥ 50, **and** above the "
        "NQ-only absorption set from the same rule (otherwise ES adds nothing). "
        "P&L: positive train, hold and new.",
        "",
        f"Sessions={len(sids)} · UK 14:30–17:00 · 1-tick slip · {H.COMMISSION} pt/RT · "
        f"train={len([s for s in sids if H.cohort(s)=='train'])} "
        f"hold={len([s for s in sids if H.cohort(s)=='hold'])} "
        f"new={len([s for s in sids if H.cohort(s)=='new'])}"
        + (f" · depth on {sum(1 for v in contacts.values() if v is not None)} sessions" if use_depth else ""),
        "",
        "## Absorption rule parameters (per-market adaptive)",
        "",
        "| variant | bar ms | vol q | ntr q | rel vol≥ | floors NQ/ES | imb≥ | flat NQ/ES ticks | range cap NQ/ES | sync ms |",
        "|---|---:|---:|---:|---:|---|---:|---|---|---:|",
    ]
    for name, p in variants.items():
        md.append(
            f"| {name} | {p.bar_ms} | {p.vol_q} | {p.ntr_q or 'off'} | {p.min_rel_vol or 'off'} | "
            f"{p.nq_abs_min_vol:.0f}/{p.es_abs_min_vol:.0f} | {p.min_imbalance} | "
            f"≤{p.nq_flat_ticks:g}/≤{p.es_flat_ticks:g} | "
            f"{p.nq_max_range_ticks or 'off'}/{p.es_max_range_ticks or 'off'} | {p.sync_ms} |"
        )
    md.append("")

    report: dict = {"status": "ok", "sessions": sids, "variants": {k: asdict(v) for k, v in variants.items()}}
    ev_report: dict = {}
    pnl_report: dict = {}
    counts: dict = {}
    dual_raw: dict[str, list[pa.Table]] = {}

    for vname, p_ab in variants.items():
        print(f"[0035] variant {vname}", flush=True)
        nq_only_parts: list[tuple[str, pa.Table]] = []
        es_only_n = 0
        dual_parts: list[tuple[str, pa.Table]] = []
        dual_raw[vname] = []
        nulls: list[tuple[str, pa.Table]] = []
        per_sid = {}
        for p in pairs:
            nq_b, es_b = p.bars(p_ab.bar_ms)
            nq_abs = absorption_bars(nq_b, p_ab, market="NQ")
            es_abs = absorption_bars(es_b, p_ab, market="ES") if es_b.num_rows else pa.table({})
            es_only_n += es_abs.num_rows
            dual = H.cross_match(nq_abs, es_abs, sync_ms=p_ab.sync_ms, match_col="aggr_side", stamp="confirm")
            if dual.num_rows:
                flags = level_contact_flags(
                    dual, contacts.get(p.sid) if use_depth else None,
                    bar_ms=p_ab.bar_ms, radius_ticks=radius,
                )
                dual = dual.append_column("level_contact", pa.array(flags, pa.int8()))
                dual = dual.append_column(
                    "es_first", pa.array((dual.column("es_signed_lag_ms").to_numpy() < 0).astype(np.int8), pa.int8())
                )
                if "event_type" in dual.column_names:
                    dual = dual.drop_columns(["event_type"])
                dual = dual.append_column("event_type", pa.array(["corr_absorb_dual"] * dual.num_rows))
                dual = H._with_session(dual, p.sid)
                dual_raw[vname].append(dual)
            per_sid[p.sid] = {"nq_abs": nq_abs.num_rows, "es_abs": es_abs.num_rows, "dual": dual.num_rows}
            nq_only_parts.append((p.sid, H.label_in_points(nq_abs, p, horizons, targets)))
            dual_parts.append((p.sid, H.label_in_points(dual, p, horizons, targets)))
            nulls.append((p.sid, H.label_in_points(H.null_events(p, max(dual.num_rows, 1)), p, horizons, targets)))

        n_nq = sum(v["nq_abs"] for v in per_sid.values())
        n_dual = sum(v["dual"] for v in per_sid.values())
        counts[vname] = {"nq_abs": n_nq, "es_abs": es_only_n, "dual": n_dual, "per_session": per_sid}
        print(f"  nq_abs={n_nq} es_abs={es_only_n} dual={n_dual}", flush=True)

        ev = {
            "null": H.summarize_by_cohort(nulls, horizons, targets),
            "exp0008_confirm": H.summarize_by_cohort(comp_parts, horizons, targets),
            "nq_only_absorb": H.summarize_by_cohort(nq_only_parts, horizons, targets),
            "dual_absorb": H.summarize_by_cohort(dual_parts, horizons, targets),
        }
        strata = {}
        if use_depth:
            strata.update(_strata_rows(dual_parts, "level_contact", {"dual_level_contact": 1, "dual_no_level": 0}))
        strata.update(_strata_rows(dual_parts, "es_first", {"dual_es_first": 1, "dual_nq_first": 0}))
        strata.update(_strata_rows(dual_parts, "side", {"dual_long_fade": 1, "dual_short_fade": -1}))
        for k, parts in strata.items():
            ev[k] = H.summarize_by_cohort(parts, horizons, targets)
        ev_report[vname] = ev

        md += [f"## {vname} — event study (NQ path, side = fade aggressor)", "",
               f"counts: NQ absorb bars {n_nq} · ES absorb bars {es_only_n} · dual {n_dual}", ""]
        for coh in ("all", "old", "new"):
            md += [f"**{coh}**", "", *H.EVENT_HDR]
            for k in ("null", "exp0008_confirm", "nq_only_absorb", "dual_absorb", *strata.keys()):
                md.append(H.event_row(k, ev[k][coh]))
            md.append("")

        # P&L
        by_sid = {t.column("session_id")[0].as_py(): t for t in dual_raw[vname] if t.num_rows}
        gate_fns = {
            "none": None,
            "trend_vwap": combine_gates(make_trend_mode("with"), make_vwap_side(True)),
        }
        rows = {}
        md += [f"### {vname} — fade P&L (events override; flat-only/cooldown apply)", "", *H.PNL_HDR]
        for gname in gate_modes:
            if gname not in gate_fns:
                continue
            for bname, brk in (("vol_k1", make_vol_bracket(1.0)), ("fixed_10_10", None)):
                sim_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=H.COMMISSION, tp_pts=10, sl_pts=10)
                parts = [
                    simulate_session(
                        p.sid, p.nq, p.es, sim_cfg, gate=gate_fns[gname], bracket_fn=brk,
                        vwap_from_ns=p.vwap_from_ns, events=by_sid[p.sid],
                    )
                    for p in pairs if p.sid in by_sid
                ]
                r = H.pack_pnl(H.merge(parts), sids) if parts else None
                rows[f"{gname}_{bname}"] = r
                if r:
                    md.append(H.pnl_row(f"{gname} · {bname}", r))
                    print(
                        f"  pnl {gname:11s} {bname:12s} n={r['n']:4d} net={r['net']:+8.1f} "
                        f"train={r['train_net']:+7.1f} hold={r['hold_net']:+7.1f} new={r['new_net']:+7.1f}",
                        flush=True,
                    )
        md.append("")
        pnl_report[vname] = rows

    report.update({"counts": counts, "events": ev_report, "pnl": pnl_report})
    H.write_report(results_dir, report, md)
    out = {"summary": report}
    first = next(iter(variants))
    if dual_raw.get(first):
        out["events"] = pa.concat_tables(dual_raw[first], promote_options="default")
    return out
