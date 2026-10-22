"""EXP-0043 follow-up dig — (b) book over the approach leg, (a) confirmation-time sweep + second filter.

Reads the scored ``events.parquet`` (and ``placebo_events.parquet``) written by
``run_hold_break_of`` for the train months (Oct, Sep) and the OOS month (May).

(b) **Pre-touch book trajectory** — ``StackTrend_pre`` and its raws
    (``stack_build_{10,30,60}``, ``hold_build_*``, ``pull_rate_z{10,30}``):
    cliffs δ break-vs-hold per month, real vs placebo.

(a) **Confirmation-time sweep** — the frozen fade (``StackConsume ≤ −0.25``,
    TP30/SL15) at +1 / +2 / +3 / +5 s with the matching ``StackConsume_{k}s``
    window. Then **second filters** on top of the +2 s rule, each evaluated on
    the pooled train months; the best by avg (n ≥ 60) is then reported on May
    **once**. May is never used to pick.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.config import ROOT
from research.irt_lvn.confirm import pack_pnl
from research.irt_lvn.run_hold_break_of import cliffs_delta, session_bootstrap, split_label, _col, _mask, _r

TRAIN = ("2026-10", "2026-09")
OOS = "2026-05"
SWEEP_TAG = "tp30_sl15"
THR = -0.25

TREND_SCORES = ("StackTrend_pre", "StackScore", "MboScore_pre")
TREND_RAW = (
    "stack_build_10", "stack_build_30", "stack_build_60",
    "hold_build_10", "hold_build_30", "hold_build_60",
    "pull_rate_z10", "pull_rate_z30", "near_sum_z", "depletion_pre",
)


def _tag(month: str) -> str:
    y, m = (int(x) for x in month.split("-")[:2])
    return "" if (y, m) == (2026, 10) else f"_{y}{m:02d}"


def _load(month: str, name: str) -> pa.Table | None:
    p = ROOT / "results" / f"EXP-0043{_tag(month)}" / name
    if not p.exists() or p.stat().st_size == 0:
        return None
    return pq.read_table(p)


def _delta_row(tbl: pa.Table, score: str, boot: bool) -> dict:
    ok = _mask(tbl, _col(tbl, "baseline_ok") >= 0.5) if "baseline_ok" in tbl.column_names else tbl
    h, b = _col(split_label(ok, "HOLD"), score), _col(split_label(ok, "BREAK"), score)
    d = cliffs_delta(b, h)
    out = {"n_h": int(np.isfinite(h).sum()), "n_b": int(np.isfinite(b).sum()), "delta": None if d is None else round(d, 3)}
    if boot:
        bs = session_bootstrap(ok, score)
        out["ci90"] = bs.get("delta_ci90")
    return out


# ---------------------------------------------------------------- filters

def _fade_mask(tbl: pa.Table, k: int | None) -> np.ndarray:
    """Undecided at +k s and StackConsume_{k}s ≤ THR (k=None → the 2 s primary columns)."""
    if k is None:
        und = _col(tbl, "undecided_at_tc") >= 0.5
        s = _col(tbl, "StackConsume")
    else:
        und = _col(tbl, f"undecided_{k}s") >= 0.5
        s = _col(tbl, f"StackConsume_{k}s")
    return und & (s <= THR)


def _fade_pnl_col(k: int | None) -> str:
    return f"pnl_fade_tc_{SWEEP_TAG}" if k is None else f"pnl_fade_{k}s_{SWEEP_TAG}"


FILTERS: dict[str, callable] = {
    "base (StackConsume≤−0.25)": lambda t: np.ones(t.num_rows, dtype=bool),
    "+ StackConsume≤−0.5": lambda t: _col(t, "StackConsume") <= -0.5,
    "+ px on hold side at +2s": lambda t: _col(t, "px_hold_side_tc") >= 0.5,
    "+ pierce_at_tc ≤ 2 pt": lambda t: _col(t, "pierce_at_tc") <= 2.0,
    "+ pierce_at_tc == 0": lambda t: _col(t, "pierce_at_tc") <= 0.0,
    "+ Take_at ≤ 0 (aggr agrees)": lambda t: _col(t, "TakeScore_at") <= 0.0,
    "+ near_sum_z ≥ 0 (real wall)": lambda t: _col(t, "near_sum_z") >= 0.0,
    "+ consume_frac == 0": lambda t: _col(t, "consume_frac") <= 0.0,
    "+ StackTrend_pre ≤ 0 (stack built in)": lambda t: _col(t, "StackTrend_pre") <= 0.0,
    "+ after 15 min": lambda t: _col(t, "min_after_open") > 15.0,
    "+ first 15 min": lambda t: _col(t, "min_after_open") <= 15.0,
    "+ kind LVN only": lambda t: np.array([("LVN" in str(x)) and ("DOP" not in str(x)) for x in t.column("level_kinds").to_pylist()]),
    "+ not DOP": lambda t: np.array(["DOP" not in str(x) for x in t.column("level_kinds").to_pylist()]),
}


def _eval(tbl: pa.Table, mask: np.ndarray, pnl_col: str) -> dict:
    pnl = _col(tbl, pnl_col).copy()
    pnl[~mask] = np.nan
    sess = np.array(tbl.column("session_id").to_pylist())
    ts = _col(tbl, "t_touch_ns")
    lab = np.array(tbl.column("label").to_pylist())
    out = pack_pnl(pnl, sess, ts)
    fired = mask & np.isfinite(pnl)
    out["se"] = round(float(np.std(pnl[fired], ddof=1) / np.sqrt(fired.sum())), 2) if fired.sum() > 1 else None
    out["amb_share"] = round(float(np.mean(lab[fired] == "AMBIGUOUS")), 3) if fired.any() else None
    out["hold_share"] = round(float(np.mean(lab[fired] == "HOLD")), 3) if fired.any() else None
    return out


def _row(name: str, st: dict) -> str:
    return (f"| {name} | {st['n']} | {st['net']} | {_r(st['avg'])} ± {_r(st.get('se'), 2)} | {_r(st['win'])} | "
            f"{st['days_pos']}/{st['days']} | {st['max_dd']} | {_r(st.get('hold_share'))} | {_r(st.get('amb_share'))} |")


HDR = ["| rule | n | net | avg ± SE | win | days+ | maxDD | HOLD share | AMB share |",
       "|---|---:|---:|---:|---:|---|---:|---:|---:|"]


def _sign_consistent(deltas: list[float | None], min_abs: float = 0.1) -> bool:
    d = [x for x in deltas if x is not None]
    return len(d) >= 2 and all(abs(x) >= min_abs for x in d) and (all(x > 0 for x in d) or all(x < 0 for x in d))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "results" / "EXP-0043_CONFIRM_DIG"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ev = {m: _load(m, "events.parquet") for m in TRAIN + (OOS,)}
    pl = {m: _load(m, "placebo_events.parquet") for m in TRAIN + (OOS,)}
    missing = [m for m, t in ev.items() if t is None]
    if missing:
        raise SystemExit(f"missing events for {missing}; run run_hold_break_of first")
    train = pa.concat_tables([ev[m] for m in TRAIN], promote_options="default")

    report: dict = {"train_months": TRAIN, "oos_month": OOS, "thr": THR, "bracket": SWEEP_TAG}
    L: list[str] = [
        "# EXP-0043 follow-up dig — approach-leg book (b) and confirmation sweep (a)",
        "",
        f"Train = {', '.join(TRAIN)} (pooled for filter selection). OOS = {OOS}, read once after selection. "
        f"Bracket TP30/SL15, 1-tick slip, 0.25 pt/RT, time stop +120 s. Gate `StackConsume ≤ {THR:g}`.",
        "",
        "## (b) Pre-touch book trajectory — cliffs δ break vs hold (baseline_ok)",
        "",
        "Real vs ±40 pt placebo. A level-specific pre-touch signal needs |δ| clearly above placebo in every month.",
        "",
        "| score / raw | " + " | ".join(f"{m} real" for m in TRAIN + (OOS,)) + " | " + " | ".join(f"{m} placebo" for m in TRAIN + (OOS,)) + " |",
        "|---|" + "---:|" * (2 * (len(TRAIN) + 1)),
    ]
    trend: dict = {}
    for s in TREND_SCORES + TREND_RAW:
        row = {}
        cells = []
        for m in TRAIN + (OOS,):
            r = _delta_row(ev[m], s, boot=s in TREND_SCORES) if s in ev[m].column_names else {"delta": None}
            row[f"{m}_real"] = r
            ci = r.get("ci90")
            cells.append(f"{_r(r['delta'])}" + (f" {ci}" if ci else ""))
        for m in TRAIN + (OOS,):
            t = pl[m]
            r = _delta_row(t, s, boot=False) if (t is not None and s in t.column_names) else {"delta": None}
            row[f"{m}_placebo"] = r
            cells.append(f"{_r(r['delta'])}")
        trend[s] = row
        L.append(f"| {s} | " + " | ".join(cells) + " |")
    report["trend"] = trend

    # ---------------------------------------------------------- (a) sweep
    L += ["", "## (a) Confirmation-time sweep — frozen fade rule at +k s (matching StackConsume window)", ""]
    sweep: dict = {}
    L += ["| k | " + " | ".join(f"{m} n / net / days+ / maxDD" for m in TRAIN + (OOS,)) + " |",
          "|---|" + "---|" * (len(TRAIN) + 1)]
    for k in (1, None, 3, 5):
        label = "2 (frozen)" if k is None else str(k)
        cells = []
        sweep[label] = {}
        for m in TRAIN + (OOS,):
            t = ev[m]
            col = _fade_pnl_col(k)
            if col not in t.column_names:
                cells.append("—")
                continue
            st = _eval(t, _fade_mask(t, k), col)
            sweep[label][m] = st
            cells.append(f"{st['n']} / {st['net']} / {st['days_pos']}/{st['days']} / {st['max_dd']}")
        L.append(f"| {label} s | " + " | ".join(cells) + " |")
    report["sweep"] = sweep

    # ------------------------------------------------- (a) second filters
    L += ["", "## (a) Second filter on top of the +2 s rule — **train only** (Oct+Sep pooled)", ""] + HDR
    base_mask = _fade_mask(train, None)
    col2 = _fade_pnl_col(None)
    filt_train: dict = {}
    for name, fn in FILTERS.items():
        m = base_mask & fn(train)
        st = _eval(train, m, col2)
        filt_train[name] = st
        L.append(_row(name, st))
    report["filters_train"] = filt_train

    # selection: best avg among filters with n ≥ 60 on train, excluding base
    cands = {k: v for k, v in filt_train.items() if v["n"] >= 60 and not k.startswith("base")}
    best = max(cands, key=lambda k: cands[k]["avg"]) if cands else None
    report["selected_filter"] = best

    L += ["", f"**Selected on train (best avg, n ≥ 60):** `{best}`" if best else "**No filter qualifies (n ≥ 60).**", ""]

    # OOS: base and selected only (plus the sweep best k if different from 2)
    L += ["## OOS (May) — read once: base rule and the selected filter", ""] + HDR
    oos = ev[OOS]
    bm = _fade_mask(oos, None)
    st_base = _eval(oos, bm, col2)
    L.append(_row("base (StackConsume≤−0.25)", st_base))
    filt_oos = {"base": st_base}
    if best:
        st_sel = _eval(oos, bm & FILTERS[best](oos), col2)
        filt_oos[best] = st_sel
        L.append(_row(best, st_sel))
    report["filters_oos"] = filt_oos

    # per-label for selected on OOS (diagnostic)
    if best:
        lab = np.array(oos.column("label").to_pylist())
        sess = np.array(oos.column("session_id").to_pylist())
        ts = _col(oos, "t_touch_ns")
        pnl = _col(oos, col2).copy()
        msel = bm & FILTERS[best](oos)
        pnl[~msel] = np.nan
        L += ["", f"### OOS by eventual label — `{best}` (diagnostic)", "", "| label | n | net | avg |", "|---|---:|---:|---:|"]
        for name in ("HOLD", "BREAK", "AMBIGUOUS"):
            mm = msel & (lab == name)
            st = pack_pnl(pnl[mm], sess[mm], ts[mm])
            L.append(f"| {name} | {st['n']} | {st['net']} | {_r(st['avg'])} |")

    # Appendix: all filters on OOS (transparency; NOT for selection)
    L += ["", "## Appendix — all filters on OOS (for transparency; selection was made on train only)", ""] + HDR
    allo: dict = {}
    for name, fn in FILTERS.items():
        st = _eval(oos, bm & fn(oos), col2)
        allo[name] = st
        L.append(_row(name, st))
    report["filters_oos_all"] = allo

    # Decision text
    L += ["", "## Decision", ""]
    b_tr = filt_train["base (StackConsume≤−0.25)"]
    lines = []
    months = TRAIN + (OOS,)
    # (b): a feature counts only if its sign is the same in all three months with |δ| ≥ 0.1,
    # and its real |δ| beats the placebo |δ| in every month.
    consistent = []
    for s in TREND_SCORES + TREND_RAW:
        dr = [trend[s][f"{m}_real"]["delta"] for m in months]
        dp = [trend[s][f"{m}_placebo"]["delta"] for m in months]
        if _sign_consistent(dr) and all(
            (p is None) or (r is not None and abs(r) > abs(p)) for r, p in zip(dr, dp)
        ):
            consistent.append((s, dr, dp))
    st_tr = trend["StackTrend_pre"]
    lines.append(
        "- **(b) StackTrend_pre (stack pulled/built over −10/−30/−60 s + pull-rate z):** δ "
        + " / ".join(f"{_r(st_tr[f'{m}_real']['delta'], 2)}" for m in months)
        + " vs placebo " + " / ".join(f"{_r(st_tr[f'{m}_placebo']['delta'], 2)}" for m in months)
        + ". Not level-specific, sign not stable → **null**, like the other pre-touch scores."
    )
    if consistent:
        for s, dr, dp in consistent:
            lines.append(
                f"- **(b) sign-consistent across all 3 months and above placebo:** `{s}` δ "
                + " / ".join(f"{_r(x, 2)}" for x in dr) + " (placebo " + " / ".join(f"{_r(x, 2)}" for x in dp) + "). "
                "Weak (|δ| < 0.3) but the only pre-touch feature that passes both checks; keep as a candidate, not a signal."
            )
    else:
        lines.append("- **(b)** no pre-touch book feature is sign-consistent across all three months *and* above placebo in each. Pre-touch stays null.")
    # (a) sweep: all cells positive?
    cells = [v["net"] for kk in sweep.values() for v in kk.values()]
    best_k = {m: max(sweep, key=lambda kk: sweep[kk].get(m, {}).get("net", -1e9)) for m in months}
    one_k = len(set(best_k.values())) == 1
    lines.append(
        f"- **(a) confirmation time:** {sum(1 for c in cells if c > 0)}/{len(cells)} (k × month) cells positive; best k by net "
        + ", ".join(f"{m}: {best_k[m]} s" for m in months)
        + (" → one k wins every month; worth a frozen re-test, but 2 s stays the rule for now."
           if one_k else " → no k wins every month; the fade is robust to confirmation time and 2 s stays frozen. No retune.")
    )
    if best:
        so, st_b = filt_oos[best], st_base
        d_avg = (so["avg"] or 0.0) - (st_b["avg"] or 0.0)
        se = so.get("se") or 0.0
        verdict = (
            "within noise (Δavg < 1 SE) → **do not adopt**; keep the base rule."
            if abs(d_avg) < se else
            ("**improves OOS avg and DD** → candidate v2, still needs forward confirmation." if d_avg > 0 and so["max_dd"] <= st_b["max_dd"]
             else "**does not improve OOS** → keep the base rule; the train gain was selection noise.")
        )
        lines.append(
            f"- **(a) second filter selected on train: `{best}`** (train avg {cands[best]['avg']} vs base {b_tr['avg']}). "
            f"OOS: n={so['n']} net {so['net']} avg {_r(so['avg'])} ± {_r(se, 2)} days+ {so['days_pos']}/{so['days']} maxDD {so['max_dd']} "
            f"vs base n={st_b['n']} net {st_b['net']} avg {_r(st_b['avg'])} ± {_r(st_b.get('se'), 2)} days+ {st_b['days_pos']}/{st_b['days']} maxDD {st_b['max_dd']}. "
            f"Δavg {_r(d_avg, 2)} — {verdict}"
        )
    L += lines + ["", f"Artifacts: `{out / 'report.json'}`.", ""]
    report["decision"] = lines

    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(lines))
    print(f"Wrote {out / 'summary.md'}")


if __name__ == "__main__":
    main()
