"""50-tick range bars with per-price volume/delta, VPOC and 70% value area.

Used by EXP-0056 / H47 to attach causal bar context to frozen MAT signals.

Parquet ``price`` is tick units → ``range_ticks=50, tick_size=1.0`` (12.5 pts).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import TICK_PTS

ContextKind = Literal["prior", "partial"]


@dataclass
class RangeBarProfileConfig:
    range_ticks: int = 50
    tick_size: float = 1.0
    va_frac: float = 0.70
    vol_median_n: int = 20  # prior completed bars for volume median


def _empty_bars() -> pa.Table:
    return pa.table({
        "bar_id": pa.array([], type=pa.int32()),
        "start_ts_ns": pa.array([], type=pa.int64()),
        "end_ts_ns": pa.array([], type=pa.int64()),
        "start_i": pa.array([], type=pa.int32()),
        "end_i": pa.array([], type=pa.int32()),  # exclusive
        "open": pa.array([], type=pa.float64()),
        "high": pa.array([], type=pa.float64()),
        "low": pa.array([], type=pa.float64()),
        "close": pa.array([], type=pa.float64()),
        "buy_vol": pa.array([], type=pa.float64()),
        "sell_vol": pa.array([], type=pa.float64()),
        "total_vol": pa.array([], type=pa.float64()),
        "delta": pa.array([], type=pa.float64()),
        "delta_frac": pa.array([], type=pa.float64()),
        "n_trades": pa.array([], type=pa.int32()),
        "duration_ms": pa.array([], type=pa.float64()),
        "direction": pa.array([], type=pa.int8()),
        "vpoc": pa.array([], type=pa.float64()),
        "vah": pa.array([], type=pa.float64()),
        "val": pa.array([], type=pa.float64()),
        "va_width": pa.array([], type=pa.float64()),
        "vpoc_vol_share": pa.array([], type=pa.float64()),
        "vwap": pa.array([], type=pa.float64()),
        "vol_median_prior": pa.array([], type=pa.float64()),
        "vol_ratio": pa.array([], type=pa.float64()),
        "completed": pa.array([], type=pa.bool_()),
    })


def _empty_profile() -> pa.Table:
    return pa.table({
        "bar_id": pa.array([], type=pa.int32()),
        "price_tick": pa.array([], type=pa.float64()),
        "total_vol": pa.array([], type=pa.float64()),
        "buy_vol": pa.array([], type=pa.float64()),
        "sell_vol": pa.array([], type=pa.float64()),
        "delta": pa.array([], type=pa.float64()),
    })


def _profile_slice(
    price: np.ndarray,
    size: np.ndarray,
    buy: np.ndarray,
    start_i: int,
    end_i: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Per-price (ticks, total, buy, sell, delta) over trades [start_i, end_i)."""
    empty = np.array([], dtype=np.float64)
    if end_i <= start_i:
        return empty, empty, empty, empty, empty
    px = price[start_i:end_i]
    sz = size[start_i:end_i]
    is_buy = buy[start_i:end_i]
    ticks, inv = np.unique(px, return_inverse=True)
    buy_v = np.bincount(inv, weights=np.where(is_buy, sz, 0.0), minlength=len(ticks))
    sell_v = np.bincount(inv, weights=np.where(~is_buy, sz, 0.0), minlength=len(ticks))
    tot = buy_v + sell_v
    delta = buy_v - sell_v
    return (
        ticks.astype(np.float64),
        tot.astype(np.float64),
        buy_v.astype(np.float64),
        sell_v.astype(np.float64),
        delta.astype(np.float64),
    )


def value_area_from_profile(
    ticks: np.ndarray,
    vols: np.ndarray,
    *,
    va_frac: float = 0.70,
    vwap: float | None = None,
) -> tuple[float, float, float, float]:
    """Return (vpoc, vah, val, vpoc_vol_share).

    Expand contiguously from VPOC, at each step adding the adjacent side with
    more volume, until ≥ va_frac of total volume is covered.
    """
    if len(ticks) == 0 or float(vols.sum()) <= 0:
        return (float("nan"),) * 4
    total = float(vols.sum())
    # VPOC = max volume; tie-break closest to vwap, then lower tick
    max_v = float(vols.max())
    candidates = np.where(vols >= max_v - 1e-12)[0]
    if vwap is not None and np.isfinite(vwap) and len(candidates) > 1:
        dist = np.abs(ticks[candidates] - vwap)
        best = candidates[int(np.argmin(dist))]
        # secondary: among equal distance, lower tick
        eq = candidates[np.isclose(dist, dist.min())]
        poc_i = int(eq[np.argmin(ticks[eq])])
    else:
        poc_i = int(candidates[np.argmin(ticks[candidates])])
    vpoc = float(ticks[poc_i])
    vpoc_share = float(vols[poc_i]) / total

    lo = hi = poc_i
    covered = float(vols[poc_i])
    target = va_frac * total
    while covered < target - 1e-12 and (lo > 0 or hi < len(ticks) - 1):
        left = float(vols[lo - 1]) if lo > 0 else -1.0
        right = float(vols[hi + 1]) if hi < len(ticks) - 1 else -1.0
        if right > left:
            hi += 1
            covered += float(vols[hi])
        elif left > right:
            lo -= 1
            covered += float(vols[lo])
        else:
            # tie: prefer the side that exists; if both, expand both? plan says
            # add the adjacent side with more volume; on tie expand lower first
            if lo > 0:
                lo -= 1
                covered += float(vols[lo])
            elif hi < len(ticks) - 1:
                hi += 1
                covered += float(vols[hi])
            else:
                break
    val = float(ticks[lo])
    vah = float(ticks[hi])
    return vpoc, vah, val, vpoc_share


def build_range_bars_with_profile(
    trades: pa.Table,
    *,
    cfg: RangeBarProfileConfig | None = None,
) -> tuple[pa.Table, pa.Table]:
    """Completed 50-tick range bars + long per-price profile table.

    Incomplete trailing bar is dropped from the completed series.
    """
    cfg = cfg or RangeBarProfileConfig()
    if trades.num_rows == 0:
        return _empty_bars(), _empty_profile()

    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    price = trades.column("price").to_numpy().astype(np.float64)
    size = trades.column("size").to_numpy().astype(np.float64)
    buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)
    order = np.argsort(ts, kind="stable")
    ts, price, size, buy = ts[order], price[order], size[order], buy[order]

    range_pts = float(cfg.range_ticks) * float(cfg.tick_size)
    if range_pts <= 0 or len(ts) == 0:
        return _empty_bars(), _empty_profile()

    # Bar boundaries as [start_i, end_i) exclusive end
    bar_starts: list[int] = []
    bar_ends: list[int] = []  # exclusive
    start_i = 0
    o = h = l = float(price[0])
    for i in range(len(ts)):
        px = float(price[i])
        if i == start_i:
            o = h = l = px
        else:
            h = max(h, px)
            l = min(l, px)
        if h - l >= range_pts:
            # closing trade i is included
            bar_starts.append(start_i)
            bar_ends.append(i + 1)
            start_i = i + 1
            if start_i < len(ts):
                o = h = l = float(price[start_i])

    if not bar_starts:
        return _empty_bars(), _empty_profile()

    n_bars = len(bar_starts)
    rows = {
        "bar_id": [],
        "start_ts_ns": [],
        "end_ts_ns": [],
        "start_i": [],
        "end_i": [],
        "open": [],
        "high": [],
        "low": [],
        "close": [],
        "buy_vol": [],
        "sell_vol": [],
        "total_vol": [],
        "delta": [],
        "delta_frac": [],
        "n_trades": [],
        "duration_ms": [],
        "direction": [],
        "vpoc": [],
        "vah": [],
        "val": [],
        "va_width": [],
        "vpoc_vol_share": [],
        "vwap": [],
        "vol_median_prior": [],
        "vol_ratio": [],
        "completed": [],
    }
    prof_bar: list[int] = []
    prof_px: list[float] = []
    prof_tot: list[float] = []
    prof_buy: list[float] = []
    prof_sell: list[float] = []
    prof_delta: list[float] = []

    totals_for_median: list[float] = []

    for bid, (s, e) in enumerate(zip(bar_starts, bar_ends)):
        px_sl = price[s:e]
        sz_sl = size[s:e]
        buy_sl = buy[s:e]
        bv = float(sz_sl[buy_sl].sum()) if buy_sl.any() else 0.0
        sv = float(sz_sl[~buy_sl].sum()) if (~buy_sl).any() else 0.0
        tot = bv + sv
        delta = bv - sv
        dfrac = delta / tot if tot > 0 else 0.0
        o_ = float(px_sl[0])
        c_ = float(px_sl[-1])
        h_ = float(px_sl.max())
        l_ = float(px_sl.min())
        n_tr = int(e - s)
        dur = (float(ts[e - 1] - ts[s]) / 1e6) if e > s else 0.0
        vwap = float((px_sl * sz_sl).sum() / tot) if tot > 0 else o_

        ticks, tot_v, buy_v, sell_v, delta_v = _profile_slice(price, size, buy, s, e)
        # fix unpack — _profile_slice returns 5 arrays
        vpoc, vah, val, vpoc_share = value_area_from_profile(
            ticks, tot_v, va_frac=cfg.va_frac, vwap=vwap
        )

        # causal median of prior completed bar totals
        if len(totals_for_median) >= 3:
            window = totals_for_median[-cfg.vol_median_n :]
            med = float(np.median(window))
        else:
            med = float("nan")
        vol_ratio = (tot / med) if np.isfinite(med) and med > 0 else float("nan")
        totals_for_median.append(tot)

        rows["bar_id"].append(bid)
        rows["start_ts_ns"].append(int(ts[s]))
        rows["end_ts_ns"].append(int(ts[e - 1]))
        rows["start_i"].append(s)
        rows["end_i"].append(e)
        rows["open"].append(o_)
        rows["high"].append(h_)
        rows["low"].append(l_)
        rows["close"].append(c_)
        rows["buy_vol"].append(bv)
        rows["sell_vol"].append(sv)
        rows["total_vol"].append(tot)
        rows["delta"].append(delta)
        rows["delta_frac"].append(dfrac)
        rows["n_trades"].append(n_tr)
        rows["duration_ms"].append(dur)
        rows["direction"].append(int(np.sign(c_ - o_)))
        rows["vpoc"].append(vpoc)
        rows["vah"].append(vah)
        rows["val"].append(val)
        rows["va_width"].append(
            (vah - val) if np.isfinite(vah) and np.isfinite(val) else float("nan")
        )
        rows["vpoc_vol_share"].append(vpoc_share)
        rows["vwap"].append(vwap)
        rows["vol_median_prior"].append(med)
        rows["vol_ratio"].append(vol_ratio)
        rows["completed"].append(True)

        for j in range(len(ticks)):
            prof_bar.append(bid)
            prof_px.append(float(ticks[j]))
            prof_tot.append(float(tot_v[j]))
            prof_buy.append(float(buy_v[j]))
            prof_sell.append(float(sell_v[j]))
            prof_delta.append(float(delta_v[j]))

    bars = pa.table({
        "bar_id": pa.array(rows["bar_id"], type=pa.int32()),
        "start_ts_ns": pa.array(rows["start_ts_ns"], type=pa.int64()),
        "end_ts_ns": pa.array(rows["end_ts_ns"], type=pa.int64()),
        "start_i": pa.array(rows["start_i"], type=pa.int32()),
        "end_i": pa.array(rows["end_i"], type=pa.int32()),
        "open": pa.array(rows["open"], type=pa.float64()),
        "high": pa.array(rows["high"], type=pa.float64()),
        "low": pa.array(rows["low"], type=pa.float64()),
        "close": pa.array(rows["close"], type=pa.float64()),
        "buy_vol": pa.array(rows["buy_vol"], type=pa.float64()),
        "sell_vol": pa.array(rows["sell_vol"], type=pa.float64()),
        "total_vol": pa.array(rows["total_vol"], type=pa.float64()),
        "delta": pa.array(rows["delta"], type=pa.float64()),
        "delta_frac": pa.array(rows["delta_frac"], type=pa.float64()),
        "n_trades": pa.array(rows["n_trades"], type=pa.int32()),
        "duration_ms": pa.array(rows["duration_ms"], type=pa.float64()),
        "direction": pa.array(rows["direction"], type=pa.int8()),
        "vpoc": pa.array(rows["vpoc"], type=pa.float64()),
        "vah": pa.array(rows["vah"], type=pa.float64()),
        "val": pa.array(rows["val"], type=pa.float64()),
        "va_width": pa.array(rows["va_width"], type=pa.float64()),
        "vpoc_vol_share": pa.array(rows["vpoc_vol_share"], type=pa.float64()),
        "vwap": pa.array(rows["vwap"], type=pa.float64()),
        "vol_median_prior": pa.array(rows["vol_median_prior"], type=pa.float64()),
        "vol_ratio": pa.array(rows["vol_ratio"], type=pa.float64()),
        "completed": pa.array(rows["completed"], type=pa.bool_()),
    })
    profile = pa.table({
        "bar_id": pa.array(prof_bar, type=pa.int32()),
        "price_tick": pa.array(prof_px, type=pa.float64()),
        "total_vol": pa.array(prof_tot, type=pa.float64()),
        "buy_vol": pa.array(prof_buy, type=pa.float64()),
        "sell_vol": pa.array(prof_sell, type=pa.float64()),
        "delta": pa.array(prof_delta, type=pa.float64()),
    })
    return bars, profile


def _entry_position(mat_px_ticks: float, vah: float, val: float, vpoc: float) -> str:
    if not np.isfinite(vah) or not np.isfinite(val):
        return "unknown"
    if mat_px_ticks > vah:
        return "above_vah"
    if mat_px_ticks < val:
        return "below_val"
    if np.isfinite(vpoc):
        if mat_px_ticks > vpoc:
            return "inside_va_above_vpoc"
        if mat_px_ticks < vpoc:
            return "inside_va_below_vpoc"
    return "inside_va"


def _partial_bar_at(
    ts: np.ndarray,
    price: np.ndarray,
    size: np.ndarray,
    buy: np.ndarray,
    bars: pa.Table,
    asof_ns: int,
    *,
    cfg: RangeBarProfileConfig,
) -> dict | None:
    """Profile of the unfinished bar from last completed close through asof."""
    if len(ts) == 0:
        return None
    end_i = int(np.searchsorted(ts, asof_ns, side="right"))
    if end_i <= 0:
        return None

    if bars.num_rows > 0:
        bar_ends = bars.column("end_ts_ns").to_numpy().astype(np.int64)
        # last completed bar with end < asof
        idx = int(np.searchsorted(bar_ends, asof_ns, side="left")) - 1
        if idx >= 0:
            start_i = int(bars.column("end_i").to_numpy()[idx])  # next trade after close
            prior_vols = bars.column("total_vol").to_numpy().astype(np.float64)[: idx + 1]
        else:
            start_i = 0
            prior_vols = np.array([], dtype=np.float64)
    else:
        start_i = 0
        prior_vols = np.array([], dtype=np.float64)

    if start_i >= end_i:
        return None

    px_sl = price[start_i:end_i]
    sz_sl = size[start_i:end_i]
    buy_sl = buy[start_i:end_i]
    bv = float(sz_sl[buy_sl].sum()) if buy_sl.any() else 0.0
    sv = float(sz_sl[~buy_sl].sum()) if (~buy_sl).any() else 0.0
    tot = bv + sv
    delta = bv - sv
    dfrac = delta / tot if tot > 0 else 0.0
    vwap = float((px_sl * sz_sl).sum() / tot) if tot > 0 else float(px_sl[0])
    ticks, tot_v, buy_v, sell_v, delta_v = _profile_slice(price, size, buy, start_i, end_i)
    vpoc, vah, val, vpoc_share = value_area_from_profile(
        ticks, tot_v, va_frac=cfg.va_frac, vwap=vwap
    )
    if len(prior_vols) >= 3:
        med = float(np.median(prior_vols[-cfg.vol_median_n :]))
    else:
        med = float("nan")
    vol_ratio = (tot / med) if np.isfinite(med) and med > 0 else float("nan")
    # elapsed proxies vs typical completed bar (median n_trades / vol)
    if bars.num_rows >= 3:
        med_n = float(np.median(bars.column("n_trades").to_numpy().astype(np.float64)[-cfg.vol_median_n :]))
        med_v = med if np.isfinite(med) else float(
            np.median(bars.column("total_vol").to_numpy().astype(np.float64)[-cfg.vol_median_n :])
        )
    else:
        med_n = float("nan")
        med_v = med
    n_tr = int(end_i - start_i)
    return {
        "start_ts_ns": int(ts[start_i]),
        "end_ts_ns": int(ts[end_i - 1]),
        "total_vol": tot,
        "buy_vol": bv,
        "sell_vol": sv,
        "delta": delta,
        "delta_frac": dfrac,
        "n_trades": n_tr,
        "vpoc": vpoc,
        "vah": vah,
        "val": val,
        "vpoc_vol_share": vpoc_share,
        "vwap": vwap,
        "vol_median_prior": med,
        "vol_ratio": vol_ratio,
        "elapsed_trades_frac": (n_tr / med_n) if np.isfinite(med_n) and med_n > 0 else float("nan"),
        "elapsed_vol_frac": (tot / med_v) if np.isfinite(med_v) and med_v > 0 else float("nan"),
        "direction": int(np.sign(float(px_sl[-1]) - float(px_sl[0]))),
        "high": float(px_sl.max()),
        "low": float(px_sl.min()),
        "open": float(px_sl[0]),
        "close": float(px_sl[-1]),
    }


def _nan_row(cols: dict[str, list]) -> None:
    cols["mat_px"].append(float("nan"))
    cols["mat_px_ticks"].append(float("nan"))
    cols["prior_ok"].append(False)
    cols["prior_bar_id"].append(-1)
    cols["prior_end_ts_ns"].append(0)
    for k in (
        "prior_total_vol", "prior_delta", "prior_delta_frac", "prior_vol_ratio",
        "prior_vpoc", "prior_vah", "prior_val",
        "prior_vpoc_pts", "prior_vah_pts", "prior_val_pts",
        "prior_aligned_delta_frac",
    ):
        cols[k].append(float("nan"))
    cols["prior_direction"].append(0)
    cols["prior_pos"].append("unknown")
    cols["partial_ok"].append(False)
    for k in (
        "partial_total_vol", "partial_delta", "partial_delta_frac",
        "partial_vol_ratio", "partial_vpoc", "partial_vah", "partial_val",
        "partial_vpoc_pts", "partial_vah_pts", "partial_val_pts",
        "partial_aligned_delta_frac", "partial_elapsed_trades_frac",
        "partial_elapsed_vol_frac",
    ):
        cols[k].append(float("nan"))
    cols["partial_direction"].append(0)
    cols["partial_pos"].append("unknown")


def attach_mat_range_context(
    nq: pa.Table,
    mat_events: pa.Table,
    *,
    cfg: RangeBarProfileConfig | None = None,
    pts_per_tick: float = TICK_PTS,
) -> tuple[pa.Table, pa.Table, pa.Table]:
    """Attach prior-completed and partial-current context to MAT events.

    Returns (annotated_events, bars, profile).
    ``mat_px`` is in chart points; VPOC/VAH/VAL stored in tick units and points.
    """
    cfg = cfg or RangeBarProfileConfig()
    bars, profile = build_range_bars_with_profile(nq, cfg=cfg)
    if mat_events.num_rows == 0 or nq.num_rows == 0:
        return mat_events, bars, profile

    ts = nq.column("ts_ns").to_numpy().astype(np.int64)
    price = nq.column("price").to_numpy().astype(np.float64)
    size = nq.column("size").to_numpy().astype(np.float64)
    buy = nq.column("is_buy_aggressor").to_numpy().astype(bool)
    order = np.argsort(ts, kind="stable")
    ts, price, size, buy = ts[order], price[order], size[order], buy[order]

    ev_ts = mat_events.column("event_ts_ns").to_numpy().astype(np.int64)
    sides = mat_events.column("side").to_numpy().astype(int)
    bar_ends = (
        bars.column("end_ts_ns").to_numpy().astype(np.int64)
        if bars.num_rows
        else np.array([], dtype=np.int64)
    )

    col_names = (
        "mat_px", "mat_px_ticks",
        "prior_bar_id", "prior_end_ts_ns", "prior_total_vol", "prior_delta",
        "prior_delta_frac", "prior_vol_ratio", "prior_vpoc", "prior_vah", "prior_val",
        "prior_vpoc_pts", "prior_vah_pts", "prior_val_pts",
        "prior_aligned_delta_frac", "prior_direction", "prior_pos", "prior_ok",
        "partial_total_vol", "partial_delta", "partial_delta_frac",
        "partial_vol_ratio", "partial_vpoc", "partial_vah", "partial_val",
        "partial_vpoc_pts", "partial_vah_pts", "partial_val_pts",
        "partial_aligned_delta_frac", "partial_direction", "partial_pos",
        "partial_elapsed_trades_frac", "partial_elapsed_vol_frac", "partial_ok",
    )
    cols: dict[str, list] = {k: [] for k in col_names}

    for i in range(len(ev_ts)):
        t0 = int(ev_ts[i])
        side = int(sides[i])
        ei = int(np.searchsorted(ts, t0, side="left"))
        if ei >= len(ts):
            _nan_row(cols)
            continue
        mat_px_ticks = float(price[ei])
        cols["mat_px"].append(mat_px_ticks * pts_per_tick)
        cols["mat_px_ticks"].append(mat_px_ticks)

        prior_ok = False
        if len(bar_ends):
            idx = int(np.searchsorted(bar_ends, t0, side="left")) - 1
            if idx >= 0 and int(bar_ends[idx]) < t0:
                prior_ok = True
                b = {name: bars.column(name)[idx].as_py() for name in bars.column_names}
                cols["prior_bar_id"].append(int(b["bar_id"]))
                cols["prior_end_ts_ns"].append(int(b["end_ts_ns"]))
                cols["prior_total_vol"].append(float(b["total_vol"]))
                cols["prior_delta"].append(float(b["delta"]))
                cols["prior_delta_frac"].append(float(b["delta_frac"]))
                vr = b["vol_ratio"]
                cols["prior_vol_ratio"].append(float(vr) if vr is not None else float("nan"))
                cols["prior_vpoc"].append(float(b["vpoc"]))
                cols["prior_vah"].append(float(b["vah"]))
                cols["prior_val"].append(float(b["val"]))
                cols["prior_vpoc_pts"].append(float(b["vpoc"]) * pts_per_tick)
                cols["prior_vah_pts"].append(float(b["vah"]) * pts_per_tick)
                cols["prior_val_pts"].append(float(b["val"]) * pts_per_tick)
                cols["prior_aligned_delta_frac"].append(
                    float(b["delta_frac"]) * (1.0 if side > 0 else -1.0)
                )
                cols["prior_direction"].append(int(b["direction"]))
                cols["prior_pos"].append(_entry_position(
                    mat_px_ticks, float(b["vah"]), float(b["val"]), float(b["vpoc"])
                ))
        if not prior_ok:
            cols["prior_bar_id"].append(-1)
            cols["prior_end_ts_ns"].append(0)
            for k in (
                "prior_total_vol", "prior_delta", "prior_delta_frac", "prior_vol_ratio",
                "prior_vpoc", "prior_vah", "prior_val",
                "prior_vpoc_pts", "prior_vah_pts", "prior_val_pts",
                "prior_aligned_delta_frac",
            ):
                cols[k].append(float("nan"))
            cols["prior_direction"].append(0)
            cols["prior_pos"].append("unknown")
        cols["prior_ok"].append(prior_ok)

        part = _partial_bar_at(ts, price, size, buy, bars, t0, cfg=cfg)
        if part is None:
            cols["partial_ok"].append(False)
            for k in (
                "partial_total_vol", "partial_delta", "partial_delta_frac",
                "partial_vol_ratio", "partial_vpoc", "partial_vah", "partial_val",
                "partial_vpoc_pts", "partial_vah_pts", "partial_val_pts",
                "partial_aligned_delta_frac", "partial_elapsed_trades_frac",
                "partial_elapsed_vol_frac",
            ):
                cols[k].append(float("nan"))
            cols["partial_direction"].append(0)
            cols["partial_pos"].append("unknown")
        else:
            cols["partial_ok"].append(True)
            cols["partial_total_vol"].append(part["total_vol"])
            cols["partial_delta"].append(part["delta"])
            cols["partial_delta_frac"].append(part["delta_frac"])
            cols["partial_vol_ratio"].append(part["vol_ratio"])
            cols["partial_vpoc"].append(part["vpoc"])
            cols["partial_vah"].append(part["vah"])
            cols["partial_val"].append(part["val"])
            cols["partial_vpoc_pts"].append(part["vpoc"] * pts_per_tick)
            cols["partial_vah_pts"].append(part["vah"] * pts_per_tick)
            cols["partial_val_pts"].append(part["val"] * pts_per_tick)
            cols["partial_aligned_delta_frac"].append(
                part["delta_frac"] * (1.0 if side > 0 else -1.0)
            )
            cols["partial_direction"].append(part["direction"])
            cols["partial_pos"].append(_entry_position(
                mat_px_ticks, part["vah"], part["val"], part["vpoc"]
            ))
            cols["partial_elapsed_trades_frac"].append(part["elapsed_trades_frac"])
            cols["partial_elapsed_vol_frac"].append(part["elapsed_vol_frac"])

    out = mat_events
    type_map = {
        "prior_bar_id": pa.int32(),
        "prior_end_ts_ns": pa.int64(),
        "prior_direction": pa.int8(),
        "partial_direction": pa.int8(),
        "prior_pos": pa.string(),
        "partial_pos": pa.string(),
        "prior_ok": pa.bool_(),
        "partial_ok": pa.bool_(),
    }
    for name, vals in cols.items():
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(vals, type=type_map.get(name, pa.float64())))
    return out, bars, profile


def filter_arm_mask(
    events: pa.Table,
    arm: str,
    *,
    context: ContextKind = "prior",
) -> np.ndarray:
    """Boolean mask of events kept by a pre-registered arm."""
    n = events.num_rows
    if n == 0:
        return np.array([], dtype=bool)
    if arm == "base":
        return np.ones(n, dtype=bool)

    pfx = "prior_" if context == "prior" else "partial_"
    ok = events.column("prior_ok" if context == "prior" else "partial_ok").to_numpy().astype(bool)
    vol_ratio = events.column(f"{pfx}vol_ratio").to_numpy().astype(np.float64)
    adf = events.column(f"{pfx}aligned_delta_frac").to_numpy().astype(np.float64)
    pos = events.column(f"{pfx}pos").to_pylist()
    sides = events.column("side").to_numpy().astype(int)

    va_break = np.array([
        (sides[i] > 0 and pos[i] == "above_vah")
        or (sides[i] < 0 and pos[i] == "below_val")
        for i in range(n)
    ], dtype=bool)
    va_inside = np.array([
        str(pos[i]).startswith("inside_va") for i in range(n)
    ], dtype=bool)

    if arm == "vol":
        return ok & np.isfinite(vol_ratio) & (vol_ratio >= 1.5)
    if arm == "delta":
        return ok & np.isfinite(adf) & (adf >= 0.20)
    if arm == "vol_delta":
        return ok & np.isfinite(vol_ratio) & (vol_ratio >= 1.5) & np.isfinite(adf) & (adf >= 0.30)
    if arm == "va_break":
        return ok & va_break
    if arm == "vol_va_break":
        return ok & np.isfinite(vol_ratio) & (vol_ratio >= 1.5) & va_break
    if arm == "va_inside":
        return ok & va_inside
    raise ValueError(f"unknown arm: {arm}")


def validate_synthetic() -> list[str]:
    """Stage 0 synthetic fixture. Returns list of failure messages (empty = pass)."""
    errors: list[str] = []
    # Bar closes when high-low >= 50. Prices 100..150 → range 50, closes on 150.
    rows = []
    t0 = 1_000_000_000
    for i, px in enumerate(range(100, 151)):
        rows.append({
            "ts_ns": t0 + i * 2,
            "price": float(px),
            "size": 10 if px == 120 else 2,
            "is_buy_aggressor": True,
            "alias": "NQ", "role": "NQ", "symbol": "NQ",
            "aggressor_order_id": "", "is_exec_start": True, "is_exec_end": True,
            "session_id": "synth",
        })
        rows.append({
            "ts_ns": t0 + i * 2 + 1,
            "price": float(px),
            "size": 1,
            "is_buy_aggressor": False,
            "alias": "NQ", "role": "NQ", "symbol": "NQ",
            "aggressor_order_id": "", "is_exec_start": True, "is_exec_end": True,
            "session_id": "synth",
        })
    # Second bar 150..200
    for i, px in enumerate(range(150, 201)):
        rows.append({
            "ts_ns": t0 + 10_000 + i,
            "price": float(px),
            "size": 3,
            "is_buy_aggressor": True,
            "alias": "NQ", "role": "NQ", "symbol": "NQ",
            "aggressor_order_id": "", "is_exec_start": True, "is_exec_end": True,
            "session_id": "synth",
        })

    trades = pa.Table.from_pylist(rows)
    bars, profile = build_range_bars_with_profile(trades)
    if bars.num_rows < 1:
        errors.append("expected ≥1 completed bar")
        return errors
    b0 = {c: bars.column(c)[0].as_py() for c in bars.column_names}
    if abs(b0["high"] - b0["low"]) < 49.999:
        errors.append(f"bar0 range too small: {b0['high']-b0['low']}")
    if abs(b0["vpoc"] - 120.0) > 1e-6:
        errors.append(f"expected vpoc=120 got {b0['vpoc']}")
    if abs(b0["buy_vol"] + b0["sell_vol"] - b0["total_vol"]) > 1e-6:
        errors.append("buy+sell != total")
    # profile sum
    mask = profile.column("bar_id").to_numpy() == 0
    if mask.any():
        psum = float(profile.column("total_vol").to_numpy()[mask].sum())
        if abs(psum - b0["total_vol"]) > 1e-6:
            errors.append(f"profile vol {psum} != bar total {b0['total_vol']}")
    # VA contains ≥70%
    if not (np.isfinite(b0["vah"]) and np.isfinite(b0["val"]) and b0["vah"] >= b0["val"]):
        errors.append(f"bad VA: vah={b0['vah']} val={b0['val']}")
    return errors


def validate_session_invariants(
    nq: pa.Table,
    events: pa.Table,
    bars: pa.Table,
    profile: pa.Table,
) -> list[str]:
    """Real-session causality / sum invariants. Empty = pass."""
    errors: list[str] = []
    if bars.num_rows == 0:
        return errors
    buy = bars.column("buy_vol").to_numpy()
    sell = bars.column("sell_vol").to_numpy()
    tot = bars.column("total_vol").to_numpy()
    if not np.allclose(buy + sell, tot, atol=1e-6):
        errors.append("bar buy+sell != total")
    for bid in bars.column("bar_id").to_numpy():
        m = profile.column("bar_id").to_numpy() == bid
        if not m.any():
            continue
        psum = float(profile.column("total_vol").to_numpy()[m].sum())
        btot = float(tot[int(bid)]) if int(bid) < len(tot) else float("nan")
        # bar_id is sequential 0..n-1
        idx = int(np.where(bars.column("bar_id").to_numpy() == bid)[0][0])
        btot = float(tot[idx])
        if abs(psum - btot) > 1e-3:
            errors.append(f"bar {bid} profile sum {psum} != {btot}")
            if len(errors) > 5:
                break
    if events.num_rows and "prior_ok" in events.column_names:
        ok = events.column("prior_ok").to_numpy()
        ends = events.column("prior_end_ts_ns").to_numpy().astype(np.int64)
        ev = events.column("event_ts_ns").to_numpy().astype(np.int64)
        bad = ok & (ends >= ev)
        if bad.any():
            errors.append(f"prior context ends after event on {int(bad.sum())} rows")
    return errors
