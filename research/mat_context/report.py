"""Shared reporting / kill-bar helpers for MAT context EXPs."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from research.backtest.mat_sim import SimSummary, TradeResult, merge_summaries
from research.correlated import cfade_family as F


def _one(parts: list[SimSummary]) -> dict:
    if not parts:
        return {"n": 0, "net": 0.0, "avg": None, "wr": None, "max_dd": 0.0, "days_pos": 0, "days": 0}
    s = merge_summaries(parts, F.COMMISSION)
    d = s.to_dict()
    d["avg"] = round(s.net / s.n, 3) if s.n else None
    # day stats
    day = F.day_nets_from_trades(s.trades)
    d["days"] = len(day)
    d["days_pos"] = sum(1 for v in day.values() if v > 0)
    nets = sorted(day.values(), reverse=True)
    top3 = sum(nets[:3]) if nets else 0.0
    d["top3_share"] = round(top3 / s.net, 3) if abs(s.net) > 1e-9 else None
    return d


def arm_pack(parts: list[SimSummary], parts_is: list[SimSummary], parts_oos: list[SimSummary]) -> dict:
    return {"all": _one(parts), "is": _one(parts_is), "oos": _one(parts_oos)}


def kill_vs_base(arm: dict, base: dict, null: dict | None = None, *, min_n: int = 50) -> dict:
    def beat(a, b):
        return (
            a["oos"]["avg"] is not None
            and b["oos"]["avg"] is not None
            and a["oos"]["avg"] > b["oos"]["avg"]
        )

    kill = {
        "oos_net_pos": arm["oos"]["net"] > 0,
        "beat_base_oos_avg": beat(arm, base),
        "n_ge_min": arm["all"]["n"] >= min_n,
    }
    if null is not None:
        kill["beat_null_oos_avg"] = beat(arm, null)
    kill["pass"] = all(kill.values())
    return kill


def bucket_stats(trades: list[TradeResult], labels: list[str], commission: float = F.COMMISSION) -> dict:
    """Descriptive bucket table keyed by label."""
    by: dict[str, list[TradeResult]] = {}
    for t, lab in zip(trades, labels):
        by.setdefault(lab, []).append(t)
    out = {}
    for lab, ts in sorted(by.items()):
        n = len(ts)
        nets = [t.pnl - commission for t in ts]
        wins = sum(1 for x in nets if x > 0)
        is_nets = [t.pnl - commission for t in ts if F.is_is(t.session_id)]
        oos_nets = [t.pnl - commission for t in ts if not F.is_is(t.session_id)]
        out[lab] = {
            "n": n,
            "wr": round(wins / n, 3) if n else None,
            "net": round(sum(nets), 2),
            "avg": round(sum(nets) / n, 3) if n else None,
            "is_n": len(is_nets),
            "is_net": round(sum(is_nets), 2),
            "oos_n": len(oos_nets),
            "oos_net": round(sum(oos_nets), 2),
            "oos_avg": round(sum(oos_nets) / len(oos_nets), 3) if oos_nets else None,
        }
    return out


def write_exp_report(out: Path, *, title: str, md_lines: list[str], report: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    import json
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)
