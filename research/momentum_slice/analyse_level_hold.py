"""Dominant-level hold-on-pullback: confirmation + stop placement.

States after clearing the dominant resting level (within 30 s of confirm):
  HOLD        — pullback approached within 1.5 pts of the level and never
                breached through it
  BREACH      — price traded back through the level
  NO_RETEST   — cleared and ran without coming back near it
  NEVER_CLEARED

Stop placement (confirm entry): SL at the level / 1 pt beyond.
Confirmation entry: enter at HOLD stamp; outcomes labelled from hold_ts
(causal). BREACH/HOLD filters applied to confirm-stamp returns are
descriptive only (look-ahead) — called out as such.
"""
from __future__ import annotations

import csv
import statistics as st
from collections import Counter
from pathlib import Path

import numpy as np
import pyarrow as pa

from research.config import load_config
from research.correlated.harness import label_in_points, load_pair
from research.momentum_slice.scan import HORIZONS, TARGETS

BASE = Path("results/MOMENTUM_SLICE")


def f(r, k):
    v = r.get(k)
    if v in ("", "nan", None):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def ret_stats(rows, key="ret10s"):
    if not rows:
        return "n=0"
    xs = [f(r, key) for r in rows]
    xs = [x for x in xs if x is not None]
    if not xs:
        return "n=0"
    mae = [f(r, "mae") for r in rows]
    mae = [x for x in mae if x is not None]
    return (f"n={len(xs)} {key} mean {st.mean(xs):+.1f} med {st.median(xs):+.1f} "
            f"P>5 {sum(x > 5 for x in xs)/len(xs):.2f} "
            f"P<-5 {sum(x < -5 for x in xs)/len(xs):.2f} "
            f"MAE {st.mean(mae):.0f}" if mae else f"n={len(xs)}")


def main():
    rows = list(csv.DictReader(open(BASE / "features/features.csv", encoding="utf-8")))
    for r in rows:
        r["state"] = r.get("nq_hold_state") or "?"
        r["stop_dist"] = f(r, "nq_stop_dist_from_confirm_pts")
        r["stop_hit"] = f(r, "nq_stop_at_level_hit")
        r["stop_1_hit"] = f(r, "nq_stop_1pt_beyond_hit")

    lines = ["# Dominant-level hold on pullback (confirmation + stops)", ""]
    lines.append(f"## State counts: {dict(Counter(r['state'] for r in rows))}")
    late = [r for r in rows if r.get("tod") == "late"]
    lines.append(f"## Late state counts: {dict(Counter(r['state'] for r in late))}")
    lines.append("")

    lines.append("## Descriptive outcomes from confirm by state (HOLD/BREACH use post-confirm info — look-ahead)")
    for stt in ("HOLD", "BREACH", "NO_RETEST", "NEVER_CLEARED"):
        sel = [r for r in rows if r["state"] == stt]
        lines.append(f"- {stt}: {ret_stats(sel)}")
        lines.append(f"  - late: {ret_stats([r for r in sel if r.get('tod') == 'late'])}")
    lines.append("")

    # Stop placement at confirm
    lines.append("## Stop placement from confirm entry (causal)")
    cleared = [r for r in rows if r["state"] in ("HOLD", "BREACH", "NO_RETEST")]
    dists = [r["stop_dist"] for r in cleared if r["stop_dist"] is not None]
    lines.append(f"- among cleared: median stop_dist to level = {st.median(dists):.1f} pts "
                 f"(confirm is this far past the sliced level)")
    for label, key in (("SL at level", "stop_hit"), ("SL 1pt beyond level", "stop_1_hit")):
        hit = [r for r in cleared if r.get(key) == 1]
        miss = [r for r in cleared if r.get(key) == 0]
        lines.append(f"- {label}: hit {len(hit)}/{len(cleared)} ({len(hit)/len(cleared):.0%})")
        lines.append(f"  - when hit: {ret_stats(hit)}  (often already the reverse)")
        lines.append(f"  - when not hit: {ret_stats(miss)}")
        # Approximate bracket: if stop not hit, keep ret10s; if hit, lose ~stop_dist
        # (crude — stop can hit before 10s)
        sim = []
        for r in cleared:
            if r.get(key) == 1:
                # stopped: lose distance from confirm to stop (level or level-1)
                d = r["stop_dist"] or 0
                loss = -(d + (1.0 if key == "stop_1_hit" else 0.0))
                sim.append(loss)
            else:
                sim.append(f(r, "ret10s") or 0)
        lines.append(f"  - crude confirm+{label} @10s proxy mean {st.mean(sim):+.1f} "
                     f"med {st.median(sim):+.1f}  (hit→-stop_dist; miss→ret10s)")
    lines.append("")

    # Causal HOLD confirmation entry: re-label from hold_ts
    lines.append("## Causal confirmation entry at HOLD stamp (outcomes from hold_ts)")
    hold_rows = [r for r in rows if r["state"] == "HOLD" and r.get("nq_hold_ts_ns")]
    if hold_rows:
        cfg = load_config(); pq_dir = Path(cfg["parquet_dir"])
        labeled = []
        by_sid: dict[str, list] = {}
        for r in hold_rows:
            by_sid.setdefault(r["session"], []).append(r)
        for sid, evs in by_sid.items():
            pair = load_pair(pq_dir, sid)
            if pair is None:
                continue
            tbl = pa.table({
                "event_ts_ns": np.array([int(float(r["nq_hold_ts_ns"])) for r in evs], dtype=np.int64),
                "side": np.array([int(float(r["side_int"])) for r in evs], dtype=np.int8),
            })
            lab = label_in_points(tbl, pair, HORIZONS, TARGETS)
            for i, r in enumerate(evs):
                if i >= lab.num_rows:
                    break
                labeled.append(dict(
                    tod=r.get("tod"),
                    ret10s=lab.column("ret_pts_10000ms")[i].as_py(),
                    ret60s=lab.column("ret_pts_60000ms")[i].as_py(),
                    mae=lab.column("mae_pts")[i].as_py(),
                    mfe=lab.column("mfe_pts")[i].as_py(),
                    time_to_hold_ms=f(r, "nq_time_clear_to_event_ms"),
                    bounce=f(r, "nq_hold_bounce_pts"),
                    stop_dist=r["stop_dist"],
                ))
        lines.append(f"- HOLD retest entries: {ret_stats(labeled)}")
        lines.append(f"  - late: {ret_stats([r for r in labeled if r.get('tod') == 'late'])}")
        if labeled:
            tth = [r["time_to_hold_ms"] for r in labeled if r.get("time_to_hold_ms") is not None]
            lines.append(f"  - median time clear→hold: {st.median(tth):.0f} ms")
            # Compare to same events from confirm (look-ahead free comparison of entry timing)
            conf_same = [r for r in hold_rows]
            lines.append(f"  - same events from confirm (earlier entry): {ret_stats(conf_same)}")
    else:
        lines.append("- no HOLD events")
    lines.append("")

    # Tags
    key = {r["event_id"]: r for r in csv.DictReader(open(BASE / "review/review_key.csv", encoding="utf-8"))}
    pack = list(csv.DictReader(open(BASE / "review/review_pack.csv", encoding="utf-8")))
    by = {(r["session"], r["side"], r["ignition_bst"]): r for r in rows}
    for p in pack:
        k = key.get(p.get("event_id", ""))
        if not k:
            continue
        fr = by.get((k["session"], k["side"], k["ignition_bst"]))
        if fr is not None:
            fr["lst_tag"] = (p.get("lst_tag") or "").strip().upper()
    tagged = [r for r in rows if r.get("lst_tag") in ("TRUE", "FALSE")]
    lines.append("## Manual tags vs hold_state")
    if tagged:
        for tag in ("TRUE", "FALSE"):
            sel = [r for r in tagged if r["lst_tag"] == tag]
            lines.append(f"- {tag}: {dict(Counter(r['state'] for r in sel))}  {ret_stats(sel)}")
    else:
        lines.append("(no tags)")

    # Anchor
    ex = [r for r in rows if r["session"] == "100926" and r["ignition_bst"].endswith("15:24:38")]
    if ex:
        r = ex[0]
        lines += ["", "## Anchor 100926 15:24:38",
                  f"state={r['state']} stop_dist={r['stop_dist']} "
                  f"stop_at_level_hit={r['stop_hit']} stop_1pt_hit={r['stop_1_hit']} "
                  f"time_to_event_ms={r.get('nq_time_clear_to_event_ms')} "
                  f"bounce={r.get('nq_hold_bounce_pts')}"]

    text = "\n".join(lines) + "\n"
    (BASE / "features/level_hold.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
