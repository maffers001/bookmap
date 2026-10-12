"""Forward outcome labeling with numpy/pyarrow only."""
from __future__ import annotations

import numpy as np
import pyarrow as pa


def build_price_path(trades: pa.Table) -> tuple[np.ndarray, np.ndarray]:
    if trades.num_rows == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.float64)
    ts = trades.column("ts_ns").to_numpy()
    price = trades.column("price").to_numpy()
    order = np.argsort(ts)
    ts, price = ts[order], price[order]
    # last price per timestamp
    uniq_ts, idx = np.unique(ts, return_index=True)
    # for duplicates keep last: reverse unique trick
    rev = ts[::-1]
    uniq_rev, idx_rev = np.unique(rev, return_index=True)
    last_idx = len(ts) - 1 - idx_rev
    order2 = np.argsort(ts[last_idx])
    return ts[last_idx][order2].astype(np.int64), price[last_idx][order2].astype(np.float64)


def label_events(
    events: pa.Table,
    path_ts: np.ndarray,
    path_px: np.ndarray,
    horizons_ms: list[int],
    point_targets: list[float],
) -> pa.Table:
    if events.num_rows == 0 or len(path_ts) == 0:
        return pa.table({})

    ev_ts = events.column("event_ts_ns").to_numpy()
    sides = events.column("side").to_numpy() if "side" in events.column_names else np.zeros(len(ev_ts))

    cols: dict[str, list] = {
        "event_ts_ns": [],
        "side": [],
        "entry_price": [],
        "entry_ts_ns": [],
        "mfe_pts": [],
        "mae_pts": [],
    }
    for h in horizons_ms:
        cols[f"ret_pts_{h}ms"] = []
        cols[f"ret_raw_pts_{h}ms"] = []
    for tgt in point_targets:
        cols[f"hit_{int(tgt)}pts"] = []
        cols[f"stopped_before_{int(tgt)}pts"] = []

    # copy through extra event columns
    extra_names = [n for n in events.column_names if n not in ("event_ts_ns", "side")]
    for n in extra_names:
        cols[n] = []

    max_h = max(horizons_ms)

    for i in range(len(ev_ts)):
        t0 = int(ev_ts[i])
        side = int(sides[i])
        i0 = int(np.searchsorted(path_ts, t0, side="left"))
        if i0 >= len(path_ts):
            continue
        p0 = float(path_px[i0])
        cols["event_ts_ns"].append(t0)
        cols["side"].append(side)
        cols["entry_price"].append(p0)
        cols["entry_ts_ns"].append(int(path_ts[i0]))

        for h in horizons_ms:
            t1 = t0 + int(h * 1_000_000)
            i1 = int(np.searchsorted(path_ts, t1, side="right") - 1)
            if i1 < i0:
                i1 = i0
            raw = float(path_px[i1] - p0)
            signed = raw if side == 0 else side * raw
            cols[f"ret_pts_{h}ms"].append(signed)
            cols[f"ret_raw_pts_{h}ms"].append(raw)

        t_end = t0 + int(max_h * 1_000_000)
        i_end = int(np.searchsorted(path_ts, t_end, side="right") - 1)
        if i_end < i0:
            i_end = i0
        path = path_px[i0 : i_end + 1]
        up = float(path.max() - p0)
        down = float(p0 - path.min())
        if side > 0:
            cols["mfe_pts"].append(up)
            cols["mae_pts"].append(down)
        elif side < 0:
            cols["mfe_pts"].append(down)
            cols["mae_pts"].append(up)
        else:
            cols["mfe_pts"].append(max(up, down))
            cols["mae_pts"].append(min(up, down))

        for tgt in point_targets:
            hit = False
            stopped = False
            if side == 0:
                hit = up >= tgt or down >= tgt
            else:
                for p in path:
                    move = side * (float(p) - p0)
                    if move >= tgt:
                        hit = True
                        break
                    if move <= -tgt:
                        stopped = True
                        break
            cols[f"hit_{int(tgt)}pts"].append(float(hit))
            cols[f"stopped_before_{int(tgt)}pts"].append(float(stopped))

        for n in extra_names:
            cols[n].append(events.column(n)[i].as_py())

    if not cols["event_ts_ns"]:
        return pa.table({})
    return pa.table(cols)
