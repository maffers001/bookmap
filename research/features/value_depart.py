"""
Port of trade-processor S20 (VPOC departure) and S4 (HVN break-hold)
onto Bookmap trade extracts: 22-tick range bars + causal developing POC/HVN.

Source: C:\\Work\\Mat\\trade-processor\\src\\nq_cd_tester.py
  signals_s20_vpoc_depart / signals_s4_hvn_break_hold
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from research.features.cd import cumulative_delta_features, trades_to_range_bars
from research.features.volume_profile import build_profile_at, _price_to_tick


@dataclass
class ValueDepartConfig:
    range_ticks: int = 22
    tick_size: float = 1.0
    lookback_ms: int = 1_800_000  # developing profile window
    near_bars_ago: int = 5
    min_clearance: float = 4.0  # frozen from trade-processor S20
    near_band: float = 8.0  # |close-vpoc| 5 bars ago; ~0.05*MPD proxy
    impulse_pct: float = 0.80  # causal rolling percentile of |delta|
    impulse_lookback_bars: int = 100
    sma_len: int = 100
    slope_lookback: int = 5
    require_cd_regime: bool = True
    require_price_confirm: bool = True
    price_confirm_bars: int = 3
    cooldown_bars: int = 2


def _developing_poc_on_bars(
    trades: pa.Table,
    bar_ts: np.ndarray,
    *,
    lookback_ms: int,
    tick_size: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Causal POC (+ secondary HVN peak) at each range-bar close."""
    if trades.num_rows == 0 or len(bar_ts) == 0:
        n = len(bar_ts)
        return np.full(n, np.nan), np.full(n, np.nan)

    ts = trades.column("ts_ns").to_numpy().astype(np.int64)
    px = _price_to_tick(trades.column("price").to_numpy().astype(np.float64), tick_size)
    sz = trades.column("size").to_numpy().astype(np.float64)
    order = np.argsort(ts)
    ts, px, sz = ts[order], px[order], sz[order]
    lookback_ns = int(lookback_ms * 1_000_000)

    poc = np.full(len(bar_ts), np.nan)
    hvn2 = np.full(len(bar_ts), np.nan)
    for i, asof in enumerate(bar_ts):
        ticks, vols, p = build_profile_at(ts, px, sz, int(asof), lookback_ns)
        if p is None or len(ticks) == 0:
            continue
        poc[i] = float(p)
        # Secondary HVN: next-highest local peak away from POC
        if len(vols) >= 3:
            # local maxima
            peaks = []
            for j in range(1, len(vols) - 1):
                if vols[j] >= vols[j - 1] and vols[j] >= vols[j + 1]:
                    peaks.append((float(vols[j]), int(ticks[j])))
            peaks.sort(reverse=True)
            for _v, t in peaks:
                if abs(t - p) >= 4:
                    hvn2[i] = float(t)
                    break
            if np.isnan(hvn2[i]) and len(peaks) > 1:
                hvn2[i] = float(peaks[1][1])
        if np.isnan(hvn2[i]):
            hvn2[i] = float(p)
    return poc, hvn2


def build_value_features(
    trades: pa.Table,
    cfg: ValueDepartConfig | None = None,
) -> pa.Table:
    """22-tick OHLC + CD + developing VPOC/HVN1 building blocks."""
    cfg = cfg or ValueDepartConfig()
    bars = trades_to_range_bars(trades, range_ticks=cfg.range_ticks, tick_size=cfg.tick_size)
    if bars.num_rows < max(20, cfg.near_bars_ago + 5):
        return pa.table({})

    cd = cumulative_delta_features(
        bars,
        sma_len=cfg.sma_len,
        slope_lookback=cfg.slope_lookback,
    )
    bar_ts = bars.column("ts_ns").to_numpy().astype(np.int64)
    o = bars.column("open").to_numpy().astype(np.float64)
    h = bars.column("high").to_numpy().astype(np.float64)
    l = bars.column("low").to_numpy().astype(np.float64)
    c = bars.column("close").to_numpy().astype(np.float64)
    delta = bars.column("delta").to_numpy().astype(np.float64)

    poc, hvn2 = _developing_poc_on_bars(
        trades, bar_ts, lookback_ms=cfg.lookback_ms, tick_size=cfg.tick_size
    )
    # HVN1 = primary value node = POC (matches auction theory; export HVN1 ≈ node)
    hvn1 = poc.copy()

    cd_sign = np.sign(delta).astype(np.int8)
    cd_abs = np.abs(delta)
    # Causal impulse threshold: rolling percentile of prior |delta|
    impulse_thr = np.full(len(delta), np.nan)
    for i in range(len(delta)):
        a = max(0, i - cfg.impulse_lookback_bars)
        w = cd_abs[a:i]  # exclude current
        if len(w) < 20:
            continue
        impulse_thr[i] = float(np.quantile(w, cfg.impulse_pct))

    ma_trend = cd.column("cd_ma_trend").to_numpy().astype(np.int8)
    # Price confirms regime: net close move over last N bars matches MA trend
    price_conf = np.zeros(len(c), dtype=np.int8)
    n = cfg.price_confirm_bars
    for i in range(n, len(c)):
        net = c[i] - c[i - n]
        if ma_trend[i] > 0 and net > 0:
            price_conf[i] = 1
        elif ma_trend[i] < 0 and net < 0:
            price_conf[i] = 1

    # Distances
    dist_poc = np.abs(c - poc)
    near_ago = cfg.near_bars_ago
    near_prev = np.zeros(len(c), dtype=bool)
    for i in range(near_ago, len(c)):
        if np.isfinite(poc[i - near_ago]) and np.isfinite(c[i - near_ago]):
            near_prev[i] = abs(c[i - near_ago] - poc[i - near_ago]) <= cfg.near_band

    held_up = np.isfinite(poc) & (l > poc * 0.999) & (c > poc)
    held_dn = np.isfinite(poc) & (h < poc * 1.001) & (c < poc)
    cleared = np.isfinite(dist_poc) & (dist_poc >= cfg.min_clearance)

    broke_hvn_up = np.zeros(len(c), dtype=bool)
    broke_hvn_dn = np.zeros(len(c), dtype=bool)
    for i in range(near_ago, len(c)):
        if not np.isfinite(hvn1[i]) or not np.isfinite(hvn1[i - near_ago]):
            continue
        broke_hvn_up[i] = (
            c[i] > hvn1[i]
            and l[i] > hvn1[i] * 0.999
            and c[i - near_ago] <= hvn1[i - near_ago]
        )
        broke_hvn_dn[i] = (
            c[i] < hvn1[i]
            and h[i] < hvn1[i] * 1.001
            and c[i - near_ago] >= hvn1[i - near_ago]
        )

    return pa.table({
        "ts_ns": bar_ts,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "delta": delta,
        "cd_sign": cd_sign,
        "cd_abs_body": cd_abs,
        "impulse_thr": impulse_thr,
        "cd_ma_trend": ma_trend,
        "price_confirms": price_conf,
        "dVPOC": poc,
        "HVN1": hvn1,
        "HVN2": hvn2,
        "near_vpoc_5ago": near_prev.astype(np.int8),
        "held_above_vpoc": held_up.astype(np.int8),
        "held_below_vpoc": held_dn.astype(np.int8),
        "cleared_vpoc": cleared.astype(np.int8),
        "broke_hvn_up": broke_hvn_up.astype(np.int8),
        "broke_hvn_dn": broke_hvn_dn.astype(np.int8),
        "dist_vpoc": dist_poc,
    })


def value_depart_events(
    feat: pa.Table,
    cfg: ValueDepartConfig | None = None,
    *,
    kind: str = "s20",
) -> pa.Table:
    """Emit continuation events: kind='s20' (VPOC depart) or 's4' (HVN break-hold)."""
    cfg = cfg or ValueDepartConfig()
    if feat.num_rows == 0:
        return pa.table({})

    ts = feat.column("ts_ns").to_numpy().astype(np.int64)
    cd_sign = feat.column("cd_sign").to_numpy()
    cd_abs = feat.column("cd_abs_body").to_numpy()
    thr = feat.column("impulse_thr").to_numpy()
    ma = feat.column("cd_ma_trend").to_numpy()
    pconf = feat.column("price_confirms").to_numpy()

    impulse = np.isfinite(thr) & (cd_abs >= thr)

    if kind == "s20":
        near = feat.column("near_vpoc_5ago").to_numpy() == 1
        held_up = feat.column("held_above_vpoc").to_numpy() == 1
        held_dn = feat.column("held_below_vpoc").to_numpy() == 1
        cleared = feat.column("cleared_vpoc").to_numpy() == 1
        long_trig = near & held_up & cleared & impulse & (cd_sign > 0)
        short_trig = near & held_dn & cleared & impulse & (cd_sign < 0)
        etype = "vpoc_depart"
    elif kind == "s4":
        bu = feat.column("broke_hvn_up").to_numpy() == 1
        bd = feat.column("broke_hvn_dn").to_numpy() == 1
        long_trig = bu & impulse & (cd_sign > 0)
        short_trig = bd & impulse & (cd_sign < 0)
        etype = "hvn_break_hold"
    else:
        raise ValueError(f"unknown kind {kind}")

    if cfg.require_cd_regime:
        long_trig = long_trig & (ma > 0)
        short_trig = short_trig & (ma < 0)
    if cfg.require_price_confirm:
        long_trig = long_trig & (pconf == 1)
        short_trig = short_trig & (pconf == 1)

    events = []
    last_i = -10**9
    for i in range(len(ts)):
        if i - last_i < cfg.cooldown_bars:
            continue
        if long_trig[i] and not short_trig[i]:
            events.append((int(ts[i]), 1, etype, float(cd_abs[i]), float(feat.column("dist_vpoc")[i].as_py()) if "dist_vpoc" in feat.column_names else 0.0))
            last_i = i
        elif short_trig[i] and not long_trig[i]:
            events.append((int(ts[i]), -1, etype, float(cd_abs[i]), float(feat.column("dist_vpoc")[i].as_py()) if "dist_vpoc" in feat.column_names else 0.0))
            last_i = i

    if not events:
        return pa.table({})
    return pa.table({
        "event_ts_ns": np.array([e[0] for e in events], dtype=np.int64),
        "side": np.array([e[1] for e in events], dtype=np.int8),
        "event_type": [e[2] for e in events],
        "cd_abs_body": np.array([e[3] for e in events], dtype=np.float64),
        "dist_vpoc": np.array([e[4] for e in events], dtype=np.float64),
    })
