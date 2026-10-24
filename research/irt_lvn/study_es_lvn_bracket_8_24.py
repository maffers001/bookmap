"""ES ∩ LVN≤10 first-touch fade — multi-month SL/TP sweep.

Uses existing annotated confluence CSVs (no retune of levels/entry).

Level anchor (``--level-mode``):
  * ``cluster`` (default) — always fade the large-order cluster centre
  * ``outer`` — far wall of the pair: buy uses min(cluster, LVN),
    sell uses max(cluster, LVN); then apply outside offset as usual
  * ``inner`` — near wall (opposite): buy max / sell min

Windows (UK clock):
  * ``--entry-until`` (default **17:00**) — last time a new fill is allowed
  * ``--eos`` (default **21:00**) — hold open trades to this clock;
    if TP never hits → flatten as ``exit=endofsession``

Exits: ``tp`` when target hits; else ``endofsession`` at ``--eos``.
Use ``--sl 0`` for TP-or-EOS only (no stop); ``--sl N`` keeps a hard stop.

    .venv\\Scripts\\python.exe -m research.irt_lvn.study_es_lvn_bracket_8_24 --sl 8 --tp 16 --level-mode outer
    .venv\\Scripts\\python.exe -m research.irt_lvn.study_es_lvn_bracket_8_24 --sl 0 --tp 60 --level-mode outer --eos 21:00
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
from research.irt_lvn.study_es_lvn_vol_brackets import _load_month_levels, _pack
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")

LEVEL_MODES = ("cluster", "outer", "inner")

MONTHS = {
    "APR": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_APR" / "clusters_with_lvn.csv",
    "MAY": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_MAY" / "clusters_with_lvn.csv",
    "JUN": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_JUN" / "clusters_with_lvn.csv",
    "JUL": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_JUL" / "clusters_with_lvn.csv",
    "AUG": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_AUG" / "clusters_with_lvn.csv",
    "SEP": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_SEP" / "clusters_with_lvn.csv",
    "OCT": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_OCT" / "clusters_with_lvn.csv",
}


def _hhmm_ns(y: int, m: int, d: int, hhmm: str) -> int:
    hh, mm = (int(x) for x in hhmm.split(":")[:2])
    return int(datetime(y, m, d, hh, mm, tzinfo=UK).timestamp() * 1e9)


def _parse_floats(s: str) -> list[float]:
    out = [float(x) for x in s.split(",") if x.strip()]
    if not out:
        raise ValueError(f"expected at least one number, got {s!r}")
    return out


def _brackets(sls: list[float], tps: list[float]) -> list[tuple[str, float, float]]:
    """Cartesian product of SLs × TPs → (name, tp, sl)."""
    out: list[tuple[str, float, float]] = []
    for sl in sls:
        for tp in tps:
            def _tag(x: float) -> str:
                return f"{x:g}".replace(".", "p")

            out.append((f"fixed_{_tag(sl)}_{_tag(tp)}", float(tp), float(sl)))
    return out


def _anchor_level(lv: dict, *, level_mode: str) -> tuple[float, str]:
    """Return (level_px, source) used before applying outside offset.

    ``cluster``: large-order centre (legacy).
    ``outer``: far wall — BID/buy → min(cluster, LVN); ASK/sell → max(cluster, LVN).
    ``inner``: near wall — BID/buy → max(cluster, LVN); ASK/sell → min(cluster, LVN).
    """
    centre = float(lv["centre"])
    if level_mode == "cluster":
        return centre, "cluster"
    if level_mode not in ("outer", "inner"):
        raise ValueError(f"unknown level_mode {level_mode!r}")
    lvn = lv.get("lvn_px")
    if lvn is None:
        return centre, "cluster"  # no LVN px → fall back
    lvn = float(lvn)
    side = lv["side"]
    if level_mode == "outer":
        # sell: higher of the two; buy: lower of the two
        if side == "ASK":
            return (lvn, "lvn") if lvn >= centre else (centre, "cluster")
        return (lvn, "lvn") if lvn <= centre else (centre, "cluster")
    # inner: sell lower / buy higher
    if side == "ASK":
        return (centre, "cluster") if lvn >= centre else (lvn, "lvn")
    return (centre, "cluster") if lvn <= centre else (lvn, "lvn")


def _fade(
    ts,
    px,
    *,
    side: str,
    entry: float,
    tp: float,
    sl: float,
    snap_ns: int,
    entry_until_ns: int,
    exit_slip: float,
    commission: float,
) -> dict[str, Any] | None:
    """First-touch fade: fill only before ``entry_until_ns``; hold to tape end.

    Exits: ``tp`` if target hits; ``sl`` if stop hits first (``sl`` > 0);
    else ``endofsession`` at last print (caller truncates tape at EOS).
    """
    if len(ts) == 0:
        return None
    fillable = (px <= entry if side == "BID" else px >= entry) & (ts < entry_until_ns)
    if not fillable.any():
        return None
    i = int(np.argmax(fillable))
    px2 = px[i:]
    sign = 1.0 if side == "BID" else -1.0
    use_sl = sl > 0
    if side == "BID":
        hit_tp = px2 >= entry + tp
        hit_sl = (px2 <= entry - sl) if use_sl else np.zeros(len(px2), dtype=bool)
        mfe, mae = float(px2.max() - entry), float(entry - px2.min())
    else:
        hit_tp = px2 <= entry - tp
        hit_sl = (px2 >= entry + sl) if use_sl else np.zeros(len(px2), dtype=bool)
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
        # Still open at EOS → flatten only because TP was not hit
        reason, pnl = "endofsession", sign * (float(px2[-1]) - entry) - exit_slip
    return {
        "pnl_net": pnl - commission,
        "exit": reason,
        "mfe": mfe,
        "mae": mae,
        "sl": sl,
        "tp": tp,
        "t_entry_s": (int(ts[i]) - snap_ns) / 1e9,
    }


def _md_table(
    headers: list[str],
    rows: list[list[str]],
    *,
    right: frozenset[int] | set[int] | None = None,
) -> list[str]:
    """Markdown table with space-padded columns so terminals stay aligned."""
    right = set(right or ())
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _cell(i: int, text: str) -> str:
        return text.rjust(widths[i]) if i in right else text.ljust(widths[i])

    head = "| " + " | ".join(_cell(i, h) for i, h in enumerate(headers)) + " |"
    sep_parts = []
    for i, w in enumerate(widths):
        if i in right:
            sep_parts.append("-" * max(1, w - 1) + ":")
        else:
            sep_parts.append("-" * w)
    sep = "| " + " | ".join(sep_parts) + " |"
    body = [
        "| " + " | ".join(_cell(i, c) for i, c in enumerate(row)) + " |"
        for row in rows
    ]
    return [head, sep, *body]


TRADE_FIELDS = [
    "date", "session_id", "side", "centre", "lvn_px", "level_px",
    "level_src", "level_mode", "entry", "sl", "tp",
    "pnl_net", "exit", "mfe", "mae", "t_entry_s", "bracket", "month",
]


def run(
    *,
    out_dir: Path,
    near_pts: float,
    offset: float,
    entry_until: str,
    eos: str,
    sls: list[float],
    tps: list[float],
    level_mode: str,
) -> None:
    if level_mode not in LEVEL_MODES:
        raise ValueError(f"--level-mode must be one of {LEVEL_MODES}, got {level_mode!r}")

    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    brackets = _brackets(sls, tps)
    exit_slip = PTS_PER_TICK
    commission = 0.125
    br_desc = ", ".join(f"SL{sl:g}/TP{tp:g}" for _, tp, sl in brackets)
    mode_desc = {
        "cluster": "large-order cluster centre (legacy)",
        "outer": "outer wall: buy min(cluster,LVN) / sell max(cluster,LVN)",
        "inner": "inner wall: buy max(cluster,LVN) / sell min(cluster,LVN)",
    }[level_mode]

    lines = [
        "# ES ∩ LVN — SL/TP sweep",
        "",
        f"Structure: ES · LVN≤{near_pts:g} · outside+{offset:g} · first-touch fade.",
        f"Fills until **{entry_until} UK**; hold to **{eos} UK** — "
        f"`exit=endofsession` only if TP not hit"
        + ("; SL active." if any(sl > 0 for _, _, sl in brackets) else " (no SL)."),
        f"Level mode: **`{level_mode}`** — {mode_desc}.",
        f"Brackets: **{br_desc}**.",
        "",
    ]
    grid: list[dict] = []
    month_rows: list[list[str]] = []
    all_trades: list[dict[str, Any]] = []
    n_switched = 0
    n_levels_total = 0

    for mo, ann in MONTHS.items():
        if not ann.exists():
            print(f"{mo}: missing {ann}", flush=True)
            continue
        levels = _load_month_levels(ann, near_pts=near_pts)
        print(f"{mo}: {len(levels)} ES confluence levels", flush=True)
        n_levels_total += len(levels)

        sess_ids = sorted({lv["session_id"] for lv in levels})
        tapes: dict[str, tuple] = {}
        for sid in sess_ids:
            y, m, d = session_id_to_ymd(sid)
            snap_ns = _hhmm_ns(y, m, d, "14:30")
            entry_until_ns = _hhmm_ns(y, m, d, entry_until)
            eos_ns = _hhmm_ns(y, m, d, eos)
            tr = _prefer_single_feed(_load_trades(pq_dir, sid, "ES"), pq_dir, sid, "ES")
            ts = tr.column("ts_ns").to_numpy().astype(np.int64)
            px = tr.column("price").to_numpy().astype(np.float64) * PTS_PER_TICK
            o = np.argsort(ts, kind="stable")
            ts, px = ts[o], px[o]
            # Hold path through EOS; fills gated inside _fade via entry_until_ns
            msk = (ts >= snap_ns) & (ts < eos_ns)
            tapes[sid] = (ts[msk], px[msk], snap_ns, entry_until_ns)

        for bname, tp, sl in brackets:
            trades: list[dict[str, Any]] = []
            for lv in levels:
                ts, px, snap_ns, entry_until_ns = tapes[lv["session_id"]]
                level_px, level_src = _anchor_level(lv, level_mode=level_mode)
                if level_src == "lvn":
                    n_switched += 1
                entry = level_px - offset if lv["side"] == "BID" else level_px + offset
                trd = _fade(
                    ts, px, side=lv["side"], entry=entry, tp=tp, sl=sl,
                    snap_ns=snap_ns, entry_until_ns=entry_until_ns,
                    exit_slip=exit_slip, commission=commission,
                )
                if trd is None:
                    continue
                trades.append({
                    **trd,
                    "date": lv["date"],
                    "session_id": lv["session_id"],
                    "side": lv["side"],
                    "centre": lv["centre"],
                    "lvn_px": lv.get("lvn_px"),
                    "level_px": level_px,
                    "level_src": level_src,
                    "level_mode": level_mode,
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "bracket": bname,
                    "month": mo,
                })
            p = _pack(trades)
            exits = {"tp": 0, "sl": 0, "endofsession": 0}
            for t in trades:
                exits[t["exit"]] = exits.get(t["exit"], 0) + 1
            grid.append({
                "month": mo, "bracket": bname, "level_mode": level_mode,
                "n": p["n"], "wr": p["wr"], "net": p["net"], "avg": p["avg"],
                "days_plus": p["days_plus"], "days": p["days"], "maxDD": p["maxDD"],
                "n_tp": exits["tp"], "n_sl": exits["sl"],
                "n_endofsession": exits["endofsession"],
            })
            if p["n"] == 0:
                month_rows.append([mo, bname, "0", "—", "—", "—", "—", "—", "—"])
            else:
                month_rows.append([
                    mo, bname, str(p["n"]), f"{p['wr']:.0%}",
                    f"{p['net']:+.1f}", f"{p['avg']:+.2f}",
                    f"{p['days_plus']}/{p['days']}", f"{p['maxDD']:.0f}",
                    f"tp={exits['tp']} sl={exits['sl']} eos={exits['endofsession']}",
                ])
            tpath = out_dir / f"trades_{mo}_{bname}.csv"
            with tpath.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
                w.writeheader()
                for t in trades:
                    w.writerow(t)
            all_trades.extend(trades)

    # n_switched counts per (level × bracket); report once per level
    n_br = max(len(brackets), 1)
    switched_levels = n_switched // n_br if n_br else 0

    lines += _md_table(
        ["Month", "bracket", "n", "WR", "net", "avg", "days+", "maxDD", "exits"],
        month_rows,
        right={2, 3, 4, 5, 6, 7},
    )
    lines += [
        "",
        f"LVN-anchor switches (LVN chosen over cluster): **{switched_levels}** / "
        f"{n_levels_total} levels (mode=`{level_mode}`).",
        "",
        "## Cross-month",
        "",
    ]

    cross_headers = ["bracket", *MONTHS.keys(), "Sum net", "+months"]
    cross_rows: list[list[str]] = []
    for bname, _, _ in brackets:
        cells: list[str] = [bname]
        snet = 0.0
        npos = 0
        for mo in MONTHS:
            g = next((x for x in grid if x["month"] == mo and x["bracket"] == bname), None)
            if not g or g["n"] == 0:
                cells.append("—")
                continue
            cells.append(f"{g['n']}/{g['net']:+.0f}/{g['avg']:+.2f}")
            snet += g["net"]
            if g["net"] > 0:
                npos += 1
        cells.append(f"{snet:+.0f}")
        cells.append(f"{npos}/{len(MONTHS)}")
        cross_rows.append(cells)
    # right-align numeric-ish columns (all except bracket)
    lines += _md_table(
        cross_headers, cross_rows,
        right=set(range(1, len(cross_headers))),
    )

    if len(brackets) >= 2:
        base_name, base_tp, base_sl = brackets[0]
        lines += [
            "",
            f"## Lift vs baseline `{base_name}` (SL{base_sl:g}/TP{base_tp:g})",
            "",
        ]
        for bname, tp, sl in brackets[1:]:
            lift_rows: list[list[str]] = []
            sum_base = sum_cand = 0.0
            for mo in MONTHS:
                a = next((x for x in grid if x["month"] == mo and x["bracket"] == base_name), None)
                b = next((x for x in grid if x["month"] == mo and x["bracket"] == bname), None)
                if not a or not b or a["n"] == 0:
                    lift_rows.append([mo, "—", "—"])
                    continue
                sum_base += a["net"]
                sum_cand += b["net"]
                davg = (
                    (b["avg"] - a["avg"])
                    if a["avg"] is not None and b["avg"] is not None else None
                )
                lift_rows.append([
                    mo,
                    f"{b['net'] - a['net']:+.1f}",
                    f"{davg:+.2f}" if davg is not None else "—",
                ])
            lift_rows.append([
                "Sum",
                f"{sum_cand - sum_base:+.1f}",
                "—",
            ])
            lines += [
                f"### `{bname}` − `{base_name}`",
                "",
                *_md_table(["Month", "Δnet", "Δavg"], lift_rows, right={1, 2}),
                "",
            ]

    sums = []
    for bname, tp, sl in brackets:
        s = sum(
            g["net"] for g in grid
            if g["bracket"] == bname and g["month"] in MONTHS
        )
        sums.append((bname, tp, sl, s))
    best = max(sums, key=lambda x: x[3]) if sums else None
    lines += [
        "## Verdict",
        "",
        " · ".join(f"`{n}` sum **{s:+.1f}**" for n, _, _, s in sums) + ".",
    ]
    if best:
        lines.append(
            f"Best on this sample: **`{best[0]}`** (SL{best[2]:g}/TP{best[1]:g}) "
            f"at **{best[3]:+.1f}** — treat as descriptive; do not retune the level set on one cell."
        )
    lines.append("")

    out_dir = out_dir.resolve()
    gpath = out_dir / "book_grid.csv"
    with gpath.open("w", newline="", encoding="utf-8") as f:
        if grid:
            w = csv.DictWriter(f, fieldnames=list(grid[0].keys()))
            w.writeheader()
            w.writerows(grid)

    trades_all = out_dir / "trades_ALL.csv"
    with trades_all.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
        w.writeheader()
        for t in sorted(all_trades, key=lambda r: (r["date"], r["t_entry_s"], r["bracket"])):
            w.writerow(t)

    summary = out_dir / "summary.md"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(summary.read_text(encoding="utf-8"))
    print("## Output files", flush=True)
    print(f"  Spreadsheet (all trades): {trades_all}", flush=True)
    print(f"  Month grid CSV:           {gpath}", flush=True)
    print(f"  Summary markdown:         {summary}", flush=True)
    print(f"  Per-month trades:         {out_dir / 'trades_<MONTH>_<bracket>.csv'}", flush=True)
    print(f"Wrote {len(all_trades)} trades → {trades_all}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "ES_LVN_BRACKET_8_24")
    ap.add_argument("--near", type=float, default=10.0)
    ap.add_argument("--offset", type=float, default=2.0)
    ap.add_argument(
        "--entry-until", default="17:00",
        help="UK clock: no new fills at/after this time (default 17:00)",
    )
    ap.add_argument(
        "--eos", default="21:00",
        help=(
            "UK clock: hold open trades to this time; flatten as exit=endofsession "
            "only if TP was not hit (default 21:00)"
        ),
    )
    ap.add_argument(
        "--until", default=None,
        help=argparse.SUPPRESS,  # legacy alias for --eos
    )
    ap.add_argument(
        "--sl", default="8",
        help="Stop loss in pts (0=disabled → TP or endofsession only; comma-sep for sweep)",
    )
    ap.add_argument(
        "--tp", default="16,24",
        help="Take profit in pts (comma-separated for a sweep, e.g. 16,24)",
    )
    ap.add_argument(
        "--level-mode",
        choices=LEVEL_MODES,
        default="cluster",
        help=(
            "cluster=large-order centre (default); "
            "outer=buy min/sell max with LVN; "
            "inner=buy max/sell min with LVN"
        ),
    )
    args = ap.parse_args()
    sls = _parse_floats(args.sl)
    tps = _parse_floats(args.tp)
    eos = args.until if args.until is not None else args.eos
    run(
        out_dir=args.out,
        near_pts=args.near,
        offset=args.offset,
        entry_until=args.entry_until,
        eos=eos,
        sls=sls,
        tps=tps,
        level_mode=args.level_mode,
    )


if __name__ == "__main__":
    main()
