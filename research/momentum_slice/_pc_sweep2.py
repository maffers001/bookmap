"""PC round-2: pullback size/bands, entry delay, slow on/off, second-push-fail.

Screen on train (fade_ret{D}_60 proxy), validate finalists later via mat_sim
in _pc_validate2.py. Also prints tagged explicit-good P&C path stats.
"""
from __future__ import annotations

import csv
import statistics as st
from pathlib import Path

from research.momentum_slice.rules import f

OUT = Path("results/MOMENTUM_SLICE/pc_review")


def load(label):
    return list(csv.DictReader(open(OUT / f"path_features_{label}.csv", encoding="utf-8")))


def num(r, k):
    v = r.get(k)
    if v in ("", "nan", None):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def stats(rows, key):
    xs = [num(r, key) for r in rows]
    xs = [x for x in xs if x is not None]
    if not xs:
        return "n=0"
    return (f"n={len(xs)} mean {st.mean(xs):+.1f} med {st.median(xs):+.1f} "
            f"P>5 {sum(x > 5 for x in xs)/len(xs):.2f} P<-5 {sum(x < -5 for x in xs)/len(xs):.2f}")


def tagged_path_summary():
    tagged = list(csv.DictReader(open(OUT / "tagged_pc_joined.csv", encoding="utf-8")))
    paths = load("train")
    by_key = {(p["session"], p["side"], p["ignition_ns"][:13]): p for p in paths}  # hour prefix fallback
    # better: join on session+side and nearest ignition minute via tagged ignition string is DD/MM - use event session/side + ret60 match is fragile.
    # Instead join tagged rows (features-joined) to path by session/side and peak proximity through ignition_bst from features file.
    feats = {}
    for r in csv.DictReader(open("results/MOMENTUM_SLICE/features/features.csv", encoding="utf-8")):
        feats[(r["session"], r["side"], r["ignition_bst"][:16])] = r
    pmap = {(p["session"], p["side"], p["ignition_bst"][:16]): p for p in paths}
    good, other = [], []
    for t in tagged:
        if not t.get("matched") or t["matched"] != "True":
            continue
        # find feature row by event: tagged has no ISO ignition; use session/side and pullback match
        cands = [p for (s, sd, _), p in pmap.items() if s == t["session"] and sd == t["side"]]
        # match by pb5 closeness
        pb = num(t, "nq_pullback_c5s_pts")
        best = None
        for p in cands:
            v = num(p, "pb5")
            if v is not None and pb is not None and (best is None or abs(v - pb) < abs(num(best, "pb5") - pb)):
                best = p
        if best is None:
            continue
        (good if t["cls"] == "PC_GOOD_EXPLICIT" else other).append(best)
    lines = ["## Tagged path features", ""]
    for name, gr in (("PC_GOOD_EXPLICIT", good), ("others matched", other)):
        lines.append(f"### {name} n={len(gr)}")
        for k in ("pb5", "pb10", "pb20", "pb30", "pb60", "push_reclaim_frac",
                  "push_beyond_peak_pts", "push_vol_ratio", "nq_post5_slow_ratio"):
            xs = [num(r, k) for r in gr]
            xs = [x for x in xs if x is not None]
            if xs:
                lines.append(f"- {k}: med {st.median(xs):.2f} mean {st.mean(xs):.2f} min {min(xs):.2f} max {max(xs):.2f}")
        fails = [num(r, "push_fails") for r in gr]
        fails = [x for x in fails if x is not None]
        if fails:
            lines.append(f"- push_fails rate: {sum(fails)/len(fails):.2f} ({int(sum(fails))}/{len(fails)})")
        lines.append("")
    return "\n".join(lines), good


RULES = []
for delay in (5, 10, 20, 30):
    for pbmin in (4, 8, 12, 15, 20, 25):
        RULES.append((f"PC2_d{delay}_pb>={pbmin}", delay, lambda r, d=delay, t=pbmin: (num(r, f"pb{d}") or 0) >= t and (num(r, f"beyond_peak{d}") or 9) <= 1.0))
    # bands
    for lo, hi in ((8, 20), (15, 40), (15, 25), (20, 40), (25, 40)):
        RULES.append((f"PC2_d{delay}_pb{lo}-{hi}", delay, lambda r, d=delay, lo=lo, hi=hi: lo <= (num(r, f"pb{d}") or -1) <= hi and (num(r, f"beyond_peak{d}") or 9) <= 1.0))
# slow on/off + second push for delay 20/30
for delay in (20, 30):
    for pbmin in (8, 12, 15, 20):
        RULES.append((f"PC2_d{delay}_pb>={pbmin}_slow<=0.85", delay,
                      lambda r, d=delay, t=pbmin: (num(r, f"pb{d}") or 0) >= t and (num(r, f"beyond_peak{d}") or 9) <= 1.0 and (num(r, "nq_post5_slow_ratio") or 9) <= 0.85))
        RULES.append((f"PC2_d{delay}_pb>={pbmin}_pushfail", delay,
                      lambda r, d=delay, t=pbmin: (num(r, f"pb{d}") or 0) >= t and (num(r, f"beyond_peak{d}") or 9) <= 1.0 and num(r, "push_fails") == 1))
        RULES.append((f"PC2_d{delay}_pb>={pbmin}_pushfail_lowvol<=0.5", delay,
                      lambda r, d=delay, t=pbmin: (num(r, f"pb{d}") or 0) >= t and (num(r, f"beyond_peak{d}") or 9) <= 1.0 and num(r, "push_fails") == 1 and (num(r, "push_vol_ratio") or 9) <= 0.5))
        RULES.append((f"PC2_d{delay}_pb>={pbmin}_pushfail_reclaim<=0.5", delay,
                      lambda r, d=delay, t=pbmin: (num(r, f"pb{d}") or 0) >= t and (num(r, f"beyond_peak{d}") or 9) <= 1.0 and num(r, "push_fails") == 1 and (num(r, "push_reclaim_frac") or 9) <= 0.5))
        RULES.append((f"PC2_d{delay}_pb>={pbmin}_nofailreq_slowAny", delay,
                      lambda r, d=delay, t=pbmin: (num(r, f"pb{d}") or 0) >= t and (num(r, f"beyond_peak{d}") or 9) <= 1.0))


def main():
    train, hold = load("train"), load("holdout")
    tagged_text, _ = tagged_path_summary()
    rows = []
    for name, delay, fn in RULES:
        tr = [r for r in train if fn(r)]
        ho = [r for r in hold if fn(r)]
        key = f"fade_ret{delay}_60"
        def mean_of(rs):
            xs = [num(r, key) for r in rs]
            xs = [x for x in xs if x is not None]
            return (len(xs), round(st.mean(xs), 2) if xs else None,
                    round(st.median(xs), 2) if xs else None)
        tn, tm, tmed = mean_of(tr)
        hn, hm, hmed = mean_of(ho)
        rows.append(dict(rule=name, delay=delay, train_n=tn, train_mean=tm, train_med=tmed,
                         hold_n=hn, hold_mean=hm, hold_med=hmed))
    rows.sort(key=lambda r: (r["train_mean"] is not None and r["train_n"] >= 5, r["train_mean"] or -1e9), reverse=True)
    lines = ["# PC round-2 sweep (train-ranked proxy fade_ret60 from entry)", "", tagged_text, "",
             "| rule | train n mean/med | hold n mean/med |", "|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['rule']} | {r['train_n']} {r['train_mean']}/{r['train_med']} | {r['hold_n']} {r['hold_mean']}/{r['hold_med']} |")
    (OUT / "sweep2.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with open(OUT / "sweep2.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print("\n".join(lines[:120]))


if __name__ == "__main__":
    main()
