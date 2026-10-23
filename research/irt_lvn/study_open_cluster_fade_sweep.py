"""Sweep TP/SL and entry offset for open-cluster first-touch fades (Oct).

    .venv\\Scripts\\python.exe -m research.irt_lvn.study_open_cluster_fade_sweep
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


def _sim(
    ts: np.ndarray,
    px: np.ndarray,
    *,
    side: str,
    centre: float,
    entry: float,
    tp: float,
    sl: float,
    snap_ns: int,
    exit_slip: float,
    commission: float,
) -> dict[str, Any] | None:
    """Fade: long BID / short ASK. Fill when price reaches entry (limit)."""
    if len(ts) == 0:
        return None
    if side == "BID":
        # buy limit at entry (at or below support); fill when px <= entry
        fillable = px <= entry
        sign = 1.0
    else:
        fillable = px >= entry
        sign = -1.0
    if not fillable.any():
        return None
    i = int(np.argmax(fillable))
    ts2, px2 = ts[i:], px[i:]
    if side == "BID":
        tp_px, sl_px = entry + tp, entry - sl
        hit_tp, hit_sl = px2 >= tp_px, px2 <= sl_px
        mfe = float(px2.max() - entry)
        mae = float(entry - px2.min())
    else:
        tp_px, sl_px = entry - tp, entry + sl
        hit_tp, hit_sl = px2 <= tp_px, px2 >= sl_px
        mfe = float(entry - px2.min())
        mae = float(px2.max() - entry)

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
        "pnl_net": pnl - commission,
        "exit": reason,
        "mfe": mfe,
        "mae": mae,
        "t_entry_s": (int(ts[i]) - snap_ns) / 1e9,
    }


def _summarize(trades: list[dict], label: str) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {
            "label": label, "n": 0, "wr": None, "net": 0.0, "avg": None,
            "pf": None, "days_plus": 0, "days": 0, "maxDD": 0.0,
            "tp": 0, "sl": 0, "time": 0, "med_mfe": None, "med_mae": None,
        }
    pnl = np.asarray([t["pnl_net"] for t in trades], dtype=np.float64)
    wins = pnl[pnl > 0].sum()
    losses = -pnl[pnl < 0].sum()
    pf = float(wins / losses) if losses > 1e-9 else float("inf")
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
        "label": label,
        "n": n,
        "wr": float((pnl > 0).mean()),
        "net": float(pnl.sum()),
        "avg": float(pnl.mean()),
        "pf": pf,
        "days_plus": int((nets > 0).sum()),
        "days": len(nets),
        "maxDD": dd,
        "tp": exits.get("tp", 0),
        "sl": exits.get("sl", 0),
        "time": exits.get("time", 0),
        "med_mfe": float(np.median([t["mfe"] for t in trades])),
        "med_mae": float(np.median([t["mae"] for t in trades])),
    }


def run(*, clusters_path: Path, out_dir: Path, until: str) -> None:
    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    clusters = list(csv.DictReader(clusters_path.open(encoding="utf-8")))
    out_dir.mkdir(parents=True, exist_ok=True)

    # Preload tapes
    tapes: dict[tuple[str, str], tuple[np.ndarray, np.ndarray, int]] = {}
    meta: list[dict[str, Any]] = []
    for c in clusters:
        sid, role = c["session_id"], c["market"]
        y, m, d = session_id_to_ymd(sid)
        snap_ns = _hhmm_ns(y, m, d, c.get("snap_time_uk") or "14:30")
        end_ns = _hhmm_ns(y, m, d, until)
        key = (sid, role)
        if key not in tapes:
            ts, px = _tape(pq_dir, sid, role, snap_ns, end_ns)
            tapes[key] = (ts, px, snap_ns)
        meta.append({
            "date": c["date"],
            "session_id": sid,
            "market": role,
            "side": c["side"],
            "centre": float(c["centre_price"]),
            "rank": int(c["rank_in_side"]),
            "age": c.get("age_stratum", ""),
            "sig": float(c["significance"]),
            "dist": float(c["dist_mid_pts"]),
            "comm": 0.25 if role == "NQ" else 0.125,
        })
    print(f"Loaded {len(tapes)} tapes, {len(meta)} levels", flush=True)

    exit_slip = PTS_PER_TICK

    # Grids
    # (sl, tp) absolute pts — covers tighter stops + 1:2 / 1:3 / 1:4
    brackets = [
        (2, 4), (2, 6), (2, 8),
        (3, 6), (3, 9), (3, 12),
        (4, 8), (4, 12), (4, 16),
        (5, 5), (5, 10), (5, 15), (5, 20),
        (8, 8), (8, 16), (8, 24),
        (10, 10), (10, 20), (10, 30),
    ]
    # entry offset pts: 0=at centre; + = outside (deeper into wall); - = inside (in front)
    offsets = [0.0, 0.5, 1.0, 2.0, -0.5, -1.0]
    universes = [
        ("all", lambda m: True),
        ("NQ", lambda m: m["market"] == "NQ"),
        ("ES", lambda m: m["market"] == "ES"),
        ("rank1", lambda m: m["rank"] == 1),
        ("ES_rank1", lambda m: m["market"] == "ES" and m["rank"] == 1),
        ("old", lambda m: m["age"] == "old"),
    ]

    rows_out: list[dict[str, Any]] = []

    for off in offsets:
        for sl, tp in brackets:
            # collect fills per universe in one pass
            fills_by_u: dict[str, list[dict]] = {u: [] for u, _ in universes}
            for m in meta:
                sid, role, side = m["session_id"], m["market"], m["side"]
                ts, px, snap_ns = tapes[(sid, role)]
                centre = m["centre"]
                # outside: BID lower, ASK higher; inside opposite
                if side == "BID":
                    entry = centre - off
                else:
                    entry = centre + off
                tr = _sim(
                    ts, px, side=side, centre=centre, entry=entry,
                    tp=tp, sl=sl, snap_ns=snap_ns,
                    exit_slip=exit_slip, commission=m["comm"],
                )
                if tr is None:
                    continue
                trade = {**tr, "date": m["date"], "session_id": sid, "market": role}
                for uname, pred in universes:
                    if pred(m):
                        fills_by_u[uname].append(trade)

            for uname, _ in universes:
                s = _summarize(fills_by_u[uname], uname)
                rows_out.append({
                    "universe": uname,
                    "entry_offset": off,
                    "offset_label": (
                        "at_level" if off == 0 else
                        f"outside_{off:g}" if off > 0 else
                        f"inside_{abs(off):g}"
                    ),
                    "sl": sl,
                    "tp": tp,
                    "rr": round(tp / sl, 2),
                    "n": s["n"],
                    "wr": None if s["wr"] is None else round(s["wr"], 4),
                    "net": round(s["net"], 2),
                    "avg": None if s["avg"] is None else round(s["avg"], 3),
                    "pf": None if s["pf"] is None else round(s["pf"], 3),
                    "days_plus": s["days_plus"],
                    "days": s["days"],
                    "maxDD": round(s["maxDD"], 2),
                    "n_tp": s["tp"],
                    "n_sl": s["sl"],
                    "n_time": s["time"],
                    "med_mfe": None if s["med_mfe"] is None else round(s["med_mfe"], 2),
                    "med_mae": None if s["med_mae"] is None else round(s["med_mae"], 2),
                    "be_wr": round(sl / (tp + sl), 4),
                })

    # write full grid
    grid_path = out_dir / "sweep_grid.csv"
    fields = list(rows_out[0].keys())
    with grid_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows_out)

    # ---- summary markdown: best nets, tighter stops, outside entries, high RR
    def top(univ: str, n: int = 8, pred=None):
        sub = [r for r in rows_out if r["universe"] == univ and r["n"] >= 15]
        if pred:
            sub = [r for r in sub if pred(r)]
        return sorted(sub, key=lambda r: r["net"], reverse=True)[:n]

    lines = [
        "# Open-cluster fade sweep — October 2026",
        "",
        "First-touch **fade** (buy BID / sell ASK). Limit fill at entry. "
        f"Exit slip {exit_slip} pt + commission. Window → {until} UK.",
        "",
        "**entry_offset:** `0` = centre of cluster; "
        "**outside** = deeper into the wall (BID limit lower / ASK limit higher — fill on push into liquidity); "
        "**inside** = in front of the wall (earlier fill on approach).",
        "",
        f"Full grid: `{grid_path.name}` ({len(rows_out)} rows).",
        "",
    ]

    for univ in ("all", "ES", "NQ", "rank1", "ES_rank1", "old"):
        lines += [f"## Best nets — `{univ}` (n≥15)", ""]
        lines += [
            "| offset | SL | TP | RR | n | WR | BE WR | net | avg | PF | days+ | maxDD | med MFE/MAE |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        for r in top(univ, 10):
            lines.append(
                f"| {r['offset_label']} | {r['sl']:g} | {r['tp']:g} | {r['rr']} | {r['n']} | "
                f"{r['wr']:.1%} | {r['be_wr']:.1%} | {r['net']:+.1f} | {r['avg']:+.2f} | "
                f"{r['pf']:.2f} | {r['days_plus']}/{r['days']} | {r['maxDD']:.0f} | "
                f"{r['med_mfe']:.1f}/{r['med_mae']:.1f} |"
            )
        lines.append("")

    # Focused comparisons at offset=0
    lines += ["## At-level entry: SL × RR slice (universe=all)", ""]
    lines += [
        "| SL | TP | RR | n | WR | net | avg | PF | maxDD |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in sorted(
        [x for x in rows_out if x["universe"] == "all" and x["entry_offset"] == 0],
        key=lambda x: (x["sl"], x["tp"]),
    ):
        lines.append(
            f"| {r['sl']:g} | {r['tp']:g} | {r['rr']} | {r['n']} | {r['wr']:.1%} | "
            f"{r['net']:+.1f} | {r['avg']:+.2f} | {r['pf']:.2f} | {r['maxDD']:.0f} |"
        )

    lines += ["", "## ES only, at-level (cleanest prior)", ""]
    lines += [
        "| SL | TP | RR | n | WR | net | avg | PF | days+ | maxDD |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in sorted(
        [x for x in rows_out if x["universe"] == "ES" and x["entry_offset"] == 0],
        key=lambda x: (x["sl"], x["tp"]),
    ):
        lines.append(
            f"| {r['sl']:g} | {r['tp']:g} | {r['rr']} | {r['n']} | {r['wr']:.1%} | "
            f"{r['net']:+.1f} | {r['avg']:+.2f} | {r['pf']:.2f} | "
            f"{r['days_plus']}/{r['days']} | {r['maxDD']:.0f} |"
        )

    # Offset effect at fixed 5/10 and best ES bracket
    lines += ["", "## Entry offset effect (all clusters, SL5/TP10)", ""]
    lines += [
        "| offset | n | WR | net | avg | med MFE/MAE |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for r in sorted(
        [x for x in rows_out if x["universe"] == "all" and x["sl"] == 5 and x["tp"] == 10],
        key=lambda x: x["entry_offset"],
    ):
        lines.append(
            f"| {r['offset_label']} | {r['n']} | {r['wr']:.1%} | {r['net']:+.1f} | "
            f"{r['avg']:+.2f} | {r['med_mfe']:.1f}/{r['med_mae']:.1f} |"
        )

    lines += ["", "## Entry offset effect (ES, SL3/TP9 — 1:3)", ""]
    lines += [
        "| offset | n | WR | net | avg | med MFE/MAE |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for r in sorted(
        [x for x in rows_out if x["universe"] == "ES" and x["sl"] == 3 and x["tp"] == 9],
        key=lambda x: x["entry_offset"],
    ):
        lines.append(
            f"| {r['offset_label']} | {r['n']} | {r['wr']:.1%} | {r['net']:+.1f} | "
            f"{r['avg']:+.2f} | {r['med_mfe']:.1f}/{r['med_mae']:.1f} |"
        )

    # Path quality: median MFE/MAE at at-level fills tells if wider TP is reachable
    lines += [
        "",
        "## Path quality (at-level, SL5/TP10 fills)",
        "",
    ]
    base = next(
        r for r in rows_out
        if r["universe"] == "all" and r["entry_offset"] == 0 and r["sl"] == 5 and r["tp"] == 10
    )
    lines.append(
        f"- All: med MFE **{base['med_mfe']}** / med MAE **{base['med_mae']}** "
        f"(if MFE ≪ 15–20, 1:3+ targets on a 5 pt stop are rarely reached)."
    )
    for univ in ("NQ", "ES"):
        b = next(
            r for r in rows_out
            if r["universe"] == univ and r["entry_offset"] == 0 and r["sl"] == 5 and r["tp"] == 10
        )
        lines.append(f"- {univ}: med MFE **{b['med_mfe']}** / med MAE **{b['med_mae']}**")

    # Verdict
    best_all = top("all", 1)[0] if top("all", 1) else None
    best_es = top("ES", 1)[0] if top("ES", 1) else None
    lines += ["", "## Verdict", ""]
    if best_all and best_es:
        lines.append(
            f"- Best `all` cell: {best_all['offset_label']} SL{best_all['sl']:g}/TP{best_all['tp']:g} "
            f"net **{best_all['net']:+.1f}** (n={best_all['n']}, WR {best_all['wr']:.1%})."
        )
        lines.append(
            f"- Best `ES` cell: {best_es['offset_label']} SL{best_es['sl']:g}/TP{best_es['tp']:g} "
            f"net **{best_es['net']:+.1f}** (n={best_es['n']}, WR {best_es['wr']:.1%})."
        )
    # Count how many all-universe cells are positive
    pos_all = sum(1 for r in rows_out if r["universe"] == "all" and r["net"] > 0 and r["n"] >= 15)
    tot_all = sum(1 for r in rows_out if r["universe"] == "all" and r["n"] >= 15)
    pos_es = sum(1 for r in rows_out if r["universe"] == "ES" and r["net"] > 0 and r["n"] >= 15)
    tot_es = sum(1 for r in rows_out if r["universe"] == "ES" and r["n"] >= 15)
    lines.append(f"- Positive cells: all {pos_all}/{tot_all} · ES {pos_es}/{tot_es} (n≥15).")
    lines.append(
        "- Single-month sweep — any 'best' cell is a search artefact until frozen and read on another month."
    )

    summary = out_dir / "summary.md"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"Wrote {summary}", flush=True)
    print(f"Wrote {grid_path}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clusters", default=str(ROOT / "results" / "open_clusters_oct2026.csv"))
    ap.add_argument("--out", default=str(ROOT / "results" / "OPEN_CLUSTER_FADE_SWEEP_OCT"))
    ap.add_argument("--until", default="17:00")
    args = ap.parse_args()
    run(clusters_path=Path(args.clusters), out_dir=Path(args.out), until=args.until)


if __name__ == "__main__":
    main()
