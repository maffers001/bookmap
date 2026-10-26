"""CFADE anatomy on the full clean sample — why does it work, can it improve?

Frozen book: against_m04 + skip_er0.3, vol_rr sl1/tp2 (k=1, clip 4–40),
UK 14:30–17:00, primary feed, 1-tick slip, 0.25 pt/RT.

Sections
  1. Attribution twins (same gates/brackets/costs):
       A  frozen CFADE  (confirm burst → ES-against bar while NQ still pushes → fade)
       B  NQ-only burst → ES-against bar → fade       (does ES *confirming* the burst matter?)
       C  confirm burst → ES *with* bar (≥+0.4) → fade (does ES *against* matter?)
       D  confirm burst → fade at the burst stamp     (is it just "fade every burst"?)
       E  random time-matched null, same n per day
  2. Event study on frozen fades: MFE/MAE/ret at 5 s…10 min (where does P&L live?)
  3. Exit-management variants on frozen entries (BE, trail, time stop).
  4. DIG3 strata re-read: in-sample (DIG4 23d) vs out-of-sample (rest).
  5. Day-level: fades/day, trades/day, day-kill rules, consecutive losses.

Writes results/CFADE_ANATOMY/{summary.md,report.json,trades.parquet}.
Excludes mislabeled folders 200626 / 220626 (May tapes).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.backtest.mat_sim import (
    MatSimConfig,
    TradeResult,
    make_daytype_skip,
    make_vol_bracket_rr,
    simulate_session,
)
from research.config import load_config
from research.correlated import harness as H
from research.features.corr_exhaust import ExhaustParams, exhaust_fade_events
from research.features.momentum import momentum_burst_events
from research.run_cfade_all_months import _discover_sessions

UK = ZoneInfo("Europe/London")
COMMISSION = 0.25
EXCLUDE = {"200626", "220626"}  # May tapes filed under June names

# DIG4 freeze sample (train 9 + hold 5 + new 9) — strata were tuned here.
DIG4_23D = set(H.TRAIN) | set(H.HOLD) | {
    "151026", "300926", "290926", "131026", "161026",
    "091026", "141026", "121026", "081026",
}

HORIZONS_MS = [5_000, 10_000, 30_000, 60_000, 120_000, 300_000, 600_000]
TARGETS = [5.0, 10.0, 20.0]


# ----------------------------------------------------------------- helpers

def _tod_min(ts_ns: int) -> int:
    d = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).astimezone(UK)
    return d.hour * 60 + d.minute


def _stat(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0, "wr": None, "net": 0.0, "avg": None, "days": 0, "days_pos": 0}
    nets = np.array([r["net"] for r in rows])
    wins = sum(1 for r in rows if r["win"])
    by_day: dict[str, float] = defaultdict(float)
    for r in rows:
        by_day[r["session_id"]] += r["net"]
    return {
        "n": n,
        "wr": round(wins / n, 3),
        "net": round(float(nets.sum()), 1),
        "avg": round(float(nets.mean()), 2),
        "days": len(by_day),
        "days_pos": sum(1 for v in by_day.values() if v > 0),
    }


def _row(name: str, s: dict) -> str:
    wr = f"{s['wr']:.2f}" if s["wr"] is not None else "–"
    avg = f"{s['avg']:+.2f}" if s["avg"] is not None else "–"
    return f"| {name} | {s['n']} | {wr} | {s['net']:+.1f} | {avg} | {s['days_pos']}/{s['days']} |"


HDR = ["| slice | n | WR | net | avg | days+ |", "|---|---:|---:|---:|---:|---:|"]


def _strata_block(title: str, rows: list[dict], key, buckets: list[tuple[str, callable]]) -> list[str]:
    """Rows split IS/OOS; buckets = [(label, pred(row))]."""
    out = [f"### {title}", "", "| slice | IS n | IS net | IS avg | OOS n | OOS WR | OOS net | OOS avg | OOS days+ |",
           "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for label, pred in buckets:
        sel = [r for r in rows if pred(r)]
        is_ = _stat([r for r in sel if r["is_"]])
        oos = _stat([r for r in sel if not r["is_"]])
        out.append(
            f"| {label} | {is_['n']} | {is_['net']:+.1f} | "
            f"{(f'{is_['avg']:+.2f}' if is_['avg'] is not None else '–')} | "
            f"{oos['n']} | {(f'{oos['wr']:.2f}' if oos['wr'] is not None else '–')} | "
            f"{oos['net']:+.1f} | {(f'{oos['avg']:+.2f}' if oos['avg'] is not None else '–')} | "
            f"{oos['days_pos']}/{oos['days']} |"
        )
    out.append("")
    return out


def _trades_to_rows(s, ev: pa.Table, sid: str) -> list[dict]:
    """Join TradeResult → event extras by (entry_ts, side)."""
    ev_map: dict[tuple[int, int], dict] = {}
    if ev.num_rows:
        d = ev.to_pydict()
        n = ev.num_rows
        for i in range(n):
            ev_map[(int(d["event_ts_ns"][i]), int(d["side"][i]))] = {k: d[k][i] for k in d}
    rows = []
    for tr in s.trades:
        e = ev_map.get((tr.entry_ts_ns, tr.side), {})
        rows.append({
            "session_id": sid,
            "month": sid[2:4],
            "is_": sid in DIG4_23D,
            "entry_ts_ns": tr.entry_ts_ns,
            "exit_ts_ns": tr.exit_ts_ns,
            "side": tr.side,
            "pnl": tr.pnl,
            "net": tr.pnl - COMMISSION,
            "win": tr.win,
            "reason": tr.reason,
            "tp": tr.tp_pts,
            "sl": tr.sl_pts,
            "hold_s": (tr.exit_ts_ns - tr.entry_ts_ns) / 1e9 if tr.exit_ts_ns else float("nan"),
            "er60": tr.er60,
            "range60": tr.range60,
            "net30": tr.net30,
            "vwap": tr.vwap,
            "entry_px": tr.entry_px,
            "med_r30": tr.med_r30,
            "tod": _tod_min(tr.entry_ts_ns),
            "wait_ms": float(e.get("wait_ms", np.nan)),
            "es_imb_x": float(e.get("es_imb_x", np.nan)),
            "nq_imb_x": float(e.get("nq_imb_x", np.nan)),
            "es_vol_x": float(e.get("es_vol_x", np.nan)),
            "nq_vol_x": float(e.get("nq_vol_x", np.nan)),
            "burst_es_vol": float(e.get("burst_es_total_vol", np.nan)),
            "burst_nq_vol": float(e.get("burst_total_vol", np.nan)),
            "burst_imb": float(e.get("burst_imbalance", np.nan)),
            "es_signed_lag_ms": float(e.get("burst_es_signed_lag_ms", np.nan)),
        })
    return rows


def _flip_es_imb(es_bars: pa.Table) -> pa.Table:
    if es_bars.num_rows == 0:
        return es_bars
    imb = -es_bars.column("imbalance").to_numpy().astype(np.float64)
    i = es_bars.column_names.index("imbalance")
    return es_bars.set_column(i, "imbalance", pa.array(imb))


def _seq_day_rules(rows: list[dict]) -> dict:
    """Day-kill / cap rules applied sequentially to the frozen trade list."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[r["session_id"]].append(r)
    rules = {
        "frozen": lambda seq: seq,
        "max3_trades": lambda seq: seq[:3],
        "max4_trades": lambda seq: seq[:4],
        "max6_trades": lambda seq: seq[:6],
        "stop_after_2_consec_losses": None,
        "stop_after_3_consec_losses": None,
        "stop_at_day_-60": None,
        "stop_at_day_-100": None,
        "stop_at_day_+60(lock)": None,
        "stop_at_day_+100(lock)": None,
    }
    out = {}
    for name in rules:
        total = 0.0
        n = 0
        days_pos = 0
        day_nets = []
        for sid, seq in by_day.items():
            seq = sorted(seq, key=lambda r: r["entry_ts_ns"])
            kept = []
            consec = 0
            eq = 0.0
            for r in seq:
                if name == "stop_after_2_consec_losses" and consec >= 2:
                    break
                if name == "stop_after_3_consec_losses" and consec >= 3:
                    break
                if name == "stop_at_day_-60" and eq <= -60:
                    break
                if name == "stop_at_day_-100" and eq <= -100:
                    break
                if name == "stop_at_day_+60(lock)" and eq >= 60:
                    break
                if name == "stop_at_day_+100(lock)" and eq >= 100:
                    break
                kept.append(r)
                eq += r["net"]
                consec = consec + 1 if r["net"] < 0 else 0
            if rules[name] is not None:
                kept = rules[name](seq)
            dn = sum(r["net"] for r in kept)
            n += len(kept)
            total += dn
            day_nets.append(dn)
            days_pos += dn > 0
        # path DD across days in calendar order
        eq = pk = dd = 0.0
        for sid in sorted(by_day, key=lambda s: (s[4:6], s[2:4], s[0:2])):
            seq = sorted(by_day[sid], key=lambda r: r["entry_ts_ns"])
            # recompute kept quickly
            kept = []
            consec = 0
            e2 = 0.0
            for r in seq:
                if name == "stop_after_2_consec_losses" and consec >= 2:
                    break
                if name == "stop_after_3_consec_losses" and consec >= 3:
                    break
                if name == "stop_at_day_-60" and e2 <= -60:
                    break
                if name == "stop_at_day_-100" and e2 <= -100:
                    break
                if name == "stop_at_day_+60(lock)" and e2 >= 60:
                    break
                if name == "stop_at_day_+100(lock)" and e2 >= 100:
                    break
                kept.append(r)
                e2 += r["net"]
                consec = consec + 1 if r["net"] < 0 else 0
            if rules[name] is not None:
                kept = rules[name](seq)
            eq += sum(r["net"] for r in kept)
            pk = max(pk, eq)
            dd = max(dd, pk - eq)
        out[name] = {
            "n": n, "net": round(total, 1), "days_pos": days_pos, "days": len(by_day),
            "worst_day": round(min(day_nets), 1) if day_nets else None,
            "max_dd": round(dd, 1),
        }
    return out


# ----------------------------------------------------------------- main

def main() -> None:
    cfg = load_config()
    pqdir = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "CFADE_ANATOMY"
    out.mkdir(parents=True, exist_ok=True)

    sessions = [s for s in _discover_sessions(pqdir) if s not in EXCLUDE]
    gate = make_daytype_skip(max_er=0.3)
    br = make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=4.0, cap=40.0)
    base_cfg = dict(slip_ticks=1.0, commission_pts_rt=COMMISSION,
                    signal_cooldown_sec=5.0, tp_pts=10, sl_pts=10)
    sim_cfg = MatSimConfig(**base_cfg)
    p_ex = ExhaustParams(
        es_imb_max=-0.4, es_min_vol=0, use_imb=True, use_vol=False,
        nq_min_imbalance=0.55, nq_min_vol=25, nq_min_trades=6,
    )
    burst_kw = dict(min_trades=6, min_vol=25.0, min_imbalance=0.55,
                    min_progress_ticks=1.0, tick_size=1.0, cooldown_bars=3, bar_ms=100)

    exit_variants = {
        "frozen": MatSimConfig(**base_cfg),
        "be_0.5R": MatSimConfig(**base_cfg, be_frac=0.5),
        "be_0.75R": MatSimConfig(**base_cfg, be_frac=0.75),
        "trail_0.5R": MatSimConfig(**base_cfg, trail_frac=0.5),
        "trail_0.75R": MatSimConfig(**base_cfg, trail_frac=0.75),
        "tstop_120s": MatSimConfig(**base_cfg, time_stop_sec=120),
        "tstop_300s": MatSimConfig(**base_cfg, time_stop_sec=300),
        "tstop_600s": MatSimConfig(**base_cfg, time_stop_sec=600),
        "be_0.5R+tstop_300s": MatSimConfig(**base_cfg, be_frac=0.5, time_stop_sec=300),
    }

    twin_rows: dict[str, list[dict]] = defaultdict(list)
    exit_rows: dict[str, list[dict]] = defaultdict(list)
    label_parts: list[pa.Table] = []
    day_meta: list[dict] = []
    candidates = {"A": 0, "B": 0, "C": 0, "D": 0}

    for k, sid in enumerate(sessions):
        pair = H.load_pair(pqdir, sid)
        if pair is None:
            continue
        nq_b, es_b = pair.bars(100)
        confirm = H.confirm_bursts(pair)

        # --- A frozen
        fades_a = exhaust_fade_events(confirm, nq_b, es_b, p_ex, max_wait_sec=5.0)
        # --- B NQ-only burst (no ES confirm) → ES against
        nq_only = momentum_burst_events(nq_b, **burst_kw)
        fades_b = exhaust_fade_events(nq_only, nq_b, es_b, p_ex, max_wait_sec=5.0)
        # --- C confirm → ES *with* (sign flip trick)
        fades_c = exhaust_fade_events(confirm, nq_b, _flip_es_imb(es_b), p_ex, max_wait_sec=5.0)
        # --- D fade every confirm burst at stamp
        if confirm.num_rows:
            fades_d = pa.table({
                "event_ts_ns": confirm.column("event_ts_ns"),
                "side": pa.array((-confirm.column("side").to_numpy().astype(int)).astype(np.int8)),
            })
        else:
            fades_d = pa.table({})
        # --- E random null, n matched to A per day
        fades_e = H.null_events(pair, max(1, fades_a.num_rows), seed=1000 + k) if fades_a.num_rows else pa.table({})

        for tag, ev in (("A", fades_a), ("B", fades_b), ("C", fades_c), ("D", fades_d)):
            candidates[tag] += int(ev.num_rows)

        for tag, ev in (("A", fades_a), ("B", fades_b), ("C", fades_c), ("D", fades_d), ("E", fades_e)):
            if ev.num_rows == 0:
                continue
            s = simulate_session(sid, pair.nq, pair.es, sim_cfg, gate=gate, bracket_fn=br,
                                 vwap_from_ns=pair.vwap_from_ns, events=ev)
            twin_rows[tag].extend(_trades_to_rows(s, ev, sid))

        # exit variants on frozen entries
        if fades_a.num_rows:
            for name, c in exit_variants.items():
                if name == "frozen":
                    continue
                s = simulate_session(sid, pair.nq, pair.es, c, gate=gate, bracket_fn=br,
                                     vwap_from_ns=pair.vwap_from_ns, events=fades_a)
                exit_rows[name].extend(_trades_to_rows(s, fades_a, sid))
            # event study on gated fades only (those that became trades)
            taken = {(r["entry_ts_ns"], r["side"]) for r in twin_rows["A"] if r["session_id"] == sid}
            if taken:
                ts_arr = fades_a.column("event_ts_ns").to_numpy().astype(np.int64)
                sd_arr = fades_a.column("side").to_numpy().astype(int)
                keep = np.array([(int(t), int(s_)) in taken for t, s_ in zip(ts_arr, sd_arr)])
                if keep.any():
                    lab = H.label_in_points(fades_a.take(np.where(keep)[0]), pair, HORIZONS_MS, TARGETS)
                    if lab.num_rows:
                        label_parts.append(lab)

        a_rows = [r for r in twin_rows["A"] if r["session_id"] == sid]
        day_meta.append({
            "session": sid, "is_": sid in DIG4_23D, "month": sid[2:4],
            "confirm_bursts": int(confirm.num_rows), "fades": int(fades_a.num_rows),
            "trades": len(a_rows), "net": round(sum(r["net"] for r in a_rows), 1),
            "range60_med": float(np.median([r["range60"] for r in a_rows])) if a_rows else float("nan"),
        })
        print(f"{sid} confirm={confirm.num_rows} A={fades_a.num_rows} B={fades_b.num_rows} "
              f"C={fades_c.num_rows} D={fades_d.num_rows} tradesA={len(a_rows)} "
              f"netA={sum(r['net'] for r in a_rows):+.1f}", flush=True)

    exit_rows["frozen"] = twin_rows["A"]
    A = twin_rows["A"]

    # ------------------------------------------------------------ report
    md = [
        "# CFADE anatomy — full clean sample",
        "",
        f"Sessions: **{len(sessions)}** (excl. mislabeled 200626/220626) · "
        f"in-sample (DIG4 23d) = {sum(1 for s in sessions if s in DIG4_23D)} · "
        f"OOS = {sum(1 for s in sessions if s not in DIG4_23D)}",
        "",
        "Frozen: against_m04 + skip_er0.3, vol_rr sl1/tp2 clip 4–40, 14:30–17:00 UK, "
        "primary feed, 1-tick slip, 0.25 pt/RT.",
        "",
        "## 1. Attribution twins (same gates / brackets / costs)",
        "",
        "| twin | construction | candidates | n | WR | net | avg | IS net | OOS net | days+ |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    twin_desc = {
        "A": "frozen: confirm burst → ES-against bar (NQ still pushing) → fade",
        "B": "NQ-only burst (no ES confirm) → ES-against bar → fade",
        "C": "confirm burst → ES **with** bar (side·es_imb ≥ +0.4), NQ still pushing → fade",
        "D": "confirm burst → fade at burst stamp (no exhaustion wait)",
        "E": "random time-matched null (n per day = A candidates)",
    }
    twin_stats = {}
    for tag in "ABCDE":
        rows = twin_rows[tag]
        s = _stat(rows)
        is_ = _stat([r for r in rows if r["is_"]])
        oos = _stat([r for r in rows if not r["is_"]])
        twin_stats[tag] = {"all": s, "is": is_, "oos": oos, "candidates": candidates.get(tag)}
        wr = f"{s['wr']:.2f}" if s["wr"] is not None else "–"
        avg = f"{s['avg']:+.2f}" if s["avg"] is not None else "–"
        md.append(
            f"| {tag} | {twin_desc[tag]} | {candidates.get(tag, '–')} | {s['n']} | {wr} | "
            f"{s['net']:+.1f} | {avg} | {is_['net']:+.1f} | {oos['net']:+.1f} | {s['days_pos']}/{s['days']} |"
        )
    md.append("")

    # ---- 2. event study
    md += ["## 2. Event study on frozen trades (NQ path from fade stamp, fade side)", ""]
    if label_parts:
        lab = pa.concat_tables(label_parts, promote_options="default")
        d = lab.to_pydict()
        mfe = np.array(d["mfe_pts"]); mae = np.array(d["mae_pts"])
        md += [
            f"n={lab.num_rows} · mean MFE={mfe.mean():.1f} · mean MAE={mae.mean():.1f} · "
            f"median MFE={np.median(mfe):.1f} · median MAE={np.median(mae):.1f}",
            "",
            "| horizon | mean ret | median ret | % > 0 |",
            "|---|---:|---:|---:|",
        ]
        for h in HORIZONS_MS:
            col = f"ret_pts_{h}ms"
            if col in d:
                a = np.array(d[col], dtype=float)
                a = a[np.isfinite(a)]
                md.append(f"| {h/1000:.0f}s | {a.mean():+.2f} | {np.median(a):+.2f} | {100*(a>0).mean():.0f}% |")
        md += ["", "| target | hit |", "|---|---:|"]
        for t in TARGETS:
            col = f"hit_{int(t)}pts"
            if col in d:
                md.append(f"| +{t:g} pts | {np.mean(d[col]):.2f} |")
        md.append("")

    # exit reason / hold time on frozen
    reasons = defaultdict(lambda: {"n": 0, "net": 0.0})
    for r in A:
        reasons[r["reason"]]["n"] += 1
        reasons[r["reason"]]["net"] += r["net"]
    md += ["**Exit reasons (frozen):** " + ", ".join(
        f"{k}: n={v['n']} net={v['net']:+.1f}" for k, v in sorted(reasons.items())), ""]
    holds = np.array([r["hold_s"] for r in A if np.isfinite(r["hold_s"])])
    if len(holds):
        md += [f"Hold time: median {np.median(holds):.0f}s · p25 {np.percentile(holds,25):.0f}s · "
               f"p75 {np.percentile(holds,75):.0f}s · wins median "
               f"{np.median([r['hold_s'] for r in A if r['win'] and np.isfinite(r['hold_s'])]):.0f}s · "
               f"losses median {np.median([r['hold_s'] for r in A if not r['win'] and np.isfinite(r['hold_s'])]):.0f}s", ""]
    md += _strata_block("Hold-time buckets", A, None, [
        ("≤30s", lambda r: r["hold_s"] <= 30),
        ("30–120s", lambda r: 30 < r["hold_s"] <= 120),
        ("120–300s", lambda r: 120 < r["hold_s"] <= 300),
        ("300–900s", lambda r: 300 < r["hold_s"] <= 900),
        (">900s", lambda r: r["hold_s"] > 900),
    ])

    # ---- 3. exit variants
    md += ["## 3. Exit management on frozen entries", "",
           "| variant | n | WR | net | avg | IS net | OOS net | days+ | maxDD(path) |",
           "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    exit_stats = {}
    for name in exit_variants:
        rows = exit_rows[name]
        s = _stat(rows); is_ = _stat([r for r in rows if r["is_"]]); oos = _stat([r for r in rows if not r["is_"]])
        # path DD
        eq = pk = dd = 0.0
        for r in sorted(rows, key=lambda r: r["entry_ts_ns"]):
            eq += r["net"]; pk = max(pk, eq); dd = max(dd, pk - eq)
        exit_stats[name] = {"all": s, "is": is_, "oos": oos, "max_dd": round(dd, 1)}
        wr = f"{s['wr']:.2f}" if s["wr"] is not None else "–"
        avg = f"{s['avg']:+.2f}" if s["avg"] is not None else "–"
        md.append(f"| {name} | {s['n']} | {wr} | {s['net']:+.1f} | {avg} | {is_['net']:+.1f} | "
                  f"{oos['net']:+.1f} | {s['days_pos']}/{s['days']} | {dd:.0f} |")
    md.append("")

    # ---- 4. strata IS vs OOS
    md += ["## 4. DIG3 strata re-read — in-sample (23d) vs out-of-sample", "",
           "Strata were *chosen* on the 23d; only the OOS columns are evidence.", ""]
    es_vol_is = np.array([r["burst_es_vol"] for r in A if r["is_"] and np.isfinite(r["burst_es_vol"])])
    ev_lo, ev_hi = (np.percentile(es_vol_is, [33.3, 66.7]) if len(es_vol_is) else (np.nan, np.nan))
    nq_imb_is = np.array([r["nq_imb_x"] for r in A if r["is_"] and np.isfinite(r["nq_imb_x"])])
    ni_lo, ni_hi = (np.percentile(nq_imb_is, [33.3, 66.7]) if len(nq_imb_is) else (np.nan, np.nan))

    md += _strata_block("Side", A, None, [
        ("fade LONG (burst was down)", lambda r: r["side"] > 0),
        ("fade SHORT (burst was up)", lambda r: r["side"] < 0),
    ])
    md += _strata_block("Wait from confirm to ES-against bar", A, None, [
        ("<500 ms", lambda r: r["wait_ms"] < 500),
        ("500–2000 ms", lambda r: 500 <= r["wait_ms"] < 2000),
        ("≥2000 ms", lambda r: r["wait_ms"] >= 2000),
    ])
    md += _strata_block("ES against strength (side·es_imb_x)", A, None, [
        ("−0.4…−0.6", lambda r: -0.6 < -abs(r["es_imb_x"]) <= -0.4),
        ("≤ −0.6", lambda r: -abs(r["es_imb_x"]) <= -0.6),
        ("≤ −0.8", lambda r: -abs(r["es_imb_x"]) <= -0.8),
    ])
    md += _strata_block(f"Burst ES volume (IS terciles {ev_lo:.0f}/{ev_hi:.0f})", A, None, [
        ("lo", lambda r: r["burst_es_vol"] < ev_lo),
        ("mid", lambda r: ev_lo <= r["burst_es_vol"] < ev_hi),
        ("hi", lambda r: r["burst_es_vol"] >= ev_hi),
    ])
    md += _strata_block(f"NQ still-push imbalance at X bar (IS terciles {ni_lo:.2f}/{ni_hi:.2f})", A, None, [
        ("lo", lambda r: abs(r["nq_imb_x"]) < ni_lo),
        ("mid", lambda r: ni_lo <= abs(r["nq_imb_x"]) < ni_hi),
        ("hi", lambda r: abs(r["nq_imb_x"]) >= ni_hi),
    ])
    md += _strata_block("ES-first vs NQ-first on the confirm burst", A, None, [
        ("ES first (signed lag < 0)", lambda r: r["es_signed_lag_ms"] < 0),
        ("NQ first (≥ 0)", lambda r: r["es_signed_lag_ms"] >= 0),
    ])
    md += _strata_block("Time of day (UK)", A, None, [
        ("14:30–14:50", lambda r: 870 <= r["tod"] < 890),
        ("14:50–15:30", lambda r: 890 <= r["tod"] < 930),
        ("15:30–16:00", lambda r: 930 <= r["tod"] < 960),
        ("16:00–16:30", lambda r: 960 <= r["tod"] < 990),
        ("16:30–17:00", lambda r: 990 <= r["tod"] < 1020),
    ])
    md += _strata_block("ER60 within the gate", A, None, [
        ("≤0.10", lambda r: r["er60"] <= 0.10),
        ("0.10–0.20", lambda r: 0.10 < r["er60"] <= 0.20),
        ("0.20–0.30", lambda r: 0.20 < r["er60"] <= 0.30),
    ])
    md += _strata_block("Bracket unit (SL pts)", A, None, [
        ("SL < 10", lambda r: r["sl"] < 10),
        ("10–20", lambda r: 10 <= r["sl"] < 20),
        ("20–30", lambda r: 20 <= r["sl"] < 30),
        ("≥30", lambda r: r["sl"] >= 30),
    ])
    md += _strata_block("30-min trend vs fade direction", A, None, [
        ("fade WITH 30m trend", lambda r: np.sign(r["net30"]) == r["side"] and r["net30"] != 0),
        ("fade AGAINST 30m trend", lambda r: np.sign(r["net30"]) == -r["side"] and r["net30"] != 0),
    ])
    md += _strata_block("VWAP side", A, None, [
        ("fade toward VWAP", lambda r: np.isfinite(r["vwap"]) and np.sign(r["vwap"] - r["entry_px"]) == r["side"]),
        ("fade away from VWAP", lambda r: np.isfinite(r["vwap"]) and np.sign(r["vwap"] - r["entry_px"]) == -r["side"]),
    ])
    md += _strata_block("Month", A, None, [
        (m, (lambda r, m=m: r["month"] == m)) for m in ["03", "04", "05", "06", "07", "08", "09", "10"]
    ])

    # ---- 5. day-level
    md += ["## 5. Day-level", ""]
    dm = [d for d in day_meta if d["trades"] > 0 or d["fades"] > 0]
    fades_arr = np.array([d["fades"] for d in day_meta]); trades_arr = np.array([d["trades"] for d in day_meta])
    nets_arr = np.array([d["net"] for d in day_meta])
    md += [f"Fades/day: mean {fades_arr.mean():.1f} median {np.median(fades_arr):.0f} max {fades_arr.max()} · "
           f"trades/day: mean {trades_arr.mean():.1f} median {np.median(trades_arr):.0f} max {trades_arr.max()} · "
           f"days with 0 trades: {(trades_arr==0).sum()}", ""]
    md += ["| trades in day | days | net | avg/day | days+ |", "|---|---:|---:|---:|---:|"]
    for lab, pred in [("0", lambda t: t == 0), ("1–2", lambda t: 1 <= t <= 2), ("3–4", lambda t: 3 <= t <= 4),
                      ("5–7", lambda t: 5 <= t <= 7), ("≥8", lambda t: t >= 8)]:
        sel = [d for d in day_meta if pred(d["trades"])]
        if sel:
            nn = np.array([d["net"] for d in sel])
            md.append(f"| {lab} | {len(sel)} | {nn.sum():+.1f} | {nn.mean():+.1f} | {(nn>0).sum()}/{len(sel)} |")
    md.append("")
    md += ["### Day-kill / cap rules (sequential on frozen trade list)", "",
           "| rule | n | net | days+ | worst day | path maxDD |", "|---|---:|---:|---:|---:|---:|"]
    dk = _seq_day_rules(A)
    for name, s in dk.items():
        md.append(f"| {name} | {s['n']} | {s['net']:+.1f} | {s['days_pos']}/{s['days']} | {s['worst_day']:+.1f} | {s['max_dd']:.0f} |")
    md.append("")
    # first trade of day vs later
    order_rows = []
    for sid in {r["session_id"] for r in A}:
        seq = sorted([r for r in A if r["session_id"] == sid], key=lambda r: r["entry_ts_ns"])
        for i, r in enumerate(seq):
            order_rows.append({**r, "k": i + 1})
    md += _strata_block("Trade order within day", order_rows, None, [
        ("1st", lambda r: r["k"] == 1), ("2nd", lambda r: r["k"] == 2),
        ("3rd–4th", lambda r: 3 <= r["k"] <= 4), ("5th+", lambda r: r["k"] >= 5),
    ])
    # previous trade outcome
    prev_rows = []
    for sid in {r["session_id"] for r in A}:
        seq = sorted([r for r in A if r["session_id"] == sid], key=lambda r: r["entry_ts_ns"])
        for i, r in enumerate(seq):
            if i == 0:
                prev_rows.append({**r, "prev": "none"})
            else:
                prev_rows.append({**r, "prev": "win" if seq[i-1]["win"] else "loss"})
    md += _strata_block("Previous trade in day", prev_rows, None, [
        ("first of day", lambda r: r["prev"] == "none"),
        ("after a win", lambda r: r["prev"] == "win"),
        ("after a loss", lambda r: r["prev"] == "loss"),
    ])

    # persist
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")
    report = {"twins": twin_stats, "exits": exit_stats, "day_rules": dk, "day_meta": day_meta,
              "sessions": sessions, "is_sessions": sorted(DIG4_23D & set(sessions))}
    (out / "report.json").write_text(json.dumps(report, indent=2, default=float), encoding="utf-8")
    if A:
        cols = {k: [r[k] for r in A] for k in A[0] if k not in ("is_",)}
        cols["is_sample"] = [r["is_"] for r in A]
        pq.write_table(pa.table(cols), out / "trades.parquet")
    print(f"wrote {out / 'summary.md'}", flush=True)
    for tag in "ABCDE":
        s = twin_stats[tag]
        print(f"{tag}: n={s['all']['n']} net={s['all']['net']} IS={s['is']['net']} OOS={s['oos']['net']}")


if __name__ == "__main__":
    main()
