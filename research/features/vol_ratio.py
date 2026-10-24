"""Cross-market NQ/ES aggressor-volume ratio shock (EXP-0051 / H42).

Tier A / A1 (CORRELATED_IDEAS S2.7): an NQ volume spike is market-wide when
ES volume spikes with it (ratio normal) and NQ-specific when the ratio
itself is shocked. Causal construction:

- Dense 1 s grid over the session window; per-second aggressor volume =
  sum of trade ``size`` in that second (trades are aggressor prints).
- Trailing ``window_sec`` (10 s) volume per market ending at each completed
  second; ``ratio = vol_NQ / vol_ES``, ``log_ratio = log(ratio)``.
- Robust z of ``log_ratio`` against the trailing ``baseline_lookback_min``
  *completed* minutes of the same session (median / IQR, per-minute steps,
  current minute excluded). This session-causal trailing z replaces the
  literal cross-day minute-of-day z of S2.7 a priori (live-feasible, no
  cross-session leakage; EXP-0049 deviation precedent).
- ``z`` is NaN (warmup) until ``baseline_warmup_min`` completed minutes with
  finite log-ratio exist, or the trailing IQR is zero. ``ratio`` is NaN
  where ES 10 s volume is 0; ``log_ratio`` also NaN where NQ volume is 0.
- Series values are stamped at second *end*; lookup uses the last stamp at
  or before the event stamp (only completed seconds). No look-ahead.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass
class VolRatioParams:
    grid_sec: int = 1
    window_sec: int = 10
    baseline_lookback_min: int = 30
    baseline_warmup_min: int = 5
    shock_z: float = 1.0
    nq_spike_mult: float = 2.0


@dataclass
class VolRatioSeries:
    """Dense 1 s volume-ratio series for one session window."""

    end_ns: np.ndarray  # second end (grid start + grid_sec); length N
    ratio: np.ndarray  # vol_NQ / vol_ES over trailing window; NaN if ES vol 0
    log_ratio: np.ndarray
    z: np.ndarray  # robust trailing z of log_ratio; NaN in warmup
    vol_nq: np.ndarray  # trailing window volume
    vol_es: np.ndarray
    nq_spike: np.ndarray  # bool: vol_nq >= mult * trailing-median vol_nq
    t0_ns: int
    t1_ns: int
    window_sec: int


def _vol_per_second(
    trades: pa.Table,
    *,
    t0_ns: int,
    t1_ns: int,
    grid_sec: int = 1,
) -> tuple[np.ndarray, np.ndarray]:
    """Dense per-second total trade size over [t0, t1). Returns (grid_start, vol)."""
    sec_ns = int(grid_sec) * 1_000_000_000
    g0 = (int(t0_ns) // sec_ns) * sec_ns
    if g0 < t0_ns:
        g0 += sec_ns
    g1 = (int(t1_ns) // sec_ns) * sec_ns
    if g1 <= g0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float64)
    n = int((g1 - g0) // sec_ns)
    grid = g0 + np.arange(n, dtype=np.int64) * sec_ns
    vol = np.zeros(n, dtype=np.float64)
    if trades.num_rows == 0:
        return grid, vol
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    size = trades.column("size").to_numpy().astype(np.float64)
    order = np.argsort(ts, kind="stable")
    ts, size = ts[order], size[order]
    in_win = (ts >= g0) & (ts < g1)
    ts, size = ts[in_win], size[in_win]
    if len(ts) == 0:
        return grid, vol
    idx = ((ts - g0) // sec_ns).astype(np.int64)
    ok = (idx >= 0) & (idx < n)
    np.add.at(vol, idx[ok], size[ok])
    return grid, vol


def _rolling_sum(x: np.ndarray, w: int) -> np.ndarray:
    """Trailing sum of last ``w`` entries ending at i (inclusive); partial at head."""
    if len(x) == 0:
        return x.copy()
    c = np.cumsum(x, dtype=np.float64)
    out = c.copy()
    if w < len(x):
        out[w:] = c[w:] - c[:-w]
    return out


def _trailing_minute_stats(
    grid_start_ns: np.ndarray,
    x: np.ndarray,
    *,
    lookback_min: int,
    warmup_min: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-index (median, q25, q75) of finite ``x`` over trailing completed minutes.

    Constant within each minute; the current minute is excluded, so values
    at minute m use minutes in [m - lookback, m). NaN until ``warmup_min``
    minutes containing finite values exist in the window (and >= 20 finite
    values total).
    """
    n = len(x)
    med = np.full(n, np.nan)
    q25 = np.full(n, np.nan)
    q75 = np.full(n, np.nan)
    if n == 0:
        return med, q25, q75
    mins = (grid_start_ns // 60_000_000_000).astype(np.int64)
    uniq, start = np.unique(mins, return_index=True)
    ends = np.append(start[1:], n)
    for k in range(len(uniq)):
        m = uniq[k]
        sel = np.where((uniq >= m - lookback_min) & (uniq < m))[0]
        finite_minutes = 0
        chunks = []
        for j in sel:
            v = x[start[j]:ends[j]]
            v = v[np.isfinite(v)]
            if len(v):
                finite_minutes += 1
                chunks.append(v)
        if finite_minutes < warmup_min or not chunks:
            continue
        vals = np.concatenate(chunks)
        if len(vals) < 20:
            continue
        med[start[k]:ends[k]] = np.median(vals)
        q25[start[k]:ends[k]] = np.quantile(vals, 0.25)
        q75[start[k]:ends[k]] = np.quantile(vals, 0.75)
    return med, q25, q75


def vol_ratio_series(
    nq: pa.Table,
    es: pa.Table,
    *,
    t0_ns: int,
    t1_ns: int,
    p: VolRatioParams | None = None,
) -> VolRatioSeries:
    """Build the causal volume-ratio series over the session window."""
    p = p or VolRatioParams()
    sec_ns = int(p.grid_sec) * 1_000_000_000
    nq_g, nq_v1 = _vol_per_second(nq, t0_ns=t0_ns, t1_ns=t1_ns, grid_sec=p.grid_sec)
    es_g, es_v1 = _vol_per_second(es, t0_ns=t0_ns, t1_ns=t1_ns, grid_sec=p.grid_sec)
    if len(nq_g) == 0:
        z = np.zeros(0, dtype=np.float64)
        return VolRatioSeries(
            end_ns=np.zeros(0, dtype=np.int64), ratio=z, log_ratio=z.copy(),
            z=z.copy(), vol_nq=z.copy(), vol_es=z.copy(),
            nq_spike=np.zeros(0, dtype=bool),
            t0_ns=t0_ns, t1_ns=t1_ns, window_sec=int(p.window_sec),
        )
    if len(es_g) != len(nq_g) or (len(es_g) and not np.array_equal(es_g, nq_g)):
        es_v = np.zeros(len(nq_g), dtype=np.float64)
        if len(es_g):
            j = np.searchsorted(es_g, nq_g, side="left")
            for i, ji in enumerate(j):
                if ji < len(es_g) and es_g[ji] == nq_g[i]:
                    es_v[i] = es_v1[ji]
        es_v1 = es_v

    w = max(1, int(round(p.window_sec / p.grid_sec)))
    vol_nq = _rolling_sum(nq_v1, w)
    vol_es = _rolling_sum(es_v1, w)

    ratio = np.divide(
        vol_nq, vol_es,
        out=np.full(len(nq_g), np.nan), where=vol_es > 0,
    )
    log_ratio = np.full(len(nq_g), np.nan)
    pos = (vol_nq > 0) & (vol_es > 0)
    log_ratio[pos] = np.log(vol_nq[pos] / vol_es[pos])

    med, q25, q75 = _trailing_minute_stats(
        nq_g, log_ratio,
        lookback_min=p.baseline_lookback_min,
        warmup_min=p.baseline_warmup_min,
    )
    sigma = (q75 - q25) / 1.349
    z = np.full(len(nq_g), np.nan)
    ok = np.isfinite(log_ratio) & np.isfinite(med) & np.isfinite(sigma) & (sigma > 1e-12)
    z[ok] = (log_ratio[ok] - med[ok]) / sigma[ok]

    med_v, _, _ = _trailing_minute_stats(
        nq_g, vol_nq,
        lookback_min=p.baseline_lookback_min,
        warmup_min=p.baseline_warmup_min,
    )
    nq_spike = np.isfinite(med_v) & (vol_nq >= float(p.nq_spike_mult) * med_v)

    return VolRatioSeries(
        end_ns=(nq_g + sec_ns).astype(np.int64),
        ratio=ratio,
        log_ratio=log_ratio,
        z=z,
        vol_nq=vol_nq,
        vol_es=vol_es,
        nq_spike=nq_spike,
        t0_ns=int(t0_ns),
        t1_ns=int(t1_ns),
        window_sec=int(p.window_sec),
    )


def vol_ratio_at(
    series: VolRatioSeries,
    event_ts_ns: int | np.ndarray,
) -> dict[str, np.ndarray | float]:
    """Last completed-second values at/before the event stamp (causal).

    Returns dict of arrays (or floats for a scalar event) with keys
    ``z``, ``ratio``, ``vol_nq``, ``vol_es``, ``nq_spike`` (float 0/1/NaN).
    """
    scalar = np.isscalar(event_ts_ns) or isinstance(event_ts_ns, (int, np.integer))
    ts = np.atleast_1d(np.asarray(event_ts_ns, dtype=np.int64))
    out: dict[str, np.ndarray] = {}
    if len(series.end_ns) == 0:
        for k in ("z", "ratio", "vol_nq", "vol_es", "nq_spike"):
            out[k] = np.full(len(ts), np.nan)
    else:
        idx = np.searchsorted(series.end_ns, ts, side="right") - 1
        valid = idx >= 0
        safe = np.where(valid, idx, 0)
        for k, arr in (
            ("z", series.z), ("ratio", series.ratio),
            ("vol_nq", series.vol_nq), ("vol_es", series.vol_es),
        ):
            v = np.full(len(ts), np.nan)
            v[valid] = arr[safe[valid]]
            out[k] = v
        sp = np.full(len(ts), np.nan)
        sp[valid] = series.nq_spike[safe[valid]].astype(np.float64)
        out["nq_spike"] = sp
    if scalar:
        return {k: float(v[0]) for k, v in out.items()}
    return out


def vol_ratio_handcheck(
    nq: pa.Table,
    es: pa.Table,
    *,
    t0_ns: int,
    event_ts_ns: int,
    p: VolRatioParams | None = None,
) -> dict:
    """Recompute the window ratio at ``event_ts_ns`` directly from raw trades."""
    p = p or VolRatioParams()
    sec_ns = int(p.grid_sec) * 1_000_000_000
    series = vol_ratio_series(nq, es, t0_ns=t0_ns, t1_ns=int(event_ts_ns) + sec_ns, p=p)
    got = vol_ratio_at(series, int(event_ts_ns))
    if len(series.end_ns) == 0:
        return {"ratio_at": float("nan"), "ratio_direct": float("nan"),
                "match": True, "z_at": float("nan")}
    i = int(np.searchsorted(series.end_ns, int(event_ts_ns), side="right") - 1)
    if i < 0:
        return {"ratio_at": float("nan"), "ratio_direct": float("nan"),
                "match": True, "z_at": float("nan")}
    end = int(series.end_ns[i])
    w_start = end - int(p.window_sec) * sec_ns

    def _vol(t: pa.Table) -> float:
        if t.num_rows == 0:
            return 0.0
        ts = t.column("ts_ns").to_numpy().astype(np.int64)
        size = t.column("size").to_numpy().astype(np.float64)
        k = (ts >= w_start) & (ts < end)
        return float(size[k].sum()) if k.any() else 0.0

    vn, ve = _vol(nq), _vol(es)
    direct = (vn / ve) if ve > 0 else float("nan")
    got_r = float(got["ratio"])  # type: ignore[arg-type]
    both_nan = (not np.isfinite(got_r)) and (not np.isfinite(direct))
    match = both_nan or (
        np.isfinite(got_r) and np.isfinite(direct) and abs(got_r - direct) < 1e-9
    )
    return {
        "ratio_at": got_r,
        "ratio_direct": direct,
        "vol_nq": vn,
        "vol_es": ve,
        "z_at": float(got["z"]),  # type: ignore[arg-type]
        "match": bool(match),
        "i": i,
    }
