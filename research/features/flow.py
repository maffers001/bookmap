"""
Aggressive flow and effort/result features using PyArrow/numpy (no pandas).
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc


def trades_table_to_bars(
    trades: pa.Table,
    bar_ms: int = 250,
    tick_size: float = 0.25,
    epsilon_ticks: float = 0.5,
    lookback: int = 8,
) -> pa.Table:
    """Aggregate aggressor trades into fixed time bars with effort/result."""
    if trades.num_rows == 0:
        return pa.table({})

    ts = trades.column("ts_ns").to_numpy()
    price = trades.column("price").to_numpy()
    size = trades.column("size").to_numpy().astype(np.float64)
    buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)

    # Sort
    order = np.argsort(ts)
    ts, price, size, buy = ts[order], price[order], size[order], buy[order]

    bar_ns = int(bar_ms * 1_000_000)
    bar_id = (ts // bar_ns) * bar_ns
    uniq, start_idx = np.unique(bar_id, return_index=True)
    # ends
    ends = np.append(start_idx[1:], len(ts))

    n = len(uniq)
    buy_vol = np.zeros(n)
    sell_vol = np.zeros(n)
    buy_orders = np.zeros(n)
    sell_orders = np.zeros(n)
    open_px = np.zeros(n)
    high_px = np.zeros(n)
    low_px = np.zeros(n)
    close_px = np.zeros(n)

    # order ids if present
    has_oid = "aggressor_order_id" in trades.column_names
    if has_oid:
        oid = trades.column("aggressor_order_id").take(order).to_numpy(zero_copy_only=False)
    else:
        oid = None

    for i, (s, e) in enumerate(zip(start_idx, ends)):
        sl = slice(s, e)
        bmask = buy[sl]
        smask = ~bmask
        buy_vol[i] = size[sl][bmask].sum() if bmask.any() else 0.0
        sell_vol[i] = size[sl][smask].sum() if smask.any() else 0.0
        open_px[i] = price[sl][0]
        high_px[i] = price[sl].max()
        low_px[i] = price[sl].min()
        close_px[i] = price[sl][-1]
        if oid is None:
            buy_orders[i] = int(bmask.sum())
            sell_orders[i] = int(smask.sum())
        else:
            o = oid[sl]
            # count unique non-empty ids per side
            buy_orders[i] = len({x for x, m in zip(o, bmask) if m and x})
            sell_orders[i] = len({x for x, m in zip(o, smask) if m and x})

    up_ticks = np.maximum((high_px - open_px) / tick_size, 0.0)
    down_ticks = np.maximum((open_px - low_px) / tick_size, 0.0)
    buy_effort = buy_vol / np.maximum(up_ticks, epsilon_ticks)
    sell_effort = sell_vol / np.maximum(down_ticks, epsilon_ticks)
    delta = buy_vol - sell_vol
    net = close_px - open_px

    # Rolling exhaustion
    exhaustion = np.zeros(n)
    exhaustion_side = np.zeros(n, dtype=np.int8)
    half = max(1, lookback // 2)
    for i in range(1, n):
        a = max(0, i - lookback + 1)
        window = slice(a, i + 1)
        length = i - a + 1
        if length < 2:
            continue
        h = min(half, length)
        net_sum = net[window].sum()
        sell_early = sell_vol[a : a + h].sum()
        sell_late = sell_vol[a + h : i + 1].sum()
        down_early = down_ticks[a : a + h].sum()
        down_late = down_ticks[a + h : i + 1].sum()
        buy_early = buy_vol[a : a + h].sum()
        buy_late = buy_vol[a + h : i + 1].sum()
        up_early = up_ticks[a : a + h].sum()
        up_late = up_ticks[a + h : i + 1].sum()
        if net_sum < 0 and sell_late > sell_early and down_late <= down_early and sell_effort[i] > buy_effort[i]:
            exhaustion[i] = 1.0
            exhaustion_side[i] = -1
        elif net_sum > 0 and buy_late > buy_early and up_late <= up_early and buy_effort[i] > sell_effort[i]:
            exhaustion[i] = 1.0
            exhaustion_side[i] = 1

    buy_effort_accel = np.diff(buy_effort, prepend=buy_effort[0])
    sell_effort_accel = np.diff(sell_effort, prepend=sell_effort[0])

    return pa.table({
        "ts_ns": uniq.astype(np.int64),
        "buy_vol": buy_vol,
        "sell_vol": sell_vol,
        "buy_orders": buy_orders,
        "sell_orders": sell_orders,
        "delta": delta,
        "open": open_px,
        "high": high_px,
        "low": low_px,
        "close": close_px,
        "up_ticks": up_ticks,
        "down_ticks": down_ticks,
        "buy_effort": buy_effort,
        "sell_effort": sell_effort,
        "buy_effort_accel": buy_effort_accel,
        "sell_effort_accel": sell_effort_accel,
        "exhaustion": exhaustion,
        "exhaustion_side": exhaustion_side,
    })


def rolling_zscore_np(x: np.ndarray, window: int = 40) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=np.float64)
    minp = max(5, window // 4)
    for i in range(len(x)):
        a = max(0, i - window + 1)
        w = x[a : i + 1]
        if len(w) < minp:
            continue
        sd = w.std()
        out[i] = 0.0 if sd < 1e-12 else (x[i] - w.mean()) / sd
    return out
