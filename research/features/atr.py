"""Prior-session ATR and causal session range-so-far (H59 / EXP-0068).

prior ATR = mean UK 14:30–17:00 range of the last N clean sessions.
Session range-so-far and |last_px − session_open| at an as-of stamp.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa

UK = ZoneInfo("Europe/London")
PTS_PER_TICK = 0.25


@dataclass
class AtrConfig:
    lookback_sessions: int = 10
    pts_per_tick: float = PTS_PER_TICK
    open_uk: time = time(14, 30)
    close_uk: time = time(17, 0)


def session_uk_bounds(mid_ts_ns: int, cfg: AtrConfig | None = None) -> tuple[int, int]:
    cfg = cfg or AtrConfig()
    d = datetime.fromtimestamp(mid_ts_ns / 1e9, tz=timezone.utc).astimezone(UK).date()
    s = int(datetime.combine(d, cfg.open_uk, tzinfo=UK).timestamp() * 1e9)
    e = int(datetime.combine(d, cfg.close_uk, tzinfo=UK).timestamp() * 1e9)
    return s, e


def session_range_pts(trades: pa.Table, *, pts_per_tick: float = PTS_PER_TICK) -> float:
    """Full-path high-low in chart points over the given trades table."""
    if trades.num_rows == 0:
        return float("nan")
    px = trades.column("price").to_numpy().astype(np.float64) * pts_per_tick
    return float(px.max() - px.min())


def filter_uk_window(trades: pa.Table, cfg: AtrConfig | None = None) -> pa.Table:
    cfg = cfg or AtrConfig()
    if trades.num_rows == 0:
        return trades
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    s, e = session_uk_bounds(int(ts[len(ts) // 2]), cfg)
    k = (ts >= s) & (ts <= e)
    return trades.take(np.where(k)[0]) if k.any() else trades.slice(0, 0)


def prior_atr(
    session_ranges: dict[str, float],
    current_sid: str,
    chrono_sessions: list[str],
    *,
    lookback: int = 10,
) -> float:
    """Mean of last ``lookback`` clean sessions *before* current_sid (causal)."""
    try:
        idx = chrono_sessions.index(current_sid)
    except ValueError:
        return float("nan")
    prior = []
    for sid in chrono_sessions[:idx][::-1]:
        r = session_ranges.get(sid)
        if r is None or not np.isfinite(r) or r <= 0:
            continue
        prior.append(float(r))
        if len(prior) >= lookback:
            break
    if not prior:
        return float("nan")
    return float(np.mean(prior))


def range_so_far_at(
    trades: pa.Table,
    asof_ts_ns: int,
    *,
    session_open_ns: int | None = None,
    pts_per_tick: float = PTS_PER_TICK,
) -> tuple[float, float, float]:
    """Return (range_so_far, dist_from_open, last_px) at asof.

    Uses only prints with ts <= asof (and >= session_open if given).
    """
    if trades.num_rows == 0:
        return float("nan"), float("nan"), float("nan")
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64) * pts_per_tick
    order = np.argsort(ts)
    ts, px = ts[order], px[order]
    j0 = 0
    if session_open_ns is not None:
        j0 = int(np.searchsorted(ts, session_open_ns, side="left"))
    j1 = int(np.searchsorted(ts, asof_ts_ns, side="right"))
    if j1 <= j0:
        return float("nan"), float("nan"), float("nan")
    sl = slice(j0, j1)
    last = float(px[j1 - 1])
    open_px = float(px[j0])
    rng = float(px[sl].max() - px[sl].min())
    return rng, abs(last - open_px), last


def annotate_atr(
    events: pa.Table,
    trades: pa.Table,
    *,
    prior_atr_pts: float,
    session_open_ns: int | None = None,
    pts_per_tick: float = PTS_PER_TICK,
) -> pa.Table:
    """Append atr_prior, range_so_far, atr_ratio, dist_from_open to events."""
    if events.num_rows == 0:
        return events
    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    n = len(ev_ts)
    prior = np.full(n, float(prior_atr_pts) if np.isfinite(prior_atr_pts) else np.nan)
    rsf = np.full(n, np.nan)
    dfo = np.full(n, np.nan)
    ratio = np.full(n, np.nan)
    for i, t0 in enumerate(ev_ts):
        rng, dist, _ = range_so_far_at(
            trades, int(t0), session_open_ns=session_open_ns, pts_per_tick=pts_per_tick
        )
        rsf[i] = rng
        dfo[i] = dist
        if np.isfinite(prior_atr_pts) and prior_atr_pts > 0 and np.isfinite(rng):
            ratio[i] = rng / prior_atr_pts
    out = events
    for name, arr in (
        ("atr_prior", prior),
        ("range_so_far", rsf),
        ("atr_ratio", ratio),
        ("dist_from_open", dfo),
    ):
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=pa.float64()))
    return out
