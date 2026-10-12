"""Dig EXP-0011 dual LVN + EXP-0012 strata."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

UK = timezone(timedelta(hours=1))
ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ev = pq.read_table(ROOT / "results/EXP-0011/events.parquet")
    print("EXP-0011 dual n=", ev.num_rows)

    sessions = ev.column("session_id").to_pylist()
    sides = ev.column("side").to_numpy()
    hit10 = ev.column("hit_10pts").to_numpy()
    mfe = ev.column("mfe_pts").to_numpy()
    mae = ev.column("mae_pts").to_numpy()
    ret2 = ev.column("ret_pts_2000ms").to_numpy()
    ret10 = ev.column("ret_pts_10000ms").to_numpy()
    lags = (
        ev.column("es_lag_ms").to_numpy()
        if "es_lag_ms" in ev.column_names
        else None
    )

    print("\nBy side:")
    for s in (-1, 1):
        m = sides == s
        if m.any():
            print(
                f"  side={s} n={m.sum()} hit10={hit10[m].mean():.3f} "
                f"mean@2s={ret2[m].mean():.2f} mean@10s={ret10[m].mean():.2f} "
                f"mfe={mfe[m].mean():.1f} mae={mae[m].mean():.1f}"
            )

    print("\nBy session:")
    for sid in sorted(set(sessions)):
        m = np.array([x == sid for x in sessions])
        print(
            f"  {sid} n={m.sum()} hit10={hit10[m].mean():.3f} "
            f"@2s={ret2[m].mean():.2f} @10s={ret10[m].mean():.2f}"
        )

    if lags is not None:
        print(
            f"\nES lag ms: mean={lags.mean():.0f} med={np.median(lags):.0f} "
            f"max={lags.max():.0f}"
        )
        tight = lags < 500
        if tight.any():
            print(f"  lag<500ms n={tight.sum()} hit10={hit10[tight].mean():.3f}")

    beyond_detail = []
    p12 = ROOT / "results/EXP-0012/events.parquet"
    strata_note = {}
    if p12.exists():
        e2 = pq.read_table(p12)
        print("\nEXP-0012 events n=", e2.num_rows)
        if "beyond_untraded" in e2.column_names:
            b = e2.column("beyond_untraded").to_numpy()
            f = e2.column("flip").to_numpy()
            h = e2.column("hit_10pts").to_numpy()
            r10 = e2.column("ret_pts_10000ms").to_numpy()
            print(f"  beyond={int(b.sum())} flip={int(f.sum())} both={int(((b == 1) & (f == 1)).sum())}")
            strata_note = {
                "n_beyond": int(b.sum()),
                "n_flip": int(f.sum()),
                "n_both": int(((b == 1) & (f == 1)).sum()),
            }
            if b.sum():
                print(f"  beyond hit10={h[b == 1].mean():.3f} @10s={r10[b == 1].mean():.2f}")
                for i in np.where(b == 1)[0]:
                    ts = int(e2.column("event_ts_ns")[i].as_py())
                    dt = datetime.fromtimestamp(ts / 1e9, tz=timezone.utc).astimezone(UK)
                    row = {
                        "session_id": e2.column("session_id")[i].as_py(),
                        "uk_time": dt.strftime("%H:%M:%S"),
                        "side": int(e2.column("side")[i].as_py()),
                        "hit10": int(h[i]),
                        "ret10": float(r10[i]),
                        "beyond_consume": float(e2.column("beyond_consume")[i].as_py()),
                    }
                    beyond_detail.append(row)
                    print(
                        f"    {row['session_id']} {row['uk_time']} side={row['side']} "
                        f"hit10={row['hit10']} @10s={row['ret10']:.1f} "
                        f"consume={row['beyond_consume']}"
                    )

    dig = {
        "exp": "EXP-0011/0012",
        "verdict": "falsify_h7_no_ship; H8 gated empty (flip=0); beyond_only n=3 too small",
        "dual_n": int(ev.num_rows),
        "dual_hit10": float(hit10.mean()),
        "dual_hit20": float(ev.column("hit_20pts").to_numpy().mean()),
        "mean_ret_2s": float(ret2.mean()),
        "mean_ret_10s": float(ret10.mean()),
        "by_side": {},
        "by_session": {},
        "beyond_detail": beyond_detail,
        "strata_counts": strata_note,
        "ship_bookmap": False,
    }
    for s in (-1, 1):
        m = sides == s
        if m.any():
            dig["by_side"][str(s)] = {
                "n": int(m.sum()),
                "hit10": float(hit10[m].mean()),
                "ret2": float(ret2[m].mean()),
                "ret10": float(ret10[m].mean()),
            }
    for sid in sorted(set(sessions)):
        m = np.array([x == sid for x in sessions])
        dig["by_session"][sid] = {
            "n": int(m.sum()),
            "hit10": float(hit10[m].mean()),
            "ret2": float(ret2[m].mean()),
            "ret10": float(ret10[m].mean()),
        }
    out = ROOT / "results/EXP-0011/dig.json"
    out.write_text(json.dumps(dig, indent=2), encoding="utf-8")
    print("\nWrote", out)


if __name__ == "__main__":
    main()
