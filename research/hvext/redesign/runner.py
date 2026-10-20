"""Common Sep/Oct runner for EXP-0038 redesign stages."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa

from research.backtest.mat_sim import MatSimConfig, SimSummary, simulate_session
from research.features.hv_extreme import HvExtremeConfig
from research.hvext.redesign.enrich import enriched_hv_events, filter_events
from research.run_experiment import _load_trades, _random_null_events, load_config

UK = ZoneInfo("Europe/London")

SEP = [
    "010926",
    "020926",
    "040926",
    "080926",
    "100926",
    "150926",
    "160926",
    "170926",
    "180926",
    "210926",
    "220926",
    "250926",
    "280926",
    "290926",
    "300926",
]
OCT = [
    "011026",
    "021026",
    "051026",
    "061026",
    "071026",
    "081026",
    "091026",
    "121026",
    "131026",
    "141026",
    "151026",
    "161026",
    "191026",
]
ALL = SEP + OCT

BASELINE_BOTH_NET = -404.0

PANEL = HvExtremeConfig(
    move_pts=20,
    confirm_pts=10,
    peak_band_pts=5,
    high_vol_mult=2.0,
    min_path_efficiency=0.0,
    max_pullback_pts=5.0,
    max_leg_sec=0.0,
    max_leg_vol_vs_avg=0.0,
    max_leg_vol_vs_tip=0.0,
    lookback_min=15,
    cooldown_sec=30,
)


def results_dir() -> Path:
    p = Path("results/EXP-0038")
    p.mkdir(parents=True, exist_ok=True)
    return p


def parquet_dir() -> Path:
    return Path(load_config()["parquet_dir"])


def default_sim(
    *,
    time_stop_sec: float = 0.0,
    be_trigger_pts: float = 0.0,
    trail_pts: float = 0.0,
) -> MatSimConfig:
    return MatSimConfig(
        signal_cooldown_sec=30,
        slip_ticks=0,
        flat_only=True,
        commission_pts_rt=0.25,
        pts_per_tick=0.25,
        time_stop_sec=time_stop_sec,
        be_trigger_pts=be_trigger_pts,
        trail_pts=trail_pts,
    )


def fixed_bracket(_sv) -> tuple[float, float]:
    return 30.0, 10.0


def pack(trades: list) -> dict:
    s = SimSummary()
    equity = peak = 0.0
    days: dict[str, float] = {}
    day_n: dict[str, int] = {}
    for t in trades:
        s.trades.append(t)
        s.n += 1
        if t.win:
            s.wins += 1
            s.gross_win += max(t.pnl, 0.0)
        else:
            s.gross_loss += abs(min(t.pnl, 0.0))
            if t.pnl > 0:
                s.gross_win += t.pnl
        s.gross += t.pnl
        net = t.pnl - 0.25
        s.commission_total += 0.25
        s.net += net
        equity += net
        peak = max(peak, equity)
        s.max_dd = max(s.max_dd, peak - equity)
        days[t.session_id] = days.get(t.session_id, 0.0) + net
        day_n[t.session_id] = day_n.get(t.session_id, 0) + 1
    d = s.to_dict()
    d["n_sessions"] = len(days)
    d["days_pos"] = sum(1 for v in days.values() if v > 0)
    d["days_pos_frac"] = (d["days_pos"] / len(days)) if days else float("nan")
    d["n_long"] = sum(1 for t in trades if t.side > 0)
    d["n_short"] = sum(1 for t in trades if t.side < 0)
    d["net_long"] = round(sum((t.pnl - 0.25) for t in trades if t.side > 0), 2)
    d["net_short"] = round(sum((t.pnl - 0.25) for t in trades if t.side < 0), 2)
    d["per_session"] = {k: round(v, 2) for k, v in sorted(days.items())}
    d["per_session_n"] = {k: day_n[k] for k in sorted(day_n)}
    return d


def uk_hour_min(ts_ns: int) -> tuple[int, int]:
    dt = datetime.fromtimestamp(ts_ns / 1e9, tz=UK)
    return dt.hour, dt.minute


def in_uk_skip(ts_ns: int) -> bool:
    """Skip UK 14:00–15:30."""
    h, m = uk_hour_min(ts_ns)
    mins = h * 60 + m
    return 14 * 60 <= mins < 15 * 60 + 30


# ---- event cache ----

_CACHE: dict[tuple, pa.Table] = {}
_TRADE_CACHE: dict[tuple[str, str], pa.Table] = {}


def load_nq(sid: str, pdir: Path | None = None) -> pa.Table:
    pdir = pdir or parquet_dir()
    key = (str(pdir), sid)
    if key not in _TRADE_CACHE:
        _TRADE_CACHE[key] = _load_trades(pdir, sid, "NQ")
    return _TRADE_CACHE[key]


def load_es(sid: str, pdir: Path | None = None) -> pa.Table:
    pdir = pdir or parquet_dir()
    key = (str(pdir), f"{sid}|ES")
    if key not in _TRADE_CACHE:
        _TRADE_CACHE[key] = _load_trades(pdir, sid, "ES")
    return _TRADE_CACHE[key]


def cfg_key(cfg: HvExtremeConfig) -> tuple:
    return (
        cfg.move_pts,
        cfg.confirm_pts,
        cfg.peak_band_pts,
        cfg.high_vol_mult,
        cfg.min_path_efficiency,
        cfg.max_pullback_pts,
        cfg.max_leg_sec,
        cfg.max_leg_vol_vs_avg,
        cfg.max_leg_vol_vs_tip,
        cfg.lookback_min,
        cfg.cooldown_sec,
        cfg.pts_per_tick,
    )


def cached_events(sid: str, cfg: HvExtremeConfig, *, enrich: bool = True) -> pa.Table:
    key = (sid, cfg_key(cfg), enrich)
    if key in _CACHE:
        return _CACHE[key]
    nq = load_nq(sid)
    if nq.num_rows == 0:
        ev = pa.table({})
    elif enrich:
        ev = enriched_hv_events(nq, cfg, attach_med_r30=True)
    else:
        from research.features.hv_extreme import hv_extreme_events

        ev = hv_extreme_events(nq, cfg, attach_med_r30=True)
    _CACHE[key] = ev
    return ev


def clear_cache() -> None:
    _CACHE.clear()


# ---- retest entry ----

@dataclass
class RetestParams:
    retest_pts: float = 8.0
    timeout_sec: float = 120.0
    stop_beyond_pts: float = 2.0
    tp_mult: float = 3.0  # × risk; 0 = use fixed_tp
    fixed_tp: float = 30.0
    weak_retest_frac: float = 0.0  # 0 = off; else retest band vol <= frac * tip vol
    pts_per_tick: float = 0.25


def find_retest_entries(
    nq: pa.Table,
    fires: pa.Table,
    rp: RetestParams,
    cfg: HvExtremeConfig,
) -> pa.Table:
    """Convert fires into retest entries (or drop if no fill within timeout)."""
    if fires.num_rows == 0 or nq.num_rows == 0:
        return pa.table({})

    ts = nq.column("ts_ns").to_numpy().astype(np.int64)
    px = np.round(nq.column("price").to_numpy().astype(np.float64)).astype(np.int64)
    sz = nq.column("size").to_numpy().astype(np.float64)
    order = np.argsort(ts, kind="stable")
    ts, px, sz = ts[order], px[order], sz[order]

    half = cfg.band_ticks()
    r_ticks = max(1, int(round(rp.retest_pts / rp.pts_per_tick)))
    stop_ticks = max(1, int(round(rp.stop_beyond_pts / rp.pts_per_tick)))
    timeout_ns = int(rp.timeout_sec * 1e9)

    rows: list[dict] = []
    fire_ts = fires.column("event_ts_ns").to_numpy().astype(np.int64)
    sides = fires.column("side").to_numpy().astype(np.int8)
    extremes = fires.column("extreme_tick").to_numpy().astype(np.int64)
    tip_vols = fires.column("extreme_vol").to_numpy().astype(np.float64)

    for i in range(len(fire_ts)):
        t0 = int(fire_ts[i])
        side = int(sides[i])
        tip = int(extremes[i])
        tip_vol = float(tip_vols[i])
        start = int(np.searchsorted(ts, t0, side="right"))
        deadline = t0 + timeout_ns
        end = int(np.searchsorted(ts, deadline, side="right"))

        filled = False
        retest_vol = 0.0
        for j in range(start, end):
            tick = int(px[j])
            # invalidate if trade through tip
            if side < 0 and tick > tip:
                break
            if side > 0 and tick < tip:
                break
            in_band = abs(tick - tip) <= r_ticks
            if in_band:
                retest_vol += float(sz[j])
                if half >= 0 and abs(tick - tip) <= half:
                    pass
                if rp.weak_retest_frac > 0 and tip_vol > 0:
                    # accumulate until we decide; check at first touch of R
                    if retest_vol > rp.weak_retest_frac * tip_vol:
                        break
                # enter on first print within R of tip
                entry_ns = int(ts[j])
                # stop beyond tip
                if side < 0:
                    stop_tick = tip + stop_ticks
                    risk_pts = abs((tick - stop_tick) * rp.pts_per_tick)
                else:
                    stop_tick = tip - stop_ticks
                    risk_pts = abs((tick - stop_tick) * rp.pts_per_tick)
                risk_pts = max(rp.pts_per_tick, risk_pts)
                if rp.tp_mult > 0:
                    tp_pts = rp.tp_mult * risk_pts
                else:
                    tp_pts = rp.fixed_tp
                sl_pts = risk_pts
                row = {c: fires.column(c)[i].as_py() for c in fires.column_names}
                row["event_ts_ns"] = entry_ns
                row["fire_ts_ns"] = t0
                row["retest_risk_pts"] = risk_pts
                row["retest_tp_pts"] = tp_pts
                row["retest_sl_pts"] = sl_pts
                row["retest_vol"] = retest_vol
                rows.append(row)
                filled = True
                break
        if not filled:
            continue

    if not rows:
        return pa.table({})
    cols: dict = {}
    keys = rows[0].keys()
    for k in keys:
        vals = [r[k] for r in rows]
        if k in ("event_ts_ns", "fire_ts_ns", "extreme_tick", "trigger_tick", "arm_ns"):
            cols[k] = pa.array(vals, type=pa.int64())
        elif k == "side":
            cols[k] = pa.array(vals, type=pa.int8())
        elif k == "event_type":
            cols[k] = pa.array(vals, type=pa.string())
        elif isinstance(vals[0], str):
            cols[k] = pa.array(vals, type=pa.string())
        else:
            cols[k] = pa.array(vals, type=pa.float64())
    return pa.table(cols)


# ---- run variant ----

def run_month(
    sessions: list[str],
    cfg: HvExtremeConfig,
    *,
    post_filter=None,
    entry: str = "extension",
    retest: RetestParams | None = None,
    sim_cfg: MatSimConfig | None = None,
    bracket_fn=None,
    enrich: bool = True,
    pdir: Path | None = None,
) -> tuple[dict, list]:
    """Returns (pack dict, trade list).

    For ``entry="retest"``: if ``bracket_fn`` is passed, it overrides tip-risk
    TP/SL (used for fixed/vol asymmetric sweeps). If omitted, TP/SL come from
    ``RetestParams`` (risk ≈ retest_pts + stop_beyond, TP = tp_mult × risk).
    """
    pdir = pdir or parquet_dir()
    sim_cfg = sim_cfg or default_sim()
    user_bracket = bracket_fn
    bracket_fn = bracket_fn or fixed_bracket
    all_trades = []
    n_fires = 0
    n_entries = 0
    for sid in sessions:
        nq = load_nq(sid, pdir)
        if nq.num_rows == 0:
            continue
        ev = cached_events(sid, cfg, enrich=enrich)
        n_fires += ev.num_rows
        if ev.num_rows == 0:
            continue
        if post_filter is not None:
            mask = post_filter(ev)
            ev = filter_events(ev, mask)
        if ev.num_rows == 0:
            continue
        if entry == "retest":
            rp = retest or RetestParams()
            ev = find_retest_entries(nq, ev, rp, cfg)
            if ev.num_rows == 0:
                continue
            if user_bracket is not None:
                bfn = user_bracket
            else:
                # Tip-risk brackets from RetestParams
                risk = rp.retest_pts + rp.stop_beyond_pts
                tp = rp.tp_mult * risk if rp.tp_mult > 0 else rp.fixed_tp
                sl = risk
                bfn = lambda _sv, tp=tp, sl=sl: (tp, sl)
            s = simulate_session(
                sid,
                nq,
                pa.table({}),
                sim_cfg,
                bracket_fn=bfn,
                events=ev,
                vol_window_min=30,
            )
            all_trades.extend(s.trades)
            n_entries += ev.num_rows
            continue

        n_entries += ev.num_rows
        s = simulate_session(
            sid,
            nq,
            pa.table({}),
            sim_cfg,
            bracket_fn=bracket_fn,
            events=ev,
            vol_window_min=30,
        )
        all_trades.extend(s.trades)
    summary = pack(all_trades)
    summary["n_fires"] = n_fires
    summary["n_entries"] = n_entries
    if entry == "retest" and n_fires > 0:
        summary["fill_rate"] = n_entries / n_fires
    return summary, all_trades


def run_both(
    cfg: HvExtremeConfig,
    *,
    post_filter=None,
    entry: str = "extension",
    retest: RetestParams | None = None,
    sim_cfg: MatSimConfig | None = None,
    bracket_fn=None,
    label: str = "",
) -> dict:
    sep, sep_tr = run_month(
        SEP, cfg, post_filter=post_filter, entry=entry, retest=retest,
        sim_cfg=sim_cfg, bracket_fn=bracket_fn,
    )
    oct_, oct_tr = run_month(
        OCT, cfg, post_filter=post_filter, entry=entry, retest=retest,
        sim_cfg=sim_cfg, bracket_fn=bracket_fn,
    )
    both_tr = sep_tr + oct_tr
    both = pack(both_tr)
    both["n_fires"] = sep.get("n_fires", 0) + oct_.get("n_fires", 0)
    both["n_entries"] = sep.get("n_entries", 0) + oct_.get("n_entries", 0)
    if both["n_fires"]:
        both["fill_rate"] = both["n_entries"] / both["n_fires"]
    return {
        "label": label,
        "sep": sep,
        "oct": oct_,
        "both": both,
        "both_net": both.get("net"),
    }


def null_month(sessions: list[str], n_per: int, seed: int = 42) -> dict:
    sim_cfg = default_sim()
    all_trades = []
    rng = np.random.default_rng(seed)
    for sid in sessions:
        nq = load_nq(sid)
        if nq.num_rows == 0:
            continue
        ev = _random_null_events(
            nq, n_per, seed=int(rng.integers(0, 1_000_000))
        )
        if ev.num_rows == 0:
            continue
        s = simulate_session(
            sid, nq, pa.table({}), sim_cfg, bracket_fn=fixed_bracket, events=ev,
            vol_window_min=30,
        )
        all_trades.extend(s.trades)
    return pack(all_trades)


def apply_overlays(trades: list) -> dict:
    """Cheap overlays on an already-simulated trade list.

    Trades don't carry med_r30 from fires reliably for post-filter of entries —
    TradeResult has med_r30 from regime at entry. UK skip uses entry_ts_ns.
    """
    out = {}
    # skip UK 14:00-15:30
    keep = [t for t in trades if not in_uk_skip(t.entry_ts_ns)]
    out["skip_uk_1400_1530"] = pack(keep)
    # medR30 >= 12
    keep2 = [t for t in trades if (t.med_r30 == t.med_r30 and t.med_r30 >= 12)]
    out["medR30_ge_12"] = pack(keep2)
    out["long_only"] = pack([t for t in trades if t.side > 0])
    out["short_only"] = pack([t for t in trades if t.side < 0])
    return out


def verdict(row: dict, baseline_sep: float = -342.0, baseline_oct: float = -62.0) -> str:
    sep, oct_ = row["sep"], row["oct"]
    sn, on = sep.get("n") or 0, oct_.get("n") or 0
    if sn < 25 or on < 25:
        tag = "thin"
    elif sn < 40 or on < 40:
        tag = "thinish"
    else:
        tag = "ok_n"
    s_net, o_net = sep.get("net") or 0, oct_.get("net") or 0
    s_pf, o_pf = sep.get("pf") or 0, oct_.get("pf") or 0
    # pf None when n=0
    s_pf = s_pf if s_pf is not None else 0
    o_pf = o_pf if o_pf is not None else 0
    improved = (s_net > baseline_sep and o_net > baseline_oct)
    # need PF improve too vs baseline roughly — baseline PF unknown; require both nets up and both PF>=1 or improve
    if improved and sn >= 40 and on >= 40 and s_net > 0 and o_net > 0:
        return "pass"
    if improved and sn >= 25 and on >= 25:
        return f"improve_{tag}"
    return f"fail_{tag}"


def write_json(name: str, payload: dict) -> Path:
    dest = results_dir() / name
    dest.write_text(json.dumps(payload, indent=2, default=str))
    return dest


def append_summary(section: str) -> None:
    path = results_dir() / "summary.md"
    if not path.exists():
        path.write_text(
            "# EXP-0038 HV Extreme Redesign\n\n"
            "Baseline panel Move20/C10/B5/HV2/pb5 SL10/TP30: "
            "Sep -342 (n=127), Oct -62 (n=209), both -404.\n\n",
            encoding="utf-8",
        )
    with path.open("a", encoding="utf-8") as f:
        f.write(section)
        if not section.endswith("\n"):
            f.write("\n")


def fmt_cell(row: dict) -> str:
    sep, oct_, both = row["sep"], row["oct"], row.get("both") or {}
    return (
        f"{row.get('label','?'):40s} "
        f"Sep n={sep.get('n')} net={sep.get('net'):+g} pf={sep.get('pf')}  "
        f"Oct n={oct_.get('n')} net={oct_.get('net'):+g} pf={oct_.get('pf')}  "
        f"Both={row.get('both_net')}"
    )
