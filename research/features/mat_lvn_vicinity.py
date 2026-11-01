"""MAT × IRT LVN vicinity (H83 / H84).

Distance-only: at each MAT stamp, nearest as-of LVN2/LVN3 on NQ and ES
vs last print ≤ stamp. Missing IRT / no last print → undefined (NaN),
not zero.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from research.irt_lvn.events import PTS_PER_TICK, _asof_idx
from research.irt_lvn.load_irt import KIND_LVN, IrtBars


@dataclass
class LvnVicinityConfig:
    nq_band_pts: float = 5.0
    es_band_pts: float = 2.0


def _lvn23(bars: IrtBars, i: int) -> list[float]:
    """Non-zero LVN2/LVN3 only at bar index i."""
    if i < 0 or i >= len(bars.ts_ns):
        return []
    out: list[float] = []
    for px in (float(bars.lvn2[i]), float(bars.lvn3[i])):
        if px != 0.0:
            out.append(px)
    return out


def _last_px_pts(trades: pa.Table, stamp_ns: int) -> float:
    """Last trade price in points with ts <= stamp; NaN if none."""
    if trades.num_rows == 0:
        return float("nan")
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64) * PTS_PER_TICK
    i = int(np.searchsorted(ts, stamp_ns, side="right") - 1)
    if i < 0:
        return float("nan")
    return float(px[i])


def nearest_lvn_dist(
    stamp_ns: int,
    last_px_pts: float,
    bars: IrtBars,
) -> float:
    """Min |last_px − LVN| using as-of bars with bar_ts <= stamp; NaN if undefined."""
    if not np.isfinite(last_px_pts) or len(bars.ts_ns) == 0:
        return float("nan")
    bi = _asof_idx(bars.ts_ns, int(stamp_ns))
    levels = _lvn23(bars, bi)
    if not levels:
        return float("nan")
    return float(min(abs(last_px_pts - lv) for lv in levels))


def annotate_lvn_vicinity(
    events: pa.Table,
    nq_trades: pa.Table,
    es_trades: pa.Table,
    nq_bars: IrtBars,
    es_bars: IrtBars,
    *,
    cfg: LvnVicinityConfig | None = None,
) -> pa.Table:
    """Append ``lvn_nq_*`` / ``lvn_es_*`` columns to MAT events."""
    cfg = cfg or LvnVicinityConfig()
    if events.num_rows == 0:
        return events

    # Ensure trades sorted by ts for searchsorted
    def _sorted(t: pa.Table) -> pa.Table:
        if t.num_rows == 0:
            return t
        order = np.argsort(t.column("ts_ns").to_numpy().astype(np.int64), kind="stable")
        return t.take(order)

    nq_trades = _sorted(nq_trades)
    es_trades = _sorted(es_trades)

    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    n = len(ev_ts)
    nq_dist = np.full(n, np.nan)
    es_dist = np.full(n, np.nan)
    for i, stamp in enumerate(ev_ts):
        nq_px = _last_px_pts(nq_trades, int(stamp))
        es_px = _last_px_pts(es_trades, int(stamp))
        nq_dist[i] = nearest_lvn_dist(int(stamp), nq_px, nq_bars)
        es_dist[i] = nearest_lvn_dist(int(stamp), es_px, es_bars)

    nq_near = (np.isfinite(nq_dist) & (nq_dist <= cfg.nq_band_pts)).astype(np.int8)
    nq_far = (np.isfinite(nq_dist) & (nq_dist > cfg.nq_band_pts)).astype(np.int8)
    es_near = (np.isfinite(es_dist) & (es_dist <= cfg.es_band_pts)).astype(np.int8)
    es_far = (np.isfinite(es_dist) & (es_dist > cfg.es_band_pts)).astype(np.int8)

    out = events
    for name, arr, typ in (
        ("lvn_nq_dist", nq_dist, pa.float64()),
        ("lvn_es_dist", es_dist, pa.float64()),
        ("lvn_nq_near", nq_near, pa.int8()),
        ("lvn_nq_far", nq_far, pa.int8()),
        ("lvn_es_near", es_near, pa.int8()),
        ("lvn_es_far", es_far, pa.int8()),
    ):
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=typ))
    return out


# Re-export KIND_LVN for callers that want to assert level set
__all__ = [
    "LvnVicinityConfig",
    "annotate_lvn_vicinity",
    "nearest_lvn_dist",
    "KIND_LVN",
]
