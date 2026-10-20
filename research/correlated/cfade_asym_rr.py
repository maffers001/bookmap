"""CFADE asymmetric vol R:R sweep (tight stop / wider target).

Frozen signal: against_m04 + skip_er0.3 (EXP-0036_DIG4). Only the bracket
changes. Hypothesis: correlated absorption marks a rotation tip → smaller SL
and larger TP should help if the fade catches the turn.

Reports DIG4 23d (train/hold/new) and the full available session set.
Exploratory — DIG4 freeze said no further param search on the 23d sample.
"""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from research.backtest.mat_sim import (
    MatSimConfig,
    SimSummary,
    make_daytype_skip,
    make_vol_bracket,
    make_vol_bracket_rr,
    simulate_session,
)
from research.correlated import harness as H
from research.correlated.exp_0036_fade_dig import _concentration
from research.features.corr_exhaust import ExhaustParams, exhaust_fade_events
from research.run_experiment import load_config


def _sim(
    pairs: list[H.Pair],
    by_sid: dict[str, pa.Table],
    *,
    bracket_fn,
    gate,
    slip: float = 1.0,
) -> SimSummary:
    cfg = MatSimConfig(
        slip_ticks=slip,
        commission_pts_rt=H.COMMISSION,
        tp_pts=10,
        sl_pts=10,
        signal_cooldown_sec=5.0,
    )
    parts = [
        simulate_session(
            p.sid, p.nq, p.es, cfg, gate=gate, bracket_fn=bracket_fn,
            vwap_from_ns=p.vwap_from_ns, events=by_sid[p.sid],
        )
        for p in pairs if p.sid in by_sid
    ]
    return H.merge(parts) if parts else SimSummary()


def _load_fades(
    pairs: list[H.Pair],
    dig4_events: Path,
) -> dict[str, pa.Table]:
    """Prefer DIG4 frozen events; synthesize for sessions not in that file."""
    by: dict[str, pa.Table] = {}
    if dig4_events.exists():
        ev = pq.read_table(dig4_events)
        idxs: dict[str, list[int]] = {}
        for i, s in enumerate(ev.column("session_id").to_pylist()):
            idxs.setdefault(str(s), []).append(i)
        by = {s: ev.take(ix) for s, ix in idxs.items()}

    p_ex = ExhaustParams(
        es_imb_max=-0.4, es_min_vol=0, use_imb=True, use_vol=False,
        nq_min_imbalance=0.55, nq_min_vol=25, nq_min_trades=6,
    )
    for p in pairs:
        if p.sid in by and by[p.sid].num_rows > 0:
            continue
        nq_b, es_b = p.bars(100)
        bursts = H.confirm_bursts(p)
        fe = exhaust_fade_events(bursts, nq_b, es_b, p_ex, max_wait_sec=5.0)
        if fe.num_rows:
            by[p.sid] = H._with_session(fe, p.sid)
    return by


def _bracket_grid() -> list[tuple[str, object]]:
    """Baseline symmetric + tight-stop / wide-target cells."""
    out: list[tuple[str, object]] = [
        ("vol_sym_k1 (baseline)", make_vol_bracket(1.0, floor=6.0, cap=30.0)),
    ]
    # unit = 1 × median 30m minute range; SL=sl_r×unit, TP=tp_r×unit
    cells = [
        (0.5, 1.0),
        (0.5, 1.5),
        (0.5, 2.0),
        (0.5, 2.5),
        (0.75, 1.5),
        (0.75, 2.0),
        (0.75, 2.5),
        (1.0, 1.5),
        (1.0, 2.0),
        (1.0, 2.5),
        (1.0, 3.0),
    ]
    for sl_r, tp_r in cells:
        name = f"vol_rr sl{sl_r:g}/tp{tp_r:g}"
        out.append((name, make_vol_bracket_rr(
            1.0, tp_r=tp_r, sl_r=sl_r, floor=4.0, cap=40.0,
        )))
    return out


def _pack(sim: SimSummary, sids: list[str]) -> dict:
    r = H.pack_pnl(sim, sids)
    conc = _concentration(sim.trades, sids)
    return {**r, "concentration": conc}


def _run_slice(
    label: str,
    pairs: list[H.Pair],
    by_sid: dict[str, pa.Table],
    gate,
) -> dict:
    sids = [p.sid for p in pairs]
    rows = []
    print(f"\n=== {label} ({len(sids)} sessions) ===", flush=True)
    for name, br in _bracket_grid():
        sim = _sim(pairs, by_sid, bracket_fn=br, gate=gate)
        r = _pack(sim, sids)
        rows.append({
            "name": name,
            "n": r["n"],
            "wins": r["wins"],
            "wr": r["wr"],
            "gross": r["gross"],
            "net": r["net"],
            "pf": r["pf"],
            "max_dd": r["max_dd"],
            "train_net": r["train_net"],
            "hold_net": r["hold_net"],
            "new_net": r["new_net"],
            "days_pos": r["concentration"]["days_pos"],
            "n_sessions": r["n_sessions"],
            "worst_day": r["worst_day"],
            "best_day": r["best_day"],
            "top3_share": r["concentration"]["top3_share_of_pos"],
        })
        print(
            f"  {name:28s} n={r['n']:4d} WR={r['wr']:.2f} net={r['net']:+8.1f} "
            f"train={r['train_net']:+7.1f} hold={r['hold_net']:+7.1f} "
            f"new={r['new_net']:+7.1f} days+={r['concentration']['days_pos']}/"
            f"{r['n_sessions']} DD={r['max_dd']:.0f}",
            flush=True,
        )
    return {"sessions": sids, "rows": rows}


def main() -> None:
    cfg = load_config()
    parquet = Path(cfg["parquet_dir"])
    out_dir = Path(cfg["results_dir"]) / "CFADE_ASYM_RR"
    out_dir.mkdir(parents=True, exist_ok=True)

    dig4 = json.loads((Path(cfg["results_dir"]) / "EXP-0036_DIG4" / "report.json").read_text())
    dig4_sids = list(dig4["sessions"])
    all_sids = H.list_sessions(parquet)

    dig4_pairs = [p for sid in dig4_sids if (p := H.load_pair(parquet, sid)) is not None]
    all_pairs = [p for sid in all_sids if (p := H.load_pair(parquet, sid)) is not None]

    dig4_events = Path(cfg["results_dir"]) / "EXP-0036_DIG4" / "events.parquet"
    print("[CFADE asym] loading fades…", flush=True)
    by_all = _load_fades(all_pairs, dig4_events)
    by_dig4 = {s: by_all[s] for s in dig4_sids if s in by_all}

    gate = make_daytype_skip(max_er=0.3)
    report = {
        "signal": "against_m04 + skip_er0.3 (DIG4 freeze)",
        "hypothesis": "tight SL / wide TP — absorption as rotation tip",
        "note": (
            "Exploratory bracket sweep only. DIG4 freeze forbade further param "
            "search on the 23d sample; treat dig4_23d as in-sample and all_32 as "
            "the broader book (includes extra Sep days + 191026)."
        ),
        "dig4_23d": _run_slice("DIG4 23d (freeze sample)", dig4_pairs, by_dig4, gate),
        "all_32": _run_slice("All available sessions", all_pairs, by_all, gate),
    }

    # Sep / Oct slices for readability
    def month(pairs, mm: str):
        return [p for p in pairs if len(p.sid) == 6 and p.sid[2:4] == mm]

    report["september"] = _run_slice("September", month(all_pairs, "09"), by_all, gate)
    report["october"] = _run_slice("October", month(all_pairs, "10"), by_all, gate)

    json_path = out_dir / "report.json"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    md = [
        "# CFADE asymmetric vol R:R",
        "",
        report["note"],
        "",
        "Signal frozen: `against_m04` + `skip_er0.3`, UK 14:30–17:00, 1-tick slip, 0.25 pt/RT.",
        "Baseline: `TP=SL=clip(1×med30m, 6, 30)`.",
        "Asym: `unit=1×med30m`; `SL=sl_r×unit`, `TP=tp_r×unit`, each clipped to [4, 40].",
        "",
    ]
    for key, title in (
        ("dig4_23d", "DIG4 23d freeze sample"),
        ("all_32", "All 32 available sessions"),
        ("september", "September"),
        ("october", "October"),
    ):
        block = report[key]
        md += [
            f"## {title}",
            "",
            f"Sessions={len(block['sessions'])}",
            "",
            "| bracket | n | WR | net | train | hold | new | days+ | maxDD |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for r in block["rows"]:
            md.append(
                f"| {r['name']} | {r['n']} | {r['wr']:.2f} | {r['net']:+.1f} | "
                f"{r['train_net']:+.1f} | {r['hold_net']:+.1f} | {r['new_net']:+.1f} | "
                f"{r['days_pos']}/{r['n_sessions']} | {r['max_dd']:.0f} |"
            )
        md.append("")

    # Quick verdict helpers
    base = report["dig4_23d"]["rows"][0]
    asym = [r for r in report["dig4_23d"]["rows"] if r["name"] != base["name"]]
    beat = [r for r in asym if r["net"] > base["net"] and r["train_net"] > 0
            and r["hold_net"] > 0 and r["new_net"] > 0]
    md += [
        "## Read",
        "",
        f"DIG4 baseline net **{base['net']:+.1f}** (train {base['train_net']:+.1f}).",
    ]
    if beat:
        best = max(beat, key=lambda r: r["net"])
        md.append(
            f"Asym cells that beat baseline net with all cohorts >0: "
            f"**{len(beat)}** — best `{best['name']}` net {best['net']:+.1f} "
            f"(train {best['train_net']:+.1f} / hold {best['hold_net']:+.1f} / "
            f"new {best['new_net']:+.1f})."
        )
    else:
        md.append(
            "No asym cell beat baseline net while keeping train∧hold∧new > 0 on the DIG4 23d."
        )
    oct_base = report["october"]["rows"][0]
    oct_best = max(report["october"]["rows"], key=lambda r: r["net"])
    md += [
        "",
        f"October (broader book): baseline **{oct_base['net']:+.1f}**; "
        f"best cell `{oct_best['name']}` **{oct_best['net']:+.1f}**.",
        "",
    ]
    md_path = out_dir / "summary.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"\nwrote {json_path}")
    print(f"wrote {md_path}")


if __name__ == "__main__":
    main()
