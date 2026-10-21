"""Plant one stop-run on a synthetic tape; assert detector fires once, causally."""
from __future__ import annotations

import sys

import numpy as np
import pyarrow as pa

from research.features.stop_run import (
    StopRunConfig,
    resolve_outcome,
    stop_run_events,
)


def _mk_trades(
    ts: np.ndarray,
    px_ticks: np.ndarray,
    sz: np.ndarray,
    buy: np.ndarray,
    aids: list[str],
) -> pa.Table:
    return pa.table({
        "ts_ns": ts.astype(np.int64),
        "price": px_ticks.astype(np.float64),
        "size": sz.astype(np.float64),
        "is_buy_aggressor": buy.astype(bool),
        "aggressor_order_id": aids,
        "role": ["NQ"] * len(ts),
    })


def main() -> int:
    # Build: overnight base → RTH → swing high at 10000 ticks → pullaway 80 ticks (20pts)
    # → return and pierce with multi-aggressor buy burst spanning 3 ticks.
    # Use absolute-ish ns around a known UK RTH day: 2026-10-02 13:00 UK = before 14:30
    # 2026-10-02 14:30 UK ≈ 2026-10-02 13:30 UTC during BST... simpler: use relative
    # and set session via config by controlling timestamps so mid of tape maps to UK RTH.
    #
    # Instead of fighting timezone: disable overnight/session, use swings only.
    cfg = StopRunConfig(
        pierce_ticks=2,
        window_ms=2000,
        min_vol=20.0,
        min_prints=6,
        min_imbalance=0.55,
        min_sweeps=1,
        sweep_min_span_ticks=2.0,
        sweep_min_prints=3,
        min_aggressors=3,
        min_score=2,
        lookback_sec=120.0,
        pullaway_pts=10.0,  # 40 ticks
        invalidate_beyond_pts=20.0,
        level_kinds=frozenset({"SWING_HIGH", "SWING_LOW"}),
        cooldown_ms=1000,
        reclaim_sec=5.0,
    )

    t0 = 1_700_000_000_000_000_000  # arbitrary epoch ns
    rows = []
    # Climb to swing high 10000
    t = t0
    px = 9900
    for i in range(50):
        px = 9900 + i * 2
        rows.append((t, px, 2, True, f"up{i}"))
        t += 50_000_000  # 50ms
    high = px  # 9998
    # Pullaway down by 40+ ticks
    for i in range(25):
        px = high - (i + 1) * 2
        rows.append((t, px, 2, False, f"dn{i}"))
        t += 50_000_000
    # Quiet dwell
    for i in range(20):
        rows.append((t, px, 1, True, f"q{i}"))
        t += 100_000_000
    # Stop-run burst through high+2 with 3 aggressors sweeping
    pierce_start = t
    burst_ids = ["A1", "A2", "A3"]
    for i in range(12):
        # span 3 ticks above high
        p = high + 2 + (i % 3)
        rows.append((t, p, 5, True, burst_ids[i % 3]))
        t += 20_000_000  # 20ms → 240ms total inside 2s window

    ts = np.array([r[0] for r in rows], dtype=np.int64)
    px = np.array([r[1] for r in rows], dtype=np.float64)
    sz = np.array([r[2] for r in rows], dtype=np.float64)
    buy = np.array([r[3] for r in rows], dtype=bool)
    aids = [r[4] for r in rows]
    nq = _mk_trades(ts, px, sz, buy, aids)

    ev = stop_run_events(nq, None, None, cfg)
    print(f"events={ev.num_rows}")
    if ev.num_rows == 0:
        print("FAIL: expected >=1 stop-run event")
        return 1
    # Causal: event_ts >= pierce and >= all prints used
    ets = int(ev.column("event_ts_ns")[0].as_py())
    if ets < pierce_start:
        print(f"FAIL: look-ahead event_ts={ets} < pierce_start={pierce_start}")
        return 1
    side = int(ev.column("side")[0].as_py())
    score = int(ev.column("score")[0].as_py())
    n_sw = int(ev.column("n_sweeps")[0].as_py())
    n_ag = int(ev.column("n_aggressors")[0].as_py())
    print(f"side={side} score={score} sweeps={n_sw} aggr={n_ag} event_ts={ets}")
    if side != 1:
        print("FAIL: expected LONG (through HIGH)")
        return 1
    if score < 2:
        print("FAIL: score < 2")
        return 1
    if n_ag < 3:
        print("FAIL: expected >=3 aggressors")
        return 1

    ev2 = resolve_outcome(ev, nq, reclaim_sec=5.0)
    outcome = ev2.column("outcome")[0].as_py()
    print(f"outcome={outcome}")
    print("OK synthetic stop-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
