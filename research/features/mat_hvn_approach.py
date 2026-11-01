"""MAT × IRT HVN approach-path keep (H89 / H90).

Keep when: near HVN ∧ MAT toward that HVN ∧ distance-to-HVN decreased
over a lookback window (price path trending into the HVN).

Dig lookbacks: 30s / 60s / 120s. Missing lookback print → not keep.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from research.features.mat_hvn_vicinity import (
    HvnVicinityConfig,
    annotate_hvn_vicinity,
    _last_px_pts,
)
from research.irt_lvn.load_irt import IrtBars

LOOKBACKS_SEC = (30, 60, 120)
NS = 1_000_000_000


@dataclass
class HvnApproachConfig(HvnVicinityConfig):
    lookbacks_sec: tuple[int, ...] = LOOKBACKS_SEC
    # Minimum distance reduction (pts) to count as approaching.
    nq_min_approach_pts: float = 1.0
    es_min_approach_pts: float = 0.5


def px_at_or_before(trades: pa.Table, stamp_ns: int) -> float:
    return _last_px_pts(trades, stamp_ns)


def is_approaching(
    px_now: float,
    px_then: float,
    hvn_px: float,
    *,
    min_pts: float,
) -> bool:
    """True if |px−HVN| fell by ≥ min_pts over the window."""
    if not all(np.isfinite(x) for x in (px_now, px_then, hvn_px)):
        return False
    dist_then = abs(px_then - hvn_px)
    dist_now = abs(px_now - hvn_px)
    return (dist_then - dist_now) >= float(min_pts)


def annotate_hvn_approach(
    events: pa.Table,
    nq_trades: pa.Table,
    es_trades: pa.Table,
    nq_bars: IrtBars,
    es_bars: IrtBars,
    *,
    cfg: HvnApproachConfig | None = None,
) -> pa.Table:
    cfg = cfg or HvnApproachConfig()
    base = annotate_hvn_vicinity(
        events, nq_trades, es_trades, nq_bars, es_bars, cfg=cfg,
    )
    if base.num_rows == 0:
        return base

    def _sorted(t: pa.Table) -> pa.Table:
        if t.num_rows == 0:
            return t
        order = np.argsort(t.column("ts_ns").to_numpy().astype(np.int64), kind="stable")
        return t.take(order)

    nq_trades = _sorted(nq_trades)
    es_trades = _sorted(es_trades)
    ev_ts = base.column("event_ts_ns").to_numpy().astype(np.int64)
    n = len(ev_ts)
    nq_toward = base.column("hvn_nq_toward").to_numpy().astype(np.int8)
    nq_away = base.column("hvn_nq_away").to_numpy().astype(np.int8)
    nq_hvn = base.column("hvn_nq_px").to_numpy().astype(np.float64)
    es_toward = base.column("hvn_es_toward").to_numpy().astype(np.int8)
    es_away = base.column("hvn_es_away").to_numpy().astype(np.int8)
    es_hvn = base.column("hvn_es_px").to_numpy().astype(np.float64)

    cols: dict[str, np.ndarray] = {}
    for lb in cfg.lookbacks_sec:
        cols[f"hvn_nq_toward_ap{lb}"] = np.zeros(n, dtype=np.int8)
        cols[f"hvn_nq_away_ap{lb}"] = np.zeros(n, dtype=np.int8)
        cols[f"hvn_es_toward_ap{lb}"] = np.zeros(n, dtype=np.int8)
        cols[f"hvn_es_away_ap{lb}"] = np.zeros(n, dtype=np.int8)
        cols[f"hvn_nq_approach{lb}"] = np.zeros(n, dtype=np.int8)
        cols[f"hvn_es_approach{lb}"] = np.zeros(n, dtype=np.int8)

    for i, stamp in enumerate(ev_ts):
        stamp = int(stamp)
        nq_now = px_at_or_before(nq_trades, stamp)
        es_now = px_at_or_before(es_trades, stamp)
        for lb in cfg.lookbacks_sec:
            then_ns = stamp - int(lb) * NS
            nq_then = px_at_or_before(nq_trades, then_ns)
            es_then = px_at_or_before(es_trades, then_ns)
            nq_ap = is_approaching(
                nq_now, nq_then, float(nq_hvn[i]), min_pts=cfg.nq_min_approach_pts,
            )
            es_ap = is_approaching(
                es_now, es_then, float(es_hvn[i]), min_pts=cfg.es_min_approach_pts,
            )
            cols[f"hvn_nq_approach{lb}"][i] = 1 if nq_ap else 0
            cols[f"hvn_es_approach{lb}"][i] = 1 if es_ap else 0
            cols[f"hvn_nq_toward_ap{lb}"][i] = 1 if (nq_toward[i] == 1 and nq_ap) else 0
            cols[f"hvn_nq_away_ap{lb}"][i] = 1 if (nq_away[i] == 1 and nq_ap) else 0
            cols[f"hvn_es_toward_ap{lb}"][i] = 1 if (es_toward[i] == 1 and es_ap) else 0
            cols[f"hvn_es_away_ap{lb}"][i] = 1 if (es_away[i] == 1 and es_ap) else 0

    out = base
    for name, arr in cols.items():
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=pa.int8()))
    return out
