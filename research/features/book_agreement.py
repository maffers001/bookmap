"""BBO/top-3 imbalance agreement states (EXP-0054 / H45, Tier A / A4).

Per market, from a depth-replayed book: per completed second,
``imb = (bid_topK - ask_topK) / (bid_topK + ask_topK)`` where bid_topK /
ask_topK are summed sizes at the K best bid/ask levels. Feature at a
trigger = mean imb over the trailing ``window_sec`` completed seconds
(defined if >= ``min_defined_sec`` of them are defined and the book is
not empty/crossed). Lean: >= +lean_cut bid, <= -lean_cut ask, else
neutral.

Replay efficiency: between sample times only the LAST update per
(side, price) in the interval is applied. Order within an interval is
irrelevant once only last-per-key updates are kept, so the resulting
book is identical to applying every update in order; the brute-force
hand-check in this module proves that on a sample.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass
class BookAgreementParams:
    window_sec: int = 5
    top_levels: int = 3
    lean_cut: float = 0.20
    min_defined_sec: int = 3


def _sorted_depth(depth: pa.Table):
    ts = depth.column("ts_ns").to_numpy().astype(np.int64)
    bid = depth.column("is_bid").to_numpy().astype(bool)
    px = depth.column("price_ticks").to_numpy().astype(np.int64)
    sz = depth.column("size").to_numpy().astype(np.int64)
    if len(ts) > 1 and np.any(ts[1:] < ts[:-1]):
        order = np.argsort(ts, kind="stable")
        ts, bid, px, sz = ts[order], bid[order], px[order], sz[order]
    return ts, bid, px, sz


def _imb_from_books(bids: dict[int, int], asks: dict[int, int], top: int) -> float:
    if not bids or not asks:
        return float("nan")
    bb = max(bids)
    ba = min(asks)
    if bb >= ba:
        return float("nan")
    bid_top = sum(bids[k] for k in heapq.nlargest(top, bids))
    ask_top = sum(asks[k] for k in heapq.nsmallest(top, asks))
    tot = bid_top + ask_top
    if tot <= 0:
        return float("nan")
    return float((bid_top - ask_top) / tot)


def replay_imbalance_samples(
    depth: pa.Table,
    init_bids: dict[int, int],
    init_asks: dict[int, int],
    sample_end_ns: np.ndarray,
    *,
    top_levels: int = 3,
) -> np.ndarray:
    """Imbalance at each sample end (book after updates with ts < end)."""
    samples = np.asarray(sample_end_ns, dtype=np.int64)
    out = np.full(len(samples), np.nan)
    if len(samples) == 0 or depth.num_rows == 0:
        return out
    d_ts, d_bid, d_px, d_sz = _sorted_depth(depth)
    order = np.argsort(samples, kind="stable")
    sorted_samples = samples[order]
    bids = dict(init_bids)
    asks = dict(init_asks)
    i, n = 0, len(d_ts)
    res_sorted = np.full(len(samples), np.nan)
    for si, t_end in enumerate(sorted_samples):
        t_end = int(t_end)
        if i < n and d_ts[i] < t_end:
            j = i + int(np.searchsorted(d_ts[i:], t_end, side="left"))
            if j > i:
                keys = d_px[i:j] * 2 + d_bid[i:j].astype(np.int64)
                # last occurrence per key within (i, j]
                _, idx_rev = np.unique(keys[::-1], return_index=True)
                last = (j - 1) - idx_rev
                for k in last:
                    book = bids if d_bid[k] else asks
                    pxk, szk = int(d_px[k]), int(d_sz[k])
                    if szk <= 0:
                        book.pop(pxk, None)
                    else:
                        book[pxk] = szk
                i = j
        res_sorted[si] = _imb_from_books(bids, asks, top_levels)
    out[order] = res_sorted
    return out


def market_event_imbalance(
    depth: pa.Table,
    init_bids: dict[int, int],
    init_asks: dict[int, int],
    event_ts_ns: np.ndarray,
    p: BookAgreementParams | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-event (mean trailing imb, n_defined_seconds). Causal."""
    p = p or BookAgreementParams()
    sec = 1_000_000_000
    ev = np.asarray(event_ts_ns, dtype=np.int64)
    base = (ev // sec) * sec  # last second-end <= event stamp
    # Needed sample ends: base-4s .. base
    ends = np.stack([base - k * sec for k in range(p.window_sec - 1, -1, -1)], axis=1)
    flat = ends.ravel()
    imb_flat = replay_imbalance_samples(
        depth, init_bids, init_asks, flat, top_levels=p.top_levels)
    imb = imb_flat.reshape(ends.shape)
    ndef = np.isfinite(imb).sum(axis=1)
    mean = np.full(len(ev), np.nan)
    ok = ndef >= p.min_defined_sec
    if ok.any():
        mean[ok] = np.nanmean(np.where(np.isfinite(imb[ok]), imb[ok], np.nan), axis=1)
    return mean, ndef.astype(np.int32)


def brute_imbalance_at(
    depth: pa.Table,
    init_bids: dict[int, int],
    init_asks: dict[int, int],
    t_end: int,
    *,
    top_levels: int = 3,
) -> float:
    """Brute-force replay of EVERY update with ts < t_end; compare to chunked."""
    d_ts, d_bid, d_px, d_sz = _sorted_depth(depth)
    bids = dict(init_bids)
    asks = dict(init_asks)
    j = int(np.searchsorted(d_ts, int(t_end), side="left"))
    bids_l, asks_l = bids, asks
    for k in range(j):
        book = bids_l if d_bid[k] else asks_l
        pxk, szk = int(d_px[k]), int(d_sz[k])
        if szk <= 0:
            book.pop(pxk, None)
        else:
            book[pxk] = szk
    return _imb_from_books(bids, asks, top_levels)


def lean(imb: float, cut: float) -> int:
    """+1 bid-lean, -1 ask-lean, 0 neutral, 99 undefined."""
    if not np.isfinite(imb):
        return 99
    if imb >= cut:
        return 1
    if imb <= -cut:
        return -1
    return 0


def absolute_state(nq_lean: int, es_lean: int) -> str:
    if nq_lean == 99 or es_lean == 99:
        return "warmup"
    if nq_lean == 1 and es_lean == 1:
        return "agree_bid"
    if nq_lean == -1 and es_lean == -1:
        return "agree_ask"
    if nq_lean != 0 and es_lean != 0:
        return "disagree"
    return "mixed"


def side_state(nq_lean: int, es_lean: int, side: int) -> str:
    """agree_with / agree_against / other / warmup relative to trade side."""
    if nq_lean == 99 or es_lean == 99:
        return "warmup"
    want = 1 if side > 0 else -1
    if nq_lean == want and es_lean == want:
        return "agree_with"
    if nq_lean == -want and es_lean == -want:
        return "agree_against"
    return "other"
