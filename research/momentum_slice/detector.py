"""Liquidity Slice-Through (LST) momentum detector — v1, provisional thresholds.

Anatomy reverse-engineered from 10-Sep-2026 15:24 BST (session 100926):
  * pre-move: above-average resting stack overhead (NQ asks / ES asks for long)
    that had persisted for tens of seconds (Bookmap heatmap bands);
  * ignition 15:24:37.250 in BOTH markets in the same 250 ms bar;
  * price traverses the stack in seconds with traded volume at the stack
    prices >= the displayed size (liquidity traded, not pulled);
  * one-sided delta through the slice; continuation afterwards
    (NQ 29179 low -> 29262 in ~15 s; ES 7602 -> 7610.5 in ~4 s).

Stages
  A  tape-only slice candidates per instrument (sliding 2 s windows, 250 ms
     grid) cross-confirmed ES<->NQ same side, starts within 2 s.
  B  depth verification in one streaming pass per session/role: stack size /
     density / big-level count / persistence at ignition-3s (-33s/-13s for
     persistence), and consume ratio = aggressor volume traded inside the
     pre-move zone during [ignition, ignition+5s] / displayed stack at -3s.

All timestamps causal: a candidate is only "known" at its confirm time
(end of the later 2 s window). `ignition_bst` is for Bookmap replay lookup.
Prices: parquet trade/depth prices are ticks (pts = ticks * 0.25).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

PTS = 0.25


@dataclass
class InstCfg:
    disp_ticks: int          # min |displacement| in 2 s window, ticks
    min_vol: float           # min total volume in window
    min_abs_delta: float     # min |delta| in window
    min_imb: float           # min |delta|/vol
    big_level: int           # displayed size counting as a "big" level


CFG = {
    # ES book is ~15-20x deeper per level than NQ; thresholds set per instrument
    # from the 100926 example (NQ 2s vol ~450/delta ~+250 at ignition;
    # ES 2s vol ~1400/delta ~+230) with headroom below for weaker examples.
    "NQ": InstCfg(disp_ticks=24, min_vol=250, min_abs_delta=100, min_imb=0.40, big_level=15),
    "ES": InstCfg(disp_ticks=6, min_vol=800, min_abs_delta=250, min_imb=0.30, big_level=120),
}
WIN_NS = 2_000_000_000
GRID_NS = 250_000_000
ZONE_TICKS = 40              # pre-move stack zone ahead of price (10 pts)
CONFIRM_SYNC_NS = 2_000_000_000


def _arrays(t: pa.Table):
    ts = t.column("ts_ns").to_numpy().astype(np.int64)
    o = np.argsort(ts, kind="stable")
    return (ts[o], t.column("price").to_numpy().astype(np.float64)[o],
            t.column("size").to_numpy().astype(np.float64)[o],
            t.column("is_buy_aggressor").to_numpy().astype(bool)[o])


def tape_candidates(t: pa.Table, role: str) -> list[dict]:
    """Sliding 2 s slice windows on a 250 ms grid; clustered to local maxima."""
    cfg = CFG[role]
    ts, px, sz, buy = _arrays(t)
    if len(ts) == 0:
        return []
    signed = np.where(buy, sz, -sz)
    cum_sz = np.concatenate([[0.0], np.cumsum(sz)])
    cum_dl = np.concatenate([[0.0], np.cumsum(signed)])
    starts = np.arange(ts[0], ts[-1] - WIN_NS, GRID_NS, dtype=np.int64)
    lo = np.searchsorted(ts, starts)
    hi = np.searchsorted(ts, starts + WIN_NS)
    ok = hi > lo
    starts, lo, hi = starts[ok], lo[ok], hi[ok]
    # first/last trade price in window (px is in ticks already)
    first_px = px[lo]
    last_px = px[np.maximum(hi - 1, lo)]
    disp = last_px - first_px
    vol = cum_sz[hi] - cum_sz[lo]
    delta = cum_dl[hi] - cum_dl[lo]
    imb = np.where(vol > 0, delta / vol, 0.0)
    side = np.sign(disp).astype(int)
    mask = ((np.abs(disp) >= cfg.disp_ticks) & (vol >= cfg.min_vol)
            & (np.abs(delta) >= cfg.min_abs_delta) & (np.abs(imb) >= cfg.min_imb)
            & (np.sign(delta) == side) & (side != 0))
    idx = np.where(mask)[0]
    if len(idx) == 0:
        return []
    # cluster windows whose starts are <5 s apart (same side); keep max |disp|
    out: list[dict] = []
    cluster = [idx[0]]
    def flush(cl):
        # Episode semantics: ignition = FIRST qualifying window (both markets
        # ignited 15:24:37.25 in the example, but their peak-disp windows were
        # 4.8 s apart on different thrust legs — syncing peaks misses the move).
        best = max(cl, key=lambda i: abs(disp[i]))
        first = cl[0]
        out.append(dict(role=role, side=int(side[first]),
                        ignition_ns=int(starts[first]),
                        confirm_ns=int(starts[first] + WIN_NS),
                        peak_ignition_ns=int(starts[best]),
                        disp_ticks=float(disp[best]), vol=float(vol[best]),
                        delta=float(delta[best]), imb=float(imb[best]),
                        pre_price_ticks=float(first_px[first]),
                        end_price_ticks=float(last_px[best]),
                        n_trades=int(hi[best] - lo[best])))
    for i in idx[1:]:
        if starts[i] - starts[cluster[-1]] < 5_000_000_000 and side[i] == side[cluster[-1]]:
            cluster.append(i)
        else:
            flush(cluster); cluster = [i]
    flush(cluster)
    return out


def cross_confirm(nq: list[dict], es: list[dict]) -> list[dict]:
    """Same-side ES+NQ ignitions with starts within 2 s. One event per pair."""
    out, used = [], set()
    for n in sorted(nq, key=lambda d: d["ignition_ns"]):
        best, bj = None, -1
        for j, e in enumerate(es):
            if j in used or e["side"] != n["side"]:
                continue
            lag = abs(e["ignition_ns"] - n["ignition_ns"])
            if lag <= CONFIRM_SYNC_NS and (best is None or lag < best):
                best, bj = lag, j
        if bj >= 0:
            used.add(bj)
            e = es[bj]
            out.append(dict(side=n["side"], ignition_ns=min(n["ignition_ns"], e["ignition_ns"]),
                            confirm_ns=max(n["confirm_ns"], e["confirm_ns"]),
                            es_lag_ms=best / 1e6, nq=n, es=e))
    return out


def _book_snapshots(parquet_path: str, role: str, want: list[int],
                    bands: list[tuple[int, int]]) -> dict[int, dict]:
    """Book (price_ticks->size, both sides encoded +ask/-bid key sign via tuple)
    at each time in `want`, tracking only prices inside any band."""
    pf = pq.ParquetFile(parquet_path)
    book: dict[tuple[bool, int], int] = {}
    snaps: dict[int, dict] = {}
    pending = sorted(want)
    lo_all = min(b[0] for b in bands); hi_all = max(b[1] for b in bands)
    for batch in pf.iter_batches(batch_size=1_000_000,
                                 columns=["ts_ns", "role", "is_bid", "price_ticks", "size"]):
        t = pa.Table.from_batches([batch])
        ts = t.column("ts_ns").to_numpy().astype(np.int64)
        if len(ts) == 0:
            continue
        while pending and pending[0] <= ts[-1]:
            st = pending.pop(0)
            k = int(np.searchsorted(ts, st))
            _apply(t, slice(0, k), role, lo_all, hi_all, book)
            snaps[st] = dict(book)
            t = t.slice(k); ts = ts[k:]
            if len(ts) == 0:
                break
        if len(ts):
            _apply(t, slice(0, len(ts)), role, lo_all, hi_all, book)
        if pending and ts[-1] >= pending[-1]:
            continue
        if not pending and len(snaps) >= len(want):
            break
    for st in pending:  # times beyond data end
        snaps[st] = dict(book)
    return snaps


def _apply(t, sl, role, lo_all, hi_all, book):
    roles = t.column("role").to_numpy(zero_copy_only=False)[sl]
    px = t.column("price_ticks").to_numpy().astype(np.int64)[sl]
    bid = t.column("is_bid").to_numpy().astype(bool)[sl]
    sz = t.column("size").to_numpy().astype(np.int64)[sl]
    m = (roles == role) & (px >= lo_all) & (px <= hi_all)
    for b, p, s in zip(bid[m], px[m], sz[m]):
        key = (bool(b), int(p))
        if s > 0:
            book[key] = int(s)
        else:
            book.pop(key, None)


def stack_metrics(book: dict, side: int, ref_ticks: float, role: str) -> dict:
    """Stack ahead of price: for long, asks in (ref, ref+ZONE]; short mirrored."""
    cfg = CFG[role]
    ref = int(round(ref_ticks))
    ahead, behind = [], []
    for (is_bid, p), s in book.items():
        d = (p - ref) * side  # >0 ahead in trade direction (ask side for long)
        on_ahead_side = (is_bid is False) if side > 0 else (is_bid is True)
        on_behind_side = (is_bid is True) if side > 0 else (is_bid is False)
        if on_ahead_side and 0 < d <= ZONE_TICKS:
            ahead.append((p, s))
        if on_behind_side and 0 < -d <= ZONE_TICKS:  # behind = opposite book behind price
            behind.append((p, s))
        if on_ahead_side and -ZONE_TICKS <= d <= 0:
            behind.append((p, s))
    stack = sum(s for _, s in ahead)
    opp = sum(s for _, s in behind)
    sizes = np.array([s for _, s in ahead], dtype=float)
    all_sizes = np.array(list(book.values()), dtype=float)
    med = float(np.median(all_sizes)) if len(all_sizes) else 0.0
    return dict(stack_sum=int(stack), opp_sum=int(opp),
                stack_vs_opp=(stack / opp if opp > 0 else float("nan")),
                n_big=int((sizes >= cfg.big_level).sum()) if len(sizes) else 0,
                max_level=int(sizes.max()) if len(sizes) else 0,
                median_level=med,
                stack_density=(stack / (med * ZONE_TICKS) if med > 0 else float("nan")))


def traded_in_zone(t: pa.Table, side: int, ref_ticks: float,
                   t0: int, t1: int) -> tuple[float, float]:
    """(aggressor vol in direction inside zone, total vol) during [t0,t1]."""
    ts, px, sz, buy = _arrays(t)
    lo, hi = np.searchsorted(ts, t0), np.searchsorted(ts, t1)
    px, sz, buy = px[lo:hi], sz[lo:hi], buy[lo:hi]
    d = (px - ref_ticks) * side
    in_zone = (d > 0) & (d <= ZONE_TICKS)
    aggr = buy if side > 0 else ~buy
    return float(sz[in_zone & aggr].sum()), float(sz.sum())


def verify_event(ev: dict, snaps_nq: dict, snaps_es: dict,
                 nq_tr: pa.Table, es_tr: pa.Table) -> dict:
    out = dict(ev)
    for role, snaps, tr in (("NQ", snaps_nq, nq_tr), ("ES", snaps_es, es_tr)):
        c = ev[role.lower()]
        t_pre = c["ignition_ns"] - 3_000_000_000
        ref = c["pre_price_ticks"]
        m = stack_metrics(snaps.get(t_pre, {}), ev["side"], ref, role)
        older = [stack_metrics(snaps.get(c["ignition_ns"] - dt, {}), ev["side"], ref, role)["stack_sum"]
                 for dt in (33_000_000_000, 13_000_000_000)]
        persist = (float(np.mean(older)) / m["stack_sum"]) if m["stack_sum"] > 0 else float("nan")
        tzv, tv = traded_in_zone(tr, ev["side"], ref, c["ignition_ns"], c["ignition_ns"] + 5_000_000_000)
        out[f"{role.lower()}_stack"] = m["stack_sum"]
        out[f"{role.lower()}_stack_vs_opp"] = m["stack_vs_opp"]
        out[f"{role.lower()}_density"] = m["stack_density"]
        out[f"{role.lower()}_n_big"] = m["n_big"]
        out[f"{role.lower()}_max_level"] = m["max_level"]
        out[f"{role.lower()}_persist"] = persist
        out[f"{role.lower()}_consume"] = (tzv / m["stack_sum"]) if m["stack_sum"] > 0 else float("nan")
        out[f"{role.lower()}_zone_vol"] = tzv
    return out
