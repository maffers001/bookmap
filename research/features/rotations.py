"""Causal rotation length: swing-to-swing moves vs rolling average.

A completed rotation is the absolute tick distance between consecutive
confirmed opposite swings (LOW→HIGH or HIGH→LOW). At any as-of time the
*current leg* runs from the last confirmed pivot to the latest print;
extension / avg_rotation is the filter ratio.

Momentum: keep when current leg is *fresh* (ratio < mult).
Reversal / fade: keep when current leg is *extended* (ratio >= mult).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pyarrow as pa

from research.features.swings import SwingConfig, SwingPivot, detect_swings


@dataclass
class RotationConfig:
    swing: SwingConfig = field(default_factory=SwingConfig)
    avg_n: int = 8  # last N completed rotations for the average
    extend_mult: float = 1.0  # extended when extension >= mult * avg
    min_rotations: int = 3  # need this many completed before filter is live


@dataclass(frozen=True)
class CompletedRotation:
    end_confirm_ts_ns: int
    length_ticks: float
    direction: int  # +1 up (LOW→HIGH), -1 down (HIGH→LOW)


def _price_ticks(trades: pa.Table, tick_size: float) -> tuple[np.ndarray, np.ndarray]:
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = np.round(trades.column("price").to_numpy().astype(np.float64) / tick_size).astype(
        np.int64
    )
    order = np.argsort(ts)
    return ts[order], px[order]


def completed_rotations(pivots: list[SwingPivot]) -> list[CompletedRotation]:
    """Build alternating swing chain; each opposite pair is one rotation."""
    if not pivots:
        return []
    ordered = sorted(pivots, key=lambda p: (p.confirm_ts_ns, p.ts_ns))
    chain: list[SwingPivot] = []
    for p in ordered:
        if not chain:
            chain.append(p)
            continue
        if p.kind == chain[-1].kind:
            # same kind: keep the later extreme in the continuing move
            if p.kind == "HIGH" and p.price_ticks >= chain[-1].price_ticks:
                chain[-1] = p
            elif p.kind == "LOW" and p.price_ticks <= chain[-1].price_ticks:
                chain[-1] = p
            # else ignore (shallower same-kind)
            continue
        chain.append(p)

    out: list[CompletedRotation] = []
    for i in range(1, len(chain)):
        a, b = chain[i - 1], chain[i]
        length = float(abs(b.price_ticks - a.price_ticks))
        if length <= 0:
            continue
        direction = 1 if b.kind == "HIGH" else -1
        out.append(
            CompletedRotation(
                end_confirm_ts_ns=b.confirm_ts_ns,
                length_ticks=length,
                direction=direction,
            )
        )
    return out


def _avg_asof(
    rots: list[CompletedRotation],
    asof_ts_ns: int,
    avg_n: int,
    min_rotations: int,
) -> float | None:
    """Mean of last avg_n rotations fully confirmed at or before asof."""
    # rots sorted by end_confirm
    lengths: list[float] = []
    for r in rots:
        if r.end_confirm_ts_ns > asof_ts_ns:
            break
        lengths.append(r.length_ticks)
    if len(lengths) < min_rotations:
        return None
    window = lengths[-avg_n:]
    return float(np.mean(window))


def _last_pivot_asof(
    pivots: list[SwingPivot], asof_ts_ns: int, kind: str | None = None
) -> SwingPivot | None:
    best: SwingPivot | None = None
    for p in pivots:
        if p.confirm_ts_ns > asof_ts_ns:
            continue
        if kind is not None and p.kind != kind:
            continue
        if best is None or p.confirm_ts_ns > best.confirm_ts_ns:
            best = p
    return best


def _open_leg(
    pivots: list[SwingPivot],
    asof_ts_ns: int,
    px: int,
) -> tuple[int, float] | None:
    """Current trending leg direction and extension from rotation origin.

    Breakout past last HIGH still measures from last LOW (full up-rotation).
    Inside the prior high/low range, leg runs from the most recent pivot.
    """
    low = _last_pivot_asof(pivots, asof_ts_ns, "LOW")
    high = _last_pivot_asof(pivots, asof_ts_ns, "HIGH")
    last = _last_pivot_asof(pivots, asof_ts_ns, None)
    if last is None:
        return None

    if low is not None and high is not None:
        if px >= high.price_ticks:
            return 1, float(px - low.price_ticks)
        if px <= low.price_ticks:
            return -1, float(high.price_ticks - px)

    if last.kind == "LOW":
        return 1, float(max(0, px - last.price_ticks))
    return -1, float(max(0, last.price_ticks - px))


def rotation_state_at(
    pivots: list[SwingPivot],
    rots: list[CompletedRotation],
    tr_ts: np.ndarray,
    tr_px: np.ndarray,
    asof_ts_ns: int,
    cfg: RotationConfig,
) -> dict:
    """Causal rotation metrics at asof. Missing avg → filter inactive (nan flags 0)."""
    avg = _avg_asof(rots, asof_ts_ns, cfg.avg_n, cfg.min_rotations)
    j = int(np.searchsorted(tr_ts, asof_ts_ns, side="right") - 1)
    empty = {
        "rot_avg_ticks": np.nan,
        "rot_extension_ticks": np.nan,
        "rot_ratio": np.nan,
        "rot_leg_dir": 0,
        "rot_extended": 0,
        "rot_fresh": 0,
        "rot_ready": 0,
    }
    if j < 0:
        return empty
    leg = _open_leg(pivots, asof_ts_ns, int(tr_px[j]))
    if leg is None:
        return empty
    leg_dir, extension = leg

    ready = avg is not None and avg > 0
    ratio = float(extension / avg) if ready else np.nan
    extended = 1 if ready and ratio >= cfg.extend_mult else 0
    fresh = 1 if ready and ratio < cfg.extend_mult else 0
    return {
        "rot_avg_ticks": float(avg) if ready else np.nan,
        "rot_extension_ticks": extension,
        "rot_ratio": ratio,
        "rot_leg_dir": leg_dir,
        "rot_extended": extended,
        "rot_fresh": fresh,
        "rot_ready": 1 if ready else 0,
    }


def annotate_rotation_state(
    events: pa.Table,
    trades: pa.Table,
    cfg: RotationConfig | None = None,
) -> pa.Table:
    """Append rotation columns to events (by event_ts_ns)."""
    cfg = cfg or RotationConfig()
    if events.num_rows == 0:
        return events
    pivots = detect_swings(trades, cfg.swing)
    rots = completed_rotations(pivots)
    tr_ts, tr_px = _price_ticks(trades, cfg.swing.tick_size)
    ts = events.column("event_ts_ns").to_numpy().astype(np.int64)

    n = len(ts)
    avg_a = np.full(n, np.nan)
    ext_a = np.full(n, np.nan)
    ratio_a = np.full(n, np.nan)
    leg_a = np.zeros(n, dtype=np.int8)
    ext_f = np.zeros(n, dtype=np.int8)
    fresh_f = np.zeros(n, dtype=np.int8)
    ready_f = np.zeros(n, dtype=np.int8)

    for i, t0 in enumerate(ts):
        st = rotation_state_at(pivots, rots, tr_ts, tr_px, int(t0), cfg)
        avg_a[i] = st["rot_avg_ticks"]
        ext_a[i] = st["rot_extension_ticks"]
        ratio_a[i] = st["rot_ratio"]
        leg_a[i] = st["rot_leg_dir"]
        ext_f[i] = st["rot_extended"]
        fresh_f[i] = st["rot_fresh"]
        ready_f[i] = st["rot_ready"]

    out = events
    for name in (
        "rot_avg_ticks",
        "rot_extension_ticks",
        "rot_ratio",
        "rot_leg_dir",
        "rot_extended",
        "rot_fresh",
        "rot_ready",
    ):
        if name in out.column_names:
            out = out.drop(name)
    out = out.append_column("rot_avg_ticks", pa.array(avg_a, type=pa.float64()))
    out = out.append_column("rot_extension_ticks", pa.array(ext_a, type=pa.float64()))
    out = out.append_column("rot_ratio", pa.array(ratio_a, type=pa.float64()))
    out = out.append_column("rot_leg_dir", pa.array(leg_a, type=pa.int8()))
    out = out.append_column("rot_extended", pa.array(ext_f, type=pa.int8()))
    out = out.append_column("rot_fresh", pa.array(fresh_f, type=pa.int8()))
    out = out.append_column("rot_ready", pa.array(ready_f, type=pa.int8()))
    return out


def momentum_rotation_ok(events: pa.Table) -> np.ndarray:
    """True where rotation filter allows a momentum (with-leg) entry."""
    if events.num_rows == 0:
        return np.array([], dtype=bool)
    side = events.column("side").to_numpy().astype(np.int8)
    leg = events.column("rot_leg_dir").to_numpy().astype(np.int8)
    fresh = events.column("rot_fresh").to_numpy().astype(np.int8)
    ready = events.column("rot_ready").to_numpy().astype(np.int8)
    # With-trend + not past avg rotation. If filter not ready, pass through.
    with_leg = side == leg
    return ((ready == 0) | ((fresh == 1) & with_leg)).astype(bool)


def reversal_rotation_ok(events: pa.Table) -> np.ndarray:
    """True where rotation filter allows a fade (against extended leg)."""
    if events.num_rows == 0:
        return np.array([], dtype=bool)
    side = events.column("side").to_numpy().astype(np.int8)
    leg = events.column("rot_leg_dir").to_numpy().astype(np.int8)
    extended = events.column("rot_extended").to_numpy().astype(np.int8)
    ready = events.column("rot_ready").to_numpy().astype(np.int8)
    against = side == -leg
    # Require ready + extended against-leg; unready → reject fade (need extension signal)
    return ((ready == 1) & (extended == 1) & against).astype(bool)
