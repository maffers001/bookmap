"""Export largest age-weighted resting order clusters at RTH open (ES + NQ).

For manual chart review: snapshot each market's book at 14:30 UK, cluster
near-mid resting size, score by size × age (older + larger = higher), write CSV.

    .venv\\Scripts\\python.exe -m research.irt_lvn.export_open_clusters
    .venv\\Scripts\\python.exe -m research.irt_lvn.export_open_clusters --month 2026-10 --out results/open_clusters_oct2026.csv
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

import numpy as np

from research.config import ROOT, load_config
from research.correlated.harness import _prefer_single_feed, list_sessions
from research.irt_lvn.events import PTS_PER_TICK
from research.irt_lvn.load_irt import session_id_to_ymd
from research.irt_lvn.of_scores import _bbo, _resolve_mbo
from research.irt_lvn.open_book import (
    OpenBookConfig,
    age_weight_cluster,
    mbo_resting_at,
    snapshot_clusters,
)
from research.irt_lvn.run_hold_break_of import load_depth_window_with_book, load_mbo_window
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")

# Market-specific clustering (matches EXP-0045 NQ / EXP-0046 ES).
MARKET_CFG: dict[str, dict[str, Any]] = {
    "NQ": {
        "book_radius_pts": 100.0,
        "cluster_ticks": 2,          # 0.5 pt bands
        "near_mid_exclude_pts": 5.0,  # keep closer levels for chart review
        "bright_size_pct": 99.0,
        "large_size_min": 15,
        "top_n_per_side": 10,
    },
    "ES": {
        "book_radius_pts": 60.0,
        "cluster_ticks": 6,          # 1.5 pt bands
        "near_mid_exclude_pts": 1.0,
        "bright_size_pct": 99.0,
        "large_size_min": 20,
        "top_n_per_side": 10,
    },
}


def _hhmm_ns(y: int, m: int, d: int, hhmm: str) -> int:
    hh, mm = (int(x) for x in hhmm.split(":")[:2])
    return int(datetime(y, m, d, hh, mm, tzinfo=UK).timestamp() * 1e9)


def _age_stats(
    cluster: dict[str, Any],
    resting: dict[tuple[bool, int], list[tuple[float, int]]],
    *,
    snap_ns: int,
    is_bid: bool,
    age_cap_s: float,
) -> dict[str, float]:
    ages: list[float] = []
    sizes: list[float] = []
    for p in cluster["prices"]:
        for sz, first in resting.get((is_bid, int(p)), []):
            age_s = min(age_cap_s, max(0.0, (snap_ns - first) / 1e9))
            ages.append(age_s)
            sizes.append(float(sz))
    depth_sz = float(cluster["size"])
    raw = float(sum(sizes))
    missing = max(0.0, depth_sz - raw)
    if missing > 0:
        ages.append(age_cap_s)
        sizes.append(missing)
    if not ages:
        return {
            "mean_age_s": 0.0,
            "max_age_s": 0.0,
            "median_age_s": 0.0,
            "n_orders": 0.0,
        }
    a = np.asarray(ages, dtype=np.float64)
    s = np.asarray(sizes, dtype=np.float64)
    wmean = float(np.sum(a * s) / np.sum(s)) if s.sum() > 0 else float(np.mean(a))
    return {
        "mean_age_s": wmean,
        "max_age_s": float(a.max()),
        "median_age_s": float(np.median(a)),
        "n_orders": float(len(ages)),
    }


def _ob_cfg(
    mkt: str,
    *,
    cluster_pts: float | None = None,
    max_width_pts: float | None = None,
    large_size_min: int | None = None,
    top_n_per_side: int | None = None,
) -> OpenBookConfig:
    p = MARKET_CFG[mkt]
    if cluster_pts is not None:
        cluster_ticks = max(1, int(round(float(cluster_pts) / PTS_PER_TICK)))
    else:
        cluster_ticks = int(p["cluster_ticks"])
    # When widening the merge gap, default max band width to the same span
    # so chains cannot grow into 50–80 pt walls.
    if max_width_pts is None and cluster_pts is not None:
        max_width_pts = float(cluster_pts)
    max_width_ticks = (
        max(1, int(round(float(max_width_pts) / PTS_PER_TICK)))
        if max_width_pts is not None
        else None
    )
    return OpenBookConfig(
        book_radius_pts=float(p["book_radius_pts"]),
        cluster_ticks=cluster_ticks,
        max_width_ticks=max_width_ticks,
        near_mid_exclude_pts=float(p["near_mid_exclude_pts"]),
        large_size_min=int(
            p["large_size_min"] if large_size_min is None else large_size_min
        ),
        top_n_per_side=int(
            p["top_n_per_side"] if top_n_per_side is None else top_n_per_side
        ),
        standout_pct=50.0,  # unused — we rank all bright clusters
        large_mult=0.0,
        age_scale_s=900.0,
        age_w_min=0.25,
        age_w_max=1.0,
        old_age_s=900.0,
        age_cap_s=7200.0,
        frac_old_thr=0.5,
    )


def _trade_mid_at(tr, snap_ns: int) -> float | None:
    """Last trade price at/before snap (points)."""
    if tr.num_rows == 0:
        return None
    ts = tr.column("ts_ns").to_numpy().astype(np.int64)
    px = tr.column("price").to_numpy().astype(np.float64) * PTS_PER_TICK
    o = np.argsort(ts, kind="stable")
    ts, px = ts[o], px[o]
    j = int(np.searchsorted(ts, snap_ns, side="right")) - 1
    if j < 0:
        return float(px[0]) if len(px) else None
    return float(px[j])


def session_clusters(
    pq_dir: Path,
    sid: str,
    role: str,
    *,
    snap_hhmm: str = "14:30",
    cluster_pts: float | None = None,
    max_width_pts: float | None = None,
    bright_size_pct: float | None = None,
    large_size_min: int | None = None,
    top_n_per_side: int | None = None,
) -> list[dict[str, Any]]:
    """Snapshot resting book at snap via a full depth fold (no SOF window bug).

    Earlier export path seeded the book 2h before open but only replayed the
    last 30 min of depth through ``prepare_session_of``, leaving stale ask
    size below the market (e.g. ES 7769.5 on 2026-10-02 labelled resistance
    while price was ~7789.5). Here the book is folded from all depth rows
    with ``ts < snap``, then bids above / asks below the trade mid are dropped.
    """
    y, m, d = session_id_to_ymd(sid)
    snap_ns = _hhmm_ns(y, m, d, snap_hhmm)
    ob = _ob_cfg(
        role,
        cluster_pts=cluster_pts,
        max_width_pts=max_width_pts,
        large_size_min=large_size_min,
        top_n_per_side=top_n_per_side,
    )
    age_lo = snap_ns - int(ob.age_cap_s * 1e9)
    date_iso = f"{y:04d}-{m:02d}-{d:02d}"

    tr0 = _prefer_single_feed(_load_trades(pq_dir, sid, role), pq_dir, sid, role)
    if tr0.num_rows == 0:
        return []
    trade_mid = _trade_mid_at(tr0, snap_ns)
    if trade_mid is None:
        return []

    # Fold every depth update before snap into the resting book (correct BBO).
    _empty, bids, asks = load_depth_window_with_book(
        pq_dir, sid, role, (snap_ns, snap_ns),
    )
    if not bids and not asks:
        return []

    mid_tick = int(round(trade_mid / PTS_PER_TICK))
    # Drop crossed / wrong-side pollution relative to trade mid
    bids = {p: s for p, s in bids.items() if s > 0 and p < mid_tick}
    asks = {p: s for p, s in asks.items() if s > 0 and p > mid_tick}
    bb, aa = _bbo(bids, asks)
    if bb is None or aa is None:
        return []
    mid_px = trade_mid
    bid_px = bb * PTS_PER_TICK
    ask_px = aa * PTS_PER_TICK

    rad_t = int(round(ob.book_radius_pts / PTS_PER_TICK))
    nz = np.asarray(
        [s for p, s in list(bids.items()) + list(asks.items()) if abs(p - mid_tick) <= rad_t],
        dtype=np.float64,
    )
    bright_pct = float(
        MARKET_CFG[role]["bright_size_pct"]
        if bright_size_pct is None else bright_size_pct
    )
    bright_floor = int(max(
        ob.large_size_min,
        float(np.percentile(nz, bright_pct)) if len(nz) else ob.large_size_min,
    ))
    clusters = snapshot_clusters(
        bids, asks, mid_tick=mid_tick, cfg=ob, min_price_size=float(bright_floor),
    )
    if not clusters:
        return []

    mbo = load_mbo_window(pq_dir, sid, role, (age_lo, snap_ns + 1))
    m_ts, m_op, m_bid, m_px, m_sz, _st = _resolve_mbo(mbo, age_lo, snap_ns + 1)
    data_start = int(m_ts[0]) if len(m_ts) else age_lo
    resting = mbo_resting_at(
        m_ts, m_op, m_bid, m_px, m_sz, snap_ns,
        data_start_ns=data_start, age_cap_s=ob.age_cap_s,
    )

    scored: list[dict[str, Any]] = []
    for c in clusters:
        if c["dist_mid_pts"] < ob.near_mid_exclude_pts:
            continue
        is_bid = c["side"] == "BID"
        centre_px = float(c["centre_tick"]) * PTS_PER_TICK
        # Safety: support must be below mid, resistance above
        if is_bid and centre_px >= mid_px:
            continue
        if (not is_bid) and centre_px <= mid_px:
            continue
        aw = age_weight_cluster(c, resting, snap_ns=snap_ns, is_bid=is_bid, cfg=ob)
        ages = _age_stats(
            c, resting, snap_ns=snap_ns, is_bid=is_bid, age_cap_s=ob.age_cap_s,
        )
        px_lo = float(c["px_lo"]) * PTS_PER_TICK
        px_hi = float(c["px_hi"]) * PTS_PER_TICK
        sig = float(aw["age_weighted_size"])
        scored.append({
            "date": date_iso,
            "session_id": sid,
            "market": role,
            "snap_time_uk": snap_hhmm,
            "side": c["side"],
            "role_vs_mid": "support" if is_bid else "resistance",
            "centre_price": round(centre_px, 2),
            "price_lo": round(px_lo, 2),
            "price_hi": round(px_hi, 2),
            "width_pts": round(px_hi - px_lo, 2),
            "size": int(round(float(c["size"]))),
            "age_weighted_size": round(sig, 1),
            "significance": round(sig, 1),
            "mean_age_min": round(ages["mean_age_s"] / 60.0, 1),
            "median_age_min": round(ages["median_age_s"] / 60.0, 1),
            "max_age_min": round(ages["max_age_s"] / 60.0, 1),
            "frac_old_15m": round(float(aw["frac_old"]), 3),
            "age_stratum": "old" if aw["age_stratum"] >= 0.5 else "fresh",
            "n_orders": int(ages["n_orders"]),
            "n_price_levels": int(c["n_prices"]),
            "dist_mid_pts": round(float(c["dist_mid_pts"]), 2),
            "mid_price": round(mid_px, 2),
            "best_bid": round(bid_px, 2),
            "best_ask": round(ask_px, 2),
            "bright_floor": bright_floor,
            "prices_ticks": ",".join(str(int(p)) for p in c["prices"]),
            "sizes_per_tick": ",".join(str(int(s)) for s in c["sizes"]),
        })

    if not scored:
        return []

    sizes = np.asarray([r["size"] for r in scored], dtype=np.float64)
    for r in scored:
        r["size_pctile_session"] = round(
            float(100.0 * np.mean(sizes <= r["size"])), 1,
        )

    top_n = int(ob.top_n_per_side)
    kept: list[dict[str, Any]] = []
    for side in ("BID", "ASK"):
        side_rows = [r for r in scored if r["side"] == side]
        side_rows.sort(key=lambda r: r["significance"], reverse=True)
        for i, r in enumerate(side_rows[:top_n], start=1):
            r["rank_in_side"] = i
            kept.append(r)
    return kept


def run(
    *,
    month: str,
    out: Path,
    snap_hhmm: str,
    markets: list[str] | None = None,
    cluster_pts: float | None = None,
    max_width_pts: float | None = None,
    bright_size_pct: float | None = None,
    large_size_min: int | None = None,
    top_n_per_side: int | None = None,
) -> Path:
    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    y_sel, m_sel = (int(x) for x in month.split("-")[:2])
    roles = markets or ["NQ", "ES"]
    sessions = []
    for sid in list_sessions(pq_dir):
        try:
            y, m, d = session_id_to_ymd(sid)
        except Exception:
            continue
        if (y, m) != (y_sel, m_sel):
            continue
        base = pq_dir / f"session_id={sid}"
        if (base / "depth.parquet").exists() and (base / "mbo.parquet").exists():
            sessions.append(sid)
    sessions.sort()
    ct_desc = (
        f"cluster_pts={cluster_pts:g}"
        if cluster_pts is not None
        else "default market cluster_ticks"
    )
    print(
        f"Sessions for {month}: {len(sessions)} · markets={roles} · {ct_desc}",
        flush=True,
    )

    rows: list[dict[str, Any]] = []
    for sid in sessions:
        print(f"[session] {sid}", flush=True)
        for role in roles:
            try:
                got = session_clusters(
                    pq_dir, sid, role, snap_hhmm=snap_hhmm,
                    cluster_pts=cluster_pts,
                    max_width_pts=max_width_pts,
                    bright_size_pct=bright_size_pct,
                    large_size_min=large_size_min,
                    top_n_per_side=top_n_per_side,
                )
            except Exception as e:
                print(f"  {role} FAIL: {e}", flush=True)
                continue
            print(f"  {role}: {len(got)} clusters", flush=True)
            rows.extend(got)

    # Global rank within market (across the month) for sorting the CSV
    for role in roles:
        mkt = [r for r in rows if r["market"] == role]
        mkt.sort(key=lambda r: r["significance"], reverse=True)
        for i, r in enumerate(mkt, start=1):
            r["rank_in_month"] = i

    rows.sort(key=lambda r: (r["date"], r["market"], -r["significance"]))

    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "date", "session_id", "market", "snap_time_uk", "side", "role_vs_mid",
        "centre_price", "price_lo", "price_hi", "width_pts",
        "size", "age_weighted_size", "significance",
        "rank_in_side", "rank_in_month", "size_pctile_session",
        "mean_age_min", "median_age_min", "max_age_min",
        "frac_old_15m", "age_stratum", "n_orders", "n_price_levels",
        "dist_mid_pts", "mid_price", "best_bid", "best_ask",
        "bright_floor", "prices_ticks", "sizes_per_tick",
    ]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # Short sidecar readme for chart use
    note = out.with_suffix(".README.txt")
    if cluster_pts is not None:
        cluster_line = f"Cluster merge gap: {cluster_pts:g} pts."
    else:
        cluster_line = "NQ: cluster within 2 ticks (0.5 pt); ES: 6 ticks (1.5 pt)."
    bright_line = (
        f"Bright filter: price size ≥ p{bright_size_pct:g} of snap book "
        f"(and min floor)."
        if bright_size_pct is not None
        else "Bright filter: price size ≥ p99 of snap book within radius (and min floor)."
    )
    note.write_text(
        "\n".join([
            f"Open-book resting clusters — {month}",
            f"Snap: {snap_hhmm} UK (RTH open).",
            f"Markets: {', '.join(roles)}.",
            "mid_price = last trade at/before snap (not a crossed BBO mid).",
            "Book = full depth fold to snap; bids above mid / asks below mid dropped.",
            "significance = age_weighted_size: resting contracts × age weight",
            "  (age weight = clip(age_s / 900, 0.25, 1.0); full weight by 15 min).",
            bright_line,
            cluster_line,
            "Top N per side per session; CSV sorted by date then significance.",
            "role_vs_mid: BID=support below mid, ASK=resistance above mid.",
            f"Rows: {len(rows)}  Sessions: {len(sessions)}",
            f"Output: {out}",
        ]) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {out} ({len(rows)} rows)", flush=True)
    print(f"Wrote {note}", flush=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--month", default="2026-10")
    ap.add_argument("--snap", default="14:30", help="UK snap time HH:MM")
    ap.add_argument(
        "--out",
        default=str(ROOT / "results" / "open_clusters_oct2026.csv"),
    )
    ap.add_argument(
        "--markets", default="NQ,ES",
        help="Comma-separated markets (default NQ,ES)",
    )
    ap.add_argument(
        "--cluster-pts", type=float, default=None,
        help="Merge gap in points (overrides market default ticks)",
    )
    ap.add_argument(
        "--max-width-pts", type=float, default=None,
        help="Cap cluster band width in points (default = --cluster-pts when set)",
    )
    ap.add_argument(
        "--bright-size-pct", type=float, default=None,
        help="Percentile floor for per-tick size before merge (default 99)",
    )
    ap.add_argument(
        "--large-size-min", type=int, default=None,
        help="Absolute min contracts per tick (with bright percentile)",
    )
    ap.add_argument(
        "--top-n-per-side", type=int, default=None,
        help="Keep top N clusters per side per session",
    )
    args = ap.parse_args()
    markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
    run(
        month=args.month,
        out=Path(args.out),
        snap_hhmm=args.snap,
        markets=markets,
        cluster_pts=args.cluster_pts,
        max_width_pts=args.max_width_pts,
        bright_size_pct=args.bright_size_pct,
        large_size_min=args.large_size_min,
        top_n_per_side=args.top_n_per_side,
    )


if __name__ == "__main__":
    main()
