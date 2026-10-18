"""
Cumulative Delta on 22-tick range bars — matches discretionary CD setup.

From projectdetails.md:
  22 Tick Range chart CD with 100 SMA (+ Bollinger). Trade with the flow:
  long only when MA rising; short when MA falling; time entries when CD
  pulls back to the MA (e.g. MA down, CD rises up to the MA → short).
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa


def trades_to_range_bars(
    trades: pa.Table,
    range_ticks: int = 22,
    tick_size: float = 1.0,
) -> pa.Table:
    """Build tick-range bars (new bar when high-low >= range_ticks * tick_size).

    For Bookmap extracts where trade ``price`` equals integer ``price_ticks``,
    use tick_size=1.0 (one price unit = one tick). Use 0.25 only if price is
    in index points.
    """
    if trades.num_rows == 0:
        return pa.table({})

    ts = trades.column("ts_ns").to_numpy()
    price = trades.column("price").to_numpy().astype(np.float64)
    size = trades.column("size").to_numpy().astype(np.float64)
    buy = trades.column("is_buy_aggressor").to_numpy().astype(bool)
    order = np.argsort(ts)
    ts, price, size, buy = ts[order], price[order], size[order], buy[order]
    return _range_bars_impl(ts, price, size, buy, range_ticks * tick_size)


def _range_bars_impl(
    ts: np.ndarray,
    price: np.ndarray,
    size: np.ndarray,
    buy: np.ndarray,
    range_pts: float,
) -> pa.Table:
    bar_ts: list[int] = []
    bar_open: list[float] = []
    bar_high: list[float] = []
    bar_low: list[float] = []
    bar_close: list[float] = []
    bar_buy: list[float] = []
    bar_sell: list[float] = []

    o = h = l = c = float(price[0])
    bv = float(size[0]) if buy[0] else 0.0
    sv = 0.0 if buy[0] else float(size[0])

    for i in range(1, len(ts)):
        px = float(price[i])
        if buy[i]:
            bv += float(size[i])
        else:
            sv += float(size[i])
        h = max(h, px)
        l = min(l, px)
        c = px

        if h - l >= range_pts:
            bar_ts.append(int(ts[i]))
            bar_open.append(o)
            bar_high.append(h)
            bar_low.append(l)
            bar_close.append(c)
            bar_buy.append(bv)
            bar_sell.append(sv)
            o = h = l = c = px
            bv = sv = 0.0

    if bv > 0 or sv > 0 or len(bar_ts) == 0:
        bar_ts.append(int(ts[-1]))
        bar_open.append(o)
        bar_high.append(h)
        bar_low.append(l)
        bar_close.append(c)
        bar_buy.append(bv)
        bar_sell.append(sv)

    buy_a = np.asarray(bar_buy, dtype=np.float64)
    sell_a = np.asarray(bar_sell, dtype=np.float64)
    return pa.table({
        "ts_ns": np.asarray(bar_ts, dtype=np.int64),
        "open": np.asarray(bar_open, dtype=np.float64),
        "high": np.asarray(bar_high, dtype=np.float64),
        "low": np.asarray(bar_low, dtype=np.float64),
        "close": np.asarray(bar_close, dtype=np.float64),
        "buy_vol": buy_a,
        "sell_vol": sell_a,
        "delta": buy_a - sell_a,
    })


def cumulative_delta_features(
    range_bars: pa.Table,
    sma_len: int = 100,
    bb_std: float = 2.0,
    slope_lookback: int = 5,
    pullback_frac_of_bb: float = 0.35,
) -> pa.Table:
    """CD, SMA, Bollinger, MA trend, and pullback-to-MA flags per range bar."""
    if range_bars.num_rows == 0:
        return pa.table({})

    delta = range_bars.column("delta").to_numpy().astype(np.float64)
    ts = range_bars.column("ts_ns").to_numpy().astype(np.int64)
    cd = np.cumsum(delta)
    n = len(cd)

    sma = np.full(n, np.nan)
    bb_width = np.full(n, np.nan)
    bb_upper = np.full(n, np.nan)
    bb_lower = np.full(n, np.nan)
    for i in range(n):
        a = max(0, i - sma_len + 1)
        w = cd[a : i + 1]
        if len(w) < max(10, sma_len // 4):
            continue
        m = float(w.mean())
        sd = float(w.std())
        sma[i] = m
        bb_upper[i] = m + bb_std * sd
        bb_lower[i] = m - bb_std * sd
        bb_width[i] = 2 * bb_std * sd

    ma_slope = np.full(n, np.nan)
    ma_trend = np.zeros(n, dtype=np.int8)
    for i in range(n):
        j = i - slope_lookback
        if j < 0 or np.isnan(sma[i]) or np.isnan(sma[j]):
            continue
        ma_slope[i] = sma[i] - sma[j]
        if ma_slope[i] > 0:
            ma_trend[i] = 1
        elif ma_slope[i] < 0:
            ma_trend[i] = -1

    dist = cd - sma
    thr = np.where(np.isfinite(bb_width) & (bb_width > 0), pullback_frac_of_bb * bb_width, np.nan)
    near_ma = np.isfinite(thr) & (np.abs(dist) <= thr)

    # Classic timing (projectdetails):
    #   MA up + CD down to MA → long candidate
    #   MA down + CD up to MA → short candidate
    pullback_long = (ma_trend > 0) & near_ma & (dist <= 0)
    pullback_short = (ma_trend < 0) & near_ma & (dist >= 0)

    return pa.table({
        "ts_ns": ts,
        "delta": delta,
        "cd": cd,
        "cd_sma": sma,
        "cd_bb_upper": bb_upper,
        "cd_bb_lower": bb_lower,
        "cd_bb_width": bb_width,
        "cd_ma_slope": ma_slope,
        "cd_ma_trend": ma_trend,
        "cd_dist_to_ma": dist,
        "cd_near_ma": near_ma.astype(np.int8),
        "cd_pullback_long": pullback_long.astype(np.int8),
        "cd_pullback_short": pullback_short.astype(np.int8),
    })


def annotate_bb_regime(
    cd: pa.Table,
    *,
    pct_lookback: int = 100,
    wide_pct: float = 0.70,
    narrow_pct: float = 0.30,
) -> pa.Table:
    """Add rolling BB-width percentile and regime code.

    regime: +1 wide (momentum), -1 narrow (reversal), 0 mid / unknown.
    Percentile is causal: rank of current width among prior ``pct_lookback`` bars.
    """
    if cd.num_rows == 0:
        return cd
    width = cd.column("cd_bb_width").to_numpy().astype(np.float64)
    n = len(width)
    pct = np.full(n, np.nan)
    regime = np.zeros(n, dtype=np.int8)
    for i in range(n):
        if not np.isfinite(width[i]):
            continue
        a = max(0, i - pct_lookback + 1)
        hist = width[a : i + 1]
        hist = hist[np.isfinite(hist)]
        if len(hist) < max(10, pct_lookback // 5):
            continue
        # fraction of history <= current
        p = float(np.mean(hist <= width[i]))
        pct[i] = p
        if p >= wide_pct:
            regime[i] = 1
        elif p <= narrow_pct:
            regime[i] = -1

    out = cd
    for name in ("cd_bb_width_pct", "cd_bb_regime"):
        if name in out.column_names:
            out = out.drop(name)
    out = out.append_column("cd_bb_width_pct", pa.array(pct, type=pa.float64()))
    out = out.append_column("cd_bb_regime", pa.array(regime, type=pa.int8()))
    return out


def lookup_regime_at(
    cd: pa.Table,
    event_ts_ns: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """As-of join: (bb_width, bb_width_pct, bb_regime) at each event."""
    n = len(event_ts_ns)
    if cd.num_rows == 0 or n == 0:
        return (
            np.full(n, np.nan),
            np.full(n, np.nan),
            np.zeros(n, dtype=np.int8),
        )
    cts = cd.column("ts_ns").to_numpy()
    width = cd.column("cd_bb_width").to_numpy().astype(np.float64)
    pct = (
        cd.column("cd_bb_width_pct").to_numpy().astype(np.float64)
        if "cd_bb_width_pct" in cd.column_names
        else np.full(len(cts), np.nan)
    )
    reg = (
        cd.column("cd_bb_regime").to_numpy().astype(np.int8)
        if "cd_bb_regime" in cd.column_names
        else np.zeros(len(cts), dtype=np.int8)
    )
    idx = np.searchsorted(cts, event_ts_ns, side="right") - 1
    out_w = np.full(n, np.nan)
    out_p = np.full(n, np.nan)
    out_r = np.zeros(n, dtype=np.int8)
    valid = idx >= 0
    out_w[valid] = width[idx[valid]]
    out_p[valid] = pct[idx[valid]]
    out_r[valid] = reg[idx[valid]]
    return out_w, out_p, out_r


def build_cd_series(
    trades: pa.Table,
    range_ticks: int = 22,
    tick_size: float = 1.0,
    sma_len: int = 100,
    slope_lookback: int = 5,
    pullback_frac_of_bb: float = 0.35,
    annotate_regime: bool = True,
    regime_pct_lookback: int = 100,
    wide_pct: float = 0.70,
    narrow_pct: float = 0.30,
) -> pa.Table:
    bars = trades_to_range_bars(trades, range_ticks=range_ticks, tick_size=tick_size)
    cd = cumulative_delta_features(
        bars,
        sma_len=sma_len,
        slope_lookback=slope_lookback,
        pullback_frac_of_bb=pullback_frac_of_bb,
    )
    if annotate_regime and cd.num_rows:
        cd = annotate_bb_regime(
            cd,
            pct_lookback=regime_pct_lookback,
            wide_pct=wide_pct,
            narrow_pct=narrow_pct,
        )
    return cd


def cd_pullback_events(
    cd: pa.Table,
    *,
    cooldown_bars: int = 3,
) -> pa.Table:
    """Primary events: first bar of each CD pullback-to-MA episode.

    Long when MA rising and CD dips to/near MA; short when MA falling and CD
    rises to/near MA. Cooldown skips consecutive pullback bars.
    """
    if cd.num_rows == 0:
        return pa.table({})
    ts = cd.column("ts_ns").to_numpy().astype(np.int64)
    pb_l = cd.column("cd_pullback_long").to_numpy().astype(np.int8)
    pb_s = cd.column("cd_pullback_short").to_numpy().astype(np.int8)
    trend = cd.column("cd_ma_trend").to_numpy().astype(np.int8)
    dist = cd.column("cd_dist_to_ma").to_numpy().astype(np.float64)

    events_ts: list[int] = []
    events_side: list[int] = []
    events_dist: list[float] = []
    last_i = -10**9
    for i in range(len(ts)):
        if i - last_i < cooldown_bars:
            continue
        if pb_l[i] == 1:
            events_ts.append(int(ts[i]))
            events_side.append(1)
            events_dist.append(float(dist[i]) if np.isfinite(dist[i]) else 0.0)
            last_i = i
        elif pb_s[i] == 1:
            events_ts.append(int(ts[i]))
            events_side.append(-1)
            events_dist.append(float(dist[i]) if np.isfinite(dist[i]) else 0.0)
            last_i = i

    if not events_ts:
        return pa.table({})
    return pa.table({
        "event_ts_ns": np.asarray(events_ts, dtype=np.int64),
        "side": np.asarray(events_side, dtype=np.int8),
        "event_type": ["cd_pullback"] * len(events_ts),
        "cd_dist_to_ma": np.asarray(events_dist, dtype=np.float64),
        "cd_ma_trend": np.asarray(
            [int(trend[np.searchsorted(ts, t, side="right") - 1]) for t in events_ts],
            dtype=np.int8,
        ),
    })


def filter_events_near_bursts(
    events: pa.Table,
    bursts: pa.Table,
    *,
    window_ms: int = 2000,
    require_same_side: bool = True,
    causal: bool = True,
) -> pa.Table:
    """Keep events that have a same-side (optional) burst nearby.

    When ``causal`` (default), only bursts at/after the event count, and
    ``event_ts_ns`` is stamped at ``max(event, burst)`` (tradable confirm time).
    Non-causal ±window is available with ``causal=False`` for digs only.
    """
    if events.num_rows == 0 or bursts.num_rows == 0:
        return pa.table({})
    ev_ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    ev_side = events.column("side").to_numpy()
    b_ts = bursts.column("event_ts_ns").to_numpy().astype(np.int64)
    b_side = bursts.column("side").to_numpy()
    order = np.argsort(b_ts)
    b_ts, b_side = b_ts[order], b_side[order]
    window = int(window_ms * 1_000_000)
    keep = []
    lags = []
    confirm_ts = []
    for i in range(len(ev_ts)):
        if causal:
            lo = np.searchsorted(b_ts, ev_ts[i], side="left")
            hi = np.searchsorted(b_ts, ev_ts[i] + window, side="right")
        else:
            lo = np.searchsorted(b_ts, ev_ts[i] - window, side="left")
            hi = np.searchsorted(b_ts, ev_ts[i] + window, side="right")
        best_lag = None
        best_bts = None
        for j in range(lo, hi):
            if require_same_side and int(b_side[j]) != int(ev_side[i]):
                continue
            lag = abs(int(b_ts[j] - ev_ts[i]))
            if best_lag is None or lag < best_lag:
                best_lag = lag
                best_bts = int(b_ts[j])
        if best_lag is not None and best_bts is not None:
            keep.append(i)
            lags.append(best_lag / 1e6)
            confirm_ts.append(max(int(ev_ts[i]), best_bts) if causal else int(ev_ts[i]))
    if not keep:
        return pa.table({})
    idx = np.asarray(keep, dtype=np.int64)
    out = events.take(idx)
    if causal:
        # stamp = last input used (burst confirm)
        out = out.set_column(
            out.schema.get_field_index("event_ts_ns"),
            "event_ts_ns",
            pa.array(confirm_ts, type=pa.int64()),
        )
    if "burst_lag_ms" in out.column_names:
        out = out.drop("burst_lag_ms")
    return out.append_column("burst_lag_ms", pa.array(lags, type=pa.float64()))


def lookup_cd_at_events(cd: pa.Table, event_ts_ns: np.ndarray) -> dict[str, np.ndarray]:
    """As-of join: last CD bar with ts <= event_ts."""
    if cd.num_rows == 0 or len(event_ts_ns) == 0:
        return {}
    cts = cd.column("ts_ns").to_numpy()
    cols = [
        "cd", "cd_sma", "cd_ma_trend", "cd_ma_slope", "cd_dist_to_ma",
        "cd_near_ma", "cd_pullback_long", "cd_pullback_short", "cd_bb_width",
    ]
    if "cd_bb_width_pct" in cd.column_names:
        cols.append("cd_bb_width_pct")
    if "cd_bb_regime" in cd.column_names:
        cols.append("cd_bb_regime")
    idx = np.searchsorted(cts, event_ts_ns, side="right") - 1
    valid = idx >= 0
    out: dict[str, np.ndarray] = {}
    for name in cols:
        src = cd.column(name).to_numpy()
        arr = np.full(len(event_ts_ns), np.nan, dtype=np.float64)
        arr[valid] = src[idx[valid]].astype(np.float64)
        out[name] = arr
    return out


def annotate_dual_cd_fields(
    events: pa.Table,
    nq_cd: pa.Table,
    es_cd: pa.Table,
) -> pa.Table:
    """Append NQ/ES CD trend + BB regime and dual-alignment flags to events.

    Flags (int8 0/1):
      nq_trend_aligned, es_trend_aligned, dual_trend_aligned
      nq_wide, es_wide, dual_wide
      dual_same_regime (both non-zero and equal)
      dual_trend_and_wide
    """
    if events.num_rows == 0:
        return events
    ts = events.column("event_ts_ns").to_numpy().astype(np.int64)
    side = events.column("side").to_numpy().astype(np.int8)
    nq = lookup_cd_at_events(nq_cd, ts)
    es = lookup_cd_at_events(es_cd, ts)

    n = len(ts)
    nq_trend = nq.get("cd_ma_trend", np.full(n, np.nan))
    es_trend = es.get("cd_ma_trend", np.full(n, np.nan))
    nq_reg = nq.get("cd_bb_regime", np.zeros(n))
    es_reg = es.get("cd_bb_regime", np.zeros(n))
    nq_pct = nq.get("cd_bb_width_pct", np.full(n, np.nan))
    es_pct = es.get("cd_bb_width_pct", np.full(n, np.nan))

    nq_aligned = (
        ((side > 0) & (nq_trend > 0)) | ((side < 0) & (nq_trend < 0))
    ).astype(np.int8)
    es_aligned = (
        ((side > 0) & (es_trend > 0)) | ((side < 0) & (es_trend < 0))
    ).astype(np.int8)
    dual_trend = ((nq_aligned == 1) & (es_aligned == 1)).astype(np.int8)

    nq_wide = (nq_reg == 1).astype(np.int8)
    es_wide = (es_reg == 1).astype(np.int8)
    dual_wide = ((nq_wide == 1) & (es_wide == 1)).astype(np.int8)
    dual_same = (
        (nq_reg != 0) & (es_reg != 0) & (nq_reg == es_reg)
    ).astype(np.int8)
    dual_trend_wide = ((dual_trend == 1) & (dual_wide == 1)).astype(np.int8)

    out = events
    cols = {
        "nq_cd_ma_trend": (nq_trend, pa.float64()),
        "es_cd_ma_trend": (es_trend, pa.float64()),
        "nq_cd_bb_regime": (nq_reg, pa.float64()),
        "es_cd_bb_regime": (es_reg, pa.float64()),
        "nq_cd_bb_width_pct": (nq_pct, pa.float64()),
        "es_cd_bb_width_pct": (es_pct, pa.float64()),
        "nq_trend_aligned": (nq_aligned, pa.int8()),
        "es_trend_aligned": (es_aligned, pa.int8()),
        "dual_trend_aligned": (dual_trend, pa.int8()),
        "nq_wide": (nq_wide, pa.int8()),
        "es_wide": (es_wide, pa.int8()),
        "dual_wide": (dual_wide, pa.int8()),
        "dual_same_regime": (dual_same, pa.int8()),
        "dual_trend_and_wide": (dual_trend_wide, pa.int8()),
    }
    for name, (arr, typ) in cols.items():
        if name in out.column_names:
            out = out.drop(name)
        out = out.append_column(name, pa.array(arr, type=typ))
    return out


def cd_gate_mask(
    side: np.ndarray,
    cd_fields: dict[str, np.ndarray],
    mode: str = "trend_and_pullback",
) -> np.ndarray:
    """Filter events to with-the-flow CD states."""
    trend = cd_fields["cd_ma_trend"]
    pb_l = cd_fields["cd_pullback_long"]
    pb_s = cd_fields["cd_pullback_short"]
    long_ev = side > 0
    short_ev = side < 0

    if mode == "trend_only":
        return (long_ev & (trend > 0)) | (short_ev & (trend < 0))
    # pullback flags already require MA trend direction
    return (long_ev & (pb_l > 0)) | (short_ev & (pb_s > 0))
