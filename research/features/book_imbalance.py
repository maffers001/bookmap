"""Near-book depth imbalance (ask vs bid size within X ticks of mid)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass
class BookImbalanceConfig:
    tick_radius: int = 10  # X ticks above/below mid
    sample_ms: int = 100
    min_abs_imbalance: float = 0.45  # |ask-bid|/(ask+bid)
    min_total_size: float = 20.0  # avoid empty thin books
    cooldown_ms: int = 1000
    # pressure: ask-heavy → short; bid-heavy → long
    # magnet: opposite (fade into heavy side)
    interpretation: str = "pressure"


def _rth_mask_uk(ts_ns: np.ndarray) -> np.ndarray:
    sec = ((ts_ns // 1_000_000_000) + 3600) % 86400
    start = 14 * 3600 + 30 * 60
    end = 17 * 3600
    return (sec >= start) & (sec <= end)


def _band_size(book: dict[int, int], lo: int, hi: int) -> float:
    if lo > hi:
        return 0.0
    total = 0.0
    for px, sz in book.items():
        if lo <= px <= hi and sz > 0:
            total += float(sz)
    return total


def book_imbalance_events(
    depth: pa.Table,
    cfg: BookImbalanceConfig | None = None,
    *,
    rth_uk_only: bool = True,
) -> pa.Table:
    """Emit continuation (or magnet) events when near-book imbalance turns extreme.

    imbalance = (ask_near - bid_near) / (ask_near + bid_near)
      ask_near = sum ask size in (mid, mid+X]
      bid_near = sum bid size in [mid-X, mid)

    pressure: imb >= +thr → SHORT; imb <= -thr → LONG
    magnet:   flipped sides (fade into the heavy book)
    """
    cfg = cfg or BookImbalanceConfig()
    if depth.num_rows == 0:
        return pa.table({})

    d_ts = depth.column("ts_ns").to_numpy().astype(np.int64)
    d_bid = depth.column("is_bid").to_numpy().astype(bool)
    d_px = depth.column("price_ticks").to_numpy().astype(np.int64)
    d_sz = depth.column("size").to_numpy().astype(np.int64)

    if rth_uk_only:
        m = _rth_mask_uk(d_ts)
        d_ts, d_bid, d_px, d_sz = d_ts[m], d_bid[m], d_px[m], d_sz[m]
    if len(d_ts) == 0:
        return pa.table({})

    order = np.argsort(d_ts, kind="mergesort")
    d_ts, d_bid, d_px, d_sz = d_ts[order], d_bid[order], d_px[order], d_sz[order]

    bids: dict[int, int] = {}
    asks: dict[int, int] = {}
    best_bid = -10**18
    best_ask = 10**18

    sample_ns = int(cfg.sample_ms * 1_000_000)
    cooldown_ns = int(cfg.cooldown_ms * 1_000_000)
    next_sample = int(d_ts[0]) + sample_ns
    last_fire = {-1: -10**18, 1: -10**18}
    was_extreme = False

    events: list[dict] = []

    for i in range(len(d_ts)):
        ts = int(d_ts[i])
        is_bid = bool(d_bid[i])
        px = int(d_px[i])
        sz = int(d_sz[i])
        book = bids if is_bid else asks
        if sz <= 0:
            if px in book:
                del book[px]
            if is_bid and px == best_bid:
                best_bid = max(book) if book else -10**18
            elif (not is_bid) and px == best_ask:
                best_ask = min(book) if book else 10**18
        else:
            book[px] = sz
            if is_bid:
                if px > best_bid:
                    best_bid = px
            else:
                if px < best_ask:
                    best_ask = px

        if ts < next_sample:
            continue
        next_sample = ts + sample_ns

        if best_bid <= -10**17 or best_ask >= 10**17:
            was_extreme = False
            continue
        mid = (best_bid + best_ask) // 2
        x = cfg.tick_radius
        bid_near = _band_size(bids, mid - x, mid - 1)
        ask_near = _band_size(asks, mid + 1, mid + x)
        total = bid_near + ask_near
        if total < cfg.min_total_size:
            was_extreme = False
            continue
        imb = (ask_near - bid_near) / total
        extreme = abs(imb) >= cfg.min_abs_imbalance

        if not extreme:
            was_extreme = False
            continue
        # Edge trigger: enter extreme from non-extreme
        if was_extreme:
            continue
        was_extreme = True

        if cfg.interpretation == "magnet":
            # fade into heavy side: ask-heavy → long into offers
            side = 1 if imb > 0 else -1
        else:
            # pressure: ask-heavy → short
            side = -1 if imb > 0 else 1

        if ts - last_fire[side] < cooldown_ns:
            continue
        last_fire[side] = ts
        events.append({
            "event_ts_ns": ts,
            "side": side,
            "imbalance": float(imb),
            "bid_near": bid_near,
            "ask_near": ask_near,
            "mid": mid,
            "best_bid": best_bid,
            "best_ask": best_ask,
        })

    if not events:
        return pa.table({})

    et = (
        "book_imb_magnet"
        if cfg.interpretation == "magnet"
        else "book_imb_pressure"
    )
    return pa.table({
        "event_ts_ns": np.array([e["event_ts_ns"] for e in events], dtype=np.int64),
        "side": np.array([e["side"] for e in events], dtype=np.int8),
        "event_type": [et] * len(events),
        "imbalance": np.array([e["imbalance"] for e in events], dtype=np.float64),
        "bid_near": np.array([e["bid_near"] for e in events], dtype=np.float64),
        "ask_near": np.array([e["ask_near"] for e in events], dtype=np.float64),
        "mid": np.array([e["mid"] for e in events], dtype=np.int64),
    })


def cross_confirm_imbalance(
    nq_events: pa.Table,
    es_events: pa.Table,
    *,
    sync_ms: int = 1500,
) -> pa.Table:
    if nq_events.num_rows == 0 or es_events.num_rows == 0:
        return pa.table({})
    nq_ts = nq_events.column("event_ts_ns").to_numpy().astype(np.int64)
    nq_side = nq_events.column("side").to_numpy()
    es_ts = es_events.column("event_ts_ns").to_numpy().astype(np.int64)
    es_side = es_events.column("side").to_numpy()
    order = np.argsort(es_ts)
    es_ts, es_side = es_ts[order], es_side[order]
    window = int(sync_ms * 1_000_000)
    keep, lags = [], []
    for i in range(len(nq_ts)):
        lo = np.searchsorted(es_ts, nq_ts[i] - window, side="left")
        hi = np.searchsorted(es_ts, nq_ts[i] + window, side="right")
        best = None
        for j in range(lo, hi):
            if int(es_side[j]) != int(nq_side[i]):
                continue
            lag = int(es_ts[j] - nq_ts[i])
            if best is None or abs(lag) < abs(best):
                best = lag
        if best is not None:
            keep.append(i)
            lags.append(best / 1e6)
    if not keep:
        return pa.table({})
    idx = np.asarray(keep, dtype=np.int64)
    out = nq_events.take(idx)
    for name in ("es_lag_ms", "event_type"):
        if name in out.column_names:
            out = out.drop(name)
    out = out.append_column("es_lag_ms", pa.array(lags, type=pa.float64()))
    out = out.append_column(
        "event_type",
        pa.array(["dual_book_imbalance"] * len(idx), type=pa.string()),
    )
    return out
