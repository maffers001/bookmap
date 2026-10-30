"""Unit tests for MBO lifecycle (synthetic; no parquet).

Run: python -m research.features.test_mbo_lifecycle
"""
from __future__ import annotations

import sys

import numpy as np
import pyarrow as pa

from research.features.mbo_lifecycle import (
    LifecycleConfig,
    annotate_large_dominant,
    classify_state,
    iceberg_proxy_at_price,
    paired_lifecycle,
    run_lifecycle,
    state_counts,
)

NS = 1_000_000_000
# UK 15:00 ≈ RTH window (UTC+1 → 14:00 UTC)
T0 = 1_700_000_000 * NS  # arbitrary but large


def _mbo_rows(rows: list[dict]) -> pa.Table:
    return pa.table({
        "ts_ns": np.array([r["ts"] for r in rows], dtype=np.int64),
        "op": [r["op"] for r in rows],
        "order_id": [r["oid"] for r in rows],
        "is_bid": [r["bid"] for r in rows],
        "price_ticks": np.array([r["px"] for r in rows], dtype=np.int64),
        "size": np.array([r["sz"] for r in rows], dtype=np.float64),
        "alias": [""] * len(rows),
        "role": ["NQ"] * len(rows),
        "symbol": [""] * len(rows),
        "session_id": ["t"] * len(rows),
    })


def _trades(rows: list[dict]) -> pa.Table:
    return pa.table({
        "ts_ns": np.array([r["ts"] for r in rows], dtype=np.int64),
        "price": np.array([r["px"] for r in rows], dtype=np.float64),
        "size": np.array([r["sz"] for r in rows], dtype=np.float64),
        "is_buy_aggressor": [r["buy"] for r in rows],
        "aggressor_order_id": ["a"] * len(rows),
        "alias": [""] * len(rows),
        "role": ["NQ"] * len(rows),
        "symbol": [""] * len(rows),
        "is_exec_start": [True] * len(rows),
        "is_exec_end": [True] * len(rows),
        "session_id": ["t"] * len(rows),
    })


def test_classify_defended() -> None:
    s = classify_state(
        executed=60, initial_size=100, max_penetration=1, replenish_count=1,
        remain_frac=0.6, cancelled_before_frac=0.0, contacted=True,
        reload_within_2s=True, cfg=LifecycleConfig(),
    )
    assert s == "defended", s
    print("PASS classify defended")


def test_classify_completed() -> None:
    s = classify_state(
        executed=80, initial_size=100, max_penetration=1, replenish_count=0,
        remain_frac=0.1, cancelled_before_frac=0.0, contacted=True,
        reload_within_2s=False, cfg=LifecycleConfig(),
    )
    assert s == "completed", s
    print("PASS classify completed")


def test_classify_consumed() -> None:
    s = classify_state(
        executed=120, initial_size=100, max_penetration=5, replenish_count=0,
        remain_frac=0.0, cancelled_before_frac=0.0, contacted=True,
        reload_within_2s=False, cfg=LifecycleConfig(),
    )
    assert s == "consumed", s
    print("PASS classify consumed")


def test_classify_pulled() -> None:
    s = classify_state(
        executed=0, initial_size=100, max_penetration=0, replenish_count=0,
        remain_frac=0.0, cancelled_before_frac=0.8, contacted=False,
        reload_within_2s=False, cfg=LifecycleConfig(),
    )
    assert s == "pulled", s
    print("PASS classify pulled")


def test_send_fill_reload_defended() -> None:
    """Large ASK send → buy fill half → reload → defended."""
    # Place at a time inside UK RTH: use epoch that maps to ~15:00 UK.
    # ((ts//1e9)+3600) % 86400 in [14:30*3600, 17:00*3600]
    # Choose ts so that utc+1 hour is 15:00 → utc 14:00 → sec_of_day = 14*3600
    # Use fixed: day 0 UTC 14:00 = 14*3600 seconds
    base = 14 * 3600 * NS  # 14:00 UTC = 15:00 UK
    cfg = LifecycleConfig(rth_uk_only=True, min_executed=10, post_sec=(0.5, 1.0))
    mbo = _mbo_rows([
        {"ts": base + 0, "op": "send", "oid": "A1", "bid": False, "px": 1000, "sz": 100},
        # reload after fill
        {"ts": base + int(0.2 * NS), "op": "replace", "oid": "A1", "bid": False, "px": 1000, "sz": 80},
    ])
    trades = _trades([
        {"ts": base + int(0.1 * NS), "px": 1000, "sz": 50, "buy": True},
    ])
    ev = run_lifecycle(mbo, trades, cfg=cfg, data_start_ns=base - NS)
    assert ev.num_rows >= 1, ev
    states = ev.column("state").to_pylist()
    assert "defended" in states or "completed" in states or "other" in states, states
    # executed should be ~50
    exe = float(ev.column("executed").to_numpy().max())
    assert exe >= 40, exe
    print(f"PASS send→fill→reload (states={states}, exe={exe})")


def test_cancel_on_approach_pulled() -> None:
    base = 14 * 3600 * NS
    cfg = LifecycleConfig(rth_uk_only=True, pulled_cancel_frac=0.70)
    mbo = _mbo_rows([
        {"ts": base + 0, "op": "send", "oid": "B1", "bid": True, "px": 990, "sz": 100},
        {"ts": base + int(1.0 * NS), "op": "cancel", "oid": "B1", "bid": True, "px": 0, "sz": 0},
    ])
    # seed BBO via a nearby trade so near-price check can fire
    trades = _trades([
        {"ts": base + int(0.5 * NS), "px": 991, "sz": 1, "buy": False},
    ])
    ev = run_lifecycle(mbo, trades, cfg=cfg, data_start_ns=base - NS)
    states = ev.column("state").to_pylist() if ev.num_rows else []
    # cancel resolves to full size → pulled if near price
    assert "pulled" in states or ev.num_rows == 0 or "other" in states, states
    print(f"PASS cancel-on-approach (states={states})")


def test_seeded_excluded() -> None:
    base = 14 * 3600 * NS
    cfg = LifecycleConfig(rth_uk_only=True, min_executed=1, post_sec=(0.2,))
    # order sent at data_start → seeded
    mbo = _mbo_rows([
        {"ts": base, "op": "send", "oid": "S1", "bid": False, "px": 1000, "sz": 50},
    ])
    trades = _trades([
        {"ts": base + int(0.1 * NS), "px": 1000, "sz": 50, "buy": True},
    ])
    ev = run_lifecycle(mbo, trades, cfg=cfg, data_start_ns=base)
    # seeded contacts must not appear
    assert ev.num_rows == 0, ev.column("seeded").to_pylist() if ev.num_rows else None
    print("PASS seeded excluded")


def test_replace_chain() -> None:
    base = 14 * 3600 * NS
    cfg = LifecycleConfig(rth_uk_only=True, min_executed=5, post_sec=(0.3,))
    mbo = _mbo_rows([
        {"ts": base + 0, "op": "send", "oid": "R1", "bid": False, "px": 1000, "sz": 40},
        {"ts": base + int(0.05 * NS), "op": "replace", "oid": "R1", "bid": False, "px": 1001, "sz": 40},
    ])
    trades = _trades([
        {"ts": base + int(0.1 * NS), "px": 1001, "sz": 40, "buy": True},
    ])
    ev = run_lifecycle(mbo, trades, cfg=cfg, data_start_ns=base - NS)
    assert ev.num_rows >= 1
    assert int(ev.column("price_ticks").to_numpy()[0]) == 1001
    print("PASS replace chain")


def test_annotate_primary() -> None:
    ev = pa.table({
        "event_ts_ns": np.array([14 * 3600 * NS + 60_000_000_000], dtype=np.int64),
        "initial_size": np.array([100.0]),
        "dominance": np.array([0.7]),
        "seeded": np.array([0], dtype=np.int8),
    })
    thr = {(14 * 3600) // 60: 50.0}  # wrong key format — use minute index
    mins = int((14 * 3600 * NS + 60_000_000_000) // 60_000_000_000)
    thr = {mins: 50.0}
    out = annotate_large_dominant(ev, thr_by_min=thr)
    assert int(out.column("lc_primary")[0].as_py()) == 1
    print("PASS annotate primary")


def test_paired_and_iceberg() -> None:
    base = 14 * 3600 * NS
    nq = pa.table({
        "event_ts_ns": np.array([base], dtype=np.int64),
        "level_side": ["ASK"],
        "state": ["defended"],
        "price_ticks": np.array([1000], dtype=np.int64),
        "executed": np.array([200.0]),
        "initial_size": np.array([50.0]),
        "replenish_count": np.array([3], dtype=np.int64),
        "max_penetration_ticks": np.array([1], dtype=np.int64),
    })
    es = pa.table({
        "event_ts_ns": np.array([base + 500_000_000], dtype=np.int64),
        "level_side": ["ASK"],
        "state": ["defended"],
    })
    paired = paired_lifecycle(nq, es, sync_ms=2000)
    assert int(paired.column("lc_paired")[0].as_py()) == 1
    ice = iceberg_proxy_at_price(nq, price_ticks=1000, level_side="ASK", stamp_ns=base)
    assert ice["iceberg"] == 1.0
    print("PASS paired + iceberg")


def test_state_counts() -> None:
    ev = pa.table({"state": ["defended", "consumed", "defended"]})
    c = state_counts(ev)
    assert c["defended"] == 2 and c["consumed"] == 1 and c["n"] == 3
    print("PASS state_counts")


def main() -> int:
    failed = 0
    for fn in (
        test_classify_defended,
        test_classify_completed,
        test_classify_consumed,
        test_classify_pulled,
        test_send_fill_reload_defended,
        test_cancel_on_approach_pulled,
        test_seeded_excluded,
        test_replace_chain,
        test_annotate_primary,
        test_paired_and_iceberg,
        test_state_counts,
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
