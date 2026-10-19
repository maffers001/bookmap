"""Correlated absorption (EXP-0035 / H28).

Absorption bar = heavy one-sided aggression that fails to move price. This is the
inverse of the EXP-0008 progress gate (burst = aggression *with* progress).

ES and NQ are different tapes: ES 100 ms bar volume is ~3-4x NQ, ES trade
counts ~2x, and ES price barely moves inside 100 ms (|net| <= 1 tick on ~99% of
ES bars vs ~70% of NQ bars). Fixed thresholds therefore mean different things
per market. Everything here is **per-market and adaptive**:

- volume / trade-count thresholds are causal rolling quantiles of that market's
  own bars (trailing ``lookback_min`` of *completed* minutes), so "heavy" is
  heavy relative to that tape at that time of day;
- "no progress" is a per-market tick threshold (``nq_flat_ticks``,
  ``es_flat_ticks``) — default NQ <= 1 tick, ES == 0 ticks (unchanged);
- optional ``min_rel_vol`` requires the bar to be at least k x the rolling
  median of its own market, for a second scale-free check.

Dual event: NQ and ES absorption bars with the **same aggressor side** within
``sync_ms``; stamp = max(bar ends); trade side = against the aggressor (fade).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass
class AbsorbParams:
    bar_ms: int = 100
    # Adaptive volume / trade-count thresholds (per market)
    vol_q: float = 0.95  # bar total_vol >= rolling quantile q of own market
    ntr_q: float = 0.0  # bar n_trades >= rolling quantile (0 = off)
    lookback_min: int = 30  # trailing window of completed minutes
    warmup_min: int = 5  # need this many completed minutes before emitting
    min_rel_vol: float = 0.0  # also require vol >= k * rolling median (0 = off)
    # Absolute floors so a dead tape cannot produce "heavy" bars
    nq_abs_min_vol: float = 10.0
    es_abs_min_vol: float = 20.0
    # Aggression
    min_imbalance: float = 0.55
    # "No progress" thresholds in own-market ticks
    nq_flat_ticks: float = 1.0
    es_flat_ticks: float = 0.0
    # Range cap (high-low) in own-market ticks; 0 = off
    nq_max_range_ticks: float = 0.0
    es_max_range_ticks: float = 0.0
    # Dual match
    sync_ms: int = 250
    # Suppress repeats inside this many bars per market
    cooldown_bars: int = 3


def _minute_index(ts_ns: np.ndarray) -> np.ndarray:
    return (ts_ns // 60_000_000_000).astype(np.int64)


def rolling_quantile_by_minute(
    ts_ns: np.ndarray,
    x: np.ndarray,
    q: float,
    *,
    lookback_min: int,
    warmup_min: int,
) -> np.ndarray:
    """Causal per-bar threshold: quantile ``q`` of ``x`` over the trailing
    ``lookback_min`` *completed* minutes (the current minute is excluded).
    NaN until ``warmup_min`` completed minutes exist in the window."""
    n = len(ts_ns)
    out = np.full(n, np.nan)
    if n == 0:
        return out
    mins = _minute_index(ts_ns)
    uniq, start = np.unique(mins, return_index=True)
    ends = np.append(start[1:], n)
    # per-minute slices (bars are time-sorted)
    for k in range(len(uniq)):
        m = uniq[k]
        lo_min = m - lookback_min
        # completed minutes in (m - lookback, m)
        sel = np.where((uniq >= lo_min) & (uniq < m))[0]
        if len(sel) < warmup_min:
            continue
        vals = np.concatenate([x[start[j]:ends[j]] for j in sel])
        if len(vals) < 20:
            continue
        out[start[k]:ends[k]] = np.quantile(vals, q)
    return out


def absorption_bars(
    bars: pa.Table,
    p: AbsorbParams,
    *,
    market: str,
) -> pa.Table:
    """Single-market absorption bars. ``market`` in {"NQ", "ES"} selects the
    per-market flat/range/floor parameters. Event stamped at bar end."""
    if bars.num_rows == 0:
        return pa.table({})
    ts = bars.column("ts_ns").to_numpy().astype(np.int64)
    vol = bars.column("total_vol").to_numpy().astype(np.float64)
    ntr = bars.column("n_trades").to_numpy().astype(np.float64)
    imb = bars.column("imbalance").to_numpy().astype(np.float64)
    net = np.abs(bars.column("net").to_numpy().astype(np.float64))
    rng = (bars.column("high").to_numpy() - bars.column("low").to_numpy()).astype(np.float64)
    mx = bars.column("max_print").to_numpy().astype(np.float64)
    bar_ns = int(p.bar_ms * 1_000_000)

    if market == "ES":
        flat, max_rng, abs_floor = p.es_flat_ticks, p.es_max_range_ticks, p.es_abs_min_vol
    else:
        flat, max_rng, abs_floor = p.nq_flat_ticks, p.nq_max_range_ticks, p.nq_abs_min_vol

    vol_thr = rolling_quantile_by_minute(
        ts, vol, p.vol_q, lookback_min=p.lookback_min, warmup_min=p.warmup_min
    )
    ok = np.isfinite(vol_thr) & (vol >= vol_thr) & (vol >= abs_floor)
    if p.ntr_q > 0:
        ntr_thr = rolling_quantile_by_minute(
            ts, ntr, p.ntr_q, lookback_min=p.lookback_min, warmup_min=p.warmup_min
        )
        ok &= np.isfinite(ntr_thr) & (ntr >= ntr_thr)
    med = None
    if p.min_rel_vol > 0:
        med = rolling_quantile_by_minute(
            ts, vol, 0.5, lookback_min=p.lookback_min, warmup_min=p.warmup_min
        )
        ok &= np.isfinite(med) & (vol >= p.min_rel_vol * np.maximum(med, 1.0))
    ok &= np.abs(imb) >= p.min_imbalance
    ok &= net <= flat
    if max_rng > 0:
        ok &= rng <= max_rng

    idx = np.where(ok)[0]
    if len(idx) == 0:
        return pa.table({})
    if p.cooldown_bars > 0:
        keep = []
        last = -10**9
        for i in idx:
            if i - last > p.cooldown_bars:
                keep.append(i)
                last = i
        idx = np.asarray(keep, dtype=np.int64)

    aggr = np.where(imb[idx] > 0, 1, -1).astype(np.int8)
    return pa.table({
        "event_ts_ns": (ts[idx] + bar_ns).astype(np.int64),
        "side": (-aggr).astype(np.int8),  # fade the aggressor
        "aggr_side": aggr,
        "event_type": [f"absorb_{market.lower()}"] * len(idx),
        "total_vol": vol[idx],
        "vol_thr": vol_thr[idx],
        "vol_rel": vol[idx] / np.maximum(vol_thr[idx], 1e-9),
        "n_trades": ntr[idx],
        "imbalance": imb[idx],
        "net_ticks": net[idx],
        "range_ticks": rng[idx],
        "max_print": mx[idx],
        "close_ticks": bars.column("close").to_numpy().astype(np.float64)[idx],
    })


def level_contact_flags(
    events: pa.Table,
    contacts: pa.Table | None,
    *,
    bar_ms: int,
    radius_ticks: int,
    pre_ms: int = 1000,
) -> np.ndarray:
    """Depth stratum: 1 if an NQ tracked large level on the *aggressed* side
    (buy aggressor -> ASK level) was contacted within [bar_start - pre, bar_end]
    and lies within ``radius_ticks`` of the bar close. 0 otherwise; -1 if no
    depth available."""
    n = events.num_rows
    if contacts is None or contacts.num_rows == 0:
        return np.full(n, -1, dtype=np.int8)
    c_ts = contacts.column("contact_ns").to_numpy().astype(np.int64)
    c_side = contacts.column("level_side").to_numpy(zero_copy_only=False)
    c_px = contacts.column("price_ticks").to_numpy().astype(np.int64)
    order = np.argsort(c_ts)
    c_ts, c_side, c_px = c_ts[order], c_side[order], c_px[order]

    ev_end = events.column("event_ts_ns").to_numpy().astype(np.int64)
    aggr = events.column("aggr_side").to_numpy().astype(int)
    close = events.column("close_ticks").to_numpy().astype(np.float64)
    out = np.zeros(n, dtype=np.int8)
    bar_ns = int(bar_ms * 1_000_000)
    pre = int(pre_ms * 1_000_000)
    for i in range(n):
        lo = int(np.searchsorted(c_ts, ev_end[i] - bar_ns - pre, side="left"))
        hi = int(np.searchsorted(c_ts, ev_end[i], side="right"))
        want = "ASK" if aggr[i] > 0 else "BID"
        for j in range(lo, hi):
            if c_side[j] == want and abs(c_px[j] - close[i]) <= radius_ticks:
                out[i] = 1
                break
    return out
