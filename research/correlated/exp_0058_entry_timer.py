"""EXP-0058 / H49 — CFADE microprice / passive entry timer (B1).

Stage 0: depth/MBO coverage audit on clean sessions.
Stage 1: on sessions with NQ depth, compare entry styles on frozen CFADE events:
  baseline — marketable 1-tick slip at event stamp
  microprice — delay <=500ms until microprice favours fade side, else market at 500ms
  passive — 0-slip fill if trade prints at/through join price within 1s, else market at +1s
Kill: retain >=80% trades AND lift >=0.25 pt/trade on IS AND OOS vs baseline.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.backtest.mat_sim import MatSimConfig, merge_summaries, simulate_session
from research.config import load_config
from research.correlated import cfade_family as F
from research.correlated import harness as H


def _bbo_series(depth: pa.Table) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return (ts, best_bid, best_ask, microprice) at every depth update with valid BBO."""
    if depth.num_rows == 0:
        return (np.zeros(0, np.int64),) * 4
    d_ts = depth.column("ts_ns").to_numpy().astype(np.int64)
    d_bid = depth.column("is_bid").to_numpy().astype(bool)
    d_px = depth.column("price_ticks").to_numpy().astype(np.int64)
    d_sz = depth.column("size").to_numpy().astype(np.int64)
    order = np.argsort(d_ts, kind="mergesort")
    d_ts, d_bid, d_px, d_sz = d_ts[order], d_bid[order], d_px[order], d_sz[order]
    bids: dict[int, int] = {}
    asks: dict[int, int] = {}
    best_bid = -10**18
    best_ask = 10**18
    out_ts, out_bb, out_ba, out_mp = [], [], [], []
    for i in range(len(d_ts)):
        is_bid = bool(d_bid[i])
        px = int(d_px[i])
        sz = int(d_sz[i])
        book = bids if is_bid else asks
        if sz <= 0:
            if px in book:
                del book[px]
            if is_bid and px == best_bid:
                best_bid = max(book) if book else -10**18
            elif (not is_bid) and px == best_ask:
                best_ask = min(book) if book else 10**18
        else:
            book[px] = sz
            if is_bid and px > best_bid:
                best_bid = px
            if (not is_bid) and px < best_ask:
                best_ask = px
        if best_bid <= -10**17 or best_ask >= 10**17 or best_bid >= best_ask:
            continue
        bb_sz = float(bids.get(best_bid, 0))
        ba_sz = float(asks.get(best_ask, 0))
        denom = bb_sz + ba_sz
        if denom <= 0:
            mp = 0.5 * (best_bid + best_ask)
        else:
            # size-weighted microprice in tick units
            mp = (best_ask * bb_sz + best_bid * ba_sz) / denom
        out_ts.append(int(d_ts[i]))
        out_bb.append(best_bid)
        out_ba.append(best_ask)
        out_mp.append(mp)
    return (
        np.asarray(out_ts, dtype=np.int64),
        np.asarray(out_bb, dtype=np.int64),
        np.asarray(out_ba, dtype=np.int64),
        np.asarray(out_mp, dtype=np.float64),
    )


def _lookup_bbo(ts_arr, bb, ba, mp, t0: int) -> tuple[int, int, float] | None:
    if len(ts_arr) == 0:
        return None
    i = int(np.searchsorted(ts_arr, t0, side="right") - 1)
    if i < 0:
        return None
    return int(bb[i]), int(ba[i]), float(mp[i])


def _microprice_entry_ts(
    ts_arr, bb, ba, mp, event_ts: int, side: int, max_delay_ms: int = 500
) -> int | None:
    """Return entry ts when microprice favours fade side, or event+max_delay, or None if no BBO."""
    end = event_ts + int(max_delay_ms * 1_000_000)
    i0 = int(np.searchsorted(ts_arr, event_ts, side="left"))
    if i0 >= len(ts_arr):
        return None
    for i in range(i0, len(ts_arr)):
        t = int(ts_arr[i])
        if t > end:
            break
        mid = 0.5 * (bb[i] + ba[i])
        # fade long (side>0): want microprice > mid (bid pressure / lift)
        # fade short (side<0): want microprice < mid
        if side > 0 and mp[i] > mid:
            return t
        if side < 0 and mp[i] < mid:
            return t
    return end  # timeout → market


def _passive_entry(
    trade_ts: np.ndarray,
    trade_px_ticks: np.ndarray,
    event_ts: int,
    side: int,
    join_px: int,
    timeout_ms: int = 1000,
) -> tuple[int, float] | None:
    """Passive join: fill at join_px with 0 slip if printed within timeout; else market at timeout.

    Returns (entry_ts, slip_ticks_override) or None if no trades after event.
    """
    end = event_ts + int(timeout_ms * 1_000_000)
    i0 = int(np.searchsorted(trade_ts, event_ts, side="left"))
    if i0 >= len(trade_ts):
        return None
    for i in range(i0, len(trade_ts)):
        t = int(trade_ts[i])
        if t > end:
            break
        px = int(trade_px_ticks[i])
        # long fade: fill when someone sells to our bid (px <= join)
        # short fade: fill when someone buys our offer (px >= join)
        if side > 0 and px <= join_px:
            return t, 0.0
        if side < 0 and px >= join_px:
            return t, 0.0
    # timeout → marketable
    return end, 1.0


def _shift_events(ev: pa.Table, new_ts: np.ndarray) -> pa.Table:
    cols = {c: ev.column(c) for c in ev.column_names if c != "event_ts_ns"}
    cols["event_ts_ns"] = pa.array(new_ts.astype(np.int64))
    # preserve column order roughly
    ordered = ["event_ts_ns"] + [c for c in ev.column_names if c != "event_ts_ns"]
    return pa.table({c: cols[c] for c in ordered})


def _stat(parts, commission=F.COMMISSION) -> dict:
    if not parts:
        return {"n": 0, "net": 0.0, "avg": None, "wr": None}
    s = merge_summaries(parts, commission)
    d = s.to_dict()
    d["avg"] = round(s.net / s.n, 2) if s.n else None
    return d


def main() -> None:
    cfg = load_config()
    pqdir = Path(cfg["parquet_dir"])
    out = Path(cfg["results_dir"]) / "EXP-0058"
    sessions = F.clean_sessions(pqdir)
    cov = F.depth_mbo_coverage(pqdir, sessions)

    # Stage 0 audit always written
    audit_md = [
        "# EXP-0058 — Stage 0 depth/MBO coverage",
        "",
        f"Clean sessions: **{cov['n_sessions']}**",
        f"With depth.parquet: **{cov['n_depth']}**",
        f"With mbo.parquet: **{cov['n_mbo']}**",
        f"With both: **{cov['n_both']}**",
        "",
    ]

    depth_sids = set(cov["depth_sessions"])
    base_parts_is, base_parts_oos = [], []
    mp_parts_is, mp_parts_oos = [], []
    pas_parts_is, pas_parts_oos = [], []
    n_events_depth = 0
    n_mp_ok = 0
    n_pas_ok = 0

    gate = F.frozen_gate()
    br = F.frozen_bracket()
    sim = F.frozen_sim_cfg()

    for sid in sessions:
        if sid not in depth_sids:
            continue
        pair = H.load_pair(pqdir, sid)
        if pair is None:
            continue
        fades = F.cfade_events(pair)
        if fades.num_rows == 0:
            continue
        # Load NQ depth (may be large — windowed)
        depth = H.load_depth_window(pqdir, sid, "NQ", pair.bounds)
        if depth.num_rows == 0:
            continue
        ts_arr, bb, ba, mp = _bbo_series(depth)
        if len(ts_arr) == 0:
            continue

        nq_ts = pair.nq.column("ts_ns").to_numpy().astype(np.int64)
        nq_px = pair.nq.column("price").to_numpy().astype(np.int64)
        order = np.argsort(nq_ts)
        nq_ts, nq_px = nq_ts[order], nq_px[order]

        ev_ts = fades.column("event_ts_ns").to_numpy().astype(np.int64)
        sides = fades.column("side").to_numpy().astype(int)
        n_events_depth += len(ev_ts)

        mp_ts_list = []
        pas_ts_list = []
        pas_slip = []
        keep_mp = []
        keep_pas = []
        for i in range(len(ev_ts)):
            t0 = int(ev_ts[i])
            side = int(sides[i])
            bbo0 = _lookup_bbo(ts_arr, bb, ba, mp, t0)
            # microprice delay
            mt = _microprice_entry_ts(ts_arr, bb, ba, mp, t0, side, 500)
            if mt is None:
                keep_mp.append(False)
                mp_ts_list.append(t0)
            else:
                keep_mp.append(True)
                mp_ts_list.append(mt)
                n_mp_ok += 1
            # passive join at bid (long) / ask (short) at trigger
            if bbo0 is None:
                keep_pas.append(False)
                pas_ts_list.append(t0)
                pas_slip.append(1.0)
            else:
                join = bbo0[0] if side > 0 else bbo0[1]
                pe = _passive_entry(nq_ts, nq_px, t0, side, join, 1000)
                if pe is None:
                    keep_pas.append(False)
                    pas_ts_list.append(t0)
                    pas_slip.append(1.0)
                else:
                    keep_pas.append(True)
                    pas_ts_list.append(pe[0])
                    pas_slip.append(pe[1])
                    n_pas_ok += 1

        # Baseline
        s0 = simulate_session(sid, pair.nq, pair.es, sim, gate=gate, bracket_fn=br,
                              vwap_from_ns=pair.vwap_from_ns, events=fades)
        # Microprice-shifted events (all retained; delay only)
        ev_mp = _shift_events(fades, np.asarray(mp_ts_list))
        s1 = simulate_session(sid, pair.nq, pair.es, sim, gate=gate, bracket_fn=br,
                              vwap_from_ns=pair.vwap_from_ns, events=ev_mp)
        # Passive: per-event slip via annotating isn't supported globally — approximate
        # by using 0-slip cfg for fills that got passive, else 1-tick. Split into two sims.
        pas_mask = np.array([s == 0.0 for s in pas_slip], dtype=bool)
        if pas_mask.any():
            ev_p0 = _shift_events(fades.take(np.where(pas_mask)[0]), np.asarray(pas_ts_list)[pas_mask])
            cfg0 = MatSimConfig(**{**sim.__dict__, "slip_ticks": 0.0})
            sp0 = simulate_session(sid, pair.nq, pair.es, cfg0, gate=gate, bracket_fn=br,
                                   vwap_from_ns=pair.vwap_from_ns, events=ev_p0)
        else:
            sp0 = None
        if (~pas_mask).any():
            ev_p1 = _shift_events(fades.take(np.where(~pas_mask)[0]), np.asarray(pas_ts_list)[~pas_mask])
            sp1 = simulate_session(sid, pair.nq, pair.es, sim, gate=gate, bracket_fn=br,
                                   vwap_from_ns=pair.vwap_from_ns, events=ev_p1)
        else:
            sp1 = None
        sp_parts = [x for x in (sp0, sp1) if x is not None]
        s2 = merge_summaries(sp_parts, F.COMMISSION) if sp_parts else s0.__class__()

        bucket_is = F.is_is(sid)
        for parts_is, parts_oos, s in (
            (base_parts_is, base_parts_oos, s0),
            (mp_parts_is, mp_parts_oos, s1),
            (pas_parts_is, pas_parts_oos, s2),
        ):
            (parts_is if bucket_is else parts_oos).append(s)

        print(
            f"{sid} depth_ev={fades.num_rows} base={s0.net:+.1f} mp={s1.net:+.1f} pas={s2.net:+.1f}",
            flush=True,
        )

    variants = {
        "baseline": (_stat(base_parts_is + base_parts_oos), _stat(base_parts_is), _stat(base_parts_oos)),
        "microprice_delay_500ms": (_stat(mp_parts_is + mp_parts_oos), _stat(mp_parts_is), _stat(mp_parts_oos)),
        "passive_join_1s": (_stat(pas_parts_is + pas_parts_oos), _stat(pas_parts_is), _stat(pas_parts_oos)),
    }
    base_all, base_is, base_oos = variants["baseline"]

    def lift(v_all, v_is, v_oos):
        def L(a, b):
            if not a["n"] or not b["n"] or a["avg"] is None or b["avg"] is None:
                return None
            return round(a["avg"] - b["avg"], 3)
        return {
            "retain_frac": round(v_all["n"] / base_all["n"], 3) if base_all["n"] else None,
            "lift_all": L(v_all, base_all),
            "lift_is": L(v_is, base_is),
            "lift_oos": L(v_oos, base_oos),
            "retain_ok": (v_all["n"] / base_all["n"] >= 0.80) if base_all["n"] else False,
        }

    kills = {k: lift(*v) for k, v in variants.items() if k != "baseline"}
    survivors = [
        k for k, info in kills.items()
        if info["retain_ok"]
        and info["lift_is"] is not None and info["lift_is"] >= 0.25
        and info["lift_oos"] is not None and info["lift_oos"] >= 0.25
    ]
    verdict = survivors[0] if survivors else "baseline (no timer clears kill bar)"

    md = audit_md + [
        "## Stage 1 — entry styles (sessions with NQ depth only)",
        "",
        f"Depth sessions used: {len(depth_sids)} · CFADE events on those: {n_events_depth}",
        f"Microprice BBO available for delay: {n_mp_ok} · Passive candidate fills: {n_pas_ok}",
        "",
        "| variant | n | WR | net | avg | IS n/net/avg | OOS n/net/avg |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for name, (a, i, o) in variants.items():
        md.append(
            f"| {name} | {a['n']} | {a.get('wr')} | {a['net']:+.1f} | {a.get('avg')} | "
            f"{i['n']}/{i['net']:+.1f}/{i.get('avg')} | {o['n']}/{o['net']:+.1f}/{o.get('avg')} |"
        )
    md += ["", "## Kill bar", ""]
    for k, info in kills.items():
        md.append(
            f"- **{k}**: retain={info['retain_frac']} lift_is={info['lift_is']} "
            f"lift_oos={info['lift_oos']} → "
            f"{'PASS' if k in survivors else 'FAIL'}"
        )
    md += ["", f"## Verdict", "", f"**{verdict}**", ""]

    F.write_report(
        out, title="EXP-0058", md_lines=md,
        report={
            "exp": "EXP-0058",
            "coverage": cov,
            "variants": {k: {"all": a, "is": i, "oos": o} for k, (a, i, o) in variants.items()},
            "kills": kills,
            "survivors": survivors,
            "verdict": verdict,
            "n_events_depth": n_events_depth,
        },
    )


if __name__ == "__main__":
    main()
