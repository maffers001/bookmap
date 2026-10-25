"""Export live-addon parity reference for one ES session.

Reads annotated open-cluster ∩ LVN CSVs (same book as the frozen study) and
writes expected ``LVN SNAP`` / ``LVN ARM`` / ``LVN FILL`` lines plus a simple
``date,lvn2,lvn3`` file the Bookmap console can load for replay.

Defaults match the frozen outer book: LVN≤10 · outside+2 · SL8/TP24 ·
min-open-dist 5 · entry-until 17:00 · EOS 21:00.

    .venv\\Scripts\\python.exe -m research.irt_lvn.export_lvn_live_ref --session 011026
    .venv\\Scripts\\python.exe -m research.irt_lvn.export_lvn_live_ref --session 011026 --out results/LVN_REF_011026
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from research.config import ROOT, load_config
from research.correlated.harness import _prefer_single_feed
from research.irt_lvn.events import PTS_PER_TICK
from research.irt_lvn.load_irt import session_id_to_ymd
from research.irt_lvn.study_es_lvn_bracket_8_24 import _anchor_level, _fade, _open_mid
from research.irt_lvn.study_es_lvn_vol_brackets import _load_month_levels
from research.run_experiment import _load_trades

UK = ZoneInfo("Europe/London")

MONTH_ANN = {
    "04": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_APR" / "clusters_with_lvn.csv",
    "05": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_MAY" / "clusters_with_lvn.csv",
    "06": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_JUN" / "clusters_with_lvn.csv",
    "07": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_JUL" / "clusters_with_lvn.csv",
    "08": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_AUG" / "clusters_with_lvn.csv",
    "09": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_SEP" / "clusters_with_lvn.csv",
    "10": ROOT / "results" / "OPEN_CLUSTER_LVN_CONFLUENCE_OCT" / "clusters_with_lvn.csv",
}


def _hhmm_ns(y: int, m: int, d: int, hhmm: str) -> int:
    hh, mm = (int(x) for x in hhmm.split(":")[:2])
    return int(datetime(y, m, d, hh, mm, tzinfo=UK).timestamp() * 1e9)


def _fmt_uk(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=UK).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def run(
    *,
    session_id: str,
    out_dir: Path,
    near_pts: float,
    offset: float,
    sl: float,
    tp: float,
    min_open_dist: float,
    level_mode: str,
    entry_until: str,
    eos: str,
) -> None:
    y, m, d = session_id_to_ymd(session_id)
    date_iso = f"{y:04d}-{m:02d}-{d:02d}"
    ann = MONTH_ANN.get(f"{m:02d}")
    if ann is None or not ann.exists():
        raise SystemExit(f"No annotated confluence CSV for month {m:02d} (looked for {ann})")

    levels = [
        lv for lv in _load_month_levels(ann, near_pts=near_pts, market="ES")
        if lv["session_id"] == session_id
    ]
    cfg = load_config()
    pq_dir = Path(cfg["parquet_dir"])
    snap_ns = _hhmm_ns(y, m, d, "14:30")
    entry_until_ns = _hhmm_ns(y, m, d, entry_until)
    eos_ns = _hhmm_ns(y, m, d, eos)

    tr = _prefer_single_feed(_load_trades(pq_dir, session_id, "ES"), pq_dir, session_id, "ES")
    if tr.num_rows == 0:
        raise SystemExit(f"No ES trades for session {session_id}")
    import numpy as np
    ts = tr.column("ts_ns").to_numpy().astype(np.int64)
    px = tr.column("price").to_numpy().astype(np.float64) * PTS_PER_TICK
    o = np.argsort(ts, kind="stable")
    ts, px = ts[o], px[o]
    open_mid = _open_mid(ts, px, snap_ns)
    msk = (ts >= snap_ns) & (ts < eos_ns)
    ts2, px2 = ts[msk], px[msk]

    # Prefer LVN2/LVN3 labels from annotated all_lvn; else unique lvn_px values
    lvn2, lvn3 = 0.0, 0.0
    with ann.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("session_id") != session_id or r.get("market") != "ES":
                continue
            raw = r.get("all_lvn") or ""
            for part in raw.split(";"):
                part = part.strip()
                if part.startswith("LVN2:"):
                    try:
                        lvn2 = float(part.split(":", 1)[1])
                    except ValueError:
                        pass
                elif part.startswith("LVN3:"):
                    try:
                        lvn3 = float(part.split(":", 1)[1])
                    except ValueError:
                        pass
            if lvn2 or lvn3:
                break
    if not lvn2 and not lvn3:
        lvn_prices = sorted({
            float(lv["lvn_px"]) for lv in levels if lv.get("lvn_px") is not None
        })
        lvn2 = lvn_prices[0] if lvn_prices else 0.0
        lvn3 = lvn_prices[1] if len(lvn_prices) > 1 else lvn2

    out_dir.mkdir(parents=True, exist_ok=True)
    simple = out_dir / f"lvn_{date_iso}.csv"
    with simple.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "lvn2", "lvn3"])
        w.writerow([date_iso, f"{lvn2:.2f}", f"{lvn3:.2f}"])

    arms: list[dict] = []
    fills: list[dict] = []
    skipped_open = 0
    for lv in levels:
        level_px, level_src = _anchor_level(lv, level_mode=level_mode)
        if open_mid is None:
            continue
        dist_open = abs(level_px - open_mid)
        if dist_open < min_open_dist:
            skipped_open += 1
            continue
        entry = level_px - offset if lv["side"] == "BID" else level_px + offset
        arms.append({
            "side": lv["side"],
            "centre": lv["centre"],
            "level_px": level_px,
            "level_src": level_src,
            "entry": entry,
            "size": lv.get("size"),
            "lvn_px": lv.get("lvn_px"),
            "lvn_dist": lv.get("lvn_dist"),
            "dist_open": dist_open,
        })
        trd = _fade(
            ts2, px2, side=lv["side"], entry=entry, tp=tp, sl=sl,
            snap_ns=snap_ns, entry_until_ns=entry_until_ns,
            exit_slip=PTS_PER_TICK, commission=0.125,
        )
        if trd is None:
            continue
        fills.append({**arms[-1], **trd})

    md = out_dir / "summary.md"
    lines = [
        f"# LVN live ref — ES {session_id} ({date_iso})",
        "",
        f"Frozen: LVN≤{near_pts:g} · outer · outside+{offset:g} · SL{sl:g}/TP{tp:g} · "
        f"min-open {min_open_dist:g} · entry-until {entry_until} · EOS {eos}.",
        "",
        f"Open mid @14:30: **{open_mid:.2f}**" if open_mid is not None else "Open mid: —",
        f"LVN console file: `{simple.name}` → LVN2={lvn2:.2f} LVN3={lvn3:.2f}",
        f"Confluence levels in CSV: **{len(levels)}** · armed after min-open: **{len(arms)}** "
        f"(skipped open-adj {skipped_open}) · fills: **{len(fills)}**",
        "",
        "## Expected Bookmap log lines",
        "",
        "```",
        f"LVN SNAP ... openMid={open_mid:.2f} lvn2={lvn2:.2f} lvn3={lvn3:.2f} armed={len(arms)}"
        if open_mid is not None else "LVN SNAP ...",
    ]
    for a in arms:
        lines.append(
            f"LVN ARM {a['side']} centre={a['centre']:.2f} level={a['level_px']:.2f}"
            f"({a['level_src']}) entry={a['entry']:.2f} size={a['size']} "
            f"lvn@{(a['lvn_px'] or 0):.2f} distOpen={a['dist_open']:.2f}"
        )
    for t in fills:
        lines.append(
            f"LVN FILL {t['side']} entry={t['entry']:.2f} exit={t['exit']} "
            f"pnl={t['pnl_net']:+.2f} t_entry_s={t['t_entry_s']:.1f} "
            f"entry_ts={t.get('entry_ts', '')}"
        )
    lines += ["```", "", "## Arms", ""]
    if arms:
        lines += [
            "| side | centre | level | src | entry | size | lvn | distOpen |",
            "|---|---:|---:|---|---:|---:|---:|---:|",
        ]
        for a in arms:
            lines.append(
                f"| {a['side']} | {a['centre']:.2f} | {a['level_px']:.2f} | {a['level_src']} | "
                f"{a['entry']:.2f} | {a['size']} | {a['lvn_px']} | {a['dist_open']:.2f} |"
            )
    lines += ["", "## Fills", ""]
    if fills:
        lines += [
            "| side | entry | exit | pnl | t_entry_s | duration_s |",
            "|---|---:|---|---:|---:|---:|",
        ]
        for t in fills:
            lines.append(
                f"| {t['side']} | {t['entry']:.2f} | {t['exit']} | {t['pnl_net']:+.2f} | "
                f"{t['t_entry_s']:.1f} | {t['duration_s']:.1f} |"
            )
    else:
        lines.append("_No fills (tape never reached entry before entry-until)._")

    lines += [
        "",
        "## Replay checklist",
        "",
        "1. Build/install `bookmap-orderflow-lvn.jar`; enable **LVN Confluence AutoTrade (ES)**.",
        "2. Console → **Load LVN file…** → this folder's `lvn_YYYY-MM-DD.csv`.",
        "3. Leave **Trading OFF**; replay the session covering 14:30–21:00 UK.",
        "4. Grep Bookmap log for `LVN SNAP` / `LVN ARM` / `LVN FILL` and match above.",
        "5. Age-ranking may change cluster centres vs research CSV — investigate before enabling trading.",
        "",
    ]
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    arms_path = out_dir / "arms.csv"
    with arms_path.open("w", newline="", encoding="utf-8") as f:
        fields = ["side", "centre", "level_px", "level_src", "entry", "size", "lvn_px", "lvn_dist", "dist_open"]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(arms)
    fills_path = out_dir / "fills.csv"
    if fills:
        with fills_path.open("w", newline="", encoding="utf-8") as f:
            fields = list(fills[0].keys())
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(fills)

    print("\n".join(lines))
    print(f"Wrote {md}", flush=True)
    print(f"Wrote {simple}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", required=True, help="Session id DDMMYY")
    ap.add_argument("--out", default=None)
    ap.add_argument("--near", type=float, default=10.0)
    ap.add_argument("--offset", type=float, default=2.0)
    ap.add_argument("--sl", type=float, default=8.0)
    ap.add_argument("--tp", type=float, default=24.0)
    ap.add_argument("--min-open-dist", type=float, default=5.0)
    ap.add_argument("--level-mode", default="outer", choices=("cluster", "outer", "inner"))
    ap.add_argument("--entry-until", default="17:00")
    ap.add_argument("--eos", default="21:00")
    args = ap.parse_args()
    out = Path(args.out) if args.out else ROOT / "results" / f"LVN_REF_{args.session}"
    run(
        session_id=args.session,
        out_dir=out,
        near_pts=args.near,
        offset=args.offset,
        sl=args.sl,
        tp=args.tp,
        min_open_dist=args.min_open_dist,
        level_mode=args.level_mode,
        entry_until=args.entry_until,
        eos=args.eos,
    )


if __name__ == "__main__":
    main()
