"""Compact trap-side volume node + departure features for DOP breaks (EXP-0082 / H73).

Before a DOP break, trap-side volume may be dispersed over a wide price
range or concentrated in one compact node. This module locates the
contiguous <= band_pts band on the trap side holding the most pre-break
volume, measures the aggressor delta inside it, and counts pullbacks on
the departure from that node to the break print.

All features use only trades with ts <= break_ts_ns (causal); trap/node
statistics exclude the break print itself. Prices in the trades table are
parquet tick units; outputs are points unless ``_ticks`` / ``_ts_ns``.
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa

PTS_PER_TICK = 0.25

NAN = float("nan")


def _arrays(trades: pa.Table):
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64)
    sz = trades.column("size").to_numpy().astype(np.float64)
    buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)
    o = np.argsort(ts, kind="stable")
    return ts[o], px[o], sz[o], buy[o]


def compute_compact_trap_features(
    trades: pa.Table,
    *,
    dop_ticks: float,
    side: int,
    break_ts_ns: int,
    open_ns: int,
    band_pts: float = 5.0,
    compact_share_min: float = 0.50,
    pullback_pts: float = 2.0,
    clean_max_pullbacks: int = 1,
    pts_per_tick: float = PTS_PER_TICK,
) -> dict:
    """Compact-node + clean-departure features for one DOP break.

    Long (side +1): trap prints are below the DOP; trapped aggression is
    buy-aggressor volume. Short mirrors (trap above; sell aggressors).
    Departure starts at the last pre-break print inside the node band.
    A pullback is a retracement of >= pullback_pts against the break
    direction from the running progress extreme; a new progress extreme
    ends it. clean = pullback count <= clean_max_pullbacks.
    """
    out: dict = {
        "trap_vol_node": NAN, "node_vol": NAN, "node_share5": NAN,
        "node_w60_pts": NAN, "node_lo_pts": NAN, "node_hi_pts": NAN,
        "node_span_pts": NAN, "node_vpoc_pts": NAN,
        "node_dist_dop_pts": NAN, "node_trap_delta": NAN,
        "node_delta_share": NAN, "node_buy_vol": NAN, "node_sell_vol": NAN,
        "node_first_ts_ns": 0, "node_last_ts_ns": 0, "node_dur_sec": NAN,
        "node_end_to_break_sec": NAN,
        "depart_ts_ns": 0, "depart_sec": NAN, "depart_dist_pts": NAN,
        "n_pullbacks": 0, "max_pullback_pts": NAN,
        "depart_path_efficiency": NAN,
        "compact": 0, "clean_departure": 0, "arm_k": 0,
    }
    if trades.num_rows == 0:
        return out
    ts, px, sz, buy = _arrays(trades)
    dop_t = float(dop_ticks)
    band_ticks = int(round(band_pts / pts_per_tick))
    pb_ticks = float(pullback_pts / pts_per_tick)

    pre = np.where((ts >= open_ns) & (ts < break_ts_ns))[0]
    upto = np.where((ts >= open_ns) & (ts <= break_ts_ns))[0]
    if len(pre) == 0 or len(upto) == 0:
        return out
    break_i = int(upto[-1])

    p_pre, s_pre = px[pre], sz[pre]
    if side > 0:
        trap_mask = p_pre < dop_t
    else:
        trap_mask = p_pre > dop_t
    trap_idx = pre[trap_mask]
    if len(trap_idx) == 0:
        return out
    trap_px = px[trap_idx]
    trap_sz = sz[trap_idx]
    trap_vol = float(trap_sz.sum())
    if trap_vol <= 0:
        return out
    out["trap_vol_node"] = trap_vol

    # ---- volume-by-price on the trap side (integer ticks)
    tick_px = np.rint(trap_px).astype(np.int64)
    uniq, inv = np.unique(tick_px, return_inverse=True)
    vol_at = np.zeros(len(uniq), dtype=np.float64)
    np.add.at(vol_at, inv, trap_sz)
    prefix = np.concatenate([[0.0], np.cumsum(vol_at)])

    # ---- best band: window of uniq prices spanning <= band_ticks
    j = 0
    best = (-1.0, 0, 0)  # (vol, i, j) window uniq[i:j+1]
    for i in range(len(uniq)):
        if j < i:
            j = i
        while j + 1 < len(uniq) and int(uniq[j + 1]) - int(uniq[i]) <= band_ticks:
            j += 1
        v = float(prefix[j + 1] - prefix[i])
        lo_t, hi_t = int(uniq[i]), int(uniq[j])
        dist = (dop_t - hi_t) if side > 0 else (lo_t - dop_t)
        b_lo, b_hi = int(uniq[best[1]]), int(uniq[best[2]]) if best[1] else int(uniq[0])
        b_dist = (dop_t - b_hi) if side > 0 else (b_lo - dop_t)
        if v > best[0] + 1e-12 or (abs(v - best[0]) <= 1e-12 and dist < b_dist):
            best = (v, i, j)
    node_vol, bi, bj = best
    win_lo, win_hi = int(uniq[bi]), int(uniq[bj])
    out["node_vol"] = float(node_vol)
    out["node_share5"] = float(node_vol / trap_vol)
    out["node_lo_pts"] = float(win_lo * pts_per_tick)
    out["node_hi_pts"] = float(win_hi * pts_per_tick)
    out["node_span_pts"] = float((win_hi - win_lo) * pts_per_tick)
    out["node_dist_dop_pts"] = float(
        ((dop_t - win_hi) if side > 0 else (win_lo - dop_t)) * pts_per_tick
    )
    k_vpoc = bi + int(np.argmax(vol_at[bi:bj + 1]))
    out["node_vpoc_pts"] = float(int(uniq[k_vpoc]) * pts_per_tick)
    out["compact"] = int(out["node_share5"] >= float(compact_share_min))

    # ---- minimal span holding >= 60% of trap volume (descriptive)
    target = 0.60 * trap_vol
    j = 0
    w60 = float(uniq[-1] - uniq[0])
    for i in range(len(uniq)):
        if j < i:
            j = i
        while j + 1 < len(uniq) and float(prefix[j + 1] - prefix[i]) < target:
            j += 1
        if float(prefix[j + 1] - prefix[i]) >= target:
            w60 = min(w60, float(uniq[j] - uniq[i]))
    out["node_w60_pts"] = float(w60 * pts_per_tick)

    # ---- node membership, delta, timing (pre-break prints in band)
    in_node = (p_pre >= win_lo) & (p_pre <= win_hi) & trap_mask
    node_idx = pre[in_node]
    if len(node_idx) == 0:
        return out
    n_sz, n_buy = sz[node_idx], buy[node_idx]
    buy_vol = float(n_sz[n_buy].sum()) if n_buy.any() else 0.0
    sell_vol = float(n_sz[~n_buy].sum()) if (~n_buy).any() else 0.0
    raw_delta = buy_vol - sell_vol
    out["node_buy_vol"] = buy_vol
    out["node_sell_vol"] = sell_vol
    out["node_trap_delta"] = float(raw_delta if side > 0 else -raw_delta)
    out["node_delta_share"] = float(out["node_trap_delta"] / node_vol) if node_vol > 0 else NAN
    out["node_first_ts_ns"] = int(ts[node_idx[0]])
    out["node_last_ts_ns"] = int(ts[node_idx[-1]])
    out["node_dur_sec"] = float((ts[node_idx[-1]] - ts[node_idx[0]]) / 1e9)
    out["node_end_to_break_sec"] = float((break_ts_ns - ts[node_idx[-1]]) / 1e9)

    # ---- departure: last in-node print -> break print (inclusive)
    dep_i = int(node_idx[-1])
    out["depart_ts_ns"] = int(ts[dep_i])
    out["depart_sec"] = float((break_ts_ns - ts[dep_i]) / 1e9)
    seg_px = px[dep_i:break_i + 1]
    start_px = float(seg_px[0])
    break_px = float(seg_px[-1])
    net_ticks = float(side * (break_px - start_px))
    out["depart_dist_pts"] = float(net_ticks * pts_per_tick)
    if len(seg_px) > 1:
        path_ticks = float(np.abs(np.diff(seg_px)).sum())
        out["depart_path_efficiency"] = (
            float(net_ticks / path_ticks) if path_ticks > 1e-12 else NAN
        )
    progress = side * (seg_px - start_px)  # ticks in break direction
    best_prog = np.maximum.accumulate(progress)
    retrace = best_prog - progress
    out["max_pullback_pts"] = float(retrace.max() * pts_per_tick) if len(retrace) else 0.0
    n_pb = 0
    in_pb = False
    cur_best = float(progress[0]) if len(progress) else 0.0
    for k in range(len(progress)):
        p = float(progress[k])
        if p >= cur_best:
            if p > cur_best:
                in_pb = False
            cur_best = max(cur_best, p)
        if cur_best - p >= pb_ticks and not in_pb:
            n_pb += 1
            in_pb = True
    out["n_pullbacks"] = int(n_pb)
    out["clean_departure"] = int(n_pb <= int(clean_max_pullbacks))
    out["arm_k"] = int(out["compact"] and out["clean_departure"])
    return out
