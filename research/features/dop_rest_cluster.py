"""Compact resting-cluster features for DOP breaks (EXP-0083 / H74).

In the trap-side zone adjacent to the DOP, find (at causal pre-break
snapshots) the contiguous <= band_pts band holding the most path-side
resting size (asks on a long setup, bids on a short). All depth rows are
replayed with ts <= snapshot time; pullbacks use trades <= break.
Prices are parquet tick units in inputs, points in ``_pts`` outputs.
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa

from research.features.dop_zone import snapshots_at

PTS_PER_TICK = 0.25
NAN = float("nan")
SEC = 1_000_000_000


def _best_band(book: dict[int, int], lo: int, hi: int, band_ticks: int) -> dict:
    items = sorted((p, s) for p, s in book.items() if lo <= p <= hi and s > 0)
    total = float(sum(s for _, s in items))
    if not items or total <= 0:
        return {"sum": 0.0, "total": total, "share": NAN, "lo": 0, "hi": 0,
                "n_levels": 0, "max_level": 0.0}
    prices = [p for p, _ in items]
    sizes = np.array([s for _, s in items], dtype=np.float64)
    pref = np.concatenate([[0.0], np.cumsum(sizes)])
    j = 0
    best = (-1.0, 0, 0)
    for i in range(len(items)):
        if j < i:
            j = i
        while j + 1 < len(items) and prices[j + 1] - prices[i] <= band_ticks:
            j += 1
        v = float(pref[j + 1] - pref[i])
        if v > best[0]:
            best = (v, i, j)
    v, i, j = best
    return {
        "sum": float(v), "total": total, "share": float(v / total),
        "lo": int(prices[i]), "hi": int(prices[j]),
        "n_levels": int(j - i + 1),
        "max_level": float(sizes[i:j + 1].max()),
    }


def _sampled_pullbacks(ts, px, side, start_i, end_i, thr_pts):
    seg_ts, seg_px = ts[start_i:end_i + 1], px[start_i:end_i + 1]
    if len(seg_px) < 2:
        return 0, 0.0
    sec = seg_ts // SEC
    keep = np.ones(len(seg_px), dtype=bool)
    keep[:-1] = sec[1:] != sec[:-1]
    p = seg_px[keep] * PTS_PER_TICK
    prog = side * (p - p[0])
    best = np.maximum.accumulate(prog)
    retr = best - prog
    n, in_pb, cur = 0, False, float(prog[0])
    for v in prog:
        v = float(v)
        if v >= cur:
            if v > cur:
                in_pb = False
            cur = max(cur, v)
        if cur - v >= thr_pts and not in_pb:
            n += 1
            in_pb = True
    return int(n), float(retr.max()) if len(retr) else 0.0


def compute_rest_cluster_features(
    depth: pa.Table,
    trades: pa.Table,
    *,
    dop_ticks: float,
    side: int,
    break_ts_ns: int,
    zone_pts: float = 30.0,
    band_pts: float = 5.0,
    share_min: float = 0.50,
    snapshot_offsets_sec: tuple[int, ...] = (300, 180, 60),
    pullback_pts: float = 2.0,
) -> dict:
    """Compact resting cluster in the trap zone before a DOP break."""
    out: dict = {
        "cluster_share_pre": NAN, "cluster_size_pre": NAN,
        "cluster_zone_resting": NAN, "cluster_lo_pts": NAN,
        "cluster_hi_pts": NAN, "cluster_dist_dop_pts": NAN,
        "cluster_max_level": NAN, "cluster_n_levels": 0,
        "cluster_persist": 0, "cluster_snap_age_sec": NAN,
        "cluster_share_break": NAN, "cluster_remaining_frac": NAN,
        "sup_share_pre": NAN, "sup_size_pre": NAN,
        "sup_zone_resting": NAN, "sup_lo_pts": NAN, "sup_hi_pts": NAN,
        "sup_dist_dop_pts": NAN, "sup_persist": 0, "sup_snap_age_sec": NAN,
        "rest_pb_ok": 0, "rest_n_pullbacks": 0, "rest_max_pullback_pts": NAN,
        "arm_r": 0, "arm_s": 0,
    }
    if depth.num_rows == 0:
        return out
    dop_t = int(round(float(dop_ticks)))
    zone_ticks = int(round(zone_pts / PTS_PER_TICK))
    band_ticks = int(round(band_pts / PTS_PER_TICK))
    if side > 0:
        zlo, zhi = dop_t - zone_ticks, dop_t
    else:
        zlo, zhi = dop_t, dop_t + zone_ticks

    times = [break_ts_ns - int(o) * SEC for o in snapshot_offsets_sec]
    snaps = snapshots_at(depth, times + [break_ts_ns])
    path_is_ask = side > 0

    def band_at(t: int, want_path: bool) -> dict | None:
        snap = snaps.get(int(t))
        if snap is None:
            return None
        bids, asks = snap
        if want_path:
            book = asks if path_is_ask else bids
        else:
            book = bids if path_is_ask else asks
        b = _best_band(book, zlo, zhi, band_ticks)
        return b if b["total"] > 0 else None

    best = None  # (share, size, t, band) path-side
    persist = 0
    best_s = None  # support-side
    persist_s = 0
    for t in times:
        b = band_at(t, True)
        if b is not None and np.isfinite(b["share"]):
            if b["share"] >= float(share_min):
                persist += 1
            if best is None or (b["share"], b["sum"]) > (best[0], best[1]):
                best = (float(b["share"]), float(b["sum"]), int(t), b)
        s = band_at(t, False)
        if s is not None and np.isfinite(s["share"]):
            if s["share"] >= float(share_min):
                persist_s += 1
            if best_s is None or (s["share"], s["sum"]) > (best_s[0], best_s[1]):
                best_s = (float(s["share"]), float(s["sum"]), int(t), s)
    if best_s is not None:
        ss, ssize, st, sband = best_s
        out["sup_share_pre"] = ss
        out["sup_size_pre"] = ssize
        out["sup_zone_resting"] = float(sband["total"])
        out["sup_lo_pts"] = float(sband["lo"] * PTS_PER_TICK)
        out["sup_hi_pts"] = float(sband["hi"] * PTS_PER_TICK)
        out["sup_dist_dop_pts"] = float(
            ((dop_t - sband["hi"]) if side > 0 else (sband["lo"] - dop_t)) * PTS_PER_TICK
        )
        out["sup_persist"] = int(persist_s)
        out["sup_snap_age_sec"] = float((break_ts_ns - st) / 1e9)
        out["arm_s"] = int(ss >= float(share_min))
    if best is None:
        return out
    share, size, bt, band = best
    out["cluster_share_pre"] = share
    out["cluster_size_pre"] = size
    out["cluster_zone_resting"] = float(band["total"])
    out["cluster_lo_pts"] = float(band["lo"] * PTS_PER_TICK)
    out["cluster_hi_pts"] = float(band["hi"] * PTS_PER_TICK)
    out["cluster_dist_dop_pts"] = float(
        ((dop_t - band["hi"]) if side > 0 else (band["lo"] - dop_t)) * PTS_PER_TICK
    )
    out["cluster_max_level"] = float(band["max_level"])
    out["cluster_n_levels"] = int(band["n_levels"])
    out["cluster_persist"] = int(persist)
    out["cluster_snap_age_sec"] = float((break_ts_ns - bt) / 1e9)
    out["arm_r"] = int(share >= float(share_min))

    # at-break state in the same price band + recomputed best share
    snap_b = snaps.get(int(break_ts_ns))
    if snap_b is not None:
        bids, asks = snap_b
        book = asks if path_is_ask else bids
        bb = _best_band(book, zlo, zhi, band_ticks)
        if bb["total"] > 0:
            out["cluster_share_break"] = float(bb["share"])
        same = float(sum(s for p, s in book.items() if band["lo"] <= p <= band["hi"] and s > 0))
        if size > 0:
            out["cluster_remaining_frac"] = float(same / size)

    # pullbacks: last in-band print -> break, 1s-sampled tape
    if trades.num_rows:
        ts = trades.column("ts_ns").to_numpy().astype(np.int64)
        px = trades.column("price").to_numpy().astype(np.float64)
        o = np.argsort(ts, kind="stable")
        ts, px = ts[o], px[o]
        pre = np.where(
            (ts < break_ts_ns) & (px >= band["lo"]) & (px <= band["hi"])
        )[0]
        upto = np.where(ts <= break_ts_ns)[0]
        if len(pre) and len(upto):
            n, mx = _sampled_pullbacks(
                ts, px, side, int(pre[-1]), int(upto[-1]), float(pullback_pts)
            )
            out["rest_pb_ok"] = 1
            out["rest_n_pullbacks"] = n
            out["rest_max_pullback_pts"] = mx
    return out
