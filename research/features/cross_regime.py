"""Cross-market ES/NQ return-correlation regime (EXP-0050 / H41).

Causal rolling Pearson rho of ES vs NQ 1-second close-to-close returns on a
dense 1 s grid. Used as a coupling-state gate on frozen CFADE (and as a
descriptive stratum on MAT).

Grid rules (mirror the EXP-0049 densify pattern, at 1 s):
- Last trade price per second, carried forward; empty seconds contribute a
  zero return once a prior price exists.
- Rho at second ``i`` uses the trailing ``window_sec`` completed seconds
  ending at ``i`` (inclusive). No look-ahead.
- A second is "paired" once both markets have traded at least once up to it.
- Rho is NaN until ``min_obs`` paired seconds exist in the window, or either
  series has zero variance in the window.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass
class CorrGateParams:
    grid_sec: int = 1
    window_sec: int = 300
    min_obs: int = 120
    skip_below: float = 0.30


@dataclass
class RhoSeries:
    """Dense 1 s rho series for one session window."""

    end_ns: np.ndarray  # window-end second start (ns); length N
    rho: np.ndarray  # float64, NaN where undefined
    n_paired: np.ndarray  # paired seconds in the window at each end
    t0_ns: int
    t1_ns: int
    window_sec: int
    min_obs: int


def _last_price_per_second(
    trades: pa.Table,
    *,
    t0_ns: int,
    t1_ns: int,
    grid_sec: int = 1,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Dense last-price grid over [t0, t1).

    Returns (grid_start_ns, close_ticks, has_print).
    ``has_print[i]`` is True if ≥1 trade fell in that second (before carry).
    Empty slots: close carried from last known; has_print stays False.
    """
    sec_ns = int(grid_sec) * 1_000_000_000
    g0 = (int(t0_ns) // sec_ns) * sec_ns
    if g0 < t0_ns:
        g0 += sec_ns
    g1 = (int(t1_ns) // sec_ns) * sec_ns
    if g1 <= g0:
        z = np.zeros(0, dtype=np.float64)
        return np.zeros(0, dtype=np.int64), z, np.zeros(0, dtype=bool)
    n = int((g1 - g0) // sec_ns)
    grid = g0 + np.arange(n, dtype=np.int64) * sec_ns
    close = np.full(n, np.nan, dtype=np.float64)
    has = np.zeros(n, dtype=bool)

    if trades.num_rows == 0:
        return grid, close, has

    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64)
    order = np.argsort(ts, kind="stable")
    ts, px = ts[order], px[order]
    # Keep only trades inside [g0, g1)
    in_win = (ts >= g0) & (ts < g1)
    ts, px = ts[in_win], px[in_win]
    if len(ts) == 0:
        return grid, close, has

    idx = ((ts - g0) // sec_ns).astype(np.int64)
    # Last trade wins within each second (ts already sorted)
    for i, c in zip(idx, px):
        if 0 <= i < n:
            close[i] = c
            has[i] = True

    last = np.nan
    for i in range(n):
        if np.isfinite(close[i]):
            last = close[i]
        else:
            close[i] = last
    return grid, close, has


def _returns_from_close(close: np.ndarray) -> np.ndarray:
    """Close-to-close returns; 0 where either endpoint is NaN (pre-first print)."""
    n = len(close)
    ret = np.zeros(n, dtype=np.float64)
    if n < 2:
        return ret
    # ret[i] = close[i] - close[i-1] once both finite; else 0
    ok = np.isfinite(close[1:]) & np.isfinite(close[:-1])
    ret[1:][ok] = close[1:][ok] - close[:-1][ok]
    return ret


def _rolling_pearson(
    x: np.ndarray,
    y: np.ndarray,
    paired: np.ndarray,
    *,
    window: int,
    min_obs: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Causal rolling Pearson rho using only paired seconds in the window.

    Unpaired seconds contribute nothing (treated as missing). Uses running
    sums over a binary mask so empty/warmup seconds do not inflate n.
    """
    n = len(x)
    rho = np.full(n, np.nan, dtype=np.float64)
    n_paired = np.zeros(n, dtype=np.int32)
    if n == 0 or window < 2:
        return rho, n_paired

    # Masked series: 0 where unpaired; counts via mask
    m = paired.astype(np.float64)
    xm = x * m
    ym = y * m
    x2 = xm * xm
    y2 = ym * ym
    xy = xm * ym

    # Prefix sums (inclusive); pad with a leading 0 for easy window diffs
    def _psum(a: np.ndarray) -> np.ndarray:
        out = np.empty(n + 1, dtype=np.float64)
        out[0] = 0.0
        np.cumsum(a, out=out[1:])
        return out

    cs_m = _psum(m)
    cs_x = _psum(xm)
    cs_y = _psum(ym)
    cs_x2 = _psum(x2)
    cs_y2 = _psum(y2)
    cs_xy = _psum(xy)

    for i in range(n):
        lo = i - window + 1
        if lo < 0:
            lo = 0
        # sums over [lo, i] inclusive → prefix[i+1] - prefix[lo]
        cnt = cs_m[i + 1] - cs_m[lo]
        n_paired[i] = int(cnt)
        if cnt < min_obs:
            continue
        sx = cs_x[i + 1] - cs_x[lo]
        sy = cs_y[i + 1] - cs_y[lo]
        sx2 = cs_x2[i + 1] - cs_x2[lo]
        sy2 = cs_y2[i + 1] - cs_y2[lo]
        sxy = cs_xy[i + 1] - cs_xy[lo]
        # Var = E[x^2] - E[x]^2, but with sum form:
        # cov = sxy - sx*sy/n ; var_x = sx2 - sx^2/n
        cov = sxy - (sx * sy) / cnt
        vx = sx2 - (sx * sx) / cnt
        vy = sy2 - (sy * sy) / cnt
        if vx <= 1e-18 or vy <= 1e-18:
            continue
        rho[i] = cov / np.sqrt(vx * vy)
        # Numerical clamp
        if rho[i] > 1.0:
            rho[i] = 1.0
        elif rho[i] < -1.0:
            rho[i] = -1.0
    return rho, n_paired


def rho_series(
    nq: pa.Table,
    es: pa.Table,
    *,
    t0_ns: int,
    t1_ns: int,
    window_sec: int = 300,
    min_obs: int = 120,
    grid_sec: int = 1,
) -> RhoSeries:
    """Build causal rolling ES/NQ return-correlation over the session window."""
    nq_g, nq_c, nq_has = _last_price_per_second(nq, t0_ns=t0_ns, t1_ns=t1_ns, grid_sec=grid_sec)
    es_g, es_c, es_has = _last_price_per_second(es, t0_ns=t0_ns, t1_ns=t1_ns, grid_sec=grid_sec)

    if len(nq_g) == 0:
        return RhoSeries(
            end_ns=np.zeros(0, dtype=np.int64),
            rho=np.zeros(0, dtype=np.float64),
            n_paired=np.zeros(0, dtype=np.int32),
            t0_ns=t0_ns,
            t1_ns=t1_ns,
            window_sec=window_sec,
            min_obs=min_obs,
        )

    # Align ES onto NQ grid if lengths differ (should match densify bounds)
    if len(es_g) != len(nq_g) or (len(es_g) and not np.array_equal(es_g, nq_g)):
        es_c2 = np.full(len(nq_g), np.nan, dtype=np.float64)
        es_has2 = np.zeros(len(nq_g), dtype=bool)
        if len(es_g):
            j = np.searchsorted(es_g, nq_g, side="left")
            for i, ji in enumerate(j):
                if ji < len(es_g) and es_g[ji] == nq_g[i]:
                    es_c2[i] = es_c[ji]
                    es_has2[i] = es_has[ji]
            last = np.nan
            for i in range(len(es_c2)):
                if np.isfinite(es_c2[i]):
                    last = es_c2[i]
                else:
                    es_c2[i] = last
        es_c, es_has = es_c2, es_has2

    # Paired once both markets have ever printed
    nq_seen = np.cumsum(nq_has.astype(np.int32)) > 0
    es_seen = np.cumsum(es_has.astype(np.int32)) > 0
    paired = nq_seen & es_seen

    nq_ret = _returns_from_close(nq_c)
    es_ret = _returns_from_close(es_c)
    rho, n_p = _rolling_pearson(
        nq_ret, es_ret, paired, window=int(window_sec), min_obs=int(min_obs),
    )
    return RhoSeries(
        end_ns=nq_g,
        rho=rho,
        n_paired=n_p,
        t0_ns=t0_ns,
        t1_ns=t1_ns,
        window_sec=int(window_sec),
        min_obs=int(min_obs),
    )


def rho_at(series: RhoSeries, event_ts_ns: int | np.ndarray) -> np.ndarray | float:
    """Last rho whose window-end second start is ≤ event stamp (causal).

    For a scalar event returns a float; for an array returns float64 ndarray.
    """
    scalar = np.isscalar(event_ts_ns) or (isinstance(event_ts_ns, (int, np.integer)))
    ts = np.atleast_1d(np.asarray(event_ts_ns, dtype=np.int64))
    out = np.full(len(ts), np.nan, dtype=np.float64)
    if len(series.end_ns) == 0:
        return float(out[0]) if scalar else out
    # searchsorted right → last index with end_ns <= t
    idx = np.searchsorted(series.end_ns, ts, side="right") - 1
    ok = idx >= 0
    out[ok] = series.rho[idx[ok]]
    return float(out[0]) if scalar else out


def rho_handcheck(
    nq: pa.Table,
    es: pa.Table,
    *,
    t0_ns: int,
    event_ts_ns: int,
    window_sec: int = 300,
    min_obs: int = 120,
) -> dict:
    """Recompute rho at ``event_ts_ns`` via np.corrcoef for parity with rho_at."""
    series = rho_series(
        nq, es, t0_ns=t0_ns, t1_ns=int(event_ts_ns) + 1,
        window_sec=window_sec, min_obs=min_obs,
    )
    got = rho_at(series, event_ts_ns)
    # Direct corrcoef on the same window ending at last second ≤ event
    if len(series.end_ns) == 0:
        return {"rho_at": got, "corrcoef": float("nan"), "match": True, "n_paired": 0}
    i = int(np.searchsorted(series.end_ns, event_ts_ns, side="right") - 1)
    if i < 0:
        return {"rho_at": got, "corrcoef": float("nan"), "match": True, "n_paired": 0}

    nq_g, nq_c, nq_has = _last_price_per_second(nq, t0_ns=t0_ns, t1_ns=int(event_ts_ns) + 1)
    es_g, es_c, es_has = _last_price_per_second(es, t0_ns=t0_ns, t1_ns=int(event_ts_ns) + 1)
    if len(es_g) != len(nq_g):
        # realign
        es_c2 = np.full(len(nq_g), np.nan)
        es_has2 = np.zeros(len(nq_g), dtype=bool)
        if len(es_g):
            j = np.searchsorted(es_g, nq_g, side="left")
            for ii, ji in enumerate(j):
                if ji < len(es_g) and es_g[ji] == nq_g[ii]:
                    es_c2[ii] = es_c[ji]
                    es_has2[ii] = es_has[ji]
            last = np.nan
            for ii in range(len(es_c2)):
                if np.isfinite(es_c2[ii]):
                    last = es_c2[ii]
                else:
                    es_c2[ii] = last
        es_c, es_has = es_c2, es_has2
    nq_seen = np.cumsum(nq_has.astype(np.int32)) > 0
    es_seen = np.cumsum(es_has.astype(np.int32)) > 0
    paired = nq_seen & es_seen
    nq_ret = _returns_from_close(nq_c)
    es_ret = _returns_from_close(es_c)
    lo = max(0, i - window_sec + 1)
    sl = slice(lo, i + 1)
    mask = paired[sl]
    if mask.sum() < min_obs:
        ref = float("nan")
    else:
        xr, yr = nq_ret[sl][mask], es_ret[sl][mask]
        if xr.std() < 1e-12 or yr.std() < 1e-12:
            ref = float("nan")
        else:
            ref = float(np.corrcoef(xr, yr)[0, 1])
    both_nan = (not np.isfinite(got)) and (not np.isfinite(ref))
    match = both_nan or (np.isfinite(got) and np.isfinite(ref) and abs(got - ref) < 1e-9)
    return {
        "rho_at": float(got) if np.isfinite(got) else float("nan"),
        "corrcoef": ref,
        "match": bool(match),
        "n_paired": int(mask.sum()),
        "i": i,
    }
