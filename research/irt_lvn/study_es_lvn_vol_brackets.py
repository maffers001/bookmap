"""ES open-cluster ∩ LVN fade with volatility-scaled brackets (Oct/Sep/May).

Frozen structure from confluence dig (no retune of entry/confluence):
  ES · cluster ≤10 pts of LVN2/3 · fade limit 2 pts outside · first touch.
Only the TP/SL regime varies: fixed 8/16 vs vol-scaled SL=clip(k·u) TP=rr·SL
where u = median ES 1-min range 14:00–14:30 UK.

    .venv\\Scripts\\python.exe -m research.irt_lvn.study_es_lvn_vol_brackets
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
from research.irt_lvn.events import PTS_PER_TICK
from research.irt_lvn.load_irt import session_id_to_ymd
from research.irt_lvn.open_book import _clip, vol_unit_preopen
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")


def _hhmm_ns(y: int, m: int, d: int, hhmm: str) -> int:
    hh, mm = (int(x) for x in hhmm.split(":")[:2])
    return int(datetime(y, m, d, hh, mm, tzinfo=UK).timestamp() * 1e9)


def _fade(ts, px, *, side, entry, tp, sl, snap_ns, exit_slip, commission):
    if len(ts) == 0:
        return None
    fillable = px <= entry if side == "BID" else px >= entry
    if not fillable.any():
        return None
    i = int(np.argmax(fillable))
    px2 = px[i:]
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
        reason, pnl = ("sl", -sl - exit_slip) if j_sl <= j_tp else ("tp", tp - exit_slip)
    else:
        reason, pnl = "time", sign * (float(px2[-1]) - entry) - exit_slip
    return {
        "pnl_net": pnl - commission,
        "exit": reason,
        "mfe": mfe,
        "mae": mae,
        "sl": sl,
        "tp": tp,
        "t_entry_s": (int(ts[i]) - snap_ns) / 1e9,
    }


def _pack(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return {"n": 0, "wr": None, "net": 0.0, "avg": None, "maxDD": 0.0,
                "days_plus": 0, "days": 0, "med_sl": None, "med_tp": None,
                "n_tp": 0, "n_sl": 0, "n_time": 0}
    pnl = np.asarray([t["pnl_net"] for t in trades], dtype=np.float64)
    by: dict[str, float] = {}
    for t in trades:
        by[t["session_id"]] = by.get(t["session_id"], 0.0) + t["pnl_net"]
    nets = np.asarray(list(by.values()), dtype=np.float64)
    ordered = sorted(trades, key=lambda t: (t["date"], t["t_entry_s"]))
    eq = np.cumsum([t["pnl_net"] for t in ordered])
    peak = np.maximum.accumulate(eq)
    dd = float((peak - eq).max()) if len(eq) else 0.0
    exits = {"tp": 0, "sl": 0, "time": 0}
    for t in trades:
        exits[t["exit"]] = exits.get(t["exit"], 0) + 1
    return {
        "n": len(trades),
        "wr": float((pnl > 0).mean()),
        "net": float(pnl.sum()),
        "avg": float(pnl.mean()),
        "maxDD": dd,
        "days_plus": int((nets > 0).sum()),
        "days": len(nets),
        "med_sl": float(np.median([t["sl"] for t in trades])),
        "med_tp": float(np.median([t["tp"] for t in trades])),
        "n_tp": exits["tp"],
        "n_sl": exits["sl"],
        "n_time": exits["time"],
    }


def _load_month_levels(
    ann_path: Path,
    *,
    near_pts: float,
    market: str = "ES",
    min_size: float = 0.0,
) -> list[dict]:
    rows = list(csv.DictReader(ann_path.open(encoding="utf-8")))
    key = f"confluence_{near_pts:g}"
    out = []
    for r in rows:
        if r["market"] != market:
            continue
        if r.get(key) != "1":
            continue
        size = float(r["size"]) if r.get("size") not in (None, "") else 0.0
        if size < min_size:
            continue
        out.append({
            "date": r["date"],
            "session_id": r["session_id"],
            "side": r["side"],
            "centre": float(r["centre_price"]),
            "lvn_px": float(r["lvn_px"]) if r.get("lvn_px") not in (None, "") else None,
            "lvn_dist": float(r["lvn_dist"]) if r.get("lvn_dist") not in (None, "") else None,
            "size": size,
            "rank": int(float(r["rank_in_side"])),
            "snap_time_uk": r.get("snap_time_uk") or "14:30",
        })
    return out


def run(*, out_dir: Path, near_pts: float, offset: float, until: str) -> None:
    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    months = {
        "OCT": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_OCT" / "clusters_with_lvn.csv",
        "SEP": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_SEP" / "clusters_with_lvn.csv",
        "MAY": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_MAY" / "clusters_with_lvn.csv",
    }

    # Bracket specs: name -> callable(u) -> (tp, sl)
    # ES-scaled floors/caps (EXP-0046 used 2–6; we also try wider caps since fixed 8 worked)
    def vol_br(k: float, rr: float, floor: float, cap: float):
        def fn(u: float) -> tuple[float, float]:
            sl = _clip(k * u, floor, cap)
            return rr * sl, sl
        return fn

    brackets: list[tuple[str, Any]] = [
        ("fixed_8_16", lambda u: (16.0, 8.0)),
        ("fixed_5_10", lambda u: (10.0, 5.0)),
        ("vol_k1_rr2_f2c6", vol_br(1.0, 2.0, 2.0, 6.0)),   # EXP-0046-like
        ("vol_k1_rr2_f3c10", vol_br(1.0, 2.0, 3.0, 10.0)),
        ("vol_k1_rr2_f4c12", vol_br(1.0, 2.0, 4.0, 12.0)),
        ("vol_k1.5_rr2_f3c12", vol_br(1.5, 2.0, 3.0, 12.0)),
        ("vol_k2_rr2_f4c15", vol_br(2.0, 2.0, 4.0, 15.0)),
        ("vol_k1_rr3_f3c10", vol_br(1.0, 3.0, 3.0, 10.0)),
        ("vol_k1.5_rr3_f3c12", vol_br(1.5, 3.0, 3.0, 12.0)),
        ("vol_k1_rr2_f2c8", vol_br(1.0, 2.0, 2.0, 8.0)),
    ]

    exit_slip = PTS_PER_TICK
    commission = 0.125
    grid: list[dict] = []
    u_by_sess: dict[str, float] = {}

    lines = [
        "# ES LVN-confluence fade — volatility brackets",
        "",
        f"Structure frozen: ES · LVN≤{near_pts:g} pts · outside+{offset:g} · first-touch fade.",
        "u = median ES 1-min range 14:00–14:30 UK. SL=clip(k·u, floor, cap), TP=rr·SL.",
        "",
    ]

    for mo, ann in months.items():
        levels = _load_month_levels(ann, near_pts=near_pts)
        print(f"{mo}: {len(levels)} ES confluence levels", flush=True)
        # vol unit + tape per session
        sess_ids = sorted({lv["session_id"] for lv in levels})
        tapes: dict[str, tuple] = {}
        for sid in sess_ids:
            y, m, d = session_id_to_ymd(sid)
            snap_ns = _hhmm_ns(y, m, d, "14:30")
            end_ns = _hhmm_ns(y, m, d, until)
            vol_lo = _hhmm_ns(y, m, d, "14:00")
            vol_hi = _hhmm_ns(y, m, d, "14:30")
            tr = _prefer_single_feed(_load_trades(pq_dir, sid, "ES"), pq_dir, sid, "ES")
            ts_all = tr.column("ts_ns").to_numpy().astype(np.int64)
            px_all = tr.column("price").to_numpy().astype(np.float64) * PTS_PER_TICK
            o = np.argsort(ts_all, kind="stable")
            ts_all, px_all = ts_all[o], px_all[o]
            u = vol_unit_preopen(ts_all, px_all, vol_lo, vol_hi)
            u_by_sess[f"{mo}:{sid}"] = u
            m = (ts_all >= snap_ns) & (ts_all < end_ns)
            tapes[sid] = (ts_all[m], px_all[m], snap_ns, u)
            print(f"  {sid} u={u:.2f}", flush=True)

        us = [tapes[s][3] for s in sess_ids]
        lines += [
            f"## {mo} — ES vol unit (pre-open)",
            "",
            f"Sessions {len(us)} · median u **{np.median(us):.2f}** · "
            f"p25 {np.percentile(us,25):.2f} · p75 {np.percentile(us,75):.2f}",
            "",
            "| bracket | n | WR | net | avg | med SL/TP | days+ | maxDD | exits |",
            "|---|---:|---:|---:|---:|---|---:|---:|---|",
        ]

        for bname, bfn in brackets:
            trades = []
            for lv in levels:
                ts, px, snap_ns, u = tapes[lv["session_id"]]
                tp, sl = bfn(u)
                centre = lv["centre"]
                entry = centre - offset if lv["side"] == "BID" else centre + offset
                trd = _fade(
                    ts, px, side=lv["side"], entry=entry, tp=tp, sl=sl,
                    snap_ns=snap_ns, exit_slip=exit_slip, commission=commission,
                )
                if trd is None:
                    continue
                trades.append({
                    **trd,
                    "date": lv["date"],
                    "session_id": lv["session_id"],
                    "u": u,
                    "bracket": bname,
                })
            p = _pack(trades)
            if p["n"] == 0:
                lines.append(f"| {bname} | 0 | — | — | — | — | — | — | — |")
            else:
                lines.append(
                    f"| {bname} | {p['n']} | {p['wr']:.0%} | {p['net']:+.1f} | {p['avg']:+.2f} | "
                    f"{p['med_sl']:.1f}/{p['med_tp']:.1f} | {p['days_plus']}/{p['days']} | "
                    f"{p['maxDD']:.0f} | tp={p['n_tp']} sl={p['n_sl']} t={p['n_time']} |"
                )
            grid.append({
                "month": mo,
                "bracket": bname,
                "n": p["n"],
                "wr": p["wr"],
                "net": p["net"],
                "avg": p["avg"],
                "med_sl": p["med_sl"],
                "med_tp": p["med_tp"],
                "days_plus": p["days_plus"],
                "days": p["days"],
                "maxDD": p["maxDD"],
                "n_tp": p["n_tp"],
                "n_sl": p["n_sl"],
                "n_time": p["n_time"],
            })
            # per-trade dump for primary vols
            if bname in ("fixed_8_16", "vol_k1_rr2_f3c10", "vol_k1.5_rr2_f3c12", "vol_k1_rr2_f2c6"):
                tpath = out_dir / f"trades_{mo}_{bname}.csv"
                with tpath.open("w", newline="", encoding="utf-8") as f:
                    fields = ["date", "session_id", "u", "sl", "tp", "pnl_net", "exit", "mfe", "mae", "t_entry_s"]
                    w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                    w.writeheader()
                    for t in trades:
                        w.writerow(t)
        lines.append("")

    # Cross-month comparison table for key brackets
    lines += [
        "## Cross-month (near-LVN ES only)",
        "",
        "| bracket | Oct net/avg/n | Sep net/avg/n | May net/avg/n | Sum net | +months |",
        "|---|---|---|---|---:|---:|",
    ]
    bnames = [b[0] for b in brackets]
    for bname in bnames:
        cells = {}
        for mo in ("OCT", "SEP", "MAY"):
            g = next(x for x in grid if x["month"] == mo and x["bracket"] == bname)
            cells[mo] = g
        snet = sum(cells[m]["net"] for m in cells)
        npos = sum(1 for m in cells if cells[m]["net"] > 0)

        def cell(g):
            if g["n"] == 0:
                return "—"
            return f"{g['net']:+.0f}/{g['avg']:+.2f}/{g['n']}"

        lines.append(
            f"| {bname} | {cell(cells['OCT'])} | {cell(cells['SEP'])} | {cell(cells['MAY'])} | "
            f"{snet:+.0f} | {npos}/3 |"
        )

    lines += [
        "",
        "## Verdict",
        "",
        "Compare vol regimes to frozen **fixed_8_16**. Prefer a vol rule only if it "
        "matches or beats fixed on sum net **and** stays ≥0 in ≥2/3 months without "
        "being a one-cell Oct fit (check med SL stays ES-sensible).",
        "",
    ]
    # auto pick
    fixed = [g for g in grid if g["bracket"] == "fixed_8_16"]
    fixed_sum = sum(g["net"] for g in fixed)
    best_vol = None
    best_sum = -1e18
    for bname in bnames:
        if bname.startswith("fixed"):
            continue
        gs = [g for g in grid if g["bracket"] == bname]
        s = sum(g["net"] for g in gs)
        npos = sum(1 for g in gs if g["net"] > 0)
        if npos >= 2 and s > best_sum:
            best_sum = s
            best_vol = (bname, s, npos, gs)

    lines.append(f"- Fixed 8/16 three-month net: **{fixed_sum:+.1f}**.")
    if best_vol:
        bname, s, npos, gs = best_vol
        lines.append(
            f"- Best vol (≥2 positive months): **{bname}** sum **{s:+.1f}** ({npos}/3). "
            + " · ".join(f"{g['month']} {g['net']:+.0f} (medSL {g['med_sl']:.1f})" for g in gs)
        )
        if s >= fixed_sum:
            lines.append("- Vol **matches/beats** fixed on this sample → candidate to replace fixed for paper.")
        else:
            lines.append("- Vol does **not** beat fixed sum → keep fixed_8_16 as primary; vol is optional risk-normaliser.")
    else:
        lines.append("- No vol bracket stayed positive in ≥2/3 months.")

    grid_path = out_dir / "vol_bracket_grid.csv"
    with grid_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(grid[0].keys()))
        w.writeheader()
        w.writerows(grid)

    summary = out_dir / "summary.md"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"Wrote {summary}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "results" / "ES_LVN_CONFLUENCE_VOL_BRACKETS"))
    ap.add_argument("--near", type=float, default=10.0)
    ap.add_argument("--offset", type=float, default=2.0)
    ap.add_argument("--until", default="17:00")
    args = ap.parse_args()
    run(out_dir=Path(args.out), near_pts=args.near, offset=args.offset, until=args.until)


if __name__ == "__main__":
    main()
