"""Path-aware opposite-side absorption exit for MAT (H67 / EXP-0076).

``exit_fn`` is called once at entry; it returns a force-exit timestamp.
Path MFE is enforced by walking the NQ trade path inside the closure and
only emitting an absorb stamp after MFE has already reached ``mfe_frac * tp``.
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa


def _path_arrays(nq: pa.Table, *, pts_per_tick: float = 0.25) -> tuple[np.ndarray, np.ndarray]:
    if nq.num_rows == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.float64)
    ts = nq.column("ts_ns").to_numpy().astype(np.int64)
    px = nq.column("price").to_numpy().astype(np.float64) * pts_per_tick
    order = np.argsort(ts, kind="mergesort")
    return ts[order], px[order]


def _tp_from_row(row: dict, *, floor: float = 6.0, cap: float = 30.0) -> float:
    tp = row.get("tp_pts")
    if tp is not None:
        try:
            v = float(tp)
            if np.isfinite(v) and v > 0:
                return v
        except (TypeError, ValueError):
            pass
    med = row.get("med_r30")
    try:
        m = float(med) if med is not None else float("nan")
    except (TypeError, ValueError):
        m = float("nan")
    if np.isfinite(m) and m > 0:
        return float(np.clip(m, floor, cap))
    return 10.0


def build_absorb_exit_fn(
    nq: pa.Table,
    abs_ts: np.ndarray,
    aggr: np.ndarray,
    *,
    require_path_mfe: bool = True,
    mfe_frac: float = 0.5,
    pts_per_tick: float = 0.25,
    reason_approx: str = "absorb_exit",
    reason_mfe: str = "absorb_exit_mfe",
):
    """Return ``ExitFn`` for ``simulate_session``.

    If ``require_path_mfe`` is False, first opposite-side absorb after entry
    (EXP-0073 approx). If True, first opposite absorb at/after the first path
    time when MFE ≥ ``mfe_frac * tp``.
    """
    path_ts, path_px = _path_arrays(nq, pts_per_tick=pts_per_tick)
    abs_ts = np.asarray(abs_ts, dtype=np.int64)
    aggr = np.asarray(aggr, dtype=int)

    def exit_fn(entry_ts_ns: int, side: int, row: dict) -> tuple[int, str] | None:
        if len(abs_ts) == 0:
            return None
        lo = int(np.searchsorted(abs_ts, int(entry_ts_ns), side="right"))
        if lo >= len(abs_ts):
            return None
        side = int(side)

        if not require_path_mfe:
            for j in range(lo, len(abs_ts)):
                if int(aggr[j]) == -side:
                    return int(abs_ts[j]), reason_approx
            return None

        if len(path_ts) == 0:
            return None
        ei = int(np.searchsorted(path_ts, int(entry_ts_ns), side="left"))
        if ei >= len(path_ts):
            return None
        entry_px = float(path_px[ei])
        need = mfe_frac * _tp_from_row(row)
        if need <= 0:
            return None

        best = entry_px
        t_ready: int | None = None
        for j in range(ei + 1, len(path_ts)):
            p = float(path_px[j])
            if side > 0:
                best = max(best, p)
                mfe = best - entry_px
            else:
                best = min(best, p)
                mfe = entry_px - best
            if mfe >= need:
                t_ready = int(path_ts[j])
                break
        if t_ready is None:
            return None

        start = int(np.searchsorted(abs_ts, t_ready, side="left"))
        start = max(start, lo)
        for j in range(start, len(abs_ts)):
            if int(aggr[j]) == -side:
                return int(abs_ts[j]), reason_mfe
        return None

    return exit_fn
