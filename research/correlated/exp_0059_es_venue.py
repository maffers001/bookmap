"""EXP-0059 / H50 — Execute frozen CFADE triggers on the ES leg."""
from __future__ import annotations

from pathlib import Path

from research.backtest.mat_sim import make_vol_bracket_rr, merge_summaries
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H


def main() -> None:
    cfg = load_config()
    pq = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0059"
    sessions = F.clean_sessions(pq)

    # ES-scaled clip (pre-registered): same RR, tighter absolute cap
    es_br = make_vol_bracket_rr(1.0, tp_r=2.0, sl_r=1.0, floor=2.0, cap=15.0)
    nq_br = F.frozen_bracket()
    gate = F.frozen_gate()

    parts = {
        "nq_1tick": [], "nq_2tick": [],
        "es_1tick": [], "es_2tick": [],
        "es_null": [],
    }
    is_parts = {k: [] for k in parts}
    oos_parts = {k: [] for k in parts}

    for i, sid in enumerate(sessions):
        pair = H.load_pair(pq, sid)
        if pair is None:
            continue
        fades = F.cfade_events(pair)
        if fades.num_rows == 0:
            continue
        null = H.null_events(pair, max(1, fades.num_rows), seed=5900 + i)

        sn1 = F.simulate_cfade(pair, events=fades, gate=gate, bracket_fn=nq_br, slip_ticks=1.0, primary="nq")
        sn2 = F.simulate_cfade(pair, events=fades, gate=gate, bracket_fn=nq_br, slip_ticks=2.0, primary="nq")
        se1 = F.simulate_cfade(pair, events=fades, gate=gate, bracket_fn=es_br, slip_ticks=1.0, primary="es")
        se2 = F.simulate_cfade(pair, events=fades, gate=gate, bracket_fn=es_br, slip_ticks=2.0, primary="es")
        sen = F.simulate_cfade(pair, events=null, gate=gate, bracket_fn=es_br, slip_ticks=1.0, primary="es")

        for key, s in (("nq_1tick", sn1), ("nq_2tick", sn2), ("es_1tick", se1),
                       ("es_2tick", se2), ("es_null", sen)):
            parts[key].append(s)
            (is_parts[key] if F.is_is(sid) else oos_parts[key]).append(s)

        print(
            f"{sid} nq={sn1.net:+.1f} es={se1.net:+.1f} es2={se2.net:+.1f}",
            flush=True,
        )

    def pack(key: str) -> dict:
        def one(ps):
            if not ps:
                return {"n": 0, "net": 0.0, "avg": None, "wr": None}
            # ES uses different commission — merge_summaries uses passed commission
            comm = F.ES_COMMISSION if key.startswith("es") else F.COMMISSION
            s = merge_summaries(ps, comm)
            d = s.to_dict()
            d["avg"] = round(s.net / s.n, 2) if s.n else None
            return d
        return {"all": one(parts[key]), "is": one(is_parts[key]), "oos": one(oos_parts[key])}

    books = {k: pack(k) for k in parts}
    es = books["es_1tick"]
    nq = books["nq_1tick"]
    es2 = books["es_2tick"]
    nq2 = books["nq_2tick"]

    def deg(a1, a2):
        if not a1["all"]["n"] or a1["all"]["avg"] is None or a2["all"]["avg"] is None:
            return None
        return round(a2["all"]["avg"] - a1["all"]["avg"], 3)

    nq_deg = deg(nq, nq2)
    es_deg = deg(es, es2)
    kill = {
        "is_net_pos": es["is"]["net"] > 0,
        "oos_net_pos": es["oos"]["net"] > 0,
        "nq_slip2_deg": nq_deg,
        "es_slip2_deg": es_deg,
        "es_slip_better": (es_deg is not None and nq_deg is not None and es_deg > nq_deg),
        # "better" = less negative degradation
    }
    kill["pass"] = kill["is_net_pos"] and kill["oos_net_pos"] and kill["es_slip_better"]

    md = [
        "# EXP-0059 — Execute CFADE on ES",
        "",
        "Frozen CFADE events; ES primary path; ES brackets floor=2 cap=15; "
        f"commission {F.ES_COMMISSION} pt/RT.",
        "",
        "| book | n | WR | net | avg | IS net | OOS net |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for k, v in books.items():
        a, i, o = v["all"], v["is"], v["oos"]
        md.append(
            f"| {k} | {a['n']} | {a.get('wr')} | {a['net']:+.1f} | {a.get('avg')} | "
            f"{i['net']:+.1f} | {o['net']:+.1f} |"
        )
    md += [
        "",
        "## Kill bar",
        "",
        f"IS net+={kill['is_net_pos']} OOS net+={kill['oos_net_pos']} "
        f"NQ slip2 Δavg={nq_deg} ES slip2 Δavg={es_deg} "
        f"ES more robust={kill['es_slip_better']}",
        "",
        f"## Verdict: **{'PASS' if kill['pass'] else 'FAIL / park'}**",
        "",
        "Parity: nq_1tick should match clean CFADE baseline (~+1211 on 113d).",
        "",
    ]
    F.write_report(out, title="EXP-0059", md_lines=md, report={
        "exp": "EXP-0059", "books": books, "kill": kill,
    })


if __name__ == "__main__":
    main()
