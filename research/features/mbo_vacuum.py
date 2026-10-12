"""Near-BBO MBO cancel vacuum events (cancel rate ≫ send rate)."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass
class VacuumConfig:
    window_ms: int = 300
    tick_radius: int = 4
    min_cancels: int = 20
    min_cancel_ratio: float = 0.70
    min_aggression_prints: int = 3
    cooldown_ms: int = 500


def _rth_mask_uk(ts_ns: np.ndarray) -> np.ndarray:
    sec = ((ts_ns // 1_000_000_000) + 3600) % 86400
    start = 14 * 3600 + 30 * 60
    end = 17 * 3600
    return (sec >= start) & (sec <= end)


def mbo_vacuum_events(
    mbo: pa.Table,
    trades: pa.Table,
    cfg: VacuumConfig | None = None,
    *,
    rth_uk_only: bool = True,
) -> pa.Table:
    """Emit continuation events when near-mid cancels dominate sends.

    Cancels lack side/price in extracts — resolve via order_id from send.
    Near-BBO proxy = last trade price (tick units). Ask-book vacuum → LONG;
    bid-book vacuum → SHORT.
    """
    cfg = cfg or VacuumConfig()
    if mbo.num_rows == 0 or trades.num_rows == 0:
        return pa.table({})

    m_ts = mbo.column("ts_ns").to_numpy().astype(np.int64)
    m_op = mbo.column("op").to_numpy(zero_copy_only=False)
    m_oid = mbo.column("order_id").to_numpy(zero_copy_only=False)
    m_px = mbo.column("price_ticks").to_numpy().astype(np.int64)
    m_bid_col = mbo.column("is_bid")
    m_bid_null = m_bid_col.is_null().to_numpy()
    m_bid_vals = m_bid_col.fill_null(False).to_numpy().astype(bool)

    if rth_uk_only:
        rm = _rth_mask_uk(m_ts)
        m_ts = m_ts[rm]
        m_op = m_op[rm]
        m_oid = m_oid[rm]
        m_px = m_px[rm]
        m_bid_null = m_bid_null[rm]
        m_bid_vals = m_bid_vals[rm]

    t_ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    t_px = np.round(trades.column("price").to_numpy().astype(np.float64)).astype(np.int64)
    t_buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)
    if rth_uk_only:
        trm = _rth_mask_uk(t_ts)
        t_ts, t_px, t_buy = t_ts[trm], t_px[trm], t_buy[trm]
    t_order = np.argsort(t_ts)
    t_ts, t_px, t_buy = t_ts[t_order], t_px[t_order], t_buy[t_order]

    active: dict[str, tuple[bool, int]] = {}
    window_ns = int(cfg.window_ms * 1_000_000)
    cooldown_ns = int(cfg.cooldown_ms * 1_000_000)

    # Per book side (+1 ask / -1 bid): deque of (ts, is_cancel)
    rolls: dict[int, deque] = {1: deque(), -1: deque()}
    counts: dict[int, list] = {1: [0, 0], -1: [0, 0]}  # cancels, sends

    def _expire(side: int, now: int) -> None:
        dq = rolls[side]
        c = counts[side]
        while dq and now - dq[0][0] > window_ns:
            _t, ic = dq.popleft()
            if ic:
                c[0] -= 1
            else:
                c[1] -= 1

    def _push(side: int, now: int, is_cancel: bool) -> None:
        rolls[side].append((now, is_cancel))
        if is_cancel:
            counts[side][0] += 1
        else:
            counts[side][1] += 1

    events = []
    last_fire = {-1: -10**18, 1: -10**18}
    trade_i = 0
    last_trade_px: int | None = None

    # Aggression rolling: deque of (ts, is_buy)
    aggr_dq: deque[tuple[int, bool]] = deque()
    buy_n = sell_n = 0

    def _expire_aggr(now: int) -> None:
        nonlocal buy_n, sell_n
        while aggr_dq and now - aggr_dq[0][0] > window_ns:
            _t, ib = aggr_dq.popleft()
            if ib:
                buy_n -= 1
            else:
                sell_n -= 1

    def _ingest_trades(now: int) -> None:
        nonlocal trade_i, last_trade_px, buy_n, sell_n
        while trade_i < len(t_ts) and t_ts[trade_i] <= now:
            last_trade_px = int(t_px[trade_i])
            ib = bool(t_buy[trade_i])
            aggr_dq.append((int(t_ts[trade_i]), ib))
            if ib:
                buy_n += 1
            else:
                sell_n += 1
            trade_i += 1
        _expire_aggr(now)

    order = np.argsort(m_ts)
    for idx in order:
        ts = int(m_ts[idx])
        op = m_op[idx]
        oid = m_oid[idx]
        if not oid:
            continue

        _ingest_trades(ts)
        if last_trade_px is None:
            continue

        book_side = None
        is_cancel = False
        px = None

        if op == "send":
            if m_bid_null[idx]:
                continue
            is_bid = bool(m_bid_vals[idx])
            px = int(m_px[idx])
            active[oid] = (is_bid, px)
            book_side = -1 if is_bid else 1
            is_cancel = False
        elif op == "replace":
            if oid not in active:
                continue
            is_bid, _ = active[oid]
            px = int(m_px[idx])
            active[oid] = (is_bid, px)
            book_side = -1 if is_bid else 1
            is_cancel = False
        elif op == "cancel":
            if oid not in active:
                continue
            is_bid, px = active.pop(oid)
            book_side = -1 if is_bid else 1
            is_cancel = True
        else:
            continue

        if px is None or abs(px - last_trade_px) > cfg.tick_radius:
            continue

        _expire(book_side, ts)
        _push(book_side, ts, is_cancel)

        cancels, sends = counts[book_side]
        total = cancels + sends
        if cancels < cfg.min_cancels or total <= 0:
            continue
        ratio = cancels / total
        if ratio < cfg.min_cancel_ratio:
            continue

        event_side = 1 if book_side == 1 else -1
        aggr = buy_n if event_side > 0 else sell_n
        if aggr < cfg.min_aggression_prints:
            continue
        if ts - last_fire[event_side] < cooldown_ns:
            continue

        events.append({
            "event_ts_ns": ts,
            "side": event_side,
            "cancels": cancels,
            "sends": sends,
            "cancel_ratio": ratio,
            "aggression_prints": aggr,
        })
        last_fire[event_side] = ts

    if not events:
        return pa.table({})

    return pa.table({
        "event_ts_ns": np.array([e["event_ts_ns"] for e in events], dtype=np.int64),
        "side": np.array([e["side"] for e in events], dtype=np.int8),
        "event_type": ["mbo_vacuum"] * len(events),
        "cancels": np.array([e["cancels"] for e in events], dtype=np.int32),
        "sends": np.array([e["sends"] for e in events], dtype=np.int32),
        "cancel_ratio": np.array([e["cancel_ratio"] for e in events], dtype=np.float64),
        "aggression_prints": np.array([e["aggression_prints"] for e in events], dtype=np.int32),
    })


def cross_confirm_vacuums(
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
        "event_type", pa.array(["dual_mbo_vacuum"] * len(idx), type=pa.string())
    )
    return out
