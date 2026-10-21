"""Nearest-HVN targeting helpers for shallow reclaim fades."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from research.irt_lvn.load_irt import IrtBars


@dataclass
class HvnTarget:
    hvn_px: float
    gap_pts: float


def nearest_hvn_profit_side(
    entry_px: float,
    side: int,
    hvns: list[float],
    *,
    min_gap: float = 0.0,
    tp_buffer: float = 0.0,
) -> HvnTarget | None:
    """Nearest HVN in the fade *profit* direction.

    Long (side=+1): nearest HVN strictly above entry.
    Short (side=-1): nearest HVN strictly below entry.
    ``gap_pts`` is the TP distance after optional ``tp_buffer`` shaved off.
    """
    if side == 0 or not hvns:
        return None
    if side > 0:
        cands = [h for h in hvns if h > entry_px]
        if not cands:
            return None
        target = min(cands)
        gap = target - entry_px
    else:
        cands = [h for h in hvns if h < entry_px]
        if not cands:
            return None
        target = max(cands)
        gap = entry_px - target
    gap = gap - float(tp_buffer)
    if gap < float(min_gap) or gap <= 0:
        return None
    return HvnTarget(hvn_px=float(target), gap_pts=float(gap))


def sl_beyond_level_pts(
    entry_px: float,
    level_px: float,
    side: int,
    *,
    beyond: float = 8.0,
) -> float:
    """Stop just beyond the S/R level (wrong-way from fade).

    Short: stop above level by ``beyond``.
    Long: stop below level by ``beyond``.
    Returns distance in points from entry (≥ 1).
    """
    if side < 0:
        stop = level_px + beyond
        dist = stop - entry_px
    else:
        stop = level_px - beyond
        dist = entry_px - stop
    return float(max(dist, 1.0))


def annotate_hvn_brackets(
    events: pa.Table,
    nq_bars: IrtBars,
    path_ts: np.ndarray,
    path_px: np.ndarray,
    *,
    min_gap: float,
    tp_buffer: float = 2.0,
    tp_cap: float = 80.0,
    sl_mode: str = "fixed15",
    sl_fixed: float = 15.0,
    sl_beyond: float = 8.0,
) -> pa.Table:
    """Add tp_pts/sl_pts/hvn_* columns; drop rows that fail min_gap / no HVN.

    ``sl_mode``: fixed15 | fixed10 | fixed12 | beyond_level
    (vol SL is applied later via bracket_fn — use fixed/beyond here).
    """
    if events.num_rows == 0:
        return events

    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    sides = events.column("side").to_numpy().astype(np.int8)
    levels = events.column("nq_level_px").to_numpy().astype(np.float64)

    keep: list[int] = []
    tps: list[float] = []
    sls: list[float] = []
    hvn_pxs: list[float] = []
    gaps: list[float] = []
    entry_pxs: list[float] = []

    for i in range(len(ev_ts)):
        t0 = int(ev_ts[i])
        side = int(sides[i])
        j = int(np.searchsorted(path_ts, t0, side="left"))
        if j >= len(path_ts):
            continue
        entry = float(path_px[j])
        tgt = nearest_hvn_profit_side(
            entry, side, nq_bars.hvns_at(t0),
            min_gap=min_gap, tp_buffer=tp_buffer,
        )
        if tgt is None:
            continue
        tp = min(tgt.gap_pts, tp_cap)
        if sl_mode.startswith("fixed"):
            sl = float(sl_fixed)
        elif sl_mode == "beyond_level":
            sl = sl_beyond_level_pts(entry, float(levels[i]), side, beyond=sl_beyond)
        else:
            raise ValueError(f"unknown sl_mode {sl_mode}")
        keep.append(i)
        tps.append(tp)
        sls.append(sl)
        hvn_pxs.append(tgt.hvn_px)
        gaps.append(tgt.gap_pts)
        entry_pxs.append(entry)

    if not keep:
        return pa.table({})
    idx = np.asarray(keep, dtype=np.int64)
    cols = {n: events.column(n).take(idx) for n in events.column_names}
    cols["tp_pts"] = np.asarray(tps, dtype=np.float64)
    cols["sl_pts"] = np.asarray(sls, dtype=np.float64)
    cols["hvn_px"] = np.asarray(hvn_pxs, dtype=np.float64)
    cols["hvn_gap_pts"] = np.asarray(gaps, dtype=np.float64)
    cols["entry_px_est"] = np.asarray(entry_pxs, dtype=np.float64)
    return pa.table(cols)
