"""One-off: HV Extreme fixed SL10/TP30 on all October parquet sessions."""
from __future__ import annotations

import json
from pathlib import Path

from research.hvext.run_hvext_sweep import SignalCell, fixed_bracket, pack_summary, simulate_cell
from research.run_experiment import _load_trades, load_config

OCT = [
    "011026",
    "021026",
    "051026",
    "061026",
    "071026",
    "081026",
    "091026",
    "121026",
    "131026",
    "141026",
    "151026",
    "161026",
    "191026",
]


def run_one(parquet_dir: Path, cell: SignalCell, label: str) -> dict:
    bfn = fixed_bracket(30.0, 10.0)
    s = simulate_cell(parquet_dir, OCT, cell, bfn)
    pack = pack_summary(s)
    print(f"\n=== {label} ===")
    print(
        f"n={pack['n']}  wr={pack.get('wr')}  pf={pack.get('pf')}  "
        f"net={pack.get('net')}  maxDD={pack.get('max_dd')}  "
        f"days+={pack.get('days_pos')}/{pack.get('n_sessions')}"
    )
    print(f"long n={pack['n_long']} net={pack['net_long']}  "
          f"short n={pack['n_short']} net={pack['net_short']}")
    print("per-session net (after 0.25 rt commission):")
    for sid in OCT:
        nq = _load_trades(parquet_dir, sid, "NQ")
        net = pack["per_session"].get(sid)
        if net is None:
            print(f"  {sid}: no trades (NQ rows={nq.num_rows})")
        else:
            print(f"  {sid}: {net:+.2f}")
    return pack


def main() -> int:
    cfg = load_config()
    p = Path(cfg["parquet_dir"])
    print("October sessions:", ", ".join(OCT))
    print("Bracket: SL 10 / TP 30 | UK 14:30–17:00 | flat-only | cooldown 30s | commission 0.25")

    frozen = SignalCell(
        move_pts=20.0,
        confirm_pts=15.0,
        peak_band_pts=2.0,
        high_vol_mult=2.0,
        min_path_efficiency=0.0,
        max_pullback_pts=5.0,
        max_leg_sec=60.0,
    )
    panel = SignalCell(
        move_pts=20.0,
        confirm_pts=10.0,
        peak_band_pts=5.0,
        high_vol_mult=2.0,
        min_path_efficiency=0.0,
        max_pullback_pts=5.0,
        max_leg_sec=0.0,
    )
    a = run_one(p, frozen, "EXP-0037 frozen signal (m20 c15 b2 hv2 pb5 leg60)")
    b = run_one(p, panel, "Panel signal (m20 c10 b5 hv2 pb5)")

    out = {
        "sessions": OCT,
        "bracket": {"sl": 10.0, "tp": 30.0},
        "frozen": {k: v for k, v in a.items()},
        "panel": {k: v for k, v in b.items()},
    }
    dest = Path("results/EXP-0037/october_10_30.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
