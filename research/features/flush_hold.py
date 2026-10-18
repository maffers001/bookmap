"""Flush into resting liquidity + hold (stop-run / sweep reverse).

Bullish: fast down-flush through thin area tags a large BID that holds
(limited further penetration), then price reclaims above the bid → LONG.
Bearish: mirror into large ASK → SHORT.

Distinct from H2 fade-the-level and H7 LVN-touch fade: entry is *with*
the bounce after a flush into the wall, not fading the wall itself.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from research.features.liquidity import LargeLevelConfig, TrackedLevel, run_liquidity_tracker
from research.features.volume_profile import VolumeProfileConfig, causal_lvn_snapshots


@dataclass
class FlushHoldConfig:
    min_flush_ticks: float = 30.0  # ~7.5 NQ pts if tick=1 is one tick; extracts use tick units
    flush_window_ms: int = 2000
    absolute_size: int = 40
    z_score: float = 99.0
    tick_radius: int = 12
    min_execute_into: float = 20.0
    min_consume_ratio: float = 0.3
    max_penetration_ticks: int = 6
    hold_ms: int = 500  # no new extreme beyond wall for this long after contact
    reclaim_ticks: int = 2  # reclaim this many ticks through the wall
    cooldown_ms: int = 5000
    tick_size: float = 1.0
    require_lvn_in_path: bool = False
    lvn_lookback_ms: int = 1_800_000
    # Use looser sandwich for path context (more zones); not the entry
    lvn_require_sandwich: bool = False


def _trades_arrays(trades: pa.Table, tick_size: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = np.round(trades.column("price").to_numpy().astype(np.float64) / tick_size).astype(np.int64)
    sz = trades.column("size").to_numpy().astype(np.float64)
    buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)
    order = np.argsort(ts)
    return ts[order], px[order], sz[order], buy[order]


def _flush_into_level(
    ts: np.ndarray,
    px: np.ndarray,
    contact_ns: int,
    level_price: int,
    side: str,
    *,
    flush_window_ms: int,
    min_flush_ticks: float,
) -> tuple[bool, float, int, int]:
    """Return (ok, flush_ticks, flush_high, flush_low) for path into the level."""
    window_ns = int(flush_window_ms * 1_000_000)
    lo = np.searchsorted(ts, contact_ns - window_ns, side="left")
    hi = np.searchsorted(ts, contact_ns, side="right")
    if hi - lo < 3:
        return False, 0.0, 0, 0
    wpx = px[lo:hi]
    hi_px = int(wpx.max())
    lo_px = int(wpx.min())
    span = hi_px - lo_px
    if span < min_flush_ticks:
        return False, float(span), hi_px, lo_px
    if side == "BID":
        # Down flush: started well above, extreme near/through bid
        if hi_px < level_price + min_flush_ticks * 0.5:
            return False, float(span), hi_px, lo_px
        if lo_px > level_price + 2:
            return False, float(span), hi_px, lo_px
        return True, float(span), hi_px, lo_px
    # ASK: up flush
    if lo_px > level_price - min_flush_ticks * 0.5:
        return False, float(span), hi_px, lo_px
    if hi_px < level_price - 2:
        return False, float(span), hi_px, lo_px
    return True, float(span), hi_px, lo_px


def _held_after_contact(
    ts: np.ndarray,
    px: np.ndarray,
    contact_ns: int,
    level_price: int,
    side: str,
    *,
    hold_ms: int,
    max_penetration_ticks: int,
) -> bool:
    hold_ns = int(hold_ms * 1_000_000)
    lo = np.searchsorted(ts, contact_ns, side="left")
    hi = np.searchsorted(ts, contact_ns + hold_ns, side="right")
    if hi <= lo:
        return False
    wpx = px[lo:hi]
    if side == "BID":
        return int(wpx.min()) >= level_price - max_penetration_ticks
    return int(wpx.max()) <= level_price + max_penetration_ticks


def _reclaim_ts(
    ts: np.ndarray,
    px: np.ndarray,
    contact_ns: int,
    level_price: int,
    side: str,
    *,
    reclaim_ticks: int,
    max_search_ms: int = 5000,
) -> int | None:
    lim_ns = contact_ns + int(max_search_ms * 1_000_000)
    lo = np.searchsorted(ts, contact_ns, side="right")
    hi = np.searchsorted(ts, lim_ns, side="right")
    for j in range(lo, hi):
        if side == "BID" and int(px[j]) >= level_price + reclaim_ticks:
            return int(ts[j])
        if side == "ASK" and int(px[j]) <= level_price - reclaim_ticks:
            return int(ts[j])
    return None


def _path_crossed_lvn(
    snaps: list,
    flush_hi: int,
    flush_lo: int,
    asof_ns: int,
) -> bool:
    if not snaps:
        return False
    zones = []
    for asof, zs in snaps:
        if asof <= asof_ns:
            zones = zs
        else:
            break
    for z in zones:
        # overlap between flush path and LVN zone
        if z.hi >= flush_lo and z.lo <= flush_hi:
            return True
    return False


def flush_into_hold_events(
    trades: pa.Table,
    depth: pa.Table,
    cfg: FlushHoldConfig | None = None,
    *,
    rth_uk_only: bool = True,
) -> pa.Table:
    """Emit continuation-after-flush-hold events (bounce with the reclaim)."""
    cfg = cfg or FlushHoldConfig()
    if trades.num_rows == 0 or depth.num_rows == 0:
        return pa.table({})

    ll_cfg = LargeLevelConfig(
        absolute_size=cfg.absolute_size,
        z_score=cfg.z_score,
        tick_radius=cfg.tick_radius,
    )
    levels = run_liquidity_tracker(depth, trades, cfg=ll_cfg, rth_uk_only=rth_uk_only)
    ts, px, _sz, _buy = _trades_arrays(trades, cfg.tick_size)

    snaps = []
    if cfg.require_lvn_in_path:
        vp = VolumeProfileConfig(
            lookback_ms=cfg.lvn_lookback_ms,
            require_hvn_sandwich=cfg.lvn_require_sandwich,
            min_profile_volume=200.0,
            edge_margin_ticks=4,
            lvn_frac_of_peak=0.40,
        )
        snaps = causal_lvn_snapshots(trades, vp)

    events_ts: list[int] = []
    events_side: list[int] = []
    flush_spans: list[float] = []
    level_prices: list[int] = []
    exec_intos: list[float] = []
    consumes: list[float] = []
    pens: list[int] = []
    lvn_flags: list[int] = []
    last_fire = -10**18
    cooldown_ns = int(cfg.cooldown_ms * 1_000_000)

    for lv in levels:
        if not lv.contacted or lv.contact_ns <= 0:
            continue
        if lv.executed_into < cfg.min_execute_into:
            continue
        if lv.consume_ratio() < cfg.min_consume_ratio:
            continue
        if lv.max_penetration_ticks > cfg.max_penetration_ticks:
            continue
        if lv.side not in ("BID", "ASK"):
            continue

        ok, span, fhi, flo = _flush_into_level(
            ts,
            px,
            lv.contact_ns,
            lv.price,
            lv.side,
            flush_window_ms=cfg.flush_window_ms,
            min_flush_ticks=cfg.min_flush_ticks,
        )
        if not ok:
            continue
        if not _held_after_contact(
            ts,
            px,
            lv.contact_ns,
            lv.price,
            lv.side,
            hold_ms=cfg.hold_ms,
            max_penetration_ticks=cfg.max_penetration_ticks,
        ):
            continue

        crossed = _path_crossed_lvn(snaps, fhi, flo, lv.contact_ns) if snaps else False
        if cfg.require_lvn_in_path and not crossed:
            continue

        reclaim = _reclaim_ts(
            ts,
            px,
            lv.contact_ns,
            lv.price,
            lv.side,
            reclaim_ticks=cfg.reclaim_ticks,
        )
        if reclaim is None:
            continue
        if reclaim - last_fire < cooldown_ns:
            continue

        # Bounce continuation: BID hold → LONG, ASK hold → SHORT
        side = 1 if lv.side == "BID" else -1
        events_ts.append(reclaim)
        events_side.append(side)
        flush_spans.append(span)
        level_prices.append(lv.price)
        exec_intos.append(float(lv.executed_into))
        consumes.append(float(lv.consume_ratio()))
        pens.append(int(lv.max_penetration_ticks))
        lvn_flags.append(1 if crossed else 0)
        last_fire = reclaim

    if not events_ts:
        return pa.table({})
    # stamp = last input used (reclaim through wall)
    return pa.table({
        "event_ts_ns": np.asarray(events_ts, dtype=np.int64),
        "side": np.asarray(events_side, dtype=np.int8),
        "event_type": ["flush_into_hold"] * len(events_ts),
        "flush_ticks": np.asarray(flush_spans, dtype=np.float64),
        "level_price_ticks": np.asarray(level_prices, dtype=np.int64),
        "executed_into": np.asarray(exec_intos, dtype=np.float64),
        "consume_ratio": np.asarray(consumes, dtype=np.float64),
        "max_penetration_ticks": np.asarray(pens, dtype=np.int64),
        "lvn_in_path": np.asarray(lvn_flags, dtype=np.int8),
    })


def cross_confirm_flush_hold(
    nq_events: pa.Table,
    es_events: pa.Table,
    *,
    sync_ms: int = 2000,
) -> pa.Table:
    if nq_events.num_rows == 0 or es_events.num_rows == 0:
        return pa.table({})
    nq_ts = nq_events.column("event_ts_ns").to_numpy().astype(np.int64)
    nq_side = nq_events.column("side").to_numpy()
    es_ts = es_events.column("event_ts_ns").to_numpy().astype(np.int64)
    es_side = es_events.column("side").to_numpy()
    order = np.argsort(es_ts)
    es_ts, es_side = es_ts[order], es_side[order]
    window = int(sync_ms * 1_000_000)
    keep = []
    lags = []
    for i in range(len(nq_ts)):
        lo = np.searchsorted(es_ts, nq_ts[i] - window, side="left")
        hi = np.searchsorted(es_ts, nq_ts[i] + window, side="right")
        best = None
        for j in range(lo, hi):
            if int(es_side[j]) != int(nq_side[i]):
                continue
            lag = abs(int(es_ts[j] - nq_ts[i]))
            if best is None or lag < best:
                best = lag
        if best is not None:
            keep.append(i)
            lags.append(best / 1e6)
    if not keep:
        return pa.table({})
    idx = np.asarray(keep, dtype=np.int64)
    out = nq_events.take(idx)
    if "es_lag_ms" in out.column_names:
        out = out.drop("es_lag_ms")
    return out.append_column("es_lag_ms", pa.array(lags, type=pa.float64()))
