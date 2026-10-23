"""Oriented orderflow scores for IRT hold vs break (EXP-0043 / H34).

Design (post-review):
* Windows: ``pre`` = [-2s, 0), ``at`` = [0, +2s], ``both`` = [-2s, +2s].
  ``*_pre`` scores cannot see the reaction and are the leak-free primary;
  ``at`` / ``both`` are mechanism checks. ``[+2s, +10s]`` is diagnostic only.
* Baselines are **session-relative, RTH-clipped, robust**: window statistics
  are compared to sliding windows over ``[max(t-30min, rth_open), t)`` using
  median / MAD. Events with < ``baseline_min_ms`` of RTH baseline are flagged
  ``baseline_ok = 0`` and reported as a separate stratum.
* Tape is a dense 100 ms grid (empty slots count), so prints/sec is honest.
* Stack baseline uses bands **relative to the then-BBO** on the beyond side,
  not the event's fixed price band (which is empty whenever price is away).
* Large-size threshold is session-relative (p99.9 of non-zero depth sizes,
  floored), not an absolute 50.
* Depth replay starts from the book state at the window start (``init_books``),
  so resting orders placed earlier are visible.
* Tape z uses a short baseline (``tape_lookback_ms``, 5 min) because print
  intensity drifts through the session; aggressor imbalance keeps the 30 min
  baseline (common level cancels in beyond−hold).
* MBO is used for more than net-add: beyond-side **pull rate**
  (cancel / (add+cancel)), **replenish count** (sends) and **depletion**
  (stack at touch vs 2 s earlier) on the near band → ``MboScore_pre``.
* Longer pre-windows ``pre5`` [-5s,0) / ``pre10`` [-10s,0) and the approach
  leg ``leg`` [-60s,0) are scored as secondary leak-free windows.
* AttractRepel never fires at IRT prices (tracker tags near-BBO); it is
  reported but no longer part of any BreakLikeness composite.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa

from research.features.liquidity import LargeLevelConfig, LargeLevelTracker, TrackedLevel
from research.irt_lvn.events import PTS_PER_TICK

NS_PER_MS = 1_000_000


@dataclass
class ScoreConfig:
    large_size_pct: float = 99.9
    large_size_min: int = 15
    tick_radius: int = 8
    far_band_ticks: int = 20
    z_lookback_ms: int = 1_800_000  # 30 min
    tape_lookback_ms: int = 300_000  # 5 min — tape intensity is non-stationary
    baseline_min_ms: int = 300_000  # 5 min of RTH needed for baseline_ok
    baseline_step_ms: int = 1_000
    book_grid_ms: int = 10_000
    primary_pre_ms: int = 2_000
    primary_post_ms: int = 2_000
    long_pre_ms: tuple[int, int] = (5_000, 10_000)
    leg_ms: int = 60_000
    book_trend_ms: tuple[int, ...] = (10_000, 30_000, 60_000)   # book snapshots before touch
    consume_windows_ms: tuple[int, ...] = (1_000, 3_000, 5_000)  # StackConsume_{w}s variants
    diagnostic_post_ms: int = 10_000
    attract_pre_ms: int = 5_000
    burst_min_trades: int = 6
    burst_min_vol: float = 25.0
    clip_z: float = 2.0
    slot_ms: int = 100


# ------------------------------------------------------------------ helpers

def _clip(x: float, lo: float = -2.0, hi: float = 2.0) -> float:
    if not np.isfinite(x):
        return 0.0
    return float(max(lo, min(hi, x)))


def _robust_z(x: float, samples: np.ndarray, min_n: int = 5) -> float:
    """(x − median) / (1.4826·MAD); std fallback when MAD is 0."""
    s = np.asarray(samples, dtype=np.float64)
    s = s[np.isfinite(s)]
    if len(s) < min_n:
        return 0.0
    med = float(np.median(s))
    mad = float(np.median(np.abs(s - med))) * 1.4826
    if mad < 1e-9:
        sd = float(s.std())
        if sd < 1e-9:
            return 0.0
        return float((x - med) / sd)
    return float((x - med) / mad)


def _beyond_hold_sides(approach: int) -> tuple[str, str, bool, bool]:
    """(beyond_book, hold_book, beyond_is_buy_aggr, hold_is_buy_aggr)."""
    if approach > 0:  # from below → testing resistance
        return "ASK", "BID", True, False
    return "BID", "ASK", False, True


def _band_ticks(level_pts: float, approach: int, near: int, far: int) -> tuple[int, int, int, int]:
    """Beyond-side bands around the frozen level: (near_lo, near_hi, far_lo, far_hi)."""
    lvl = int(round(level_pts / PTS_PER_TICK))
    if approach > 0:
        return lvl, lvl + near, lvl + near + 1, lvl + far
    return lvl - near, lvl, lvl - far, lvl - near - 1


def _book_band_stats(book: dict[int, int], lo: int, hi: int, large: int) -> dict[str, float]:
    if lo > hi:
        lo, hi = hi, lo
    sizes = [sz for px, sz in book.items() if lo <= px <= hi and sz > 0]
    if not sizes:
        return {"sum": 0.0, "max": 0.0, "n_large": 0.0, "n_large2x": 0.0}
    return {
        "sum": float(sum(sizes)),
        "max": float(max(sizes)),
        "n_large": float(sum(1 for s in sizes if s >= large)),
        "n_large2x": float(sum(1 for s in sizes if s >= 2 * large)),
    }


def _bbo(bids: dict[int, int], asks: dict[int, int]) -> tuple[int | None, int | None]:
    bb = max(bids) if bids else None
    ba = min(asks) if asks else None
    return bb, ba


# ------------------------------------------------------------ session state

@dataclass
class _SessionOF:
    # trades (sorted)
    t_ts: np.ndarray
    t_px: np.ndarray  # ticks
    t_sz: np.ndarray
    t_buy: np.ndarray
    # dense 100ms grid (cumsums, length n_slots+1)
    slot0_ns: int
    slot_ns: int
    n_slots: int
    cs_n: np.ndarray
    cs_vol: np.ndarray
    cs_buy: np.ndarray
    cs_sell: np.ndarray
    cs_burst: np.ndarray
    # depth stream (sorted) + initial book at d_ts[0]
    d_ts: np.ndarray
    d_bid: np.ndarray
    d_px: np.ndarray
    d_sz: np.ndarray
    init_bids: dict[int, int]
    init_asks: dict[int, int]
    # mbo (sorted), flattened: m_op = +1 add / −1 cancel with resolved px/sz
    m_ts: np.ndarray
    m_op: np.ndarray
    m_bid: np.ndarray
    m_px: np.ndarray
    m_sz: np.ndarray
    cfg: ScoreConfig
    rth_open_ns: int
    large_thr: int
    closed_levels: list[TrackedLevel] = field(default_factory=list)
    mbo_stats: dict[str, int] = field(default_factory=dict)

    # --- grid helpers
    def slot(self, t_ns: int) -> int:
        k = (int(t_ns) - self.slot0_ns) // self.slot_ns
        return int(min(max(k, 0), self.n_slots))

    def win_sum(self, cs: np.ndarray, a_ns: int, b_ns: int) -> float:
        """Sum over slots covering [a, b)."""
        ia, ib = self.slot(a_ns), self.slot(b_ns)
        if ib <= ia:
            return 0.0
        return float(cs[ib] - cs[ia])

    def sliding_sums(self, cs: np.ndarray, b0_ns: int, b1_ns: int, win_ns: int, step_ns: int) -> np.ndarray:
        """Sliding window sums of length win over [b0, b1) with given step."""
        if b1_ns - win_ns <= b0_ns:
            return np.array([], dtype=np.float64)
        starts = np.arange(b0_ns, b1_ns - win_ns + 1, step_ns, dtype=np.int64)
        ia = np.clip((starts - self.slot0_ns) // self.slot_ns, 0, self.n_slots)
        ib = np.clip((starts + win_ns - self.slot0_ns) // self.slot_ns, 0, self.n_slots)
        return (cs[ib] - cs[ia]).astype(np.float64)

    def baseline_bounds(self, t_ns: int, lookback_ms: int | None = None) -> tuple[int, int, bool]:
        look = int((self.cfg.z_lookback_ms if lookback_ms is None else lookback_ms) * NS_PER_MS)
        b0 = max(int(t_ns) - look, int(self.rth_open_ns))
        b1 = int(t_ns)
        ok = (b1 - b0) >= int(self.cfg.baseline_min_ms * NS_PER_MS)
        return b0, b1, ok


def prepare_session_of(
    trades: pa.Table,
    depth: pa.Table,
    mbo: pa.Table | None,
    *,
    cfg: ScoreConfig | None = None,
    bounds: tuple[int, int],
    init_books: tuple[dict[int, int], dict[int, int]] | None = None,
) -> _SessionOF | None:
    """Slice feeds to bounds ± lookback, build dense tape, run LargeLevelTracker once.

    ``bounds`` = (rth_open_ns, rth_close_ns). ``init_books`` = book state just
    before the first depth row in the slice (see runner loader).
    """
    cfg = cfg or ScoreConfig()
    if trades.num_rows == 0 or depth.num_rows == 0:
        return None
    lo, hi = int(bounds[0]), int(bounds[1])
    look_ns = int(cfg.z_lookback_ms * NS_PER_MS)
    lo_ext = lo - look_ns
    hi_ext = hi + int(cfg.diagnostic_post_ms * NS_PER_MS)

    # --- trades
    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    m = (ts >= lo_ext) & (ts <= hi_ext)
    idx = np.where(m)[0]
    if len(idx) == 0:
        return None
    order = idx[np.argsort(ts[idx], kind="stable")]
    t_ts = ts[order]
    t_px = trades.column("price").to_numpy().astype(np.int64)[order]
    t_sz = trades.column("size").to_numpy().astype(np.float64)[order]
    t_buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)[order]

    # --- dense tape grid
    slot_ns = int(cfg.slot_ms * NS_PER_MS)
    slot0 = (lo_ext // slot_ns) * slot_ns
    n_slots = int((hi_ext - slot0) // slot_ns) + 1
    k = np.clip((t_ts - slot0) // slot_ns, 0, n_slots - 1).astype(np.int64)
    n_per = np.bincount(k, minlength=n_slots).astype(np.float64)
    v_per = np.bincount(k, weights=t_sz, minlength=n_slots)
    b_per = np.bincount(k[t_buy], weights=t_sz[t_buy], minlength=n_slots)
    s_per = np.bincount(k[~t_buy], weights=t_sz[~t_buy], minlength=n_slots)
    burst = ((n_per >= cfg.burst_min_trades) & (v_per >= cfg.burst_min_vol)).astype(np.float64)

    def _cs(a: np.ndarray) -> np.ndarray:
        out = np.zeros(len(a) + 1, dtype=np.float64)
        np.cumsum(a, out=out[1:])
        return out

    # --- depth
    d_ts0 = depth.column("ts_ns").to_numpy().astype(np.int64)
    d_bid_all = depth.column("is_bid").to_numpy().astype(bool)
    d_px_all = depth.column("price_ticks").to_numpy().astype(np.int64)
    d_sz_all = depth.column("size").to_numpy().astype(np.int64)
    init_bids, init_asks = ({}, {}) if init_books is None else (dict(init_books[0]), dict(init_books[1]))

    # If the caller seeded ``init_books`` earlier than ``lo_ext`` and passed a
    # wider depth slice (e.g. open-book age_cap 2h vs z_lookback 30m), fold any
    # pre-lo_ext rows into init before slicing. Otherwise stale size survives
    # and the book at snap can be badly crossed (EXP-0045/0046 / export bug).
    pre = np.where(d_ts0 < lo_ext)[0]
    if len(pre):
        pre = pre[np.argsort(d_ts0[pre], kind="stable")]
        for i in pre:
            book = init_bids if bool(d_bid_all[i]) else init_asks
            px, sz = int(d_px_all[i]), int(d_sz_all[i])
            if sz <= 0:
                book.pop(px, None)
            else:
                book[px] = sz

    dm = (d_ts0 >= lo_ext) & (d_ts0 <= hi_ext)
    d_idx = np.where(dm)[0]
    if len(d_idx) == 0:
        return None
    d_order = d_idx[np.argsort(d_ts0[d_idx], kind="stable")]
    d_ts = d_ts0[d_order]
    d_bid = d_bid_all[d_order]
    d_px = d_px_all[d_order]
    d_sz = d_sz_all[d_order]

    nz = d_sz[d_sz > 0]
    large_thr = int(max(cfg.large_size_min, np.percentile(nz, cfg.large_size_pct))) if len(nz) else cfg.large_size_min

    # --- mbo: resolve cancels / replaces against the order map (cancel rows carry only order_id)
    m_ts, m_op, m_bid, m_px, m_sz, mbo_stats = _resolve_mbo(mbo, lo_ext, hi_ext)

    # --- tracker (session-relative absolute threshold; z disabled)
    tracker = LargeLevelTracker(LargeLevelConfig(
        absolute_size=large_thr, z_score=99.0, tick_radius=cfg.tick_radius,
    ))
    closed: list[TrackedLevel] = []
    t_prime = int(d_ts[0]) - 1
    tracker.on_timestamp(t_prime)
    for px, sz in init_bids.items():
        tracker.on_depth(True, int(px), int(sz))
    for px, sz in init_asks.items():
        tracker.on_depth(False, int(px), int(sz))
    tracker.active.clear()  # pre-existing size is context, not a "new" level
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
    for lvl in list(tracker.active.values()):
        if not lvl.closed:
            tracker._close_level(lvl)
    closed.extend(tracker.drain_closed())

    return _SessionOF(
        t_ts=t_ts, t_px=t_px, t_sz=t_sz, t_buy=t_buy,
        slot0_ns=int(slot0), slot_ns=slot_ns, n_slots=n_slots,
        cs_n=_cs(n_per), cs_vol=_cs(v_per), cs_buy=_cs(b_per), cs_sell=_cs(s_per), cs_burst=_cs(burst),
        d_ts=d_ts, d_bid=d_bid, d_px=d_px, d_sz=d_sz, init_bids=init_bids, init_asks=init_asks,
        m_ts=m_ts, m_op=m_op, m_bid=m_bid, m_px=m_px, m_sz=m_sz,
        cfg=cfg, rth_open_ns=lo, large_thr=large_thr, closed_levels=closed, mbo_stats=mbo_stats,
    )


def _resolve_mbo(mbo: pa.Table | None, lo_ext: int, hi_ext: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict[str, int]]:
    """Flatten MBO into signed liquidity events: (+1 add, −1 cancel) with resolved price/size.

    Bookmap ``cancel`` rows have price=size=0 and only ``order_id``; ``replace``
    rows carry the new price/size. Each row is matched to the previous row of
    the same order id (vectorised: stable sort by (oid, index)) so a cancel is
    booked at the order's last known price/size and a replace becomes
    cancel(old) + add(new). Orders whose send precedes the slice are dropped.
    """
    empty = (np.array([], dtype=np.int64), np.array([], dtype=np.int8), np.array([], dtype=bool),
             np.array([], dtype=np.int64), np.array([], dtype=np.float64), {"rows": 0})
    if mbo is None or mbo.num_rows == 0:
        return empty
    import pyarrow.compute as pc

    ts0 = mbo.column("ts_ns").to_numpy().astype(np.int64)
    mm = (ts0 >= lo_ext) & (ts0 <= hi_ext)
    idx = np.where(mm)[0]
    if len(idx) == 0:
        return empty
    idx = idx[np.argsort(ts0[idx], kind="stable")]
    sub = mbo.take(pa.array(idx))
    ts = sub.column("ts_ns").to_numpy().astype(np.int64)
    opc = pc.utf8_lower(sub.column("op"))
    is_send = pc.equal(opc, "send").to_numpy(zero_copy_only=False).astype(bool)
    is_can = pc.equal(opc, "cancel").to_numpy(zero_copy_only=False).astype(bool)
    is_rep = pc.equal(opc, "replace").to_numpy(zero_copy_only=False).astype(bool)
    bid = sub.column("is_bid").to_numpy(zero_copy_only=False).astype(bool)
    px = sub.column("price_ticks").to_numpy().astype(np.int64)
    sz = sub.column("size").to_numpy().astype(np.float64)
    oid = pc.dictionary_encode(sub.column("order_id")).combine_chunks().indices.to_numpy(zero_copy_only=False).astype(np.int64)

    n = len(ts)
    order = np.lexsort((np.arange(n), oid))          # by oid, then time order
    o_sorted = oid[order]
    prev_in_sorted = np.full(n, -1, dtype=np.int64)
    same = np.zeros(n, dtype=bool)
    same[1:] = o_sorted[1:] == o_sorted[:-1]
    prev_in_sorted[1:][same[1:]] = order[:-1][same[1:]]
    prev = np.full(n, -1, dtype=np.int64)
    prev[order] = prev_in_sorted
    has_prev = prev >= 0
    # state of the order before this row = px/sz of the previous row (send or replace)
    prev_px = np.where(has_prev, px[np.maximum(prev, 0)], 0)
    prev_sz = np.where(has_prev, sz[np.maximum(prev, 0)], 0.0)
    prev_bid = np.where(has_prev, bid[np.maximum(prev, 0)], bid)
    prev_live = has_prev & (is_send[np.maximum(prev, 0)] | is_rep[np.maximum(prev, 0)])

    add_m = is_send | is_rep
    can_m = (is_can | is_rep) & prev_live
    out_ts = np.concatenate([ts[add_m], ts[can_m]])
    out_op = np.concatenate([np.ones(add_m.sum(), dtype=np.int8), -np.ones(can_m.sum(), dtype=np.int8)])
    out_bid = np.concatenate([bid[add_m], prev_bid[can_m]])
    out_px = np.concatenate([px[add_m], prev_px[can_m]])
    out_sz = np.concatenate([sz[add_m], prev_sz[can_m]])
    o = np.argsort(out_ts, kind="stable")
    stats = {
        "rows": int(n), "send": int(is_send.sum()), "cancel": int(is_can.sum()), "replace": int(is_rep.sum()),
        "cancel_resolved": int((is_can & prev_live).sum()), "replace_resolved": int((is_rep & prev_live).sum()),
    }
    return out_ts[o], out_op[o], out_bid[o], out_px[o], out_sz[o], stats


def _snapshot_books_at_times(
    sof: _SessionOF, times: list[int]
) -> dict[int, tuple[dict[int, int], dict[int, int]]]:
    """One-pass depth replay from the initial book; snapshot at each time."""
    if not times:
        return {}
    uniq = sorted(set(int(t) for t in times))
    out: dict[int, tuple[dict[int, int], dict[int, int]]] = {}
    bids = dict(sof.init_bids)
    asks = dict(sof.init_asks)
    i, n = 0, len(sof.d_ts)
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


def _mbo_net_add(sof: _SessionOF, t0: int, t1: int, *, want_bid: bool, px_lo: int, px_hi: int) -> float:
    """Net size sent − cancelled on one book side within a tick band over [t0, t1]."""
    if len(sof.m_ts) == 0:
        return 0.0
    if px_lo > px_hi:
        px_lo, px_hi = px_hi, px_lo
    lo = int(np.searchsorted(sof.m_ts, t0, side="left"))
    hi = int(np.searchsorted(sof.m_ts, t1, side="right"))
    if hi <= lo:
        return 0.0
    sl = slice(lo, hi)
    m = (sof.m_bid[sl] == want_bid) & (sof.m_px[sl] >= px_lo) & (sof.m_px[sl] <= px_hi)
    if not m.any():
        return 0.0
    ops = sof.m_op[sl][m]
    sz = sof.m_sz[sl][m]
    return float(sz[ops > 0].sum() - sz[ops < 0].sum())


def _mbo_band_flow(
    sof: _SessionOF, t0: int, t1: int, *, want_bid: bool, px_lo: int, px_hi: int
) -> tuple[float, float, int, int]:
    """(add_size, cancel_size, n_add, n_cancel) on one book side in a tick band over [t0, t1]."""
    if len(sof.m_ts) == 0:
        return 0.0, 0.0, 0, 0
    if px_lo > px_hi:
        px_lo, px_hi = px_hi, px_lo
    lo = int(np.searchsorted(sof.m_ts, t0, side="left"))
    hi = int(np.searchsorted(sof.m_ts, t1, side="right"))
    if hi <= lo:
        return 0.0, 0.0, 0, 0
    sl = slice(lo, hi)
    m = (sof.m_bid[sl] == want_bid) & (sof.m_px[sl] >= px_lo) & (sof.m_px[sl] <= px_hi)
    if not m.any():
        return 0.0, 0.0, 0, 0
    ops = sof.m_op[sl][m]
    sz = sof.m_sz[sl][m]
    ia = ops > 0
    ic = ops < 0
    return float(sz[ia].sum()), float(sz[ic].sum()), int(ia.sum()), int(ic.sum())


def _pull_rate(add: float, can: float) -> float:
    tot = add + can
    return float(can / tot) if tot > 0 else 0.5


def _rel_band(bb: int | None, ba: int | None, side: str, near: int) -> tuple[int, int] | None:
    """Near band relative to the then-BBO on ``side``."""
    if side == "ASK":
        if ba is None:
            return None
        return ba, ba + near
    if bb is None:
        return None
    return bb - near, bb


# ------------------------------------------------------------------ scoring

def score_event(
    sof: _SessionOF,
    *,
    t_touch: int,
    level_px: float,
    approach: int,
    book_at: dict[int, tuple[dict[int, int], dict[int, int]]],
) -> dict[str, Any]:
    cfg = sof.cfg
    C = cfg.clip_z
    pre_ns = int(cfg.primary_pre_ms * NS_PER_MS)
    at_ns = int(cfg.primary_post_ms * NS_PER_MS)
    post_ns = int(cfg.diagnostic_post_ms * NS_PER_MS)
    attr_pre = int(cfg.attract_pre_ms * NS_PER_MS)
    step_ns = int(cfg.baseline_step_ms * NS_PER_MS)
    large = sof.large_thr

    beyond_book, hold_book, beyond_buy, hold_buy = _beyond_hold_sides(approach)
    cs_beyond = sof.cs_buy if beyond_buy else sof.cs_sell
    cs_hold = sof.cs_buy if hold_buy else sof.cs_sell
    near_lo, near_hi, far_lo, far_hi = _band_ticks(level_px, approach, cfg.tick_radius, cfg.far_band_ticks)
    band_lo = min(near_lo, near_hi, far_lo, far_hi)
    band_hi = max(near_lo, near_hi, far_lo, far_hi)
    lvl_tick = int(round(level_px / PTS_PER_TICK))
    want_bid_beyond = beyond_book == "BID"

    b0, b1, baseline_ok = sof.baseline_bounds(t_touch)
    tb0, tb1, _ = sof.baseline_bounds(t_touch, cfg.tape_lookback_ms)
    baseline_min = (b1 - b0) / 60e9
    pre5_ns, pre10_ns = (int(x * NS_PER_MS) for x in cfg.long_pre_ms)
    leg_ns = int(cfg.leg_ms * NS_PER_MS)

    windows = {
        "pre": (t_touch - pre_ns, t_touch),
        "at": (t_touch, t_touch + at_ns),
        "both": (t_touch - pre_ns, t_touch + at_ns),
        "pre5": (t_touch - pre5_ns, t_touch),
        "pre10": (t_touch - pre10_ns, t_touch),
        "leg": (t_touch - leg_ns, t_touch),
    }

    feats: dict[str, Any] = {}
    tape_z: dict[str, tuple[float, float, float]] = {}
    take_in: dict[str, tuple[float, float, float]] = {}  # (bmh_z, share, beyond_z)

    for name, (a, b) in windows.items():
        win = b - a
        beyond = sof.win_sum(cs_beyond, a, b)
        hold = sof.win_sum(cs_hold, a, b)
        prints = sof.win_sum(sof.cs_n, a, b)
        vol = sof.win_sum(sof.cs_vol, a, b)
        bursts = sof.win_sum(sof.cs_burst, a, b)
        # baselines: sliding windows of the same length over RTH-clipped lookback;
        # the 60 s leg window needs the long baseline to have enough samples.
        tape_lo, tape_hi = (b0, b1) if name == "leg" else (tb0, tb1)
        s_beyond = sof.sliding_sums(cs_beyond, b0, b1, win, step_ns)
        s_hold = sof.sliding_sums(cs_hold, b0, b1, win, step_ns)
        s_prints = sof.sliding_sums(sof.cs_n, tape_lo, tape_hi, win, step_ns)
        s_vol = sof.sliding_sums(sof.cs_vol, tape_lo, tape_hi, win, step_ns)
        s_burst = sof.sliding_sums(sof.cs_burst, tape_lo, tape_hi, win, step_ns)
        bz = _robust_z(beyond, s_beyond)
        hz = _robust_z(hold, s_hold)
        pz = _robust_z(prints, s_prints)
        vz = _robust_z(vol, s_vol)
        uz = _robust_z(bursts, s_burst)
        share = beyond / (beyond + hold) if (beyond + hold) > 0 else 0.5
        feats[f"beyond_aggr_{name}"] = beyond
        feats[f"hold_aggr_{name}"] = hold
        feats[f"beyond_share_{name}"] = share
        feats[f"beyond_minus_hold_z_{name}"] = bz - hz
        feats[f"prints_per_s_{name}"] = prints / max(1e-9, win / 1e9)
        feats[f"vol_{name}"] = vol
        feats[f"print_z_{name}"] = pz
        feats[f"vol_z_{name}"] = vz
        feats[f"burst_n_{name}"] = bursts
        feats[f"burst_z_{name}"] = uz
        tape_z[name] = (pz, vz, uz)
        take_in[name] = (bz - hz, share, bz)

    # --- book at touch
    bids, asks = book_at[t_touch]
    beyond_dict = asks if beyond_book == "ASK" else bids
    hold_dict = bids if beyond_book == "ASK" else asks
    near = _book_band_stats(beyond_dict, near_lo, near_hi, large)   # stack just beyond the level
    far = _book_band_stats(beyond_dict, far_lo, far_hi, large)
    bbo_b0, bbo_a0 = _bbo(bids, asks)
    # BBO-relative near bands on both sides at touch (for imbalance)
    rb_beyond = _rel_band(bbo_b0, bbo_a0, beyond_book, cfg.tick_radius)
    rb_hold = _rel_band(bbo_b0, bbo_a0, hold_book, cfg.tick_radius)
    beyond_bbo = _book_band_stats(beyond_dict, *rb_beyond, large) if rb_beyond else {"sum": 0.0}
    hold_near = _book_band_stats(hold_dict, *rb_hold, large) if rb_hold else {"sum": 0.0}
    tot = beyond_bbo["sum"] + hold_near["sum"]
    book_imb = (beyond_bbo["sum"] - hold_near["sum"]) / tot if tot > 0 else 0.0
    # offset of the level band from the BBO at touch (ticks, ≥0); baseline bands use the same offset
    if beyond_book == "ASK":
        off = max(0, near_lo - bbo_a0) if bbo_a0 is not None else 0
    else:
        off = max(0, bbo_b0 - near_hi) if bbo_b0 is not None else 0

    def _off_band(bb_s: dict[int, int], aa_s: dict[int, int], width: int) -> tuple[int, int] | None:
        bbo_b, bbo_a = _bbo(bb_s, aa_s)
        if beyond_book == "ASK":
            return None if bbo_a is None else (bbo_a + off, bbo_a + off + width)
        return None if bbo_b is None else (bbo_b - off - width, bbo_b - off)

    # offset-matched baseline for the beyond-side near band
    s_sum: list[float] = []
    s_large: list[float] = []
    # fixed grid only (not other events' touch / pre-window snapshots that share book_at)
    grid_ns = int(cfg.book_grid_ms * NS_PER_MS)
    grid_times = [t for t in book_at if b0 <= t < b1 and t % grid_ns == 0]
    for t_s in grid_times:
        bb_s, aa_s = book_at[t_s]
        rb = _off_band(bb_s, aa_s, cfg.tick_radius)
        if rb is None:
            continue
        st = _book_band_stats(aa_s if beyond_book == "ASK" else bb_s, rb[0], rb[1], large)
        s_sum.append(st["sum"])
        s_large.append(st["n_large"])
    near_sum_z = _robust_z(near["sum"], np.asarray(s_sum))
    large_count_z = _robust_z(near["n_large"], np.asarray(s_large))

    # --- MBO net add on beyond band in [-2s, 0]; baseline on offset-matched bands at grid points
    net_add = _mbo_net_add(sof, t_touch - pre_ns, t_touch, want_bid=want_bid_beyond, px_lo=band_lo, px_hi=band_hi)
    s_net: list[float] = []
    for t_s in grid_times:
        bb_s, aa_s = book_at[t_s]
        rb = _off_band(bb_s, aa_s, cfg.far_band_ticks)
        if rb is None:
            continue
        s_net.append(_mbo_net_add(sof, t_s, t_s + pre_ns, want_bid=want_bid_beyond, px_lo=rb[0], px_hi=rb[1]))
    net_add_z = _robust_z(net_add, np.asarray(s_net))

    # --- MBO pull rate / replenish on the beyond NEAR band in [-2s, 0); depletion vs book 2 s earlier
    add_sz, can_sz, n_add, n_can = _mbo_band_flow(
        sof, t_touch - pre_ns, t_touch, want_bid=want_bid_beyond, px_lo=near_lo, px_hi=near_hi
    )
    pull_rate = _pull_rate(add_sz, can_sz)
    s_pull: list[float] = []
    s_repl: list[float] = []
    for t_s in grid_times:
        bb_s, aa_s = book_at[t_s]
        rb = _off_band(bb_s, aa_s, cfg.tick_radius)
        if rb is None:
            continue
        a_s, c_s, na_s, _ = _mbo_band_flow(sof, t_s, t_s + pre_ns, want_bid=want_bid_beyond, px_lo=rb[0], px_hi=rb[1])
        s_pull.append(_pull_rate(a_s, c_s))
        s_repl.append(float(na_s))
    pull_rate_z = _robust_z(pull_rate, np.asarray(s_pull))
    replenish_z = _robust_z(float(n_add), np.asarray(s_repl))
    bids_m2, asks_m2 = book_at.get(t_touch - pre_ns, (bids, asks))
    near_m2 = _book_band_stats(asks_m2 if beyond_book == "ASK" else bids_m2, near_lo, near_hi, large)
    depletion = ((near_m2["sum"] - near["sum"]) / near_m2["sum"]) if near_m2["sum"] > 0 else 0.0
    depletion = float(max(-1.0, min(1.0, depletion)))

    # --- book trajectory over the approach leg (price-fixed bands at the level)
    if approach > 0:
        h_lo, h_hi = lvl_tick - cfg.tick_radius, lvl_tick
    else:
        h_lo, h_hi = lvl_tick, lvl_tick + cfg.tick_radius
    hold_fixed = _book_band_stats(hold_dict, h_lo, h_hi, large)
    trend_feats: dict[str, float] = {}
    for tm in cfg.book_trend_ms:
        k = tm // 1000
        b_then, a_then = book_at.get(t_touch - int(tm * NS_PER_MS), (bids, asks))
        then_beyond = _book_band_stats(a_then if beyond_book == "ASK" else b_then, near_lo, near_hi, large)
        # hold side: BBO-relative near band then vs now (price-fixed band is empty before touch)
        rb_h_then = _rel_band(*_bbo(b_then, a_then), hold_book, cfg.tick_radius)
        then_hold = _book_band_stats(b_then if beyond_book == "ASK" else a_then, *rb_h_then, large) if rb_h_then else {"sum": 0.0}
        trend_feats[f"near_sum_m{k}"] = then_beyond["sum"]
        # log-ratio build: >0 stack grew into the touch, <0 pulled
        trend_feats[f"stack_build_{k}"] = float(np.log((near["sum"] + 1.0) / (then_beyond["sum"] + 1.0)))
        trend_feats[f"hold_build_{k}"] = float(np.log((hold_near["sum"] + 1.0) / (then_hold["sum"] + 1.0)))
        # MBO pull-rate on beyond near band over [-tm, 0)
        a_l, c_l, _, _ = _mbo_band_flow(sof, t_touch - int(tm * NS_PER_MS), t_touch, want_bid=want_bid_beyond, px_lo=near_lo, px_hi=near_hi)
        trend_feats[f"pull_rate_pre{k}"] = _pull_rate(a_l, c_l)
    # baseline for the 10 s / 30 s pull rates at grid points (offset-matched bands)
    for tm in cfg.book_trend_ms[:2]:
        k = tm // 1000
        s_p: list[float] = []
        for t_s in grid_times:
            bb_s, aa_s = book_at[t_s]
            rb = _off_band(bb_s, aa_s, cfg.tick_radius)
            if rb is None:
                continue
            a_s, c_s, _, _ = _mbo_band_flow(sof, t_s, t_s + int(tm * NS_PER_MS), want_bid=want_bid_beyond, px_lo=rb[0], px_hi=rb[1])
            s_p.append(_pull_rate(a_s, c_s))
        trend_feats[f"pull_rate_z{k}"] = _robust_z(trend_feats[f"pull_rate_pre{k}"], np.asarray(s_p))

    # --- consume of the beyond stack in [0, +2s]
    stack0 = near["sum"] + far["sum"]
    i0 = int(np.searchsorted(sof.t_ts, t_touch, side="left"))
    i1 = int(np.searchsorted(sof.t_ts, t_touch + at_ns, side="right"))
    consumed = 0.0
    if i1 > i0:
        sl = slice(i0, i1)
        m = (sof.t_buy[sl] == beyond_buy) & (sof.t_px[sl] >= band_lo) & (sof.t_px[sl] <= band_hi)
        consumed = float(sof.t_sz[sl][m].sum())
    consume_frac = consumed / stack0 if stack0 > 0 else (1.0 if consumed > 0 else 0.0)
    s_beyond_at = sof.sliding_sums(cs_beyond, b0, b1, at_ns, step_ns)
    beyond_consume_z = _robust_z(consumed, s_beyond_at)
    # variable-window consume fractions for the confirmation-time sweep
    consume_frac_w: dict[int, float] = {}
    for wm in cfg.consume_windows_ms:
        i1w = int(np.searchsorted(sof.t_ts, t_touch + int(wm * NS_PER_MS), side="right"))
        cw = 0.0
        if i1w > i0:
            slw = slice(i0, i1w)
            mw = (sof.t_buy[slw] == beyond_buy) & (sof.t_px[slw] >= band_lo) & (sof.t_px[slw] <= band_hi)
            cw = float(sof.t_sz[slw][mw].sum())
        consume_frac_w[wm // 1000] = cw / stack0 if stack0 > 0 else (1.0 if cw > 0 else 0.0)

    # --- tracker-based flags (session-relative large levels)
    take_flag = 0.0
    absorb_flag = 0.0
    n_rel = 0
    ar = 0.0
    for lvl in sof.closed_levels:
        fs = lvl.first_seen_ns
        if fs < t_touch - attr_pre or fs > t_touch + at_ns:
            continue
        if abs(lvl.price - lvl_tick) > cfg.far_band_ticks:
            continue
        n_rel += 1
        is_beyond = lvl.side == beyond_book
        if is_beyond:
            if lvl.max_penetration_ticks > max(1, cfg.tick_radius // 2):
                take_flag = 1.0
            if lvl.label == "ABSORB":
                absorb_flag = 1.0
        # attract / repel within 8 ticks of level
        if abs(lvl.price - lvl_tick) > cfg.tick_radius:
            continue
        lab = lvl.label
        if lab in ("NONE", "DWELL_SUPPORT") and lvl.post_move_1s is not None:
            toward = (lvl.side == "ASK" and lvl.post_move_1s > 0) or (lvl.side == "BID" and lvl.post_move_1s < 0)
            lab = "ATTRACT" if toward else "REPEL"
        if is_beyond:
            if lab == "ATTRACT" or (lvl.max_penetration_ticks > 0 and lab != "ABSORB"):
                ar += 1.0
            elif lab in ("REPEL", "ABSORB", "WITHDRAW"):
                ar -= 1.0
        elif lvl.side == hold_book:
            if lab == "ATTRACT":
                ar -= 1.0
            elif lab in ("REPEL", "WITHDRAW"):
                ar += 1.0
    if take_flag < 0.5 and absorb_flag > 0.5:
        take_flag = -1.0
    elif take_flag < 0.5:
        bmh_at, share_at, _ = take_in["at"]
        take_flag = 1.0 if (consume_frac >= 0.5 and share_at > 0.5) else (
            -1.0 if (stack0 > 0 and consume_frac < 0.2 and share_at < 0.5) else 0.0
        )

    # --- scores
    def _take(name: str, with_consume: bool) -> float:
        bmh, share, _ = take_in[name]
        if with_consume:
            return _clip(0.50 * _clip(bmh, -C, C) + 0.30 * _clip(beyond_consume_z, -C, C) + 0.20 * take_flag, -C, C)
        return _clip(0.60 * _clip(bmh, -C, C) + 0.40 * _clip((share - 0.5) * 4.0, -C, C), -C, C)

    def _tape(name: str) -> float:
        pz, vz, uz = tape_z[name]
        return _clip(0.40 * _clip(pz, -C, C) + 0.40 * _clip(vz, -C, C) + 0.20 * _clip(uz, -C, C), -C, C)

    take_pre, take_at, take_both = _take("pre", False), _take("at", True), _take("both", True)
    tape_pre, tape_at, tape_both = _tape("pre"), _tape("at"), _tape("both")
    take_pre5, take_pre10, take_leg = _take("pre5", False), _take("pre10", False), _take("leg", False)
    tape_pre5, tape_pre10 = _tape("pre5"), _tape("pre10")
    stack_score = _clip(0.40 * _clip(near_sum_z, -C, C) + 0.30 * _clip(large_count_z, -C, C) + 0.30 * _clip(net_add_z, -C, C), -C, C)
    # beyond-side liquidity being withdrawn ahead of touch → break-like (+)
    mbo_pre = _clip(
        0.40 * _clip(pull_rate_z, -C, C) + 0.30 * _clip(depletion * 2.0, -C, C) - 0.30 * _clip(replenish_z, -C, C), -C, C
    )
    stack_consume = _clip(0.5 * _clip(near_sum_z, -C, C) + 0.5 * _clip((consume_frac - 0.5) * 4.0, -C, C), -C, C)
    stack_consume_w = {
        k: _clip(0.5 * _clip(near_sum_z, -C, C) + 0.5 * _clip((cf - 0.5) * 4.0, -C, C), -C, C)
        for k, cf in consume_frac_w.items()
    }
    # pre-touch book trajectory: stack pulled into the touch (+) vs built (−), plus short pull-rate z
    stack_trend = _clip(
        0.35 * _clip(-trend_feats.get("stack_build_30", 0.0) * 2.0, -C, C)
        + 0.25 * _clip(-trend_feats.get("stack_build_10", 0.0) * 2.0, -C, C)
        + 0.25 * _clip(trend_feats.get("pull_rate_z10", 0.0), -C, C)
        + 0.15 * _clip(trend_feats.get("pull_rate_z30", 0.0), -C, C),
        -C, C,
    )
    attract_repel = _clip(ar, -C, C)  # reported only; ≡0 at IRT prices
    bl_both = _clip(0.45 * take_both + 0.30 * stack_consume + 0.25 * tape_both, -C, C)
    bl_pre = _clip(0.35 * take_pre + 0.25 * stack_score + 0.20 * tape_pre + 0.20 * mbo_pre, -C, C)

    # --- diagnostic post window
    post_vol = sof.win_sum(sof.cs_vol, t_touch + at_ns, t_touch + post_ns)
    post_prints = sof.win_sum(sof.cs_n, t_touch + at_ns, t_touch + post_ns)

    out: dict[str, Any] = {
        # primary (pre-touch only; leak-free)
        "TakeScore_pre": take_pre,
        "TapeScore_pre": tape_pre,
        "StackScore": stack_score,
        "MboScore_pre": mbo_pre,
        "StackTrend_pre": stack_trend,
        "BreakLikeness_pre": bl_pre,
        # secondary leak-free windows
        "TakeScore_pre5": take_pre5, "TapeScore_pre5": tape_pre5,
        "TakeScore_pre10": take_pre10, "TapeScore_pre10": tape_pre10,
        "TakeScore_leg": take_leg,
        # MBO raws (pre window, beyond near band)
        "pull_rate_pre": pull_rate, "pull_rate_z": pull_rate_z,
        "replenish_n_pre": float(n_add), "replenish_z": replenish_z,
        "cancel_n_pre": float(n_can), "depletion_pre": depletion, "near_sum_m2": near_m2["sum"],
        # mechanism (sees first 2 s of reaction)
        "TakeScore_at": take_at,
        "TapeScore_at": tape_at,
        "TakeScore": take_both,
        "TapeScore": tape_both,
        "AttractRepelScore": attract_repel,
        "StackConsume": stack_consume,
        "BreakLikeness": bl_both,
        # flags
        "baseline_ok": 1.0 if baseline_ok else 0.0,
        "baseline_min": baseline_min,
        "large_thr": float(large),
        # book raw
        "near_sum": near["sum"], "near_max": near["max"], "near_n_large": near["n_large"],
        "far_sum": far["sum"], "beyond_bbo_sum": beyond_bbo["sum"], "hold_near_sum": hold_near["sum"],
        "book_imbalance": book_imb, "band_offset_ticks": float(off),
        "near_sum_z": near_sum_z, "large_count_z": large_count_z,
        "net_add": net_add, "net_add_z": net_add_z,
        "beyond_consume": consumed, "beyond_consume_z": beyond_consume_z, "consume_frac": consume_frac,
        "take_flag": take_flag, "n_tracker_levels": float(n_rel),
        "post_vol": post_vol, "post_prints": post_prints,
    }
    out.update(feats)
    out.update(trend_feats)
    out["hold_fixed_sum"] = hold_fixed["sum"]
    for k, v in stack_consume_w.items():
        out[f"StackConsume_{k}s"] = v
        out[f"consume_frac_{k}s"] = consume_frac_w[k]
    return out


def enrich_events_with_scores(events: pa.Table, sof: _SessionOF) -> pa.Table:
    """Append score columns for all events in one session."""
    if events.num_rows == 0:
        return events
    cfg = sof.cfg
    grid_ns = int(cfg.book_grid_ms * NS_PER_MS)
    times: list[int] = []
    touches = [int(x) for x in events.column("t_touch_ns").to_pylist()]
    pre_ns = int(cfg.primary_pre_ms * NS_PER_MS)
    for t in touches:
        times.append(t)
        times.append(t - pre_ns)  # book 2 s before touch (depletion)
        for tm in cfg.book_trend_ms:
            times.append(t - int(tm * NS_PER_MS))
        b0, b1, _ = sof.baseline_bounds(t)
        g = (b0 // grid_ns) * grid_ns
        while g < b1:
            if g >= b0:
                times.append(int(g))
            g += grid_ns
    book_at = _snapshot_books_at_times(sof, times)

    levels = [float(x) for x in events.column("level_px").to_pylist()]
    apps = [int(x) for x in events.column("approach").to_pylist()]
    rows = [
        score_event(sof, t_touch=t, level_px=L, approach=a, book_at=book_at)
        for t, L, a in zip(touches, levels, apps)
    ]
    out = events
    for k in rows[0]:
        out = out.append_column(k, pa.array([float(r[k]) for r in rows], type=pa.float64()))
    return out
