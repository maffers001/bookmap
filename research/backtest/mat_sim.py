"""MAT-semantics simulator: bar-end entry, causal ES confirm, flat-only brackets.

Matches Momentum AutoTrade (and v2) as closely as offline trades allow:
  - NQ 100 ms burst (EXP-0008 params), causal ES same-side within sync_ms,
    lag <= max_es_lag_ms
  - Entry = first NQ trade at/after burst bar end
  - Flat-only, signal cooldown
  - Symmetric TP/SL in NQ chart points; if a trade path touches both, stop wins
  - Optional 1-tick (0.25 pt) slippage per side
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pyarrow as pa

from research.features.momentum import (
    cross_confirm_bursts,
    momentum_burst_events,
    trades_to_tape_bars,
)
from research.features.regime import RegimeSnapshot, build_regime_index, lookup_regime


TICK_PTS = 0.25  # NQ tick in chart points; parquet price is tick units


@dataclass
class MatSimConfig:
    bar_ms: int = 100
    min_trades: int = 6
    min_vol: float = 25.0
    min_imbalance: float = 0.55
    min_progress_ticks: float = 1.0
    tick_size: float = 1.0  # parquet price units
    cooldown_bars: int = 3
    sync_ms: int = 1500
    max_es_lag_ms: float = 250.0
    signal_cooldown_sec: float = 5.0
    tp_pts: float = 10.0
    sl_pts: float = 10.0
    slip_ticks: float = 0.0  # per side
    flat_only: bool = True
    pts_per_tick: float = TICK_PTS
    # "causal" = ES at/before NQ (live MAT v2); "confirm" = ±sync, enter at max(nq,es)
    es_stamp: str = "causal"
    # Round-trip commission in chart points (subtracted once per trade in summary)
    commission_pts_rt: float = 0.0
    # Optional exit management (defaults off)
    time_stop_sec: float = 0.0
    be_trigger_pts: float = 0.0  # move stop to entry after this MFE
    trail_pts: float = 0.0  # trail stop by this many pts from best
    be_frac: float = 0.0  # if >0 and bracket known: be_trigger = frac * bracket
    trail_frac: float = 0.0  # if >0: trail = frac * bracket
    # Optional CD pullback gate (mirrors live Require CD pullback)
    cd_gate: bool = False


@dataclass
class TradeResult:
    session_id: str
    entry_ts_ns: int
    side: int
    entry_px: float  # points
    exit_px: float
    pnl: float
    win: bool
    bars_held: int
    reason: str  # tp | sl | eod
    tp_pts: float
    sl_pts: float
    range60: float = float("nan")
    er60: float = float("nan")
    net30: float = float("nan")
    vwap: float = float("nan")
    med_r30: float = float("nan")


@dataclass
class SimSummary:
    n: int = 0
    wins: int = 0
    net: float = 0.0  # after commission
    gross: float = 0.0  # price P&L before commission
    gross_win: float = 0.0
    gross_loss: float = 0.0
    max_dd: float = 0.0
    commission_total: float = 0.0
    trades: list[TradeResult] = field(default_factory=list)

    @property
    def wr(self) -> float:
        return self.wins / self.n if self.n else float("nan")

    @property
    def pf(self) -> float:
        if self.gross_loss <= 1e-12:
            return float("inf") if self.gross_win > 0 else float("nan")
        return self.gross_win / self.gross_loss

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "wins": self.wins,
            "wr": None if self.n == 0 else round(self.wr, 4),
            "gross": round(self.gross, 2),
            "net": round(self.net, 2),
            "commission": round(self.commission_total, 2),
            "pf": None if self.n == 0 else (None if not np.isfinite(self.pf) else round(self.pf, 3)),
            "max_dd": round(self.max_dd, 2),
            "gross_win": round(self.gross_win, 2),
            "gross_loss": round(self.gross_loss, 2),
        }


def detect_signals(
    nq: pa.Table,
    es: pa.Table,
    cfg: MatSimConfig,
) -> pa.Table:
    """ES+NQ confirmed bursts; event_ts_ns = bar end (or confirm time).

    ``cfg.es_stamp``:
      - ``"causal"`` (default): ES at/before NQ — matches live MAT v2
      - ``"confirm"``: ES within ±sync; stamp at max(nq, es) — includes NQ-first
    """
    if nq.num_rows == 0:
        return pa.table({})
    burst_kw = dict(
        min_trades=cfg.min_trades,
        min_vol=cfg.min_vol,
        min_imbalance=cfg.min_imbalance,
        min_progress_ticks=cfg.min_progress_ticks,
        tick_size=cfg.tick_size,
        cooldown_bars=cfg.cooldown_bars,
        bar_ms=cfg.bar_ms,
    )
    nq_bars = trades_to_tape_bars(nq, bar_ms=cfg.bar_ms)
    es_bars = trades_to_tape_bars(es, bar_ms=cfg.bar_ms) if es.num_rows else pa.table({})
    nq_burst = momentum_burst_events(nq_bars, **burst_kw)
    es_burst = momentum_burst_events(es_bars, **burst_kw) if es_bars.num_rows else pa.table({})
    stamp = cfg.es_stamp if cfg.es_stamp in ("causal", "confirm", "nq") else "causal"
    events = cross_confirm_bursts(
        nq_burst, es_burst, sync_ms=cfg.sync_ms, stamp=stamp
    )
    if events.num_rows == 0:
        return events
    lag = events.column("es_lag_ms").to_numpy()
    keep = lag <= cfg.max_es_lag_ms
    if not keep.any():
        return pa.table({})
    return events.take(np.where(keep)[0])


def _simulate_path(
    path_ts: np.ndarray,
    path_px: np.ndarray,  # points
    entry_i: int,
    side: int,
    tp_pts: float,
    sl_pts: float,
    slip_pts: float,
    *,
    time_stop_sec: float = 0.0,
    be_trigger_pts: float = 0.0,
    trail_pts: float = 0.0,
) -> tuple[float, float, bool, int, str]:
    """Return exit_px, pnl, win, bars_held, reason.

    Optional exits (defaults off): ``time`` / ``be`` / ``trail``. Stop hits are
    conservative vs TP when both print on the same bar.
    """
    entry = float(path_px[entry_i]) + side * slip_pts  # worse fill
    entry_ts = int(path_ts[entry_i])
    tp = entry + side * tp_pts
    init_sl = entry - side * sl_pts
    sl = init_sl
    be_armed = False
    trail_armed = False
    best = entry
    time_ns = int(time_stop_sec * 1e9) if time_stop_sec and time_stop_sec > 0 else 0

    for j in range(entry_i + 1, len(path_ts)):
        px = float(path_px[j])
        # Update best / BE / trail before hit checks
        if side > 0:
            best = max(best, px)
            mfe = best - entry
        else:
            best = min(best, px)
            mfe = entry - best

        if be_trigger_pts > 0 and mfe >= be_trigger_pts and not be_armed:
            be_armed = True
            # Move stop to entry (breakeven fill before exit slip)
            sl = entry if side > 0 else entry
            if side > 0:
                sl = max(sl, entry)
            else:
                sl = min(sl, entry)

        if trail_pts > 0 and (be_armed or be_trigger_pts <= 0):
            # Trail from best once BE armed, or immediately if no BE trigger
            if be_trigger_pts <= 0 or be_armed:
                trail_armed = True
                trail_sl = best - side * trail_pts
                if side > 0:
                    sl = max(sl, trail_sl)
                else:
                    sl = min(sl, trail_sl)

        hit_tp = px >= tp if side > 0 else px <= tp
        hit_sl = px <= sl if side > 0 else px >= sl
        if hit_tp and hit_sl:
            exit_px = sl - side * slip_pts
            pnl = side * (exit_px - entry)
            reason = _stop_reason(sl, entry, init_sl, be_armed, trail_armed, side)
            return exit_px, pnl, pnl > 0, j - entry_i, reason
        if hit_sl:
            exit_px = sl - side * slip_pts
            pnl = side * (exit_px - entry)
            reason = _stop_reason(sl, entry, init_sl, be_armed, trail_armed, side)
            return exit_px, pnl, pnl > 0, j - entry_i, reason
        if hit_tp:
            exit_px = tp - side * slip_pts
            pnl = side * (exit_px - entry)
            return exit_px, pnl, True, j - entry_i, "tp"
        if time_ns > 0 and int(path_ts[j]) - entry_ts >= time_ns:
            exit_px = px - side * slip_pts
            pnl = side * (exit_px - entry)
            return exit_px, pnl, pnl > 0, j - entry_i, "time"
    # EOD
    exit_px = float(path_px[-1]) - side * slip_pts
    pnl = side * (exit_px - entry)
    return exit_px, pnl, pnl > 0, len(path_ts) - 1 - entry_i, "eod"


def _stop_reason(
    sl: float,
    entry: float,
    init_sl: float,
    be_armed: bool,
    trail_armed: bool,
    side: int,
) -> str:
    if trail_armed and (
        (side > 0 and sl > entry + 1e-9) or (side < 0 and sl < entry - 1e-9)
    ):
        return "trail"
    if be_armed and abs(sl - entry) < 1e-9:
        return "be"
    if trail_armed and abs(sl - init_sl) > 1e-9:
        return "trail"
    if be_armed:
        return "be"
    return "sl"


GateFn = Callable[[int, RegimeSnapshot, RegimeSnapshot, float], bool]
# (side, snap_regime_window, snap_trend_window, last_px) -> accept


@dataclass
class BracketFn:
    """Optional dynamic TP/SL from regime."""

    fn: Callable[[RegimeSnapshot], tuple[float, float]] | None = None


def simulate_session(
    session_id: str,
    nq: pa.Table,
    es: pa.Table,
    cfg: MatSimConfig,
    *,
    gate: GateFn | None = None,
    regime_window_min: int = 60,
    trend_window_min: int = 30,
    vol_window_min: int = 30,
    bracket_fn: Callable[[RegimeSnapshot], tuple[float, float]] | None = None,
    vwap_from_ns: int | None = None,
) -> SimSummary:
    events = detect_signals(nq, es, cfg)
    summary = SimSummary()
    if events.num_rows == 0 or nq.num_rows == 0:
        return summary

    ts = nq.column("ts_ns").to_numpy().astype(np.int64)
    px = nq.column("price").to_numpy().astype(np.float64) * cfg.pts_per_tick
    order = np.argsort(ts)
    ts, px = ts[order], px[order]

    bars, bar_ts, cum_pv, cum_v, trade_ts, trade_px, trade_sz = build_regime_index(
        nq, pts_per_tick=cfg.pts_per_tick
    )

    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    sides = events.column("side").to_numpy().astype(int)

    # Precompute regime at each event for both windows (completed minutes only)
    kw = dict(
        vwap_from_ns=vwap_from_ns,
        trade_ts=trade_ts,
        trade_px=trade_px,
        trade_sz=trade_sz,
    )
    snaps_r = lookup_regime(
        bars, bar_ts, cum_pv, cum_v, ev_ts, window_min=regime_window_min, **kw
    )
    snaps_t = lookup_regime(
        bars, bar_ts, cum_pv, cum_v, ev_ts, window_min=trend_window_min, **kw
    )
    snaps_v = lookup_regime(
        bars, bar_ts, cum_pv, cum_v, ev_ts, window_min=vol_window_min, **kw
    )

    cd_pb_long = cd_pb_short = None
    if cfg.cd_gate:
        from research.features.cd import build_cd_series, lookup_cd_at_events

        cd = build_cd_series(nq)
        cd_fields = lookup_cd_at_events(cd, ev_ts) if cd.num_rows else {}
        cd_pb_long = cd_fields.get("cd_pullback_long")
        cd_pb_short = cd_fields.get("cd_pullback_short")

    last_exit_ts = -1
    last_signal_ts = -10**18
    cool_ns = int(cfg.signal_cooldown_sec * 1e9)
    slip_pts = cfg.slip_ticks * cfg.pts_per_tick
    commission = float(cfg.commission_pts_rt or 0.0)
    equity = 0.0
    peak = 0.0

    for i in range(len(ev_ts)):
        t0 = int(ev_ts[i])
        side = int(sides[i])
        if cfg.flat_only and t0 < last_exit_ts:
            continue
        if t0 - last_signal_ts < cool_ns:
            continue

        if cfg.cd_gate and cd_pb_long is not None and cd_pb_short is not None:
            ok_cd = (
                (side > 0 and cd_pb_long[i] == 1)
                or (side < 0 and cd_pb_short[i] == 1)
            )
            if not ok_cd:
                continue

        sr, st, sv = snaps_r[i], snaps_t[i], snaps_v[i]
        if gate is not None and not gate(side, sr, st, sr.last_px):
            continue

        entry_i = int(np.searchsorted(ts, t0, side="left"))
        if entry_i >= len(ts):
            continue

        if bracket_fn is not None:
            tp_pts, sl_pts = bracket_fn(sv)
        else:
            tp_pts, sl_pts = cfg.tp_pts, cfg.sl_pts

        bracket = max(tp_pts, sl_pts)
        be_trig = cfg.be_trigger_pts
        trail = cfg.trail_pts
        if cfg.be_frac and cfg.be_frac > 0:
            be_trig = cfg.be_frac * bracket
        if cfg.trail_frac and cfg.trail_frac > 0:
            trail = cfg.trail_frac * bracket

        exit_px, pnl, win, held, reason = _simulate_path(
            ts, px, entry_i, side, tp_pts, sl_pts, slip_pts,
            time_stop_sec=cfg.time_stop_sec,
            be_trigger_pts=be_trig,
            trail_pts=trail,
        )
        # exit time approx
        exit_i = min(entry_i + held, len(ts) - 1)
        last_exit_ts = int(ts[exit_i])
        last_signal_ts = t0

        tr = TradeResult(
            session_id=session_id,
            entry_ts_ns=t0,
            side=side,
            entry_px=float(px[entry_i]),
            exit_px=exit_px,
            pnl=pnl,
            win=win,
            bars_held=held,
            reason=reason,
            tp_pts=tp_pts,
            sl_pts=sl_pts,
            range60=sr.range_pts,
            er60=sr.efficiency_ratio,
            net30=st.net_pts,
            vwap=st.vwap,
            med_r30=sv.median_minute_range,
        )
        summary.trades.append(tr)
        summary.n += 1
        if win:
            summary.wins += 1
            summary.gross_win += max(pnl, 0.0)
        else:
            summary.gross_loss += abs(min(pnl, 0.0))
            if pnl > 0:
                summary.gross_win += pnl
        summary.gross += pnl
        summary.commission_total += commission
        net_pnl = pnl - commission
        summary.net += net_pnl
        equity += net_pnl
        peak = max(peak, equity)
        summary.max_dd = max(summary.max_dd, peak - equity)

    return summary


def _rebuild_summary(trades: list[TradeResult], commission_pts_rt: float = 0.0) -> SimSummary:
    out = SimSummary()
    equity = 0.0
    peak = 0.0
    for tr in trades:
        out.trades.append(tr)
        out.n += 1
        out.gross += tr.pnl
        out.commission_total += commission_pts_rt
        net_pnl = tr.pnl - commission_pts_rt
        out.net += net_pnl
        if tr.win:
            out.wins += 1
            out.gross_win += max(tr.pnl, 0.0)
        else:
            out.gross_loss += abs(min(tr.pnl, 0.0))
            if tr.pnl > 0:
                out.gross_win += tr.pnl
        equity += net_pnl
        peak = max(peak, equity)
        out.max_dd = max(out.max_dd, peak - equity)
    return out


def merge_summaries(parts: list[SimSummary], commission_pts_rt: float | None = None) -> SimSummary:
    trades: list[TradeResult] = []
    for s in parts:
        trades.extend(s.trades)
    if commission_pts_rt is None:
        # Infer from parts if uniform, else 0 (pnls already netted per-session)
        commissions = {round(s.commission_total / s.n, 6) for s in parts if s.n}
        commission_pts_rt = next(iter(commissions)) if len(commissions) == 1 else 0.0
        if commission_pts_rt == 0.0 and any(s.commission_total for s in parts):
            # Mixed — rebuild with 0 and sum nets from parts instead
            out = SimSummary()
            equity = 0.0
            peak = 0.0
            for s in parts:
                out.trades.extend(s.trades)
                out.n += s.n
                out.wins += s.wins
                out.gross += s.gross
                out.net += s.net
                out.commission_total += s.commission_total
                out.gross_win += s.gross_win
                out.gross_loss += s.gross_loss
                for tr in s.trades:
                    # approximate DD across sessions in chronological append order
                    c_per = s.commission_total / s.n if s.n else 0.0
                    equity += tr.pnl - c_per
                    peak = max(peak, equity)
                    out.max_dd = max(out.max_dd, peak - equity)
            return out
    return _rebuild_summary(trades, commission_pts_rt)


def filter_summary(s: SimSummary, pred, commission_pts_rt: float | None = None) -> SimSummary:
    trades = [tr for tr in s.trades if pred(tr)]
    if commission_pts_rt is None:
        commission_pts_rt = (s.commission_total / s.n) if s.n else 0.0
    return _rebuild_summary(trades, commission_pts_rt)


# --- Gate builders used by EXP-0034 ---

def gate_always(side, sr, st, px) -> bool:
    return True


def make_daytype_skip(max_range: float = 0.0, max_er: float = 0.0):
    def g(side, sr, st, px) -> bool:
        if max_range > 0 and sr.range_pts > max_range:
            return False
        if max_er > 0 and sr.efficiency_ratio > max_er:
            return False
        return True
    return g


def make_daytype_counter(max_range: float = 0.0, max_er: float = 0.0):
    """When hot: only against 30m trend; when cool: take all."""
    def g(side, sr, st, px) -> bool:
        hot = False
        if max_range > 0 and sr.range_pts > max_range:
            hot = True
        if max_er > 0 and sr.efficiency_ratio > max_er:
            hot = True
        if not hot:
            return True
        if abs(st.net_pts) < 1e-9:
            return True
        # against-trend
        return int(np.sign(st.net_pts)) != side
    return g


def make_trend_mode(mode: str):
    """mode: with | against | off"""
    def g(side, sr, st, px) -> bool:
        if mode == "off" or abs(st.net_pts) < 1e-9:
            return True
        sgn = int(np.sign(st.net_pts))
        if mode == "with":
            return sgn == side
        if mode == "against":
            return sgn != side
        return True
    return g


def make_vwap_side(require: bool = True):
    def g(side, sr, st, px) -> bool:
        if not require or not np.isfinite(st.vwap) or not np.isfinite(px):
            return True
        # long above VWAP / short below
        if side > 0:
            return px >= st.vwap
        return px <= st.vwap
    return g


def combine_gates(*gates: GateFn) -> GateFn:
    def g(side, sr, st, px) -> bool:
        return all(fn(side, sr, st, px) for fn in gates)
    return g


def make_vol_bracket(k: float, floor: float = 6.0, cap: float = 30.0):
    """Symmetric TP=SL = clip(k * median_minute_range, floor, cap)."""
    def fn(sv: RegimeSnapshot) -> tuple[float, float]:
        raw = k * max(sv.median_minute_range, 0.25)
        pts = float(np.clip(raw, floor, cap))
        return pts, pts
    return fn


def make_vol_bracket_rr(
    k: float,
    tp_r: float,
    sl_r: float,
    *,
    floor: float = 4.0,
    cap: float = 40.0,
):
    """Asymmetric vol brackets: unit = k * med_minute_range; TP=tp_r*unit, SL=sl_r*unit.

    Example: tp_r=3, sl_r=1 -> 1:3 risk:reward (risk 1R, target 3R).
    Each side is clipped to [floor, cap] independently.
    """
    def fn(sv: RegimeSnapshot) -> tuple[float, float]:
        unit = k * max(sv.median_minute_range, 0.25)
        tp = float(np.clip(tp_r * unit, floor, cap))
        sl = float(np.clip(sl_r * unit, floor, cap))
        return tp, sl
    return fn
