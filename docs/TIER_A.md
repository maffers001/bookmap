# Tier A — correlated digs on proven books (tracker)

> **Home for Tier A** as defined in
> [`CORRELATED_IDEAS_2026-10-23.md` §8](CORRELATED_IDEAS_2026-10-23.md).
> Tier A items are trades-first (A4: depth-subset) strata/features **on the
> frozen MAT and CFADE trigger sets**, not new standalone entries. They ship,
> if at all, as a gate/flag parameter on an existing Bookmap module plus a
> parity check — not as a new module.
>
> **This doc is a tracker, not a preregistration.** Each item earned an
> H-number and a frozen YAML only when it was preregistered, per
> [`RESEARCH_RULES.md`](RESEARCH_RULES.md) and the `CORRELATED_IDEAS` §5
> rules. **Tier A is complete: A1–A4 were all preregistered, run, and
> closed reject/park on 2026-10-24 (EXP-0051–EXP-0054 / H42–H45).** No
> Tier A gate or state ships, and none of the four read May.

## Status board

| ID | Source | Idea | Status | Trigger set | Next action | Blocker |
|---|---|---|---|---|---|---|
| A1 | §2.7 | Volume-ratio shock | **CLOSED — reject/park (EXP-0051, Stage 0 power)** | Frozen CFADE + MAT (DIG4 23d) | None — shock candidates 14 < 20; no P&L, no May, no retune | Closed on this sample |
| A2 | §2.3 | Aggressor count vs size | **CLOSED — reject/park (EXP-0052, Stage 1)** | Frozen MAT (primary FLB gate) + CFADE (MSQ stratum) | None — gated avg +0.59 vs +1.55, placebo fail; MSQ empty; no May, no retune | Closed on this sample |
| A3 | §4.3 | Large-print clustering | **CLOSED — reject/park (EXP-0053, Stage 1)** | Frozen MAT (primary both-burst gate) + CFADE (strata) | None — gated avg +1.13 vs +1.55; hold/maxDD/placebo fail; no May, no retune | Closed on this sample |
| A4 | §1.3 Claim A | BBO-imbalance agreement state | **CLOSED — reject/park (EXP-0054, matters bar)** | Frozen MAT + CFADE trades (all 23 DIG4 sessions had depth) | None — `agree_with` trades 0 on both books; no May, no retune | Closed on this sample |

Dig order was **A1 → A2 → A3 → A4**. A1 closed 2026-10-24 (EXP-0051,
Stage 0); A2 closed 2026-10-24 (EXP-0052, Stage 1); A3 closed
2026-10-24 (EXP-0053, Stage 1); A4 closed 2026-10-24 (EXP-0054,
matters bar). **Tier A is exhausted.** The next queue item is Tier B B1
(§1.3 Claim B execution timer), but only in its microprice-only fallback
form and only under a fresh preregistration: A4 did not earn the right
to supply its agreement state as the timer variable.

## Rules applying to all four

1. **Count first.** No P&L run until the target stratum's count on the DIG4
   23d discovery sample is reported. Gate strata need n ≥ 20 skipped/kept
   trades; a standalone entry needs n ≥ 200 events. If the count fails, the
   item parks at Stage 0 — that is a result, and it gets logged.
2. **Stratify before you signal.** First read is always descriptive:
   net/avg of the *existing* frozen book split by the new feature at trigger
   time. A standalone entry is considered only if a stratum clears its
   placebo on discovery.
3. **Samples.** Discovery = DIG4 23d freeze sample. May 2026 is read **once**,
   only after a discovery pass, with thresholds unchanged. No retuning on
   May, no second read.
4. **Mandatory nulls.** NQ-only twin of the same feature, time-matched
   random null, and — for any gate — a random-skip placebo (same number of
   trades dropped, ≥ 1,000 seed-fixed draws; gated avg must beat p90).
5. **Units and stamps.** Points, not ticks. Causal feature values only
   (feature at trigger uses data at or before the trigger stamp; verify by
   truncation invariance before any P&L, as in EXP-0049/0050 Stage 0).
6. **Costs and books.** Where P&L is run: `simulate_session` in
   `research/backtest/mat_sim.py`, 1-tick slip, 0.25 pt/RT, 5 s cooldown;
   CFADE book = frozen `against_m04` + ER60 ≤ 0.30 + vol RR SL1/TP2; MAT
   book = frozen vol k=1 + trend/VWAP, causal. Comparator is always the
   ungated-by-this-feature frozen book (incremental test, as in EXP-0050).
7. **Ship gate.** Discovery pass → May pass (gated avg > comparator avg
   **and** gated net > 0) → only then Java. A stratum that only works as a
   report never ships.

---

## A1 — §2.7 Volume-ratio shock

> **Outcome — EXP-0051 / H42 (2026-10-24): CLOSED, reject/park at
> Stage 0.** Frozen as below (trailing-z form). Causality PASS. Coverage:
> warmup 17 / low 82 / mid 19 / **shock 14 < 20 power floor** → no P&L,
> no May, no retune of the z cut on this sample. Ratio median 0.37,
> p90 0.76. Report: `results/EXP-0051/`.

- **Claim.** An NQ aggressor-volume spike is a market-wide event and
  continues when ES volume spikes with it (NQ/ES ratio near normal), but is
  NQ-specific and reverts when the ratio spikes (ratio ≫ 1). As a gate:
  CFADE/MAT outcomes differ by ratio state at trigger. Motivating stratum:
  CFADE's low burst-ES-vol tercile was +427 vs high tercile −78 on the 23d
  sample (`results/EXP-0036_DIG3/`).
- **Construction (to freeze at preregistration).** Rolling 10 s aggressor
  volume per market on the dense grid; ratio = NQ vol / ES vol, z-scored by
  minute-of-day; value stamped causally at window end. Window length, z
  lookback, and stratum cuts are **to-freeze** — not frozen here.
- **Trigger set.** Every frozen CFADE candidate and every frozen MAT trade
  on DIG4 23d, annotated with the ratio z at its stamp.
- **First step.** Coverage/count report (n per stratum, per book), then
  net/avg by ratio tercile plus the pre-specified split: NQ-volume-spike
  (top tercile of NQ 10 s volume) × ratio state. No standalone entry yet.
- **Kill / promote template.** Promote to a gate P&L only if at least one
  stratum reaches n ≥ 20 trades **and** its avg differs from the book avg
  by ≥ 1.0 pt in the claimed direction. Gate must then clear the full
  EXP-0050-style bar (avg lift ≥ 1.0 pt vs comparator, net retained ≥ 60 %,
  train and hold avg not worse, maxDD not worse, gated avg > random-skip
  placebo p90). Any failure = reject/park, no retune on the same sample.
- **Data / plumbing.** Trades only. Reuses the dense-grid machinery in
  `research/features/corr_divergence.py` and the DIG4 session list / harness
  in `research/correlated/exp_0049_divergence.py` /
  `research/correlated/harness.py`.
- **Ship path.** Gate parameter on the CFADE module (a rolling volume-ratio
  tracker alongside the correlation-tracker design noted in EXP-0050's
  Stage 3, which was cancelled and would need its own parity run); Java
  only after a May pass.
- **Not allowed.** Standalone ratio-spike entries before the stratum read;
  choosing stratum cuts from the discovery P&L; reviving EXP-0049
  persistence/D thresholds under a ratio label.

## A2 — §2.3 Aggressor count vs size

> **Outcome — EXP-0052 / H43 (2026-10-24): CLOSED, reject/park at
> Stage 1.** Causality PASS; FLB power PASS (160/1,956 MAT signals).
> MAT gated (keep FLB) avg **+0.59** (n=64) vs comparator **+1.55**
> (n=502, net +779 reproduced); placebo p90 +4.92; new cohort −118 →
> FAIL. MSQ empty at CFADE candidates (0/132). No May, no Java, no
> cell swap. Report: `results/EXP-0052/`.

- **Claim.** "Few-large in both markets" (few distinct aggressors, large
  mean size) is institutional flow and continues; "many-small in NQ,
  nothing in ES" is retail/algo chop and fades. This is a founding idea
  (handoff §2.2–2.4) that has never been built as a feature.
- **Construction (to freeze at preregistration).** Per market, per 1 s:
  count of distinct `aggressor_order_id` values and mean aggressive size,
  trailing-window aggregated and stamped causally at the trigger. Window
  and few/many + large/small cuts are **to-freeze** — not frozen here.
- **Trigger set.** Every frozen CFADE candidate and every frozen MAT trade
  on DIG4 23d, annotated with both markets' count/size state.
- **First step.** Descriptive 16-cell table (few/many × large/small, per
  market, combined) reporting n per cell only. At most **two** cells —
  few-large-in-both and many-small-NQ-only — may be carried into the
  net/avg stratum read; the other cells stay descriptive.
- **Kill / promote template.** A carried cell promotes only with
  n ≥ 20 trades and avg ≥ 1.0 pt from the book avg in the claimed
  direction; a gate then faces the same EXP-0050-style bar as A1
  (including the random-skip placebo where it drops trades). Picking
  different cells after seeing the stratum P&L = reject and log.
- **Data / plumbing.** Trades only; `aggressor_order_id` / `is_exec_start`
  are already in the parquet (only `research/features/sweeps.py` and the
  stop-run detector use them today).
- **Ship path.** Feature/gate on the existing MAT or CFADE module; no new
  module. Java only after a May pass.
- **Not allowed.** Mining all 16 cells for the best P&L; using
  post-trigger prints in the count/size window; redefining "large" from
  the discovery outcome split.

## A3 — §4.3 Cross-market large-print clustering

> **Outcome — EXP-0053 / H44 (2026-10-24): CLOSED, reject/park at
> Stage 1.** Design as frozen below (causal trailing-30 m p99, burst
> ≥ 2/5 s, MAT keep-`both`). Causality PASS; power PASS (both =
> 741/1,956 MAT signals, thresholds NQ ≥ 3 / ES ≥ 5). Stage 1 FAIL:
> gated avg **+1.13** vs +1.55, net retained 35 %, gated hold −87,
> maxDD 368 > 290, placebo p90 +2.95. Joined strata: MAT `both` 1.58
> ≈ book avg (not a separator; best cell `nq_only` 4.11, n=46, was
> not carried and must not be promoted); CFADE `both` 6.93 < 7.63.
> No May, no Java, no percentile/window/burst retune. Full detail:
> `results/EXP-0053/summary.md`, EXPERIMENT_LOG 2026-10-24 EXP-0053.

- **Claim.** Bursts of large (p99) prints in **both** markets mark
  institutional participation and change MAT/CFADE outcomes vs large
  prints in one market only or none. This extends the EXP-0044 Arm B idea
  (dual large take) off the dead IRT-level machinery onto proven triggers.
- **Construction (to freeze at preregistration).** Per market: session
  p99 print-size threshold (causal, per the EXP-0043 session-relative
  large-size precedent), count of prints ≥ p99 in the trailing 5 s at the
  trigger, stamped causally. Threshold percentile, window, and
  burst-count cut are **to-freeze** — not frozen here.
- **Trigger set.** Every frozen CFADE candidate and every frozen MAT trade
  on DIG4 23d, annotated with both markets' large-print counts.
- **First step.** Count first — Arm B's known failure was rarity
  (n ≤ 4/month at levels). Report triggers by both / NQ-only / ES-only /
  none strata with n; only strata with n ≥ 20 trades get a net/avg read.
- **Kill / promote template.** Promote only a stratum with n ≥ 20 trades
  and avg ≥ 1.0 pt from book avg in the claimed direction; gate form then
  faces the EXP-0050-style bar (incl. random-skip placebo p90). If no
  stratum reaches n ≥ 20, park at Stage 0 and log the counts.
- **Data / plumbing.** Trades only; print-size percentiles from the same
  parquet used by A2. Level machinery from EXP-0044 is **not** reused —
  triggers come from the frozen books, not from IRT levels.
- **Ship path.** Gate/sizing flag on MAT or CFADE. Java only after a
  May pass.
- **Not allowed.** Re-attaching the feature to IRT level touches (that
  family is closed); lowering the percentile to manufacture counts after
  seeing Stage 0; standalone large-print entries.

## A4 — §1.3 Claim A: BBO-imbalance agreement state

> **Outcome — EXP-0054 / H45 (2026-10-24): CLOSED, reject/park at the
> frozen matters bar.** Causality PASS (truncation: 238 event imbalances
> compared over 2 sessions, 0 diffs; chunked vs brute-force replay exact
> on both markets). Coverage was not the problem in the usual sense: all
> 23 DIG4 sessions had usable NQ+ES depth and all 2,088 MAT/CFADE
> triggers got a defined 5 s top-3 imbalance state. The claimed state
> itself was essentially absent: MAT triggers were `mixed` 1,949/1,956
> (agree_ask 5, disagree 2, agree_bid 0) and CFADE triggers were `mixed`
> 132/132. Joined to the exactly reproduced frozen books (MAT n=502,
> net +779.25; CFADE n=87, net +663.38), side-relative `agree_with`
> had **0 trades on both books**, so n / avg-lift / train-direction /
> hold-direction checks all fail. MAT `agree_against` n=2 (avg −18.5)
> is a two-trade cell, not a finding and not a substitute stratum. No
> May read, no window/depth/lean-cut retune, no Java. Report:
> `results/EXP-0054/`.

- **Claim.** Two-market BBO/top-3 imbalance *agreement* at a frozen
  trigger (both books leaning the same way) separates trigger outcomes,
  even though single-market imbalance alone failed as a 10-pt predictor
  (H15/EXP-0020 pressure, falsified). As a stratum this has never been
  read; its payoff is feeding the Tier B execution timer
  (`CORRELATED_IDEAS` §8 B1 / REVIEW §6.3).
- **Construction (to freeze at preregistration).** Rolling 1–5 s signed
  BBO/top-3 imbalance per market, normalised per market, classified at the
  trigger as agree-bid / agree-ask / disagree / neutral. Window, depth
  (BBO vs top-3), normalisation, and state cuts are **to-freeze** — not
  frozen here.
- **Trigger set.** Every frozen CFADE candidate and every frozen MAT trade
  on the sessions with NQ+ES depth (the depth subset of DIG4 23d);
  triggers on trades-only sessions are reported as uncovered, not dropped
  silently.
- **First step.** Coverage report (share of triggers with a defined state,
  n per state), then net/avg per state per book. Stratum only — there is
  no standalone-entry step for A4.
- **Kill / promote template.** A state "matters" only with n ≥ 20 trades
  and avg ≥ 1.0 pt from book avg, same direction on train and hold. Even
  then A4 does not ship alone: it promotes only as the state variable for
  the Tier B execution-timer test, which has its own kill bar (retain
  ≥ 80 % of trades, ≥ 1 tick/trade lift on train **and** May). No state
  reaching the bar = park, and the execution timer falls back to
  microprice-only per REVIEW §6.3.
- **Data / plumbing.** NQ+ES depth parquet; this is the only Tier A item
  needing depth, which is why it is last. Book-snapshot handling follows
  the EXP-0043/0045 depth-seed precedent (fold pre-window depth into the
  seed book before slicing).
- **Ship path.** None directly. Ship, if any, is the Tier B execution
  parameter on CFADE; A4 itself stays a research feature.
- **Not allowed.** Standalone imbalance entries in either direction
  (pressure or magnet as an entry — closed by EXP-0020, whose
  `dual_magnet` cell belongs to the legacy queue, not to A4); choosing
  the imbalance window from the stratum P&L; reading May for A4 alone.

---

## Tier A completion summary (2026-10-24)

Tier A asked whether a cross-market state read at the frozen MAT/CFADE
triggers could separate outcomes inside two books that already work.
Four preregistered answers, all negative:

| Item | Experiment | Frozen test | Result |
|---|---|---|---|
| A1 volume-ratio shock | EXP-0051 / H42 | Keep CFADE only at ratio z ≥ 1.0 | Shock stratum 14 candidates < 20; parked at Stage 0, no P&L |
| A2 aggressor count/size | EXP-0052 / H43 | Keep MAT only in few-large-both (FLB) | Gated avg +0.59 vs +1.55; placebo fail; CFADE MSQ cell empty |
| A3 large-print clustering | EXP-0053 / H44 | Keep MAT only when both markets burst | Gated avg +1.13 vs +1.55; hold/maxDD/placebo fail; `both` ≈ book avg in joined strata |
| A4 book-imbalance agreement | EXP-0054 / H45 | `agree_with` stratum on MAT + CFADE | State essentially absent at triggers (`mixed` 99.6 % of MAT, 100 % of CFADE); `agree_with` trades 0/0 |

**Pattern across Tier A.** The states are either rare exactly when the
books fire (A1, A4), or common precisely because they re-describe the
burst conditions the triggers already select for (A2, A3). None
separated outcomes inside the frozen books, none reached May, and
nothing ships: MAT and CFADE remain exactly as frozen before Tier A.

**What Tier A bought.** Four dead ends are now closed with counts and
causal plumbing rather than left as plausible-sounding filters, and the
surviving queue is execution/portfolio work (Tier B), where the known
CFADE bottleneck actually is (fill quality at 2-tick slip). Tier B B1
may still be preregistered, but after A4 it is a **microprice-only**
entry-timer study under its own kill bar (retain ≥ 80 % of trades,
≥ 1 tick/trade lift on train and May) — the A4 agreement state is not
an approved timer variable. Do not reopen Tier A by loosening cuts,
windows, or carried cells on the DIG4 sample.

---

## Closing a Tier A item

Whatever the outcome, closing an item means the same four updates that
closed EXP-0049/0050: result box in `CORRELATED_IDEAS_2026-10-23.md` §8
(Tier A table), verdict in `docs/HYPOTHESES.md` (once it has a number),
dated entry in `docs/EXPERIMENT_LOG.md`, and the report under
`results/EXP-XXXX/`. The status board above is updated in the same commit.
An item that parks at Stage 0 (counts) is closed with its counts as the
result — it does not stay "open" to be quietly retried with looser cuts.
