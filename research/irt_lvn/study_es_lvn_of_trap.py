"""ES ∩ LVN first-touch: does break-side aggressor size / trap delta improve holds?

At each frozen ES open-cluster ∩ LVN≤10 level, find first touch, then measure
tape OF in a short post-touch window:

  * break-side aggressors = sellers at BID support / buyers at ASK resistance
  * trap_delta = break_sz − hold_sz  (large ⇒ aggressors stuck the wrong side
    if the level subsequently holds)

Books:
  1. first_touch — baseline outside+2 · SL8/TP16 (no OF gate)
  2. Diagnostic terciles of break_sz / trap_delta on the same first-touch fills
  3. Live gate — only fade if trap_delta (and optionally break_sz) clears a
     threshold within the window; enter at signal time (market at then print)

    .venv\\Scripts\\python.exe -m research.irt_lvn.study_es_lvn_of_trap
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
from research.irt_lvn.study_es_lvn_vol_brackets import _fade, _load_month_levels, _pack
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")


def _hhmm_ns(y: int, m: int, d: int, hhmm: str) -> int:
    hh, mm = (int(x) for x in hhmm.split(":")[:2])
    return int(datetime(y, m, d, hh, mm, tzinfo=UK).timestamp() * 1e9)


def _first_touch_idx(ts, px, *, side: str, centre: float, band: float) -> int | None:
    if side == "BID":
        m = px <= centre + band
    else:
        m = px >= centre - band
    if not m.any():
        return None
    return int(np.argmax(m))


def _of_window(
    ts, px, sz, is_buy, *,
    i_touch: int,
    side: str,
    centre: float,
    win_ms: int,
) -> dict[str, float]:
    """Break-side / hold-side aggressor size and delta in [touch, touch+win]."""
    t0 = int(ts[i_touch])
    t1 = t0 + win_ms * 1_000_000
    # include touch print
    m = (ts >= t0) & (ts < t1)
    if not m.any():
        return {
            "break_sz": 0.0, "hold_sz": 0.0, "trap_delta": 0.0,
            "delta": 0.0, "buy_sz": 0.0, "sell_sz": 0.0,
            "n_prints": 0, "pierce_pts": 0.0, "max_beyond": 0.0,
        }
    pxw, szw, buyw = px[m], sz[m], is_buy[m]
    buy_sz = float(szw[buyw].sum())
    sell_sz = float(szw[~buyw].sum())
    # BID support: break = sell aggressors; ASK resistance: break = buy
    if side == "BID":
        break_sz, hold_sz = sell_sz, buy_sz
        beyond = centre - float(pxw.min())  # how far below centre
    else:
        break_sz, hold_sz = buy_sz, sell_sz
        beyond = float(pxw.max()) - centre
    return {
        "break_sz": break_sz,
        "hold_sz": hold_sz,
        "trap_delta": break_sz - hold_sz,
        "delta": buy_sz - sell_sz,
        "buy_sz": buy_sz,
        "sell_sz": sell_sz,
        "n_prints": int(m.sum()),
        "pierce_pts": max(0.0, beyond),
        "max_beyond": beyond,
    }


def _market_from(
    ts, px, *, side: str, entry: float, tp: float, sl: float,
    from_i: int, snap_ns: int, exit_slip: float, commission: float,
) -> dict[str, Any] | None:
    if from_i >= len(ts):
        return None
    ts2, px2 = ts[from_i:], px[from_i:]
    sign = 1.0 if side == "BID" else -1.0
    if side == "BID":
        hit_tp, hit_sl = px2 >= entry + tp, px2 <= entry - sl
        mfe = float(px2.max() - entry)
        mae = float(entry - px2.min())
    else:
        hit_tp, hit_sl = px2 <= entry - tp, px2 >= entry + sl
        mfe = float(entry - px2.min())
        mae = float(px2.max() - entry)
    if hit_tp.any() or hit_sl.any():
        j_tp = int(np.argmax(hit_tp)) if hit_tp.any() else 10**18
        j_sl = int(np.argmax(hit_sl)) if hit_sl.any() else 10**18
        if not hit_tp.any():
            j_tp = 10**18
        if not hit_sl.any():
            j_sl = 10**18
        reason, pnl = ("sl", -sl - exit_slip) if j_sl <= j_tp else ("tp", tp - exit_slip)
    else:
        reason = "time"
        pnl = sign * (float(px2[-1]) - entry) - exit_slip
    return {
        "pnl_net": pnl - commission,
        "exit": reason,
        "mfe": mfe,
        "mae": mae,
        "sl": sl,
        "tp": tp,
        "t_entry_s": (int(ts2[0]) - snap_ns) / 1e9,
        "entry": entry,
    }


def _tercile_masks(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Low / mid / high masks; ties go to mid via percentile ranks."""
    if len(x) == 0:
        z = np.array([], dtype=bool)
        return z, z, z
    lo, hi = np.nanpercentile(x, [33.333, 66.667])
    return x <= lo, (x > lo) & (x < hi), x >= hi


def run(
    *,
    out_dir: Path,
    near_pts: float,
    offset: float,
    until: str,
    band: float,
    win_ms: int,
    trap_delta_min: float,
    break_sz_min: float,
) -> None:
    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    months = {
        "OCT": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_OCT" / "clusters_with_lvn.csv",
        "SEP": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_SEP" / "clusters_with_lvn.csv",
        "MAY": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_MAY" / "clusters_with_lvn.csv",
        "AUG": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_AUG" / "clusters_with_lvn.csv",
    }

    exit_slip = PTS_PER_TICK
    commission = 0.125
    tp, sl = 16.0, 8.0

    lines = [
        "# ES ∩ LVN — break-side OF trap vs first-touch",
        "",
        f"Levels: ES · LVN≤{near_pts:g} · SL{sl:g}/TP{tp:g} · outside+{offset:g} baseline.",
        f"OF window: **[touch, +{win_ms} ms]**. "
        "Break-side = sell aggr at BID / buy aggr at ASK. "
        "`trap_delta = break_sz − hold_sz`.",
        f"Live gate defaults: trap_delta≥{trap_delta_min:g} and break_sz≥{break_sz_min:g} "
        "(also reported: session-relative top tercile).",
        "",
    ]

    all_events: list[dict] = []
    grid: list[dict] = []

    for mo, ann in months.items():
        if not ann.exists():
            print(f"{mo}: missing {ann}", flush=True)
            continue
        levels = _load_month_levels(ann, near_pts=near_pts)
        print(f"{mo}: {len(levels)} ES confluence levels", flush=True)

        sess_ids = sorted({lv["session_id"] for lv in levels})
        tapes: dict[str, dict] = {}
        for sid in sess_ids:
            y, m, d = session_id_to_ymd(sid)
            snap_ns = _hhmm_ns(y, m, d, "14:30")
            end_ns = _hhmm_ns(y, m, d, until)
            tr = _prefer_single_feed(_load_trades(pq_dir, sid, "ES"), pq_dir, sid, "ES")
            ts = tr.column("ts_ns").to_numpy().astype(np.int64)
            px = tr.column("price").to_numpy().astype(np.float64) * PTS_PER_TICK
            sz = tr.column("size").to_numpy().astype(np.float64)
            buy = tr.column("is_buy_aggressor").to_numpy().astype(bool)
            o = np.argsort(ts, kind="stable")
            ts, px, sz, buy = ts[o], px[o], sz[o], buy[o]
            msk = (ts >= snap_ns) & (ts < end_ns)
            tapes[sid] = {
                "ts": ts[msk], "px": px[msk], "sz": sz[msk], "buy": buy[msk],
                "snap_ns": snap_ns,
            }
            print(f"  {sid}: {int(msk.sum())} prints", flush=True)

        events: list[dict] = []
        for lv in levels:
            td = tapes[lv["session_id"]]
            ts, px, sz, buy = td["ts"], td["px"], td["sz"], td["buy"]
            snap_ns = td["snap_ns"]
            centre, side = lv["centre"], lv["side"]
            i = _first_touch_idx(ts, px, side=side, centre=centre, band=band)
            if i is None:
                events.append({**lv, "month": mo, "touched": 0})
                continue
            of = _of_window(
                ts, px, sz, buy, i_touch=i, side=side, centre=centre, win_ms=win_ms,
            )
            # when does trap gate first fire inside the window?
            trap_i = None
            if of["n_prints"] > 0:
                t0 = int(ts[i])
                t1 = t0 + win_ms * 1_000_000
                # cumulative scan
                br = ho = 0.0
                j = i
                while j < len(ts) and ts[j] < t1:
                    if buy[j]:
                        if side == "ASK":
                            br += sz[j]
                        else:
                            ho += sz[j]
                    else:
                        if side == "BID":
                            br += sz[j]
                        else:
                            ho += sz[j]
                    if (br - ho) >= trap_delta_min and br >= break_sz_min:
                        trap_i = j
                        break
                    j += 1

            # first-touch fade
            entry_ft = centre - offset if side == "BID" else centre + offset
            ft = _fade(
                ts, px, side=side, entry=entry_ft, tp=tp, sl=sl,
                snap_ns=snap_ns, exit_slip=exit_slip, commission=commission,
            )

            # live trap entry: market at print when gate fires; skip if already
            # blown through SL from centre
            trap_trd = None
            if trap_i is not None:
                blown = False
                if side == "BID" and float(px[i:trap_i + 1].min()) <= centre - sl:
                    blown = True
                if side == "ASK" and float(px[i:trap_i + 1].max()) >= centre + sl:
                    blown = True
                if not blown:
                    entry_px = float(px[trap_i])
                    trap_trd = _market_from(
                        ts, px, side=side, entry=entry_px, tp=tp, sl=sl,
                        from_i=trap_i, snap_ns=snap_ns,
                        exit_slip=exit_slip, commission=commission,
                    )

            events.append({
                **lv,
                "month": mo,
                "touched": 1,
                "t_touch_s": (int(ts[i]) - snap_ns) / 1e9,
                **of,
                "trap_i": trap_i,
                "trap_fired": int(trap_i is not None),
                "ft_pnl": ft["pnl_net"] if ft else None,
                "ft_exit": ft["exit"] if ft else None,
                "ft_filled": int(ft is not None),
                "trap_pnl": trap_trd["pnl_net"] if trap_trd else None,
                "trap_exit": trap_trd["exit"] if trap_trd else None,
                "trap_filled": int(trap_trd is not None),
            })

        all_events.extend(events)
        touched = [e for e in events if e.get("touched")]
        filled = [e for e in touched if e.get("ft_filled")]

        # --- books ---
        books: dict[str, list[dict]] = {
            "first_touch": [],
            "trap_gate_live": [],
            "ft_hi_trap_delta": [],
            "ft_lo_trap_delta": [],
            "ft_hi_break_sz": [],
            "ft_lo_break_sz": [],
            "ft_hi_both": [],  # top tercile trap_delta AND break_sz
        }

        def _ft_row(e: dict) -> dict:
            return {
                "date": e["date"], "session_id": e["session_id"],
                "side": e["side"], "centre": e["centre"],
                "pnl_net": e["ft_pnl"], "exit": e["ft_exit"],
                "t_entry_s": e["t_touch_s"], "month": mo,
                "break_sz": e["break_sz"], "trap_delta": e["trap_delta"],
                "hold_sz": e["hold_sz"], "pierce_pts": e["pierce_pts"],
                "sl": sl, "tp": tp,
            }

        for e in filled:
            books["first_touch"].append(_ft_row(e))

        for e in touched:
            if e.get("trap_filled") and e.get("trap_pnl") is not None:
                books["trap_gate_live"].append({
                    "date": e["date"], "session_id": e["session_id"],
                    "side": e["side"], "centre": e["centre"],
                    "pnl_net": e["trap_pnl"], "exit": e["trap_exit"],
                    "t_entry_s": e["t_touch_s"], "month": mo,
                    "break_sz": e["break_sz"], "trap_delta": e["trap_delta"],
                    "hold_sz": e["hold_sz"], "pierce_pts": e["pierce_pts"],
                    "sl": sl, "tp": tp,
                })

        if filled:
            td = np.asarray([e["trap_delta"] for e in filled], dtype=np.float64)
            bs = np.asarray([e["break_sz"] for e in filled], dtype=np.float64)
            lo_td, _, hi_td = _tercile_masks(td)
            lo_bs, _, hi_bs = _tercile_masks(bs)
            for e, a, b, c, d in zip(filled, lo_td, hi_td, lo_bs, hi_bs):
                row = _ft_row(e)
                if b:
                    books["ft_hi_trap_delta"].append(row)
                if a:
                    books["ft_lo_trap_delta"].append(row)
                if d:
                    books["ft_hi_break_sz"].append(row)
                if c:
                    books["ft_lo_break_sz"].append(row)
                if b and d:
                    books["ft_hi_both"].append(row)

        # hold / TP rate diagnostic
        def _tp_rate(evs: list[dict]) -> float | None:
            xs = [e for e in evs if e.get("ft_exit")]
            if not xs:
                return None
            return float(np.mean([e["ft_exit"] == "tp" for e in xs]))

        med_td = float(np.median([e["trap_delta"] for e in filled])) if filled else 0.0
        med_bs = float(np.median([e["break_sz"] for e in filled])) if filled else 0.0
        n_trap = sum(1 for e in touched if e.get("trap_fired"))

        lines += [
            f"## {mo}",
            "",
            f"Levels {len(levels)} · touched {len(touched)} · FT fills {len(filled)} · "
            f"trap gate fired {n_trap}",
            f"OF medians (fills): trap_delta **{med_td:.0f}** · break_sz **{med_bs:.0f}**",
            "",
            "| book | n | WR | net | avg | TP-rate | days+ | maxDD |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]

        for bname, trades in books.items():
            p = _pack(trades)
            tp_rate = None
            if trades:
                tp_rate = float(np.mean([t["exit"] == "tp" for t in trades]))
            grid.append({
                "month": mo, "book": bname,
                "n": p["n"], "wr": p["wr"], "net": p["net"], "avg": p["avg"],
                "days_plus": p["days_plus"], "days": p["days"], "maxDD": p["maxDD"],
                "tp_rate": tp_rate,
                "med_trap_delta": med_td, "med_break_sz": med_bs,
            })
            if p["n"] == 0:
                lines.append(f"| {bname} | 0 | — | — | — | — | — | — |")
            else:
                lines.append(
                    f"| {bname} | {p['n']} | {p['wr']:.0%} | {p['net']:+.1f} | "
                    f"{p['avg']:+.2f} | {tp_rate:.0%} | "
                    f"{p['days_plus']}/{p['days']} | {p['maxDD']:.0f} |"
                )

        # explicit hi vs lo lift
        hi = next(g for g in grid if g["month"] == mo and g["book"] == "ft_hi_trap_delta")
        lo = next(g for g in grid if g["month"] == mo and g["book"] == "ft_lo_trap_delta")
        hi_b = next(g for g in grid if g["month"] == mo and g["book"] == "ft_hi_break_sz")
        lo_b = next(g for g in grid if g["month"] == mo and g["book"] == "ft_lo_break_sz")

        def _lift(a, b, key="avg"):
            if not a["n"] or not b["n"] or a[key] is None or b[key] is None:
                return "—"
            return f"{a[key] - b[key]:+.2f}"

        lines += [
            "",
            f"Tercile lift (hi−lo avg): trap_delta {_lift(hi, lo)} · "
            f"break_sz {_lift(hi_b, lo_b)}",
            f"Tercile lift (hi−lo TP-rate): "
            f"trap_delta {_lift(hi, lo, 'tp_rate')} · "
            f"break_sz {_lift(hi_b, lo_b, 'tp_rate')}",
            "",
        ]

        # dump
        for bname in books:
            tpath = out_dir / f"trades_{mo}_{bname}.csv"
            with tpath.open("w", newline="", encoding="utf-8") as f:
                fields = [
                    "date", "session_id", "side", "centre", "pnl_net", "exit",
                    "t_entry_s", "break_sz", "trap_delta", "hold_sz", "pierce_pts",
                    "month",
                ]
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                for t in books[bname]:
                    w.writerow(t)

    # cross-month
    lines += [
        "## Cross-month",
        "",
        "| book | Oct n/net/avg/TPr | Sep | May | Aug | Sum net |",
        "|---|---|---|---|---|---:|",
    ]
    book_names = [
        "first_touch", "trap_gate_live",
        "ft_hi_trap_delta", "ft_lo_trap_delta",
        "ft_hi_break_sz", "ft_lo_break_sz", "ft_hi_both",
    ]
    for bname in book_names:
        cells = {}
        for mo in ("OCT", "SEP", "MAY", "AUG"):
            cells[mo] = next((x for x in grid if x["month"] == mo and x["book"] == bname), None)

        def cell(g):
            if not g or g["n"] == 0:
                return "—"
            tpr = f"{g['tp_rate']:.0%}" if g.get("tp_rate") is not None else "—"
            return f"{g['n']}/{g['net']:+.0f}/{g['avg']:+.2f}/{tpr}"

        snet = sum((cells[m]["net"] if cells[m] else 0.0) for m in cells)
        lines.append(
            f"| {bname} | {cell(cells['OCT'])} | {cell(cells['SEP'])} | "
            f"{cell(cells['MAY'])} | {cell(cells['AUG'])} | {snet:+.0f} |"
        )

    lines += [
        "",
        "## Verdict",
        "",
        "OF trap helps if **ft_hi_trap_delta** (or hi break_sz / hi_both) shows "
        "higher TP-rate and avg than **ft_lo_*** across months, and/or "
        "**trap_gate_live** beats **first_touch** on avg without collapsing n. "
        "Oct-only lift without Sep/May = reject.",
        "",
    ]

    epath = out_dir / "touch_of_events.csv"
    with epath.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "month", "date", "session_id", "side", "centre", "lvn_dist", "rank",
            "touched", "t_touch_s", "break_sz", "hold_sz", "trap_delta", "delta",
            "buy_sz", "sell_sz", "n_prints", "pierce_pts", "trap_fired",
            "ft_filled", "ft_pnl", "ft_exit", "trap_filled", "trap_pnl", "trap_exit",
        ]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for e in all_events:
            w.writerow(e)

    gpath = out_dir / "book_grid.csv"
    with gpath.open("w", newline="", encoding="utf-8") as f:
        if grid:
            w = csv.DictWriter(f, fieldnames=list(grid[0].keys()))
            w.writeheader()
            w.writerows(grid)

    summary = out_dir / "summary.md"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(summary.read_text(encoding="utf-8"))
    print(f"Wrote {summary}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "ES_LVN_OF_TRAP")
    ap.add_argument("--near", type=float, default=10.0)
    ap.add_argument("--offset", type=float, default=2.0)
    ap.add_argument("--until", default="17:00")
    ap.add_argument("--band", type=float, default=1.5)
    ap.add_argument("--win-ms", type=int, default=2000)
    ap.add_argument(
        "--trap-delta-min", type=float, default=40.0,
        help="Live gate: min break_sz − hold_sz",
    )
    ap.add_argument(
        "--break-sz-min", type=float, default=50.0,
        help="Live gate: min break-side aggressor size",
    )
    args = ap.parse_args()
    run(
        out_dir=args.out,
        near_pts=args.near,
        offset=args.offset,
        until=args.until,
        band=args.band,
        win_ms=args.win_ms,
        trap_delta_min=args.trap_delta_min,
        break_sz_min=args.break_sz_min,
    )


if __name__ == "__main__":
    main()
