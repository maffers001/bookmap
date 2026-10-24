"""Opening range construction and breakout detection (EXP-0054 / H45).

The opening range (OR) is the traded high/low of the first 15 minutes of
RTH, UK 14:30-14:45 (Europe/London; the runner supplies the ns bounds).
All prices here are chart points (parquet ``price`` ticks x 0.25, the same
convention as ``research/outcomes/label.py``); tick-space copies are kept
on the dataclass so the 1-tick pierce test is exact.

Session state (Relative Volume, contraction ratio) is causal by
construction: ``annotate_causal_state`` only ever looks at sessions that
precede the row being annotated.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pyarrow as pa

PTS_PER_TICK = 0.25


@dataclass
class OpeningRange:
    valid: bool
    orh_pts: float = float("nan")
    orl_pts: float = float("nan")
    orh_ticks: float = float("nan")
    orl_ticks: float = float("nan")
    open_pts: float = float("nan")
    close_pts: float = float("nan")
    vwap_pts: float = float("nan")
    volume: float = 0.0
    n_trades: int = 0
    first_ts_ns: int = 0
    last_ts_ns: int = 0
    orh_ts_ns: int = 0
    orl_ts_ns: int = 0

    @property
    def width_pts(self) -> float:
        return self.orh_pts - self.orl_pts

    @property
    def direction(self) -> int:
        if not self.valid:
            return 0
        if self.close_pts > self.open_pts:
            return 1
        if self.close_pts < self.open_pts:
            return -1
        return 0


def _sorted(trades: pa.Table) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = trades.column("price").to_numpy().astype(np.float64)
    sz = trades.column("size").to_numpy().astype(np.float64)
    o = np.argsort(ts, kind="stable")
    return ts[o], px[o], sz[o]


def build_opening_range(
    trades: pa.Table,
    or_start_ns: int,
    or_end_ns: int,
    *,
    pts_per_tick: float = PTS_PER_TICK,
    max_first_print_delay_sec: float = 60.0,
) -> OpeningRange:
    """OR from trades in [or_start_ns, or_end_ns). Invalid if the market did
    not trade in the window or its first print is later than the delay cap
    (data-coverage rule, frozen in EXP-0054.yaml)."""
    if trades.num_rows == 0:
        return OpeningRange(valid=False)
    ts, px_ticks, sz = _sorted(trades)
    k = (ts >= or_start_ns) & (ts < or_end_ns)
    if not k.any():
        return OpeningRange(valid=False)
    ts_w, px_w, sz_w = ts[k], px_ticks[k], sz[k]
    first_ts = int(ts_w[0])
    if first_ts - int(or_start_ns) > int(max_first_print_delay_sec * 1e9):
        return OpeningRange(valid=False)
    hi_i = int(np.argmax(px_w))
    lo_i = int(np.argmin(px_w))
    vol = float(sz_w.sum())
    vwap_ticks = float((px_w * sz_w).sum() / vol) if vol > 0 else float(px_w[-1])
    return OpeningRange(
        valid=True,
        orh_pts=float(px_w[hi_i] * pts_per_tick),
        orl_pts=float(px_w[lo_i] * pts_per_tick),
        orh_ticks=float(px_w[hi_i]),
        orl_ticks=float(px_w[lo_i]),
        open_pts=float(px_w[0] * pts_per_tick),
        close_pts=float(px_w[-1] * pts_per_tick),
        vwap_pts=float(vwap_ticks * pts_per_tick),
        volume=vol,
        n_trades=int(len(ts_w)),
        first_ts_ns=first_ts,
        last_ts_ns=int(ts_w[-1]),
        orh_ts_ns=int(ts_w[hi_i]),
        orl_ts_ns=int(ts_w[lo_i]),
    )


def session_range_pts(
    trades: pa.Table,
    start_ns: int,
    end_ns: int,
    *,
    pts_per_tick: float = PTS_PER_TICK,
) -> float:
    """High-low range (pts) of trades in [start_ns, end_ns); NaN if empty."""
    if trades.num_rows == 0:
        return float("nan")
    ts, px_ticks, _ = _sorted(trades)
    k = (ts >= start_ns) & (ts < end_ns)
    if not k.any():
        return float("nan")
    return float((px_ticks[k].max() - px_ticks[k].min()) * pts_per_tick)


def annotate_causal_state(
    rows: list[dict],
    *,
    prior_n: int = 14,
) -> list[dict]:
    """Add relvol / contraction_ratio / prior_range_median to session rows.

    ``rows`` must be chronological for ONE market, each with ``or_volume``,
    ``or_width_pts`` and ``session_range_pts``. Only strictly prior rows are
    used; values are NaN (warmup) until ``prior_n`` valid prior rows exist.
    Returns the same list (mutated) for convenience.
    """
    hist_vol: list[float] = []
    hist_rng: list[float] = []
    for r in rows:
        if len(hist_vol) >= prior_n:
            r["relvol"] = float(r["or_volume"] / np.mean(hist_vol[-prior_n:])) \
                if np.mean(hist_vol[-prior_n:]) > 0 else float("nan")
            med_rng = float(np.median(hist_rng[-prior_n:]))
            r["prior_range_median"] = med_rng
            r["contraction_ratio"] = float(r["or_width_pts"] / med_rng) if med_rng > 0 else float("nan")
        else:
            r["relvol"] = float("nan")
            r["prior_range_median"] = float("nan")
            r["contraction_ratio"] = float("nan")
        if np.isfinite(r.get("or_volume", np.nan)) and r["or_volume"] > 0:
            hist_vol.append(float(r["or_volume"]))
        if np.isfinite(r.get("session_range_pts", np.nan)):
            hist_rng.append(float(r["session_range_pts"]))
    return rows


@dataclass
class Breakout:
    event_ts_ns: int
    side: int
    entry_px_pts: float
    opposite_ts_ns: int = 0  # first opposite-side break (descriptive, 0=none)
    opposite_side: int = 0


def detect_breakout(
    trades: pa.Table,
    orng: OpeningRange,
    or_end_ns: int,
    window_end_ns: int,
    *,
    pts_per_tick: float = PTS_PER_TICK,
    pierce_ticks: float = 1.0,
) -> Breakout | None:
    """First print >= pierce_ticks beyond the OR extreme in the OR direction.

    Long (bullish OR): first print with price_ticks >= orh_ticks + pierce.
    Short mirrors. The opposite-side first break is stamped too (descriptive
    stratum only). Returns None for doji/invalid ORs or no break by
    ``window_end_ns``.
    """
    if not orng.valid or orng.direction == 0 or trades.num_rows == 0:
        return None
    ts, px_ticks, _ = _sorted(trades)
    k = (ts >= or_end_ns) & (ts <= window_end_ns)
    if not k.any():
        return None
    ts_w, px_w = ts[k], px_ticks[k]
    side = orng.direction
    if side > 0:
        hit = px_w >= orng.orh_ticks + pierce_ticks
        opp = px_w <= orng.orl_ticks - pierce_ticks
    else:
        hit = px_w <= orng.orl_ticks - pierce_ticks
        opp = px_w >= orng.orh_ticks + pierce_ticks
    opp_ts, opp_side = 0, 0
    if opp.any():
        j = int(np.argmax(opp))
        opp_ts, opp_side = int(ts_w[j]), -side
    if not hit.any():
        return None
    i = int(np.argmax(hit))
    return Breakout(
        event_ts_ns=int(ts_w[i]),
        side=side,
        entry_px_pts=float(px_w[i] * pts_per_tick),
        opposite_ts_ns=opp_ts,
        opposite_side=opp_side,
    )


def first_reentry_ts(
    trades: pa.Table,
    orng: OpeningRange,
    side: int,
    from_ns: int,
    to_ns: int,
    *,
    pts_per_tick: float = PTS_PER_TICK,
) -> int:
    """First print back inside the OR after ``from_ns`` (0 = never).

    Long: print <= ORH. Short: print >= ORL. Used for the failed-break
    label and the Arm C no-reentry condition (descriptive/label only --
    never part of the Arm A/B entry stamp).
    """
    if trades.num_rows == 0 or not orng.valid:
        return 0
    ts, px_ticks, _ = _sorted(trades)
    k = (ts >= from_ns) & (ts <= to_ns)
    if not k.any():
        return 0
    ts_w, px_w = ts[k], px_ticks[k]
    inside = px_w <= orng.orh_ticks if side > 0 else px_w >= orng.orl_ticks
    if not inside.any():
        return 0
    return int(ts_w[int(np.argmax(inside))])
