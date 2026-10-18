"""Causal session regime features for MAT day-type / trend / vol gates.

Formulas are mirrored 1:1 by ``RegimeTracker.java`` (research rule 5).

Prices in parquet are tick units (tick_size=1.0). Convert to NQ chart points
with ``pts_per_tick=0.25`` (1 point = 4 ticks).

**Causal rules (no hindsight):**
- Trend / range windows use only **completed** 1-minute bars (bar end <= asof).
- VWAP is a running trade VWAP using only prints with ts <= asof.
- last_px is the last trade price at/before asof (not the unfinished minute's final close).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass
class MinuteBar:
    ts_ns: int  # minute-bucket start
    open: float  # points
    high: float
    low: float
    close: float
    volume: float
    end_ns: int = 0  # exclusive end = ts_ns + 60s


def trades_to_minute_bars(
    trades: pa.Table,
    *,
    pts_per_tick: float = 0.25,
) -> list[MinuteBar]:
    """Build contiguous 1-minute OHLCV bars from trades (causal buckets)."""
    if trades.num_rows == 0:
        return []
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64) * pts_per_tick
    sz = trades.column("size").to_numpy().astype(np.float64)
    order = np.argsort(ts)
    ts, px, sz = ts[order], px[order], sz[order]

    bar_ns = 60_000_000_000
    bar_id = (ts // bar_ns) * bar_ns
    uniq, start = np.unique(bar_id, return_index=True)
    ends = np.append(start[1:], len(ts))
    out: list[MinuteBar] = []
    for i, (s, e) in enumerate(zip(start, ends)):
        sl = slice(s, e)
        out.append(MinuteBar(
            ts_ns=int(uniq[i]),
            open=float(px[sl][0]),
            high=float(px[sl].max()),
            low=float(px[sl].min()),
            close=float(px[sl][-1]),
            volume=float(sz[sl].sum()),
            end_ns=int(uniq[i]) + bar_ns,
        ))
    return out


@dataclass
class RegimeSnapshot:
    range_pts: float
    net_pts: float
    efficiency_ratio: float
    vwap: float
    median_minute_range: float
    last_px: float
    n_bars: int


def _empty_snap(last_px: float = float("nan")) -> RegimeSnapshot:
    return RegimeSnapshot(
        range_pts=0.0,
        net_pts=0.0,
        efficiency_ratio=0.0,
        vwap=float("nan"),
        median_minute_range=0.0,
        last_px=last_px,
        n_bars=0,
    )


def regime_at(
    bars: list[MinuteBar],
    *,
    asof_ns: int,
    window_min: int,
    vwap_from_ns: int | None = None,
    cum_pv: np.ndarray | None = None,
    cum_v: np.ndarray | None = None,
    bar_ts: np.ndarray | None = None,
    trade_ts: np.ndarray | None = None,
    trade_px: np.ndarray | None = None,
    trade_sz: np.ndarray | None = None,
) -> RegimeSnapshot:
    """Causal regime snapshot.

    Trend/range: only minutes with ``end_ns <= asof_ns`` (fully closed).
    VWAP / last_px: from trades with ``ts <= asof_ns``.
    """
    if not bars and trade_ts is None:
        return _empty_snap()

    # Completed minutes only (no unfinished bar → no intra-minute hindsight)
    closed = [b for b in bars if b.end_ns <= asof_ns]
    last_px = float("nan")
    vwap = float("nan")
    if trade_ts is not None and trade_px is not None and trade_sz is not None and len(trade_ts):
        i = int(np.searchsorted(trade_ts, asof_ns, side="right"))
        if i > 0:
            last_px = float(trade_px[i - 1])
            j0 = 0
            if vwap_from_ns is not None:
                j0 = int(np.searchsorted(trade_ts, vwap_from_ns, side="left"))
            if i > j0:
                sl = slice(j0, i)
                vv = float(trade_sz[sl].sum())
                if vv > 0:
                    vwap = float((trade_px[sl] * trade_sz[sl]).sum() / vv)

    if not closed:
        return _empty_snap(last_px=last_px)

    i_end = len(closed)
    i0 = max(0, i_end - max(1, window_min))
    win = closed[i0:i_end]
    highs = np.array([b.high for b in win])
    lows = np.array([b.low for b in win])
    closes = np.array([b.close for b in win])
    opens = np.array([b.open for b in win])
    ranges = highs - lows
    net = float(closes[-1] - opens[0])
    if len(closes) == 1:
        path = abs(float(closes[0] - opens[0]))
    else:
        path = float(np.abs(np.diff(closes)).sum())
    er = abs(net) / path if path > 1e-9 else 0.0
    rng = float(highs.max() - lows.min())
    med_r = float(np.median(ranges)) if len(ranges) else 0.0
    if not np.isfinite(last_px):
        last_px = float(closes[-1])

    # Fallback VWAP from closed bars if trade path not provided
    if not np.isfinite(vwap):
        if cum_pv is not None and cum_v is not None and bar_ts is not None:
            # map closed bars to original index
            if closed:
                # find last closed bar index in full bars list
                last_c = closed[-1].ts_ns
                j = int(np.searchsorted(bar_ts, last_c, side="left"))
                if j < len(bar_ts) and bar_ts[j] == last_c:
                    j0 = 0
                    if vwap_from_ns is not None:
                        j0 = int(np.searchsorted(bar_ts, vwap_from_ns, side="left"))
                    if j >= j0:
                        pv = cum_pv[j] - (cum_pv[j0 - 1] if j0 > 0 else 0.0)
                        vv = cum_v[j] - (cum_v[j0 - 1] if j0 > 0 else 0.0)
                        if vv > 0:
                            vwap = float(pv / vv)
        else:
            sess = closed
            if vwap_from_ns is not None:
                sess = [b for b in sess if b.ts_ns >= vwap_from_ns]
            vv = sum(b.volume for b in sess)
            if vv > 0:
                vwap = float(sum(b.close * b.volume for b in sess) / vv)

    return RegimeSnapshot(
        range_pts=rng,
        net_pts=net,
        efficiency_ratio=float(er),
        vwap=vwap,
        median_minute_range=med_r,
        last_px=last_px,
        n_bars=len(win),
    )


def build_regime_index(
    trades: pa.Table,
    *,
    pts_per_tick: float = 0.25,
) -> tuple[list[MinuteBar], np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Minute bars + trade arrays for causal VWAP / last_px."""
    bars = trades_to_minute_bars(trades, pts_per_tick=pts_per_tick)
    if trades.num_rows == 0:
        empty = np.array([], dtype=np.float64)
        empty_i = np.array([], dtype=np.int64)
        return [], empty_i, empty, empty, empty_i, empty, empty
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64) * pts_per_tick
    sz = trades.column("size").to_numpy().astype(np.float64)
    order = np.argsort(ts)
    ts, px, sz = ts[order], px[order], sz[order]
    if not bars:
        return [], np.array([], dtype=np.int64), np.array([], dtype=np.float64), np.array([], dtype=np.float64), ts, px, sz
    bar_ts = np.array([b.ts_ns for b in bars], dtype=np.int64)
    pv = np.cumsum([b.close * b.volume for b in bars]).astype(np.float64)
    vv = np.cumsum([b.volume for b in bars]).astype(np.float64)
    return bars, bar_ts, pv, vv, ts, px, sz


def lookup_regime(
    bars: list[MinuteBar],
    bar_ts: np.ndarray,
    cum_pv: np.ndarray,
    cum_v: np.ndarray,
    event_ts_ns: np.ndarray,
    *,
    window_min: int,
    vwap_from_ns: int | None = None,
    trade_ts: np.ndarray | None = None,
    trade_px: np.ndarray | None = None,
    trade_sz: np.ndarray | None = None,
) -> list[RegimeSnapshot]:
    return [
        regime_at(
            bars,
            asof_ns=int(t),
            window_min=window_min,
            vwap_from_ns=vwap_from_ns,
            cum_pv=cum_pv,
            cum_v=cum_v,
            bar_ts=bar_ts,
            trade_ts=trade_ts,
            trade_px=trade_px,
            trade_sz=trade_sz,
        )
        for t in event_ts_ns
    ]
