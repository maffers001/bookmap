"""EXP-0100 / H91 — both-markets above/below dVPOC+DOP; host-gate dig.

Usage:
  python -m research.mat_context.exp_0100_mat_vpoc_dop_align
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from research.backtest.mat_sim import (
    combine_gates,
    detect_signals,
    make_trend_mode,
    make_vwap_side,
    simulate_session,
)
from research.config import ROOT, load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H
from research.features.mat_vpoc_dop_align import (
    VpocDopAlignConfig,
    annotate_vpoc_dop_align,
)
from research.irt_lvn.load_irt import default_irt_paths, load_irt_csv
from research.irt_lvn.study_es_intraday_lvn_large import _session_irt
from research.mat_context.report import arm_pack, write_exp_report
from research.run_cfade_all_months import _sid_chrono_key

HORIZONS = [10_000, 60_000, 300_000]
TARGETS = [5.0, 10.0, 20.0]
CACHE_EXP = "EXP-0100"
ANNOTATE_VERSION = "vpoc-dop-align-v1"
HOST_NAMES = ("none", "vwap", "trend", "frozen")


def _load_exp() -> dict:
    path = Path(__file__).resolve().parents[1] / "experiments" / "EXP-0100.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _host_gate(name: str):
    if name == "none":
        return None
    if name == "vwap":
        return make_vwap_side(True)
    if name == "trend":
        return make_trend_mode("with")
    if name == "frozen":
        return combine_gates(make_trend_mode("with"), make_vwap_side(True))
    raise ValueError(name)


def _filter_flag(ev: pa.Table, col: str) -> pa.Table:
    if ev.num_rows == 0 or col not in ev.column_names:
        return pa.table({})
    mk = ev.column(col).to_numpy().astype(np.int8) == 1
    return ev.take(np.where(mk)[0]) if mk.any() else pa.table({})


def _sim(sid, pair, ev, *, gate, slip=0.0):
    if ev.num_rows == 0:
        return simulate_session(
            sid, pair.nq, pair.es, F.mat_sim_cfg(slip_ticks=slip),
            gate=gate, bracket_fn=F.mat_vol_bracket(),
            vwap_from_ns=pair.vwap_from_ns, events=pa.table({}),
        )
    cols = {"event_ts_ns": ev.column("event_ts_ns"), "side": ev.column("side")}
    return simulate_session(
        sid, pair.nq, pair.es, F.mat_sim_cfg(slip_ticks=slip),
        gate=gate, bracket_fn=F.mat_vol_bracket(),
        vwap_from_ns=pair.vwap_from_ns, events=pa.table(cols),
    )


def _pack(sims: dict) -> dict:
    parts, is_p, oos_p = [], [], []
    for sid, s in sims.items():
        parts.append(s)
        (is_p if F.is_is(sid) else oos_p).append(s)
    return arm_pack(parts, is_p, oos_p)


def _concat(parts: list[pa.Table]) -> pa.Table:
    parts = [p for p in parts if p.num_rows]
    if not parts:
        return pa.table({})
    cols = set(parts[0].column_names)
    for p in parts[1:]:
        cols &= set(p.column_names)
    keep = [c for c in parts[0].column_names if c in cols]
    return pa.concat_tables([p.select(keep) for p in parts], promote_options="default")


def _oos_pnls(sims: dict, commission: float = F.COMMISSION) -> np.ndarray:
    out = []
    for sid, s in sims.items():
        if F.is_is(sid):
            continue
        for t in s.trades:
            out.append(t.pnl - commission)
    return np.asarray(out, dtype=np.float64)


def _random_keep_p90(pool: np.ndarray, n_keep: int, *, seed: int, draws: int = 1000) -> float:
    if n_keep <= 0 or len(pool) == 0:
        return float("nan")
    n_keep = min(n_keep, len(pool))
    rng = np.random.default_rng(seed)
    avgs = np.empty(draws)
    for i in range(draws):
        idx = rng.choice(len(pool), size=n_keep, replace=False)
        avgs[i] = float(np.mean(pool[idx]))
    return float(np.quantile(avgs, 0.90))


def _book_kill(arm, base, twin, *, placebo_p90, min_oos_n, min_lift):
    a, b, t = arm["oos"], base["oos"], twin["oos"]
    avg = a.get("avg")
    kill = {
        "oos_net_pos": a["net"] > 0,
        "beat_base_plus_lift": (
            avg is not None and b.get("avg") is not None and avg >= b["avg"] + min_lift
        ),
        "beat_twin": avg is not None and t.get("avg") is not None and avg > t["avg"],
        "beat_placebo_p90": avg is not None and np.isfinite(placebo_p90) and avg > placebo_p90,
        "n_oos_ge_min": a["n"] >= min_oos_n,
        "placebo_p90": None if not np.isfinite(placebo_p90) else round(placebo_p90, 4),
    }
    kill["pass"] = all(
        kill[k]
        for k in (
            "oos_net_pos", "beat_base_plus_lift", "beat_twin",
            "beat_placebo_p90", "n_oos_ge_min",
        )
    )
    return kill


def run(*, max_sessions: int | None = None, skip_docs: bool = False) -> dict:
    exp_name = "EXP-0100"
    hyp = "H91"
    primary_col = "vpoc_dop_align"
    anti_col = "vpoc_dop_anti"

    cfg = load_config()
    exp = _load_exp()
    pq_dir = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / exp_name
    out.mkdir(parents=True, exist_ok=True)
    cache_path = out / "events.parquet"
    cache_marker = out / "annotate_version.txt"

    kill_cfg = dict(exp.get("kill") or {})
    seed = int(exp.get("placebo_seed", 100))
    hv_cfg = VpocDopAlignConfig(inclusive=True)

    sessions = F.clean_sessions(pq_dir)
    chrono = sorted(sessions, key=_sid_chrono_key)
    if max_sessions:
        chrono = chrono[:max_sessions]

    print(f"[{exp_name}] sessions={len(chrono)} host_dig={list(HOST_NAMES)}", flush=True)
    irt_paths = default_irt_paths(ROOT)
    irt_nq_all = load_irt_csv(irt_paths["NQ"])
    irt_es_all = load_irt_csv(irt_paths["ES"])

    pairs: dict[str, H.Pair] = {}
    session_bars: dict[str, tuple] = {}

    cache_ok = (
        cache_path.exists()
        and cache_marker.exists()
        and cache_marker.read_text(encoding="utf-8").strip() == ANNOTATE_VERSION
    )
    if cache_ok and not max_sessions:
        print(f"[{exp_name}] loading cached annotate {cache_path}", flush=True)
        ann_all = pq.read_table(cache_path)
        sids = set(ann_all.column("session_id").to_pylist()) if ann_all.num_rows else set()
        for sid in chrono:
            if sid not in sids:
                continue
            pair = H.load_pair(pq_dir, sid)
            if pair is not None:
                pairs[sid] = pair
    else:
        if cache_path.exists() and not cache_ok:
            print(f"[{exp_name}] ignoring stale annotate cache", flush=True)
        ann_parts: list[pa.Table] = []
        for sid in chrono:
            pair = H.load_pair(pq_dir, sid)
            if pair is None:
                continue
            pairs[sid] = pair
            nq_b, nq_sh = _session_irt(irt_nq_all, sid, pq_dir, "NQ")
            es_b, es_sh = _session_irt(irt_es_all, sid, pq_dir, "ES")
            session_bars[sid] = (nq_b, es_b)
            raw = detect_signals(pair.nq, pair.es, F.mat_sim_cfg())
            if raw.num_rows == 0:
                continue
            ann = annotate_vpoc_dop_align(raw, pair.nq, pair.es, nq_b, es_b, cfg=hv_cfg)
            ann = ann.append_column("session_id", pa.array([sid] * ann.num_rows))
            ann_parts.append(ann)
            print(
                f"  {sid}: mat={ann.num_rows} "
                f"align={int(ann.column(primary_col).to_numpy().sum())} "
                f"anti={int(ann.column(anti_col).to_numpy().sum())} "
                f"(shift nq={nq_sh} es={es_sh})",
                flush=True,
            )
        ann_all = _concat(ann_parts)
        if ann_all.num_rows:
            pq.write_table(ann_all, cache_path, compression="zstd")
            cache_marker.write_text(ANNOTATE_VERSION + "\n", encoding="utf-8")

    def _n(col: str) -> int:
        return int(ann_all.column(col).to_numpy().sum()) if ann_all.num_rows else 0

    n_prim = _n(primary_col)
    n_anti = _n(anti_col)
    n_base = ann_all.num_rows
    print(
        f"[{exp_name}] annotated={n_base} align={n_prim} anti={n_anti} "
        f"both_above={_n('both_above')} both_below={_n('both_below')}",
        flush=True,
    )

    report: dict = {
        "status": "ok", "experiment": exp_name, "hypothesis": hyp,
        "primary": "align", "twin": "base", "primary_host": "none",
        "host_dig": list(HOST_NAMES), "stages": {},
    }
    md = [
        f"# {exp_name} — both-mkt dVPOC+DOP align ({hyp})",
        "",
        f"Frozen: `research/experiments/{exp_name}.yaml`. "
        "Long iff NQ∧ES ≥ dVPOC and ≥ DOP; short iff both ≤ both (inclusive). "
        "Primary = **align × host none**; twin = ungated base. "
        f"Host dig {list(HOST_NAMES)}; neighbours cannot promote alone.",
        f"Annotated: {n_base} · align: {n_prim} · anti: {n_anti} · "
        f"both_above stamps: {_n('both_above')} · both_below: {_n('both_below')}",
        "",
    ]

    # Stage 0 truncation
    trunc_rows = []
    if ann_all.num_rows and pairs:
        sample = _filter_flag(ann_all, primary_col)
        if sample.num_rows == 0:
            sample = ann_all
        for i in range(min(3, sample.num_rows)):
            sid = sample.column("session_id")[i].as_py()
            stamp = int(sample.column("event_ts_ns")[i].as_py())
            pair = pairs.get(sid)
            if pair is None:
                continue
            bars_pair = session_bars.get(sid)
            if bars_pair is None:
                nq_b, _ = _session_irt(irt_nq_all, sid, pq_dir, "NQ")
                es_b, _ = _session_irt(irt_es_all, sid, pq_dir, "ES")
                session_bars[sid] = (nq_b, es_b)
            else:
                nq_b, es_b = bars_pair
            ev = pa.table({
                "event_ts_ns": np.asarray([stamp], dtype=np.int64),
                "side": np.asarray([int(sample.column("side")[i].as_py())], dtype=np.int8),
            })
            full = annotate_vpoc_dop_align(ev, pair.nq, pair.es, nq_b, es_b, cfg=hv_cfg)

            def _cut_trades(t: pa.Table, cut: int) -> pa.Table:
                if t.num_rows == 0:
                    return t
                ts = t.column("ts_ns").to_numpy().astype(np.int64)
                return t.take(np.where(ts <= cut)[0])

            nq_cut = _cut_trades(pair.nq, stamp)
            es_cut = _cut_trades(pair.es, stamp)
            nq_b_cut = nq_b.slice_range(0, stamp) if len(nq_b.ts_ns) else nq_b
            es_b_cut = es_b.slice_range(0, stamp) if len(es_b.ts_ns) else es_b
            trunc = annotate_vpoc_dop_align(
                ev, nq_cut, es_cut, nq_b_cut, es_b_cut, cfg=hv_cfg,
            )
            ok = True
            diffs = {}
            for col in (primary_col, "both_above", "both_below", "nq_vpoc", "es_dop"):
                a, b = full.column(col)[0].as_py(), trunc.column(col)[0].as_py()
                if isinstance(a, float):
                    match = (
                        (a is None and b is None)
                        or (
                            isinstance(a, float) and isinstance(b, float)
                            and ((np.isnan(a) and np.isnan(b)) or abs(a - b) < 1e-9)
                        )
                    )
                else:
                    match = a == b
                if not match:
                    ok = False
                    diffs[col] = (a, b)
            trunc_rows.append({"session": sid, "match": ok, "diffs": diffs})

    trunc_ok = bool(trunc_rows) and all(r["match"] for r in trunc_rows)

    sims_p: dict = {}
    for sid, pair in pairs.items():
        if ann_all.num_rows == 0:
            continue
        mask = np.array(
            [ann_all.column("session_id")[i].as_py() == sid for i in range(ann_all.num_rows)]
        )
        if not mask.any():
            continue
        sub = ann_all.take(np.where(mask)[0])
        sims_p[sid] = _sim(sid, pair, _filter_flag(sub, primary_col), gate=None)
    n_prim_trades = sum(s.n for s in sims_p.values())
    min_sig = int(kill_cfg.get("min_primary_signals", 50))
    min_tr = int(kill_cfg.get("min_resim_trades", 20))
    power_ok = n_prim >= min_sig and n_prim_trades >= min_tr

    report["stages"]["0"] = {
        "n_mat": n_base, "n_primary": n_prim, "n_anti": n_anti,
        "n_primary_trades": n_prim_trades,
        "truncation": trunc_rows, "truncation_ok": trunc_ok, "power_ok": power_ok,
    }
    md += [
        "## Stage 0 — causality + power",
        "",
        f"align: **{n_prim}** · anti: {n_anti} · base: {n_base}",
        f"align ungated trades: **{n_prim_trades}**",
        f"Truncation: **{'PASS' if trunc_ok else 'FAIL'}** · "
        f"power: **{'PASS' if power_ok else 'FAIL'}**",
        "",
    ]
    if not trunc_ok or not power_ok:
        why = (
            "Stage 0 causality failed" if not trunc_ok
            else "Stage 0 underpowered (counts are the result; no retune)"
        )
        report["verdict"] = f"reject / park — {why}"
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        write_exp_report(out, title=exp_name, md_lines=md, report=report)
        if not skip_docs:
            _write_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    # Stage 1 event study (gate-free)
    print(f"[{exp_name}] Stage 1…", flush=True)
    arm_names = ("base", "align", "anti", "null")
    lab_store = {a: [] for a in arm_names}
    rng = np.random.default_rng(seed)
    for sid, pair in pairs.items():
        mask = np.array(
            [ann_all.column("session_id")[i].as_py() == sid for i in range(ann_all.num_rows)]
        )
        if not mask.any():
            continue
        sub = ann_all.take(np.where(mask)[0])
        sets = {
            "base": sub,
            "align": _filter_flag(sub, primary_col),
            "anti": _filter_flag(sub, anti_col),
        }
        n_null = max(sets["align"].num_rows, 1)
        nul = H.null_events(pair, n_null, seed=seed + hash(sid) % 10_000)
        if sets["align"].num_rows and nul.num_rows:
            sides = sets["align"].column("side").to_numpy()
            pick = rng.choice(sides, size=nul.num_rows, replace=True)
            nul = nul.drop("side").append_column("side", pa.array(pick.astype(np.int8)))
        sets["null"] = nul
        for name, tbl in sets.items():
            if tbl.num_rows:
                lab = H.label_in_points(tbl, pair, HORIZONS, TARGETS)
                if lab.num_rows:
                    lab_store[name].append(lab)

    stage1 = {}
    md += [
        "## Stage 1 — event study (align vs base; gate-free)",
        "",
        "| set | n | hit5 | hit10 | @10s | @60s | @300s | MFE | MAE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in arm_names:
        s = H.summarize(lab_store[name], HORIZONS, TARGETS)
        stage1[name] = s
        if s.get("n_events"):
            md.append(
                f"| {name} | {s['n_events']} | "
                f"{s.get('p_hit_5pts', float('nan')):.2f} | "
                f"{s.get('p_hit_10pts', float('nan')):.2f} | "
                f"{H.fmt(s.get('mean_ret_pts_10000ms'), 2)} | "
                f"{H.fmt(s.get('mean_ret_pts_60000ms'), 2)} | "
                f"{H.fmt(s.get('mean_ret_pts_300000ms'), 2)} | "
                f"{H.fmt(s.get('mean_mfe_pts'), 1)} | "
                f"{H.fmt(s.get('mean_mae_pts'), 1)} |"
            )
        else:
            md.append(f"| {name} | 0 | – | – | – | – | – | – | – |")

    lift_60 = float(kill_cfg.get("stage1_lift_60s_pts", 1.0))
    lift_300 = float(kill_cfg.get("stage1_lift_300s_pts", 2.0))
    prim, twin, nl = stage1["align"], stage1["base"], stage1["null"]
    m60p, m60t = prim.get("mean_ret_pts_60000ms"), twin.get("mean_ret_pts_60000ms")
    m300p, m300t = prim.get("mean_ret_pts_300000ms"), twin.get("mean_ret_pts_300000ms")
    m60n = nl.get("mean_ret_pts_60000ms")
    s1 = {
        "lift_60": (
            m60p is not None and m60t is not None
            and np.isfinite(m60p) and np.isfinite(m60t)
            and (m60p - m60t) >= lift_60
        ),
        "lift_300": (
            m300p is not None and m300t is not None
            and np.isfinite(m300p) and np.isfinite(m300t)
            and (m300p - m300t) >= lift_300
        ),
        "beat_null_60": (
            m60p is not None and m60n is not None
            and np.isfinite(m60p) and np.isfinite(m60n)
            and m60p > m60n
        ),
    }
    s1_pass = all(s1.values())
    report["stages"]["1"] = {"summaries": stage1, "checks": s1, "pass": s1_pass}
    md += [
        "",
        f"Checks: {s1}",
        f"**Stage 1: {'PASS' if s1_pass else 'FAIL'}**",
        "",
    ]
    if not s1_pass:
        # Still print a lightweight host dig on event-study? No — protocol parks.
        # But user asked with/without vwap/trend — print Stage 2 dig table as
        # descriptive even on Stage 1 fail? Plan for HVN said no Stage 2 P&L.
        # Here user explicitly wants with/without gates. Run Stage 2 dig as
        # *descriptive* when Stage 1 fails, but verdict remains Stage 1 reject
        # and neighbours cannot promote.
        print(f"[{exp_name}] Stage 1 FAIL — running descriptive host dig (no promote)…", flush=True)
        dig = _host_dig(pairs, ann_all, primary_col, seed=seed)
        report["stages"]["2"] = {"descriptive_host_dig": dig, "pass": False, "promoted": False}
        md += _host_dig_md(dig, kill=None, descriptive=True)
        report["verdict"] = (
            "reject / park — Stage 1 event-study fail (no promote; host dig descriptive only)"
        )
        md += ["## Verdict", "", f"**{report['verdict']}**", ""]
        write_exp_report(out, title=exp_name, md_lines=md, report=report)
        if not skip_docs:
            _write_docs(report["verdict"])
        print(report["verdict"], flush=True)
        return report

    # Stage 2 — primary kill on host=none; dig other hosts
    print(f"[{exp_name}] Stage 2…", flush=True)
    dig = _host_dig(pairs, ann_all, primary_col, seed=seed)
    books_align_none = dig["align"]["none"]
    books_base_none = dig["base"]["none"]
    pool = _oos_pnls_from_pack_sessions(dig["sims_base_none"])
    p90 = _random_keep_p90(
        pool, books_align_none["oos"]["n"], seed=seed + 1,
    )
    min_oos = int(kill_cfg.get("min_oos_trades_promote", 50))
    min_lift = float(kill_cfg.get("min_avg_lift_pts", 1.0))
    kill = _book_kill(
        books_align_none, books_base_none, books_base_none,
        placebo_p90=p90, min_oos_n=min_oos, min_lift=min_lift,
    )
    report["stages"]["2"] = {
        "host_dig": dig["align"], "base_dig": dig["base"],
        "anti_dig": dig["anti"], "kill": kill, "pass": kill["pass"],
    }
    md += _host_dig_md(dig, kill=kill, descriptive=False)
    report["verdict"] = (
        "PASS research — candidate both-mkt dVPOC+DOP align keep-gate; virgin months before ship"
        if kill["pass"]
        else "reject / park — Stage 2 P&L fail (no retune; host neighbours not promoted)"
    )
    md += ["## Verdict", "", f"**{report['verdict']}**", ""]
    write_exp_report(out, title=exp_name, md_lines=md, report=report)
    if not skip_docs:
        _write_docs(report["verdict"])
    print(report["verdict"], flush=True)
    return report


def _oos_pnls_from_pack_sessions(sims: dict) -> np.ndarray:
    return _oos_pnls(sims)


def _host_dig(pairs, ann_all, primary_col: str, *, seed: int) -> dict:
    """Simulate base/align/anti under each host gate."""
    anti_col = "vpoc_dop_anti"
    out_books = {arm: {} for arm in ("base", "align", "anti")}
    sims_store = {arm: {h: {} for h in HOST_NAMES} for arm in ("base", "align", "anti")}

    for sid, pair in pairs.items():
        mask = np.array(
            [ann_all.column("session_id")[i].as_py() == sid for i in range(ann_all.num_rows)]
        )
        sub = ann_all.take(np.where(mask)[0]) if mask.any() else pa.table({})
        sets = {
            "base": sub,
            "align": _filter_flag(sub, primary_col),
            "anti": _filter_flag(sub, anti_col),
        }
        for arm, ev in sets.items():
            for host in HOST_NAMES:
                sims_store[arm][host][sid] = _sim(
                    sid, pair, ev, gate=_host_gate(host),
                )

    for arm in ("base", "align", "anti"):
        for host in HOST_NAMES:
            out_books[arm][host] = _pack(sims_store[arm][host])

    return {
        "base": out_books["base"],
        "align": out_books["align"],
        "anti": out_books["anti"],
        "sims_base_none": sims_store["base"]["none"],
    }


def _host_dig_md(dig: dict, *, kill, descriptive: bool) -> list[str]:
    title = (
        "## Stage 2 — P&L host dig (descriptive; no promote)"
        if descriptive
        else "## Stage 2 — P&L (primary host=none; dig vwap/trend/frozen)"
    )
    md = [
        title,
        "",
        "| arm × host | n | WR | net | avg | IS net | OOS net | OOS avg |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in ("base", "align", "anti"):
        for host in HOST_NAMES:
            v = dig[arm][host]
            aa, oo = v["all"], v["oos"]
            md.append(
                f"| {arm}×{host} | {aa['n']} | {aa.get('wr')} | {aa['net']:+.1f} | "
                f"{aa.get('avg')} | {v['is']['net']:+.1f} | {oo['net']:+.1f} | {oo.get('avg')} |"
            )
    md.append("")
    if kill is not None:
        md += [f"Kill (align×none vs base×none): {kill}",
               f"**Stage 2: {'PASS' if kill['pass'] else 'FAIL'}**", ""]
    else:
        md += ["_Host dig is descriptive only — Stage 1 already failed; no promote._", ""]
    return md


def _write_docs(verdict: str) -> None:
    exp_name, hyp = "EXP-0100", "H91"
    hyp_path = Path("docs/HYPOTHESES.md")
    text = hyp_path.read_text(encoding="utf-8")
    needle = "## H91 — Raw MAT keep when both ES+NQ above/below dVPOC+DOP\n"
    pending = "**Result:** pending — registered 2026-11-01."
    idx = text.find(needle)
    if idx >= 0:
        ridx = text.find(pending, idx)
        end = text.find("\n---\n", idx)
        if ridx >= 0 and (end < 0 or ridx < end):
            text = (
                text[:ridx]
                + f"**Result ({exp_name}): {verdict}.** See `results/{exp_name}/`."
                + text[ridx + len(pending) :]
            )
            hyp_path.write_text(text, encoding="utf-8")

    log = Path("docs/EXPERIMENT_LOG.md")
    lt = log.read_text(encoding="utf-8")
    old = (
        "| EXP-0100 | H91 both-mkt above/below dVPOC+DOP (host dig) | registered — pending run | "
        "clean sessions + IRT | primary align×none; dig vwap/trend/frozen; see notes |"
    )
    new = (
        f"| EXP-0100 | H91 both-mkt above/below dVPOC+DOP (host dig) | "
        f"ran — **{verdict.split('—')[0].strip()}** | "
        f"clean sessions + IRT | {verdict}; see notes |"
    )
    if old in lt:
        lt = lt.replace(old, new, 1)
    note_h = "### 2026-11-01 — EXP-0100 both-mkt dVPOC+DOP align (H91)"
    note = (
        f"\n{note_h}\n\n"
        f"- Verdict: **{verdict}**\n"
        f"- Results: `results/{exp_name}/`.\n"
        f"- Runner: `python -m research.mat_context.exp_0100_mat_vpoc_dop_align`.\n"
    )
    if note_h not in lt:
        anchor = "### 2026-11-01 — EXP-0100 both-mkt dVPOC+DOP align registered (H91)"
        idx = lt.find(anchor)
        nxt = lt.find("\n### ", idx + 5) if idx >= 0 else -1
        if nxt > 0:
            lt = lt[:nxt] + note + lt[nxt:]
        else:
            lt = lt + note
    log.write_text(lt, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-sessions", type=int, default=None)
    ap.add_argument("--skip-docs", action="store_true")
    args = ap.parse_args()
    run(max_sessions=args.max_sessions, skip_docs=args.skip_docs)


if __name__ == "__main__":
    main()
