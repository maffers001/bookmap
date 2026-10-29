"""Unit tests for EXP-0078 DOP plumbing (synthetic, no market data).

Run: python -m pytest research/dop/test_dop.py -q
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa

from research.features.daily_open import (
    annotate_causal_trap_state,
    detect_dop_break,
    session_dop,
)
from research.features.momentum import trades_to_tape_bars
from research.features.orb_flow import flow_at

SEC = 1_000_000_000
T0 = 1_700_000_000 * SEC


def _trades(rows: list[tuple[int, float, float, bool]]) -> pa.Table:
    return pa.table({
        "ts_ns": pa.array([r[0] for r in rows], pa.int64()),
        "price": pa.array([r[1] for r in rows], pa.float64()),
        "size": pa.array([r[2] for r in rows], pa.float64()),
        "is_buy_aggressor": pa.array([r[3] for r in rows]),
        "aggressor_order_id": pa.array(["a"] * len(rows)),
    })


def test_dop_and_long_break():
    """Build below DOP for 6+ min, then pierce above → long."""
    rows = []
    # open at 1000
    rows.append((T0, 1000.0, 10.0, True))
    # build below during grace + after
    for i in range(1, 40):
        rows.append((T0 + i * 10 * SEC, 990.0, 20.0, True))  # buy aggressors trapped below
    # after grace (5 min = 300s): break above
    rows.append((T0 + 360 * SEC, 1001.0, 5.0, True))
    tr = _trades(rows)
    dop = session_dop(tr, T0)
    assert dop.valid and dop.dop_ticks == 1000.0
    bk = detect_dop_break(tr, dop, T0, T0 + 400 * SEC, grace_min=5.0)
    assert bk is not None
    assert bk.side == 1
    assert bk.trap_side == -1
    assert bk.trap_vol > bk.vol_above
    assert bk.trap_share > 0.5
    assert bk.trap_delta > 0  # buy aggressors below
    assert bk.sl_pts > 0 and bk.tp_pts == 2.0 * bk.r_pts


def test_dop_short_break():
    rows = []
    rows.append((T0, 1000.0, 10.0, False))
    for i in range(1, 40):
        rows.append((T0 + i * 10 * SEC, 1010.0, 20.0, False))  # sell aggressors above
    rows.append((T0 + 360 * SEC, 999.0, 5.0, False))
    tr = _trades(rows)
    dop = session_dop(tr, T0)
    bk = detect_dop_break(tr, dop, T0, T0 + 400 * SEC, grace_min=5.0)
    assert bk is not None
    assert bk.side == -1
    assert bk.trap_side == 1
    assert bk.trap_delta > 0  # sells above → positive trap_delta


def test_grace_suppresses_early_break():
    rows = [(T0, 1000.0, 10.0, True)]
    rows.append((T0 + 60 * SEC, 990.0, 100.0, True))
    rows.append((T0 + 120 * SEC, 1001.0, 5.0, True))  # would-be break inside grace
    rows.append((T0 + 200 * SEC, 985.0, 50.0, True))
    rows.append((T0 + 360 * SEC, 1001.0, 5.0, True))  # real break after grace
    tr = _trades(rows)
    dop = session_dop(tr, T0)
    bk = detect_dop_break(tr, dop, T0, T0 + 400 * SEC, grace_min=5.0)
    assert bk is not None
    assert bk.event_ts_ns == T0 + 360 * SEC


def test_trap_z_is_causal():
    rows = [{"trap_vol": float(100 + i), "trap_delta": float(10 + i)} for i in range(20)]
    annotate_causal_trap_state(rows, prior_n=14)
    assert np.isnan(rows[13]["trap_vol_z"]) and np.isfinite(rows[14]["trap_vol_z"])
    before = rows[15]["trap_vol_z"]
    rows[19]["trap_vol"] = 1e9
    annotate_causal_trap_state(rows, prior_n=14)
    assert rows[15]["trap_vol_z"] == before


def test_flow_truncation_still_holds():
    rows = []
    rng = np.random.default_rng(1)
    for i in range(2000):
        rows.append((T0 + i * 100_000_000, 1000.0 + (i % 7), 3.0, bool(rng.random() > 0.4)))
    tr = _trades(rows)
    stamp = T0 + 1500 * 100_000_000
    bars_full = trades_to_tape_bars(tr, bar_ms=100)
    f_full = flow_at(bars_full, stamp, 5)
    ts_all = tr.column("ts_ns").to_numpy()
    cut = tr.take(np.where(ts_all <= stamp)[0])
    bars_cut = trades_to_tape_bars(cut, bar_ms=100)
    f_cut = flow_at(bars_cut, stamp, 5)
    for k in ("imbalance", "total_vol", "delta"):
        assert abs(f_full[k] - f_cut[k]) < 1e-9
