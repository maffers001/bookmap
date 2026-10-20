"""HV Extreme Move detector — Python port of HvExtremeMoveTracker.java.

Parquet ``price`` is already in tick units (tick_size=1). Distances in the
config are in **chart points**; they are converted to ticks via
``pts_per_tick`` (NQ = 0.25 → 20 pts = 80 ticks).

Side: +1 LONG (HV trough → move up), −1 SHORT (HV peak → move down).

Quiet-leg gates (0 = off):
  Gate B: ``leg_vol / (avg_vol_per_tick × outside_ticks) < max_leg_vol_vs_avg``
          where ``outside_ticks = max(1, move_ticks − band_half)``.
  Gate A: ``leg_vol / extreme_vol < max_leg_vol_vs_tip``.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque

import numpy as np
import pyarrow as pa

from research.features.regime import build_regime_index, lookup_regime


@dataclass
class HvExtremeConfig:
    move_pts: float = 20.0
    confirm_pts: float = 10.0
    peak_band_pts: float = 5.0
    high_vol_mult: float = 2.0
    min_path_efficiency: float = 0.0  # 0 = off
    max_pullback_pts: float = 5.0  # 0 = off
    max_leg_sec: float = 0.0  # 0 = off
    max_leg_vol_vs_avg: float = 0.0  # 0 = off; Gate B
    max_leg_vol_vs_tip: float = 0.0  # 0 = off; Gate A
    lookback_min: float = 15.0
    cooldown_sec: float = 30.0
    pts_per_tick: float = 0.25
    vol_window_min: int = 30

    def move_ticks(self) -> int:
        return max(1, int(round(self.move_pts / self.pts_per_tick)))

    def confirm_ticks(self) -> int:
        return max(1, int(round(self.confirm_pts / self.pts_per_tick)))

    def band_ticks(self) -> int:
        return max(0, int(round(self.peak_band_pts / self.pts_per_tick)))

    def max_pullback_ticks(self) -> int:
        if self.max_pullback_pts <= 0:
            return 0
        return max(0, int(round(self.max_pullback_pts / self.pts_per_tick)))

    def lookback_ns(self) -> int:
        return int(max(1.0, self.lookback_min) * 60_000_000_000)

    def cooldown_ns(self) -> int:
        return int(max(0.0, self.cooldown_sec) * 1_000_000_000)

    def max_leg_ns(self) -> int:
        if self.max_leg_sec <= 0:
            return 0
        return int(self.max_leg_sec * 1_000_000_000)


@dataclass
class _Print:
    ns: int
    tick: int
    size: float


class HvExtremeMoveTracker:
    """Stateful port of the Java tracker. Feed trades in time order."""

    NONE, LONG, SHORT = 0, 1, -1

    def __init__(self, cfg: HvExtremeConfig):
        self.move_ticks = cfg.move_ticks()
        self.confirm_ticks = cfg.confirm_ticks()
        self.extreme_band_ticks = cfg.band_ticks()
        self.high_vol_mult = max(0.1, cfg.high_vol_mult)
        self.lookback_ns = cfg.lookback_ns()
        self.cooldown_ns = cfg.cooldown_ns()
        self.min_path_efficiency = max(0.0, cfg.min_path_efficiency)
        self.max_pullback_ticks_allowed = cfg.max_pullback_ticks()
        self.max_leg_ns = cfg.max_leg_ns()
        self.max_leg_vol_vs_avg = max(0.0, cfg.max_leg_vol_vs_avg)
        self.max_leg_vol_vs_tip = max(0.0, cfg.max_leg_vol_vs_tip)

        self.prints: Deque[_Print] = deque()
        self.vol_by_tick: dict[int, float] = {}
        self.total_vol = 0.0

        self.last_tick = None
        self.run_hi = -(10**12)
        self.run_lo = 10**12
        self.run_hi_ns = 0
        self.run_lo_ns = 0

        self.watching = self.NONE
        self.extreme_tick = 0
        self.extreme_vol = 0.0
        self.extreme_vol_ratio = 0.0
        self.arm_ns = 0
        self.path_last_tick = None
        self.path_length_ticks = 0
        self.max_pullback_ticks = 0
        self.favor_tick = 0
        self.leg_vol = 0.0

        self.last_signal_ns = -(10**18)
        self.pending: dict | None = None

    def on_trade(self, t_ns: int, tick: int, size: float) -> None:
        if size <= 0:
            return
        prev = self.last_tick
        self.last_tick = tick
        self._add(t_ns, tick, size)
        self._prune(t_ns)

        if tick >= self.run_hi:
            self.run_hi = tick
            self.run_hi_ns = t_ns
        if tick <= self.run_lo:
            self.run_lo = tick
            self.run_lo_ns = t_ns

        if self.watching == self.NONE:
            self._try_watch(t_ns, tick, size)
        else:
            if self._is_outside_peak_band(tick):
                self.leg_vol += size
            self._update_watch(t_ns, tick, prev, size)

    def poll_signal(self) -> dict | None:
        s = self.pending
        self.pending = None
        return s

    def _try_watch(self, t_ns: int, tick: int, size: float) -> None:
        if t_ns - self.last_signal_ns < self.cooldown_ns:
            return
        if self.run_hi - tick >= self.confirm_ticks:
            if self._arm_if_high_vol(self.SHORT, self.run_hi, t_ns, tick, size):
                return
            self.run_hi = tick
            self.run_hi_ns = t_ns
        if tick - self.run_lo >= self.confirm_ticks:
            if self._arm_if_high_vol(self.LONG, self.run_lo, t_ns, tick, size):
                return
            self.run_lo = tick
            self.run_lo_ns = t_ns

    def _arm_if_high_vol(
        self, side: int, extreme: int, t_ns: int, tick: int, size: float
    ) -> bool:
        avg = self._avg_vol_per_tick()
        if avg <= 0:
            return False
        half = self.extreme_band_ticks
        vol = self._volume_in_range(extreme - half, extreme + half)
        width = max(1, 2 * half + 1)
        ratio = vol / (avg * width)
        if ratio < self.high_vol_mult:
            return False
        self.watching = side
        self.extreme_tick = extreme
        self.extreme_vol = vol
        self.extreme_vol_ratio = ratio
        self.arm_ns = t_ns
        self.path_last_tick = tick
        self.path_length_ticks = abs(tick - extreme)
        self.max_pullback_ticks = 0
        self.favor_tick = tick
        self.leg_vol = 0.0
        if self._is_outside_peak_band(tick):
            self.leg_vol = float(size)
        return True

    def _is_outside_peak_band(self, tick: int) -> bool:
        half = self.extreme_band_ticks
        if self.watching == self.LONG:
            return tick > self.extreme_tick + half
        if self.watching == self.SHORT:
            return tick < self.extreme_tick - half
        return False

    def _outside_ticks(self) -> int:
        return max(1, self.move_ticks - self.extreme_band_ticks)

    def _update_watch(
        self, t_ns: int, tick: int, prev_tick: int | None, size: float
    ) -> None:
        if prev_tick is not None and self.path_last_tick is not None:
            self.path_length_ticks += abs(tick - self.path_last_tick)
        self.path_last_tick = tick

        if self.watching == self.SHORT:
            if tick > self.extreme_tick:
                self._clear_watch(t_ns, tick)
                self._try_watch(t_ns, tick, size)
                return
            if tick < self.favor_tick:
                self.favor_tick = tick
            giveback = tick - self.favor_tick
            if giveback > self.max_pullback_ticks:
                self.max_pullback_ticks = giveback
            if self.max_leg_ns > 0 and t_ns - self.arm_ns > self.max_leg_ns:
                self._clear_watch(t_ns, tick)
                return
            if (
                self.max_pullback_ticks_allowed > 0
                and self.max_pullback_ticks > self.max_pullback_ticks_allowed
            ):
                self._clear_watch(t_ns, tick)
                return
            if self.extreme_tick - tick >= self.move_ticks:
                self._try_fire(t_ns, tick)
        elif self.watching == self.LONG:
            if tick < self.extreme_tick:
                self._clear_watch(t_ns, tick)
                self._try_watch(t_ns, tick, size)
                return
            if tick > self.favor_tick:
                self.favor_tick = tick
            giveback = self.favor_tick - tick
            if giveback > self.max_pullback_ticks:
                self.max_pullback_ticks = giveback
            if self.max_leg_ns > 0 and t_ns - self.arm_ns > self.max_leg_ns:
                self._clear_watch(t_ns, tick)
                return
            if (
                self.max_pullback_ticks_allowed > 0
                and self.max_pullback_ticks > self.max_pullback_ticks_allowed
            ):
                self._clear_watch(t_ns, tick)
                return
            if tick - self.extreme_tick >= self.move_ticks:
                self._try_fire(t_ns, tick)

    def _try_fire(self, t_ns: int, tick: int) -> None:
        disp = abs(tick - self.extreme_tick)
        path_len = max(1.0, float(self.path_length_ticks))
        eff = disp / path_len
        if self.min_path_efficiency > 0 and eff < self.min_path_efficiency:
            self._clear_watch(t_ns, tick)
            return
        avg = self._avg_vol_per_tick()
        expected = avg * self._outside_ticks()
        leg_ratio = (self.leg_vol / expected) if expected > 0 else float("inf")
        if self.max_leg_vol_vs_avg > 0 and not (leg_ratio < self.max_leg_vol_vs_avg):
            self._clear_watch(t_ns, tick)
            return
        vs_tip = (
            (self.leg_vol / self.extreme_vol) if self.extreme_vol > 0 else float("inf")
        )
        if self.max_leg_vol_vs_tip > 0 and not (vs_tip < self.max_leg_vol_vs_tip):
            self._clear_watch(t_ns, tick)
            return
        leg_sec = (t_ns - self.arm_ns) / 1_000_000_000.0
        self.pending = {
            "event_ts_ns": t_ns,
            "side": int(self.watching),
            "extreme_tick": int(self.extreme_tick),
            "trigger_tick": int(tick),
            "extreme_vol": float(self.extreme_vol),
            "avg_vol_per_tick": float(avg),
            "vol_ratio": float(self.extreme_vol_ratio),
            "move_ticks": int(self.move_ticks),
            "path_eff": float(eff),
            "max_pullback_ticks": int(self.max_pullback_ticks),
            "leg_sec": float(leg_sec),
            "leg_vol": float(self.leg_vol),
            "leg_vol_ratio": float(leg_ratio),
            "leg_vol_vs_tip": float(vs_tip),
        }
        self.last_signal_ns = t_ns
        self.watching = self.NONE
        self.run_hi = tick
        self.run_lo = tick
        self.run_hi_ns = t_ns
        self.run_lo_ns = t_ns
        self._reset_path()

    def _clear_watch(self, t_ns: int, tick: int) -> None:
        self.watching = self.NONE
        self.run_hi = tick
        self.run_lo = tick
        self.run_hi_ns = t_ns
        self.run_lo_ns = t_ns
        self._reset_path()

    def _reset_path(self) -> None:
        self.path_length_ticks = 0
        self.max_pullback_ticks = 0
        self.path_last_tick = None
        self.favor_tick = 0
        self.arm_ns = 0
        self.extreme_vol_ratio = 0.0
        self.leg_vol = 0.0

    def _avg_vol_per_tick(self) -> float:
        if not self.vol_by_tick or self.total_vol <= 0:
            return 0.0
        keys = self.vol_by_tick.keys()
        span = max(1, max(keys) - min(keys) + 1)
        return self.total_vol / span

    def _volume_in_range(self, lo: int, hi: int) -> float:
        if hi < lo:
            lo, hi = hi, lo
        get = self.vol_by_tick.get
        return float(sum(get(k, 0.0) for k in range(lo, hi + 1)))

    def _add(self, ns: int, tick: int, size: float) -> None:
        self.prints.append(_Print(ns, tick, size))
        self.vol_by_tick[tick] = self.vol_by_tick.get(tick, 0.0) + size
        self.total_vol += size

    def _prune(self, now_ns: int) -> None:
        cut = now_ns - self.lookback_ns
        while self.prints and self.prints[0].ns < cut:
            p = self.prints.popleft()
            self.total_vol -= p.size
            v = self.vol_by_tick.get(p.tick)
            if v is None:
                continue
            nxt = v - p.size
            if nxt <= 1e-9:
                del self.vol_by_tick[p.tick]
            else:
                self.vol_by_tick[p.tick] = nxt
        if self.total_vol < 0:
            self.total_vol = 0.0


def hv_extreme_events(
    trades: pa.Table,
    cfg: HvExtremeConfig | None = None,
    *,
    window_ns: tuple[int, int] | None = None,
    attach_med_r30: bool = True,
) -> pa.Table:
    """Detect HV extreme fires on ``trades``.

    Run over the full table (for profile warm-up). If ``window_ns`` is set,
    keep only events with ``event_ts_ns`` inside ``[lo, hi]``.
    """
    cfg = cfg or HvExtremeConfig()
    if trades.num_rows == 0:
        return _empty_events()

    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = np.round(trades.column("price").to_numpy().astype(np.float64)).astype(np.int64)
    sz = trades.column("size").to_numpy().astype(np.float64)
    order = np.argsort(ts, kind="stable")
    ts, px, sz = ts[order], px[order], sz[order]

    tracker = HvExtremeMoveTracker(cfg)
    rows: list[dict] = []
    for t, tick, s in zip(ts, px, sz):
        tracker.on_trade(int(t), int(tick), float(s))
        sig = tracker.poll_signal()
        if sig is not None:
            rows.append(sig)

    if not rows:
        return _empty_events()

    if window_ns is not None:
        lo, hi = window_ns
        rows = [r for r in rows if lo <= r["event_ts_ns"] <= hi]
        if not rows:
            return _empty_events()

    if attach_med_r30:
        bars, bar_ts, cum_pv, cum_v, trade_ts, trade_px, trade_sz = build_regime_index(
            trades, pts_per_tick=cfg.pts_per_tick
        )
        ev_ts = np.asarray([r["event_ts_ns"] for r in rows], dtype=np.int64)
        snaps = lookup_regime(
            bars,
            bar_ts,
            cum_pv,
            cum_v,
            ev_ts,
            window_min=cfg.vol_window_min,
            trade_ts=trade_ts,
            trade_px=trade_px,
            trade_sz=trade_sz,
        )
        for r, snap in zip(rows, snaps):
            r["med_r30"] = float(snap.median_minute_range)
    else:
        for r in rows:
            r["med_r30"] = float("nan")

    cols = {
        "event_ts_ns": pa.array([r["event_ts_ns"] for r in rows], type=pa.int64()),
        "side": pa.array([r["side"] for r in rows], type=pa.int8()),
        "extreme_tick": pa.array([r["extreme_tick"] for r in rows], type=pa.int64()),
        "trigger_tick": pa.array([r["trigger_tick"] for r in rows], type=pa.int64()),
        "extreme_vol": pa.array([r["extreme_vol"] for r in rows], type=pa.float64()),
        "avg_vol_per_tick": pa.array([r["avg_vol_per_tick"] for r in rows], type=pa.float64()),
        "vol_ratio": pa.array([r["vol_ratio"] for r in rows], type=pa.float64()),
        "move_ticks": pa.array([r["move_ticks"] for r in rows], type=pa.int32()),
        "path_eff": pa.array([r["path_eff"] for r in rows], type=pa.float64()),
        "max_pullback_ticks": pa.array(
            [r["max_pullback_ticks"] for r in rows], type=pa.int32()
        ),
        "leg_sec": pa.array([r["leg_sec"] for r in rows], type=pa.float64()),
        "leg_vol": pa.array([r["leg_vol"] for r in rows], type=pa.float64()),
        "leg_vol_ratio": pa.array([r["leg_vol_ratio"] for r in rows], type=pa.float64()),
        "leg_vol_vs_tip": pa.array([r["leg_vol_vs_tip"] for r in rows], type=pa.float64()),
        "med_r30": pa.array([r["med_r30"] for r in rows], type=pa.float64()),
        "event_type": pa.array(["hv_extreme"] * len(rows), type=pa.string()),
    }
    return pa.table(cols)


def _empty_events() -> pa.Table:
    return pa.table(
        {
            "event_ts_ns": pa.array([], type=pa.int64()),
            "side": pa.array([], type=pa.int8()),
            "extreme_tick": pa.array([], type=pa.int64()),
            "trigger_tick": pa.array([], type=pa.int64()),
            "extreme_vol": pa.array([], type=pa.float64()),
            "avg_vol_per_tick": pa.array([], type=pa.float64()),
            "vol_ratio": pa.array([], type=pa.float64()),
            "move_ticks": pa.array([], type=pa.int32()),
            "path_eff": pa.array([], type=pa.float64()),
            "max_pullback_ticks": pa.array([], type=pa.int32()),
            "leg_sec": pa.array([], type=pa.float64()),
            "leg_vol": pa.array([], type=pa.float64()),
            "leg_vol_ratio": pa.array([], type=pa.float64()),
            "leg_vol_vs_tip": pa.array([], type=pa.float64()),
            "med_r30": pa.array([], type=pa.float64()),
            "event_type": pa.array([], type=pa.string()),
        }
    )
