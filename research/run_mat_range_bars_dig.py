"""EXP-0056 dig — percentile / contrarian range-bar combinations.

Exploratory only (discovery Apr–Aug). Current EXP-0056 arms use fixed ratios
(vol_ratio >= 1.5; aligned delta_frac >= 0.20/0.30), NOT percentiles.

This dig:
  1. adds/screens causal rolling percentile masks + Stage-1-inspired combos
     (mid-volume, contrary delta, bar-against, inside-above-VPOC, side splits)
  2. re-simulates the best masks across MAT gate combos x R:R combos

Writes results/EXP-0056_DIG/. Holdout is never touched.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import MatSimConfig, detect_signals, simulate_session
from research.config import load_config
from research.correlated.harness import _prefer_single_feed
from research.features.mat_fail_fade import filter_gated_mat
from research.features.range_bar_profile import (
    RangeBarProfileConfig,
    attach_mat_range_context,
)
from research.run_experiment import _load_trades
from research.run_mat_range_bars import (
    COMMISSION,
    DISCOVERY_MMYY,
    bounds,
    build_mat_gate,
    discovery_pass,
    filt,
    list_sessions,
    make_bracket,
    sid_month,
    summary_dict,
)

GATE_CONFIGS = [
    ("gated", True, True),
    ("nogates", False, False),
    ("trend_only", True, False),
    ("vwap_only", False, True),
]
BRACKETS = [
    ("rr1_1", 1.0, 1.0),
    ("rr1_2", 2.0, 1.0),  # tp_r=2, sl_r=1
]
FORCED = [
    "bar_against",
    "vol_mid_delta_contra",
    "inside_above_vpoc",
    "long_above_vah",
    "vol_p90",
    "delta_contra_p20",
]


def candidate_masks(ev: pa.Table) -> dict[str, np.ndarray]:
    n = ev.num_rows
    if n == 0:
        return {}
    ok = ev.column("prior_ok").to_numpy().astype(bool)
    side = ev.column("side").to_numpy().astype(int)
    vr = ev.column("prior_vol_ratio").to_numpy().astype(np.float64)
    vp = ev.column("prior_vol_percentile").to_numpy().astype(np.float64)
    adf = ev.column("prior_aligned_delta_frac").to_numpy().astype(np.float64)
    dp = ev.column("prior_delta_percentile").to_numpy().astype(np.float64)
    pos = ev.column("prior_pos").to_pylist()
    direction = ev.column("prior_direction").to_numpy().astype(int)

    above_vah = np.array([p == "above_vah" for p in pos], dtype=bool)
    below_val = np.array([p == "below_val" for p in pos], dtype=bool)
    inside = np.array([str(p).startswith("inside_va") for p in pos], dtype=bool)
    inside_above = np.array([p == "inside_va_above_vpoc" for p in pos], dtype=bool)
    inside_below = np.array([p == "inside_va_below_vpoc" for p in pos], dtype=bool)
    bar_against = ok & (direction == -side) & (direction != 0)
    vol_mid = ok & np.isfinite(vr) & (vr >= 0.8) & (vr <= 1.4)
    delta_contra = ok & np.isfinite(adf) & (adf < 0)

    # Two-bar trapped-traders: bar before the prior (mid/low) bar is high
    # volume; prior bar pushed away from that bar's VPOC.
    if "prior2_ok" in ev.column_names:
        ok2 = ev.column("prior2_ok").to_numpy().astype(bool)
        vr2 = ev.column("prior2_vol_ratio").to_numpy().astype(np.float64)
        poc2 = ev.column("prior2_vpoc").to_numpy().astype(np.float64)
        p_high = ev.column("prior_high").to_numpy().astype(np.float64)
        p_low = ev.column("prior_low").to_numpy().astype(np.float64)
        hv_prev = ok2 & np.isfinite(vr2) & (vr2 >= 1.5)
        midlow = ok & np.isfinite(vr) & (vr <= 1.4)
        geo_ok = np.isfinite(poc2) & np.isfinite(p_low) & np.isfinite(p_high)
        # As specified: long -> prior low above HV VPOC; short -> prior low
        # below HV VPOC.
        trap_literal = geo_ok & (
            ((side > 0) & (p_low > poc2)) | ((side < 0) & (p_low < poc2))
        )
        # Symmetric diagnostic: short requires the whole prior bar below VPOC.
        trap_strict = geo_ok & (
            ((side > 0) & (p_low > poc2)) | ((side < 0) & (p_high < poc2))
        )
        twobar_literal = hv_prev & midlow & trap_literal
        twobar_strict = hv_prev & midlow & trap_strict
    else:
        twobar_literal = np.zeros(n, dtype=bool)
        twobar_strict = np.zeros(n, dtype=bool)

    return {
        "base": np.ones(n, dtype=bool),
        "vol_fixed_1p5": ok & np.isfinite(vr) & (vr >= 1.5),
        "vol_p80": ok & np.isfinite(vp) & (vp >= 80),
        "vol_p90": ok & np.isfinite(vp) & (vp >= 90),
        "vol_p95": ok & np.isfinite(vp) & (vp >= 95),
        "vol_mid": vol_mid,
        "delta_aligned_fixed": ok & np.isfinite(adf) & (adf >= 0.20),
        "delta_aligned_p80": ok & np.isfinite(dp) & (dp >= 80),
        "delta_aligned_p90": ok & np.isfinite(dp) & (dp >= 90),
        "delta_contra": delta_contra,
        "delta_contra_strong": ok & np.isfinite(adf) & (adf <= -0.20),
        "delta_contra_p20": ok & np.isfinite(dp) & (dp <= 20),
        "bar_against": bar_against,
        "bar_against_delta_contra": bar_against & delta_contra,
        "vol_mid_delta_contra": vol_mid & delta_contra,
        "vol_mid_bar_against": vol_mid & bar_against,
        "twobar_trap_literal": twobar_literal,
        "twobar_trap_strict": twobar_strict,
        "twobar_trap_literal_bar_against": twobar_literal & bar_against,
        "twobar_trap_strict_bar_against": twobar_strict & bar_against,
        "vol_p90_delta_contra": ok & np.isfinite(vp) & (vp >= 90) & delta_contra,
        "vol_delta_orig": ok & np.isfinite(vr) & (vr >= 1.5) & np.isfinite(adf) & (adf >= 0.30),
        "va_break": ok & (((side > 0) & above_vah) | ((side < 0) & below_val)),
        "va_inside": ok & inside,
        "inside_above_vpoc": ok & inside_above,
        "inside_below_vpoc": ok & inside_below,
        "long_above_vah": ok & (side > 0) & above_vah,
        "short_below_val": ok & (side < 0) & below_val,
        "long_only": side > 0,
        "short_only": side < 0,
    }


def agg(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "avg": None, "net": 0.0, "wr": None, "months_pos": 0, "by_month": {}}
    pnls = np.array([r["pnl"] for r in rows], dtype=np.float64)
    by_m: dict[str, dict] = {}
    months = sorted({r["month"] for r in rows})
    for m in months:
        sub = [r for r in rows if r["month"] == m]
        by_m[m] = {
            "n": len(sub),
            "avg": round(float(np.mean([r["pnl"] for r in sub])), 3),
            "net": round(float(np.sum([r["pnl"] for r in sub])), 2),
        }
    return {
        "n": len(rows),
        "avg": round(float(pnls.mean()), 3),
        "net": round(float(pnls.sum()), 2),
        "wr": round(float(np.mean([r["win"] for r in rows])), 4),
        "months_pos": sum(1 for s in by_m.values() if s["net"] > 0),
        "by_month": by_m,
    }


def load_raw(cfg: dict) -> dict[str, dict]:
    pq_dir = Path(cfg["parquet_dir"])
    base_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")
    rb_cfg = RangeBarProfileConfig()
    data: dict[str, dict] = {}
    for sid in list_sessions(pq_dir):
        if sid_month(sid) not in DISCOVERY_MMYY:
            continue
        nq = filt(_prefer_single_feed(_load_trades(pq_dir, sid, "NQ"), pq_dir, sid, "NQ"))
        es0 = _prefer_single_feed(_load_trades(pq_dir, sid, "ES"), pq_dir, sid, "ES")
        es = filt(es0) if es0.num_rows else es0
        if nq.num_rows == 0:
            continue
        mid = int(nq.column("ts_ns").to_numpy()[nq.num_rows // 2])
        vf = bounds(mid)[0]
        raw = detect_signals(nq, es, base_cfg)
        ev, _bars, _prof = attach_mat_range_context(nq, raw, cfg=rb_cfg)
        data[sid] = {"nq": nq, "es": es, "vf": vf, "raw_events": ev, "month": sid_month(sid)}
        print(f"load {sid}: raw_mat={raw.num_rows}", flush=True)
    return data


def gated_events(data: dict[str, dict], trend: bool, vwap: bool) -> dict[str, pa.Table]:
    gate = build_mat_gate(trend=trend, vwap=vwap)
    out: dict[str, pa.Table] = {}
    for sid, sd in data.items():
        raw = sd["raw_events"]
        out[sid] = (
            filter_gated_mat(sd["nq"], raw, gate, vwap_from_ns=sd["vf"])
            if gate is not None
            else raw
        )
    return out


def simulate_mask(
    data: dict[str, dict],
    events_by_sid: dict[str, pa.Table],
    mask_name: str,
    *,
    tp_r: float,
    sl_r: float,
) -> dict:
    sim_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")
    br = make_bracket(tp_r=tp_r, sl_r=sl_r)
    parts = []
    parts_m: dict[str, list] = defaultdict(list)
    for sid, sd in data.items():
        ev = events_by_sid[sid]
        if ev.num_rows == 0:
            continue
        mask = candidate_masks(ev)[mask_name]
        if not mask.any():
            continue
        s = simulate_session(
            sid, sd["nq"], sd["es"], sim_cfg,
            gate=None, bracket_fn=br, vwap_from_ns=sd["vf"],
            events=ev.take(np.where(mask)[0]),
        )
        parts.append(s)
        parts_m[sd["month"]].append(s)
    out = summary_dict(parts)
    out["by_month"] = {m: summary_dict(ps) for m, ps in sorted(parts_m.items())}
    return out


def main() -> None:
    cfg = load_config()
    out = Path(cfg["results_dir"]) / "EXP-0056_DIG"
    out.mkdir(parents=True, exist_ok=True)
    data = load_raw(cfg)

    # ---- Screen on gated + symmetric base trades (descriptive, no resim) ----
    gated = gated_events(data, True, True)
    sim_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")
    br = make_bracket(tp_r=1.0, sl_r=1.0)
    rows_by_mask: dict[str, list[dict]] = defaultdict(list)
    mask_names: list[str] = []
    for sid, sd in data.items():
        ev = gated[sid]
        if ev.num_rows == 0:
            continue
        masks = candidate_masks(ev)
        if not mask_names:
            mask_names = list(masks.keys())
        s = simulate_session(
            sid, sd["nq"], sd["es"], sim_cfg,
            gate=None, bracket_fn=br, vwap_from_ns=sd["vf"], events=ev,
        )
        ev_ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        for tr in s.trades:
            j = int(np.searchsorted(ev_ts, tr.entry_ts_ns, side="right")) - 1
            if j < 0:
                continue
            for name, mask in masks.items():
                if mask[j]:
                    rows_by_mask[name].append({
                        "month": sd["month"], "pnl": tr.pnl - COMMISSION, "win": tr.win,
                    })
    screen = {name: agg(rows) for name, rows in rows_by_mask.items()}
    for name in sorted(screen, key=lambda k: (screen[k]["avg"] is not None, screen[k]["avg"] or -9), reverse=True):
        r = screen[name]
        print(f"screen {name}: n={r['n']} avg={r['avg']} net={r['net']} months+={r['months_pos']}", flush=True)

    # Select: top by (months>=3, avg) among n>=50, plus forced
    elig = [
        (name, r) for name, r in screen.items()
        if name != "base" and r["n"] >= 50
    ]
    elig.sort(key=lambda kv: (kv[1]["months_pos"] >= 3, kv[1]["avg"] or -9), reverse=True)
    selected = [name for name, _ in elig[:6]]
    for f in FORCED:
        if f in screen and screen[f]["n"] >= 30 and f not in selected:
            selected.append(f)
    selected = selected[:10]
    print(f"selected for resim: {selected}", flush=True)

    # ---- Resim selected across gate x bracket ----
    results: list[dict] = []
    events_cache: dict[str, dict[str, pa.Table]] = {}
    for gname, trend, vwap in GATE_CONFIGS:
        events_cache[gname] = gated_events(data, trend, vwap)
        for bname, tp_r, sl_r in BRACKETS:
            base = simulate_mask(data, events_cache[gname], "base", tp_r=tp_r, sl_r=sl_r)
            for arm in selected:
                r = simulate_mask(data, events_cache[gname], arm, tp_r=tp_r, sl_r=sl_r)
                ok, reason = (True, "base") if arm == "base" else discovery_pass(r, base.get("avg"))
                rec = {
                    "gate": gname, "bracket": bname, "arm": arm,
                    "n": r.get("n"), "avg": r.get("avg"), "net": r.get("net"),
                    "wr": r.get("wr"), "pf": r.get("pf"),
                    "days_pos": r.get("days_pos"), "days": r.get("days"),
                    "by_month": r.get("by_month"),
                    "pass": ok if arm != "base" else None,
                    "pass_reason": reason,
                    "base_avg": base.get("avg"),
                }
                results.append(rec)
                print(
                    f"resim {gname}/{bname}/{arm}: n={r.get('n')} avg={r.get('avg')} "
                    f"net={r.get('net')} pass={rec['pass']} ({reason})",
                    flush=True,
                )

    passing = [r for r in results if r["pass"]]
    passing.sort(key=lambda r: r["avg"] or -9, reverse=True)
    results_sorted = sorted(results, key=lambda r: r["avg"] or -9, reverse=True)

    report = {
        "note": "Exploratory dig on discovery only; original EXP-0056 verdict unchanged unless a combo is frozen and holdout-tested separately.",
        "percentile_definition": "prior bar volume/delta percentile vs previous 50 completed bars (causal, min 10). Original arms used fixed vol_ratio>=1.5 and adf>=0.20/0.30.",
        "screen_gated_symmetric": screen,
        "selected": selected,
        "resim": results_sorted,
        "passing": passing,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    md = [
        "# EXP-0056 dig — percentile / contrarian combos",
        "",
        "Original arms are **fixed cutoffs**, not percentiles: "
        "`vol_ratio >= 1.5`, `adf >= 0.20`, `vol_delta adf >= 0.30`.",
        "This dig adds causal rolling percentiles (vs prior 50 bars) and "
        "Stage-1-inspired combos. Discovery only; holdout untouched.",
        "",
        f"**Passing resim combos:** {len(passing)}",
        "",
        "| gate | bracket | arm | n | avg | net | WR | pass |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for r in results_sorted[:30]:
        md.append(
            f"| {r['gate']} | {r['bracket']} | {r['arm']} | {r['n']} | {r['avg']} | "
            f"{r['net']} | {r['wr']} | {'PASS' if r['pass'] else '—'} |"
        )
    md += ["", "## Screen (gated, symmetric, descriptive)", "",
           "| mask | n | avg | net | months+ |", "|---|---:|---:|---:|---:|"]
    for name in sorted(screen, key=lambda k: screen[k]["avg"] or -9, reverse=True):
        r = screen[name]
        md.append(f"| {name} | {r['n']} | {r['avg']} | {r['net']} | {r['months_pos']} |")
    md.append("")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)
    print(f"PASSING: {passing[:3]}", flush=True)


if __name__ == "__main__":
    main()
