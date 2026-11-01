"""MAT × both-markets above/below dVPOC + DOP (H91).

Long keep: NQ and ES last prints are both ≥ as-of dVPOC and ≥ as-of DOP.
Short keep: both markets ≤ both levels.
Missing any level → not keep (undefined).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from research.features.mat_hvn_vicinity import _last_px_pts
from research.irt_lvn.events import _asof_idx
from research.irt_lvn.load_irt import IrtBars


@dataclass
class VpocDopAlignConfig:
    # Inclusive side-of-level (≥ / ≤).
    inclusive: bool = True


def asof_level(stamp_ns: int, bars: IrtBars, field: str) -> float:
    """As-of bar field; 0 → missing → nan."""
    if len(bars.ts_ns) == 0:
        return float("nan")
    bi = _asof_idx(bars.ts_ns, int(stamp_ns))
    if bi < 0:
        return float("nan")
    v = float(getattr(bars, field)[bi])
    return float("nan") if v == 0.0 else v


def side_of_levels(
    px: float,
    vpoc: float,
    dop: float,
    *,
    inclusive: bool = True,
) -> tuple[int, int]:
    """Return (above_both, below_both) as 0/1. Missing → (0, 0)."""
    if not all(np.isfinite(x) for x in (px, vpoc, dop)):
        return 0, 0
    if inclusive:
        above = 1 if (px >= vpoc and px >= dop) else 0
        below = 1 if (px <= vpoc and px <= dop) else 0
    else:
        above = 1 if (px > vpoc and px > dop) else 0
        below = 1 if (px < vpoc and px < dop) else 0
    return above, below


def annotate_vpoc_dop_align(
    events: pa.Table,
    nq_trades: pa.Table,
    es_trades: pa.Table,
    nq_bars: IrtBars,
    es_bars: IrtBars,
    *,
    cfg: VpocDopAlignConfig | None = None,
) -> pa.Table:
    cfg = cfg or VpocDopAlignConfig()
    if events.num_rows == 0:
        return events

    def _sorted(t: pa.Table) -> pa.Table:
        if t.num_rows == 0:
            return t
        order = np.argsort(t.column("ts_ns").to_numpy().astype(np.int64), kind="stable")
        return t.take(order)

    nq_trades = _sorted(nq_trades)
    es_trades = _sorted(es_trades)
    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    sides = events.column("side").to_numpy().astype(int)
    n = len(ev_ts)

    cols: dict[str, np.ndarray] = {
        "nq_vpoc": np.full(n, np.nan),
        "nq_dop": np.full(n, np.nan),
        "es_vpoc": np.full(n, np.nan),
        "es_dop": np.full(n, np.nan),
        "nq_above_both": np.zeros(n, dtype=np.int8),
        "nq_below_both": np.zeros(n, dtype=np.int8),
        "es_above_both": np.zeros(n, dtype=np.int8),
        "es_below_both": np.zeros(n, dtype=np.int8),
        "both_above": np.zeros(n, dtype=np.int8),
        "both_below": np.zeros(n, dtype=np.int8),
        # Primary keep: long∧both_above ∨ short∧both_below
        "vpoc_dop_align": np.zeros(n, dtype=np.int8),
        # Anti: long∧both_below ∨ short∧both_above
        "vpoc_dop_anti": np.zeros(n, dtype=np.int8),
    }

    for i, stamp in enumerate(ev_ts):
        stamp = int(stamp)
        side = int(sides[i])
        nq_px = _last_px_pts(nq_trades, stamp)
        es_px = _last_px_pts(es_trades, stamp)
        nq_v = asof_level(stamp, nq_bars, "dvpoc")
        nq_d = asof_level(stamp, nq_bars, "dop")
        es_v = asof_level(stamp, es_bars, "dvpoc")
        es_d = asof_level(stamp, es_bars, "dop")
        cols["nq_vpoc"][i] = nq_v
        cols["nq_dop"][i] = nq_d
        cols["es_vpoc"][i] = es_v
        cols["es_dop"][i] = es_d
        na, nb = side_of_levels(nq_px, nq_v, nq_d, inclusive=cfg.inclusive)
        ea, eb = side_of_levels(es_px, es_v, es_d, inclusive=cfg.inclusive)
        cols["nq_above_both"][i] = na
        cols["nq_below_both"][i] = nb
        cols["es_above_both"][i] = ea
        cols["es_below_both"][i] = eb
        both_a = 1 if (na == 1 and ea == 1) else 0
        both_b = 1 if (nb == 1 and eb == 1) else 0
        cols["both_above"][i] = both_a
        cols["both_below"][i] = both_b
        if side > 0:
            cols["vpoc_dop_align"][i] = both_a
            cols["vpoc_dop_anti"][i] = both_b
        elif side < 0:
            cols["vpoc_dop_align"][i] = both_b
            cols["vpoc_dop_anti"][i] = both_a

    out = events
    for name, arr in cols.items():
        typ = pa.float64() if arr.dtype.kind == "f" else pa.int8()
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=typ))
    return out
