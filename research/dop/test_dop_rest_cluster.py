"""Unit tests for EXP-0083 resting-cluster features (synthetic).

Run: python -m research.dop.test_dop_rest_cluster
Depth rows are (ts_ns, is_bid, price_ticks, size); DOP = 1000 ticks.
Long setup: trap zone asks in 880..1000 ticks (30pt below DOP).
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa

from research.features.dop_rest_cluster import compute_rest_cluster_features

SEC = 1_000_000_000
T0 = 1_700_000_000 * SEC
DOP = 1000.0


def _depth(rows):
    return pa.table({
        "ts_ns": pa.array([r[0] for r in rows], pa.int64()),
        "is_bid": pa.array([r[1] for r in rows]),
        "price_ticks": pa.array([r[2] for r in rows], pa.int32()),
        "size": pa.array([r[3] for r in rows], pa.int32()),
    })


def _trades(rows):
    return pa.table({
        "ts_ns": pa.array([r[0] for r in rows], pa.int64()),
        "price": pa.array([r[1] for r in rows], pa.float64()),
        "size": pa.array([r[2] for r in rows], pa.float64()),
        "is_buy_aggressor": pa.array([r[3] for r in rows]),
    })


def _book_rows(cluster: bool):
    rows = []
    t = T0 - 400 * SEC
    # diffuse background asks across the zone (10 each at spread levels)
    for p in range(890, 1000, 10):
        rows.append((t, False, p, 10))
    if cluster:
        # compact cluster: 900 lots within 4 ticks at 960..963
        for p in (960, 961, 962, 963):
            rows.append((t, False, p, 225))
    return rows


def _trade_rows(brk):
    rows = [(T0 - 100 * SEC, 961.0, 5.0, True)]  # last in-band print
    for k, p in enumerate([970.0, 980.0, 990.0, 999.0]):
        rows.append((T0 - (90 - k * 10) * SEC, p, 5.0, True))
    rows.append((brk, DOP + 1, 5.0, True))
    return rows


def test_cluster_detected_long():
    brk = T0
    f = compute_rest_cluster_features(
        _depth(_book_rows(True)), _trades(_trade_rows(brk)),
        dop_ticks=DOP, side=+1, break_ts_ns=brk,
    )
    assert f["arm_r"] == 1, f
    assert f["cluster_share_pre"] > 0.8
    assert f["cluster_persist"] == 3
    assert f["cluster_dist_dop_pts"] > 5.0
    assert f["rest_n_pullbacks"] == 0


def test_support_side_arm_s():
    """Compact bids behind a long break → Arm S."""
    brk = T0
    rows = []
    t = T0 - 400 * SEC
    for p in range(890, 1000, 10):  # diffuse bids
        rows.append((t, True, p, 10))
    for p in (960, 961, 962, 963):  # compact support cluster
        rows.append((t, True, p, 225))
    for p in range(890, 1000, 10):  # diffuse asks (no path cluster)
        rows.append((t, False, p, 10))
    f = compute_rest_cluster_features(
        _depth(rows), _trades(_trade_rows(brk)),
        dop_ticks=DOP, side=+1, break_ts_ns=brk,
    )
    assert f["arm_s"] == 1, f
    assert f["sup_share_pre"] > 0.8
    assert f["arm_r"] == 0


def test_no_cluster_when_diffuse():
    brk = T0
    f = compute_rest_cluster_features(
        _depth(_book_rows(False)), _trades(_trade_rows(brk)),
        dop_ticks=DOP, side=+1, break_ts_ns=brk,
    )
    assert f["arm_r"] == 0, f
    assert f["cluster_share_pre"] < 0.5


def test_truncation_invariance():
    brk = T0
    d = _depth(_book_rows(True) + [(T0 + 50 * SEC, False, 500, 99999)])
    tr = _trades(_trade_rows(brk) + [(brk + 50 * SEC, 700.0, 999.0, False)])
    full = compute_rest_cluster_features(
        d, tr, dop_ticks=DOP, side=+1, break_ts_ns=brk,
    )
    dts = d.column("ts_ns").to_numpy()
    tts = tr.column("ts_ns").to_numpy()
    cut_d = d.take(np.where(dts <= brk)[0])
    cut_t = tr.take(np.where(tts <= brk)[0])
    part = compute_rest_cluster_features(
        cut_d, cut_t, dop_ticks=DOP, side=+1, break_ts_ns=brk,
    )
    for k in ("cluster_share_pre", "cluster_size_pre", "arm_r", "cluster_persist"):
        assert full[k] == part[k], k


if __name__ == "__main__":
    test_cluster_detected_long()
    test_support_side_arm_s()
    test_no_cluster_when_diffuse()
    test_truncation_invariance()
    print("test_dop_rest_cluster: all passed")
