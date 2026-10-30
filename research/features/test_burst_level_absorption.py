"""Tests for H79 burst level absorption.

Run: python -m research.features.test_burst_level_absorption
"""
from __future__ import annotations

import sys

from research.features.burst_level_absorption import (
    BurstLevelConfig,
    classify_from_level,
)


def test_breakthrough() -> None:
    c = classify_from_level(
        side="ASK",
        contact_ns=1_000_500_000_000,
        stamp_ns=1_000_000_000_000,
        executed_into=50,
        max_penetration_ticks=5,
        consume_ratio=1.2,
        replenish_count=0,
    )
    assert c == "breakthrough", c
    print("PASS breakthrough")


def test_absorbed_shallow() -> None:
    c = classify_from_level(
        side="ASK",
        contact_ns=1_000_500_000_000,
        stamp_ns=1_000_000_000_000,
        executed_into=20,
        max_penetration_ticks=2,
        consume_ratio=0.5,
        replenish_count=0,
    )
    assert c == "absorbed", c
    print("PASS absorbed (shallow)")


def test_absorbed_replenish() -> None:
    # engaged but not breakthrough (low consume)
    c = classify_from_level(
        side="ASK",
        contact_ns=1_000_500_000_000,
        stamp_ns=1_000_000_000_000,
        executed_into=30,
        max_penetration_ticks=1,
        consume_ratio=0.4,
        replenish_count=2,
    )
    assert c == "absorbed", c
    print("PASS absorbed (replenish/low consume)")


def test_no_level_outside_window() -> None:
    c = classify_from_level(
        side="ASK",
        contact_ns=1_010_000_000_000,  # +10s outside 3s window
        stamp_ns=1_000_000_000_000,
        executed_into=50,
        max_penetration_ticks=5,
        consume_ratio=1.2,
        replenish_count=0,
    )
    assert c == "no_level", c
    print("PASS no_level (outside window)")


def test_no_level_low_executed() -> None:
    c = classify_from_level(
        side="ASK",
        contact_ns=1_000_500_000_000,
        stamp_ns=1_000_000_000_000,
        executed_into=5,  # < 10
        max_penetration_ticks=5,
        consume_ratio=1.2,
        replenish_count=0,
        cfg=BurstLevelConfig(min_executed=10),
    )
    assert c == "no_level", c
    print("PASS no_level (low executed)")


def test_short_uses_bid() -> None:
    c = classify_from_level(
        side="BID",
        contact_ns=1_000_500_000_000,
        stamp_ns=1_000_000_000_000,
        executed_into=40,
        max_penetration_ticks=6,
        consume_ratio=1.0,
        replenish_count=0,
    )
    assert c == "breakthrough", c
    print("PASS short→BID breakthrough")


def main() -> int:
    failed = 0
    for fn in (
        test_breakthrough,
        test_absorbed_shallow,
        test_absorbed_replenish,
        test_no_level_outside_window,
        test_no_level_low_executed,
        test_short_uses_bid,
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
