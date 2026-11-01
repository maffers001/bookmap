"""Tests for MAT × HVN vicinity toward/away.

Run: python -m research.features.test_mat_hvn_vicinity
"""
from __future__ import annotations

import sys

import numpy as np
import pyarrow as pa

from research.features.mat_hvn_vicinity import (
    HvnVicinityConfig,
    annotate_hvn_vicinity,
    direction_flags,
    nearest_hvn,
)
from research.irt_lvn.load_irt import IrtBars, IrtColumns


def _cols() -> IrtColumns:
    return IrtColumns(
        date=0, close=1, dop=2, dvpoc2=3,
        lvn2=4, lvn3=5, hvn1=6, hvn2=7, hvn3=8,
    )


def _bars(ts, hvn1, hvn2=None, hvn3=None) -> IrtBars:
    n = len(ts)
    z = np.zeros(n, dtype=np.float64)
    return IrtBars(
        "SYN",
        np.asarray(ts, dtype=np.int64),
        z.copy(), z.copy(), z.copy(), z.copy(), z.copy(),
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


def test_toward_long() -> None:
    # HVN at 1005, price 1002, long → toward
    near, toward, away = direction_flags(1, 1002.0, 1005.0, 3.0, 5.0)
    assert (near, toward, away) == (1, 1, 0)
    print("PASS toward long")


def test_away_long() -> None:
    # HVN at 998, price 1002, long → away (HVN below)
    near, toward, away = direction_flags(1, 1002.0, 998.0, 4.0, 5.0)
    assert (near, toward, away) == (1, 0, 1)
    print("PASS away long")


def test_toward_short() -> None:
    near, toward, away = direction_flags(-1, 1002.0, 998.0, 4.0, 5.0)
    assert (near, toward, away) == (1, 1, 0)
    print("PASS toward short")


def test_outside_band() -> None:
    near, toward, away = direction_flags(1, 1000.0, 1020.0, 20.0, 5.0)
    assert (near, toward, away) == (0, 0, 0)
    print("PASS outside band")


def test_at_level_no_direction() -> None:
    near, toward, away = direction_flags(1, 1000.0, 1000.0, 0.0, 5.0)
    assert (near, toward, away) == (1, 0, 0)
    print("PASS at-level neither toward nor away")


def test_annotate_mirror() -> None:
    bars = _bars([50], [1010.0])  # HVN above
    trades = _trades([90], [1006.0])  # dist=4 ≤ 5
    empty = _trades([], [])
    ev = pa.table({
        "event_ts_ns": np.asarray([100, 100], dtype=np.int64),
        "side": np.asarray([1, -1], dtype=np.int8),
    })
    out = annotate_hvn_vicinity(ev, trades, empty, bars, _bars([], []))
    assert out.column("hvn_nq_toward")[0].as_py() == 1  # long toward
    assert out.column("hvn_nq_away")[0].as_py() == 0
    assert out.column("hvn_nq_toward")[1].as_py() == 0  # short away
    assert out.column("hvn_nq_away")[1].as_py() == 1
    print("PASS annotate long/short mirror")


def test_truncation() -> None:
    bars_full = _bars([50, 200], [1000.0, 2000.0])
    bars_cut = _bars([50], [1000.0])
    _, d1 = nearest_hvn(100, 1000.0, bars_full)
    _, d2 = nearest_hvn(100, 1000.0, bars_cut)
    assert abs(d1 - d2) < 1e-9
    print("PASS truncation as-of")


def test_missing_undefined() -> None:
    _, d = nearest_hvn(100, 1000.0, _bars([], []))
    assert np.isnan(d)
    print("PASS missing IRT undefined")


def test_basis_shift_moves_hvn() -> None:
    """Rollover regression: _shift_bars must shift HVN1/2/3 (zero preserved).

    Pre-fix, a -590 pt NQ basis shift moved LVN/close but left HVNs ~590 pts
    away, so no MAT stamp was ever 'near' an HVN before the Sep roll.
    """
    from research.irt_lvn.study_es_intraday_lvn_large import _shift_bars

    bars = _bars([50], [16590.0], hvn2=[0.0], hvn3=[16600.0])
    bars.close = np.asarray([16595.0])
    _, d_pre = nearest_hvn(100, 16002.0, bars)
    assert d_pre > 500.0
    _shift_bars(bars, -590.0)
    assert float(bars.hvn1[0]) == 16000.0
    assert float(bars.hvn2[0]) == 0.0  # missing sentinel preserved
    assert float(bars.hvn3[0]) == 16010.0
    px, d_post = nearest_hvn(100, 16002.0, bars)
    assert px == 16000.0 and abs(d_post - 2.0) < 1e-9
    near, toward, away = direction_flags(-1, 16002.0, px, d_post, 5.0)
    assert (near, toward, away) == (1, 1, 0)  # short toward HVN below
    print("PASS basis shift moves HVN (rollover regression)")


def main() -> int:
    failed = 0
    for fn in (
        test_toward_long,
        test_away_long,
        test_toward_short,
        test_outside_band,
        test_at_level_no_direction,
        test_annotate_mirror,
        test_truncation,
        test_missing_undefined,
        test_basis_shift_moves_hvn,
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
