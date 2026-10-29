"""When does the pitch-and-catch fade become tradable? Fade returns from
confirm + {5,10,20,30}s for FALSE-tagged, notes-pitch&catch, PC, and TRUE
(control: fading TRUE should lose)."""
from __future__ import annotations

import statistics as st
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.config import load_config
from research.correlated.harness import label_in_points, load_pair
from research.momentum_slice.scan import HORIZONS, TARGETS
from research.momentum_slice.taglib import BASE, load_tagged_features

OFFSETS = (5, 10, 20, 30)


def stats(rows, key):
    xs = [r[key] for r in rows if r.get(key) is not None]
    if not xs:
        return "n=0"
    return (f"n={len(xs)} {st.mean(xs):+.1f}/{st.median(xs):+.1f} "
            f"P>5 {sum(x > 5 for x in xs)/len(xs):.2f} P<-5 {sum(x < -5 for x in xs)/len(xs):.2f}")


def main():
    rows = load_tagged_features()
    cfg = load_config(); pq_dir = Path(cfg["parquet_dir"])
    by_sid: dict[str, list] = {}
    for r in rows:
        by_sid.setdefault(r["session"], []).append(r)
    for sid, evs in by_sid.items():
        pair = load_pair(pq_dir, sid)
        if pair is None:
            continue
        for off in OFFSETS:
            tbl = pa.table({
                "event_ts_ns": np.array([int(float(r["confirm_ns"])) + off * 1_000_000_000 for r in evs], dtype=np.int64),
                "side": np.array([-int(float(r["side_int"])) for r in evs], dtype=np.int8),
            })
            lab = label_in_points(tbl, pair, HORIZONS, TARGETS)
            for i, r in enumerate(evs):
                if i >= lab.num_rows:
                    break
                r[f"fade{off}_ret10s"] = lab.column("ret_pts_10000ms")[i].as_py()
                r[f"fade{off}_ret60s"] = lab.column("ret_pts_60000ms")[i].as_py()
    subsets = {
        "FALSE tagged": [r for r in rows if r.get("lst_tag") == "FALSE"],
        "notes pitch&catch": [r for r in rows if "pitch" in (r.get("notes") or "").lower()],
        "FALSE late": [r for r in rows if r.get("lst_tag") == "FALSE" and r.get("tod") == "late"],
        "TRUE (fade control)": [r for r in rows if r.get("lst_tag") == "TRUE"],
        "ALL": rows,
    }
    lines = ["# Pitch-and-catch fade timing (entry = confirm + offset)", ""]
    for name, sel in subsets.items():
        lines.append(f"## {name} n={len(sel)}")
        for off in OFFSETS:
            lines.append(f"- +{off}s: 10s {stats(sel, f'fade{off}_ret10s')} | 60s {stats(sel, f'fade{off}_ret60s')}")
        lines.append("")
    (BASE / "features/pitch_catch_timing.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
