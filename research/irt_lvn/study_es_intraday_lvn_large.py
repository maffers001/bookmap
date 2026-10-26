"""Intraday fresh large ES orders near LVNs → first-touch fade (Oct).

Unlike the open-snapshot book, this arms whenever a **new** large resting size
appears during RTH that is:
  - within ``near_lvn_pts`` of as-of LVN2/LVN3,
  - at least ``min_dist_pts`` from mid at birth (not already in the fray),
  - at a price not yet traded through since RTH open,

then fades the **first touch** with the frozen confluence rules:
  limit ``offset`` pts outside, SL/TP fixed (default 8/16).

    .venv\\Scripts\\python.exe -m research.irt_lvn.study_es_intraday_lvn_large
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from research.config import ROOT, load_config
from research.correlated.harness import _prefer_single_feed, list_sessions
from research.irt_lvn.events import PTS_PER_TICK, _active_levels, _asof_idx
from research.irt_lvn.load_irt import KIND_LVN, default_irt_paths, load_irt_csv, session_id_to_ymd
from research.irt_lvn.open_book import _clip, vol_unit_preopen
from research.irt_lvn.run_exp_0042 import validate_timezone
from research.irt_lvn.run_hold_break_of import basis_offset, bounds, filt, load_depth_window_with_book
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")


def _hhmm_ns(y: int, m: int, d: int, hhmm: str) -> int:
    hh, mm = (int(x) for x in hhmm.split(":")[:2])
    return int(datetime(y, m, d, hh, mm, tzinfo=UK).timestamp() * 1e9)


def _shift_bars(bars, shift: float):
    if shift == 0.0:
        return bars
    bars.lvn2 = bars.lvn2 + shift
    bars.lvn3 = bars.lvn3 + shift
    bars.dop = np.where(bars.dop != 0, bars.dop + shift, bars.dop)
    bars.dvpoc = np.where(bars.dvpoc != 0, bars.dvpoc + shift, bars.dvpoc)
    bars.close = np.where(bars.close != 0, bars.close + shift, bars.close)
    return bars


def _session_irt(irt_all, sid: str, pq_dir: Path, role: str = "ES"):
    y, m, d = session_id_to_ymd(sid)
    day0 = int(datetime(y, m, d, 0, 0, tzinfo=UK).timestamp() * 1e9)
    day1 = int(datetime(y, m, d, 23, 59, tzinfo=UK).timestamp() * 1e9)
    bars = irt_all.slice_range(day0, day1)
    if len(bars.ts_ns) == 0:
        return bars, 0.0
    tr0 = _prefer_single_feed(_load_trades(pq_dir, sid, role), pq_dir, sid, role)
    mid = int(tr0.column("ts_ns").to_numpy()[tr0.num_rows // 2]) if tr0.num_rows else day0
    lo, hi = bounds(mid)
    win = filt(tr0, lo, hi)
    align = validate_timezone(bars, win)
    med = align.get("median_abs_err_pts")
    shift = 0.0
    if med is not None and med >= 25.0:
        basis = basis_offset(bars, win)
        if basis.get("n", 0) >= 20 and basis.get("mad_pts", 99) <= 2.0:
            shift = -round(basis["median_pts"] / 0.25) * 0.25
            _shift_bars(bars, shift)
    return bars, shift


@dataclass
class Pending:
    side: str
    px_tick: int
    birth_ns: int
    birth_size: int
    dist_mid_pts: float
    lvn_id: str
    lvn_px: float
    lvn_dist: float
    mid_at_birth: float


@dataclass
class SessionState:
    bids: dict[int, int] = field(default_factory=dict)
    asks: dict[int, int] = field(default_factory=dict)
    # prices already touched by tape since open (ticks)
    touched: set[int] = field(default_factory=set)
    # armed pendings keyed by (side, px)
    pending: dict[tuple[str, int], Pending] = field(default_factory=dict)
    # already traded this (side, px) this session
    traded_keys: set[tuple[str, int]] = field(default_factory=set)
    # last mid from trades
    last_mid_tick: int | None = None
    # sizes previously seen (to detect fresh cross of threshold)
    prev_size: dict[tuple[str, int], int] = field(default_factory=dict)


def _nearest_lvn(
    bars,
    t_ns: int,
    centre_pts: float,
    stack=None,
) -> tuple[str, float, float] | None:
    """Nearest LVN to ``centre_pts``.

    Prefer the developing stack (all valleys in the trailing profile) when
    provided; otherwise fall back to IRT export LVN2/LVN3 as-of ``t_ns``.
    """
    if stack is not None:
        hit = stack.nearest(t_ns, centre_pts)
        if hit is not None:
            return hit
    bi = _asof_idx(bars.ts_ns, t_ns)
    levels = [(lid, px) for kind, lid, px in _active_levels(bars, bi) if kind == KIND_LVN]
    if not levels:
        return None
    lid, px = min(levels, key=lambda x: abs(x[1] - centre_pts))
    return lid, px, abs(px - centre_pts)


def _fade(ts, px, *, side, entry, tp, sl, t_entry_ns, exit_slip, commission):
    if len(ts) == 0:
        return None
    # path from fill print onward (caller passes slice starting at touch)
    sign = 1.0 if side == "BID" else -1.0
    if side == "BID":
        hit_tp, hit_sl = px >= entry + tp, px <= entry - sl
        mfe, mae = float(px.max() - entry), float(entry - px.min())
    else:
        hit_tp, hit_sl = px <= entry - tp, px >= entry + sl
        mfe, mae = float(entry - px.min()), float(px.max() - entry)
    if hit_tp.any() or hit_sl.any():
        j_tp = int(np.argmax(hit_tp)) if hit_tp.any() else 10**18
        j_sl = int(np.argmax(hit_sl)) if hit_sl.any() else 10**18
        if not hit_tp.any():
            j_tp = 10**18
        if not hit_sl.any():
            j_sl = 10**18
        reason, pnl = ("sl", -sl - exit_slip) if j_sl <= j_tp else ("tp", tp - exit_slip)
        j = min(j_tp, j_sl) if j_tp < 10**18 or j_sl < 10**18 else 0
    else:
        reason, pnl = "time", sign * (float(px[-1]) - entry) - exit_slip
        j = len(px) - 1
    return {
        "pnl_net": pnl - commission,
        "exit": reason,
        "mfe": mfe,
        "mae": mae,
        "t_exit_s": (int(ts[j]) - t_entry_ns) / 1e9 if len(ts) else 0.0,
    }


def _pack(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return {"n": 0, "wr": None, "net": 0.0, "avg": None, "maxDD": 0.0,
                "days_plus": 0, "days": 0, "n_armed": 0, "n_pulled": 0}
    pnl = np.asarray([t["pnl_net"] for t in trades], dtype=np.float64)
    by: dict[str, float] = {}
    for t in trades:
        by[t["session_id"]] = by.get(t["session_id"], 0.0) + t["pnl_net"]
    nets = np.asarray(list(by.values()), dtype=np.float64)
    ordered = sorted(trades, key=lambda t: (t["date"], t["t_entry_s"]))
    eq = np.cumsum([t["pnl_net"] for t in ordered])
    peak = np.maximum.accumulate(eq)
    dd = float((peak - eq).max()) if len(eq) else 0.0
    return {
        "n": len(trades),
        "wr": float((pnl > 0).mean()),
        "net": float(pnl.sum()),
        "avg": float(pnl.mean()),
        "maxDD": dd,
        "days_plus": int((nets > 0).sum()),
        "days": len(nets),
    }


def run_session(
    pq_dir: Path,
    sid: str,
    bars,
    *,
    large_thr: int,
    min_dist_pts: float,
    near_lvn_pts: float,
    offset: float,
    tp: float,
    sl: float,
    open_ns: int,
    end_ns: int,
    max_arm_s: float,
    min_age_s: float = 0.0,
    stack=None,
) -> tuple[list[dict], dict[str, int]]:
    """Detect + simulate one ES session. Returns trades and census."""
    y, m, d = session_id_to_ymd(sid)
    date_iso = f"{y:04d}-{m:02d}-{d:02d}"
    census = {
        "armed": 0, "pulled": 0, "touched_pre": 0, "fills": 0, "expired": 0,
        "too_young_touch": 0,
    }

    # Depth from open-2h seed fold for clean book at open
    depth, init_b, init_a = load_depth_window_with_book(
        pq_dir, sid, "ES", (open_ns, end_ns),
    )
    tr = _prefer_single_feed(_load_trades(pq_dir, sid, "ES"), pq_dir, sid, "ES")
    if tr.num_rows == 0 or depth.num_rows == 0:
        return [], census

    t_ts = tr.column("ts_ns").to_numpy().astype(np.int64)
    t_px = tr.column("price").to_numpy().astype(np.int64)  # ticks in parquet? check
    # trades price column is often in points/ticks — open_book uses * PTS_PER_TICK so it's tick units stored as float price field
    t_px_pts = tr.column("price").to_numpy().astype(np.float64) * PTS_PER_TICK
    t_px_ticks = np.round(t_px_pts / PTS_PER_TICK).astype(np.int64)
    o = np.argsort(t_ts, kind="stable")
    t_ts, t_px_pts, t_px_ticks = t_ts[o], t_px_pts[o], t_px_ticks[o]

    d_ts = depth.column("ts_ns").to_numpy().astype(np.int64)
    d_bid = depth.column("is_bid").to_numpy().astype(bool)
    d_px = depth.column("price_ticks").to_numpy().astype(np.int64)
    d_sz = depth.column("size").to_numpy().astype(np.int64)
    d_order = np.argsort(d_ts, kind="stable")
    d_ts, d_bid, d_px, d_sz = d_ts[d_order], d_bid[d_order], d_px[d_order], d_sz[d_order]

    st = SessionState(bids=dict(init_b), asks=dict(init_a))
    # Existing open book size is not "fresh" — only new crosses of large_thr arm.
    for px, sz in init_b.items():
        st.prev_size[("BID", int(px))] = int(sz)
    for px, sz in init_a.items():
        st.prev_size[("ASK", int(px))] = int(sz)
    # seed mid from last trade before open
    j0 = int(np.searchsorted(t_ts, open_ns, side="left")) - 1
    if j0 >= 0:
        st.last_mid_tick = int(t_px_ticks[j0])
    # Mark any pre-open prints in the RTH hour as already touched? No — only session
    # prints from open onward. But if open auction already traded a price, mark it.
    for j in range(max(0, j0 - 5000), j0 + 1):
        if j >= 0 and t_ts[j] >= open_ns - 60_000_000_000:
            st.touched.add(int(t_px_ticks[j]))

    trades_out: list[dict] = []
    max_arm_ns = int(max_arm_s * 1e9)
    min_age_ns = int(max(0.0, min_age_s) * 1e9)
    exit_slip = PTS_PER_TICK
    commission = 0.125

    i_t = int(np.searchsorted(t_ts, open_ns, side="left"))
    i_d = 0
    # merge walk
    n_d, n_t = len(d_ts), len(t_ts)

    def mid_tick() -> int | None:
        if st.bids and st.asks:
            return (max(st.bids) + min(st.asks)) // 2
        return st.last_mid_tick

    def mark_touched(px_tick: int) -> None:
        st.touched.add(int(px_tick))

    def try_arm(side: str, px_tick: int, sz: int, t_ns: int) -> None:
        key = (side, px_tick)
        if key in st.traded_keys or key in st.pending:
            return
        if px_tick in st.touched:
            census["touched_pre"] += 1
            return
        mt = mid_tick()
        if mt is None:
            return
        centre = px_tick * PTS_PER_TICK
        mid_pts = mt * PTS_PER_TICK
        dist = abs(centre - mid_pts)
        if dist < min_dist_pts:
            return
        # correct side of book vs mid
        if side == "BID" and centre >= mid_pts:
            return
        if side == "ASK" and centre <= mid_pts:
            return
        lv = _nearest_lvn(bars, t_ns, centre, stack=stack)
        if lv is None or lv[2] > near_lvn_pts:
            return
        # fresh: crossed threshold (prev < thr <= sz) or brand new
        prev = st.prev_size.get(key, 0)
        if prev >= large_thr:
            return  # already large — not fresh
        st.pending[key] = Pending(
            side=side, px_tick=px_tick, birth_ns=t_ns, birth_size=sz,
            dist_mid_pts=dist, lvn_id=lv[0], lvn_px=lv[1], lvn_dist=lv[2],
            mid_at_birth=mid_pts,
        )
        census["armed"] += 1

    def expire_pulls(t_ns: int) -> None:
        dead = []
        for key, p in st.pending.items():
            book = st.bids if p.side == "BID" else st.asks
            cur = book.get(p.px_tick, 0)
            if cur < large_thr:
                dead.append((key, "pulled"))
            elif t_ns - p.birth_ns > max_arm_ns:
                dead.append((key, "expired"))
        for key, why in dead:
            st.pending.pop(key, None)
            census["pulled" if why == "pulled" else "expired"] += 1

    def on_trade_print(t_ns: int, px_tick: int, px_pts: float) -> None:
        st.last_mid_tick = px_tick
        mark_touched(px_tick)
        # check pendings for first eligible touch (must have sat ≥ min_age)
        hit = []
        for key, p in list(st.pending.items()):
            if p.side == "BID":
                # touch when trade <= level (hits the bid)
                if px_tick > p.px_tick:
                    continue
            else:
                if px_tick < p.px_tick:
                    continue
            if t_ns - p.birth_ns < min_age_ns:
                # Still sitting — ignore early touches; keep arming until pull/expire
                census["too_young_touch"] += 1
                continue
            hit.append(key)
        for key in hit:
            p = st.pending.pop(key)
            if key in st.traded_keys:
                continue
            st.traded_keys.add(key)
            # fill at outside offset
            centre = p.px_tick * PTS_PER_TICK
            entry = centre - offset if p.side == "BID" else centre + offset
            # remaining tape from this print
            j = int(np.searchsorted(t_ts, t_ns, side="left"))
            # include current print index
            while j < n_t and t_ts[j] < t_ns:
                j += 1
            # find first index at/after this trade
            j = int(np.searchsorted(t_ts, t_ns, side="left"))
            ts_path = t_ts[j:]
            px_path = t_px_pts[j:]
            # path must reach entry for limit fill
            if p.side == "BID":
                fillable = px_path <= entry
            else:
                fillable = px_path >= entry
            if not fillable.any():
                # touched level but not our outside limit — still count as touch miss
                continue
            ji = int(np.argmax(fillable))
            ts2, px2 = ts_path[ji:], px_path[ji:]
            # flatten at end_ns
            k_end = int(np.searchsorted(ts2, end_ns, side="left"))
            ts2, px2 = ts2[: max(k_end, 1)], px2[: max(k_end, 1)]
            trd = _fade(
                ts2, px2, side=p.side, entry=entry, tp=tp, sl=sl,
                t_entry_ns=int(ts2[0]), exit_slip=exit_slip, commission=commission,
            )
            if trd is None:
                continue
            census["fills"] += 1
            trades_out.append({
                "date": date_iso,
                "session_id": sid,
                "side": p.side,
                "centre_price": round(centre, 2),
                "entry": round(entry, 2),
                "birth_size": p.birth_size,
                "dist_mid_at_birth": round(p.dist_mid_pts, 2),
                "lvn_id": p.lvn_id,
                "lvn_px": round(p.lvn_px, 2),
                "lvn_dist": round(p.lvn_dist, 2),
                "t_arm_s": round((p.birth_ns - open_ns) / 1e9, 1),
                "t_entry_s": round((int(ts2[0]) - open_ns) / 1e9, 1),
                "wait_s": round((int(ts2[0]) - p.birth_ns) / 1e9, 1),
                "large_thr": large_thr,
                **trd,
            })

    # Walk depth+trades in time order from open
    while i_d < n_d or i_t < n_t:
        take_d = i_t >= n_t or (i_d < n_d and d_ts[i_d] <= t_ts[i_t])
        if take_d:
            t_ns = int(d_ts[i_d])
            if t_ns > end_ns:
                break
            if t_ns >= open_ns:
                expire_pulls(t_ns)
                is_bid = bool(d_bid[i_d])
                px, sz = int(d_px[i_d]), int(d_sz[i_d])
                side = "BID" if is_bid else "ASK"
                book = st.bids if is_bid else st.asks
                if sz <= 0:
                    book.pop(px, None)
                else:
                    book[px] = sz
                key = (side, px)
                if sz >= large_thr and t_ns >= open_ns:
                    try_arm(side, px, sz, t_ns)
                st.prev_size[key] = max(0, sz)
            i_d += 1
        else:
            t_ns = int(t_ts[i_t])
            if t_ns > end_ns:
                break
            if t_ns >= open_ns:
                expire_pulls(t_ns)
                on_trade_print(t_ns, int(t_px_ticks[i_t]), float(t_px_pts[i_t]))
            i_t += 1

    return trades_out, census


def estimate_large_thr(pq_dir: Path, sid: str, open_ns: int, *, pct: float, floor: int) -> int:
    """p``pct`` of positive ES depth sizes in first 30 min of RTH (or floor)."""
    hi = open_ns + 30 * 60 * 10**9
    depth, _, _ = load_depth_window_with_book(pq_dir, sid, "ES", (open_ns, hi))
    if depth.num_rows == 0:
        return floor
    sz = depth.column("size").to_numpy().astype(np.float64)
    sz = sz[sz > 0]
    if len(sz) == 0:
        return floor
    return int(max(floor, float(np.percentile(sz, pct))))


def run(*, month: str, out_dir: Path, min_dist: float, near_lvn: float,
        offset: float, tp: float, sl: float, large_pct: float, large_floor: int,
        max_arm_min: float, until: str = "17:00", min_age_min: float = 0.0,
        lvn_stack: Path | None = None) -> None:
    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    stack = None
    if lvn_stack is not None:
        from research.irt_lvn.lvn_stack import load_stack_csv
        stack = load_stack_csv(Path(lvn_stack))
        print(f"LVN stack loaded: {lvn_stack} ({len(stack.ts_ns)} snaps)", flush=True)
    y_sel, m_sel = (int(x) for x in month.split("-")[:2])

    irt = load_irt_csv(default_irt_paths(ROOT)["ES"])
    sessions = []
    for sid in list_sessions(pq_dir):
        try:
            y, m, d = session_id_to_ymd(sid)
        except Exception:
            continue
        if (y, m) != (y_sel, m_sel):
            continue
        base = pq_dir / f"session_id={sid}"
        if (base / "depth.parquet").exists() and (base / "trades.parquet").exists():
            sessions.append(sid)
    sessions.sort()
    print(f"{month}: {len(sessions)} ES depth sessions", flush=True)

    all_trades: list[dict] = []
    census_rows: list[dict] = []

    for sid in sessions:
        y, m, d = session_id_to_ymd(sid)
        open_ns = _hhmm_ns(y, m, d, "14:30")
        end_ns = _hhmm_ns(y, m, d, until)
        bars, shift = _session_irt(irt, sid, pq_dir, "ES")
        if len(bars.ts_ns) == 0:
            print(f"  {sid}: no IRT", flush=True)
            continue
        thr = estimate_large_thr(pq_dir, sid, open_ns, pct=large_pct, floor=large_floor)
        print(f"  {sid}: large_thr={thr} basis_shift={shift:+.2f}", flush=True)
        trades, cen = run_session(
            pq_dir, sid, bars,
            large_thr=thr, min_dist_pts=min_dist, near_lvn_pts=near_lvn,
            offset=offset, tp=tp, sl=sl, open_ns=open_ns, end_ns=end_ns,
            max_arm_s=max_arm_min * 60.0,
            min_age_s=min_age_min * 60.0,
            stack=stack,
        )
        print(
            f"    armed={cen['armed']} fills={cen['fills']} pulled={cen['pulled']} "
            f"expired={cen['expired']} too_young={cen['too_young_touch']} "
            f"pre-touched skip={cen['touched_pre']}",
            flush=True,
        )
        all_trades.extend(trades)
        census_rows.append({"session_id": sid, "large_thr": thr, **cen, "n_trades": len(trades)})

    # Also compare open-only baseline from prior study if present
    open_base = ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_ES_OOS" / "summary.md"

    p = _pack(all_trades)
    lines = [
        f"# ES intraday fresh large ∩ LVN — first-touch fade ({month})",
        "",
        f"Arm when a **fresh** ES resting size ≥ session p{large_pct:g} (floor {large_floor}) "
        f"appears during 14:30–{until} UK that is:",
        f"- within **{near_lvn:g} pts** of as-of LVN2/LVN3,",
        f"- ≥ **{min_dist:g} pts** from mid at birth,",
        f"- at a price **not yet traded** since the open,",
        f"- must sit **≥ {min_age_min:g} min** before a touch can fill,",
        f"- then **fade first eligible touch** limit {offset:g} pts outside, SL{sl:g}/TP{tp:g}.",
        f"Disarm if size drops below thr or unarmed >{max_arm_min:g} min.",
        "",
        "P&L book = **fills only** (orders that were touched; pulled/expired arms are census-only).",
        "",
        f"## Results",
        "",
        f"- Sessions: {len(census_rows)}",
        f"- Armed levels: **{sum(c['armed'] for c in census_rows)}**",
        f"- Fills: **{p['n']}** · WR {p['wr']:.0%} · net **{p['net']:+.1f}** · avg {p['avg']:+.2f}"
        if p["n"] else f"- Fills: **0**",
        f"- Days+: {p['days_plus']}/{p['days']} · maxDD {p['maxDD']:.0f}" if p["n"] else "",
        f"- Pulled before touch: {sum(c['pulled'] for c in census_rows)} · "
        f"expired: {sum(c['expired'] for c in census_rows)} · "
        f"too-young touches: {sum(c.get('too_young_touch', 0) for c in census_rows)}",
        "",
        "Open-only ES∩LVN≤10 out2 8/16 baseline (prior): Oct n=27 net +147 — "
        "this run should produce **more fills** if the intraday net is wider.",
        "",
    ]
    if p["n"]:
        # ToD of entries
        te = [t["t_entry_s"] for t in all_trades]
        lines += [
            "## Entry time (seconds after 14:30)",
            "",
            f"- median {np.median(te):.0f}s · p25 {np.percentile(te,25):.0f} · "
            f"p75 {np.percentile(te,75):.0f} · max {np.max(te):.0f}",
            f"- wait from arm→fill median {np.median([t['wait_s'] for t in all_trades]):.0f}s",
            f"- dist at birth median {np.median([t['dist_mid_at_birth'] for t in all_trades]):.1f} pts",
            "",
        ]
        # hour buckets
        lines += ["| hour UK | n | net | avg |", "|---|---:|---:|---:|"]
        for h0, h1, lab in ((0, 3600, "14:30–15:30"), (3600, 7200, "15:30–16:30"), (7200, 10**9, "16:30–17:00")):
            sub = [t for t in all_trades if h0 <= t["t_entry_s"] < h1]
            if not sub:
                lines.append(f"| {lab} | 0 | — | — |")
            else:
                net = sum(t["pnl_net"] for t in sub)
                lines.append(f"| {lab} | {len(sub)} | {net:+.1f} | {net/len(sub):+.2f} |")

    # vs open-only: count how many fills are after 14:35 (truly intraday)
    if all_trades:
        after = [t for t in all_trades if t["t_arm_s"] >= 300]
        lines += [
            "",
            f"## Open vs later",
            "",
            f"- Armed ≥5 min after open: fills **{len(after)}** / {len(all_trades)} "
            f"({len(after)/len(all_trades):.0%})",
        ]
        if after:
            pa = _pack(after)
            lines.append(f"- Later-only book: n={pa['n']} WR {pa['wr']:.0%} net {pa['net']:+.1f} avg {pa['avg']:+.2f}")

    trades_path = out_dir / "trades.csv"
    if all_trades:
        fields = list(all_trades[0].keys())
        with trades_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(all_trades)
    with (out_dir / "census.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(census_rows[0].keys()) if census_rows else ["session_id"])
        if census_rows:
            w.writeheader()
            w.writerows(census_rows)

    summary = out_dir / "summary.md"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"Wrote {summary}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--month", default="2026-10")
    ap.add_argument("--out", default=str(ROOT / "results" / "ES_INTRADAY_LVN_LARGE_OCT"))
    ap.add_argument("--min-dist", type=float, default=8.0, help="Min pts from mid at birth")
    ap.add_argument("--near-lvn", type=float, default=10.0)
    ap.add_argument("--offset", type=float, default=2.0)
    ap.add_argument("--tp", type=float, default=16.0)
    ap.add_argument("--sl", type=float, default=8.0)
    ap.add_argument("--large-pct", type=float, default=99.0)
    ap.add_argument("--large-floor", type=int, default=40)
    ap.add_argument("--max-arm-min", type=float, default=90.0)
    ap.add_argument(
        "--min-age-min", type=float, default=0.0,
        help="Min minutes a large order must rest before a touch can fill (default 0)",
    )
    ap.add_argument(
        "--until", default="17:00",
        help="UK clock: arm/fill window end (default 17:00)",
    )
    ap.add_argument(
        "--lvn-stack",
        default=None,
        help="Optional LVN_STACK_ES.csv from build_lvn_stack (all valleys as-of arm)",
    )
    args = ap.parse_args()
    run(
        month=args.month,
        out_dir=Path(args.out),
        min_dist=args.min_dist,
        near_lvn=args.near_lvn,
        offset=args.offset,
        tp=args.tp,
        sl=args.sl,
        large_pct=args.large_pct,
        large_floor=args.large_floor,
        max_arm_min=args.max_arm_min,
        until=args.until,
        min_age_min=args.min_age_min,
        lvn_stack=Path(args.lvn_stack) if args.lvn_stack else None,
    )


if __name__ == "__main__":
    main()
