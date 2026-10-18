"""
Agent entrypoint for offline orderflow experiments (PyArrow / numpy — no pandas).

  python -m research.run_experiment EXP-0002 --sessions all
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from research.config import load_config
from research.events.detectors import effort_exhaustion_events
from research.features.flow import trades_table_to_bars
from research.features.liquidity import (
    LargeLevelConfig,
    absorption_events_from_levels,
    aggressive_take_events_from_levels,
    annotate_near_lvn,
    challenge_without_trade_events,
    cross_confirm_liquidity_events,
    dwell_withdraw_events_from_levels,
    hold_without_trade_events_from_levels,
    micro_regime_composite_events,
    run_liquidity_tracker,
)
from research.features.momentum import (
    cross_confirm_bursts,
    filter_events_near_fast_tape,
    momentum_burst_events,
    trades_to_tape_bars,
)
from research.features.cd import (
    annotate_dual_cd_fields,
    build_cd_series,
    cd_pullback_events,
    filter_events_near_bursts,
    lookup_regime_at,
)
from research.events.lvn import (
    TouchConfig,
    annotate_beyond_and_flip,
    dual_lvn_touch_events,
)
from research.features.volume_profile import VolumeProfileConfig, causal_lvn_snapshots
from research.features.flush_hold import (
    FlushHoldConfig,
    cross_confirm_flush_hold,
    flush_into_hold_events,
)
from research.features.sweeps import SweepConfig, aggressor_sweep_events, cross_confirm_sweeps
from research.features.swings import (
    SwingConfig,
    annotate_burst_swing_break,
    cross_confirm_failed_breaks,
    failed_swing_break_events,
)
from research.features.rotations import (
    RotationConfig,
    annotate_rotation_state,
    momentum_rotation_ok,
    reversal_rotation_ok,
)
from research.features.trapped import TrappedConfig, trapped_events
from research.features.range_imbalance import (
    MODES as RANGE_IMB_MODES,
    RangeImbalanceConfig,
    annotate_vs_prior,
    range_imbalance_events,
    trades_to_enriched_range_bars,
)
from research.features.simultaneous import (
    SimultaneousFlowConfig,
    dual_range_imbalance_events,
    simultaneous_flow_events,
)
from research.features.mbo_vacuum import VacuumConfig, mbo_vacuum_events, cross_confirm_vacuums
from research.features.value_depart import ValueDepartConfig, build_value_features, value_depart_events
from research.features.book_imbalance import (
    BookImbalanceConfig,
    book_imbalance_events,
    cross_confirm_imbalance,
)
from research.outcomes.label import build_price_path, label_events

# BST display/filter (UTC+1). Good enough for UK RTH window in summer months.
UK = timezone(timedelta(hours=1))


def _load_exp(name: str) -> dict:
    path = Path(__file__).parent / "experiments" / f"{name}.yaml"
    if not path.exists():
        path = Path(__file__).parent / "experiments" / name
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _list_sessions(parquet_dir: Path, which: str, smoke: list) -> list[str]:
    sessions = []
    if parquet_dir.exists():
        sessions = sorted(
            p.name.replace("session_id=", "")
            for p in parquet_dir.iterdir()
            if p.is_dir() and p.name.startswith("session_id=")
        )
    # Prefer real extracts; keep synthetic only for explicit smoke runs
    real = [s for s in sessions if s != "smoke_synthetic"]
    if which == "smoke":
        if smoke:
            return [s for s in sessions if s in smoke]
        return sessions[:1]
    return real if real else sessions


def _filter_rth_uk_table(trades: pa.Table) -> pa.Table:
    if trades.num_rows == 0:
        return trades
    ts = trades.column("ts_ns").to_numpy()
    keep = []
    for i, ts_ns in enumerate(ts):
        dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).astimezone(UK)
        t = dt.timetz().replace(tzinfo=None)
        keep.append(time(14, 30) <= t <= time(17, 0))
    mask = np.array(keep, dtype=bool)
    if not mask.any():
        return trades.slice(0, 0)
    indices = np.where(mask)[0]
    return trades.take(indices)


def _load_trades(parquet_dir: Path, session_id: str, role: str) -> pa.Table:
    path = parquet_dir / f"session_id={session_id}" / "trades.parquet"
    if not path.exists():
        return pa.table({})
    table = pq.read_table(path)
    if role and "role" in table.column_names:
        roles = table.column("role").to_numpy(zero_copy_only=False)
        idx = np.where(roles == role)[0]
        if len(idx) == 0:
            return table.slice(0, 0)
        table = table.take(idx)
    return table


def _mean(arr) -> float | None:
    a = np.asarray(arr, dtype=float)
    return float(a.mean()) if len(a) else None


def _median(arr) -> float | None:
    a = np.asarray(arr, dtype=float)
    return float(np.median(a)) if len(a) else None


def _load_depth(parquet_dir: Path, session_id: str, role: str) -> pa.Table:
    path = parquet_dir / f"session_id={session_id}" / "depth.parquet"
    if not path.exists():
        return pa.table({})
    filters = [("role", "=", role)] if role else None
    try:
        return pq.read_table(
            path,
            columns=["ts_ns", "role", "is_bid", "price_ticks", "size"],
            filters=filters,
        )
    except Exception:
        table = pq.read_table(path, columns=["ts_ns", "role", "is_bid", "price_ticks", "size"])
        if role and "role" in table.column_names:
            roles = table.column("role").to_numpy(zero_copy_only=False)
            idx = np.where(roles == role)[0]
            return table.take(idx) if len(idx) else table.slice(0, 0)
        return table


def _load_mbo(parquet_dir: Path, session_id: str, role: str) -> pa.Table:
    path = parquet_dir / f"session_id={session_id}" / "mbo.parquet"
    if not path.exists():
        return pa.table({})
    cols = ["ts_ns", "role", "op", "order_id", "is_bid", "price_ticks", "size"]
    filters = [("role", "=", role)] if role else None
    try:
        return pq.read_table(path, columns=cols, filters=filters)
    except Exception:
        table = pq.read_table(path, columns=cols)
        if role and "role" in table.column_names:
            roles = table.column("role").to_numpy(zero_copy_only=False)
            idx = np.where(roles == role)[0]
            return table.take(idx) if len(idx) else table.slice(0, 0)
        return table


def _summarize_labeled(df: pa.Table, horizons: list, targets: list, extra: dict | None = None) -> dict:
    summary = {
        "status": "ok",
        "n_events": int(df.num_rows),
        "n_sessions_with_events": int(len({x.as_py() for x in df.column("session_id")})),
        "mean_mfe_pts": _mean(df.column("mfe_pts")),
        "mean_mae_pts": _mean(df.column("mae_pts")),
        "median_mfe_pts": _median(df.column("mfe_pts")),
        "median_mae_pts": _median(df.column("mae_pts")),
        "null_note": "Compare p_hit_* to ~0.5; full bootstrap in later iteration.",
    }
    if extra:
        summary.update(extra)
    for h in horizons:
        col = f"ret_pts_{h}ms"
        if col in df.column_names:
            summary[f"mean_{col}"] = _mean(df.column(col))
            summary[f"median_{col}"] = _median(df.column(col))
    for tgt in targets:
        col = f"hit_{int(tgt)}pts"
        if col in df.column_names:
            summary[f"p_{col}"] = _mean(df.column(col))
    return summary


def run_exp_absorption(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0005 / EXP-0006 — large-level absorption event study."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    role = exp.get("role", "NQ")
    ll_cfg = LargeLevelConfig(
        absolute_size=int(exp.get("absolute_size", 100)),
        z_score=float(exp.get("z_score", 2.5)),
        tick_radius=int(exp.get("tick_radius", 8)),
        dwell_alert_sec=float(exp.get("dwell_alert_sec", 3.0)),
    )
    min_consume = float(exp.get("min_consume_ratio", 1.0))
    require_repl = exp.get("require_replenish", None)
    if require_repl is not None:
        require_repl = bool(require_repl)
    rth = bool(exp.get("rth_uk_only", True))

    labeled_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[absorption] session {sid} …", flush=True)
        depth = _load_depth(parquet_dir, sid, role)
        trades = _load_trades(parquet_dir, sid, role)
        if depth.num_rows == 0 or trades.num_rows == 0:
            per_session.append({
                "session_id": sid,
                "events": 0,
                "note": "missing depth or trades",
                "depth_rows": int(depth.num_rows),
                "trades": int(trades.num_rows),
            })
            continue

        levels = run_liquidity_tracker(depth, trades, cfg=ll_cfg, rth_uk_only=rth)
        events = absorption_events_from_levels(
            levels,
            min_consume_ratio=min_consume,
            require_replenish=require_repl,
            tick_radius=ll_cfg.tick_radius,
        )
        # Outcomes on full-day NQ path (events themselves are RTH-gated)
        path_ts, path_px = build_price_path(trades)
        labeled = label_events(events, path_ts, path_px, horizons, targets)
        n_closed = len(levels)
        n_contacted = sum(1 for lv in levels if lv.contacted)
        if labeled.num_rows > 0:
            sid_col = pa.array([sid] * labeled.num_rows, type=pa.string())
            labeled = labeled.append_column("session_id", sid_col)
            labeled_parts.append(labeled)
        per_session.append({
            "session_id": sid,
            "events": int(labeled.num_rows),
            "closed_levels": n_closed,
            "contacted_levels": n_contacted,
            "depth_rows": int(depth.num_rows),
            "trades": int(trades.num_rows),
        })
        print(
            f"  closed={n_closed} contacted={n_contacted} absorb_events={labeled.num_rows}",
            flush=True,
        )

    if not labeled_parts:
        return {
            "status": "no_events",
            "sessions": per_session,
            "message": "No absorption events found.",
        }

    df = pa.concat_tables(labeled_parts)
    # Replenish split diagnostics (even when require_replenish filters the set)
    if "replenish_count" in df.column_names:
        rc = df.column("replenish_count").to_numpy()
        n_with = int((rc >= 1).sum())
        n_without = int((rc < 1).sum())
    else:
        n_with = n_without = None

    summary = _summarize_labeled(
        df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "gates": {
                "absolute_size": ll_cfg.absolute_size,
                "z_score": ll_cfg.z_score,
                "tick_radius": ll_cfg.tick_radius,
                "min_consume_ratio": min_consume,
                "require_replenish": require_repl,
            },
            "n_with_replenish": n_with,
            "n_without_replenish": n_without,
        },
    )
    return {"summary": summary, "events": df}


def run_exp_momentum(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0008 — ES+NQ fast-tape momentum bursts (continuation)."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    bar_ms = int(exp.get("bar_ms", 100))
    sync_ms = int(exp.get("sync_ms", 1500))
    tick_size = float(exp.get("tick_size", 1.0))
    rth = bool(exp.get("rth_uk_only", True))
    burst_kw = dict(
        min_trades=int(exp.get("min_trades", 6)),
        min_vol=float(exp.get("min_vol", 25)),
        min_imbalance=float(exp.get("min_imbalance", 0.55)),
        min_progress_ticks=float(exp.get("min_progress_ticks", 1.0)),
        tick_size=tick_size,
        min_max_print=float(exp.get("min_max_print", 0.0)),
        cooldown_bars=int(exp.get("cooldown_bars", 3)),
        bar_ms=bar_ms,
    )

    labeled_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[momentum] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ trades"})
            continue
        if rth:
            nq = _filter_rth_uk_table(nq)
            if es.num_rows:
                es = _filter_rth_uk_table(es)
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no RTH NQ"})
            continue

        nq_bars = trades_to_tape_bars(nq, bar_ms=bar_ms)
        es_bars = trades_to_tape_bars(es, bar_ms=bar_ms) if es.num_rows else pa.table({})
        nq_burst = momentum_burst_events(nq_bars, **burst_kw)
        es_burst = momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({})
        events = cross_confirm_bursts(nq_burst, es_burst, sync_ms=sync_ms)

        # Also label NQ-only bursts for contrast (stored in per_session counts)
        path_ts, path_px = build_price_path(nq)
        labeled = label_events(events, path_ts, path_px, horizons, targets)
        labeled_nq_only = label_events(nq_burst, path_ts, path_px, horizons, targets)

        if labeled.num_rows > 0:
            labeled = labeled.append_column(
                "session_id", pa.array([sid] * labeled.num_rows, type=pa.string())
            )
            labeled_parts.append(labeled)

        per_session.append({
            "session_id": sid,
            "events": int(labeled.num_rows),
            "nq_bursts": int(nq_burst.num_rows),
            "es_bursts": int(es_burst.num_rows),
            "nq_only_hit10": _mean(labeled_nq_only.column("hit_10pts")) if labeled_nq_only.num_rows else None,
            "nq_trades": int(nq.num_rows),
            "es_trades": int(es.num_rows),
        })
        print(
            f"  nq_bursts={nq_burst.num_rows} es_bursts={es_burst.num_rows} "
            f"confirmed={labeled.num_rows}",
            flush=True,
        )

    if not labeled_parts:
        return {
            "status": "no_events",
            "sessions": per_session,
            "message": "No ES+NQ confirmed momentum bursts found.",
        }

    df = pa.concat_tables(labeled_parts)
    summary = _summarize_labeled(
        df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "gates": {
                "bar_ms": bar_ms,
                "sync_ms": sync_ms,
                **burst_kw,
            },
        },
    )
    return {"summary": summary, "events": df}


def run_exp_depth_momentum(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0008b/c — tape momentum + large-level hit confirmation (depth).

    Pure penetration-through takes are vanishingly rare on NQ (pen≥1 ≈ 2/session).
    Hybrid matches discretionary better: fast tape burst while a large resting
    level is being traded into, with ES same-side burst nearby.
    """
    from research.features.momentum import filter_events_near_level_hits

    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    sync_ms = int(exp.get("sync_ms", 1500))
    rth = bool(exp.get("rth_uk_only", True))
    bar_ms = int(exp.get("bar_ms", 100))
    tick_size = float(exp.get("tick_size", 1.0))
    burst_kw = dict(
        min_trades=int(exp.get("min_trades", 6)),
        min_vol=float(exp.get("min_vol", 25)),
        min_imbalance=float(exp.get("min_imbalance", 0.55)),
        min_progress_ticks=float(exp.get("min_progress_ticks", 1.0)),
        tick_size=tick_size,
        cooldown_bars=int(exp.get("cooldown_bars", 3)),
        bar_ms=bar_ms,
    )
    nq_cfg = LargeLevelConfig(
        absolute_size=int(exp.get("nq_absolute_size", 50)),
        z_score=float(exp.get("z_score", 99.0)),
        tick_radius=int(exp.get("tick_radius", 8)),
    )
    require_es = bool(exp.get("require_es_burst", True))
    require_nq_level = bool(exp.get("require_nq_level_hit", True))
    require_es_level = bool(exp.get("require_es_level_hit", False))
    level_window_ms = int(exp.get("level_window_ms", 500))
    min_executed = float(exp.get("min_level_executed", 10))
    es_cfg = LargeLevelConfig(
        absolute_size=int(exp.get("es_absolute_size", 100)),
        z_score=float(exp.get("z_score", 99.0)),
        tick_radius=int(exp.get("tick_radius", 8)),
    )

    labeled_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[hybrid-momentum] session {sid} …", flush=True)
        nq_tr = _load_trades(parquet_dir, sid, "NQ")
        es_tr = _load_trades(parquet_dir, sid, "ES")
        if nq_tr.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ"})
            continue
        nq_rth = _filter_rth_uk_table(nq_tr) if rth else nq_tr
        es_rth = _filter_rth_uk_table(es_tr) if (rth and es_tr.num_rows) else es_tr

        nq_bars = trades_to_tape_bars(nq_rth, bar_ms=bar_ms)
        es_bars = trades_to_tape_bars(es_rth, bar_ms=bar_ms) if es_rth.num_rows else pa.table({})
        nq_burst = momentum_burst_events(nq_bars, **burst_kw)
        es_burst = momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({})

        if require_es:
            events = cross_confirm_bursts(nq_burst, es_burst, sync_ms=sync_ms)
        else:
            events = nq_burst

        nq_levels = []
        es_levels = []
        if require_nq_level or require_es_level:
            nq_depth = _load_depth(parquet_dir, sid, "NQ")
            if require_nq_level and nq_depth.num_rows:
                nq_levels = run_liquidity_tracker(nq_depth, nq_tr, cfg=nq_cfg, rth_uk_only=rth)
                events = filter_events_near_level_hits(
                    events, nq_levels, window_ms=level_window_ms, min_executed=min_executed
                )
            if require_es_level and events.num_rows and es_tr.num_rows:
                es_depth = _load_depth(parquet_dir, sid, "ES")
                if es_depth.num_rows:
                    es_levels = run_liquidity_tracker(es_depth, es_tr, cfg=es_cfg, rth_uk_only=rth)
                    events = filter_events_near_level_hits(
                        events, es_levels, window_ms=level_window_ms, min_executed=min_executed
                    )

        path_ts, path_px = build_price_path(nq_tr)
        labeled = label_events(events, path_ts, path_px, horizons, targets)
        if labeled.num_rows > 0:
            labeled = labeled.append_column(
                "session_id", pa.array([sid] * labeled.num_rows, type=pa.string())
            )
            labeled_parts.append(labeled)

        per_session.append({
            "session_id": sid,
            "events": int(labeled.num_rows),
            "nq_bursts": int(nq_burst.num_rows),
            "es_bursts": int(es_burst.num_rows),
            "nq_levels_closed": len(nq_levels),
            "es_levels_closed": len(es_levels),
        })
        print(
            f"  nq_bursts={nq_burst.num_rows} es_bursts={es_burst.num_rows} "
            f"final={labeled.num_rows}",
            flush=True,
        )

    if not labeled_parts:
        return {
            "status": "no_events",
            "sessions": per_session,
            "message": "No hybrid momentum events found.",
        }

    df = pa.concat_tables(labeled_parts)
    summary = _summarize_labeled(
        df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "gates": {
                "require_es_burst": require_es,
                "require_nq_level_hit": require_nq_level,
                "require_es_level_hit": require_es_level,
                "nq_absolute_size": nq_cfg.absolute_size,
                "level_window_ms": level_window_ms,
                "min_level_executed": min_executed,
                "sync_ms": sync_ms,
                **burst_kw,
            },
        },
    )
    return {"summary": summary, "events": df}


def _random_null_events(
    trades: pa.Table,
    n: int,
    *,
    seed: int = 42,
) -> pa.Table:
    """Random RTH trade timestamps with random fade sides (±1)."""
    if trades.num_rows == 0 or n <= 0:
        return pa.table({})
    rng = np.random.default_rng(seed)
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    # subsample unique-ish timestamps
    pick = rng.choice(len(ts), size=min(n, len(ts)), replace=False)
    pick = np.sort(pick)
    sides = rng.choice(np.array([-1, 1], dtype=np.int8), size=len(pick))
    return pa.table({
        "event_ts_ns": ts[pick],
        "side": sides,
        "event_type": ["null_random"] * len(pick),
    })


def _vp_touch_from_exp(exp: dict) -> tuple[VolumeProfileConfig, TouchConfig]:
    vp = VolumeProfileConfig(
        lookback_ms=int(exp.get("lookback_ms", 1_800_000)),
        refresh_ms=int(exp.get("refresh_ms", 30_000)),
        lvn_frac_of_peak=float(exp.get("lvn_frac_of_peak", 0.35)),
        min_lvn_width_ticks=int(exp.get("min_lvn_width_ticks", 2)),
        max_lvn_width_ticks=int(exp.get("max_lvn_width_ticks", 12)),
        min_sep_from_poc_ticks=int(exp.get("min_sep_from_poc_ticks", 4)),
        tick_size=float(exp.get("tick_size", 1.0)),
        min_profile_volume=float(exp.get("min_profile_volume", 500.0)),
        hvn_frac_of_poc=float(exp.get("hvn_frac_of_poc", 0.50)),
        hvn_search_ticks=int(exp.get("hvn_search_ticks", 20)),
        edge_margin_ticks=int(exp.get("edge_margin_ticks", 6)),
        require_hvn_sandwich=bool(exp.get("require_hvn_sandwich", True)),
    )
    touch = TouchConfig(
        sync_ms=int(exp.get("sync_ms", 2000)),
        touch_cooldown_ms=int(exp.get("touch_cooldown_ms", 5000)),
        tick_size=float(exp.get("tick_size", 1.0)),
    )
    return vp, touch


def run_exp_dual_lvn(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0011 / EXP-0023 — dual ES+NQ LVN touch fade vs NQ-only vs null (± mom)."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    vp_cfg, touch_cfg = _vp_touch_from_exp(exp)
    compare_mom = bool(exp.get("compare_momentum", False))
    burst_kw = dict(
        min_trades=6, min_vol=25.0, min_imbalance=0.55,
        min_progress_ticks=1.0, tick_size=1.0, cooldown_bars=3, bar_ms=100)

    dual_parts: list[pa.Table] = []
    nq_only_parts: list[pa.Table] = []
    null_parts: list[pa.Table] = []
    mom_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[dual-lvn] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ"})
            continue
        if rth:
            nq = _filter_rth_uk_table(nq)
            if es.num_rows:
                es = _filter_rth_uk_table(es)
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no RTH NQ"})
            continue

        dual, nq_only, meta = dual_lvn_touch_events(nq, es, vp_cfg, touch_cfg)
        path_ts, path_px = build_price_path(nq)
        labeled_dual = label_events(dual, path_ts, path_px, horizons, targets)
        labeled_nq = label_events(nq_only, path_ts, path_px, horizons, targets)

        n_null = max(int(labeled_dual.num_rows), 50)
        null_ev = _random_null_events(nq, n_null, seed=hash(sid) % (2**31))
        labeled_null = label_events(null_ev, path_ts, path_px, horizons, targets)

        labeled_mom = pa.table({})
        if compare_mom:
            nq_bars = trades_to_tape_bars(nq, bar_ms=100)
            es_bars = trades_to_tape_bars(es, bar_ms=100) if es.num_rows else pa.table({})
            mom = cross_confirm_bursts(
                momentum_burst_events(nq_bars, **burst_kw),
                momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
                sync_ms=1500,
            )
            if mom.num_rows and "es_lag_ms" in mom.column_names:
                mom = mom.take(
                    np.where(np.abs(mom.column("es_lag_ms").to_numpy()) <= 250)[0]
                )
            labeled_mom = label_events(mom, path_ts, path_px, horizons, targets)

        def _sid(t: pa.Table) -> pa.Table:
            if t.num_rows == 0:
                return t
            return t.append_column(
                "session_id", pa.array([sid] * t.num_rows, type=pa.string())
            )

        labeled_dual, labeled_nq, labeled_null, labeled_mom = map(
            _sid, (labeled_dual, labeled_nq, labeled_null, labeled_mom)
        )
        if labeled_dual.num_rows:
            dual_parts.append(labeled_dual)
        if labeled_nq.num_rows:
            nq_only_parts.append(labeled_nq)
        if labeled_null.num_rows:
            null_parts.append(labeled_null)
        if labeled_mom.num_rows:
            mom_parts.append(labeled_mom)

        row = {
            "session_id": sid,
            "events": int(labeled_dual.num_rows),
            "nq_only_events": int(labeled_nq.num_rows),
            "null_events": int(labeled_null.num_rows),
            "dual_hit10": _mean(labeled_dual.column("hit_10pts")) if labeled_dual.num_rows else None,
            "nq_only_hit10": _mean(labeled_nq.column("hit_10pts")) if labeled_nq.num_rows else None,
            "null_hit10": _mean(labeled_null.column("hit_10pts")) if labeled_null.num_rows else None,
            "mom_hit10": _mean(labeled_mom.column("hit_10pts")) if labeled_mom.num_rows else None,
            **meta,
        }
        per_session.append(row)
        print(
            f"  dual={labeled_dual.num_rows} nq_only={labeled_nq.num_rows} "
            f"zones_nq_last={meta.get('nq_zones_last')} "
            f"dual_hit10={row['dual_hit10']}",
            flush=True,
        )

    def _pack_parts(parts: list[pa.Table]) -> dict | None:
        if not parts:
            return None
        t = pa.concat_tables(parts)
        return {
            "n_events": int(t.num_rows),
            "p_hit_10pts": _mean(t.column("hit_10pts")),
            "p_hit_20pts": _mean(t.column("hit_20pts")) if "hit_20pts" in t.column_names else None,
            "mean_mfe_pts": _mean(t.column("mfe_pts")),
            "mean_mae_pts": _mean(t.column("mae_pts")),
            "mean_ret_pts_2000ms": _mean(t.column("ret_pts_2000ms")) if "ret_pts_2000ms" in t.column_names else None,
            "mean_ret_pts_10000ms": _mean(t.column("ret_pts_10000ms")) if "ret_pts_10000ms" in t.column_names else None,
            "median_ret_pts_10000ms": _median(t.column("ret_pts_10000ms")) if "ret_pts_10000ms" in t.column_names else None,
        }

    if not dual_parts and not nq_only_parts:
        return {
            "status": "no_events",
            "sessions": per_session,
            "message": "No LVN touch events found.",
        }

    baselines = {
        "nq_only": _pack_parts(nq_only_parts),
        "null_random": _pack_parts(null_parts),
    }
    if mom_parts:
        baselines["exp0008_lag250"] = _pack_parts(mom_parts)

    if not dual_parts:
        return {
            "status": "no_dual_events",
            "sessions": per_session,
            "baselines": baselines,
            "message": "NQ-only LVN touches found but no dual ES+NQ confirms.",
        }

    df = pa.concat_tables(dual_parts)
    summary = _summarize_labeled(
        df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "baselines": baselines,
            "gates": {
                "lookback_ms": vp_cfg.lookback_ms,
                "refresh_ms": vp_cfg.refresh_ms,
                "lvn_frac_of_peak": vp_cfg.lvn_frac_of_peak,
                "min_profile_volume": vp_cfg.min_profile_volume,
                "require_hvn_sandwich": vp_cfg.require_hvn_sandwich,
                "hvn_frac_of_poc": vp_cfg.hvn_frac_of_poc,
                "hvn_search_ticks": vp_cfg.hvn_search_ticks,
                "edge_margin_ticks": vp_cfg.edge_margin_ticks,
                "sync_ms": touch_cfg.sync_ms,
                "touch_cooldown_ms": touch_cfg.touch_cooldown_ms,
            },
        },
    )
    return {"summary": summary, "events": df}


def run_exp_lvn_flip(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0012 — dual LVN + beyond-untraded + opposing flip gates."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    vp_cfg, touch_cfg = _vp_touch_from_exp(exp)
    nq_cfg = LargeLevelConfig(
        absolute_size=int(exp.get("nq_absolute_size", 50)),
        z_score=float(exp.get("z_score", 99.0)),
        tick_radius=int(exp.get("tick_radius", 8)),
    )
    beyond_ticks = int(exp.get("beyond_ticks", 8))
    max_beyond_consume = float(exp.get("max_beyond_consume", 0.25))
    flip_window_ms = int(exp.get("flip_window_ms", 2000))
    min_flip_executed = float(exp.get("min_flip_executed", 10))
    require_beyond = bool(exp.get("require_beyond", True))
    require_flip = bool(exp.get("require_flip", True))

    dual_parts: list[pa.Table] = []
    gated_parts: list[pa.Table] = []
    strata = {"both": [], "beyond_only": [], "flip_only": [], "neither": []}
    per_session = []

    for sid in session_ids:
        print(f"[lvn-flip] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ"})
            continue
        if rth:
            nq = _filter_rth_uk_table(nq)
            if es.num_rows:
                es = _filter_rth_uk_table(es)

        dual, _nq_only, meta = dual_lvn_touch_events(nq, es, vp_cfg, touch_cfg)
        if dual.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no dual", **meta})
            continue

        nq_depth = _load_depth(parquet_dir, sid, "NQ")
        levels = []
        if nq_depth.num_rows:
            # load full-session trades for tracker (not RTH-filtered path alone)
            nq_full = _load_trades(parquet_dir, sid, "NQ")
            levels = run_liquidity_tracker(nq_depth, nq_full, cfg=nq_cfg, rth_uk_only=rth)

        annotated = annotate_beyond_and_flip(
            dual,
            levels,
            beyond_ticks=beyond_ticks,
            max_beyond_consume=max_beyond_consume,
            flip_window_ms=flip_window_ms,
            min_flip_executed=min_flip_executed,
        )

        path_ts, path_px = build_price_path(nq)
        labeled = label_events(annotated, path_ts, path_px, horizons, targets)
        if labeled.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "label empty"})
            continue

        labeled = labeled.append_column(
            "session_id", pa.array([sid] * labeled.num_rows, type=pa.string())
        )
        dual_parts.append(labeled)

        beyond = labeled.column("beyond_untraded").to_numpy()
        flip = labeled.column("flip").to_numpy()
        both_mask = (beyond == 1) & (flip == 1)
        b_only = (beyond == 1) & (flip == 0)
        f_only = (beyond == 0) & (flip == 1)
        neither = (beyond == 0) & (flip == 0)

        def _take(mask: np.ndarray) -> pa.Table:
            idx = np.where(mask)[0]
            return labeled.take(idx) if len(idx) else pa.table({})

        strata["both"].append(_take(both_mask))
        strata["beyond_only"].append(_take(b_only))
        strata["flip_only"].append(_take(f_only))
        strata["neither"].append(_take(neither))

        gate_mask = np.ones(labeled.num_rows, dtype=bool)
        if require_beyond:
            gate_mask &= beyond == 1
        if require_flip:
            gate_mask &= flip == 1
        gated = _take(gate_mask)
        if gated.num_rows:
            gated_parts.append(gated)

        per_session.append({
            "session_id": sid,
            "events": int(gated.num_rows),
            "dual_events": int(labeled.num_rows),
            "n_beyond": int(beyond.sum()),
            "n_flip": int(flip.sum()),
            "n_both": int(both_mask.sum()),
            "levels_closed": len(levels),
            "gated_hit10": _mean(gated.column("hit_10pts")) if gated.num_rows else None,
            "dual_hit10": _mean(labeled.column("hit_10pts")),
        })
        print(
            f"  dual={labeled.num_rows} both={int(both_mask.sum())} "
            f"gated={gated.num_rows} levels={len(levels)}",
            flush=True,
        )

    def _strata_summary(parts: list[pa.Table]) -> dict | None:
        parts = [p for p in parts if p.num_rows]
        if not parts:
            return None
        t = pa.concat_tables(parts)
        return {
            "n_events": int(t.num_rows),
            "p_hit_10pts": _mean(t.column("hit_10pts")),
            "p_hit_20pts": _mean(t.column("hit_20pts")) if "hit_20pts" in t.column_names else None,
            "mean_mfe_pts": _mean(t.column("mfe_pts")),
            "mean_mae_pts": _mean(t.column("mae_pts")),
            "mean_ret_pts_2000ms": _mean(t.column("ret_pts_2000ms")) if "ret_pts_2000ms" in t.column_names else None,
            "mean_ret_pts_10000ms": _mean(t.column("ret_pts_10000ms")) if "ret_pts_10000ms" in t.column_names else None,
        }

    strata_summary = {k: _strata_summary(v) for k, v in strata.items()}

    if not gated_parts:
        # still return dual pool for dig if available
        out_events = pa.concat_tables(dual_parts) if dual_parts else None
        return {
            "status": "no_gated_events",
            "sessions": per_session,
            "strata": strata_summary,
            "message": "Dual LVN events found but none passed beyond+flip gates.",
            **({"events": out_events, "summary": {
                "status": "no_gated_events",
                "n_events": 0,
                "sessions": per_session,
                "strata": strata_summary,
                "baselines_dual": _strata_summary(dual_parts) if dual_parts else None,
            }} if out_events is not None else {}),
        }

    df = pa.concat_tables(gated_parts)
    summary = _summarize_labeled(
        df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "strata": strata_summary,
            "baselines_dual": _strata_summary(dual_parts),
            "gates": {
                "require_beyond": require_beyond,
                "require_flip": require_flip,
                "beyond_ticks": beyond_ticks,
                "max_beyond_consume": max_beyond_consume,
                "flip_window_ms": flip_window_ms,
                "nq_absolute_size": nq_cfg.absolute_size,
                "sync_ms": touch_cfg.sync_ms,
            },
        },
    )
    # Also write full dual+flags for dig
    dual_all = pa.concat_tables(dual_parts) if dual_parts else df
    return {"summary": summary, "events": df, "events_all_dual": dual_all}


def run_exp_cd_pullback(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0014 — CD pullback primary ± ES+NQ momentum confirm."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    bar_ms = int(exp.get("bar_ms", 100))
    sync_ms = int(exp.get("sync_ms", 1500))
    confirm_ms = int(exp.get("confirm_window_ms", 2000))
    tick_size = float(exp.get("tick_size", 1.0))
    burst_kw = dict(
        min_trades=int(exp.get("min_trades", 6)),
        min_vol=float(exp.get("min_vol", 25)),
        min_imbalance=float(exp.get("min_imbalance", 0.55)),
        min_progress_ticks=float(exp.get("min_progress_ticks", 1.0)),
        tick_size=tick_size,
        cooldown_bars=int(exp.get("cooldown_bars", 3)),
        bar_ms=bar_ms,
    )

    pb_parts: list[pa.Table] = []
    conf_parts: list[pa.Table] = []
    mom_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[cd-pullback] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ"})
            continue
        if rth:
            nq = _filter_rth_uk_table(nq)
            if es.num_rows:
                es = _filter_rth_uk_table(es)

        cd = build_cd_series(
            nq,
            range_ticks=int(exp.get("range_ticks", 22)),
            tick_size=tick_size,
            sma_len=int(exp.get("sma_len", 100)),
            slope_lookback=int(exp.get("slope_lookback", 5)),
            pullback_frac_of_bb=float(exp.get("pullback_frac_of_bb", 0.35)),
        )
        pb = cd_pullback_events(cd, cooldown_bars=int(exp.get("cd_cooldown_bars", 3)))

        nq_bars = trades_to_tape_bars(nq, bar_ms=bar_ms)
        es_bars = trades_to_tape_bars(es, bar_ms=bar_ms) if es.num_rows else pa.table({})
        nq_burst = momentum_burst_events(nq_bars, **burst_kw)
        es_burst = momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({})
        mom = cross_confirm_bursts(nq_burst, es_burst, sync_ms=sync_ms)

        confirmed = filter_events_near_bursts(
            pb, mom, window_ms=confirm_ms, require_same_side=True
        ) if pb.num_rows and mom.num_rows else pa.table({})

        path_ts, path_px = build_price_path(nq)
        lab_pb = label_events(pb, path_ts, path_px, horizons, targets)
        lab_cf = label_events(confirmed, path_ts, path_px, horizons, targets)
        lab_mom = label_events(mom, path_ts, path_px, horizons, targets)

        def _sid(t: pa.Table) -> pa.Table:
            if t.num_rows == 0:
                return t
            return t.append_column("session_id", pa.array([sid] * t.num_rows, type=pa.string()))

        lab_pb, lab_cf, lab_mom = _sid(lab_pb), _sid(lab_cf), _sid(lab_mom)
        if lab_pb.num_rows:
            pb_parts.append(lab_pb)
        if lab_cf.num_rows:
            conf_parts.append(lab_cf)
        if lab_mom.num_rows:
            mom_parts.append(lab_mom)

        row = {
            "session_id": sid,
            "events": int(lab_cf.num_rows),
            "cd_pullbacks": int(lab_pb.num_rows),
            "momentum": int(lab_mom.num_rows),
            "confirmed": int(lab_cf.num_rows),
            "pb_hit10": _mean(lab_pb.column("hit_10pts")) if lab_pb.num_rows else None,
            "conf_hit10": _mean(lab_cf.column("hit_10pts")) if lab_cf.num_rows else None,
            "mom_hit10": _mean(lab_mom.column("hit_10pts")) if lab_mom.num_rows else None,
        }
        per_session.append(row)
        print(
            f"  pb={lab_pb.num_rows} mom={lab_mom.num_rows} conf={lab_cf.num_rows} "
            f"pb_hit10={row['pb_hit10']} conf_hit10={row['conf_hit10']}",
            flush=True,
        )

    def _pack(parts: list[pa.Table]) -> dict | None:
        if not parts:
            return None
        t = pa.concat_tables(parts)
        return {
            "n_events": int(t.num_rows),
            "p_hit_10pts": _mean(t.column("hit_10pts")),
            "p_hit_20pts": _mean(t.column("hit_20pts")) if "hit_20pts" in t.column_names else None,
            "mean_mfe_pts": _mean(t.column("mfe_pts")),
            "mean_mae_pts": _mean(t.column("mae_pts")),
            "mean_ret_pts_2000ms": _mean(t.column("ret_pts_2000ms")) if "ret_pts_2000ms" in t.column_names else None,
            "mean_ret_pts_10000ms": _mean(t.column("ret_pts_10000ms")) if "ret_pts_10000ms" in t.column_names else None,
            "median_ret_pts_10000ms": _median(t.column("ret_pts_10000ms")) if "ret_pts_10000ms" in t.column_names else None,
        }

    baselines = {
        "cd_pullback_only": _pack(pb_parts),
        "momentum_es_nq": _pack(mom_parts),
    }
    if not conf_parts and not pb_parts:
        return {"status": "no_events", "sessions": per_session, "baselines": baselines}

    # Primary result = confirmed if any, else pullback-only for dig
    primary_parts = conf_parts if conf_parts else pb_parts
    df = pa.concat_tables(primary_parts)
    summary = _summarize_labeled(
        df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "baselines": baselines,
            "confirmed": _pack(conf_parts),
            "primary": "cd_pullback_x_momentum" if conf_parts else "cd_pullback_only",
            "gates": {
                "confirm_window_ms": confirm_ms,
                "sync_ms": sync_ms,
                **burst_kw,
            },
        },
    )
    # Always persist pullback-only + confirmed for dig
    out = {"summary": summary, "events": df}
    if pb_parts:
        out["events_pullback"] = pa.concat_tables(pb_parts)
    if conf_parts:
        out["events_confirmed"] = pa.concat_tables(conf_parts)
    return out


def run_exp_dwell_withdraw(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0013 — dual ES+NQ dwell_repel / withdraw events."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    sync_ms = int(exp.get("sync_ms", 2000))
    nq_cfg = LargeLevelConfig(
        absolute_size=int(exp.get("nq_absolute_size", 50)),
        z_score=float(exp.get("z_score", 99.0)),
        tick_radius=int(exp.get("tick_radius", 8)),
        dwell_alert_sec=float(exp.get("dwell_alert_sec", 3.0)),
    )
    es_cfg = LargeLevelConfig(
        absolute_size=int(exp.get("es_absolute_size", 100)),
        z_score=float(exp.get("z_score", 99.0)),
        tick_radius=int(exp.get("tick_radius", 8)),
        dwell_alert_sec=float(exp.get("dwell_alert_sec", 3.0)),
    )
    min_dwell = float(exp.get("min_dwell_sec", 3.0))

    dual_parts: list[pa.Table] = []
    nq_parts: list[pa.Table] = []
    per_type: dict[str, list[pa.Table]] = {"dwell_repel": [], "withdraw": []}
    per_session = []

    for sid in session_ids:
        print(f"[dwell-withdraw] session {sid} …", flush=True)
        nq_tr = _load_trades(parquet_dir, sid, "NQ")
        es_tr = _load_trades(parquet_dir, sid, "ES")
        nq_depth = _load_depth(parquet_dir, sid, "NQ")
        es_depth = _load_depth(parquet_dir, sid, "ES")
        if nq_tr.num_rows == 0 or nq_depth.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ depth/trades"})
            continue

        nq_levels = run_liquidity_tracker(nq_depth, nq_tr, cfg=nq_cfg, rth_uk_only=rth)
        es_levels = (
            run_liquidity_tracker(es_depth, es_tr, cfg=es_cfg, rth_uk_only=rth)
            if es_depth.num_rows and es_tr.num_rows
            else []
        )
        nq_ev = dwell_withdraw_events_from_levels(
            nq_levels,
            min_dwell_sec=min_dwell,
            min_initial_size=nq_cfg.absolute_size,
        )
        es_ev = dwell_withdraw_events_from_levels(
            es_levels,
            min_dwell_sec=min_dwell,
            min_initial_size=es_cfg.absolute_size,
        )
        dual = cross_confirm_liquidity_events(nq_ev, es_ev, sync_ms=sync_ms)

        nq_rth = _filter_rth_uk_table(nq_tr) if rth else nq_tr
        path_ts, path_px = build_price_path(nq_rth if nq_rth.num_rows else nq_tr)
        lab_nq = label_events(nq_ev, path_ts, path_px, horizons, targets)
        lab_dual = label_events(dual, path_ts, path_px, horizons, targets)

        if lab_nq.num_rows:
            lab_nq = lab_nq.append_column(
                "session_id", pa.array([sid] * lab_nq.num_rows, type=pa.string())
            )
            nq_parts.append(lab_nq)
            types = lab_nq.column("event_type").to_pylist() if "event_type" in lab_nq.column_names else []
            for et in ("dwell_repel", "withdraw"):
                idx = [i for i, t in enumerate(types) if t == et]
                if idx:
                    per_type[et].append(lab_nq.take(np.asarray(idx, dtype=np.int64)))

        if lab_dual.num_rows:
            lab_dual = lab_dual.append_column(
                "session_id", pa.array([sid] * lab_dual.num_rows, type=pa.string())
            )
            dual_parts.append(lab_dual)

        per_session.append({
            "session_id": sid,
            "events": int(lab_dual.num_rows),
            "nq_only": int(lab_nq.num_rows),
            "nq_levels": len(nq_levels),
            "es_levels": len(es_levels),
            "dual_hit10": _mean(lab_dual.column("hit_10pts")) if lab_dual.num_rows else None,
            "nq_hit10": _mean(lab_nq.column("hit_10pts")) if lab_nq.num_rows else None,
        })
        print(
            f"  nq={lab_nq.num_rows} dual={lab_dual.num_rows} "
            f"levels nq/es={len(nq_levels)}/{len(es_levels)}",
            flush=True,
        )

    def _pack(parts: list[pa.Table]) -> dict | None:
        parts = [p for p in parts if p.num_rows]
        if not parts:
            return None
        t = pa.concat_tables(parts)
        return {
            "n_events": int(t.num_rows),
            "p_hit_10pts": _mean(t.column("hit_10pts")),
            "p_hit_20pts": _mean(t.column("hit_20pts")) if "hit_20pts" in t.column_names else None,
            "mean_mfe_pts": _mean(t.column("mfe_pts")),
            "mean_mae_pts": _mean(t.column("mae_pts")),
            "mean_ret_pts_2000ms": _mean(t.column("ret_pts_2000ms")) if "ret_pts_2000ms" in t.column_names else None,
            "mean_ret_pts_10000ms": _mean(t.column("ret_pts_10000ms")) if "ret_pts_10000ms" in t.column_names else None,
        }

    type_summary = {k: _pack(v) for k, v in per_type.items()}
    baselines = {"nq_only": _pack(nq_parts), "by_type_nq": type_summary}

    if not dual_parts:
        return {
            "status": "no_dual_events",
            "sessions": per_session,
            "baselines": baselines,
            "message": "No dual dwell/withdraw confirms.",
            **({"events": pa.concat_tables(nq_parts), "summary": {
                "status": "no_dual_events",
                "n_events": 0,
                "sessions": per_session,
                "baselines": baselines,
            }} if nq_parts else {}),
        }

    df = pa.concat_tables(dual_parts)
    # Split dual by type
    dual_by_type = {}
    if "event_type" in df.column_names:
        types = df.column("event_type").to_pylist()
        for et in ("dwell_repel", "withdraw"):
            idx = [i for i, t in enumerate(types) if t == et]
            if idx:
                dual_by_type[et] = _pack([df.take(np.asarray(idx, dtype=np.int64))])

    summary = _summarize_labeled(
        df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "baselines": baselines,
            "dual_by_type": dual_by_type,
            "gates": {
                "sync_ms": sync_ms,
                "nq_absolute_size": nq_cfg.absolute_size,
                "es_absolute_size": es_cfg.absolute_size,
                "min_dwell_sec": min_dwell,
            },
        },
    )
    return {"summary": summary, "events": df}


def run_exp_momentum_bb_regime(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0015 — ES+NQ momentum stratified by CD BB width regime."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    bar_ms = int(exp.get("bar_ms", 100))
    sync_ms = int(exp.get("sync_ms", 1500))
    max_lag = float(exp.get("max_es_lag_ms", 250))
    tick_size = float(exp.get("tick_size", 1.0))
    wide_pct = float(exp.get("wide_pct", 0.70))
    narrow_pct = float(exp.get("narrow_pct", 0.30))
    burst_kw = dict(
        min_trades=int(exp.get("min_trades", 6)),
        min_vol=float(exp.get("min_vol", 25)),
        min_imbalance=float(exp.get("min_imbalance", 0.55)),
        min_progress_ticks=float(exp.get("min_progress_ticks", 1.0)),
        tick_size=tick_size,
        cooldown_bars=int(exp.get("cooldown_bars", 3)),
        bar_ms=bar_ms,
    )

    all_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[mom-bb-regime] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ"})
            continue
        if rth:
            nq = _filter_rth_uk_table(nq)
            if es.num_rows:
                es = _filter_rth_uk_table(es)

        nq_bars = trades_to_tape_bars(nq, bar_ms=bar_ms)
        es_bars = trades_to_tape_bars(es, bar_ms=bar_ms) if es.num_rows else pa.table({})
        nq_burst = momentum_burst_events(nq_bars, **burst_kw)
        es_burst = momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({})
        events = cross_confirm_bursts(nq_burst, es_burst, sync_ms=sync_ms)

        if events.num_rows and max_lag > 0 and "es_lag_ms" in events.column_names:
            lag = events.column("es_lag_ms").to_numpy()
            events = events.take(np.where(lag <= max_lag)[0])

        cd = build_cd_series(
            nq,
            range_ticks=int(exp.get("range_ticks", 22)),
            tick_size=tick_size,
            sma_len=int(exp.get("sma_len", 100)),
            regime_pct_lookback=int(exp.get("regime_pct_lookback", 100)),
            wide_pct=wide_pct,
            narrow_pct=narrow_pct,
        )
        path_ts, path_px = build_price_path(nq)
        labeled = label_events(events, path_ts, path_px, horizons, targets)
        if labeled.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0})
            continue

        w, p, r = lookup_regime_at(cd, labeled.column("event_ts_ns").to_numpy())
        for name, arr, typ in (
            ("cd_bb_width", w, pa.float64()),
            ("cd_bb_width_pct", p, pa.float64()),
            ("cd_bb_regime", r, pa.int8()),
        ):
            if name in labeled.column_names:
                labeled = labeled.drop(name)
            labeled = labeled.append_column(name, pa.array(arr, type=typ))

        labeled = labeled.append_column(
            "session_id", pa.array([sid] * labeled.num_rows, type=pa.string())
        )
        all_parts.append(labeled)
        reg = labeled.column("cd_bb_regime").to_numpy()
        hit = labeled.column("hit_10pts").to_numpy()
        per_session.append({
            "session_id": sid,
            "events": int(labeled.num_rows),
            "n_wide": int((reg == 1).sum()),
            "n_narrow": int((reg == -1).sum()),
            "n_mid": int((reg == 0).sum()),
            "wide_hit10": _mean(hit[reg == 1]) if (reg == 1).any() else None,
            "narrow_hit10": _mean(hit[reg == -1]) if (reg == -1).any() else None,
            "all_hit10": _mean(hit),
        })
        print(
            f"  n={labeled.num_rows} wide={(reg == 1).sum()} narrow={(reg == -1).sum()} "
            f"wide_hit10={per_session[-1]['wide_hit10']} narrow_hit10={per_session[-1]['narrow_hit10']}",
            flush=True,
        )

    if not all_parts:
        return {"status": "no_events", "sessions": per_session}

    df = pa.concat_tables(all_parts)
    reg = df.column("cd_bb_regime").to_numpy()

    def _pack(mask: np.ndarray) -> dict | None:
        idx = np.where(mask)[0]
        if len(idx) == 0:
            return None
        t = df.take(idx)
        return {
            "n_events": int(t.num_rows),
            "p_hit_10pts": _mean(t.column("hit_10pts")),
            "p_hit_20pts": _mean(t.column("hit_20pts")),
            "mean_mfe_pts": _mean(t.column("mfe_pts")),
            "mean_mae_pts": _mean(t.column("mae_pts")),
            "mean_ret_pts_2000ms": _mean(t.column("ret_pts_2000ms")),
            "mean_ret_pts_10000ms": _mean(t.column("ret_pts_10000ms")),
            "median_ret_pts_10000ms": _median(t.column("ret_pts_10000ms")),
        }

    strata = {
        "all": _pack(np.ones(df.num_rows, dtype=bool)),
        "wide": _pack(reg == 1),
        "narrow": _pack(reg == -1),
        "mid": _pack(reg == 0),
    }
    # Primary = wide (hypothesized momentum regime)
    primary = df.take(np.where(reg == 1)[0]) if (reg == 1).any() else df
    summary = _summarize_labeled(
        primary if primary.num_rows else df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "strata": strata,
            "primary": "wide_bb_regime",
            "gates": {
                "max_es_lag_ms": max_lag,
                "wide_pct": wide_pct,
                "narrow_pct": narrow_pct,
                "sync_ms": sync_ms,
                **burst_kw,
            },
        },
    )
    return {"summary": summary, "events": df}


def run_exp_dual_cd_filter(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0022 — dual ES+NQ CD trend/BB filters on EXP-0008 momentum."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    bar_ms = int(exp.get("bar_ms", 100))
    sync_ms = int(exp.get("sync_ms", 1500))
    max_lag = float(exp.get("max_es_lag_ms", 250))
    tick_size = float(exp.get("tick_size", 1.0))
    wide_pct = float(exp.get("wide_pct", 0.70))
    narrow_pct = float(exp.get("narrow_pct", 0.30))
    cd_kw = dict(
        range_ticks=int(exp.get("range_ticks", 22)),
        tick_size=tick_size,
        sma_len=int(exp.get("sma_len", 100)),
        slope_lookback=int(exp.get("slope_lookback", 5)),
        regime_pct_lookback=int(exp.get("regime_pct_lookback", 100)),
        wide_pct=wide_pct,
        narrow_pct=narrow_pct,
    )
    burst_kw = dict(
        min_trades=int(exp.get("min_trades", 6)),
        min_vol=float(exp.get("min_vol", 25)),
        min_imbalance=float(exp.get("min_imbalance", 0.55)),
        min_progress_ticks=float(exp.get("min_progress_ticks", 1.0)),
        tick_size=tick_size,
        cooldown_bars=int(exp.get("cooldown_bars", 3)),
        bar_ms=bar_ms,
    )

    all_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[dual-cd-filter] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ"})
            continue
        if rth:
            nq = _filter_rth_uk_table(nq)
            if es.num_rows:
                es = _filter_rth_uk_table(es)

        nq_bars = trades_to_tape_bars(nq, bar_ms=bar_ms)
        es_bars = trades_to_tape_bars(es, bar_ms=bar_ms) if es.num_rows else pa.table({})
        events = cross_confirm_bursts(
            momentum_burst_events(nq_bars, **burst_kw),
            momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
            sync_ms=sync_ms,
        )
        if events.num_rows and max_lag > 0 and "es_lag_ms" in events.column_names:
            lag = events.column("es_lag_ms").to_numpy()
            events = events.take(np.where(np.abs(lag) <= max_lag)[0])

        nq_cd = build_cd_series(nq, **cd_kw)
        es_cd = build_cd_series(es, **cd_kw) if es.num_rows else pa.table({})
        path_ts, path_px = build_price_path(nq)
        labeled = label_events(events, path_ts, path_px, horizons, targets)
        if labeled.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0})
            continue

        labeled = annotate_dual_cd_fields(labeled, nq_cd, es_cd)
        labeled = labeled.append_column(
            "session_id", pa.array([sid] * labeled.num_rows, type=pa.string())
        )
        all_parts.append(labeled)

        dual_t = labeled.column("dual_trend_aligned").to_numpy()
        dual_w = labeled.column("dual_wide").to_numpy()
        hit = labeled.column("hit_10pts").to_numpy()
        row = {
            "session_id": sid,
            "events": int(labeled.num_rows),
            "n_dual_trend": int(dual_t.sum()),
            "n_dual_wide": int(dual_w.sum()),
            "n_dual_trend_wide": int(
                (labeled.column("dual_trend_and_wide").to_numpy() == 1).sum()
            ),
            "all_hit10": _mean(hit),
            "dual_trend_hit10": _mean(hit[dual_t == 1]) if dual_t.any() else None,
            "dual_wide_hit10": _mean(hit[dual_w == 1]) if dual_w.any() else None,
        }
        per_session.append(row)
        print(
            f"  n={row['events']} dual_trend={row['n_dual_trend']} "
            f"dual_wide={row['n_dual_wide']} "
            f"trend_hit10={row['dual_trend_hit10']} all_hit10={row['all_hit10']}",
            flush=True,
        )

    if not all_parts:
        return {"status": "no_events", "sessions": per_session}

    df = pa.concat_tables(all_parts)

    def _pack(mask: np.ndarray) -> dict | None:
        idx = np.where(mask)[0]
        if len(idx) == 0:
            return None
        t = df.take(idx)
        return {
            "n_events": int(t.num_rows),
            "p_hit_10pts": _mean(t.column("hit_10pts")),
            "p_hit_20pts": _mean(t.column("hit_20pts")),
            "mean_mfe_pts": _mean(t.column("mfe_pts")),
            "mean_mae_pts": _mean(t.column("mae_pts")),
            "mean_ret_pts_2000ms": _mean(t.column("ret_pts_2000ms")),
            "mean_ret_pts_10000ms": _mean(t.column("ret_pts_10000ms")),
            "median_ret_pts_10000ms": _median(t.column("ret_pts_10000ms")),
        }

    dual_t = df.column("dual_trend_aligned").to_numpy() == 1
    nq_t = df.column("nq_trend_aligned").to_numpy() == 1
    es_t = df.column("es_trend_aligned").to_numpy() == 1
    dual_w = df.column("dual_wide").to_numpy() == 1
    dual_same = df.column("dual_same_regime").to_numpy() == 1
    dual_tw = df.column("dual_trend_and_wide").to_numpy() == 1
    # against: trade side opposes both CD trends
    nq_tr = df.column("nq_cd_ma_trend").to_numpy()
    es_tr = df.column("es_cd_ma_trend").to_numpy()
    side = df.column("side").to_numpy()
    against = (
        ((side > 0) & (nq_tr < 0) & (es_tr < 0))
        | ((side < 0) & (nq_tr > 0) & (es_tr > 0))
    )

    strata = {
        "all_unfiltered": _pack(np.ones(df.num_rows, dtype=bool)),
        "nq_trend_aligned": _pack(nq_t),
        "es_trend_aligned": _pack(es_t),
        "dual_trend_aligned": _pack(dual_t),
        "dual_wide": _pack(dual_w),
        "dual_same_regime": _pack(dual_same),
        "dual_trend_and_wide": _pack(dual_tw),
        "dual_against_trend": _pack(against),
        "nq_trend_not_dual": _pack(nq_t & ~dual_t),
    }

    # Primary = dual trend (user ask)
    if dual_t.any():
        primary = df.take(np.where(dual_t)[0])
        primary_name = "dual_trend_aligned"
    else:
        primary = df
        primary_name = "all_unfiltered"

    summary = _summarize_labeled(
        primary if primary.num_rows else df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "strata": strata,
            "primary": primary_name,
            "gates": {
                "max_es_lag_ms": max_lag,
                "sync_ms": sync_ms,
                "wide_pct": wide_pct,
                "narrow_pct": narrow_pct,
                **burst_kw,
                **{k: cd_kw[k] for k in ("range_ticks", "sma_len", "slope_lookback")},
            },
        },
    )
    return {"summary": summary, "events": df}


def run_exp_challenge_wo_trade(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0016 — dual challenge-without-trade × narrow BB (± LVN)."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    sync_ms = int(exp.get("sync_ms", 2500))
    wide_pct = float(exp.get("wide_pct", 0.70))
    narrow_pct = float(exp.get("narrow_pct", 0.30))
    nq_cfg = LargeLevelConfig(
        absolute_size=int(exp.get("nq_absolute_size", 40)),
        z_score=float(exp.get("z_score", 99.0)),
        tick_radius=int(exp.get("tick_radius", 12)),
        dwell_alert_sec=float(exp.get("dwell_alert_sec", 2.0)),
    )
    es_cfg = LargeLevelConfig(
        absolute_size=int(exp.get("es_absolute_size", 80)),
        z_score=float(exp.get("z_score", 99.0)),
        tick_radius=int(exp.get("tick_radius", 12)),
        dwell_alert_sec=float(exp.get("dwell_alert_sec", 2.0)),
    )
    min_dwell = float(exp.get("min_dwell_sec", 2.0))
    max_cons = float(exp.get("max_consume_ratio", 0.15))
    vp_cfg = VolumeProfileConfig(
        lookback_ms=int(exp.get("lvn_lookback_ms", 1_800_000)),
        refresh_ms=int(exp.get("lvn_refresh_ms", 30_000)),
        tick_size=float(exp.get("tick_size", 1.0)),
    )

    dual_parts: list[pa.Table] = []
    nq_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[challenge-wo-trade] session {sid} …", flush=True)
        nq_tr = _load_trades(parquet_dir, sid, "NQ")
        es_tr = _load_trades(parquet_dir, sid, "ES")
        nq_depth = _load_depth(parquet_dir, sid, "NQ")
        es_depth = _load_depth(parquet_dir, sid, "ES")
        if nq_tr.num_rows == 0 or nq_depth.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ"})
            continue

        nq_rth = _filter_rth_uk_table(nq_tr) if rth else nq_tr
        es_rth = _filter_rth_uk_table(es_tr) if (rth and es_tr.num_rows) else es_tr

        nq_levels = run_liquidity_tracker(nq_depth, nq_tr, cfg=nq_cfg, rth_uk_only=rth)
        es_levels = (
            run_liquidity_tracker(es_depth, es_tr, cfg=es_cfg, rth_uk_only=rth)
            if es_depth.num_rows and es_tr.num_rows
            else []
        )
        nq_ev = challenge_without_trade_events(
            nq_levels,
            min_dwell_sec=min_dwell,
            max_consume_ratio=max_cons,
            min_initial_size=nq_cfg.absolute_size,
        )
        es_ev = challenge_without_trade_events(
            es_levels,
            min_dwell_sec=min_dwell,
            max_consume_ratio=max_cons,
            min_initial_size=es_cfg.absolute_size,
        )
        dual = cross_confirm_liquidity_events(
            nq_ev, es_ev, sync_ms=sync_ms, require_same_event_type=True
        )

        lvn_snaps = causal_lvn_snapshots(nq_rth, vp_cfg) if nq_rth.num_rows else []
        if dual.num_rows and lvn_snaps:
            dual = annotate_near_lvn(dual, lvn_snaps, pad_ticks=int(exp.get("lvn_pad_ticks", 6)))
        elif dual.num_rows:
            dual = dual.append_column(
                "near_lvn", pa.array([0] * dual.num_rows, type=pa.int8())
            )
        if nq_ev.num_rows and lvn_snaps:
            nq_ev = annotate_near_lvn(nq_ev, lvn_snaps, pad_ticks=int(exp.get("lvn_pad_ticks", 6)))

        cd = build_cd_series(
            nq_rth if nq_rth.num_rows else nq_tr,
            range_ticks=int(exp.get("range_ticks", 22)),
            tick_size=float(exp.get("tick_size", 1.0)),
            sma_len=int(exp.get("sma_len", 100)),
            regime_pct_lookback=int(exp.get("regime_pct_lookback", 100)),
            wide_pct=wide_pct,
            narrow_pct=narrow_pct,
        )
        path_ts, path_px = build_price_path(nq_rth if nq_rth.num_rows else nq_tr)

        def _label_anno(ev: pa.Table) -> pa.Table:
            lab = label_events(ev, path_ts, path_px, horizons, targets)
            if lab.num_rows == 0:
                return lab
            w, p, r = lookup_regime_at(cd, lab.column("event_ts_ns").to_numpy())
            for name, arr, typ in (
                ("cd_bb_width", w, pa.float64()),
                ("cd_bb_width_pct", p, pa.float64()),
                ("cd_bb_regime", r, pa.int8()),
            ):
                if name in lab.column_names:
                    lab = lab.drop(name)
                lab = lab.append_column(name, pa.array(arr, type=typ))
            return lab.append_column(
                "session_id", pa.array([sid] * lab.num_rows, type=pa.string())
            )

        lab_dual = _label_anno(dual)
        lab_nq = _label_anno(nq_ev)
        if lab_dual.num_rows:
            dual_parts.append(lab_dual)
        if lab_nq.num_rows:
            nq_parts.append(lab_nq)

        reg = lab_dual.column("cd_bb_regime").to_numpy() if lab_dual.num_rows else np.array([])
        near = (
            lab_dual.column("near_lvn").to_numpy()
            if lab_dual.num_rows and "near_lvn" in lab_dual.column_names
            else np.array([])
        )
        per_session.append({
            "session_id": sid,
            "events": int(lab_dual.num_rows),
            "nq_only": int(lab_nq.num_rows),
            "n_narrow": int((reg == -1).sum()) if len(reg) else 0,
            "n_near_lvn": int(near.sum()) if len(near) else 0,
            "dual_hit10": _mean(lab_dual.column("hit_10pts")) if lab_dual.num_rows else None,
            "nq_hit10": _mean(lab_nq.column("hit_10pts")) if lab_nq.num_rows else None,
            "nq_levels": len(nq_levels),
            "es_levels": len(es_levels),
        })
        print(
            f"  nq_chal={lab_nq.num_rows} dual={lab_dual.num_rows} "
            f"narrow={(reg == -1).sum() if len(reg) else 0} "
            f"near_lvn={int(near.sum()) if len(near) else 0}",
            flush=True,
        )

    def _pack(t: pa.Table | None) -> dict | None:
        if t is None or t.num_rows == 0:
            return None
        return {
            "n_events": int(t.num_rows),
            "p_hit_10pts": _mean(t.column("hit_10pts")),
            "p_hit_20pts": _mean(t.column("hit_20pts")),
            "mean_mfe_pts": _mean(t.column("mfe_pts")),
            "mean_mae_pts": _mean(t.column("mae_pts")),
            "mean_ret_pts_2000ms": _mean(t.column("ret_pts_2000ms")),
            "mean_ret_pts_10000ms": _mean(t.column("ret_pts_10000ms")),
            "median_ret_pts_10000ms": _median(t.column("ret_pts_10000ms")),
        }

    def _take_mask(t: pa.Table, mask: np.ndarray) -> pa.Table:
        idx = np.where(mask)[0]
        return t.take(idx) if len(idx) else pa.table({})

    if not dual_parts and not nq_parts:
        return {"status": "no_events", "sessions": per_session}

    nq_df = pa.concat_tables(nq_parts) if nq_parts else None
    dual_df = pa.concat_tables(dual_parts) if dual_parts else None

    strata = {"nq_only": _pack(nq_df)}
    if dual_df is not None:
        reg = dual_df.column("cd_bb_regime").to_numpy()
        near = (
            dual_df.column("near_lvn").to_numpy()
            if "near_lvn" in dual_df.column_names
            else np.zeros(dual_df.num_rows, dtype=np.int8)
        )
        strata.update({
            "dual_all": _pack(dual_df),
            "dual_narrow": _pack(_take_mask(dual_df, reg == -1)),
            "dual_wide": _pack(_take_mask(dual_df, reg == 1)),
            "dual_mid": _pack(_take_mask(dual_df, reg == 0)),
            "dual_near_lvn": _pack(_take_mask(dual_df, near == 1)),
            "dual_narrow_and_lvn": _pack(_take_mask(dual_df, (reg == -1) & (near == 1))),
            "dual_narrow_no_lvn": _pack(_take_mask(dual_df, (reg == -1) & (near == 0))),
        })

    # Primary hypothesized: dual + narrow BB
    if dual_df is not None and (dual_df.column("cd_bb_regime").to_numpy() == -1).any():
        primary = _take_mask(dual_df, dual_df.column("cd_bb_regime").to_numpy() == -1)
        primary_name = "dual_narrow"
    elif dual_df is not None:
        primary = dual_df
        primary_name = "dual_all"
    else:
        primary = nq_df
        primary_name = "nq_only"

    summary = _summarize_labeled(
        primary if primary.num_rows else (dual_df or nq_df),
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "strata": strata,
            "primary": primary_name,
            "gates": {
                "sync_ms": sync_ms,
                "max_consume_ratio": max_cons,
                "min_dwell_sec": min_dwell,
                "nq_absolute_size": nq_cfg.absolute_size,
                "es_absolute_size": es_cfg.absolute_size,
                "tick_radius": nq_cfg.tick_radius,
                "narrow_pct": narrow_pct,
                "wide_pct": wide_pct,
            },
        },
    )
    out_events = dual_df if dual_df is not None else nq_df
    return {"summary": summary, "events": out_events}


def run_exp_aggressor_sweep(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0017 — dual same-aggressor sweeps vs null / NQ-only / EXP-0008."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    sync_ms = int(exp.get("sync_ms", 1500))
    max_lag = float(exp.get("max_es_lag_ms", 250))
    sweep_cfg = SweepConfig(
        min_prints=int(exp.get("min_prints", 5)),
        min_vol=float(exp.get("min_vol", 5)),
        min_span_ticks=float(exp.get("min_span_ticks", 1)),
        max_duration_ms=int(exp.get("max_duration_ms", 2000)),
        tick_size=float(exp.get("tick_size", 1.0)),
        cooldown_ms=int(exp.get("cooldown_ms", 500)),
    )
    burst_kw = dict(
        min_trades=6, min_vol=25.0, min_imbalance=0.55,
        min_progress_ticks=1.0, tick_size=1.0, cooldown_bars=3, bar_ms=100)

    dual_parts: list[pa.Table] = []
    nq_parts: list[pa.Table] = []
    mom_parts: list[pa.Table] = []
    null_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[aggressor-sweep] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ"})
            continue
        if rth:
            nq = _filter_rth_uk_table(nq)
            if es.num_rows:
                es = _filter_rth_uk_table(es)

        nq_sw = aggressor_sweep_events(nq, sweep_cfg)
        es_sw = aggressor_sweep_events(es, sweep_cfg) if es.num_rows else pa.table({})
        dual = cross_confirm_sweeps(nq_sw, es_sw, sync_ms=sync_ms)
        if dual.num_rows and max_lag > 0 and "es_lag_ms" in dual.column_names:
            lag = np.abs(dual.column("es_lag_ms").to_numpy())
            dual = dual.take(np.where(lag <= max_lag)[0])

        # EXP-0008 control on same session
        nq_bars = trades_to_tape_bars(nq, bar_ms=100)
        es_bars = trades_to_tape_bars(es, bar_ms=100) if es.num_rows else pa.table({})
        mom = cross_confirm_bursts(
            momentum_burst_events(nq_bars, **burst_kw),
            momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
            sync_ms=1500,
        )
        if mom.num_rows and "es_lag_ms" in mom.column_names:
            mom = mom.take(np.where(np.abs(mom.column("es_lag_ms").to_numpy()) <= max_lag)[0])

        path_ts, path_px = build_price_path(nq)
        lab_dual = label_events(dual, path_ts, path_px, horizons, targets)
        lab_nq = label_events(nq_sw, path_ts, path_px, horizons, targets)
        lab_mom = label_events(mom, path_ts, path_px, horizons, targets)
        null_ev = _random_null_events(nq, max(int(lab_dual.num_rows), 50), seed=hash(sid) % (2**31))
        lab_null = label_events(null_ev, path_ts, path_px, horizons, targets)

        def _sid(t: pa.Table) -> pa.Table:
            if t.num_rows == 0:
                return t
            return t.append_column("session_id", pa.array([sid] * t.num_rows, type=pa.string()))

        lab_dual, lab_nq, lab_mom, lab_null = map(_sid, (lab_dual, lab_nq, lab_mom, lab_null))
        if lab_dual.num_rows:
            dual_parts.append(lab_dual)
        if lab_nq.num_rows:
            nq_parts.append(lab_nq)
        if lab_mom.num_rows:
            mom_parts.append(lab_mom)
        if lab_null.num_rows:
            null_parts.append(lab_null)

        row = {
            "session_id": sid,
            "events": int(lab_dual.num_rows),
            "nq_sweeps": int(lab_nq.num_rows),
            "mom_0008": int(lab_mom.num_rows),
            "dual_hit10": _mean(lab_dual.column("hit_10pts")) if lab_dual.num_rows else None,
            "nq_hit10": _mean(lab_nq.column("hit_10pts")) if lab_nq.num_rows else None,
            "mom_hit10": _mean(lab_mom.column("hit_10pts")) if lab_mom.num_rows else None,
            "null_hit10": _mean(lab_null.column("hit_10pts")) if lab_null.num_rows else None,
        }
        per_session.append(row)
        print(
            f"  dual={lab_dual.num_rows} nq={lab_nq.num_rows} mom={lab_mom.num_rows} "
            f"dual_hit10={row['dual_hit10']}",
            flush=True,
        )

    def _pack(parts: list[pa.Table]) -> dict | None:
        if not parts:
            return None
        t = pa.concat_tables(parts)
        return {
            "n_events": int(t.num_rows),
            "p_hit_10pts": _mean(t.column("hit_10pts")),
            "p_hit_20pts": _mean(t.column("hit_20pts")),
            "mean_mfe_pts": _mean(t.column("mfe_pts")),
            "mean_mae_pts": _mean(t.column("mae_pts")),
            "mean_ret_pts_2000ms": _mean(t.column("ret_pts_2000ms")),
            "mean_ret_pts_10000ms": _mean(t.column("ret_pts_10000ms")),
            "median_ret_pts_10000ms": _median(t.column("ret_pts_10000ms")),
        }

    baselines = {
        "nq_only_sweeps": _pack(nq_parts),
        "exp0008_lag250": _pack(mom_parts),
        "null_random": _pack(null_parts),
    }
    if not dual_parts:
        return {
            "status": "no_events",
            "sessions": per_session,
            "baselines": baselines,
            "message": "No dual aggressor sweeps found.",
        }

    df = pa.concat_tables(dual_parts)
    summary = _summarize_labeled(
        df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "baselines": baselines,
            "gates": {
                "min_prints": sweep_cfg.min_prints,
                "min_vol": sweep_cfg.min_vol,
                "min_span_ticks": sweep_cfg.min_span_ticks,
                "max_duration_ms": sweep_cfg.max_duration_ms,
                "sync_ms": sync_ms,
                "max_es_lag_ms": max_lag,
            },
        },
    )
    return {"summary": summary, "events": df}


def run_exp_mbo_vacuum(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0018 — dual near-BBO MBO cancel vacuum vs null / NQ-only."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    sync_ms = int(exp.get("sync_ms", 1500))
    max_lag = float(exp.get("max_es_lag_ms", 250))
    vac_cfg = VacuumConfig(
        window_ms=int(exp.get("window_ms", 300)),
        tick_radius=int(exp.get("tick_radius", 4)),
        min_cancels=int(exp.get("min_cancels", 20)),
        min_cancel_ratio=float(exp.get("min_cancel_ratio", 0.70)),
        min_aggression_prints=int(exp.get("min_aggression_prints", 3)),
        cooldown_ms=int(exp.get("cooldown_ms", 500)),
    )

    dual_parts: list[pa.Table] = []
    nq_parts: list[pa.Table] = []
    null_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[mbo-vacuum] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        nq_mbo = _load_mbo(parquet_dir, sid, "NQ")
        es_mbo = _load_mbo(parquet_dir, sid, "ES")
        if nq.num_rows == 0 or nq_mbo.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ/mbo"})
            continue
        if rth:
            nq_rth = _filter_rth_uk_table(nq)
            es_rth = _filter_rth_uk_table(es) if es.num_rows else es
        else:
            nq_rth, es_rth = nq, es

        print(f"  NQ mbo rows={nq_mbo.num_rows} …", flush=True)
        nq_vac = mbo_vacuum_events(nq_mbo, nq, vac_cfg, rth_uk_only=rth)
        print(f"  NQ vacuum events={nq_vac.num_rows}; ES mbo…", flush=True)
        es_vac = (
            mbo_vacuum_events(es_mbo, es, vac_cfg, rth_uk_only=rth)
            if es_mbo.num_rows and es.num_rows
            else pa.table({})
        )
        print(f"  ES vacuum events={es_vac.num_rows}", flush=True)
        dual = cross_confirm_vacuums(nq_vac, es_vac, sync_ms=sync_ms)
        if dual.num_rows and max_lag > 0 and "es_lag_ms" in dual.column_names:
            lag = np.abs(dual.column("es_lag_ms").to_numpy())
            dual = dual.take(np.where(lag <= max_lag)[0])

        path_ts, path_px = build_price_path(nq_rth if nq_rth.num_rows else nq)
        lab_dual = label_events(dual, path_ts, path_px, horizons, targets)
        lab_nq = label_events(nq_vac, path_ts, path_px, horizons, targets)
        null_ev = _random_null_events(
            nq_rth if nq_rth.num_rows else nq,
            max(int(lab_dual.num_rows), 50),
            seed=hash("vac" + sid) % (2**31),
        )
        lab_null = label_events(null_ev, path_ts, path_px, horizons, targets)

        def _sid(t: pa.Table) -> pa.Table:
            if t.num_rows == 0:
                return t
            return t.append_column("session_id", pa.array([sid] * t.num_rows, type=pa.string()))

        lab_dual, lab_nq, lab_null = _sid(lab_dual), _sid(lab_nq), _sid(lab_null)
        if lab_dual.num_rows:
            dual_parts.append(lab_dual)
        if lab_nq.num_rows:
            nq_parts.append(lab_nq)
        if lab_null.num_rows:
            null_parts.append(lab_null)

        per_session.append({
            "session_id": sid,
            "events": int(lab_dual.num_rows),
            "nq_only": int(lab_nq.num_rows),
            "dual_hit10": _mean(lab_dual.column("hit_10pts")) if lab_dual.num_rows else None,
            "nq_hit10": _mean(lab_nq.column("hit_10pts")) if lab_nq.num_rows else None,
            "null_hit10": _mean(lab_null.column("hit_10pts")) if lab_null.num_rows else None,
        })
        print(
            f"  dual={lab_dual.num_rows} nq={lab_nq.num_rows} "
            f"dual_hit10={per_session[-1]['dual_hit10']}",
            flush=True,
        )

    def _pack(parts: list[pa.Table]) -> dict | None:
        if not parts:
            return None
        t = pa.concat_tables(parts)
        return {
            "n_events": int(t.num_rows),
            "p_hit_10pts": _mean(t.column("hit_10pts")),
            "p_hit_20pts": _mean(t.column("hit_20pts")),
            "mean_mfe_pts": _mean(t.column("mfe_pts")),
            "mean_mae_pts": _mean(t.column("mae_pts")),
            "mean_ret_pts_2000ms": _mean(t.column("ret_pts_2000ms")),
            "mean_ret_pts_10000ms": _mean(t.column("ret_pts_10000ms")),
            "median_ret_pts_10000ms": _median(t.column("ret_pts_10000ms")),
        }

    baselines = {
        "nq_only": _pack(nq_parts),
        "null_random": _pack(null_parts),
    }
    if not dual_parts:
        return {
            "status": "no_events",
            "sessions": per_session,
            "baselines": baselines,
            **({"events": pa.concat_tables(nq_parts)} if nq_parts else {}),
            "summary": {
                "status": "no_dual_events",
                "n_events": 0,
                "sessions": per_session,
                "baselines": baselines,
            },
        }

    df = pa.concat_tables(dual_parts)
    summary = _summarize_labeled(
        df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "baselines": baselines,
            "gates": {
                "window_ms": vac_cfg.window_ms,
                "tick_radius": vac_cfg.tick_radius,
                "min_cancels": vac_cfg.min_cancels,
                "min_cancel_ratio": vac_cfg.min_cancel_ratio,
                "sync_ms": sync_ms,
                "max_es_lag_ms": max_lag,
            },
        },
    )
    return {"summary": summary, "events": df}


def run_exp_value_depart(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0019 — S20 VPOC depart / S4 HVN break-hold port vs null / EXP-0008."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    kinds = list(exp.get("kinds") or ["s20", "s4"])
    max_lag = float(exp.get("max_es_lag_ms", 250))
    sync_ms = int(exp.get("sync_ms", 1500))
    annotate_es = bool(exp.get("annotate_es_momentum", True))
    vcfg = ValueDepartConfig(
        range_ticks=int(exp.get("range_ticks", 22)),
        tick_size=float(exp.get("tick_size", 1.0)),
        lookback_ms=int(exp.get("lookback_ms", 1_800_000)),
        near_bars_ago=int(exp.get("near_bars_ago", 5)),
        min_clearance=float(exp.get("min_clearance", 4.0)),
        near_band=float(exp.get("near_band", 8.0)),
        impulse_pct=float(exp.get("impulse_pct", 0.80)),
        require_cd_regime=bool(exp.get("require_cd_regime", True)),
        require_price_confirm=bool(exp.get("require_price_confirm", True)),
    )
    burst_kw = dict(
        min_trades=6, min_vol=25.0, min_imbalance=0.55,
        min_progress_ticks=1.0, tick_size=1.0, cooldown_bars=3, bar_ms=100)

    by_kind: dict[str, list[pa.Table]] = {k: [] for k in kinds}
    mom_parts: list[pa.Table] = []
    null_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[value-depart] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ"})
            continue
        if rth:
            nq = _filter_rth_uk_table(nq)
            if es.num_rows:
                es = _filter_rth_uk_table(es)

        feat = build_value_features(nq, vcfg)
        print(f"  range bars={feat.num_rows}", flush=True)
        path_ts, path_px = build_price_path(nq)

        # EXP-0008 control
        nq_bars = trades_to_tape_bars(nq, bar_ms=100)
        es_bars = trades_to_tape_bars(es, bar_ms=100) if es.num_rows else pa.table({})
        mom = cross_confirm_bursts(
            momentum_burst_events(nq_bars, **burst_kw),
            momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
            sync_ms=sync_ms,
        )
        if mom.num_rows and "es_lag_ms" in mom.column_names:
            mom = mom.take(np.where(np.abs(mom.column("es_lag_ms").to_numpy()) <= max_lag)[0])
        lab_mom = label_events(mom, path_ts, path_px, horizons, targets)
        if lab_mom.num_rows:
            lab_mom = lab_mom.append_column(
                "session_id", pa.array([sid] * lab_mom.num_rows, type=pa.string())
            )
            mom_parts.append(lab_mom)

        row = {"session_id": sid, "bars": int(feat.num_rows)}
        n_null = 50
        for kind in kinds:
            ev = value_depart_events(feat, vcfg, kind=kind)
            # Optional: flag ES momentum nearby (does not filter primary)
            if annotate_es and ev.num_rows and mom.num_rows:
                from research.features.cd import filter_events_near_bursts
                near = filter_events_near_bursts(ev, mom, window_ms=int(max_lag) or 2000)
                # merge flag
                near_ts = set(near.column("event_ts_ns").to_pylist()) if near.num_rows else set()
                flags = [1 if int(t) in near_ts else 0 for t in ev.column("event_ts_ns").to_pylist()]
                if "es_mom_near" in ev.column_names:
                    ev = ev.drop("es_mom_near")
                ev = ev.append_column("es_mom_near", pa.array(flags, type=pa.int8()))

            lab = label_events(ev, path_ts, path_px, horizons, targets)
            if lab.num_rows:
                lab = lab.append_column(
                    "session_id", pa.array([sid] * lab.num_rows, type=pa.string())
                )
                lab = lab.append_column(
                    "kind", pa.array([kind] * lab.num_rows, type=pa.string())
                )
                by_kind[kind].append(lab)
            row[f"{kind}_n"] = int(lab.num_rows)
            row[f"{kind}_hit10"] = _mean(lab.column("hit_10pts")) if lab.num_rows else None
            n_null = max(n_null, int(lab.num_rows))
            print(f"  {kind}: n={lab.num_rows} hit10={row[f'{kind}_hit10']}", flush=True)

        null_ev = _random_null_events(nq, n_null, seed=hash("vd" + sid) % (2**31))
        lab_null = label_events(null_ev, path_ts, path_px, horizons, targets)
        if lab_null.num_rows:
            lab_null = lab_null.append_column(
                "session_id", pa.array([sid] * lab_null.num_rows, type=pa.string())
            )
            null_parts.append(lab_null)
        row["null_hit10"] = _mean(lab_null.column("hit_10pts")) if lab_null.num_rows else None
        row["mom_hit10"] = _mean(lab_mom.column("hit_10pts")) if lab_mom.num_rows else None
        per_session.append(row)

    def _pack(parts: list[pa.Table]) -> dict | None:
        if not parts:
            return None
        t = pa.concat_tables(parts)
        out = {
            "n_events": int(t.num_rows),
            "p_hit_10pts": _mean(t.column("hit_10pts")),
            "p_hit_20pts": _mean(t.column("hit_20pts")),
            "mean_mfe_pts": _mean(t.column("mfe_pts")),
            "mean_mae_pts": _mean(t.column("mae_pts")),
            "mean_ret_pts_2000ms": _mean(t.column("ret_pts_2000ms")),
            "mean_ret_pts_10000ms": _mean(t.column("ret_pts_10000ms")),
            "median_ret_pts_10000ms": _median(t.column("ret_pts_10000ms")),
        }
        if "es_mom_near" in t.column_names:
            near = t.column("es_mom_near").to_numpy() == 1
            if near.any():
                tn = t.take(np.where(near)[0])
                out["es_mom_near"] = {
                    "n_events": int(tn.num_rows),
                    "p_hit_10pts": _mean(tn.column("hit_10pts")),
                    "mean_ret_pts_10000ms": _mean(tn.column("ret_pts_10000ms")),
                    "median_ret_pts_10000ms": _median(tn.column("ret_pts_10000ms")),
                }
        return out

    strata = {k: _pack(v) for k, v in by_kind.items()}
    baselines = {
        "null_random": _pack(null_parts),
        "exp0008_lag250": _pack(mom_parts),
    }

    # Primary = s20 if present else first kind with events
    primary_parts = by_kind.get("s20") or next((by_kind[k] for k in kinds if by_kind[k]), [])
    if not primary_parts:
        return {
            "status": "no_events",
            "sessions": per_session,
            "strata": strata,
            "baselines": baselines,
            "message": "No VPOC/HVN departure events.",
        }

    df = pa.concat_tables(primary_parts)
    # Also concat all kinds for dig
    all_parts = [p for parts in by_kind.values() for p in parts]
    all_df = pa.concat_tables(all_parts) if all_parts else df

    summary = _summarize_labeled(
        df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "strata": strata,
            "baselines": baselines,
            "primary": "s20_vpoc_depart",
            "gates": {
                "min_clearance": vcfg.min_clearance,
                "near_band": vcfg.near_band,
                "impulse_pct": vcfg.impulse_pct,
                "require_cd_regime": vcfg.require_cd_regime,
                "require_price_confirm": vcfg.require_price_confirm,
                "lookback_ms": vcfg.lookback_ms,
            },
            "source": "trade-processor S20/S4 port",
        },
    )
    return {"summary": summary, "events": df, "events_all_kinds": all_df}


def run_exp_book_imbalance(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0020 — near-book ask/bid imbalance pressure (+ magnet strata)."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    sync_ms = int(exp.get("sync_ms", 1500))
    max_lag = float(exp.get("max_es_lag_ms", 250))
    also_magnet = bool(exp.get("also_magnet", True))
    bicfg = BookImbalanceConfig(
        tick_radius=int(exp.get("tick_radius", 10)),
        sample_ms=int(exp.get("sample_ms", 100)),
        min_abs_imbalance=float(exp.get("min_abs_imbalance", 0.45)),
        min_total_size=float(exp.get("min_total_size", 20)),
        cooldown_ms=int(exp.get("cooldown_ms", 1000)),
        interpretation=str(exp.get("interpretation", "pressure")),
    )
    burst_kw = dict(
        min_trades=6, min_vol=25.0, min_imbalance=0.55,
        min_progress_ticks=1.0, tick_size=1.0, cooldown_bars=3, bar_ms=100)

    dual_parts: list[pa.Table] = []
    nq_parts: list[pa.Table] = []
    mag_parts: list[pa.Table] = []
    mom_parts: list[pa.Table] = []
    null_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[book-imbalance] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        nq_d = _load_depth(parquet_dir, sid, "NQ")
        es_d = _load_depth(parquet_dir, sid, "ES")
        if nq.num_rows == 0 or nq_d.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ depth"})
            continue
        if rth:
            nq_rth = _filter_rth_uk_table(nq)
            es_rth = _filter_rth_uk_table(es) if es.num_rows else es
        else:
            nq_rth, es_rth = nq, es

        print(f"  NQ depth={nq_d.num_rows} …", flush=True)
        nq_ev = book_imbalance_events(nq_d, bicfg, rth_uk_only=rth)
        print(f"  NQ pressure events={nq_ev.num_rows}; ES…", flush=True)
        es_ev = (
            book_imbalance_events(es_d, bicfg, rth_uk_only=rth)
            if es_d.num_rows
            else pa.table({})
        )
        dual = cross_confirm_imbalance(nq_ev, es_ev, sync_ms=sync_ms)
        if dual.num_rows and max_lag > 0 and "es_lag_ms" in dual.column_names:
            lag = np.abs(dual.column("es_lag_ms").to_numpy())
            dual = dual.take(np.where(lag <= max_lag)[0])

        path_ts, path_px = build_price_path(nq_rth if nq_rth.num_rows else nq)
        lab_dual = label_events(dual, path_ts, path_px, horizons, targets)
        lab_nq = label_events(nq_ev, path_ts, path_px, horizons, targets)

        # Magnet interpretation on same NQ depth (flip sides)
        if also_magnet:
            mcfg = BookImbalanceConfig(
                tick_radius=bicfg.tick_radius,
                sample_ms=bicfg.sample_ms,
                min_abs_imbalance=bicfg.min_abs_imbalance,
                min_total_size=bicfg.min_total_size,
                cooldown_ms=bicfg.cooldown_ms,
                interpretation="magnet",
            )
            nq_mag = book_imbalance_events(nq_d, mcfg, rth_uk_only=rth)
            es_mag = (
                book_imbalance_events(es_d, mcfg, rth_uk_only=rth)
                if es_d.num_rows
                else pa.table({})
            )
            dual_mag = cross_confirm_imbalance(nq_mag, es_mag, sync_ms=sync_ms)
            if dual_mag.num_rows and max_lag > 0 and "es_lag_ms" in dual_mag.column_names:
                lag = np.abs(dual_mag.column("es_lag_ms").to_numpy())
                dual_mag = dual_mag.take(np.where(lag <= max_lag)[0])
            lab_mag = label_events(dual_mag, path_ts, path_px, horizons, targets)
        else:
            lab_mag = pa.table({})

        # EXP-0008 control
        nq_bars = trades_to_tape_bars(nq_rth if nq_rth.num_rows else nq, bar_ms=100)
        es_bars = trades_to_tape_bars(es_rth, bar_ms=100) if es_rth.num_rows else pa.table({})
        mom = cross_confirm_bursts(
            momentum_burst_events(nq_bars, **burst_kw),
            momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
            sync_ms=1500,
        )
        if mom.num_rows and "es_lag_ms" in mom.column_names:
            mom = mom.take(np.where(np.abs(mom.column("es_lag_ms").to_numpy()) <= max_lag)[0])
        lab_mom = label_events(mom, path_ts, path_px, horizons, targets)

        null_ev = _random_null_events(
            nq_rth if nq_rth.num_rows else nq,
            max(int(lab_dual.num_rows), 50),
            seed=hash("imb" + sid) % (2**31),
        )
        lab_null = label_events(null_ev, path_ts, path_px, horizons, targets)

        def _sid(t: pa.Table) -> pa.Table:
            if t.num_rows == 0:
                return t
            return t.append_column("session_id", pa.array([sid] * t.num_rows, type=pa.string()))

        lab_dual, lab_nq, lab_mag, lab_mom, lab_null = map(
            _sid, (lab_dual, lab_nq, lab_mag, lab_mom, lab_null)
        )
        if lab_dual.num_rows:
            dual_parts.append(lab_dual)
        if lab_nq.num_rows:
            nq_parts.append(lab_nq)
        if lab_mag.num_rows:
            mag_parts.append(lab_mag)
        if lab_mom.num_rows:
            mom_parts.append(lab_mom)
        if lab_null.num_rows:
            null_parts.append(lab_null)

        row = {
            "session_id": sid,
            "events": int(lab_dual.num_rows),
            "nq_only": int(lab_nq.num_rows),
            "magnet_dual": int(lab_mag.num_rows),
            "dual_hit10": _mean(lab_dual.column("hit_10pts")) if lab_dual.num_rows else None,
            "nq_hit10": _mean(lab_nq.column("hit_10pts")) if lab_nq.num_rows else None,
            "magnet_hit10": _mean(lab_mag.column("hit_10pts")) if lab_mag.num_rows else None,
            "mom_hit10": _mean(lab_mom.column("hit_10pts")) if lab_mom.num_rows else None,
            "null_hit10": _mean(lab_null.column("hit_10pts")) if lab_null.num_rows else None,
        }
        per_session.append(row)
        print(
            f"  dual={lab_dual.num_rows} nq={lab_nq.num_rows} mag={lab_mag.num_rows} "
            f"dual_hit10={row['dual_hit10']}",
            flush=True,
        )

    def _pack(parts: list[pa.Table]) -> dict | None:
        if not parts:
            return None
        t = pa.concat_tables(parts)
        return {
            "n_events": int(t.num_rows),
            "p_hit_10pts": _mean(t.column("hit_10pts")),
            "p_hit_20pts": _mean(t.column("hit_20pts")),
            "mean_mfe_pts": _mean(t.column("mfe_pts")),
            "mean_mae_pts": _mean(t.column("mae_pts")),
            "mean_ret_pts_2000ms": _mean(t.column("ret_pts_2000ms")),
            "mean_ret_pts_10000ms": _mean(t.column("ret_pts_10000ms")),
            "median_ret_pts_10000ms": _median(t.column("ret_pts_10000ms")),
        }

    baselines = {
        "nq_only_pressure": _pack(nq_parts),
        "dual_magnet": _pack(mag_parts),
        "null_random": _pack(null_parts),
        "exp0008_lag250": _pack(mom_parts),
    }
    if not dual_parts:
        return {
            "status": "no_events",
            "sessions": per_session,
            "baselines": baselines,
            "message": "No dual book-imbalance events.",
        }

    df = pa.concat_tables(dual_parts)
    summary = _summarize_labeled(
        df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "baselines": baselines,
            "primary": "dual_pressure",
            "gates": {
                "tick_radius": bicfg.tick_radius,
                "min_abs_imbalance": bicfg.min_abs_imbalance,
                "min_total_size": bicfg.min_total_size,
                "sample_ms": bicfg.sample_ms,
                "sync_ms": sync_ms,
                "max_es_lag_ms": max_lag,
                "interpretation": bicfg.interpretation,
            },
        },
    )
    out = {"summary": summary, "events": df}
    if mag_parts:
        out["events_magnet"] = pa.concat_tables(mag_parts)
    return out


def run_exp_dual_micro_regime(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0021 — dual attract→take + opposite hold vs null / NQ / EXP-0008."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    sync_ms = int(exp.get("sync_ms", 2500))
    pair_ms = int(exp.get("pair_ms", 3000))
    max_lag = float(exp.get("max_es_lag_ms", 2500))
    require_attr = bool(exp.get("require_attracted", True))
    min_take = float(exp.get("min_take_consume", 0.5))
    min_pen = int(exp.get("min_penetration_ticks", 1))
    min_dwell = float(exp.get("min_dwell_sec", 2.0))
    max_hold_cons = float(exp.get("max_hold_consume", 0.15))
    nq_cfg = LargeLevelConfig(
        absolute_size=int(exp.get("nq_absolute_size", 40)),
        z_score=float(exp.get("z_score", 99.0)),
        tick_radius=int(exp.get("tick_radius", 10)),
        dwell_alert_sec=float(exp.get("dwell_alert_sec", 2.0)),
    )
    es_cfg = LargeLevelConfig(
        absolute_size=int(exp.get("es_absolute_size", 80)),
        z_score=float(exp.get("z_score", 99.0)),
        tick_radius=int(exp.get("tick_radius", 10)),
        dwell_alert_sec=float(exp.get("dwell_alert_sec", 2.0)),
    )
    burst_kw = dict(
        min_trades=6, min_vol=25.0, min_imbalance=0.55,
        min_progress_ticks=1.0, tick_size=1.0, cooldown_bars=3, bar_ms=100)

    dual_parts: list[pa.Table] = []
    nq_parts: list[pa.Table] = []
    take_dual_parts: list[pa.Table] = []
    hold_dual_parts: list[pa.Table] = []
    mom_parts: list[pa.Table] = []
    null_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[micro-regime] session {sid} …", flush=True)
        nq_tr = _load_trades(parquet_dir, sid, "NQ")
        es_tr = _load_trades(parquet_dir, sid, "ES")
        nq_d = _load_depth(parquet_dir, sid, "NQ")
        es_d = _load_depth(parquet_dir, sid, "ES")
        if nq_tr.num_rows == 0 or nq_d.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ"})
            continue
        nq_rth = _filter_rth_uk_table(nq_tr) if rth else nq_tr
        es_rth = _filter_rth_uk_table(es_tr) if (rth and es_tr.num_rows) else es_tr

        nq_levels = run_liquidity_tracker(nq_d, nq_tr, cfg=nq_cfg, rth_uk_only=rth)
        es_levels = (
            run_liquidity_tracker(es_d, es_tr, cfg=es_cfg, rth_uk_only=rth)
            if es_d.num_rows and es_tr.num_rows
            else []
        )

        nq_take = aggressive_take_events_from_levels(
            nq_levels,
            min_consume_ratio=min_take,
            min_penetration_ticks=min_pen,
            require_attracted=require_attr,
        )
        es_take = aggressive_take_events_from_levels(
            es_levels,
            min_consume_ratio=min_take,
            min_penetration_ticks=min_pen,
            require_attracted=require_attr,
        )
        nq_hold = hold_without_trade_events_from_levels(
            nq_levels,
            min_dwell_sec=min_dwell,
            min_initial_size=nq_cfg.absolute_size,
            max_consume_ratio=max_hold_cons,
        )
        es_hold = hold_without_trade_events_from_levels(
            es_levels,
            min_dwell_sec=min_dwell,
            min_initial_size=es_cfg.absolute_size,
            max_consume_ratio=max_hold_cons,
        )

        nq_comp = micro_regime_composite_events(nq_take, nq_hold, pair_ms=pair_ms)
        es_comp = micro_regime_composite_events(es_take, es_hold, pair_ms=pair_ms)
        dual = cross_confirm_liquidity_events(
            nq_comp, es_comp, sync_ms=sync_ms, require_same_event_type=True
        )
        if dual.num_rows and max_lag > 0 and "es_lag_ms" in dual.column_names:
            lag = np.abs(dual.column("es_lag_ms").to_numpy())
            dual = dual.take(np.where(lag <= max_lag)[0])

        take_dual = cross_confirm_liquidity_events(
            nq_take, es_take, sync_ms=sync_ms, require_same_event_type=True
        )
        hold_dual = cross_confirm_liquidity_events(
            nq_hold, es_hold, sync_ms=sync_ms, require_same_event_type=True
        )

        path_ts, path_px = build_price_path(nq_rth if nq_rth.num_rows else nq_tr)
        lab_dual = label_events(dual, path_ts, path_px, horizons, targets)
        lab_nq = label_events(nq_comp, path_ts, path_px, horizons, targets)
        lab_take = label_events(take_dual, path_ts, path_px, horizons, targets)
        lab_hold = label_events(hold_dual, path_ts, path_px, horizons, targets)

        nq_bars = trades_to_tape_bars(nq_rth if nq_rth.num_rows else nq_tr, bar_ms=100)
        es_bars = trades_to_tape_bars(es_rth, bar_ms=100) if es_rth.num_rows else pa.table({})
        mom = cross_confirm_bursts(
            momentum_burst_events(nq_bars, **burst_kw),
            momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
            sync_ms=1500,
        )
        if mom.num_rows and "es_lag_ms" in mom.column_names:
            mom = mom.take(np.where(np.abs(mom.column("es_lag_ms").to_numpy()) <= 250)[0])
        lab_mom = label_events(mom, path_ts, path_px, horizons, targets)

        null_ev = _random_null_events(
            nq_rth if nq_rth.num_rows else nq_tr,
            max(int(lab_dual.num_rows), 50),
            seed=hash("micro" + sid) % (2**31),
        )
        lab_null = label_events(null_ev, path_ts, path_px, horizons, targets)

        def _sid(t: pa.Table) -> pa.Table:
            if t.num_rows == 0:
                return t
            return t.append_column("session_id", pa.array([sid] * t.num_rows, type=pa.string()))

        lab_dual, lab_nq, lab_take, lab_hold, lab_mom, lab_null = map(
            _sid, (lab_dual, lab_nq, lab_take, lab_hold, lab_mom, lab_null)
        )
        if lab_dual.num_rows:
            dual_parts.append(lab_dual)
        if lab_nq.num_rows:
            nq_parts.append(lab_nq)
        if lab_take.num_rows:
            take_dual_parts.append(lab_take)
        if lab_hold.num_rows:
            hold_dual_parts.append(lab_hold)
        if lab_mom.num_rows:
            mom_parts.append(lab_mom)
        if lab_null.num_rows:
            null_parts.append(lab_null)

        row = {
            "session_id": sid,
            "events": int(lab_dual.num_rows),
            "nq_only": int(lab_nq.num_rows),
            "dual_take": int(lab_take.num_rows),
            "dual_hold": int(lab_hold.num_rows),
            "nq_levels": len(nq_levels),
            "es_levels": len(es_levels),
            "dual_hit10": _mean(lab_dual.column("hit_10pts")) if lab_dual.num_rows else None,
            "nq_hit10": _mean(lab_nq.column("hit_10pts")) if lab_nq.num_rows else None,
            "take_hit10": _mean(lab_take.column("hit_10pts")) if lab_take.num_rows else None,
            "hold_hit10": _mean(lab_hold.column("hit_10pts")) if lab_hold.num_rows else None,
            "mom_hit10": _mean(lab_mom.column("hit_10pts")) if lab_mom.num_rows else None,
            "null_hit10": _mean(lab_null.column("hit_10pts")) if lab_null.num_rows else None,
        }
        per_session.append(row)
        print(
            f"  dual={lab_dual.num_rows} nq={lab_nq.num_rows} "
            f"take_dual={lab_take.num_rows} hold_dual={lab_hold.num_rows} "
            f"dual_hit10={row['dual_hit10']}",
            flush=True,
        )

    def _pack(parts: list[pa.Table]) -> dict | None:
        if not parts:
            return None
        t = pa.concat_tables(parts)
        return {
            "n_events": int(t.num_rows),
            "p_hit_10pts": _mean(t.column("hit_10pts")),
            "p_hit_20pts": _mean(t.column("hit_20pts")),
            "mean_mfe_pts": _mean(t.column("mfe_pts")),
            "mean_mae_pts": _mean(t.column("mae_pts")),
            "mean_ret_pts_2000ms": _mean(t.column("ret_pts_2000ms")),
            "mean_ret_pts_10000ms": _mean(t.column("ret_pts_10000ms")),
            "median_ret_pts_10000ms": _median(t.column("ret_pts_10000ms")),
        }

    baselines = {
        "nq_only_composite": _pack(nq_parts),
        "dual_take_only": _pack(take_dual_parts),
        "dual_hold_only": _pack(hold_dual_parts),
        "null_random": _pack(null_parts),
        "exp0008_lag250": _pack(mom_parts),
    }
    if not dual_parts and not nq_parts:
        return {
            "status": "no_events",
            "sessions": per_session,
            "baselines": baselines,
            "message": "No micro-regime composite events.",
        }

    primary_parts = dual_parts if dual_parts else nq_parts
    primary_name = "dual_composite" if dual_parts else "nq_only_composite"
    df = pa.concat_tables(primary_parts)
    summary = _summarize_labeled(
        df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "baselines": baselines,
            "primary": primary_name,
            "gates": {
                "nq_absolute_size": nq_cfg.absolute_size,
                "es_absolute_size": es_cfg.absolute_size,
                "tick_radius": nq_cfg.tick_radius,
                "min_take_consume": min_take,
                "min_penetration_ticks": min_pen,
                "require_attracted": require_attr,
                "min_dwell_sec": min_dwell,
                "max_hold_consume": max_hold_cons,
                "pair_ms": pair_ms,
                "sync_ms": sync_ms,
                "max_es_lag_ms": max_lag,
            },
        },
    )
    out: dict = {"summary": summary, "events": df}
    if take_dual_parts:
        out["events_take_dual"] = pa.concat_tables(take_dual_parts)
    if hold_dual_parts:
        out["events_hold_dual"] = pa.concat_tables(hold_dual_parts)
    if dual_parts and nq_parts and primary_name == "dual_composite":
        out["events_nq_only"] = pa.concat_tables(nq_parts)
    return out


def _pack_labeled_parts(parts: list[pa.Table]) -> dict | None:
    if not parts:
        return None
    t = pa.concat_tables(parts)
    return {
        "n_events": int(t.num_rows),
        "p_hit_10pts": _mean(t.column("hit_10pts")),
        "p_hit_20pts": _mean(t.column("hit_20pts")),
        "mean_mfe_pts": _mean(t.column("mfe_pts")),
        "mean_mae_pts": _mean(t.column("mae_pts")),
        "mean_ret_pts_2000ms": _mean(t.column("ret_pts_2000ms")),
        "mean_ret_pts_10000ms": _mean(t.column("ret_pts_10000ms")),
        "median_ret_pts_10000ms": _median(t.column("ret_pts_10000ms")),
    }


def run_exp_es_leads_momentum(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0024 — ES-leads vs NQ-leads among lag<250 momentum bursts."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    bar_ms = int(exp.get("bar_ms", 100))
    sync_ms = int(exp.get("sync_ms", 1500))
    max_lag = float(exp.get("max_es_lag_ms", 250))
    sim_ms = float(exp.get("simultaneous_ms", 10))
    tick_size = float(exp.get("tick_size", 1.0))
    burst_kw = dict(
        min_trades=int(exp.get("min_trades", 6)),
        min_vol=float(exp.get("min_vol", 25)),
        min_imbalance=float(exp.get("min_imbalance", 0.55)),
        min_progress_ticks=float(exp.get("min_progress_ticks", 1.0)),
        tick_size=tick_size,
        cooldown_bars=int(exp.get("cooldown_bars", 3)),
        bar_ms=bar_ms,
    )

    all_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[es-leads] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ"})
            continue
        if rth:
            nq = _filter_rth_uk_table(nq)
            if es.num_rows:
                es = _filter_rth_uk_table(es)
        nq_bars = trades_to_tape_bars(nq, bar_ms=bar_ms)
        es_bars = trades_to_tape_bars(es, bar_ms=bar_ms) if es.num_rows else pa.table({})
        events = cross_confirm_bursts(
            momentum_burst_events(nq_bars, **burst_kw),
            momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
            sync_ms=sync_ms,
        )
        if events.num_rows and "es_lag_ms" in events.column_names:
            lag = np.abs(events.column("es_lag_ms").to_numpy())
            events = events.take(np.where(lag <= max_lag)[0])
        path_ts, path_px = build_price_path(nq)
        labeled = label_events(events, path_ts, path_px, horizons, targets)
        if labeled.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0})
            continue
        signed = labeled.column("es_signed_lag_ms").to_numpy()
        # ES leads: signed < -sim_ms (ES timestamp before NQ)
        lead = np.where(
            signed < -sim_ms, 1, np.where(signed > sim_ms, -1, 0)
        ).astype(np.int8)  # 1=ES leads, -1=NQ leads, 0=simultaneous
        if "lead_code" in labeled.column_names:
            labeled = labeled.drop("lead_code")
        labeled = labeled.append_column("lead_code", pa.array(lead, type=pa.int8()))
        labeled = labeled.append_column(
            "session_id", pa.array([sid] * labeled.num_rows, type=pa.string())
        )
        all_parts.append(labeled)
        hit = labeled.column("hit_10pts").to_numpy()
        row = {
            "session_id": sid,
            "events": int(labeled.num_rows),
            "n_es_leads": int((lead == 1).sum()),
            "n_nq_leads": int((lead == -1).sum()),
            "n_simultaneous": int((lead == 0).sum()),
            "es_leads_hit10": _mean(hit[lead == 1]) if (lead == 1).any() else None,
            "nq_leads_hit10": _mean(hit[lead == -1]) if (lead == -1).any() else None,
            "all_hit10": _mean(hit),
        }
        per_session.append(row)
        print(
            f"  n={row['events']} es_leads={row['n_es_leads']} "
            f"nq_leads={row['n_nq_leads']} es_hit10={row['es_leads_hit10']}",
            flush=True,
        )

    if not all_parts:
        return {"status": "no_events", "sessions": per_session}

    df = pa.concat_tables(all_parts)
    lead = df.column("lead_code").to_numpy()

    def _pack(mask: np.ndarray) -> dict | None:
        idx = np.where(mask)[0]
        if len(idx) == 0:
            return None
        return _pack_labeled_parts([df.take(idx)])

    strata = {
        "all_lag250": _pack(np.ones(df.num_rows, dtype=bool)),
        "es_leads": _pack(lead == 1),
        "nq_leads": _pack(lead == -1),
        "simultaneous": _pack(lead == 0),
    }
    # Per-session stability of ES-leads hit
    es_session_hits = [
        s["es_leads_hit10"] for s in per_session if s.get("es_leads_hit10") is not None
    ]
    primary = df.take(np.where(lead == 1)[0]) if (lead == 1).any() else df
    summary = _summarize_labeled(
        primary if primary.num_rows else df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "strata": strata,
            "primary": "es_leads",
            "es_leads_sessions_with_events": len(es_session_hits),
            "gates": {
                "max_es_lag_ms": max_lag,
                "simultaneous_ms": sim_ms,
                "sync_ms": sync_ms,
                **burst_kw,
            },
        },
    )
    return {"summary": summary, "events": df}


def run_exp_swing_break_momentum(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0025 — swing-break filter on lag<250 momentum."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    bar_ms = int(exp.get("bar_ms", 100))
    sync_ms = int(exp.get("sync_ms", 1500))
    max_lag = float(exp.get("max_es_lag_ms", 250))
    tick_size = float(exp.get("tick_size", 1.0))
    swing_cfg = SwingConfig(
        lookback_ms=int(exp.get("swing_lookback_ms", 180_000)),
        confirm_ms=int(exp.get("swing_confirm_ms", 20_000)),
        tick_size=tick_size,
        break_ticks=float(exp.get("break_ticks", 1.0)),
    )
    burst_kw = dict(
        min_trades=int(exp.get("min_trades", 6)),
        min_vol=float(exp.get("min_vol", 25)),
        min_imbalance=float(exp.get("min_imbalance", 0.55)),
        min_progress_ticks=float(exp.get("min_progress_ticks", 1.0)),
        tick_size=tick_size,
        cooldown_bars=int(exp.get("cooldown_bars", 3)),
        bar_ms=bar_ms,
    )

    all_parts: list[pa.Table] = []
    null_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[swing-break] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ"})
            continue
        if rth:
            nq = _filter_rth_uk_table(nq)
            if es.num_rows:
                es = _filter_rth_uk_table(es)
        nq_bars = trades_to_tape_bars(nq, bar_ms=bar_ms)
        es_bars = trades_to_tape_bars(es, bar_ms=bar_ms) if es.num_rows else pa.table({})
        events = cross_confirm_bursts(
            momentum_burst_events(nq_bars, **burst_kw),
            momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
            sync_ms=sync_ms,
        )
        if events.num_rows and "es_lag_ms" in events.column_names:
            lag = np.abs(events.column("es_lag_ms").to_numpy())
            events = events.take(np.where(lag <= max_lag)[0])
        events = annotate_burst_swing_break(events, nq, swing_cfg)
        path_ts, path_px = build_price_path(nq)
        labeled = label_events(events, path_ts, path_px, horizons, targets)
        null_ev = _random_null_events(nq, max(int(labeled.num_rows), 50), seed=hash("sw" + sid) % (2**31))
        lab_null = label_events(null_ev, path_ts, path_px, horizons, targets)
        if labeled.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0})
            continue
        labeled = labeled.append_column(
            "session_id", pa.array([sid] * labeled.num_rows, type=pa.string())
        )
        all_parts.append(labeled)
        if lab_null.num_rows:
            null_parts.append(
                lab_null.append_column(
                    "session_id", pa.array([sid] * lab_null.num_rows, type=pa.string())
                )
            )
        br = labeled.column("swing_break").to_numpy()
        hit = labeled.column("hit_10pts").to_numpy()
        row = {
            "session_id": sid,
            "events": int(labeled.num_rows),
            "n_break": int((br == 1).sum()),
            "n_non_break": int((br == 0).sum()),
            "break_hit10": _mean(hit[br == 1]) if (br == 1).any() else None,
            "non_break_hit10": _mean(hit[br == 0]) if (br == 0).any() else None,
            "all_hit10": _mean(hit),
        }
        per_session.append(row)
        print(
            f"  n={row['events']} break={row['n_break']} "
            f"break_hit10={row['break_hit10']} all={row['all_hit10']}",
            flush=True,
        )

    if not all_parts:
        return {"status": "no_events", "sessions": per_session}

    df = pa.concat_tables(all_parts)
    br = df.column("swing_break").to_numpy() == 1

    def _pack(mask: np.ndarray) -> dict | None:
        idx = np.where(mask)[0]
        if len(idx) == 0:
            return None
        return _pack_labeled_parts([df.take(idx)])

    strata = {
        "all_lag250": _pack(np.ones(df.num_rows, dtype=bool)),
        "swing_break": _pack(br),
        "non_break": _pack(~br),
        "null_random": _pack_labeled_parts(null_parts),
    }
    primary = df.take(np.where(br)[0]) if br.any() else df
    summary = _summarize_labeled(
        primary if primary.num_rows else df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "strata": strata,
            "primary": "swing_break",
            "gates": {
                "max_es_lag_ms": max_lag,
                "swing_lookback_ms": swing_cfg.lookback_ms,
                "swing_confirm_ms": swing_cfg.confirm_ms,
                "break_ticks": swing_cfg.break_ticks,
                **burst_kw,
            },
        },
    )
    return {"summary": summary, "events": df}


def run_exp_failed_swing_break(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0026 — dual failed swing-break fade vs null / NQ / EXP-0008."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    tick_size = float(exp.get("tick_size", 1.0))
    sync_ms = int(exp.get("sync_ms", 2000))
    pierce = int(exp.get("pierce_ticks", 4))
    reclaim_ms = int(exp.get("reclaim_ms", 2000))
    cooldown_ms = int(exp.get("cooldown_ms", 5000))
    swing_cfg = SwingConfig(
        lookback_ms=int(exp.get("swing_lookback_ms", 180_000)),
        confirm_ms=int(exp.get("swing_confirm_ms", 20_000)),
        tick_size=tick_size,
    )
    burst_kw = dict(
        min_trades=6, min_vol=25.0, min_imbalance=0.55,
        min_progress_ticks=1.0, tick_size=tick_size, cooldown_bars=3, bar_ms=100)

    dual_parts: list[pa.Table] = []
    nq_parts: list[pa.Table] = []
    null_parts: list[pa.Table] = []
    mom_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[failed-swing] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ"})
            continue
        if rth:
            nq = _filter_rth_uk_table(nq)
            if es.num_rows:
                es = _filter_rth_uk_table(es)

        nq_ev = failed_swing_break_events(
            nq, swing_cfg, pierce_ticks=pierce, reclaim_ms=reclaim_ms, cooldown_ms=cooldown_ms
        )
        es_ev = (
            failed_swing_break_events(
                es, swing_cfg, pierce_ticks=pierce, reclaim_ms=reclaim_ms, cooldown_ms=cooldown_ms
            )
            if es.num_rows
            else pa.table({})
        )
        dual = cross_confirm_failed_breaks(nq_ev, es_ev, sync_ms=sync_ms)
        path_ts, path_px = build_price_path(nq)
        lab_dual = label_events(dual, path_ts, path_px, horizons, targets)
        lab_nq = label_events(nq_ev, path_ts, path_px, horizons, targets)

        nq_bars = trades_to_tape_bars(nq, bar_ms=100)
        es_bars = trades_to_tape_bars(es, bar_ms=100) if es.num_rows else pa.table({})
        mom = cross_confirm_bursts(
            momentum_burst_events(nq_bars, **burst_kw),
            momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
            sync_ms=1500,
        )
        if mom.num_rows and "es_lag_ms" in mom.column_names:
            mom = mom.take(np.where(np.abs(mom.column("es_lag_ms").to_numpy()) <= 250)[0])
        lab_mom = label_events(mom, path_ts, path_px, horizons, targets)
        null_ev = _random_null_events(
            nq, max(int(lab_dual.num_rows), 50), seed=hash("fsb" + sid) % (2**31)
        )
        lab_null = label_events(null_ev, path_ts, path_px, horizons, targets)

        def _sid(t: pa.Table) -> pa.Table:
            if t.num_rows == 0:
                return t
            return t.append_column("session_id", pa.array([sid] * t.num_rows, type=pa.string()))

        lab_dual, lab_nq, lab_mom, lab_null = map(_sid, (lab_dual, lab_nq, lab_mom, lab_null))
        if lab_dual.num_rows:
            dual_parts.append(lab_dual)
        if lab_nq.num_rows:
            nq_parts.append(lab_nq)
        if lab_mom.num_rows:
            mom_parts.append(lab_mom)
        if lab_null.num_rows:
            null_parts.append(lab_null)

        row = {
            "session_id": sid,
            "events": int(lab_dual.num_rows),
            "nq_only": int(lab_nq.num_rows),
            "dual_hit10": _mean(lab_dual.column("hit_10pts")) if lab_dual.num_rows else None,
            "nq_hit10": _mean(lab_nq.column("hit_10pts")) if lab_nq.num_rows else None,
            "mom_hit10": _mean(lab_mom.column("hit_10pts")) if lab_mom.num_rows else None,
            "null_hit10": _mean(lab_null.column("hit_10pts")) if lab_null.num_rows else None,
        }
        per_session.append(row)
        print(
            f"  dual={lab_dual.num_rows} nq={lab_nq.num_rows} dual_hit10={row['dual_hit10']}",
            flush=True,
        )

    baselines = {
        "nq_only": _pack_labeled_parts(nq_parts),
        "null_random": _pack_labeled_parts(null_parts),
        "exp0008_lag250": _pack_labeled_parts(mom_parts),
    }
    dual_n = sum(p.num_rows for p in dual_parts)
    if dual_n < 15:
        return {
            "status": "empty_or_thin",
            "n_dual": dual_n,
            "sessions": per_session,
            "baselines": baselines,
            "message": f"Dual failed-break n={dual_n} < 15 — stop without loosening.",
        }
    df = pa.concat_tables(dual_parts)
    summary = _summarize_labeled(
        df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "baselines": baselines,
            "primary": "dual_failed_swing_break",
            "gates": {
                "swing_lookback_ms": swing_cfg.lookback_ms,
                "pierce_ticks": pierce,
                "reclaim_ms": reclaim_ms,
                "sync_ms": sync_ms,
            },
        },
    )
    out = {"summary": summary, "events": df}
    if nq_parts:
        out["events_nq_only"] = pa.concat_tables(nq_parts)
    return out


def run_exp_flush_into_hold(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0027 — flush into resting liquidity + hold bounce vs null / 0008."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    sync_ms = int(exp.get("sync_ms", 2000))
    tick_size = float(exp.get("tick_size", 1.0))
    require_lvn = bool(exp.get("require_lvn_in_path", False))
    nq_cfg = FlushHoldConfig(
        min_flush_ticks=float(exp.get("min_flush_ticks", 80)),
        flush_window_ms=int(exp.get("flush_window_ms", 2000)),
        absolute_size=int(exp.get("nq_absolute_size", 40)),
        z_score=float(exp.get("z_score", 99.0)),
        tick_radius=int(exp.get("tick_radius", 12)),
        min_execute_into=float(exp.get("min_execute_into", 20)),
        min_consume_ratio=float(exp.get("min_consume_ratio", 0.3)),
        max_penetration_ticks=int(exp.get("max_penetration_ticks", 6)),
        hold_ms=int(exp.get("hold_ms", 500)),
        reclaim_ticks=int(exp.get("reclaim_ticks", 2)),
        cooldown_ms=int(exp.get("cooldown_ms", 5000)),
        tick_size=tick_size,
        require_lvn_in_path=require_lvn,
    )
    es_cfg = FlushHoldConfig(
        min_flush_ticks=float(exp.get("es_min_flush_ticks", max(20, nq_cfg.min_flush_ticks * 0.4))),
        flush_window_ms=nq_cfg.flush_window_ms,
        absolute_size=int(exp.get("es_absolute_size", 80)),
        z_score=nq_cfg.z_score,
        tick_radius=nq_cfg.tick_radius,
        min_execute_into=float(exp.get("es_min_execute_into", 40)),
        min_consume_ratio=nq_cfg.min_consume_ratio,
        max_penetration_ticks=nq_cfg.max_penetration_ticks,
        hold_ms=nq_cfg.hold_ms,
        reclaim_ticks=nq_cfg.reclaim_ticks,
        cooldown_ms=nq_cfg.cooldown_ms,
        tick_size=tick_size,
        require_lvn_in_path=False,
    )
    burst_kw = dict(
        min_trades=6, min_vol=25.0, min_imbalance=0.55,
        min_progress_ticks=1.0, tick_size=tick_size, cooldown_bars=3, bar_ms=100)

    dual_parts: list[pa.Table] = []
    nq_parts: list[pa.Table] = []
    nq_lvn_parts: list[pa.Table] = []
    null_parts: list[pa.Table] = []
    mom_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[flush-hold] session {sid} …", flush=True)
        nq_tr = _load_trades(parquet_dir, sid, "NQ")
        es_tr = _load_trades(parquet_dir, sid, "ES")
        nq_d = _load_depth(parquet_dir, sid, "NQ")
        es_d = _load_depth(parquet_dir, sid, "ES")
        if nq_tr.num_rows == 0 or nq_d.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no NQ"})
            continue
        if rth:
            nq_tr = _filter_rth_uk_table(nq_tr)
            if es_tr.num_rows:
                es_tr = _filter_rth_uk_table(es_tr)

        # LVN path as optional annotate (always compute for stratum)
        nq_cfg_base = FlushHoldConfig(
            min_flush_ticks=nq_cfg.min_flush_ticks,
            flush_window_ms=nq_cfg.flush_window_ms,
            absolute_size=nq_cfg.absolute_size,
            z_score=nq_cfg.z_score,
            tick_radius=nq_cfg.tick_radius,
            min_execute_into=nq_cfg.min_execute_into,
            min_consume_ratio=nq_cfg.min_consume_ratio,
            max_penetration_ticks=nq_cfg.max_penetration_ticks,
            hold_ms=nq_cfg.hold_ms,
            reclaim_ticks=nq_cfg.reclaim_ticks,
            cooldown_ms=nq_cfg.cooldown_ms,
            tick_size=nq_cfg.tick_size,
            require_lvn_in_path=False,
        )
        nq_ev = flush_into_hold_events(nq_tr, nq_d, nq_cfg_base, rth_uk_only=rth)
        # Re-annotate LVN path
        from research.features.volume_profile import VolumeProfileConfig, causal_lvn_snapshots
        from research.features.flush_hold import _path_crossed_lvn, _trades_arrays

        if nq_ev.num_rows:
            vp = VolumeProfileConfig(
                lookback_ms=1_800_000,
                require_hvn_sandwich=False,
                min_profile_volume=200.0,
                edge_margin_ticks=4,
                lvn_frac_of_peak=0.40,
            )
            snaps = causal_lvn_snapshots(nq_tr, vp)
            ts_a, px_a, _, _ = _trades_arrays(nq_tr, tick_size)
            # Approximate flush hi/lo from level ± flush_ticks
            flags = []
            for i in range(nq_ev.num_rows):
                t0 = int(nq_ev.column("event_ts_ns")[i].as_py())
                lp = int(nq_ev.column("level_price_ticks")[i].as_py())
                ft = float(nq_ev.column("flush_ticks")[i].as_py())
                side = int(nq_ev.column("side")[i].as_py())
                if side > 0:
                    flo, fhi = lp, int(lp + ft)
                else:
                    flo, fhi = int(lp - ft), lp
                flags.append(1 if _path_crossed_lvn(snaps, fhi, flo, t0) else 0)
            if "lvn_in_path" in nq_ev.column_names:
                nq_ev = nq_ev.drop("lvn_in_path")
            nq_ev = nq_ev.append_column(
                "lvn_in_path", pa.array(flags, type=pa.int8())
            )

        es_ev = (
            flush_into_hold_events(es_tr, es_d, es_cfg, rth_uk_only=rth)
            if es_tr.num_rows and es_d.num_rows
            else pa.table({})
        )
        dual = cross_confirm_flush_hold(nq_ev, es_ev, sync_ms=sync_ms)

        path_ts, path_px = build_price_path(nq_tr)
        lab_dual = label_events(dual, path_ts, path_px, horizons, targets)
        lab_nq = label_events(nq_ev, path_ts, path_px, horizons, targets)
        lab_lvn = pa.table({})
        if lab_nq.num_rows and "lvn_in_path" in lab_nq.column_names:
            m = lab_nq.column("lvn_in_path").to_numpy() == 1
            if m.any():
                lab_lvn = lab_nq.take(np.where(m)[0])

        nq_bars = trades_to_tape_bars(nq_tr, bar_ms=100)
        es_bars = trades_to_tape_bars(es_tr, bar_ms=100) if es_tr.num_rows else pa.table({})
        mom = cross_confirm_bursts(
            momentum_burst_events(nq_bars, **burst_kw),
            momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
            sync_ms=1500,
        )
        if mom.num_rows and "es_lag_ms" in mom.column_names:
            mom = mom.take(np.where(np.abs(mom.column("es_lag_ms").to_numpy()) <= 250)[0])
        lab_mom = label_events(mom, path_ts, path_px, horizons, targets)
        null_ev = _random_null_events(
            nq_tr, max(int(lab_nq.num_rows), 50), seed=hash("fh" + sid) % (2**31)
        )
        lab_null = label_events(null_ev, path_ts, path_px, horizons, targets)

        def _sid(t: pa.Table) -> pa.Table:
            if t.num_rows == 0:
                return t
            return t.append_column("session_id", pa.array([sid] * t.num_rows, type=pa.string()))

        lab_dual, lab_nq, lab_lvn, lab_mom, lab_null = map(
            _sid, (lab_dual, lab_nq, lab_lvn, lab_mom, lab_null)
        )
        if lab_dual.num_rows:
            dual_parts.append(lab_dual)
        if lab_nq.num_rows:
            nq_parts.append(lab_nq)
        if lab_lvn.num_rows:
            nq_lvn_parts.append(lab_lvn)
        if lab_mom.num_rows:
            mom_parts.append(lab_mom)
        if lab_null.num_rows:
            null_parts.append(lab_null)

        row = {
            "session_id": sid,
            "events": int(lab_dual.num_rows),
            "nq_only": int(lab_nq.num_rows),
            "nq_lvn": int(lab_lvn.num_rows),
            "dual_hit10": _mean(lab_dual.column("hit_10pts")) if lab_dual.num_rows else None,
            "nq_hit10": _mean(lab_nq.column("hit_10pts")) if lab_nq.num_rows else None,
            "lvn_hit10": _mean(lab_lvn.column("hit_10pts")) if lab_lvn.num_rows else None,
            "mom_hit10": _mean(lab_mom.column("hit_10pts")) if lab_mom.num_rows else None,
            "null_hit10": _mean(lab_null.column("hit_10pts")) if lab_null.num_rows else None,
        }
        per_session.append(row)
        print(
            f"  dual={lab_dual.num_rows} nq={lab_nq.num_rows} lvn={lab_lvn.num_rows} "
            f"nq_hit10={row['nq_hit10']}",
            flush=True,
        )

    baselines = {
        "nq_only": _pack_labeled_parts(nq_parts),
        "nq_lvn_in_path": _pack_labeled_parts(nq_lvn_parts),
        "null_random": _pack_labeled_parts(null_parts),
        "exp0008_lag250": _pack_labeled_parts(mom_parts),
    }
    if not dual_parts and not nq_parts:
        return {
            "status": "no_events",
            "sessions": per_session,
            "baselines": baselines,
            "message": "No flush-into-hold events.",
        }

    primary_parts = dual_parts if dual_parts else nq_parts
    primary_name = "dual" if dual_parts else "nq_only"
    df = pa.concat_tables(primary_parts)
    summary = _summarize_labeled(
        df,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "baselines": baselines,
            "primary": primary_name,
            "gates": {
                "min_flush_ticks": nq_cfg.min_flush_ticks,
                "flush_window_ms": nq_cfg.flush_window_ms,
                "nq_absolute_size": nq_cfg.absolute_size,
                "hold_ms": nq_cfg.hold_ms,
                "max_penetration_ticks": nq_cfg.max_penetration_ticks,
                "sync_ms": sync_ms,
            },
        },
    )
    out: dict = {"summary": summary, "events": df}
    if dual_parts and nq_parts:
        out["events_nq_only"] = pa.concat_tables(nq_parts)
    if nq_lvn_parts:
        out["events_nq_lvn"] = pa.concat_tables(nq_lvn_parts)
    return out


def run_exp0002(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    parquet_dir = Path(cfg["parquet_dir"])
    bar_ms = int(cfg.get("feature_bar_ms", 250))
    tick_size = float(cfg.get("nq_tick_size", 0.25))
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    min_effort = float(exp.get("min_effort", 50.0))
    role = exp.get("role", "NQ")
    forbid_flat = bool(exp.get("forbid_flat_bar", False))
    require_counter = bool(exp.get("require_counter_ticks", False))
    min_counter = float(exp.get("min_counter_ticks", 1.0))
    trade_sides = str(exp.get("trade_sides", "both"))

    labeled_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        trades = _load_trades(parquet_dir, sid, role)
        if trades.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no trades"})
            continue
        if exp.get("rth_uk_only", True):
            trades = _filter_rth_uk_table(trades)
        if trades.num_rows == 0:
            per_session.append({"session_id": sid, "events": 0, "note": "no RTH trades"})
            continue

        bars = trades_table_to_bars(trades, bar_ms=bar_ms, tick_size=tick_size)
        events = effort_exhaustion_events(
            bars,
            min_effort=min_effort,
            forbid_flat_bar=forbid_flat,
            require_counter_ticks=require_counter,
            min_counter_ticks=min_counter,
            trade_sides=trade_sides,
        )
        path_ts, path_px = build_price_path(trades)
        labeled = label_events(events, path_ts, path_px, horizons, targets)
        if labeled.num_rows > 0:
            sid_col = pa.array([sid] * labeled.num_rows, type=pa.string())
            labeled = labeled.append_column("session_id", sid_col)
            labeled_parts.append(labeled)
        per_session.append({
            "session_id": sid,
            "events": int(labeled.num_rows),
            "bars": int(bars.num_rows),
            "trades": int(trades.num_rows),
        })

    if not labeled_parts:
        return {
            "status": "no_events",
            "sessions": per_session,
            "message": "No exhaustion events found (need extracted Parquet with NQ trades).",
        }

    df = pa.concat_tables(labeled_parts)
    summary = {
        "status": "ok",
        "n_events": int(df.num_rows),
        "n_sessions_with_events": int(len({x.as_py() for x in df.column("session_id")})),
        "sessions": per_session,
        "mean_mfe_pts": _mean(df.column("mfe_pts")),
        "mean_mae_pts": _mean(df.column("mae_pts")),
        "median_mfe_pts": _median(df.column("mfe_pts")),
        "median_mae_pts": _median(df.column("mae_pts")),
        "null_note": "Compare p_hit_* to ~0.5; full bootstrap in later iteration.",
        "gates": {
            "min_effort": min_effort,
            "forbid_flat_bar": forbid_flat,
            "require_counter_ticks": require_counter,
            "min_counter_ticks": min_counter,
            "trade_sides": trade_sides,
        },
    }
    for h in horizons:
        col = f"ret_pts_{h}ms"
        if col in df.column_names:
            summary[f"mean_{col}"] = _mean(df.column(col))
            summary[f"median_{col}"] = _median(df.column(col))
    for tgt in targets:
        col = f"hit_{int(tgt)}pts"
        if col in df.column_names:
            summary[f"p_{col}"] = _mean(df.column(col))

    return {"summary": summary, "events": df}


def run_exp_rotation_filter(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0030 — avg rotation length gate on momentum (fresh) and absorption (extended)."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    bar_ms = int(exp.get("bar_ms", 100))
    sync_ms = int(exp.get("sync_ms", 1500))
    max_lag = float(exp.get("max_es_lag_ms", 250))
    tick_size = float(exp.get("tick_size", 1.0))
    rot_cfg = RotationConfig(
        swing=SwingConfig(
            lookback_ms=int(exp.get("swing_lookback_ms", 180_000)),
            confirm_ms=int(exp.get("swing_confirm_ms", 20_000)),
            tick_size=tick_size,
        ),
        avg_n=int(exp.get("rot_avg_n", 8)),
        extend_mult=float(exp.get("rot_extend_mult", 1.0)),
        min_rotations=int(exp.get("rot_min_rotations", 3)),
    )
    burst_kw = dict(
        min_trades=int(exp.get("min_trades", 6)),
        min_vol=float(exp.get("min_vol", 25)),
        min_imbalance=float(exp.get("min_imbalance", 0.55)),
        min_progress_ticks=float(exp.get("min_progress_ticks", 1.0)),
        tick_size=tick_size,
        cooldown_bars=int(exp.get("cooldown_bars", 3)),
        bar_ms=bar_ms,
    )
    ll_cfg = LargeLevelConfig(
        absolute_size=int(exp.get("absolute_size", 50)),
        z_score=float(exp.get("z_score", 99.0)),
        tick_radius=int(exp.get("tick_radius", 8)),
    )
    min_consume = float(exp.get("min_consume_ratio", 1.0))

    mom_parts: list[pa.Table] = []
    abs_parts: list[pa.Table] = []
    null_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[rotation-filter] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        depth = _load_depth(parquet_dir, sid, "NQ")
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "note": "no NQ"})
            continue
        if rth:
            nq = _filter_rth_uk_table(nq)
            if es.num_rows:
                es = _filter_rth_uk_table(es)

        nq_bars = trades_to_tape_bars(nq, bar_ms=bar_ms)
        es_bars = trades_to_tape_bars(es, bar_ms=bar_ms) if es.num_rows else pa.table({})
        events = cross_confirm_bursts(
            momentum_burst_events(nq_bars, **burst_kw),
            momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
            sync_ms=sync_ms,
        )
        if events.num_rows and "es_lag_ms" in events.column_names:
            lag = np.abs(events.column("es_lag_ms").to_numpy())
            events = events.take(np.where(lag <= max_lag)[0])
        events = annotate_rotation_state(events, nq, rot_cfg)
        path_ts, path_px = build_price_path(nq)
        lab_m = label_events(events, path_ts, path_px, horizons, targets)
        if lab_m.num_rows:
            lab_m = lab_m.append_column(
                "session_id", pa.array([sid] * lab_m.num_rows, type=pa.string())
            )
            mom_parts.append(lab_m)

        lab_a = pa.table({})
        if depth.num_rows:
            levels = run_liquidity_tracker(depth, nq, cfg=ll_cfg, rth_uk_only=False)
            abs_ev = absorption_events_from_levels(
                levels,
                min_consume_ratio=min_consume,
                require_replenish=None,
                tick_radius=ll_cfg.tick_radius,
            )
            abs_ev = annotate_rotation_state(abs_ev, nq, rot_cfg)
            lab_a = label_events(abs_ev, path_ts, path_px, horizons, targets)
            if lab_a.num_rows:
                lab_a = lab_a.append_column(
                    "session_id", pa.array([sid] * lab_a.num_rows, type=pa.string())
                )
                abs_parts.append(lab_a)

        null_ev = _random_null_events(
            nq, max(int(lab_m.num_rows), 50), seed=hash("rot" + sid) % (2**31)
        )
        lab_n = label_events(null_ev, path_ts, path_px, horizons, targets)
        if lab_n.num_rows:
            null_parts.append(
                lab_n.append_column(
                    "session_id", pa.array([sid] * lab_n.num_rows, type=pa.string())
                )
            )

        ready_m = int(lab_m.column("rot_ready").to_numpy().sum()) if lab_m.num_rows else 0
        ok_m = int(momentum_rotation_ok(lab_m).sum()) if lab_m.num_rows else 0
        ok_a = int(reversal_rotation_ok(lab_a).sum()) if lab_a.num_rows else 0
        row = {
            "session_id": sid,
            "mom_n": int(lab_m.num_rows),
            "mom_ready": ready_m,
            "mom_ok": ok_m,
            "abs_n": int(lab_a.num_rows),
            "abs_ok": ok_a,
            "mom_hit10": _mean(lab_m.column("hit_10pts")) if lab_m.num_rows else None,
            "abs_hit10": _mean(lab_a.column("hit_10pts")) if lab_a.num_rows else None,
        }
        per_session.append(row)
        print(
            f"  mom={row['mom_n']} ready={ready_m} ok={ok_m} "
            f"abs={row['abs_n']} abs_ok={ok_a}",
            flush=True,
        )

    if not mom_parts:
        return {"status": "no_events", "sessions": per_session}

    mom = pa.concat_tables(mom_parts)
    abs_df = pa.concat_tables(abs_parts) if abs_parts else pa.table({})

    def _pack_mask(df: pa.Table, mask: np.ndarray) -> dict | None:
        idx = np.where(mask)[0]
        if len(idx) == 0:
            return None
        return _pack_labeled_parts([df.take(idx)])

    mom_ok = momentum_rotation_ok(mom)
    mom_ready = mom.column("rot_ready").to_numpy() == 1
    mom_fresh = mom.column("rot_fresh").to_numpy() == 1
    mom_ext = mom.column("rot_extended").to_numpy() == 1
    side = mom.column("side").to_numpy().astype(np.int8)
    leg = mom.column("rot_leg_dir").to_numpy().astype(np.int8)

    strata: dict = {
        "mom_all_lag250": _pack_labeled_parts([mom]),
        "mom_fresh_with_leg": _pack_mask(mom, mom_ok & mom_ready),
        "mom_extended_blocked": _pack_mask(mom, mom_ready & mom_ext),
        "mom_fresh_only": _pack_mask(mom, mom_ready & mom_fresh),
        "mom_against_leg": _pack_mask(mom, mom_ready & (side != leg)),
        "null_random": _pack_labeled_parts(null_parts),
    }
    if abs_df.num_rows:
        abs_ok = reversal_rotation_ok(abs_df)
        abs_ready = abs_df.column("rot_ready").to_numpy() == 1
        abs_ext = abs_df.column("rot_extended").to_numpy() == 1
        strata["abs_all"] = _pack_labeled_parts([abs_df])
        strata["abs_extended_fade"] = _pack_mask(abs_df, abs_ok)
        strata["abs_not_extended"] = _pack_mask(abs_df, abs_ready & ~abs_ext)

    primary_idx = np.where(mom_ok & mom_ready)[0]
    primary = mom.take(primary_idx) if len(primary_idx) else mom

    ratios = mom.column("rot_ratio").to_numpy()
    ready_r = ratios[mom_ready & np.isfinite(ratios)]
    ratio_stats = {
        "mom_pct_ready": float(mom_ready.mean()) if mom.num_rows else 0.0,
        "mom_pct_ok": float((mom_ok & mom_ready).mean()) if mom.num_rows else 0.0,
        "mom_mean_ratio": float(np.mean(ready_r)) if len(ready_r) else None,
        "mom_median_ratio": float(np.median(ready_r)) if len(ready_r) else None,
        "mom_mean_avg_ticks": _mean(mom.column("rot_avg_ticks")),
    }

    summary = _summarize_labeled(
        primary,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "strata": strata,
            "primary": "mom_fresh_with_leg",
            "ratio_stats": ratio_stats,
            "gates": {
                "max_es_lag_ms": max_lag,
                "rot_avg_n": rot_cfg.avg_n,
                "rot_extend_mult": rot_cfg.extend_mult,
                "rot_min_rotations": rot_cfg.min_rotations,
                "swing_lookback_ms": rot_cfg.swing.lookback_ms,
                "swing_confirm_ms": rot_cfg.swing.confirm_ms,
                **burst_kw,
            },
        },
    )
    out: dict = {"summary": summary, "events": mom}
    if abs_df.num_rows:
        out["events_absorb"] = abs_df
    return out


def run_exp_trapped_peak(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0031 — above-avg vol peak @ 50-price LVN band + quiet 20pt pullback ≤30s."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    tick_size = float(exp.get("tick_size", 1.0))
    tcfg = TrappedConfig(
        tick_size=tick_size,
        peak_window_ms=int(exp.get("peak_window_ms", 250)),
        vol_avg_lookback_ms=int(exp.get("vol_avg_lookback_ms", 60_000)),
        min_vol_mult=float(exp.get("min_vol_mult", 2.0)),
        local_extreme_ms=int(exp.get("local_extreme_ms", 20_000)),
        max_ticks_from_extreme=int(exp.get("max_ticks_from_extreme", 1)),
        lvn_band_ticks=int(exp.get("lvn_band_ticks", 50)),
        lvn_lookback_ms=int(exp.get("lvn_lookback_ms", 1_800_000)),
        lvn_frac_of_shoulder=float(exp.get("lvn_frac_of_shoulder", 0.55)),
        profile_refresh_ms=int(exp.get("profile_refresh_ms", 15_000)),
        min_profile_volume=float(exp.get("min_profile_volume", 200)),
        min_pullback_pts=float(exp.get("min_pullback_pts", 20)),
        pullback_min_ms=int(exp.get("pullback_min_ms", 5_000)),
        pullback_max_ms=int(exp.get("pullback_max_ms", 30_000)),
        reclaim_ticks=float(exp.get("reclaim_ticks", 1)),
        max_pullback_vs_peak=float(exp.get("max_pullback_vs_peak", 0.45)),
        cooldown_ms=int(exp.get("cooldown_ms", 60_000)),
    )
    burst_kw = dict(
        min_trades=6,
        min_vol=25.0,
        min_imbalance=0.55,
        min_progress_ticks=1.0,
        tick_size=tick_size,
        cooldown_bars=3, bar_ms=100)

    nq_parts: list[pa.Table] = []
    null_parts: list[pa.Table] = []
    mom_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[trapped] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "note": "no NQ"})
            continue
        if rth:
            nq = _filter_rth_uk_table(nq)
            if es.num_rows:
                es = _filter_rth_uk_table(es)

        path_ts, path_px = build_price_path(nq)
        ev = trapped_events(nq, tcfg)
        lab = label_events(ev, path_ts, path_px, horizons, targets)
        if lab.num_rows:
            lab = lab.append_column(
                "session_id", pa.array([sid] * lab.num_rows, type=pa.string())
            )
            nq_parts.append(lab)

        null_ev = _random_null_events(
            nq, max(int(lab.num_rows), 50), seed=hash("trap" + sid) % (2**31)
        )
        lab_n = label_events(null_ev, path_ts, path_px, horizons, targets)
        if lab_n.num_rows:
            null_parts.append(
                lab_n.append_column(
                    "session_id", pa.array([sid] * lab_n.num_rows, type=pa.string())
                )
            )

        nq_bars = trades_to_tape_bars(nq, bar_ms=100)
        es_bars = trades_to_tape_bars(es, bar_ms=100) if es.num_rows else pa.table({})
        mom = cross_confirm_bursts(
            momentum_burst_events(nq_bars, **burst_kw),
            momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
            sync_ms=1500,
        )
        if mom.num_rows and "es_lag_ms" in mom.column_names:
            mom = mom.take(
                np.where(np.abs(mom.column("es_lag_ms").to_numpy()) <= 250)[0]
            )
        lab_m = label_events(mom, path_ts, path_px, horizons, targets)
        if lab_m.num_rows:
            mom_parts.append(
                lab_m.append_column(
                    "session_id", pa.array([sid] * lab_m.num_rows, type=pa.string())
                )
            )

        n_short = (
            int((lab.column("side").to_numpy() == -1).sum()) if lab.num_rows else 0
        )
        row = {
            "session_id": sid,
            "events": int(lab.num_rows),
            "n_short": n_short,
            "hit10": _mean(lab.column("hit_10pts")) if lab.num_rows else None,
            "ret10s": _mean(lab.column("ret_pts_10000ms")) if lab.num_rows else None,
            "mean_vol_mult": _mean(lab.column("vol_mult")) if lab.num_rows and "vol_mult" in lab.column_names else None,
            "mean_quiet_ms": _mean(lab.column("quiet_ms")) if lab.num_rows and "quiet_ms" in lab.column_names else None,
        }
        per_session.append(row)
        print(
            f"  n={row['events']} shorts={n_short} hit10={row['hit10']} "
            f"quiet_ms={row['mean_quiet_ms']}",
            flush=True,
        )

    if not nq_parts:
        return {"status": "no_events", "sessions": per_session}

    df = pa.concat_tables(nq_parts)

    def _side_pack(side: int) -> dict | None:
        s = df.column("side").to_numpy().astype(np.int8)
        idx = np.where(s == side)[0]
        if len(idx) == 0:
            return None
        return _pack_labeled_parts([df.take(idx)])

    strata: dict = {
        "all": _pack_labeled_parts([df]),
        "shorts": _side_pack(-1),
        "longs": _side_pack(1),
        "null_random": _pack_labeled_parts(null_parts),
        "exp0008_lag250": _pack_labeled_parts(mom_parts),
    }
    primary = df
    if (df.column("side").to_numpy() == -1).any():
        primary = df.take(np.where(df.column("side").to_numpy() == -1)[0])

    summary = _summarize_labeled(
        primary,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "strata": strata,
            "primary": "shorts",
            "gates": {
                "min_vol_mult": tcfg.min_vol_mult,
                "peak_window_ms": tcfg.peak_window_ms,
                "lvn_band_ticks": tcfg.lvn_band_ticks,
                "lvn_frac_of_shoulder": tcfg.lvn_frac_of_shoulder,
                "min_pullback_pts": tcfg.min_pullback_pts,
                "pullback_max_ms": tcfg.pullback_max_ms,
                "max_pullback_vs_peak": tcfg.max_pullback_vs_peak,
            },
        },
    )
    return {"summary": summary, "events": df}


def run_exp_range_imbalance(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0032 — range-bar aggressor imbalance vs prior-range averages."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    tick_size = float(exp.get("tick_size", 1.0))
    ranges = [int(x) for x in (exp.get("range_ticks_list") or [5, 10, 22, 50])]
    modes = list(exp.get("modes") or RANGE_IMB_MODES)
    primary_range = int(exp.get("primary_range_ticks", 22))
    primary_mode = str(exp.get("primary_mode", "imb_opp_low"))
    avg_n = int(exp.get("avg_n", 20))
    min_imb = float(exp.get("min_imbalance", 0.30))
    warmup = int(exp.get("min_bars_warmup", 10))
    burst_kw = dict(
        min_trades=6,
        min_vol=25.0,
        min_imbalance=0.55,
        min_progress_ticks=1.0,
        tick_size=tick_size,
        cooldown_bars=3, bar_ms=100)

    # key -> list of labeled tables
    cell_parts: dict[str, list[pa.Table]] = {}
    primary_parts: list[pa.Table] = []
    null_parts: list[pa.Table] = []
    mom_parts: list[pa.Table] = []
    per_session = []

    for sid in session_ids:
        print(f"[range-imb] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "note": "no NQ"})
            continue
        if rth:
            nq = _filter_rth_uk_table(nq)
            if es.num_rows:
                es = _filter_rth_uk_table(es)

        path_ts, path_px = build_price_path(nq)
        sess_row: dict = {"session_id": sid}

        for rt in ranges:
            bars = trades_to_enriched_range_bars(nq, range_ticks=rt, tick_size=tick_size)
            if bars.num_rows == 0:
                continue
            bars = annotate_vs_prior(bars, avg_n=avg_n)
            ricfg = RangeImbalanceConfig(
                range_ticks=rt,
                tick_size=tick_size,
                avg_n=avg_n,
                min_imbalance=min_imb,
                min_bars_warmup=warmup,
            )
            for mode in modes:
                ev = range_imbalance_events(bars, ricfg, mode=mode)
                if ev.num_rows == 0:
                    continue
                lab = label_events(ev, path_ts, path_px, horizons, targets)
                if lab.num_rows == 0:
                    continue
                lab = lab.append_column(
                    "session_id", pa.array([sid] * lab.num_rows, type=pa.string())
                )
                key = f"r{rt}_{mode}"
                cell_parts.setdefault(key, []).append(lab)
                if rt == primary_range and mode == primary_mode:
                    primary_parts.append(lab)
                sess_row[key] = int(lab.num_rows)

        null_ev = _random_null_events(
            nq,
            max(int(sess_row.get(f"r{primary_range}_{primary_mode}", 0)), 50),
            seed=hash("rimb" + sid) % (2**31),
        )
        lab_n = label_events(null_ev, path_ts, path_px, horizons, targets)
        if lab_n.num_rows:
            null_parts.append(
                lab_n.append_column(
                    "session_id", pa.array([sid] * lab_n.num_rows, type=pa.string())
                )
            )

        nq_bars = trades_to_tape_bars(nq, bar_ms=100)
        es_bars = trades_to_tape_bars(es, bar_ms=100) if es.num_rows else pa.table({})
        mom = cross_confirm_bursts(
            momentum_burst_events(nq_bars, **burst_kw),
            momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
            sync_ms=1500,
        )
        if mom.num_rows and "es_lag_ms" in mom.column_names:
            mom = mom.take(
                np.where(np.abs(mom.column("es_lag_ms").to_numpy()) <= 250)[0]
            )
        lab_m = label_events(mom, path_ts, path_px, horizons, targets)
        if lab_m.num_rows:
            mom_parts.append(
                lab_m.append_column(
                    "session_id", pa.array([sid] * lab_m.num_rows, type=pa.string())
                )
            )

        per_session.append(sess_row)
        prim_n = sess_row.get(f"r{primary_range}_{primary_mode}", 0)
        print(f"  primary n={prim_n} cells={len([k for k in sess_row if k.startswith('r')])}", flush=True)

    if not primary_parts and not any(cell_parts.values()):
        return {"status": "no_events", "sessions": per_session}

    strata: dict = {
        "null_random": _pack_labeled_parts(null_parts),
        "exp0008_lag250": _pack_labeled_parts(mom_parts),
    }
    for key, parts in sorted(cell_parts.items()):
        strata[key] = _pack_labeled_parts(parts)

    primary = (
        pa.concat_tables(primary_parts)
        if primary_parts
        else pa.concat_tables(next(iter(cell_parts.values())))
    )
    # side packs on primary
    side = primary.column("side").to_numpy().astype(np.int8)
    strata["primary_longs"] = _pack_labeled_parts(
        [primary.take(np.where(side == 1)[0])]
    ) if (side == 1).any() else None
    strata["primary_shorts"] = _pack_labeled_parts(
        [primary.take(np.where(side == -1)[0])]
    ) if (side == -1).any() else None

    summary = _summarize_labeled(
        primary,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "strata": strata,
            "primary": f"r{primary_range}_{primary_mode}",
            "gates": {
                "range_ticks_list": ranges,
                "primary_range_ticks": primary_range,
                "primary_mode": primary_mode,
                "avg_n": avg_n,
                "min_imbalance": min_imb,
                "min_bars_warmup": warmup,
            },
        },
    )
    return {"summary": summary, "events": primary}


def run_exp_simultaneous_flow(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0033 — same-clock ES+NQ aggressor imbalance (+ dual range sync)."""
    parquet_dir = Path(cfg["parquet_dir"])
    horizons = list(exp.get("horizons_ms") or cfg.get("outcome_horizons_ms"))
    targets = list(exp.get("point_targets") or cfg.get("nq_point_targets"))
    rth = bool(exp.get("rth_uk_only", True))
    tick_size = float(exp.get("tick_size", 1.0))
    bar_list = [int(x) for x in (exp.get("bar_ms_list") or [100, 250, 500])]
    primary_bar = int(exp.get("primary_bar_ms", 100))
    min_imb = float(exp.get("min_imbalance", 0.55))
    min_vol_nq = float(exp.get("min_vol_nq", 25.0))
    min_vol_es = float(exp.get("min_vol_es", 15.0))
    min_tr_nq = int(exp.get("min_trades_nq", 6))
    min_tr_es = int(exp.get("min_trades_es", 4))
    cooldown = int(exp.get("cooldown_bars", 3))
    range_list = [int(x) for x in (exp.get("range_ticks_list") or [10, 22])]
    range_sync = int(exp.get("range_sync_ms", 250))
    range_mode = str(exp.get("range_mode", "imb_opp_low"))
    range_mi = float(exp.get("range_min_imbalance", 0.30))
    burst_kw = dict(
        min_trades=6,
        min_vol=25.0,
        min_imbalance=0.55,
        min_progress_ticks=1.0,
        tick_size=tick_size,
        cooldown_bars=3, bar_ms=100)

    cell_parts: dict[str, list[pa.Table]] = {}
    primary_parts: list[pa.Table] = []
    null_parts: list[pa.Table] = []
    mom_parts: list[pa.Table] = []
    mom_sim_parts: list[pa.Table] = []  # 0008 with |lag| < 50ms
    per_session = []

    for sid in session_ids:
        print(f"[sim-flow] session {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        if nq.num_rows == 0:
            per_session.append({"session_id": sid, "note": "no NQ"})
            continue
        if rth:
            nq = _filter_rth_uk_table(nq)
            if es.num_rows:
                es = _filter_rth_uk_table(es)
        path_ts, path_px = build_price_path(nq)
        sess_row: dict = {"session_id": sid}

        for bar_ms in bar_list:
            for prog, tag in ((0.0, "flow"), (1.0, "flow_px")):
                scfg = SimultaneousFlowConfig(
                    bar_ms=bar_ms,
                    min_imbalance=min_imb,
                    min_vol_nq=min_vol_nq,
                    min_vol_es=min_vol_es,
                    min_trades_nq=min_tr_nq,
                    min_trades_es=min_tr_es,
                    min_progress_ticks=prog,
                    tick_size=tick_size,
                    cooldown_bars=cooldown,
                )
                ev = simultaneous_flow_events(nq, es, scfg) if es.num_rows else pa.table({})
                if ev.num_rows == 0:
                    continue
                lab = label_events(ev, path_ts, path_px, horizons, targets)
                if lab.num_rows == 0:
                    continue
                lab = lab.append_column(
                    "session_id", pa.array([sid] * lab.num_rows, type=pa.string())
                )
                key = f"b{bar_ms}_{tag}"
                cell_parts.setdefault(key, []).append(lab)
                sess_row[key] = int(lab.num_rows)
                if bar_ms == primary_bar and tag == "flow":
                    primary_parts.append(lab)

        for rt in range_list:
            ev = (
                dual_range_imbalance_events(
                    nq,
                    es,
                    range_ticks=rt,
                    sync_ms=range_sync,
                    mode=range_mode,
                    min_imbalance=range_mi,
                    tick_size=tick_size,
                )
                if es.num_rows
                else pa.table({})
            )
            if ev.num_rows == 0:
                continue
            lab = label_events(ev, path_ts, path_px, horizons, targets)
            if lab.num_rows == 0:
                continue
            lab = lab.append_column(
                "session_id", pa.array([sid] * lab.num_rows, type=pa.string())
            )
            key = f"dual_r{rt}_s{range_sync}"
            cell_parts.setdefault(key, []).append(lab)
            sess_row[key] = int(lab.num_rows)

        null_ev = _random_null_events(
            nq,
            max(int(sess_row.get(f"b{primary_bar}_flow", 0)), 50),
            seed=hash("sim" + sid) % (2**31),
        )
        lab_n = label_events(null_ev, path_ts, path_px, horizons, targets)
        if lab_n.num_rows:
            null_parts.append(
                lab_n.append_column(
                    "session_id", pa.array([sid] * lab_n.num_rows, type=pa.string())
                )
            )

        nq_bars = trades_to_tape_bars(nq, bar_ms=100)
        es_bars = trades_to_tape_bars(es, bar_ms=100) if es.num_rows else pa.table({})
        mom = cross_confirm_bursts(
            momentum_burst_events(nq_bars, **burst_kw),
            momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({}),
            sync_ms=1500,
        )
        if mom.num_rows and "es_lag_ms" in mom.column_names:
            lag = np.abs(mom.column("es_lag_ms").to_numpy())
            mom250 = mom.take(np.where(lag <= 250)[0])
            mom50 = mom.take(np.where(lag <= 50)[0])
        else:
            mom250, mom50 = mom, pa.table({})
        lab_m = label_events(mom250, path_ts, path_px, horizons, targets)
        if lab_m.num_rows:
            mom_parts.append(
                lab_m.append_column(
                    "session_id", pa.array([sid] * lab_m.num_rows, type=pa.string())
                )
            )
        lab_s = label_events(mom50, path_ts, path_px, horizons, targets)
        if lab_s.num_rows:
            mom_sim_parts.append(
                lab_s.append_column(
                    "session_id", pa.array([sid] * lab_s.num_rows, type=pa.string())
                )
            )

        per_session.append(sess_row)
        print(
            f"  primary={sess_row.get(f'b{primary_bar}_flow', 0)} "
            f"cells={len([k for k in sess_row if k.startswith(('b', 'dual'))])}",
            flush=True,
        )

    if not primary_parts and not any(cell_parts.values()):
        return {"status": "no_events", "sessions": per_session}

    strata: dict = {
        "null_random": _pack_labeled_parts(null_parts),
        "exp0008_lag250": _pack_labeled_parts(mom_parts),
        "exp0008_lag50_simultaneous": _pack_labeled_parts(mom_sim_parts),
    }
    for key, parts in sorted(cell_parts.items()):
        strata[key] = _pack_labeled_parts(parts)

    primary = (
        pa.concat_tables(primary_parts)
        if primary_parts
        else pa.concat_tables(next(iter(cell_parts.values())))
    )
    summary = _summarize_labeled(
        primary,
        horizons,
        targets,
        extra={
            "sessions": per_session,
            "strata": strata,
            "primary": f"b{primary_bar}_flow",
            "gates": {
                "bar_ms_list": bar_list,
                "primary_bar_ms": primary_bar,
                "min_imbalance": min_imb,
                "min_vol_nq": min_vol_nq,
                "min_vol_es": min_vol_es,
                "range_ticks_list": range_list,
                "range_sync_ms": range_sync,
            },
        },
    )
    return {"summary": summary, "events": primary}


def _filter_rth_us_et_table(trades: pa.Table) -> pa.Table:
    """US RTH 09:30–16:00 America/New_York."""
    if trades.num_rows == 0:
        return trades
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
    except Exception:
        et = timezone(timedelta(hours=-4))  # EDT fallback
    ts = trades.column("ts_ns").to_numpy()
    keep = []
    for ts_ns in ts:
        dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).astimezone(et)
        t = dt.timetz().replace(tzinfo=None)
        keep.append(time(9, 30) <= t < time(16, 0))
    mask = np.array(keep, dtype=bool)
    if not mask.any():
        return trades.slice(0, 0)
    return trades.take(np.where(mask)[0])


def run_exp_mat_gates(cfg: dict, exp: dict, session_ids: list[str]) -> dict:
    """EXP-0034 — MAT bracket sim + day-type / trend / vol-scaled gate sweep."""
    from research.backtest.mat_sim import (
        MatSimConfig,
        combine_gates,
        filter_summary,
        gate_always,
        make_daytype_counter,
        make_daytype_skip,
        make_trend_mode,
        make_vol_bracket,
        make_vwap_side,
        merge_summaries,
        simulate_session,
    )

    parquet_dir = Path(cfg["parquet_dir"])
    results_dir = Path(cfg["results_dir"]) / exp.get("name", "EXP-0034")
    results_dir.mkdir(parents=True, exist_ok=True)

    base_cfg = MatSimConfig(
        bar_ms=int(exp.get("bar_ms", 100)),
        min_trades=int(exp.get("min_trades", 6)),
        min_vol=float(exp.get("min_vol", 25)),
        min_imbalance=float(exp.get("min_imbalance", 0.55)),
        min_progress_ticks=float(exp.get("min_progress_ticks", 1.0)),
        tick_size=float(exp.get("tick_size", 1.0)),
        cooldown_bars=int(exp.get("cooldown_bars", 3)),
        sync_ms=int(exp.get("sync_ms", 1500)),
        max_es_lag_ms=float(exp.get("max_es_lag_ms", 250)),
        signal_cooldown_sec=float(exp.get("signal_cooldown_sec", 5)),
        tp_pts=float(exp.get("tp_pts", 10)),
        sl_pts=float(exp.get("sl_pts", 10)),
        slip_ticks=float(exp.get("slip_ticks", 1)),
        pts_per_tick=float(exp.get("pts_per_tick", 0.25)),
    )
    regime_w = int(exp.get("regime_window_min", 60))
    trend_w = int(exp.get("trend_window_min", 30))
    vol_w = int(exp.get("vol_window_min", 30))
    train_set = set(exp.get("train_sessions") or [])
    holdout_set = set(exp.get("holdout_sessions") or [])
    bad_set = set(exp.get("bad_sessions") or [])

    # Preload windowed trades once
    cache: dict[str, tuple[pa.Table, pa.Table]] = {}
    for sid in session_ids:
        print(f"[mat-gates] load {sid} …", flush=True)
        nq = _load_trades(parquet_dir, sid, "NQ")
        es = _load_trades(parquet_dir, sid, "ES")
        if nq.num_rows == 0:
            continue
        nq = _filter_rth_us_et_table(nq)
        es = _filter_rth_us_et_table(es) if es.num_rows else es
        if nq.num_rows == 0:
            continue
        cache[sid] = (nq, es)

    def run_variant(name: str, gate, bracket_fn=None, cfg_override=None) -> dict:
        cfg_use = cfg_override or base_cfg
        parts = []
        for sid, (nq, es) in cache.items():
            s = simulate_session(
                sid, nq, es, cfg_use,
                gate=gate,
                regime_window_min=regime_w,
                trend_window_min=trend_w,
                vol_window_min=vol_w,
                bracket_fn=bracket_fn,
            )
            parts.append(s)
        all_s = merge_summaries(parts)
        train = filter_summary(all_s, lambda t: t.session_id in train_set)
        hold = filter_summary(all_s, lambda t: t.session_id in holdout_set)
        bad = filter_summary(all_s, lambda t: t.session_id in bad_set)
        good = filter_summary(all_s, lambda t: t.session_id not in bad_set)
        per_sid = {
            sid: filter_summary(all_s, lambda t, s=sid: t.session_id == s).to_dict()
            for sid in cache
        }
        return {
            "name": name,
            "all": all_s.to_dict(),
            "train": train.to_dict(),
            "holdout": hold.to_dict(),
            "bad4": bad.to_dict(),
            "good10": good.to_dict(),
            "per_session": per_sid,
        }

    variants: list[dict] = []

    # Baseline fixed ±10 / ±20
    print("[mat-gates] baseline ±10 …", flush=True)
    variants.append(run_variant("baseline_tp10", gate_always))
    cfg20 = MatSimConfig(**{**base_cfg.__dict__, "tp_pts": 20.0, "sl_pts": 20.0})
    variants.append(run_variant("baseline_tp20", gate_always, cfg_override=cfg20))

    # Day-type skip
    for R in (60, 80, 100, 120):
        print(f"[mat-gates] daytype skip range>{R} …", flush=True)
        variants.append(run_variant(
            f"daytype_skip_range{R}",
            make_daytype_skip(max_range=float(R)),
        ))
    for E in (0.10, 0.15, 0.20):
        print(f"[mat-gates] daytype skip er>{E} …", flush=True)
        variants.append(run_variant(
            f"daytype_skip_er{E:.2f}",
            make_daytype_skip(max_er=E),
        ))

    # Day-type counter-when-hot
    for R in (80, 100, 120):
        print(f"[mat-gates] daytype counter-when range>{R} …", flush=True)
        variants.append(run_variant(
            f"daytype_counter_range{R}",
            make_daytype_counter(max_range=float(R)),
        ))
    for E in (0.10, 0.15):
        print(f"[mat-gates] daytype counter-when er>{E} …", flush=True)
        variants.append(run_variant(
            f"daytype_counter_er{E:.2f}",
            make_daytype_counter(max_er=E),
        ))

    # Trend modes (always on)
    for mode in ("with", "against"):
        print(f"[mat-gates] trend {mode} …", flush=True)
        variants.append(run_variant(f"trend_{mode}", make_trend_mode(mode)))

    # VWAP side
    print("[mat-gates] vwap side …", flush=True)
    variants.append(run_variant("vwap_side", make_vwap_side(True)))

    # Combined: skip range100 + against when cool? and counter-hot + against
    print("[mat-gates] combos …", flush=True)
    variants.append(run_variant(
        "skip_range100_and_against",
        combine_gates(make_daytype_skip(max_range=100), make_trend_mode("against")),
    ))
    variants.append(run_variant(
        "counter_range100",
        make_daytype_counter(max_range=100),
    ))
    variants.append(run_variant(
        "against_and_vwap",
        combine_gates(make_trend_mode("against"), make_vwap_side(True)),
    ))

    # Vol-scaled brackets (no gate)
    for k in (2, 3, 4, 6):
        print(f"[mat-gates] vol bracket k={k} …", flush=True)
        variants.append(run_variant(
            f"vol_k{k}",
            gate_always,
            bracket_fn=make_vol_bracket(float(k)),
        ))

    # Best-looking daytype + vol (range100 skip + k=3)
    variants.append(run_variant(
        "skip_range100_vol_k3",
        make_daytype_skip(max_range=100),
        bracket_fn=make_vol_bracket(3.0),
    ))
    variants.append(run_variant(
        "counter_range100_vol_k3",
        make_daytype_counter(max_range=100),
        bracket_fn=make_vol_bracket(3.0),
    ))

    baseline = next(v for v in variants if v["name"] == "baseline_tp10")

    def lift(v: dict, split: str) -> float | None:
        b = baseline[split].get("net")
        n = v[split].get("net")
        if b is None or n is None:
            return None
        return round(n - b, 2)

    recommended = []
    for v in variants:
        if v["name"] == "baseline_tp10":
            continue
        lt, lh = lift(v, "train"), lift(v, "holdout")
        # same-sign lift on train AND holdout, and holdout n >= 20
        if lt is None or lh is None:
            continue
        if lt > 0 and lh > 0 and (v["holdout"].get("n") or 0) >= 20:
            recommended.append({
                "name": v["name"],
                "train_lift": lt,
                "holdout_lift": lh,
                "train": v["train"],
                "holdout": v["holdout"],
                "bad4": v["bad4"],
                "good10": v["good10"],
            })
    recommended.sort(key=lambda r: r["holdout_lift"], reverse=True)

    # summary.md
    lines = [
        "# EXP-0034 — MAT gates (bar-end corrected)",
        "",
        f"Sessions: {len(cache)} (US RTH). Slippage: {base_cfg.slip_ticks} tick/side.",
        f"Train={sorted(train_set)} Holdout={sorted(holdout_set)} Bad4={sorted(bad_set)}",
        "",
        "## Baseline (tp=sl=10 pts)",
        f"- all: {baseline['all']}",
        f"- train: {baseline['train']}",
        f"- holdout: {baseline['holdout']}",
        f"- bad4: {baseline['bad4']}",
        f"- good10: {baseline['good10']}",
        "",
        "## Recommended (same-sign lift train AND holdout, holdout n≥20)",
        "",
    ]
    if not recommended:
        lines.append("_No variant cleared the success bar._")
    else:
        for r in recommended:
            lines.append(
                f"- **{r['name']}**: train_lift={r['train_lift']} holdout_lift={r['holdout_lift']} "
                f"| train {r['train']} | holdout {r['holdout']} | bad4 {r['bad4']}"
            )
    lines.extend(["", "## All variants", ""])
    lines.append("| variant | train net | holdout net | bad4 net | good10 net | train n | hold n |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for v in variants:
        lines.append(
            f"| {v['name']} | {v['train'].get('net')} | {v['holdout'].get('net')} | "
            f"{v['bad4'].get('net')} | {v['good10'].get('net')} | "
            f"{v['train'].get('n')} | {v['holdout'].get('n')} |"
        )
    summary_md = "\n".join(lines) + "\n"
    (results_dir / "summary.md").write_text(summary_md, encoding="utf-8")

    report = {
        "status": "ok",
        "baseline": baseline,
        "variants": [{k: v[k] for k in ("name", "all", "train", "holdout", "bad4", "good10")} for v in variants],
        "recommended": recommended,
        "sessions_used": sorted(cache.keys()),
        "gates": {
            "bar_ms": base_cfg.bar_ms,
            "sync_ms": base_cfg.sync_ms,
            "max_es_lag_ms": base_cfg.max_es_lag_ms,
            "slip_ticks": base_cfg.slip_ticks,
            "causal_only": True,
            "event_ts": "bar_end",
        },
        "note": "Bar-end entry + causal ES; older EXP-0008 hit rates used bar-start look-ahead.",
    }
    return {"summary": report}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("experiment", help="e.g. EXP-0002")
    parser.add_argument("--sessions", choices=["all", "smoke"], default="all")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config) if args.config else None)
    exp_name = args.experiment.replace(".yaml", "")
    exp = _load_exp(exp_name)

    parquet_dir = Path(cfg["parquet_dir"])
    results_dir = Path(cfg["results_dir"]) / exp_name
    results_dir.mkdir(parents=True, exist_ok=True)

    session_ids = _list_sessions(parquet_dir, args.sessions, cfg.get("smoke_sessions") or [])

    if not session_ids:
        report = {
            "experiment": exp_name,
            "status": "waiting_for_data",
            "message": (
                "No Parquet sessions found. Extract .bmf with Session Extractor into "
                f"{cfg['raw_dir']}, then run: python -m research.extract.to_parquet"
            ),
            "raw_dir": cfg["raw_dir"],
            "parquet_dir": cfg["parquet_dir"],
        }
        (results_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0

    if exp_name.startswith("EXP-0002") or exp.get("detector") == "effort_exhaustion":
        result = run_exp0002(cfg, exp, session_ids)
    elif exp.get("detector") == "absorption" or exp_name in ("EXP-0005", "EXP-0006"):
        result = run_exp_absorption(cfg, exp, session_ids)
    elif exp.get("detector") == "es_nq_momentum" or exp_name == "EXP-0008":
        result = run_exp_momentum(cfg, exp, session_ids)
    elif exp.get("detector") == "es_nq_depth_momentum" or exp_name in ("EXP-0008b", "EXP-0008c"):
        result = run_exp_depth_momentum(cfg, exp, session_ids)
    elif exp.get("detector") == "dual_lvn_touch" or exp_name in ("EXP-0011", "EXP-0023"):
        result = run_exp_dual_lvn(cfg, exp, session_ids)
    elif exp.get("detector") == "dual_lvn_flip" or exp_name == "EXP-0012":
        result = run_exp_lvn_flip(cfg, exp, session_ids)
    elif exp.get("detector") == "cd_pullback_momentum" or exp_name == "EXP-0014":
        result = run_exp_cd_pullback(cfg, exp, session_ids)
    elif exp.get("detector") == "dual_dwell_withdraw" or exp_name == "EXP-0013":
        result = run_exp_dwell_withdraw(cfg, exp, session_ids)
    elif exp.get("detector") == "momentum_bb_regime" or exp_name == "EXP-0015":
        result = run_exp_momentum_bb_regime(cfg, exp, session_ids)
    elif exp.get("detector") == "dual_cd_filter_momentum" or exp_name == "EXP-0022":
        result = run_exp_dual_cd_filter(cfg, exp, session_ids)
    elif exp.get("detector") == "dual_challenge_wo_trade" or exp_name == "EXP-0016":
        result = run_exp_challenge_wo_trade(cfg, exp, session_ids)
    elif exp.get("detector") == "aggressor_sweep" or exp_name == "EXP-0017":
        result = run_exp_aggressor_sweep(cfg, exp, session_ids)
    elif exp.get("detector") == "mbo_vacuum" or exp_name == "EXP-0018":
        result = run_exp_mbo_vacuum(cfg, exp, session_ids)
    elif exp.get("detector") == "value_depart" or exp_name == "EXP-0019":
        result = run_exp_value_depart(cfg, exp, session_ids)
    elif exp.get("detector") == "book_imbalance" or exp_name == "EXP-0020":
        result = run_exp_book_imbalance(cfg, exp, session_ids)
    elif exp.get("detector") == "dual_micro_regime" or exp_name == "EXP-0021":
        result = run_exp_dual_micro_regime(cfg, exp, session_ids)
    elif exp.get("detector") == "es_leads_momentum" or exp_name == "EXP-0024":
        result = run_exp_es_leads_momentum(cfg, exp, session_ids)
    elif exp.get("detector") == "swing_break_momentum" or exp_name == "EXP-0025":
        result = run_exp_swing_break_momentum(cfg, exp, session_ids)
    elif exp.get("detector") == "dual_failed_swing_break" or exp_name == "EXP-0026":
        result = run_exp_failed_swing_break(cfg, exp, session_ids)
    elif exp.get("detector") == "flush_into_hold" or exp_name == "EXP-0027":
        result = run_exp_flush_into_hold(cfg, exp, session_ids)
    elif exp.get("detector") == "rotation_filter" or exp_name == "EXP-0030":
        result = run_exp_rotation_filter(cfg, exp, session_ids)
    elif exp.get("detector") == "trapped_peak" or exp_name == "EXP-0031":
        result = run_exp_trapped_peak(cfg, exp, session_ids)
    elif exp.get("detector") == "range_imbalance" or exp_name == "EXP-0032":
        result = run_exp_range_imbalance(cfg, exp, session_ids)
    elif exp.get("detector") == "simultaneous_flow" or exp_name == "EXP-0033":
        result = run_exp_simultaneous_flow(cfg, exp, session_ids)
    elif exp.get("detector") == "mat_gates" or exp_name == "EXP-0034":
        result = run_exp_mat_gates(cfg, exp, session_ids)
    else:
        print(f"Unknown experiment detector for {exp_name}", file=sys.stderr)
        return 2

    if "events" in result and isinstance(result["events"], pa.Table) and result["events"].num_rows > 0:
        events_path = results_dir / "events.parquet"
        pq.write_table(result["events"], events_path, compression="zstd")
        summary = result["summary"]
        summary["events_path"] = str(events_path)
    else:
        summary = result if "summary" not in result else result["summary"]
        if "status" not in summary and "status" in result:
            summary = result

    if "events_all_dual" in result and isinstance(result["events_all_dual"], pa.Table):
        all_path = results_dir / "events_all_dual.parquet"
        pq.write_table(result["events_all_dual"], all_path, compression="zstd")
        if isinstance(summary, dict):
            summary["events_all_dual_path"] = str(all_path)

    for extra_key, fname in (
        ("events_pullback", "events_pullback.parquet"),
        ("events_confirmed", "events_confirmed.parquet"),
        ("events_all_kinds", "events_all_kinds.parquet"),
        ("events_magnet", "events_magnet.parquet"),
        ("events_take_dual", "events_take_dual.parquet"),
        ("events_hold_dual", "events_hold_dual.parquet"),
        ("events_nq_only", "events_nq_only.parquet"),
        ("events_nq_lvn", "events_nq_lvn.parquet"),
        ("events_absorb", "events_absorb.parquet"),
    ):
        if extra_key in result and isinstance(result[extra_key], pa.Table) and result[extra_key].num_rows:
            p = results_dir / fname
            pq.write_table(result[extra_key], p, compression="zstd")
            if isinstance(summary, dict):
                summary[f"{extra_key}_path"] = str(p)

    if isinstance(summary, dict):
        summary["experiment"] = exp_name
        summary["title"] = exp.get("title")
        (results_dir / "report.json").write_text(
            json.dumps(summary, indent=2, default=str), encoding="utf-8"
        )
        print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
