"""EXP-0014_PNL — standalone CD-pullback × ES+NQ momentum vol k=1 P&L book.

Frozen event construction from EXP-0014.yaml (no retune). Entries = confirmed
CD-pullback ∩ same-side ES+NQ momentum within confirm_window_ms, stamped at
max(pullback, burst). Sim = MAT-sim flat-only / 5 s cooldown / 1-tick slip /
0.25 pt/RT / vol k=1, **no gate**.

Train = Sep + Oct; Mar/Aug shown; May loaded only with --may after the
pre-registered bar passes.

Usage:
  python -m research.run_cd_pullback_pnl --smoke
  python -m research.run_cd_pullback_pnl
  python -m research.run_cd_pullback_pnl --may
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
    SimSummary,
    TradeResult,
    combine_gates,
    detect_signals,
    filter_summary,
    make_trend_mode,
    make_vol_bracket,
    make_vol_bracket_rr,
    make_vwap_side,
    merge_summaries,
    simulate_session,
)
from research.config import load_config
from research.correlated import harness as H
from research.features.cd import (
    build_cd_series,
    cd_pullback_events,
    filter_events_near_bursts,
    lookup_cd_at_events,
)
from research.features.momentum import (
    cross_confirm_bursts,
    momentum_burst_events,
    trades_to_tape_bars,
)
from research.run_experiment import _random_null_events

COMMISSION = H.COMMISSION
EXP_DIR = Path(__file__).parent / "experiments"


# ---------------------------------------------------------------- helpers

def _sid_month(sid: str) -> int:
    """DDMMYY → YYYYMM (assume 20YY)."""
    mm = int(sid[2:4])
    yy = int(sid[4:6])
    return 2000 * 100 + yy * 100 + mm


def _month_label(ym: int) -> str:
    return f"{ym // 100}-{ym % 100:02d}"


def _load_exp(name: str = "EXP-0014_PNL.yaml") -> dict:
    path = EXP_DIR / name
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _sessions_for_months(parquet_dir: Path, months: list[int]) -> list[str]:
    want = set(int(m) for m in months)
    out = []
    for sid in H.list_sessions(parquet_dir):
        if _sid_month(sid) in want:
            out.append(sid)
    return out


def _sim_cfg(exp: dict) -> MatSimConfig:
    return MatSimConfig(
        slip_ticks=float(exp.get("slip_ticks", 1.0)),
        commission_pts_rt=float(exp.get("commission_pts_rt", COMMISSION)),
        signal_cooldown_sec=float(exp.get("signal_cooldown_sec", 5.0)),
        flat_only=bool(exp.get("flat_only", True)),
        # event-driven books supply events=; es_stamp unused then
        es_stamp="confirm",
    )


def _brackets(exp: dict) -> dict:
    """Name → bracket_fn (or None for fixed 10/10 via MatSimConfig defaults)."""
    out = {
        "vol_k1": make_vol_bracket(1.0),
        "vol_rr_1_2": make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0),
        "fixed_10_10": None,
    }
    primary = exp.get("primary_bracket", "vol_k1")
    if primary not in out:
        raise ValueError(f"unknown primary_bracket: {primary}")
    return out


# ---------------------------------------------------------------- events

def build_events(pair: H.Pair, exp: dict) -> dict[str, pa.Table]:
    """Return {confirmed, pullback, momentum} for one session.

    Confirmed = CD pullback with a same-side ES+NQ momentum burst within
    confirm_window_ms (causal stamp at max(pb, burst)).
    """
    tick_size = float(exp.get("tick_size", 1.0))
    bar_ms = int(exp.get("bar_ms", 100))
    sync_ms = int(exp.get("sync_ms", 1500))
    confirm_ms = int(exp.get("confirm_window_ms", 2000))
    burst_kw = dict(
        min_trades=int(exp.get("min_trades", 6)),
        min_vol=float(exp.get("min_vol", 25)),
        min_imbalance=float(exp.get("min_imbalance", 0.55)),
        min_progress_ticks=float(exp.get("min_progress_ticks", 1.0)),
        tick_size=tick_size,
        cooldown_bars=int(exp.get("cooldown_bars", 3)),
        bar_ms=bar_ms,
    )

    cd = build_cd_series(
        pair.nq,
        range_ticks=int(exp.get("range_ticks", 22)),
        tick_size=tick_size,
        sma_len=int(exp.get("sma_len", 100)),
        slope_lookback=int(exp.get("slope_lookback", 5)),
        pullback_frac_of_bb=float(exp.get("pullback_frac_of_bb", 0.35)),
    )
    pb = cd_pullback_events(cd, cooldown_bars=int(exp.get("cd_cooldown_bars", 3)))

    nq_bars = trades_to_tape_bars(pair.nq, bar_ms=bar_ms)
    es_bars = trades_to_tape_bars(pair.es, bar_ms=bar_ms) if pair.es.num_rows else pa.table({})
    nq_burst = momentum_burst_events(nq_bars, **burst_kw)
    es_burst = momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({})
    mom = cross_confirm_bursts(nq_burst, es_burst, sync_ms=sync_ms)  # default confirm stamp

    confirmed = (
        filter_events_near_bursts(pb, mom, window_ms=confirm_ms, require_same_side=True, causal=True)
        if pb.num_rows and mom.num_rows
        else pa.table({})
    )

    # Annotate confirmed with CD fields at event time (for strata).
    if confirmed.num_rows and cd.num_rows:
        ev_ts = confirmed.column("event_ts_ns").to_numpy().astype(np.int64)
        fields = lookup_cd_at_events(cd, ev_ts)
        for name, arr in fields.items():
            if name in confirmed.column_names:
                continue
            # lookup returns float64 with NaN for missing; cast ints carefully
            if name in ("cd_ma_trend", "cd_near_ma", "cd_pullback_long", "cd_pullback_short", "cd_bb_regime"):
                confirmed = confirmed.append_column(
                    name, pa.array(np.nan_to_num(arr, nan=0).astype(np.int8), type=pa.int8())
                )
            else:
                confirmed = confirmed.append_column(name, pa.array(arr, type=pa.float64()))

    return {
        "confirmed": H._with_session(confirmed, pair.sid) if confirmed.num_rows else confirmed,
        "pullback": H._with_session(pb, pair.sid) if pb.num_rows else pb,
        "momentum": H._with_session(mom, pair.sid) if mom.num_rows else mom,
        "cd": cd,
    }


# ---------------------------------------------------------------- sim books

def run_book(
    pairs: list[H.Pair],
    events_by_sid: dict[str, pa.Table],
    *,
    sim_cfg: MatSimConfig,
    bracket_fn,
    gate=None,
    detect: bool = False,
) -> SimSummary:
    """Simulate each session. If detect=True, ignore events_by_sid and use detect_signals."""
    parts: list[SimSummary] = []
    for p in pairs:
        if detect:
            s = simulate_session(
                p.sid, p.nq, p.es, sim_cfg,
                gate=gate, bracket_fn=bracket_fn, vwap_from_ns=p.vwap_from_ns,
            )
        else:
            ev = events_by_sid.get(p.sid)
            if ev is None or ev.num_rows == 0:
                parts.append(SimSummary())
                continue
            s = simulate_session(
                p.sid, p.nq, p.es, sim_cfg,
                gate=gate, bracket_fn=bracket_fn, vwap_from_ns=p.vwap_from_ns,
                events=ev,
            )
        parts.append(s)
    return merge_summaries(parts, sim_cfg.commission_pts_rt) if parts else SimSummary()


def null_distribution(
    pairs: list[H.Pair],
    n_by_sid: dict[str, int],
    *,
    sim_cfg: MatSimConfig,
    bracket_fn,
    n_seeds: int = 50,
) -> dict:
    nets = []
    for seed in range(n_seeds):
        ev_map = {}
        for p in pairs:
            n = int(n_by_sid.get(p.sid, 0))
            if n <= 0:
                ev_map[p.sid] = pa.table({})
                continue
            # Stable per-session salt (avoid PYTHONHASHSEED nondeterminism).
            salt = sum(ord(c) for c in p.sid) * 17
            ev_map[p.sid] = _random_null_events(p.nq, n, seed=seed + salt)
        s = run_book(pairs, ev_map, sim_cfg=sim_cfg, bracket_fn=bracket_fn)
        nets.append(float(s.net))
    arr = np.asarray(nets, dtype=np.float64)
    return {
        "n_seeds": n_seeds,
        "mean": round(float(arr.mean()), 2),
        "std": round(float(arr.std()), 2),
        "p10": round(float(np.percentile(arr, 10)), 2),
        "p50": round(float(np.percentile(arr, 50)), 2),
        "p90": round(float(np.percentile(arr, 90)), 2),
        "min": round(float(arr.min()), 2),
        "max": round(float(arr.max()), 2),
        "nets": [round(float(x), 2) for x in nets],
    }


def concentration(trades: list[TradeResult], sids: list[str], commission: float = COMMISSION) -> dict:
    by = defaultdict(float)
    n_by = defaultdict(int)
    for tr in trades:
        by[tr.session_id] += tr.pnl - commission
        n_by[tr.session_id] += 1
    day = [
        {
            "session_id": sid,
            "cohort": H.cohort(sid),
            "month": _month_label(_sid_month(sid)),
            "n": int(n_by.get(sid, 0)),
            "net": round(by.get(sid, 0.0), 2),
        }
        for sid in sids
    ]
    nets = np.array([d["net"] for d in day], dtype=np.float64)
    pos = nets[nets > 0]
    top3 = sorted(day, key=lambda d: d["net"], reverse=True)[:3]
    bot3 = sorted(day, key=lambda d: d["net"])[:3]
    total_pos = float(pos.sum()) if len(pos) else 0.0
    total_net = float(nets.sum())

    # Leave-one-session-out: count drops where remaining train net < 0
    loo_neg = 0
    loo_nets = []
    for i, sid in enumerate(sids):
        rem = total_net - float(by.get(sid, 0.0))
        loo_nets.append({"drop": sid, "remaining_net": round(rem, 2)})
        if rem < 0:
            loo_neg += 1

    return {
        "days_pos": int((nets > 0).sum()),
        "days_neg": int((nets < 0).sum()),
        "days_flat": int((nets == 0).sum()),
        "top3": top3,
        "worst3": bot3,
        "top3_net": round(sum(d["net"] for d in top3), 2),
        "top3_share_of_pos": round(sum(d["net"] for d in top3) / total_pos, 3) if total_pos > 0 else None,
        "worst3_net": round(sum(d["net"] for d in bot3), 2),
        "loo_neg": loo_neg,
        "loo": loo_nets,
        "per_session": day,
    }


def _month_nets(trades: list[TradeResult], sids: list[str], commission: float = COMMISSION) -> dict[str, dict]:
    by_m: dict[str, float] = defaultdict(float)
    n_m: dict[str, int] = defaultdict(int)
    days_m: dict[str, set[str]] = defaultdict(set)
    days_pos_m: dict[str, set[str]] = defaultdict(set)
    day_net: dict[str, float] = defaultdict(float)
    for tr in trades:
        day_net[tr.session_id] += tr.pnl - commission
        n_m[_month_label(_sid_month(tr.session_id))] += 1
    for sid in sids:
        m = _month_label(_sid_month(sid))
        days_m[m].add(sid)
        dn = day_net.get(sid, 0.0)
        by_m[m] += dn
        if dn > 0:
            days_pos_m[m].add(sid)
    out = {}
    for m in sorted(days_m):
        out[m] = {
            "n": int(n_m.get(m, 0)),
            "net": round(by_m.get(m, 0.0), 2),
            "days": len(days_m[m]),
            "days_pos": len(days_pos_m.get(m, set())),
            "avg": round(by_m.get(m, 0.0) / n_m[m], 3) if n_m.get(m) else None,
        }
    return out


def _avg(s: SimSummary) -> float | None:
    return round(s.net / s.n, 3) if s.n else None


def _pack(s: SimSummary, sids: list[str], commission: float = COMMISSION) -> dict:
    r = H.pack_pnl(s, sids, commission)
    r["avg"] = _avg(s)
    r["by_month"] = _month_nets(s.trades, sids, commission)
    return r


# ---------------------------------------------------------------- strata (read-only)

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


def strata(
    pairs: list[H.Pair],
    events_by_sid: dict[str, pa.Table],
    primary: SimSummary,
    *,
    sim_cfg: MatSimConfig,
    bracket_fn,
) -> dict:
    """Join primary trades back to events and report stratum P&L (informational)."""
    # Build lookup: (sid, entry_ts) → event row fields
    trade_key = {(tr.session_id, tr.entry_ts_ns): tr for tr in primary.trades}
    rows = []
    for p in pairs:
        ev = events_by_sid.get(p.sid)
        if ev is None or ev.num_rows == 0:
            continue
        # regime at events for ER60
        from research.features.regime import build_regime_index, lookup_regime

        bars, bar_ts, cum_pv, cum_v, trade_ts, trade_px, trade_sz = build_regime_index(
            p.nq, pts_per_tick=sim_cfg.pts_per_tick
        )
        ev_ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
        snaps = lookup_regime(
            bars, bar_ts, cum_pv, cum_v, ev_ts,
            window_min=60,
            vwap_from_ns=p.vwap_from_ns,
            trade_ts=trade_ts, trade_px=trade_px, trade_sz=trade_sz,
        )
        snaps_t = lookup_regime(
            bars, bar_ts, cum_pv, cum_v, ev_ts,
            window_min=30,
            vwap_from_ns=p.vwap_from_ns,
            trade_ts=trade_ts, trade_px=trade_px, trade_sz=trade_sz,
        )
        sides = ev.column("side").to_numpy().astype(int)
        lag = (
            ev.column("burst_lag_ms").to_numpy().astype(np.float64)
            if "burst_lag_ms" in ev.column_names
            else np.full(ev.num_rows, np.nan)
        )
        bb_reg = (
            ev.column("cd_bb_regime").to_numpy().astype(np.float64)
            if "cd_bb_regime" in ev.column_names
            else np.full(ev.num_rows, np.nan)
        )
        for i in range(ev.num_rows):
            tr = trade_key.get((p.sid, int(ev_ts[i])))
            if tr is None:
                continue
            sr, st = snaps[i], snaps_t[i]
            side = int(sides[i])
            trend_ok = (side > 0 and st.net_pts > 0) or (side < 0 and st.net_pts < 0)
            vwap_ok = (
                (side > 0 and sr.last_px >= st.vwap)
                or (side < 0 and sr.last_px <= st.vwap)
                if np.isfinite(st.vwap)
                else False
            )
            rows.append({
                "session_id": p.sid,
                "entry_ts_ns": int(ev_ts[i]),
                "side": side,
                "pnl_net": tr.pnl - sim_cfg.commission_pts_rt,
                "er60": float(sr.efficiency_ratio) if np.isfinite(sr.efficiency_ratio) else np.nan,
                "hour": _hour_uk(int(ev_ts[i])),
                "burst_lag_ms": float(lag[i]),
                "cd_bb_regime": int(bb_reg[i]) if np.isfinite(bb_reg[i]) else 0,
                "trend_vwap": "aligned" if (trend_ok and vwap_ok) else "not",
            })

    if not rows:
        return {"n": 0}

    er_edges = _tercile_edges(np.array([r["er60"] for r in rows]))
    lag_edges = _tercile_edges(np.array([r["burst_lag_ms"] for r in rows]))

    def _bucket_pnl(key_fn) -> dict[str, dict]:
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
        "side": _bucket_pnl(lambda r: "long" if r["side"] > 0 else "short"),
        "er60_tercile": _bucket_pnl(lambda r: _tercile_label(r["er60"], er_edges)),
        "hour_uk": _bucket_pnl(lambda r: r["hour"]),
        "burst_lag_tercile": _bucket_pnl(lambda r: _tercile_label(r["burst_lag_ms"], lag_edges)),
        "cd_bb_regime": _bucket_pnl(
            lambda r: {1: "wide", -1: "narrow", 0: "mid"}.get(r["cd_bb_regime"], "mid")
        ),
        "trend_vwap": _bucket_pnl(lambda r: r["trend_vwap"]),
        "er_edges": list(er_edges) if er_edges else None,
        "lag_edges": list(lag_edges) if lag_edges else None,
    }


# ---------------------------------------------------------------- bar

def evaluate_bar(
    primary: dict,
    null_dist: dict,
    mom: dict,
    conc: dict,
    *,
    bar_cfg: dict,
    train_months: list[int],
) -> dict:
    """Pre-registered bar. All five checks required."""
    checks = {}
    by_m = primary.get("by_month") or {}
    # (1) net > 0 in each train month
    month_ok = True
    month_detail = {}
    for ym in train_months:
        lab = _month_label(int(ym))
        net = (by_m.get(lab) or {}).get("net")
        month_detail[lab] = net
        if net is None or net <= 0:
            month_ok = False
    checks["month_nets_pos"] = {"pass": month_ok, "detail": month_detail}

    # (2) train net ≥ p90 of null
    train_net = primary.get("net", 0.0)
    p90 = null_dist.get("p90")
    checks["vs_null_p90"] = {
        "pass": p90 is not None and train_net >= p90,
        "train_net": train_net,
        "null_p90": p90,
    }

    # (3) avg/trade ≥ momentum-only parent
    avg_p = primary.get("avg")
    avg_m = mom.get("avg")
    checks["avg_ge_momentum"] = {
        "pass": avg_p is not None and avg_m is not None and avg_p >= avg_m,
        "primary_avg": avg_p,
        "momentum_avg": avg_m,
    }

    # (4) n_train ≥ min
    min_n = int(bar_cfg.get("min_n_train", 100))
    checks["n_train"] = {
        "pass": primary.get("n", 0) >= min_n,
        "n": primary.get("n", 0),
        "min": min_n,
    }

    # (5) top3 share ≤ max AND loo_neg ≤ max
    max_share = float(bar_cfg.get("max_top3_share_of_pos", 0.55))
    max_loo = int(bar_cfg.get("max_loo_neg", 2))
    share = conc.get("top3_share_of_pos")
    loo_neg = conc.get("loo_neg", 99)
    share_ok = share is not None and share <= max_share
    loo_ok = loo_neg <= max_loo
    checks["concentration"] = {
        "pass": bool(share_ok and loo_ok),
        "top3_share_of_pos": share,
        "max_share": max_share,
        "loo_neg": loo_neg,
        "max_loo_neg": max_loo,
    }

    passed = all(c["pass"] for c in checks.values())
    return {"pass": passed, "checks": checks}


# ---------------------------------------------------------------- report

def _fmt_pnl_row(name: str, r: dict) -> str:
    avg = r.get("avg")
    avg_s = f"{avg:+.2f}" if avg is not None else "–"
    return (
        f"| {name} | {r.get('n', 0)} | {r.get('wr') if r.get('wr') is not None else '–'} | "
        f"{r.get('gross', 0):+.1f} | {r.get('net', 0):+.1f} | {avg_s} | "
        f"{r.get('train_net', 0):+.1f} | {r.get('hold_net', 0):+.1f} | {r.get('new_net', 0):+.1f} | "
        f"{r.get('days_pos', 0)}/{r.get('n_sessions', 0)} | {r.get('max_dd', 0):.0f} | "
        f"{r.get('worst_day', 0):+.1f} |"
    )


PNL_HDR = [
    "| variant | n | WR | gross | net | avg | train* | hold* | new* | days+ | maxDD | worst |",
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
]

PNL_NOTE = (
    "\\*train/hold/new = harness cohorts (continuity); "
    "train months for the bar are Sep+Oct."
)


def write_report(
    out: Path,
    *,
    exp: dict,
    sids: list[str],
    books: dict[str, dict],
    null_dist: dict,
    conc: dict,
    bar: dict,
    strata_rep: dict,
    secondary: dict,
    show_books: dict | None,
    may_books: dict | None,
    events_table: pa.Table,
    trades: list[TradeResult],
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    primary = books["cd_x_mom"]
    md = [
        f"# {exp['name']} — {exp.get('title', '')}",
        "",
        exp.get("description", "").strip(),
        "",
        f"Sessions (train+show)={len(sids)} · UK 14:30–17:00 · 1-tick slip · "
        f"{COMMISSION} pt/RT · primary bracket = vol k=1 · **no gate**.",
        "",
        f"**Power note:** {exp.get('bar', {}).get('mde_note', 'SE≈2 pt/trade at n≈100')}. "
        "A miss at this n is thin, not falsified.",
        "",
        "## Pre-registered bar",
        "",
        f"**Result: {'PASS' if bar['pass'] else 'FAIL'}** — "
        + ("May OOS may be read." if bar["pass"] else "reject/park; no May; no parameter neighbours."),
        "",
        "| check | pass | detail |",
        "|---|---|---|",
    ]
    for name, c in bar["checks"].items():
        detail = {k: v for k, v in c.items() if k != "pass"}
        md.append(f"| {name} | {'yes' if c['pass'] else 'no'} | `{json.dumps(detail, default=str)}` |")

    md += ["", "## Primary P&L (vol k=1)", "", *PNL_HDR]
    for name, r in books.items():
        md.append(_fmt_pnl_row(name, r))
    md += ["", PNL_NOTE]

    md += [
        "",
        "### By month (primary cd_x_mom)",
        "",
        "| month | n | net | avg | days+ | days |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for m, d in (primary.get("by_month") or {}).items():
        md.append(
            f"| {m} | {d['n']} | {d['net']:+.1f} | "
            f"{d['avg'] if d['avg'] is not None else '–'} | {d['days_pos']}/{d['days']} |"
        )

    md += [
        "",
        "## Null distribution (50 seeds, matched n/session)",
        "",
        f"mean={null_dist['mean']:+.1f}  p10={null_dist['p10']:+.1f}  "
        f"p50={null_dist['p50']:+.1f}  **p90={null_dist['p90']:+.1f}**  "
        f"min={null_dist['min']:+.1f}  max={null_dist['max']:+.1f}",
        "",
        "## Concentration (primary)",
        "",
        f"days+={conc['days_pos']} / days−={conc['days_neg']} / flat={conc['days_flat']} · "
        f"top3 share of +net = {conc.get('top3_share_of_pos')} · "
        f"LOO remaining-net<0 count = **{conc['loo_neg']}**",
        "",
        "Top-3 sessions:",
        "",
        "| session | month | cohort | n | net |",
        "|---|---|---|---:|---:|",
    ]
    for d in conc["top3"]:
        md.append(
            f"| {d['session_id']} | {d['month']} | {d['cohort']} | {d['n']} | {d['net']:+.1f} |"
        )
    md += [
        "",
        "Worst-3 sessions:",
        "",
        "| session | month | cohort | n | net |",
        "|---|---|---|---:|---:|",
    ]
    for d in conc["worst3"]:
        md.append(
            f"| {d['session_id']} | {d['month']} | {d['cohort']} | {d['n']} | {d['net']:+.1f} |"
        )
    md += [
        "",
        "### Per session",
        "",
        "| session | month | cohort | n | net |",
        "|---|---|---|---:|---:|",
    ]
    for d in conc["per_session"]:
        md.append(
            f"| {d['session_id']} | {d['month']} | {d['cohort']} | {d['n']} | {d['net']:+.1f} |"
        )

    if secondary:
        md += ["", "## Secondary brackets (informational — never selected)", "", *PNL_HDR]
        for name, r in secondary.items():
            md.append(_fmt_pnl_row(name, r))

    if strata_rep and strata_rep.get("n", 0) > 0:
        md += ["", "## Strata on primary trades (informational)", ""]
        for sname in ("side", "er60_tercile", "hour_uk", "burst_lag_tercile", "cd_bb_regime", "trend_vwap"):
            bucket = strata_rep.get(sname) or {}
            md += [f"### {sname}", "", "| cell | n | net | avg | WR |", "|---|---:|---:|---:|---:|"]
            for k, v in bucket.items():
                md.append(f"| {k} | {v['n']} | {v['net']:+.1f} | {v['avg']:+.3f} | {v['wr']:.3f} |")
            md.append("")

    if show_books:
        md += ["", "## Mar/Aug (shown; not in bar)", "", *PNL_HDR]
        for name, r in show_books.items():
            md.append(_fmt_pnl_row(name, r))

    if may_books:
        md += ["", "## May 2026 OOS (read once)", "", *PNL_HDR]
        for name, r in may_books.items():
            md.append(_fmt_pnl_row(name, r))
        md += [
            "",
            "### May by session (cd_x_mom)",
            "",
            "| session | n | net |",
            "|---|---:|---:|",
        ]
        may_conc = may_books.get("_concentration")
        if may_conc:
            for d in may_conc["per_session"]:
                md.append(f"| {d['session_id']} | {d['n']} | {d['net']:+.1f} |")

    md += [
        "",
        "## Decision",
        "",
        (
            "Bar **PASS** — soft confirm pending May (if run) / forward paper. "
            "Do not retune event params."
            if bar["pass"]
            else "Bar **FAIL** — **reject / park**. No May. No parameter neighbours. "
            "CD×mom as a standalone P&L book does not clear the pre-registered bar "
            "on Sep+Oct under vol k=1."
        ),
        "",
    ]

    report = {
        "experiment": exp["name"],
        "title": exp.get("title"),
        "sessions": sids,
        "bar": bar,
        "books": {k: {kk: vv for kk, vv in v.items() if kk != "by_reason"} for k, v in books.items()},
        "null": {k: v for k, v in null_dist.items() if k != "nets"},
        "null_nets": null_dist.get("nets"),
        "concentration": {
            k: v for k, v in conc.items() if k not in ("loo",)  # keep per_session
        },
        "loo": conc.get("loo"),
        "strata": strata_rep,
        "secondary": secondary,
        "show": show_books,
        "may": (
            {k: v for k, v in may_books.items() if k != "_concentration"}
            if may_books else None
        ),
        "power_note": exp.get("bar", {}).get("mde_note"),
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    if events_table.num_rows:
        pq.write_table(events_table, out / "events.parquet")
    # trades parquet
    if trades:
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
            "er60": np.asarray([t.er60 for t in trades], dtype=np.float64),
            "net30": np.asarray([t.net30 for t in trades], dtype=np.float64),
            "med_r30": np.asarray([t.med_r30 for t in trades], dtype=np.float64),
        }
        pq.write_table(pa.table(cols), out / "trades.parquet")
    print(f"wrote {out / 'summary.md'}", flush=True)


# ---------------------------------------------------------------- main pipeline

def _load_pairs(parquet_dir: Path, sids: list[str]) -> list[H.Pair]:
    pairs = []
    for sid in sids:
        p = H.load_pair(parquet_dir, sid)
        if p is None:
            print(f"  skip {sid} (no NQ in window)", flush=True)
            continue
        pairs.append(p)
    return pairs


def _build_all_events(pairs: list[H.Pair], exp: dict) -> dict[str, dict[str, pa.Table]]:
    """sid → {confirmed, pullback, momentum, cd}."""
    out = {}
    for p in pairs:
        print(f"[events] {p.sid} …", flush=True)
        out[p.sid] = build_events(p, exp)
        c = out[p.sid]["confirmed"]
        print(
            f"  conf={c.num_rows} pb={out[p.sid]['pullback'].num_rows} "
            f"mom={out[p.sid]['momentum'].num_rows}",
            flush=True,
        )
    return out


def run_cohort(
    pairs: list[H.Pair],
    ev_all: dict[str, dict[str, pa.Table]],
    *,
    exp: dict,
    sim_cfg: MatSimConfig,
    brackets: dict,
    run_null: bool = True,
    run_strata: bool = True,
) -> dict:
    sids = [p.sid for p in pairs]
    primary_name = exp.get("primary_bracket", "vol_k1")
    br_primary = brackets[primary_name]

    conf_map = {sid: ev_all[sid]["confirmed"] for sid in sids if sid in ev_all}
    pb_map = {sid: ev_all[sid]["pullback"] for sid in sids if sid in ev_all}
    mom_map = {sid: ev_all[sid]["momentum"] for sid in sids if sid in ev_all}
    n_by_sid = {sid: (conf_map.get(sid).num_rows if conf_map.get(sid) is not None else 0) for sid in sids}

    print("[sim] cd_x_mom (primary) …", flush=True)
    primary_s = run_book(pairs, conf_map, sim_cfg=sim_cfg, bracket_fn=br_primary)
    print("[sim] momentum-only …", flush=True)
    mom_s = run_book(pairs, mom_map, sim_cfg=sim_cfg, bracket_fn=br_primary)
    print("[sim] cd-only …", flush=True)
    cd_s = run_book(pairs, pb_map, sim_cfg=sim_cfg, bracket_fn=br_primary)
    print("[sim] MAT frozen base (causal + trend+VWAP) …", flush=True)
    mat_cfg = MatSimConfig(
        slip_ticks=sim_cfg.slip_ticks,
        commission_pts_rt=sim_cfg.commission_pts_rt,
        signal_cooldown_sec=sim_cfg.signal_cooldown_sec,
        flat_only=sim_cfg.flat_only,
        es_stamp="causal",
    )
    mat_gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    mat_s = run_book(pairs, {}, sim_cfg=mat_cfg, bracket_fn=br_primary, gate=mat_gate, detect=True)

    books = {
        "cd_x_mom": _pack(primary_s, sids),
        "momentum_only": _pack(mom_s, sids),
        "cd_only": _pack(cd_s, sids),
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
        print(f"[null] {int(exp.get('null_seeds', 50))} seeds …", flush=True)
        null_dist = null_distribution(
            pairs, n_by_sid, sim_cfg=sim_cfg, bracket_fn=br_primary,
            n_seeds=int(exp.get("null_seeds", 50)),
        )
        print(
            f"  null mean={null_dist['mean']:+.1f} p90={null_dist['p90']:+.1f} "
            f"p10={null_dist['p10']:+.1f}",
            flush=True,
        )

    conc = concentration(primary_s.trades, sids)
    print(
        f"[conc] top3_share={conc.get('top3_share_of_pos')} loo_neg={conc['loo_neg']}",
        flush=True,
    )

    secondary = {}
    for bname in exp.get("secondary_brackets") or []:
        if bname == primary_name:
            continue
        print(f"[sim] secondary {bname} …", flush=True)
        br = brackets.get(bname)
        # fixed_10_10: bracket_fn=None uses cfg.tp/sl defaults (10/10)
        cfg_b = sim_cfg
        if bname == "fixed_10_10":
            cfg_b = MatSimConfig(**{**sim_cfg.__dict__, "tp_pts": 10.0, "sl_pts": 10.0})
        s = run_book(pairs, conf_map, sim_cfg=cfg_b, bracket_fn=br)
        secondary[f"cd_x_mom_{bname}"] = _pack(s, sids)

    strata_rep = {"n": 0}
    if run_strata:
        print("[strata] …", flush=True)
        strata_rep = strata(pairs, conf_map, primary_s, sim_cfg=sim_cfg, bracket_fn=br_primary)

    # concat confirmed events
    conf_parts = [conf_map[sid] for sid in sids if conf_map.get(sid) is not None and conf_map[sid].num_rows]
    events_table = pa.concat_tables(conf_parts) if conf_parts else pa.table({})

    return {
        "sids": sids,
        "books": books,
        "null_dist": null_dist,
        "conc": conc,
        "secondary": secondary,
        "strata": strata_rep,
        "events_table": events_table,
        "trades": primary_s.trades,
        "primary_s": primary_s,
        "conf_map": conf_map,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="EXP-0014_PNL.yaml")
    ap.add_argument("--out", default=None, help="Override results dir name")
    ap.add_argument("--smoke", action="store_true", help="2 sessions only, 3 null seeds")
    ap.add_argument("--may", action="store_true", help="Also run May OOS (only after bar pass)")
    ap.add_argument("--force-may", action="store_true", help="Run May even if bar fails (debug)")
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

    if args.smoke:
        train_sids = train_sids[:2]
        show_sids = []
        exp = {**exp, "null_seeds": 3}
        out = Path(cfg["results_dir"]) / f"{out_name}_SMOKE"
        print(f"[smoke] sessions={train_sids} null_seeds=3", flush=True)

    print(f"[load] train={train_sids}", flush=True)
    train_pairs = _load_pairs(parquet_dir, train_sids)
    print(f"[load] {len(train_pairs)} train pairs", flush=True)
    train_ev = _build_all_events(train_pairs, exp)

    sim_cfg = _sim_cfg(exp)
    brackets = _brackets(exp)

    train = run_cohort(
        train_pairs, train_ev, exp=exp, sim_cfg=sim_cfg, brackets=brackets,
        run_null=True, run_strata=not args.smoke,
    )

    bar = evaluate_bar(
        train["books"]["cd_x_mom"],
        train["null_dist"],
        train["books"]["momentum_only"],
        train["conc"],
        bar_cfg=exp.get("bar") or {},
        train_months=train_months,
    )
    print(f"[bar] {'PASS' if bar['pass'] else 'FAIL'}: {json.dumps({k: v['pass'] for k, v in bar['checks'].items()})}", flush=True)

    # Mar/Aug shown
    show_books = None
    if show_sids and not args.smoke:
        print(f"[show] {show_sids}", flush=True)
        show_pairs = _load_pairs(parquet_dir, show_sids)
        show_ev = _build_all_events(show_pairs, exp)
        show = run_cohort(
            show_pairs, show_ev, exp=exp, sim_cfg=sim_cfg, brackets=brackets,
            run_null=False, run_strata=False,
        )
        show_books = show["books"]

    may_books = None
    if (args.may or args.force_may) and may_sids:
        if not bar["pass"] and not args.force_may:
            print("[may] bar FAILED — refusing May read (use --force-may to override)", flush=True)
        else:
            print(f"[may] {may_sids}", flush=True)
            may_pairs = _load_pairs(parquet_dir, may_sids)
            may_ev = _build_all_events(may_pairs, exp)
            may = run_cohort(
                may_pairs, may_ev, exp=exp, sim_cfg=sim_cfg, brackets=brackets,
                run_null=False, run_strata=False,
            )
            may_books = may["books"]
            may_books["_concentration"] = may["conc"]

    write_report(
        out,
        exp=exp,
        sids=train["sids"],
        books=train["books"],
        null_dist=train["null_dist"],
        conc=train["conc"],
        bar=bar,
        strata_rep=train["strata"],
        secondary=train["secondary"],
        show_books=show_books,
        may_books=may_books,
        events_table=train["events_table"],
        trades=train["trades"],
    )
    print(
        f"[done] bar={'PASS' if bar['pass'] else 'FAIL'} "
        f"n={train['books']['cd_x_mom']['n']} net={train['books']['cd_x_mom']['net']:+.1f} "
        f"→ {out}",
        flush=True,
    )


if __name__ == "__main__":
    main()
