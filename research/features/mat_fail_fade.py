"""MAT fail-fade events (EXP-0048 / H39).

Given gated MAT stamps, emit opposite-side entries when the push fails to break
the MAT entry extreme (arm A) or after a held retest / double (arm B).
"""
from __future__ import annotations

from typing import Literal

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import TICK_PTS
from research.features.regime import build_regime_index, lookup_regime

Arm = Literal["A", "B"]


def nq_sorted_path(nq: pa.Table, *, pts_per_tick: float = TICK_PTS) -> tuple[np.ndarray, np.ndarray]:
    """Sorted NQ trade path in chart points."""
    if nq.num_rows == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.float64)
    ts = nq.column("ts_ns").to_numpy().astype(np.int64)
    px = nq.column("price").to_numpy().astype(np.float64) * pts_per_tick
    order = np.argsort(ts, kind="stable")
    return ts[order], px[order]


def filter_gated_mat(
    nq: pa.Table,
    mat_events: pa.Table,
    gate,
    *,
    vwap_from_ns: int | None,
    trend_window_min: int = 30,
    regime_window_min: int = 60,
    pts_per_tick: float = TICK_PTS,
) -> pa.Table:
    """Keep MAT events that pass ``gate(side, sr, st, last_px)`` at stamp."""
    if mat_events.num_rows == 0 or nq.num_rows == 0:
        return mat_events.slice(0, 0)
    bars, bar_ts, cum_pv, cum_v, trade_ts, trade_px, trade_sz = build_regime_index(
        nq, pts_per_tick=pts_per_tick
    )
    ev_ts = mat_events.column("event_ts_ns").to_numpy().astype(np.int64)
    sides = mat_events.column("side").to_numpy().astype(int)
    kw = dict(
        vwap_from_ns=vwap_from_ns,
        trade_ts=trade_ts,
        trade_px=trade_px,
        trade_sz=trade_sz,
    )
    snaps_r = lookup_regime(
        bars, bar_ts, cum_pv, cum_v, ev_ts, window_min=regime_window_min, **kw
    )
    snaps_t = lookup_regime(
        bars, bar_ts, cum_pv, cum_v, ev_ts, window_min=trend_window_min, **kw
    )
    keep = []
    for i in range(len(ev_ts)):
        sr, st = snaps_r[i], snaps_t[i]
        if gate(int(sides[i]), sr, st, sr.last_px):
            keep.append(i)
    if not keep:
        return mat_events.slice(0, 0)
    return mat_events.take(np.asarray(keep, dtype=np.int64))


def _mat_entry_px(ts: np.ndarray, px: np.ndarray, mat_ts: int) -> tuple[int, float] | None:
    i = int(np.searchsorted(ts, mat_ts, side="left"))
    if i >= len(ts):
        return None
    return i, float(px[i])


def _broke(side: int, p: float, mat_px: float, x: float) -> bool:
    """Push broke beyond mat_px.

    X>0: long if p >= mat_px+X; short if p <= mat_px-X.
    X==0: strict past entry (p > mat_px / p < mat_px) so the entry print and
    an exact retest touch are not themselves a break.
    """
    if side > 0:
        return p > mat_px if x <= 0 else p >= mat_px + x
    return p < mat_px if x <= 0 else p <= mat_px - x


def build_fail_fade_events(
    nq: pa.Table,
    mat_events: pa.Table,
    *,
    arm: Arm = "A",
    fail_sec: float = 20.0,
    beyond_pts: float = 0.0,
    retest_tol: float = 1.0,
    hold_sec: float = 2.0,
    retest_max_sec: float = 60.0,
    pts_per_tick: float = TICK_PTS,
) -> pa.Table:
    """Build fade events from gated MAT stamps.

    Parameters
    ----------
    arm:
      ``A`` — fade at ``mat_ts + fail_sec`` if no break of ``mat_px ± beyond``.
      ``B`` — after A-fail, fade on held retest within ``retest_tol`` of ``mat_px``.
    """
    empty = pa.table({
        "event_ts_ns": pa.array([], type=pa.int64()),
        "side": pa.array([], type=pa.int32()),
        "src_mat_ts": pa.array([], type=pa.int64()),
        "mat_side": pa.array([], type=pa.int32()),
        "mat_px": pa.array([], type=pa.float64()),
        "arm": pa.array([], type=pa.string()),
    })
    if mat_events.num_rows == 0 or nq.num_rows == 0:
        return empty

    ts, px = nq_sorted_path(nq, pts_per_tick=pts_per_tick)
    if len(ts) == 0:
        return empty

    mat_ts_arr = mat_events.column("event_ts_ns").to_numpy().astype(np.int64)
    mat_side_arr = mat_events.column("side").to_numpy().astype(int)
    t_ns = int(fail_sec * 1e9)
    hold_ns = int(hold_sec * 1e9)
    retest_max_ns = int(retest_max_sec * 1e9)
    x = float(beyond_pts)
    tol = float(retest_tol)

    out_ts: list[int] = []
    out_side: list[int] = []
    out_src: list[int] = []
    out_mat_side: list[int] = []
    out_mat_px: list[float] = []
    out_arm: list[str] = []

    for k in range(len(mat_ts_arr)):
        mat_ts = int(mat_ts_arr[k])
        mat_side = int(mat_side_arr[k])
        if mat_side == 0:
            continue
        ent = _mat_entry_px(ts, px, mat_ts)
        if ent is None:
            continue
        entry_i, mat_px = ent
        deadline = mat_ts + t_ns

        # Walk trades *after* the entry print — entry itself is mat_px and must
        # not count as an immediate break when X=0.
        j = entry_i + 1
        broke = False
        while j < len(ts) and ts[j] <= deadline:
            if _broke(mat_side, float(px[j]), mat_px, x):
                broke = True
                break
            j += 1
        if broke:
            continue

        fail_ts = deadline
        fade_side = -mat_side

        if arm == "A":
            # Enter at fail_ts (first trade at/after deadline handled by sim)
            out_ts.append(fail_ts)
            out_side.append(fade_side)
            out_src.append(mat_ts)
            out_mat_side.append(mat_side)
            out_mat_px.append(mat_px)
            out_arm.append("A")
            continue

        # Arm B: after fail, find retest then hold without break
        search_end = fail_ts + retest_max_ns
        # Start scan at first trade after fail_ts
        j = int(np.searchsorted(ts, fail_ts, side="right"))
        retest_i: int | None = None
        while j < len(ts) and ts[j] <= search_end:
            p = float(px[j])
            if _broke(mat_side, p, mat_px, x):
                break  # push eventually broke — no fade
            # Retest: price returns to within tol of mat_px from the fade side
            if mat_side > 0:
                # Failed long → double top: touch near mat_px from below
                if (mat_px - tol) <= p <= (mat_px + x):
                    retest_i = j
                    break
            else:
                # Failed short → double bottom: touch near mat_px from above
                if (mat_px - x) <= p <= (mat_px + tol):
                    retest_i = j
                    break
            j += 1
        if retest_i is None:
            continue

        hold_end = int(ts[retest_i]) + hold_ns
        held = True
        h = retest_i
        while h < len(ts) and ts[h] <= hold_end:
            if _broke(mat_side, float(px[h]), mat_px, x):
                held = False
                break
            h += 1
        if not held:
            continue

        out_ts.append(hold_end)
        out_side.append(fade_side)
        out_src.append(mat_ts)
        out_mat_side.append(mat_side)
        out_mat_px.append(mat_px)
        out_arm.append("B")

    if not out_ts:
        return empty
    return pa.table({
        "event_ts_ns": pa.array(out_ts, type=pa.int64()),
        "side": pa.array(out_side, type=pa.int32()),
        "src_mat_ts": pa.array(out_src, type=pa.int64()),
        "mat_side": pa.array(out_mat_side, type=pa.int32()),
        "mat_px": pa.array(out_mat_px, type=pa.float64()),
        "arm": pa.array(out_arm, type=pa.string()),
    })
