"""MAT × IRT HVN vicinity with toward/away (H85 / H86).

At each MAT stamp: nearest as-of HVN1/2/3 vs last print ≤ stamp.
Near = dist ≤ band. Toward = MAT side points at that nearest HVN
(long ∧ HVN > px; short ∧ HVN < px). Away = opposite. Dist≈0 →
neither toward nor away. Missing IRT → undefined (NaN), not zero.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from research.irt_lvn.events import PTS_PER_TICK, _asof_idx
from research.irt_lvn.load_irt import IrtBars


@dataclass
class HvnVicinityConfig:
    nq_band_pts: float = 5.0
    es_band_pts: float = 2.0
    eps_pts: float = 1e-6  # treat as at-level (no direction)


def _hvn123(bars: IrtBars, i: int) -> list[float]:
    if i < 0 or i >= len(bars.ts_ns):
        return []
    out: list[float] = []
    for px in (float(bars.hvn1[i]), float(bars.hvn2[i]), float(bars.hvn3[i])):
        if px != 0.0:
            out.append(px)
    return out


def _last_px_pts(trades: pa.Table, stamp_ns: int) -> float:
    if trades.num_rows == 0:
        return float("nan")
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64) * PTS_PER_TICK
    i = int(np.searchsorted(ts, stamp_ns, side="right") - 1)
    if i < 0:
        return float("nan")
    return float(px[i])


def nearest_hvn(
    stamp_ns: int,
    last_px_pts: float,
    bars: IrtBars,
) -> tuple[float, float]:
    """Return (nearest_hvn_px, dist) or (nan, nan)."""
    if not np.isfinite(last_px_pts) or len(bars.ts_ns) == 0:
        return float("nan"), float("nan")
    bi = _asof_idx(bars.ts_ns, int(stamp_ns))
    levels = _hvn123(bars, bi)
    if not levels:
        return float("nan"), float("nan")
    best = min(levels, key=lambda lv: abs(last_px_pts - lv))
    return float(best), float(abs(last_px_pts - best))


def direction_flags(
    side: int,
    last_px: float,
    hvn_px: float,
    dist: float,
    band: float,
    *,
    eps: float = 1e-6,
) -> tuple[int, int, int]:
    """Return (near, toward, away) as 0/1. At-level → near only."""
    if not (np.isfinite(dist) and np.isfinite(hvn_px) and np.isfinite(last_px)):
        return 0, 0, 0
    if dist > band:
        return 0, 0, 0
    near = 1
    if dist <= eps or side == 0:
        return near, 0, 0
    # toward: long and HVN above, or short and HVN below
    hvn_above = hvn_px > last_px
    if side > 0:
        toward = 1 if hvn_above else 0
    else:
        toward = 1 if not hvn_above else 0
    away = 1 - toward
    return near, toward, away


def annotate_hvn_vicinity(
    events: pa.Table,
    nq_trades: pa.Table,
    es_trades: pa.Table,
    nq_bars: IrtBars,
    es_bars: IrtBars,
    *,
    cfg: HvnVicinityConfig | None = None,
) -> pa.Table:
    cfg = cfg or HvnVicinityConfig()
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
        "hvn_nq_dist": np.full(n, np.nan),
        "hvn_nq_px": np.full(n, np.nan),
        "hvn_es_dist": np.full(n, np.nan),
        "hvn_es_px": np.full(n, np.nan),
        "hvn_nq_near": np.zeros(n, dtype=np.int8),
        "hvn_nq_toward": np.zeros(n, dtype=np.int8),
        "hvn_nq_away": np.zeros(n, dtype=np.int8),
        "hvn_es_near": np.zeros(n, dtype=np.int8),
        "hvn_es_toward": np.zeros(n, dtype=np.int8),
        "hvn_es_away": np.zeros(n, dtype=np.int8),
    }

    for i, stamp in enumerate(ev_ts):
        side = int(sides[i])
        nq_px = _last_px_pts(nq_trades, int(stamp))
        es_px = _last_px_pts(es_trades, int(stamp))
        nq_h, nq_d = nearest_hvn(int(stamp), nq_px, nq_bars)
        es_h, es_d = nearest_hvn(int(stamp), es_px, es_bars)
        cols["hvn_nq_px"][i] = nq_h
        cols["hvn_nq_dist"][i] = nq_d
        cols["hvn_es_px"][i] = es_h
        cols["hvn_es_dist"][i] = es_d
        nn, nt, na = direction_flags(
            side, nq_px, nq_h, nq_d, cfg.nq_band_pts, eps=cfg.eps_pts
        )
        en, et, ea = direction_flags(
            side, es_px, es_h, es_d, cfg.es_band_pts, eps=cfg.eps_pts
        )
        cols["hvn_nq_near"][i] = nn
        cols["hvn_nq_toward"][i] = nt
        cols["hvn_nq_away"][i] = na
        cols["hvn_es_near"][i] = en
        cols["hvn_es_toward"][i] = et
        cols["hvn_es_away"][i] = ea

    out = events
    for name, arr in cols.items():
        typ = pa.float64() if arr.dtype.kind == "f" else pa.int8()
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=typ))
    return out
