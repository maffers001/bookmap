"""Consumed-level acceptance arms (H81 / EXP-0090).

After a lifecycle ``consumed`` contact, classify path at contact+accept_sec:
  left_behind — ≥1 pt past P, never returned, pullback ≤ 25% of peak-past
  retest_hold — returned within 1.5 pts of P without trading through
  breach_back — traded back through P (P&C fade candidate)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

NS = 1_000_000_000
PTS_PER_TICK = 0.25


@dataclass
class ConsumedAcceptConfig:
    accept_sec: float = 5.0
    clear_pts: float = 1.0
    approach_pts: float = 1.5
    max_pullback_frac: float = 0.25
    pts_per_tick: float = PTS_PER_TICK


def classify_acceptance(
    ts: np.ndarray,
    px_pts: np.ndarray,
    *,
    contact_ns: int,
    price_pts: float,
    side: int,  # +1 = broke upward through ASK; -1 = broke downward through BID
    cfg: ConsumedAcceptConfig,
) -> dict:
    """side = continuation direction through the level."""
    t1 = contact_ns + int(cfg.accept_sec * NS)
    lo = int(np.searchsorted(ts, contact_ns, side="left"))
    hi = int(np.searchsorted(ts, t1, side="right"))
    empty = {
        "arm": "no_path", "ca_left_behind": 0, "ca_retest_hold": 0, "ca_breach_back": 0,
        "peak_past_pts": float("nan"), "pullback_frac": float("nan"), "accept_ts_ns": t1,
    }
    if hi <= lo:
        return empty
    past = side * (px_pts[lo:hi] - price_pts)
    peak = float(past.max())
    if peak < cfg.clear_pts:
        return {**empty, "arm": "never_cleared", "peak_past_pts": peak}

    peak_i = int(np.argmax(past))
    after = past[peak_i:]
    pullback = float(peak - after.min()) if len(after) else 0.0
    pull_frac = pullback / peak if peak > 0 else 1.0
    # breach: past <= 0 after having cleared
    crossed = np.where(past >= cfg.clear_pts)[0]
    i0 = int(crossed[0])
    post = past[i0:]
    breached = bool((post <= 0).any())
    approached = bool(((post > 0) & (post <= cfg.approach_pts)).any())

    if breached:
        arm = "breach_back"
        # stamp = first breach
        bi = int(np.where(post <= 0)[0][0])
        accept_ts = int(ts[lo + i0 + bi])
    elif (not approached) and pull_frac <= cfg.max_pullback_frac:
        arm = "left_behind"
        accept_ts = t1
    elif approached and not breached:
        arm = "retest_hold"
        ai = int(np.where((post > 0) & (post <= cfg.approach_pts))[0][0])
        accept_ts = int(ts[lo + i0 + ai])
    else:
        arm = "other"
        accept_ts = t1

    return {
        "arm": arm,
        "ca_left_behind": 1 if arm == "left_behind" else 0,
        "ca_retest_hold": 1 if arm == "retest_hold" else 0,
        "ca_breach_back": 1 if arm == "breach_back" else 0,
        "peak_past_pts": peak,
        "pullback_frac": pull_frac,
        "accept_ts_ns": accept_ts,
    }


def consumed_acceptance_events(
    lifecycle: pa.Table,
    trades: pa.Table,
    *,
    cfg: ConsumedAcceptConfig | None = None,
    state_filter: str = "consumed",
) -> pa.Table:
    cfg = cfg or ConsumedAcceptConfig()
    if lifecycle.num_rows == 0 or trades.num_rows == 0:
        return pa.table({})
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64) * cfg.pts_per_tick
    o = np.argsort(ts, kind="stable")
    ts, px = ts[o], px[o]

    states = lifecycle.column("state").to_pylist()
    rows: list[dict] = []
    for i in range(lifecycle.num_rows):
        if states[i] != state_filter:
            continue
        if int(lifecycle.column("seeded")[i].as_py()) == 1:
            continue
        contact_ns = int(lifecycle.column("event_ts_ns")[i].as_py())
        price_ticks = int(lifecycle.column("price_ticks")[i].as_py())
        level_side = lifecycle.column("level_side")[i].as_py()
        # continuation through ASK (buy) → side +1; through BID → side -1
        side = 1 if level_side == "ASK" else -1
        price_pts = price_ticks * cfg.pts_per_tick
        acc = classify_acceptance(
            ts, px, contact_ns=contact_ns, price_pts=price_pts, side=side, cfg=cfg,
        )
        if acc["arm"] in ("no_path", "never_cleared", "other"):
            continue
        rows.append({
            "event_ts_ns": acc["accept_ts_ns"],
            "contact_ts_ns": contact_ns,
            "side": side if acc["arm"] != "breach_back" else -side,  # fade on breach
            "cont_side": side,
            "level_side": level_side,
            "price_ticks": price_ticks,
            "arm": acc["arm"],
            "peak_past_pts": acc["peak_past_pts"],
            "pullback_frac": acc["pullback_frac"],
            "ca_left_behind": acc["ca_left_behind"],
            "ca_retest_hold": acc["ca_retest_hold"],
            "ca_breach_back": acc["ca_breach_back"],
            "executed": float(lifecycle.column("executed")[i].as_py()),
            "dominance": float(lifecycle.column("dominance")[i].as_py()),
            "order_id": lifecycle.column("order_id")[i].as_py(),
            "event_type": "consumed_acceptance",
        })
    if not rows:
        return pa.table({})
    return pa.table({
        "event_ts_ns": np.array([r["event_ts_ns"] for r in rows], dtype=np.int64),
        "contact_ts_ns": np.array([r["contact_ts_ns"] for r in rows], dtype=np.int64),
        "side": np.array([r["side"] for r in rows], dtype=np.int8),
        "cont_side": np.array([r["cont_side"] for r in rows], dtype=np.int8),
        "level_side": [r["level_side"] for r in rows],
        "price_ticks": np.array([r["price_ticks"] for r in rows], dtype=np.int64),
        "arm": [r["arm"] for r in rows],
        "peak_past_pts": np.array([r["peak_past_pts"] for r in rows], dtype=np.float64),
        "pullback_frac": np.array([r["pullback_frac"] for r in rows], dtype=np.float64),
        "ca_left_behind": np.array([r["ca_left_behind"] for r in rows], dtype=np.int8),
        "ca_retest_hold": np.array([r["ca_retest_hold"] for r in rows], dtype=np.int8),
        "ca_breach_back": np.array([r["ca_breach_back"] for r in rows], dtype=np.int8),
        "executed": np.array([r["executed"] for r in rows], dtype=np.float64),
        "dominance": np.array([r["dominance"] for r in rows], dtype=np.float64),
        "order_id": [r["order_id"] for r in rows],
        "event_type": [r["event_type"] for r in rows],
    })
