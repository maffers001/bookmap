"""EXP-0077 / H68 — CFADE ↔ EXP-0061 flat hand-off vs independent sum.

Reuses flat-merge + session book builders from study_combined_book.
Kill (flat_handoff): OOS net > 0; beat independent CFADE+0061 sum on OOS avg;
n ≥ 50. MAT(c) is reporting-only.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import numpy as np

from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.correlated.study_combined_book import (
    HANDOFF_NS,
    TaggedTrade,
    _day_nets,
    _flat_only_merge,
    _net,
    _path_stats,
    _session_books,
    _trade_stats,
)
from research.features.corr_exhaust import ExhaustParams, build_exhaust_exit_fn
from research.run_cfade_all_months import _sid_chrono_key


def _split_day_nets(day: dict[str, float]) -> tuple[dict[str, float], dict[str, float]]:
    is_d, oos_d = {}, {}
    for sid, v in day.items():
        (is_d if F.is_is(sid) else oos_d)[sid] = v
    return is_d, oos_d


def _pack_from_day(day: dict[str, float], tagged: list[TaggedTrade] | None = None) -> dict:
    is_d, oos_d = _split_day_nets(day)
    ps = _path_stats(day)
    ps_is = _path_stats(is_d) if is_d else {"net": 0.0, "max_dd": 0.0, "days": 0, "days_pos": 0}
    ps_oos = _path_stats(oos_d) if oos_d else {"net": 0.0, "max_dd": 0.0, "days": 0, "days_pos": 0}
    ts = _trade_stats(tagged) if tagged is not None else {"n": 0, "avg": None, "wr": None, "net": 0.0}
    # OOS trade avg from tagged if available
    oos_tagged = [t for t in (tagged or []) if not F.is_is(t.trade.session_id)]
    is_tagged = [t for t in (tagged or []) if F.is_is(t.trade.session_id)]
    oos_ts = _trade_stats(oos_tagged)
    is_ts = _trade_stats(is_tagged)
    nets = sorted(day.values(), reverse=True)
    top3 = sum(nets[:3]) if nets else 0.0
    return {
        "all": {
            "n": ts.get("n", 0),
            "net": ps["net"],
            "avg": ts.get("avg"),
            "wr": ts.get("wr"),
            "max_dd": ps["max_dd"],
            "days_pos": ps["days_pos"],
            "days": ps["days"],
            "top3_share": round(top3 / ps["net"], 3) if abs(ps["net"]) > 1e-9 else None,
        },
        "is": {
            "n": is_ts.get("n", 0),
            "net": ps_is["net"],
            "avg": is_ts.get("avg"),
            "days_pos": ps_is.get("days_pos", 0),
            "days": ps_is.get("days", 0),
        },
        "oos": {
            "n": oos_ts.get("n", 0),
            "net": ps_oos["net"],
            "avg": oos_ts.get("avg"),
            "days_pos": ps_oos.get("days_pos", 0),
            "days": ps_oos.get("days", 0),
            "max_dd": ps_oos.get("max_dd", 0.0),
        },
    }


def _null_cont_book(sessions: list[str], pq: Path, seed0: int = 7700) -> list[TaggedTrade]:
    """Time-matched random null on continuation stamp count per session."""
    out: list[TaggedTrade] = []
    for i, sid in enumerate(sessions):
        pair = H.load_pair(pq, sid)
        if pair is None:
            continue
        _, _, cont = _session_books(pair)
        n = max(len(cont), 1)
        null_ev = H.null_events(pair, n, seed=seed0 + i)
        # Simulate null as continuation-style with exhaust exit
        nq_b, es_b = pair.bars(100)
        p_ex = ExhaustParams(
            es_imb_max=-0.4, es_min_vol=0, use_imb=True, use_vol=False,
            nq_min_imbalance=0.55, nq_min_vol=25, nq_min_trades=6,
            max_wait_sec=5.0,
        )
        exit_fn = build_exhaust_exit_fn(nq_b, es_b, p_ex)
        s = F.simulate_cfade(pair, events=null_ev, exit_fn=exit_fn)
        out.extend(TaggedTrade(t, "null", _net(t)) for t in s.trades)
    return out


def _loo_day_nets(day: dict[str, float]) -> dict:
    """Leave-one-day-out: fraction of days whose removal flips overall sign."""
    if not day:
        return {"n_days": 0, "loo_neg": None}
    total = sum(day.values())
    loo_neg = 0
    for sid, v in day.items():
        if total - v < 0:
            loo_neg += 1
    return {"n_days": len(day), "loo_neg": loo_neg, "loo_neg_frac": round(loo_neg / len(day), 3)}


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0077"
    out.mkdir(parents=True, exist_ok=True)
    sessions = F.clean_sessions(pq)

    all_mat: list[TaggedTrade] = []
    all_cfade: list[TaggedTrade] = []
    all_cont: list[TaggedTrade] = []
    all_flat: list[TaggedTrade] = []  # CFADE + 0061 only (kill book)
    all_flat_with_mat: list[TaggedTrade] = []  # reporting
    skip_tot = {"overlap": 0, "cooldown": 0, "handoff_allowed": 0}

    for sid in sessions:
        pair = H.load_pair(pq, sid)
        if pair is None:
            continue
        mat_c, cfade, cont = _session_books(pair)
        all_mat.extend(mat_c)
        all_cfade.extend(cfade)
        all_cont.extend(cont)

        merged, skipped = _flat_only_merge(cfade + cont)
        all_flat.extend(merged)
        merged_m, _ = _flat_only_merge(mat_c + cfade + cont)
        all_flat_with_mat.extend(merged_m)
        for k in skip_tot:
            skip_tot[k] += skipped.get(k, 0)
        print(
            f"{sid} cfade={len(cfade)} cont={len(cont)} flat={len(merged)} "
            f"handoff={skipped.get('handoff_allowed', 0)}",
            flush=True,
        )

    d_cfade = _day_nets(all_cfade)
    d_cont = _day_nets(all_cont)
    d_mat = _day_nets(all_mat)
    all_sids = sorted(
        set(d_cfade) | set(d_cont) | set(d_mat) | {t.trade.session_id for t in all_flat},
        key=_sid_chrono_key,
    )
    d_indep = {s: d_cfade.get(s, 0.0) + d_cont.get(s, 0.0) for s in all_sids}
    d_flat = _day_nets(all_flat)
    for s in all_sids:
        d_flat.setdefault(s, 0.0)
        d_indep.setdefault(s, 0.0)

    null_tagged = _null_cont_book(sessions, pq)
    d_null = _day_nets(null_tagged)

    books = {
        "cfade_alone": _pack_from_day(d_cfade, all_cfade),
        "cont0061_alone": _pack_from_day(d_cont, all_cont),
        "independent_sum": _pack_from_day(d_indep, all_cfade + all_cont),
        "flat_handoff": _pack_from_day(d_flat, all_flat),
        "null_cont": _pack_from_day(d_null, null_tagged),
        "mat_c_report_only": _pack_from_day(d_mat, all_mat),
        "flat_with_mat_c_report": _pack_from_day(_day_nets(all_flat_with_mat), all_flat_with_mat),
    }

    # Kill: flat_handoff must beat independent sum on OOS avg
    fh, ind = books["flat_handoff"], books["independent_sum"]
    kill = {
        "oos_net_pos": fh["oos"]["net"] > 0,
        "beat_independent_sum_oos_avg": (
            fh["oos"]["avg"] is not None
            and ind["oos"]["avg"] is not None
            and fh["oos"]["avg"] > ind["oos"]["avg"]
        ),
        "n_ge_min": fh["all"]["n"] >= 50,
        "beat_null_oos_avg": (
            fh["oos"]["avg"] is not None
            and books["null_cont"]["oos"]["avg"] is not None
            and fh["oos"]["avg"] > books["null_cont"]["oos"]["avg"]
        ),
    }
    kill["pass"] = bool(
        kill["oos_net_pos"]
        and kill["beat_independent_sum_oos_avg"]
        and kill["n_ge_min"]
    )
    kill["loo"] = _loo_day_nets(d_flat)
    kill["handoff_count"] = skip_tot["handoff_allowed"]
    kill["HANDOFF_NS_ms"] = HANDOFF_NS / 1e6

    md = [
        "# EXP-0077 — CFADE ↔ EXP-0061 flat hand-off (H68)",
        "",
        "Kill book = CFADE + 0061 only (MAT(c) reporting-only).",
        f"Hand-offs allowed: {skip_tot['handoff_allowed']}.",
        "",
        "| book | n | WR | net | avg | IS net | OOS net | OOS avg | kill |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name in (
        "cfade_alone", "cont0061_alone", "independent_sum",
        "flat_handoff", "null_cont", "mat_c_report_only",
    ):
        v = books[name]
        a, o = v["all"], v["oos"]
        mark = ""
        if name == "flat_handoff":
            mark = "PASS" if kill["pass"] else "FAIL"
        elif name in ("independent_sum", "cfade_alone", "cont0061_alone", "null_cont"):
            mark = "—"
        md.append(
            f"| {name} | {a['n']} | {a.get('wr')} | {a['net']:+.1f} | {a.get('avg')} | "
            f"{v['is']['net']:+.1f} | {o['net']:+.1f} | {o.get('avg')} | {mark or '—'} |"
        )
    md += [
        "",
        "## Kill detail",
        "",
        "```json",
        json.dumps(kill, indent=2),
        "```",
        "",
        "## Ship gate",
        "",
        "No live defaults. Re-score on sessions after 221026 / newly converted",
        "months before any Java port.",
        "",
    ]
    (out / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    report = {"books": books, "kills": {"flat_handoff": kill}, "skip": skip_tot}
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out / 'summary.md'} pass={kill['pass']}", flush=True)


if __name__ == "__main__":
    main()
