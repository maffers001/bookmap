"""Tests for H78 stack survival.

Run: python -m research.features.test_stack_survival
"""
from __future__ import annotations

import sys

import numpy as np
import pyarrow as pa

from research.features.stack_survival import annotate_stack_survival, make_scratch_exit_fn


def _ev(sup, post2, path=10.0, path2=10.0):
    return pa.table({
        "event_ts_ns": np.asarray([1_000_000_000_000], dtype=np.int64),
        "side": np.asarray([1], dtype=np.int8),
        "bc_sup_size": np.asarray([sup], dtype=np.float64),
        "bc_path_size": np.asarray([path], dtype=np.float64),
        "bc_sup_size_p2000ms": np.asarray([post2], dtype=np.float64),
        "bc_path_size_p2000ms": np.asarray([path2], dtype=np.float64),
        "bc_sup_size_p5000ms": np.asarray([post2], dtype=np.float64),
    })


def test_pulled() -> None:
    # support halved → scratch
    out = annotate_stack_survival(_ev(100.0, 40.0))
    assert abs(out.column("sv_sup_remain")[0].as_py() - 0.4) < 1e-9
    assert out.column("sv_scratch")[0].as_py() == 1
    assert out.column("sv_survive")[0].as_py() == 0
    print("PASS pulled → scratch")


def test_replenished() -> None:
    out = annotate_stack_survival(_ev(100.0, 120.0))
    assert out.column("sv_scratch")[0].as_py() == 0
    assert out.column("sv_survive")[0].as_py() == 1
    print("PASS replenished → survive")


def test_consumed_boundary() -> None:
    out = annotate_stack_survival(_ev(100.0, 50.0))  # exactly 0.50 → survive (>=)
    assert out.column("sv_scratch")[0].as_py() == 0
    assert out.column("sv_survive")[0].as_py() == 1
    print("PASS remain==0.50 survive")


def test_delayed_ok() -> None:
    # survive + wall shrinks
    out = annotate_stack_survival(_ev(100.0, 80.0, path=50.0, path2=20.0))
    assert out.column("sv_delayed_ok")[0].as_py() == 1
    # survive but wall grew
    out2 = annotate_stack_survival(_ev(100.0, 80.0, path=50.0, path2=80.0))
    assert out2.column("sv_delayed_ok")[0].as_py() == 0
    print("PASS delayed_ok")


def test_exit_fn() -> None:
    fn = make_scratch_exit_fn()
    row = {"sv_scratch": 1, "sv_decision_ts_ns": 1_000_002_000_000_000}
    assert fn(1_000_000_000_000_000, 1, row) == (1_000_002_000_000_000, "stack_scratch")
    assert fn(1_000_000_000_000_000, 1, {"sv_scratch": 0}) is None
    print("PASS exit_fn")


def main() -> int:
    failed = 0
    for fn in (test_pulled, test_replenished, test_consumed_boundary, test_delayed_ok, test_exit_fn):
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print("All passed." if not failed else f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
