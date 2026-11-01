"""MAT × IRT HVN magnet + dVPOC clearance (H87 / H88).

Keep when: near HVN ∧ toward HVN ∧ |px − as-of dVPOC| ≥ poc_min.
Dig thresholds: 10 / 15 / 20 pts. Missing dVPOC → undefined (not keep).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from research.features.mat_hvn_vicinity import (
    HvnVicinityConfig,
    annotate_hvn_vicinity,
    direction_flags,
    nearest_hvn,
    _last_px_pts,
)
from research.irt_lvn.events import _asof_idx
from research.irt_lvn.load_irt import IrtBars

POC_MINS = (10, 15, 20)


@dataclass
class HvnMagnetConfig(HvnVicinityConfig):
    poc_mins: tuple[int, ...] = POC_MINS


def asof_dvpoc(stamp_ns: int, bars: IrtBars) -> float:
    """As-of dVPOC (0 → missing → nan)."""
    if len(bars.ts_ns) == 0:
        return float("nan")
    bi = _asof_idx(bars.ts_ns, int(stamp_ns))
    if bi < 0:
        return float("nan")
    v = float(bars.dvpoc[bi])
    return float("nan") if v == 0.0 else v


def poc_clearance_flags(
    toward: int,
    away: int,
    last_px: float,
    poc_px: float,
    poc_mins: tuple[int, ...] = POC_MINS,
) -> dict[str, int]:
    """toward_poc{k} / away_poc{k} for each threshold. Missing POC → all 0."""
    out: dict[str, int] = {}
    if not (np.isfinite(last_px) and np.isfinite(poc_px)):
        for k in poc_mins:
            out[f"toward_poc{k}"] = 0
            out[f"away_poc{k}"] = 0
        return out
    dist = abs(last_px - poc_px)
    for k in poc_mins:
        ok = dist >= float(k)
        out[f"toward_poc{k}"] = 1 if (toward == 1 and ok) else 0
        out[f"away_poc{k}"] = 1 if (away == 1 and ok) else 0
    return out


def annotate_hvn_magnet(
    events: pa.Table,
    nq_trades: pa.Table,
    es_trades: pa.Table,
    nq_bars: IrtBars,
    es_bars: IrtBars,
    *,
    cfg: HvnMagnetConfig | None = None,
) -> pa.Table:
    cfg = cfg or HvnMagnetConfig()
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
    es_toward = base.column("hvn_es_toward").to_numpy().astype(np.int8)
    es_away = base.column("hvn_es_away").to_numpy().astype(np.int8)

    cols: dict[str, np.ndarray] = {
        "hvn_nq_poc_px": np.full(n, np.nan),
        "hvn_nq_poc_dist": np.full(n, np.nan),
        "hvn_es_poc_px": np.full(n, np.nan),
        "hvn_es_poc_dist": np.full(n, np.nan),
    }
    for k in cfg.poc_mins:
        cols[f"hvn_nq_toward_poc{k}"] = np.zeros(n, dtype=np.int8)
        cols[f"hvn_nq_away_poc{k}"] = np.zeros(n, dtype=np.int8)
        cols[f"hvn_es_toward_poc{k}"] = np.zeros(n, dtype=np.int8)
        cols[f"hvn_es_away_poc{k}"] = np.zeros(n, dtype=np.int8)

    for i, stamp in enumerate(ev_ts):
        nq_px = _last_px_pts(nq_trades, int(stamp))
        es_px = _last_px_pts(es_trades, int(stamp))
        nq_poc = asof_dvpoc(int(stamp), nq_bars)
        es_poc = asof_dvpoc(int(stamp), es_bars)
        cols["hvn_nq_poc_px"][i] = nq_poc
        cols["hvn_es_poc_px"][i] = es_poc
        cols["hvn_nq_poc_dist"][i] = (
            abs(nq_px - nq_poc) if np.isfinite(nq_px) and np.isfinite(nq_poc) else float("nan")
        )
        cols["hvn_es_poc_dist"][i] = (
            abs(es_px - es_poc) if np.isfinite(es_px) and np.isfinite(es_poc) else float("nan")
        )
        nf = poc_clearance_flags(
            int(nq_toward[i]), int(nq_away[i]), nq_px, nq_poc, cfg.poc_mins,
        )
        ef = poc_clearance_flags(
            int(es_toward[i]), int(es_away[i]), es_px, es_poc, cfg.poc_mins,
        )
        for k in cfg.poc_mins:
            cols[f"hvn_nq_toward_poc{k}"][i] = nf[f"toward_poc{k}"]
            cols[f"hvn_nq_away_poc{k}"][i] = nf[f"away_poc{k}"]
            cols[f"hvn_es_toward_poc{k}"][i] = ef[f"toward_poc{k}"]
            cols[f"hvn_es_away_poc{k}"][i] = ef[f"away_poc{k}"]

    out = base
    for name, arr in cols.items():
        typ = pa.float64() if arr.dtype.kind == "f" else pa.int8()
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=typ))
    return out


# re-export helpers used by tests
__all__ = [
    "HvnMagnetConfig",
    "POC_MINS",
    "annotate_hvn_magnet",
    "asof_dvpoc",
    "poc_clearance_flags",
    "direction_flags",
    "nearest_hvn",
]
