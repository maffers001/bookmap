"""Simultaneous ES+NQ aggressor behaviour on the same clock (H27).

Distinct from:
  - EXP-0008: NQ burst confirmed by ES burst within ±sync (can be sequential)
  - EXP-0029: same-window *price* co-move
  - H26: NQ-only range-bar imbalance

Here both markets must show same-side aggressor imbalance in the *same*
wall-clock tape bar (zero lag by construction). Optional dual range-bar
closes within a tight sync window.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from research.features.momentum import trades_to_tape_bars
from research.features.range_imbalance import (
    RangeImbalanceConfig,
    annotate_vs_prior,
    range_imbalance_events,
    trades_to_enriched_range_bars,
)


@dataclass
class SimultaneousFlowConfig:
    bar_ms: int = 100
    min_imbalance: float = 0.55
    min_vol_nq: float = 25.0
    min_vol_es: float = 15.0
    min_trades_nq: int = 6
    min_trades_es: int = 4
    min_progress_ticks: float = 0.0  # 0 = flow-only; >0 also require same-sign price
    tick_size: float = 1.0
    cooldown_bars: int = 3


def _align_bars(nq_bars: pa.Table, es_bars: pa.Table) -> tuple[np.ndarray, ...]:
    """Inner-join tape bars on identical bar timestamps."""
    nq_ts = nq_bars.column("ts_ns").to_numpy().astype(np.int64)
    es_ts = es_bars.column("ts_ns").to_numpy().astype(np.int64)
    # map ES ts -> row
    es_map = {int(t): i for i, t in enumerate(es_ts)}
    nq_idx: list[int] = []
    es_idx: list[int] = []
    for i, t in enumerate(nq_ts):
        j = es_map.get(int(t))
        if j is not None:
            nq_idx.append(i)
            es_idx.append(j)
    if not nq_idx:
        return tuple(np.array([], dtype=np.int64) for _ in range(11))
    ni = np.asarray(nq_idx, dtype=np.int64)
    ei = np.asarray(es_idx, dtype=np.int64)
    return (
        nq_ts[ni],
        nq_bars.column("imbalance").to_numpy()[ni],
        nq_bars.column("total_vol").to_numpy()[ni],
        nq_bars.column("n_trades").to_numpy()[ni],
        nq_bars.column("net").to_numpy()[ni],
        es_bars.column("imbalance").to_numpy()[ei],
        es_bars.column("total_vol").to_numpy()[ei],
        es_bars.column("n_trades").to_numpy()[ei],
        es_bars.column("net").to_numpy()[ei],
        nq_bars.column("buy_vol").to_numpy()[ni],
        nq_bars.column("sell_vol").to_numpy()[ni],
    )


def simultaneous_flow_events(
    nq: pa.Table,
    es: pa.Table,
    cfg: SimultaneousFlowConfig | None = None,
) -> pa.Table:
    """Same wall-clock bar: both markets same-side aggressor imbalance."""
    cfg = cfg or SimultaneousFlowConfig()
    if nq.num_rows == 0 or es.num_rows == 0:
        return pa.table({})

    nq_bars = trades_to_tape_bars(nq, bar_ms=cfg.bar_ms)
    es_bars = trades_to_tape_bars(es, bar_ms=cfg.bar_ms)
    if nq_bars.num_rows == 0 or es_bars.num_rows == 0:
        return pa.table({})

    (
        ts,
        nq_imb,
        nq_vol,
        nq_n,
        nq_net,
        es_imb,
        es_vol,
        es_n,
        es_net,
        nq_buy,
        nq_sell,
    ) = _align_bars(nq_bars, es_bars)
    if len(ts) == 0:
        return pa.table({})

    thr = cfg.min_imbalance
    nq_ok = (nq_vol >= cfg.min_vol_nq) & (nq_n >= cfg.min_trades_nq)
    es_ok = (es_vol >= cfg.min_vol_es) & (es_n >= cfg.min_trades_es)
    long_c = (
        nq_ok
        & es_ok
        & (nq_imb >= thr)
        & (es_imb >= thr)
    )
    short_c = (
        nq_ok
        & es_ok
        & (nq_imb <= -thr)
        & (es_imb <= -thr)
    )
    if cfg.min_progress_ticks > 0:
        prog = cfg.min_progress_ticks * cfg.tick_size
        long_c &= (nq_net >= prog) & (es_net > 0)
        short_c &= (nq_net <= -prog) & (es_net < 0)

    raw = long_c | short_c
    keep = np.zeros(len(raw), dtype=bool)
    last = -10**9
    for i in np.where(raw)[0]:
        if i - last >= cfg.cooldown_bars:
            keep[i] = True
            last = i
    idx = np.where(keep)[0]
    if len(idx) == 0:
        return pa.table({})

    side = np.where(long_c[idx], 1, -1).astype(np.int8)
    # Bar end = when the simultaneous bar is known (no look-ahead into the bar).
    bar_ns = int(cfg.bar_ms * 1_000_000)
    event_ts = (ts[idx] + bar_ns).astype(np.int64)
    return pa.table({
        "event_ts_ns": event_ts,
        "side": side,
        "event_type": pa.array(["sim_flow"] * len(idx), type=pa.string()),
        "nq_imbalance": nq_imb[idx],
        "es_imbalance": es_imb[idx],
        "nq_total_vol": nq_vol[idx],
        "es_total_vol": es_vol[idx],
        "nq_n_trades": nq_n[idx],
        "es_n_trades": es_n[idx],
        "nq_net": nq_net[idx],
        "es_net": es_net[idx],
        "nq_buy_vol": nq_buy[idx],
        "nq_sell_vol": nq_sell[idx],
        "bar_ms": np.full(len(idx), cfg.bar_ms, dtype=np.int32),
        "es_lag_ms": np.zeros(len(idx), dtype=np.float64),  # same bucket
    })


def dual_range_imbalance_events(
    nq: pa.Table,
    es: pa.Table,
    *,
    range_ticks: int = 22,
    sync_ms: int = 250,
    mode: str = "imb_opp_low",
    min_imbalance: float = 0.30,
    avg_n: int = 20,
    tick_size: float = 1.0,
) -> pa.Table:
    """NQ range-imbalance with same-side ES within ±sync_ms.

    Stamp = last input used: ``event_ts_ns = max(nq_close, es_close)`` so
    NQ-first closes are not labelled mid-impulse before ES confirms.
    """
    if nq.num_rows == 0 or es.num_rows == 0:
        return pa.table({})

    def _ev(trades: pa.Table) -> pa.Table:
        bars = trades_to_enriched_range_bars(
            trades, range_ticks=range_ticks, tick_size=tick_size
        )
        if bars.num_rows == 0:
            return pa.table({})
        bars = annotate_vs_prior(bars, avg_n=avg_n)
        return range_imbalance_events(
            bars,
            RangeImbalanceConfig(
                range_ticks=range_ticks,
                tick_size=tick_size,
                avg_n=avg_n,
                min_imbalance=min_imbalance,
            ),
            mode=mode,
        )

    nq_ev = _ev(nq)
    es_ev = _ev(es)
    if nq_ev.num_rows == 0 or es_ev.num_rows == 0:
        return pa.table({})

    nq_ts = nq_ev.column("event_ts_ns").to_numpy().astype(np.int64)
    nq_side = nq_ev.column("side").to_numpy().astype(np.int8)
    es_ts = es_ev.column("event_ts_ns").to_numpy().astype(np.int64)
    es_side = es_ev.column("side").to_numpy().astype(np.int8)
    order = np.argsort(es_ts)
    es_ts, es_side = es_ts[order], es_side[order]
    window = int(sync_ms * 1_000_000)

    keep: list[int] = []
    lags: list[float] = []
    es_matched: list[int] = []
    for i, (t, s) in enumerate(zip(nq_ts, nq_side)):
        lo = int(np.searchsorted(es_ts, t - window, side="left"))
        hi = int(np.searchsorted(es_ts, t + window, side="right"))
        best_abs = None
        best_signed = None
        best_es = None
        for j in range(lo, hi):
            if es_side[j] != s:
                continue
            signed = int(es_ts[j] - t)
            lag = abs(signed)
            if best_abs is None or lag < best_abs:
                best_abs = lag
                best_signed = signed
                best_es = int(es_ts[j])
        if best_abs is not None and best_es is not None:
            keep.append(i)
            lags.append(best_signed / 1e6)
            es_matched.append(best_es)

    if not keep:
        return pa.table({})
    idx = np.asarray(keep, dtype=np.int64)
    sub = nq_ev.take(idx)
    confirm_ts = np.maximum(nq_ts[idx], np.asarray(es_matched, dtype=np.int64))
    out = sub.set_column(
        sub.schema.get_field_index("event_ts_ns"),
        "event_ts_ns",
        pa.array(confirm_ts, type=pa.int64()),
    )
    if "es_lag_ms" in out.column_names:
        out = out.drop("es_lag_ms")
    out = out.append_column(
        "es_lag_ms", pa.array(np.asarray(lags, dtype=np.float64), type=pa.float64())
    )
    if "event_type" in out.column_names:
        out = out.drop("event_type")
    out = out.append_column(
        "event_type",
        pa.array([f"dual_range_r{range_ticks}"] * len(idx), type=pa.string()),
    )
    return out
