"""Quick dig EXP-0017 / EXP-0018."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from research.run_experiment import _mean

ROOT = Path(__file__).resolve().parents[1]


def dig(path: Path) -> dict:
    if not path.exists():
        return {}
    t = pq.read_table(path)
    sides = t.column("side").to_numpy()
    hit = t.column("hit_10pts").to_numpy()
    r10 = t.column("ret_pts_10000ms").to_numpy()
    out = {
        "n": int(t.num_rows),
        "hit10": _mean(hit),
        "r10s": _mean(r10),
        "by_side": {},
    }
    for s in (-1, 1):
        m = sides == s
        if m.any():
            out["by_side"][str(s)] = {
                "n": int(m.sum()),
                "hit10": _mean(hit[m]),
                "r10s": _mean(r10[m]),
            }
    if "span_ticks" in t.column_names:
        span = t.column("span_ticks").to_numpy()
        tight = span >= 2
        if tight.any():
            out["span_ge2"] = {"n": int(tight.sum()), "hit10": _mean(hit[tight])}
    if "n_prints" in t.column_names:
        np_ = t.column("n_prints").to_numpy()
        big = np_ >= 8
        if big.any():
            out["prints_ge8"] = {"n": int(big.sum()), "hit10": _mean(hit[big])}
    if "cancel_ratio" in t.column_names:
        cr = t.column("cancel_ratio").to_numpy()
        hi = cr >= 0.85
        if hi.any():
            out["cancel_ratio_ge85"] = {"n": int(hi.sum()), "hit10": _mean(hit[hi])}
    return out


def main() -> None:
    report = {
        "exp0017": dig(ROOT / "results/EXP-0017/events.parquet"),
        "exp0018": dig(ROOT / "results/EXP-0018/events.parquet"),
        "verdict": {
            "h12": "falsify_vs_exp0008 — dual sweep hit10 52% << 0008 85%",
            "h13": "falsify_vs_null — dual vacuum hit10 40% ≈ null 41%",
            "ship_bookmap": False,
        },
    }
    out = ROOT / "results/EXP-0017/dig.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
