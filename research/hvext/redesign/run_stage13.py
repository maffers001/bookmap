"""Stage 1 (controls: is the HV tip doing anything?) and Stage 3 (MFE-driven
filter search on retest fills), with absorbed volume folded in as a gate and
as a feature.

Writes results/EXP-0038/stage1_controls.json, stage3_mfe_filters.json and
appends to summary.md.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.hvext.redesign import run_research_fixes as F
from research.hvext.redesign import runner as R
from research.hvext.redesign.enrich import filter_events

HOLDOUT = F.HOLDOUT
RP = F.FREEZE_RP
N_SEEDS = 20
ABS_P90 = 38.0  # pooled p90 of absorbed volume on HV tips (research_fixes_round2)
MONTHS = (("sep", R.SEP), ("oct", R.OCT), ("holdout", HOLDOUT))

CFG_HV = R.PANEL
CFG_ALL = dataclasses.replace(R.PANEL, high_vol_mult=0.0)  # every 20-pt impulse


# ---------------------------------------------------------------- helpers ----

def _col(ev: pa.Table, name: str) -> np.ndarray:
    return ev.column(name).to_numpy(zero_copy_only=False).astype(np.float64)


def gate_abs(thr: float, strict_gt: bool = False):
    def f(ev):
        v = _col(ev, "mbo_tip_filled")
        ok = np.isfinite(v)
        return ok & ((v > thr) if strict_gt else (v >= thr))
    return f


def gate_vol_ratio_lt(x: float):
    return lambda ev: _col(ev, "vol_ratio") < x


def augment_es(sid: str, ev: pa.Table) -> pa.Table:
    """Attach es_leg_move_dir: ES points moved from arm (fire - leg_sec) to
    fire, signed in the trade direction (positive = ES confirmed the leg)."""
    n = ev.num_rows
    out = np.full(n, np.nan)
    es = R.load_es(sid)
    if es.num_rows and n:
        ts = es.column("ts_ns").to_numpy().astype(np.int64)
        px = es.column("price").to_numpy().astype(np.float64) * 0.25
        o = np.argsort(ts, kind="stable")
        ts, px = ts[o], px[o]
        fire = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        leg = np.maximum(_col(ev, "leg_sec"), 0.0)
        arm = fire - (leg * 1e9).astype(np.int64)
        side = ev.column("side").to_numpy().astype(np.float64)
        i_f = np.clip(np.searchsorted(ts, fire, side="right") - 1, 0, len(ts) - 1)
        i_a = np.clip(np.searchsorted(ts, arm, side="right") - 1, 0, len(ts) - 1)
        out = side * (px[i_f] - px[i_a])
    return ev.append_column("es_leg_move_dir", pa.array(out, type=pa.float64()))


def _fmt(pk: dict) -> str:
    return (
        f"n={pk.get('n', 0):3d} net={pk.get('net') if pk.get('net') is not None else 0:>8.1f} "
        f"wr={(pk.get('wr') or 0):.3f} fill={(pk.get('fill_rate') or 0):.2f}"
    )


def _fmt_null(nul: dict) -> str:
    z = nul.get("z")
    return (
        f"null n~{nul['n_mean']:.0f} net {nul['net_mean']:+.1f}±{nul['net_sd']:.0f} "
        f"wr~{(nul.get('wr_mean') or 0):.3f} fill~{(nul.get('fill_rate_mean') or 0):.2f} | "
        f"cell-null {nul['cell_minus_null_mean']:+.1f} z={z if z is None else round(z, 2)} "
        f"P(null>=cell)={nul['frac_null_ge_cell']:.2f}"
    )


def run_cell(label: str, cfg, *, post_filter=None, fill_filter=None, with_mbo=False,
             augment=None, months=MONTHS, events_out: dict | None = None) -> dict:
    print(f"\n--- {label} ---", flush=True)
    out = {"label": label}
    trades_by = {}
    for mname, sess in months:
        evs = [] if events_out is not None else None
        pk, tr = R.run_month(
            sess, cfg, entry="retest", retest=RP, post_filter=post_filter,
            fill_filter=fill_filter, with_mbo=with_mbo, augment=augment,
            events_out=evs,
        )
        nul = R.null_retest_paired(
            sess, pk, RP, cfg, n_seeds=N_SEEDS, fill_filter=fill_filter
        )
        nul_small = {k: v for k, v in nul.items() if k != "runs"}
        print(f"  {mname:8s} {_fmt(pk)} fires={pk.get('n_fires_gated')} | {_fmt_null(nul)}")
        out[mname] = {"cell": pk, "null": nul_small}
        trades_by[mname] = tr
        if events_out is not None:
            events_out[mname] = (evs, tr)
    both = R.pack(trades_by["sep"] + trades_by["oct"])
    out["both"] = both
    out["both_net"] = both.get("net")
    out["both_null_mean"] = out["sep"]["null"]["net_mean"] + out["oct"]["null"]["net_mean"]
    out["beats_null_both_months"] = bool(
        out["sep"]["null"]["frac_null_ge_cell"] <= 0.1
        and out["oct"]["null"]["frac_null_ge_cell"] <= 0.1
    )
    print(f"  both     n={both['n']} net={both.get('net')} wr={both.get('wr')} | "
          f"null mean {out['both_null_mean']:+.1f} | beats null (P<=0.1 both months): "
          f"{out['beats_null_both_months']}")
    return out


# ---------------------------------------------------------------- stage 1 ----

def stage1(store: dict) -> dict:
    print("=== Stage 1: HV fires vs all 20-pt impulses vs random, identical retest fill ===")
    cells = []
    cells.append(run_cell("1a_hv_fires", CFG_HV, events_out=store.setdefault("a", {})))
    cells.append(run_cell("1b_all_impulses", CFG_ALL, events_out=store.setdefault("b", {})))
    cells.append(run_cell("1b_not_hv(vol_ratio<2)", CFG_ALL, post_filter=gate_vol_ratio_lt(2.0)))
    # absorbed-volume gates (MBO) on both populations
    cells.append(run_cell("1d_hv_absorbed>0", CFG_HV, post_filter=gate_abs(0, True), with_mbo=True,
                          events_out=store.setdefault("a_mbo", {})))
    cells.append(run_cell(f"1d_hv_absorbed>={ABS_P90:.0f}", CFG_HV, post_filter=gate_abs(ABS_P90), with_mbo=True))
    cells.append(run_cell("1d_all_absorbed>0", CFG_ALL, post_filter=gate_abs(0, True), with_mbo=True,
                          events_out=store.setdefault("b_mbo", {})))
    cells.append(run_cell(f"1d_all_absorbed>={ABS_P90:.0f}", CFG_ALL, post_filter=gate_abs(ABS_P90), with_mbo=True))

    a, b = cells[0], cells[1]
    # Decision: (a) == (b) ⇒ HV gate is decoration. Compare per-trade expectancy, not net.
    def exp_(c, m):
        pk = c[m]["cell"]
        return (pk.get("net") or 0.0) / max(1, pk.get("n") or 0)
    decision = {
        "exp_a_sep": exp_(a, "sep"), "exp_b_sep": exp_(b, "sep"),
        "exp_a_oct": exp_(a, "oct"), "exp_b_oct": exp_(b, "oct"),
        "exp_a_hold": exp_(a, "holdout"), "exp_b_hold": exp_(b, "holdout"),
        "a_beats_null_both": a["beats_null_both_months"],
        "b_beats_null_both": b["beats_null_both_months"],
    }
    a_gt_b_both = decision["exp_a_sep"] > decision["exp_b_sep"] and decision["exp_a_oct"] > decision["exp_b_oct"]
    if a_gt_b_both and a["beats_null_both_months"]:
        decision["verdict"] = "tip_carries_information"
    elif not a_gt_b_both:
        decision["verdict"] = "hv_gate_is_decoration"
    else:
        decision["verdict"] = "a>b_but_not_above_null"
    print("\nStage 1 decision:", json.dumps(decision, indent=1))
    payload = {"stage": "stage1_controls", "retest_params": RP.__dict__, "cells": cells,
               "decision": decision}
    dest = Path("results/EXP-0038/stage1_controls.json")
    dest.write_text(json.dumps(payload, indent=2, default=str))
    print(f"Wrote {dest}")
    return payload


# ---------------------------------------------------------------- stage 3 ----

FIRE_FEATURES = [
    "leg_rate_ratio", "leg_sec", "path_eff", "tip_max_order",
    "leg_max_order_same_side", "med_r30", "vol_ratio", "leg_vol_ratio",
    "tip_against_frac", "tod_min", "es_leg_move_dir",
    "mbo_tip_max_resting", "mbo_tip_filled",
]
FILL_FEATURES = ["fill_delay_sec", "retest_vol_frac"]
HORIZON_NS = int(30 * 60 * 1e9)


def _derive(ev: pa.Table) -> dict[str, np.ndarray]:
    n = ev.num_rows
    d: dict[str, np.ndarray] = {}
    for c in FIRE_FEATURES + FILL_FEATURES:
        d[c] = _col(ev, c) if c in ev.column_names else np.full(n, np.nan)
    side = ev.column("side").to_numpy().astype(np.float64)
    if "tip_buy_frac" in ev.column_names:
        bf = _col(ev, "tip_buy_frac")
        d["tip_against_frac"] = np.where(side < 0, bf, 1.0 - bf)
    ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
    d["tod_min"] = np.array([h * 60 + m for h, m in (R.uk_hour_min(int(t)) for t in ts)], dtype=float)
    if "retest_vol" in ev.column_names and "extreme_vol" in ev.column_names:
        tv = _col(ev, "extreme_vol")
        d["retest_vol_frac"] = np.where(tv > 0, _col(ev, "retest_vol") / np.where(tv > 0, tv, 1), np.nan)
    return d


def _mfe_pre_stop(nq_cache: dict, sid: str, tr) -> tuple[float, float]:
    """(MFE before the stop distance is hit, within 30 min; MAE over same)."""
    if sid not in nq_cache:
        nq = R.load_nq(sid)
        ts = nq.column("ts_ns").to_numpy().astype(np.int64)
        px = nq.column("price").to_numpy().astype(np.float64) * 0.25
        o = np.argsort(ts, kind="stable")
        nq_cache[sid] = (ts[o], px[o])
    ts, px = nq_cache[sid]
    i0 = int(np.searchsorted(ts, tr.entry_ts_ns, side="left"))
    i1 = int(np.searchsorted(ts, tr.entry_ts_ns + HORIZON_NS, side="right"))
    if i0 >= len(ts) or i1 <= i0:
        return float("nan"), float("nan")
    path = (px[i0:i1] - float(tr.entry_px)) * (1.0 if tr.side > 0 else -1.0)
    adverse = np.maximum.accumulate(np.maximum(-path, 0.0))
    stopped = np.nonzero(adverse >= tr.sl_pts)[0]
    end = int(stopped[0]) + 1 if len(stopped) else len(path)
    return float(max(0.0, path[:end].max())), float(adverse[end - 1])


def build_fill_table(months: dict) -> dict[str, dict[str, np.ndarray]]:
    """months: {mname: ([(sid, ev)], trades)} → {mname: feature arrays + mfe}."""
    out = {}
    nq_cache: dict = {}
    for mname, (evs, trades) in months.items():
        feats_by_key: dict[tuple, dict] = {}
        for sid, ev in evs:
            d = _derive(ev)
            ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
            sd = ev.column("side").to_numpy().astype(int)
            for i in range(ev.num_rows):
                feats_by_key[(sid, int(ts[i]), int(sd[i]))] = {k: float(v[i]) for k, v in d.items()}
        rows = []
        for tr in trades:
            f = feats_by_key.get((tr.session_id, int(tr.entry_ts_ns), int(tr.side)))
            if f is None:
                continue
            mfe, mae = _mfe_pre_stop(nq_cache, tr.session_id, tr)
            f = dict(f)
            f["mfe"] = mfe
            f["mae"] = mae
            f["pnl"] = tr.pnl - 0.25
            f["side"] = tr.side
            rows.append(f)
        if not rows:
            out[mname] = {}
            continue
        out[mname] = {k: np.array([r[k] for r in rows], dtype=float) for k in rows[0]}
    return out


def feature_scan(tbl: dict, label: str) -> list[dict]:
    """Per feature: pooled-median split gap in mean MFE (hi - lo) per month,
    tercile monotonicity, runner/dud rates. Rank by min signed gap across
    Sep/Oct (same sign required)."""
    sep, oct_ = tbl.get("sep", {}), tbl.get("oct", {})
    if not sep or not oct_:
        return []
    feats = FIRE_FEATURES + FILL_FEATURES
    res = []
    for fname in feats:
        xs = np.concatenate([sep[fname], oct_[fname]])
        ok = np.isfinite(xs)
        if ok.sum() < 30:
            continue
        q = np.nanpercentile(xs[ok], [33.3, 50, 66.7])
        row = {"feature": fname, "p33": float(q[0]), "p50": float(q[1]), "p67": float(q[2]),
               "n_valid": int(ok.sum())}
        gaps = []
        mono_ok = True
        for mname, m in (("sep", sep), ("oct", oct_)):
            x, y = m[fname], m["mfe"]
            v = np.isfinite(x) & np.isfinite(y)
            if v.sum() < 20:
                gaps.append(np.nan)
                continue
            x, y = x[v], y[v]
            hi, lo = y[x > q[1]], y[x <= q[1]]
            gap = float(hi.mean() - lo.mean()) if len(hi) and len(lo) else np.nan
            t1, t2, t3 = y[x <= q[0]], y[(x > q[0]) & (x <= q[2])], y[x > q[2]]
            means = [float(t.mean()) if len(t) else np.nan for t in (t1, t2, t3)]
            mono = (means[0] <= means[1] <= means[2]) or (means[0] >= means[1] >= means[2])
            mono_ok &= bool(mono)
            runner_hi = float(np.mean(hi >= 30)) if len(hi) else np.nan
            runner_lo = float(np.mean(lo >= 30)) if len(lo) else np.nan
            row[mname] = {"gap_hi_minus_lo": gap, "tercile_means": means, "monotone": bool(mono),
                          "runner_frac_hi": runner_hi, "runner_frac_lo": runner_lo,
                          "n_hi": int(len(hi)), "n_lo": int(len(lo))}
            gaps.append(gap)
        if any(not np.isfinite(g) for g in gaps):
            continue
        same_sign = np.sign(gaps[0]) == np.sign(gaps[1]) and gaps[0] != 0
        row["same_sign"] = bool(same_sign)
        row["monotone_both"] = bool(mono_ok)
        row["min_abs_gap"] = float(min(abs(gaps[0]), abs(gaps[1]))) if same_sign else 0.0
        row["direction"] = "hi" if same_sign and gaps[0] > 0 else ("lo" if same_sign else None)
        row["passes_bar"] = bool(same_sign and mono_ok and row["min_abs_gap"] >= 15.0)
        # holdout check if available
        hold = tbl.get("holdout", {})
        if hold and fname in hold:
            x, y = hold[fname], hold["mfe"]
            v = np.isfinite(x) & np.isfinite(y)
            if v.sum() >= 10:
                hi, lo = y[v][x[v] > q[1]], y[v][x[v] <= q[1]]
                row["holdout"] = {"gap_hi_minus_lo": float(hi.mean() - lo.mean()) if len(hi) and len(lo) else None,
                                  "n_hi": int(len(hi)), "n_lo": int(len(lo))}
        res.append(row)
    res.sort(key=lambda r: (r["passes_bar"], r["min_abs_gap"]), reverse=True)
    print(f"\n  Feature scan [{label}] (gap = mean MFE_pre_stop hi - lo at pooled median):")
    for r in res:
        print(f"    {r['feature']:24s} sep {r['sep']['gap_hi_minus_lo']:+6.1f} oct {r['oct']['gap_hi_minus_lo']:+6.1f} "
              f"mono={str(r['monotone_both']):5s} dir={r['direction']} min|gap|={r['min_abs_gap']:.1f} "
              f"hold={(r.get('holdout') or {}).get('gap_hi_minus_lo')} bar={r['passes_bar']}")
    return res


def gate_tests(ranked: list[dict], cfg, *, with_mbo: bool, augment, label: str) -> list[dict]:
    """Top-2 features as gates at p33/p50/p67 in the favourable direction."""
    cells = []
    for r in ranked[:2]:
        fname, dirn = r["feature"], r["direction"]
        if dirn is None:
            continue
        is_fill = fname in FILL_FEATURES
        for thr_name in ("p33", "p50", "p67"):
            thr = r[thr_name]

            def gate(ev, fname=fname, dirn=dirn, thr=thr):
                d = _derive(ev)
                v = d[fname]
                ok = np.isfinite(v)
                return ok & ((v > thr) if dirn == "hi" else (v <= thr))

            op = ">" if dirn == "hi" else "<="
            cl = f"3g_{label}_{fname}{op}{thr_name}({thr:.2f})"
            kw = {"fill_filter": gate} if is_fill else {"post_filter": gate}
            need_mbo = with_mbo or fname.startswith("mbo_")
            cells.append(run_cell(cl, cfg, with_mbo=need_mbo, augment=augment, **kw))
    return cells


def stage3(store: dict) -> dict:
    print("\n=== Stage 3: MFE-driven filter search on retest fills ===")
    # Rebuild (a) and (b) fills with ES + MBO features attached (one pass each)
    pops = {}
    for key, cfg, lbl in (("a", CFG_HV, "hv"), ("b", CFG_ALL, "all")):
        evs_store: dict = {}
        print(f"\n  collecting fills with ES+MBO features: {lbl}", flush=True)
        for mname, sess in MONTHS:
            evs = []
            pk, tr = R.run_month(sess, cfg, entry="retest", retest=RP, with_mbo=True,
                                 augment=augment_es, events_out=evs)
            evs_store[mname] = (evs, tr)
            print(f"    {mname:8s} {_fmt(pk)}")
        tbl = build_fill_table(evs_store)
        for mname in tbl:
            if tbl[mname]:
                mfe = tbl[mname]["mfe"]
                print(f"    {mname:8s} fills={len(mfe)} MFE_pre_stop med={np.nanmedian(mfe):.1f} "
                      f"runner(>=30)={np.nanmean(mfe >= 30):.2f} dud(<=5)={np.nanmean(mfe <= 5):.2f}")
        ranked = feature_scan(tbl, lbl)
        pops[lbl] = {"cfg": cfg, "table_summary": {
            m: {"n": int(len(t["mfe"])), "mfe_median": float(np.nanmedian(t["mfe"])),
                "runner_frac": float(np.nanmean(t["mfe"] >= 30)), "dud_frac": float(np.nanmean(t["mfe"] <= 5))}
            for m, t in tbl.items() if t}, "ranked": ranked}

    # Gate tests: top-2 features for each population (null-paired)
    gates = {}
    for lbl, cfg in (("hv", CFG_HV), ("all", CFG_ALL)):
        ranked = pops[lbl]["ranked"]
        print(f"\n  gate tests [{lbl}] on top-2: {[r['feature'] for r in ranked[:2]]}")
        gates[lbl] = gate_tests(ranked, cfg, with_mbo=False, augment=augment_es, label=lbl)

    payload = {"stage": "stage3_mfe_filters", "retest_params": RP.__dict__,
               "populations": {k: {kk: vv for kk, vv in v.items() if kk != "cfg"} for k, v in pops.items()},
               "gates": gates}
    dest = Path("results/EXP-0038/stage3_mfe_filters.json")
    dest.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nWrote {dest}")
    return payload


# ------------------------------------------------------------------- main ----

def _cell_line(c: dict) -> str:
    def m(k):
        pk, nl = c[k]["cell"], c[k]["null"]
        return (f"{k} n={pk.get('n', 0)} net={pk.get('net')} wr={pk.get('wr')} "
                f"(null {nl['net_mean']:+.0f}±{nl['net_sd']:.0f}, P={nl['frac_null_ge_cell']:.2f})")
    return f"  - `{c['label']}`: {m('sep')}; {m('oct')}; {m('holdout')}; both={c.get('both_net')}"


def main() -> int:
    R.clear_cache()
    store: dict = {}
    s1 = stage1(store)
    s3 = stage3(store)

    lines = ["## Stage 1 - controls (HV fires vs all 20-pt impulses vs random; retest R10/300s/sb2/x3, per-event risk)\n"]
    for c in s1["cells"]:
        lines.append(_cell_line(c))
    d = s1["decision"]
    lines.append(f"- Expectancy/trade Sep: HV {d['exp_a_sep']:+.2f} vs all {d['exp_b_sep']:+.2f}; "
                 f"Oct: HV {d['exp_a_oct']:+.2f} vs all {d['exp_b_oct']:+.2f}; "
                 f"holdout: HV {d['exp_a_hold']:+.2f} vs all {d['exp_b_hold']:+.2f}. "
                 f"HV beats null both months: {d['a_beats_null_both']}; all-impulses: {d['b_beats_null_both']}. "
                 f"**Verdict: {d['verdict']}**.")
    lines.append("- Wrote `results/EXP-0038/stage1_controls.json`.\n")
    lines.append("## Stage 3 - MFE-driven filter search (MFE_pre_stop over 30 min; runner>=30, dud<=5)\n")
    for lbl, p in s3["populations"].items():
        ts = p["table_summary"]
        lines.append(f"- [{lbl}] fills: " + "; ".join(
            f"{m} n={v['n']} MFEmed={v['mfe_median']:.1f} runner={v['runner_frac']:.2f} dud={v['dud_frac']:.2f}"
            for m, v in ts.items()))
        passing = [r for r in p["ranked"] if r["passes_bar"]]
        lines.append(f"  - features passing bar (same sign, monotone, min|gap|>=15 both months): "
                     f"{[r['feature'] for r in passing] or 'none'}")
        for r in p["ranked"][:5]:
            lines.append(f"  - {r['feature']}: gap Sep {r['sep']['gap_hi_minus_lo']:+.1f} / Oct {r['oct']['gap_hi_minus_lo']:+.1f} "
                         f"/ holdout {(r.get('holdout') or {}).get('gap_hi_minus_lo')} dir={r['direction']} mono={r['monotone_both']}")
    for lbl, cells in s3["gates"].items():
        lines.append(f"- Gate tests [{lbl}]:")
        for c in cells:
            lines.append(_cell_line(c))
    lines.append("- Wrote `results/EXP-0038/stage3_mfe_filters.json`.\n")
    R.append_summary("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
