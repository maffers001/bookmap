"""Per-fire tip/leg features for EXP-0038 redesign stages."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.features.hv_extreme import HvExtremeConfig, hv_extreme_events

# Tip aggressor / order stats use a short window ending at arm (not full lookback).
# Lookback is still used for base_rate and order_p99.
TIP_WINDOW_SEC = 60.0
TIP_WINDOW_SEC_ALT = 30.0
# Min resting order size (contracts) counted as an "absorber" at the tip.
# NQ MBO: p90 of max resting at tip is ~25, so 50 is unreachable; use 10.
MBO_MIN_REST = 10.0

ENRICH_COLS = [
    "arm_ns",
    "tip_buy_vol",
    "tip_sell_vol",
    "tip_buy_frac",
    "tip_delta",
    "tip_max_order",
    "tip_buy_frac_30s",
    "tip_max_order_30s",
    "leg_max_order",
    "leg_max_order_same_side",
    "lookback_vol",
    "lookback_sec",
    "base_rate",
    "leg_rate",
    "leg_rate_ratio",
    "order_p99_lookback",
    "mbo_tip_max_resting",
    "mbo_tip_filled",  # volume absorbed by large (>=50) resting orders at tip
]


def _empty_enrich() -> dict[str, list]:
    return {c: [] for c in ENRICH_COLS}


def _tip_stats(
    ts: np.ndarray,
    px: np.ndarray,
    sz: np.ndarray,
    buy: np.ndarray,
    oid: np.ndarray,
    *,
    lo_i: int,
    hi_i: int,
    tip_lo: int,
    tip_hi: int,
) -> tuple[float, float, float, float, float]:
    tip_buy = tip_sell = 0.0
    tip_orders: dict[str, float] = defaultdict(float)
    for j in range(lo_i, hi_i):
        if not (tip_lo <= int(px[j]) <= tip_hi):
            continue
        s = float(sz[j])
        if bool(buy[j]):
            tip_buy += s
        else:
            tip_sell += s
        o = str(oid[j]) if oid[j] is not None else ""
        if o:
            tip_orders[o] += s
    tip_tot = tip_buy + tip_sell
    frac = (tip_buy / tip_tot) if tip_tot > 0 else float("nan")
    delta = tip_buy - tip_sell
    tip_max = max(tip_orders.values()) if tip_orders else 0.0
    return tip_buy, tip_sell, frac, delta, float(tip_max)


def enrich_events(
    trades: pa.Table,
    events: pa.Table,
    cfg: HvExtremeConfig,
    *,
    mbo: pa.Table | None = None,
    tip_window_sec: float = TIP_WINDOW_SEC,
) -> pa.Table:
    """Attach tip aggressor / order / rate-normalised leg features to fires.

    Tip stats use prints in the tip band over the last ``tip_window_sec`` before
    arm (default 60s) — not the full lookback. Leg order stats use prints
    outside the tip band between arm and fire. Optional ``mbo`` adds resting
    size / fill at the tip tick near arm.
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
    tip_win_ns = int(tip_window_sec * 1_000_000_000)
    tip_win_30_ns = int(TIP_WINDOW_SEC_ALT * 1_000_000_000)

    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    sides = events.column("side").to_numpy().astype(np.int8)
    extremes = events.column("extreme_tick").to_numpy().astype(np.int64)
    leg_secs = events.column("leg_sec").to_numpy().astype(np.float64)
    leg_vols = events.column("leg_vol").to_numpy().astype(np.float64)

    mbo_ready = None
    if mbo is not None and mbo.num_rows > 0:
        mbo_ready = _prepare_mbo(mbo)

    out = _empty_enrich()

    for i in range(len(ev_ts)):
        fire_ns = int(ev_ts[i])
        leg_sec = float(leg_secs[i])
        arm_ns = int(fire_ns - max(0.0, leg_sec) * 1_000_000_000)
        extreme = int(extremes[i])
        side = int(sides[i])
        tip_lo, tip_hi = extreme - half, extreme + half

        lb_lo = int(np.searchsorted(ts, arm_ns - lookback_ns, side="left"))
        tip_lo_i = int(np.searchsorted(ts, arm_ns - tip_win_ns, side="left"))
        tip_30_i = int(np.searchsorted(ts, arm_ns - tip_win_30_ns, side="left"))
        arm_i = int(np.searchsorted(ts, arm_ns, side="right"))
        fire_i = int(np.searchsorted(ts, fire_ns, side="right"))

        lookback_vol = 0.0
        lb_orders: dict[str, float] = defaultdict(float)
        for j in range(lb_lo, arm_i):
            lookback_vol += float(sz[j])
            o = str(oid[j]) if oid[j] is not None else ""
            if o:
                lb_orders[o] += float(sz[j])

        tip_buy, tip_sell, tip_buy_frac, tip_delta, tip_max_order = _tip_stats(
            ts, px, sz, buy, oid,
            lo_i=tip_lo_i, hi_i=arm_i, tip_lo=tip_lo, tip_hi=tip_hi,
        )
        _, _, tip_frac_30, _, tip_max_30 = _tip_stats(
            ts, px, sz, buy, oid,
            lo_i=tip_30_i, hi_i=arm_i, tip_lo=tip_lo, tip_hi=tip_hi,
        )
        order_p99 = (
            float(np.percentile(list(lb_orders.values()), 99)) if lb_orders else float("nan")
        )

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
            # SHORT tip = trapped buyers → same-side buy on leg; LONG mirror
            if side < 0 and bool(buy[j]):
                leg_same[o] += s
            elif side > 0 and not bool(buy[j]):
                leg_same[o] += s

        leg_max_order = max(leg_orders.values()) if leg_orders else 0.0
        leg_max_same = max(leg_same.values()) if leg_same else 0.0

        base_rate = lookback_vol / lookback_sec
        leg_rate = (float(leg_vols[i]) / leg_sec) if leg_sec > 1e-6 else float("inf")
        leg_rate_ratio = (leg_rate / base_rate) if base_rate > 1e-9 else float("inf")

        mbo_max = mbo_filled = float("nan")
        if mbo_ready is not None:
            mbo_max, mbo_filled = _mbo_at_tip(
                mbo_ready,
                extreme,
                side,
                arm_ns,
                half=half,
                trades=(ts, px, sz, buy),
            )

        out["arm_ns"].append(arm_ns)
        out["tip_buy_vol"].append(tip_buy)
        out["tip_sell_vol"].append(tip_sell)
        out["tip_buy_frac"].append(tip_buy_frac)
        out["tip_delta"].append(tip_delta)
        out["tip_max_order"].append(float(tip_max_order))
        out["tip_buy_frac_30s"].append(tip_frac_30)
        out["tip_max_order_30s"].append(float(tip_max_30))
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
        out["mbo_tip_max_resting"].append(mbo_max)
        out["mbo_tip_filled"].append(mbo_filled)

    cols = {n: events.column(n) for n in events.column_names}
    cols["arm_ns"] = pa.array(out["arm_ns"], type=pa.int64())
    for c in ENRICH_COLS:
        if c == "arm_ns":
            continue
        cols[c] = pa.array(out[c], type=pa.float64())
    return pa.table(cols)


def _prepare_mbo(mbo: pa.Table) -> dict:
    ts = mbo.column("ts_ns").to_numpy().astype(np.int64)
    order = np.argsort(ts, kind="stable")
    return {
        "ts": ts[order],
        "op": mbo.column("op").take(order).to_numpy(zero_copy_only=False),
        "oid": mbo.column("order_id").take(order).to_numpy(zero_copy_only=False),
        "is_bid": mbo.column("is_bid").take(order).to_numpy().astype(bool),
        "px": mbo.column("price_ticks").take(order).to_numpy().astype(np.int64),
        "sz": mbo.column("size").take(order).to_numpy().astype(np.float64),
    }


def _mbo_at_tip(
    mbo: dict,
    tip_tick: int,
    side: int,
    arm_ns: int,
    *,
    half: int,
    lookback_sec: float = 60.0,
    trades: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None = None,
    min_rest: float | None = None,
) -> tuple[float, float]:
    """Max resting size at tip band + volume absorbed by large resting orders.

    The MBO feed carries only send/replace/cancel (no execute ops), so
    "absorbed" is derived by joining each large resting order's lifetime
    [send_ns, cancel_ns or arm] at its price with aggressive trades at that
    tick on the opposite side (buy aggressor hits a resting ask, etc.).

    SHORT tip (peak): resting **asks** at tip absorb buy aggression.
    LONG tip (trough): resting **bids** at tip absorb sell aggression.
    """
    if min_rest is None:
        min_rest = MBO_MIN_REST
    want_bid = side > 0  # LONG trough → bids absorb sells; SHORT peak → asks absorb buys
    lo_ns = arm_ns - int(lookback_sec * 1e9)
    ts = mbo["ts"]
    lo = int(np.searchsorted(ts, lo_ns, side="left"))
    hi = int(np.searchsorted(ts, arm_ns, side="right"))
    tip_lo, tip_hi = tip_tick - half, tip_tick + half

    # Track lifetime of resting orders at tip band: oid -> [px, size, start_ns, end_ns]
    live: dict[str, list] = {}
    done: list[list] = []
    max_rest = 0.0
    for j in range(lo, hi):
        if bool(mbo["is_bid"][j]) != want_bid:
            continue
        px = int(mbo["px"][j])
        oid = str(mbo["oid"][j]) if mbo["oid"][j] is not None else ""
        if not oid:
            continue
        op = str(mbo["op"][j]).lower()
        sz = float(mbo["sz"][j])
        t = int(ts[j])
        in_band = tip_lo <= px <= tip_hi
        if op in ("send", "replace", "modify"):
            if oid in live:
                rec = live.pop(oid)
                rec[3] = t
                done.append(rec)
            if in_band:
                live[oid] = [px, sz, t, 0]
                if sz > max_rest:
                    max_rest = sz
        elif op in ("cancel", "delete"):
            if oid in live:
                rec = live.pop(oid)
                rec[3] = t
                done.append(rec)
    for rec in live.values():
        rec[3] = arm_ns
        done.append(rec)

    absorbed = 0.0
    if trades is not None and done:
        t_ts, t_px, t_sz, t_buy = trades
        # Aggressor opposite to resting side: resting bid absorbs sell aggressors
        want_buy_aggr = not want_bid
        for px, sz, t0, t1 in done:
            if sz < min_rest or t1 <= t0:
                continue
            a = int(np.searchsorted(t_ts, t0, side="left"))
            b = int(np.searchsorted(t_ts, t1, side="right"))
            if b <= a:
                continue
            seg_px = t_px[a:b]
            seg_buy = t_buy[a:b]
            m = (seg_px == px) & (seg_buy == want_buy_aggr)
            if m.any():
                absorbed += float(t_sz[a:b][m].sum())
    return float(max_rest), float(absorbed)


def load_mbo_nq(
    parquet_dir: Path,
    sid: str,
    *,
    ts_lo: int | None = None,
    ts_hi: int | None = None,
) -> pa.Table:
    path = parquet_dir / f"session_id={sid}" / "mbo.parquet"
    if not path.exists() or path.stat().st_size == 0:
        return pa.table({})
    cols = ["ts_ns", "role", "op", "order_id", "is_bid", "price_ticks", "size"]
    filters: list = [("role", "=", "NQ")]
    if ts_lo is not None:
        filters.append(("ts_ns", ">=", int(ts_lo)))
    if ts_hi is not None:
        filters.append(("ts_ns", "<=", int(ts_hi)))
    try:
        t = pq.read_table(path, columns=cols, filters=filters)
    except Exception:
        t = pq.read_table(path, columns=cols)
        if "role" in t.column_names:
            roles = t.column("role").to_numpy(zero_copy_only=False)
            idx = np.where(roles == "NQ")[0]
            t = t.take(idx) if len(idx) else t.slice(0, 0)
        if t.num_rows and (ts_lo is not None or ts_hi is not None):
            tsn = t.column("ts_ns").to_numpy().astype(np.int64)
            m = np.ones(len(tsn), dtype=bool)
            if ts_lo is not None:
                m &= tsn >= ts_lo
            if ts_hi is not None:
                m &= tsn <= ts_hi
            t = t.take(np.where(m)[0])
    return t


def enriched_hv_events(
    trades: pa.Table,
    cfg: HvExtremeConfig | None = None,
    *,
    window_ns: tuple[int, int] | None = None,
    attach_med_r30: bool = True,
    mbo: pa.Table | None = None,
) -> pa.Table:
    cfg = cfg or HvExtremeConfig()
    base = hv_extreme_events(
        trades, cfg, window_ns=window_ns, attach_med_r30=attach_med_r30
    )
    return enrich_events(trades, base, cfg, mbo=mbo)


def filter_events(ev: pa.Table, mask: np.ndarray) -> pa.Table:
    if ev.num_rows == 0:
        return ev
    idx = np.where(np.asarray(mask, dtype=bool))[0]
    if len(idx) == 0:
        return ev.slice(0, 0)
    return ev.take(idx)


def tip_trapped_mask(ev: pa.Table, min_frac: float, col: str = "tip_buy_frac") -> np.ndarray:
    """SHORT: tip_buy_frac >= min_frac; LONG: tip_buy_frac <= 1 - min_frac."""
    if ev.num_rows == 0:
        return np.array([], dtype=bool)
    side = ev.column("side").to_numpy().astype(np.int8)
    frac = ev.column(col).to_numpy().astype(np.float64)
    ok = np.zeros(len(side), dtype=bool)
    short = side < 0
    long_ = side > 0
    ok[short] = frac[short] >= min_frac
    ok[long_] = frac[long_] <= (1.0 - min_frac)
    ok &= np.isfinite(frac)
    return ok


def tip_against_mask(ev: pa.Table, min_frac: float, col: str = "tip_buy_frac") -> np.ndarray:
    """Control: delta with the move (should be worse)."""
    if ev.num_rows == 0:
        return np.array([], dtype=bool)
    side = ev.column("side").to_numpy().astype(np.int8)
    frac = ev.column(col).to_numpy().astype(np.float64)
    ok = np.zeros(len(side), dtype=bool)
    short = side < 0
    long_ = side > 0
    ok[short] = frac[short] <= (1.0 - min_frac)
    ok[long_] = frac[long_] >= min_frac
    ok &= np.isfinite(frac)
    return ok
