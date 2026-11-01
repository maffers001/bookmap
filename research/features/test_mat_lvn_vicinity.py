"""Tests for MAT × LVN vicinity.

Run: python -m research.features.test_mat_lvn_vicinity
"""
from __future__ import annotations

import sys

import numpy as np
import pyarrow as pa

from research.features.mat_lvn_vicinity import (
    LvnVicinityConfig,
    annotate_lvn_vicinity,
    nearest_lvn_dist,
)
from research.irt_lvn.load_irt import IrtBars, IrtColumns


def _cols() -> IrtColumns:
    return IrtColumns(
        date=0, close=1, dop=2, dvpoc2=3,
        lvn2=4, lvn3=5, hvn1=6, hvn2=7, hvn3=8,
    )


def _bars(
    ts: list[int],
    lvn2: list[float],
    lvn3: list[float],
) -> IrtBars:
    n = len(ts)
    z = np.zeros(n, dtype=np.float64)
    return IrtBars(
        "SYN",
        np.asarray(ts, dtype=np.int64),
        z.copy(),
        z.copy(),
        z.copy(),
        np.asarray(lvn2, dtype=np.float64),
        np.asarray(lvn3, dtype=np.float64),
        z.copy(),
        z.copy(),
        z.copy(),
        _cols(),
        set(),
    )


def _trades(ts: list[int], prices_ticks: list[int]) -> pa.Table:
    return pa.table({
        "ts_ns": np.asarray(ts, dtype=np.int64),
        "price": np.asarray(prices_ticks, dtype=np.int64),
    })


def test_inside_outside_nq() -> None:
    # LVN2=1000 pts; stamp at t=100; last print 1002 pts (= 4008 ticks @0.25)
    bars = _bars([50, 150], [1000.0, 1000.0], [0.0, 0.0])
    # price ticks: pts / 0.25
    trades = _trades([90], [int(1002.0 / 0.25)])  # dist=2 ≤ 5 → near
    ev = pa.table({
        "event_ts_ns": np.asarray([100], dtype=np.int64),
        "side": np.asarray([1], dtype=np.int8),
    })
    empty = pa.table({"ts_ns": np.asarray([], dtype=np.int64), "price": np.asarray([], dtype=np.int64)})
    out = annotate_lvn_vicinity(ev, trades, empty, bars, _bars([], [], []), cfg=LvnVicinityConfig())
    assert abs(out.column("lvn_nq_dist")[0].as_py() - 2.0) < 1e-9
    assert out.column("lvn_nq_near")[0].as_py() == 1
    assert out.column("lvn_nq_far")[0].as_py() == 0

    trades_far = _trades([90], [int(1010.0 / 0.25)])  # dist=10 > 5
    out2 = annotate_lvn_vicinity(ev, trades_far, empty, bars, _bars([], [], []))
    assert out2.column("lvn_nq_near")[0].as_py() == 0
    assert out2.column("lvn_nq_far")[0].as_py() == 1
    print("PASS inside/outside NQ band")


def test_es_band() -> None:
    bars = _bars([50], [5000.0], [0.0])
    # ES print 5001.5 → dist 1.5 ≤ 2 near; 5004 → far
    trades = _trades([40], [int(5001.5 / 0.25)])
    ev = pa.table({
        "event_ts_ns": np.asarray([100], dtype=np.int64),
        "side": np.asarray([-1], dtype=np.int8),
    })
    empty = pa.table({"ts_ns": np.asarray([], dtype=np.int64), "price": np.asarray([], dtype=np.int64)})
    out = annotate_lvn_vicinity(ev, empty, trades, _bars([], [], []), bars)
    assert out.column("lvn_es_near")[0].as_py() == 1
    trades_far = _trades([40], [int(5004.0 / 0.25)])
    out2 = annotate_lvn_vicinity(ev, empty, trades_far, _bars([], [], []), bars)
    assert out2.column("lvn_es_far")[0].as_py() == 1
    print("PASS ES band ±2")


def test_truncation_future_bar() -> None:
    # Future bar at t=200 has different LVN; stamp=100 must use t=50 bar
    bars_full = _bars([50, 200], [1000.0, 2000.0], [0.0, 0.0])
    bars_cut = _bars([50], [1000.0], [0.0])
    d_full = nearest_lvn_dist(100, 1000.0, bars_full)
    d_cut = nearest_lvn_dist(100, 1000.0, bars_cut)
    assert abs(d_full - d_cut) < 1e-9
    assert abs(d_full - 0.0) < 1e-9
    # stamp after second bar sees 2000
    d_late = nearest_lvn_dist(250, 2000.0, bars_full)
    assert abs(d_late - 0.0) < 1e-9
    print("PASS truncation / as-of")


def test_missing_irt_undefined() -> None:
    empty_bars = _bars([], [], [])
    d = nearest_lvn_dist(100, 1000.0, empty_bars)
    assert np.isnan(d)
    empty_t = pa.table({"ts_ns": np.asarray([], dtype=np.int64), "price": np.asarray([], dtype=np.int64)})
    ev = pa.table({
        "event_ts_ns": np.asarray([100], dtype=np.int64),
        "side": np.asarray([1], dtype=np.int8),
    })
    out = annotate_lvn_vicinity(ev, empty_t, empty_t, empty_bars, empty_bars)
    assert np.isnan(out.column("lvn_nq_dist")[0].as_py())
    assert out.column("lvn_nq_near")[0].as_py() == 0  # undefined ≠ near
    assert out.column("lvn_nq_far")[0].as_py() == 0
    print("PASS missing IRT → undefined not zero/near")


def test_zero_lvn_skipped() -> None:
    bars = _bars([50], [0.0], [0.0])  # both zero → no levels
    d = nearest_lvn_dist(100, 1000.0, bars)
    assert np.isnan(d)
    print("PASS zero LVN skipped")


def main() -> int:
    failed = 0
    for fn in (
        test_inside_outside_nq,
        test_es_band,
        test_truncation_future_bar,
        test_missing_irt_undefined,
        test_zero_lvn_skipped,
    ):
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print("All passed." if not failed else f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
