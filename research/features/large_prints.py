"""Cross-market large-print clustering (EXP-0053 / H44, Tier A / A3).

Per market, a "large" print is a trade whose size >= that market's causal
large threshold: the ``large_q`` (p99) quantile of its own print sizes over
the trailing ``baseline_lookback_min`` *completed* minutes (current minute
excluded; threshold constant within a minute; undefined until
``baseline_warmup_min`` completed minutes with at least
``min_baseline_prints`` prints exist). A whole-session percentile would
use future prints, so it is never used here.

Feature at each completed second: per-market count of large prints in the
trailing ``window_sec`` seconds. Burst per market = count >=
``burst_min_count``. Strata codes: 0 warmup (either threshold undefined),
1 both, 2 nq_only, 3 es_only, 4 none. Values stamped at second end;
lookup uses only completed seconds (no look-ahead).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

STRATA = {0: "warmup", 1: "both", 2: "nq_only", 3: "es_only", 4: "none"}


@dataclass
class LargePrintParams:
    grid_sec: int = 1
    window_sec: int = 5
    large_q: float = 0.99
    baseline_lookback_min: int = 30
    baseline_warmup_min: int = 5
    min_baseline_prints: int = 100
    burst_min_count: int = 2


@dataclass
class LargePrintSeries:
    end_ns: np.ndarray
    nq_cnt: np.ndarray
    es_cnt: np.ndarray
    nq_thr: np.ndarray
    es_thr: np.ndarray
    stratum: np.ndarray  # int codes in STRATA
    t0_ns: int
    t1_ns: int
    window_sec: int


def _minute_thresholds(
    ts: np.ndarray,
    size: np.ndarray,
    *,
    lookback_min: int,
    warmup_min: int,
    min_prints: int,
    q: float,
) -> dict[int, float]:
    """Causal per-minute large threshold from trailing completed minutes."""
    if len(ts) == 0:
        return {}
    mins = (ts // 60_000_000_000).astype(np.int64)
    uniq, counts = np.unique(mins, return_counts=True)
    by_min: dict[int, np.ndarray] = {}
    pos = 0
    order = np.argsort(ts, kind="stable")
    sorted_size = size[order]
    sorted_mins = mins[order]
    for m, c in zip(uniq, counts):
        seg = sorted_size[pos:pos + int(c)]
        # sorted by ts, minutes are contiguous after sort
        by_min[int(m)] = seg
        pos += int(c)
    out: dict[int, float] = {}
    for m in uniq:
        sel = [by_min[int(x)] for x in uniq
               if (m - lookback_min) <= x < m and len(by_min[int(x)])]
        if len(sel) < warmup_min:
            continue
        vals = np.concatenate(sel) if sel else np.zeros(0)
        if len(vals) < min_prints:
            continue
        out[int(m)] = float(np.quantile(vals, q))
    return out


def _market_counts(
    trades: pa.Table,
    *,
    t0_ns: int,
    t1_ns: int,
    p: LargePrintParams,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (grid_start, large_cnt_window, thr_per_second)."""
    sec_ns = int(p.grid_sec) * 1_000_000_000
    g0 = (int(t0_ns) // sec_ns) * sec_ns
    if g0 < t0_ns:
        g0 += sec_ns
    g1 = (int(t1_ns) // sec_ns) * sec_ns
    if g1 <= g0:
        z = np.zeros(0, dtype=np.float64)
        return np.zeros(0, dtype=np.int64), z, z
    n = int((g1 - g0) // sec_ns)
    grid = g0 + np.arange(n, dtype=np.int64) * sec_ns
    thr_sec = np.full(n, np.nan)
    cnt_sec = np.zeros(n, dtype=np.float64)
    if trades.num_rows == 0:
        return grid, cnt_sec, thr_sec

    ts_all = trades.column("ts_ns").to_numpy().astype(np.int64)
    size_all = trades.column("size").to_numpy().astype(np.float64)
    in_win = (ts_all >= g0) & (ts_all < g1)
    ts, size = ts_all[in_win], size_all[in_win]
    if len(ts) == 0:
        return grid, cnt_sec, thr_sec

    thr_by_min = _minute_thresholds(
        ts, size,
        lookback_min=p.baseline_lookback_min,
        warmup_min=p.baseline_warmup_min,
        min_prints=p.min_baseline_prints,
        q=p.large_q,
    )
    if not thr_by_min:
        return grid, cnt_sec, thr_sec
    mins_sorted = np.array(sorted(thr_by_min), dtype=np.int64)
    thr_sorted = np.array([thr_by_min[int(m)] for m in mins_sorted], dtype=np.float64)

    sec_min = (grid // 60_000_000_000).astype(np.int64)
    j = np.searchsorted(mins_sorted, sec_min, side="left")
    hit = (j < len(mins_sorted)) & (mins_sorted[np.minimum(j, len(mins_sorted) - 1)] == sec_min)
    thr_sec[hit] = thr_sorted[j[hit]]

    trade_min = (ts // 60_000_000_000).astype(np.int64)
    jt = np.searchsorted(mins_sorted, trade_min, side="left")
    hit_t = (jt < len(mins_sorted)) & (mins_sorted[np.minimum(jt, len(mins_sorted) - 1)] == trade_min)
    thr_trade = np.full(len(ts), np.nan)
    thr_trade[hit_t] = thr_sorted[jt[hit_t]]
    large = hit_t & np.isfinite(thr_trade) & (size >= thr_trade)
    if large.any():
        sec_idx = ((ts[large] - g0) // sec_ns).astype(np.int64)
        ok = (sec_idx >= 0) & (sec_idx < n)
        np.add.at(cnt_sec, sec_idx[ok], 1.0)

    w = max(1, int(round(p.window_sec / p.grid_sec)))
    c = np.cumsum(cnt_sec)
    cnt_win = c.copy()
    if w < n:
        cnt_win[w:] = c[w:] - c[:-w]
    return grid, cnt_win, thr_sec


def large_print_series(
    nq: pa.Table,
    es: pa.Table,
    *,
    t0_ns: int,
    t1_ns: int,
    p: LargePrintParams | None = None,
) -> LargePrintSeries:
    p = p or LargePrintParams()
    sec_ns = int(p.grid_sec) * 1_000_000_000
    grid, nq_cnt, nq_thr = _market_counts(nq, t0_ns=t0_ns, t1_ns=t1_ns, p=p)
    if len(grid) == 0:
        z = np.zeros(0, dtype=np.float64)
        return LargePrintSeries(
            end_ns=np.zeros(0, dtype=np.int64), nq_cnt=z, es_cnt=z.copy(),
            nq_thr=z.copy(), es_thr=z.copy(), stratum=np.zeros(0, dtype=np.int32),
            t0_ns=t0_ns, t1_ns=t1_ns, window_sec=int(p.window_sec))
    _, es_cnt, es_thr = _market_counts(es, t0_ns=t0_ns, t1_ns=t1_ns, p=p)
    warm = ~(np.isfinite(nq_thr) & np.isfinite(es_thr))
    nq_b = nq_cnt >= p.burst_min_count
    es_b = es_cnt >= p.burst_min_count
    stratum = np.full(len(grid), 4, dtype=np.int32)  # none
    stratum[nq_b & es_b] = 1
    stratum[nq_b & ~es_b] = 2
    stratum[~nq_b & es_b] = 3
    stratum[warm] = 0
    return LargePrintSeries(
        end_ns=(grid + sec_ns).astype(np.int64),
        nq_cnt=nq_cnt, es_cnt=es_cnt, nq_thr=nq_thr, es_thr=es_thr,
        stratum=stratum, t0_ns=int(t0_ns), t1_ns=int(t1_ns),
        window_sec=int(p.window_sec))


def large_print_at(series: LargePrintSeries, event_ts_ns) -> dict[str, np.ndarray]:
    """Last completed-second values at/before the event stamp (causal)."""
    ts = np.atleast_1d(np.asarray(event_ts_ns, dtype=np.int64))
    out: dict[str, np.ndarray] = {}
    keys = (("lp_stratum", series.stratum.astype(np.float64)),
            ("lp_nq_cnt", series.nq_cnt), ("lp_es_cnt", series.es_cnt),
            ("lp_nq_thr", series.nq_thr), ("lp_es_thr", series.es_thr))
    if len(series.end_ns) == 0:
        for k, _ in keys:
            out[k] = np.full(len(ts), np.nan)
        return out
    idx = np.searchsorted(series.end_ns, ts, side="right") - 1
    valid = idx >= 0
    safe = np.where(valid, idx, 0)
    for k, arr in keys:
        v = np.full(len(ts), np.nan)
        v[valid] = arr[safe[valid]]
        out[k] = v
    return out


def large_print_handcheck(
    nq: pa.Table,
    es: pa.Table,
    *,
    t0_ns: int,
    event_ts_ns: int,
    p: LargePrintParams | None = None,
) -> dict:
    """Directly count large prints in the window using the series thresholds."""
    p = p or LargePrintParams()
    sec_ns = int(p.grid_sec) * 1_000_000_000
    series = large_print_series(nq, es, t0_ns=t0_ns, t1_ns=int(event_ts_ns) + sec_ns, p=p)
    got = large_print_at(series, int(event_ts_ns))
    if len(series.end_ns) == 0:
        return {"match": True, "nq_cnt_at": float("nan"), "nq_cnt_direct": -1}
    i = int(np.searchsorted(series.end_ns, int(event_ts_ns), side="right") - 1)
    if i < 0:
        return {"match": True, "nq_cnt_at": float("nan"), "nq_cnt_direct": -1}
    end = int(series.end_ns[i])
    w_start = end - int(p.window_sec) * sec_ns

    def _direct(t: pa.Table, thr: float) -> int:
        if t.num_rows == 0 or not np.isfinite(thr):
            return -1
        ts = t.column("ts_ns").to_numpy().astype(np.int64)
        size = t.column("size").to_numpy().astype(np.float64)
        k = (ts >= w_start) & (ts < end) & (size >= thr)
        return int(k.sum())

    nq_d = _direct(nq, float(got["lp_nq_thr"][0]))
    es_d = _direct(es, float(got["lp_es_thr"][0]))
    match = (int(got["lp_nq_cnt"][0]) == nq_d) and (int(got["lp_es_cnt"][0]) == es_d)
    return {
        "match": bool(match),
        "nq_cnt_at": float(got["lp_nq_cnt"][0]), "nq_cnt_direct": nq_d,
        "es_cnt_at": float(got["lp_es_cnt"][0]), "es_cnt_direct": es_d,
        "nq_thr": float(got["lp_nq_thr"][0]), "es_thr": float(got["lp_es_thr"][0]),
        "stratum_at": float(got["lp_stratum"][0]), "i": i,
    }
