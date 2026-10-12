"""ES/NQ helpers using numpy only."""
from __future__ import annotations

import numpy as np
import pyarrow as pa

from research.features.flow import rolling_zscore_np


def align_es_nq_delta_z(
    nq_bars: pa.Table,
    es_bars: pa.Table | None,
    z_window: int = 40,
    agree_z: float = 0.75,
) -> pa.Table:
    if nq_bars.num_rows == 0:
        return pa.table({})
    nq_ts = nq_bars.column("ts_ns").to_numpy()
    nq_delta = nq_bars.column("delta").to_numpy().astype(float)
    nq_z = rolling_zscore_np(nq_delta, z_window)

    if es_bars is None or es_bars.num_rows == 0:
        return pa.table({
            "ts_ns": nq_ts,
            "nq_delta_z": nq_z,
            "es_delta_z": np.full(len(nq_ts), np.nan),
            "confirm": np.zeros(len(nq_ts), dtype=np.int8),
        })

    es_ts = es_bars.column("ts_ns").to_numpy()
    es_delta = es_bars.column("delta").to_numpy().astype(float)
    es_z = rolling_zscore_np(es_delta, z_window)
    # asof backward
    es_z_aligned = np.full(len(nq_ts), np.nan)
    j = 0
    for i, t in enumerate(nq_ts):
        while j + 1 < len(es_ts) and es_ts[j + 1] <= t:
            j += 1
        if es_ts[j] <= t:
            es_z_aligned[i] = es_z[j]

    def sign(z):
        if np.isnan(z):
            return 0
        if z >= agree_z:
            return 1
        if z <= -agree_z:
            return -1
        return 0

    confirm = np.zeros(len(nq_ts), dtype=np.int8)
    for i in range(len(nq_ts)):
        ns, es = sign(nq_z[i]), sign(es_z_aligned[i])
        if ns and es and ns == es:
            confirm[i] = ns
        elif ns and es and ns != es:
            confirm[i] = 2

    return pa.table({
        "ts_ns": nq_ts,
        "nq_delta_z": nq_z,
        "es_delta_z": es_z_aligned,
        "confirm": confirm,
    })
