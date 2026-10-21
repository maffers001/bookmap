"""EXP-0043 / H34 — IRT hold vs break orderflow scores (October).

UK 14:30–17:00. NQ-only. Descriptive — no P&L book.
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
from research.correlated.harness import _prefer_single_feed, list_sessions, load_depth_window
from research.irt_lvn.hold_break import (
    HoldBreakConfig,
    census_labels,
    detect_hold_break_events,
    stratified_sample,
)
from research.irt_lvn.load_irt import default_irt_paths, load_irt_csv, session_id_to_ymd
from research.irt_lvn.of_scores import ScoreConfig, enrich_events_with_scores, prepare_session_of
from research.irt_lvn.run_exp_0042 import validate_timezone
from research.run_experiment import _load_mbo, _load_trades

UK = ZoneInfo("Europe/London")
OPEN = time(14, 30)
CLOSE = time(17, 0)

PRIMARY_SCORES = ("TakeScore", "StackScore", "TapeScore", "AttractRepelScore")
COMPANION = ("StackConsume", "BreakLikeness")
RAW_FEATURES = (
    "beyond_aggressor_vol", "holdside_aggressor_vol", "beyond_minus_hold_vol_z",
    "beyond_consume", "consume_frac", "take_flag",
    "near_sum", "near_max", "near_n_large", "near_n_large100", "far_sum",
    "net_add", "print_rate", "total_vol", "abs_imbalance", "burst_count",
    "post_vol", "post_prints", "impulse_pts", "impulse_speed", "first_pierce_pts",
    "pre_touch_vol", "cluster_n",
)


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


def load_mbo_window(
    parquet_dir: Path, sid: str, role: str, bounds_ns: tuple[int, int],
    batch_rows: int = 2_000_000,
) -> pa.Table:
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
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).astimezone(UK).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _median(a) -> float | None:
    x = np.asarray(a, dtype=np.float64)
    x = x[np.isfinite(x)]
    return float(np.median(x)) if len(x) else None


def _mean(a) -> float | None:
    x = np.asarray(a, dtype=np.float64)
    x = x[np.isfinite(x)]
    return float(np.mean(x)) if len(x) else None


def _iqr(a) -> float | None:
    x = np.asarray(a, dtype=np.float64)
    x = x[np.isfinite(x)]
    if len(x) < 2:
        return None
    return float(np.percentile(x, 75) - np.percentile(x, 25))


def cliffs_delta(a, b) -> float | None:
    """Cliff's delta: P(a>b) - P(a<b). Positive ⇒ a tends larger than b."""
    aa = np.asarray(a, dtype=np.float64)
    bb = np.asarray(b, dtype=np.float64)
    aa = aa[np.isfinite(aa)]
    bb = bb[np.isfinite(bb)]
    if len(aa) == 0 or len(bb) == 0:
        return None
    # Efficient approx via ranking if large
    if len(aa) * len(bb) > 2_000_000:
        # sample
        rng = np.random.default_rng(0)
        aa = rng.choice(aa, size=min(500, len(aa)), replace=False)
        bb = rng.choice(bb, size=min(500, len(bb)), replace=False)
    gt = lt = 0
    for x in aa:
        gt += int(np.sum(x > bb))
        lt += int(np.sum(x < bb))
    n = len(aa) * len(bb)
    return float((gt - lt) / n) if n else None


def cohens_d(a, b) -> float | None:
    aa = np.asarray(a, dtype=np.float64)
    bb = np.asarray(b, dtype=np.float64)
    aa = aa[np.isfinite(aa)]
    bb = bb[np.isfinite(bb)]
    if len(aa) < 2 or len(bb) < 2:
        return None
    m1, m2 = float(aa.mean()), float(bb.mean())
    s1, s2 = float(aa.std(ddof=1)), float(bb.std(ddof=1))
    n1, n2 = len(aa), len(bb)
    pooled = np.sqrt(((n1 - 1) * s1 ** 2 + (n2 - 1) * s2 ** 2) / max(1, n1 + n2 - 2))
    if pooled < 1e-12:
        return 0.0
    return float((m1 - m2) / pooled)


def compare_groups(hold: np.ndarray, brk: np.ndarray) -> dict:
    return {
        "n_hold": int(np.isfinite(hold).sum()),
        "n_break": int(np.isfinite(brk).sum()),
        "median_hold": _median(hold),
        "median_break": _median(brk),
        "iqr_hold": _iqr(hold),
        "iqr_break": _iqr(brk),
        "mean_hold": _mean(hold),
        "mean_break": _mean(brk),
        "median_gap_break_minus_hold": (
            None if _median(brk) is None or _median(hold) is None
            else round(_median(brk) - _median(hold), 4)
        ),
        "cliffs_delta_break_vs_hold": (
            None if cliffs_delta(brk, hold) is None else round(cliffs_delta(brk, hold), 4)
        ),
        "cohens_d_break_vs_hold": (
            None if cohens_d(brk, hold) is None else round(cohens_d(brk, hold), 4)
        ),
    }


def _col(tbl: pa.Table, name: str) -> np.ndarray:
    if name not in tbl.column_names:
        return np.array([], dtype=np.float64)
    return tbl.column(name).to_numpy().astype(np.float64)


def split_label(tbl: pa.Table, label: str) -> pa.Table:
    lab = tbl.column("label").to_numpy(zero_copy_only=False)
    idx = [i for i in range(tbl.num_rows) if str(lab[i]) == label]
    return tbl.take(np.asarray(idx, dtype=np.int64)) if idx else tbl.slice(0, 0)


def loo_sign_stable(tbl: pa.Table, score: str) -> dict:
    """Leave-one-session-out: does sign(median_break - median_hold) survive dropping fattest day?"""
    if tbl.num_rows == 0 or "session_id" not in tbl.column_names:
        return {"stable": False, "reason": "empty"}
    lab = tbl.column("label").to_numpy(zero_copy_only=False)
    sid = tbl.column("session_id").to_numpy(zero_copy_only=False)
    vals = _col(tbl, score)
    # fattest day by event count among HOLD+BREAK
    counts: dict[str, int] = defaultdict(int)
    for i in range(tbl.num_rows):
        if str(lab[i]) in ("HOLD", "BREAK"):
            counts[str(sid[i])] += 1
    if not counts:
        return {"stable": False, "reason": "no_hb"}
    fat = max(counts, key=counts.get)
    mask = np.array([str(sid[i]) != fat for i in range(tbl.num_rows)])
    hold = vals[(np.array([str(x) == "HOLD" for x in lab])) & mask]
    brk = vals[(np.array([str(x) == "BREAK" for x in lab])) & mask]
    full_h = vals[np.array([str(x) == "HOLD" for x in lab])]
    full_b = vals[np.array([str(x) == "BREAK" for x in lab])]
    g_full = (_median(full_b) or 0) - (_median(full_h) or 0)
    g_loo = (_median(brk) or 0) - (_median(hold) or 0)
    return {
        "dropped_session": fat,
        "dropped_n": counts[fat],
        "gap_full": round(g_full, 4),
        "gap_loo": round(g_loo, 4),
        "stable": bool(np.sign(g_full) == np.sign(g_loo) and abs(g_full) > 1e-9),
    }


def impulse_tertile_split(tbl: pa.Table, score: str) -> dict:
    hold = split_label(tbl, "HOLD")
    brk = split_label(tbl, "BREAK")
    if hold.num_rows == 0 or brk.num_rows == 0:
        return {}
    # tertiles on pooled impulse_pts
    hb = pa.concat_tables([hold, brk])
    imp = _col(hb, "impulse_pts")
    if len(imp) < 6:
        return {"note": "too_few"}
    q33, q66 = np.percentile(imp[np.isfinite(imp)], [33.3, 66.6])
    out = {}
    for name, lo, hi in (("low", -1e18, q33), ("mid", q33, q66), ("high", q66, 1e18)):
        def _mask(t, lo=lo, hi=hi):
            x = _col(t, "impulse_pts")
            return (x >= lo) & (x < hi) if name != "high" else (x >= lo)

        h = _col(hold, score)[_mask(hold)]
        b = _col(brk, score)[_mask(brk)]
        out[name] = {
            "n_hold": int(len(h)),
            "n_break": int(len(b)),
            "median_gap_break_minus_hold": (
                None if _median(b) is None or _median(h) is None
                else round(_median(b) - _median(h), 4)
            ),
            "cliffs_delta": (
                None if cliffs_delta(b, h) is None else round(cliffs_delta(b, h), 4)
            ),
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="EXP-0043 IRT hold vs break OF")
    ap.add_argument("--config", default=str(ROOT / "research" / "experiments" / "EXP-0043.yaml"))
    ap.add_argument(
        "--month",
        default=None,
        help="Calendar month YYYY-MM (default from EXP-0043.yaml month_prefix)",
    )
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
        suffix = "" if (y_sel == 2026 and m_sel == 10) else f"_{y_sel}{m_sel:02d}"
        out = ROOT / "results" / f"EXP-0043{suffix}"
    out.mkdir(parents=True, exist_ok=True)

    max_align = float(
        args.max_align_err if args.max_align_err is not None
        else exp.get("max_align_err_pts", 25.0)
    )
    exclude = set(exp.get("exclude_sessions", []))

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
    )
    sc_cfg = ScoreConfig(
        large_size=int(exp.get("large_size", 50)),
        tick_radius=int(exp.get("tick_radius", 8)),
        far_band_ticks=int(exp.get("far_band_ticks", 20)),
        z_lookback_ms=int(exp.get("z_lookback_ms", 1_800_000)),
    )

    irt_paths = default_irt_paths(ROOT)
    nq_irt = load_irt_csv(irt_paths["NQ"])
    nq_days = nq_irt.calendar_days()

    # Month sessions with parquet + depth/mbo, exclude listed, with IRT day
    all_sids = list_sessions(pq_dir, exclude=exclude)
    eligible = []
    for sid in all_sids:
        try:
            y, m, d = session_id_to_ymd(sid)
        except Exception:
            continue
        if y != y_sel or m != m_sel:
            continue
        if (y, m, d) not in nq_days:
            continue
        # require depth+mbo
        dpath = pq_dir / f"session_id={sid}" / "depth.parquet"
        mpath = pq_dir / f"session_id={sid}" / "mbo.parquet"
        if not dpath.exists() or dpath.stat().st_size == 0:
            continue
        if not mpath.exists() or mpath.stat().st_size == 0:
            continue
        eligible.append(sid)
    if args.smoke:
        eligible = eligible[:1]
    print(f"Eligible {month_str} sessions: {eligible} -> {out}", flush=True)

    universe_parts: list[pa.Table] = []
    skipped_align: list[str] = []
    session_census: dict[str, dict] = {}

    for sid in eligible:
        print(f"[universe] {sid} …", flush=True)
        nq0 = _prefer_single_feed(_load_trades(pq_dir, sid, "NQ"), pq_dir, sid, "NQ")
        if nq0.num_rows == 0:
            session_census[sid] = {"note": "empty"}
            continue
        mid = int(nq0.column("ts_ns").to_numpy()[nq0.num_rows // 2])
        lo, hi = bounds(mid)
        # keep lookback context in trades for impulse
        look_ns = int(hb_cfg.impulse_lookback_ms * 1_000_000)
        nq_ctx = filt(nq0, lo - look_ns, hi + int(hb_cfg.decision_horizon_ms * 1_000_000))
        if nq_ctx.num_rows == 0:
            session_census[sid] = {"note": "empty_window"}
            continue

        y, m, d = session_id_to_ymd(sid)
        day_lo = int(datetime(y, m, d, 0, 0, tzinfo=UK).timestamp() * 1e9)
        day_hi = int(datetime(y, m, d, 23, 59, 59, tzinfo=UK).timestamp() * 1e9)
        nq_bars = nq_irt.slice_range(day_lo, day_hi)

        align = validate_timezone(nq_bars, filt(nq0, lo, hi))
        med = align.get("median_abs_err_pts")
        if med is None or med >= max_align:
            skipped_align.append(sid)
            session_census[sid] = {"note": "skip_align", "align": align}
            print(f"  skip align {med}", flush=True)
            continue
        print(f"  align ok median_err={med}", flush=True)

        ev = detect_hold_break_events(nq_ctx, nq_bars, cfg=hb_cfg, window=(lo, hi))
        cen = census_labels(ev)
        session_census[sid] = {"align": align, "census": cen, "n": ev.num_rows}
        print(f"  census {cen}", flush=True)
        if ev.num_rows:
            ev = ev.append_column("session_id", pa.array([sid] * ev.num_rows, type=pa.string()))
            # UK clock helpers
            tod = []
            uk_s = []
            for i in range(ev.num_rows):
                t = int(ev.column("event_ts_ns")[i].as_py())
                uk_s.append(_uk_str(t))
                dt = datetime.fromtimestamp(t / 1e9, tz=timezone.utc).astimezone(UK)
                tod.append(dt.hour * 60 + dt.minute)
            ev = ev.append_column("uk_time", pa.array(uk_s, type=pa.string()))
            ev = ev.append_column("tod_minute", pa.array(tod, type=pa.int32()))
            universe_parts.append(ev)

    if not universe_parts:
        print("No universe events.", flush=True)
        (out / "summary.md").write_text("# EXP-0043\n\nNo events.\n", encoding="utf-8")
        return

    universe = pa.concat_tables(universe_parts, promote_options="default")
    univ_census = census_labels(universe)
    print(f"Universe census: {univ_census} total={universe.num_rows}", flush=True)

    # Stratified sample
    sampled = stratified_sample(
        universe,
        n_per_class=int(exp.get("sample_n_per_class", 100)),
        min_per_class=int(exp.get("sample_min_per_class", 50)),
        max_per_session=int(exp.get("max_per_session", 12)),
        seed=int(exp.get("sample_seed", 42)),
    )
    samp_census = census_labels(sampled)
    print(f"Sampled census: {samp_census} total={sampled.num_rows}", flush=True)

    # Persist universe + sample (pre-score)
    pq.write_table(universe, out / "universe.parquet")
    pq.write_table(sampled, out / "sample_pre_score.parquet")

    if args.census_only:
        report = {
            "universe": univ_census,
            "sampled": samp_census,
            "sessions": session_census,
            "skipped_align": skipped_align,
        }
        (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        (out / "summary.md").write_text(
            f"# EXP-0043 census\n\nUniverse `{univ_census}`\n\nSampled `{samp_census}`\n",
            encoding="utf-8",
        )
        return

    # Score by session
    scored_parts: list[pa.Table] = []
    sids_sample = sorted({str(x) for x in sampled.column("session_id").to_numpy(zero_copy_only=False)})
    for sid in sids_sample:
        print(f"[score] {sid} …", flush=True)
        idx = [
            i for i in range(sampled.num_rows)
            if str(sampled.column("session_id")[i].as_py()) == sid
        ]
        sev = sampled.take(np.asarray(idx, dtype=np.int64))

        nq0 = _prefer_single_feed(_load_trades(pq_dir, sid, "NQ"), pq_dir, sid, "NQ")
        mid = int(nq0.column("ts_ns").to_numpy()[nq0.num_rows // 2])
        lo, hi = bounds(mid)
        look_ns = int(sc_cfg.z_lookback_ms * 1_000_000)
        post_ns = int(sc_cfg.diagnostic_post_ms * 1_000_000)
        depth_bounds = (lo - look_ns, hi + post_ns)
        print(f"  loading depth/mbo window…", flush=True)
        depth = load_depth_window(pq_dir, sid, "NQ", depth_bounds)
        mbo = load_mbo_window(pq_dir, sid, "NQ", depth_bounds)
        trades = filt(nq0, depth_bounds[0], depth_bounds[1])
        print(
            f"  depth={depth.num_rows} mbo={mbo.num_rows} trades={trades.num_rows} events={sev.num_rows}",
            flush=True,
        )
        sof = prepare_session_of(trades, depth, mbo, cfg=sc_cfg, bounds=(lo, hi))
        if sof is None:
            print(f"  skip: no OF feeds", flush=True)
            scored_parts.append(sev)
            continue
        scored = enrich_events_with_scores(sev, sof)
        scored_parts.append(scored)
        print(f"  scored {scored.num_rows}", flush=True)

    events = pa.concat_tables(scored_parts, promote_options="default")
    pq.write_table(events, out / "events.parquet")

    hold = split_label(events, "HOLD")
    brk = split_label(events, "BREAK")
    amb = split_label(events, "AMBIGUOUS")

    score_table = {}
    for s in PRIMARY_SCORES + COMPANION:
        score_table[s] = compare_groups(_col(hold, s), _col(brk, s))
        if amb.num_rows and s in amb.column_names:
            score_table[s]["median_ambiguous"] = _median(_col(amb, s))

    loo = {s: loo_sign_stable(events, s) for s in PRIMARY_SCORES}
    tertile = {s: impulse_tertile_split(events, s) for s in PRIMARY_SCORES}

    raw_table = {}
    for f in RAW_FEATURES:
        if f not in events.column_names:
            continue
        raw_table[f] = compare_groups(_col(hold, f), _col(brk, f))

    # Per-session concentration
    by_sess: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    lab = events.column("label").to_numpy(zero_copy_only=False)
    sid_a = events.column("session_id").to_numpy(zero_copy_only=False)
    for i in range(events.num_rows):
        by_sess[str(sid_a[i])][str(lab[i])] += 1

    # Chart-review: top/bottom BreakLikeness per class
    def _extremes(tbl: pa.Table, n: int = 20) -> list[dict]:
        if tbl.num_rows == 0 or "BreakLikeness" not in tbl.column_names:
            return []
        bl = _col(tbl, "BreakLikeness")
        order = np.argsort(bl)
        picks = list(order[:n]) + list(order[-n:])
        picks = sorted(set(int(x) for x in picks))
        rows = []
        for i in picks:
            rows.append({
                "session_id": str(tbl.column("session_id")[i].as_py()),
                "uk_time": str(tbl.column("uk_time")[i].as_py()),
                "label": str(tbl.column("label")[i].as_py()),
                "level_px": float(tbl.column("level_px")[i].as_py()),
                "level_kinds": str(tbl.column("level_kinds")[i].as_py()),
                "approach": str(tbl.column("approach_str")[i].as_py()),
                "first_pierce_pts": float(tbl.column("first_pierce_pts")[i].as_py()),
                "TakeScore": float(tbl.column("TakeScore")[i].as_py()),
                "StackScore": float(tbl.column("StackScore")[i].as_py()),
                "TapeScore": float(tbl.column("TapeScore")[i].as_py()),
                "AttractRepelScore": float(tbl.column("AttractRepelScore")[i].as_py()),
                "BreakLikeness": float(bl[i]),
            })
        rows.sort(key=lambda r: r["BreakLikeness"])
        return rows

    # Most break-like holds / most hold-like breaks
    hold_bl = _col(hold, "BreakLikeness") if hold.num_rows else np.array([])
    brk_bl = _col(brk, "BreakLikeness") if brk.num_rows else np.array([])
    chart_hold_like_breaks = []
    chart_break_like_holds = []
    if hold.num_rows and len(hold_bl):
        top = np.argsort(hold_bl)[-15:][::-1]
        for i in top:
            chart_break_like_holds.append({
                "session_id": str(hold.column("session_id")[int(i)].as_py()),
                "uk_time": str(hold.column("uk_time")[int(i)].as_py()),
                "level_px": float(hold.column("level_px")[int(i)].as_py()),
                "level_kinds": str(hold.column("level_kinds")[int(i)].as_py()),
                "approach": str(hold.column("approach_str")[int(i)].as_py()),
                "BreakLikeness": float(hold_bl[int(i)]),
                "TakeScore": float(hold.column("TakeScore")[int(i)].as_py()),
                "StackScore": float(hold.column("StackScore")[int(i)].as_py()),
                "TapeScore": float(hold.column("TapeScore")[int(i)].as_py()),
                "AttractRepelScore": float(hold.column("AttractRepelScore")[int(i)].as_py()),
                "first_pierce_pts": float(hold.column("first_pierce_pts")[int(i)].as_py()),
            })
    if brk.num_rows and len(brk_bl):
        bot = np.argsort(brk_bl)[:15]
        for i in bot:
            chart_hold_like_breaks.append({
                "session_id": str(brk.column("session_id")[int(i)].as_py()),
                "uk_time": str(brk.column("uk_time")[int(i)].as_py()),
                "level_px": float(brk.column("level_px")[int(i)].as_py()),
                "level_kinds": str(brk.column("level_kinds")[int(i)].as_py()),
                "approach": str(brk.column("approach_str")[int(i)].as_py()),
                "BreakLikeness": float(brk_bl[int(i)]),
                "TakeScore": float(brk.column("TakeScore")[int(i)].as_py()),
                "StackScore": float(brk.column("StackScore")[int(i)].as_py()),
                "TapeScore": float(brk.column("TapeScore")[int(i)].as_py()),
                "AttractRepelScore": float(brk.column("AttractRepelScore")[int(i)].as_py()),
                "first_pierce_pts": float(brk.column("first_pierce_pts")[int(i)].as_py()),
            })

    report = {
        "experiment": "EXP-0043",
        "hypothesis": "H34",
        "month": month_str,
        "window": "14:30-17:00 UK",
        "eligible_sessions": eligible,
        "skipped_align": skipped_align,
        "sessions_used": [s for s in eligible if s not in skipped_align],
        "universe_census": univ_census,
        "universe_n": universe.num_rows,
        "sampled_census": samp_census,
        "sampled_n": events.num_rows,
        "per_session": {k: dict(v) for k, v in sorted(by_sess.items())},
        "session_census_detail": session_census,
        "score_table": score_table,
        "loo": loo,
        "impulse_tertile": tertile,
        "raw_features": raw_table,
        "chart_break_like_holds": chart_break_like_holds,
        "chart_hold_like_breaks": chart_hold_like_breaks,
        "params": exp,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    # Summary markdown
    lines = [
        "# EXP-0043 — IRT hold vs break orderflow (H34)",
        "",
        f"**Month:** {month_str} · UK **14:30–17:00**, NQ-only. "
        f"Align gate median |IRT−Bookmap| < {max_align} pts.",
        f"Sessions used: `{report['sessions_used']}`. Skipped align: `{skipped_align}`.",
        "",
        "## Universe → sample",
        "",
        f"- Universe: HOLD={univ_census['HOLD']} BREAK={univ_census['BREAK']} "
        f"AMBIGUOUS={univ_census['AMBIGUOUS']} (n={universe.num_rows})",
        f"- Sampled: HOLD={samp_census['HOLD']} BREAK={samp_census['BREAK']} "
        f"AMBIGUOUS={samp_census['AMBIGUOUS']} (n={events.num_rows})",
        "",
        "### Per-session concentration (sampled)",
        "",
        "| session | HOLD | BREAK | AMB |",
        "|---|---:|---:|---:|",
    ]
    for s, c in sorted(by_sess.items()):
        lines.append(
            f"| {s} | {c.get('HOLD', 0)} | {c.get('BREAK', 0)} | {c.get('AMBIGUOUS', 0)} |"
        )

    lines += [
        "",
        "## Primary scores (break − hold median gap)",
        "",
        "| score | n_h | n_b | med_h | med_b | gap(b−h) | cliffs δ | Cohen d | LOO stable |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for s in PRIMARY_SCORES:
        st = score_table[s]
        lo = loo[s]
        lines.append(
            f"| {s} | {st['n_hold']} | {st['n_break']} | "
            f"{_r(st['median_hold'])} | {_r(st['median_break'])} | "
            f"{_r(st['median_gap_break_minus_hold'])} | "
            f"{_r(st['cliffs_delta_break_vs_hold'])} | "
            f"{_r(st['cohens_d_break_vs_hold'])} | "
            f"{'yes' if lo.get('stable') else 'no'} (drop {lo.get('dropped_session')}) |"
        )
    lines += [
        "",
        "### Companions",
        "",
        "| score | med_h | med_b | gap(b−h) | cliffs δ |",
        "|---|---:|---:|---:|---:|",
    ]
    for s in COMPANION:
        st = score_table[s]
        lines.append(
            f"| {s} | {_r(st['median_hold'])} | {_r(st['median_break'])} | "
            f"{_r(st['median_gap_break_minus_hold'])} | {_r(st['cliffs_delta_break_vs_hold'])} |"
        )

    lines += ["", "## Impulse-tertile (does score separate inside same approach strength?)", ""]
    for s in PRIMARY_SCORES:
        lines.append(f"### {s}")
        lines.append("")
        t = tertile.get(s) or {}
        if not t:
            lines.append("_n/a_")
            continue
        lines.append("| tertile | n_h | n_b | gap(b−h) | cliffs δ |")
        lines.append("|---|---:|---:|---:|---:|")
        for name in ("low", "mid", "high"):
            row = t.get(name, {})
            lines.append(
                f"| {name} | {row.get('n_hold')} | {row.get('n_break')} | "
                f"{_r(row.get('median_gap_break_minus_hold'))} | {_r(row.get('cliffs_delta'))} |"
            )
        lines.append("")

    lines += [
        "## Ambiguous medians (3-way gradient)",
        "",
        "| score | HOLD | AMB | BREAK |",
        "|---|---:|---:|---:|",
    ]
    for s in PRIMARY_SCORES:
        st = score_table[s]
        lines.append(
            f"| {s} | {_r(st['median_hold'])} | {_r(st.get('median_ambiguous'))} | {_r(st['median_break'])} |"
        )

    lines += [
        "",
        "## Raw feature appendix (selected)",
        "",
        "| feature | med_h | med_b | gap(b−h) | cliffs δ |",
        "|---|---:|---:|---:|---:|",
    ]
    for f, st in raw_table.items():
        lines.append(
            f"| {f} | {_r(st['median_hold'])} | {_r(st['median_break'])} | "
            f"{_r(st['median_gap_break_minus_hold'])} | {_r(st['cliffs_delta_break_vs_hold'])} |"
        )

    lines += [
        "",
        "## Chart-review IDs",
        "",
        "### Top break-like HOLDs (highest BreakLikeness)",
        "",
        "| session | UK time | level | kinds | side | BL | Take | Stack | Tape | AR | pierce |",
        "|---|---|---:|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in chart_break_like_holds:
        lines.append(
            f"| {r['session_id']} | {r['uk_time']} | {r['level_px']:.2f} | {r['level_kinds']} | "
            f"{r['approach']} | {_r(r['BreakLikeness'])} | {_r(r['TakeScore'])} | "
            f"{_r(r['StackScore'])} | {_r(r['TapeScore'])} | {_r(r['AttractRepelScore'])} | "
            f"{_r(r['first_pierce_pts'])} |"
        )
    lines += [
        "",
        "### Top hold-like BREAKs (lowest BreakLikeness)",
        "",
        "| session | UK time | level | kinds | side | BL | Take | Stack | Tape | AR | pierce |",
        "|---|---|---:|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in chart_hold_like_breaks:
        lines.append(
            f"| {r['session_id']} | {r['uk_time']} | {r['level_px']:.2f} | {r['level_kinds']} | "
            f"{r['approach']} | {_r(r['BreakLikeness'])} | {_r(r['TakeScore'])} | "
            f"{_r(r['StackScore'])} | {_r(r['TapeScore'])} | {_r(r['AttractRepelScore'])} | "
            f"{_r(r['first_pierce_pts'])} |"
        )

    lines += [
        "",
        "## Interpretation notes",
        "",
        "- Primary scores use only `[-2s, +2s]` around `t_touch` (no post-touch outcome leakage).",
        "- Expected: break > hold on TakeScore; Stack×consume separates; Tape intensity break ≥ hold.",
        "- A score \"matters\" if median gap is large, LOO-stable, and not just an impulse-size proxy.",
        "",
        f"Artifacts: `{out / 'events.parquet'}`, `{out / 'report.json'}`.",
        "",
    ]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out / 'summary.md'}", flush=True)


def _r(x, nd: int = 3):
    if x is None:
        return "—"
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return "—"


if __name__ == "__main__":
    main()
