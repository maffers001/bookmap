"""Per-fire tip/leg features for EXP-0038 redesign stages."""
from __future__ import annotations

from collections import defaultdict

import numpy as np
import pyarrow as pa

from research.features.hv_extreme import HvExtremeConfig, hv_extreme_events


ENRICH_COLS = [
    "arm_ns",
    "tip_buy_vol",
    "tip_sell_vol",
    "tip_buy_frac",
    "tip_delta",
    "tip_max_order",
    "leg_max_order",
    "leg_max_order_same_side",
    "lookback_vol",
    "lookback_sec",
    "base_rate",
    "leg_rate",
    "leg_rate_ratio",
    "order_p99_lookback",
]


def _empty_enrich() -> dict[str, list]:
    return {c: [] for c in ENRICH_COLS}


def enrich_events(
    trades: pa.Table,
    events: pa.Table,
    cfg: HvExtremeConfig,
) -> pa.Table:
    """Attach tip aggressor / order / rate-normalised leg features to fires.

    Tip stats use prints in the tip band over the lookback window ending at arm.
    Leg order stats use prints outside the tip band between arm and fire.
    """
    if events.num_rows == 0 or trades.num_rows == 0:
        cols = {n: events.column(n) for n in events.column_names}
        empty = _empty_enrich()
        for c, vals in empty.items():
            cols[c] = pa.array(vals, type=pa.float64() if c != "arm_ns" else pa.int64())
        return pa.table(cols)

    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = np.round(trades.column("price").to_numpy().astype(np.float64)).astype(np.int64)
    sz = trades.column("size").to_numpy().astype(np.float64)
    buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)
    oid = trades.column("aggressor_order_id").to_numpy(zero_copy_only=False)
    order = np.argsort(ts, kind="stable")
    ts, px, sz, buy, oid = ts[order], px[order], sz[order], buy[order], oid[order]

    half = cfg.band_ticks()
    lookback_ns = cfg.lookback_ns()
    lookback_sec = max(1.0, cfg.lookback_min * 60.0)

    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    sides = events.column("side").to_numpy().astype(np.int8)
    extremes = events.column("extreme_tick").to_numpy().astype(np.int64)
    leg_secs = events.column("leg_sec").to_numpy().astype(np.float64)
    leg_vols = events.column("leg_vol").to_numpy().astype(np.float64)

    out = _empty_enrich()

    for i in range(len(ev_ts)):
        fire_ns = int(ev_ts[i])
        leg_sec = float(leg_secs[i])
        arm_ns = int(fire_ns - max(0.0, leg_sec) * 1_000_000_000)
        extreme = int(extremes[i])
        side = int(sides[i])
        tip_lo, tip_hi = extreme - half, extreme + half

        lb_lo = int(np.searchsorted(ts, arm_ns - lookback_ns, side="left"))
        arm_i = int(np.searchsorted(ts, arm_ns, side="right"))
        fire_i = int(np.searchsorted(ts, fire_ns, side="right"))

        tip_buy = tip_sell = 0.0
        tip_orders: dict[str, float] = defaultdict(float)
        lookback_vol = 0.0
        order_sizes: list[float] = []
        lb_orders: dict[str, float] = defaultdict(float)

        for j in range(lb_lo, arm_i):
            lookback_vol += float(sz[j])
            o = str(oid[j]) if oid[j] is not None else ""
            if o:
                lb_orders[o] += float(sz[j])
            if tip_lo <= int(px[j]) <= tip_hi:
                if bool(buy[j]):
                    tip_buy += float(sz[j])
                else:
                    tip_sell += float(sz[j])
                if o:
                    tip_orders[o] += float(sz[j])

        tip_tot = tip_buy + tip_sell
        tip_buy_frac = (tip_buy / tip_tot) if tip_tot > 0 else float("nan")
        tip_delta = tip_buy - tip_sell
        tip_max_order = max(tip_orders.values()) if tip_orders else 0.0
        if lb_orders:
            order_sizes = list(lb_orders.values())
            order_p99 = float(np.percentile(order_sizes, 99))
        else:
            order_p99 = float("nan")

        leg_orders: dict[str, float] = defaultdict(float)
        leg_same: dict[str, float] = defaultdict(float)
        for j in range(arm_i, fire_i):
            tick = int(px[j])
            outside = tick > tip_hi if side > 0 else tick < tip_lo
            if not outside:
                continue
            o = str(oid[j]) if oid[j] is not None else ""
            if not o:
                continue
            s = float(sz[j])
            leg_orders[o] += s
            # same-side as tip aggressors: SHORT tip = buyers trapped → same-side buy
            # LONG tip = sellers trapped → same-side sell
            if side < 0 and bool(buy[j]):
                leg_same[o] += s
            elif side > 0 and not bool(buy[j]):
                leg_same[o] += s

        leg_max_order = max(leg_orders.values()) if leg_orders else 0.0
        leg_max_same = max(leg_same.values()) if leg_same else 0.0

        base_rate = lookback_vol / lookback_sec
        leg_rate = (float(leg_vols[i]) / leg_sec) if leg_sec > 1e-6 else float("inf")
        leg_rate_ratio = (leg_rate / base_rate) if base_rate > 1e-9 else float("inf")

        out["arm_ns"].append(arm_ns)
        out["tip_buy_vol"].append(tip_buy)
        out["tip_sell_vol"].append(tip_sell)
        out["tip_buy_frac"].append(tip_buy_frac)
        out["tip_delta"].append(tip_delta)
        out["tip_max_order"].append(float(tip_max_order))
        out["leg_max_order"].append(float(leg_max_order))
        out["leg_max_order_same_side"].append(float(leg_max_same))
        out["lookback_vol"].append(lookback_vol)
        out["lookback_sec"].append(lookback_sec)
        out["base_rate"].append(base_rate)
        out["leg_rate"].append(leg_rate if np.isfinite(leg_rate) else 1e18)
        out["leg_rate_ratio"].append(
            leg_rate_ratio if np.isfinite(leg_rate_ratio) else 1e18
        )
        out["order_p99_lookback"].append(order_p99)

    cols = {n: events.column(n) for n in events.column_names}
    cols["arm_ns"] = pa.array(out["arm_ns"], type=pa.int64())
    for c in ENRICH_COLS:
        if c == "arm_ns":
            continue
        cols[c] = pa.array(out[c], type=pa.float64())
    return pa.table(cols)


def enriched_hv_events(
    trades: pa.Table,
    cfg: HvExtremeConfig | None = None,
    *,
    window_ns: tuple[int, int] | None = None,
    attach_med_r30: bool = True,
) -> pa.Table:
    cfg = cfg or HvExtremeConfig()
    base = hv_extreme_events(
        trades, cfg, window_ns=window_ns, attach_med_r30=attach_med_r30
    )
    return enrich_events(trades, base, cfg)


def filter_events(ev: pa.Table, mask: np.ndarray) -> pa.Table:
    if ev.num_rows == 0:
        return ev
    idx = np.where(np.asarray(mask, dtype=bool))[0]
    if len(idx) == 0:
        return ev.slice(0, 0)
    return ev.take(idx)


def tip_trapped_mask(ev: pa.Table, min_frac: float) -> np.ndarray:
    """SHORT: tip_buy_frac >= min_frac; LONG: tip_buy_frac <= 1 - min_frac."""
    if ev.num_rows == 0:
        return np.array([], dtype=bool)
    side = ev.column("side").to_numpy().astype(np.int8)
    frac = ev.column("tip_buy_frac").to_numpy().astype(np.float64)
    ok = np.zeros(len(side), dtype=bool)
    short = side < 0
    long = side > 0
    ok[short] = frac[short] >= min_frac
    ok[long] = frac[long] <= (1.0 - min_frac)
    ok &= np.isfinite(frac)
    return ok


def tip_against_mask(ev: pa.Table, min_frac: float) -> np.ndarray:
    """Control: delta *with* the move (should be worse)."""
    if ev.num_rows == 0:
        return np.array([], dtype=bool)
    side = ev.column("side").to_numpy().astype(np.int8)
    frac = ev.column("tip_buy_frac").to_numpy().astype(np.float64)
    ok = np.zeros(len(side), dtype=bool)
    short = side < 0
    long = side > 0
    # SHORT with sellers at tip (not trapped buyers)
    ok[short] = frac[short] <= (1.0 - min_frac)
    ok[long] = frac[long] >= min_frac
    ok &= np.isfinite(frac)
    return ok
