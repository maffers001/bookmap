"""Blind 60-event manual review pack from the Sep-Oct LST scan.

  .venv\\Scripts\\python.exe -m research.momentum_slice.make_review_pack \
      results/MOMENTUM_SLICE/scan_sep_oct results/MOMENTUM_SLICE/review

Selection: score tiers (top/mid/low 20 each, score as in analyse.py),
deterministically shuffled so presentation order leaks nothing. Outcomes,
score and rank live only in review_key.csv — do not open it while tagging.
Fill in review_pack.csv: lst_tag TRUE/FALSE/UNSURE, reason_code
(PULLED/ABSORBED/NO_STACK/EXHAUSTED/ONE_MARKET/OTHER), notes.
"""
from __future__ import annotations

import csv
import random
import sys
from pathlib import Path


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "results/MOMENTUM_SLICE/scan_sep_oct")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "results/MOMENTUM_SLICE/review")
    out.mkdir(parents=True, exist_ok=True)
    with open(src / "candidates.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["score"] = min(min(float(r["nq_consume"]), float(r["es_consume"])), 1.2) \
            + 0.5 * min(float(r["nq_density"]), float(r["es_density"]))
    rows.sort(key=lambda r: -r["score"])
    tiers = [rows[i::3] for i in range(3)]  # interleaved tiers preserve side/TOD mix
    picked = [r for t in tiers for r in t[:20]]
    assert len(picked) == 60, len(picked)
    random.Random(42).shuffle(picked)
    pack_fields = ["event_id", "session", "side", "ignition_bst", "confirm_bst",
                   "nq_px_ignition", "es_px_ignition", "lst_tag", "reason_code", "notes"]
    with open(out / "review_pack.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=pack_fields); w.writeheader()
        for i, r in enumerate(picked, 1):
            w.writerow({**{k: r[k] for k in pack_fields[1:7]},
                        "event_id": f"LST-{i:03d}", "lst_tag": "", "reason_code": "", "notes": ""})
    key_fields = ["event_id"] + list(rows[0].keys())
    with open(out / "review_key.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=key_fields); w.writeheader()
        for i, r in enumerate(picked, 1):
            w.writerow({"event_id": f"LST-{i:03d}", **r})
    (out / "README.md").write_text(
        "# Blind LST review\n\nOpen each event in Bookmap Replay at `ignition_bst` (BST, "
        "NQ chart; check ES too). Fill `lst_tag` = TRUE (textbook slice-through: persistent "
        "stack in both markets, traded through, momentum follows), FALSE, or UNSURE; "
        "`reason_code` for FALSE: PULLED / ABSORBED / NO_STACK / EXHAUSTED / ONE_MARKET / OTHER. "
        "Do not open review_key.csv until tagging is done — it contains outcomes.\n",
        encoding="utf-8")
    print(f"60 events -> {out}/review_pack.csv (key: review_key.csv)")


if __name__ == "__main__":
    main()
