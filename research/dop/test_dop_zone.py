"""Unit tests for EXP-0079 zone plumbing (synthetic).

Run: python -m research.dop.test_dop_zone  (or pytest if available)
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa

from research.features.dop_zone import compute_zone_features, zone_band_ticks

SEC = 1_000_000_000
T0 = 1_700_000_000 * SEC


def _trades(rows):
    return pa.table({
        "ts_ns": pa.array([r[0] for r in rows], pa.int64()),
        "price": pa.array([r[1] for r in rows], pa.float64()),
        "size": pa.array([r[2] for r in rows], pa.float64()),
        "is_buy_aggressor": pa.array([r[3] for r in rows]),
    })


def _depth(rows):
    # rows: (ts, is_bid, price_ticks, size)
    return pa.table({
        "ts_ns": pa.array([r[0] for r in rows], pa.int64()),
        "is_bid": pa.array([r[1] for r in rows]),
        "price_ticks": pa.array([r[2] for r in rows], pa.int64()),
        "size": pa.array([r[3] for r in rows], pa.int64()),
    })


def test_band_ticks():
    lo, hi = zone_band_ticks(20000, +1)
    assert (lo, hi) == (20040, 20080)
    lo, hi = zone_band_ticks(20000, -1)
    assert (lo, hi) == (19920, 19960)


def test_fast_long_traversal():
    dop = 20000.0
    brk = T0 + 600 * SEC
    # depth: asks in zone 20040..20080 resting 500 total at break; pulled from 1000 at -60s
    depth_rows = [
        (brk - 120 * SEC, False, 20050, 1000),
        (brk - 30 * SEC, False, 20050, 500),   # pulled / consumed down to 500 before break
        (brk - 30 * SEC, True, 19990, 300),    # bid elsewhere
        (brk + 20 * SEC, False, 20050, 100),   # after traversal: 100 left
    ]
    # trades: break at dop+1 tick, +10 at +10s, +20 at +30s (20s traversal)
    trade_rows = [
        (brk, 20001.0, 10.0, True),
        (brk + 10 * SEC, 20040.0, 50.0, True),
        (brk + 20 * SEC, 20060.0, 200.0, True),
        (brk + 30 * SEC, 20080.0, 50.0, True),
    ]
    f = compute_zone_features(
        _depth(depth_rows), _trades(trade_rows),
        dop_ticks=dop, side=+1, break_ts_ns=brk,
    )
    assert f["depth_ok"] == 1
    assert f["zone_resting"] == 500.0
    assert f["stack_build_60s"] < 0  # pulled
    assert f["reached_zone"] == 1 and f["reached_exit"] == 1
    assert abs(f["traversal_sec"] - 20.0) < 1e-9
    assert f["fast_traversal"] == 1
    assert f["confirm_ts_ns"] == brk + 30 * SEC
    assert f["executed_into_zone"] == 300.0  # 50+200+50 buy-aggr in band through exit print
    assert abs(f["consume_frac"] - 0.6) < 1e-9


def test_slow_traversal_not_fast():
    dop = 20000.0
    brk = T0 + 600 * SEC
    depth_rows = [(brk - 120 * SEC, True, 19950, 800)]  # bids in short zone
    trade_rows = [
        (brk, 19999.0, 10.0, False),
        (brk + 10 * SEC, 19960.0, 50.0, False),   # zone entry (short)
        (brk + 200 * SEC, 19920.0, 50.0, False),  # exit 190s later: slow
    ]
    f = compute_zone_features(
        _depth(depth_rows), _trades(trade_rows),
        dop_ticks=dop, side=-1, break_ts_ns=brk,
    )
    assert f["reached_exit"] == 1
    assert f["fast_traversal"] == 0
    assert f["confirm_ts_ns"] == 0


def test_truncation_at_break_same_resting():
    dop = 20000.0
    brk = T0 + 600 * SEC
    depth_rows = [
        (brk - 120 * SEC, False, 20050, 700),
        (brk + 20 * SEC, False, 20050, 100),
    ]
    trade_rows = [
        (brk, 20001.0, 10.0, True),
        (brk + 10 * SEC, 20040.0, 50.0, True),
    ]
    full = compute_zone_features(_depth(depth_rows), _trades(trade_rows), dop_ticks=dop, side=+1, break_ts_ns=brk)
    cut_depth = _depth([r for r in depth_rows if r[0] <= brk])
    cut_trades = _trades([r for r in trade_rows if r[0] <= brk])
    cut = compute_zone_features(cut_depth, cut_trades, dop_ticks=dop, side=+1, break_ts_ns=brk)
    assert full["zone_resting"] == cut["zone_resting"] == 700.0
    assert full["stack_build_60s"] == cut["stack_build_60s"]


if __name__ == "__main__":
    test_band_ticks()
    test_fast_long_traversal()
    test_slow_traversal_not_fast()
    test_truncation_at_break_same_resting()
    print("ALL OK")
