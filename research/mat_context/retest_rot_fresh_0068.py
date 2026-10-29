"""Retest EXP-0068 rot_fresh_0.90 on the current clean trades sample.

rot_fresh is trades-only (NQ swings); depth is not required. Original run was
on 113 sessions — March was thin/missing then because trades parquet was not
converted, not because depth gated the sample.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from research.correlated import cfade_family as F
from research.correlated.study_combined_book import _plot_equity
from research.mat_context.cache import build_cache
from research.mat_context.plot_0075_equity import _path
from research.mat_context.run_queue import _null_book, _pack_book, _resim_masked, kill_vs_base
from research.run_cfade_all_months import _sid_chrono_key

ARM = "rot_fresh_0.90"
COMM = F.COMMISSION
MONTH_NAMES = {
    "03": "Mar", "04": "Apr", "05": "May", "06": "Jun",
    "07": "Jul", "08": "Aug", "09": "Sep", "10": "Oct",
}


def _month(sid: str) -> str:
    return sid[2:4]


def main() -> None:
    print("Building MAT trade cache (trades-only; no depth)...", flush=True)
    cache = build_cache(verbose=False)
    sessions = sorted(cache.sessions, key=_sid_chrono_key)
    mar = [s for s in sessions if _month(s) == "03"]
    jun = [s for s in sessions if _month(s) == "06"]
    print(f"sessions={len(sessions)} excl={sorted(F.EXCLUDE)}", flush=True)
    print(f"March={len(mar)} {mar}", flush=True)
    print(f"June={len(jun)} (still excl 200626/220626)", flush=True)

    print(f"Re-sim {ARM}...", flush=True)
    sims = _resim_masked(cache, ARM)
    base = _pack_book({sid: cache.bundles[sid].sim for sid in cache.sessions})
    null = _pack_book(_null_book(cache, seed0=6800))
    arm = _pack_book(sims)
    kill = kill_vs_base(arm, base, null)

    trades = []
    for s in sims.values():
        trades.extend(s.trades)
    day: dict[str, float] = defaultdict(float)
    for t in trades:
        day[t.session_id] += t.pnl - COMM

    by_m: dict[str, dict] = {}
    for m, nm in MONTH_NAMES.items():
        sids = [s for s in sessions if _month(s) == m]
        tr = [t for t in trades if t.session_id in set(sids)]
        n = len(tr)
        wins = sum(1 for t in tr if (t.pnl - COMM) > 0)
        net = sum(day.get(s, 0.0) for s in sids)
        by_m[m] = {
            "name": nm,
            "days": len(sids),
            "n": n,
            "wr": round(wins / n, 3) if n else None,
            "net": round(net, 1),
            "days_pos": sum(1 for s in sids if day.get(s, 0.0) > 0),
        }

    out = Path("results/RETEST_ROT_FRESH_0068")
    out.mkdir(parents=True, exist_ok=True)
    _plot_equity(
        _path(dict(day), sessions),
        out / "equity_rot_fresh.svg",
        f"EXP-0068 {ARM} retest ({len(sessions)}d)",
    )
    _plot_equity(
        _path({s: day.get(s, 0.0) for s in mar}, mar),
        out / "equity_rot_fresh_march.svg",
        f"{ARM} — March 2026 only",
    )

    a, o = arm["all"], arm["oos"]
    verdict = "PASS" if kill.get("pass") else "FAIL"
    lines = [
        "# Retest — MAT rot_fresh_0.90 (EXP-0068 / H59)",
        "",
        "**Depth:** not required (NQ trades swings only). Original EXP-0068 ran on",
        "**113** clean sessions; March days were missing because trades parquet was",
        "not converted yet — not because depth gated the sample. Retest on the",
        "current clean sample.",
        "",
        f"Clean sessions: **{len(sessions)}** (excl. `{sorted(F.EXCLUDE)}`).",
        f"March: {len(mar)} · June: {len(jun)} (June still excludes misfiled 200626/220626).",
        "",
        "## Headline vs original 113d",
        "",
        "| sample | n | WR | net | OOS net | OOS avg | kill |",
        "|---|---:|---:|---:|---:|---:|---|",
        "| original EXP-0068 (113d) | 1031 | 0.526 | +501.4 | +212.2 | 0.254 | PASS |",
        (
            f"| retest ({len(sessions)}d) | {a['n']} | {a.get('wr')} | "
            f"{a['net']:+.1f} | {o['net']:+.1f} | {o.get('avg')} | {verdict} |"
        ),
        "",
        "## By month (retest)",
        "",
        "| month | days | n | WR | net | days+ |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for m in MONTH_NAMES:
        st = by_m[m]
        lines.append(
            f"| {st['name']} | {st['days']} | {st['n']} | {st['wr']} | "
            f"{st['net']:+.1f} | {st['days_pos']}/{st['days']} |"
        )
    lines += [
        "",
        "## Kill detail",
        "",
        "```json",
        json.dumps(kill, indent=2),
        "```",
        "",
        "## Charts",
        "",
        "![all](equity_rot_fresh.svg)",
        "",
        "![march](equity_rot_fresh_march.svg)",
        "",
    ]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "report.json").write_text(
        json.dumps(
            {
                "n_sessions": len(sessions),
                "march": mar,
                "june": jun,
                "books": {"base": base, "null": null, ARM: arm},
                "kill": kill,
                "by_month": by_m,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("\n".join(lines), flush=True)
    print(f"Wrote {out}", flush=True)


if __name__ == "__main__":
    main()
