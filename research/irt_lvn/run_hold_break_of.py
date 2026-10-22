"""EXP-0043 / H34 — IRT hold vs break orderflow scores.

UK 14:30–17:00. NQ-only. Descriptive — no P&L book.

    python -m research.irt_lvn.run_hold_break_of [--month 2026-10] [--census-only]
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from research.config import ROOT, load_config
from research.correlated.harness import _prefer_single_feed, list_sessions
from research.irt_lvn.hold_break import (
    HoldBreakConfig,
    census_labels,
    detect_hold_break_events,
    stratified_sample,
)
from research.irt_lvn.confirm import ConfirmConfig, confirmation_table, gated_pnl, pack_pnl, score_tertile_break_rate
from research.irt_lvn.load_irt import IrtBars, default_irt_paths, load_irt_csv, session_id_to_ymd
from research.irt_lvn.of_scores import ScoreConfig, enrich_events_with_scores, prepare_session_of
from research.irt_lvn.run_exp_0042 import validate_timezone
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")
OPEN = time(14, 30)
CLOSE = time(17, 0)

PRIMARY = ("TakeScore_pre", "TapeScore_pre", "StackScore", "MboScore_pre", "BreakLikeness_pre")
LONGPRE = ("TakeScore_pre5", "TapeScore_pre5", "TakeScore_pre10", "TapeScore_pre10", "TakeScore_leg")
MECHANISM = ("TakeScore_at", "TapeScore_at", "TakeScore", "TapeScore", "StackConsume", "AttractRepelScore", "BreakLikeness")
RAW = (
    "beyond_share_pre", "beyond_minus_hold_z_pre", "prints_per_s_pre", "vol_pre", "burst_n_pre",
    "beyond_share_at", "beyond_aggr_at", "hold_aggr_at", "prints_per_s_at",
    "near_sum", "near_max", "near_n_large", "beyond_bbo_sum", "hold_near_sum", "book_imbalance", "far_sum",
    "band_offset_ticks", "net_add_z", "near_sum_z",
    "pull_rate_pre", "pull_rate_z", "replenish_n_pre", "replenish_z", "cancel_n_pre", "depletion_pre", "near_sum_m2",
    "net_add", "beyond_consume", "consume_frac", "take_flag", "n_tracker_levels",
    "post_vol", "post_prints", "impulse_pts", "impulse_speed", "impulse_dur_s", "first_pierce_pts",
    "pre_touch_vol", "leg_last10_frac", "leg_max_pullback_pts", "beyond_share_leg", "beyond_minus_hold_z_leg",
    "cluster_n", "min_after_open",
)


# --------------------------------------------------------- level utilities

def shift_bars(bars: IrtBars, shift_pts: float) -> IrtBars:
    """Return a copy with every non-zero level (and close) shifted by ``shift_pts``."""
    def sh(a: np.ndarray) -> np.ndarray:
        b = a.copy()
        nz = b != 0.0
        b[nz] = b[nz] + shift_pts
        return b
    return IrtBars(
        bars.symbol, bars.ts_ns.copy(), sh(bars.close), sh(bars.dop), sh(bars.dvpoc),
        sh(bars.lvn2), sh(bars.lvn3), sh(bars.hvn1), sh(bars.hvn2), sh(bars.hvn3),
        bars.columns, set(bars.days),
    )


def basis_offset(bars: IrtBars, trades: pa.Table) -> dict:
    """Signed IRT close − Bookmap last-print offset over all bars in the window (median / MAD)."""
    if trades.num_rows == 0 or len(bars.ts_ns) == 0:
        return {"n": 0}
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64) * 0.25
    o = np.argsort(ts)
    ts, px = ts[o], px[o]
    errs = []
    for i in range(len(bars.ts_ns)):
        if bars.close[i] == 0 or not (ts[0] <= bars.ts_ns[i] <= ts[-1]):
            continue
        j = int(np.searchsorted(ts, int(bars.ts_ns[i]), side="right") - 1)
        if j >= 0:
            errs.append(float(bars.close[i]) - float(px[j]))
    if not errs:
        return {"n": 0}
    e = np.asarray(errs)
    med = float(np.median(e))
    return {"n": len(e), "median_pts": round(med, 3), "mad_pts": round(float(np.median(np.abs(e - med))), 3)}


# ------------------------------------------------------------------ loaders

def bounds(mid_ns: int) -> tuple[int, int]:
    d = datetime.fromtimestamp(mid_ns / 1e9, tz=timezone.utc).astimezone(UK).date()
    return (
        int(datetime.combine(d, OPEN, tzinfo=UK).timestamp() * 1e9),
        int(datetime.combine(d, CLOSE, tzinfo=UK).timestamp() * 1e9),
    )


def filt(t: pa.Table, lo: int, hi: int) -> pa.Table:
    if t.num_rows == 0:
        return t
    ts = t.column("ts_ns").to_numpy().astype(np.int64)
    k = (ts >= lo) & (ts <= hi)
    return t.take(np.where(k)[0]) if k.any() else t.slice(0, 0)


def load_depth_window_with_book(
    parquet_dir: Path, sid: str, role: str, bounds_ns: tuple[int, int], batch_rows: int = 2_000_000,
) -> tuple[pa.Table, dict[int, int], dict[int, int]]:
    """Stream depth; return rows inside ``bounds`` plus the book state just before it.

    Rows before ``bounds[0]`` are folded into (bids, asks) vectorised per batch
    (last size per price), so resting orders placed earlier are not lost.
    """
    path = parquet_dir / f"session_id={sid}" / "depth.parquet"
    bids: dict[int, int] = {}
    asks: dict[int, int] = {}
    if not path.exists() or path.stat().st_size == 0:
        print(f"MISSING depth.parquet for session {sid}", flush=True)
        return pa.table({}), bids, asks
    pf = pq.ParquetFile(path)
    cols = ["ts_ns", "role", "is_bid", "price_ticks", "size"]
    parts = []
    for batch in pf.iter_batches(batch_size=batch_rows, columns=cols):
        t = pa.Table.from_batches([batch])
        ts = t.column("ts_ns").to_numpy().astype(np.int64)
        roles = t.column("role").to_numpy(zero_copy_only=False)
        rm = roles == role
        pre = rm & (ts < bounds_ns[0])
        if pre.any():
            idx = np.where(pre)[0]
            idx = idx[np.argsort(ts[idx], kind="stable")]
            isb = t.column("is_bid").to_numpy().astype(bool)[idx]
            px = t.column("price_ticks").to_numpy().astype(np.int64)[idx]
            sz = t.column("size").to_numpy().astype(np.int64)[idx]
            key = px * 2 + isb.astype(np.int64)
            # last occurrence per key
            _, last_rev = np.unique(key[::-1], return_index=True)
            last = len(key) - 1 - last_rev
            for k_ in last:
                book = bids if isb[k_] else asks
                if sz[k_] <= 0:
                    book.pop(int(px[k_]), None)
                else:
                    book[int(px[k_])] = int(sz[k_])
        k = rm & (ts >= bounds_ns[0]) & (ts <= bounds_ns[1])
        if k.any():
            parts.append(t.take(np.where(k)[0]))
    return (pa.concat_tables(parts) if parts else pa.table({})), bids, asks


def load_mbo_window(parquet_dir: Path, sid: str, role: str, bounds_ns: tuple[int, int], batch_rows: int = 2_000_000) -> pa.Table:
    path = parquet_dir / f"session_id={sid}" / "mbo.parquet"
    if not path.exists() or path.stat().st_size == 0:
        return pa.table({})
    pf = pq.ParquetFile(path)
    cols = ["ts_ns", "role", "op", "order_id", "is_bid", "price_ticks", "size"]
    parts = []
    for batch in pf.iter_batches(batch_size=batch_rows, columns=cols):
        t = pa.Table.from_batches([batch])
        ts = t.column("ts_ns").to_numpy().astype(np.int64)
        roles = t.column("role").to_numpy(zero_copy_only=False)
        k = (ts >= bounds_ns[0]) & (ts <= bounds_ns[1]) & (roles == role)
        if k.any():
            parts.append(t.take(np.where(k)[0]))
    return pa.concat_tables(parts) if parts else pa.table({})


def _uk_str(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).astimezone(UK).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------- statistics

def _fin(a) -> np.ndarray:
    x = np.asarray(a, dtype=np.float64)
    return x[np.isfinite(x)]


def _median(a) -> float | None:
    x = _fin(a)
    return float(np.median(x)) if len(x) else None


def _iqr(a) -> float | None:
    x = _fin(a)
    return float(np.percentile(x, 75) - np.percentile(x, 25)) if len(x) >= 2 else None


def cliffs_delta(a, b) -> float | None:
    aa, bb = _fin(a), _fin(b)
    if len(aa) == 0 or len(bb) == 0:
        return None
    gt = sum(int(np.sum(x > bb)) for x in aa)
    lt = sum(int(np.sum(x < bb)) for x in aa)
    return float((gt - lt) / (len(aa) * len(bb)))


def cohens_d(a, b) -> float | None:
    aa, bb = _fin(a), _fin(b)
    if len(aa) < 2 or len(bb) < 2:
        return None
    s1, s2 = aa.std(ddof=1), bb.std(ddof=1)
    pooled = np.sqrt(((len(aa) - 1) * s1 ** 2 + (len(bb) - 1) * s2 ** 2) / (len(aa) + len(bb) - 2))
    return 0.0 if pooled < 1e-12 else float((aa.mean() - bb.mean()) / pooled)


def _col(tbl: pa.Table, name: str) -> np.ndarray:
    if tbl.num_rows == 0 or name not in tbl.column_names:
        return np.array([], dtype=np.float64)
    return tbl.column(name).to_numpy().astype(np.float64)


def _mask(tbl: pa.Table, m: np.ndarray) -> pa.Table:
    return tbl.take(np.where(m)[0]) if m.any() else tbl.slice(0, 0)


def split_label(tbl: pa.Table, label: str) -> pa.Table:
    if tbl.num_rows == 0:
        return tbl
    lab = np.array(tbl.column("label").to_pylist())
    return _mask(tbl, lab == label)


def compare_groups(hold: np.ndarray, brk: np.ndarray) -> dict:
    mh, mb = _median(hold), _median(brk)
    d = cliffs_delta(brk, hold)
    return {
        "n_hold": int(len(_fin(hold))), "n_break": int(len(_fin(brk))),
        "median_hold": mh, "median_break": mb,
        "iqr_hold": _iqr(hold), "iqr_break": _iqr(brk),
        "median_gap_break_minus_hold": None if mh is None or mb is None else round(mb - mh, 4),
        "cliffs_delta_break_vs_hold": None if d is None else round(d, 4),
        "cohens_d_break_vs_hold": (lambda v: None if v is None else round(v, 4))(cohens_d(brk, hold)),
    }


def session_bootstrap(tbl: pa.Table, score: str, n_boot: int = 1000, seed: int = 7) -> dict:
    """Cluster bootstrap over sessions: CI for median gap and cliffs δ (break vs hold)."""
    if tbl.num_rows == 0:
        return {}
    lab = np.array(tbl.column("label").to_pylist())
    sid = np.array(tbl.column("session_id").to_pylist())
    vals = _col(tbl, score)
    sess = sorted(set(sid[(lab == "HOLD") | (lab == "BREAK")]))
    if len(sess) < 3:
        return {"note": "too_few_sessions"}
    rng = np.random.default_rng(seed)
    by = {s: (vals[(sid == s) & (lab == "HOLD")], vals[(sid == s) & (lab == "BREAK")]) for s in sess}
    gaps, deltas = [], []
    for _ in range(n_boot):
        pick = rng.choice(sess, size=len(sess), replace=True)
        h = np.concatenate([by[s][0] for s in pick]) if pick.size else np.array([])
        b = np.concatenate([by[s][1] for s in pick]) if pick.size else np.array([])
        if len(_fin(h)) < 3 or len(_fin(b)) < 3:
            continue
        gaps.append(float(np.median(_fin(b)) - np.median(_fin(h))))
        d = cliffs_delta(b, h)
        if d is not None:
            deltas.append(d)
    if len(gaps) < 50:
        return {"note": "insufficient_resamples", "n_ok": len(gaps)}
    g = np.asarray(gaps)
    dd = np.asarray(deltas)
    return {
        "n_sessions": len(sess), "n_resamples": len(gaps),
        "gap_ci90": [round(float(np.percentile(g, 5)), 4), round(float(np.percentile(g, 95)), 4)],
        "delta_ci90": [round(float(np.percentile(dd, 5)), 4), round(float(np.percentile(dd, 95)), 4)],
        "p_gap_le_0": round(float(np.mean(g <= 0)), 3),
    }


def loo_sign_stable(tbl: pa.Table, score: str) -> dict:
    if tbl.num_rows == 0:
        return {"stable": False, "reason": "empty"}
    lab = np.array(tbl.column("label").to_pylist())
    sid = np.array(tbl.column("session_id").to_pylist())
    vals = _col(tbl, score)
    hb = (lab == "HOLD") | (lab == "BREAK")
    if not hb.any():
        return {"stable": False, "reason": "no_hb"}
    counts: dict[str, int] = defaultdict(int)
    for s in sid[hb]:
        counts[str(s)] += 1
    fat = max(counts, key=counts.get)
    keep = sid != fat
    g_full = (_median(vals[lab == "BREAK"]) or 0) - (_median(vals[lab == "HOLD"]) or 0)
    g_loo = (_median(vals[keep & (lab == "BREAK")]) or 0) - (_median(vals[keep & (lab == "HOLD")]) or 0)
    return {
        "dropped_session": fat, "dropped_n": counts[fat],
        "gap_full": round(g_full, 4), "gap_loo": round(g_loo, 4),
        "stable": bool(np.sign(g_full) == np.sign(g_loo) and abs(g_full) > 1e-9),
    }


def impulse_tertile_split(tbl: pa.Table, score: str) -> dict:
    hold, brk = split_label(tbl, "HOLD"), split_label(tbl, "BREAK")
    if hold.num_rows == 0 or brk.num_rows == 0:
        return {}
    imp = _fin(np.concatenate([_col(hold, "impulse_pts"), _col(brk, "impulse_pts")]))
    if len(imp) < 6:
        return {"note": "too_few"}
    q33, q66 = np.percentile(imp, [33.3, 66.6])
    out = {}
    for name, lo, hi in (("low", -np.inf, q33), ("mid", q33, q66), ("high", q66, np.inf)):
        def sel(t):
            x = _col(t, "impulse_pts")
            return _col(t, score)[(x >= lo) & (x < hi)]
        h, b = sel(hold), sel(brk)
        mh, mb = _median(h), _median(b)
        d = cliffs_delta(b, h)
        out[name] = {
            "n_hold": int(len(h)), "n_break": int(len(b)),
            "median_gap_break_minus_hold": None if mh is None or mb is None else round(mb - mh, 4),
            "cliffs_delta": None if d is None else round(d, 4),
        }
    return out


def _r(x, nd: int = 3):
    if x is None:
        return "—"
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return "—"


def _score_rows(tbl: pa.Table, scores: tuple[str, ...], with_boot: bool) -> tuple[dict, list[str]]:
    hold, brk, amb = split_label(tbl, "HOLD"), split_label(tbl, "BREAK"), split_label(tbl, "AMBIGUOUS")
    table: dict = {}
    lines = [
        "| score | n_h | n_b | med_h | med_b | gap(b−h) | cliffs δ | d | LOO | boot gap CI90 | boot δ CI90 | med AMB |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|---|---|---:|",
    ]
    for s in scores:
        if s not in tbl.column_names:
            continue
        st = compare_groups(_col(hold, s), _col(brk, s))
        st["median_ambiguous"] = _median(_col(amb, s)) if amb.num_rows else None
        st["loo"] = loo_sign_stable(tbl, s)
        st["boot"] = session_bootstrap(tbl, s) if with_boot else {}
        table[s] = st
        bo = st["boot"]
        lines.append(
            f"| {s} | {st['n_hold']} | {st['n_break']} | {_r(st['median_hold'])} | {_r(st['median_break'])} | "
            f"{_r(st['median_gap_break_minus_hold'])} | {_r(st['cliffs_delta_break_vs_hold'])} | "
            f"{_r(st['cohens_d_break_vs_hold'])} | {'yes' if st['loo'].get('stable') else 'no'} | "
            f"{bo.get('gap_ci90', '—')} | {bo.get('delta_ci90', '—')} | {_r(st['median_ambiguous'])} |"
        )
    return table, lines


def _chart_rows(tbl: pa.Table, score: str, n: int, lowest: bool) -> list[dict]:
    if tbl.num_rows == 0 or score not in tbl.column_names:
        return []
    v = _col(tbl, score)
    order = np.argsort(v)
    pick = order[:n] if lowest else order[-n:][::-1]
    rows = []
    for i in pick:
        i = int(i)
        rows.append({
            "session_id": tbl.column("session_id")[i].as_py(),
            "uk_time": tbl.column("uk_time")[i].as_py(),
            "label": tbl.column("label")[i].as_py(),
            "level_px": float(tbl.column("level_px")[i].as_py()),
            "level_kinds": tbl.column("level_kinds")[i].as_py(),
            "approach": tbl.column("approach_str")[i].as_py(),
            "first_pierce_pts": float(tbl.column("first_pierce_pts")[i].as_py()),
            "min_after_open": float(tbl.column("min_after_open")[i].as_py()),
            score: float(v[i]),
            "TakeScore_pre": float(tbl.column("TakeScore_pre")[i].as_py()),
            "TapeScore_pre": float(tbl.column("TapeScore_pre")[i].as_py()),
            "StackScore": float(tbl.column("StackScore")[i].as_py()),
            "TakeScore_at": float(tbl.column("TakeScore_at")[i].as_py()),
        })
    return rows


def _chart_md(rows: list[dict], score: str) -> list[str]:
    out = [
        f"| session | UK time | min | level | kinds | side | {score} | Take_pre | Tape_pre | Stack | Take_at | pierce |",
        "|---|---|---:|---:|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        out.append(
            f"| {r['session_id']} | {r['uk_time']} | {_r(r['min_after_open'], 1)} | {r['level_px']:.2f} | {r['level_kinds']} | "
            f"{r['approach']} | {_r(r[score])} | {_r(r['TakeScore_pre'])} | {_r(r['TapeScore_pre'])} | "
            f"{_r(r['StackScore'])} | {_r(r['TakeScore_at'])} | {_r(r['first_pierce_pts'])} |"
        )
    return out


# --------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="EXP-0043 IRT hold vs break OF")
    ap.add_argument("--config", default=str(ROOT / "research" / "experiments" / "EXP-0043.yaml"))
    ap.add_argument("--month", default=None, help="Calendar month YYYY-MM (default from yaml)")
    ap.add_argument("--out", default=None, help="Output dir (default results/EXP-0043[_YYYYMM])")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--max-align-err", type=float, default=None)
    ap.add_argument("--census-only", action="store_true", help="Skip OF scoring")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        exp = yaml.safe_load(f)
    cfg_root = load_config()
    pq_dir = Path(cfg_root["parquet_dir"])
    month_str = args.month or str(exp.get("month_prefix", "2026-10"))
    y_sel, m_sel = (int(x) for x in month_str.split("-")[:2])
    if args.out:
        out = Path(args.out)
    else:
        suffix = "" if (y_sel, m_sel) == (2026, 10) else f"_{y_sel}{m_sel:02d}"
        out = ROOT / "results" / f"EXP-0043{suffix}"
    out.mkdir(parents=True, exist_ok=True)

    max_align = float(args.max_align_err if args.max_align_err is not None else exp.get("max_align_err_pts", 25.0))
    exclude = set(exp.get("exclude_sessions", []))
    first_split_min = float(exp.get("first_window_split_min", 15.0))

    hb_cfg = HoldBreakConfig(
        pad_pts=float(exp.get("pad_pts", 3.0)),
        impulse_pts=float(exp.get("impulse_pts", 30.0)),
        impulse_lookback_ms=int(exp.get("impulse_lookback_ms", 60_000)),
        decision_horizon_ms=int(exp.get("decision_horizon_ms", 120_000)),
        reject_pts=float(exp.get("reject_pts", 30.0)),
        pierce_max_hold=float(exp.get("pierce_max_hold", 15.0)),
        pierce_min_break=float(exp.get("pierce_min_break", 15.0)),
        extension_break_pts=float(exp.get("extension_break_pts", 30.0)),
        cluster_tol_pts=float(exp.get("cluster_tol_pts", 5.0)),
        cooldown_ms=int(exp.get("cooldown_ms", 30_000)),
        dop_exclude_first_min=float(exp.get("dop_exclude_first_min", 5.0)),
    )
    sc_cfg = ScoreConfig(
        large_size_pct=float(exp.get("large_size_pct", 99.9)),
        large_size_min=int(exp.get("large_size_min", 15)),
        tick_radius=int(exp.get("tick_radius", 8)),
        far_band_ticks=int(exp.get("far_band_ticks", 20)),
        z_lookback_ms=int(exp.get("z_lookback_ms", 1_800_000)),
        tape_lookback_ms=int(exp.get("tape_lookback_ms", 300_000)),
        baseline_min_ms=int(exp.get("baseline_min_ms", 300_000)),
    )
    conf_cfg = ConfirmConfig(
        confirm_ms=int(exp.get("confirm_ms", 2_000)),
        horizon_ms=int(exp.get("decision_horizon_ms", 120_000)),
        reject_pts=float(exp.get("reject_pts", 30.0)),
        extension_pts=float(exp.get("extension_break_pts", 30.0)),
        brackets=tuple(tuple(float(x) for x in b) for b in exp.get("confirm_brackets", [[20, 15], [30, 15], [12.5, 30]])),
        slip_ticks=float(exp.get("slip_ticks", 1.0)),
        commission_pts_rt=float(exp.get("commission_pts_rt", 0.25)),
        reclaim_min_pierce_pts=float(exp.get("reclaim_min_pierce_pts", 2.0)),
    )
    placebo_shift = float(exp.get("placebo_shift_pts", 40.0))
    basis_mad_max = float(exp.get("basis_shift_mad_max_pts", 2.0))

    nq_irt = load_irt_csv(default_irt_paths(ROOT)["NQ"])
    nq_days = nq_irt.calendar_days()

    eligible = []
    for sid in list_sessions(pq_dir, exclude=exclude):
        try:
            y, m, d = session_id_to_ymd(sid)
        except Exception:
            continue
        if (y, m) != (y_sel, m_sel) or (y, m, d) not in nq_days:
            continue
        dpath = pq_dir / f"session_id={sid}" / "depth.parquet"
        mpath = pq_dir / f"session_id={sid}" / "mbo.parquet"
        if not (dpath.exists() and dpath.stat().st_size and mpath.exists() and mpath.stat().st_size):
            continue
        eligible.append(sid)
    if args.smoke:
        eligible = eligible[:1]
    print(f"Eligible {month_str} sessions: {eligible} -> {out}", flush=True)

    # ------------------------------------------------------------ universe
    universe_parts: list[pa.Table] = []
    placebo_parts: list[pa.Table] = []
    skipped_align: list[str] = []
    basis_shifts: dict[str, float] = {}
    session_census: dict[str, dict] = {}

    def _tag(ev: pa.Table, sid: str, placebo: float) -> pa.Table:
        ev = ev.append_column("session_id", pa.array([sid] * ev.num_rows, type=pa.string()))
        ev = ev.append_column("uk_time", pa.array([_uk_str(int(t)) for t in ev.column("event_ts_ns").to_pylist()], type=pa.string()))
        return ev.append_column("placebo_shift", pa.array([placebo] * ev.num_rows, type=pa.float64()))

    for sid in eligible:
        print(f"[universe] {sid}", flush=True)
        nq0 = _prefer_single_feed(_load_trades(pq_dir, sid, "NQ"), pq_dir, sid, "NQ")
        if nq0.num_rows == 0:
            session_census[sid] = {"note": "empty"}
            continue
        mid = int(nq0.column("ts_ns").to_numpy()[nq0.num_rows // 2])
        lo, hi = bounds(mid)
        look_ns = int(hb_cfg.impulse_lookback_ms * 1_000_000)
        nq_ctx = filt(nq0, lo - look_ns, hi + int(hb_cfg.decision_horizon_ms * 1_000_000))
        if nq_ctx.num_rows == 0:
            session_census[sid] = {"note": "empty_window"}
            continue
        y, m, d = session_id_to_ymd(sid)
        day_lo = int(datetime(y, m, d, 0, 0, tzinfo=UK).timestamp() * 1e9)
        day_hi = int(datetime(y, m, d, 23, 59, 59, tzinfo=UK).timestamp() * 1e9)
        nq_bars = nq_irt.slice_range(day_lo, day_hi)
        nq_win = filt(nq0, lo, hi)
        align = validate_timezone(nq_bars, nq_win)
        med = align.get("median_abs_err_pts")
        basis = None
        if med is not None and med >= max_align:
            # Contract roll: IRT @NQ# is back-adjusted; Bookmap traded the front month.
            # A constant, tight offset (MAD small) is a basis, not a timezone error → shift levels.
            basis = basis_offset(nq_bars, nq_win)
            if basis.get("n", 0) >= 20 and basis["mad_pts"] <= basis_mad_max:
                shift = -round(basis["median_pts"] / 0.25) * 0.25
                nq_bars = shift_bars(nq_bars, shift)
                align = validate_timezone(nq_bars, nq_win)
                med = align.get("median_abs_err_pts")
                basis_shifts[sid] = shift
                print(f"  basis shift {shift:+.2f} pts (mad {basis['mad_pts']}) -> median_err={med}", flush=True)
        if med is None or med >= max_align:
            skipped_align.append(sid)
            session_census[sid] = {"note": "skip_align", "align": align, "basis": basis}
            print(f"  skip align {med}", flush=True)
            continue
        ev = detect_hold_break_events(nq_ctx, nq_bars, cfg=hb_cfg, window=(lo, hi), rth_open_ns=lo)
        cen = census_labels(ev)
        # placebo: same detector at levels shifted ±placebo_shift (beyond the reject distance)
        pl_cen: dict[str, int] = {}
        for sgn in (+1.0, -1.0):
            pl = detect_hold_break_events(nq_ctx, shift_bars(nq_bars, sgn * placebo_shift), cfg=hb_cfg, window=(lo, hi), rth_open_ns=lo)
            for k_, v_ in census_labels(pl).items():
                pl_cen[k_] = pl_cen.get(k_, 0) + v_
            if pl.num_rows:
                placebo_parts.append(_tag(pl, sid, sgn * placebo_shift))
        session_census[sid] = {"align": align, "basis": basis, "basis_shift": basis_shifts.get(sid), "census": cen, "n": ev.num_rows, "placebo_census": pl_cen}
        print(f"  align ok median_err={med}  census {cen}  placebo {pl_cen}", flush=True)
        if ev.num_rows:
            universe_parts.append(_tag(ev, sid, 0.0))

    if not universe_parts:
        (out / "summary.md").write_text("# EXP-0043\n\nNo events.\n", encoding="utf-8")
        print("No universe events.", flush=True)
        return

    universe = pa.concat_tables(universe_parts, promote_options="default")
    univ_census = census_labels(universe)
    samp_kw = dict(
        n_per_class=int(exp.get("sample_n_per_class", 100)),
        min_per_class=int(exp.get("sample_min_per_class", 50)),
        max_per_session=int(exp.get("max_per_session", 12)),
        seed=int(exp.get("sample_seed", 42)),
    )
    sampled = stratified_sample(universe, **samp_kw)
    samp_census = census_labels(sampled)
    placebo_univ = pa.concat_tables(placebo_parts, promote_options="default") if placebo_parts else pa.table({})
    placebo_s = stratified_sample(placebo_univ, **samp_kw) if placebo_univ.num_rows else pa.table({})
    print(f"Universe {univ_census} n={universe.num_rows} | Sampled {samp_census} n={sampled.num_rows} | "
          f"Placebo {census_labels(placebo_univ)} sampled {placebo_s.num_rows}", flush=True)
    pq.write_table(universe, out / "universe.parquet")
    pq.write_table(sampled, out / "sample_pre_score.parquet")
    if placebo_univ.num_rows:
        pq.write_table(placebo_univ, out / "placebo_universe.parquet")

    if args.census_only:
        (out / "report.json").write_text(json.dumps({
            "universe": univ_census, "sampled": samp_census,
            "sessions": session_census, "skipped_align": skipped_align,
        }, indent=2, default=str), encoding="utf-8")
        (out / "summary.md").write_text(f"# EXP-0043 census\n\nUniverse `{univ_census}`\n\nSampled `{samp_census}`\n", encoding="utf-8")
        return

    # -------------------------------------------------------------- scoring
    scored_parts: list[pa.Table] = []
    placebo_scored: list[pa.Table] = []
    large_thr_by_sess: dict[str, int] = {}
    mbo_stats_by_sess: dict[str, dict] = {}
    sids_sample = sorted(set(sampled.column("session_id").to_pylist()) | set(placebo_s.column("session_id").to_pylist() if placebo_s.num_rows else []))
    hor_ns = int(conf_cfg.horizon_ms * 1_000_000)
    for sid in sids_sample:
        print(f"[score] {sid}", flush=True)
        sev = _mask(sampled, np.array(sampled.column("session_id").to_pylist()) == sid)
        pev = _mask(placebo_s, np.array(placebo_s.column("session_id").to_pylist()) == sid) if placebo_s.num_rows else pa.table({})
        nq0 = _prefer_single_feed(_load_trades(pq_dir, sid, "NQ"), pq_dir, sid, "NQ")
        mid = int(nq0.column("ts_ns").to_numpy()[nq0.num_rows // 2])
        lo, hi = bounds(mid)
        look_ns = int(sc_cfg.z_lookback_ms * 1_000_000)
        post_ns = max(int(sc_cfg.diagnostic_post_ms * 1_000_000), hor_ns)
        db = (lo - look_ns, hi + post_ns)
        depth, ib, ia = load_depth_window_with_book(pq_dir, sid, "NQ", db)
        mbo = load_mbo_window(pq_dir, sid, "NQ", db)
        trades = filt(nq0, db[0], db[1])
        print(f"  depth={depth.num_rows} (init book {len(ib)}/{len(ia)}) mbo={mbo.num_rows} trades={trades.num_rows} events={sev.num_rows} placebo={pev.num_rows}", flush=True)
        sof = prepare_session_of(trades, depth, mbo, cfg=sc_cfg, bounds=(lo, hi), init_books=(ib, ia))
        if sof is None:
            print("  skip: no OF feeds", flush=True)
            if sev.num_rows:
                scored_parts.append(sev)
            continue
        large_thr_by_sess[sid] = sof.large_thr
        mbo_stats_by_sess[sid] = sof.mbo_stats
        if sev.num_rows:
            scored_parts.append(confirmation_table(trades, enrich_events_with_scores(sev, sof), conf_cfg))
        if pev.num_rows:
            placebo_scored.append(enrich_events_with_scores(pev, sof))
        print(f"  scored {sev.num_rows}+{pev.num_rows} (large_thr={sof.large_thr}; mbo {sof.mbo_stats})", flush=True)

    events = pa.concat_tables(scored_parts, promote_options="default")
    pq.write_table(events, out / "events.parquet")
    placebo = pa.concat_tables(placebo_scored, promote_options="default") if placebo_scored else pa.table({})
    if placebo.num_rows:
        pq.write_table(placebo, out / "placebo_events.parquet")

    # --------------------------------------------------------------- report
    bl_ok = _col(events, "baseline_ok") >= 0.5
    mao = _col(events, "min_after_open")
    ev_ok = _mask(events, bl_ok)
    ev_early = _mask(events, mao <= first_split_min)
    ev_late = _mask(events, mao > first_split_min)

    primary_tbl, primary_md = _score_rows(ev_ok, PRIMARY, with_boot=True)
    longpre_tbl, longpre_md = _score_rows(ev_ok, LONGPRE, with_boot=True)
    mech_tbl, mech_md = _score_rows(ev_ok, MECHANISM, with_boot=True)
    all_tbl, all_md = _score_rows(events, PRIMARY, with_boot=False)
    early_tbl, early_md = _score_rows(ev_early, PRIMARY + ("TakeScore_at",), with_boot=False)
    late_tbl, late_md = _score_rows(ev_late, PRIMARY + ("TakeScore_at",), with_boot=False)
    tertile = {s: impulse_tertile_split(ev_ok, s) for s in PRIMARY}

    hold_ok, brk_ok = split_label(ev_ok, "HOLD"), split_label(ev_ok, "BREAK")
    raw_tbl = {f: compare_groups(_col(hold_ok, f), _col(brk_ok, f)) for f in RAW if f in events.column_names}

    # placebo: same scores at levels ±placebo_shift away from the real ones
    placebo_tbl: dict = {}
    placebo_md: list[str] = []
    placebo_census = census_labels(placebo) if placebo.num_rows else {}
    if placebo.num_rows and "baseline_ok" in placebo.column_names:
        pl_ok = _mask(placebo, _col(placebo, "baseline_ok") >= 0.5)
        placebo_tbl, placebo_md = _score_rows(pl_ok, PRIMARY + ("TakeScore_at", "StackConsume"), with_boot=True)
        placebo_census = {"all": placebo_census, "baseline_ok": census_labels(pl_ok)}

    # confirmation at +confirm_ms: undecided events only
    und = _col(events, "undecided_at_tc") >= 0.5
    ev_und = _mask(events, und & bl_ok)
    conf_tbl, conf_md = _score_rows(ev_und, ("TakeScore_at", "TapeScore_at", "StackConsume", "BreakLikeness"), with_boot=True)
    conf_tertiles = {s: score_tertile_break_rate(ev_und, s) for s in ("TakeScore_at", "StackConsume", "BreakLikeness")}
    sess_arr = np.array(events.column("session_id").to_pylist())
    ts_arr = _col(events, "t_touch_ns")
    und_all = _mask(events, und)
    sess_u, ts_u = np.array(und_all.column("session_id").to_pylist()), _col(und_all, "t_touch_ns")
    pnl_tbl: dict[str, dict] = {}
    for tp, sl in conf_cfg.brackets:
        tag = f"tp{tp:g}_sl{sl:g}"
        rows: dict[str, dict] = {
            "fade_tc": pack_pnl(_col(und_all, f"pnl_fade_tc_{tag}"), sess_u, ts_u),
            "cont_tc": pack_pnl(_col(und_all, f"pnl_cont_tc_{tag}"), sess_u, ts_u),
            "fade_reclaim(all)": pack_pnl(_col(events, f"pnl_fade_reclaim_{tag}"), sess_arr, ts_arr),
        }
        rp = _col(events, "reclaim_pierce_pts")
        shallow = _mask(events, np.isfinite(rp) & (rp < 8.0) & (_col(events, "t_reclaim_ns") > 0))
        rows["fade_reclaim(pierce<8)"] = pack_pnl(_col(shallow, f"pnl_fade_reclaim_{tag}"), np.array(shallow.column("session_id").to_pylist()), _col(shallow, "t_touch_ns"))
        for sc in conf_cfg.gate_scores:
            if sc not in und_all.column_names:
                continue
            for thr in conf_cfg.gate_thresholds:
                rows[f"gated[{sc}≥{thr:g}]"] = pack_pnl(gated_pnl(und_all, sc, thr, tag), sess_u, ts_u)
        pnl_tbl[tag] = rows

    def _pnl_md(tag: str) -> list[str]:
        out_ = [f"### Bracket `{tag}` (pts; 1-tick slip each side, {conf_cfg.commission_pts_rt} pt/RT; time stop +{conf_cfg.horizon_ms // 1000}s)", "",
                "| strategy | n | net | avg | win | days+ | tpd | maxDD |", "|---|---:|---:|---:|---:|---|---:|---:|"]
        for k, v in pnl_tbl[tag].items():
            out_.append(f"| {k} | {v['n']} | {v['net']} | {_r(v['avg'])} | {_r(v['win'])} | {v['days_pos']}/{v['days']} | {_r(v['tpd'], 2)} | {v['max_dd']} |")
        return out_ + [""]

    by_sess: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for s, l in zip(events.column("session_id").to_pylist(), events.column("label").to_pylist()):
        by_sess[str(s)][str(l)] += 1

    chart_bl_holds = _chart_rows(hold_ok, "BreakLikeness_pre", 15, lowest=False)
    chart_hl_breaks = _chart_rows(brk_ok, "BreakLikeness_pre", 15, lowest=True)

    und_c = census_labels(ev_und)
    report = {
        "basis_shifts": basis_shifts,
        "placebo_shift_pts": placebo_shift, "placebo_census": placebo_census, "placebo_scores_baseline_ok": placebo_tbl,
        "longpre_scores_baseline_ok": longpre_tbl,
        "confirmation": {
            "confirm_ms": conf_cfg.confirm_ms, "undecided_census_baseline_ok": und_c,
            "undecided_census_all": census_labels(und_all),
            "scores_undecided": conf_tbl, "tertile_break_rate": conf_tertiles, "pnl": pnl_tbl,
        },
        "experiment": "EXP-0043", "hypothesis": "H34", "month": month_str, "window": "14:30-17:00 UK",
        "eligible_sessions": eligible, "skipped_align": skipped_align,
        "sessions_used": [s for s in eligible if s not in skipped_align],
        "universe_census": univ_census, "universe_n": universe.num_rows,
        "sampled_census": samp_census, "sampled_n": events.num_rows,
        "baseline_ok_census": census_labels(ev_ok), "early_census": census_labels(ev_early), "late_census": census_labels(ev_late),
        "per_session": {k: dict(v) for k, v in sorted(by_sess.items())},
        "large_thr_by_session": large_thr_by_sess,
        "mbo_resolution_by_session": mbo_stats_by_sess,
        "session_census_detail": session_census,
        "primary_scores_baseline_ok": primary_tbl,
        "mechanism_scores_baseline_ok": mech_tbl,
        "primary_scores_all": all_tbl,
        "primary_scores_first": early_tbl, "primary_scores_after": late_tbl,
        "impulse_tertile": tertile, "raw_features": raw_tbl,
        "chart_break_like_holds": chart_bl_holds, "chart_hold_like_breaks": chart_hl_breaks,
        "params": exp,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    ok_c, e_c, l_c = census_labels(ev_ok), census_labels(ev_early), census_labels(ev_late)
    lines = [
        "# EXP-0043 — IRT hold vs break orderflow (H34)",
        "",
        f"**Month:** {month_str} · UK **14:30–17:00**, NQ-only. Align gate median |IRT−Bookmap| < {max_align} pts.",
        f"Sessions used: `{report['sessions_used']}`. Skipped align: `{skipped_align}`.",
        "",
        "## Method (post-review)",
        "",
        "- Primary scores are **pre-touch only** `[-2s, 0)`: `TakeScore_pre` (beyond−hold aggressor z, beyond share), "
        "`TapeScore_pre` (prints/vol/burst z), `StackScore` (book at touch vs BBO-relative baseline). "
        "`*_at` / unsuffixed scores see `[0,+2s]` and are mechanism checks only.",
        f"- Baselines: robust (median/MAD) sliding windows over `[max(t−30m, 14:30), t)`; events with < {sc_cfg.baseline_min_ms/60000:.0f} min "
        "RTH baseline are `baseline_ok=0` and excluded from primary tables.",
        f"- Large size = session p{sc_cfg.large_size_pct} of non-zero depth sizes (floor {sc_cfg.large_size_min}); per-session: `{large_thr_by_sess}`.",
        f"- DOP ignored for first {hb_cfg.dop_exclude_first_min:.0f} min; levels within {hb_cfg.cluster_tol_pts:.0f} pts merged; cooldown per level id; "
        "jump-through prints count as touches; depth replay starts from the pre-window book.",
        f"- Tape z baseline is {sc_cfg.tape_lookback_ms // 60000} min (non-stationary intensity); aggressor/book baselines 30 min. "
        "`MboScore_pre` = beyond-side pull rate z + depletion − replenish z on the near band in `[-2s,0)`. "
        "AttractRepel is reported but not in any composite (never fires at IRT prices).",
        f"- Placebo: same detector/scores at levels shifted ±{placebo_shift:g} pts. "
        f"Roll handling: sessions whose IRT−Bookmap offset is constant (MAD ≤ {basis_mad_max:g}) get levels shifted by the median; applied: `{basis_shifts}`.",
        "- CIs: session-cluster bootstrap (1000×), 5–95%.",
        "",
        "## Universe → sample",
        "",
        f"- Universe: HOLD={univ_census['HOLD']} BREAK={univ_census['BREAK']} AMB={univ_census['AMBIGUOUS']} (n={universe.num_rows})",
        f"- Sampled: HOLD={samp_census['HOLD']} BREAK={samp_census['BREAK']} AMB={samp_census['AMBIGUOUS']} (n={events.num_rows})",
        f"- baseline_ok: HOLD={ok_c['HOLD']} BREAK={ok_c['BREAK']} AMB={ok_c['AMBIGUOUS']}",
        f"- ≤{first_split_min:.0f} min after open: HOLD={e_c['HOLD']} BREAK={e_c['BREAK']} · after: HOLD={l_c['HOLD']} BREAK={l_c['BREAK']}",
        "",
        "### Per-session concentration (sampled)",
        "",
        "| session | HOLD | BREAK | AMB |", "|---|---:|---:|---:|",
    ]
    for s, c in sorted(by_sess.items()):
        lines.append(f"| {s} | {c.get('HOLD', 0)} | {c.get('BREAK', 0)} | {c.get('AMBIGUOUS', 0)} |")
    lines += ["", "## Primary scores — pre-touch, baseline_ok", ""] + primary_md
    lines += ["", "## Longer pre-windows (5 s / 10 s / 60 s leg) — leak-free, baseline_ok", ""] + longpre_md
    lines += ["", f"## Placebo — same scores at levels ±{placebo_shift:g} pts away (baseline_ok)", ""]
    if placebo_md:
        lines += [f"Placebo census: `{placebo_census}`. If placebo δ ≈ real δ the separation is not about the level.", ""] + placebo_md
    else:
        lines += ["_no placebo events_"]
    lines += ["", "## Mechanism scores — see first 2 s of reaction (not leak-free)", ""] + mech_md
    lines += ["", f"## Reaction-confirmation at +{conf_cfg.confirm_ms / 1000:g} s — undecided events only", "",
              f"Undecided = neither ≥{conf_cfg.reject_pts:g} reject nor ≥{conf_cfg.extension_pts:g} extension reached by +{conf_cfg.confirm_ms / 1000:g} s. "
              f"baseline_ok undecided census: `{und_c}`; all undecided: `{census_labels(und_all)}`.", "",
              "### Scores on `[0, +2s]` among undecided HOLD/BREAK", ""] + conf_md + [""]
    for sc, rows_ in conf_tertiles.items():
        if rows_:
            lines += [f"**P(BREAK) by {sc} tertile:** " + " · ".join(f"{r['tertile']} n={r['n']} p={r['p_break']}" for r in rows_), ""]
    lines += ["### P&L — same entry, direction by gate vs unconditional vs plain reclaim", "",
              "`fade_tc`/`cont_tc` enter on the first print after +2 s (undecided only). `gated[s≥thr]` goes with the approach if s ≥ thr, fades if s ≤ −thr, else abstains. "
              f"`fade_reclaim` is the EXP-0042-style entry (fade on the first print back at the level after a ≥{conf_cfg.reclaim_min_pierce_pts:g} pt pierce), all events.", ""]
    for tp, sl in conf_cfg.brackets:
        lines += _pnl_md(f"tp{tp:g}_sl{sl:g}")
    lines += ["", "## Primary scores — all events (incl. baseline_ok=0)", ""] + all_md
    lines += ["", f"## Time-of-day split (primary + Take_at) — first {first_split_min:.0f} min", ""] + early_md
    lines += ["", f"## Time-of-day split — after {first_split_min:.0f} min", ""] + late_md
    lines += ["", "## Impulse tertiles (baseline_ok)", ""]
    for s in PRIMARY:
        t = tertile.get(s) or {}
        lines += [f"### {s}", ""]
        if not t or "note" in t:
            lines += ["_n/a_", ""]
            continue
        lines += ["| tertile | n_h | n_b | gap(b−h) | cliffs δ |", "|---|---:|---:|---:|---:|"]
        for nm in ("low", "mid", "high"):
            r = t.get(nm, {})
            lines.append(f"| {nm} | {r.get('n_hold')} | {r.get('n_break')} | {_r(r.get('median_gap_break_minus_hold'))} | {_r(r.get('cliffs_delta'))} |")
        lines.append("")
    lines += ["## Raw feature appendix (baseline_ok)", "", "| feature | med_h | med_b | gap(b−h) | cliffs δ |", "|---|---:|---:|---:|---:|"]
    for f_, st in raw_tbl.items():
        lines.append(f"| {f_} | {_r(st['median_hold'])} | {_r(st['median_break'])} | {_r(st['median_gap_break_minus_hold'])} | {_r(st['cliffs_delta_break_vs_hold'])} |")
    lines += ["", "## Chart-review IDs (BreakLikeness_pre)", "", "### Most break-like HOLDs", ""] + _chart_md(chart_bl_holds, "BreakLikeness_pre")
    lines += ["", "### Most hold-like BREAKs", ""] + _chart_md(chart_hl_breaks, "BreakLikeness_pre")
    lines += ["", f"Artifacts: `{out / 'events.parquet'}`, `{out / 'report.json'}`.", ""]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
