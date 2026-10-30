"""MAT burst absorption at tracked level (H79 / EXP-0088, C4 one-shot).

Uses ``LargeLevelTracker``; classifies MAT stamps as breakthrough /
absorbed / no_level based on burst-side contact within engage_window.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from research.features.liquidity import LargeLevelConfig, TrackedLevel, run_liquidity_tracker


@dataclass
class BurstLevelConfig:
    engage_window_sec: float = 3.0
    min_executed: float = 10.0
    breakthrough_pierce_ticks: float = 4.0
    breakthrough_consume_ratio: float = 1.0
    level_cfg: LargeLevelConfig | None = None


def _classify(
    levels: list[TrackedLevel],
    stamp_ns: int,
    side: int,
    cfg: BurstLevelConfig,
) -> dict:
    """side +1 long → burst into ASK; side -1 short → burst into BID."""
    want = "ASK" if side > 0 else "BID"
    win = int(cfg.engage_window_sec * 1e9)
    engaged: list[TrackedLevel] = []
    for lv in levels:
        if lv.side != want:
            continue
        if not lv.contacted or lv.contact_ns <= 0:
            continue
        if lv.contact_ns < stamp_ns or lv.contact_ns > stamp_ns + win:
            continue
        if lv.executed_into < cfg.min_executed:
            continue
        engaged.append(lv)
    empty = {
        "bl_class": "no_level",
        "bl_breakthrough": 0,
        "bl_absorbed": 0,
        "bl_no_level": 1,
        "bl_executed": np.nan,
        "bl_penetration": np.nan,
        "bl_consume_ratio": np.nan,
        "bl_replenish": 0,
    }
    if not engaged:
        return empty
    # pick the most-executed contact
    best = max(engaged, key=lambda x: x.executed_into)
    cr = best.consume_ratio()
    pen = float(best.max_penetration_ticks)
    is_bt = (
        pen >= cfg.breakthrough_pierce_ticks
        and np.isfinite(cr)
        and cr >= cfg.breakthrough_consume_ratio
    )
    # absorbed = engaged but not breakthrough
    return {
        "bl_class": "breakthrough" if is_bt else "absorbed",
        "bl_breakthrough": int(is_bt),
        "bl_absorbed": int(not is_bt),
        "bl_no_level": 0,
        "bl_executed": float(best.executed_into),
        "bl_penetration": pen,
        "bl_consume_ratio": float(cr) if np.isfinite(cr) else np.nan,
        "bl_replenish": int(best.replenish_count),
    }


def annotate_burst_level(
    events: pa.Table,
    depth: pa.Table,
    trades: pa.Table,
    *,
    cfg: BurstLevelConfig | None = None,
) -> pa.Table:
    cfg = cfg or BurstLevelConfig()
    if events.num_rows == 0:
        return events
    levels = run_liquidity_tracker(
        depth, trades, cfg=cfg.level_cfg or LargeLevelConfig(), rth_uk_only=False
    )
    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    sides = events.column("side").to_numpy().astype(int)
    n = len(ev_ts)
    classes = [""] * n
    flags = {
        "bl_breakthrough": np.zeros(n, dtype=np.int8),
        "bl_absorbed": np.zeros(n, dtype=np.int8),
        "bl_no_level": np.ones(n, dtype=np.int8),
        "bl_replenish": np.zeros(n, dtype=np.int8),
    }
    floats = {
        "bl_executed": np.full(n, np.nan),
        "bl_penetration": np.full(n, np.nan),
        "bl_consume_ratio": np.full(n, np.nan),
    }
    for i in range(n):
        st = _classify(levels, int(ev_ts[i]), int(sides[i]), cfg)
        classes[i] = st["bl_class"]
        for k in flags:
            flags[k][i] = st[k]
        for k in floats:
            floats[k][i] = st[k]

    out = events
    if "bl_class" in out.column_names:
        out = out.drop("bl_class")
    out = out.append_column("bl_class", pa.array(classes, type=pa.string()))
    for name, arr in floats.items():
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=pa.float64()))
    for name, arr in flags.items():
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=pa.int8()))
    return out


def classify_from_level(
    *,
    side: str,
    contact_ns: int,
    stamp_ns: int,
    executed_into: float,
    max_penetration_ticks: float,
    consume_ratio: float,
    replenish_count: int,
    cfg: BurstLevelConfig | None = None,
) -> str:
    """Unit-test helper: classify a single synthetic level contact."""
    cfg = cfg or BurstLevelConfig()
    lv = TrackedLevel(
        side=side, price=0, first_seen_ns=0, initial_size=max(int(executed_into), 1),
        peak_size=max(int(executed_into), 1), current_size=0, last_update_ns=contact_ns,
    )
    lv.contacted = True
    lv.contact_ns = contact_ns
    lv.executed_into = int(executed_into)
    lv.max_penetration_ticks = int(max_penetration_ticks)
    lv.replenish_count = int(replenish_count)
    # consume_ratio is computed from initial_size; set initial to match desired ratio
    if consume_ratio > 0 and np.isfinite(consume_ratio):
        lv.initial_size = max(1, int(round(executed_into / consume_ratio)))
    trade_side = 1 if side == "ASK" else -1
    return _classify([lv], stamp_ns, trade_side, cfg)["bl_class"]
