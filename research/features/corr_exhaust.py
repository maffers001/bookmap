"""Correlated exhaustion (EXP-0036 / H29).

Idea: an ES+NQ confirmed burst is "healthy" while both markets keep pushing the
same way. The first 100 ms bar after entry where NQ is *still* aggressive in the
trade direction but ES has stopped participating (ES imbalance against/flat, or
ES volume collapsed relative to its confirming burst) is read as NQ running
alone -> exhaustion. Two uses:

1. Exit rule for live MAT trades (``build_exhaust_exit_fn`` -> ``simulate_session(exit_fn=...)``).
2. Standalone fade event after any confirm burst (``exhaust_fade_events``).

All bar lookups are causal: only bars whose *start* is at/after the entry
timestamp are inspected, and the exit is stamped at that bar's close.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass
class ExhaustParams:
    bar_ms: int = 100
    # NQ "still with-trade" conditions
    nq_min_imbalance: float = 0.55
    nq_min_vol: float = 25.0  # 0 = plan-literal (imbalance only)
    nq_min_trades: int = 6  # 0 = off
    # ES "stopped" conditions (either satisfied -> exhaustion)
    es_imb_max: float = 0.0  # side * es_imbalance <= this
    es_min_vol: float = 0.0  # imb condition additionally requires es_vol >= this (0 = off)
    es_vol_frac: float = 0.30  # es_total_vol < frac * es burst vol (0 = off)
    es_vol_counts_empty: bool = False  # treat an ES bar with no prints as vol=0
    use_imb: bool = True
    use_vol: bool = True
    # Window after entry in which to look; 0 = until EOD
    max_wait_sec: float = 0.0
    # Skip the first N full bars after entry (0 = none)
    skip_bars: int = 0


@dataclass
class _Bars:
    start: np.ndarray  # bar start ns
    imb: np.ndarray
    vol: np.ndarray
    n_tr: np.ndarray
    close: np.ndarray  # parquet price units (ticks)

    @classmethod
    def from_table(cls, t: pa.Table) -> "_Bars":
        if t.num_rows == 0:
            z = np.zeros(0)
            return cls(np.zeros(0, dtype=np.int64), z, z, z, z)
        return cls(
            t.column("ts_ns").to_numpy().astype(np.int64),
            t.column("imbalance").to_numpy().astype(np.float64),
            t.column("total_vol").to_numpy().astype(np.float64),
            t.column("n_trades").to_numpy().astype(np.float64),
            t.column("close").to_numpy().astype(np.float64),
        )


# Return: (bar_start_ns, reason, nq_bar_i, es_bar_j_or_-1)
_Hit = tuple[int, str, int, int]


def _first_exhaust_bar(
    nq: _Bars,
    es: _Bars,
    entry_ts: int,
    side: int,
    es_burst_vol: float,
    p: ExhaustParams,
) -> _Hit | None:
    """Return (bar_start_ns, reason, nq_i, es_j) of the first exhaustion bar, or None.

    ``es_j`` is -1 when the ES bar is missing (empty-counts path).
    """
    i0 = int(np.searchsorted(nq.start, entry_ts, side="left")) + int(p.skip_bars)
    if i0 >= len(nq.start):
        return None
    limit_ns = int(p.max_wait_sec * 1e9) if p.max_wait_sec > 0 else 0
    j = int(np.searchsorted(es.start, nq.start[i0], side="left"))
    n_es = len(es.start)
    vol_thr = p.es_vol_frac * es_burst_vol if (p.use_vol and p.es_vol_frac > 0 and es_burst_vol > 0) else -1.0

    for i in range(i0, len(nq.start)):
        b0 = int(nq.start[i])
        if limit_ns and b0 - entry_ts > limit_ns:
            return None
        # NQ still pushing
        if side * nq.imb[i] < p.nq_min_imbalance:
            continue
        if p.nq_min_vol > 0 and nq.vol[i] < p.nq_min_vol:
            continue
        if p.nq_min_trades > 0 and nq.n_tr[i] < p.nq_min_trades:
            continue
        # Same wall-clock ES bar
        while j < n_es and es.start[j] < b0:
            j += 1
        has_es = j < n_es and es.start[j] == b0
        es_imb = float(es.imb[j]) if has_es else 0.0
        es_vol = float(es.vol[j]) if has_es else 0.0

        if (
            p.use_imb
            and has_es
            and es_vol > 0
            and (p.es_min_vol <= 0 or es_vol >= p.es_min_vol)
            and side * es_imb <= p.es_imb_max
        ):
            return b0, "exhaust_imb", i, j
        if vol_thr >= 0:
            if has_es and es_vol < vol_thr:
                return b0, "exhaust_vol", i, j
            if (not has_es) and p.es_vol_counts_empty:
                return b0, "exhaust_vol", i, -1
    return None


def build_exhaust_exit_fn(nq_bars: pa.Table, es_bars: pa.Table, p: ExhaustParams):
    """Return an ``ExitFn`` for ``simulate_session`` that exits at the close of
    the first exhaustion bar after entry."""
    nq = _Bars.from_table(nq_bars)
    es = _Bars.from_table(es_bars)
    bar_ns = int(p.bar_ms * 1_000_000)

    def exit_fn(entry_ts: int, side: int, row: dict) -> tuple[int, str] | None:
        if len(nq.start) == 0:
            return None
        burst_vol = float(row.get("es_total_vol") or 0.0)
        hit = _first_exhaust_bar(nq, es, int(entry_ts), int(side), burst_vol, p)
        if hit is None:
            return None
        return hit[0] + bar_ns, hit[1]

    return exit_fn


_BURST_PASSTHROUGH = (
    "es_lag_ms",
    "es_signed_lag_ms",
    "es_total_vol",
    "total_vol",
    "imbalance",
    "n_trades",
)


def exhaust_fade_events(
    confirm_events: pa.Table,
    nq_bars: pa.Table,
    es_bars: pa.Table,
    p: ExhaustParams,
    *,
    max_wait_sec: float = 5.0,
) -> pa.Table:
    """Standalone event: after each confirm burst, the first exhaustion bar.

    Event is stamped at that bar's close; ``side`` is the fade (against the
    burst). Extra columns carry the burst side/time, wait, exhaustion-bar
    fields (``nq_imb_x`` / ``es_imb_x`` / vols), and selected ``burst_*``
    columns copied from the confirming burst when present.
    """
    if confirm_events.num_rows == 0 or nq_bars.num_rows == 0:
        return pa.table({})
    nq = _Bars.from_table(nq_bars)
    es = _Bars.from_table(es_bars)
    bar_ns = int(p.bar_ms * 1_000_000)
    pp = ExhaustParams(**{**p.__dict__, "max_wait_sec": max_wait_sec})

    ev_ts = confirm_events.column("event_ts_ns").to_numpy().astype(np.int64)
    ev_side = confirm_events.column("side").to_numpy().astype(int)
    ev_vol = (
        confirm_events.column("es_total_vol").to_numpy().astype(np.float64)
        if "es_total_vol" in confirm_events.column_names
        else np.zeros(len(ev_ts))
    )
    burst_cols = {
        name: confirm_events.column(name).to_numpy(zero_copy_only=False)
        for name in _BURST_PASSTHROUGH
        if name in confirm_events.column_names
    }

    out_ts, out_side, b_ts, b_side, wait_ms, reason = [], [], [], [], [], []
    nq_px, nq_imb_x, nq_vol_x, es_imb_x, es_vol_x = [], [], [], [], []
    burst_out: dict[str, list] = {f"burst_{n}": [] for n in burst_cols}
    last_stamp = -1
    for k in np.argsort(ev_ts, kind="stable"):
        t0, s = int(ev_ts[k]), int(ev_side[k])
        hit = _first_exhaust_bar(nq, es, t0, s, float(ev_vol[k]), pp)
        if hit is None:
            continue
        b0, why, ni, ej = hit
        stamp = b0 + bar_ns
        if stamp == last_stamp:  # two bursts resolving on the same bar
            continue
        last_stamp = stamp
        out_ts.append(stamp)
        out_side.append(-s)
        b_ts.append(t0)
        b_side.append(s)
        wait_ms.append((stamp - t0) / 1e6)
        reason.append(why)
        nq_px.append(float(nq.close[ni]) if ni < len(nq.close) else float("nan"))
        nq_imb_x.append(float(nq.imb[ni]))
        nq_vol_x.append(float(nq.vol[ni]))
        if ej >= 0:
            es_imb_x.append(float(es.imb[ej]))
            es_vol_x.append(float(es.vol[ej]))
        else:
            es_imb_x.append(0.0)
            es_vol_x.append(0.0)
        for name, arr in burst_cols.items():
            burst_out[f"burst_{name}"].append(arr[k])

    if not out_ts:
        return pa.table({})
    cols: dict = {
        "event_ts_ns": np.asarray(out_ts, dtype=np.int64),
        "side": np.asarray(out_side, dtype=np.int8),
        "event_type": ["corr_exhaust_fade"] * len(out_ts),
        "burst_ts_ns": np.asarray(b_ts, dtype=np.int64),
        "burst_side": np.asarray(b_side, dtype=np.int8),
        "wait_ms": np.asarray(wait_ms, dtype=np.float64),
        "exhaust_reason": reason,
        "nq_close_ticks": np.asarray(nq_px, dtype=np.float64),
        "nq_imb_x": np.asarray(nq_imb_x, dtype=np.float64),
        "nq_vol_x": np.asarray(nq_vol_x, dtype=np.float64),
        "es_imb_x": np.asarray(es_imb_x, dtype=np.float64),
        "es_vol_x": np.asarray(es_vol_x, dtype=np.float64),
    }
    for name, vals in burst_out.items():
        cols[name] = np.asarray(vals)
    return pa.table(cols)
