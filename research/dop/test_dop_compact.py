"""Unit tests for EXP-0082 compact trap-node features (synthetic).

Run: python -m research.dop.test_dop_compact
Prices are ticks (0.25pt): DOP=1000 ticks, a node 25pt below = 900.
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa

from research.features.dop_compact import compute_compact_trap_features

SEC = 1_000_000_000
T0 = 1_700_000_000 * SEC
DOP = 1000.0


def _trades(rows):
    return pa.table({
        "ts_ns": pa.array([r[0] for r in rows], pa.int64()),
        "price": pa.array([r[1] for r in rows], pa.float64()),
        "size": pa.array([r[2] for r in rows], pa.float64()),
        "is_buy_aggressor": pa.array([r[3] for r in rows]),
    })


def test_compact_clean_long():
    rows = [(T0, DOP, 10.0, True)]
    t = T0
    for i in range(10):  # scattered far below first: 20 lots
        t += 10 * SEC
        rows.append((t, 800.0, 2.0, True))
    for i in range(10):  # then compact node forms: 200 lots at 900-902 ticks
        t += 10 * SEC
        rows.append((t, 900.0 + (i % 3), 20.0, True))
    for k, p in enumerate([940.0, 960.0, 980.0]):  # clean departure
        rows.append((t + (k + 1) * 10 * SEC, p, 5.0, True))
    brk = t + 40 * SEC
    rows.append((brk, DOP + 1, 5.0, True))
    f = compute_compact_trap_features(
        _trades(rows), dop_ticks=DOP, side=+1, break_ts_ns=brk, open_ns=T0,
    )
    assert f["compact"] == 1 and f["arm_k"] == 1, f
    assert f["node_share5"] > 0.85
    assert f["node_span_pts"] <= 5.0
    assert f["node_delta_share"] > 0.99  # buy aggressors below DOP
    assert f["n_pullbacks"] == 0 and f["clean_departure"] == 1
    assert f["node_dist_dop_pts"] > 20.0


def test_dispersed_not_compact():
    rows = [(T0, DOP, 10.0, True)]
    t = T0
    for p in (900.0, 930.0, 960.0, 840.0, 870.0):  # 50 lots per level, >5pt apart
        for i in range(10):
            t += 10 * SEC
            rows.append((t, p, 5.0, True))
    brk = t + 100 * SEC
    rows.append((brk, DOP + 1, 5.0, True))
    f = compute_compact_trap_features(
        _trades(rows), dop_ticks=DOP, side=+1, break_ts_ns=brk, open_ns=T0,
    )
    assert f["node_share5"] < 0.5, f
    assert f["compact"] == 0 and f["arm_k"] == 0


def test_pullbacks_counted():
    rows = [(T0, DOP, 10.0, True)]
    t = T0
    for i in range(10):
        t += 10 * SEC
        rows.append((t, 900.0, 20.0, True))
    # departure (all above the 5pt node band): two >=2pt retracements
    path = [960.0, 940.0, 980.0, 940.0, 990.0]
    for k, p in enumerate(path):
        rows.append((t + (k + 1) * 10 * SEC, p, 5.0, True))
    brk = t + 60 * SEC
    rows.append((brk, DOP + 1, 5.0, True))
    f = compute_compact_trap_features(
        _trades(rows), dop_ticks=DOP, side=+1, break_ts_ns=brk, open_ns=T0,
    )
    assert f["n_pullbacks"] == 2, f
    assert f["clean_departure"] == 0 and f["arm_k"] == 0
    assert f["max_pullback_pts"] >= 2.0


def test_short_mirror_and_delta_sign():
    rows = [(T0, DOP, 10.0, False)]
    t = T0
    for i in range(10):  # sell aggressors in node 25pt above DOP
        t += 10 * SEC
        rows.append((t, 1100.0, 20.0, False))
    for k, p in enumerate([1060.0, 1040.0, 1020.0]):
        rows.append((t + (k + 1) * 10 * SEC, p, 5.0, False))
    brk = t + 40 * SEC
    rows.append((brk, DOP - 1, 5.0, False))
    f = compute_compact_trap_features(
        _trades(rows), dop_ticks=DOP, side=-1, break_ts_ns=brk, open_ns=T0,
    )
    assert f["compact"] == 1 and f["arm_k"] == 1, f
    assert f["node_trap_delta"] > 0 and f["node_delta_share"] > 0.99


def test_truncation_invariance():
    rows = [(T0, DOP, 10.0, True)]
    t = T0
    for i in range(10):
        t += 10 * SEC
        rows.append((t, 900.0, 20.0, True))
    for k, p in enumerate([940.0, 960.0, 980.0]):
        rows.append((t + (k + 1) * 10 * SEC, p, 5.0, True))
    brk = t + 40 * SEC
    rows.append((brk, DOP + 1, 5.0, True))
    rows.append((brk + 10 * SEC, 700.0, 999.0, False))  # post-break noise
    rows.append((brk + 20 * SEC, 1300.0, 999.0, True))
    tr = _trades(rows)
    full = compute_compact_trap_features(
        tr, dop_ticks=DOP, side=+1, break_ts_ns=brk, open_ns=T0,
    )
    ts = tr.column("ts_ns").to_numpy()
    cut = tr.take(np.where(ts <= brk)[0])
    part = compute_compact_trap_features(
        cut, dop_ticks=DOP, side=+1, break_ts_ns=brk, open_ns=T0,
    )
    for k in ("node_share5", "node_delta_share", "n_pullbacks", "arm_k"):
        assert full[k] == part[k], k


if __name__ == "__main__":
    test_compact_clean_long()
    test_dispersed_not_compact()
    test_pullbacks_counted()
    test_short_mirror_and_delta_sign()
    test_truncation_invariance()
    print("test_dop_compact: all passed")
