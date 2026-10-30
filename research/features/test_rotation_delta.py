"""Synthetic-tape / unit tests for H76 rotation delta-efficiency.

Run: python -m research.features.test_rotation_delta
"""
from __future__ import annotations

import sys

import numpy as np
import pyarrow as pa

from research.features.rotation_delta import (
    CompletedRotationDelta,
    PriorSessionDeltaStats,
    annotate_rotation_delta,
    classify_ir,
    completed_rotation_deltas,
    state_at_rotation_delta,
)
from research.features.swings import SwingPivot


def _trades(rows: list[tuple[int, float, float, bool]]) -> pa.Table:
    """rows: (ts_ns, price_ticks, size, is_buy)."""
    return pa.table({
        "ts_ns": np.asarray([r[0] for r in rows], dtype=np.int64),
        "price": np.asarray([r[1] for r in rows], dtype=np.float64),
        "size": np.asarray([r[2] for r in rows], dtype=np.float64),
        "is_buy_aggressor": np.asarray([r[3] for r in rows], dtype=bool),
    })


def test_classify_user_example() -> None:
    """30pt down on Δ=-1000 vs baseline 400/30 → IR=2.5 inefficient."""
    L = 30.0
    delta = -1000.0
    d = -1
    aligned = d * delta  # +1000
    effort = aligned / L  # 33.333...
    baseline = 400.0 / 30.0  # 13.333...
    ir, cls, _ = classify_ir(aligned, effort, baseline)
    assert cls == "inefficient", cls
    assert abs(ir - 2.5) < 1e-9, ir
    print("PASS classify user example IR=2.5 inefficient")


def test_classify_efficient_and_divergent() -> None:
    ir, cls, _ = classify_ir(400.0, 400.0 / 30.0, 400.0 / 30.0)
    assert cls == "efficient" and abs(ir - 1.0) < 1e-9
    ir, cls, _ = classify_ir(-100.0, -100.0 / 30.0, 13.33)
    assert cls == "divergent"
    ir, cls, _ = classify_ir(500.0, 500.0 / 30.0, None)
    assert cls == "warmup"
    print("PASS classify efficient / divergent / warmup")


def test_completed_rotation_delta_math() -> None:
    """Inject pivots + trades; prior down rotations at ~400 contracts → IR 2.5."""
    # Prices in ticks: 30 pts = 120 ticks
    # Build: HIGH@1000 → LOW@880 (down 30pt) twice for baseline, then one heavy.
    # Pivot ts and confirm must be ordered; delta windows use pivot print times.
    t0 = 1_000_000_000_000  # arbitrary ns
    sec = 1_000_000_000

    # Prior rotation 1: HIGH at t0, LOW at t0+10s, confirm LOW at t0+70s
    # Prior rotation 2: HIGH at t0+100s, LOW at t0+110s, confirm at t0+170s
    # Target: HIGH at t0+200s, LOW at t0+210s, confirm at t0+270s
    pivots = [
        SwingPivot(ts_ns=t0, price_ticks=1000, kind="HIGH", confirm_ts_ns=t0 + 60 * sec),
        SwingPivot(ts_ns=t0 + 10 * sec, price_ticks=880, kind="LOW", confirm_ts_ns=t0 + 70 * sec),
        SwingPivot(ts_ns=t0 + 100 * sec, price_ticks=1000, kind="HIGH", confirm_ts_ns=t0 + 160 * sec),
        SwingPivot(ts_ns=t0 + 110 * sec, price_ticks=880, kind="LOW", confirm_ts_ns=t0 + 170 * sec),
        SwingPivot(ts_ns=t0 + 200 * sec, price_ticks=1000, kind="HIGH", confirm_ts_ns=t0 + 260 * sec),
        SwingPivot(ts_ns=t0 + 210 * sec, price_ticks=880, kind="LOW", confirm_ts_ns=t0 + 270 * sec),
    ]

    rows = []
    # Prior1 window [t0, t0+10s]: sell 400
    rows.append((t0 + 1 * sec, 900.0, 400.0, False))
    # Prior2 window [t0+100s, t0+110s]: sell 400
    rows.append((t0 + 105 * sec, 900.0, 400.0, False))
    # Target window [t0+200s, t0+210s]: sell 1000
    rows.append((t0 + 205 * sec, 900.0, 1000.0, False))
    # Price prints after confirm for annotate
    rows.append((t0 + 280 * sec, 900.0, 1.0, True))  # mid undo

    trades = _trades(rows)
    rots = completed_rotation_deltas(pivots, trades)
    downs = [r for r in rots if r.direction < 0]
    assert len(downs) == 3, [(r.length_pts, r.delta, r.class_name) for r in downs]
    # First two: no baseline yet (need min_same_dir=2) → warmup
    assert downs[0].class_name == "warmup"
    assert downs[1].class_name == "warmup"
    # Third: baseline = median(effort1, effort2) = 400/30
    assert abs(downs[2].delta - (-1000.0)) < 1e-9, downs[2].delta
    assert abs(downs[2].aligned_delta - 1000.0) < 1e-9
    assert abs(downs[2].length_pts - 30.0) < 1e-9
    assert downs[2].class_name == "inefficient", (
        downs[2].class_name, downs[2].ir, downs[2].baseline_effort, downs[2].effort_per_pt
    )
    assert abs(downs[2].ir - 2.5) < 1e-9, downs[2].ir
    print("PASS completed_rotation_delta math IR=2.5")


def test_mirrored_up_inefficient() -> None:
    t0 = 2_000_000_000_000
    sec = 1_000_000_000
    pivots = [
        SwingPivot(ts_ns=t0, price_ticks=880, kind="LOW", confirm_ts_ns=t0 + 60 * sec),
        SwingPivot(ts_ns=t0 + 10 * sec, price_ticks=1000, kind="HIGH", confirm_ts_ns=t0 + 70 * sec),
        SwingPivot(ts_ns=t0 + 100 * sec, price_ticks=880, kind="LOW", confirm_ts_ns=t0 + 160 * sec),
        SwingPivot(ts_ns=t0 + 110 * sec, price_ticks=1000, kind="HIGH", confirm_ts_ns=t0 + 170 * sec),
        SwingPivot(ts_ns=t0 + 200 * sec, price_ticks=880, kind="LOW", confirm_ts_ns=t0 + 260 * sec),
        SwingPivot(ts_ns=t0 + 210 * sec, price_ticks=1000, kind="HIGH", confirm_ts_ns=t0 + 270 * sec),
    ]
    rows = [
        (t0 + 1 * sec, 940.0, 400.0, True),
        (t0 + 105 * sec, 940.0, 400.0, True),
        (t0 + 205 * sec, 940.0, 1000.0, True),
        (t0 + 280 * sec, 940.0, 1.0, False),
    ]
    rots = completed_rotation_deltas(pivots, _trades(rows))
    ups = [r for r in rots if r.direction > 0]
    assert ups[-1].class_name == "inefficient" and abs(ups[-1].ir - 2.5) < 1e-9
    print("PASS mirrored up inefficient IR=2.5")


def test_divergent_rotation() -> None:
    """Price down but buy-aggressor delta → A ≤ 0 → divergent."""
    t0 = 3_000_000_000_000
    sec = 1_000_000_000
    pivots = [
        SwingPivot(ts_ns=t0, price_ticks=1000, kind="HIGH", confirm_ts_ns=t0 + 60 * sec),
        SwingPivot(ts_ns=t0 + 10 * sec, price_ticks=880, kind="LOW", confirm_ts_ns=t0 + 70 * sec),
        SwingPivot(ts_ns=t0 + 100 * sec, price_ticks=1000, kind="HIGH", confirm_ts_ns=t0 + 160 * sec),
        SwingPivot(ts_ns=t0 + 110 * sec, price_ticks=880, kind="LOW", confirm_ts_ns=t0 + 170 * sec),
        SwingPivot(ts_ns=t0 + 200 * sec, price_ticks=1000, kind="HIGH", confirm_ts_ns=t0 + 260 * sec),
        SwingPivot(ts_ns=t0 + 210 * sec, price_ticks=880, kind="LOW", confirm_ts_ns=t0 + 270 * sec),
    ]
    # priors: normal sell effort; target: buys dominate (against the down move)
    rows = [
        (t0 + 1 * sec, 900.0, 400.0, False),
        (t0 + 105 * sec, 900.0, 400.0, False),
        (t0 + 205 * sec, 900.0, 800.0, True),  # buy into a down rotation
        (t0 + 280 * sec, 900.0, 1.0, True),
    ]
    rots = completed_rotation_deltas(pivots, _trades(rows))
    downs = [r for r in rots if r.direction < 0]
    assert downs[-1].aligned_delta <= 0
    assert downs[-1].class_name == "divergent", downs[-1].class_name
    print("PASS divergent class")


def test_not_usable_before_confirm() -> None:
    t0 = 4_000_000_000_000
    sec = 1_000_000_000
    r = CompletedRotationDelta(
        end_confirm_ts_ns=t0 + 270 * sec,
        start_ts_ns=t0 + 200 * sec,
        end_ts_ns=t0 + 210 * sec,
        start_px_pts=250.0,
        end_px_pts=220.0,
        length_pts=30.0,
        duration_sec=10.0,
        direction=-1,
        delta=-1000.0,
        aligned_delta=1000.0,
        effort_per_pt=1000.0 / 30.0,
        total_vol=1000.0,
        baseline_effort=400.0 / 30.0,
        ir=2.5,
        class_name="inefficient",
    )
    ts = np.asarray([t0 + 250 * sec, t0 + 280 * sec], dtype=np.int64)
    px = np.asarray([225.0, 228.0], dtype=np.float64)
    # Before confirm: nothing usable
    st = state_at_rotation_delta([r], ts, px, t0 + 250 * sec, side=+1)
    assert st["rd_ready"] == 0
    # After confirm + long (counter to down): primary if retrace ≤ 0.5
    st2 = state_at_rotation_delta([r], ts, px, t0 + 280 * sec, side=+1)
    assert st2["rd_ready"] == 1
    assert st2["rd_relation"] == "counter"
    assert st2["rd_class"] == "inefficient"
    assert st2["rd_primary"] == 1, st2
    # Short after down = with-rotation
    st3 = state_at_rotation_delta([r], ts, px, t0 + 280 * sec, side=-1)
    assert st3["rd_relation"] == "with"
    assert st3["rd_primary"] == 0
    print("PASS not usable before confirm / primary eligibility")


def test_truncation_invariance() -> None:
    """rd_* at stamp identical when trades are cut at stamp."""
    t0 = 5_000_000_000_000
    sec = 1_000_000_000
    pivots = [
        SwingPivot(ts_ns=t0, price_ticks=1000, kind="HIGH", confirm_ts_ns=t0 + 60 * sec),
        SwingPivot(ts_ns=t0 + 10 * sec, price_ticks=880, kind="LOW", confirm_ts_ns=t0 + 70 * sec),
        SwingPivot(ts_ns=t0 + 100 * sec, price_ticks=1000, kind="HIGH", confirm_ts_ns=t0 + 160 * sec),
        SwingPivot(ts_ns=t0 + 110 * sec, price_ticks=880, kind="LOW", confirm_ts_ns=t0 + 170 * sec),
        SwingPivot(ts_ns=t0 + 200 * sec, price_ticks=1000, kind="HIGH", confirm_ts_ns=t0 + 260 * sec),
        SwingPivot(ts_ns=t0 + 210 * sec, price_ticks=880, kind="LOW", confirm_ts_ns=t0 + 270 * sec),
    ]
    rows = [
        (t0 + 1 * sec, 900.0, 400.0, False),
        (t0 + 105 * sec, 900.0, 400.0, False),
        (t0 + 205 * sec, 900.0, 1000.0, False),
        (t0 + 280 * sec, 900.0, 1.0, True),
        (t0 + 400 * sec, 950.0, 50.0, True),  # future tape past stamp
    ]
    trades = _trades(rows)
    stamp = t0 + 280 * sec
    ev = pa.table({
        "event_ts_ns": np.asarray([stamp], dtype=np.int64),
        "side": np.asarray([1], dtype=np.int8),
    })
    full = annotate_rotation_delta(ev, trades, rots=completed_rotation_deltas(pivots, trades))
    cut_rows = [r for r in rows if r[0] <= stamp]
    cut = _trades(cut_rows)
    # Pivots that confirm after stamp must be dropped for causal truncate
    piv_cut = [p for p in pivots if p.confirm_ts_ns <= stamp]
    trunc = annotate_rotation_delta(
        ev, cut, rots=completed_rotation_deltas(piv_cut, cut)
    )
    for col in (
        "rd_ir", "rd_class", "rd_primary", "rd_delta", "rd_aligned_delta",
        "rd_relation", "rd_retrace_frac", "rd_end_confirm_ts_ns",
    ):
        a = full.column(col)[0].as_py()
        b = trunc.column(col)[0].as_py()
        if isinstance(a, float):
            assert (np.isnan(a) and np.isnan(b)) or abs(a - b) < 1e-9, (col, a, b)
        else:
            assert a == b, (col, a, b)
    print("PASS truncation invariance")


def test_prior_session_fallback() -> None:
    t0 = 6_000_000_000_000
    sec = 1_000_000_000
    pivots = [
        SwingPivot(ts_ns=t0, price_ticks=1000, kind="HIGH", confirm_ts_ns=t0 + 60 * sec),
        SwingPivot(ts_ns=t0 + 10 * sec, price_ticks=880, kind="LOW", confirm_ts_ns=t0 + 70 * sec),
    ]
    rows = [(t0 + 1 * sec, 900.0, 1000.0, False), (t0 + 80 * sec, 900.0, 1.0, True)]
    prior = PriorSessionDeltaStats(down_med_effort=400.0 / 30.0)
    rots = completed_rotation_deltas(pivots, _trades(rows), prior=prior)
    assert len(rots) == 1
    assert rots[0].from_prior == 1
    assert rots[0].class_name == "inefficient"
    assert abs(rots[0].ir - 2.5) < 1e-9
    print("PASS prior-session baseline fallback")


def main() -> int:
    tests = [
        test_classify_user_example,
        test_classify_efficient_and_divergent,
        test_completed_rotation_delta_math,
        test_mirrored_up_inefficient,
        test_divergent_rotation,
        test_not_usable_before_confirm,
        test_truncation_invariance,
        test_prior_session_fallback,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}", flush=True)
    if failed:
        print(f"{failed}/{len(tests)} failed", flush=True)
        return 1
    print(f"All {len(tests)} tests passed.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
