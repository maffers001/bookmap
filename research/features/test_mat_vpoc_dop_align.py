"""Tests for both-markets VPOC+DOP align.

Run: python -m research.features.test_mat_vpoc_dop_align
"""
from __future__ import annotations

import sys

import numpy as np
import pyarrow as pa

from research.features.mat_vpoc_dop_align import (
    annotate_vpoc_dop_align,
    side_of_levels,
)
from research.irt_lvn.load_irt import IrtBars, IrtColumns


def _cols() -> IrtColumns:
    return IrtColumns(
        date=0, close=1, dop=2, dvpoc2=3,
        lvn2=4, lvn3=5, hvn1=6, hvn2=7, hvn3=8,
    )


def _bars(*, ts, dop, dvpoc) -> IrtBars:
    n = len(ts)
    z = np.zeros(n, dtype=np.float64)
    return IrtBars(
        "SYN",
        np.asarray(ts, dtype=np.int64),
        z.copy(),
        np.asarray(dop, dtype=np.float64),
        np.asarray(dvpoc, dtype=np.float64),
        z.copy(), z.copy(), z.copy(), z.copy(), z.copy(),
        _cols(), set(),
    )


def _trades(ts, prices_pts) -> pa.Table:
    ticks = [int(round(p / 0.25)) for p in prices_pts]
    return pa.table({
        "ts_ns": np.asarray(ts, dtype=np.int64),
        "price": np.asarray(ticks, dtype=np.int64),
    })


def test_side_of_levels() -> None:
    assert side_of_levels(1010.0, 1000.0, 1005.0) == (1, 0)
    assert side_of_levels(990.0, 1000.0, 1005.0) == (0, 1)
    assert side_of_levels(1002.0, 1000.0, 1005.0) == (0, 0)  # between
    assert side_of_levels(1000.0, 1000.0, 1000.0) == (1, 1)  # on both
    assert side_of_levels(1000.0, float("nan"), 1000.0) == (0, 0)
    print("PASS side_of_levels")


def test_annotate_align() -> None:
    # Both markets above both levels → long keep
    nq = _bars(ts=[50], dop=[1000.0], dvpoc=[1005.0])
    es = _bars(ts=[50], dop=[5000.0], dvpoc=[5002.0])
    nq_t = _trades([90], [1010.0])
    es_t = _trades([90], [5010.0])
    ev = pa.table({
        "event_ts_ns": np.asarray([100], dtype=np.int64),
        "side": np.asarray([1], dtype=np.int8),
    })
    out = annotate_vpoc_dop_align(ev, nq_t, es_t, nq, es)
    assert out.column("both_above")[0].as_py() == 1
    assert out.column("vpoc_dop_align")[0].as_py() == 1
    assert out.column("vpoc_dop_anti")[0].as_py() == 0

    # Short should not keep when both above
    ev_s = pa.table({
        "event_ts_ns": np.asarray([100], dtype=np.int64),
        "side": np.asarray([-1], dtype=np.int8),
    })
    out_s = annotate_vpoc_dop_align(ev_s, nq_t, es_t, nq, es)
    assert out_s.column("vpoc_dop_align")[0].as_py() == 0
    assert out_s.column("vpoc_dop_anti")[0].as_py() == 1

    # Missing DOP → not keep
    nq_miss = _bars(ts=[50], dop=[0.0], dvpoc=[1005.0])
    out_m = annotate_vpoc_dop_align(ev, nq_t, es_t, nq_miss, es)
    assert out_m.column("vpoc_dop_align")[0].as_py() == 0
    print("PASS annotate align")


def main() -> int:
    failed = 0
    for fn in (test_side_of_levels, test_annotate_align):
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print("All passed." if not failed else f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
