"""EXP-0036_DIG3 — anatomy dig on the reopened ES-against fade.

Answers whether the trigger carries information or the bracket/regime does
the work. Pre-registered ship bar is in the YAML description and written into
the summary header before any numbers.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    TradeResult,
    combine_gates,
    filter_summary,
    make_daytype_skip,
    make_trend_mode,
    make_vol_bracket,
    make_vwap_side,
    make_vwap_side_mode,
    simulate_session,
)
from research.correlated import harness as H
from research.features.corr_exhaust import ExhaustParams, exhaust_fade_events
from research.outcomes.label import label_events


SHIP_BAR = (
    "On the chosen slice: train AND hold AND new all > 0; days+ >= 14/23; "
    "top-3 share of positive net <= 0.55; leave-one-day-out net never < 0 on "
    "more than 2 drops; regime/strata story must hold on train AND new. "
    "Short of that = keep open dig. Exit overlay / EXP-0035 out of scope."
)


def _params(base: ExhaustParams, over: dict) -> ExhaustParams:
    d = {**asdict(base), **{k: v for k, v in over.items() if k in asdict(base)}}
    return ExhaustParams(**d)


def _tercile_edges(vals: np.ndarray) -> tuple[float, float] | None:
    v = vals[np.isfinite(vals)]
    if len(v) < 9:
        return None
    q1, q2 = np.nanpercentile(v, [33.333, 66.667])
    return float(q1), float(q2)


def _tercile_label(x: float, edges: tuple[float, float] | None) -> str:
    if edges is None or not np.isfinite(x):
        return "na"
    if x <= edges[0]:
        return "lo"
    if x <= edges[1]:
        return "mid"
    return "hi"


def _pack_trades(trades: list[TradeResult], sids: list[str], commission: float = H.COMMISSION) -> dict:
    s = SimSummary()
    for tr in trades:
        s.trades.append(tr)
        s.n += 1
        s.gross += tr.pnl
        s.commission_total += commission
        net = tr.pnl - commission
        s.net += net
        if tr.win:
            s.wins += 1
            s.gross_win += max(tr.pnl, 0.0)
        else:
            s.gross_loss += abs(min(tr.pnl, 0.0))
            if tr.pnl > 0:
                s.gross_win += tr.pnl
    # rebuild max_dd
    eq = peak = 0.0
    for tr in trades:
        eq += tr.pnl - commission
        peak = max(peak, eq)
        s.max_dd = max(s.max_dd, peak - eq)
    return H.pack_pnl(s, sids, commission)


def _concentration(trades: list[TradeResult], sids: list[str], commission: float = H.COMMISSION) -> dict:
    by = defaultdict(float)
    for tr in trades:
        by[tr.session_id] += tr.pnl - commission
    day = [{"session_id": sid, "cohort": H.cohort(sid), "net": round(by.get(sid, 0.0), 2)} for sid in sids]
    nets = np.array([d["net"] for d in day])
    pos = nets[nets > 0]
    top3 = sorted(day, key=lambda d: d["net"], reverse=True)[:3]
    bot3 = sorted(day, key=lambda d: d["net"])[:3]
    total_pos = float(pos.sum()) if len(pos) else 0.0
    return {
        "days_pos": int((nets > 0).sum()),
        "days_neg": int((nets < 0).sum()),
        "top3": top3,
        "worst3": bot3,
        "top3_net": round(sum(d["net"] for d in top3), 2),
        "top3_share_of_pos": round(sum(d["net"] for d in top3) / total_pos, 3) if total_pos > 0 else None,
        "worst3_net": round(sum(d["net"] for d in bot3), 2),
    }


def _hour_bucket_uk(ns: int) -> str:
    dt = datetime.fromtimestamp(ns / 1e9, tz=H.UK)
    h, m = dt.hour, dt.minute
    # half-hour buckets 14:30-17:00
    start_m = (m // 30) * 30
    return f"{h:02d}:{start_m:02d}"


def _sim_fade(
    pairs: list[H.Pair],
    by_sid: dict[str, pa.Table],
    *,
    gate=None,
    bracket_fn=None,
    tp: float = 10.0,
    sl: float = 10.0,
    slip: float = 1.0,
    cooldown: float = 5.0,
) -> SimSummary:
    cfg = MatSimConfig(
        slip_ticks=slip, commission_pts_rt=H.COMMISSION, tp_pts=tp, sl_pts=sl,
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


def _join_trades_to_events(events: pa.Table, trades: list[TradeResult]) -> pa.Table:
    """Left-join TradeResult fields onto fade events on (session_id, entry_ts_ns)."""
    if events.num_rows == 0:
        return events
    lookup = {(t.session_id, t.entry_ts_ns): t for t in trades}
    sids = events.column("session_id").to_numpy(zero_copy_only=False)
    ets = events.column("event_ts_ns").to_numpy().astype(np.int64)
    cols = {
        "traded": [], "pnl": [], "win": [], "reason": [], "tp_pts": [], "sl_pts": [],
        "bars_held": [], "range60": [], "er60": [], "net30": [], "vwap": [], "med_r30": [],
        "entry_px": [], "exit_px": [], "exit_ts_ns": [],
    }
    for sid, t0 in zip(sids, ets):
        tr = lookup.get((str(sid), int(t0)))
        if tr is None:
            cols["traded"].append(0)
            cols["pnl"].append(float("nan"))
            cols["win"].append(0)
            cols["reason"].append("")
            cols["tp_pts"].append(float("nan"))
            cols["sl_pts"].append(float("nan"))
            cols["bars_held"].append(0)
            cols["range60"].append(float("nan"))
            cols["er60"].append(float("nan"))
            cols["net30"].append(float("nan"))
            cols["vwap"].append(float("nan"))
            cols["med_r30"].append(float("nan"))
            cols["entry_px"].append(float("nan"))
            cols["exit_px"].append(float("nan"))
            cols["exit_ts_ns"].append(0)
        else:
            cols["traded"].append(1)
            cols["pnl"].append(tr.pnl)
            cols["win"].append(1 if tr.win else 0)
            cols["reason"].append(tr.reason)
            cols["tp_pts"].append(tr.tp_pts)
            cols["sl_pts"].append(tr.sl_pts)
            cols["bars_held"].append(tr.bars_held)
            cols["range60"].append(tr.range60)
            cols["er60"].append(tr.er60)
            cols["net30"].append(tr.net30)
            cols["vwap"].append(tr.vwap)
            cols["med_r30"].append(tr.med_r30)
            cols["entry_px"].append(tr.entry_px)
            cols["exit_px"].append(tr.exit_px)
            cols["exit_ts_ns"].append(tr.exit_ts_ns)
    out = events
    for name, vals in cols.items():
        if name in out.column_names:
            out = out.drop_columns([name])
        if name in ("traded", "win", "bars_held"):
            out = out.append_column(name, pa.array(vals, type=pa.int32()))
        elif name in ("reason",):
            out = out.append_column(name, pa.array(vals, type=pa.string()))
        elif name == "exit_ts_ns":
            out = out.append_column(name, pa.array(vals, type=pa.int64()))
        else:
            out = out.append_column(name, pa.array(vals, type=pa.float64()))
    return out


def _stratum_pnl(joined: pa.Table, mask: np.ndarray, sids: list[str], commission: float = H.COMMISSION) -> dict:
    if joined.num_rows == 0 or not mask.any():
        return {"n": 0, "net": 0.0, "train_net": 0.0, "hold_net": 0.0, "new_net": 0.0, "days_pos": 0}
    idx = np.where(mask & (joined.column("traded").to_numpy() == 1))[0]
    if len(idx) == 0:
        return {"n": 0, "net": 0.0, "train_net": 0.0, "hold_net": 0.0, "new_net": 0.0, "days_pos": 0}
    sub = joined.take(idx)
    trades = []
    for i in range(sub.num_rows):
        trades.append(TradeResult(
            session_id=str(sub.column("session_id")[i].as_py()),
            entry_ts_ns=int(sub.column("event_ts_ns")[i].as_py()),
            side=int(sub.column("side")[i].as_py()),
            entry_px=float(sub.column("entry_px")[i].as_py()),
            exit_px=float(sub.column("exit_px")[i].as_py()),
            pnl=float(sub.column("pnl")[i].as_py()),
            win=bool(sub.column("win")[i].as_py()),
            bars_held=int(sub.column("bars_held")[i].as_py()),
            reason=str(sub.column("reason")[i].as_py()),
            tp_pts=float(sub.column("tp_pts")[i].as_py()),
            sl_pts=float(sub.column("sl_pts")[i].as_py()),
            range60=float(sub.column("range60")[i].as_py()),
            er60=float(sub.column("er60")[i].as_py()),
            net30=float(sub.column("net30")[i].as_py()),
            vwap=float(sub.column("vwap")[i].as_py()),
            med_r30=float(sub.column("med_r30")[i].as_py()),
            exit_ts_ns=int(sub.column("exit_ts_ns")[i].as_py()),
        ))
    r = _pack_trades(trades, sids, commission)
    c = _concentration(trades, sids, commission)
    return {**r, "top3_share": c["top3_share_of_pos"], "top3": c["top3"], "worst3": c["worst3"]}


def _md_stratum_table(title: str, rows: dict[str, dict]) -> list[str]:
    md = [f"### {title}", "", *H.PNL_HDR]
    for name, r in rows.items():
        if not r or r.get("n", 0) == 0:
            md.append(f"| {name} | 0 | – | +0.0 | +0.0 | +0.0 | +0.0 | +0.0 | 0/0 | 0 | +0.0 |")
        else:
            md.append(H.pnl_row(name, r))
    md.append("")
    return md


def run(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    parquet_dir = Path(cfg["parquet_dir"])
    results_dir = Path(cfg["results_dir"]) / exp["name"]
    horizons = list(exp.get("horizons_ms") or cfg["outcome_horizons_ms"])
    targets = list(exp.get("point_targets") or cfg["nq_point_targets"])
    exclude = set(str(s) for s in exp.get("exclude_sessions") or [])
    bar_ms = int(exp.get("bar_ms", 100))
    fade_wait = float(exp.get("fade_max_wait_sec", 5.0))
    daytype_ranges = list(exp.get("daytype_ranges") or [150, 175, 200])
    daytype_ers = list(exp.get("daytype_ers") or [0.3, 0.4])

    pairs: list[H.Pair] = []
    for sid in session_ids:
        if sid in exclude:
            continue
        p = H.load_pair(parquet_dir, sid)
        if p is not None:
            pairs.append(p)
    sids = [p.sid for p in pairs]
    print(f"[DIG3] {len(pairs)} sessions", flush=True)

    default_p = ExhaustParams(**(exp.get("exhaust") or {}))
    variants = {"against_m04" if "against_m04" in (exp.get("variants") or {"against_m04": {}}) else "default": default_p}
    for name, over in (exp.get("variants") or {}).items():
        variants[name] = _params(default_p, over)

    # Build fade events once per variant
    fade_raw: dict[str, list[pa.Table]] = {k: [] for k in variants}
    fade_by_sid: dict[str, dict[str, pa.Table]] = {k: {} for k in variants}
    bursts_by_sid: dict[str, pa.Table] = {}
    for p in pairs:
        nq_b, es_b = p.bars(bar_ms)
        # Same confirm bursts as DIG1/DIG2 (detect_signals + max_es_lag filter)
        bursts = H.confirm_bursts(p)
        bursts_by_sid[p.sid] = bursts
        for vname, p_ex in variants.items():
            fe = exhaust_fade_events(bursts, nq_b, es_b, p_ex, max_wait_sec=fade_wait)
            if fe.num_rows:
                fe = H._with_session(fe, p.sid)
                fade_raw[vname].append(fe)
                fade_by_sid[vname][p.sid] = fe
        print(
            f"  {p.sid}: bursts={bursts.num_rows} "
            + " ".join(f"{k}={fade_by_sid[k].get(p.sid, pa.table({})).num_rows}" for k in variants),
            flush=True,
        )

    primary = "against_m04" if "against_m04" in variants else next(iter(variants))
    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        exp.get("description", "").strip(),
        "",
        f"**Pre-registered ship bar:** {SHIP_BAR}",
        "",
        f"Sessions={len(sids)} · UK 14:30–17:00 · 1-tick slip · {H.COMMISSION} pt/RT · "
        f"train={sum(1 for s in sids if H.cohort(s)=='train')} "
        f"hold={sum(1 for s in sids if H.cohort(s)=='hold')} "
        f"new={sum(1 for s in sids if H.cohort(s)=='new')}",
        "",
        "## Variants",
        "",
        "| name | es_imb_max | nq_imb | nq_vol | use_vol |",
        "|---|---:|---:|---:|---|",
    ]
    for name, p in variants.items():
        md.append(f"| {name} | {p.es_imb_max} | {p.nq_min_imbalance} | {p.nq_min_vol} | {p.use_vol} |")
    md.append("")

    report: dict = {
        "status": "ok",
        "sessions": sids,
        "ship_bar": SHIP_BAR,
        "variants": {k: asdict(v) for k, v in variants.items()},
    }

    # ============================================================ Baseline P&L
    print("[DIG3] baseline P&L", flush=True)
    br_vol = make_vol_bracket(1.0)
    baselines: dict[str, dict] = {}
    joined_by_var: dict[str, pa.Table] = {}
    sims_by_var: dict[str, SimSummary] = {}
    md += ["## Baseline (ungated vol k=1)", "", *H.PNL_HDR]
    for vname in variants:
        sim = _sim_fade(pairs, fade_by_sid[vname], gate=None, bracket_fn=br_vol)
        sims_by_var[vname] = sim
        r = H.pack_pnl(sim, sids)
        conc = _concentration(sim.trades, sids)
        baselines[vname] = {**r, "concentration": conc}
        md.append(H.pnl_row(vname, r))
        print(
            f"  {vname}: n={r['n']} net={r['net']:+.1f} train={r['train_net']:+.1f} "
            f"hold={r['hold_net']:+.1f} new={r['new_net']:+.1f} top3_share={conc['top3_share_of_pos']}",
            flush=True,
        )
        # join
        if fade_raw[vname]:
            ev = pa.concat_tables(fade_raw[vname], promote_options="default")
            joined_by_var[vname] = _join_trades_to_events(ev, sim.trades)
        else:
            joined_by_var[vname] = pa.table({})
    md.append("")
    for vname, b in baselines.items():
        c = b["concentration"]
        md += [
            f"**{vname} concentration:** days+ {c['days_pos']}/23 · top3 share={c['top3_share_of_pos']} "
            f"(top3 {c['top3_net']:+.1f}; worst3 {c['worst3_net']:+.1f})",
            f"- top3: {c['top3']}",
            f"- worst3: {c['worst3']}",
            "",
        ]
    report["baseline"] = baselines

    # Use primary joined table for strata
    J = joined_by_var[primary]
    traded = J.column("traded").to_numpy() == 1 if J.num_rows else np.zeros(0, dtype=bool)

    # ============================================================ 1. Regime
    print("[DIG3] §1 regime", flush=True)
    md += ["## 1. Regime split", ""]
    regime_rows: dict[str, dict] = {}
    if J.num_rows and traded.any():
        for col, label in (("range60", "range60"), ("er60", "er60"), ("med_r30", "med_r30")):
            vals = J.column(col).to_numpy().astype(float)
            edges = _tercile_edges(vals[traded])
            terc = np.array([_tercile_label(float(x), edges) for x in vals])
            for lab in ("lo", "mid", "hi"):
                key = f"{label}_{lab}"
                regime_rows[key] = _stratum_pnl(J, terc == lab, sids)
            # also per-cohort for hi/lo
            for lab in ("lo", "hi"):
                for coh in ("train", "hold", "new"):
                    sids_c = {s for s in sids if H.cohort(s) == coh}
                    mask = (terc == lab) & np.array([str(s) in sids_c for s in J.column("session_id").to_numpy(zero_copy_only=False)])
                    regime_rows[f"{label}_{lab}_{coh}"] = _stratum_pnl(J, mask, sids)
            md.append(f"Tercile edges `{label}`: {edges}")
        md += _md_stratum_table("Regime terciles (all cohorts)", {k: v for k, v in regime_rows.items() if k.count("_") == 1})
        md += _md_stratum_table("Regime × cohort (lo/hi)", {k: v for k, v in regime_rows.items() if k.count("_") == 2})

    # Daytype gates
    daytype_pnl: dict[str, dict] = {}
    md += ["### Daytype skip gates (fade only when cool)", "", *H.PNL_HDR]
    for R in daytype_ranges:
        g = make_daytype_skip(max_range=float(R))
        sim = _sim_fade(pairs, fade_by_sid[primary], gate=g, bracket_fn=br_vol)
        r = H.pack_pnl(sim, sids)
        daytype_pnl[f"skip_range{R}"] = {**r, "concentration": _concentration(sim.trades, sids)}
        md.append(H.pnl_row(f"skip_range{R}", r))
        print(f"  skip_range{R}: n={r['n']} net={r['net']:+.1f} train={r['train_net']:+.1f} new={r['new_net']:+.1f}", flush=True)
    for er in daytype_ers:
        g = make_daytype_skip(max_er=float(er))
        sim = _sim_fade(pairs, fade_by_sid[primary], gate=g, bracket_fn=br_vol)
        r = H.pack_pnl(sim, sids)
        daytype_pnl[f"skip_er{er}"] = {**r, "concentration": _concentration(sim.trades, sids)}
        md.append(H.pnl_row(f"skip_er{er}", r))
        print(f"  skip_er{er}: n={r['n']} net={r['net']:+.1f} train={r['train_net']:+.1f} new={r['new_net']:+.1f}", flush=True)
    md.append("")
    report["regime"] = {"terciles": regime_rows, "daytype_gates": daytype_pnl}

    # ============================================================ 2. Anatomy
    print("[DIG3] §2 anatomy", flush=True)
    md += ["## 2. Trade anatomy", ""]
    anatomy: dict = {}
    if J.num_rows and traded.any():
        reasons = Counter(J.column("reason").to_numpy(zero_copy_only=False)[traded].tolist())
        tp = J.column("tp_pts").to_numpy()[traded]
        pnl = J.column("pnl").to_numpy()[traded]
        held = J.column("bars_held").to_numpy()[traded]
        win = J.column("win").to_numpy()[traded] == 1
        anatomy["reasons"] = dict(reasons)
        anatomy["bracket_win_mean"] = float(np.nanmean(tp[win])) if win.any() else None
        anatomy["bracket_loss_mean"] = float(np.nanmean(tp[~win])) if (~win).any() else None
        anatomy["held_win_median"] = float(np.nanmedian(held[win])) if win.any() else None
        anatomy["held_loss_median"] = float(np.nanmedian(held[~win])) if (~win).any() else None
        big = tp >= 20
        anatomy["net_bracket_ge20"] = round(float(pnl[big].sum()), 2) if big.any() else 0.0
        anatomy["n_bracket_ge20"] = int(big.sum())
        anatomy["net_bracket_lt20"] = round(float(pnl[~big].sum()), 2) if (~big).any() else 0.0
        anatomy["n_bracket_lt20"] = int((~big).sum())
        md += [
            f"Exit reasons: {dict(reasons)}",
            f"Bracket size mean win/loss: {anatomy['bracket_win_mean']:.1f} / {anatomy['bracket_loss_mean']:.1f}",
            f"Bars held median win/loss: {anatomy['held_win_median']:.0f} / {anatomy['held_loss_median']:.0f}",
            f"Net on bracket≥20: {anatomy['net_bracket_ge20']:+.1f} (n={anatomy['n_bracket_ge20']}); "
            f"<20: {anatomy['net_bracket_lt20']:+.1f} (n={anatomy['n_bracket_lt20']})",
            "",
        ]
        # MFE/MAE via label_events on fade events
        mfe_parts = []
        for p in pairs:
            fe = fade_by_sid[primary].get(p.sid)
            if fe is None or fe.num_rows == 0:
                continue
            ts, px = p.path()
            mfe_parts.append(H._with_session(label_events(fe, ts, px, horizons, targets), p.sid))
        if mfe_parts:
            from research.run_experiment import _summarize_labeled

            lab = pa.concat_tables(mfe_parts, promote_options="default")
            anatomy["event_study_all"] = _summarize_labeled(lab, horizons, targets)
            s = anatomy["event_study_all"]
            md += [
                f"Event study (fade side, NQ path): n={s.get('n_events')} hit10={s.get('p_hit_10pts')} "
                f"@60s={s.get('mean_ret_pts_60000ms')} MFE={s.get('mean_mfe_pts')} MAE={s.get('mean_mae_pts')}",
                "",
            ]

    # Confirming brackets
    bracket_pnl: dict[str, dict] = {}
    md += ["### Confirming brackets", "", *H.PNL_HDR]
    for bname, brk, tp, sl in (
        ("vol_k1", br_vol, 10.0, 10.0),
        ("fixed_20", None, 20.0, 20.0),
        ("vol_k15", make_vol_bracket(1.5), 10.0, 10.0),
    ):
        sim = _sim_fade(pairs, fade_by_sid[primary], gate=None, bracket_fn=brk, tp=tp, sl=sl)
        r = H.pack_pnl(sim, sids)
        bracket_pnl[bname] = {**r, "concentration": _concentration(sim.trades, sids)}
        md.append(H.pnl_row(bname, r))
        print(f"  bracket {bname}: n={r['n']} net={r['net']:+.1f}", flush=True)
    md.append("")
    report["anatomy"] = {**anatomy, "brackets": bracket_pnl}

    # ============================================================ 3. Against gates
    print("[DIG3] §3 against gates", flush=True)
    gate_pnl: dict[str, dict] = {}
    md += ["## 3. Hypothesis-consistent gates (trend/VWAP against)", "", *H.PNL_HDR]
    gate_specs = {
        "none": None,
        "trend_against": make_trend_mode("against"),
        "vwap_against": make_vwap_side_mode("against"),
        "trend_vwap_against": combine_gates(make_trend_mode("against"), make_vwap_side_mode("against")),
        "trend_with_vwap_with": combine_gates(make_trend_mode("with"), make_vwap_side(True)),  # known-bad comparator
    }
    for gname, g in gate_specs.items():
        sim = _sim_fade(pairs, fade_by_sid[primary], gate=g, bracket_fn=br_vol)
        r = H.pack_pnl(sim, sids)
        gate_pnl[gname] = {**r, "concentration": _concentration(sim.trades, sids)}
        md.append(H.pnl_row(gname, r))
        print(f"  gate {gname}: n={r['n']} net={r['net']:+.1f} train={r['train_net']:+.1f} new={r['new_net']:+.1f}", flush=True)
    md.append("")

    # VWAP stretch terciles on traded entries
    stretch_rows: dict[str, dict] = {}
    if J.num_rows and traded.any():
        entry = J.column("entry_px").to_numpy()
        vwap = J.column("vwap").to_numpy()
        stretch = np.abs(entry - vwap)
        edges = _tercile_edges(stretch[traded])
        terc = np.array([_tercile_label(float(x), edges) for x in stretch])
        for lab in ("lo", "mid", "hi"):
            stretch_rows[f"vwap_stretch_{lab}"] = _stratum_pnl(J, terc == lab, sids)
        md.append(f"VWAP-stretch tercile edges: {edges}")
        md += _md_stratum_table("VWAP stretch", stretch_rows)
    report["gates"] = {"pnl": gate_pnl, "vwap_stretch": stretch_rows}

    # ============================================================ 4. Side / ToD
    print("[DIG3] §4 side/ToD", flush=True)
    side_tod: dict[str, dict] = {}
    if J.num_rows and traded.any():
        side = J.column("side").to_numpy()
        side_tod["long_fade"] = _stratum_pnl(J, side > 0, sids)
        side_tod["short_fade"] = _stratum_pnl(J, side < 0, sids)
        ets = J.column("event_ts_ns").to_numpy()
        buckets = np.array([_hour_bucket_uk(int(t)) for t in ets])
        for b in sorted(set(buckets[traded].tolist())):
            side_tod[f"tod_{b}"] = _stratum_pnl(J, buckets == b, sids)
        md += ["## 4. Side and time-of-day", ""]
        md += _md_stratum_table("Side", {k: side_tod[k] for k in ("long_fade", "short_fade")})
        md += _md_stratum_table("Half-hour buckets (UK)", {k: v for k, v in side_tod.items() if k.startswith("tod_")})
    report["side_tod"] = side_tod

    # ============================================================ 5. Trigger quality
    print("[DIG3] §5 trigger quality", flush=True)
    trig: dict[str, dict] = {}
    if J.num_rows and traded.any():
        wait = J.column("wait_ms").to_numpy()
        trig["wait_lt500"] = _stratum_pnl(J, wait < 500, sids)
        trig["wait_500_2000"] = _stratum_pnl(J, (wait >= 500) & (wait <= 2000), sids)
        trig["wait_gt2000"] = _stratum_pnl(J, wait > 2000, sids)
        if "es_imb_x" in J.column_names:
            ei = J.column("es_imb_x").to_numpy()
            # fade side is against burst; ES was against burst side so side*es_imb was <= -0.4
            # look at magnitude of ES against
            burst_side = J.column("burst_side").to_numpy() if "burst_side" in J.column_names else -J.column("side").to_numpy()
            es_against = burst_side * ei  # more negative = more against
            trig["es_against_m04_m06"] = _stratum_pnl(J, (es_against <= -0.4) & (es_against > -0.6), sids)
            trig["es_against_lt_m06"] = _stratum_pnl(J, es_against <= -0.6, sids)
        if "nq_imb_x" in J.column_names:
            ni = J.column("nq_imb_x").to_numpy()
            burst_side = J.column("burst_side").to_numpy() if "burst_side" in J.column_names else -J.column("side").to_numpy()
            nq_with = burst_side * ni
            edges = _tercile_edges(nq_with[traded])
            terc = np.array([_tercile_label(float(x), edges) for x in nq_with])
            for lab in ("lo", "mid", "hi"):
                trig[f"nq_with_{lab}"] = _stratum_pnl(J, terc == lab, sids)
        if "burst_es_signed_lag_ms" in J.column_names:
            lag = J.column("burst_es_signed_lag_ms").to_numpy()
            trig["es_first"] = _stratum_pnl(J, lag < 0, sids)
            trig["nq_first"] = _stratum_pnl(J, lag >= 0, sids)
        if "burst_es_total_vol" in J.column_names:
            bv = J.column("burst_es_total_vol").to_numpy().astype(float)
            edges = _tercile_edges(bv[traded])
            terc = np.array([_tercile_label(float(x), edges) for x in bv])
            for lab in ("lo", "mid", "hi"):
                trig[f"burst_es_vol_{lab}"] = _stratum_pnl(J, terc == lab, sids)
        md += ["## 5. Trigger quality", ""]
        md += _md_stratum_table("Trigger strata", trig)
    report["trigger"] = trig

    # ============================================================ 6. MAT overlap
    print("[DIG3] §6 MAT overlap", flush=True)
    mat_gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    mat_cfg = MatSimConfig(slip_ticks=1.0, commission_pts_rt=H.COMMISSION, es_stamp="causal")
    mat_parts = [
        simulate_session(p.sid, p.nq, p.es, mat_cfg, gate=mat_gate, bracket_fn=br_vol, vwap_from_ns=p.vwap_from_ns)
        for p in pairs
    ]
    mat_all = H.merge(mat_parts)
    mat_by_sid: dict[str, list[TradeResult]] = defaultdict(list)
    for tr in mat_all.trades:
        mat_by_sid[tr.session_id].append(tr)

    if J.num_rows:
        sids_j = J.column("session_id").to_numpy(zero_copy_only=False)
        ets = J.column("event_ts_ns").to_numpy().astype(np.int64)
        sides = J.column("side").to_numpy().astype(int)
        burst_ts = J.column("burst_ts_ns").to_numpy().astype(np.int64) if "burst_ts_ns" in J.column_names else ets
        in_mat = np.zeros(J.num_rows, dtype=bool)
        after_sl = np.zeros(J.num_rows, dtype=bool)
        for i in range(J.num_rows):
            sid = str(sids_j[i])
            t0 = int(ets[i])
            side = int(sides[i])
            bt = int(burst_ts[i])
            for mt in mat_by_sid.get(sid, []):
                # in open MAT trade, opposite side
                if mt.entry_ts_ns <= t0 <= (mt.exit_ts_ns or mt.entry_ts_ns) and mt.side == -side:
                    in_mat[i] = True
                # MAT trade that entered near the same burst and stopped out
                if abs(mt.entry_ts_ns - bt) <= int(2e9) and mt.reason == "sl" and mt.side == -side:
                    after_sl[i] = True
        overlap_rows = {
            "in_mat_opp": _stratum_pnl(J, in_mat, sids),
            "not_in_mat": _stratum_pnl(J, ~in_mat, sids),
            "after_mat_sl": _stratum_pnl(J, after_sl, sids),
            "not_after_mat_sl": _stratum_pnl(J, ~after_sl, sids),
        }
        # combined equity: MAT + fade (independent books)
        fade_sim = sims_by_var[primary]
        # rebuild combined: all trades sorted by entry
        combined = list(mat_all.trades) + list(fade_sim.trades)
        combined.sort(key=lambda t: (t.session_id, t.entry_ts_ns))
        comb_pack = _pack_trades(combined, sids)
        mat_pack = H.pack_pnl(mat_all, sids)
        fade_pack = H.pack_pnl(fade_sim, sids)
        md += [
            "## 6. Overlap with MAT base (causal vol k=1 trend+VWAP)",
            "",
            f"MAT alone: n={mat_pack['n']} net={mat_pack['net']:+.1f} "
            f"(train {mat_pack['train_net']:+.1f} / hold {mat_pack['hold_net']:+.1f} / new {mat_pack['new_net']:+.1f})",
            f"Fade alone: n={fade_pack['n']} net={fade_pack['net']:+.1f}",
            f"Combined (independent books): n={comb_pack['n']} net={comb_pack['net']:+.1f} "
            f"(train {comb_pack['train_net']:+.1f} / hold {comb_pack['hold_net']:+.1f} / new {comb_pack['new_net']:+.1f})",
            f"in_mat_opp fraction of fade events: {in_mat.mean():.2f}; after_mat_sl: {after_sl.mean():.2f}",
            "",
        ]
        md += _md_stratum_table("Fade P&L by overlap flag", overlap_rows)
        report["overlap"] = {
            "mat": mat_pack,
            "fade": fade_pack,
            "combined": comb_pack,
            "flags": overlap_rows,
            "frac_in_mat_opp": float(in_mat.mean()) if len(in_mat) else None,
            "frac_after_mat_sl": float(after_sl.mean()) if len(after_sl) else None,
        }
    else:
        report["overlap"] = {}

    # ============================================================ 7. Robustness
    print("[DIG3] §7 robustness", flush=True)
    md += ["## 7. Robustness", ""]
    # leave-one-day-out
    base_trades = sims_by_var[primary].trades
    base_net = sum(t.pnl - H.COMMISSION for t in base_trades)
    loo = []
    for sid in sids:
        net = sum(t.pnl - H.COMMISSION for t in base_trades if t.session_id != sid)
        loo.append({"drop": sid, "cohort": H.cohort(sid), "net": round(net, 2), "delta": round(net - base_net, 2)})
    n_neg = sum(1 for r in loo if r["net"] < 0)
    md += [
        f"Leave-one-day-out: base net={base_net:+.1f}; drops with net<0: **{n_neg}**/23 "
        f"(bar allows ≤2).",
        "",
        "| drop | cohort | net without | Δ |",
        "|---|---|---:|---:|",
    ]
    for r in sorted(loo, key=lambda x: x["net"]):
        md.append(f"| {r['drop']} | {r['cohort']} | {r['net']:+.1f} | {r['delta']:+.1f} |")
    md.append("")

    # neighbourhood
    neigh_pnl: dict[str, dict] = {}
    md += ["### Parameter neighbourhood + slip/cooldown", "", *H.PNL_HDR]
    # rebuild events for neighbourhood variants
    neigh_specs = dict(exp.get("neighbourhood") or {})
    neigh_specs["slip2"] = {"_slip": 2.0}
    neigh_specs["cooldown10"] = {"_cooldown": 10.0}
    for nname, over in neigh_specs.items():
        slip = float(over.get("_slip", 1.0))
        cool = float(over.get("_cooldown", 5.0))
        wait = float(over.get("fade_max_wait_sec", fade_wait))
        p_ex = _params(default_p, {k: v for k, v in over.items() if not k.startswith("_") and k != "fade_max_wait_sec"})
        by = {}
        for p in pairs:
            nq_b, es_b = p.bars(bar_ms)
            bursts = bursts_by_sid[p.sid]
            fe = exhaust_fade_events(bursts, nq_b, es_b, p_ex, max_wait_sec=wait)
            if fe.num_rows:
                by[p.sid] = H._with_session(fe, p.sid)
        sim = _sim_fade(pairs, by, gate=None, bracket_fn=br_vol, slip=slip, cooldown=cool)
        r = H.pack_pnl(sim, sids)
        neigh_pnl[nname] = {**r, "concentration": _concentration(sim.trades, sids)}
        md.append(H.pnl_row(nname, r))
        print(f"  neigh {nname}: n={r['n']} net={r['net']:+.1f} train={r['train_net']:+.1f} new={r['new_net']:+.1f}", flush=True)
    # also include primary baseline row
    md.append(H.pnl_row("against_m04 (baseline)", baselines[primary]))
    md.append("")
    report["robustness"] = {"loo": loo, "loo_neg_count": n_neg, "neighbourhood": neigh_pnl}

    # ============================================================ Decision
    b = baselines[primary]
    c = b["concentration"]
    train_ok = b["train_net"] > 0
    hold_ok = b["hold_net"] > 0
    new_ok = b["new_net"] > 0
    days_ok = c["days_pos"] >= 14
    top3_ok = (c["top3_share_of_pos"] or 1.0) <= 0.55
    loo_ok = n_neg <= 2
    # Best gated slice that clears train+hold+new if any
    best_slice = None
    for name, r in {**daytype_pnl, **gate_pnl}.items():
        if r.get("n", 0) >= 30 and r.get("train_net", 0) > 0 and r.get("hold_net", 0) > 0 and r.get("new_net", 0) > 0:
            conc = r.get("concentration") or {}
            share = conc.get("top3_share_of_pos")
            if share is not None and share <= 0.55 and conc.get("days_pos", 0) >= 14:
                best_slice = (name, r)
                break

    if train_ok and hold_ok and new_ok and days_ok and top3_ok and loo_ok:
        decision = "ship-bar cleared (baseline)"
    elif best_slice is not None:
        decision = f"ship-bar cleared on slice `{best_slice[0]}` — keep open dig (confirm before ship)"
    else:
        decision = "keep open dig — ship bar not cleared"

    md += [
        "## Decision",
        "",
        f"**{decision}**",
        "",
        f"Baseline checks: train>0={train_ok} hold>0={hold_ok} new>0={new_ok} "
        f"days+≥14={days_ok} ({c['days_pos']}) top3≤0.55={top3_ok} ({c['top3_share_of_pos']}) "
        f"loo≤2={loo_ok} ({n_neg})",
        "",
    ]
    report["decision"] = {
        "text": decision,
        "baseline_checks": {
            "train_ok": train_ok, "hold_ok": hold_ok, "new_ok": new_ok,
            "days_ok": days_ok, "top3_ok": top3_ok, "loo_ok": loo_ok,
        },
        "best_slice": best_slice[0] if best_slice else None,
    }
    print(f"[DIG3] decision: {decision}", flush=True)

    H.write_report(results_dir, report, md)
    out = {"summary": report}
    if primary in joined_by_var and joined_by_var[primary].num_rows:
        out["events"] = joined_by_var[primary]
    return out
