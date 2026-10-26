"""Shared MAT trade cache with attached context features (EXP-0067+).

One session load → one frozen MAT sim → attach rotation / ATR / CD / VWAP /
range-bar columns. Downstream EXPs filter or re-sim from this cache.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyarrow as pa
import yaml

from research.backtest.mat_sim import (
    SimSummary,
    TradeResult,
    detect_signals,
    merge_summaries,
    simulate_session,
)
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.features.atr import (
    AtrConfig,
    annotate_atr,
    filter_uk_window,
    prior_atr,
    session_range_pts,
    session_uk_bounds,
)
from research.features.cd import build_cd_series, lookup_cd_at_events
from research.features.range_bar_profile import (
    RangeBarProfileConfig,
    attach_mat_range_context,
)
from research.features.session_rotation import (
    PriorSessionRotationStats,
    SessionRotationConfig,
    annotate_session_rotation,
    prior_session_stats,
    session_rotations_for_trades,
)
from research.run_cfade_all_months import _sid_chrono_key


def load_protocol() -> dict:
    path = Path(__file__).resolve().parents[1] / "experiments" / "MAT_CONTEXT_PROTOCOL.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class SessionBundle:
    sid: str
    nq: pa.Table
    es: pa.Table
    vwap_from_ns: int
    events: pa.Table  # MAT signals with attached features
    sim: SimSummary  # base frozen MAT trades
    prior_rot: PriorSessionRotationStats
    session_range: float
    prior_atr_pts: float


@dataclass
class MatTradeCache:
    sessions: list[str]
    bundles: dict[str, SessionBundle] = field(default_factory=dict)
    session_ranges: dict[str, float] = field(default_factory=dict)
    protocol: dict = field(default_factory=dict)

    def base_trades(self) -> list[TradeResult]:
        out: list[TradeResult] = []
        for sid in self.sessions:
            b = self.bundles.get(sid)
            if b is not None:
                out.extend(b.sim.trades)
        return out

    def base_summary(self) -> SimSummary:
        parts = [self.bundles[s].sim for s in self.sessions if s in self.bundles]
        return merge_summaries(parts, F.COMMISSION) if parts else SimSummary()


def _events_from_trades(trades: list[TradeResult]) -> pa.Table:
    if not trades:
        return pa.table({})
    return pa.table({
        "event_ts_ns": np.asarray([t.entry_ts_ns for t in trades], dtype=np.int64),
        "side": np.asarray([t.side for t in trades], dtype=np.int8),
        "pnl": np.asarray([t.pnl for t in trades], dtype=np.float64),
        "tp_pts": np.asarray([t.tp_pts for t in trades], dtype=np.float64),
        "sl_pts": np.asarray([t.sl_pts for t in trades], dtype=np.float64),
        "er60": np.asarray([t.er60 for t in trades], dtype=np.float64),
        "range60": np.asarray([t.range60 for t in trades], dtype=np.float64),
        "vwap": np.asarray([t.vwap for t in trades], dtype=np.float64),
        "med_r30": np.asarray([t.med_r30 for t in trades], dtype=np.float64),
        "entry_px": np.asarray([t.entry_px for t in trades], dtype=np.float64),
        "exit_px": np.asarray([t.exit_px for t in trades], dtype=np.float64),
        "reason": pa.array([t.reason for t in trades], type=pa.string()),
        "win": np.asarray([1 if t.win else 0 for t in trades], dtype=np.int8),
    })


def _attach_vwap_dist(ev: pa.Table) -> pa.Table:
    if ev.num_rows == 0:
        return ev
    side = ev.column("side").to_numpy().astype(int)
    entry = ev.column("entry_px").to_numpy().astype(np.float64)
    vwap = ev.column("vwap").to_numpy().astype(np.float64)
    medr = ev.column("med_r30").to_numpy().astype(np.float64)
    dist = entry - vwap
    aligned = dist * np.where(side > 0, 1.0, -1.0)
    abs_norm = np.where(
        np.isfinite(dist) & np.isfinite(medr) & (medr > 0),
        np.abs(dist) / medr,
        np.nan,
    )
    out = ev
    for name, arr in (
        ("vwap_dist", dist),
        ("vwap_dist_aligned", aligned),
        ("vwap_dist_norm", abs_norm),
    ):
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=pa.float64()))
    return out


def _attach_cd(ev: pa.Table, nq: pa.Table) -> pa.Table:
    if ev.num_rows == 0:
        return ev
    cd = build_cd_series(nq, sma_min_bars=100)
    # Also build legacy expanding SMA for comparator
    cd_legacy = build_cd_series(nq, sma_min_bars=max(10, 100 // 4))
    ts = ev.column("event_ts_ns").to_numpy().astype(np.int64)
    fields = lookup_cd_at_events(cd, ts) if cd.num_rows else {}
    leg = lookup_cd_at_events(cd_legacy, ts) if cd_legacy.num_rows else {}
    out = ev
    for name in (
        "cd_dist_sigma", "cd_ma_trend", "cd_near_ma",
        "cd_pullback_long", "cd_pullback_short",
        "cd_tight_pb_long", "cd_tight_pb_short", "cd_cheap", "cd_sma_ready",
    ):
        arr = fields.get(name, np.full(len(ts), np.nan))
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=pa.float64()))
    # Legacy comparator flags
    for name, key in (
        ("cd_legacy_pb_long", "cd_pullback_long"),
        ("cd_legacy_pb_short", "cd_pullback_short"),
        ("cd_legacy_near_ma", "cd_near_ma"),
    ):
        arr = leg.get(key, np.full(len(ts), np.nan))
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=pa.float64()))
    return out


def build_cache(
    *,
    parquet_dir: Path | None = None,
    sessions: list[str] | None = None,
    verbose: bool = True,
) -> MatTradeCache:
    """Build the shared MAT + features cache over clean sessions."""
    cfg = load_config()
    pq = Path(parquet_dir or cfg["parquet_dir"])
    proto = load_protocol()
    all_sessions = sessions or F.clean_sessions(pq)
    chrono = sorted(all_sessions, key=_sid_chrono_key)

    # Pass 1: session ranges for prior ATR + prior rotation stats
    session_ranges: dict[str, float] = {}
    prior_rot_by_sid: dict[str, PriorSessionRotationStats] = {}
    rot_cfg = SessionRotationConfig()
    atr_cfg = AtrConfig(lookback_sessions=int(proto.get("atr", {}).get("lookback_sessions", 10)))

    for sid in chrono:
        pair = H.load_pair(pq, sid)
        if pair is None:
            continue
        nq_uk = filter_uk_window(pair.nq, atr_cfg)
        session_ranges[sid] = session_range_pts(nq_uk)
        rots = session_rotations_for_trades(nq_uk, cfg=rot_cfg)
        prior_rot_by_sid[sid] = prior_session_stats(rots, avg_n=rot_cfg.avg_n)
        if verbose:
            print(f"pass1 {sid} range={session_ranges[sid]:.1f}", flush=True)

    cache = MatTradeCache(sessions=[], bundles={}, session_ranges=session_ranges, protocol=proto)

    for sid in chrono:
        pair = H.load_pair(pq, sid)
        if pair is None:
            continue
        # Causal prior rotation = immediately preceding clean session
        try:
            idx = chrono.index(sid)
        except ValueError:
            idx = -1
        prior = PriorSessionRotationStats()
        for prev in chrono[:idx][::-1]:
            if prev in prior_rot_by_sid:
                prior = prior_rot_by_sid[prev]
                break
        prior_atr_pts = prior_atr(session_ranges, sid, chrono, lookback=atr_cfg.lookback_sessions)

        sim = F.simulate_mat(pair)
        # Also keep raw MAT signal events (pre-gate) for re-sim with filters
        raw = detect_signals(pair.nq, pair.es, F.mat_sim_cfg())
        # Attach features to *executed* trades for strata; also annotate raw for gates
        trade_ev = _events_from_trades(sim.trades)
        trade_ev = _attach_vwap_dist(trade_ev)
        trade_ev = _attach_cd(trade_ev, pair.nq)

        nq_uk = filter_uk_window(pair.nq, atr_cfg)
        open_ns, _ = session_uk_bounds(
            int(pair.nq.column("ts_ns").to_numpy()[pair.nq.num_rows // 2])
            if pair.nq.num_rows else 0,
            atr_cfg,
        ) if pair.nq.num_rows else (None, None)

        if trade_ev.num_rows:
            trade_ev = annotate_session_rotation(trade_ev, nq_uk, cfg=rot_cfg, prior=prior)
            trade_ev = annotate_atr(
                trade_ev, nq_uk, prior_atr_pts=prior_atr_pts, session_open_ns=open_ns
            )
            trade_ev, bars, profile = attach_mat_range_context(pair.nq, trade_ev)
        else:
            bars = profile = pa.table({})

        # Annotate raw signals the same way for gate re-sims
        raw_ann = raw
        if raw.num_rows:
            # stub entry_px from last trade at event for VWAP dist
            ts = pair.nq.column("ts_ns").to_numpy().astype(np.int64)
            px = pair.nq.column("price").to_numpy().astype(np.float64) * 0.25
            order = np.argsort(ts)
            ts, px = ts[order], px[order]
            ev_ts = raw.column("event_ts_ns").to_numpy().astype(np.int64)
            entry_px = np.full(len(ev_ts), np.nan)
            for i, t0 in enumerate(ev_ts):
                j = int(np.searchsorted(ts, t0, side="left"))
                if j < len(ts):
                    entry_px[i] = float(px[j])
            # regime at events already available via simulate; compute VWAP via annotate path
            from research.features.regime import build_regime_index, lookup_regime
            bars_r, bar_ts, cum_pv, cum_v, t_ts, t_px, t_sz = build_regime_index(pair.nq)
            s60 = lookup_regime(
                bars_r, bar_ts, cum_pv, cum_v, ev_ts, window_min=60,
                vwap_from_ns=pair.vwap_from_ns, trade_ts=t_ts, trade_px=t_px, trade_sz=t_sz,
            )
            s30 = lookup_regime(
                bars_r, bar_ts, cum_pv, cum_v, ev_ts, window_min=30,
                vwap_from_ns=pair.vwap_from_ns, trade_ts=t_ts, trade_px=t_px, trade_sz=t_sz,
            )
            raw_ann = raw
            for name, arr in (
                ("entry_px", entry_px),
                ("vwap", np.array([s.vwap for s in s60])),
                ("med_r30", np.array([s.median_minute_range for s in s30])),
                ("er60", np.array([s.efficiency_ratio for s in s60])),
                ("range60", np.array([s.range_pts for s in s60])),
            ):
                if name in raw_ann.column_names:
                    raw_ann = raw_ann.drop(name)
                raw_ann = raw_ann.append_column(name, pa.array(arr, type=pa.float64()))
            raw_ann = _attach_vwap_dist(raw_ann)
            raw_ann = _attach_cd(raw_ann, pair.nq)
            raw_ann = annotate_session_rotation(raw_ann, nq_uk, cfg=rot_cfg, prior=prior)
            raw_ann = annotate_atr(
                raw_ann, nq_uk, prior_atr_pts=prior_atr_pts, session_open_ns=open_ns
            )
            raw_ann, bars, profile = attach_mat_range_context(pair.nq, raw_ann)

        # Store both: trade_ev for strata, events=raw_ann for gate re-sim
        # Pack raw_ann into the bundle.events field; trade table kept via sim.trades
        # and a side column trade_features on the bundle via events metadata.
        # Simpler: store annotated raw as events; attach trade features as a second table.
        bundle = SessionBundle(
            sid=sid,
            nq=pair.nq,
            es=pair.es,
            vwap_from_ns=pair.vwap_from_ns,
            events=raw_ann,
            sim=sim,
            prior_rot=prior,
            session_range=session_ranges.get(sid, float("nan")),
            prior_atr_pts=prior_atr_pts,
        )
        # Stash executed-trade feature table on the events schema via a parallel attr
        bundle.trade_features = trade_ev  # type: ignore[attr-defined]
        bundle.bars = bars  # type: ignore[attr-defined]
        bundle.profile = profile  # type: ignore[attr-defined]

        cache.sessions.append(sid)
        cache.bundles[sid] = bundle
        if verbose:
            print(
                f"pass2 {sid} mat_n={sim.n} net={sim.net:+.1f} "
                f"prior_atr={prior_atr_pts:.1f} raw={raw.num_rows}",
                flush=True,
            )

    return cache
