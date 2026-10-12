"""LVN touch event detectors (dual-market + NQ-only)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from research.features.volume_profile import LvnZone, VolumeProfileConfig, causal_lvn_snapshots


@dataclass
class TouchConfig:
    sync_ms: int = 2000
    touch_cooldown_ms: int = 5000
    tick_size: float = 1.0


def _active_zones(
    snaps: list[tuple[int, list[LvnZone]]],
    ts_ns: int,
) -> list[LvnZone]:
    """Latest snapshot with asof <= ts_ns."""
    best: list[LvnZone] = []
    for asof, zones in snaps:
        if asof <= ts_ns:
            best = zones
        else:
            break
    return best


def _approach_side(prev_px: float, zone: LvnZone) -> int | None:
    """+1 approached from below (testing upside LVN → fade SHORT);
    -1 from above (testing downside LVN → fade LONG)."""
    if prev_px < zone.lo:
        return 1  # from below → short fade
    if prev_px > zone.hi:
        return -1  # from above → long fade
    return None


def detect_lvn_touches(
    trades: pa.Table,
    snaps: list[tuple[int, list[LvnZone]]],
    cfg: TouchConfig | None = None,
) -> pa.Table:
    """Single-market LVN touch events (fade side)."""
    cfg = cfg or TouchConfig()
    if trades.num_rows == 0 or not snaps:
        return pa.table({})

    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64)
    order = np.argsort(ts)
    ts, px = ts[order], px[order]
    tick_px = np.round(px / cfg.tick_size).astype(np.int64)

    cooldown_ns = int(cfg.touch_cooldown_ms * 1_000_000)
    last_fire = -10**18
    events = []

    for i in range(1, len(ts)):
        zones = _active_zones(snaps, int(ts[i]))
        if not zones:
            continue
        p = int(tick_px[i])
        prev = float(tick_px[i - 1])
        for z in zones:
            if not (z.lo <= p <= z.hi):
                continue
            approach = _approach_side(prev, z)
            if approach is None:
                continue
            if ts[i] - last_fire < cooldown_ns:
                continue
            # fade: approach +1 → side -1 (short), approach -1 → side +1 (long)
            side = -approach
            events.append({
                "event_ts_ns": int(ts[i]),
                "side": side,
                "approach": approach,
                "lvn_lo": z.lo,
                "lvn_hi": z.hi,
                "lvn_mid": z.mid,
                "lvn_poc": z.poc,
                "entry_tick": p,
            })
            last_fire = int(ts[i])
            break

    if not events:
        return pa.table({})
    return pa.table({
        "event_ts_ns": np.array([e["event_ts_ns"] for e in events], dtype=np.int64),
        "side": np.array([e["side"] for e in events], dtype=np.int8),
        "event_type": ["lvn_touch"] * len(events),
        "approach": np.array([e["approach"] for e in events], dtype=np.int8),
        "lvn_lo": np.array([e["lvn_lo"] for e in events], dtype=np.int64),
        "lvn_hi": np.array([e["lvn_hi"] for e in events], dtype=np.int64),
        "lvn_mid": np.array([e["lvn_mid"] for e in events], dtype=np.int64),
        "lvn_poc": np.array([e["lvn_poc"] for e in events], dtype=np.int64),
        "entry_tick": np.array([e["entry_tick"] for e in events], dtype=np.int64),
    })


def dual_lvn_touch_events(
    nq_trades: pa.Table,
    es_trades: pa.Table,
    vp_cfg: VolumeProfileConfig | None = None,
    touch_cfg: TouchConfig | None = None,
) -> tuple[pa.Table, pa.Table, dict]:
    """
    Returns (dual_events, nq_only_events, meta).
    Dual: NQ touch with ES touch same approach within sync_ms.
    """
    vp_cfg = vp_cfg or VolumeProfileConfig()
    touch_cfg = touch_cfg or TouchConfig()

    nq_snaps = causal_lvn_snapshots(nq_trades, vp_cfg)
    es_snaps = causal_lvn_snapshots(es_trades, vp_cfg)
    nq_touches = detect_lvn_touches(nq_trades, nq_snaps, touch_cfg)
    es_touches = detect_lvn_touches(es_trades, es_snaps, touch_cfg)

    meta = {
        "nq_snapshots": len(nq_snaps),
        "es_snapshots": len(es_snaps),
        "nq_zones_last": len(nq_snaps[-1][1]) if nq_snaps else 0,
        "es_zones_last": len(es_snaps[-1][1]) if es_snaps else 0,
        "nq_touches": int(nq_touches.num_rows),
        "es_touches": int(es_touches.num_rows),
    }

    if nq_touches.num_rows == 0:
        return pa.table({}), nq_touches, meta
    if es_touches.num_rows == 0:
        return pa.table({}), nq_touches, meta

    nq_ts = nq_touches.column("event_ts_ns").to_numpy()
    nq_side = nq_touches.column("side").to_numpy()
    nq_app = nq_touches.column("approach").to_numpy()
    es_ts = es_touches.column("event_ts_ns").to_numpy()
    es_app = es_touches.column("approach").to_numpy()
    window = int(touch_cfg.sync_ms * 1_000_000)

    order = np.argsort(es_ts)
    es_ts = es_ts[order]
    es_app = es_app[order]

    keep = []
    lags = []
    for i in range(len(nq_ts)):
        lo = np.searchsorted(es_ts, nq_ts[i] - window, side="left")
        hi = np.searchsorted(es_ts, nq_ts[i] + window, side="right")
        best = None
        best_abs = None
        for j in range(lo, hi):
            if es_app[j] != nq_app[i]:
                continue
            lag = abs(int(es_ts[j] - nq_ts[i]))
            if best_abs is None or lag < best_abs:
                best_abs = lag
                best = j
        if best is not None:
            keep.append(i)
            lags.append(best_abs / 1e6)

    if not keep:
        return pa.table({}), nq_touches, meta

    idx = np.asarray(keep, dtype=np.int64)
    dual = pa.table({
        "event_ts_ns": nq_ts[idx].astype(np.int64),
        "side": nq_side[idx].astype(np.int8),
        "event_type": ["dual_lvn_touch"] * len(idx),
        "approach": nq_app[idx].astype(np.int8),
        "lvn_lo": nq_touches.column("lvn_lo").take(idx),
        "lvn_hi": nq_touches.column("lvn_hi").take(idx),
        "lvn_mid": nq_touches.column("lvn_mid").take(idx),
        "lvn_poc": nq_touches.column("lvn_poc").take(idx),
        "entry_tick": nq_touches.column("entry_tick").take(idx),
        "es_lag_ms": np.asarray(lags, dtype=np.float64),
    })
    meta["dual_touches"] = int(dual.num_rows)
    return dual, nq_touches, meta


def annotate_beyond_and_flip(
    events: pa.Table,
    levels: list,
    *,
    beyond_ticks: int = 8,
    max_beyond_consume: float = 0.25,
    flip_window_ms: int = 2000,
    min_flip_executed: float = 10.0,
) -> pa.Table:
    """Add beyond_untraded and flip flags using LargeLevelTracker closed/open levels."""
    if events.num_rows == 0:
        return events

    # All closed levels (contacted or withdrawn) for beyond; contacted for flip
    pool = list(levels)
    hits = [lv for lv in pool if getattr(lv, "contacted", False) and lv.contact_ns > 0]
    hits.sort(key=lambda x: x.contact_ns)

    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    sides = events.column("side").to_numpy()
    mids = events.column("lvn_mid").to_numpy() if "lvn_mid" in events.column_names else np.zeros(len(ev_ts))
    los = events.column("lvn_lo").to_numpy() if "lvn_lo" in events.column_names else mids
    his = events.column("lvn_hi").to_numpy() if "lvn_hi" in events.column_names else mids

    flip_ns = int(flip_window_ms * 1_000_000)
    beyond_flags = []
    flip_flags = []
    beyond_consume = []

    for i, t0 in enumerate(ev_ts):
        side = int(sides[i])  # fade side: +1 long, -1 short
        # Upside LVN (short fade): beyond = asks above hi; flip = bids below start trading
        # Downside LVN (long fade): beyond = bids below lo; flip = asks above start trading
        has_beyond = False
        best_cons = 1.0
        for lv in pool:
            if lv.first_seen_ns > t0:
                continue
            # Approximate consume *at touch*: ignore post-touch fills
            if (not lv.contacted) or lv.contact_ns > t0:
                cons = 0.0
                exec_into = 0.0
            else:
                cons = lv.consume_ratio() if lv.initial_size > 0 else 1.0
                exec_into = float(lv.executed_into)
            in_beyond = False
            if side < 0:  # short: beyond is ASK above zone
                if lv.side == "ASK" and lv.price >= int(his[i]) and lv.price <= int(his[i]) + beyond_ticks:
                    in_beyond = True
            else:  # long: beyond is BID below zone
                if lv.side == "BID" and lv.price <= int(los[i]) and lv.price >= int(los[i]) - beyond_ticks:
                    in_beyond = True
            if in_beyond and (
                cons <= max_beyond_consume or exec_into < max(1.0, 0.25 * lv.initial_size)
            ):
                has_beyond = True
                best_cons = min(best_cons, cons)

        # Flip: opposing side starts trading after touch
        has_flip = False
        for lv in hits:
            if lv.contact_ns < t0 or lv.contact_ns > t0 + flip_ns:
                continue
            if lv.executed_into < min_flip_executed:
                continue
            if side < 0:  # short: want BID trading (support becomes fuel on way down)
                if lv.side == "BID" and lv.price <= int(mids[i]):
                    has_flip = True
                    break
            else:  # long: want ASK trading
                if lv.side == "ASK" and lv.price >= int(mids[i]):
                    has_flip = True
                    break

        beyond_flags.append(1 if has_beyond else 0)
        flip_flags.append(1 if has_flip else 0)
        beyond_consume.append(float(best_cons if has_beyond else np.nan))

    out = events
    for name in ("beyond_untraded", "flip", "beyond_consume"):
        if name in out.column_names:
            out = out.drop(name)
    out = out.append_column("beyond_untraded", pa.array(beyond_flags, type=pa.int8()))
    out = out.append_column("flip", pa.array(flip_flags, type=pa.int8()))
    out = out.append_column("beyond_consume", pa.array(beyond_consume, type=pa.float64()))
    return out
