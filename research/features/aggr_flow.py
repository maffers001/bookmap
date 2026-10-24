"""Aggressor count vs size states (EXP-0052 / H43, Tier A / A2).

Per market, over the trailing ``window_sec`` of completed 1 s seconds:
``cnt`` = distinct positive ``aggressor_order_id`` count (union over the
window), ``vol`` = summed trade size, ``mean_per_aggr = vol / cnt``.
States are per-market and session-causal: ``cnt`` and ``mean_per_aggr`` are
compared to their trailing-30-completed-minute medians (current minute
excluded; warmup until 5 completed baseline minutes exist or cnt == 0):

- few = cnt <= median_cnt; many = cnt > median_cnt
- large = mean_per_aggr >= median_mean; small = mean_per_aggr < median_mean

State codes: 0 warmup, 1 few-small, 2 few-large, 3 many-small, 4 many-large.
Carried combined cells (frozen in EXP-0052): FLB = few-large in both
markets; MSQ = NQ many-small AND ES window volume <= its trailing median.
Values are stamped at second end; lookup uses only completed seconds.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from research.features.vol_ratio import _trailing_minute_stats

STATE_NAMES = {0: "warmup", 1: "few_small", 2: "few_large", 3: "many_small", 4: "many_large"}


@dataclass
class AggrFlowParams:
    grid_sec: int = 1
    window_sec: int = 5
    baseline_lookback_min: int = 30
    baseline_warmup_min: int = 5


@dataclass
class AggrSeries:
    end_ns: np.ndarray
    nq_cnt: np.ndarray
    nq_mean: np.ndarray
    nq_vol: np.ndarray
    nq_state: np.ndarray  # int codes above
    es_cnt: np.ndarray
    es_mean: np.ndarray
    es_vol: np.ndarray
    es_state: np.ndarray
    es_vol_le_med: np.ndarray  # bool: ES vol <= trailing median ES vol
    t0_ns: int
    t1_ns: int
    window_sec: int


def _clean_ids(trades: pa.Table) -> np.ndarray:
    """aggressor_order_id as int64; blank/'0'/invalid -> -1 (excluded)."""
    if trades.num_rows == 0 or "aggressor_order_id" not in trades.column_names:
        return np.zeros(trades.num_rows, dtype=np.int64)
    col = trades.column("aggressor_order_id")
    try:
        clean = pc.if_else(
            pc.or_(pc.equal(col, ""), pc.equal(col, "0")),
            pa.scalar("-1", type=pa.string()), col,
        )
        return clean.combine_chunks().cast(pa.int64()).to_numpy()
    except Exception:
        out = np.full(trades.num_rows, -1, dtype=np.int64)
        for i, v in enumerate(col.to_pylist()):
            try:
                out[i] = int(v)
            except (TypeError, ValueError):
                out[i] = -1
        return out


def _market_window_arrays(
    trades: pa.Table,
    *,
    t0_ns: int,
    t1_ns: int,
    grid_sec: int,
    window_sec: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return (grid_start, cnt_window, vol_window, mean_window) on a dense 1 s grid."""
    sec_ns = int(grid_sec) * 1_000_000_000
    g0 = (int(t0_ns) // sec_ns) * sec_ns
    if g0 < t0_ns:
        g0 += sec_ns
    g1 = (int(t1_ns) // sec_ns) * sec_ns
    if g1 <= g0:
        z = np.zeros(0, dtype=np.float64)
        return np.zeros(0, dtype=np.int64), z, z, z
    n = int((g1 - g0) // sec_ns)
    grid = g0 + np.arange(n, dtype=np.int64) * sec_ns
    vol_sec = np.zeros(n, dtype=np.float64)
    # Per-second distinct id codes (list of int arrays)
    per_sec: list[np.ndarray] = [np.zeros(0, dtype=np.int64) for _ in range(n)]

    if trades.num_rows:
        ts = trades.column("ts_ns").to_numpy().astype(np.int64)
        size = trades.column("size").to_numpy().astype(np.float64)
        ids = _clean_ids(trades)
        in_win = (ts >= g0) & (ts < g1)
        sec_idx = ((ts[in_win] - g0) // sec_ns).astype(np.int64)
        sz = size[in_win]
        idw = ids[in_win]
        np.add.at(vol_sec, sec_idx, sz)
        valid = idw > 0
        if valid.any():
            s0, i0 = sec_idx[valid], idw[valid]
            order = np.lexsort((i0, s0))
            s_sorted, i_sorted = s0[order], i0[order]
            first = np.ones(len(s_sorted), dtype=bool)
            if len(s_sorted) > 1:
                first[1:] = (s_sorted[1:] != s_sorted[:-1]) | (i_sorted[1:] != i_sorted[:-1])
            pair_sec = s_sorted[first]
            pair_id = i_sorted[first]
            uniq_sec, counts = np.unique(pair_sec, return_counts=True)
            starts = np.zeros(len(uniq_sec), dtype=np.int64)
            if len(uniq_sec) > 1:
                starts[1:] = np.cumsum(counts)[:-1]
            for sec_u, st, ct in zip(uniq_sec, starts, counts):
                per_sec[int(sec_u)] = pair_id[st:st + ct]

    w = max(1, int(round(window_sec / grid_sec)))
    # Rolling volume (partial windows at head; state warmup covers early bars)
    c = np.cumsum(vol_sec)
    vol_win = c.copy()
    if w < n:
        vol_win[w:] = c[w:] - c[:-w]
    # Rolling distinct count via sliding counter over per-second code sets
    cnt_win = np.zeros(n, dtype=np.float64)
    counter: dict[int, int] = {}
    distinct = 0
    for i in range(n):
        for code in per_sec[i]:
            k = int(code)
            if counter.get(k, 0) == 0:
                distinct += 1
            counter[k] = counter.get(k, 0) + 1
        if i - w >= 0:
            for code in per_sec[i - w]:
                k = int(code)
                counter[k] -= 1
                if counter[k] == 0:
                    distinct -= 1
                    del counter[k]
        cnt_win[i] = distinct
    mean_win = np.divide(vol_win, cnt_win, out=np.full(n, np.nan), where=cnt_win > 0)
    return grid, cnt_win, vol_win, mean_win


def _states(
    grid: np.ndarray,
    cnt: np.ndarray,
    mean: np.ndarray,
    *,
    lookback_min: int,
    warmup_min: int,
) -> np.ndarray:
    med_cnt, _, _ = _trailing_minute_stats(
        grid, cnt, lookback_min=lookback_min, warmup_min=warmup_min)
    med_mean, _, _ = _trailing_minute_stats(
        grid, mean, lookback_min=lookback_min, warmup_min=warmup_min)
    state = np.zeros(len(cnt), dtype=np.int32)
    ok = np.isfinite(med_cnt) & np.isfinite(med_mean) & (cnt > 0)
    many = cnt > med_cnt
    large = mean >= med_mean
    state[ok] = 1 + large[ok].astype(np.int32) + (2 * many[ok]).astype(np.int32)
    # 1=few_small 2=few_large 3=many_small 4=many_large
    return state


def aggr_series(
    nq: pa.Table,
    es: pa.Table,
    *,
    t0_ns: int,
    t1_ns: int,
    p: AggrFlowParams | None = None,
) -> AggrSeries:
    p = p or AggrFlowParams()
    sec_ns = int(p.grid_sec) * 1_000_000_000
    grid, nq_cnt, nq_vol, nq_mean = _market_window_arrays(
        nq, t0_ns=t0_ns, t1_ns=t1_ns, grid_sec=p.grid_sec, window_sec=p.window_sec)
    if len(grid) == 0:
        z = np.zeros(0, dtype=np.float64)
        zi = np.zeros(0, dtype=np.int32)
        return AggrSeries(
            end_ns=np.zeros(0, dtype=np.int64), nq_cnt=z, nq_mean=z.copy(),
            nq_vol=z.copy(), nq_state=zi, es_cnt=z.copy(), es_mean=z.copy(),
            es_vol=z.copy(), es_state=zi.copy(),
            es_vol_le_med=np.zeros(0, dtype=bool),
            t0_ns=t0_ns, t1_ns=t1_ns, window_sec=int(p.window_sec))
    _, es_cnt, es_vol, es_mean = _market_window_arrays(
        es, t0_ns=t0_ns, t1_ns=t1_ns, grid_sec=p.grid_sec, window_sec=p.window_sec)
    nq_state = _states(grid, nq_cnt, nq_mean,
                       lookback_min=p.baseline_lookback_min, warmup_min=p.baseline_warmup_min)
    es_state = _states(grid, es_cnt, es_mean,
                       lookback_min=p.baseline_lookback_min, warmup_min=p.baseline_warmup_min)
    med_es_vol, _, _ = _trailing_minute_stats(
        grid, es_vol, lookback_min=p.baseline_lookback_min, warmup_min=p.baseline_warmup_min)
    es_le = np.isfinite(med_es_vol) & (es_vol <= med_es_vol)
    return AggrSeries(
        end_ns=(grid + sec_ns).astype(np.int64),
        nq_cnt=nq_cnt, nq_mean=nq_mean, nq_vol=nq_vol, nq_state=nq_state,
        es_cnt=es_cnt, es_mean=es_mean, es_vol=es_vol, es_state=es_state,
        es_vol_le_med=es_le,
        t0_ns=int(t0_ns), t1_ns=int(t1_ns), window_sec=int(p.window_sec))


def combined_cell(nq_state: int, es_state: int, es_vol_le_med: bool) -> str:
    """Combined label for the 16-cell + carried-cell accounting."""
    if nq_state == 0 or es_state == 0:
        return "warmup"
    return f"{STATE_NAMES[int(nq_state)]}__{STATE_NAMES[int(es_state)]}"


def is_flb(nq_state, es_state) -> np.ndarray:
    return (np.asarray(nq_state) == 2) & (np.asarray(es_state) == 2)


def is_msq(nq_state, es_vol_le_med) -> np.ndarray:
    return (np.asarray(nq_state) == 3) & np.asarray(es_vol_le_med, dtype=bool)


def aggr_at(series: AggrSeries, event_ts_ns: int | np.ndarray) -> dict[str, np.ndarray]:
    """Last completed-second values at/before the event stamp (causal)."""
    ts = np.atleast_1d(np.asarray(event_ts_ns, dtype=np.int64))
    out: dict[str, np.ndarray] = {}
    keys = (
        ("nq_cnt", series.nq_cnt), ("nq_mean", series.nq_mean),
        ("nq_state", series.nq_state.astype(np.float64)),
        ("es_cnt", series.es_cnt), ("es_mean", series.es_mean),
        ("es_state", series.es_state.astype(np.float64)),
        ("es_vol", series.es_vol),
        ("es_vol_le_med", series.es_vol_le_med.astype(np.float64)),
    )
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


def aggr_handcheck(
    nq: pa.Table,
    es: pa.Table,
    *,
    t0_ns: int,
    event_ts_ns: int,
    p: AggrFlowParams | None = None,
) -> dict:
    """Recompute window cnt/mean at ``event_ts_ns`` directly from raw trades."""
    p = p or AggrFlowParams()
    sec_ns = int(p.grid_sec) * 1_000_000_000
    series = aggr_series(nq, es, t0_ns=t0_ns, t1_ns=int(event_ts_ns) + sec_ns, p=p)
    got = aggr_at(series, int(event_ts_ns))
    if len(series.end_ns) == 0:
        return {"match": True, "nq_cnt_at": float("nan"), "nq_cnt_direct": -1}
    i = int(np.searchsorted(series.end_ns, int(event_ts_ns), side="right") - 1)
    if i < 0:
        return {"match": True, "nq_cnt_at": float("nan"), "nq_cnt_direct": -1}
    end = int(series.end_ns[i])
    w_start = end - int(p.window_sec) * sec_ns

    def _direct(t: pa.Table) -> tuple[int, float, float]:
        if t.num_rows == 0:
            return 0, 0.0, float("nan")
        ts = t.column("ts_ns").to_numpy().astype(np.int64)
        size = t.column("size").to_numpy().astype(np.float64)
        ids = _clean_ids(t)
        k = (ts >= w_start) & (ts < end)
        if not k.any():
            return 0, 0.0, float("nan")
        cnt = int(len(np.unique(ids[k & (ids > 0)]))) if (ids[k] > 0).any() else 0
        vol = float(size[k].sum())
        return cnt, vol, (vol / cnt if cnt else float("nan"))

    nq_c, nq_v, nq_m = _direct(nq)
    es_c, es_v, es_m = _direct(es)
    def _close(a: float, b: float) -> bool:
        if not np.isfinite(a) and not np.isfinite(b):
            return True
        return np.isfinite(a) and np.isfinite(b) and abs(a - b) < 1e-9

    match = (
        int(got["nq_cnt"][0]) == nq_c and int(got["es_cnt"][0]) == es_c
        and _close(float(got["nq_mean"][0]), nq_m)
        and _close(float(got["es_mean"][0]), es_m)
    )
    return {
        "match": bool(match),
        "nq_cnt_at": float(got["nq_cnt"][0]), "nq_cnt_direct": nq_c,
        "es_cnt_at": float(got["es_cnt"][0]), "es_cnt_direct": es_c,
        "nq_mean_at": float(got["nq_mean"][0]), "nq_mean_direct": nq_m,
        "es_mean_at": float(got["es_mean"][0]), "es_mean_direct": es_m,
        "i": i,
    }
