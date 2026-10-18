"""Causal swing highs/lows and break / failed-break detectors."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass
class SwingConfig:
    lookback_ms: int = 180_000  # 3 minutes
    confirm_ms: int = 20_000  # extreme not exceeded for 20s
    tick_size: float = 1.0
    break_ticks: float = 1.0  # ticks beyond swing to count as break


@dataclass(frozen=True)
class SwingPivot:
    ts_ns: int
    price_ticks: int
    kind: str  # HIGH | LOW
    confirm_ts_ns: int


def _price_ticks(trades: pa.Table, tick_size: float) -> tuple[np.ndarray, np.ndarray]:
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = np.round(trades.column("price").to_numpy().astype(np.float64) / tick_size).astype(
        np.int64
    )
    order = np.argsort(ts)
    return ts[order], px[order]


def detect_swings(
    trades: pa.Table,
    cfg: SwingConfig | None = None,
) -> list[SwingPivot]:
    """Causal swings: lookback extreme that is not exceeded for confirm_ms.

    After confirm, the pivot is available for break tests. Uses trade prints only.
    """
    cfg = cfg or SwingConfig()
    if trades.num_rows == 0:
        return []
    ts, px = _price_ticks(trades, cfg.tick_size)
    lookback_ns = int(cfg.lookback_ms * 1_000_000)
    confirm_ns = int(cfg.confirm_ms * 1_000_000)

    pivots: list[SwingPivot] = []
    # Candidate: at each trade, if it is the extreme of the prior lookback window,
    # start a confirm clock; if not exceeded until confirm_ms, emit pivot.
    pending_high: tuple[int, int, int] | None = None  # (extreme_ts, extreme_px, start_i)
    pending_low: tuple[int, int, int] | None = None

    for i in range(len(ts)):
        t = int(ts[i])
        p = int(px[i])
        lo_t = t - lookback_ns
        # Invalidate / update pending highs
        if pending_high is not None:
            et, ep, _ = pending_high
            if p > ep:
                pending_high = None
            elif t - et >= confirm_ns:
                pivots.append(
                    SwingPivot(ts_ns=et, price_ticks=ep, kind="HIGH", confirm_ts_ns=t)
                )
                pending_high = None
        if pending_low is not None:
            et, ep, _ = pending_low
            if p < ep:
                pending_low = None
            elif t - et >= confirm_ns:
                pivots.append(
                    SwingPivot(ts_ns=et, price_ticks=ep, kind="LOW", confirm_ts_ns=t)
                )
                pending_low = None

        # Window for new candidate (trades strictly before current)
        j0 = np.searchsorted(ts, lo_t, side="right")
        if i - j0 < 3:
            continue
        window_px = px[j0:i]
        window_ts = ts[j0:i]
        hi_i = int(np.argmax(window_px))
        lo_i = int(np.argmin(window_px))
        # Candidate high: last trade of lookback equals the max, and current does not exceed
        if int(window_px[hi_i]) == int(window_px[-1]) and p <= int(window_px[-1]):
            if pending_high is None or pending_high[1] != int(window_px[-1]):
                pending_high = (int(window_ts[-1]), int(window_px[-1]), i)
        if int(window_px[lo_i]) == int(window_px[-1]) and p >= int(window_px[-1]):
            if pending_low is None or pending_low[1] != int(window_px[-1]):
                pending_low = (int(window_ts[-1]), int(window_px[-1]), i)

    return pivots


def last_confirmed_swing(
    pivots: list[SwingPivot],
    asof_ts_ns: int,
    kind: str,
) -> SwingPivot | None:
    """Latest pivot of kind with confirm_ts_ns <= asof."""
    best: SwingPivot | None = None
    for p in pivots:
        if p.kind != kind:
            continue
        if p.confirm_ts_ns > asof_ts_ns:
            continue
        if best is None or p.confirm_ts_ns > best.confirm_ts_ns:
            best = p
    return best


def annotate_burst_swing_break(
    events: pa.Table,
    trades: pa.Table,
    cfg: SwingConfig | None = None,
) -> pa.Table:
    """Flag momentum bursts that break the last confirmed swing in trade direction.

    Long: break last HIGH. Short: break last LOW.
    break_through=1 if event price (or nearby print) is beyond swing by break_ticks.
    """
    cfg = cfg or SwingConfig()
    if events.num_rows == 0:
        return events
    pivots = detect_swings(trades, cfg)
    ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    side = events.column("side").to_numpy().astype(np.int8)

    # Price at event: last trade at or before event
    tr_ts, tr_px = _price_ticks(trades, cfg.tick_size)
    break_flags = np.zeros(len(ts), dtype=np.int8)
    swing_px = np.full(len(ts), np.nan)
    swing_age_ms = np.full(len(ts), np.nan)

    for i, (t0, s) in enumerate(zip(ts, side)):
        kind = "HIGH" if s > 0 else "LOW"
        piv = last_confirmed_swing(pivots, int(t0), kind)
        if piv is None:
            continue
        swing_px[i] = float(piv.price_ticks)
        swing_age_ms[i] = (t0 - piv.confirm_ts_ns) / 1e6
        j = np.searchsorted(tr_ts, t0, side="right") - 1
        if j < 0:
            continue
        px = int(tr_px[j])
        if s > 0 and px >= piv.price_ticks + cfg.break_ticks:
            break_flags[i] = 1
        elif s < 0 and px <= piv.price_ticks - cfg.break_ticks:
            break_flags[i] = 1

    out = events
    for name in ("swing_break", "swing_price_ticks", "swing_age_ms"):
        if name in out.column_names:
            out = out.drop(name)
    out = out.append_column("swing_break", pa.array(break_flags, type=pa.int8()))
    out = out.append_column("swing_price_ticks", pa.array(swing_px, type=pa.float64()))
    out = out.append_column("swing_age_ms", pa.array(swing_age_ms, type=pa.float64()))
    return out


def failed_swing_break_events(
    trades: pa.Table,
    cfg: SwingConfig | None = None,
    *,
    pierce_ticks: int = 4,
    reclaim_ms: int = 2000,
    cooldown_ms: int = 5000,
) -> pa.Table:
    """Stop-run reject: pierce beyond swing by pierce_ticks, reclaim inside within reclaim_ms.

    Side = fade (short after failed break of HIGH, long after failed break of LOW).
    Event timestamp = reclaim print.
    """
    cfg = cfg or SwingConfig()
    if trades.num_rows == 0:
        return pa.table({})
    pivots = detect_swings(trades, cfg)
    if not pivots:
        return pa.table({})
    ts, px = _price_ticks(trades, cfg.tick_size)
    reclaim_ns = int(reclaim_ms * 1_000_000)
    cooldown_ns = int(cooldown_ms * 1_000_000)

    events_ts: list[int] = []
    events_side: list[int] = []
    events_swing: list[int] = []
    events_pierce: list[int] = []
    last_fire = -10**18

    # Walk trades; track whether we are in a pierce state for active swings
    # Use latest confirmed HIGH and LOW at each time
    for i in range(len(ts)):
        t = int(ts[i])
        p = int(px[i])
        if t - last_fire < cooldown_ns:
            continue
        high = last_confirmed_swing(pivots, t, "HIGH")
        low = last_confirmed_swing(pivots, t, "LOW")

        # Failed break of HIGH → short
        if high is not None:
            # Look back reclaim_ns for a pierce beyond high, then current back inside
            lo_i = np.searchsorted(ts, t - reclaim_ns, side="left")
            pierced = False
            max_pierce = 0
            for j in range(lo_i, i):
                if int(px[j]) >= high.price_ticks + pierce_ticks:
                    pierced = True
                    max_pierce = max(max_pierce, int(px[j]) - high.price_ticks)
            if pierced and p <= high.price_ticks:
                events_ts.append(t)
                events_side.append(-1)
                events_swing.append(high.price_ticks)
                events_pierce.append(max_pierce)
                last_fire = t
                continue

        # Failed break of LOW → long
        if low is not None:
            lo_i = np.searchsorted(ts, t - reclaim_ns, side="left")
            pierced = False
            max_pierce = 0
            for j in range(lo_i, i):
                if int(px[j]) <= low.price_ticks - pierce_ticks:
                    pierced = True
                    max_pierce = max(max_pierce, low.price_ticks - int(px[j]))
            if pierced and p >= low.price_ticks:
                events_ts.append(t)
                events_side.append(1)
                events_swing.append(low.price_ticks)
                events_pierce.append(max_pierce)
                last_fire = t

    if not events_ts:
        return pa.table({})
    # stamp = last input used (reclaim print after pierce)
    return pa.table({
        "event_ts_ns": np.asarray(events_ts, dtype=np.int64),
        "side": np.asarray(events_side, dtype=np.int8),
        "event_type": ["failed_swing_break"] * len(events_ts),
        "swing_price_ticks": np.asarray(events_swing, dtype=np.int64),
        "pierce_ticks": np.asarray(events_pierce, dtype=np.int64),
    })


def cross_confirm_failed_breaks(
    nq_events: pa.Table,
    es_events: pa.Table,
    *,
    sync_ms: int = 2000,
) -> pa.Table:
    """Keep NQ failed-breaks with same-side ES failed-break within sync_ms."""
    if nq_events.num_rows == 0 or es_events.num_rows == 0:
        return pa.table({})
    nq_ts = nq_events.column("event_ts_ns").to_numpy().astype(np.int64)
    nq_side = nq_events.column("side").to_numpy()
    es_ts = es_events.column("event_ts_ns").to_numpy().astype(np.int64)
    es_side = es_events.column("side").to_numpy()
    order = np.argsort(es_ts)
    es_ts, es_side = es_ts[order], es_side[order]
    window = int(sync_ms * 1_000_000)
    keep = []
    lags = []
    for i in range(len(nq_ts)):
        lo = np.searchsorted(es_ts, nq_ts[i] - window, side="left")
        hi = np.searchsorted(es_ts, nq_ts[i] + window, side="right")
        best = None
        for j in range(lo, hi):
            if int(es_side[j]) != int(nq_side[i]):
                continue
            lag = abs(int(es_ts[j] - nq_ts[i]))
            if best is None or lag < best:
                best = lag
        if best is not None:
            keep.append(i)
            lags.append(best / 1e6)
    if not keep:
        return pa.table({})
    idx = np.asarray(keep, dtype=np.int64)
    out = nq_events.take(idx)
    if "es_lag_ms" in out.column_names:
        out = out.drop("es_lag_ms")
    return out.append_column("es_lag_ms", pa.array(lags, type=pa.float64()))
