"""One-shot OOS: freeze retest on sessions outside Sep+Oct working set."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from research.hvext.redesign import runner as R

HOLDOUT = ["030826", "040826", "050826", "260326"]


def main() -> int:
    cfg = R.PANEL
    rp = R.RetestParams(
        retest_pts=10, timeout_sec=300, stop_beyond_pts=2, tp_mult=3.0
    )
    for sid in HOLDOUT:
        nq = R.load_nq(sid)
        print(f"{sid}: nq_rows={nq.num_rows}", flush=True)

    pack, trades = R.run_month(HOLDOUT, cfg, entry="retest", retest=rp)
    wins = defaultdict(int)
    counts = defaultdict(int)
    for t in trades:
        counts[t.session_id] += 1
        if t.win:
            wins[t.session_id] += 1

    print("--- FREEZE retest R10/t300/sb2/x3 ---")
    print(
        f"n={pack.get('n')} wins={pack.get('wins')} wr={pack.get('wr')} "
        f"net={pack.get('net')} pf={pack.get('pf')} "
        f"days_pos={pack.get('days_pos')}/{pack.get('n_sessions')} "
        f"fill_rate={pack.get('fill_rate')} fires={pack.get('n_fires')}"
    )
    for sid in HOLDOUT:
        nn = counts.get(sid, 0)
        ww = wins.get(sid, 0)
        wr = (ww / nn) if nn else None
        print(
            f"  {sid}: n={nn} wins={ww} wr={wr} "
            f"net={pack.get('per_session', {}).get(sid)}"
        )

    rp2 = R.RetestParams(
        retest_pts=8, timeout_sec=300, stop_beyond_pts=4, tp_mult=3.0
    )
    pack2, _ = R.run_month(HOLDOUT, cfg, entry="retest", retest=rp2)
    print("--- NEIGHBOUR R8/t300/sb4/x3 ---")
    print(
        f"n={pack2.get('n')} wins={pack2.get('wins')} wr={pack2.get('wr')} "
        f"net={pack2.get('net')}"
    )

    pack3, _ = R.run_month(HOLDOUT, cfg, entry="extension")
    print("--- EXTENSION baseline SL10/TP30 ---")
    print(
        f"n={pack3.get('n')} wins={pack3.get('wins')} wr={pack3.get('wr')} "
        f"net={pack3.get('net')}"
    )

    out = {
        "sessions": HOLDOUT,
        "freeze": pack,
        "neighbour": pack2,
        "extension": pack3,
        "per_day_wr": {
            sid: {
                "n": counts.get(sid, 0),
                "wins": wins.get(sid, 0),
                "wr": (wins[sid] / counts[sid]) if counts.get(sid) else None,
                "net": pack.get("per_session", {}).get(sid),
            }
            for sid in HOLDOUT
        },
    }
    dest = Path("results/EXP-0038/holdout_retest.json")
    dest.write_text(json.dumps(out, indent=2, default=str))
    print(f"Wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
