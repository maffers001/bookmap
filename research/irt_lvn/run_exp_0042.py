"""EXP-0042 / H33 — IRT dual-level correlated S/R.

UK 14:30–21:00. October IRT ∩ Bookmap overlap. Writes results/EXP-0042/.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import yaml

from research.backtest.mat_sim import (
    MatSimConfig,
    TradeResult,
    make_vol_bracket,
    simulate_session,
)
from research.config import ROOT, load_config
from research.correlated.harness import _prefer_single_feed
from research.irt_lvn.events import (
    IrtTouchConfig,
    MarketPierceConfig,
    classify_dual_events,
    detect_irt_touches,
    dual_irt_touch_events,
    pierce_bucket,
    reclaim_events_table,
)
from research.irt_lvn.load_irt import (
    coverage_stats,
    default_irt_paths,
    load_irt_csv,
    session_id_to_ymd,
)
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import _load_trades, _random_null_events

UK = ZoneInfo("Europe/London")
COMMISSION = 0.25
OPEN = time(14, 30)
CLOSE = time(21, 0)


def _mean(col) -> float | None:
    if col is None:
        return None
    a = np.asarray(col.to_numpy() if hasattr(col, "to_numpy") else col, dtype=np.float64)
    a = a[np.isfinite(a)]
    return float(np.mean(a)) if len(a) else None


def _pack_trades(trades: list[TradeResult], commission: float = COMMISSION) -> dict:
    if not trades:
        return {
            "n": 0, "net": 0.0, "avg": None, "days": 0, "days_pos": 0,
            "trades_per_day": 0.0, "wins": 0, "max_dd": 0.0,
        }
    net = 0.0
    wins = 0
    equity = peak = max_dd = 0.0
    by_day: dict[str, float] = defaultdict(float)
    for tr in trades:
        n = tr.pnl - commission
        net += n
        if tr.win:
            wins += 1
        equity += n
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        by_day[tr.session_id] += n
    days = len(by_day)
    return {
        "n": len(trades),
        "net": round(net, 2),
        "avg": round(net / len(trades), 3),
        "days": days,
        "days_pos": sum(1 for v in by_day.values() if v > 0),
        "trades_per_day": round(len(trades) / days, 2) if days else 0.0,
        "wins": wins,
        "hit_rate": round(wins / len(trades), 3),
        "max_dd": round(max_dd, 2),
        "worst_day": round(min(by_day.values()), 2) if by_day else None,
        "by_session": {k: round(v, 2) for k, v in sorted(by_day.items())},
    }


def _pack_strata(store: dict[str, list[TradeResult]], commission: float = COMMISSION) -> dict:
    return {k: _pack_trades(v, commission) for k, v in sorted(store.items(), key=lambda kv: -len(kv[1]))}


def _simulate(
    sid: str,
    nq: pa.Table,
    es: pa.Table,
    lo: int,
    events: pa.Table,
    sim_cfg: MatSimConfig,
    bracket_fn,
) -> list[TradeResult]:
    if events.num_rows == 0:
        return []
    return simulate_session(
        sid, nq, es, sim_cfg, gate=None, bracket_fn=bracket_fn,
        vwap_from_ns=lo, events=events,
    ).trades


def _split_by_pair(events: pa.Table) -> dict[str, pa.Table]:
    """Partition event table by nq_kind x es_kind."""
    if events.num_rows == 0:
        return {}
    if "nq_kind" not in events.column_names or "es_kind" not in events.column_names:
        return {"ALL": events}
    nq_k = events.column("nq_kind").to_pylist()
    es_k = events.column("es_kind").to_pylist()
    buckets: dict[str, list[int]] = defaultdict(list)
    for i, (a, b) in enumerate(zip(nq_k, es_k)):
        buckets[f"{a}x{b}"].append(i)
    return {k: events.take(np.asarray(ix, dtype=np.int64)) for k, ix in buckets.items()}


def _split_by_nq_kind(events: pa.Table) -> dict[str, pa.Table]:
    if events.num_rows == 0 or "nq_kind" not in events.column_names:
        return {}
    kinds = events.column("nq_kind").to_pylist()
    buckets: dict[str, list[int]] = defaultdict(list)
    for i, k in enumerate(kinds):
        buckets[str(k)].append(i)
    return {k: events.take(np.asarray(ix, dtype=np.int64)) for k, ix in buckets.items()}


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


def list_parquet_sessions(pq: Path) -> list[str]:
    out = []
    for p in sorted(pq.iterdir()):
        if not p.is_dir() or not p.name.startswith("session_id="):
            continue
        if "smoke" in p.name:
            continue
        if (p / "trades.parquet").exists():
            out.append(p.name.replace("session_id=", ""))
    return out


def validate_timezone(
    bars_nq,
    nq_trades: pa.Table,
    *,
    samples: int = 20,
) -> dict:
    """Compare IRT Close to Bookmap NQ mid at matching timestamps."""
    if nq_trades.num_rows == 0 or len(bars_nq.ts_ns) == 0:
        return {"ok": False, "reason": "empty"}
    ts = nq_trades.column("ts_ns").to_numpy().astype(np.int64)
    px = nq_trades.column("price").to_numpy().astype(np.float64) * 0.25
    order = np.argsort(ts)
    ts, px = ts[order], px[order]
    # pick IRT bars inside trade range
    m = (bars_nq.ts_ns >= ts[0]) & (bars_nq.ts_ns <= ts[-1]) & (bars_nq.close != 0)
    idxs = np.where(m)[0]
    if len(idxs) == 0:
        return {"ok": False, "reason": "no_overlap"}
    rng = np.random.default_rng(0)
    pick = rng.choice(idxs, size=min(samples, len(idxs)), replace=False)
    errs = []
    for i in pick:
        t = int(bars_nq.ts_ns[i])
        j = int(np.searchsorted(ts, t, side="right") - 1)
        if j < 0:
            continue
        errs.append(abs(float(bars_nq.close[i]) - float(px[j])))
    if not errs:
        return {"ok": False, "reason": "no_samples"}
    med = float(np.median(errs))
    return {
        "ok": med < 25.0,  # allow some bar/mid mismatch
        "median_abs_err_pts": round(med, 3),
        "p90_abs_err_pts": round(float(np.percentile(errs, 90)), 3),
        "n": len(errs),
        "note": "IRT Date assumed Europe/London",
    }


def hit10(lab: pa.Table) -> float | None:
    if lab.num_rows == 0 or "hit_10pts" not in lab.column_names:
        return None
    return _mean(lab.column("hit_10pts"))


def _r(x: float | None, nd: int = 3) -> float | None:
    return None if x is None else round(float(x), nd)


def path_stats(lab: pa.Table) -> dict:
    if lab.num_rows == 0:
        return {"n": 0}
    out = {
        "n": int(lab.num_rows),
        "hit10": _r(hit10(lab)),
        "mfe": _r(_mean(lab.column("mfe_pts")), 2) if "mfe_pts" in lab.column_names else None,
        "mae": _r(_mean(lab.column("mae_pts")), 2) if "mae_pts" in lab.column_names else None,
    }
    if "ret_pts_60000ms" in lab.column_names:
        out["ret60"] = _r(_mean(lab.column("ret_pts_60000ms")), 2)
    return out


def load_exp_yaml() -> dict:
    path = Path(__file__).resolve().parents[1] / "experiments" / "EXP-0042.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sync-ms", type=int, default=None, help="Override primary sync_ms")
    ap.add_argument(
        "--month",
        type=str,
        default=None,
        help="Calendar month YYYY-MM (default from EXP-0042.yaml month_prefix)",
    )
    ap.add_argument(
        "--max-align-err",
        type=float,
        default=25.0,
        help="Skip sessions where IRT Close vs Bookmap NQ median |err| exceeds this (pts)",
    )
    ap.add_argument("--smoke", action="store_true", help="First session only")
    args = ap.parse_args()

    exp = load_exp_yaml()
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    month_str = args.month or str(exp.get("month_prefix", "2026-10"))
    y_sel, m_sel = (int(x) for x in month_str.split("-")[:2])
    tag = "EXP-0042" if (y_sel, m_sel) == (2026, 10) else f"EXP-0042_{y_sel}{m_sel:02d}"
    out = Path(cfg["results_dir"]) / tag
    out.mkdir(parents=True, exist_ok=True)

    irt_paths = default_irt_paths(ROOT)
    print(f"Loading IRT CSVs NQ={irt_paths['NQ'].name} ES={irt_paths['ES'].name} …", flush=True)
    nq_irt = load_irt_csv(irt_paths["NQ"])
    es_irt = load_irt_csv(irt_paths["ES"])
    cov = {
        "NQ": {**coverage_stats(nq_irt), "path": irt_paths["NQ"].name},
        "ES": {**coverage_stats(es_irt), "path": irt_paths["ES"].name},
    }
    print(f"IRT coverage: {json.dumps(cov)}", flush=True)

    touch_cfg = IrtTouchConfig(
        sync_ms=int(args.sync_ms or exp.get("sync_ms_primary", 5000)),
        touch_cooldown_ms=int(exp.get("touch_cooldown_ms", 8000)),
        decision_horizon_ms=int(exp.get("decision_horizon_ms", 120_000)),
        nq=MarketPierceConfig(
            float(exp["nq_pierce_typical"]),
            float(exp["nq_pierce_fail"]),
            float(exp["nq_reject_away"]),
            float(exp["nq_pad_pts"]),
        ),
        es=MarketPierceConfig(
            float(exp["es_pierce_typical"]),
            float(exp["es_pierce_fail"]),
            float(exp["es_reject_away"]),
            float(exp["es_pad_pts"]),
        ),
    )
    sync_sweep = list(exp.get("sync_ms_sweep", [2000, 5000, 10000]))
    horizons = list(exp.get("horizons_ms", [10_000, 30_000, 60_000]))
    targets = [float(x) for x in exp.get("point_targets", [5, 10, 20])]

    sessions = list_parquet_sessions(pq)
    nq_days = nq_irt.calendar_days()
    es_days = es_irt.calendar_days()
    eligible = []
    for sid in sessions:
        y, m, d = session_id_to_ymd(sid)
        if (y, m) != (y_sel, m_sel):
            continue
        if (y, m, d) not in nq_days or (y, m, d) not in es_days:
            continue
        eligible.append(sid)
    if args.smoke:
        eligible = eligible[:1]
    print(
        f"Eligible {month_str} sessions (IRT ES&NQ&parquet): {eligible} -> {out}",
        flush=True,
    )

    tz_checks: list[dict] = []
    skipped_align: list[str] = []
    sync_counts: dict[int, int] = {}
    all_dual_labeled: list[pa.Table] = []
    all_nq_labeled: list[pa.Table] = []
    all_null_labeled: list[pa.Table] = []
    all_classified: list[pa.Table] = []
    shallow_max = float(exp.get("shallow_pierce_max", 8.0))
    fixed_tp = float(exp.get("fixed_tp_pts", 12.5))
    fixed_sl = float(exp.get("fixed_sl_pts", 30.0))
    slip = float(exp.get("slip_ticks", 1.0))
    cool = float(exp.get("signal_cooldown_sec", 5.0))
    comm = float(exp.get("commission_pts_rt", COMMISSION))

    # bracket_name -> list[TradeResult]
    pnl_reclaim: dict[str, list[TradeResult]] = {"vol_k1": [], "fixed_12p5_30": []}
    pnl_shallow: dict[str, list[TradeResult]] = {"vol_k1": [], "fixed_12p5_30": []}
    pnl_touch: dict[str, list[TradeResult]] = {"vol_k1": [], "fixed_12p5_30": []}
    # kind-pair / nq-kind P&L on vol_k1 (primary yardstick)
    pair_reclaim: dict[str, list[TradeResult]] = defaultdict(list)
    pair_shallow: dict[str, list[TradeResult]] = defaultdict(list)
    nqkind_reclaim: dict[str, list[TradeResult]] = defaultdict(list)
    nqkind_shallow: dict[str, list[TradeResult]] = defaultdict(list)
    session_rows = []

    sim_vol = MatSimConfig(
        slip_ticks=slip, commission_pts_rt=comm,
        signal_cooldown_sec=cool, flat_only=True,
    )
    sim_fixed = MatSimConfig(
        slip_ticks=slip, commission_pts_rt=comm,
        signal_cooldown_sec=cool, flat_only=True,
        tp_pts=fixed_tp, sl_pts=fixed_sl,
    )
    br_vol = make_vol_bracket(1.0)
    bracket_specs = [
        ("vol_k1", sim_vol, br_vol),
        ("fixed_12p5_30", sim_fixed, None),
    ]

    for sid in eligible:
        print(f"[{tag}] {sid} …", flush=True)
        nq0 = _prefer_single_feed(_load_trades(pq, sid, "NQ"), pq, sid, "NQ")
        es0 = _prefer_single_feed(_load_trades(pq, sid, "ES"), pq, sid, "ES")
        if nq0.num_rows == 0 or es0.num_rows == 0:
            session_rows.append({"session": sid, "note": "empty trades"})
            continue
        mid = int(nq0.column("ts_ns").to_numpy()[nq0.num_rows // 2])
        lo, hi = bounds(mid)
        nq = filt(nq0, lo, hi)
        es = filt(es0, lo, hi)
        if nq.num_rows == 0 or es.num_rows == 0:
            session_rows.append({"session": sid, "note": "empty window"})
            continue

        y, m, d = session_id_to_ymd(sid)
        day_lo = int(datetime(y, m, d, 0, 0, tzinfo=UK).timestamp() * 1e9)
        day_hi = int(datetime(y, m, d, 23, 59, 59, tzinfo=UK).timestamp() * 1e9)
        nq_bars = nq_irt.slice_range(day_lo, day_hi)
        es_bars = es_irt.slice_range(day_lo, day_hi)

        align = validate_timezone(nq_bars, nq)
        align["session"] = sid
        # override ok threshold from CLI
        med = align.get("median_abs_err_pts")
        align["ok"] = bool(med is not None and med < float(args.max_align_err))
        tz_checks.append(align)
        if not align["ok"]:
            skipped_align.append(sid)
            session_rows.append({
                "session": sid,
                "note": "skip_price_align",
                "align": align,
            })
            print(
                f"  skip align median_err={med} (max {args.max_align_err})",
                flush=True,
            )
            continue
        print(f"  align ok median_err={med}", flush=True)

        nq_touches = detect_irt_touches(
            nq, nq_bars, touch_cfg.nq, cooldown_ms=touch_cfg.touch_cooldown_ms,
        )
        es_touches = detect_irt_touches(
            es, es_bars, touch_cfg.es, cooldown_ms=touch_cfg.touch_cooldown_ms,
        )

        for sm in sync_sweep:
            dual_s, meta_s = dual_irt_touch_events(nq_touches, es_touches, sync_ms=sm)
            sync_counts[sm] = sync_counts.get(sm, 0) + int(meta_s.get("dual_touches", 0))

        dual, meta = dual_irt_touch_events(
            nq_touches, es_touches, sync_ms=touch_cfg.sync_ms,
        )
        nq_path_ts, nq_path_px = build_price_path(nq)
        es_path_ts, es_path_px = build_price_path(es)

        classified = classify_dual_events(
            dual, nq_path_ts, nq_path_px, es_path_ts, es_path_px, touch_cfg,
        )
        if classified.num_rows:
            sid_col = pa.array([sid] * classified.num_rows)
            classified = classified.append_column("session_id", sid_col)
            all_classified.append(classified)

        # Path study: touch stamp fade side
        lab_dual = (
            label_events(dual, nq_path_ts, nq_path_px, horizons, targets)
            if dual.num_rows else pa.table({})
        )
        lab_nq = (
            label_events(nq_touches, nq_path_ts, nq_path_px, horizons, targets)
            if nq_touches.num_rows else pa.table({})
        )
        null_ev = _random_null_events(
            nq, max(int(dual.num_rows), 50), seed=hash(sid) % (2**31),
        )
        # null sides as fade-random already; label
        lab_null = label_events(null_ev, nq_path_ts, nq_path_px, horizons, targets)

        for lab, bucket in (
            (lab_dual, all_dual_labeled),
            (lab_nq, all_nq_labeled),
            (lab_null, all_null_labeled),
        ):
            if lab.num_rows:
                bucket.append(lab.append_column("session_id", pa.array([sid] * lab.num_rows)))

        # P&L: reclaim / shallow / touch × (vol k=1 + fixed 12.5/30)
        rec = reclaim_events_table(classified)
        pierce = (
            rec.column("nq_pierce_pts").to_numpy().astype(np.float64)
            if rec.num_rows else np.asarray([], dtype=np.float64)
        )
        rec_sh = (
            rec.take(np.where(pierce < shallow_max)[0])
            if rec.num_rows and (pierce < shallow_max).any()
            else pa.table({})
        )
        touch_ev = (
            pa.table({
                "event_ts_ns": dual.column("event_ts_ns"),
                "side": dual.column("side"),
                "nq_kind": dual.column("nq_kind"),
                "es_kind": dual.column("es_kind"),
            })
            if dual.num_rows else pa.table({})
        )

        for bname, scfg, bfn in bracket_specs:
            pnl_reclaim[bname].extend(_simulate(sid, nq, es, lo, rec, scfg, bfn))
            pnl_shallow[bname].extend(_simulate(sid, nq, es, lo, rec_sh, scfg, bfn))
            pnl_touch[bname].extend(_simulate(sid, nq, es, lo, touch_ev, scfg, bfn))

        # Kind-pair + NQ-kind P&L on vol k=1 only (primary)
        for key, sub in _split_by_pair(rec).items():
            pair_reclaim[key].extend(_simulate(sid, nq, es, lo, sub, sim_vol, br_vol))
        for key, sub in _split_by_pair(rec_sh).items():
            pair_shallow[key].extend(_simulate(sid, nq, es, lo, sub, sim_vol, br_vol))
        for key, sub in _split_by_nq_kind(rec).items():
            nqkind_reclaim[key].extend(_simulate(sid, nq, es, lo, sub, sim_vol, br_vol))
        for key, sub in _split_by_nq_kind(rec_sh).items():
            nqkind_shallow[key].extend(_simulate(sid, nq, es, lo, sub, sim_vol, br_vol))

        joint = (
            Counter(classified.column("joint_outcome").to_pylist())
            if classified.num_rows else Counter()
        )
        session_rows.append({
            "session": sid,
            "nq_touches": int(nq_touches.num_rows),
            "es_touches": int(es_touches.num_rows),
            "dual": int(dual.num_rows),
            "hold": int(joint.get("hold", 0)),
            "fail": int(joint.get("fail", 0)),
            "ambiguous": int(joint.get("ambiguous", 0)),
            "reclaim_n": int(rec.num_rows) if rec.num_rows else 0,
            "shallow_n": int(rec_sh.num_rows) if rec_sh.num_rows else 0,
            "dual_hit10": hit10(lab_dual),
        })

    def concat(parts: list[pa.Table]) -> pa.Table:
        if not parts:
            return pa.table({})
        return pa.concat_tables(parts, promote_options="default")

    dual_lab = concat(all_dual_labeled)
    nq_lab = concat(all_nq_labeled)
    null_lab = concat(all_null_labeled)
    classified_all = concat(all_classified)

    # Stratification
    by_nq_kind: dict[str, dict] = {}
    by_pair: dict[str, dict] = {}
    pierce_rows = []
    outcome_counts = Counter()
    same_kind_n = cross_kind_n = 0

    if classified_all.num_rows:
        nq_kinds = classified_all.column("nq_kind").to_pylist()
        es_kinds = classified_all.column("es_kind").to_pylist()
        joints = classified_all.column("joint_outcome").to_pylist()
        pierces = classified_all.column("nq_pierce_pts").to_numpy()
        same = classified_all.column("same_kind").to_pylist()
        outcome_counts = Counter(joints)
        same_kind_n = sum(1 for x in same if x)
        cross_kind_n = len(same) - same_kind_n

        # path labels aligned by touch ts — join roughly via order within sessions
        # Build touch-path lookup from dual_lab
        path_by_ts: dict[int, dict] = {}
        if dual_lab.num_rows:
            for i in range(dual_lab.num_rows):
                t = int(dual_lab.column("event_ts_ns")[i].as_py())
                path_by_ts[t] = {
                    "hit10": int(dual_lab.column("hit_10pts")[i].as_py())
                    if "hit_10pts" in dual_lab.column_names else None,
                    "ret60": float(dual_lab.column("ret_pts_60000ms")[i].as_py())
                    if "ret_pts_60000ms" in dual_lab.column_names else None,
                    "mfe": float(dual_lab.column("mfe_pts")[i].as_py())
                    if "mfe_pts" in dual_lab.column_names else None,
                    "mae": float(dual_lab.column("mae_pts")[i].as_py())
                    if "mae_pts" in dual_lab.column_names else None,
                }

        def accum(store: dict, key: str, i: int) -> None:
            slot = store.setdefault(key, {
                "n": 0, "hold": 0, "fail": 0, "ambiguous": 0,
                "hit10s": [], "ret60s": [],
            })
            slot["n"] += 1
            slot[joints[i]] = slot.get(joints[i], 0) + 1
            t = int(classified_all.column("event_ts_ns")[i].as_py())
            info = path_by_ts.get(t)
            if info and info.get("hit10") is not None:
                slot["hit10s"].append(info["hit10"])
            if info and info.get("ret60") is not None:
                slot["ret60s"].append(info["ret60"])

        for i in range(classified_all.num_rows):
            accum(by_nq_kind, nq_kinds[i], i)
            accum(by_pair, f"{nq_kinds[i]}x{es_kinds[i]}", i)
            b = pierce_bucket(float(pierces[i]), nq=True)
            pierce_rows.append({
                "bucket": b,
                "outcome": joints[i],
                "hit10": path_by_ts.get(
                    int(classified_all.column("event_ts_ns")[i].as_py()), {},
                ).get("hit10"),
                "ret60": path_by_ts.get(
                    int(classified_all.column("event_ts_ns")[i].as_py()), {},
                ).get("ret60"),
            })

    def finalize_strata(store: dict) -> dict:
        out_s = {}
        for k, v in store.items():
            out_s[k] = {
                "n": v["n"],
                "hold": v.get("hold", 0),
                "fail": v.get("fail", 0),
                "ambiguous": v.get("ambiguous", 0),
                "hit10": round(float(np.mean(v["hit10s"])), 3) if v["hit10s"] else None,
                "ret60": round(float(np.mean(v["ret60s"])), 3) if v["ret60s"] else None,
            }
        return out_s

    pierce_table = []
    if pierce_rows:
        by_b: dict[str, list] = defaultdict(list)
        for r in pierce_rows:
            by_b[r["bucket"]].append(r)
        for b, rows in sorted(by_b.items()):
            hits = [r["hit10"] for r in rows if r["hit10"] is not None]
            rets = [r["ret60"] for r in rows if r["ret60"] is not None]
            pierce_table.append({
                "bucket": b,
                "n": len(rows),
                "hold": sum(1 for r in rows if r["outcome"] == "hold"),
                "fail": sum(1 for r in rows if r["outcome"] == "fail"),
                "hit10": round(float(np.mean(hits)), 3) if hits else None,
                "ret60": round(float(np.mean(rets)), 3) if rets else None,
            })

    reclaim_pnl = {k: _pack_trades(v) for k, v in pnl_reclaim.items()}
    shallow_reclaim_pnl = {k: _pack_trades(v) for k, v in pnl_shallow.items()}
    touch_pnl = {k: _pack_trades(v) for k, v in pnl_touch.items()}
    pair_reclaim_pnl = _pack_strata(pair_reclaim)
    pair_shallow_pnl = _pack_strata(pair_shallow)
    nqkind_reclaim_pnl = _pack_strata(nqkind_reclaim)
    nqkind_shallow_pnl = _pack_strata(nqkind_shallow)
    # primary aliases for decision / compat
    reclaim_vol = reclaim_pnl["vol_k1"]
    shallow_vol = shallow_reclaim_pnl["vol_k1"]
    touch_vol = touch_pnl["vol_k1"]

    # Hold-stratum path (touch stamps where joint hold)
    hold_path = {"n": 0}
    fail_path = {"n": 0}
    if classified_all.num_rows and dual_lab.num_rows:
        hold_ts = set()
        fail_ts = set()
        for i in range(classified_all.num_rows):
            t = int(classified_all.column("event_ts_ns")[i].as_py())
            j = classified_all.column("joint_outcome")[i].as_py()
            if j == "hold":
                hold_ts.add(t)
            elif j == "fail":
                fail_ts.add(t)
        if hold_ts:
            mask = [
                int(dual_lab.column("event_ts_ns")[i].as_py()) in hold_ts
                for i in range(dual_lab.num_rows)
            ]
            hold_path = path_stats(dual_lab.take(np.where(mask)[0]))
        if fail_ts:
            mask = [
                int(dual_lab.column("event_ts_ns")[i].as_py()) in fail_ts
                for i in range(dual_lab.num_rows)
            ]
            fail_path = path_stats(dual_lab.take(np.where(mask)[0]))

    report = {
        "experiment": tag,
        "hypothesis": "H33",
        "month": month_str,
        "window": "UK 14:30-21:00",
        "sync_ms_primary": touch_cfg.sync_ms,
        "sync_ms_sweep_dual_n": sync_counts,
        "irt_coverage": cov,
        "timezone_checks": tz_checks,
        "skipped_align": skipped_align,
        "max_align_err": float(args.max_align_err),
        "sessions_eligible": eligible,
        "sessions_used": [s for s in eligible if s not in skipped_align],
        "sessions": session_rows,
        "dual_path": path_stats(dual_lab),
        "nq_only_path": path_stats(nq_lab),
        "null_path": path_stats(null_lab),
        "outcomes": dict(outcome_counts),
        "same_kind_n": same_kind_n,
        "cross_kind_n": cross_kind_n,
        "by_nq_kind": finalize_strata(by_nq_kind),
        "by_kind_pair": finalize_strata(by_pair),
        "pierce_buckets": pierce_table,
        "hold_path": hold_path,
        "fail_path": fail_path,
        "reclaim_pnl": reclaim_pnl,
        "shallow_reclaim_pnl": shallow_reclaim_pnl,
        "touch_fade_pnl": touch_pnl,
        "reclaim_pnl_vol_k1": reclaim_vol,
        "shallow_reclaim_pnl_vol_k1": shallow_vol,
        "touch_fade_pnl_vol_k1": touch_vol,
        "kind_pair_reclaim_pnl_vol_k1": pair_reclaim_pnl,
        "kind_pair_shallow_pnl_vol_k1": pair_shallow_pnl,
        "nq_kind_reclaim_pnl_vol_k1": nqkind_reclaim_pnl,
        "nq_kind_shallow_pnl_vol_k1": nqkind_shallow_pnl,
        "fixed_bracket": {"tp_pts": fixed_tp, "sl_pts": fixed_sl},
        "shallow_pierce_max": shallow_max,
        "pierce_defaults": {
            "nq": touch_cfg.nq.__dict__ if touch_cfg.nq else {},
            "es": touch_cfg.es.__dict__ if touch_cfg.es else {},
        },
    }

    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Markdown summary
    lines = [
        f"# {tag} — IRT dual-level correlated S/R (H33)",
        "",
        f"**Month:** {month_str} · **Window:** UK 14:30–21:00 · "
        f"**sync_ms:** {touch_cfg.sync_ms} (sweep {sync_counts}) · "
        f"**sessions:** {eligible}",
        "",
        "## Data note",
        "",
        f"IRT files: NQ=`{irt_paths['NQ'].name}` ES=`{irt_paths['ES'].name}`.",
        "",
        f"Price align gate: median |IRT Close − Bookmap NQ| &lt; {args.max_align_err} pts. "
        f"Skipped (roll/basis): `{skipped_align}`.",
        "",
        f"Per-session align: `{tz_checks}`",
        "",
        "## Dual event study (fade side from touch)",
        "",
        f"| Arm | n | hit+10 | @60s | MFE | MAE |",
        f"|-----|---|-------|------|-----|-----|",
        _row("dual", report["dual_path"]),
        _row("nq_only", report["nq_only_path"]),
        _row("null", report["null_path"]),
        _row("dual hold", hold_path),
        _row("dual fail", fail_path),
        "",
        f"Outcomes: `{dict(outcome_counts)}` · same-kind={same_kind_n} · "
        f"cross-kind={cross_kind_n}",
        "",
        "## By NQ kind",
        "",
        _strata_md(report["by_nq_kind"]),
        "",
        "## By kind pair (NQ×ES)",
        "",
        _strata_md(report["by_kind_pair"]),
        "",
        "## Pierce buckets (NQ)",
        "",
        _pierce_md(pierce_table),
        "",
        f"## P&L (NQ, 1-tick slip, {comm} pt/RT, no gates)",
        "",
        f"Brackets: **vol k=1** and fixed **TP{fixed_tp}/SL{fixed_sl}**. "
        f"Shallow = NQ pierce **up to the reclaim print** &lt; {shallow_max} (causal). "
        "Entries: NQ reclaim by `reject_away` with ES not failed *by that time*; "
        "the eventual hold/fail outcome is **not** used to select entries.",
        "",
        _pnl_md("Reclaim fade", reclaim_pnl),
        "",
        _pnl_md(f"Shallow reclaim (pierce<{shallow_max})", shallow_reclaim_pnl),
        "",
        _pnl_md("Touch fade (exploratory)", touch_pnl),
        "",
        "## Kind-pair P&L (vol k=1)",
        "",
        "### Reclaim by NQ×ES kind",
        "",
        _pnl_strata_md(pair_reclaim_pnl),
        "",
        "### Shallow reclaim by NQ×ES kind",
        "",
        _pnl_strata_md(pair_shallow_pnl),
        "",
        "### Reclaim by NQ kind",
        "",
        _pnl_strata_md(nqkind_reclaim_pnl),
        "",
        "### Shallow reclaim by NQ kind",
        "",
        _pnl_strata_md(nqkind_shallow_pnl),
        "",
        "### Per-session reclaim net (vol k=1)",
        "",
        "```",
        json.dumps(reclaim_vol.get("by_session", {}), indent=2),
        "```",
        "",
        "### Per-session shallow reclaim net (vol k=1)",
        "",
        "```",
        json.dumps(shallow_vol.get("by_session", {}), indent=2),
        "```",
        "",
        "### Per-session dual counts",
        "",
        "```",
        json.dumps(session_rows, indent=2),
        "```",
        "",
        "## Decision",
        "",
    ]
    decision = _decide(
        reclaim_vol, report["dual_path"], hold_path, fail_path,
        [s for s in eligible if s not in skipped_align],
        shallow=shallow_vol,
    )
    lines.append(decision)
    lines.append("")
    lines.append(f"Full JSON: `results/{tag}/report.json`")
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(decision, flush=True)
    print(f"Wrote {out / 'summary.md'}", flush=True)


def _row(name: str, p: dict) -> str:
    return (
        f"| {name} | {p.get('n', 0)} | {p.get('hit10')} | {p.get('ret60')} | "
        f"{p.get('mfe')} | {p.get('mae')} |"
    )


def _strata_md(store: dict) -> str:
    if not store:
        return "_(empty)_"
    lines = [
        "| key | n | hold | fail | amb | hit10 | ret60 |",
        "|-----|---|------|------|-----|-------|-------|",
    ]
    for k, v in sorted(store.items(), key=lambda kv: -kv[1]["n"]):
        lines.append(
            f"| {k} | {v['n']} | {v['hold']} | {v['fail']} | {v['ambiguous']} | "
            f"{v['hit10']} | {v['ret60']} |"
        )
    return "\n".join(lines)


def _pnl_line(label: str, p: dict) -> str:
    return (
        f"**{label}:** n={p.get('n')} net={p.get('net')} avg={p.get('avg')} "
        f"days+={p.get('days_pos')}/{p.get('days')} "
        f"trades/day={p.get('trades_per_day')} maxDD={p.get('max_dd')}"
    )


def _pnl_md(title: str, by_bracket: dict[str, dict]) -> str:
    lines = [f"### {title}", ""]
    for bname, p in by_bracket.items():
        lines.append(_pnl_line(bname, p))
        lines.append("")
    return "\n".join(lines).rstrip()


def _pnl_strata_md(store: dict) -> str:
    if not store:
        return "_(empty)_"
    lines = [
        "| key | n | net | avg | days+ | tpd | maxDD |",
        "|-----|---|-----|-----|-------|-----|-------|",
    ]
    for k, v in store.items():
        lines.append(
            f"| {k} | {v.get('n')} | {v.get('net')} | {v.get('avg')} | "
            f"{v.get('days_pos')}/{v.get('days')} | {v.get('trades_per_day')} | "
            f"{v.get('max_dd')} |"
        )
    return "\n".join(lines)


def _pierce_md(rows: list[dict]) -> str:
    if not rows:
        return "_(empty)_"
    lines = [
        "| bucket | n | hold | fail | hit10 | ret60 |",
        "|--------|---|------|------|-------|-------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['bucket']} | {r['n']} | {r['hold']} | {r['fail']} | "
            f"{r['hit10']} | {r['ret60']} |"
        )
    return "\n".join(lines)


def _decide(reclaim, dual_path, hold_path, fail_path, sessions, shallow=None) -> str:
    n = reclaim.get("n", 0)
    hold_h = hold_path.get("hit10")
    fail_h = fail_path.get("hit10")
    net = reclaim.get("net")
    tpd = reclaim.get("trades_per_day")
    days_pos = reclaim.get("days_pos")
    days = reclaim.get("days")
    sh = shallow or {}
    sh_note = (
        f" Shallow(&lt;8) reclaim n={sh.get('n')} net={sh.get('net')} "
        f"days+={sh.get('days_pos')}/{sh.get('days')} tpd={sh.get('trades_per_day')}."
    )
    if len(sessions) < 8:
        return (
            f"**Smoke / thin sample** ({len(sessions)} sessions). Plumbing OK if "
            f"dual/reclaim events fire; **do not ship**. reclaim n={n} net={net}."
            f"{sh_note}"
        )
    if n < 30:
        return (
            f"**Park (thin):** reclaim n={n} < 30. "
            f"dual hit10={dual_path.get('hit10')} vs null — revisit after more days."
            f"{sh_note}"
        )
    # Honest yardstick is reclaim P&L; hold-path is outcome-selected diagnostic.
    sh_net = sh.get("net")
    if sh_net is not None and sh.get("n", 0) >= 20 and sh_net > (net or 0) and sh_net > 0:
        return (
            f"**Dig candidate (shallow reclaim):** all reclaim n={n} net={net}; "
            f"shallow n={sh.get('n')} net={sh_net} days+={sh.get('days_pos')}/{sh.get('days')} "
            f"tpd={sh.get('trades_per_day')}. hold hit10={hold_h} fail={fail_h}. "
            f"Next: kind-pair + CD/orderflow on shallow arm only."
        )
    if net is not None and net > 0 and days and days_pos is not None and days_pos >= max(3, days // 2):
        if hold_h is not None and fail_h is not None and hold_h > fail_h + 0.05:
            return (
                f"**Dig candidate:** reclaim n={n} net={net} days+={days_pos}/{days} "
                f"trades/day={tpd}; hold hit10={hold_h} > fail={fail_h} "
                f"(hold-path diagnostic).{sh_note}"
            )
        return (
            f"**Weak dig / watch:** reclaim n={n} net={net} days+={days_pos}/{days} "
            f"but hold/fail path asymmetry unclear. dual hit10={dual_path.get('hit10')}."
            f"{sh_note}"
        )
    return (
        f"**Park:** reclaim n={n} net={net} days+={days_pos}/{days}; "
        f"hold hit10={hold_h} fail={fail_h} dual={dual_path.get('hit10')} — "
        f"no ship on this October sample.{sh_note}"
    )


if __name__ == "__main__":
    main()
