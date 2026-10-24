"""Confirm LVN fade touches with a later MAT causal same-side burst."""
from __future__ import annotations

import numpy as np
import pyarrow as pa

from research.features.momentum import cross_confirm_bursts


PTS_PER_TICK = 0.25


def mat_confirm_lvn_touches(
    touches: pa.Table,
    nq_burst: pa.Table,
    es_burst: pa.Table,
    trades: pa.Table,
    *,
    confirm_window_sec: float = 60.0,
    sync_ms: int = 1500,
    max_es_lag_ms: float = 250.0,
    near_lvn_pts: float = 5.0,
    require_near: bool = True,
) -> pa.Table:
    """Keep LVN touches that get MAT same-side confirm within confirm_window.

    Stamp = max(touch_ts, mat_ts). Optional: at stamp, price still within
    ``near_lvn_pts`` of the LVN zone on the fade side (short ≤ hi+tol in pts,
    long ≥ lo−tol).
    """
    if touches.num_rows == 0:
        return pa.table({})
    mat = cross_confirm_bursts(nq_burst, es_burst, sync_ms=sync_ms, stamp="causal")
    if mat.num_rows == 0:
        return pa.table({})
    lag = mat.column("es_lag_ms").to_numpy()
    mat = mat.take(np.where(lag <= max_es_lag_ms)[0])
    if mat.num_rows == 0:
        return pa.table({})

    t_ts = touches.column("event_ts_ns").to_numpy().astype(np.int64)
    t_side = touches.column("side").to_numpy().astype(int)
    lvn_lo = touches.column("lvn_lo").to_numpy().astype(np.float64) * PTS_PER_TICK
    lvn_hi = touches.column("lvn_hi").to_numpy().astype(np.float64) * PTS_PER_TICK
    m_ts = mat.column("event_ts_ns").to_numpy().astype(np.int64)
    m_side = mat.column("side").to_numpy().astype(int)
    win_ns = int(confirm_window_sec * 1e9)

    tr_ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    tr_px = trades.column("price").to_numpy().astype(np.float64) * PTS_PER_TICK
    order = np.argsort(tr_ts)
    tr_ts, tr_px = tr_ts[order], tr_px[order]

    keep: list[int] = []
    stamp_ts: list[int] = []
    for i in range(len(t_ts)):
        lo = int(np.searchsorted(m_ts, t_ts[i], side="left"))
        hi = int(np.searchsorted(m_ts, t_ts[i] + win_ns, side="right"))
        best = None
        for j in range(lo, hi):
            if int(m_side[j]) != int(t_side[i]):
                continue
            if best is None or m_ts[j] < best:
                best = int(m_ts[j])
        if best is None:
            continue
        stamp = max(int(t_ts[i]), best)
        if require_near:
            ei = int(np.searchsorted(tr_ts, stamp, side="left"))
            if ei >= len(tr_ts):
                continue
            px = float(tr_px[ei])
            # fade short: want price near/just below LVN; long: near/just above
            if t_side[i] < 0:
                # short: within near_lvn of zone, not deep above hi
                if px > lvn_hi[i] + near_lvn_pts + 1e-12:
                    continue
                if px < lvn_lo[i] - near_lvn_pts - 1e-12:
                    continue
            else:
                if px < lvn_lo[i] - near_lvn_pts - 1e-12:
                    continue
                if px > lvn_hi[i] + near_lvn_pts + 1e-12:
                    continue
        keep.append(i)
        stamp_ts.append(stamp)

    if not keep:
        return pa.table({})
    idx = np.asarray(keep, dtype=np.int64)
    out = touches.take(idx)
    out = out.set_column(
        out.schema.get_field_index("event_ts_ns"),
        "event_ts_ns",
        pa.array(stamp_ts, type=pa.int64()),
    )
    # Chart-point level for structural brackets
    mid_pts = (
        (out.column("lvn_lo").to_numpy().astype(np.float64)
         + out.column("lvn_hi").to_numpy().astype(np.float64))
        * 0.5 * PTS_PER_TICK
    )
    if "swing_px" in out.column_names:
        out = out.set_column(
            out.schema.get_field_index("swing_px"),
            "swing_px",
            pa.array(mid_pts),
        )
    else:
        out = out.append_column("swing_px", pa.array(mid_pts))
    return out
