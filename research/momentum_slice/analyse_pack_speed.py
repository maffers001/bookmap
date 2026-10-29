"""Check whether the blind review pack is skewed toward slow tape, and
re-test absolute tape speed during/around the move (user replay feedback).
"""
from __future__ import annotations

import csv
import statistics as st
from collections import Counter
from pathlib import Path

BASE = Path("results/MOMENTUM_SLICE")


def fnum(r, k):
    v = r.get(k)
    if v in ("", "nan", None):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def summary(rows, k):
    xs = [fnum(r, k) for r in rows]
    xs = [x for x in xs if x is not None]
    if not xs:
        return "n=0"
    return f"n={len(xs)} med={st.median(xs):.2f} mean={st.mean(xs):.2f}"


def ret_stats(rows):
    if not rows:
        return "n=0"
    r10 = [fnum(r, "ret10s") for r in rows]
    r10 = [x for x in r10 if x is not None]
    return (f"n={len(r10)} ret10s mean {st.mean(r10):+.1f} med {st.median(r10):+.1f} "
            f"P>5 {sum(x > 5 for x in r10)/len(r10):.2f} "
            f"P<-5 {sum(x < -5 for x in r10)/len(r10):.2f}")


def main():
    feat = list(csv.DictReader(open(BASE / "features/features.csv", encoding="utf-8")))
    by = {(r["session"], r["side"], r["ignition_bst"]): r for r in feat}
    pack = list(csv.DictReader(open(BASE / "review/review_pack.csv", encoding="utf-8")))
    # Join via review_key: Excel often mangles pack ignition_bst / leading-zero sessions.
    key = {r["event_id"]: r for r in csv.DictReader(open(BASE / "review/review_key.csv", encoding="utf-8"))}
    joined = []
    for p in pack:
        k = key.get(p.get("event_id", ""))
        if not k:
            continue
        f = by.get((k["session"], k["side"], k["ignition_bst"]))
        if f:
            f = dict(f)
            f["lst_tag"] = (p.get("lst_tag") or "").strip().upper()
            joined.append(f)

    lines = ["# Review-pack tape-speed bias check", "",
             "User feedback: pack looks slow-tape heavy; best LST when tape is "
             "fast while the market is moving.", ""]
    lines.append("## Pack vs all — speed feature distributions")
    for label, rows in (("ALL76", feat), ("PACK60", joined)):
        lines.append(f"### {label}")
        for k in ("nq_tps_30s", "es_tps_30s", "min_rel_speed", "nq_base_tps",
                  "min_base_tps", "nq_burst_n_trades", "max_order_share"):
            lines.append(f"- {k}: {summary(rows, k)}")
        lines.append("")

    tps = sorted(fnum(r, "nq_tps_30s") for r in feat)
    tps = [x for x in tps if x is not None]
    q1, q2 = tps[len(tps) // 3], tps[2 * len(tps) // 3]

    def bucket(r):
        v = fnum(r, "nq_tps_30s")
        if v is None:
            return "?"
        if v <= q1:
            return "slow"
        if v <= q2:
            return "mid"
        return "fast"

    lines.append(f"## Absolute NQ tps_30s terciles (cuts {q1:.1f} / {q2:.1f})")
    lines.append(f"- ALL: {dict(Counter(bucket(r) for r in feat))}")
    lines.append(f"- PACK: {dict(Counter(bucket(r) for r in joined))}")
    lines.append("")

    # Burst intensity: trades in the 2s confirm window / 2 = trades per sec during move
    for r in feat + joined:
        n = fnum(r, "nq_burst_n_trades")
        r["burst_tps"] = (n / 2.0) if n is not None else None
    bt = sorted(r["burst_tps"] for r in feat if r["burst_tps"] is not None)
    b1, b2 = bt[len(bt) // 3], bt[2 * len(bt) // 3]

    def burst_bucket(r):
        v = r.get("burst_tps")
        if v is None:
            return "?"
        if v <= b1:
            return "slow_burst"
        if v <= b2:
            return "mid_burst"
        return "fast_burst"

    lines.append(f"## Burst tape speed (nq_burst_n_trades/2s) terciles (cuts {b1:.1f} / {b2:.1f})")
    lines.append(f"- ALL: {dict(Counter(burst_bucket(r) for r in feat))}")
    lines.append(f"- PACK: {dict(Counter(burst_bucket(r) for r in joined))}")
    lines.append("")

    for scope, rows in (("ALL", feat), ("LATE", [r for r in feat if r.get("tod") == "late"])):
        lines.append(f"## Outcomes — {scope} by pre-ignition nq_tps_30s")
        for name in ("slow", "mid", "fast"):
            sel = [r for r in rows if bucket(r) == name]
            lines.append(f"- {name}: {ret_stats(sel)}")
        lines.append(f"## Outcomes — {scope} by burst_tps (during the move)")
        for name in ("slow_burst", "mid_burst", "fast_burst"):
            sel = [r for r in rows if burst_bucket(r) == name]
            lines.append(f"- {name}: {ret_stats(sel)}")
        lines.append("")

    # Interaction: fast burst + left behind
    for r in feat:
        r["left"] = fnum(r, "nq_left_behind_confirm")
    lines.append("## Interaction: fast burst + left-behind (confirm)")
    for scope, rows in (("ALL", feat), ("LATE", [r for r in feat if r.get("tod") == "late"])):
        fast_lb = [r for r in rows if burst_bucket(r) == "fast_burst" and r.get("left") == 1]
        slow_no = [r for r in rows if burst_bucket(r) == "slow_burst" and r.get("left") != 1]
        lines.append(f"- {scope} fast_burst+left_behind: {ret_stats(fast_lb)}")
        lines.append(f"- {scope} slow_burst+NOT left_behind: {ret_stats(slow_no)}")
    lines.append("")
    tagged = [r for r in joined if r.get("lst_tag") in ("TRUE", "FALSE")]
    lines += ["## Manual tags so far vs tape (from review_pack via event_id)", ""]
    if not tagged:
        lines.append("(no TRUE/FALSE tags filled yet)")
    else:
        from collections import Counter as C
        lines.append(f"tagged: {dict(C(r['lst_tag'] for r in tagged))} "
                     f"(blank remaining: {sum(1 for r in joined if not r.get('lst_tag'))})")
        for tag in ("TRUE", "FALSE"):
            sel = [r for r in tagged if r["lst_tag"] == tag]
            lines.append(f"### {tag} n={len(sel)}")
            lines.append(f"- nq_tps_30s: {summary(sel, 'nq_tps_30s')}")
            bxs = [r["burst_tps"] for r in sel if r.get("burst_tps") is not None]
            lines.append(f"- burst_tps: med={st.median(bxs):.1f}" if bxs else "- burst_tps: n/a")
            lines.append(f"- burst buckets: {dict(C(burst_bucket(r) for r in sel))}")
            lines.append(f"- left_behind_confirm=1: "
                         f"{sum(1 for r in sel if fnum(r,'nq_left_behind_confirm')==1)}/"
                         f"{len(sel)}")
            lines.append(f"- max_order_share: {summary(sel, 'max_order_share')}")
            lines.append(f"- outcomes (hidden during tagging): {ret_stats(sel)}")
        lines.append("")
        lines.append("### Tag rate by burst_tps tercile (among tagged)")
        for name in ("slow_burst", "mid_burst", "fast_burst"):
            sel = [r for r in tagged if burst_bucket(r) == name]
            if not sel:
                continue
            n_t = sum(1 for r in sel if r["lst_tag"] == "TRUE")
            lines.append(f"- {name}: TRUE {n_t}/{len(sel)} ({n_t/len(sel):.0%})  {ret_stats(sel)}")
    lines.append("")
    lines.append(
        "Note: pre-ignition tps_30s includes quiet base; burst_tps is participation "
        "during the 2 s ignition→confirm window — closer to 'tape is fast when the "
        "market is moving'. Pack join uses review_key (Excel often mangles "
        "ignition_bst / leading-zero session ids in review_pack.csv)."
    )
    text = "\n".join(lines) + "\n"
    (BASE / "features/pack_speed_bias.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
