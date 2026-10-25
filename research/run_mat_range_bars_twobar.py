"""Two-bar trapped-traders dig for EXP-0056 (discovery Apr–Aug only).

Rule (prior = last completed 50-tick bar B1, prior2 = bar before it B2):
  - B2 high volume: B2 vol_ratio >= 1.5
  - B1 mid/low volume: B1 vol_ratio <= 1.4
  - Push away from B2 VPOC, as specified:
      long  (MAT side +1): B1 low  > B2 VPOC
      short (MAT side -1): B1 low  < B2 VPOC   [literal]
    Strict symmetric diagnostic instead requires, for shorts, B1 high < VPOC
    (whole B1 below VPOC), longs unchanged.

Context fixed to the previously frozen setting: MAT trend+VWAP gated, rr1_1.
Sep–Oct is NOT touched: it was already read for the previous frozen arm, so
it cannot serve as a clean holdout for this post-hoc rule.

Writes results/EXP-0056_TWOBAR/.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from research.config import load_config
from research.features.range_bar_profile import validate_synthetic
from research.run_mat_range_bars import discovery_pass
from research.run_mat_range_bars_dig import candidate_masks, gated_events, load_raw, simulate_mask

MASKS = [
    "twobar_trap_literal",
    "twobar_trap_strict",
    "twobar_trap_literal_bar_against",
    "twobar_trap_strict_bar_against",
]
COMPARATORS = ["base", "vol_mid_bar_against"]


def main() -> None:
    errs = validate_synthetic()
    if errs:
        raise SystemExit(f"Stage-0 synthetic failed: {errs}")

    cfg = load_config()
    out = Path(cfg["results_dir"]) / "EXP-0056_TWOBAR"
    out.mkdir(parents=True, exist_ok=True)

    data = load_raw(cfg)  # DISCOVERY_MMYY only
    events = gated_events(data, True, True)

    # Event counts (before simulator's one-trade-at-a-time filtering).
    counts: dict[str, dict] = {}
    for name in COMPARATORS + MASKS:
        n_long = n_short = 0
        for sid, sd in data.items():
            ev = events[sid]
            if ev.num_rows == 0:
                continue
            mask = candidate_masks(ev)[name]
            side = ev.column("side").to_numpy().astype(int)
            n_long += int((mask & (side > 0)).sum())
            n_short += int((mask & (side < 0)).sum())
        counts[name] = {"events": n_long + n_short, "long": n_long, "short": n_short}
        print(f"count {name}: events={n_long + n_short} long={n_long} short={n_short}", flush=True)

    results: dict[str, dict] = {}
    for name in COMPARATORS + MASKS:
        r = simulate_mask(data, events, name, tp_r=1.0, sl_r=1.0)
        r["events"] = counts[name]
        results[name] = r
        print(
            f"sim {name}: n={r.get('n')} avg={r.get('avg')} net={r.get('net')} "
            f"wr={r.get('wr')} pf={r.get('pf')} days+={r.get('days_pos')}/{r.get('days')}",
            flush=True,
        )

    base_avg = results["base"].get("avg")
    verdicts = {}
    for name in MASKS:
        ok, reason = discovery_pass(results[name], base_avg)
        verdicts[name] = {"pass": ok, "reason": reason}
        print(f"verdict {name}: pass={ok} ({reason})", flush=True)

    passing = [n for n, v in verdicts.items() if v["pass"]]
    report = {
        "note": (
            "Exploratory two-bar dig on discovery Apr-Aug only. Sep-Oct already "
            "read for previous frozen arm; not a clean holdout for this rule."
        ),
        "definition": {
            "b2_high_vol": "prior2 vol_ratio >= 1.5",
            "b1_midlow_vol": "prior vol_ratio <= 1.4",
            "trap_literal": "long: prior_low > prior2_vpoc; short: prior_low < prior2_vpoc",
            "trap_strict": "long: prior_low > prior2_vpoc; short: prior_high < prior2_vpoc",
            "gate": "MAT trend+VWAP",
            "bracket": "rr1_1",
        },
        "results": results,
        "verdicts": verdicts,
        "passing": passing,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    md = [
        "# EXP-0056 two-bar trapped-traders dig (discovery Apr–Aug)",
        "",
        "B2 (bar before prior) high volume `vol_ratio>=1.5`; B1 (prior) mid/low "
        "`vol_ratio<=1.4`; push away from B2 VPOC. Gated MAT (trend+VWAP), rr1_1.",
        "Literal short leg uses B1 **low** < VPOC (as specified); strict diagnostic "
        "requires B1 **high** < VPOC for shorts. Sep–Oct not touched (already read).",
        "",
        f"**Passing (EXP-0056 kill rules): {passing or 'none'}**",
        "",
        "| mask | events (L/S) | n | avg | net | WR | PF | days+ | verdict |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for name in COMPARATORS + MASKS:
        r = results[name]
        ev = r["events"]
        v = verdicts.get(name, {"pass": None, "reason": "comparator"})
        md.append(
            f"| {name} | {ev['events']} ({ev['long']}/{ev['short']}) | {r.get('n')} | "
            f"{r.get('avg')} | {r.get('net')} | {r.get('wr')} | {r.get('pf')} | "
            f"{r.get('days_pos')}/{r.get('days')} | {v['reason']} |"
        )
    md += ["", "## By month", "", "| mask | month | n | avg | net |", "|---|---|---:|---:|---:|"]
    for name in COMPARATORS + MASKS:
        for m, s in sorted((results[name].get("by_month") or {}).items()):
            md.append(f"| {name} | {m} | {s.get('n')} | {s.get('avg')} | {s.get('net')} |")
    md.append("")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
