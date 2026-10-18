"""
Momentum / aggressive take features.

Discretionary read (projectdetails): large resting orders lifted on NQ with
ES also trading into size nearby in time, and a fast tape (many prints + volume).

v1 is tape-based (no depth replay): one-sided aggression bursts with high
print-count/volume, cross-confirmed ES↔NQ within a sync window. Continuation
side = burst direction (not fade).
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa


def trades_to_tape_bars(
    trades: pa.Table,
    bar_ms: int = 100,
) -> pa.Table:
    if trades.num_rows == 0:
        return pa.table({})
    ts = trades.column("ts_ns").to_numpy()
    price = trades.column("price").to_numpy().astype(np.float64)
    size = trades.column("size").to_numpy().astype(np.float64)
    buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)
    order = np.argsort(ts)
    ts, price, size, buy = ts[order], price[order], size[order], buy[order]

    bar_ns = int(bar_ms * 1_000_000)
    bar_id = (ts // bar_ns) * bar_ns
    uniq, start = np.unique(bar_id, return_index=True)
    ends = np.append(start[1:], len(ts))

    n = len(uniq)
    buy_vol = np.zeros(n)
    sell_vol = np.zeros(n)
    n_trades = np.zeros(n)
    max_print = np.zeros(n)
    open_px = np.zeros(n)
    high_px = np.zeros(n)
    low_px = np.zeros(n)
    close_px = np.zeros(n)

    for i, (s, e) in enumerate(zip(start, ends)):
        sl = slice(s, e)
        bmask = buy[sl]
        buy_vol[i] = size[sl][bmask].sum() if bmask.any() else 0.0
        sell_vol[i] = size[sl][~bmask].sum() if (~bmask).any() else 0.0
        n_trades[i] = e - s
        max_print[i] = size[sl].max() if e > s else 0.0
        open_px[i] = price[sl][0]
        high_px[i] = price[sl].max()
        low_px[i] = price[sl].min()
        close_px[i] = price[sl][-1]

    total = buy_vol + sell_vol
    imbalance = np.where(total > 0, (buy_vol - sell_vol) / total, 0.0)
    return pa.table({
        "ts_ns": uniq.astype(np.int64),
        "buy_vol": buy_vol,
        "sell_vol": sell_vol,
        "total_vol": total,
        "n_trades": n_trades,
        "max_print": max_print,
        "imbalance": imbalance,
        "open": open_px,
        "high": high_px,
        "low": low_px,
        "close": close_px,
        "net": close_px - open_px,
    })


def momentum_burst_events(
    bars: pa.Table,
    *,
    min_trades: int = 6,
    min_vol: float = 25.0,
    min_imbalance: float = 0.55,
    min_progress_ticks: float = 1.0,
    tick_size: float = 1.0,
    min_max_print: float = 0.0,
    cooldown_bars: int = 3,
    bar_ms: int = 100,
) -> pa.Table:
    """One-sided fast-tape bursts with price progress in the same direction.

    ``event_ts_ns`` is the **bar end** (bar_start + bar_ms), matching live MAT
    which can only fire after the 100 ms bar closes. Labelling from bar start
    was a look-ahead (the burst move itself counted toward the target).
    """
    if bars.num_rows == 0:
        return pa.table({})

    n_tr = bars.column("n_trades").to_numpy()
    vol = bars.column("total_vol").to_numpy()
    imb = bars.column("imbalance").to_numpy()
    net = bars.column("net").to_numpy()
    mx = bars.column("max_print").to_numpy()
    ts = bars.column("ts_ns").to_numpy()
    buy_v = bars.column("buy_vol").to_numpy()
    sell_v = bars.column("sell_vol").to_numpy()
    bar_ns = int(bar_ms * 1_000_000)

    progress = np.abs(net) / max(tick_size, 1e-9)
    fast = (n_tr >= min_trades) & (vol >= min_vol)
    if min_max_print > 0:
        fast &= mx >= min_max_print
    long_burst = fast & (imb >= min_imbalance) & (net > 0) & (progress >= min_progress_ticks)
    short_burst = fast & (imb <= -min_imbalance) & (net < 0) & (progress >= min_progress_ticks)
    raw = long_burst | short_burst

    # cooldown: keep first of a cluster
    keep = np.zeros(len(raw), dtype=bool)
    last = -10**9
    for i in np.where(raw)[0]:
        if i - last >= cooldown_bars:
            keep[i] = True
            last = i

    idx = np.where(keep)[0]
    if len(idx) == 0:
        return pa.table({})

    side = np.where(long_burst[idx], 1, -1).astype(np.int8)
    # Bar end = when the burst is known (matches TapeBurstTracker finishBar).
    event_ts = (ts[idx] + bar_ns).astype(np.int64)
    return pa.table({
        "event_ts_ns": event_ts,
        "side": side,
        "event_type": ["momentum_burst"] * len(idx),
        "n_trades": n_tr[idx],
        "total_vol": vol[idx],
        "imbalance": imb[idx],
        "max_print": mx[idx],
        "buy_vol": buy_v[idx],
        "sell_vol": sell_v[idx],
        "net_pts": net[idx],
    })


def cross_confirm_bursts(
    nq_events: pa.Table,
    es_events: pa.Table,
    sync_ms: int = 1500,
    *,
    causal_only: bool | None = None,
    stamp: str | None = None,
) -> pa.Table:
    """Keep NQ bursts that have a same-side ES burst within sync_ms.

    ``stamp`` modes (default ``"confirm"`` — tradable, no look-ahead):
      - ``"confirm"``: ES within ±sync; ``event_ts_ns = max(nq_end, es_end)``
        (NQ-first events enter at ES confirm time).
      - ``"causal"``: only ES at/before NQ (matches live MAT ``hasRecentBurst``);
        ``event_ts_ns = nq_end``.
      - ``"nq"``: ES within ±sync; stamp at NQ end (old non-causal research default;
        can use future ES — prefer ``confirm``).

    ``causal_only=True/False`` maps to ``stamp="causal"/"nq"`` for backward compat.
    """
    if stamp is None:
        if causal_only is True:
            stamp = "causal"
        elif causal_only is False:
            stamp = "nq"
        else:
            stamp = "confirm"
    if stamp not in ("confirm", "causal", "nq"):
        raise ValueError(f"unknown stamp mode: {stamp}")

    if nq_events.num_rows == 0:
        return pa.table({})
    if es_events.num_rows == 0:
        return pa.table({})

    nq_ts = nq_events.column("event_ts_ns").to_numpy().astype(np.int64)
    nq_side = nq_events.column("side").to_numpy()
    es_ts = es_events.column("event_ts_ns").to_numpy().astype(np.int64)
    es_side = es_events.column("side").to_numpy()
    window = int(sync_ms * 1_000_000)

    order = np.argsort(es_ts)
    es_ts = es_ts[order]
    es_side = es_side[order]

    keep = []
    es_n_tr = (
        es_events.column("n_trades").to_numpy()[order]
        if "n_trades" in es_events.column_names
        else np.zeros(len(es_ts))
    )
    es_vol = (
        es_events.column("total_vol").to_numpy()[order]
        if "total_vol" in es_events.column_names
        else np.zeros(len(es_ts))
    )
    matched_es_trades = []
    matched_es_vol = []
    matched_lag_ms = []
    matched_signed_lag_ms = []
    matched_es_ts = []

    for i, (t, s) in enumerate(zip(nq_ts, nq_side)):
        lo_t = t - window
        hi_t = t if stamp == "causal" else t + window
        lo = np.searchsorted(es_ts, lo_t, side="left")
        hi = np.searchsorted(es_ts, hi_t, side="right")
        best = None
        best_abs = None
        best_signed = None
        for j in range(lo, hi):
            if es_side[j] != s:
                continue
            signed = int(es_ts[j] - t)  # negative => ES before NQ
            lag = abs(signed)
            if best_abs is None or lag < best_abs:
                best_abs = lag
                best_signed = signed
                best = j
        if best is not None:
            keep.append(i)
            matched_es_trades.append(float(es_n_tr[best]))
            matched_es_vol.append(float(es_vol[best]))
            matched_lag_ms.append(best_abs / 1e6)
            matched_signed_lag_ms.append(best_signed / 1e6)
            matched_es_ts.append(int(es_ts[best]))

    if not keep:
        return pa.table({})

    idx = np.asarray(keep, dtype=np.int64)
    nq_sel = nq_ts[idx]
    es_sel = np.asarray(matched_es_ts, dtype=np.int64)
    if stamp == "confirm":
        event_ts = np.maximum(nq_sel, es_sel)
    else:
        event_ts = nq_sel
    cols = {
        "event_ts_ns": event_ts,
        "side": nq_side[idx].astype(np.int8),
        "event_type": ["es_nq_momentum"] * len(idx),
        "es_lag_ms": np.asarray(matched_lag_ms, dtype=np.float64),
        "es_signed_lag_ms": np.asarray(matched_signed_lag_ms, dtype=np.float64),
        "es_n_trades": np.asarray(matched_es_trades, dtype=np.float64),
        "es_total_vol": np.asarray(matched_es_vol, dtype=np.float64),
        "nq_event_ts_ns": nq_sel,
        "es_event_ts_ns": es_sel,
    }
    for name in nq_events.column_names:
        if name in ("event_ts_ns", "side", "event_type"):
            continue
        cols[name] = nq_events.column(name).take(idx)
    return pa.table(cols)


def filter_events_near_level_hits(
    events: pa.Table,
    levels: list,
    *,
    window_ms: int = 500,
    min_executed: float = 10.0,
    min_consume_ratio: float = 0.0,
) -> pa.Table:
    """Keep events near a large level being traded into (same side).

    ASK hit ↔ long event; BID hit ↔ short event. Does not require penetration
    through the level (most NQ consumes print at the level price).
    """
    if events.num_rows == 0:
        return pa.table({})

    hits = []
    for lv in levels:
        if not getattr(lv, "contacted", False) or lv.contact_ns <= 0:
            continue
        if lv.executed_into < min_executed:
            continue
        if lv.consume_ratio() < min_consume_ratio:
            continue
        # side of continuation if this level is taken
        side = 1 if lv.side == "ASK" else -1
        hits.append((int(lv.contact_ns), side, int(lv.executed_into), float(lv.consume_ratio())))

    if not hits:
        return pa.table({})

    hits.sort(key=lambda x: x[0])
    h_ts = np.array([h[0] for h in hits], dtype=np.int64)
    h_side = np.array([h[1] for h in hits], dtype=np.int8)
    window = int(window_ms * 1_000_000)

    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    ev_side = events.column("side").to_numpy()
    keep = []
    hit_exec = []
    hit_consume = []
    for i, (t, s) in enumerate(zip(ev_ts, ev_side)):
        lo = np.searchsorted(h_ts, t - window, side="left")
        hi = np.searchsorted(h_ts, t + window, side="right")
        best = None
        for j in range(lo, hi):
            if h_side[j] != s:
                continue
            best = j
            break
        if best is not None:
            keep.append(i)
            hit_exec.append(float(hits[best][2]))
            hit_consume.append(float(hits[best][3]))

    if not keep:
        return pa.table({})
    idx = np.asarray(keep, dtype=np.int64)
    out = events.take(idx)
    # drop prior hit diagnostics if re-filtering
    for col in ("level_executed_into", "level_consume_ratio"):
        if col in out.column_names:
            out = out.drop(col)
    out = out.append_column("level_executed_into", pa.array(hit_exec, type=pa.float64()))
    out = out.append_column("level_consume_ratio", pa.array(hit_consume, type=pa.float64()))
    return out


def filter_events_near_fast_tape(
    events: pa.Table,
    tape_bars: pa.Table,
    *,
    window_ms: int = 200,
    min_trades: int = 6,
    min_vol: float = 25.0,
    require_same_side: bool = True,
) -> pa.Table:
    """Keep events that coincide with a fast tape bar nearby (optional same side)."""
    if events.num_rows == 0 or tape_bars.num_rows == 0:
        return pa.table({})

    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    ev_side = events.column("side").to_numpy()
    b_ts = tape_bars.column("ts_ns").to_numpy().astype(np.int64)
    b_tr = tape_bars.column("n_trades").to_numpy()
    b_vol = tape_bars.column("total_vol").to_numpy()
    b_imb = tape_bars.column("imbalance").to_numpy()
    window = int(window_ms * 1_000_000)

    fast = (b_tr >= min_trades) & (b_vol >= min_vol)
    keep = []
    for i, (t, s) in enumerate(zip(ev_ts, ev_side)):
        lo = np.searchsorted(b_ts, t - window, side="left")
        hi = np.searchsorted(b_ts, t + window, side="right")
        ok = False
        for j in range(lo, hi):
            if not fast[j]:
                continue
            if require_same_side:
                if s > 0 and b_imb[j] < 0.4:
                    continue
                if s < 0 and b_imb[j] > -0.4:
                    continue
            ok = True
            break
        if ok:
            keep.append(i)

    if not keep:
        return pa.table({})
    idx = np.asarray(keep, dtype=np.int64)
    return events.take(idx)
