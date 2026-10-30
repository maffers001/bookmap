"""MAT book corridor at stamp (H77 / EXP-0086).

Generalises the DOP ``_best_band`` finder to zones anchored at the MAT
entry fill: support behind the trade, path wall ahead. Snapshot uses
depth rows with ts <= stamp only (causal).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from research.features.dop_rest_cluster import _best_band
from research.features.dop_zone import snapshots_at

PTS_PER_TICK = 0.25
NAN = float("nan")
SEC = 1_000_000_000


@dataclass
class BookCorridorConfig:
    zone_pts: float = 20.0  # see EXP-0086.yaml: band/zone must keep uniform share ≪ 0.50
    band_pts: float = 5.0
    share_min: float = 0.50
    persist_lookback_sec: float = 10.0
    pts_per_tick: float = PTS_PER_TICK


def _zones(
    entry_ticks: int, side: int, zone_ticks: int
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return (support_lo_hi, path_lo_hi) in tick units."""
    if side > 0:
        # long: support = bids below, path = asks above
        return (entry_ticks - zone_ticks, entry_ticks), (entry_ticks, entry_ticks + zone_ticks)
    return (entry_ticks, entry_ticks + zone_ticks), (entry_ticks - zone_ticks, entry_ticks)


def _entry_px_at(
    tr_ts: np.ndarray, tr_px_ticks: np.ndarray, stamp_ns: int
) -> float | None:
    j = int(np.searchsorted(tr_ts, stamp_ns, side="left"))
    if j >= len(tr_ts):
        return None
    return float(tr_px_ticks[j])


def corridor_at(
    bids: dict[int, int],
    asks: dict[int, int],
    *,
    entry_ticks: int,
    side: int,
    cfg: BookCorridorConfig,
    bids_persist: dict[int, int] | None = None,
    asks_persist: dict[int, int] | None = None,
) -> dict:
    """Compute corridor features from a book snapshot at the stamp."""
    zone_ticks = int(round(cfg.zone_pts / cfg.pts_per_tick))
    band_ticks = int(round(cfg.band_pts / cfg.pts_per_tick))
    (slo, shi), (plo, phi) = _zones(entry_ticks, side, zone_ticks)

    # support book = bids for long, asks for short; path = opposite
    if side > 0:
        sup_book, path_book = bids, asks
    else:
        sup_book, path_book = asks, bids

    empty = {
        "bc_sup_share": NAN, "bc_sup_size": NAN, "bc_sup_zone": NAN,
        "bc_sup_lo_pts": NAN, "bc_sup_hi_pts": NAN, "bc_sup_dist_pts": NAN,
        "bc_path_share": NAN, "bc_path_size": NAN, "bc_path_zone": NAN,
        "bc_path_lo_pts": NAN, "bc_path_hi_pts": NAN, "bc_path_dist_pts": NAN,
        "bc_sup_persist_share": NAN,
        "bc_support_only": 0, "bc_wall_only": 0, "bc_corridor": 0,
        "bc_ready": 0,
    }
    sb = _best_band(sup_book, slo, shi, band_ticks)
    pb = _best_band(path_book, plo, phi, band_ticks)
    out = dict(empty)
    if sb["total"] > 0 and np.isfinite(sb["share"]):
        out["bc_sup_share"] = float(sb["share"])
        out["bc_sup_size"] = float(sb["sum"])
        out["bc_sup_zone"] = float(sb["total"])
        out["bc_sup_lo_pts"] = float(sb["lo"] * cfg.pts_per_tick)
        out["bc_sup_hi_pts"] = float(sb["hi"] * cfg.pts_per_tick)
        mid = 0.5 * (sb["lo"] + sb["hi"])
        out["bc_sup_dist_pts"] = float(abs(mid - entry_ticks) * cfg.pts_per_tick)
        out["bc_ready"] = 1
    if pb["total"] > 0 and np.isfinite(pb["share"]):
        out["bc_path_share"] = float(pb["share"])
        out["bc_path_size"] = float(pb["sum"])
        out["bc_path_zone"] = float(pb["total"])
        out["bc_path_lo_pts"] = float(pb["lo"] * cfg.pts_per_tick)
        out["bc_path_hi_pts"] = float(pb["hi"] * cfg.pts_per_tick)
        mid = 0.5 * (pb["lo"] + pb["hi"])
        out["bc_path_dist_pts"] = float(abs(mid - entry_ticks) * cfg.pts_per_tick)
        out["bc_ready"] = 1

    # persistence: same-side support share at stamp - lookback
    if bids_persist is not None and asks_persist is not None:
        sp_book = bids_persist if side > 0 else asks_persist
        sp = _best_band(sp_book, slo, shi, band_ticks)
        if sp["total"] > 0 and np.isfinite(sp["share"]):
            out["bc_sup_persist_share"] = float(sp["share"])

    thr = float(cfg.share_min)
    sup_ok = np.isfinite(out["bc_sup_share"]) and out["bc_sup_share"] >= thr
    wall_ok = np.isfinite(out["bc_path_share"]) and out["bc_path_share"] >= thr
    out["bc_support_only"] = int(sup_ok)
    out["bc_wall_only"] = int(wall_ok)
    # corridor = support present AND no wall
    out["bc_corridor"] = int(sup_ok and not wall_ok)
    return out


def band_size_in(
    book: dict[int, int], lo_ticks: int, hi_ticks: int
) -> float:
    if lo_ticks > hi_ticks:
        lo_ticks, hi_ticks = hi_ticks, lo_ticks
    return float(sum(s for p, s in book.items() if lo_ticks <= p <= hi_ticks and s > 0))


def annotate_book_corridor(
    events: pa.Table,
    depth: pa.Table,
    trades: pa.Table,
    *,
    init_bids: dict[int, int] | None = None,
    init_asks: dict[int, int] | None = None,
    cfg: BookCorridorConfig | None = None,
    extra_offsets_sec: tuple[float, ...] = (),
) -> pa.Table:
    """Append ``bc_*`` columns. Also stores band tick bounds for survival reuse.

    ``extra_offsets_sec`` requests additional snapshots relative to stamp
    (e.g. +2, +5) stored under ``bc_snap_{ms}`` metadata columns for
    EXP-0087 without a second depth replay. Stamp-time features only use
    offset 0 and -persist_lookback.
    """
    cfg = cfg or BookCorridorConfig()
    if events.num_rows == 0:
        return events

    tr_ts = trades.column("ts_ns").to_numpy().astype(np.int64) if trades.num_rows else np.array([], dtype=np.int64)
    tr_px = (
        np.round(trades.column("price").to_numpy().astype(np.float64)).astype(np.int64)
        if trades.num_rows else np.array([], dtype=np.int64)
    )
    if len(tr_ts) and np.any(tr_ts[1:] < tr_ts[:-1]):
        o = np.argsort(tr_ts, kind="stable")
        tr_ts, tr_px = tr_ts[o], tr_px[o]

    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    sides = events.column("side").to_numpy().astype(int)
    n = len(ev_ts)

    # Collect all snapshot times needed
    persist_ns = int(cfg.persist_lookback_sec * SEC)
    times: list[int] = []
    for t0 in ev_ts:
        times.append(int(t0))
        times.append(int(t0) - persist_ns)
        for off in extra_offsets_sec:
            times.append(int(t0) + int(off * SEC))

    # Seed init books into a synthetic pre-row stream by replaying from empty
    # via snapshots_at after injecting init into depth-less path.
    snaps = snapshots_at(depth, times) if depth.num_rows else {}
    # Apply init book for times before any depth row: merge init into each snap
    # that has no depth yet by walking chronologically from init.
    if init_bids is not None or init_asks is not None:
        ib = dict(init_bids or {})
        ia = dict(init_asks or {})
        # For each requested time, if snap missing, use init; else snap already
        # includes only depth rows ≤ t — but NOT the pre-window init. Fix by
        # replaying: start from init, apply depth chronologically.
        snaps = _replay_with_init(depth, times, ib, ia)

    float_names = [
        "bc_sup_share", "bc_sup_size", "bc_sup_zone", "bc_sup_lo_pts", "bc_sup_hi_pts",
        "bc_sup_dist_pts", "bc_path_share", "bc_path_size", "bc_path_zone",
        "bc_path_lo_pts", "bc_path_hi_pts", "bc_path_dist_pts", "bc_sup_persist_share",
        "bc_entry_pts",
    ]
    int_names = [
        "bc_support_only", "bc_wall_only", "bc_corridor", "bc_ready",
        "bc_sup_lo_ticks", "bc_sup_hi_ticks", "bc_path_lo_ticks", "bc_path_hi_ticks",
    ]
    cols_f = {k: np.full(n, NAN) for k in float_names}
    cols_i = {k: np.zeros(n, dtype=np.int64 if "ticks" in k else np.int8) for k in int_names}

    # Optional post-stamp snap sizes for survival (same band bounds)
    post_cols: dict[str, np.ndarray] = {}
    for off in extra_offsets_sec:
        tag = f"bc_sup_size_p{int(off * 1000)}ms"
        post_cols[tag] = np.full(n, NAN)
        tag2 = f"bc_path_size_p{int(off * 1000)}ms"
        post_cols[tag2] = np.full(n, NAN)

    for i in range(n):
        t0 = int(ev_ts[i])
        side = int(sides[i])
        entry = _entry_px_at(tr_ts, tr_px, t0)
        if entry is None:
            continue
        entry_ticks = int(round(entry))
        cols_f["bc_entry_pts"][i] = entry_ticks * cfg.pts_per_tick
        snap = snaps.get(t0)
        if snap is None:
            continue
        bids, asks = snap
        persist_t = t0 - persist_ns
        snap_p = snaps.get(persist_t)
        bids_p = asks_p = None
        if snap_p is not None:
            bids_p, asks_p = snap_p
        st = corridor_at(
            bids, asks,
            entry_ticks=entry_ticks, side=side, cfg=cfg,
            bids_persist=bids_p, asks_persist=asks_p,
        )
        for k in float_names:
            if k == "bc_entry_pts":
                continue
            if k in st:
                cols_f[k][i] = st[k]
        for k in ("bc_support_only", "bc_wall_only", "bc_corridor", "bc_ready"):
            cols_i[k][i] = st[k]
        # store band tick bounds from corridor_at via recompute for survival
        zone_ticks = int(round(cfg.zone_pts / cfg.pts_per_tick))
        band_ticks = int(round(cfg.band_pts / cfg.pts_per_tick))
        (slo, shi), (plo, phi) = _zones(entry_ticks, side, zone_ticks)
        if side > 0:
            sb = _best_band(bids, slo, shi, band_ticks)
            pb = _best_band(asks, plo, phi, band_ticks)
        else:
            sb = _best_band(asks, slo, shi, band_ticks)
            pb = _best_band(bids, plo, phi, band_ticks)
        if sb["total"] > 0:
            cols_i["bc_sup_lo_ticks"][i] = int(sb["lo"])
            cols_i["bc_sup_hi_ticks"][i] = int(sb["hi"])
        if pb["total"] > 0:
            cols_i["bc_path_lo_ticks"][i] = int(pb["lo"])
            cols_i["bc_path_hi_ticks"][i] = int(pb["hi"])

        for off in extra_offsets_sec:
            st_post = snaps.get(t0 + int(off * SEC))
            if st_post is None:
                continue
            b2, a2 = st_post
            book_sup = b2 if side > 0 else a2
            book_path = a2 if side > 0 else b2
            if cols_i["bc_sup_lo_ticks"][i] or cols_i["bc_sup_hi_ticks"][i]:
                post_cols[f"bc_sup_size_p{int(off * 1000)}ms"][i] = band_size_in(
                    book_sup, int(cols_i["bc_sup_lo_ticks"][i]), int(cols_i["bc_sup_hi_ticks"][i])
                )
            if cols_i["bc_path_lo_ticks"][i] or cols_i["bc_path_hi_ticks"][i]:
                post_cols[f"bc_path_size_p{int(off * 1000)}ms"][i] = band_size_in(
                    book_path, int(cols_i["bc_path_lo_ticks"][i]), int(cols_i["bc_path_hi_ticks"][i])
                )

    out = events
    for name, arr in cols_f.items():
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=pa.float64()))
    for name, arr in cols_i.items():
        if name in out.column_names:
            out = out.drop(name)
        typ = pa.int64() if "ticks" in name else pa.int8()
        out = out.append_column(name, pa.array(arr, type=typ))
    for name, arr in post_cols.items():
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=pa.float64()))
    return out


def _replay_with_init(
    depth: pa.Table,
    times_ns: list[int],
    init_bids: dict[int, int],
    init_asks: dict[int, int],
) -> dict[int, tuple[dict[int, int], dict[int, int]]]:
    """Replay depth from init book state; snapshot at each time (ts <= t)."""
    if not times_ns:
        return {}
    uniq = sorted(set(int(t) for t in times_ns))
    bids = dict(init_bids)
    asks = dict(init_asks)
    out: dict[int, tuple[dict[int, int], dict[int, int]]] = {}
    if depth.num_rows == 0:
        for t in uniq:
            out[t] = (dict(bids), dict(asks))
        return out
    ts = depth.column("ts_ns").to_numpy().astype(np.int64)
    bid = depth.column("is_bid").to_numpy().astype(bool)
    px = depth.column("price_ticks").to_numpy().astype(np.int64)
    sz = depth.column("size").to_numpy().astype(np.int64)
    if len(ts) and np.any(ts[1:] < ts[:-1]):
        o = np.argsort(ts, kind="stable")
        ts, bid, px, sz = ts[o], bid[o], px[o], sz[o]
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
