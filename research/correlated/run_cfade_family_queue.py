"""Run CFADE-family experiment queue EXP-0057..0066 sequentially.

Usage:
  python -m research.correlated.run_cfade_family_queue
  python -m research.correlated.run_cfade_family_queue --only 0059,0060
"""
from __future__ import annotations

import argparse
import importlib
import traceback


QUEUE = [
    ("0057", "research.correlated.exp_0057_allocator"),
    ("0058", "research.correlated.exp_0058_entry_timer"),
    ("0059", "research.correlated.exp_0059_es_venue"),
    ("0060", "research.correlated.exp_0060_role_swap"),
    ("0061", "research.correlated.exp_0061_burst_cont"),
    ("0062", "research.correlated.exp_0062_basis"),
    ("0063", "research.correlated.exp_0063_effort_result"),
    ("0064", "research.correlated.exp_0064_flow_inflection"),
    ("0065", "research.correlated.exp_0065_persist2"),
    ("0066", "research.correlated.exp_0066_tier_d_counts"),
]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma ids e.g. 0059,0060")
    ap.add_argument("--skip", default="", help="comma ids to skip")
    args = ap.parse_args(argv)
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    skip = {x.strip() for x in args.skip.split(",") if x.strip()}
    for eid, modname in QUEUE:
        if only and eid not in only:
            continue
        if eid in skip:
            print(f"SKIP {eid}", flush=True)
            continue
        print(f"\n======== EXP-{eid} ({modname}) ========", flush=True)
        try:
            mod = importlib.import_module(modname)
            mod.main()
        except Exception:
            traceback.print_exc()
            print(f"EXP-{eid} FAILED", flush=True)


if __name__ == "__main__":
    main()
