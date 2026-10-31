"""Tests for defended_retest + consumed_acceptance.

Run: python -m research.features.test_defended_retest
"""
from __future__ import annotations

import sys

import numpy as np

from research.features.consumed_acceptance import ConsumedAcceptConfig, classify_acceptance
from research.features.defended_retest import DefendedRetestConfig, classify_retest_arm, find_retest

NS = 1_000_000_000


def test_find_retest_geometry() -> None:
    cfg = DefendedRetestConfig(depart_pts=8.0, tol_pts=0.5, max_retest_sec=60)
    # BID at 1000: price goes up to 1032 (+8 pts = 32 ticks), then back to 1000
    base = 1_000_000_000_000
    ts = np.array([base + i * 100_000_000 for i in range(50)], dtype=np.int64)
    # ticks: stay, go up, return
    px = np.full(50, 1000.0)
    px[10:30] = np.linspace(1000, 1040, 20)  # +10 pts
    px[30:40] = np.linspace(1040, 1000, 10)
    px[40:] = 1000.0
    geo = find_retest(ts, px, contact_ns=base, price_ticks=1000, level_is_bid=True, cfg=cfg)
    assert geo is not None, geo
    assert geo["retest_ts_ns"] >= base
    print("PASS find_retest geometry")


def test_classify_arms() -> None:
    cfg = DefendedRetestConfig()
    assert classify_retest_arm(remain_frac=0.6, approach_net_add=1.0, dominance=0.7, cfg=cfg) == "defended"
    assert classify_retest_arm(remain_frac=0.1, approach_net_add=-1.0, dominance=0.7, cfg=cfg) == "completed"
    assert classify_retest_arm(remain_frac=0.6, approach_net_add=1.0, dominance=0.1, cfg=cfg) == "fragmented"
    print("PASS classify retest arms")


def test_acceptance_left_behind() -> None:
    cfg = ConsumedAcceptConfig(accept_sec=5.0, clear_pts=1.0, max_pullback_frac=0.25)
    base = 1_000_000_000_000
    # price breaks up through ASK at 100 pts, stays above with shallow pullback
    n = 50
    ts = np.array([base + i * 100_000_000 for i in range(n)], dtype=np.int64)
    px = np.linspace(100.0, 110.0, n)  # steadily up, no pullback
    acc = classify_acceptance(ts, px, contact_ns=base, price_pts=100.0, side=1, cfg=cfg)
    assert acc["arm"] == "left_behind", acc
    print("PASS left_behind")


def test_acceptance_breach() -> None:
    cfg = ConsumedAcceptConfig(accept_sec=5.0, clear_pts=1.0)
    base = 1_000_000_000_000
    n = 50
    ts = np.array([base + i * 100_000_000 for i in range(n)], dtype=np.int64)
    px = np.concatenate([np.linspace(100, 105, 25), np.linspace(105, 99, 25)])
    acc = classify_acceptance(ts, px, contact_ns=base, price_pts=100.0, side=1, cfg=cfg)
    assert acc["arm"] == "breach_back", acc
    print("PASS breach_back")


def main() -> int:
    failed = 0
    for fn in (
        test_find_retest_geometry,
        test_classify_arms,
        test_acceptance_left_behind,
        test_acceptance_breach,
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
