"""Tests for HVN approach-path flags.

Run: python -m research.features.test_mat_hvn_approach
"""
from __future__ import annotations

import sys

import numpy as np
import pyarrow as pa

from research.features.mat_hvn_approach import (
    annotate_hvn_approach,
    is_approaching,
)
from research.irt_lvn.load_irt import IrtBars, IrtColumns


def _cols() -> IrtColumns:
    return IrtColumns(
        date=0, close=1, dop=2, dvpoc2=3,
        lvn2=4, lvn3=5, hvn1=6, hvn2=7, hvn3=8,
    )


def _bars(*, ts, hvn1) -> IrtBars:
    n = len(ts)
    z = np.zeros(n, dtype=np.float64)
    return IrtBars(
        "SYN",
        np.asarray(ts, dtype=np.int64),
        z.copy(), z.copy(), z.copy(), z.copy(), z.copy(),
        np.asarray(hvn1, dtype=np.float64), z.copy(), z.copy(),
        _cols(), set(),
    )


def _trades(ts, prices_pts) -> pa.Table:
    ticks = [int(round(p / 0.25)) for p in prices_pts]
    return pa.table({
        "ts_ns": np.asarray(ts, dtype=np.int64),
        "price": np.asarray(ticks, dtype=np.int64),
    })


def test_is_approaching() -> None:
    # then |1000-1010|=10, now |1005-1010|=5 → closed 5
    assert is_approaching(1005.0, 1000.0, 1010.0, min_pts=1.0)
    # closed exactly 1 pt
    assert is_approaching(1001.0, 1000.0, 1010.0, min_pts=1.0)
    # closed 0.5 < 1
    assert not is_approaching(1000.5, 1000.0, 1010.0, min_pts=1.0)
    # moved away
    assert not is_approaching(995.0, 1000.0, 1010.0, min_pts=1.0)
    print("PASS is_approaching")


def test_annotate_keep_drop() -> None:
    # HVN 1010; long; at stamp px=1006 (near+toward); 60s earlier px=1000 → approach 10→4
    bars = _bars(ts=[50], hvn1=[1010.0])
    ns = 1_000_000_000
    stamp = 100 * ns
    trades = _trades(
        [stamp - 60 * ns, stamp - 1],
        [1000.0, 1006.0],
    )
    empty = _trades([], [])
    ev = pa.table({
        "event_ts_ns": np.asarray([stamp], dtype=np.int64),
        "side": np.asarray([1], dtype=np.int8),
    })
    out = annotate_hvn_approach(ev, trades, empty, bars, _bars(ts=[], hvn1=[]))
    assert out.column("hvn_nq_toward")[0].as_py() == 1
    assert out.column("hvn_nq_approach60")[0].as_py() == 1
    assert out.column("hvn_nq_toward_ap60")[0].as_py() == 1

    # same but lookback also at 1006 → no approach
    trades2 = _trades([stamp - 60 * ns, stamp - 1], [1006.0, 1006.0])
    out2 = annotate_hvn_approach(ev, trades2, empty, bars, _bars(ts=[], hvn1=[]))
    assert out2.column("hvn_nq_toward")[0].as_py() == 1
    assert out2.column("hvn_nq_toward_ap60")[0].as_py() == 0
    print("PASS annotate keep/drop")


def main() -> int:
    failed = 0
    for fn in (test_is_approaching, test_annotate_keep_drop):
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print("All passed." if not failed else f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
