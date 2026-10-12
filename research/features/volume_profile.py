"""Causal volume profile and LVN zone detection (numpy / pyarrow).

Tightened LVN definition (H7 retest / EXP-0023):
  - Rolling lookback with min total traded volume (meaningful profile).
  - Local valley vs nearby volume, requiring an HVN *above and below*
    (sandwich) so edge-of-profile / high-of-day artifacts are dropped.
  - Zones within ``edge_margin_ticks`` of the profile high/low are rejected.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass
class VolumeProfileConfig:
    lookback_ms: int = 1_800_000  # 30 min
    refresh_ms: int = 30_000
    lvn_frac_of_peak: float = 0.35
    min_lvn_width_ticks: int = 2
    max_lvn_width_ticks: int = 12
    min_sep_from_poc_ticks: int = 4
    tick_size: float = 1.0  # price column is tick units in extracts
    # Tightened gates (EXP-0023)
    min_profile_volume: float = 500.0  # sum of size in lookback
    hvn_frac_of_poc: float = 0.50  # each side peak must be ≥ this × POC vol
    hvn_search_ticks: int = 20  # search window above/below LVN for HVN
    edge_margin_ticks: int = 6  # reject LVNs this close to profile hi/lo
    require_hvn_sandwich: bool = True


@dataclass(frozen=True)
class LvnZone:
    lo: int
    hi: int
    poc: int
    asof_ts_ns: int
    mid: int
    hvn_below: int = 0
    hvn_above: int = 0
    valley_vol: float = 0.0
    profile_volume: float = 0.0

    @property
    def width(self) -> int:
        return self.hi - self.lo + 1


def _price_to_tick(price: np.ndarray, tick_size: float) -> np.ndarray:
    return np.round(price / tick_size).astype(np.int64)


def build_profile_at(
    trade_ts: np.ndarray,
    trade_px_ticks: np.ndarray,
    trade_sz: np.ndarray,
    asof_ts_ns: int,
    lookback_ns: int,
) -> tuple[np.ndarray, np.ndarray, int | None, float]:
    """Return (ticks_sorted, volumes, poc_tick, total_volume)."""
    lo_t = asof_ts_ns - lookback_ns
    mask = (trade_ts > lo_t) & (trade_ts <= asof_ts_ns)
    if not mask.any():
        return np.array([], dtype=np.int64), np.array([], dtype=np.float64), None, 0.0
    px = trade_px_ticks[mask]
    sz = trade_sz[mask]
    total = float(sz.sum())
    ticks, inv = np.unique(px, return_inverse=True)
    vols = np.bincount(inv, weights=sz)
    poc = int(ticks[int(np.argmax(vols))])
    return ticks.astype(np.int64), vols.astype(np.float64), poc, total


def _local_peaks(smooth: np.ndarray) -> np.ndarray:
    """Boolean mask of strict local maxima (interior)."""
    peaks = np.zeros(len(smooth), dtype=bool)
    for i in range(1, len(smooth) - 1):
        if smooth[i] >= smooth[i - 1] and smooth[i] > smooth[i + 1]:
            peaks[i] = True
        elif smooth[i] > smooth[i - 1] and smooth[i] >= smooth[i + 1]:
            peaks[i] = True
    return peaks


def find_lvn_zones(
    ticks: np.ndarray,
    vols: np.ndarray,
    poc: int,
    cfg: VolumeProfileConfig,
    asof_ts_ns: int,
    profile_volume: float = 0.0,
) -> list[LvnZone]:
    """LVN valleys sandwiched by HVNs, away from POC and profile edges."""
    if len(ticks) < 5:
        return []
    if profile_volume < cfg.min_profile_volume:
        return []

    t0, t1 = int(ticks[0]), int(ticks[-1])
    axis = np.arange(t0, t1 + 1, dtype=np.int64)
    vol_map = np.zeros(len(axis), dtype=np.float64)
    idx_map = {int(t): i for i, t in enumerate(ticks)}
    for t, v in zip(ticks, vols):
        vol_map[idx_map[int(t)]] = float(v)

    if len(vol_map) >= 3:
        pad = np.pad(vol_map, 1, mode="edge")
        smooth = (pad[:-2] + pad[1:-1] + pad[2:]) / 3.0
    else:
        smooth = vol_map

    poc_vol = float(vol_map[idx_map[poc]]) if poc in idx_map else float(vols.max())
    hvn_floor = cfg.hvn_frac_of_poc * max(poc_vol, 1e-9)
    peaks = _local_peaks(smooth)

    zones: list[LvnZone] = []
    i = 1
    while i < len(smooth) - 1:
        if not (smooth[i] <= smooth[i - 1] and smooth[i] <= smooth[i + 1]):
            i += 1
            continue

        # Neighborhood peaks for valley depth (legacy local check)
        left_peak = float(smooth[max(0, i - 8) : i + 1].max())
        right_peak = float(smooth[i : min(len(smooth), i + 9)].max())
        peak = max(left_peak, right_peak, 1e-9)
        if smooth[i] > cfg.lvn_frac_of_peak * peak:
            i += 1
            continue

        thr = cfg.lvn_frac_of_peak * peak
        lo_i = i
        while lo_i > 0 and smooth[lo_i - 1] <= thr:
            lo_i -= 1
        hi_i = i
        while hi_i < len(smooth) - 1 and smooth[hi_i + 1] <= thr:
            hi_i += 1
        width = hi_i - lo_i + 1
        if width < cfg.min_lvn_width_ticks or width > cfg.max_lvn_width_ticks:
            i = hi_i + 1
            continue

        lo_tick = int(axis[lo_i])
        hi_tick = int(axis[hi_i])
        mid = (lo_tick + hi_tick) // 2
        if abs(mid - poc) < cfg.min_sep_from_poc_ticks:
            i = hi_i + 1
            continue

        # Reject profile extremes (high/low of lookback profile)
        if lo_tick - t0 < cfg.edge_margin_ticks or t1 - hi_tick < cfg.edge_margin_ticks:
            i = hi_i + 1
            continue

        hvn_below_tick = 0
        hvn_above_tick = 0
        if cfg.require_hvn_sandwich:
            # Search below LVN for a local peak ≥ hvn_floor
            search_lo = max(0, lo_i - cfg.hvn_search_ticks)
            best_b = -1
            best_bv = -1.0
            for j in range(search_lo, lo_i):
                if peaks[j] and smooth[j] >= hvn_floor and smooth[j] > best_bv:
                    best_bv = float(smooth[j])
                    best_b = j
            # Search above LVN
            search_hi = min(len(smooth), hi_i + 1 + cfg.hvn_search_ticks)
            best_a = -1
            best_av = -1.0
            for j in range(hi_i + 1, search_hi):
                if peaks[j] and smooth[j] >= hvn_floor and smooth[j] > best_av:
                    best_av = float(smooth[j])
                    best_a = j
            if best_b < 0 or best_a < 0:
                i = hi_i + 1
                continue
            # Valley must be low vs *both* sandwich HVNs
            sandwich_peak = max(best_bv, best_av, 1e-9)
            if smooth[i] > cfg.lvn_frac_of_peak * sandwich_peak:
                i = hi_i + 1
                continue
            hvn_below_tick = int(axis[best_b])
            hvn_above_tick = int(axis[best_a])

        if zones and lo_tick <= zones[-1].hi + 1:
            i = hi_i + 1
            continue

        zones.append(
            LvnZone(
                lo=lo_tick,
                hi=hi_tick,
                poc=poc,
                asof_ts_ns=asof_ts_ns,
                mid=mid,
                hvn_below=hvn_below_tick,
                hvn_above=hvn_above_tick,
                valley_vol=float(smooth[i]),
                profile_volume=float(profile_volume),
            )
        )
        i = hi_i + 1
    return zones


def causal_lvn_snapshots(
    trades: pa.Table,
    cfg: VolumeProfileConfig | None = None,
) -> list[tuple[int, list[LvnZone]]]:
    """Refresh LVN zones every refresh_ms using only past lookback volume."""
    cfg = cfg or VolumeProfileConfig()
    if trades.num_rows == 0:
        return []
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = _price_to_tick(trades.column("price").to_numpy().astype(np.float64), cfg.tick_size)
    sz = trades.column("size").to_numpy().astype(np.float64)
    order = np.argsort(ts)
    ts, px, sz = ts[order], px[order], sz[order]

    lookback_ns = int(cfg.lookback_ms * 1_000_000)
    refresh_ns = int(cfg.refresh_ms * 1_000_000)
    t_start = int(ts[0])
    t_end = int(ts[-1])
    asof = t_start + lookback_ns
    if asof > t_end:
        asof = t_end

    snaps: list[tuple[int, list[LvnZone]]] = []
    while asof <= t_end:
        ticks, vols, poc, total = build_profile_at(ts, px, sz, asof, lookback_ns)
        zones = (
            find_lvn_zones(ticks, vols, poc, cfg, asof, profile_volume=total)
            if poc is not None
            else []
        )
        snaps.append((asof, zones))
        asof += refresh_ns
    return snaps


def zones_table(snaps: list[tuple[int, list[LvnZone]]]) -> pa.Table:
    if not snaps:
        return pa.table({})
    rows = []
    for asof, zones in snaps:
        for z in zones:
            rows.append(
                (
                    asof,
                    z.lo,
                    z.hi,
                    z.mid,
                    z.poc,
                    z.width,
                    z.hvn_below,
                    z.hvn_above,
                    z.valley_vol,
                    z.profile_volume,
                )
            )
    if not rows:
        return pa.table({})
    a = np.array(rows)
    return pa.table({
        "asof_ts_ns": a[:, 0].astype(np.int64),
        "lo": a[:, 1].astype(np.int64),
        "hi": a[:, 2].astype(np.int64),
        "mid": a[:, 3].astype(np.int64),
        "poc": a[:, 4].astype(np.int64),
        "width": a[:, 5].astype(np.int64),
        "hvn_below": a[:, 6].astype(np.int64),
        "hvn_above": a[:, 7].astype(np.int64),
        "valley_vol": a[:, 8].astype(np.float64),
        "profile_volume": a[:, 9].astype(np.float64),
    })
