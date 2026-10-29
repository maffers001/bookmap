"""Synthetic tests for EXP-0081 approach features.

Run: python -m research.dop.test_dop_approach
"""
from __future__ import annotations

import pyarrow as pa

from research.features.dop_zone import compute_approach_features

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
    return pa.table({
        "ts_ns": pa.array([r[0] for r in rows], pa.int64()),
        "is_bid": pa.array([r[1] for r in rows]),
        "price_ticks": pa.array([r[2] for r in rows], pa.int64()),
        "size": pa.array([r[3] for r in rows], pa.int64()),
    })


def test_fast_long_approach():
    dop = 20000.0
    brk = T0 + 600 * SEC
    start = brk - 30 * SEC
    depth = _depth([(start - 10 * SEC, False, 19950, 400)])  # asks in DOP-20..DOP
    trades = _trades([
        (start, 19920.0, 10.0, True),    # at DOP-20 (19920 = dop-80 ticks)
        (brk - 20 * SEC, 19960.0, 100.0, True),
        (brk - 10 * SEC, 19990.0, 100.0, True),
        (brk, 20001.0, 10.0, True),      # break print
    ])
    f = compute_approach_features(depth, trades, dop_ticks=dop, side=+1, break_ts_ns=brk)
    assert f["approach_reached"] == 1
    assert abs(f["approach_sec"] - 30.0) < 1e-9
    assert f["approach_fast"] == 1
    assert f["approach_resting"] == 400.0
    assert f["executed_into_approach"] == 210.0
    assert abs(f["approach_consume_frac"] - 210.0 / 400.0) < 1e-9


def test_slow_short_and_no_approach():
    dop = 20000.0
    brk = T0 + 600 * SEC
    start = brk - 300 * SEC
    depth = _depth([(start - 10 * SEC, True, 20050, 400)])
    trades = _trades([
        (start, 20080.0, 10.0, False),
        (brk - 100 * SEC, 20040.0, 50.0, False),
        (brk, 19999.0, 10.0, False),
    ])
    f = compute_approach_features(depth, trades, dop_ticks=dop, side=-1, break_ts_ns=brk)
    assert f["approach_reached"] == 1 and f["approach_fast"] == 0
    # no print beyond 20 pts on trap side -> not reached
    trades2 = _trades([
        (brk - 100 * SEC, 20040.0, 50.0, False),
        (brk, 19999.0, 10.0, False),
    ])
    f2 = compute_approach_features(depth, trades2, dop_ticks=dop, side=-1, break_ts_ns=brk)
    assert f2["approach_reached"] == 0


def test_truncation_invariance():
    dop = 20000.0
    brk = T0 + 600 * SEC
    start = brk - 30 * SEC
    depth = _depth([
        (start - 10 * SEC, False, 19950, 400),
        (brk + 60 * SEC, False, 19950, 999),  # post-break change must not matter
    ])
    trades = _trades([
        (start, 19920.0, 10.0, True),
        (brk - 10 * SEC, 19990.0, 100.0, True),
        (brk, 20001.0, 10.0, True),
        (brk + 60 * SEC, 20100.0, 10.0, True),
    ])
    full = compute_approach_features(depth, trades, dop_ticks=dop, side=+1, break_ts_ns=brk)
    cut_d = _depth([r for r in depth.to_pylist()] if False else [
        (start - 10 * SEC, False, 19950, 400),
    ])
    cut_t = _trades([
        (start, 19920.0, 10.0, True),
        (brk - 10 * SEC, 19990.0, 100.0, True),
        (brk, 20001.0, 10.0, True),
    ])
    cut = compute_approach_features(cut_d, cut_t, dop_ticks=dop, side=+1, break_ts_ns=brk)
    for k in ("approach_sec", "approach_resting", "executed_into_approach", "approach_fast"):
        assert full[k] == cut[k], k


if __name__ == "__main__":
    test_fast_long_approach()
    test_slow_short_and_no_approach()
    test_truncation_invariance()
    print("ALL OK")
