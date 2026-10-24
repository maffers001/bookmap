"""Cumulative ES/NQ tape divergence (EXP-0049 / H40).

Dose-response cousin of CFADE (H29): instead of one 100 ms ES bar against while
NQ still pushes, require rolling W-second signed aggressor imbalance on NQ to
stay with a direction *and* NQ price to progress, while ES imbalance over the
same window is flat/against, sustained for a persistence run.

Construction (causal, dense 100 ms grid):
- Sparse tape bars → dense grid (empty slots = zero delta/vol; last close carried).
- Per market: imb_m(W) = Σdelta / Σtotal_vol over trailing W.
- Activity gate: each market's W-volume ≥ trailing-30-completed-minutes median
  of that market's own W-volume (reuse rolling_quantile_by_minute).
- Empty ES window (vol=0) never counts as flat/against.
- Event stamped at the bar *end* completing persistence; fade side = −d.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from research.features.corr_absorb import rolling_quantile_by_minute


@dataclass
class DivergenceParams:
    bar_ms: int = 100
    window_sec: float = 10.0
    persist_sec: float = 5.0
    nq_min_imb: float = 0.30
    es_imb_max: float = 0.0  # d * es_imb <= this
    min_D: float = 0.30  # d * (nq_imb - es_imb) >= this
    nq_min_progress_pts: float = 2.0
    cooldown_sec: float = 30.0
    activity_lookback_min: int = 30
    activity_warmup_min: int = 5
    activity_q: float = 0.50  # median
    pts_per_tick: float = 0.25
    # Variant switches (mutually exclusive modes for comparators)
    require_es: bool = True  # False → NQ-only twin
    require_progress: bool = True  # False → no-progress twin (progress BELOW thr)
    es_shift_sec: float = 0.0  # >0 → shift ES series forward (placebo)


def _bars_to_arrays(bars: pa.Table) -> tuple[np.ndarray, ...]:
    """Return (start_ns, buy_vol, sell_vol, close_ticks) sorted by start."""
    if bars.num_rows == 0:
        z = np.zeros(0, dtype=np.float64)
        return np.zeros(0, dtype=np.int64), z, z, z
    ts = bars.column("ts_ns").to_numpy().astype(np.int64)
    buy = bars.column("buy_vol").to_numpy().astype(np.float64)
    sell = bars.column("sell_vol").to_numpy().astype(np.float64)
    close = bars.column("close").to_numpy().astype(np.float64)
    order = np.argsort(ts, kind="stable")
    return ts[order], buy[order], sell[order], close[order]


def densify_tape(
    bars: pa.Table,
    *,
    bar_ms: int,
    t0_ns: int,
    t1_ns: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Dense 100 ms grid over [t0, t1).

    Returns (grid_start_ns, delta, total_vol, close_ticks).
    Empty slots: delta=0, vol=0; close = last known (or nan until first print).
    """
    bar_ns = int(bar_ms * 1_000_000)
    # Align grid to bar boundaries
    g0 = (int(t0_ns) // bar_ns) * bar_ns
    if g0 < t0_ns:
        g0 += bar_ns
    g1 = (int(t1_ns) // bar_ns) * bar_ns
    if g1 <= g0:
        z = np.zeros(0, dtype=np.float64)
        return np.zeros(0, dtype=np.int64), z, z, z
    n = int((g1 - g0) // bar_ns)
    grid = g0 + np.arange(n, dtype=np.int64) * bar_ns
    delta = np.zeros(n, dtype=np.float64)
    vol = np.zeros(n, dtype=np.float64)
    close = np.full(n, np.nan, dtype=np.float64)

    ts, buy, sell, cl = _bars_to_arrays(bars)
    if len(ts) == 0:
        return grid, delta, vol, close

    # Map sparse bars onto grid indices
    idx = ((ts - g0) // bar_ns).astype(np.int64)
    ok = (idx >= 0) & (idx < n)
    for i, b, s, c in zip(idx[ok], buy[ok], sell[ok], cl[ok]):
        delta[i] += b - s
        vol[i] += b + s
        close[i] = c

    # Carry forward last close
    last = np.nan
    for i in range(n):
        if np.isfinite(close[i]):
            last = close[i]
        else:
            close[i] = last
    return grid, delta, vol, close


def _rolling_sum(x: np.ndarray, w: int) -> np.ndarray:
    """Causal trailing sum of last ``w`` bars ending at i (inclusive)."""
    if len(x) == 0:
        return x.copy()
    c = np.cumsum(x, dtype=np.float64)
    out = c.copy()
    if w < len(x):
        out[w:] = c[w:] - c[:-w]
    # bars before a full window still use partial sum (activity gate handles warmup)
    return out


def _shift_series(arr: np.ndarray, shift_bars: int) -> np.ndarray:
    """Shift series forward by ``shift_bars`` (later values come from earlier).

    Positive shift: arr[i] <- arr[i - shift] (ES "from the past" at current i —
    wait: placebo wants to destroy synchrony. Shift ES *forward in time* means
    the ES value at t is what was at t - shift (look-back), OR ES at t is what
    will be at t+shift. Plan says "ES series shifted +60 s". Convention:
    es_dense[i] = original[i - shift] (use past ES as "current") — destroys
    contemporaneous agreement while keeping marginals.
    """
    if shift_bars <= 0 or len(arr) == 0:
        return arr.copy()
    out = np.full_like(arr, np.nan if arr.dtype.kind == "f" else 0)
    if shift_bars < len(arr):
        out[shift_bars:] = arr[:-shift_bars]
    # For float close, carry first known; for delta/vol leave leading zeros/nans
    if arr.dtype.kind == "f" and np.isnan(out).any():
        # leave leading nans for close; zeros already for delta/vol if we fill 0
        pass
    return out


def _activity_median_of_window_vol(
    grid_ns: np.ndarray,
    window_vol: np.ndarray,
    *,
    lookback_min: int,
    warmup_min: int,
    q: float,
) -> np.ndarray:
    """Per-bar causal median of trailing completed-minute window volumes."""
    return rolling_quantile_by_minute(
        grid_ns,
        window_vol,
        q,
        lookback_min=lookback_min,
        warmup_min=warmup_min,
    )


def divergence_fade_events(
    nq_bars: pa.Table,
    es_bars: pa.Table,
    *,
    t0_ns: int,
    t1_ns: int,
    p: DivergenceParams,
) -> pa.Table:
    """Emit fade events for one session window.

    ``t0_ns``/``t1_ns`` = UK RTH bounds (inclusive start, exclusive-ish end —
    densify uses [t0, t1)).
    """
    bar_ns = int(p.bar_ms * 1_000_000)
    w_bars = max(1, int(round(p.window_sec * 1000.0 / p.bar_ms)))
    persist_bars = max(0, int(round(p.persist_sec * 1000.0 / p.bar_ms)))
    cool_bars = max(1, int(round(p.cooldown_sec * 1000.0 / p.bar_ms)))
    shift_bars = int(round(p.es_shift_sec * 1000.0 / p.bar_ms)) if p.es_shift_sec > 0 else 0

    nq_g, nq_d, nq_v, nq_c = densify_tape(nq_bars, bar_ms=p.bar_ms, t0_ns=t0_ns, t1_ns=t1_ns)
    es_g, es_d, es_v, es_c = densify_tape(es_bars, bar_ms=p.bar_ms, t0_ns=t0_ns, t1_ns=t1_ns)
    if len(nq_g) == 0:
        return pa.table({})

    # Align ES to NQ grid (same densify bounds → same grid; assert then proceed)
    if len(es_g) != len(nq_g) or (len(es_g) and not np.array_equal(es_g, nq_g)):
        # Reindex ES onto NQ grid via searchsorted (should be rare)
        es_d2 = np.zeros(len(nq_g), dtype=np.float64)
        es_v2 = np.zeros(len(nq_g), dtype=np.float64)
        es_c2 = np.full(len(nq_g), np.nan, dtype=np.float64)
        if len(es_g):
            j = np.searchsorted(es_g, nq_g, side="left")
            for i, ji in enumerate(j):
                if ji < len(es_g) and es_g[ji] == nq_g[i]:
                    es_d2[i] = es_d[ji]
                    es_v2[i] = es_v[ji]
                    es_c2[i] = es_c[ji]
            last = np.nan
            for i in range(len(es_c2)):
                if np.isfinite(es_c2[i]):
                    last = es_c2[i]
                else:
                    es_c2[i] = last
        es_d, es_v, es_c = es_d2, es_v2, es_c2

    if shift_bars > 0:
        es_d = _shift_series(es_d, shift_bars)
        es_v = _shift_series(es_v, shift_bars)
        # leading zeros for vol/delta; for close use forward-fill after shift
        es_c_s = _shift_series(es_c, shift_bars)
        last = np.nan
        for i in range(len(es_c_s)):
            if np.isfinite(es_c_s[i]):
                last = es_c_s[i]
            es_c_s[i] = last if not np.isfinite(es_c_s[i]) else es_c_s[i]
        es_c = es_c_s
        # shifted leading entries that were nan→0 for delta/vol: force 0
        es_d = np.nan_to_num(es_d, nan=0.0)
        es_v = np.nan_to_num(es_v, nan=0.0)

    nq_w_delta = _rolling_sum(nq_d, w_bars)
    nq_w_vol = _rolling_sum(nq_v, w_bars)
    es_w_delta = _rolling_sum(es_d, w_bars)
    es_w_vol = _rolling_sum(es_v, w_bars)

    nq_imb = np.divide(nq_w_delta, nq_w_vol, out=np.zeros_like(nq_w_delta), where=nq_w_vol > 0)
    es_imb = np.divide(es_w_delta, es_w_vol, out=np.zeros_like(es_w_delta), where=es_w_vol > 0)

    # NQ progress in points over the window: close[i] - close[i - w_bars + 1].
    # Close is in tick units.
    n = len(nq_g)
    nq_prog_ticks = np.full(n, np.nan, dtype=np.float64)
    if n > 0:
        # start close: for i >= w_bars-1 use close[i-w_bars+1]; else close[0]
        start_close = np.empty(n, dtype=np.float64)
        start_close[: w_bars - 1] = nq_c[0] if n else np.nan
        if w_bars <= n:
            start_close[w_bars - 1 :] = nq_c[: n - (w_bars - 1)]
        okc = np.isfinite(nq_c) & np.isfinite(start_close)
        nq_prog_ticks[okc] = nq_c[okc] - start_close[okc]
    nq_prog_pts = nq_prog_ticks * p.pts_per_tick

    nq_act = _activity_median_of_window_vol(
        nq_g, nq_w_vol,
        lookback_min=p.activity_lookback_min,
        warmup_min=p.activity_warmup_min,
        q=p.activity_q,
    )
    es_act = _activity_median_of_window_vol(
        nq_g, es_w_vol,  # same grid timestamps
        lookback_min=p.activity_lookback_min,
        warmup_min=p.activity_warmup_min,
        q=p.activity_q,
    )

    # Need a full window of bars before evaluating
    min_i = w_bars - 1
    # Activity warmup: NaN until warmup_min completed minutes
    active = (
        (np.arange(n) >= min_i)
        & np.isfinite(nq_act)
        & (nq_w_vol >= nq_act)
        & np.isfinite(nq_c)
    )
    if p.require_es:
        active &= np.isfinite(es_act) & (es_w_vol >= es_act) & (es_w_vol > 0)

    # Condition at each bar (before persistence)
    # d = sign(nq_imb); require |nq_imb| meaningful
    d = np.sign(nq_imb).astype(np.int8)
    d[d == 0] = 0
    nq_ok = (d != 0) & (d * nq_imb >= p.nq_min_imb)
    if p.require_progress:
        prog_ok = nq_ok & np.isfinite(nq_prog_pts) & (d * nq_prog_pts >= p.nq_min_progress_pts)
    else:
        # no-progress twin: NQ imbalance ok but progress BELOW threshold
        prog_ok = nq_ok & (
            (~np.isfinite(nq_prog_pts))
            | (d * nq_prog_pts < p.nq_min_progress_pts)
        )

    if p.require_es:
        es_ok = (d * es_imb <= p.es_imb_max) & (es_w_vol > 0)
        D = d * (nq_imb - es_imb)
        d_ok = D >= p.min_D
        cond = active & prog_ok & es_ok & d_ok
    else:
        # NQ-only: no ES / D conditions
        D = d * nq_imb  # diagnostic only
        cond = active & prog_ok

    # Persistence: cond must hold for persist_bars consecutive bars ending at i.
    # persist_sec=0 → single bar (persist_bars=0): treat as 1 bar required.
    need = max(1, persist_bars)
    run = np.zeros(n, dtype=np.int32)
    for i in range(n):
        if cond[i]:
            run[i] = (run[i - 1] + 1) if i > 0 else 1
        else:
            run[i] = 0
    fire = run >= need

    # Cooldown: keep first of a cluster, then skip cool_bars
    keep_idx: list[int] = []
    last = -10**9
    for i in np.where(fire)[0]:
        if i - last >= cool_bars:
            keep_idx.append(int(i))
            last = int(i)

    if not keep_idx:
        return pa.table({})

    idx = np.asarray(keep_idx, dtype=np.int64)
    # Stamp at bar *end* (grid start + bar_ms)
    event_ts = (nq_g[idx] + bar_ns).astype(np.int64)
    side = (-d[idx]).astype(np.int8)  # fade
    mode = "dual"
    if not p.require_es:
        mode = "nq_only"
    elif not p.require_progress:
        mode = "no_progress"
    if p.es_shift_sec > 0:
        mode = "shifted_es"

    return pa.table({
        "event_ts_ns": event_ts,
        "side": side,
        "event_type": [f"corr_div_{mode}"] * len(idx),
        "push_side": d[idx].astype(np.int8),
        "nq_imb_w": nq_imb[idx],
        "es_imb_w": es_imb[idx],
        "D": D[idx],
        "nq_progress_pts": nq_prog_pts[idx],
        "nq_vol_w": nq_w_vol[idx],
        "es_vol_w": es_w_vol[idx],
        "persist_bars": run[idx].astype(np.int32),
        "window_sec": np.full(len(idx), p.window_sec, dtype=np.float64),
        "persist_sec": np.full(len(idx), p.persist_sec, dtype=np.float64),
        "nq_close_ticks": nq_c[idx],
    })


def make_params(base: DivergenceParams, **over) -> DivergenceParams:
    d = {**base.__dict__, **over}
    return DivergenceParams(**d)
