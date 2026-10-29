"""Joint flat book: CFADE + MAT rot_fresh_and_vwap_near + ES LVN outer.

One position at a time across both products (NQ and ES). Equity in USD
(NQ $20/pt, ES $50/pt) so legs are comparable.

Writes results/JOINT_CFADE_MAT075_ESLVN/.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from research.config import ROOT, load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.correlated.study_combined_book import _plot_equity
from research.mat_context.cache import build_cache
from research.mat_context.run_queue import _resim_masked
from research.run_cfade_all_months import _sid_chrono_key

NQ_USD = 20.0
ES_USD = 50.0
ES_LVN_CSV = ROOT / "results" / "ES_LVN_OUTER_824_20261025" / "trades_ALL.csv"
MAT_ARM = "rot_fresh_and_vwap_near"


@dataclass
class Cand:
    kind: str  # cfade | mat075 | es_lvn
    market: str  # NQ | ES
    session_id: str
    entry_ts_ns: int
    exit_ts_ns: int
    pnl_pts: float  # market points, commission already in
    pnl_usd: float


def _parse_iso_ns(s: str) -> int:
    # 2026-04-01T14:39:05.195+01:00
    dt = datetime.fromisoformat(s)
    return int(dt.timestamp() * 1e9)


def _load_es_lvn(path: Path) -> list[Cand]:
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    out: list[Cand] = []
    for r in rows:
        pts = float(r["pnl_net"])
        out.append(Cand(
            kind="es_lvn",
            market="ES",
            session_id=r["session_id"],
            entry_ts_ns=_parse_iso_ns(r["entry_ts"]),
            exit_ts_ns=_parse_iso_ns(r["exit_ts"]),
            pnl_pts=pts,
            pnl_usd=pts * ES_USD,
        ))
    return out


def _from_trades(trades, *, kind: str, market: str = "NQ") -> list[Cand]:
    mult = NQ_USD if market == "NQ" else ES_USD
    out: list[Cand] = []
    for t in trades:
        pts = float(t.pnl) - F.COMMISSION
        exit_ns = int(t.exit_ts_ns) if t.exit_ts_ns else int(t.entry_ts_ns)
        out.append(Cand(
            kind=kind,
            market=market,
            session_id=t.session_id,
            entry_ts_ns=int(t.entry_ts_ns),
            exit_ts_ns=exit_ns,
            pnl_pts=pts,
            pnl_usd=pts * mult,
        ))
    return out


def _flat_merge(cands: list[Cand]) -> tuple[list[Cand], dict]:
    """Global one-position-at-a-time across products. No cooldown gap."""
    ordered = sorted(cands, key=lambda c: (c.entry_ts_ns, 0 if c.kind == "cfade" else 1))
    taken: list[Cand] = []
    last_exit = -10**18
    skip = {"overlap": 0, "by_kind_skipped": defaultdict(int)}
    for c in ordered:
        if c.entry_ts_ns < last_exit:
            skip["overlap"] += 1
            skip["by_kind_skipped"][c.kind] += 1
            continue
        taken.append(c)
        last_exit = max(c.exit_ts_ns, c.entry_ts_ns)
    skip["by_kind_skipped"] = dict(skip["by_kind_skipped"])
    return taken, skip


def _day_path_usd(cands: list[Cand]) -> list[dict]:
    by_day: dict[str, float] = defaultdict(float)
    day_sid: dict[str, str] = {}
    for c in cands:
        # UK/session calendar from session_id DDMMYY
        sid = c.session_id
        if len(sid) == 6 and sid.isdigit():
            d = f"20{sid[4:6]}-{sid[2:4]}-{sid[0:2]}"
        else:
            d = sid
        by_day[d] += c.pnl_usd
        day_sid[d] = sid
    pe = 0.0
    rows = []
    for d in sorted(by_day):
        pe += by_day[d]
        rows.append({
            "session": day_sid[d],
            "date": d,
            "day_net": round(by_day[d], 2),
            "equity": round(pe, 2),
        })
    return rows


def _stats(cands: list[Cand]) -> dict:
    if not cands:
        return {"n": 0, "wr": None, "net_usd": 0.0, "avg_usd": None, "max_dd": 0.0,
                "by_kind": {}}
    wins = sum(1 for c in cands if c.pnl_usd > 0)
    net = sum(c.pnl_usd for c in cands)
    by: dict[str, list[Cand]] = defaultdict(list)
    for c in cands:
        by[c.kind].append(c)
    kind_stats = {}
    for k, xs in by.items():
        kn = sum(x.pnl_usd for x in xs)
        kw = sum(1 for x in xs if x.pnl_usd > 0)
        kind_stats[k] = {
            "n": len(xs),
            "wr": round(kw / len(xs), 3),
            "net_usd": round(kn, 2),
            "avg_usd": round(kn / len(xs), 2),
        }
    # maxDD on chronological trade path
    pe = peak = dd = 0.0
    for c in sorted(cands, key=lambda x: x.entry_ts_ns):
        pe += c.pnl_usd
        peak = max(peak, pe)
        dd = max(dd, peak - pe)
    return {
        "n": len(cands),
        "wr": round(wins / len(cands), 3),
        "net_usd": round(net, 2),
        "avg_usd": round(net / len(cands), 2),
        "max_dd": round(dd, 2),
        "by_kind": kind_stats,
    }


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "JOINT_CFADE_MAT075_ESLVN"
    out.mkdir(parents=True, exist_ok=True)

    print("Loading ES LVN…", flush=True)
    es_lvn = _load_es_lvn(ES_LVN_CSV)
    print(f"  ES LVN n={len(es_lvn)}", flush=True)

    print("Building MAT cache + CFADE…", flush=True)
    cache = build_cache(verbose=False)
    print(f"  cache sessions={len(cache.sessions)}", flush=True)

    print(f"Re-sim MAT arm {MAT_ARM}…", flush=True)
    mat_sims = _resim_masked(cache, MAT_ARM)
    mat_trades = [t for s in mat_sims.values() for t in s.trades]
    mat_cands = _from_trades(mat_trades, kind="mat075")
    print(f"  MAT {MAT_ARM} n={len(mat_cands)}", flush=True)

    cfade_trades = []
    for sid in cache.sessions:
        pair = H.load_pair(pq, sid)
        if pair is None:
            continue
        s = F.simulate_cfade(pair)
        cfade_trades.extend(s.trades)
    cfade_cands = _from_trades(cfade_trades, kind="cfade")
    print(f"  CFADE n={len(cfade_cands)}", flush=True)

    all_cands = cfade_cands + mat_cands + es_lvn
    flat, skip = _flat_merge(all_cands)
    print(
        f"Flat book n={len(flat)} (from {len(all_cands)}); "
        f"skipped overlap={skip['overlap']} {skip['by_kind_skipped']}",
        flush=True,
    )

    books = {
        "cfade_alone": cfade_cands,
        "mat075_alone": mat_cands,
        "es_lvn_alone": es_lvn,
        "independent_sum": all_cands,  # overlapping OK — day-sum path only
        "flat_joint": flat,
    }

    # Independent sum day path (sum USD by calendar day)
    indep_by_day: dict[str, float] = defaultdict(float)
    for c in all_cands:
        sid = c.session_id
        d = f"20{sid[4:6]}-{sid[2:4]}-{sid[0:2]}" if len(sid) == 6 else sid
        indep_by_day[d] += c.pnl_usd
    indep_path = []
    pe = 0.0
    for d in sorted(indep_by_day):
        pe += indep_by_day[d]
        indep_path.append({"session": d, "day_net": round(indep_by_day[d], 2), "equity": round(pe, 2)})

    flat_path = _day_path_usd(flat)
    _plot_equity(
        flat_path,
        out / "equity_flat_joint.svg",
        "Flat joint — CFADE + MAT rot∩vwap_near + ES LVN (USD)",
    )
    _plot_equity(
        indep_path,
        out / "equity_independent.svg",
        "Independent sum — CFADE + MAT075 + ES LVN (USD, overlap OK)",
    )
    for name, cands in (
        ("cfade", cfade_cands),
        ("mat075", mat_cands),
        ("es_lvn", es_lvn),
    ):
        _plot_equity(
            _day_path_usd(cands),
            out / f"equity_{name}_alone.svg",
            f"{name} alone (USD)",
        )

    from research.mat_context.plot_0075_equity import _overlay

    def _date_key(r: dict) -> str:
        if "date" in r and r["date"]:
            return str(r["date"])
        sid = r["session"]
        if len(sid) == 6 and sid.isdigit():
            return f"20{sid[4:6]}-{sid[2:4]}-{sid[0:2]}"
        return sid

    dates = sorted({_date_key(r) for r in flat_path} | {_date_key(r) for r in indep_path})

    def align(path: list[dict]) -> list[dict]:
        by = {_date_key(r): float(r["day_net"]) for r in path}
        pe = 0.0
        rows = []
        for d in dates:
            pe += by.get(d, 0.0)
            rows.append({"session": d, "day_net": by.get(d, 0.0), "equity": pe})
        return rows

    _overlay(
        {"flat_joint": align(flat_path), "independent": align(indep_path)},
        out / "equity_overlay.svg",
        "Joint book: flat vs independent (USD)",
    )

    # daily csv
    with (out / "daily.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "session", "flat_net", "flat_equity", "kind_counts"])
        w.writeheader()
        # per-day kind counts in flat
        by_day_cands: dict[str, list[Cand]] = defaultdict(list)
        for c in flat:
            sid = c.session_id
            d = f"20{sid[4:6]}-{sid[2:4]}-{sid[0:2]}" if len(sid) == 6 else sid
            by_day_cands[d].append(c)
        pe = 0.0
        for r in flat_path:
            d = r.get("date") or (
                f"20{r['session'][4:6]}-{r['session'][2:4]}-{r['session'][0:2]}"
                if len(r["session"]) == 6 else r["session"]
            )
            pe = r["equity"]
            kinds = defaultdict(int)
            for c in by_day_cands.get(d, []):
                kinds[c.kind] += 1
            w.writerow({
                "date": d,
                "session": r["session"],
                "flat_net": r["day_net"],
                "flat_equity": r["equity"],
                "kind_counts": dict(kinds),
            })

    stats = {k: _stats(v) for k, v in books.items()}
    # independent_sum trade stats are OK; path maxDD from indep day path
    pe = peak = dd = 0.0
    for r in indep_path:
        pe = r["equity"]
        peak = max(peak, pe)
        dd = max(dd, peak - pe)
    stats["independent_sum"]["max_dd"] = round(dd, 2)
    stats["independent_sum"]["net_usd"] = round(indep_path[-1]["equity"], 2) if indep_path else 0.0

    md = [
        "# Joint book — CFADE + MAT `rot_fresh_and_vwap_near` + ES LVN",
        "",
        "Rules:",
        "- **CFADE:** frozen against_m04 + ER≤0.30, vol RR 1:2 (NQ)",
        "- **MAT075:** frozen MAT trend+VWAP vol k=1, keep-gate "
        "`rot_fresh_and_vwap_near` (EXP-0075)",
        "- **ES LVN:** outer open-cluster ∩ LVN, SL8/TP32, minOpen≥3 "
        f"(`{ES_LVN_CSV.as_posix()}`)",
        "- **flat_joint:** one position at a time across **both** NQ and ES "
        "(no overlap; no cross-product cooldown).",
        "",
        "Equity in **USD** (NQ $20/pt, ES $50/pt). Bookmap-parity fills; commission already in pts.",
        "",
        f"Overlap skips: {skip['overlap']} {skip['by_kind_skipped']}",
        "",
        "## Books",
        "",
        "| book | n | WR | net USD | avg USD | maxDD |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in ("cfade_alone", "mat075_alone", "es_lvn_alone", "independent_sum", "flat_joint"):
        s = stats[name]
        md.append(
            f"| {name} | {s['n']} | {s['wr']} | {s['net_usd']:+.1f} | "
            f"{s['avg_usd']} | {s['max_dd']:.1f} |"
        )
    md += [
        "",
        "### flat_joint by kind",
        "",
        "| kind | n | WR | net USD | avg USD |",
        "|---|---:|---:|---:|---:|",
    ]
    for k, s in sorted(stats["flat_joint"]["by_kind"].items()):
        md.append(
            f"| {k} | {s['n']} | {s['wr']} | {s['net_usd']:+.1f} | {s['avg_usd']:+.2f} |"
        )
    md += [
        "",
        "## Charts",
        "",
        "![flat joint](equity_flat_joint.svg)",
        "",
        "![overlay](equity_overlay.svg)",
        "",
        "![independent](equity_independent.svg)",
        "",
    ]
    (out / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"wrote {out / 'summary.md'}", flush=True)
    print(f"flat net=${stats['flat_joint']['net_usd']:+.1f} "
          f"indep net=${stats['independent_sum']['net_usd']:+.1f}", flush=True)


if __name__ == "__main__":
    main()
