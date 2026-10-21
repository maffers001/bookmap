"""EXP-0041 — Stop-run fingerprint (H32).

Stage A: event study in points (continue vs fade vs null vs corrected 0008).
Stage B: MAT P&L via simulate_session events override (continuation + fade@reclaim).
May 2026 held out. Writes results/EXP-0041/.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import yaml

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    TradeResult,
    combine_gates,
    make_trend_mode,
    make_vol_bracket,
    make_vwap_side,
    simulate_session,
)
from research.config import load_config
from research.correlated.harness import _prefer_single_feed
from research.features.momentum import (
    cross_confirm_bursts,
    momentum_burst_events,
    trades_to_tape_bars,
)
from research.features.stop_run import (
    StopRunConfig,
    fade_events_at_reclaim,
    resolve_outcome,
    stop_run_events,
)
from research.outcomes.label import build_price_path, label_events
from research.run_experiment import (
    _load_mbo,
    _load_trades,
    _mean,
    _median,
    _random_null_events,
    _summarize_labeled,
)

UK = ZoneInfo("Europe/London")
COMMISSION = 0.25
MAY = {
    "010526", "040526", "050526", "070526", "080526",
    "110526", "120526", "130526", "140526", "150526",
    "180526", "190526", "200526", "210526", "260526",
    "280526", "290526",
}


def bounds(mid: int) -> tuple[int, int]:
    d = datetime.fromtimestamp(mid / 1e9, tz=timezone.utc).astimezone(UK).date()
    return (
        int(datetime.combine(d, time(14, 30), tzinfo=UK).timestamp() * 1e9),
        int(datetime.combine(d, time(17, 0), tzinfo=UK).timestamp() * 1e9),
    )


def filt(t: pa.Table) -> pa.Table:
    if t.num_rows == 0:
        return t
    ts = t.column("ts_ns").to_numpy().astype(np.int64)
    s, e = bounds(int(ts[len(ts) // 2]))
    k = (ts >= s) & (ts <= e)
    return t.take(np.where(k)[0]) if k.any() else t.slice(0, 0)


def pack_trades(trades: list[TradeResult], commission: float = COMMISSION) -> dict:
    s = SimSummary()
    equity = peak = 0.0
    for tr in trades:
        s.trades.append(tr)
        s.n += 1
        s.gross += tr.pnl
        s.commission_total += commission
        net = tr.pnl - commission
        s.net += net
        if tr.win:
            s.wins += 1
        if tr.pnl > 0:
            s.gross_win += tr.pnl
        else:
            s.gross_loss += abs(tr.pnl)
        equity += net
        peak = max(peak, equity)
        s.max_dd = max(s.max_dd, peak - equity)
    d = s.to_dict()
    d["avg"] = round(s.net / s.n, 3) if s.n else None
    days = {tr.session_id for tr in trades}
    day_nets = [
        sum(tr.pnl - commission for tr in trades if tr.session_id == sid)
        for sid in days
    ]
    d["days"] = len(days)
    d["days_pos"] = sum(1 for x in day_nets if x > 0)
    d["worst_day"] = round(min(day_nets), 2) if day_nets else None
    if day_nets:
        ordered = sorted(day_nets, reverse=True)
        top3 = sum(ordered[:3])
        total_pos = sum(x for x in day_nets if x > 0) or 1.0
        d["top3_share"] = round(top3 / total_pos, 3) if total_pos > 0 else None
    else:
        d["top3_share"] = None
    # LOO: leave-one-day-out net never negative?
    if len(day_nets) >= 2:
        total = sum(day_nets)
        loo_neg = sum(1 for x in day_nets if total - x < 0)
        d["loo_neg"] = loo_neg
    else:
        d["loo_neg"] = None
    return d


def cohort_split(trades: list[TradeResult]) -> tuple[list[TradeResult], list[TradeResult]]:
    train = [t for t in trades if t.session_id not in MAY]
    may = [t for t in trades if t.session_id in MAY]
    return train, may


def _cfg_from_yaml(exp: dict) -> StopRunConfig:
    kinds = exp.get("level_kinds") or list(StopRunConfig().level_kinds)
    return StopRunConfig(
        pierce_ticks=int(exp.get("pierce_ticks", 2)),
        window_ms=int(exp.get("window_ms", 2000)),
        min_vol=float(exp.get("min_vol", 25.0)),
        min_prints=int(exp.get("min_prints", 8)),
        min_imbalance=float(exp.get("min_imbalance", 0.60)),
        min_sweeps=int(exp.get("min_sweeps", 1)),
        sweep_min_span_ticks=float(exp.get("sweep_min_span_ticks", 2.0)),
        sweep_min_prints=int(exp.get("sweep_min_prints", 3)),
        min_aggressors=int(exp.get("min_aggressors", 3)),
        min_score=int(exp.get("min_score", 2)),
        mbo_radius_ticks=int(exp.get("mbo_radius_ticks", 4)),
        mbo_pre_ms=int(exp.get("mbo_pre_ms", 1000)),
        mbo_min_cancels=int(exp.get("mbo_min_cancels", 5)),
        mbo_min_pull_ratio=float(exp.get("mbo_min_pull_ratio", 0.60)),
        sync_ms=int(exp.get("sync_ms", 1500)),
        cooldown_ms=int(exp.get("cooldown_ms", 5000)),
        reclaim_sec=float(exp.get("reclaim_sec", 5.0)),
        lookback_sec=float(exp.get("lookback_sec", 600.0)),
        pullaway_pts=float(exp.get("pullaway_pts", 20.0)),
        cluster_tol_pts=float(exp.get("cluster_tol_pts", 5.0)),
        invalidate_beyond_pts=float(exp.get("invalidate_beyond_pts", 5.0)),
        use_cluster=bool(exp.get("use_cluster", False)),
        level_kinds=frozenset(kinds),
    )


def _pack_labeled(parts: list[pa.Table], horizons: list, targets: list) -> dict | None:
    if not parts:
        return None
    t = pa.concat_tables(parts)
    out = {
        "n_events": int(t.num_rows),
        "p_hit_5pts": _mean(t.column("hit_5pts")) if "hit_5pts" in t.column_names else None,
        "p_hit_10pts": _mean(t.column("hit_10pts")) if "hit_10pts" in t.column_names else None,
        "p_hit_20pts": _mean(t.column("hit_20pts")) if "hit_20pts" in t.column_names else None,
        "mean_mfe_pts": _mean(t.column("mfe_pts")),
        "mean_mae_pts": _mean(t.column("mae_pts")),
    }
    for h in horizons:
        col = f"ret_pts_{h}ms"
        if col in t.column_names:
            out[f"mean_{col}"] = _mean(t.column(col))
            out[f"median_{col}"] = _median(t.column(col))
    return out


def _stratum(parts: list[pa.Table], mask_fn) -> list[pa.Table]:
    out = []
    for t in parts:
        if t.num_rows == 0:
            continue
        keep = mask_fn(t)
        if keep is None or not np.any(keep):
            continue
        out.append(t.take(np.where(keep)[0]))
    return out


def _aid_empty_rate(trades: pa.Table) -> float | None:
    if trades.num_rows == 0 or "aggressor_order_id" not in trades.column_names:
        return None
    aid = trades.column("aggressor_order_id").to_numpy(zero_copy_only=False)
    empty = sum(1 for a in aid if not a)
    return empty / len(aid)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-sessions", type=int, default=0, help="Limit sessions (0=all)")
    ap.add_argument("--skip-pnl", action="store_true", help="Stage A only")
    ap.add_argument("--no-mbo", action="store_true", help="Skip MBO load (faster)")
    args = ap.parse_args()

    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    exp_path = Path(__file__).resolve().parents[1] / "experiments" / "EXP-0041.yaml"
    with open(exp_path, encoding="utf-8") as f:
        exp = yaml.safe_load(f)
    sr_cfg = _cfg_from_yaml(exp)
    horizons = list(exp.get("horizons_ms") or [10000, 30000, 60000])
    targets = list(exp.get("point_targets") or [5, 10, 20])

    out = Path(cfg["results_dir"]) / "EXP-0041"
    out.mkdir(parents=True, exist_ok=True)

    sessions = sorted(
        p.name.replace("session_id=", "")
        for p in pq.iterdir()
        if p.is_dir()
        and p.name.startswith("session_id=")
        and (p / "trades.parquet").exists()
        and "smoke" not in p.name
    )
    if args.max_sessions > 0:
        sessions = sessions[: args.max_sessions]
    print(f"sessions: {len(sessions)}", flush=True)

    cont_parts: list[pa.Table] = []
    fade_parts: list[pa.Table] = []
    null_parts: list[pa.Table] = []
    mom_parts: list[pa.Table] = []
    sanity = []
    cont_trades: list[TradeResult] = []
    fade_trades: list[TradeResult] = []

    gate = combine_gates(make_trend_mode("with"), make_vwap_side(True))
    br = make_vol_bracket(1.0)
    sim_cfg = MatSimConfig(
        slip_ticks=float(exp.get("slip_ticks", 1.0)),
        commission_pts_rt=COMMISSION,
        es_stamp="causal",
    )
    burst_kw = dict(
        min_trades=6, min_vol=25.0, min_imbalance=0.55,
        min_progress_ticks=1.0, tick_size=1.0, cooldown_bars=3, bar_ms=100,
    )

    for sid in sessions:
        print(f"[stop-run] {sid} …", flush=True)
        nq0 = _load_trades(pq, sid, "NQ")
        es0 = _load_trades(pq, sid, "ES")
        nq0 = _prefer_single_feed(nq0, pq, sid, "NQ")
        es0 = _prefer_single_feed(es0, pq, sid, "ES")
        if nq0.num_rows == 0:
            sanity.append({"session": sid, "events": 0, "note": "no NQ"})
            continue

        # Levels need overnight context — build on full day, events filtered to RTH
        nq_rth = filt(nq0)
        es_rth = filt(es0) if es0.num_rows else es0
        if nq_rth.num_rows == 0:
            sanity.append({"session": sid, "events": 0, "note": "empty RTH"})
            continue

        mbo = None
        mbo_rows = 0
        if not args.no_mbo:
            mbo0 = _load_mbo(pq, sid, "NQ")
            mbo_rows = int(mbo0.num_rows)
            mbo = mbo0 if mbo_rows else None

        # Use full NQ for levels (overnight), but fingerprint only needs RTH pierce
        # Pass full nq0 so overnight levels exist; filter events to RTH after.
        ev = stop_run_events(nq0, es0 if es0.num_rows else None, mbo, sr_cfg)
        if ev.num_rows:
            # Keep events whose event_ts is inside RTH window
            mid = int(nq_rth.column("ts_ns").to_numpy()[nq_rth.num_rows // 2])
            s, e = bounds(mid)
            ets = ev.column("event_ts_ns").to_numpy().astype(np.int64)
            keep = (ets >= s) & (ets <= e)
            ev = ev.take(np.where(keep)[0]) if keep.any() else ev.slice(0, 0)

        ev = resolve_outcome(ev, nq0, reclaim_sec=sr_cfg.reclaim_sec)
        aid_empty = _aid_empty_rate(nq_rth)

        path_ts, path_px = build_price_path(nq_rth)

        # Continuation branch (side = run direction)
        lab_cont = label_events(ev, path_ts, path_px, horizons, targets) if ev.num_rows else pa.table({})
        # Fade branch: flip side on all events (fade the run); stratum stall later
        if ev.num_rows:
            fade_side = pa.array(-ev.column("side").to_numpy().astype(np.int8), type=pa.int8())
            ev_fade = ev
            if "side" in ev_fade.column_names:
                names = ev_fade.column_names
                cols = [
                    fade_side if n == "side" else ev_fade.column(n)
                    for n in names
                ]
                ev_fade = pa.table({n: c for n, c in zip(names, cols)})
            lab_fade = label_events(ev_fade, path_ts, path_px, horizons, targets)
        else:
            lab_fade = pa.table({})

        # Null + 0008
        null_ev = _random_null_events(nq_rth, max(int(ev.num_rows), 50), seed=hash(sid) % (2**31))
        lab_null = label_events(null_ev, path_ts, path_px, horizons, targets)
        nq_bars = trades_to_tape_bars(nq_rth, bar_ms=100)
        es_bars = trades_to_tape_bars(es_rth, bar_ms=100) if es_rth.num_rows else pa.table({})
        mom = cross_confirm_bursts(
            momentum_burst_events(nq_bars, **burst_kw),
            momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
            sync_ms=1500,
            stamp="confirm",
        )
        if mom.num_rows and "es_lag_ms" in mom.column_names:
            mom = mom.take(np.where(np.abs(mom.column("es_lag_ms").to_numpy()) <= 250)[0])
        lab_mom = label_events(mom, path_ts, path_px, horizons, targets)

        def _sid(t: pa.Table) -> pa.Table:
            if t.num_rows == 0:
                return t
            if "session_id" in t.column_names:
                t = t.drop("session_id")
            return t.append_column(
                "session_id", pa.array([sid] * t.num_rows, type=pa.string())
            )

        # Carry strata columns onto labeled tables
        def _merge_meta(lab: pa.Table, src: pa.Table) -> pa.Table:
            if lab.num_rows == 0 or src.num_rows == 0:
                return lab
            # Align by event_ts_ns
            lab_ts = lab.column("event_ts_ns").to_numpy().astype(np.int64)
            src_ts = src.column("event_ts_ns").to_numpy().astype(np.int64)
            # For fade-at-window-end, same ts; map src rows
            idx_map = {int(src_ts[i]): i for i in range(len(src_ts))}
            meta_names = [
                "level_kind", "score", "es_confirmed", "mbo_pull",
                "n_aggressors", "n_sweeps", "outcome", "reclaim_ts_ns",
            ]
            extras: dict[str, list] = {n: [] for n in meta_names if n in src.column_names}
            keep_idx = []
            for i, t0 in enumerate(lab_ts):
                j = idx_map.get(int(t0))
                if j is None:
                    continue
                keep_idx.append(i)
                for n in extras:
                    extras[n].append(src.column(n)[j].as_py())
            if not keep_idx:
                return lab.slice(0, 0)
            out_t = lab.take(np.asarray(keep_idx, dtype=np.int64))
            for n, vals in extras.items():
                if n in out_t.column_names:
                    out_t = out_t.drop(n)
                if n in ("es_confirmed", "mbo_pull"):
                    out_t = out_t.append_column(n, pa.array(vals, type=pa.bool_()))
                elif n in ("score", "n_aggressors", "n_sweeps"):
                    out_t = out_t.append_column(n, pa.array(vals, type=pa.int32()))
                elif n == "reclaim_ts_ns":
                    out_t = out_t.append_column(n, pa.array(vals, type=pa.int64()))
                else:
                    out_t = out_t.append_column(n, pa.array(vals, type=pa.string()))
            return out_t

        lab_cont = _sid(_merge_meta(lab_cont, ev))
        # Fade labeled used flipped side but same event_ts — merge from original ev
        lab_fade = _sid(_merge_meta(lab_fade, ev))
        lab_null = _sid(lab_null)
        lab_mom = _sid(lab_mom)

        if lab_cont.num_rows:
            cont_parts.append(lab_cont)
        if lab_fade.num_rows:
            fade_parts.append(lab_fade)
        if lab_null.num_rows:
            null_parts.append(lab_null)
        if lab_mom.num_rows:
            mom_parts.append(lab_mom)

        n_stall = 0
        n_cont_out = 0
        if ev.num_rows and "outcome" in ev.column_names:
            oc = ev.column("outcome").to_numpy(zero_copy_only=False)
            n_stall = int(sum(1 for x in oc if x == "stall"))
            n_cont_out = int(sum(1 for x in oc if x == "continue"))

        sanity.append({
            "session": sid,
            "cohort": "may" if sid in MAY else "train",
            "events": int(ev.num_rows),
            "stall": n_stall,
            "continue": n_cont_out,
            "mbo_rows": mbo_rows,
            "aid_empty_rate": None if aid_empty is None else round(aid_empty, 4),
            "score_mean": (
                round(float(np.mean(ev.column("score").to_numpy())), 2)
                if ev.num_rows else None
            ),
            "es_confirmed_n": (
                int(sum(ev.column("es_confirmed").to_numpy()))
                if ev.num_rows else 0
            ),
            "mbo_pull_n": (
                int(sum(ev.column("mbo_pull").to_numpy()))
                if ev.num_rows else 0
            ),
            "cont_hit10": _mean(lab_cont.column("hit_10pts")) if lab_cont.num_rows else None,
            "fade_hit10": _mean(lab_fade.column("hit_10pts")) if lab_fade.num_rows else None,
            "mom_hit10": _mean(lab_mom.column("hit_10pts")) if lab_mom.num_rows else None,
            "null_hit10": _mean(lab_null.column("hit_10pts")) if lab_null.num_rows else None,
        })
        print(
            f"  ev={ev.num_rows} stall={n_stall} cont_hit10="
            f"{sanity[-1]['cont_hit10']} fade_hit10={sanity[-1]['fade_hit10']}",
            flush=True,
        )

        # Stage B P&L
        if not args.skip_pnl and ev.num_rows:
            mid = int(nq_rth.column("ts_ns").to_numpy()[nq_rth.num_rows // 2])
            vf = bounds(mid)[0]
            # Continuation entries
            cont_ev = ev.select(["event_ts_ns", "side"])
            s_cont = simulate_session(
                sid, nq_rth, es_rth, sim_cfg,
                gate=gate, bracket_fn=br, vwap_from_ns=vf, events=cont_ev,
            )
            cont_trades.extend(s_cont.trades)
            # Fade at reclaim
            fade_ev = fade_events_at_reclaim(ev)
            if fade_ev.num_rows:
                fade_ev2 = fade_ev.select(["event_ts_ns", "side"])
                s_fade = simulate_session(
                    sid, nq_rth, es_rth, sim_cfg,
                    gate=gate, bracket_fn=br, vwap_from_ns=vf, events=fade_ev2,
                )
                fade_trades.extend(s_fade.trades)

    # ---- Stage A report ----
    report: dict = {
        "experiment": "EXP-0041",
        "hypothesis": "H32",
        "config": {
            "pierce_ticks": sr_cfg.pierce_ticks,
            "window_ms": sr_cfg.window_ms,
            "min_vol": sr_cfg.min_vol,
            "min_prints": sr_cfg.min_prints,
            "min_imbalance": sr_cfg.min_imbalance,
            "min_sweeps": sr_cfg.min_sweeps,
            "min_aggressors": sr_cfg.min_aggressors,
            "min_score": sr_cfg.min_score,
            "sync_ms": sr_cfg.sync_ms,
            "reclaim_sec": sr_cfg.reclaim_sec,
            "level_kinds": sorted(sr_cfg.level_kinds),
        },
        "stage_a": {
            "continuation": _pack_labeled(cont_parts, horizons, targets),
            "fade": _pack_labeled(fade_parts, horizons, targets),
            "null_random": _pack_labeled(null_parts, horizons, targets),
            "exp0008_lag250": _pack_labeled(mom_parts, horizons, targets),
        },
        "strata": {},
        "stage_b": {},
        "sanity": sanity,
    }

    # Strata on continuation
    if cont_parts:
        all_c = pa.concat_tables(cont_parts)

        def _pack_s(parts):
            return _pack_labeled(parts, horizons, targets)

        strata = {}
        if "level_kind" in all_c.column_names:
            kinds = sorted({x.as_py() for x in all_c.column("level_kind")})
            for k in kinds:
                parts = _stratum(cont_parts, lambda t, kk=k: (
                    np.array([x.as_py() == kk for x in t.column("level_kind")])
                ))
                strata[f"kind_{k}"] = _pack_s(parts)
        if "score" in all_c.column_names:
            for sc in (2, 3, 4):
                parts = _stratum(cont_parts, lambda t, s=sc: (
                    t.column("score").to_numpy().astype(int) >= s
                ))
                strata[f"score_ge_{sc}"] = _pack_s(parts)
        if "es_confirmed" in all_c.column_names:
            strata["es_confirmed"] = _pack_s(_stratum(
                cont_parts, lambda t: t.column("es_confirmed").to_numpy().astype(bool)
            ))
            strata["es_not"] = _pack_s(_stratum(
                cont_parts, lambda t: ~t.column("es_confirmed").to_numpy().astype(bool)
            ))
        if "mbo_pull" in all_c.column_names:
            strata["mbo_pull"] = _pack_s(_stratum(
                cont_parts, lambda t: t.column("mbo_pull").to_numpy().astype(bool)
            ))
        if "outcome" in all_c.column_names:
            strata["outcome_stall"] = _pack_s(_stratum(
                cont_parts,
                lambda t: np.array([x.as_py() == "stall" for x in t.column("outcome")]),
            ))
            strata["outcome_continue"] = _pack_s(_stratum(
                cont_parts,
                lambda t: np.array([x.as_py() == "continue" for x in t.column("outcome")]),
            ))
        if "n_aggressors" in all_c.column_names:
            vals = all_c.column("n_aggressors").to_numpy().astype(float)
            if len(vals):
                q33, q66 = np.percentile(vals, [33, 66])
                strata["aggr_low"] = _pack_s(_stratum(
                    cont_parts,
                    lambda t, q=q33: t.column("n_aggressors").to_numpy() <= q,
                ))
                strata["aggr_mid"] = _pack_s(_stratum(
                    cont_parts,
                    lambda t, a=q33, b=q66: (
                        (t.column("n_aggressors").to_numpy() > a)
                        & (t.column("n_aggressors").to_numpy() <= b)
                    ),
                ))
                strata["aggr_high"] = _pack_s(_stratum(
                    cont_parts,
                    lambda t, q=q66: t.column("n_aggressors").to_numpy() > q,
                ))
        report["strata"] = strata

        # Fade × stall stratum
        if fade_parts:
            report["strata"]["fade_on_stall"] = _pack_s(_stratum(
                fade_parts,
                lambda t: np.array(
                    [x.as_py() == "stall" for x in t.column("outcome")]
                ) if "outcome" in t.column_names else np.zeros(t.num_rows, dtype=bool),
            ))

    # Stage B
    if not args.skip_pnl:
        ct, cm = cohort_split(cont_trades)
        ft, fm = cohort_split(fade_trades)
        report["stage_b"] = {
            "continuation_gated": {
                "all": pack_trades(cont_trades),
                "train": pack_trades(ct),
                "may": pack_trades(cm),
            },
            "fade_reclaim_gated": {
                "all": pack_trades(fade_trades),
                "train": pack_trades(ft),
                "may": pack_trades(fm),
            },
        }

    # Write outputs
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "sanity.json").write_text(json.dumps(sanity, indent=2, default=str), encoding="utf-8")

    # Summary markdown
    sa = report["stage_a"]
    lines = [
        "# EXP-0041 — Stop-run fingerprint (H32)",
        "",
        f"Sessions: {len(sessions)} (May held out). UK 14:30–17:00.",
        "",
        "## Stage A — event study (points)",
        "",
        "| Branch | n | hit5 | hit10 | hit20 | mean@10s | mean@60s | MFE | MAE |",
        "|--------|---|------|-------|-------|----------|----------|-----|-----|",
    ]

    def _row(name, d):
        if not d:
            return f"| {name} | 0 | — | — | — | — | — | — | — |"
        def f(k):
            v = d.get(k)
            return "—" if v is None else f"{v:.3f}" if isinstance(v, float) else str(v)
        return (
            f"| {name} | {d.get('n_events', 0)} | {f('p_hit_5pts')} | {f('p_hit_10pts')} "
            f"| {f('p_hit_20pts')} | {f('mean_ret_pts_10000ms')} | {f('mean_ret_pts_60000ms')} "
            f"| {f('mean_mfe_pts')} | {f('mean_mae_pts')} |"
        )

    lines.append(_row("continuation", sa.get("continuation")))
    lines.append(_row("fade (flip)", sa.get("fade")))
    lines.append(_row("null", sa.get("null_random")))
    lines.append(_row("0008 lag<250", sa.get("exp0008_lag250")))
    lines.append("")
    lines.append("### Strata (continuation)")
    lines.append("")
    for k, d in (report.get("strata") or {}).items():
        if d:
            lines.append(
                f"- **{k}**: n={d.get('n_events')} hit10={d.get('p_hit_10pts')} "
                f"@60s={d.get('mean_ret_pts_60000ms')}"
            )
    lines.append("")
    lines.append("## Stage B — MAT P&L (vol k=1, trend+VWAP, 1-tick slip, 0.25 RT)")
    lines.append("")
    sb = report.get("stage_b") or {}
    for branch, block in sb.items():
        lines.append(f"### {branch}")
        lines.append("")
        lines.append("| Cohort | n | net | wr | pf | days+ | top3 | loo_neg |")
        lines.append("|--------|---|-----|----|----|-------|------|---------|")
        for cohort in ("all", "train", "may"):
            d = (block or {}).get(cohort) or {}
            lines.append(
                f"| {cohort} | {d.get('n', 0)} | {d.get('net')} | {d.get('wr')} "
                f"| {d.get('pf')} | {d.get('days_pos')}/{d.get('days')} "
                f"| {d.get('top3_share')} | {d.get('loo_neg')} |"
            )
        lines.append("")

    # Freeze note
    cont = sa.get("continuation") or {}
    null = sa.get("null_random") or {}
    mom = sa.get("exp0008_lag250") or {}
    hit_c = cont.get("p_hit_10pts")
    hit_n = null.get("p_hit_10pts")
    hit_m = mom.get("p_hit_10pts")
    n_ev = cont.get("n_events") or 0
    lines.append("## Freeze / decision")
    lines.append("")
    lines.append(
        f"Default thresholds: pierce={sr_cfg.pierce_ticks} ticks, "
        f"window={sr_cfg.window_ms}ms, min_vol={sr_cfg.min_vol}, "
        f"min_imb={sr_cfg.min_imbalance}, min_sweeps={sr_cfg.min_sweeps}, "
        f"min_aggr={sr_cfg.min_aggressors}, min_score={sr_cfg.min_score}."
    )
    lines.append("")
    if n_ev < 30:
        verdict = "EMPTY / thin — identification only; no P&L ship."
    elif hit_c is not None and hit_n is not None and hit_c > hit_n + 0.05:
        # Check May P&L agreement if present
        may_net = ((sb.get("continuation_gated") or {}).get("may") or {}).get("net")
        train_net = ((sb.get("continuation_gated") or {}).get("train") or {}).get("net")
        if train_net is not None and may_net is not None and train_net > 0 and may_net > 0:
            verdict = "PROMISING — train+May P&L positive; freeze for visual indicator + watch live."
        else:
            verdict = (
                f"Identification OK (hit10={hit_c:.2f} vs null={hit_n:.2f}, "
                f"0008={hit_m}); P&L not both-cohorts positive — "
                "**visual indicator only**, no auto-trade."
            )
    else:
        verdict = (
            f"No material lift vs null (hit10 cont={hit_c} null={hit_n} 0008={hit_m}). "
            "Freeze thresholds for visual Stop-Run module; no auto-trade."
        )
    lines.append(f"**Verdict:** {verdict}")
    lines.append("")
    report["verdict"] = verdict
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {out}", flush=True)
    print(f"Verdict: {verdict}", flush=True)


if __name__ == "__main__":
    main()
