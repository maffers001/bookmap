"""EXP-0017_REOPEN — aggressor sweeps: points event study → conditional P&L.

Stage 1: dual ES+NQ same-aggressor sweeps (qualify + confirm stamp, |lag|≤250)
vs null / NQ-only / EXP-0008 context on Sep+Oct. Pre-registered event bar.

Stage 2 (only if Stage 1 passes): standalone vol k=1 P&L book under the same
bar used for EXP-0014_PNL. May read once only if Stage 2 also passes.

Usage:
  python -m research.run_sweep_pnl --smoke
  python -m research.run_sweep_pnl --stage1-only
  python -m research.run_sweep_pnl
  python -m research.run_sweep_pnl --may
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from research.backtest.mat_sim import (
    MatSimConfig,
    TradeResult,
    combine_gates,
    make_trend_mode,
    make_vol_bracket,
    make_vol_bracket_rr,
    make_vwap_side,
)
from research.config import load_config
from research.correlated import harness as H
from research.features.sweeps import SweepConfig, aggressor_sweep_events, cross_confirm_sweeps
from research.run_cd_pullback_pnl import (
    COMMISSION,
    _avg,
    _brackets,
    _month_label,
    _pack,
    _sid_month,
    _sim_cfg,
    concentration,
    evaluate_bar,
    null_distribution,
    run_book,
)
from research.run_experiment import _random_null_events

EXP_DIR = Path(__file__).parent / "experiments"


def _load_exp(name: str = "EXP-0017_REOPEN.yaml") -> dict:
    with open(EXP_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _sessions_for_months(parquet_dir: Path, months: list[int]) -> list[str]:
    want = set(int(m) for m in months)
    return [sid for sid in H.list_sessions(parquet_dir) if _sid_month(sid) in want]


def _load_pairs(parquet_dir: Path, sids: list[str]) -> list[H.Pair]:
    pairs = []
    for sid in sids:
        p = H.load_pair(parquet_dir, sid)
        if p is None:
            print(f"  skip {sid} (no NQ in window)", flush=True)
            continue
        pairs.append(p)
    return pairs


# ---------------------------------------------------------------- events

def _sweep_cfg(exp: dict, stamp: str) -> SweepConfig:
    return SweepConfig(
        min_prints=int(exp.get("min_prints", 5)),
        min_vol=float(exp.get("min_vol", 5)),
        min_span_ticks=float(exp.get("min_span_ticks", 1)),
        max_duration_ms=int(exp.get("max_duration_ms", 2000)),
        tick_size=float(exp.get("tick_size", 1.0)),
        cooldown_ms=int(exp.get("cooldown_ms", 500)),
        stamp=stamp,
    )


def _filter_lag(dual: pa.Table, max_lag_ms: float) -> pa.Table:
    if dual.num_rows == 0 or max_lag_ms <= 0 or "es_lag_ms" not in dual.column_names:
        return dual
    lag = np.abs(dual.column("es_lag_ms").to_numpy().astype(np.float64))
    keep = np.where(lag <= max_lag_ms)[0]
    return dual.take(keep) if len(keep) else pa.table({})


def build_events(pair: H.Pair, exp: dict) -> dict[str, pa.Table]:
    """Return dual_q / dual_end / dual_q_lag1500 / nq_only_q / momentum for one session."""
    sync_ms = int(exp.get("sync_ms", 1500))
    max_lag = float(exp.get("max_es_lag_ms", 250))
    dual_stamp = str(exp.get("dual_stamp", "confirm"))

    cfg_q = _sweep_cfg(exp, "qualify")
    cfg_end = _sweep_cfg(exp, "end")

    nq_q = aggressor_sweep_events(pair.nq, cfg_q)
    es_q = aggressor_sweep_events(pair.es, cfg_q) if pair.es.num_rows else pa.table({})
    nq_end = aggressor_sweep_events(pair.nq, cfg_end)
    es_end = aggressor_sweep_events(pair.es, cfg_end) if pair.es.num_rows else pa.table({})

    dual_q_all = cross_confirm_sweeps(nq_q, es_q, sync_ms=sync_ms, stamp=dual_stamp)
    dual_end_all = cross_confirm_sweeps(nq_end, es_end, sync_ms=sync_ms, stamp=dual_stamp)

    dual_q = _filter_lag(dual_q_all, max_lag)
    dual_end = _filter_lag(dual_end_all, max_lag)
    dual_q_lag1500 = dual_q_all  # unfiltered ±sync

    mom = H.confirm_bursts(pair)

    return {
        "dual_q": H._with_session(dual_q, pair.sid) if dual_q.num_rows else dual_q,
        "dual_end": H._with_session(dual_end, pair.sid) if dual_end.num_rows else dual_end,
        "dual_q_lag1500": (
            H._with_session(dual_q_lag1500, pair.sid) if dual_q_lag1500.num_rows else dual_q_lag1500
        ),
        "nq_only_q": H._with_session(nq_q, pair.sid) if nq_q.num_rows else nq_q,
        "momentum": H._with_session(mom, pair.sid) if mom.num_rows else mom,
    }


# ---------------------------------------------------------------- Stage 1

def _empty_summary() -> dict:
    return {
        "status": "no_events",
        "n_events": 0,
        "p_hit_10pts": None,
        "mean_ret_pts_60000ms": None,
    }


def _summarize_month(
    labeled_by_sid: dict[str, pa.Table],
    sids: list[str],
    horizons: list[int],
    targets: list[float],
) -> dict:
    parts = [labeled_by_sid[sid] for sid in sids if sid in labeled_by_sid and labeled_by_sid[sid].num_rows]
    if not parts:
        return _empty_summary()
    return H.summarize(parts, horizons, targets)


def evaluate_stage1_bar(
    by_month: dict[str, dict[str, dict]],
    *,
    train_months: list[int],
    bar_cfg: dict,
) -> dict:
    """Pre-registered Stage-1 bar. All checks required on each train month."""
    min_n = int(bar_cfg.get("min_n_per_month", 100))
    hit_null_pp = float(bar_cfg.get("hit10_vs_null_pp", 0.05))
    hit_nq_pp = float(bar_cfg.get("hit10_vs_nq_only_pp", 0.03))
    checks = {}
    month_ok = True
    for ym in train_months:
        lab = _month_label(int(ym))
        m = by_month.get(lab) or {}
        dual = m.get("dual_q") or _empty_summary()
        null_l = m.get("null_large") or _empty_summary()
        nq_o = m.get("nq_only_q") or _empty_summary()

        n = int(dual.get("n_events") or 0)
        hit_d = dual.get("p_hit_10pts")
        hit_null = null_l.get("p_hit_10pts")
        hit_nq = nq_o.get("p_hit_10pts")
        ret_d = dual.get("mean_ret_pts_60000ms")
        ret_null = null_l.get("mean_ret_pts_60000ms")

        n_ok = n >= min_n
        vs_null = (
            hit_d is not None and hit_null is not None and hit_d >= hit_null + hit_null_pp
        )
        vs_ret = (
            ret_d is not None and ret_null is not None and ret_d > ret_null
        )
        vs_nq = (
            hit_d is not None and hit_nq is not None and hit_d >= hit_nq + hit_nq_pp
        )
        ok = bool(n_ok and vs_null and vs_ret and vs_nq)
        if not ok:
            month_ok = False
        checks[lab] = {
            "pass": ok,
            "n": n,
            "min_n": min_n,
            "hit10_dual": hit_d,
            "hit10_null_large": hit_null,
            "hit10_nq_only": hit_nq,
            "ret60_dual": ret_d,
            "ret60_null_large": ret_null,
            "n_ok": n_ok,
            "vs_null_hit": vs_null,
            "vs_null_ret60": vs_ret,
            "vs_nq_only": vs_nq,
        }
    return {"pass": month_ok, "checks": checks}


def stage1(
    pairs: list[H.Pair],
    ev_all: dict[str, dict[str, pa.Table]],
    *,
    exp: dict,
) -> dict:
    horizons = list(exp.get("horizons_ms") or [10000, 60000])
    targets = list(exp.get("point_targets") or [5, 10, 20])
    null_large_n = int(exp.get("null_large_n_per_session", 200))
    sids = [p.sid for p in pairs]

    sets = ("dual_q", "dual_end", "dual_q_lag1500", "nq_only_q", "momentum", "null_same_n", "null_large")
    labeled: dict[str, dict[str, pa.Table]] = {k: {} for k in sets}

    for p in pairs:
        ev = ev_all[p.sid]
        dual_n = ev["dual_q"].num_rows if ev["dual_q"].num_rows else 0
        salt = sum(ord(c) for c in p.sid) * 17

        for name in ("dual_q", "dual_end", "dual_q_lag1500", "nq_only_q", "momentum"):
            labeled[name][p.sid] = H.label_in_points(ev[name], p, horizons, targets)

        null_same = _random_null_events(p.nq, max(dual_n, 1), seed=42 + salt)
        null_large = _random_null_events(p.nq, null_large_n, seed=99 + salt)
        labeled["null_same_n"][p.sid] = H.label_in_points(null_same, p, horizons, targets)
        labeled["null_large"][p.sid] = H.label_in_points(null_large, p, horizons, targets)

        print(
            f"  [s1] {p.sid} dual_q={dual_n} dual_end={ev['dual_end'].num_rows} "
            f"lag1500={ev['dual_q_lag1500'].num_rows} nq_only={ev['nq_only_q'].num_rows} "
            f"mom={ev['momentum'].num_rows}",
            flush=True,
        )

    # All-train + by-month summaries
    all_sums = {k: _summarize_month(labeled[k], sids, horizons, targets) for k in sets}

    by_month: dict[str, dict[str, dict]] = {}
    months = sorted({_month_label(_sid_month(sid)) for sid in sids})
    for m in months:
        msids = [sid for sid in sids if _month_label(_sid_month(sid)) == m]
        by_month[m] = {
            k: _summarize_month(labeled[k], msids, horizons, targets) for k in sets
        }

    bar = evaluate_stage1_bar(
        by_month,
        train_months=list(exp.get("train_months") or [202609, 202610]),
        bar_cfg=exp.get("stage1_bar") or {},
    )
    print(
        f"[stage1 bar] {'PASS' if bar['pass'] else 'FAIL'}: "
        f"{json.dumps({k: v['pass'] for k, v in bar['checks'].items()})}",
        flush=True,
    )
    for m, c in bar["checks"].items():
        print(
            f"  {m}: n={c['n']} hit10 dual={c['hit10_dual']} null={c['hit10_null_large']} "
            f"nq={c['hit10_nq_only']} ret60 dual={c['ret60_dual']} null={c['ret60_null_large']}",
            flush=True,
        )

    # Concat primary events for parquet
    dual_parts = [
        ev_all[sid]["dual_q"]
        for sid in sids
        if sid in ev_all and ev_all[sid]["dual_q"].num_rows
    ]
    events_table = pa.concat_tables(dual_parts) if dual_parts else pa.table({})

    return {
        "sids": sids,
        "all": all_sums,
        "by_month": by_month,
        "bar": bar,
        "events_table": events_table,
        "labeled": labeled,
    }


# ---------------------------------------------------------------- Stage 2 strata (sweep-specific)

def _hour_uk(ns: int) -> str:
    dt = datetime.fromtimestamp(ns / 1e9, tz=H.UK)
    return f"{dt.hour:02d}:{0 if dt.minute < 30 else 30:02d}"


def _tercile_edges(vals: np.ndarray) -> tuple[float, float] | None:
    v = vals[np.isfinite(vals)]
    if len(v) < 9:
        return None
    return float(np.percentile(v, 33.333)), float(np.percentile(v, 66.666))


def _tercile_label(x: float, edges: tuple[float, float] | None) -> str:
    if edges is None or not np.isfinite(x):
        return "na"
    lo, hi = edges
    if x <= lo:
        return "lo"
    if x <= hi:
        return "mid"
    return "hi"


def sweep_strata(
    pairs: list[H.Pair],
    events_by_sid: dict[str, pa.Table],
    primary_trades: list[TradeResult],
    *,
    sim_cfg: MatSimConfig,
) -> dict:
    """Read-only strata on primary dual_q trades."""
    from research.features.regime import build_regime_index, lookup_regime

    trade_key = {(tr.session_id, tr.entry_ts_ns): tr for tr in primary_trades}
    rows = []
    for p in pairs:
        ev = events_by_sid.get(p.sid)
        if ev is None or ev.num_rows == 0:
            continue
        bars, bar_ts, cum_pv, cum_v, trade_ts, trade_px, trade_sz = build_regime_index(
            p.nq, pts_per_tick=sim_cfg.pts_per_tick
        )
        ev_ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        snaps = lookup_regime(
            bars, bar_ts, cum_pv, cum_v, ev_ts,
            window_min=60, vwap_from_ns=p.vwap_from_ns,
            trade_ts=trade_ts, trade_px=trade_px, trade_sz=trade_sz,
        )
        sides = ev.column("side").to_numpy().astype(int)
        lag = (
            ev.column("es_lag_ms").to_numpy().astype(np.float64)
            if "es_lag_ms" in ev.column_names
            else np.full(ev.num_rows, np.nan)
        )
        span = (
            ev.column("span_ticks").to_numpy().astype(np.float64)
            if "span_ticks" in ev.column_names
            else np.full(ev.num_rows, np.nan)
        )
        n_prints = (
            ev.column("n_prints").to_numpy().astype(np.float64)
            if "n_prints" in ev.column_names
            else np.full(ev.num_rows, np.nan)
        )
        vol = (
            ev.column("total_vol").to_numpy().astype(np.float64)
            if "total_vol" in ev.column_names
            else np.full(ev.num_rows, np.nan)
        )
        for i in range(ev.num_rows):
            tr = trade_key.get((p.sid, int(ev_ts[i])))
            if tr is None:
                continue
            rows.append({
                "side": int(sides[i]),
                "pnl_net": tr.pnl - sim_cfg.commission_pts_rt,
                "es_lag_ms": float(lag[i]),
                "lead": (
                    "es_first" if np.isfinite(lag[i]) and lag[i] < 0
                    else ("nq_first" if np.isfinite(lag[i]) and lag[i] > 0 else "simult")
                ),
                "abs_lag": abs(float(lag[i])) if np.isfinite(lag[i]) else np.nan,
                "span_ge2": "span_ge2" if np.isfinite(span[i]) and span[i] >= 2 else "span_lt2",
                "prints_ge8": "prints_ge8" if np.isfinite(n_prints[i]) and n_prints[i] >= 8 else "prints_lt8",
                "total_vol": float(vol[i]),
                "er60": float(snaps[i].efficiency_ratio) if np.isfinite(snaps[i].efficiency_ratio) else np.nan,
                "hour": _hour_uk(int(ev_ts[i])),
            })

    if not rows:
        return {"n": 0}

    er_edges = _tercile_edges(np.array([r["er60"] for r in rows]))
    lag_edges = _tercile_edges(np.array([r["abs_lag"] for r in rows]))
    vol_edges = _tercile_edges(np.array([r["total_vol"] for r in rows]))

    def _bucket(key_fn) -> dict[str, dict]:
        buckets: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            buckets[key_fn(r)].append(r["pnl_net"])
        out = {}
        for k, vals in sorted(buckets.items()):
            a = np.asarray(vals)
            out[k] = {
                "n": int(len(a)),
                "net": round(float(a.sum()), 2),
                "avg": round(float(a.mean()), 3),
                "wr": round(float((a > 0).mean()), 3),
            }
        return out

    return {
        "n": len(rows),
        "side": _bucket(lambda r: "long" if r["side"] > 0 else "short"),
        "lead": _bucket(lambda r: r["lead"]),
        "abs_lag_tercile": _bucket(lambda r: _tercile_label(r["abs_lag"], lag_edges)),
        "span": _bucket(lambda r: r["span_ge2"]),
        "prints": _bucket(lambda r: r["prints_ge8"]),
        "vol_tercile": _bucket(lambda r: _tercile_label(r["total_vol"], vol_edges)),
        "er60_tercile": _bucket(lambda r: _tercile_label(r["er60"], er_edges)),
        "hour_uk": _bucket(lambda r: r["hour"]),
    }


def stage2(
    pairs: list[H.Pair],
    ev_all: dict[str, dict[str, pa.Table]],
    *,
    exp: dict,
    run_null: bool = True,
    run_strata: bool = True,
) -> dict:
    sids = [p.sid for p in pairs]
    sim_cfg = _sim_cfg(exp)
    brackets = _brackets(exp)
    br = brackets[exp.get("primary_bracket", "vol_k1")]

    dual_map = {sid: ev_all[sid]["dual_q"] for sid in sids if sid in ev_all}
    nq_map = {sid: ev_all[sid]["nq_only_q"] for sid in sids if sid in ev_all}
    mom_map = {sid: ev_all[sid]["momentum"] for sid in sids if sid in ev_all}
    n_by_sid = {sid: (dual_map[sid].num_rows if dual_map.get(sid) is not None else 0) for sid in sids}

    print("[s2] dual_q (primary) …", flush=True)
    dual_s = run_book(pairs, dual_map, sim_cfg=sim_cfg, bracket_fn=br)
    print("[s2] nq_only …", flush=True)
    nq_s = run_book(pairs, nq_map, sim_cfg=sim_cfg, bracket_fn=br)
    print("[s2] momentum-only …", flush=True)
    mom_s = run_book(pairs, mom_map, sim_cfg=sim_cfg, bracket_fn=br)
    print("[s2] MAT frozen base …", flush=True)
    mat_cfg = MatSimConfig(
        slip_ticks=sim_cfg.slip_ticks,
        commission_pts_rt=sim_cfg.commission_pts_rt,
        signal_cooldown_sec=sim_cfg.signal_cooldown_sec,
        flat_only=sim_cfg.flat_only,
        es_stamp="causal",
    )
    mat_gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    mat_s = run_book(pairs, {}, sim_cfg=mat_cfg, bracket_fn=br, gate=mat_gate, detect=True)

    books = {
        "dual_q": _pack(dual_s, sids),
        "nq_only_q": _pack(nq_s, sids),
        "momentum_only": _pack(mom_s, sids),
        "mat_base": _pack(mat_s, sids),
    }
    for name, r in books.items():
        print(
            f"  {name:16s} n={r['n']:4d} net={r['net']:+8.1f} avg={r.get('avg')} "
            f"days+={r['days_pos']}/{r['n_sessions']} maxDD={r['max_dd']:.0f}",
            flush=True,
        )

    null_dist = {"n_seeds": 0, "mean": 0, "p90": 0, "p10": 0, "p50": 0, "min": 0, "max": 0, "nets": []}
    if run_null:
        print(f"[s2 null] {int(exp.get('null_seeds', 50))} seeds …", flush=True)
        null_dist = null_distribution(
            pairs, n_by_sid, sim_cfg=sim_cfg, bracket_fn=br,
            n_seeds=int(exp.get("null_seeds", 50)),
        )
        print(f"  null mean={null_dist['mean']:+.1f} p90={null_dist['p90']:+.1f}", flush=True)

    conc = concentration(dual_s.trades, sids)
    print(f"[s2 conc] top3_share={conc.get('top3_share_of_pos')} loo_neg={conc['loo_neg']}", flush=True)

    secondary = {}
    for bname in exp.get("secondary_brackets") or []:
        if bname == exp.get("primary_bracket", "vol_k1"):
            continue
        print(f"[s2] secondary {bname} …", flush=True)
        br2 = brackets.get(bname)
        cfg_b = sim_cfg
        if bname == "fixed_10_10":
            cfg_b = MatSimConfig(**{**sim_cfg.__dict__, "tp_pts": 10.0, "sl_pts": 10.0})
        s = run_book(pairs, dual_map, sim_cfg=cfg_b, bracket_fn=br2)
        secondary[f"dual_q_{bname}"] = _pack(s, sids)

    strata_rep = {"n": 0}
    if run_strata:
        print("[s2 strata] …", flush=True)
        strata_rep = sweep_strata(pairs, dual_map, dual_s.trades, sim_cfg=sim_cfg)

    bar = evaluate_bar(
        books["dual_q"],
        null_dist,
        books["momentum_only"],
        conc,
        bar_cfg=exp.get("stage2_bar") or {},
        train_months=list(exp.get("train_months") or [202609, 202610]),
    )
    print(
        f"[stage2 bar] {'PASS' if bar['pass'] else 'FAIL'}: "
        f"{json.dumps({k: v['pass'] for k, v in bar['checks'].items()})}",
        flush=True,
    )

    return {
        "sids": sids,
        "books": books,
        "null_dist": null_dist,
        "conc": conc,
        "secondary": secondary,
        "strata": strata_rep,
        "bar": bar,
        "trades": dual_s.trades,
    }


# ---------------------------------------------------------------- report

def _fmt_event_row(name: str, s: dict) -> str:
    if not s or not s.get("n_events"):
        return f"| {name} | 0 | – | – | – | – | – |"
    return (
        f"| {name} | {s['n_events']} | "
        f"{s.get('p_hit_5pts', float('nan')):.2f} | "
        f"{s.get('p_hit_10pts', float('nan')):.2f} | "
        f"{s.get('p_hit_20pts', float('nan')):.2f} | "
        f"{_fmt(s.get('mean_ret_pts_10000ms'))} | "
        f"{_fmt(s.get('mean_ret_pts_60000ms'))} |"
    )


def _fmt(x, nd=2) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "–"
    return f"{x:+.{nd}f}"


def _fmt_pnl_row(name: str, r: dict) -> str:
    avg = r.get("avg")
    avg_s = f"{avg:+.2f}" if avg is not None else "–"
    return (
        f"| {name} | {r.get('n', 0)} | {r.get('wr') if r.get('wr') is not None else '–'} | "
        f"{r.get('gross', 0):+.1f} | {r.get('net', 0):+.1f} | {avg_s} | "
        f"{r.get('days_pos', 0)}/{r.get('n_sessions', 0)} | {r.get('max_dd', 0):.0f} | "
        f"{r.get('worst_day', 0):+.1f} |"
    )


def write_report(
    out: Path,
    *,
    exp: dict,
    s1: dict,
    s2: dict | None,
    show_s1: dict | None,
    may_s2: dict | None,
    forced_may_note: str | None = None,
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    power = (exp.get("stage2_bar") or {}).get("mde_note") or exp.get("power_note") or ""

    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        exp.get("description", "").strip(),
        "",
    ]
    if forced_may_note:
        md += [f"> **{forced_may_note}**", ""]
    md += [
        f"Sessions={len(s1['sids'])} · UK 14:30–17:00 · sweep_stamp=qualify · "
        f"dual_stamp=confirm · |es_lag|≤{exp.get('max_es_lag_ms', 250)} ms.",
        "",
        f"**Power note:** {power}",
        "",
        "## Stage 1 — points event study",
        "",
        f"**Bar: {'PASS' if s1['bar']['pass'] else 'FAIL'}** — "
        + (
            "Stage 2 may run."
            if s1["bar"]["pass"]
            else (
                "forced May look — see Decision."
                if forced_may_note
                else "reject/park H12; no Stage 2; no May; no threshold neighbours."
            )
        ),
        "",
        "| month | pass | n | hit10 dual | null_large | nq_only | @60s dual | @60s null |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for m, c in s1["bar"]["checks"].items():
        md.append(
            f"| {m} | {'yes' if c['pass'] else 'no'} | {c['n']} | "
            f"{_fmt(c['hit10_dual'], 3)} | {_fmt(c['hit10_null_large'], 3)} | "
            f"{_fmt(c['hit10_nq_only'], 3)} | {_fmt(c['ret60_dual'])} | "
            f"{_fmt(c['ret60_null_large'])} |"
        )

    md += [
        "",
        "### All train",
        "",
        "| set | n | hit5 | hit10 | hit20 | @10s | @60s |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("dual_q", "dual_end", "dual_q_lag1500", "nq_only_q", "null_same_n", "null_large", "momentum"):
        md.append(_fmt_event_row(name, s1["all"].get(name) or {}))

    md += ["", "### By month", ""]
    for m, sets in s1["by_month"].items():
        md += [
            f"**{m}**",
            "",
            "| set | n | hit5 | hit10 | hit20 | @10s | @60s |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for name in ("dual_q", "dual_end", "nq_only_q", "null_large", "momentum"):
            md.append(_fmt_event_row(name, sets.get(name) or {}))
        md.append("")

    if show_s1:
        md += ["", "## Mar/Aug event study (shown; not in bar)", ""]
        md += [
            "| set | n | hit5 | hit10 | hit20 | @10s | @60s |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for name in ("dual_q", "nq_only_q", "null_large", "momentum"):
            md.append(_fmt_event_row(name, show_s1["all"].get(name) or {}))

    if s2 is None:
        md += [
            "",
            "## Stage 2 — P&L",
            "",
            "Skipped (Stage 1 bar failed).",
            "",
            "## Decision",
            "",
            "Stage-1 bar **FAIL** — **reject / park H12**. Dual aggressor sweeps do not "
            "clear the corrected points event bar on Sep+Oct. No Stage 2, no May, "
            "no threshold neighbours.",
            "",
        ]
    else:
        md += [
            "",
            "## Stage 2 — standalone vol k=1 P&L",
            "",
            f"**Bar: {'PASS' if s2['bar']['pass'] else 'FAIL'}**",
            "",
            "| check | pass | detail |",
            "|---|---|---|",
        ]
        for name, c in s2["bar"]["checks"].items():
            detail = {k: v for k, v in c.items() if k != "pass"}
            md.append(f"| {name} | {'yes' if c['pass'] else 'no'} | `{json.dumps(detail, default=str)}` |")

        md += [
            "",
            "| variant | n | WR | gross | net | avg | days+ | maxDD | worst |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for name, r in s2["books"].items():
            md.append(_fmt_pnl_row(name, r))

        md += [
            "",
            "### By month (dual_q)",
            "",
            "| month | n | net | avg | days+ | days |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for m, d in (s2["books"]["dual_q"].get("by_month") or {}).items():
            md.append(
                f"| {m} | {d['n']} | {d['net']:+.1f} | "
                f"{d['avg'] if d['avg'] is not None else '–'} | {d['days_pos']}/{d['days']} |"
            )

        nd = s2["null_dist"]
        md += [
            "",
            f"Null (50 seeds): mean={nd.get('mean', 0):+.1f} p10={nd.get('p10', 0):+.1f} "
            f"p50={nd.get('p50', 0):+.1f} **p90={nd.get('p90', 0):+.1f}**",
            "",
            f"Concentration: days+={s2['conc']['days_pos']} · top3 share={s2['conc'].get('top3_share_of_pos')} · "
            f"LOO-neg={s2['conc']['loo_neg']}",
            "",
        ]

        if s2.get("secondary"):
            md += ["", "### Secondary brackets (informational)", ""]
            md += [
                "| variant | n | WR | gross | net | avg | days+ | maxDD | worst |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
            for name, r in s2["secondary"].items():
                md.append(_fmt_pnl_row(name, r))

        if s2.get("strata") and s2["strata"].get("n", 0) > 0:
            md += ["", "### Strata (informational — never selected)", ""]
            for sname in (
                "side", "lead", "abs_lag_tercile", "span", "prints",
                "vol_tercile", "er60_tercile", "hour_uk",
            ):
                bucket = s2["strata"].get(sname) or {}
                md += [f"**{sname}**", "", "| cell | n | net | avg | WR |", "|---|---:|---:|---:|---:|"]
                for k, v in bucket.items():
                    md.append(f"| {k} | {v['n']} | {v['net']:+.1f} | {v['avg']:+.3f} | {v['wr']:.3f} |")
                md.append("")

        if may_s2:
            md += ["", "## May 2026 OOS (read once)", ""]
            md += [
                "| variant | n | WR | gross | net | avg | days+ | maxDD | worst |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
            for name, r in may_s2["books"].items():
                md.append(_fmt_pnl_row(name, r))

        if forced_may_note:
            decision = (
                f"{forced_may_note} Stage-1 {'PASS' if s1['bar']['pass'] else 'FAIL'}; "
                f"Stage-2 {'PASS' if s2['bar']['pass'] else 'FAIL'}. "
                "Ship status unchanged from train: **reject / park** unless a new "
                "pre-registered experiment is opened."
            )
        elif s1["bar"]["pass"] and s2["bar"]["pass"]:
            decision = "Both bars **PASS** — soft confirm / forward paper. Do not retune thresholds."
        elif s1["bar"]["pass"]:
            decision = (
                "Stage-1 PASS but Stage-2 bar **FAIL** — **reject / park** as a P&L book. "
                "Event signal may exist but does not clear the MAT vol k=1 yardstick. "
                "No May (unless already forced). No threshold neighbours."
            )
        else:
            decision = (
                "Stage-1 bar **FAIL** on this cohort — **reject / park**. No threshold neighbours."
            )
        md += ["", "## Decision", "", decision, ""]

    report = {
        "experiment": exp["name"],
        "title": exp.get("title"),
        "sessions": s1["sids"],
        "stage1": {
            "bar": s1["bar"],
            "all": s1["all"],
            "by_month": s1["by_month"],
        },
        "stage2": (
            {
                "bar": s2["bar"],
                "books": {k: {kk: vv for kk, vv in v.items() if kk != "by_reason"} for k, v in s2["books"].items()},
                "null": {k: v for k, v in s2["null_dist"].items() if k != "nets"},
                "concentration": {k: v for k, v in s2["conc"].items() if k != "loo"},
                "strata": s2["strata"],
                "secondary": s2["secondary"],
            }
            if s2 else None
        ),
        "show_stage1": show_s1["all"] if show_s1 else None,
        "may": (
            {k: {kk: vv for kk, vv in v.items() if kk != "by_reason"} for k, v in may_s2["books"].items()}
            if may_s2 else None
        ),
        "forced_may_note": forced_may_note,
        "power_note": power,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    if s1["events_table"].num_rows:
        pq.write_table(s1["events_table"], out / "events.parquet")
    if s2 and s2["trades"]:
        trades = s2["trades"]
        cols = {
            "session_id": [t.session_id for t in trades],
            "entry_ts_ns": np.asarray([t.entry_ts_ns for t in trades], dtype=np.int64),
            "exit_ts_ns": np.asarray([t.exit_ts_ns for t in trades], dtype=np.int64),
            "side": np.asarray([t.side for t in trades], dtype=np.int8),
            "entry_px": np.asarray([t.entry_px for t in trades], dtype=np.float64),
            "exit_px": np.asarray([t.exit_px for t in trades], dtype=np.float64),
            "pnl": np.asarray([t.pnl for t in trades], dtype=np.float64),
            "win": np.asarray([int(t.win) for t in trades], dtype=np.int8),
            "reason": [t.reason for t in trades],
            "tp_pts": np.asarray([t.tp_pts for t in trades], dtype=np.float64),
            "sl_pts": np.asarray([t.sl_pts for t in trades], dtype=np.float64),
        }
        pq.write_table(pa.table(cols), out / "trades.parquet")
    print(f"wrote {out / 'summary.md'}", flush=True)


# ---------------------------------------------------------------- main

def _build_all(pairs: list[H.Pair], exp: dict) -> dict[str, dict[str, pa.Table]]:
    out = {}
    for p in pairs:
        print(f"[events] {p.sid} …", flush=True)
        out[p.sid] = build_events(p, exp)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="EXP-0017_REOPEN.yaml")
    ap.add_argument("--out", default=None)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--stage1-only", action="store_true")
    ap.add_argument("--may", action="store_true")
    ap.add_argument("--force-may", action="store_true")
    ap.add_argument(
        "--may-only",
        action="store_true",
        help="Forced May OOS only (Stage 1 event + Stage 2 P&L). Use after train FAIL; no retune.",
    )
    args = ap.parse_args()

    cfg = load_config()
    exp = _load_exp(args.config)
    parquet_dir = Path(cfg["parquet_dir"])
    out_name = args.out or exp["name"]
    out = Path(cfg["results_dir"]) / out_name

    train_months = list(exp.get("train_months") or [202609, 202610])
    show_months = list(exp.get("show_months") or [202603, 202608])
    oos_month = int(exp.get("oos_month") or 202605)

    train_sids = _sessions_for_months(parquet_dir, train_months)
    show_sids = _sessions_for_months(parquet_dir, show_months)
    may_sids = _sessions_for_months(parquet_dir, [oos_month])

    # --- Forced May-only OOS (after train FAIL; frozen params; no retune) ---
    if args.may_only:
        if not may_sids:
            raise SystemExit(f"no May sessions for month {oos_month}")
        out = Path(cfg["results_dir"]) / (args.out or f"{out_name}_MAY")
        print(f"[may-only] FORCED OOS after train FAIL — frozen params; sessions={may_sids}", flush=True)
        may_pairs = _load_pairs(parquet_dir, may_sids)
        may_ev = _build_all(may_pairs, exp)
        may_exp = {**exp, "train_months": [oos_month]}
        print("[may stage1] …", flush=True)
        s1 = stage1(may_pairs, may_ev, exp=may_exp)
        print("[may stage2] …", flush=True)
        s2 = stage2(may_pairs, may_ev, exp=may_exp, run_null=True, run_strata=True)
        write_report(
            out, exp=exp, s1=s1, s2=s2, show_s1=None, may_s2=None,
            forced_may_note=(
                "FORCED May OOS read after Sep+Oct Stage-1 FAIL. Frozen params; "
                "do not retune. Verdict stays reject/park unless both stages clear "
                "on this untouched month *and* a pre-registered follow-up is opened."
            ),
        )
        print(
            f"[done] may-only stage1={'PASS' if s1['bar']['pass'] else 'FAIL'} "
            f"stage2={'PASS' if s2['bar']['pass'] else 'FAIL'} → {out}",
            flush=True,
        )
        return

    if args.smoke:
        train_sids = train_sids[:2]
        show_sids = []
        exp = {**exp, "null_seeds": 3, "null_large_n_per_session": 50}
        out = Path(cfg["results_dir"]) / f"{out_name}_SMOKE"
        print(f"[smoke] sessions={train_sids}", flush=True)

    print(f"[load] train={train_sids}", flush=True)
    train_pairs = _load_pairs(parquet_dir, train_sids)
    train_ev = _build_all(train_pairs, exp)

    print("[stage1] …", flush=True)
    s1 = stage1(train_pairs, train_ev, exp=exp)

    show_s1 = None
    if show_sids and not args.smoke:
        print(f"[show] {show_sids}", flush=True)
        show_pairs = _load_pairs(parquet_dir, show_sids)
        show_ev = _build_all(show_pairs, exp)
        show_s1 = stage1(show_pairs, show_ev, exp={**exp, "train_months": show_months})

    s2 = None
    may_s2 = None
    if args.stage1_only:
        print("[stage2] skipped (--stage1-only)", flush=True)
    elif not s1["bar"]["pass"]:
        print("[stage2] skipped (Stage 1 FAIL)", flush=True)
    else:
        print("[stage2] …", flush=True)
        s2 = stage2(
            train_pairs, train_ev, exp=exp,
            run_null=not args.smoke, run_strata=not args.smoke,
        )
        if (args.may or args.force_may) and may_sids:
            if not s2["bar"]["pass"] and not args.force_may:
                print("[may] Stage-2 bar FAILED — refusing May (use --force-may)", flush=True)
            else:
                print(f"[may] {may_sids}", flush=True)
                may_pairs = _load_pairs(parquet_dir, may_sids)
                may_ev = _build_all(may_pairs, exp)
                may_s2 = stage2(
                    may_pairs, may_ev, exp=exp, run_null=False, run_strata=False,
                )

    write_report(out, exp=exp, s1=s1, s2=s2, show_s1=show_s1, may_s2=may_s2)
    print(
        f"[done] stage1={'PASS' if s1['bar']['pass'] else 'FAIL'} "
        f"stage2={'PASS' if s2 and s2['bar']['pass'] else ('FAIL' if s2 else 'skip')} "
        f"→ {out}",
        flush=True,
    )


if __name__ == "__main__":
    main()
