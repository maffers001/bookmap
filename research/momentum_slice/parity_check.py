"""Parity checklist: expected Python MOMENTUM_V1 / PC_V1 signals vs Java CSV log.

  .venv\\Scripts\\python.exe -m research.momentum_slice.parity_check
  .venv\\Scripts\\python.exe -m research.momentum_slice.parity_check \
      --java-log lst_momentum_signals.csv --kind MOMENTUM

Without a Java log, prints the expected Python events for the anchor + tagged
TRUE set (for Bookmap replay verification).
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from research.momentum_slice.rules import enrich, f, is_momentum_v1, is_pc_v1
from research.momentum_slice.taglib import BASE, load_tagged_features

UK = ZoneInfo("Europe/London")
TOL_NS = 2_000_000_000  # confirm-window tolerance


def bst(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).astimezone(UK).strftime("%Y-%m-%d %H:%M:%S")


def expected(kind: str) -> list[dict]:
    rows = [enrich(r) for r in load_tagged_features()]
    out = []
    for r in rows:
        if kind == "MOMENTUM" and is_momentum_v1(r):
            out.append(r)
        elif kind == "PITCH_CATCH" and is_pc_v1(r):
            out.append(r)
    return out


def load_java(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="MOMENTUM", choices=["MOMENTUM", "PITCH_CATCH"])
    ap.add_argument("--java-log", default="")
    ap.add_argument("--out", default=str(BASE / "parity"))
    args = ap.parse_args()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    exp = expected(args.kind)
    lines = [
        f"# Parity — {args.kind}", "",
        f"Python expected fires (train features): {len(exp)}", "",
        "## Anchor + tagged expectations", "",
    ]
    for r in exp:
        tag = r.get("lst_tag", "")
        lines.append(
            f"- {r['session']} {r['side']} ign={r.get('ignition_bst')} conf_ns={r.get('confirm_ns')} "
            f"trend={r.get('trend_5m_pts')} dom={r.get('nq_dom_vs_med')} tag={tag}"
        )
    # Hard checks from plan
    anchor = [r for r in exp if r["session"] == "100926" and "15:24:38" in str(r.get("ignition_bst", ""))]
    short_1527 = [r for r in load_tagged_features()
                  if r["session"] == "100926" and "15:27" in str(r.get("ignition_bst", ""))]
    lines += ["", "## Plan hard checks"]
    lines.append(f"- 100926 15:24:38 LONG must fire momentum: "
                 f"{'PASS' if any(is_momentum_v1(enrich(r)) for r in load_tagged_features()
                                   if r['session']=='100926' and '15:24:38' in str(r.get('ignition_bst',''))) else 'FAIL'}")
    lines.append(f"- 100926 15:27 SHORT must NOT fire momentum: "
                 f"{'PASS' if not any(is_momentum_v1(enrich(r)) for r in short_1527) else 'FAIL'}")

    if args.java_log:
        jpath = Path(args.java_log)
        jrows = [r for r in load_java(jpath) if r.get("kind") == args.kind]
        lines += ["", f"## Java log {jpath} ({len(jrows)} {args.kind} rows)", ""]
        matched = 0
        for r in exp:
            cns = int(float(r["confirm_ns"]))
            hit = None
            for j in jrows:
                jts = int(float(j["signal_ts_ns"]))
                if abs(jts - cns) <= TOL_NS or abs(jts - (cns + 5_000_000_000)) <= TOL_NS:
                    hit = j; break
            ok = hit is not None
            matched += int(ok)
            lines.append(f"- {r['session']} {r.get('ignition_bst')}: "
                         f"{'MATCH' if ok else 'MISS'}"
                         + (f" java_ts={hit['signal_ts_ns']} level={hit.get('dominant_level_px')}" if hit else ""))
        lines.append(f"", f"matched {matched}/{len(exp)}")
        # extras
        extras = 0
        for j in jrows:
            jts = int(float(j["signal_ts_ns"]))
            if not any(abs(jts - int(float(r["confirm_ns"]))) <= TOL_NS
                       or abs(jts - int(float(r["confirm_ns"])) - 5_000_000_000) <= TOL_NS
                       for r in exp):
                extras += 1
        lines.append(f"java extras (not in python expected): {extras}")
    else:
        lines += ["", "## Java log", "No --java-log supplied. Replay 100926 in Bookmap with",
                  "`LST Momentum` / `LST Pitch-and-Catch`, then re-run with the CSV path.",
                  "Confirm-window tolerance = 2 s. Compare dominant_level_px and state."]

    lines += ["", "## Replay performance checklist",
              "- Warmup: no signals until ≥5 min tape + book depth for both legs",
              "- Latency: signal_ts − confirm_ns should be small for momentum; PC ≥5 s stall",
              "- Dual-instrument: ES open; confirm requires both bursts", ""]
    text = "\n".join(lines) + "\n"
    (out / f"parity_{args.kind.lower()}.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
