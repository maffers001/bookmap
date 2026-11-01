"""Tests for HVN magnet + dVPOC clearance.

Run: python -m research.features.test_mat_hvn_magnet
"""
from __future__ import annotations

import sys

import numpy as np
import pyarrow as pa

from research.features.mat_hvn_magnet import (
    annotate_hvn_magnet,
    asof_dvpoc,
    poc_clearance_flags,
)
from research.irt_lvn.load_irt import IrtBars, IrtColumns


def _cols() -> IrtColumns:
    return IrtColumns(
        date=0, close=1, dop=2, dvpoc2=3,
        lvn2=4, lvn3=5, hvn1=6, hvn2=7, hvn3=8,
    )


def _bars(*, ts, hvn1, dvpoc, hvn2=None, hvn3=None) -> IrtBars:
    n = len(ts)
    z = np.zeros(n, dtype=np.float64)
    return IrtBars(
        "SYN",
        np.asarray(ts, dtype=np.int64),
        z.copy(), z.copy(),
        np.asarray(dvpoc, dtype=np.float64),
        z.copy(), z.copy(),
        np.asarray(hvn1, dtype=np.float64),
        np.asarray(hvn2 if hvn2 is not None else z, dtype=np.float64),
        np.asarray(hvn3 if hvn3 is not None else z, dtype=np.float64),
        _cols(),
        set(),
    )


def _trades(ts, prices_pts) -> pa.Table:
    ticks = [int(round(p / 0.25)) for p in prices_pts]
    return pa.table({
        "ts_ns": np.asarray(ts, dtype=np.int64),
        "price": np.asarray(ticks, dtype=np.int64),
    })


def test_poc_clear_toward() -> None:
    f = poc_clearance_flags(1, 0, 1000.0, 980.0)  # dist=20
    assert f["toward_poc10"] == 1
    assert f["toward_poc15"] == 1
    assert f["toward_poc20"] == 1
    assert f["away_poc15"] == 0
    print("PASS poc clear toward at 20")


def test_poc_too_close() -> None:
    f = poc_clearance_flags(1, 0, 1000.0, 992.0)  # dist=8
    assert f["toward_poc10"] == 0
    assert f["toward_poc15"] == 0
    print("PASS poc too close drops")


def test_missing_poc() -> None:
    f = poc_clearance_flags(1, 0, 1000.0, float("nan"))
    assert f["toward_poc15"] == 0
    print("PASS missing POC not keep")


def test_asof_dvpoc_zero_missing() -> None:
    bars = _bars(ts=[50], hvn1=[1010.0], dvpoc=[0.0])
    assert np.isnan(asof_dvpoc(100, bars))
    bars2 = _bars(ts=[50], hvn1=[1010.0], dvpoc=[990.0])
    assert asof_dvpoc(100, bars2) == 990.0
    print("PASS asof dvpoc zero=missing")


def test_annotate_magnet() -> None:
    # HVN 1010, price 1006 (near+toward long), POC 980 → dist 26 ≥ 20
    bars = _bars(ts=[50], hvn1=[1010.0], dvpoc=[980.0])
    trades = _trades([90], [1006.0])
    empty = _trades([], [])
    ev = pa.table({
        "event_ts_ns": np.asarray([100], dtype=np.int64),
        "side": np.asarray([1], dtype=np.int8),
    })
    out = annotate_hvn_magnet(ev, trades, empty, bars, _bars(ts=[], hvn1=[], dvpoc=[]))
    assert out.column("hvn_nq_toward")[0].as_py() == 1
    assert out.column("hvn_nq_toward_poc15")[0].as_py() == 1
    assert out.column("hvn_nq_toward_poc20")[0].as_py() == 1
    # POC close: 1000
    bars_close = _bars(ts=[50], hvn1=[1010.0], dvpoc=[1000.0])
    out2 = annotate_hvn_magnet(ev, trades, empty, bars_close, _bars(ts=[], hvn1=[], dvpoc=[]))
    assert out2.column("hvn_nq_toward")[0].as_py() == 1
    assert out2.column("hvn_nq_toward_poc10")[0].as_py() == 0
    print("PASS annotate magnet keep/drop")


def main() -> int:
    failed = 0
    for fn in (
        test_poc_clear_toward,
        test_poc_too_close,
        test_missing_poc,
        test_asof_dvpoc_zero_missing,
        test_annotate_magnet,
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
