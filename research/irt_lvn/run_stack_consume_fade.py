"""EXP-0043_STACK_FADE — frozen StackConsume-intact fade at +2 s.

Freeze (do not retune):
  * entry: first print after +2 s at an IRT hold/break touch that is still
    undecided, and ``StackConsume ≤ −0.25`` (beyond-side near+far stack not
    consumed in the first 2 s)
  * direction: fade (against the approach)
  * exit: TP 30 / SL 15, 1-tick slip, 0.25 pt/RT, time stop +120 s

May is the OOS holdout. Oct/Sep are context only (the freeze was chosen there).

    python -m research.irt_lvn.run_stack_consume_fade
    python -m research.irt_lvn.run_stack_consume_fade --skip-run   # report from existing events only
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from research.config import ROOT
from research.irt_lvn.confirm import fade_when_score_le, pack_pnl


def _month_tag(month: str) -> str:
    y, m = (int(x) for x in month.split("-")[:2])
    return "" if (y, m) == (2026, 10) else f"_{y}{m:02d}"


def _results_dir(month: str) -> Path:
    return ROOT / "results" / f"EXP-0043{_month_tag(month)}"


def _load_events(month: str) -> pa.Table | None:
    p = _results_dir(month) / "events.parquet"
    if not p.exists() or p.stat().st_size == 0:
        return None
    return pq.read_table(p)


def _run_month(month: str) -> None:
    out = _results_dir(month)
    cmd = [
        sys.executable, "-m", "research.irt_lvn.run_hold_break_of",
        "--month", month, "--out", str(out),
    ]
    print(f"[run] {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True, cwd=str(ROOT))


def _r(x, nd: int = 3):
    if x is None:
        return "—"
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return "—"


def evaluate_month(tbl: pa.Table, *, score: str, thr: float, tag: str) -> dict:
    """Frozen fade-only P&L + AMBIGUOUS / HOLD / BREAK splits + per-session."""
    pnl = fade_when_score_le(tbl, score, thr, tag)
    sess = np.array(tbl.column("session_id").to_pylist())
    ts = tbl.column("t_touch_ns").to_numpy().astype(np.float64)
    lab = np.array(tbl.column("label").to_pylist())
    und = tbl.column("undecided_at_tc").to_numpy().astype(np.float64) >= 0.5
    s = tbl.column(score).to_numpy().astype(np.float64)
    fired = und & (s <= thr) & np.isfinite(pnl)

    overall = pack_pnl(pnl, sess, ts)
    by_label: dict[str, dict] = {}
    for name in ("HOLD", "BREAK", "AMBIGUOUS"):
        m = fired & (lab == name)
        by_label[name] = pack_pnl(pnl[m], sess[m], ts[m])

    by_sess: dict[str, float] = {}
    for x, sid in zip(pnl[fired], sess[fired]):
        by_sess[str(sid)] = by_sess.get(str(sid), 0.0) + float(x)
    by_sess = {k: round(v, 2) for k, v in sorted(by_sess.items())}

    # universe context
    univ = {k: int((lab == k).sum()) for k in ("HOLD", "BREAK", "AMBIGUOUS")}
    und_c = {k: int((und & (lab == k)).sum()) for k in ("HOLD", "BREAK", "AMBIGUOUS")}
    fired_c = {k: int((fired & (lab == k)).sum()) for k in ("HOLD", "BREAK", "AMBIGUOUS")}

    return {
        "universe": univ,
        "undecided": und_c,
        "fired": fired_c,
        "overall": overall,
        "by_label": by_label,
        "per_session": by_sess,
        "score_med_fired": round(float(np.median(s[fired])), 4) if fired.any() else None,
        "n_sessions_eligible": int(len(set(sess.tolist()))),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Frozen StackConsume-intact fade dig")
    ap.add_argument("--config", default=str(ROOT / "research" / "experiments" / "EXP-0043_STACK_FADE.yaml"))
    ap.add_argument("--skip-run", action="store_true", help="Do not re-run missing months; only report")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        exp = yaml.safe_load(f)
    score = str(exp.get("score", "StackConsume"))
    thr = float(exp.get("thr", -0.25))
    tp = float(exp.get("tp_pts", 30.0))
    sl = float(exp.get("sl_pts", 15.0))
    tag = f"tp{tp:g}_sl{sl:g}"
    oos = str(exp.get("oos_month", "2026-05"))
    ctx = [str(m) for m in exp.get("context_months", ["2026-10", "2026-09"])]
    months = [oos] + [m for m in ctx if m != oos]

    out = Path(args.out) if args.out else ROOT / "results" / "EXP-0043_STACK_FADE"
    out.mkdir(parents=True, exist_ok=True)

    # Ensure OOS month has events (run full EXP-0043 pipeline if needed)
    if not args.skip_run:
        if _load_events(oos) is None:
            _run_month(oos)
        for m in ctx:
            if _load_events(m) is None:
                print(f"[warn] no events for context month {m}; skipping", flush=True)

    report: dict = {
        "experiment": "EXP-0043_STACK_FADE",
        "hypothesis": "H34",
        "freeze": {
            "score": score, "thr": thr, "side": "fade",
            "confirm_ms": int(exp.get("confirm_ms", 2000)),
            "tp_pts": tp, "sl_pts": sl,
            "slip_ticks": float(exp.get("slip_ticks", 1.0)),
            "commission_pts_rt": float(exp.get("commission_pts_rt", 0.25)),
            "horizon_ms": int(exp.get("horizon_ms", 120_000)),
            "pnl_column": f"pnl_fade_tc_{tag}",
        },
        "oos_month": oos,
        "months": {},
    }

    lines = [
        "# EXP-0043_STACK_FADE — frozen StackConsume-intact fade",
        "",
        "**Freeze (do not retune):** at IRT key-level first touch, wait +2 s; if still undecided "
        f"and `{score} ≤ {thr:g}`, **fade** with TP {tp:g} / SL {sl:g} "
        f"(1-tick slip, {exp.get('commission_pts_rt', 0.25)} pt/RT, time stop +{int(exp.get('horizon_ms', 120000))/1000:g}s).",
        "",
        f"**OOS holdout:** `{oos}`. Oct/Sep are context (freeze chosen there).",
        "",
        "## Results by month",
        "",
        "| month | role | n | net | avg | win | days+ | tpd | maxDD | fired HOLD/BREAK/AMB |",
        "|---|---|---:|---:|---:|---:|---|---:|---:|---|",
    ]

    for month in months:
        tbl = _load_events(month)
        if tbl is None:
            lines.append(f"| {month} | {'OOS' if month == oos else 'context'} | — | — | — | — | — | — | — | missing events |")
            report["months"][month] = {"note": "missing_events"}
            continue
        if score not in tbl.column_names or f"pnl_fade_tc_{tag}" not in tbl.column_names:
            lines.append(f"| {month} | {'OOS' if month == oos else 'context'} | — | — | — | — | — | — | — | events lack freeze columns |")
            report["months"][month] = {"note": "missing_columns", "columns": tbl.column_names}
            continue
        st = evaluate_month(tbl, score=score, thr=thr, tag=tag)
        report["months"][month] = st
        role = "OOS" if month == oos else "context"
        o = st["overall"]
        f = st["fired"]
        lines.append(
            f"| {month} | {role} | {o['n']} | {o['net']} | {_r(o['avg'])} | {_r(o['win'])} | "
            f"{o['days_pos']}/{o['days']} | {_r(o['tpd'], 2)} | {o['max_dd']} | "
            f"{f.get('HOLD', 0)}/{f.get('BREAK', 0)}/{f.get('AMBIGUOUS', 0)} |"
        )

    lines += ["", "## OOS detail", ""]
    oos_st = report["months"].get(oos, {})
    if "overall" in oos_st:
        o = oos_st["overall"]
        lines += [
            f"- Universe (sampled): `{oos_st['universe']}` · undecided: `{oos_st['undecided']}` · fired: `{oos_st['fired']}`",
            f"- Overall: n={o['n']} net={o['net']} avg={_r(o['avg'])} win={_r(o['win'])} "
            f"days+={o['days_pos']}/{o['days']} tpd={_r(o['tpd'], 2)} maxDD={o['max_dd']}",
            "",
            "### By eventual label (diagnostic — not known at entry)",
            "",
            "| label | n | net | avg | win | days+ | maxDD |",
            "|---|---:|---:|---:|---:|---|---:|",
        ]
        for name in ("HOLD", "BREAK", "AMBIGUOUS"):
            b = oos_st["by_label"][name]
            lines.append(
                f"| {name} | {b['n']} | {b['net']} | {_r(b['avg'])} | {_r(b['win'])} | "
                f"{b['days_pos']}/{b['days']} | {b['max_dd']} |"
            )
        lines += ["", "### Per-session net", "", "```", json.dumps(oos_st["per_session"], indent=2), "```", ""]

        # Verdict
        ctx_nets = []
        for m in ctx:
            st = report["months"].get(m, {})
            if "overall" in st and st["overall"]["n"] > 0:
                ctx_nets.append(st["overall"]["net"])
        oos_net = o["net"]
        if o["n"] < 20:
            verdict = f"**thin OOS** (n={o['n']}) — do not ship; need more depth days or forward paper."
        elif oos_net > 0 and all(x > 0 for x in ctx_nets):
            verdict = (
                f"**soft confirm** — OOS net {oos_net} with context all positive "
                f"({', '.join(str(x) for x in ctx_nets)}). Still one gate of three frozen post hoc; "
                "next is forward paper / May AMBIGUOUS carry check before any live size."
            )
        elif oos_net > 0:
            verdict = f"**mixed** — OOS net {oos_net} but context not uniformly positive; park / dig further."
        else:
            verdict = f"**reject / park** — OOS net {oos_net} (n={o['n']}, days+ {o['days_pos']}/{o['days']})."
        lines += ["## Decision", "", verdict, "", f"Artifacts: `{out / 'report.json'}`.", ""]
        report["decision"] = verdict
    else:
        lines += ["_OOS events missing — conversion or run failed._", ""]
        report["decision"] = "missing_oos"

    # Context by-label appendix
    lines += ["## Context months — by-label appendix", ""]
    for month in ctx:
        st = report["months"].get(month, {})
        if "by_label" not in st:
            continue
        lines += [f"### {month}", "", "| label | n | net | avg | win | days+ | maxDD |", "|---|---:|---:|---:|---:|---|---:|"]
        for name in ("HOLD", "BREAK", "AMBIGUOUS"):
            b = st["by_label"][name]
            lines.append(
                f"| {name} | {b['n']} | {b['net']} | {_r(b['avg'])} | {_r(b['win'])} | "
                f"{b['days_pos']}/{b['days']} | {b['max_dd']} |"
            )
        lines.append("")

    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out / 'summary.md'}", flush=True)
    print(report.get("decision", ""), flush=True)


if __name__ == "__main__":
    main()
