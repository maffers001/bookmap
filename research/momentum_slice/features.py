"""Causal per-event features for LST candidates (all inputs <= confirm time,
except `pull_est`, which describes the slice itself and is only used as a
descriptor of the event, never as an entry filter).

MBO note: mbo.parquet carries send/cancel/replace only — no execution op and
trades carry only the aggressor order id — so executed-vs-cancelled size for
individual resting orders cannot be reconstructed exactly. `pull_est` is the
depth+trades accounting proxy: per zone price,
unaccounted = displayed(-3s) - traded_aggr(slice 5s) - displayed(+5s),
clipped at >=0; summed / displayed stack. 0 = everything that vanished traded
(or is still there); ->1 = vanished without trading (pulled).
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa

from research.momentum_slice.detector import CFG, ZONE_TICKS, _arrays, stack_metrics


def stack_geometry(book: dict, side: int, ref_ticks: float, role: str) -> dict:
    m = stack_metrics(book, side, ref_ticks, role)
    cfg = CFG[role]
    ref = int(round(ref_ticks))
    ahead = []
    for (is_bid, p), s in book.items():
        on_ahead = (is_bid is False) if side > 0 else (is_bid is True)
        d = (p - ref) * side
        if on_ahead and 0 < d <= ZONE_TICKS:
            ahead.append((d, s))
    ahead.sort()
    sizes = np.array([s for _, s in ahead], dtype=float)
    if len(sizes):
        order = np.argsort(-sizes)
        m["dist_to_max_ticks"] = float(ahead[order[0]][0])
        m["dist_to_top_ticks"] = float(ahead[-1][0])
        m["top3_conc"] = float(np.sort(sizes)[::-1][:3].sum() / sizes.sum())
    else:
        m.update(dist_to_max_ticks=float("nan"), dist_to_top_ticks=float("nan"),
                 top3_conc=float("nan"))
    return m


def pull_estimate(book_pre: dict, book_post: dict, trades: pa.Table, side: int,
                  ref_ticks: float, t0: int, t1: int) -> float:
    ref = int(round(ref_ticks))
    ts, px, sz, buy = _arrays(trades)
    lo, hi = np.searchsorted(ts, t0), np.searchsorted(ts, t1)
    px, sz, buy = px[lo:hi], sz[lo:hi], buy[lo:hi]
    aggr = buy if side > 0 else ~buy
    stack = traded_total = unaccounted = 0.0
    for d in range(1, ZONE_TICKS + 1):
        p = ref + side * d
        key = (False, p) if side > 0 else (True, p)
        start = float(book_pre.get(key, 0))
        if start <= 0:
            continue
        stack += start
        traded = float(sz[(px == p) & aggr].sum())
        traded_total += traded
        end = float(book_post.get(key, 0))
        unaccounted += max(0.0, start - traded - end)
    return (unaccounted / stack) if stack > 0 else float("nan")


def tape_speed_context(trades: pa.Table, ignition_ns: int, confirm_ns: int) -> dict:
    """Tape speed before ignition + burst breadth during [ignition, confirm].

    User hypothesis (2026-10-27): continuation works when the tape is already
    fast (broad participation); a burst in a slow tape is often one large
    market order being absorbed -> reversal (pitch-and-catch fuel).
    All inputs are known at confirm_ns.
    """
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    o = np.argsort(ts, kind="stable")
    ts = ts[o]
    sz = trades.column("size").to_numpy().astype(np.float64)[o]
    ids = trades.column("aggressor_order_id").to_numpy(zero_copy_only=False)[o] \
        if "aggressor_order_id" in trades.column_names else None

    def rate(sec: float) -> tuple[float, float]:
        lo = int(np.searchsorted(ts, ignition_ns - int(sec * 1e9)))
        hi = int(np.searchsorted(ts, ignition_ns))
        return (hi - lo) / sec, (sz[lo:hi].sum() / sec if hi > lo else 0.0)

    tps10, _ = rate(10); tps30, vps30 = rate(30); tps60, _ = rate(60)
    # baseline: non-overlapping 30 s blocks over the prior 10 min
    blocks = []
    for k in range(1, 21):
        b_hi = ignition_ns - (k - 1) * 30_000_000_000
        b_lo = b_hi - 30_000_000_000
        if b_lo < ts[0]:
            break
        blocks.append((np.searchsorted(ts, b_hi) - np.searchsorted(ts, b_lo)) / 30.0)
    base = float(np.median(blocks)) if blocks else float("nan")
    out = dict(tps_10s=tps10, tps_30s=tps30, tps_60s=tps60, vps_30s=vps30,
               base_tps_30s=base,
               rel_speed_30s=(tps30 / base if base and np.isfinite(base) and base > 0 else float("nan")))
    lo = int(np.searchsorted(ts, ignition_ns)); hi = int(np.searchsorted(ts, confirm_ns))
    out["burst_n_trades"] = int(hi - lo)
    if hi > lo:
        out["burst_max_print_share"] = float(sz[lo:hi].max() / sz[lo:hi].sum())
        if ids is not None:
            vol_by_order: dict = {}
            for oid, s in zip(ids[lo:hi], sz[lo:hi]):
                vol_by_order[oid] = vol_by_order.get(oid, 0.0) + float(s)
            out["burst_unique_aggr"] = int(len(vol_by_order))
            out["burst_max_order_share"] = float(max(vol_by_order.values()) / sz[lo:hi].sum())
    return out


def clearance_context(
    trades: pa.Table,
    *,
    side: int,
    ignition_ns: int,
    confirm_ns: int,
    pre_price_pts: float,
    dist_max_ticks: float,
) -> dict:
    """Trade-past / pullback / leave-behind relative to the dominant resting level.

    Dominant level = ignition price + side * dist_max_ticks * 0.25 pts.
    User hyp: continuations clear past the size and leave it behind (passive
    trapped); reversals lose momentum and pull back into/through it.

    Windows are stamped at confirm and at +2s/+5s after confirm so we can
    separate causal-at-confirm filters from slightly-later clearance confirms.
    """
    if not np.isfinite(dist_max_ticks) or dist_max_ticks <= 0:
        return {}
    max_lvl = pre_price_pts + side * dist_max_ticks * 0.25
    ts, px, _, _ = _arrays(trades)
    px = px * 0.25
    if len(ts) == 0:
        return {}

    def slice_path(t0: int, t1: int):
        lo = int(np.searchsorted(ts, t0)); hi = int(np.searchsorted(ts, t1, side="right"))
        return ts[lo:hi], px[lo:hi]

    def signed(p):
        return side * (p - max_lvl)

    out: dict = {"max_level_pts": float(max_lvl), "dist_max_pts": float(dist_max_ticks * 0.25)}
    for label, t1 in (("confirm", confirm_ns),
                      ("c2s", confirm_ns + 2_000_000_000),
                      ("c5s", confirm_ns + 5_000_000_000)):
        t_path, p_path = slice_path(ignition_ns, t1)
        if len(p_path) == 0:
            continue
        past = signed(p_path)
        out[f"clear_{label}_pts"] = float(past[-1])
        out[f"peak_past_{label}_pts"] = float(past.max())
        # pullback from peak of the window (signed drawdown from best)
        peak_i = int(np.argmax(past))
        after = past[peak_i:]
        out[f"pullback_{label}_pts"] = float(past[peak_i] - after.min()) if len(after) else 0.0
        # left-behind: first print at least 1 pt past the level; then did price
        # ever come back to/through the level (past <= 0) before t1?
        crossed = np.where(past >= 1.0)[0]
        if len(crossed):
            i0 = int(crossed[0])
            returned = bool((past[i0:] <= 0).any())
            out[f"left_behind_{label}"] = 0 if returned else 1
            out[f"min_past_after_cross_{label}"] = float(past[i0:].min())
        else:
            out[f"left_behind_{label}"] = 0
            out[f"min_past_after_cross_{label}"] = float(past.min())
        out[f"cleared_{label}"] = 1 if past.max() >= 1.0 else 0
    return out


def level_hold_context(
    trades: pa.Table,
    *,
    side: int,
    ignition_ns: int,
    confirm_ns: int,
    pre_price_pts: float,
    dist_max_ticks: float,
    approach_pts: float = 1.5,
    clear_pts: float = 1.0,
    watch_sec: float = 30.0,
) -> dict:
    """After slicing the dominant level, does a pullback hold it or breach it?

    Sequence (causal once the hold/breach stamp fires):
      1. First print at least ``clear_pts`` past the max level.
      2. Watch until ``confirm_ns + watch_sec`` for a pullback that comes
         within ``approach_pts`` of the level (retest).
      3. HOLD  = retest occurs and price never goes through the level
         (signed past stays > 0) before the next thrust or window end.
      4. BREACH = price prints at/through the level (past <= 0) after having
         cleared it.
      5. NO_RETEST = cleared but never approached within approach_pts.

    Also reports stop distance from confirm price to the level (for SL
    placement the other side of the sliced size).
    """
    if not np.isfinite(dist_max_ticks) or dist_max_ticks <= 0:
        return {}
    max_lvl = pre_price_pts + side * dist_max_ticks * 0.25
    ts, px, _, _ = _arrays(trades)
    px = px * 0.25
    if len(ts) == 0:
        return {}
    t_end = confirm_ns + int(watch_sec * 1e9)
    lo = int(np.searchsorted(ts, ignition_ns))
    hi = int(np.searchsorted(ts, t_end, side="right"))
    ts_w, px_w = ts[lo:hi], px[lo:hi]
    if len(px_w) == 0:
        return {}
    past = side * (px_w - max_lvl)

    out: dict = {"max_level_pts": float(max_lvl)}
    # Confirm-price stop distance: how many pts beyond the level for a stop
    ci = int(np.searchsorted(ts, confirm_ns, side="right") - 1)
    if 0 <= ci < len(ts):
        conf_px = float(px[ci] if ci >= lo else px_w[0])
        # For long: stop below level; distance = confirm - level (positive if above)
        out["stop_dist_from_confirm_pts"] = float(side * (conf_px - max_lvl))
        out["confirm_px"] = conf_px

    crossed = np.where(past >= clear_pts)[0]
    if len(crossed) == 0:
        out["hold_state"] = "NEVER_CLEARED"
        return out
    i0 = int(crossed[0])
    out["clear_ts_ns"] = int(ts_w[i0])
    out["clear_px"] = float(px_w[i0])

    # After clear: must first get FURTHER past the level (peak), then either
    # pull back into the approach band (HOLD) or through the level (BREACH).
    # Without the peak requirement, the clear print itself can sit in the
    # approach band and falsely fire HOLD at time 0.
    after = past[i0:]
    after_ts = ts_w[i0:]
    peak_needed = approach_pts + 0.5  # must have been at least this far past
    peak_idx = np.where(after >= peak_needed)[0]
    if len(peak_idx) == 0:
        # Cleared by clear_pts but never stretched far enough for a retest read
        breach_early = np.where(after <= 0)[0]
        if len(breach_early):
            bi = int(breach_early[0])
            out["hold_state"] = "BREACH"
            out["hold_ts_ns"] = int(after_ts[bi])
            out["hold_px"] = float(px_w[i0 + bi])
            out["time_clear_to_event_ms"] = float((after_ts[bi] - ts_w[i0]) / 1e6)
            out["breach_depth_pts"] = float(-after[bi:].min())
        else:
            out["hold_state"] = "NO_RETEST"
            out["hold_min_past_pts"] = float(after.min())
            out["time_clear_to_event_ms"] = float((after_ts[-1] - ts_w[i0]) / 1e6)
    else:
        ip = int(peak_idx[0])
        out["peak_past_pts"] = float(after[ip: ].max())  # peak in window after clear
        post_peak = after[ip:]
        post_ts = after_ts[ip:]
        breach_idx = np.where(post_peak <= 0)[0]
        approach_idx = np.where((post_peak > 0) & (post_peak <= approach_pts))[0]
        # Whichever comes first after the peak
        bi = int(breach_idx[0]) if len(breach_idx) else None
        ai = int(approach_idx[0]) if len(approach_idx) else None
        if bi is not None and (ai is None or bi <= ai):
            out["hold_state"] = "BREACH"
            out["hold_ts_ns"] = int(post_ts[bi])
            out["hold_px"] = float(px_w[i0 + ip + bi])
            out["time_clear_to_event_ms"] = float((post_ts[bi] - ts_w[i0]) / 1e6)
            out["breach_depth_pts"] = float(-post_peak[bi:].min())
        elif ai is not None:
            out["hold_state"] = "HOLD"
            out["hold_ts_ns"] = int(post_ts[ai])
            out["hold_px"] = float(px_w[i0 + ip + ai])
            out["time_clear_to_event_ms"] = float((post_ts[ai] - ts_w[i0]) / 1e6)
            out["hold_min_past_pts"] = float(post_peak[ai:].min())
            out["hold_bounce_pts"] = float(post_peak[ai:].max() - post_peak[ai])
        else:
            out["hold_state"] = "NO_RETEST"
            out["hold_min_past_pts"] = float(post_peak.min())
            out["time_clear_to_event_ms"] = float((post_ts[-1] - ts_w[i0]) / 1e6)

    # Path MAE toward the level after confirm (would a stop beyond the level have been hit?)
    c_lo = int(np.searchsorted(ts_w, confirm_ns))
    if c_lo < len(px_w):
        past_after_conf = past[c_lo:]
        out["min_past_after_confirm"] = float(past_after_conf.min())
        # Stop just beyond level (past=0 line): hit if min_past <= 0
        out["stop_at_level_hit"] = 1 if past_after_conf.min() <= 0 else 0
        # Stop 1 pt beyond level (past <= -1)
        out["stop_1pt_beyond_hit"] = 1 if past_after_conf.min() <= -1.0 else 0
    return out


def base_speed_context(trades: pa.Table, ignition_ns: int) -> dict:
    """Tape speed over the BASE: [ignition-300s, ignition-30s], excluding the
    final 30 s so move onset does not contaminate the measurement.
    Relative baseline: 30 s block rates over [ignition-900s, ignition-300s].
    """
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    o = np.argsort(ts, kind="stable")
    ts = ts[o]
    sz = trades.column("size").to_numpy().astype(np.float64)[o]
    lo = int(np.searchsorted(ts, ignition_ns - 300_000_000_000))
    hi = int(np.searchsorted(ts, ignition_ns - 30_000_000_000))
    dur = 270.0
    base_tps = (hi - lo) / dur if hi > lo else 0.0
    base_vps = float(sz[lo:hi].sum() / dur) if hi > lo else 0.0
    blocks = []
    end = ignition_ns - 300_000_000_000
    for k in range(20):
        b_hi = end - k * 30_000_000_000
        b_lo = b_hi - 30_000_000_000
        if b_lo < ts[0]:
            break
        blocks.append((np.searchsorted(ts, b_hi) - np.searchsorted(ts, b_lo)) / 30.0)
    base_ref = float(np.median(blocks)) if blocks else float("nan")
    return dict(base_tps=base_tps, base_vps=base_vps, base_ref_tps=base_ref,
                base_rel_speed=(base_tps / base_ref if base_ref and base_ref > 0 else float("nan")),
                base_n_blocks=len(blocks))


def pre_ignition_trend(
    trades: pa.Table,
    *,
    side: int,
    ignition_ns: int,
    window_start_ns: int,
) -> dict:
    """Causal with-trend context ending 30 s before ignition (excludes the burst).

    ``trend_5m_pts`` = side * (price@ign-30s − price@ign-5m).
    ``trend_vwap_side`` = 1 if price@ign-30s is on the trade side of session VWAP.
    """
    ts, px, sz, _ = _arrays(trades)
    px = px * 0.25
    t_end = ignition_ns - 30_000_000_000
    t_start = ignition_ns - 300_000_000_000
    i_end = int(np.searchsorted(ts, t_end, side="right")) - 1
    i_start = int(np.searchsorted(ts, t_start, side="right"))
    if i_end < 0 or i_start >= len(px) or i_end < i_start:
        return {}
    p0, p1 = float(px[i_start]), float(px[i_end])
    j0 = int(np.searchsorted(ts, window_start_ns, side="left"))
    vwap = float((px[j0:i_end + 1] * sz[j0:i_end + 1]).sum() / sz[j0:i_end + 1].sum()) \
        if i_end >= j0 and sz[j0:i_end + 1].sum() > 0 else float("nan")
    return dict(
        trend_5m_pts=float(side * (p1 - p0)),
        trend_vwap_side=int(1 if np.isfinite(vwap) and side * (p1 - vwap) >= 0 else 0),
        trend_end_px=p1,
        trend_vwap=vwap if np.isfinite(vwap) else float("nan"),
    )


def pre_move_context(nq: pa.Table, t_ns: int, side: int, window_start_ns: int) -> dict:
    """Range/base/exhaustion/VWAP context from NQ trades at/before t_ns."""
    ts, px, sz, _ = _arrays(nq)  # px in ticks
    px = px * 0.25
    i = int(np.searchsorted(ts, t_ns, side="right"))
    if i == 0:
        return {}
    now_px = px[i - 1]

    def window(sec: float):
        j = int(np.searchsorted(ts, t_ns - int(sec * 1e9), side="right"))
        return px[j:i], ts[j:i]

    p5, _ = window(300); p1, _ = window(60)
    first5 = p5[0] if len(p5) else now_px
    # minute ranges over prior 30 min (median = vol scale)
    mins: dict[int, list] = {}
    j30 = int(np.searchsorted(ts, t_ns - 1800 * 1_000_000_000, side="right"))
    for t_, p_ in zip(ts[j30:i], px[j30:i]):
        mins.setdefault(int(t_ // 60_000_000_000), []).append(p_)
    ranges = [max(v) - min(v) for v in mins.values() if len(v) > 1]
    med_min = float(np.median(ranges)) if ranges else float("nan")
    # session VWAP from window start
    j0 = int(np.searchsorted(ts, window_start_ns, side="left"))
    vwap = float((px[j0:i] * sz[j0:i]).sum() / sz[j0:i].sum()) if i > j0 and sz[j0:i].sum() else float("nan")
    return dict(
        range_5m_pts=float(p5.max() - p5.min()) if len(p5) else float("nan"),
        range_1m_pts=float(p1.max() - p1.min()) if len(p1) else float("nan"),
        compression_1m_5m=float((p1.max() - p1.min()) / (p5.max() - p5.min()))
        if len(p1) and len(p5) and p5.max() > p5.min() else float("nan"),
        prior_run_5m_pts=float(side * (now_px - first5)),
        med_min_range_pts=med_min,
        dist_vwap_pts=float(side * (now_px - vwap)) if np.isfinite(vwap) else float("nan"),
    )
