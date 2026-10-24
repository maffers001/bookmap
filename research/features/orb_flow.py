"""Causal orderflow features at ORB break stamps (EXP-0054 / H45).

Everything is computed from completed 100 ms tape bars
(``research.features.momentum.trades_to_tape_bars``): a bar contributes
only if its bar end is at/before the stamp, so features at a stamp are
identical when the input is truncated at that stamp (Stage 0 checks this).

Signed values are raw (buy-positive); the runner multiplies by the break
side when it needs direction-aligned values.
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa

BAR_NS = 100_000_000  # 100 ms
PTS_PER_TICK = 0.25


def _bar_arrays(bars: pa.Table) -> dict[str, np.ndarray]:
    if bars.num_rows == 0:
        return {}
    return {
        "ts": bars.column("ts_ns").to_numpy().astype(np.int64),
        "buy": bars.column("buy_vol").to_numpy().astype(np.float64),
        "sell": bars.column("sell_vol").to_numpy().astype(np.float64),
        "total": bars.column("total_vol").to_numpy().astype(np.float64),
        "ntr": bars.column("n_trades").to_numpy().astype(np.float64),
        "maxp": bars.column("max_print").to_numpy().astype(np.float64),
        "open": bars.column("open").to_numpy().astype(np.float64),
        "close": bars.column("close").to_numpy().astype(np.float64),
    }


def flow_at(bars: pa.Table, stamp_ns: int, window_sec: float) -> dict:
    """Aggressor flow over completed bars ending in (stamp - W, stamp]."""
    a = _bar_arrays(bars)
    empty = {
        "imbalance": float("nan"), "delta": 0.0, "total_vol": 0.0,
        "buy_vol": 0.0, "sell_vol": 0.0, "n_trades": 0.0, "max_print": 0.0,
        "progress_pts": float("nan"), "efficiency": float("nan"),
        "vol_burst_ratio": float("nan"), "n_bars": 0,
    }
    if not a:
        return empty
    end = a["ts"] + BAR_NS
    win_ns = int(window_sec * 1e9)
    k = (end <= stamp_ns) & (end > stamp_ns - win_ns)
    if not k.any():
        return empty
    buy = float(a["buy"][k].sum())
    sell = float(a["sell"][k].sum())
    tot = buy + sell
    idx = np.where(k)[0]
    progress = float((a["close"][idx[-1]] - a["open"][idx[0]]) * PTS_PER_TICK)
    out = {
        "imbalance": float((buy - sell) / tot) if tot > 0 else float("nan"),
        "delta": buy - sell,
        "total_vol": tot,
        "buy_vol": buy,
        "sell_vol": sell,
        "n_trades": float(a["ntr"][k].sum()),
        "max_print": float(a["maxp"][k].max()),
        "progress_pts": progress,
        # effort/result: signed price progress per contract traded
        "efficiency": float(progress / tot) if tot > 0 else float("nan"),
        "vol_burst_ratio": float("nan"),
        "n_bars": int(k.sum()),
    }
    # Volume burst: this window's total vs the median window-total of the
    # trailing 5 minutes (completed, non-overlapping windows before it).
    base_ns = 300 * 1_000_000_000
    kb = (end <= stamp_ns - win_ns) & (end > stamp_ns - base_ns)
    if kb.any() and tot > 0:
        # dense rolling sum on the bar grid (missing bars = 0 volume)
        t0 = int(stamp_ns - base_ns)
        grid_n = int(base_ns // BAR_NS)
        dense = np.zeros(grid_n)
        ends_b = end[kb]
        pos = ((ends_b - BAR_NS - t0) // BAR_NS).astype(np.int64)
        ok = (pos >= 0) & (pos < grid_n)
        np.add.at(dense, pos[ok], a["total"][kb][ok])
        w_bars = max(1, int(round(win_ns / BAR_NS)))
        c = np.concatenate([[0.0], np.cumsum(dense)])
        roll = c[w_bars:] - c[:-w_bars]
        med = float(np.median(roll)) if len(roll) else float("nan")
        if np.isfinite(med) and med > 0:
            out["vol_burst_ratio"] = float(tot / med)
    return out


def first_confirm_stamp(
    traded_bars: pa.Table,
    cross_bars: pa.Table,
    break_ts_ns: int,
    side: int,
    deadline_ns: int,
    *,
    or_level_pts: float,
    traded_imb_min: float = 0.30,
    cross_imb_min: float = 0.0,
    window_sec: float = 5.0,
) -> int:
    """Arm C: first completed-bar end in (break, deadline] where the Arm B
    flow conditions hold and the traded market's bar close is still outside
    the OR (long: close > ORH; short: close < ORL). 0 = never confirmed."""
    a = _bar_arrays(traded_bars)
    if not a:
        return 0
    end = a["ts"] + BAR_NS
    cand = np.where((end > break_ts_ns) & (end <= deadline_ns))[0]
    for i in cand:
        stamp = int(end[i])
        close_pts = float(a["close"][i] * PTS_PER_TICK)
        if side > 0 and not close_pts > or_level_pts:
            continue
        if side < 0 and not close_pts < or_level_pts:
            continue
        f_t = flow_at(traded_bars, stamp, window_sec)
        f_c = flow_at(cross_bars, stamp, window_sec)
        imb_t = f_t["imbalance"]
        imb_c = f_c["imbalance"]
        if not np.isfinite(imb_t) or side * imb_t < traded_imb_min:
            continue
        # Cross market with no completed bars in the window does not oppose;
        # a defined imbalance must be >= cross_imb_min in break direction.
        if np.isfinite(imb_c) and side * imb_c < cross_imb_min:
            continue
        return stamp
    return 0


def sweep_flag_at(
    trades: pa.Table,
    stamp_ns: int,
    side: int,
    *,
    window_ms: int = 2000,
    min_prints: int = 5,
    min_vol: float = 5.0,
    min_span_ticks: float = 1.0,
) -> int:
    """1 if one aggressor_order_id swept in the break direction in the
    trailing window ending at the stamp (EXP-0017-style thresholds, but
    computed on the narrow causal window only)."""
    if trades.num_rows == 0 or "aggressor_order_id" not in trades.column_names:
        return 0
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    lo = int(stamp_ns - window_ms * 1_000_000)
    k = (ts > lo) & (ts <= stamp_ns)
    if not k.any():
        return 0
    px = trades.column("price").to_numpy().astype(np.float64)[k]
    sz = trades.column("size").to_numpy().astype(np.float64)[k]
    buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)[k]
    aid = trades.column("aggressor_order_id").to_numpy(zero_copy_only=False)[k]
    agg: dict[str, list] = {}
    for i in range(len(px)):
        a = aid[i]
        if not a:
            continue
        e = agg.setdefault(a, [0, 0.0, px[i], px[i], 0.0, 0.0])
        e[0] += 1
        e[1] += float(sz[i])
        e[2] = min(e[2], float(px[i]))
        e[3] = max(e[3], float(px[i]))
        if buy[i]:
            e[4] += float(sz[i])
        else:
            e[5] += float(sz[i])
    for n, vol, lo_px, hi_px, bv, sv in agg.values():
        if n < min_prints or vol < min_vol or (hi_px - lo_px) < min_span_ticks:
            continue
        dom = 1 if bv >= 0.7 * (bv + sv) else (-1 if sv >= 0.7 * (bv + sv) else 0)
        if dom == side:
            return 1
    return 0
