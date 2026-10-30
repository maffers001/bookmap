"""Defended-retest events from MBO lifecycle contacts (H80 / EXP-0089).

Geometry: after a primary (large∧dominant) contact at P, price departs
≥ depart_pts, then returns within tol_pts of P within max_retest_sec
without trading through P by > through_ticks. Arms classified at retest
arrival from stack/MBO state known at that stamp.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from research.features.mbo_lifecycle import LifecycleConfig

NS = 1_000_000_000
PTS_PER_TICK = 0.25


@dataclass
class DefendedRetestConfig:
    depart_pts: float = 8.0
    tol_pts: float = 0.5
    max_retest_sec: float = 300.0
    through_ticks: int = 2
    defended_remain: float = 0.50
    completed_remain: float = 0.25
    approach_sec: float = 10.0
    placebo_shift_pts: float = 40.0
    pts_per_tick: float = PTS_PER_TICK
    min_executed: float = 10.0


def _price_path(trades: pa.Table) -> tuple[np.ndarray, np.ndarray]:
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64)  # tick units
    o = np.argsort(ts, kind="stable")
    return ts[o], px[o]


def find_retest(
    ts: np.ndarray,
    px_ticks: np.ndarray,
    *,
    contact_ns: int,
    price_ticks: int,
    level_is_bid: bool,
    cfg: DefendedRetestConfig,
) -> dict | None:
    """Return retest stamp info or None if geometry fails."""
    i0 = int(np.searchsorted(ts, contact_ns, side="left"))
    if i0 >= len(ts):
        return None
    p0 = float(price_ticks)
    depart = cfg.depart_pts / cfg.pts_per_tick
    tol = cfg.tol_pts / cfg.pts_per_tick
    through = float(cfg.through_ticks)
    t_end = contact_ns + int(cfg.max_retest_sec * NS)

    # side away from level: BID → price must go up to depart; ASK → down
    # depart direction is away from the resting side
    away = 1.0 if level_is_bid else -1.0

    departed = False
    depart_i = None
    for i in range(i0, len(ts)):
        if ts[i] > t_end:
            break
        signed = away * (float(px_ticks[i]) - p0)
        # traded through the level the wrong way?
        if away * (float(px_ticks[i]) - p0) < -through:
            return None
        if not departed and signed >= depart:
            departed = True
            depart_i = i
            continue
        if departed:
            # retest: within tol of P, still not through
            if abs(float(px_ticks[i]) - p0) <= tol:
                return {
                    "retest_ts_ns": int(ts[i]),
                    "depart_ts_ns": int(ts[depart_i]),
                    "depart_pts": float(abs(float(px_ticks[depart_i]) - p0) * cfg.pts_per_tick),
                    "retest_dist_pts": float(abs(float(px_ticks[i]) - p0) * cfg.pts_per_tick),
                }
    return None


def classify_retest_arm(
    *,
    remain_frac: float,
    approach_net_add: float,
    dominance: float,
    cfg: DefendedRetestConfig,
    fragmented_max: float = 0.25,
) -> str:
    if np.isfinite(dominance) and dominance < fragmented_max:
        return "fragmented"
    if remain_frac >= cfg.defended_remain and approach_net_add >= 0:
        return "defended"
    if remain_frac < cfg.completed_remain or approach_net_add < 0:
        return "completed"
    return "other"


def defended_retest_events(
    lifecycle: pa.Table,
    trades: pa.Table,
    *,
    cfg: DefendedRetestConfig | None = None,
    lc_cfg: LifecycleConfig | None = None,
    require_primary: bool = True,
) -> pa.Table:
    """Build retest events from lifecycle contacts.

    ``approach_net_add`` / ``remain_frac`` are taken from lifecycle columns
    when present (``remain_frac`` at contact+2s); otherwise remain_frac from
    the event and approach_net_add=0 (conservative → completed if remain low).
    """
    cfg = cfg or DefendedRetestConfig()
    lc_cfg = lc_cfg or LifecycleConfig()
    if lifecycle.num_rows == 0 or trades.num_rows == 0:
        return pa.table({})

    ts, px = _price_path(trades)
    rows: list[dict] = []

    primary = (
        lifecycle.column("lc_primary").to_numpy().astype(np.int8)
        if "lc_primary" in lifecycle.column_names
        else np.ones(lifecycle.num_rows, dtype=np.int8)
    )
    frag = (
        lifecycle.column("lc_fragmented").to_numpy().astype(np.int8)
        if "lc_fragmented" in lifecycle.column_names
        else np.zeros(lifecycle.num_rows, dtype=np.int8)
    )

    for i in range(lifecycle.num_rows):
        if require_primary and int(primary[i]) != 1 and int(frag[i]) != 1:
            continue
        if float(lifecycle.column("executed")[i].as_py()) < cfg.min_executed:
            continue
        if int(lifecycle.column("seeded")[i].as_py()) == 1:
            continue
        contact_ns = int(lifecycle.column("event_ts_ns")[i].as_py())
        price = int(lifecycle.column("price_ticks")[i].as_py())
        level_side = lifecycle.column("level_side")[i].as_py()
        is_bid = level_side == "BID"
        geo = find_retest(
            ts, px, contact_ns=contact_ns, price_ticks=price,
            level_is_bid=is_bid, cfg=cfg,
        )
        if geo is None:
            continue
        remain = float(lifecycle.column("remain_frac")[i].as_py())
        # proxy: replenish_count > 0 ⇒ net add positive; cancelled_before high ⇒ negative
        rep = int(lifecycle.column("replenish_count")[i].as_py())
        cbf = float(lifecycle.column("cancelled_before_contact_frac")[i].as_py())
        approach_net = float(rep) - (1.0 if cbf > 0.3 else 0.0)
        dom = float(lifecycle.column("dominance")[i].as_py())
        if int(frag[i]) == 1:
            arm = "fragmented"
        else:
            arm = classify_retest_arm(
                remain_frac=remain, approach_net_add=approach_net,
                dominance=dom, cfg=cfg, fragmented_max=lc_cfg.fragmented_max,
            )
        # fade away from level
        side = 1 if is_bid else -1
        rows.append({
            "event_ts_ns": geo["retest_ts_ns"],
            "contact_ts_ns": contact_ns,
            "side": side,
            "level_side": level_side,
            "price_ticks": price,
            "arm": arm,
            "remain_frac": remain,
            "dominance": dom,
            "depart_pts": geo["depart_pts"],
            "retest_dist_pts": geo["retest_dist_pts"],
            "executed": float(lifecycle.column("executed")[i].as_py()),
            "initial_size": float(lifecycle.column("initial_size")[i].as_py()),
            "order_id": lifecycle.column("order_id")[i].as_py(),
            "dr_defended": 1 if arm == "defended" else 0,
            "dr_completed": 1 if arm == "completed" else 0,
            "dr_fragmented": 1 if arm == "fragmented" else 0,
            "event_type": "defended_retest",
        })

    if not rows:
        return pa.table({})
    return pa.table({
        "event_ts_ns": np.array([r["event_ts_ns"] for r in rows], dtype=np.int64),
        "contact_ts_ns": np.array([r["contact_ts_ns"] for r in rows], dtype=np.int64),
        "side": np.array([r["side"] for r in rows], dtype=np.int8),
        "level_side": [r["level_side"] for r in rows],
        "price_ticks": np.array([r["price_ticks"] for r in rows], dtype=np.int64),
        "arm": [r["arm"] for r in rows],
        "remain_frac": np.array([r["remain_frac"] for r in rows], dtype=np.float64),
        "dominance": np.array([r["dominance"] for r in rows], dtype=np.float64),
        "depart_pts": np.array([r["depart_pts"] for r in rows], dtype=np.float64),
        "retest_dist_pts": np.array([r["retest_dist_pts"] for r in rows], dtype=np.float64),
        "executed": np.array([r["executed"] for r in rows], dtype=np.float64),
        "initial_size": np.array([r["initial_size"] for r in rows], dtype=np.float64),
        "order_id": [r["order_id"] for r in rows],
        "dr_defended": np.array([r["dr_defended"] for r in rows], dtype=np.int8),
        "dr_completed": np.array([r["dr_completed"] for r in rows], dtype=np.int8),
        "dr_fragmented": np.array([r["dr_fragmented"] for r in rows], dtype=np.int8),
        "event_type": [r["event_type"] for r in rows],
    })


def placebo_retests(
    events: pa.Table,
    trades: pa.Table,
    *,
    cfg: DefendedRetestConfig | None = None,
    shift_pts: float | None = None,
) -> pa.Table:
    """Shift contact prices ±shift_pts and re-run geometry (same clocks)."""
    cfg = cfg or DefendedRetestConfig()
    shift = shift_pts if shift_pts is not None else cfg.placebo_shift_pts
    if events.num_rows == 0:
        return pa.table({})
    shift_ticks = int(round(shift / cfg.pts_per_tick))
    # Build a synthetic lifecycle-like table with shifted prices
    cols = {c: events.column(c) for c in events.column_names}
    px = events.column("price_ticks").to_numpy().astype(np.int64)
    # use contact_ts as event_ts for find_retest via a fake lifecycle
    fake = pa.table({
        "event_ts_ns": events.column("contact_ts_ns") if "contact_ts_ns" in events.column_names else events.column("event_ts_ns"),
        "price_ticks": pa.array(px + shift_ticks, type=pa.int64()),
        "level_side": events.column("level_side"),
        "executed": events.column("executed"),
        "seeded": pa.array(np.zeros(events.num_rows, dtype=np.int8)),
        "remain_frac": events.column("remain_frac"),
        "replenish_count": pa.array(np.zeros(events.num_rows, dtype=np.int64)),
        "cancelled_before_contact_frac": pa.array(np.zeros(events.num_rows, dtype=np.float64)),
        "dominance": events.column("dominance"),
        "initial_size": events.column("initial_size"),
        "order_id": events.column("order_id"),
        "lc_primary": pa.array(np.ones(events.num_rows, dtype=np.int8)),
        "lc_fragmented": pa.array(np.zeros(events.num_rows, dtype=np.int8)),
    })
    out = defended_retest_events(fake, trades, cfg=cfg, require_primary=True)
    if out.num_rows == 0:
        return out
    # mark as placebo
    if "arm" in out.column_names:
        arms = ["placebo"] * out.num_rows
        out = out.drop("arm").append_column("arm", pa.array(arms, type=pa.string()))
    return out
