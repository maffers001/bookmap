"""Validate lvn_profile against IRT *_ALL.csv LVN1-3 / HVN1-3."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from research.irt_lvn.lvn_profile import (
    MARKET_CFG,
    UK,
    aggregate_vap_window,
    compute_labeled_nodes,
    load_all_snapshot,
    match_score,
)

ROOT = Path(__file__).resolve().parents[2]

ES_DAYS = ["2026-10-07", "2026-10-09", "2026-10-13"]
NQ_DAYS = ["2026-10-07", "2026-10-20"]
TIMES = ["14:30", "15:00", "16:00"]


def run_market(market: str, days: list[str]) -> list[dict]:
    cfg = MARKET_CFG[market]
    tick = ROOT / cfg["tick_path"]
    allp = ROOT / cfg["all_path"]
    sens = cfg["sensitivity"]
    win_days = cfg["window_days"]
    results = []
    print(f"  {market}: sens={sens} ticks, window={win_days}d")

    variants = [
        {"strict": True, "skip_edge": True, "smooth": 0, "name": "strict+edge"},
        {"strict": False, "skip_edge": True, "smooth": 0, "name": "nonstrict+edge"},
        {"strict": True, "skip_edge": False, "smooth": 0, "name": "strict+noedge"},
        {"strict": True, "skip_edge": True, "smooth": 9, "name": "strict+smooth9"},
    ]

    for day in days:
        for hhmm in TIMES:
            actual = load_all_snapshot(allp, day, hhmm)
            if actual is None or actual["LVN3"] == 0.0:
                print(f"  skip {market} {day} {hhmm}: no ALL row")
                continue
            end = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=UK)
            start = end - timedelta(days=win_days)
            print(f"  aggregating {market} {day} {hhmm} window {start.date()}→{end}…")
            vap = aggregate_vap_window(market, start, end, tick_path=tick, root=ROOT)
            print(f"    vap rows={len(vap)} total_vol={sum(vap.values()):.0f}")
            close = actual["Close"]
            for var in variants:
                lvn, hvn, valleys, peaks = compute_labeled_nodes(
                    vap,
                    close,
                    sensitivity=sens,
                    strict=var["strict"],
                    skip_edge=var["skip_edge"],
                    smooth=var["smooth"],
                )
                lvn_sc = match_score(lvn, actual, ["LVN1", "LVN2", "LVN3"])
                hvn_sc = match_score(hvn, actual, ["HVN1", "HVN2", "HVN3"])
                row = {
                    "market": market,
                    "day": day,
                    "hhmm": hhmm,
                    "variant": var["name"],
                    "n_valleys": len(valleys),
                    "n_peaks": len(peaks),
                    "pred_lvn": lvn,
                    "pred_hvn": hvn,
                    "actual_lvn": {k: actual[k] for k in ("LVN1", "LVN2", "LVN3")},
                    "actual_hvn": {k: actual[k] for k in ("HVN1", "HVN2", "HVN3")},
                    "close": close,
                    "lvn_score": lvn_sc,
                    "hvn_score": hvn_sc,
                }
                results.append(row)
                print(
                    f"    {var['name']}: valleys={len(valleys)} peaks={len(peaks)} "
                    f"LVN hit={lvn_sc['hit_pct']:.0f}% med={lvn_sc['median']} "
                    f"pred={lvn} act={row['actual_lvn']}"
                )
    return results


def main() -> None:
    all_rows: list[dict] = []
    print("=== ES ===")
    all_rows += run_market("ES", ES_DAYS)
    print("=== NQ ===")
    all_rows += run_market("NQ", NQ_DAYS)

    # Summarise by variant
    from collections import defaultdict

    by = defaultdict(list)
    for r in all_rows:
        by[(r["market"], r["variant"])].append(r["lvn_score"])

    print("\n=== SUMMARY LVN ===")
    for (mkt, var), scores in sorted(by.items()):
        ns = [s for s in scores if s["n"]]
        if not ns:
            continue
        hit = sum(s["hit_pct"] * s["n"] for s in ns) / sum(s["n"] for s in ns)
        med = sorted(s["median"] for s in ns)[len(ns) // 2]
        print(f"  {mkt:3} {var:20} hit={hit:.1f}% median_err≈{med}")

    out = ROOT / "results" / "LVN_PROFILE_VALIDATE"
    out.mkdir(parents=True, exist_ok=True)
    # JSON-friendly
    serialisable = []
    for r in all_rows:
        serialisable.append(
            {
                **{k: r[k] for k in (
                    "market", "day", "hhmm", "variant", "n_valleys", "n_peaks", "close"
                )},
                "pred_lvn": r["pred_lvn"],
                "actual_lvn": r["actual_lvn"],
                "pred_hvn": r["pred_hvn"],
                "actual_hvn": r["actual_hvn"],
                "lvn_score": r["lvn_score"],
                "hvn_score": r["hvn_score"],
            }
        )
    (out / "validation.json").write_text(json.dumps(serialisable, indent=2), encoding="utf-8")
    print(f"\nWrote {out / 'validation.json'}")


if __name__ == "__main__":
    main()
