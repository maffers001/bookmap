"""Range-bar aggressor imbalance vs prior-range averages (H26).

As price traverses a fixed tick range, measure buy/sell/total volume and
duration, compare to a rolling average of prior completed ranges, and emit
continuation events when imbalance (and optional speed/volume) gates fire.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass
class RangeImbalanceConfig:
    range_ticks: int = 22
    tick_size: float = 1.0
    avg_n: int = 20  # prior completed bars for averages
    min_imbalance: float = 0.30
    min_bars_warmup: int = 10  # need this many completed before emitting


def trades_to_enriched_range_bars(
    trades: pa.Table,
    *,
    range_ticks: int = 22,
    tick_size: float = 1.0,
) -> pa.Table:
    """Completed tick-range bars with aggressor vols, duration, direction.

    Incomplete trailing bar is dropped (not used for events).
    """
    if trades.num_rows == 0:
        return pa.table({})

    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    price = trades.column("price").to_numpy().astype(np.float64)
    size = trades.column("size").to_numpy().astype(np.float64)
    buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)
    order = np.argsort(ts)
    ts, price, size, buy = ts[order], price[order], size[order], buy[order]

    range_pts = float(range_ticks) * float(tick_size)
    if range_pts <= 0:
        return pa.table({})

    out_end: list[int] = []
    out_start: list[int] = []
    out_open: list[float] = []
    out_high: list[float] = []
    out_low: list[float] = []
    out_close: list[float] = []
    out_buy: list[float] = []
    out_sell: list[float] = []
    out_n: list[int] = []

    start_ts = int(ts[0])
    o = h = l = c = float(price[0])
    bv = float(size[0]) if buy[0] else 0.0
    sv = 0.0 if buy[0] else float(size[0])
    n_tr = 1

    for i in range(1, len(ts)):
        px = float(price[i])
        if buy[i]:
            bv += float(size[i])
        else:
            sv += float(size[i])
        n_tr += 1
        h = max(h, px)
        l = min(l, px)
        c = px

        if h - l >= range_pts:
            out_start.append(start_ts)
            out_end.append(int(ts[i]))
            out_open.append(o)
            out_high.append(h)
            out_low.append(l)
            out_close.append(c)
            out_buy.append(bv)
            out_sell.append(sv)
            out_n.append(n_tr)
            # next bar starts at this print
            start_ts = int(ts[i])
            o = h = l = c = px
            bv = sv = 0.0
            n_tr = 0

    if not out_end:
        return pa.table({})

    buy_a = np.asarray(out_buy, dtype=np.float64)
    sell_a = np.asarray(out_sell, dtype=np.float64)
    total = buy_a + sell_a
    imb = np.where(total > 0, (buy_a - sell_a) / total, 0.0)
    start_a = np.asarray(out_start, dtype=np.int64)
    end_a = np.asarray(out_end, dtype=np.int64)
    dur_ms = (end_a - start_a) / 1e6
    close_a = np.asarray(out_close, dtype=np.float64)
    open_a = np.asarray(out_open, dtype=np.float64)
    direction = np.sign(close_a - open_a).astype(np.int8)

    return pa.table({
        "start_ts_ns": start_a,
        "event_ts_ns": end_a,  # bar close = event time
        "open": open_a,
        "high": np.asarray(out_high, dtype=np.float64),
        "low": np.asarray(out_low, dtype=np.float64),
        "close": close_a,
        "buy_vol": buy_a,
        "sell_vol": sell_a,
        "total_vol": total,
        "imbalance": imb,
        "duration_ms": dur_ms.astype(np.float64),
        "n_trades": np.asarray(out_n, dtype=np.int64),
        "direction": direction,
        "range_ticks": np.full(len(end_a), range_ticks, dtype=np.int32),
    })


def annotate_vs_prior(
    bars: pa.Table,
    *,
    avg_n: int = 20,
) -> pa.Table:
    """Add rolling prior averages and relative flags (causal: exclude current)."""
    if bars.num_rows == 0:
        return bars
    n = bars.num_rows
    buy = bars.column("buy_vol").to_numpy().astype(np.float64)
    sell = bars.column("sell_vol").to_numpy().astype(np.float64)
    tot = bars.column("total_vol").to_numpy().astype(np.float64)
    dur = bars.column("duration_ms").to_numpy().astype(np.float64)
    imb = bars.column("imbalance").to_numpy().astype(np.float64)

    avg_buy = np.full(n, np.nan)
    avg_sell = np.full(n, np.nan)
    avg_tot = np.full(n, np.nan)
    avg_dur = np.full(n, np.nan)
    avg_imb = np.full(n, np.nan)

    for i in range(n):
        lo = max(0, i - avg_n)
        if i - lo < 3:
            continue
        avg_buy[i] = buy[lo:i].mean()
        avg_sell[i] = sell[lo:i].mean()
        avg_tot[i] = tot[lo:i].mean()
        avg_dur[i] = dur[lo:i].mean()
        avg_imb[i] = imb[lo:i].mean()

    # Relatives (1 = above avg / faster = duration below avg)
    buy_vs = np.where(np.isfinite(avg_buy) & (avg_buy > 0), buy / avg_buy, np.nan)
    sell_vs = np.where(np.isfinite(avg_sell) & (avg_sell > 0), sell / avg_sell, np.nan)
    tot_vs = np.where(np.isfinite(avg_tot) & (avg_tot > 0), tot / avg_tot, np.nan)
    # speed_vs > 1 means faster than average (shorter duration)
    speed_vs = np.where(np.isfinite(avg_dur) & (dur > 0), avg_dur / dur, np.nan)

    out = bars
    for name, arr in (
        ("avg_buy_vol", avg_buy),
        ("avg_sell_vol", avg_sell),
        ("avg_total_vol", avg_tot),
        ("avg_duration_ms", avg_dur),
        ("avg_imbalance", avg_imb),
        ("buy_vs_avg", buy_vs),
        ("sell_vs_avg", sell_vs),
        ("total_vs_avg", tot_vs),
        ("speed_vs_avg", speed_vs),
    ):
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=pa.float64()))
    return out


def range_imbalance_events(
    bars: pa.Table,
    cfg: RangeImbalanceConfig | None = None,
    *,
    mode: str = "imb_opp_low",
) -> pa.Table:
    """Emit continuation events at bar close for a named gate mode.

    Modes:
      imb_only — |imb| >= min_imbalance
      imb_opp_low — buy imb & sell < avg_sell (or sell imb & buy < avg_buy)
      imb_opp_low_fast — opp_low & speed_vs_avg > 1
      imb_opp_low_slow — opp_low & speed_vs_avg < 1
      imb_opp_low_hivol — opp_low & total_vs_avg > 1
      imb_opp_low_lovol — opp_low & total_vs_avg < 1
      imb_opp_low_fast_hivol — opp_low & fast & high vol
      with_bar — same as imb_opp_low but require side == bar direction
    """
    cfg = cfg or RangeImbalanceConfig()
    if bars.num_rows == 0:
        return pa.table({})
    if "avg_sell_vol" not in bars.column_names:
        bars = annotate_vs_prior(bars, avg_n=cfg.avg_n)

    imb = bars.column("imbalance").to_numpy()
    buy = bars.column("buy_vol").to_numpy()
    sell = bars.column("sell_vol").to_numpy()
    avg_buy = bars.column("avg_buy_vol").to_numpy()
    avg_sell = bars.column("avg_sell_vol").to_numpy()
    tot_vs = bars.column("total_vs_avg").to_numpy()
    speed_vs = bars.column("speed_vs_avg").to_numpy()
    direction = bars.column("direction").to_numpy().astype(np.int8)
    ts = bars.column("event_ts_ns").to_numpy().astype(np.int64)

    thr = cfg.min_imbalance
    warm = np.isfinite(avg_buy) & np.isfinite(avg_sell)
    # need index >= min_bars_warmup
    idx = np.arange(len(ts))
    warm &= idx >= cfg.min_bars_warmup

    buy_imb = warm & (imb >= thr)
    sell_imb = warm & (imb <= -thr)
    opp_low_long = buy_imb & (sell < avg_sell)
    opp_low_short = sell_imb & (buy < avg_buy)

    if mode == "imb_only":
        keep_long, keep_short = buy_imb, sell_imb
    elif mode == "imb_opp_low":
        keep_long, keep_short = opp_low_long, opp_low_short
    elif mode == "imb_opp_low_fast":
        keep_long = opp_low_long & np.isfinite(speed_vs) & (speed_vs > 1.0)
        keep_short = opp_low_short & np.isfinite(speed_vs) & (speed_vs > 1.0)
    elif mode == "imb_opp_low_slow":
        keep_long = opp_low_long & np.isfinite(speed_vs) & (speed_vs < 1.0)
        keep_short = opp_low_short & np.isfinite(speed_vs) & (speed_vs < 1.0)
    elif mode == "imb_opp_low_hivol":
        keep_long = opp_low_long & np.isfinite(tot_vs) & (tot_vs > 1.0)
        keep_short = opp_low_short & np.isfinite(tot_vs) & (tot_vs > 1.0)
    elif mode == "imb_opp_low_lovol":
        keep_long = opp_low_long & np.isfinite(tot_vs) & (tot_vs < 1.0)
        keep_short = opp_low_short & np.isfinite(tot_vs) & (tot_vs < 1.0)
    elif mode == "imb_opp_low_fast_hivol":
        keep_long = (
            opp_low_long
            & np.isfinite(speed_vs)
            & (speed_vs > 1.0)
            & np.isfinite(tot_vs)
            & (tot_vs > 1.0)
        )
        keep_short = (
            opp_low_short
            & np.isfinite(speed_vs)
            & (speed_vs > 1.0)
            & np.isfinite(tot_vs)
            & (tot_vs > 1.0)
        )
    elif mode == "with_bar":
        keep_long = opp_low_long & (direction > 0)
        keep_short = opp_low_short & (direction < 0)
    else:
        raise ValueError(f"unknown mode: {mode}")

    keep = keep_long | keep_short
    if not keep.any():
        return pa.table({})

    sel = np.where(keep)[0]
    side = np.where(keep_long[sel], 1, -1).astype(np.int8)
    sub = bars.take(sel)

    cols = {
        "event_ts_ns": sub.column("event_ts_ns"),
        "side": pa.array(side, type=pa.int8()),
        "event_type": pa.array([f"range_imb_{mode}"] * len(sel), type=pa.string()),
        "imbalance": sub.column("imbalance"),
        "buy_vol": sub.column("buy_vol"),
        "sell_vol": sub.column("sell_vol"),
        "total_vol": sub.column("total_vol"),
        "duration_ms": sub.column("duration_ms"),
        "buy_vs_avg": sub.column("buy_vs_avg"),
        "sell_vs_avg": sub.column("sell_vs_avg"),
        "total_vs_avg": sub.column("total_vs_avg"),
        "speed_vs_avg": sub.column("speed_vs_avg"),
        "direction": sub.column("direction"),
        "range_ticks": sub.column("range_ticks"),
        "gate_mode": pa.array([mode] * len(sel), type=pa.string()),
    }
    return pa.table(cols)


MODES = (
    "imb_only",
    "imb_opp_low",
    "imb_opp_low_fast",
    "imb_opp_low_slow",
    "imb_opp_low_hivol",
    "imb_opp_low_lovol",
    "imb_opp_low_fast_hivol",
    "with_bar",
)
