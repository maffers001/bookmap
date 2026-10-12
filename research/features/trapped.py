"""Trapped traders: above-avg volume peak into a moderate 50-price LVN band,
then quiet ≥20pt pullback within ~20–30s (no large volume, no high reclaim).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from research.features.volume_profile import build_profile_at


@dataclass
class TrappedConfig:
    tick_size: float = 1.0
    # Peak volume vs average (250ms clusters)
    peak_window_ms: int = 250
    vol_avg_lookback_ms: int = 60_000
    min_vol_mult: float = 2.0
    local_extreme_ms: int = 20_000
    max_ticks_from_extreme: int = 1
    # Moderate LVN = 50-price band around peak is low vs neighboring bands
    lvn_band_ticks: int = 50  # full width (~25 each side)
    lvn_lookback_ms: int = 1_800_000
    lvn_frac_of_shoulder: float = 0.55  # center band vol ≤ this × max(shoulder)
    profile_refresh_ms: int = 15_000
    min_profile_volume: float = 200.0
    # Quiet 20pt pullback in 20–30s
    min_pullback_pts: float = 20.0
    pullback_min_ms: int = 5_000
    pullback_max_ms: int = 30_000
    reclaim_ticks: float = 1.0
    # Pullback cluster vol must stay below this fraction of *peak* cluster vol
    max_pullback_vs_peak: float = 0.45
    cooldown_ms: int = 60_000


def _trades_arrays(
    trades: pa.Table, tick_size: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = np.round(trades.column("price").to_numpy().astype(np.float64) / tick_size).astype(
        np.int64
    )
    sz = trades.column("size").to_numpy().astype(np.float64)
    order = np.argsort(ts)
    return ts[order], px[order], sz[order]


def _prefix_sum(sz: np.ndarray) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum(sz)])


def _cluster_vol_pref(
    ts: np.ndarray, pref: np.ndarray, t0: int, window_ms: int
) -> float:
    w = int(window_ms * 1_000_000)
    lo = np.searchsorted(ts, t0 - w // 2, side="left")
    hi = np.searchsorted(ts, t0 + w // 2, side="right")
    return float(pref[hi] - pref[lo])


def _precompute_avg_cluster(
    ts: np.ndarray,
    pref: np.ndarray,
    *,
    window_ms: int,
    lookback_ms: int,
) -> np.ndarray:
    """Causal rolling mean cluster vol at each trade index (vector-friendly walk)."""
    n = len(ts)
    out = np.zeros(n, dtype=np.float64)
    step = int(window_ms * 1_000_000)
    lb = int(lookback_ms * 1_000_000)
    # deque of (center_ts, vol) for windows ending before current
    from collections import deque

    q: deque[tuple[int, float]] = deque()
    running = 0.0
    next_center = int(ts[0]) + step // 2 if n else 0

    for i in range(n):
        t = int(ts[i])
        # advance window centers up to now
        while next_center <= t:
            v = _cluster_vol_pref(ts, pref, next_center, window_ms)
            q.append((next_center, v))
            running += v
            next_center += step
        # drop centers older than lookback
        cut = t - lb
        while q and q[0][0] < cut:
            running -= q[0][1]
            q.popleft()
        out[i] = running / len(q) if q else 0.0
    return out


def _profile_cache(
    ts: np.ndarray,
    px: np.ndarray,
    sz: np.ndarray,
    *,
    lookback_ms: int,
    refresh_ms: int,
) -> list[tuple[int, np.ndarray, np.ndarray, float]]:
    """List of (asof_ns, ticks, vols, total)."""
    if len(ts) == 0:
        return []
    look_ns = int(lookback_ms * 1_000_000)
    ref_ns = int(refresh_ms * 1_000_000)
    out: list[tuple[int, np.ndarray, np.ndarray, float]] = []
    t0 = int(ts[0])
    t_end = int(ts[-1])
    asof = t0 + look_ns // 10
    while asof <= t_end:
        ticks, vols, _poc, total = build_profile_at(ts, px, sz, asof, look_ns)
        if total > 0 and len(ticks):
            out.append((asof, ticks, vols, total))
        asof += ref_ns
    return out


def _profile_at(
    cache: list[tuple[int, np.ndarray, np.ndarray, float]], asof: int
) -> tuple[np.ndarray, np.ndarray, float] | None:
    best = None
    best_t = -1
    for t, ticks, vols, total in cache:
        if t <= asof and t >= best_t:
            best_t = t
            best = (ticks, vols, total)
    return best


def _moderate_lvn_band(
    ticks: np.ndarray,
    vols: np.ndarray,
    peak_px: int,
    *,
    band_ticks: int,
    frac: float,
    min_total: float,
) -> tuple[bool, float, float]:
    """True if ~band_ticks window around peak is low vs left/right shoulders.

    Returns (ok, center_vol, shoulder_vol).
    """
    if len(ticks) == 0:
        return False, 0.0, 0.0
    half = max(1, band_ticks // 2)
    # densemap on contiguous axis
    t0, t1 = int(ticks[0]), int(ticks[-1])
    if peak_px < t0 or peak_px > t1:
        return False, 0.0, 0.0
    axis = np.arange(t0, t1 + 1, dtype=np.int64)
    dens = np.zeros(len(axis), dtype=np.float64)
    idx = {int(t): i for i, t in enumerate(ticks)}
    for t, v in zip(ticks, vols):
        dens[idx[int(t)]] = float(v)
    # peak index
    pi = peak_px - t0
    c_lo = max(0, pi - half)
    c_hi = min(len(dens), pi + half + 1)
    center = float(dens[c_lo:c_hi].sum())
    # shoulders: same width immediately outside center
    width = c_hi - c_lo
    l_hi = c_lo
    l_lo = max(0, l_hi - width)
    r_lo = c_hi
    r_hi = min(len(dens), r_lo + width)
    left = float(dens[l_lo:l_hi].sum()) if l_hi > l_lo else 0.0
    right = float(dens[r_lo:r_hi].sum()) if r_hi > r_lo else 0.0
    shoulder = max(left, right)
    total = float(dens.sum())
    if total < min_total or shoulder <= 0:
        return False, center, shoulder
    ok = center <= frac * shoulder
    return ok, center, shoulder


def trapped_events(trades: pa.Table, cfg: TrappedConfig | None = None) -> pa.Table:
    cfg = cfg or TrappedConfig()
    if trades.num_rows == 0:
        return pa.table({})

    ts, px, sz = _trades_arrays(trades, cfg.tick_size)
    pref = _prefix_sum(sz)
    avg_at = _precompute_avg_cluster(
        ts,
        pref,
        window_ms=cfg.peak_window_ms,
        lookback_ms=cfg.vol_avg_lookback_ms,
    )
    profiles = _profile_cache(
        ts,
        px,
        sz,
        lookback_ms=cfg.lvn_lookback_ms,
        refresh_ms=cfg.profile_refresh_ms,
    )
    if not profiles:
        return pa.table({})

    extreme_ns = int(cfg.local_extreme_ms * 1_000_000)
    pb_min = int(cfg.pullback_min_ms * 1_000_000)
    pb_max = int(cfg.pullback_max_ms * 1_000_000)
    cooldown_ns = int(cfg.cooldown_ms * 1_000_000)
    peak_w = cfg.peak_window_ms

    events_ts: list[int] = []
    events_side: list[int] = []
    meta: list[dict] = []
    last_fire = -10**18
    n = len(ts)

    # Only evaluate when price makes a new local extreme (reduces work)
    run_hi = -10**18
    run_lo = 10**18
    run_hi_since = 0
    run_lo_since = 0

    i = 0
    while i < n:
        t0 = int(ts[i])
        p0 = int(px[i])
        if p0 >= run_hi:
            run_hi = p0
            run_hi_since = t0
        if p0 <= run_lo:
            run_lo = p0
            run_lo_since = t0
        # Reset running extreme if lookback expired
        j0 = np.searchsorted(ts, t0 - extreme_ns, side="left")
        if j0 > 0:
            # recompute cheaply from window when needed
            pass
        wpx = px[j0 : i + 1]
        if len(wpx) < 8:
            i += 1
            continue
        hi = int(wpx.max())
        lo = int(wpx.min())
        is_high = p0 == hi
        is_low = p0 == lo
        if not (is_high or is_low) or (is_high and is_low):
            i += 1
            continue
        if t0 - last_fire < cooldown_ns:
            i += 1
            continue

        peak_vol = _cluster_vol_pref(ts, pref, t0, peak_w)
        avg_vol = float(avg_at[i])
        if avg_vol <= 0 or peak_vol < cfg.min_vol_mult * avg_vol:
            i += 1
            continue

        prof = _profile_at(profiles, t0)
        if prof is None:
            i += 1
            continue
        ticks, vols, total = prof
        ok_lvn, center_vol, shoulder_vol = _moderate_lvn_band(
            ticks,
            vols,
            p0,
            band_ticks=cfg.lvn_band_ticks,
            frac=cfg.lvn_frac_of_shoulder,
            min_total=cfg.min_profile_volume,
        )
        if not ok_lvn:
            i += 1
            continue

        side = -1 if is_high else 1
        peak_px = p0
        i_end = np.searchsorted(ts, t0 + pb_max, side="right")
        aborted = False
        entry_t = None
        max_pull_vol = 0.0
        extreme_after = peak_px
        left_peak = False
        drop = 0.0

        for j in range(i + 1, i_end):
            tj = int(ts[j])
            pj = int(px[j])
            age = tj - t0
            if age > int(peak_w * 500_000):
                cv = _cluster_vol_pref(ts, pref, tj, peak_w)
                max_pull_vol = max(max_pull_vol, cv)
                if cv > cfg.max_pullback_vs_peak * peak_vol:
                    aborted = True
                    break

            if side < 0:
                if pj > peak_px:
                    aborted = True
                    break
                extreme_after = min(extreme_after, pj)
                drop = float(peak_px - extreme_after)
                if drop >= 3:
                    left_peak = True
                if left_peak and pj >= peak_px - cfg.reclaim_ticks:
                    aborted = True
                    break
            else:
                if pj < peak_px:
                    aborted = True
                    break
                extreme_after = max(extreme_after, pj)
                drop = float(extreme_after - peak_px)
                if drop >= 3:
                    left_peak = True
                if left_peak and pj <= peak_px + cfg.reclaim_ticks:
                    aborted = True
                    break

            if drop >= cfg.min_pullback_pts and age >= pb_min:
                entry_t = tj
                break

        if aborted or entry_t is None:
            i += 1
            continue

        events_ts.append(int(entry_t))
        events_side.append(side)
        meta.append({
            "peak_ts_ns": t0,
            "peak_price_ticks": peak_px,
            "peak_vol": peak_vol,
            "avg_vol": avg_vol,
            "vol_mult": peak_vol / avg_vol,
            "lvn_center_vol": center_vol,
            "lvn_shoulder_vol": shoulder_vol,
            "lvn_band_ticks": cfg.lvn_band_ticks,
            "pullback_pts": drop,
            "quiet_ms": (entry_t - t0) / 1e6,
            "max_pull_vol": max_pull_vol,
            "pull_vs_peak": max_pull_vol / peak_vol if peak_vol > 0 else np.nan,
        })
        last_fire = int(entry_t)
        i = np.searchsorted(ts, last_fire, side="right")
        # reset run extremes after fire
        run_hi = -10**18
        run_lo = 10**18

    if not events_ts:
        return pa.table({})

    cols: dict = {
        "event_ts_ns": np.asarray(events_ts, dtype=np.int64),
        "side": np.asarray(events_side, dtype=np.int8),
        "event_type": ["trapped_lvn"] * len(events_ts),
    }
    for key in meta[0]:
        vals = [m[key] for m in meta]
        if key.endswith("_ts_ns") or key in ("peak_price_ticks", "lvn_band_ticks"):
            cols[key] = np.asarray(vals, dtype=np.int64)
        else:
            cols[key] = np.asarray(vals, dtype=np.float64)
    return pa.table(cols)


def tape_peak_trapped_events(trades: pa.Table, cfg: TrappedConfig | None = None) -> pa.Table:
    return trapped_events(trades, cfg)
