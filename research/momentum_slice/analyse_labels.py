"""Discriminator analysis: manual tags (when present) + pre-registered rules vs controls.

  .venv\\Scripts\\python.exe -m research.momentum_slice.analyse_labels

Reads review/review_pack.csv + review_key.csv, features/features.csv,
features/controls.csv. If lst_tag is still blank everywhere, reports
'AWAITING TAGS' plus the outcome-only rule/control comparison (exploratory).
Rules are pre-registered here — do not add rules after seeing tag results
without noting them as post-hoc.
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

BASE = Path("results/MOMENTUM_SLICE")
NUM = ("nq_consume", "es_consume", "nq_density", "es_density", "nq_persist", "es_persist",
       "nq_dist_max_ticks", "es_dist_max_ticks", "nq_pull_est", "es_pull_est",
       "range_5m_pts", "range_1m_pts", "compression_1m_5m", "prior_run_5m_pts",
       "med_min_range_pts", "dist_vwap_pts", "ret10s", "ret60s", "mfe", "mae")


def load(p: Path):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(p, newline="", encoding=enc) as f:
                rows = list(csv.DictReader(f))
            break
        except UnicodeDecodeError:
            continue
    for r in rows:
        for k in NUM:
            if k in r and r[k] not in ("", None):
                try:
                    r[k] = float(r[k])
                except ValueError:
                    pass
    return rows


def stats(rows, key="ret10s"):
    if not rows:
        return "n=0"
    x = np.array([r[key] for r in rows], dtype=float)
    return (f"n={len(rows)} {key} mean {x.mean():+.1f} med {np.median(x):+.1f} "
            f"P(>5) {(x > 5).mean():.2f} P(<-5) {(x < -5).mean():.2f}")


RULES = {
    "R1_tierA": lambda r: (r.get("nq_consume", 0) >= 0.5 and r.get("es_consume", 0) >= 0.5
                           and r.get("nq_density", 0) >= 1.2 and r.get("es_density", 0) >= 1.0
                           and r.get("nq_persist", 0) >= 0.7 and r.get("es_persist", 0) >= 0.7),
    "R2_R1+near_max+not_extended": lambda r: (RULES["R1_tierA"](r)
                                              and r.get("nq_dist_max_ticks", 99) <= 20
                                              and r.get("prior_run_5m_pts", 99) <= 15),
    "R3_R1+late_only": lambda r: RULES["R1_tierA"](r) and r.get("tod") == "late",
}


def main():
    feats = load(BASE / "features/features.csv")
    ctrls = load(BASE / "features/controls.csv") if (BASE / "features/controls.csv").exists() else []
    pack = load(BASE / "review/review_pack.csv")
    key = {r["event_id"]: r for r in load(BASE / "review/review_key.csv")}
    tagged = {r["event_id"]: r for r in pack if str(r.get("lst_tag", "")).strip()}
    lines = ["# LST discriminator analysis", ""]
    if not tagged:
        lines += ["**STATUS: AWAITING TAGS** — lst_tag is blank in review_pack.csv. "
                  "Rule/control numbers below are exploratory and outcome-only; the "
                  "decision gate requires your TRUE/FALSE tags.", ""]
    else:
        by = {(r["session"], r["side"], r["ignition_bst"]): r for r in feats}
        t_rows = [by[(key[e]["session"], key[e]["side"], key[e]["ignition_bst"])]
                  for e, r in tagged.items()
                  if (key[e]["session"], key[e]["side"], key[e]["ignition_bst"]) in by]
        for tag in ("TRUE", "FALSE"):
            sel = [t for t, (_, r) in zip(t_rows, tagged.items()) if r["lst_tag"].strip().upper() == tag]
            lines += [f"## tag={tag}: {stats(sel)}", ""]
            for f_ in NUM[:16]:
                vals = [t[f_] for t in sel if isinstance(t.get(f_), float)]
                if vals:
                    lines.append(f"- {f_}: med {np.median(vals):.2f}")
            lines.append("")
    lines += ["## Pre-registered rules vs rest vs controls (ret10s, confirm-entry)", ""]
    ctrl_nq = [c for c in ctrls if c.get("control") == "nq_only"]
    ctrl_rnd = [c for c in ctrls if c.get("control") == "random"]
    for c in ctrl_nq + ctrl_rnd:
        c["ret10s"] = c.get("ret_pts_10000ms", float("nan"))
    lines.append(f"- control NQ-only bursts: {stats(ctrl_nq)}")
    lines.append(f"- control random: {stats(ctrl_rnd)}")
    lines.append(f"- ALL candidates: {stats(feats)}")
    for name, fn in RULES.items():
        sel = [r for r in feats if fn(r)]
        rest = [r for r in feats if not fn(r)]
        lines.append(f"- {name}: {stats(sel)} || rest: {stats(rest)}")
        late = [r for r in sel if r.get("tod") == "late"]
        lines.append(f"  - {name} & late-only: {stats(late)}")
    lines += ["", "Decision gate (per plan): a rule must align with TRUE/FALSE tags AND "
              "beat NQ-only controls in the late subset. No tag alignment -> no expansion."]
    (BASE / "features/discriminator.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
