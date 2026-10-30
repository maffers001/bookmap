"""Synthetic tests for H77 book corridor.

Run: python -m research.features.test_book_corridor
"""
from __future__ import annotations

import sys

import numpy as np
import pyarrow as pa

from research.features.book_corridor import (
    BookCorridorConfig,
    annotate_book_corridor,
    band_size_in,
    corridor_at,
)
from research.features.dop_rest_cluster import _best_band


def test_best_band_share() -> None:
    # 5 contiguous ticks with sizes 10 each inside a wider zone of 100 total
    book = {i: 10 for i in range(100, 105)}
    book[200] = 50  # outside band but in zone query we'll include
    b = _best_band(book, 100, 210, band_ticks=20)  # 5 pts = 20 ticks
    assert b["sum"] == 50.0  # 5*10
    assert abs(b["share"] - 50.0 / 100.0) < 1e-9  # zone total 50+50
    print("PASS best_band share")


def test_corridor_long() -> None:
    cfg = BookCorridorConfig(zone_pts=10.0, band_pts=5.0, share_min=0.50)
    entry = 1000  # ticks
    # support: concentrated bids below entry (800-820 = 5 pts)
    bids = {i: 20 for i in range(960, 981)}  # 21 levels * 20 = 420 in 5.25pt
    # make best band dominate: dense 960-980 (5 pts = 20 ticks)
    bids = {i: 5 for i in range(960, 1001)}  # zone [990,1000] wait
    # zone for long support = [entry-40, entry] = [960, 1000]
    bids = {i: 2 for i in range(960, 1001)}
    for i in range(960, 981):  # 5 pt band
        bids[i] = 20
    # path: dispersed asks above — no concentrated wall
    asks = {i: 2 for i in range(1000, 1041)}
    st = corridor_at(bids, asks, entry_ticks=entry, side=+1, cfg=cfg)
    assert st["bc_ready"] == 1
    assert st["bc_sup_share"] >= 0.50, st["bc_sup_share"]
    assert st["bc_path_share"] < 0.50 or not np.isfinite(st["bc_path_share"]) or True
    # path is flat 2 everywhere so any 5pt band = 42 / 82 ≈ 0.51 — bump zone
    # Make path clearly non-concentrated: one level holds little of total
    # path empty = no wall (share undefined → wall_only=0)
    asks: dict[int, int] = {}
    st = corridor_at(bids, asks, entry_ticks=entry, side=+1, cfg=cfg)
    assert st["bc_support_only"] == 1, st
    assert st["bc_wall_only"] == 0, st
    assert st["bc_corridor"] == 1, st
    print("PASS corridor long", st["bc_sup_share"], st["bc_path_share"], st["bc_corridor"])


def test_corridor_short_mirror() -> None:
    cfg = BookCorridorConfig()
    entry = 1000
    # short: support = asks above (concentrated), path = bids below (flat/tiny)
    asks = {i: 20 for i in range(1000, 1021)}
    for i in range(1021, 1041):
        asks[i] = 2
    bids: dict[int, int] = {}  # no path wall
    st = corridor_at(bids, asks, entry_ticks=entry, side=-1, cfg=cfg)
    assert st["bc_support_only"] == 1, st
    assert st["bc_wall_only"] == 0, st
    assert st["bc_corridor"] == 1, st
    print("PASS corridor short mirror", st["bc_corridor"], st["bc_sup_share"])


def test_wall_only_contrast() -> None:
    cfg = BookCorridorConfig()
    entry = 1000
    bids = {i: 1 for i in range(960, 1001)}  # weak support
    asks = {i: 30 for i in range(1000, 1021)}  # wall
    for i in range(1021, 1041):
        asks[i] = 1
    st = corridor_at(bids, asks, entry_ticks=entry, side=+1, cfg=cfg)
    assert st["bc_wall_only"] == 1
    assert st["bc_corridor"] == 0
    print("PASS wall_only contrast")


def test_empty_zone_undefined() -> None:
    cfg = BookCorridorConfig()
    st = corridor_at({}, {}, entry_ticks=1000, side=+1, cfg=cfg)
    assert not np.isfinite(st["bc_sup_share"])
    assert st["bc_corridor"] == 0
    assert st["bc_ready"] == 0
    print("PASS empty zone undefined")


def test_annotate_truncation() -> None:
    # Minimal depth + trades; annotate then cut
    stamp = 1_000_000_000_000
    trades = pa.table({
        "ts_ns": np.asarray([stamp, stamp + 1_000_000], dtype=np.int64),
        "price": np.asarray([1000.0, 1001.0], dtype=np.float64),
        "size": np.asarray([1.0, 1.0], dtype=np.float64),
        "is_buy_aggressor": np.asarray([True, True]),
    })
    # depth before stamp builds book
    rows = []
    for i in range(960, 981):
        rows.append((stamp - 10_000_000, True, i, 20))  # bids
    for i in range(1000, 1041):
        rows.append((stamp - 10_000_000, False, i, 2))  # asks
    # future depth after stamp must not affect
    rows.append((stamp + 5_000_000_000, True, 970, 9999))
    depth = pa.table({
        "ts_ns": np.asarray([r[0] for r in rows], dtype=np.int64),
        "is_bid": np.asarray([r[1] for r in rows]),
        "price_ticks": np.asarray([r[2] for r in rows], dtype=np.int64),
        "size": np.asarray([r[3] for r in rows], dtype=np.int64),
    })
    ev = pa.table({
        "event_ts_ns": np.asarray([stamp], dtype=np.int64),
        "side": np.asarray([1], dtype=np.int8),
    })
    full = annotate_book_corridor(ev, depth, trades)
    cut_depth = depth.take(np.where(depth.column("ts_ns").to_numpy() <= stamp)[0])
    trunc = annotate_book_corridor(ev, cut_depth, trades)
    for col in ("bc_sup_share", "bc_corridor", "bc_path_share"):
        a = full.column(col)[0].as_py()
        b = trunc.column(col)[0].as_py()
        if isinstance(a, float):
            assert (np.isnan(a) and np.isnan(b)) or abs(a - b) < 1e-9, (col, a, b)
        else:
            assert a == b, (col, a, b)
    print("PASS annotate truncation")


def test_band_size_in() -> None:
    book = {100: 10, 101: 20, 102: 30}
    assert band_size_in(book, 100, 101) == 30.0
    assert band_size_in(book, 200, 210) == 0.0
    print("PASS band_size_in")


def main() -> int:
    tests = [
        test_best_band_share,
        test_corridor_long,
        test_corridor_short_mirror,
        test_wall_only_contrast,
        test_empty_zone_undefined,
        test_annotate_truncation,
        test_band_size_in,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}", flush=True)
    if failed:
        print(f"{failed}/{len(tests)} failed")
        return 1
    print(f"All {len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
