"""Screen entry-criteria variants on TRAIN only (proxy forward returns).

Selection uses train features.csv only. Holdout is joined afterwards for the
caller / report, not for ranking here. Proxy = directional ret10s/ret60s from
confirm (momentum) or fade_ret-like -ret (PC, approx) — finalists must be
re-run through mat_sim (see _validate_entry.py).
"""
from __future__ import annotations

import csv
import statistics as st
from pathlib import Path

from research.momentum_slice.rules import enrich, f, is_momentum_v1, is_pc_v1

OUT = Path("results/MOMENTUM_SLICE/entry_sweep")
OUT.mkdir(parents=True, exist_ok=True)


def load(path):
    return [enrich(r) for r in csv.DictReader(open(path, encoding="utf-8"))]


def g(r, k, default=None):
    v = f(r, k)
    return default if v is None else v


def stats(rows, retkey="ret60s"):
    xs = [f(r, retkey) for r in rows]
    xs = [x for x in xs if x is not None]
    if not xs:
        return dict(n=len(rows), nret=0, mean=None, med=None, p_gt5=None, p_lt_5=None)
    return dict(
        n=len(rows), nret=len(xs),
        mean=round(st.mean(xs), 2), med=round(st.median(xs), 2),
        p_gt5=round(sum(x > 5 for x in xs) / len(xs), 2),
        p_lt_5=round(sum(x < -5 for x in xs) / len(xs), 2),
    )


# ---------- rule definitions: (family, name, fn) ----------
def mom_rules():
    def base(r):
        return is_momentum_v1(r)

    rules = [
        ("mom", "MOM_V1_baseline", base),
        ("mom", "MOM_all_candidates", lambda r: True),
        ("mom", "MOM_no_trend_gate", lambda r: f(r, "nq_left_behind_confirm") == 1 and g(r, "nq_dom_vs_med", 0) >= 4),
        ("mom", "MOM_no_dom_gate", lambda r: f(r, "nq_left_behind_confirm") == 1 and g(r, "trend_5m_pts", -1e9) >= 0),
        ("mom", "MOM_no_left_gate", lambda r: g(r, "trend_5m_pts", -1e9) >= 0 and g(r, "nq_dom_vs_med", 0) >= 4),
        ("mom", "MOM_trend>=10", lambda r: base(r) and g(r, "trend_5m_pts", -1e9) >= 10),
        ("mom", "MOM_trend>=20", lambda r: base(r) and g(r, "trend_5m_pts", -1e9) >= 20),
        ("mom", "MOM_dom>=8", lambda r: base(r) and g(r, "nq_dom_vs_med", 0) >= 8),
        ("mom", "MOM_dom>=12", lambda r: base(r) and g(r, "nq_dom_vs_med", 0) >= 12),
        ("mom", "MOM_LONG_only", lambda r: base(r) and f(r, "side_int") == 1),
        ("mom", "MOM_SHORT_only", lambda r: base(r) and f(r, "side_int") == -1),
        ("mom", "MOM_late_only", lambda r: base(r) and r.get("tod") == "late"),
        ("mom", "MOM_open_only", lambda r: base(r) and r.get("tod") == "open"),
        ("mom", "MOM_LONG_late", lambda r: base(r) and f(r, "side_int") == 1 and r.get("tod") == "late"),
        ("mom", "MOM_left_c2s", lambda r: f(r, "nq_left_behind_c2s") == 1 and g(r, "trend_5m_pts", -1e9) >= 0 and g(r, "nq_dom_vs_med", 0) >= 4),
        ("mom", "MOM_left_c5s", lambda r: f(r, "nq_left_behind_c5s") == 1 and g(r, "trend_5m_pts", -1e9) >= 0 and g(r, "nq_dom_vs_med", 0) >= 4),
        ("mom", "MOM_both_left_confirm", lambda r: base(r) and f(r, "both_left_behind_confirm") == 1),
        ("mom", "MOM_es_left_confirm", lambda r: base(r) and f(r, "es_left_behind_confirm") == 1),
        ("mom", "MOM_cleared_confirm", lambda r: base(r) and f(r, "nq_cleared_confirm") == 1),
        ("mom", "MOM_low_pullback<=0.3", lambda r: base(r) and g(r, "nq_pullback_ratio_confirm", 9) <= 0.3),
        ("mom", "MOM_both_with_trend", lambda r: base(r) and f(r, "both_with_trend") == 1),
        ("mom", "MOM_consume>=0.5", lambda r: base(r) and g(r, "nq_consume", 0) >= 0.5),
        ("mom", "MOM_consume>=1.0", lambda r: base(r) and g(r, "nq_consume", 0) >= 1.0),
        ("mom", "MOM_fast_rel_speed>=1", lambda r: base(r) and g(r, "min_rel_speed", 0) >= 1.0),
        ("mom", "MOM_no_single_order<=0.3", lambda r: base(r) and g(r, "max_order_share", 9) <= 0.3),
        ("mom", "MOM_dom_share>=0.15", lambda r: base(r) and g(r, "nq_dom_share", 0) >= 0.15),
        ("mom", "MOM_LONG_trend>=10_dom>=8", lambda r: base(r) and f(r, "side_int") == 1 and g(r, "trend_5m_pts", -1e9) >= 10 and g(r, "nq_dom_vs_med", 0) >= 8),
        # Counter-hypothesis: maybe continuation does NOT need trend/dom at all
        ("mom", "MOM_left_only_any", lambda r: f(r, "nq_left_behind_confirm") == 1),
        ("mom", "MOM_left_LONG_only", lambda r: f(r, "nq_left_behind_confirm") == 1 and f(r, "side_int") == 1),
    ]
    return rules


def pc_base(r):
    return is_pc_v1(r)


def pc_rules():
    rules = [
        ("pc", "PC_V1_baseline", pc_base),
        ("pc", "PC_no_slow_gate", lambda r: f(r, "nq_left_behind_c5s") == 0 and g(r, "nq_pullback_c5s_pts", 0) >= 4 and g(r, "nq_pullback_ratio_c5s", 0) >= 0.70 and g(r, "nq_post5_net_pts", 9) <= 1.0),
        ("pc", "PC_no_net_gate", lambda r: f(r, "nq_left_behind_c5s") == 0 and g(r, "nq_pullback_c5s_pts", 0) >= 4 and g(r, "nq_pullback_ratio_c5s", 0) >= 0.70 and g(r, "nq_post5_slow_ratio", 9) <= 0.85),
        ("pc", "PC_pullback>=6", lambda r: pc_base(r) and g(r, "nq_pullback_c5s_pts", 0) >= 6),
        ("pc", "PC_pullback>=8", lambda r: pc_base(r) and g(r, "nq_pullback_c5s_pts", 0) >= 8),
        ("pc", "PC_ratio>=0.9", lambda r: pc_base(r) and g(r, "nq_pullback_ratio_c5s", 0) >= 0.9),
        ("pc", "PC_slow<=0.7", lambda r: pc_base(r) and g(r, "nq_post5_slow_ratio", 9) <= 0.7),
        ("pc", "PC_net<=0", lambda r: pc_base(r) and g(r, "nq_post5_net_pts", 9) <= 0),
        ("pc", "PC_net<=-2", lambda r: pc_base(r) and g(r, "nq_post5_net_pts", 9) <= -2),
        # fade direction: original LONG slice -> fade is SHORT trade, etc.
        ("pc", "PC_fade_after_LONGslice", lambda r: pc_base(r) and f(r, "side_int") == 1),
        ("pc", "PC_fade_after_SHORTslice", lambda r: pc_base(r) and f(r, "side_int") == -1),
        ("pc", "PC_late_only", lambda r: pc_base(r) and r.get("tod") == "late"),
        ("pc", "PC_open_only", lambda r: pc_base(r) and r.get("tod") == "open"),
        ("pc", "PC_countertrend_slice_trend<=0", lambda r: pc_base(r) and g(r, "trend_5m_pts", 1e9) <= 0),
        ("pc", "PC_withtrend_slice_trend>=0", lambda r: pc_base(r) and g(r, "trend_5m_pts", -1e9) >= 0),
        ("pc", "PC_low_dom<4", lambda r: pc_base(r) and g(r, "nq_dom_vs_med", 1e9) < 4),
        ("pc", "PC_high_dom>=4", lambda r: pc_base(r) and g(r, "nq_dom_vs_med", 0) >= 4),
        ("pc", "PC_strong_stall_pull8_ratio9_slow7", lambda r: pc_base(r) and g(r, "nq_pullback_c5s_pts", 0) >= 8 and g(r, "nq_pullback_ratio_c5s", 0) >= 0.9 and g(r, "nq_post5_slow_ratio", 9) <= 0.7),
        # Looser stall: maybe V1 stall gates are too strict / wrong stamp
        ("pc", "PC_loose_pull>=2_ratio>=0.5", lambda r: f(r, "nq_left_behind_c5s") == 0 and g(r, "nq_pullback_c5s_pts", 0) >= 2 and g(r, "nq_pullback_ratio_c5s", 0) >= 0.5),
        ("pc", "PC_any_not_left_c5s", lambda r: f(r, "nq_left_behind_c5s") == 0),
        ("pc", "PC_not_left_LONGslice_only", lambda r: f(r, "nq_left_behind_c5s") == 0 and f(r, "side_int") == 1),
        ("pc", "PC_not_left_SHORTslice_only", lambda r: f(r, "nq_left_behind_c5s") == 0 and f(r, "side_int") == -1),
    ]
    return rules


def main():
    train = load("results/MOMENTUM_SLICE/features/features.csv")
    hold = load("results/MOMENTUM_SLICE/holdout/features.csv")
    out_rows = []
    for fam, name, fn in mom_rules() + pc_rules():
        tr = [r for r in train if fn(r)]
        ho = [r for r in hold if fn(r)]
        if fam == "mom":
            s_tr, s_ho = stats(tr, "ret60s"), stats(ho, "ret60s")
            s_tr10, s_ho10 = stats(tr, "ret10s"), stats(ho, "ret10s")
        else:
            # fade proxy: holdout has fade_ret60s; train approx via -ret60s
            for r in tr:
                v = f(r, "ret60s")
                r["_fade60"] = -v if v is not None else None
            s_tr, s_ho = stats(tr, "_fade60"), stats(ho, "fade_ret60s")
            s_tr10, s_ho10 = stats(tr, "ret10s"), stats(ho, "fade_ret10s")
            # train 10s fade approx sign-flip for display only in mean
            if s_tr10["mean"] is not None:
                s_tr10 = dict(s_tr10, mean=-s_tr10["mean"], med=-s_tr10["med"] if s_tr10["med"] is not None else None)
        out_rows.append(dict(family=fam, rule=name,
                             train_n=s_tr["n"], train_mean60=s_tr["mean"], train_med60=s_tr["med"],
                             train_p_gt5=s_tr["p_gt5"], train_p_lt_5=s_tr["p_lt_5"],
                             train_mean10=s_tr10["mean"],
                             hold_n=s_ho["n"], hold_mean60=s_ho["mean"], hold_med60=s_ho["med"],
                             hold_p_gt5=s_ho["p_gt5"], hold_p_lt_5=s_ho["p_lt_5"],
                             hold_mean10=s_ho10["mean"]))
    # rank within family by train mean60, requiring train n>=4 where possible
    lines = ["# Entry-criteria sweep (train-ranked, proxy ret60)", "",
             "Proxy only — finalists need mat_sim. Holdout shown for validation, **not** used to rank.", ""]
    for fam in ("mom", "pc"):
        rows = [r for r in out_rows if r["family"] == fam]
        rows.sort(key=lambda r: (r["train_mean60"] is not None, r["train_mean60"] or -1e9), reverse=True)
        lines.append(f"## {fam}")
        lines.append("| rule | train n | train ret60 mean/med | train P>5/<-5 | hold n | hold ret60 mean/med | hold P>5/<-5 |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in rows:
            lines.append(f"| {r['rule']} | {r['train_n']} | {r['train_mean60']}/{r['train_med60']} | {r['train_p_gt5']}/{r['train_p_lt_5']} | {r['hold_n']} | {r['hold_mean60']}/{r['hold_med60']} | {r['hold_p_gt5']}/{r['hold_p_lt_5']} |")
        lines.append("")
    (OUT / "screen.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with open(OUT / "screen.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        w.writeheader(); w.writerows(out_rows)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
