"""Event detectors (PyArrow/numpy)."""
from __future__ import annotations

import numpy as np
import pyarrow as pa


def effort_exhaustion_events(
    bars: pa.Table,
    min_effort: float = 50.0,
    *,
    forbid_flat_bar: bool = False,
    require_counter_ticks: bool = False,
    min_counter_ticks: float = 1.0,
    trade_sides: str = "both",
) -> pa.Table:
    """Detect effort/result exhaustion bars.

    trade_sides: "both" | "long" | "short" — which *reversal* sides to keep
    (long = fade sell exhaustion; short = fade buy exhaustion).

    forbid_flat_bar: drop bars with up_ticks==0 and down_ticks==0 (pure ε-spikes).

    require_counter_ticks: for sell exhaustion require up_ticks >= min;
    for buy exhaustion require down_ticks >= min (prints into opposing ticks).
    """
    if bars.num_rows == 0 or "exhaustion" not in bars.column_names:
        return pa.table({})

    exh = bars.column("exhaustion").to_numpy()
    side_ex = bars.column("exhaustion_side").to_numpy()
    buy_e = bars.column("buy_effort").to_numpy()
    sell_e = bars.column("sell_effort").to_numpy()
    ts = bars.column("ts_ns").to_numpy()
    up = bars.column("up_ticks").to_numpy()
    down = bars.column("down_ticks").to_numpy()
    buy_vol = bars.column("buy_vol").to_numpy()
    sell_vol = bars.column("sell_vol").to_numpy()

    mask = (exh > 0) & (
        ((side_ex > 0) & (buy_e >= min_effort)) | ((side_ex < 0) & (sell_e >= min_effort))
    )

    if forbid_flat_bar:
        mask &= ~((up <= 0) & (down <= 0))

    if require_counter_ticks:
        # Sell exhaustion → need upticks (sellers absorbed); buy exh → downticks
        mask &= (
            ((side_ex < 0) & (up >= min_counter_ticks))
            | ((side_ex > 0) & (down >= min_counter_ticks))
        )

    # Reversal side: opposite of exhaustion_side
    trade_side = (-side_ex).astype(np.int8)
    sides = (trade_sides or "both").lower()
    if sides == "long":
        mask &= trade_side > 0
    elif sides == "short":
        mask &= trade_side < 0
    elif sides != "both":
        raise ValueError(f"trade_sides must be both|long|short, got {trade_sides!r}")

    idx = np.where(mask)[0]
    if len(idx) == 0:
        return pa.table({})

    # Quality tag for dig/compare
    quality = []
    for i in idx:
        if up[i] <= 0 and down[i] <= 0:
            quality.append("flat_epsilon")
        elif side_ex[i] < 0 and up[i] >= min_counter_ticks:
            quality.append("sell_into_upticks")
        elif side_ex[i] > 0 and down[i] >= min_counter_ticks:
            quality.append("buy_into_downticks")
        else:
            quality.append("other")

    return pa.table({
        "event_ts_ns": ts[idx].astype(np.int64),
        "side": trade_side[idx],
        "event_type": ["effort_exhaustion"] * len(idx),
        "buy_effort": buy_e[idx],
        "sell_effort": sell_e[idx],
        "exhaustion_side": side_ex[idx],
        "up_ticks": up[idx],
        "down_ticks": down[idx],
        "buy_vol": buy_vol[idx],
        "sell_vol": sell_vol[idx],
        "quality": quality,
    })
