"""ES/NQ correlation-first signals.

The working lead (EXP-0008) is same-side flow on both markets. These detectors
make that correlation the event, not a later filter:

- catch-up: ES has already moved; NQ is still quiet → trade NQ toward ES.
- comove: both markets' short-window returns are same-sign and large → continuation.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass
class CatchUpConfig:
    window_ms: int = 1000
    sample_ms: int = 100
    min_es_ticks: float = 6.0
    max_nq_ticks: float = 3.0
    cooldown_ms: int = 2000
    agree_lookback_sec: int = 60
    agree_high: float = 0.60


@dataclass
class ComoveConfig:
    window_ms: int = 500
    sample_ms: int = 100
    min_es_ticks: float = 4.0
    min_nq_ticks: float = 4.0
    cooldown_ms: int = 1000


def _sorted_price(trades: pa.Table) -> tuple[np.ndarray, np.ndarray]:
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64)
    order = np.argsort(ts)
    return ts[order], px[order]


def _move(ts: np.ndarray, px: np.ndarray, t: int, window_ns: int) -> float | None:
    i1 = int(np.searchsorted(ts, t, side="right") - 1)
    i0 = int(np.searchsorted(ts, t - window_ns, side="right") - 1)
    if i0 < 0 or i1 <= i0:
        return None
    if ts[i1] < t - window_ns:
        return None
    return float(px[i1] - px[i0])


def _sign_agree_frac(
    es_ts: np.ndarray,
    es_px: np.ndarray,
    nq_ts: np.ndarray,
    nq_px: np.ndarray,
    t: int,
    lookback_sec: int,
) -> float:
    """Fraction of recent 1s buckets where ES and NQ 1s returns share a sign."""
    step = 1_000_000_000
    n = lookback_sec
    agrees = 0
    counted = 0
    for k in range(1, n + 1):
        t1 = t - (k - 1) * step
        es_m = _move(es_ts, es_px, t1, step)
        nq_m = _move(nq_ts, nq_px, t1, step)
        if es_m is None or nq_m is None:
            continue
        if es_m == 0 or nq_m == 0:
            continue
        counted += 1
        if np.sign(es_m) == np.sign(nq_m):
            agrees += 1
    if counted < 10:
        return float("nan")
    return agrees / counted


def catchup_events(
    nq: pa.Table,
    es: pa.Table,
    cfg: CatchUpConfig | None = None,
) -> pa.Table:
    """ES moved, NQ quiet → side = ES direction (NQ catch-up)."""
    cfg = cfg or CatchUpConfig()
    if nq.num_rows == 0 or es.num_rows == 0:
        return pa.table({})
    nq_ts, nq_px = _sorted_price(nq)
    es_ts, es_px = _sorted_price(es)
    window_ns = int(cfg.window_ms * 1_000_000)
    sample_ns = int(cfg.sample_ms * 1_000_000)
    cooldown_ns = int(cfg.cooldown_ms * 1_000_000)
    t0 = int(max(nq_ts[0], es_ts[0]) + window_ns)
    t1 = int(min(nq_ts[-1], es_ts[-1]))
    if t1 <= t0:
        return pa.table({})

    times: list[int] = []
    sides: list[int] = []
    es_m: list[float] = []
    nq_m: list[float] = []
    agree: list[float] = []
    last = -10**18
    t = t0
    while t <= t1:
        if t - last >= cooldown_ns:
            em = _move(es_ts, es_px, t, window_ns)
            nm = _move(nq_ts, nq_px, t, window_ns)
            if em is not None and nm is not None:
                if abs(em) >= cfg.min_es_ticks and abs(nm) <= cfg.max_nq_ticks:
                    times.append(t)
                    sides.append(1 if em > 0 else -1)
                    es_m.append(em)
                    nq_m.append(nm)
                    agree.append(
                        _sign_agree_frac(
                            es_ts, es_px, nq_ts, nq_px, t, cfg.agree_lookback_sec
                        )
                    )
                    last = t
        t += sample_ns

    if not times:
        return pa.table({})
    ag = np.asarray(agree, dtype=np.float64)
    high = np.where(np.isfinite(ag) & (ag >= cfg.agree_high), 1, 0).astype(np.int8)
    return pa.table({
        "event_ts_ns": np.asarray(times, dtype=np.int64),
        "side": np.asarray(sides, dtype=np.int8),
        "event_type": ["es_nq_catchup"] * len(times),
        "es_move_ticks": np.asarray(es_m, dtype=np.float64),
        "nq_move_ticks": np.asarray(nq_m, dtype=np.float64),
        "recent_sign_agree": ag,
        "high_recent_corr": high,
    })


def comove_events(
    nq: pa.Table,
    es: pa.Table,
    cfg: ComoveConfig | None = None,
) -> pa.Table:
    """Both markets move the same way by at least min ticks → continuation."""
    cfg = cfg or ComoveConfig()
    if nq.num_rows == 0 or es.num_rows == 0:
        return pa.table({})
    nq_ts, nq_px = _sorted_price(nq)
    es_ts, es_px = _sorted_price(es)
    window_ns = int(cfg.window_ms * 1_000_000)
    sample_ns = int(cfg.sample_ms * 1_000_000)
    cooldown_ns = int(cfg.cooldown_ms * 1_000_000)
    t0 = int(max(nq_ts[0], es_ts[0]) + window_ns)
    t1 = int(min(nq_ts[-1], es_ts[-1]))
    if t1 <= t0:
        return pa.table({})

    times: list[int] = []
    sides: list[int] = []
    es_m: list[float] = []
    nq_m: list[float] = []
    last = -10**18
    prev_on = False
    t = t0
    while t <= t1:
        em = _move(es_ts, es_px, t, window_ns)
        nm = _move(nq_ts, nq_px, t, window_ns)
        on = False
        side = 0
        if em is not None and nm is not None and em != 0 and nm != 0:
            if np.sign(em) == np.sign(nm) and abs(em) >= cfg.min_es_ticks and abs(nm) >= cfg.min_nq_ticks:
                on = True
                side = 1 if em > 0 else -1
        # edge trigger
        if on and not prev_on and t - last >= cooldown_ns:
            times.append(t)
            sides.append(side)
            es_m.append(float(em))
            nq_m.append(float(nm))
            last = t
        prev_on = on
        t += sample_ns

    if not times:
        return pa.table({})
    return pa.table({
        "event_ts_ns": np.asarray(times, dtype=np.int64),
        "side": np.asarray(sides, dtype=np.int8),
        "event_type": ["es_nq_comove"] * len(times),
        "es_move_ticks": np.asarray(es_m, dtype=np.float64),
        "nq_move_ticks": np.asarray(nq_m, dtype=np.float64),
    })
