"""First-touch fade of open clusters: limit at level, TP 10 / SL 5 (points).

Fade = buy BID (support) / sell ASK (resistance) on first print in the band.
Fill at limit (centre price). Exit at TP/SL or session end (17:00 UK).
No entry slip; optional 1-tick exit slip + commission.

    .venv\\Scripts\\python.exe -m research.irt_lvn.study_open_cluster_fade
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


def _fade_trade(
    ts: np.ndarray,
    px: np.ndarray,
    *,
    side: str,
    lo: float,
    hi: float,
    entry_px: float,
    tp: float,
    sl: float,
    snap_ns: int,
    exit_slip: float,
    commission: float,
) -> dict[str, Any]:
    """Limit fade: long if BID, short if ASK. Returns flat dict (no fill → hit=0)."""
    empty = {
        "filled": 0, "hit": 0, "pnl": 0.0, "pnl_net": 0.0,
        "exit": "no_touch", "t_entry_s": None, "t_exit_s": None,
        "mfe": None, "mae": None,
    }
    if len(ts) == 0:
        return empty
    in_band = (px >= lo) & (px <= hi)
    # also count pierce as fillable at limit (price went through → limit would have filled)
    if side == "ASK":
        fillable = in_band | (px >= lo)  # any print at/above ask band from below path
        # more precise: first time px >= lo (touched the resting ask)
        fillable = px >= lo
    else:
        fillable = px <= hi  # touched the resting bid
    if not fillable.any():
        return empty
    i = int(np.argmax(fillable))
    t_entry = int(ts[i])
    # remaining path after fill
    ts2, px2 = ts[i:], px[i:]
    if side == "BID":  # long
        tp_px = entry_px + tp
        sl_px = entry_px - sl
        # exit: first hit TP or SL
        hit_tp = px2 >= tp_px
        hit_sl = px2 <= sl_px
        mfe = float(px2.max() - entry_px)
        mae = float(entry_px - px2.min())
        sign = 1.0
    else:  # short
        tp_px = entry_px - tp
        sl_px = entry_px + sl
        hit_tp = px2 <= tp_px
        hit_sl = px2 >= sl_px
        mfe = float(entry_px - px2.min())
        mae = float(px2.max() - entry_px)
        sign = -1.0

    exit_reason = "time"
    j_exit = len(px2) - 1
    if hit_tp.any() or hit_sl.any():
        j_tp = int(np.argmax(hit_tp)) if hit_tp.any() else 10**18
        j_sl = int(np.argmax(hit_sl)) if hit_sl.any() else 10**18
        if not hit_tp.any():
            j_tp = 10**18
        if not hit_sl.any():
            j_sl = 10**18
        if j_sl < j_tp:
            j_exit = j_sl
            exit_reason = "sl"
            exit_px = sl_px
        elif j_tp < j_sl:
            j_exit = j_tp
            exit_reason = "tp"
            exit_px = tp_px
        else:
            # same bar — conservative: SL first
            j_exit = j_sl
            exit_reason = "sl"
            exit_px = sl_px
    else:
        exit_px = float(px2[j_exit])

    # exit slip against us
    if exit_reason == "tp":
        exit_fill = exit_px - sign * exit_slip  # long TP: sell lower; short TP: buy higher
    elif exit_reason == "sl":
        exit_fill = exit_px - sign * exit_slip  # slip worsens stop
    else:
        exit_fill = exit_px - sign * exit_slip

    pnl_gross = sign * (exit_fill - entry_px)
    # For TP/SL we used exact levels then applied slip on exit_fill; recompute cleanly:
    if exit_reason == "tp":
        pnl_gross = tp - exit_slip
    elif exit_reason == "sl":
        pnl_gross = -sl - exit_slip
    else:
        pnl_gross = sign * (float(px2[j_exit]) - entry_px) - exit_slip

    pnl_net = pnl_gross - commission
    return {
        "filled": 1,
        "hit": 1,
        "pnl": round(pnl_gross, 3),
        "pnl_net": round(pnl_net, 3),
        "exit": exit_reason,
        "t_entry_s": round((t_entry - snap_ns) / 1e9, 1),
        "t_exit_s": round((int(ts2[j_exit]) - snap_ns) / 1e9, 1),
        "mfe": round(mfe, 2),
        "mae": round(mae, 2),
    }


def _pack(rows: list[dict], label: str) -> dict[str, Any]:
    filled = [r for r in rows if r["filled"]]
    n = len(filled)
    if n == 0:
        return {"label": label, "n": 0, "n_levels": len(rows), "fill_rate": 0.0}
    pnl = np.asarray([r["pnl_net"] for r in filled], dtype=np.float64)
    wins = pnl > 0
    exits = {}
    for r in filled:
        exits[r["exit"]] = exits.get(r["exit"], 0) + 1
    # session nets
    by_sess: dict[str, float] = {}
    for r in filled:
        by_sess[r["session_id"]] = by_sess.get(r["session_id"], 0.0) + r["pnl_net"]
    sess_nets = np.asarray(list(by_sess.values()), dtype=np.float64)
    # equity path by trade order (date, t_entry)
    ordered = sorted(filled, key=lambda r: (r["date"], r["t_entry_s"] if r["t_entry_s"] is not None else 0))
    eq = np.cumsum([r["pnl_net"] for r in ordered])
    peak = np.maximum.accumulate(eq)
    dd = float((peak - eq).max()) if len(eq) else 0.0
    return {
        "label": label,
        "n_levels": len(rows),
        "n": n,
        "fill_rate": n / len(rows) if rows else 0.0,
        "wr": float(wins.mean()),
        "net": float(pnl.sum()),
        "avg": float(pnl.mean()),
        "gross_tp_sl": exits,
        "days": len(by_sess),
        "days_plus": int((sess_nets > 0).sum()),
        "maxDD": dd,
        "median_mfe": float(np.median([r["mfe"] for r in filled if r["mfe"] is not None])),
        "median_mae": float(np.median([r["mae"] for r in filled if r["mae"] is not None])),
    }


def _flat_only(rows: list[dict]) -> list[dict]:
    """Keep first fill per market; skip new entries while a prior trade on that market is open."""
    # Build open intervals per market chronologically
    filled = [r for r in rows if r["filled"]]
    filled = sorted(filled, key=lambda r: (r["date"], r["market"], r["t_entry_s"] or 0))
    keep_ids = set()
    # per (date, market) track busy until t_exit
    busy_until: dict[tuple[str, str], float] = {}
    for r in filled:
        key = (r["date"], r["market"])
        t0 = r["t_entry_s"] or 0.0
        t1 = r["t_exit_s"] or t0
        if key in busy_until and t0 < busy_until[key]:
            continue
        keep_ids.add(id(r))
        busy_until[key] = t1
    out = []
    for r in rows:
        if not r["filled"]:
            out.append(r)
        elif id(r) in keep_ids:
            out.append(r)
        else:
            skip = dict(r)
            skip["filled"] = 0
            skip["hit"] = 1
            skip["pnl"] = 0.0
            skip["pnl_net"] = 0.0
            skip["exit"] = "skipped_flat_only"
            out.append(skip)
    return out


def run(*, clusters_path: Path, out_dir: Path, tp: float, sl: float, until: str) -> None:
    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    clusters = list(csv.DictReader(clusters_path.open(encoding="utf-8")))
    out_dir.mkdir(parents=True, exist_ok=True)

    exit_slip = PTS_PER_TICK  # 0.25 pt
    # commission: NQ 0.25 / ES 0.125 (match open-book yamls)
    tape_cache: dict[tuple[str, str], tuple[np.ndarray, np.ndarray, int]] = {}
    rows: list[dict[str, Any]] = []

    for c in clusters:
        sid, role = c["session_id"], c["market"]
        y, m, d = session_id_to_ymd(sid)
        snap_ns = _hhmm_ns(y, m, d, c.get("snap_time_uk") or "14:30")
        end_ns = _hhmm_ns(y, m, d, until)
        key = (sid, role)
        if key not in tape_cache:
            ts, px = _tape(pq_dir, sid, role, snap_ns, end_ns)
            tape_cache[key] = (ts, px, snap_ns)
            print(f"  tape {sid} {role}: {len(ts)}", flush=True)
        ts, px, snap_ns = tape_cache[key]
        lo, hi = float(c["price_lo"]), float(c["price_hi"])
        if hi < lo:
            lo, hi = hi, lo
        entry = float(c["centre_price"])
        comm = 0.25 if role == "NQ" else 0.125
        tr = _fade_trade(
            ts, px, side=c["side"], lo=lo, hi=hi, entry_px=entry,
            tp=tp, sl=sl, snap_ns=snap_ns, exit_slip=exit_slip, commission=comm,
        )
        rows.append({
            **{k: c[k] for k in ("date", "session_id", "market", "side", "role_vs_mid",
                                  "centre_price", "price_lo", "price_hi", "size",
                                  "significance", "rank_in_side", "dist_mid_pts", "age_stratum")},
            "rank_in_side": int(c["rank_in_side"]),
            "significance": float(c["significance"]),
            **tr,
        })

    # variants
    packs = [
        _pack(rows, "all_clusters"),
        _pack([r for r in rows if r["rank_in_side"] == 1], "rank1_only"),
        _pack([r for r in rows if r["market"] == "NQ"], "NQ_all"),
        _pack([r for r in rows if r["market"] == "ES"], "ES_all"),
        _pack([r for r in rows if r["market"] == "NQ" and r["rank_in_side"] == 1], "NQ_rank1"),
        _pack([r for r in rows if r["market"] == "ES" and r["rank_in_side"] == 1], "ES_rank1"),
        _pack([r for r in rows if r.get("age_stratum") == "old"], "old_only"),
        _pack(_flat_only(rows), "flat_only_per_market"),
    ]

    out_csv = out_dir / "trades.csv"
    fields = [
        "date", "session_id", "market", "side", "role_vs_mid", "centre_price",
        "size", "significance", "rank_in_side", "dist_mid_pts", "age_stratum",
        "filled", "hit", "pnl", "pnl_net", "exit", "t_entry_s", "t_exit_s", "mfe", "mae",
    ]
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    lines = [
        f"# Open-cluster first-touch fade — October 2026",
        "",
        f"Rule: **fade** first touch (buy BID / sell ASK) limit at `centre_price`; "
        f"**TP {tp:g} / SL {sl:g}** pts; flatten at {until} UK if still open.",
        f"Costs: limit fill (no entry slip), exit slip {exit_slip} pt, commission NQ 0.25 / ES 0.125 pt/RT.",
        f"Levels: `{clusters_path.name}` (n={len(rows)}).",
        "",
        "## Results",
        "",
        "| book | n fills | fill% | WR | net pts | avg | days+ | days | maxDD | exits |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for p in packs:
        if p["n"] == 0:
            lines.append(f"| {p['label']} | 0 | — | — | — | — | — | — | — | — |")
            continue
        ex = ", ".join(f"{k}={v}" for k, v in sorted(p["gross_tp_sl"].items()))
        lines.append(
            f"| {p['label']} | {p['n']} | {p['fill_rate']:.0%} | {p['wr']:.1%} | "
            f"{p['net']:+.1f} | {p['avg']:+.2f} | {p['days_plus']}/{p['days']} | "
            f"{p['days']} | {p['maxDD']:.1f} | {ex} |"
        )

    # expectancy check vs random
    lines += [
        "",
        "## Notes",
        "",
        f"- Fixed RR is {tp/sl:.1f}:1; breakeven WR ≈ {sl/(tp+sl):.1%} before costs.",
        "- `flat_only_per_market` skips a new fade on NQ or ES while that market still has an open fade.",
        "- This is a single-month readout on the same levels used for the hit study — not an OOS claim.",
        "",
        f"Trades: `{out_csv.name}`",
    ]
    summary = out_dir / "summary.md"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"Wrote {summary}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clusters", default=str(ROOT / "results" / "open_clusters_oct2026.csv"))
    ap.add_argument("--out", default=str(ROOT / "results" / "OPEN_CLUSTER_FADE_TP10_SL5_OCT"))
    ap.add_argument("--tp", type=float, default=10.0)
    ap.add_argument("--sl", type=float, default=5.0)
    ap.add_argument("--until", default="17:00")
    args = ap.parse_args()
    run(
        clusters_path=Path(args.clusters),
        out_dir=Path(args.out),
        tp=args.tp,
        sl=args.sl,
        until=args.until,
    )


if __name__ == "__main__":
    main()
