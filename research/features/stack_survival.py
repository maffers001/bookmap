"""MAT stack survival after stamp (H78 / EXP-0087).

Uses support/path band bounds and post-stamp sizes cached by
``annotate_book_corridor`` (``bc_sup_size_p2000ms`` etc.).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass
class StackSurvivalConfig:
    decision_sec: float = 2.0
    remain_scratch_max: float = 0.50
    descriptive_sec: float = 5.0


def annotate_stack_survival(
    events: pa.Table,
    *,
    cfg: StackSurvivalConfig | None = None,
) -> pa.Table:
    """Append ``sv_*`` columns from corridor post-stamp size columns.

    Requires ``bc_sup_size``, ``bc_sup_size_p2000ms`` (and optionally p5000ms)
    already on the table from EXP-0086 annotate.
    """
    cfg = cfg or StackSurvivalConfig()
    if events.num_rows == 0:
        return events
    n = events.num_rows
    stamp_sz = (
        events.column("bc_sup_size").to_numpy().astype(np.float64)
        if "bc_sup_size" in events.column_names
        else np.full(n, np.nan)
    )
    path_sz = (
        events.column("bc_path_size").to_numpy().astype(np.float64)
        if "bc_path_size" in events.column_names
        else np.full(n, np.nan)
    )
    tag2 = f"bc_sup_size_p{int(cfg.decision_sec * 1000)}ms"
    tag2p = f"bc_path_size_p{int(cfg.decision_sec * 1000)}ms"
    tag5 = f"bc_sup_size_p{int(cfg.descriptive_sec * 1000)}ms"
    post2 = (
        events.column(tag2).to_numpy().astype(np.float64)
        if tag2 in events.column_names
        else np.full(n, np.nan)
    )
    post2p = (
        events.column(tag2p).to_numpy().astype(np.float64)
        if tag2p in events.column_names
        else np.full(n, np.nan)
    )
    post5 = (
        events.column(tag5).to_numpy().astype(np.float64)
        if tag5 in events.column_names
        else np.full(n, np.nan)
    )

    remain = np.where(
        np.isfinite(stamp_sz) & (stamp_sz > 0) & np.isfinite(post2),
        post2 / stamp_sz,
        np.nan,
    )
    path_remain = np.where(
        np.isfinite(path_sz) & (path_sz > 0) & np.isfinite(post2p),
        post2p / path_sz,
        np.nan,
    )
    remain5 = np.where(
        np.isfinite(stamp_sz) & (stamp_sz > 0) & np.isfinite(post5),
        post5 / stamp_sz,
        np.nan,
    )
    # scratch when we had support at stamp and it collapsed
    had_sup = np.isfinite(stamp_sz) & (stamp_sz > 0)
    scratch = (
        had_sup & np.isfinite(remain) & (remain < cfg.remain_scratch_max)
    ).astype(np.int8)
    survive = (
        had_sup & np.isfinite(remain) & (remain >= cfg.remain_scratch_max)
    ).astype(np.int8)
    # delayed entry descriptive: support survives AND wall has not grown
    wall_grew = np.isfinite(path_remain) & (path_remain > 1.0)
    delayed_ok = (survive == 1) & (~wall_grew)

    out = events
    for name, arr, typ in (
        ("sv_sup_remain", remain, pa.float64()),
        ("sv_path_remain", path_remain, pa.float64()),
        ("sv_sup_remain_5s", remain5, pa.float64()),
        ("sv_scratch", scratch, pa.int8()),
        ("sv_survive", survive, pa.int8()),
        ("sv_delayed_ok", delayed_ok.astype(np.int8), pa.int8()),
    ):
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=typ))

    # decision timestamp for exit_fn
    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    decision = ev_ts + int(cfg.decision_sec * 1e9)
    if "sv_decision_ts_ns" in out.column_names:
        out = out.drop("sv_decision_ts_ns")
    out = out.append_column("sv_decision_ts_ns", pa.array(decision, type=pa.int64()))
    return out


def make_scratch_exit_fn(remain_max: float = 0.50):
    """ExitFn: if row marked scratch, force exit at sv_decision_ts_ns."""

    def exit_fn(entry_ts_ns: int, side: int, row: dict):
        if int(row.get("sv_scratch") or 0) != 1:
            return None
        ts = int(row.get("sv_decision_ts_ns") or 0)
        if ts <= 0:
            return None
        # decision must be after entry
        if ts < entry_ts_ns:
            return None
        return ts, "stack_scratch"

    return exit_fn
