"""All-month indicator filter search for gated MAT (Apr–Oct 2026).

IN-SAMPLE MINING, NOT VALIDATION: Sep–Oct were already read (EXP-0056_DIG
holdout), so no untouched holdout remains. Goal: find a causal filter that is
net-positive in *every* month Apr–Oct. Anything found here needs future /
unseen data to validate.

Method: simulate gated MAT once per session, screen many causal single
filters + pairs on the executed trades, then exactly re-simulate the
all-month-positive shortlist (filtered event subsets, flat-only simulator).

Fixed context: MAT trend+VWAP gated, rr1_1 brackets. Writes
results/EXP-0056_ALLMONTH/.
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
from research.features.range_bar_profile import RangeBarProfileConfig, attach_mat_range_context
from research.features.regime import build_regime_index, lookup_regime
from research.run_experiment import _load_trades
from research.run_mat_range_bars import (
    COMMISSION,
    DISCOVERY_MMYY,
    HOLDOUT_MMYY,
    bounds,
    filt,
    list_sessions,
    make_bracket,
    sid_month,
    summary_dict,
)
from research.run_mat_range_bars_dig import candidate_masks, gated_events

ALL_MMYY = DISCOVERY_MMYY | HOLDOUT_MMYY
ANCHORS = ["vol_mid_bar_against", "bar_against", "delta_contra", "twobar_trap_literal", "va_inside"]


def load_all(cfg: dict) -> dict[str, dict]:
    pq_dir = Path(cfg["parquet_dir"])
    base_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")
    rb_cfg = RangeBarProfileConfig()
    data: dict[str, dict] = {}
    for sid in list_sessions(pq_dir):
        if sid_month(sid) not in ALL_MMYY:
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
        # Causal regime indicators at each event (60m / 30m snapshots).
        if ev.num_rows:
            bars, bar_ts, cum_pv, cum_v, t_ts, t_px, t_sz = build_regime_index(nq)
            kw = dict(vwap_from_ns=vf, trade_ts=t_ts, trade_px=t_px, trade_sz=t_sz)
            ev_ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
            side = ev.column("side").to_numpy().astype(int)
            s60 = lookup_regime(bars, bar_ts, cum_pv, cum_v, ev_ts, window_min=60, **kw)
            s30 = lookup_regime(bars, bar_ts, cum_pv, cum_v, ev_ts, window_min=30, **kw)
            rg = {
                "rg_range60": np.array([s.range_pts for s in s60]),
                "rg_er60": np.array([s.efficiency_ratio for s in s60]),
                "rg_net30_aligned": np.array([s.net_pts * (1 if sd > 0 else -1) for s, sd in zip(s30, side)]),
                "rg_net60_aligned": np.array([s.net_pts * (1 if sd > 0 else -1) for s, sd in zip(s60, side)]),
                "rg_vwap_dist_aligned": np.array([
                    (s.last_px - s.vwap) * (1 if sd > 0 else -1) if np.isfinite(s.vwap) else np.nan
                    for s, sd in zip(s60, side)
                ]),
                "rg_med_r30": np.array([s.median_minute_range for s in s30]),
            }
            for k, v in rg.items():
                if k in ev.column_names:
                    ev = ev.drop(k)
                ev = ev.append_column(k, pa.array(v, type=pa.float64()))
        data[sid] = {"nq": nq, "es": es, "vf": vf, "raw_events": ev, "month": sid_month(sid)}
        print(f"load {sid} ({sid_month(sid)}): raw_mat={raw.num_rows}", flush=True)
    return data


def _terciles(vals: np.ndarray) -> tuple[float, float]:
    v = vals[np.isfinite(vals)]
    if len(v) < 10:
        return float("nan"), float("nan")
    return float(np.quantile(v, 1 / 3)), float(np.quantile(v, 2 / 3))


def pooled_edges(events: dict[str, pa.Table]) -> dict[str, tuple[float, float]]:
    cols = ["total_vol", "n_trades", "max_print", "es_total_vol", "rg_range60"]
    out = {}
    for c in cols:
        vals = np.concatenate([
            ev.column(c).to_numpy().astype(np.float64)
            for ev in events.values() if ev.num_rows and c in ev.column_names
        ]) if events else np.array([])
        out[c] = _terciles(vals)
    # VA width from prior vah/val
    widths = []
    for ev in events.values():
        if ev.num_rows:
            widths.append(ev.column("prior_vah").to_numpy() - ev.column("prior_val").to_numpy())
    out["va_width"] = _terciles(np.concatenate(widths)) if widths else (float("nan"),) * 2
    return out


def event_masks(ev: pa.Table, vf: int, edges: dict) -> dict[str, np.ndarray]:
    """All causal single-filter masks (+ candidate_masks anchors) for events."""
    n = ev.num_rows
    if n == 0:
        return {}
    masks = dict(candidate_masks(ev))
    side = ev.column("side").to_numpy().astype(int)
    ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
    mins = (ts - vf) / 60e9

    def col(name: str) -> np.ndarray:
        return ev.column(name).to_numpy().astype(np.float64)

    imb = np.abs(col("imbalance"))
    tv, nt, mp = col("total_vol"), col("n_trades"), col("max_print")
    es_lag, es_vol = col("es_lag_ms"), col("es_total_vol")
    vr, vp, adf = col("prior_vol_ratio"), col("prior_vol_percentile"), col("prior_aligned_delta_frac")
    mat_ticks = col("mat_px_ticks")
    vpoc_dist = side * (mat_ticks - col("prior_vpoc"))
    va_w = col("prior_vah") - col("prior_val")
    p_dir = ev.column("partial_direction").to_numpy().astype(int)
    padf = col("partial_aligned_delta_frac")
    er60, r60 = col("rg_er60"), col("rg_range60")
    net30a, net60a = col("rg_net30_aligned"), col("rg_net60_aligned")
    vwda, medr = col("rg_vwap_dist_aligned"), col("rg_med_r30")

    def terc(v: np.ndarray, key: str, which: str) -> np.ndarray:
        lo, hi = edges[key]
        if not np.isfinite(lo):
            return np.zeros(n, dtype=bool)
        return (v <= lo) if which == "lo" else ((v > lo) & (v <= hi)) if which == "mid" else (v > hi)

    m: dict[str, np.ndarray] = {
        "tod_0_30": (mins >= 0) & (mins < 30),
        "tod_30_90": (mins >= 30) & (mins < 90),
        "tod_90_150": mins >= 90,
        "tod_after60": mins >= 60,
        "side_long": side > 0,
        "side_short": side < 0,
        "nq_imb_ge80": imb >= 0.80,
        "nq_imb_lt80": imb < 0.80,
        "nq_vol_lo": terc(tv, "total_vol", "lo"),
        "nq_vol_mid": terc(tv, "total_vol", "mid"),
        "nq_vol_hi": terc(tv, "total_vol", "hi"),
        "nq_ntr_lo": terc(nt, "n_trades", "lo"),
        "nq_ntr_hi": terc(nt, "n_trades", "hi"),
        "nq_maxprint_hi": terc(mp, "max_print", "hi"),
        "es_lag_le250": es_lag <= 250,
        "es_lag_250_750": (es_lag > 250) & (es_lag <= 750),
        "es_lag_gt750": es_lag > 750,
        "es_vol_lo": terc(es_vol, "es_total_vol", "lo"),
        "es_vol_hi": terc(es_vol, "es_total_vol", "hi"),
        "prior_vol_low": np.isfinite(vr) & (vr < 0.8),
        "prior_vol_not_high": np.isfinite(vr) & (vr <= 1.4),
        "prior_volpct_lt50": np.isfinite(vp) & (vp < 50),
        "prior_volpct_ge80": np.isfinite(vp) & (vp >= 80),
        "prior_vpocdist_pos": np.isfinite(vpoc_dist) & (vpoc_dist > 0),
        "prior_vpocdist_neg": np.isfinite(vpoc_dist) & (vpoc_dist < 0),
        "prior_vpocdist_far": np.isfinite(vpoc_dist) & (vpoc_dist > 10),
        "prior_vawidth_lo": terc(va_w, "va_width", "lo"),
        "prior_vawidth_hi": terc(va_w, "va_width", "hi"),
        "partial_with": (p_dir == side) & (p_dir != 0),
        "partial_against": (p_dir == -side) & (p_dir != 0),
        "partial_adf_aligned": np.isfinite(padf) & (padf >= 0.2),
        "partial_adf_contra": np.isfinite(padf) & (padf < 0),
        "rg_er60_le30": er60 <= 0.30,
        "rg_er60_30_60": (er60 > 0.30) & (er60 <= 0.60),
        "rg_er60_gt60": er60 > 0.60,
        "rg_range60_lo": terc(r60, "rg_range60", "lo"),
        "rg_range60_hi": terc(r60, "rg_range60", "hi"),
        "rg_net30_with": net30a > 0,
        "rg_net30_against": net30a < 0,
        "rg_net60_with": net60a > 0,
        "rg_net60_against": net60a < 0,
        "rg_vwapdist_pos": np.isfinite(vwda) & (vwda > 0),
        "rg_vwapdist_neg": np.isfinite(vwda) & (vwda < 0),
        "rg_medr_ge8": medr >= 8,
        "rg_medr_ge12": medr >= 12,
        "rg_medr_lt12": medr < 12,
    }
    masks.update(m)
    return masks


def evaluate(mask_by_trade: np.ndarray, pnls: np.ndarray, months: np.ndarray) -> dict:
    by_m = {}
    for mo in sorted(set(months.tolist())):
        sel = mask_by_trade & (months == mo)
        by_m[mo] = {
            "n": int(sel.sum()),
            "avg": round(float(pnls[sel].mean()), 3) if sel.any() else None,
            "net": round(float(pnls[sel].sum()), 2),
        }
    sel = mask_by_trade
    nets = [s["net"] for s in by_m.values()]
    avgs = [s["avg"] for s in by_m.values() if s["avg"] is not None]
    return {
        "n": int(sel.sum()),
        "avg": round(float(pnls[sel].mean()), 3) if sel.any() else None,
        "net": round(float(pnls[sel].sum()), 2),
        "months_pos": int(sum(1 for x in nets if x > 0)),
        "min_month_avg": round(min(avgs), 3) if avgs else None,
        "min_month_n": min((s["n"] for s in by_m.values()), default=0),
        "by_month": by_m,
    }


def all_months_ok(r: dict) -> bool:
    return (
        r["n"] >= 50
        and r["months_pos"] == 7
        and r["min_month_n"] >= 3
        and (r["min_month_avg"] or -9) > 0
    )


def main() -> None:
    cfg = load_config()
    out = Path(cfg["results_dir"]) / "EXP-0056_ALLMONTH"
    out.mkdir(parents=True, exist_ok=True)
    data = load_all(cfg)
    events = gated_events(data, True, True)
    edges = pooled_edges(events)
    print(f"tercile edges: {edges}", flush=True)

    sim_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=COMMISSION, es_stamp="causal")
    br = make_bracket(tp_r=1.0, sl_r=1.0)

    # Screen: base gated trades once per session, tagged with event masks.
    trade_masks: dict[str, list[bool]] = defaultdict(list)
    pnls: list[float] = []
    months: list[str] = []
    single_names: list[str] = []
    for sid, sd in data.items():
        ev = events[sid]
        if ev.num_rows == 0:
            continue
        masks = event_masks(ev, sd["vf"], edges)
        if not single_names:
            single_names = list(masks.keys())
        s = simulate_session(
            sid, sd["nq"], sd["es"], sim_cfg,
            gate=None, bracket_fn=br, vwap_from_ns=sd["vf"], events=ev,
        )
        ev_ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        for tr in s.trades:
            j = int(np.searchsorted(ev_ts, tr.entry_ts_ns, side="right")) - 1
            if j < 0:
                continue
            pnls.append(tr.pnl - COMMISSION)
            months.append(sd["month"])
            for name, mask in masks.items():
                trade_masks[name].append(bool(mask[j]))
    pnls_a = np.array(pnls)
    months_a = np.array(months)
    tm = {k: np.array(v, dtype=bool) for k, v in trade_masks.items()}
    print(f"screen trades: n={len(pnls_a)} base avg={pnls_a.mean():.3f}", flush=True)

    singles = {name: evaluate(tm[name], pnls_a, months_a) for name in single_names}
    for name in sorted(singles, key=lambda k: singles[k]["min_month_avg"] or -9, reverse=True)[:15]:
        r = singles[name]
        print(f"single {name}: n={r['n']} avg={r['avg']} months+={r['months_pos']}/7 min_avg={r['min_month_avg']}", flush=True)

    # Pairs: anchors x singles, plus top singles x top singles.
    pair_masks: dict[str, np.ndarray] = {}
    for a in ANCHORS:
        if a not in tm:
            continue
        for sname in single_names:
            if sname in (a, "base"):
                continue
            pair_masks[f"{a} & {sname}"] = tm[a] & tm[sname]
    top_singles = [
        k for k in sorted(singles, key=lambda k: (singles[k]["months_pos"], singles[k]["avg"] or -9), reverse=True)
        if k not in ("base",) and singles[k]["n"] >= 80
    ][:12]
    for i, a in enumerate(top_singles):
        for b in top_singles[i + 1:]:
            pair_masks[f"{a} & {b}"] = tm[a] & tm[b]

    pairs = {name: evaluate(mk, pnls_a, months_a) for name, mk in pair_masks.items()}
    screen_winners = {k: v for k, v in {**singles, **pairs}.items() if all_months_ok(v)}
    for name in sorted(screen_winners, key=lambda k: screen_winners[k]["min_month_avg"] or -9, reverse=True)[:15]:
        r = screen_winners[name]
        print(f"SCREEN-WIN {name}: n={r['n']} avg={r['avg']} net={r['net']} min_month_avg={r['min_month_avg']}", flush=True)
    print(f"screen winners (descriptive): {len(screen_winners)}", flush=True)

    # Exact resim of top descriptive winners only.
    def parse(name: str) -> list[str]:
        return [p.strip() for p in name.split(" & ")]

    shortlist = sorted(
        screen_winners, key=lambda k: (screen_winners[k]["min_month_avg"] or -9, screen_winners[k]["avg"] or -9),
        reverse=True,
    )[:8]
    resim: dict[str, dict] = {}
    for name in shortlist:
        parts_needed = parse(name)
        sel_parts = []
        parts_by_month: dict[str, list] = defaultdict(list)
        for sid, sd in data.items():
            ev = events[sid]
            if ev.num_rows == 0:
                continue
            masks = event_masks(ev, sd["vf"], edges)
            mk = np.ones(ev.num_rows, dtype=bool)
            for p in parts_needed:
                mk &= masks[p]
            if not mk.any():
                continue
            s = simulate_session(
                sid, sd["nq"], sd["es"], sim_cfg,
                gate=None, bracket_fn=br, vwap_from_ns=sd["vf"],
                events=ev.take(np.where(mk)[0]),
            )
            sel_parts.append(s)
            parts_by_month[sd["month"]].append(s)
        r = summary_dict(sel_parts)
        r["by_month"] = {mo: summary_dict(ps) for mo, ps in sorted(parts_by_month.items())}
        r["all_months_positive"] = (
            len(r["by_month"]) == 7
            and all((s.get("net") or 0) > 0 and (s.get("avg") or -9) > 0 for s in r["by_month"].values())
            and r.get("n", 0) >= 50
        )
        resim[name] = r
        print(f"RESIM {name}: n={r.get('n')} avg={r.get('avg')} net={r.get('net')} all7+={r['all_months_positive']}", flush=True)

    base_r = singles.get("base", {})
    confirmed = [k for k, v in resim.items() if v["all_months_positive"]]
    report = {
        "warning": "In-sample all-month mining Apr-Oct; Sep-Oct already read. No untouched holdout. Future data required.",
        "tercile_edges": {k: list(v) for k, v in edges.items()},
        "base_screen": base_r,
        "singles": singles,
        "screen_winners": screen_winners,
        "resim": resim,
        "confirmed_all_months": confirmed,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    md = [
        "# EXP-0056 all-month filter search (Apr–Oct, IN-SAMPLE)",
        "",
        "**Warning:** Sep–Oct already read — this is mining, not validation. "
        "A confirmed filter here still needs future/unseen data.",
        "",
        f"Base (screen): n={base_r.get('n')} avg={base_r.get('avg')} months+={base_r.get('months_pos')}/7",
        "",
        f"**Descriptive screen winners: {len(screen_winners)} · Exact-resim all-7-months-positive: {confirmed or 'none'}**",
        "",
        "## Exact resim shortlist",
        "",
        "| filter | n | avg | net | WR | PF | all 7 months + |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for name in shortlist:
        r = resim[name]
        md.append(f"| {name} | {r.get('n')} | {r.get('avg')} | {r.get('net')} | {r.get('wr')} | {r.get('pf')} | {r['all_months_positive']} |")
    if shortlist:
        md += ["", "### Resim by month", "", "| filter | month | n | avg | net |", "|---|---|---:|---:|---:|"]
        for name in shortlist:
            for mo, s in sorted((resim[name].get("by_month") or {}).items()):
                md.append(f"| {name} | {mo} | {s.get('n')} | {s.get('avg')} | {s.get('net')} |")
    md += ["", "## Top singles (descriptive screen)", "",
           "| filter | n | avg | net | months+ | min month avg |", "|---|---:|---:|---:|---:|---:|"]
    for name in sorted(singles, key=lambda k: singles[k]["min_month_avg"] or -9, reverse=True)[:20]:
        r = singles[name]
        md.append(f"| {name} | {r['n']} | {r['avg']} | {r['net']} | {r['months_pos']}/7 | {r['min_month_avg']} |")
    md.append("")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)
    print(f"CONFIRMED: {confirmed}", flush=True)


if __name__ == "__main__":
    main()
