"""Oriented orderflow scores for IRT hold vs break (EXP-0043 / H34).

Primary scores use only [-2s, +2s] around t_touch. Post [+2s, +10s] is diagnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa

from research.features.liquidity import LargeLevelConfig, LargeLevelTracker, TrackedLevel
from research.features.momentum import trades_to_tape_bars
from research.irt_lvn.events import PTS_PER_TICK


@dataclass
class ScoreConfig:
    large_size: int = 50
    tick_radius: int = 8
    far_band_ticks: int = 20
    z_lookback_ms: int = 1_800_000  # 30 min
    primary_pre_ms: int = 2_000
    primary_post_ms: int = 2_000
    diagnostic_post_ms: int = 10_000
    attract_pre_ms: int = 5_000
    burst_min_trades: int = 6
    burst_min_vol: float = 25.0
    clip_z: float = 2.0


def _clip(x: float, lo: float = -2.0, hi: float = 2.0) -> float:
    return float(max(lo, min(hi, x)))


def _safe_z(x: float, mean: float, std: float) -> float:
    if std < 1e-12 or not np.isfinite(std):
        return 0.0
    return float((x - mean) / std)


def _window_stats(
    ts: np.ndarray,
    values: np.ndarray,
    t0: int,
    t1: int,
) -> tuple[float, float, int]:
    """Sum, mean of values in [t0, t1], and count."""
    lo = int(np.searchsorted(ts, t0, side="left"))
    hi = int(np.searchsorted(ts, t1, side="right"))
    if hi <= lo:
        return 0.0, 0.0, 0
    seg = values[lo:hi]
    return float(seg.sum()), float(seg.mean()), int(hi - lo)


def _rolling_baseline(
    ts: np.ndarray,
    series: np.ndarray,
    t: int,
    lookback_ns: int,
) -> tuple[float, float]:
    """Mean/std of ``series`` in [t-lookback, t)."""
    lo = int(np.searchsorted(ts, t - lookback_ns, side="left"))
    hi = int(np.searchsorted(ts, t, side="left"))
    if hi - lo < 2:
        return 0.0, 1.0
    seg = series[lo:hi]
    return float(seg.mean()), float(max(seg.std(), 1e-9))


def _beyond_hold_sides(approach: int) -> tuple[str, str, bool, bool]:
    """Return (beyond_book, hold_book, beyond_is_buy_aggr, hold_is_buy_aggr).

    approach +1 from below → beyond=ASK / buy; hold=BID / sell
    approach -1 from above → beyond=BID / sell; hold=ASK / buy
    """
    if approach > 0:
        return "ASK", "BID", True, False
    return "BID", "ASK", False, True


def _band_ticks(level_pts: float, approach: int, near: int, far: int) -> tuple[int, int, int, int]:
    """Beyond-side tick bands: (near_lo, near_hi, far_lo, far_hi) inclusive."""
    lvl = int(round(level_pts / PTS_PER_TICK))
    if approach > 0:  # beyond = above
        return lvl, lvl + near, lvl + near + 1, lvl + far
    # beyond = below
    return lvl - near, lvl, lvl - far, lvl - near - 1


def _book_band_stats(book: dict[int, int], lo: int, hi: int, large: int) -> dict[str, float]:
    if lo > hi:
        lo, hi = hi, lo
    sizes = [sz for px, sz in book.items() if lo <= px <= hi and sz > 0]
    if not sizes:
        return {"sum": 0.0, "max": 0.0, "n_large": 0.0, "n_large100": 0.0}
    return {
        "sum": float(sum(sizes)),
        "max": float(max(sizes)),
        "n_large": float(sum(1 for s in sizes if s >= large)),
        "n_large100": float(sum(1 for s in sizes if s >= 100)),
    }


@dataclass
class _SessionOF:
    """Precomputed session arrays for scoring many events."""
    t_ts: np.ndarray
    t_px: np.ndarray  # ticks
    t_sz: np.ndarray
    t_buy: np.ndarray
    tape_ts: np.ndarray
    tape_n: np.ndarray
    tape_vol: np.ndarray
    tape_imb: np.ndarray
    # depth stream
    d_ts: np.ndarray
    d_bid: np.ndarray
    d_px: np.ndarray
    d_sz: np.ndarray
    # mbo
    m_ts: np.ndarray
    m_op: np.ndarray
    m_bid: np.ndarray
    m_px: np.ndarray
    m_sz: np.ndarray
    cfg: ScoreConfig
    closed_levels: list[TrackedLevel] = field(default_factory=list)


def prepare_session_of(
    trades: pa.Table,
    depth: pa.Table,
    mbo: pa.Table | None,
    *,
    cfg: ScoreConfig | None = None,
    bounds: tuple[int, int] | None = None,
) -> _SessionOF | None:
    """Filter feeds to bounds (plus z lookback) and run LargeLevelTracker once."""
    cfg = cfg or ScoreConfig()
    if trades.num_rows == 0 or depth.num_rows == 0:
        return None

    look_ns = int(cfg.z_lookback_ms * 1_000_000)
    if bounds is not None:
        lo, hi = bounds
        # extend for lookback + post diagnostic
        lo_ext = lo - look_ns
        hi_ext = hi + int(cfg.diagnostic_post_ms * 1_000_000)
    else:
        lo_ext, hi_ext = -10**18, 10**18

    def _slice_trades(t: pa.Table) -> tuple[np.ndarray, ...]:
        ts = t.column("ts_ns").to_numpy().astype(np.int64)
        m = (ts >= lo_ext) & (ts <= hi_ext)
        idx = np.where(m)[0]
        if len(idx) == 0:
            return (
                np.array([], dtype=np.int64),
                np.array([], dtype=np.int64),
                np.array([], dtype=np.float64),
                np.array([], dtype=bool),
            )
        order = idx[np.argsort(ts[idx], kind="stable")]
        return (
            ts[order],
            t.column("price").to_numpy().astype(np.int64)[order],
            t.column("size").to_numpy().astype(np.float64)[order],
            t.column("is_buy_aggressor").to_numpy().astype(bool)[order],
        )

    t_ts, t_px, t_sz, t_buy = _slice_trades(trades)
    if len(t_ts) == 0:
        return None

    d_ts = depth.column("ts_ns").to_numpy().astype(np.int64)
    dm = (d_ts >= lo_ext) & (d_ts <= hi_ext)
    d_idx = np.where(dm)[0]
    if len(d_idx) == 0:
        return None
    d_order = d_idx[np.argsort(d_ts[d_idx], kind="stable")]
    d_ts = d_ts[d_order]
    d_bid = depth.column("is_bid").to_numpy().astype(bool)[d_order]
    d_px = depth.column("price_ticks").to_numpy().astype(np.int64)[d_order]
    d_sz = depth.column("size").to_numpy().astype(np.int64)[d_order]

    # MBO optional
    if mbo is not None and mbo.num_rows > 0:
        m_ts0 = mbo.column("ts_ns").to_numpy().astype(np.int64)
        mm = (m_ts0 >= lo_ext) & (m_ts0 <= hi_ext)
        m_idx = np.where(mm)[0]
        if len(m_idx):
            m_order = m_idx[np.argsort(m_ts0[m_idx], kind="stable")]
            m_ts = m_ts0[m_order]
            m_op = mbo.column("op").to_numpy(zero_copy_only=False)[m_order]
            m_bid = mbo.column("is_bid").to_numpy().astype(bool)[m_order]
            m_px = mbo.column("price_ticks").to_numpy().astype(np.int64)[m_order]
            m_sz = mbo.column("size").to_numpy().astype(np.float64)[m_order]
        else:
            m_ts = np.array([], dtype=np.int64)
            m_op = m_bid = m_px = m_sz = np.array([])
    else:
        m_ts = np.array([], dtype=np.int64)
        m_op = m_bid = m_px = m_sz = np.array([])

    # Tape bars on full sliced trades
    tr_tbl = pa.table({
        "ts_ns": t_ts,
        "price": t_px.astype(np.float64),
        "size": t_sz,
        "is_buy_aggressor": t_buy,
    })
    tape = trades_to_tape_bars(tr_tbl, bar_ms=100)
    if tape.num_rows:
        tape_ts = tape.column("ts_ns").to_numpy().astype(np.int64)
        tape_n = tape.column("n_trades").to_numpy().astype(np.float64)
        tape_vol = tape.column("total_vol").to_numpy().astype(np.float64)
        tape_imb = tape.column("imbalance").to_numpy().astype(np.float64)
    else:
        tape_ts = np.array([], dtype=np.int64)
        tape_n = tape_vol = tape_imb = np.array([], dtype=np.float64)

    # Run tracker (absolute-only large levels)
    ll_cfg = LargeLevelConfig(
        absolute_size=cfg.large_size,
        z_score=99.0,
        tick_radius=cfg.tick_radius,
    )
    tracker = LargeLevelTracker(ll_cfg)
    closed: list[TrackedLevel] = []
    i = j = 0
    n_d, n_t = len(d_ts), len(t_ts)
    while i < n_d or j < n_t:
        take_depth = j >= n_t or (i < n_d and d_ts[i] <= t_ts[j])
        if take_depth:
            tracker.on_timestamp(int(d_ts[i]))
            tracker.on_depth(bool(d_bid[i]), int(d_px[i]), int(d_sz[i]))
            i += 1
        else:
            tracker.on_timestamp(int(t_ts[j]))
            tracker.on_trade(int(t_px[j]), int(t_sz[j]), bool(t_buy[j]))
            j += 1
        closed.extend(tracker.drain_closed())
    # leftover actives
    for lvl in tracker.active.values():
        if not lvl.closed:
            tracker._close_level(lvl)
    closed.extend(tracker.drain_closed())

    return _SessionOF(
        t_ts=t_ts, t_px=t_px, t_sz=t_sz, t_buy=t_buy,
        tape_ts=tape_ts, tape_n=tape_n, tape_vol=tape_vol, tape_imb=tape_imb,
        d_ts=d_ts, d_bid=d_bid, d_px=d_px, d_sz=d_sz,
        m_ts=m_ts, m_op=m_op, m_bid=m_bid, m_px=m_px, m_sz=m_sz,
        cfg=cfg, closed_levels=closed,
    )


def _snapshot_books_at_times(
    sof: _SessionOF, times: list[int]
) -> dict[int, tuple[dict[int, int], dict[int, int]]]:
    """One-pass depth replay; snapshot (bids, asks) at each timestamp in ``times``."""
    if not times:
        return {}
    uniq = sorted(set(int(t) for t in times))
    out: dict[int, tuple[dict[int, int], dict[int, int]]] = {}
    bids: dict[int, int] = {}
    asks: dict[int, int] = {}
    i = 0
    n = len(sof.d_ts)
    for t_target in uniq:
        while i < n and int(sof.d_ts[i]) <= t_target:
            book = bids if sof.d_bid[i] else asks
            px, sz = int(sof.d_px[i]), int(sof.d_sz[i])
            if sz <= 0:
                book.pop(px, None)
            else:
                book[px] = sz
            i += 1
        out[t_target] = (dict(bids), dict(asks))
    return out


def _mbo_net_add(
    sof: _SessionOF,
    t0: int,
    t1: int,
    *,
    want_bid: bool,
    px_lo: int,
    px_hi: int,
) -> float:
    """Net size sent − cancelled in window on beyond side within band."""
    if len(sof.m_ts) == 0:
        return 0.0
    if px_lo > px_hi:
        px_lo, px_hi = px_hi, px_lo
    lo = int(np.searchsorted(sof.m_ts, t0, side="left"))
    hi = int(np.searchsorted(sof.m_ts, t1, side="right"))
    net = 0.0
    for i in range(lo, hi):
        if bool(sof.m_bid[i]) != want_bid:
            continue
        px = int(sof.m_px[i])
        if px < px_lo or px > px_hi:
            continue
        op = str(sof.m_op[i]).lower()
        sz = float(sof.m_sz[i])
        if op in ("send", "a", "add", "new", "0"):
            net += sz
        elif op in ("cancel", "c", "delete", "1"):
            net -= sz
        # replace ignored for net-add purity
    return net


def score_event(
    sof: _SessionOF,
    *,
    t_touch: int,
    level_px: float,
    approach: int,
    book_at: dict[int, tuple[dict[int, int], dict[int, int]]] | None = None,
) -> dict[str, Any]:
    """Compute four primary scores + raw features for one event."""
    cfg = sof.cfg
    clip = cfg.clip_z
    look_ns = int(cfg.z_lookback_ms * 1_000_000)
    pre_ns = int(cfg.primary_pre_ms * 1_000_000)
    at_ns = int(cfg.primary_post_ms * 1_000_000)
    post_ns = int(cfg.diagnostic_post_ms * 1_000_000)
    attr_pre = int(cfg.attract_pre_ms * 1_000_000)

    beyond_book, hold_book, beyond_buy, hold_buy = _beyond_hold_sides(approach)
    near_lo, near_hi, far_lo, far_hi = _band_ticks(
        level_px, approach, cfg.tick_radius, cfg.far_band_ticks
    )
    want_bid_beyond = beyond_book == "BID"
    lvl_tick = int(round(level_px / PTS_PER_TICK))

    # --- Tape / aggressor volumes in primary window [-2s, +2s] ---
    w0, w1 = t_touch - pre_ns, t_touch + at_ns
    lo = int(np.searchsorted(sof.t_ts, w0, side="left"))
    hi = int(np.searchsorted(sof.t_ts, w1, side="right"))
    buy_vol = sell_vol = 0.0
    n_prints = 0
    if hi > lo:
        buy_m = sof.t_buy[lo:hi]
        sz = sof.t_sz[lo:hi]
        buy_vol = float(sz[buy_m].sum()) if buy_m.any() else 0.0
        sell_vol = float(sz[~buy_m].sum()) if (~buy_m).any() else 0.0
        n_prints = int(hi - lo)
    total_vol = buy_vol + sell_vol
    beyond_aggr = buy_vol if beyond_buy else sell_vol
    hold_aggr = buy_vol if hold_buy else sell_vol

    tlo = int(np.searchsorted(sof.tape_ts, w0, side="left"))
    thi = int(np.searchsorted(sof.tape_ts, w1, side="right"))
    win_prints = float(sof.tape_n[tlo:thi].sum()) if thi > tlo else float(n_prints)
    win_vol = float(sof.tape_vol[tlo:thi].sum()) if thi > tlo else total_vol
    win_imb = float(np.mean(np.abs(sof.tape_imb[tlo:thi]))) if thi > tlo else 0.0
    burst_n = 0
    if thi > tlo:
        for k in range(tlo, thi):
            if sof.tape_n[k] >= cfg.burst_min_trades and sof.tape_vol[k] >= cfg.burst_min_vol:
                burst_n += 1

    b_lo = int(np.searchsorted(sof.tape_ts, t_touch - look_ns, side="left"))
    b_hi = int(np.searchsorted(sof.tape_ts, t_touch, side="left"))
    if b_hi - b_lo >= 5:
        n_bars_win = max(1, thi - tlo)
        bar_n_mean = float(sof.tape_n[b_lo:b_hi].mean())
        bar_n_std = float(max(sof.tape_n[b_lo:b_hi].std(), 1e-9))
        bar_v_mean = float(sof.tape_vol[b_lo:b_hi].mean())
        bar_v_std = float(max(sof.tape_vol[b_lo:b_hi].std(), 1e-9))
        print_z = _safe_z(win_prints / n_bars_win, bar_n_mean, bar_n_std)
        vol_z = _safe_z(win_vol / n_bars_win, bar_v_mean, bar_v_std)
        base_burst = np.array([
            1.0 if (sof.tape_n[k] >= cfg.burst_min_trades and sof.tape_vol[k] >= cfg.burst_min_vol) else 0.0
            for k in range(b_lo, b_hi)
        ])
        exp_burst = float(base_burst.mean() * n_bars_win)
        burst_std = float(max(np.sqrt(max(exp_burst, 0.25)), 0.5))
        burst_z = (burst_n - exp_burst) / burst_std
    else:
        print_z = vol_z = burst_z = 0.0

    win_len = w1 - w0
    aggr_beyond_samples: list[float] = []
    aggr_hold_samples: list[float] = []
    t_cursor = t_touch - look_ns
    while t_cursor + win_len <= t_touch:
        a, b = t_cursor, t_cursor + win_len
        i0 = int(np.searchsorted(sof.t_ts, a, side="left"))
        i1 = int(np.searchsorted(sof.t_ts, b, side="right"))
        if i1 > i0:
            bm = sof.t_buy[i0:i1]
            s = sof.t_sz[i0:i1]
            bv = float(s[bm].sum()) if bm.any() else 0.0
            sv = float(s[~bm].sum()) if (~bm).any() else 0.0
            aggr_beyond_samples.append(bv if beyond_buy else sv)
            aggr_hold_samples.append(bv if hold_buy else sv)
        t_cursor += win_len
    if len(aggr_beyond_samples) >= 3:
        beyond_vol_z = _safe_z(
            beyond_aggr, float(np.mean(aggr_beyond_samples)),
            float(max(np.std(aggr_beyond_samples), 1e-9)),
        )
        hold_vol_z = _safe_z(
            hold_aggr, float(np.mean(aggr_hold_samples)),
            float(max(np.std(aggr_hold_samples), 1e-9)),
        )
    else:
        beyond_vol_z = hold_vol_z = 0.0
    beyond_minus_hold_z = beyond_vol_z - hold_vol_z

    # --- Stack at t_touch (books precomputed when possible) ---
    if book_at is not None and t_touch in book_at:
        bids, asks = book_at[t_touch]
    else:
        snap = _snapshot_books_at_times(sof, [t_touch])
        bids, asks = snap[t_touch]
    beyond_book_dict = asks if beyond_book == "ASK" else bids
    near_stats = _book_band_stats(beyond_book_dict, near_lo, near_hi, cfg.large_size)
    far_stats = _book_band_stats(beyond_book_dict, far_lo, far_hi, cfg.large_size)

    # Baseline near-sum from precomputed grid snaps in prior 30m
    stack_samples: list[float] = []
    large_samples: list[float] = []
    if book_at is not None:
        for t_s, (bb, aa) in book_at.items():
            if t_s < t_touch - look_ns or t_s >= t_touch:
                continue
            bd = aa if beyond_book == "ASK" else bb
            st = _book_band_stats(bd, near_lo, near_hi, cfg.large_size)
            stack_samples.append(st["sum"])
            large_samples.append(st["n_large"])
    if len(stack_samples) >= 3:
        near_sum_z = _safe_z(
            near_stats["sum"], float(np.mean(stack_samples)),
            float(max(np.std(stack_samples), 1e-9)),
        )
        large_count_z = _safe_z(
            near_stats["n_large"], float(np.mean(large_samples)),
            float(max(np.std(large_samples), 1e-9)),
        )
    else:
        near_sum_z = large_count_z = 0.0

    band_lo = min(near_lo, far_lo, near_hi, far_hi)
    band_hi = max(near_lo, far_lo, near_hi, far_hi)
    net_add = _mbo_net_add(
        sof, t_touch - pre_ns, t_touch,
        want_bid=want_bid_beyond, px_lo=band_lo, px_hi=band_hi,
    )
    net_samples: list[float] = []
    t_cursor = t_touch - look_ns
    while t_cursor + pre_ns <= t_touch and len(net_samples) < 40:
        net_samples.append(_mbo_net_add(
            sof, t_cursor, t_cursor + pre_ns,
            want_bid=want_bid_beyond, px_lo=band_lo, px_hi=band_hi,
        ))
        t_cursor += pre_ns
    if len(net_samples) >= 3:
        net_add_z = _safe_z(
            net_add, float(np.mean(net_samples)),
            float(max(np.std(net_samples), 1e-9)),
        )
    else:
        net_add_z = 0.0

    stack0 = near_stats["sum"] + far_stats["sum"]
    consumed = 0.0
    i0 = int(np.searchsorted(sof.t_ts, t_touch, side="left"))
    i1 = int(np.searchsorted(sof.t_ts, t_touch + at_ns, side="right"))
    for i in range(i0, i1):
        if bool(sof.t_buy[i]) != beyond_buy:
            continue
        px = int(sof.t_px[i])
        if band_lo <= px <= band_hi:
            consumed += float(sof.t_sz[i])
    consume_frac = (consumed / stack0) if stack0 > 0 else (1.0 if consumed > 0 else 0.0)
    if len(aggr_beyond_samples) >= 3:
        beyond_consume_z = _safe_z(
            consumed, float(np.mean(aggr_beyond_samples)),
            float(max(np.std(aggr_beyond_samples), 1e-9)),
        )
    else:
        beyond_consume_z = 0.0

    take_flag = 0.0
    absorb_flag = 0.0
    relevant: list[TrackedLevel] = []
    for lvl in sof.closed_levels:
        if lvl.side != beyond_book:
            continue
        fs = lvl.first_seen_ns
        if fs < t_touch - attr_pre or fs > t_touch + at_ns:
            continue
        if not (band_lo - cfg.tick_radius <= lvl.price <= band_hi + cfg.tick_radius):
            continue
        relevant.append(lvl)
        if lvl.max_penetration_ticks > max(1, cfg.tick_radius // 2):
            take_flag = 1.0
        if lvl.label == "ABSORB":
            absorb_flag = 1.0
    if take_flag < 0.5 and absorb_flag > 0.5:
        take_flag = -1.0
    elif take_flag > 0.5:
        take_flag = 1.0
    else:
        take_flag = 1.0 if (consume_frac >= 0.5 and beyond_aggr > hold_aggr) else (
            -1.0 if (stack0 > 0 and consume_frac < 0.2 and beyond_aggr < hold_aggr) else 0.0
        )

    ar = 0.0
    for lvl in sof.closed_levels:
        fs = lvl.first_seen_ns
        if fs < t_touch - attr_pre or fs > t_touch + at_ns:
            continue
        if abs(lvl.price - lvl_tick) > cfg.tick_radius:
            continue
        is_beyond = lvl.side == beyond_book
        is_hold = lvl.side == hold_book
        lab = lvl.label
        if lab in ("NONE", "DWELL_SUPPORT") and lvl.post_move_1s is not None:
            if lvl.side == "ASK" and lvl.post_move_1s > 0:
                lab = "ATTRACT"
            elif lvl.side == "BID" and lvl.post_move_1s < 0:
                lab = "ATTRACT"
            elif lvl.side == "ASK" and lvl.post_move_1s < 0:
                lab = "REPEL"
            elif lvl.side == "BID" and lvl.post_move_1s > 0:
                lab = "REPEL"
        if is_beyond:
            if lab == "ATTRACT" or (lvl.max_penetration_ticks > 0 and lab != "ABSORB"):
                ar += 1.0
            elif lab in ("REPEL", "ABSORB", "WITHDRAW"):
                ar -= 1.0
        elif is_hold:
            if lab == "ATTRACT":
                ar -= 1.0
            elif lab in ("REPEL", "WITHDRAW"):
                ar += 1.0

    take_score = _clip(
        0.50 * _clip(beyond_minus_hold_z, -clip, clip)
        + 0.30 * _clip(beyond_consume_z, -clip, clip)
        + 0.20 * take_flag,
        -clip, clip,
    )
    stack_score = _clip(
        0.40 * _clip(near_sum_z, -clip, clip)
        + 0.30 * _clip(large_count_z, -clip, clip)
        + 0.30 * _clip(net_add_z, -clip, clip),
        -clip, clip,
    )
    stack_consume = _clip(
        0.5 * _clip(near_sum_z, -clip, clip)
        + 0.5 * _clip((consume_frac - 0.5) * 4.0, -clip, clip),
        -clip, clip,
    )
    tape_score = _clip(
        0.40 * _clip(print_z, -clip, clip)
        + 0.40 * _clip(vol_z, -clip, clip)
        + 0.20 * _clip(burst_z, -clip, clip),
        -clip, clip,
    )
    attract_repel = _clip(ar, -clip, clip)
    break_likeness = _clip(
        0.35 * take_score
        + 0.25 * stack_consume
        + 0.20 * tape_score
        + 0.20 * attract_repel,
        -clip, clip,
    )

    p0, p1 = t_touch + at_ns, t_touch + post_ns
    plo = int(np.searchsorted(sof.t_ts, p0, side="left"))
    phi = int(np.searchsorted(sof.t_ts, p1, side="right"))
    post_vol = float(sof.t_sz[plo:phi].sum()) if phi > plo else 0.0
    post_prints = int(phi - plo)

    return {
        "TakeScore": take_score,
        "StackScore": stack_score,
        "StackConsume": stack_consume,
        "TapeScore": tape_score,
        "AttractRepelScore": attract_repel,
        "BreakLikeness": break_likeness,
        "beyond_aggressor_vol": beyond_aggr,
        "holdside_aggressor_vol": hold_aggr,
        "beyond_minus_hold_vol_z": beyond_minus_hold_z,
        "beyond_consume": consumed,
        "beyond_consume_z": beyond_consume_z,
        "consume_frac": consume_frac,
        "take_flag": take_flag,
        "near_sum": near_stats["sum"],
        "near_max": near_stats["max"],
        "near_n_large": near_stats["n_large"],
        "near_n_large100": near_stats["n_large100"],
        "far_sum": far_stats["sum"],
        "near_sum_z": near_sum_z,
        "large_count_z": large_count_z,
        "net_add": net_add,
        "net_add_z": net_add_z,
        "print_rate": win_prints / max(1e-6, (w1 - w0) / 1e9),
        "total_vol": win_vol,
        "print_z": print_z,
        "vol_z": vol_z,
        "abs_imbalance": win_imb,
        "burst_count": float(burst_n),
        "burst_z": burst_z,
        "post_vol": post_vol,
        "post_prints": float(post_prints),
        "n_tracker_levels": float(len(relevant)),
    }


def enrich_events_with_scores(
    events: pa.Table,
    sof: _SessionOF,
) -> pa.Table:
    """Append score columns for all events in a single session (sof)."""
    if events.num_rows == 0:
        return events
    cfg = sof.cfg
    look_ns = int(cfg.z_lookback_ms * 1_000_000)
    # Snapshot books at each event + 10s grid in lookback for stack z
    times: list[int] = []
    for i in range(events.num_rows):
        t = int(events.column("t_touch_ns")[i].as_py())
        times.append(t)
        t0 = t - look_ns
        step = 10_000_000_000  # 10s
        tt = t0
        while tt < t:
            times.append(tt)
            tt += step
    book_at = _snapshot_books_at_times(sof, times)

    rows = [
        score_event(
            sof,
            t_touch=int(events.column("t_touch_ns")[i].as_py()),
            level_px=float(events.column("level_px")[i].as_py()),
            approach=int(events.column("approach")[i].as_py()),
            book_at=book_at,
        )
        for i in range(events.num_rows)
    ]
    out = events
    for k in rows[0]:
        out = out.append_column(k, pa.array([r[k] for r in rows], type=pa.float64()))
    return out
