"""October open clusters ∩ IRT LVNs: does confluence improve first-touch fades?

For each open standout cluster, find nearest LVN2/LVN3 (as-of snap, basis-aligned
for @NQ#) and mark confluence within a distance band. Compare hit rate and
first-touch fade P&L for near-LVN vs not.

    .venv\\Scripts\\python.exe -m research.irt_lvn.study_open_cluster_lvn_confluence
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
from research.correlated.harness import _prefer_single_feed
from research.irt_lvn.events import PTS_PER_TICK, _active_levels, _asof_idx
from research.irt_lvn.load_irt import KIND_LVN, default_irt_paths, load_irt_csv, session_id_to_ymd
from research.irt_lvn.run_exp_0042 import validate_timezone
from research.irt_lvn.run_hold_break_of import basis_offset, bounds, filt
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")


def _hhmm_ns(y: int, m: int, d: int, hhmm: str) -> int:
    hh, mm = (int(x) for x in hhmm.split(":")[:2])
    return int(datetime(y, m, d, hh, mm, tzinfo=UK).timestamp() * 1e9)


def _tape(pq_dir: Path, sid: str, role: str, t0: int, t1: int):
    tr = _prefer_single_feed(_load_trades(pq_dir, sid, role), pq_dir, sid, role)
    if tr.num_rows == 0:
        return np.array([], np.int64), np.array([], np.float64)
    ts = tr.column("ts_ns").to_numpy().astype(np.int64)
    px = tr.column("price").to_numpy().astype(np.float64) * PTS_PER_TICK
    o = np.argsort(ts, kind="stable")
    ts, px = ts[o], px[o]
    m = (ts >= t0) & (ts < t1)
    return ts[m], px[m]


def _shift_bars(bars, shift: float):
    """Apply constant basis shift to all price columns (IRT → Bookmap)."""
    if shift == 0.0:
        return bars
    bars.lvn2 = bars.lvn2 + shift
    bars.lvn3 = bars.lvn3 + shift
    bars.dop = np.where(bars.dop != 0, bars.dop + shift, bars.dop)
    bars.dvpoc = np.where(bars.dvpoc != 0, bars.dvpoc + shift, bars.dvpoc)
    bars.close = np.where(bars.close != 0, bars.close + shift, bars.close)
    return bars


def _session_bars(irt_all, sid: str, role: str, pq_dir: Path):
    """Slice IRT to session day and apply basis shift if needed (NQ continuous)."""
    y, m, d = session_id_to_ymd(sid)
    day0 = int(datetime(y, m, d, 0, 0, tzinfo=UK).timestamp() * 1e9)
    day1 = int(datetime(y, m, d, 23, 59, tzinfo=UK).timestamp() * 1e9)
    bars = irt_all.slice_range(day0, day1)
    if len(bars.ts_ns) == 0:
        return bars, None, None

    tr0 = _prefer_single_feed(_load_trades(pq_dir, sid, role), pq_dir, sid, role)
    mid = int(tr0.column("ts_ns").to_numpy()[tr0.num_rows // 2]) if tr0.num_rows else day0
    lo, hi = bounds(mid)
    win = filt(tr0, lo, hi)
    align = validate_timezone(bars, win)
    med = align.get("median_abs_err_pts")
    shift = 0.0
    basis = None
    if med is not None and med >= 25.0:
        basis = basis_offset(bars, win)
        if basis.get("n", 0) >= 20 and basis.get("mad_pts", 99) <= 2.0:
            shift = -round(basis["median_pts"] / 0.25) * 0.25
            _shift_bars(bars, shift)
            align = validate_timezone(bars, win)
            med = align.get("median_abs_err_pts")
    return bars, {"align_med": med, "basis_shift": shift, "basis": basis}, win


def _nearest_lvn(bars, snap_ns: int, centre: float) -> dict[str, Any]:
    bi = _asof_idx(bars.ts_ns, snap_ns)
    levels = [(lid, px) for kind, lid, px in _active_levels(bars, bi) if kind == KIND_LVN]
    if not levels:
        return {
            "near_lvn": False, "lvn_id": "", "lvn_px": None,
            "lvn_dist": None, "n_lvn": 0,
        }
    best = min(levels, key=lambda x: abs(x[1] - centre))
    dist = abs(best[1] - centre)
    return {
        "near_lvn": True,  # filled later by threshold
        "lvn_id": best[0],
        "lvn_px": round(best[1], 2),
        "lvn_dist": round(dist, 2),
        "n_lvn": len(levels),
        "all_lvn": ";".join(f"{lid}:{px:.2f}" for lid, px in levels),
    }


def _fade(
    ts, px, *, side: str, entry: float, tp: float, sl: float,
    snap_ns: int, exit_slip: float, commission: float,
) -> dict[str, Any] | None:
    if len(ts) == 0:
        return None
    fillable = px <= entry if side == "BID" else px >= entry
    if not fillable.any():
        return None
    i = int(np.argmax(fillable))
    ts2, px2 = ts[i:], px[i:]
    sign = 1.0 if side == "BID" else -1.0
    if side == "BID":
        hit_tp, hit_sl = px2 >= entry + tp, px2 <= entry - sl
        mfe, mae = float(px2.max() - entry), float(entry - px2.min())
    else:
        hit_tp, hit_sl = px2 <= entry - tp, px2 >= entry + sl
        mfe, mae = float(entry - px2.min()), float(px2.max() - entry)
    if hit_tp.any() or hit_sl.any():
        j_tp = int(np.argmax(hit_tp)) if hit_tp.any() else 10**18
        j_sl = int(np.argmax(hit_sl)) if hit_sl.any() else 10**18
        if not hit_tp.any():
            j_tp = 10**18
        if not hit_sl.any():
            j_sl = 10**18
        if j_sl <= j_tp:
            reason, pnl = "sl", -sl - exit_slip
        else:
            reason, pnl = "tp", tp - exit_slip
    else:
        reason = "time"
        pnl = sign * (float(px2[-1]) - entry) - exit_slip
    return {
        "filled": 1, "pnl_net": pnl - commission, "exit": reason,
        "mfe": mfe, "mae": mae,
        "t_entry_s": (int(ts[i]) - snap_ns) / 1e9,
    }


def _hit(ts, px, *, side: str, lo: float, hi: float) -> int:
    if len(ts) == 0:
        return 0
    if side == "ASK":
        return int(bool((px >= lo).any()))
    return int(bool((px <= hi).any()))


def _pack(trades: list[dict], label: str) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"label": label, "n": 0, "wr": None, "net": 0.0, "avg": None, "maxDD": 0.0,
                "days_plus": 0, "days": 0, "hit_rate": None, "n_levels": 0}
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
        "label": label,
        "n": n,
        "wr": float((pnl > 0).mean()),
        "net": float(pnl.sum()),
        "avg": float(pnl.mean()),
        "maxDD": dd,
        "days_plus": int((nets > 0).sum()),
        "days": len(nets),
    }


def run(*, clusters_path: Path, out_dir: Path, until: str, near_pts: list[float]) -> None:
    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    clusters = list(csv.DictReader(clusters_path.open(encoding="utf-8")))
    out_dir.mkdir(parents=True, exist_ok=True)

    paths = default_irt_paths(ROOT)
    irt = {role: load_irt_csv(paths[role]) for role in ("NQ", "ES") if role in paths}
    print(f"IRT loaded: {list(irt)}", flush=True)

    # Session-level bars + align (cache)
    sess_bars: dict[tuple[str, str], Any] = {}
    align_log: list[dict] = []
    for c in clusters:
        key = (c["session_id"], c["market"])
        if key in sess_bars:
            continue
        if c["market"] not in irt:
            sess_bars[key] = None
            continue
        bars, meta, _ = _session_bars(irt[c["market"]], c["session_id"], c["market"], pq_dir)
        sess_bars[key] = bars
        if meta:
            align_log.append({"session_id": c["session_id"], "market": c["market"], **{
                k: meta[k] for k in ("align_med", "basis_shift")
            }})
            print(
                f"  align {c['session_id']} {c['market']}: med={meta['align_med']} "
                f"shift={meta['basis_shift']:+.2f}",
                flush=True,
            )

    # Annotate clusters
    annotated: list[dict[str, Any]] = []
    for c in clusters:
        key = (c["session_id"], c["market"])
        bars = sess_bars.get(key)
        y, m, d = session_id_to_ymd(c["session_id"])
        snap_ns = _hhmm_ns(y, m, d, c.get("snap_time_uk") or "14:30")
        centre = float(c["centre_price"])
        if bars is None or len(bars.ts_ns) == 0:
            lv = {"near_lvn": False, "lvn_id": "", "lvn_px": None, "lvn_dist": None, "n_lvn": 0, "all_lvn": ""}
        else:
            lv = _nearest_lvn(bars, snap_ns, centre)
        row = {
            **{k: c[k] for k in c},
            "centre_price": centre,
            "price_lo": float(c["price_lo"]),
            "price_hi": float(c["price_hi"]),
            "rank_in_side": int(c["rank_in_side"]),
            "significance": float(c["significance"]),
            "dist_mid_pts": float(c["dist_mid_pts"]),
            "lvn_id": lv.get("lvn_id", ""),
            "lvn_px": lv.get("lvn_px"),
            "lvn_dist": lv.get("lvn_dist"),
            "n_lvn": lv.get("n_lvn", 0),
            "all_lvn": lv.get("all_lvn", ""),
        }
        annotated.append(row)

    # Dist distribution
    dists = [r["lvn_dist"] for r in annotated if r["lvn_dist"] is not None]
    print(
        f"Clusters with LVN as-of: {len(dists)}/{len(annotated)}; "
        f"median dist={np.median(dists):.1f} p25={np.percentile(dists,25):.1f} "
        f"p75={np.percentile(dists,75):.1f}",
        flush=True,
    )

    # Preload tapes
    tapes: dict[tuple[str, str], tuple] = {}
    for r in annotated:
        key = (r["session_id"], r["market"])
        if key in tapes:
            continue
        y, m, d = session_id_to_ymd(r["session_id"])
        snap_ns = _hhmm_ns(y, m, d, r.get("snap_time_uk") or "14:30")
        end_ns = _hhmm_ns(y, m, d, until)
        ts, px = _tape(pq_dir, r["session_id"], r["market"], snap_ns, end_ns)
        tapes[key] = (ts, px, snap_ns)

    exit_slip = PTS_PER_TICK
    brackets = [(5, 10), (5, 15), (8, 16), (10, 20), (10, 30)]
    # also outside-2 for ES-flavoured
    offsets = [0.0, 2.0]

    lines = [
        "# Open cluster × LVN confluence — October 2026",
        "",
        "Open standout resting clusters matched to as-of **LVN2/LVN3** at 14:30 UK "
        "(IRT `@NQ#` / `ESZ6`, basis-shifted when median align err ≥ 25 pts).",
        f"First-touch **fade** (buy BID / sell ASK). Costs: exit slip {exit_slip} + commission.",
        "",
        f"## Distance to nearest LVN (n={len(dists)})",
        "",
        f"- median **{np.median(dists):.1f}** pts · p25 {np.percentile(dists,25):.1f} · "
        f"p75 {np.percentile(dists,75):.1f} · max {np.max(dists):.1f}",
        "",
    ]

    # Write annotated CSV with flags for each near threshold
    ann_path = out_dir / "clusters_with_lvn.csv"
    for thr in near_pts:
        for r in annotated:
            r[f"confluence_{thr:g}"] = int(
                r["lvn_dist"] is not None and r["lvn_dist"] <= thr
            )

    fields = [
        "date", "session_id", "market", "side", "role_vs_mid", "centre_price",
        "price_lo", "price_hi", "size", "significance", "rank_in_side", "dist_mid_pts",
        "age_stratum", "lvn_id", "lvn_px", "lvn_dist", "n_lvn", "all_lvn",
    ] + [f"confluence_{thr:g}" for thr in near_pts]
    with ann_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in annotated:
            w.writerow(r)

    grid_rows: list[dict] = []

    for thr in near_pts:
        near = [r for r in annotated if r[f"confluence_{thr:g}"]]
        far = [r for r in annotated if not r[f"confluence_{thr:g}"]]
        lines += [
            f"## Confluence band ≤ {thr:g} pts",
            "",
            f"- Near LVN: **{len(near)}** / {len(annotated)} "
            f"({len(near)/len(annotated):.0%}) · Far: {len(far)}",
        ]
        # by market
        for mkt in ("NQ", "ES", "ALL"):
            if mkt == "ALL":
                n_sub, f_sub = near, far
            else:
                n_sub = [r for r in near if r["market"] == mkt]
                f_sub = [r for r in far if r["market"] == mkt]
            # hit rates
            def hit_rate(rows):
                if not rows:
                    return None, 0, 0
                h = 0
                for r in rows:
                    ts, px, _ = tapes[(r["session_id"], r["market"])]
                    h += _hit(ts, px, side=r["side"], lo=r["price_lo"], hi=r["price_hi"])
                return h / len(rows), h, len(rows)
            hr_n, hn, nn = hit_rate(n_sub)
            hr_f, hf, nf = hit_rate(f_sub)
            n_txt = "—" if hr_n is None else f"{hn}/{nn}={hr_n:.1%}"
            f_txt = "—" if hr_f is None else f"{hf}/{nf}={hr_f:.1%}"
            lines.append(f"- **{mkt} hit rate**: near {n_txt} · far {f_txt}")

        lines += [
            "",
            f"### First-touch fade P&L (confluence ≤{thr:g})",
            "",
            "| market | offset | SL | TP | near n/WR/net/avg | far n/WR/net/avg | Δavg |",
            "|---|---|---:|---:|---|---|---:|",
        ]

        for mkt_filter in ("ALL", "NQ", "ES"):
            for off in offsets:
                for sl, tp in brackets:
                    def sim_set(rows):
                        trades = []
                        for r in rows:
                            if mkt_filter != "ALL" and r["market"] != mkt_filter:
                                continue
                            ts, px, snap_ns = tapes[(r["session_id"], r["market"])]
                            centre = r["centre_price"]
                            entry = centre - off if r["side"] == "BID" else centre + off
                            comm = 0.25 if r["market"] == "NQ" else 0.125
                            tr = _fade(
                                ts, px, side=r["side"], entry=entry, tp=tp, sl=sl,
                                snap_ns=snap_ns, exit_slip=exit_slip, commission=comm,
                            )
                            if tr is None:
                                continue
                            trades.append({
                                **tr,
                                "date": r["date"],
                                "session_id": r["session_id"],
                                "market": r["market"],
                            })
                        return trades

                    tn = sim_set(near)
                    tf = sim_set(far)
                    pn, pf = _pack(tn, "near"), _pack(tf, "far")
                    if pn["n"] < 5 and pf["n"] < 5:
                        continue
                    def fmt(p):
                        if p["n"] == 0:
                            return "n=0"
                        return (
                            f"{p['n']}/{p['wr']:.0%}/{p['net']:+.0f}/{p['avg']:+.2f}"
                        )
                    davg = (
                        (pn["avg"] - pf["avg"])
                        if pn["avg"] is not None and pf["avg"] is not None
                        else float("nan")
                    )
                    off_lab = "at" if off == 0 else f"out{off:g}"
                    lines.append(
                        f"| {mkt_filter} | {off_lab} | {sl} | {tp} | {fmt(pn)} | {fmt(pf)} | "
                        f"{davg:+.2f} |"
                    )
                    grid_rows.append({
                        "near_pts": thr,
                        "market": mkt_filter,
                        "offset": off,
                        "sl": sl,
                        "tp": tp,
                        "near_n": pn["n"],
                        "near_wr": pn["wr"],
                        "near_net": pn["net"],
                        "near_avg": pn["avg"],
                        "far_n": pf["n"],
                        "far_wr": pf["wr"],
                        "far_net": pf["net"],
                        "far_avg": pf["avg"],
                        "delta_avg": davg,
                    })
        lines.append("")

    # Primary readout at 10 pts
    thr0 = 10.0 if 10.0 in near_pts else near_pts[0]
    near = [r for r in annotated if r[f"confluence_{thr0:g}"]]
    far = [r for r in annotated if not r[f"confluence_{thr0:g}"]]
    # hold proxy: hit but fade wins? Or not hit = "held unused"
    # Better: among fills, WR and pierce-hold: hit then reverse
    lines += [
        "## Verdict",
        "",
        f"Using ≤{thr0:g} pt as the primary confluence band "
        f"({len(near)} near / {len(far)} far):",
        "",
    ]
    # Compare avg at baseline 5/10 and at 10/20 for ALL
    def find(thr, mkt, off, sl, tp):
        for g in grid_rows:
            if (
                g["near_pts"] == thr and g["market"] == mkt and g["offset"] == off
                and g["sl"] == sl and g["tp"] == tp
            ):
                return g
        return None

    for sl, tp in ((5, 10), (10, 20), (10, 30)):
        g = find(thr0, "ALL", 0.0, sl, tp)
        if not g:
            continue
        better = (
            "near better" if (g["near_avg"] or -999) > (g["far_avg"] or -999)
            else "far better / no lift"
        )
        lines.append(
            f"- At-level SL{sl}/TP{tp}: near avg {g['near_avg']} (n={g['near_n']}) vs "
            f"far avg {g['far_avg']} (n={g['far_n']}) → **{better}** "
            f"(Δavg {g['delta_avg']:+.2f})."
        )

    # Hit rate summary at thr0
    def hr(rows):
        if not rows:
            return float("nan")
        h = sum(
            _hit(
                *tapes[(r["session_id"], r["market"])][:2],
                side=r["side"], lo=r["price_lo"], hi=r["price_hi"],
            )
            for r in rows
        )
        return h / len(rows)

    # fix _hit call - tapes returns (ts,px,snap)
    def hr2(rows):
        if not rows:
            return float("nan"), 0, 0
        h = 0
        for r in rows:
            ts, px, _ = tapes[(r["session_id"], r["market"])]
            h += _hit(ts, px, side=r["side"], lo=r["price_lo"], hi=r["price_hi"])
        return h / len(rows), h, len(rows)

    hr_n, hn, nn = hr2(near)
    hr_f, hf, nf = hr2(far)
    lines.append(
        f"- Hit rate ≤{thr0:g} pts: near **{hr_n:.1%}** ({hn}/{nn}) vs far **{hr_f:.1%}** ({hf}/{nf}). "
        f"{'Near hit *less* (holds more often unused / not run).' if hr_n < hr_f else 'Near hit more (no hold advantage).'}"
    )
    lines.append("")
    lines.append(
        "If confluence helped, near-LVN fades would show **higher WR / avg** than far. "
        "Single month — treat as descriptive."
    )
    lines.append("")
    lines.append(f"Annotated clusters: `{ann_path.name}`")

    grid_path = out_dir / "confluence_fade_grid.csv"
    if grid_rows:
        with grid_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(grid_rows[0].keys()))
            w.writeheader()
            w.writerows(grid_rows)

    summary = out_dir / "summary.md"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"Wrote {summary}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clusters", default=str(ROOT / "results" / "open_clusters_oct2026.csv"))
    ap.add_argument("--out", default=str(ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_OCT"))
    ap.add_argument("--until", default="17:00")
    ap.add_argument(
        "--near",
        default="5,10,15,20",
        help="Comma-separated confluence distance bands in points",
    )
    args = ap.parse_args()
    near_pts = [float(x) for x in args.near.split(",") if x.strip()]
    run(
        clusters_path=Path(args.clusters),
        out_dir=Path(args.out),
        until=args.until,
        near_pts=near_pts,
    )


if __name__ == "__main__":
    main()
