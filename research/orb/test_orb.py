"""Unit tests for EXP-0055 ORB plumbing (synthetic, no market data).

Run: python -m pytest research/orb/test_orb.py -q
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa

from research.features.momentum import trades_to_tape_bars
from research.features.opening_range import (
    annotate_causal_state,
    build_opening_range,
    detect_breakout,
)
from research.features.orb_flow import flow_at

SEC = 1_000_000_000
T0 = 1_700_000_000 * SEC


def _trades(rows: list[tuple[int, float, float, bool]]) -> pa.Table:
    return pa.table({
        "ts_ns": pa.array([r[0] for r in rows], pa.int64()),
        "price": pa.array([r[1] for r in rows], pa.float64()),  # ticks
        "size": pa.array([r[2] for r in rows], pa.float64()),
        "is_buy_aggressor": pa.array([r[3] for r in rows]),
        "aggressor_order_id": pa.array(["a"] * len(rows)),
    })


def test_or_and_breakout_bullish():
    rows = []
    # OR 15 min: open 1000 ticks, high 1010, low 990, close 1008 (bullish)
    for i, px in enumerate([1000, 1005, 1010, 995, 990, 1008]):
        rows.append((T0 + i * 60 * SEC, float(px), 10.0, True))
    # after OR: first print beyond ORH + 1 tick at +16 min
    rows.append((T0 + 16 * 60 * SEC, 1011.0, 5.0, True))
    tr = _trades(rows)
    orng = build_opening_range(tr, T0, T0 + 15 * 60 * SEC)
    assert orng.valid and orng.direction == 1
    assert orng.orh_pts == 1010 * 0.25 and orng.orl_pts == 990 * 0.25
    bk = detect_breakout(tr, orng, T0 + 15 * 60 * SEC, T0 + 150 * 60 * SEC)
    assert bk is not None and bk.side == 1
    assert bk.event_ts_ns == T0 + 16 * 60 * SEC


def test_bearish_or_ignores_upside_break():
    rows = []
    for i, px in enumerate([1000, 995, 990, 1005, 1010, 992]):  # close < open
        rows.append((T0 + i * 60 * SEC, float(px), 10.0, False))
    rows.append((T0 + 16 * 60 * SEC, 1011.0, 5.0, True))   # upside break: ignored
    rows.append((T0 + 17 * 60 * SEC, 989.0, 5.0, False))   # downside break: taken
    tr = _trades(rows)
    orng = build_opening_range(tr, T0, T0 + 15 * 60 * SEC)
    assert orng.direction == -1
    bk = detect_breakout(tr, orng, T0 + 15 * 60 * SEC, T0 + 150 * 60 * SEC)
    assert bk is not None and bk.side == -1
    assert bk.event_ts_ns == T0 + 17 * 60 * SEC
    assert bk.opposite_ts_ns == T0 + 16 * 60 * SEC


def test_relvol_is_causal():
    rows = [{"or_volume": float(100 + i), "or_width_pts": 10.0,
             "session_range_pts": 50.0} for i in range(20)]
    annotate_causal_state(rows, prior_n=14)
    assert np.isnan(rows[13]["relvol"]) and np.isfinite(rows[14]["relvol"])
    before = rows[15]["relvol"]
    rows[19]["or_volume"] = 1e9  # future session cannot change earlier state
    annotate_causal_state(rows, prior_n=14)
    assert rows[15]["relvol"] == before


def test_flow_at_truncation_invariance():
    rows = []
    rng = np.random.default_rng(0)
    for i in range(2000):
        rows.append((T0 + i * 100_000_000, 1000.0 + (i % 7), 3.0, bool(rng.random() > 0.4)))
    tr = _trades(rows)
    stamp = T0 + 1500 * 100_000_000
    bars_full = trades_to_tape_bars(tr, bar_ms=100)
    f_full = flow_at(bars_full, stamp, 5)
    ts_all = tr.column("ts_ns").to_numpy()
    cut = tr.take(np.where(ts_all <= stamp)[0])
    bars_cut = trades_to_tape_bars(cut, bar_ms=100)
    f_cut = flow_at(bars_cut, stamp, 5)
    for k in ("imbalance", "total_vol", "delta"):
        assert abs(f_full[k] - f_cut[k]) < 1e-9
