"""Beyond-zone depth features for DOP breaks (EXP-0079 / H70).

Zone (NQ): for a long DOP break, resting asks DOP+10..DOP+20 pts
(40..80 ticks); mirrored bids for shorts. Depth is replayed from the
loaded window (runner seeds 30 min before the UK open).

At-break features use only depth rows with ts <= break (causal).
Traversal features use post-break trades/depth and are mechanism /
confirmation-entry inputs — never described as known at the break.
Prices are parquet tick units unless ``_pts`` is in the name.
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa

PTS_PER_TICK = 0.25


def zone_band_ticks(
    dop_ticks: float, side: int, inner_pts: float = 10.0, outer_pts: float = 20.0,
    *, pts_per_tick: float = PTS_PER_TICK,
) -> tuple[int, int]:
    inner = int(round(inner_pts / pts_per_tick))
    outer = int(round(outer_pts / pts_per_tick))
    c = int(round(dop_ticks))
    if side > 0:
        return c + inner, c + outer
    return c - outer, c - inner


def _depth_arrays(depth: pa.Table):
    ts = depth.column("ts_ns").to_numpy().astype(np.int64)
    bid = depth.column("is_bid").to_numpy().astype(bool)
    px = depth.column("price_ticks").to_numpy().astype(np.int64)
    sz = depth.column("size").to_numpy().astype(np.int64)
    o = np.argsort(ts, kind="stable")
    return ts[o], bid[o], px[o], sz[o]


def _trade_arrays(trades: pa.Table):
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64)
    sz = trades.column("size").to_numpy().astype(np.float64)
    buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)
    o = np.argsort(ts, kind="stable")
    return ts[o], px[o], sz[o], buy[o]


def snapshots_at(
    depth: pa.Table, times_ns: list[int],
) -> dict[int, tuple[dict[int, int], dict[int, int]]]:
    """Replay depth once; book (bids, asks) snapshot at each time (ts <= t)."""
    if depth.num_rows == 0 or not times_ns:
        return {}
    ts, bid, px, sz = _depth_arrays(depth)
    uniq = sorted(set(int(t) for t in times_ns))
    out: dict[int, tuple[dict[int, int], dict[int, int]]] = {}
    bids: dict[int, int] = {}
    asks: dict[int, int] = {}
    i, n = 0, len(ts)
    for target in uniq:
        while i < n and int(ts[i]) <= target:
            book = bids if bid[i] else asks
            p, s = int(px[i]), int(sz[i])
            if s <= 0:
                book.pop(p, None)
            else:
                book[p] = s
            i += 1
        out[target] = (dict(bids), dict(asks))
    return out


def compute_approach_features(
    depth: pa.Table,
    trades: pa.Table,
    *,
    dop_ticks: float,
    side: int,
    break_ts_ns: int,
    zone_pts: float = 20.0,
    fast_cap_sec: float = 60.0,
    large_pct: float = 99.9,
    large_min: int = 15,
) -> dict:
    """Pre-break final-approach chew into the DOP (EXP-0081 / H72).

    Long: last print at/below DOP-zone_pts before the break starts the
    approach; resting = asks in [DOP-zone, DOP] at that print; executed =
    buy-aggressor volume in the zone from approach start to the break.
    Mirror for shorts. All inputs at/before ``break_ts_ns`` (causal).
    """
    out: dict = {
        "approach_reached": 0, "approach_fast": 0,
        "approach_start_ts_ns": 0, "approach_sec": float("nan"),
        "approach_speed_pts_s": float("nan"),
        "approach_resting": float("nan"), "approach_resting_max": float("nan"),
        "approach_n_large": 0,
        "executed_into_approach": float("nan"),
        "approach_consume_frac": float("nan"),
        "pullback_in_approach_pts": float("nan"),
        "approach_path_efficiency": float("nan"),
        "approach_large_thr": float("nan"),
    }
    if trades.num_rows == 0:
        return out
    t_ts, t_px, t_sz, t_buy = _trade_arrays(trades)
    zone_ticks = int(round(zone_pts / PTS_PER_TICK))
    dop_t = float(dop_ticks)
    if side > 0:
        far_level = dop_t - zone_ticks
        beyond = t_px <= far_level
    else:
        far_level = dop_t + zone_ticks
        beyond = t_px >= far_level
    upto = np.where(t_ts <= break_ts_ns)[0]
    if len(upto) == 0:
        return out
    last_i = int(upto[-1])
    cand = np.where(beyond[: last_i + 1])[0]
    if len(cand) == 0:
        return out
    j = int(cand[-1])  # last print at/beyond the far edge before break
    start_ts = int(t_ts[j])
    out["approach_reached"] = 1
    out["approach_start_ts_ns"] = start_ts
    sec = float((break_ts_ns - start_ts) / 1e9)
    out["approach_sec"] = sec
    out["approach_speed_pts_s"] = float(zone_pts / sec) if sec > 1e-9 else float("inf")
    out["approach_fast"] = int(sec <= float(fast_cap_sec))

    seg_px = t_px[j: last_i + 1]
    seg_sz = t_sz[j: last_i + 1]
    seg_buy = t_buy[j: last_i + 1]
    path_pts = float(np.abs(np.diff(seg_px)).sum() * PTS_PER_TICK) if len(seg_px) > 1 else 0.0
    out["approach_path_efficiency"] = (
        float(zone_pts / path_pts) if path_pts > 1e-9 else float("nan")
    )
    if side > 0:
        run_max = np.maximum.accumulate(seg_px)
        pull = float((run_max - seg_px).max() * PTS_PER_TICK) if len(seg_px) else 0.0
        in_zone = (seg_px >= dop_t - zone_ticks) & (seg_px <= dop_t) & seg_buy
        band_lo, band_hi = int(round(dop_t - zone_ticks)), int(round(dop_t))
    else:
        run_min = np.minimum.accumulate(seg_px)
        pull = float((seg_px - run_min).max() * PTS_PER_TICK) if len(seg_px) else 0.0
        in_zone = (seg_px <= dop_t + zone_ticks) & (seg_px >= dop_t) & (~seg_buy)
        band_lo, band_hi = int(round(dop_t)), int(round(dop_t + zone_ticks))
    out["pullback_in_approach_pts"] = pull
    executed = float(seg_sz[in_zone].sum()) if in_zone.any() else 0.0
    out["executed_into_approach"] = executed

    if depth.num_rows:
        _, _, _, d_sz = _depth_arrays(depth)
        nz = d_sz[d_sz > 0]
        large_thr = (
            float(max(large_min, np.percentile(nz, large_pct))) if len(nz) else float(large_min)
        )
        out["approach_large_thr"] = large_thr
        snaps = snapshots_at(depth, [start_ts])
        snap = snaps.get(start_ts)
        if snap is not None:
            bids, asks = snap
            book = asks if side > 0 else bids
            st = _band_stats(book, band_lo, band_hi, large_thr)
            out["approach_resting"] = st["sum"]
            out["approach_resting_max"] = st["max"]
            out["approach_n_large"] = st["n_large"]
            if st["sum"] > 0:
                out["approach_consume_frac"] = float(executed / st["sum"])
    return out


def _band_stats(book: dict[int, int], lo: int, hi: int, large_thr: float) -> dict:
    sizes = [s for p, s in book.items() if lo <= p <= hi and s > 0]
    if not sizes:
        return {"sum": 0.0, "max": 0.0, "n_levels": 0, "n_large": 0}
    return {
        "sum": float(sum(sizes)),
        "max": float(max(sizes)),
        "n_levels": int(len(sizes)),
        "n_large": int(sum(1 for s in sizes if s >= large_thr)),
    }


def compute_zone_features(
    depth: pa.Table,
    trades: pa.Table,
    *,
    dop_ticks: float,
    side: int,
    break_ts_ns: int,
    inner_pts: float = 10.0,
    outer_pts: float = 20.0,
    traversal_cap_sec: float = 60.0,
    zone_entry_deadline_min: float = 15.0,
    observe_min: float = 30.0,
    large_pct: float = 99.9,
    large_min: int = 15,
) -> dict:
    """At-break zone state + post-break traversal for one DOP break."""
    lo_t, hi_t = zone_band_ticks(dop_ticks, side, inner_pts, outer_pts)
    out: dict = {
        "zone_lo_ticks": float(lo_t), "zone_hi_ticks": float(hi_t),
        "depth_ok": 0, "large_thr": float("nan"),
        "zone_resting": float("nan"), "zone_max": float("nan"),
        "zone_n_levels": 0, "zone_n_large": 0,
        "zone_resting_m60": float("nan"),
        "stack_build_60s": float("nan"),
        "reached_zone": 0, "reached_exit": 0,
        "time_to_zone_sec": float("nan"), "time_to_exit_sec": float("nan"),
        "traversal_sec": float("nan"), "fast_traversal": 0,
        "confirm_ts_ns": 0, "confirm_px_pts": float("nan"),
        "executed_into_zone": float("nan"), "consume_frac": float("nan"),
        "pullback_in_zone_pts": float("nan"),
        "zone_resting_at_exit": float("nan"), "replenish_est": float("nan"),
    }
    if depth.num_rows == 0 or trades.num_rows == 0:
        return out

    d_ts, _, _, d_sz = _depth_arrays(depth)
    nz = d_sz[d_sz > 0]
    large_thr = (
        float(max(large_min, np.percentile(nz, large_pct))) if len(nz) else float(large_min)
    )
    out["large_thr"] = large_thr
    out["depth_ok"] = int(int(d_ts[0]) <= break_ts_ns <= int(d_ts[-1]))

    t_ts, t_px, t_sz, t_buy = _trade_arrays(trades)
    observe_end = break_ts_ns + int(observe_min * 60 * 1e9)
    entry_level = float(dop_ticks + side * int(round(inner_pts / PTS_PER_TICK)))
    exit_level = float(dop_ticks + side * int(round(outer_pts / PTS_PER_TICK)))

    # --- traversal stamps from trades
    entry_ts = 0
    exit_ts = 0
    exit_px_ticks = float("nan")
    pullback_pts = float("nan")
    executed = float("nan")
    k = (t_ts >= break_ts_ns) & (t_ts <= observe_end)
    idx = np.where(k)[0]
    if len(idx):
        seg_ts, seg_px = t_ts[idx], t_px[idx]
        if side > 0:
            hit_e = seg_px >= entry_level
            hit_x = seg_px >= exit_level
        else:
            hit_e = seg_px <= entry_level
            hit_x = seg_px <= exit_level
        if hit_e.any():
            ie = int(np.argmax(hit_e))
            entry_ts = int(seg_ts[ie])
            out["reached_zone"] = 1
            out["time_to_zone_sec"] = float((entry_ts - break_ts_ns) / 1e9)
            sub = slice(ie, len(seg_px))
            if hit_x[ie:].any():
                ix = ie + int(np.argmax(hit_x[ie:]))
                exit_ts = int(seg_ts[ix])
                exit_px_ticks = float(seg_px[ix])
                out["reached_exit"] = 1
                out["time_to_exit_sec"] = float((exit_ts - break_ts_ns) / 1e9)
                out["traversal_sec"] = float((exit_ts - entry_ts) / 1e9)
                end_i = ix
            else:
                end_i = len(seg_px) - 1
            seg2_px = seg_px[ie:end_i + 1]
            seg2_sz = t_sz[idx][ie:end_i + 1]
            seg2_buy = t_buy[idx][ie:end_i + 1]
            if side > 0:
                adverse = float(entry_level - seg2_px.min()) * PTS_PER_TICK
                in_zone = (seg2_px >= lo_t) & (seg2_px <= hi_t) & seg2_buy
            else:
                adverse = float(seg2_px.max() - entry_level) * PTS_PER_TICK
                in_zone = (seg2_px >= lo_t) & (seg2_px <= hi_t) & (~seg2_buy)
            pullback_pts = max(0.0, adverse)
            executed = float(seg2_sz[in_zone].sum()) if in_zone.any() else 0.0

    out["pullback_in_zone_pts"] = pullback_pts
    out["executed_into_zone"] = executed
    deadline_ok = bool(
        entry_ts
        and (entry_ts - break_ts_ns) <= int(zone_entry_deadline_min * 60 * 1e9)
    )
    cap_ok = bool(
        exit_ts and np.isfinite(out["traversal_sec"])
        and out["traversal_sec"] <= float(traversal_cap_sec)
    )
    if deadline_ok and cap_ok:
        out["fast_traversal"] = 1
        out["confirm_ts_ns"] = int(exit_ts)
        out["confirm_px_pts"] = float(exit_px_ticks * PTS_PER_TICK)

    # --- depth snapshots
    times = [break_ts_ns - int(60e9), break_ts_ns]
    if entry_ts:
        times.append(entry_ts)
    if exit_ts:
        times.append(exit_ts)
    snaps = snapshots_at(depth, times)
    want_bid = side < 0  # short: resting bids below; long: asks above

    def _zone_at(t: int) -> dict:
        snap = snaps.get(t)
        if snap is None:
            return {"sum": float("nan"), "max": float("nan"), "n_levels": 0, "n_large": 0}
        bids, asks = snap
        return _band_stats(bids if want_bid else asks, lo_t, hi_t, large_thr)

    at_break = _zone_at(break_ts_ns)
    m60 = _zone_at(break_ts_ns - int(60e9))
    out["zone_resting"] = at_break["sum"]
    out["zone_max"] = at_break["max"]
    out["zone_n_levels"] = at_break["n_levels"]
    out["zone_n_large"] = at_break["n_large"]
    out["zone_resting_m60"] = m60["sum"]
    if np.isfinite(at_break["sum"]) and np.isfinite(m60["sum"]):
        out["stack_build_60s"] = float(np.log((at_break["sum"] + 1.0) / (m60["sum"] + 1.0)))
    if exit_ts:
        at_exit = _zone_at(exit_ts)
        out["zone_resting_at_exit"] = at_exit["sum"]
        if np.isfinite(executed) and np.isfinite(at_break["sum"]) and np.isfinite(at_exit["sum"]):
            out["replenish_est"] = float(executed - (at_break["sum"] - at_exit["sum"]))
    if np.isfinite(out["zone_resting"]) and out["zone_resting"] > 0 and np.isfinite(executed):
        out["consume_frac"] = float(executed / out["zone_resting"])
    return out
