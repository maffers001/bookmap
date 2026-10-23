# Research review and recommendations — 2026-10-23

> Scope: everything in `docs/EXPERIMENT_LOG.md`, `docs/HYPOTHESES.md` (H1–H37),
> `docs/EDGE_RANKING.md` (2026-10-21) and the EXP-0043→0046 series run this
> week. Numbers are NQ points, UK 14:30–17:00, 1-tick slip + 0.25 pt/RT unless
> stated. "May" = the 17 May-2026 sessions used as the untouched holdout.

## 1. Short answer

**If I had to trade one thing from this data tomorrow it would be the
correlated-exhaustion fade (CFADE, EXP-0036 DIG4) as the core book, with the
MAT momentum base as a trend-regime leg, switched by the 60-minute efficiency
ratio — one contract each, with the DIG4 kill switch. Nothing level- or
depth-based is ready to trade. The StackConsume-intact fade (H34) is the only
book-derived signal that survived a holdout and should run as a shadow/paper
book, not live.**

Three things matter more than any new signal idea:

1. **We are sample-starved.** 342 `.bmf` recordings exist (≈20–30 per month,
   2025-07 → 2026-10); **52 are converted**. Every "edge" and every "kill" in
   this project rests on 14–23 train days plus one 17-day holdout. At the
   effect sizes we see (avg +1 to +3 pt per trade, SD ≈ 20–25), that is
   underpowered by roughly an order of magnitude (section 5). Bulk conversion
   is the highest-value task in the repo.
2. **The one mechanism that keeps working is ES/NQ aggressor-tape (dis)agreement
   within a few seconds.** Every survivor (MAT, CFADE, and the only positive
   strata of EXP-0033/0014) is tape-flow. Every level, depth-imbalance,
   resting-order, LVN, VPOC and swing idea has failed or is ≤ placebo
   (H2, H7, H8, H11, H13–H16, H19–H21, H25, H26, H28, H31, H33, H35–H37).
   Future construction should start from tape and add book features as
   *confirmation*, not the other way round.
3. **Execution is the whole CFADE ballgame.** The frozen fade is +355 at 1 tick
   and train goes to ≈0 at 2 ticks. We have full MBO on both markets and have
   never used it to model fill probability of a passive entry. That is cheap,
   directly monetisable, and untouched.

## 2. What we have

| Asset | State |
|---|---|
| Recordings | 342 `.bmf`, NQ+ES, ~142 GB, 2024-09 → 2026-10; 2025-07 onward is dense (10–31 files/month) |
| Converted parquet | **52 sessions**: Mar 1, May 17, Aug 3, Sep 15, Oct 15 — all with trades + depth + MBO for **both NQ and ES** |
| Feature plumbing | tape bars, momentum bursts, cross-market matcher, LargeLevelTracker, MBO resolve (cancel → order map), book replay from seeded state, robust session baselines, IRT level loader with roll/basis handling, placebo-level machinery, bracket P&L sim, session-cluster bootstrap |
| Live add-ons | MAT (vol k=1, trend+VWAP), CFADE autotrade (frozen, 132/132 parity), HV-extreme visual, Stop-run visual |
| Methodology | points not ticks; causal/confirm stamps; null + MAT-P&L yardstick for fades; pre-registered freeze then one OOS read; `docs/RESEARCH_RULES.md` |

The infrastructure is in good shape. The bottleneck is data volume and
statistical power, not tooling.

## 3. Scorecard — what the evidence actually says

### 3.1 Works (holdout-confirmed, same sign)

| Idea | Train | Holdout | Read |
|---|---|---|---|
| **CFADE** against_m04 + ER60 ≤ 0.30, vol RR SL1/TP2 | 23d +355 (train +6 / hold +123 / new +226), LOO_neg 0 | **May n=95 net +335**, 10/17 days+, maxDD 175 | Only idea that made money on untouched May in the regime where MAT lost. Shorts carried Sep–Oct; May side split still unread. Two May days (−153, −145) are the entire drawdown; four days are +592 of +335. |
| **MAT** vol k=1 trend+VWAP | 23d ≈ +750, new-9 PF 1.22 | **May −264**, 7/17 days+ | Real but regime-dependent; died in rotational May. `skip_range175` softens (−186) but does not flip. Trend leg only. |

### 3.2 Soft confirm (one holdout read, caveats)

| Idea | Numbers | Caveat |
|---|---|---|
| **StackConsume-intact fade** (H34): IRT touch, undecided at +2 s, beyond-stack not eaten → fade TP30/SL15 | Oct +406 / Sep +142 / **May +160** (n 192, 8/17, maxDD 277); positive in 12/12 confirm-time × month cells | Gate chosen after seeing Oct/Sep; May level-specificity (δ 0.23) = placebo; AMB/BREAK false fades lose in every month and no filter cuts them OOS (EXP-0043_CONFIRM_DIG, EXP-0044). Edge is "stop-side liquidity held for 2 s", which is a tape/consumption read, not a book-shape read. |

### 3.3 Promising but never properly tested (dig backlog, unchanged since 2026-10-19)

- **EXP-0014** standalone CD-pullback × momentum: best corrected path of any
  event set (n=49, hit10 0.55, @60s +7.8) — **EXP-0014_PNL (2026-10-23) reject/park**:
  Sep+Oct n=89 net −110.6; bar FAIL; May not read.
- **EXP-0017** aggressor sweeps: beat null 52% vs 43% pre-fix; killed only vs
  the look-ahead 85% comparator — **EXP-0017_REOPEN (2026-10-23) reject/park**:
  Stage-1 FAIL (Sep pass / Oct miss); dual_q n=3672 hit10 0.45; no Stage 2 / May.
- **EXP-0033** high-vol simultaneous flow stratum as MAT overlay.
- **EDGE_RANKING §A** combined MAT+CFADE book / regime allocator — proposed,
  not run. `results/` has no combined-book artefact.
- **EDGE_RANKING §F** CFADE execution — proposed, not run.

### 3.4 Dead or ≤ placebo (do not reopen in the same construction)

Depth imbalance, absorption on 100 ms bars, LVN/VPOC/HVN families, swing-break
and bounce, trapped peak, range-bar imbalance, rotation gate, BB regime, dual
IRT S/R reclaim (look-ahead once removed), pre-touch orderflow at IRT levels,
longer pre-window / approach-leg book, dwell-duration hold claim, NQ and ES
open-book standout resting clusters. The common failure: **chart-visible
liquidity structure does not predict the next 60–120 s on NQ once you control
for placebo levels and causal stamps.**

### 3.5 Visual-only

HV extreme retest (parameter sign flips across holdouts) and Stop-run
fingerprint (≈ null continuation). Shadow on fresh sessions; no auto-trade.

## 4. The strategy I would trade from this data

**A two-leg regime portfolio, one contract per leg, both frozen as-is.**

| Leg | Fires when | Rule (frozen) | Role |
|---|---|---|---|
| CFADE | ER60 ≤ 0.30 (rotational) | against_m04 fade, vol RR SL1/TP2 (clip 4–40), 5 s cooldown, flat-only | Core. Only May-positive book. |
| MAT | ER60 > 0.30 (trending) | vol k=1 symmetric, trend+VWAP gate, causal ES | Trend leg. Its May loss was in exactly the regime CFADE's gate excludes. |

Risk: DIG4 kill (−150 day / 3 losing sessions) per leg; 17:00 flatten. Size
stays at 1 lot until the pre-registered checks in section 6.2 pass.

**Why this and not something cleverer:** it is the only combination where every
component has (a) a causal implementation already paired to live Bookmap bars,
(b) a frozen spec, (c) an untouched-holdout read, and (d) *opposite* regime
appetite so the combination is diversifying by construction rather than by
fitting. DIG3 §6 already showed the books are additive on 23d (MAT+fade +1,259
vs MAT +779). What has **not** been done is the correlation / drawdown study on
all 40 sessions and an allocator comparison — that is the first job below.

**What I would not trade:** anything from the level family, including the
StackConsume fade, until it has ≥ 2 more untouched months and the AMB-dilution
problem is addressed in a pre-registered way.

## 5. Why we keep finding "soft" results — statistical power

A rough but honest calculation. Per-trade P&L SD for our 2:1 and symmetric vol
brackets is ≈ 20–25 pt. To detect a true mean of **+2 pt/trade** at 80 % power,
one-sided α = 0.05:

\[
n \approx \left(\frac{(1.64 + 0.84)\,\sigma}{\mu}\right)^2 = \left(\frac{2.48 \times 22}{2}\right)^2 \approx 750 \text{ trades}
\]

Our holdout reads have n = 95 (CFADE May), 192 (StackConsume May), 38 (dwell
Arm A). At n = 192 the standard error of the mean is ≈ 1.6 pt — i.e. we cannot
distinguish +0.8 from +3 or from −1.5. This is why every result is a "soft
confirm", why second filters never clear 1 SE, and why month-to-month sign
flips are common. **It is not that the ideas are marginal; it is that we
cannot see them.** Converting the remaining ~290 sessions turns 1 holdout month
into 12–14 and raises n by ~6–8× on trades-only signals (MAT, CFADE, 0014,
0017) immediately.

Also relevant: session-cluster bootstrap CIs are already in the code; with 40
sessions the CI90 on cliffs δ is ± 0.2, which is wider than every pre-touch
effect we measured. Those "nulls" are consistent with small effects we cannot
power.

## 6. Recommended work, ranked

Each item has a pre-registered kill bar so it ends after one honest read.

### 6.1 P0 — Bulk conversion and walk-forward (infrastructure, highest EV)

- Convert **trades-only** for every `.bmf` from 2025-07 onward (~280 sessions;
  trades parquet is cheap). Then depth+MBO for a stratified subset (e.g. every
  third session) so book-dependent ideas get ≥ 100 days.
- Re-run frozen MAT, frozen CFADE and the combined book as a **monthly
  walk-forward** (each month read once, no retune). Report per-month net,
  days+, maxDD, side split, ToD, ER-regime split.
- Kill bars: CFADE positive in ≥ 60 % of months and no month < −400; combined
  book maxDD < 1.5× the better single leg. If CFADE fails this, nothing else in
  the project is ready and we go back to tape-feature research with the bigger
  sample.
- Watch for: contract-roll weeks (basis handled for IRT, but check MAT/CFADE
  vol brackets across roll), dual-alias extracts (TV + Rithmic — keep primary
  only, as the May fix), feed gaps at the open.

### 6.2 P1 — Combined book + regime allocator (EDGE_RANKING §A; zero new signal risk)

- Daily P&L correlation MAT vs CFADE over all converted sessions; combined
  equity; three allocators compared: (a) both always-on, (b) ER60 switch,
  (c) ER60 switch with `skip_range175` on the MAT leg.
- Also read once: CFADE May **side split** (shorts carried Sep–Oct) and
  **wait_500_2000 / after_mat_sl / low-burst-ES-vol** strata (DIG3, not frozen).
- Kill bar: allocator (b) or (c) must beat (a) on maxDD *and* days+ on ≥ 2 of
  3 months, else ship (a).

### 6.3 P2 — CFADE execution model from MBO (EDGE_RANKING §F; new construction)

The edge lives or dies on fill quality, and we have the data to model it.

- For each frozen CFADE event, reconstruct the NQ book at the trigger from MBO
  and compute: queue ahead of a hypothetical passive order at the fade price,
  volume that traded through that price within 250/500/1000 ms, whether the
  level was fully consumed (fill) or price left (no fill).
- Compare three entry styles on the same event set: marketable at 1 tick
  (baseline), passive join at trigger price with 1 s timeout then market,
  passive with queue-position-aware timeout.
- Also test a **microprice / BBO-imbalance entry timer**: delay the market
  order up to 500 ms until BBO imbalance favours the fade side. This is the
  one use where classic queue-imbalance (which failed as a 10-pt predictor)
  should help: it predicts the next tick, which is all slippage is.
- Kill bar: passive variant must retain ≥ 80 % of trades and lift net ≥ 1 tick
  per trade on train *and* May.

### 6.4 P3 — Dig backlog, in order (cheap; existing plumbing)

1. ~~**EXP-0014 standalone CD-pullback × momentum**~~ — **done reject/park**
   (`results/EXP-0014_PNL/`, 2026-10-23).
2. ~~**EXP-0017 sweeps**~~ — **done reject/park** (`results/EXP-0017_REOPEN/`,
   2026-10-23).
3. **EXP-0033 high-vol stratum** as MAT sizing overlay.
4. dual_magnet one-shot (lottery ticket; lowest).

### 6.5 P4 — Cumulative tape divergence (EDGE_RANKING §D; the "dose" version of CFADE)

CFADE triggers on one 100 ms ES bar against. The slower cousin: rolling
5–30 s cumulative signed aggressor imbalance per market, normalised per market.
Fire when NQ cumulative keeps pushing while ES cumulative is flat/against over
≥ 5 s, *or* both push but NQ price progress per unit imbalance collapses
(effort/result at the right horizon — H1 died at 100 ms on ε-spikes, never
tested at 5–30 s). Higher n than CFADE, same mechanism. Kill bar: beats null
and ≥ 50 % of CFADE's avg on train before any OOS read.

### 6.6 P5 — StackConsume fade: forward paper + two honest follow-ups

- Forward paper on every newly converted depth session (no retune).
- **Progress time-stop** (untested on this book): exit at +20/30 s if MFE
  < 5 pt. Rationale: AMB losers are the trades that never go; MAT exits study
  does not transfer because MAT is a continuation book.
- **ES-context gate at +2 s**: ES aggressor imbalance not pushing the break
  side over the same 2 s. The entire project says ES tape agreement is the
  thing that generalises; this is the one filter family not yet tried on H34.
- Kill bar: either filter must improve May avg by ≥ 1 SE *and* cut maxDD; else
  the fade stays shadow-only.

### 6.7 P6 — Hidden/iceberg liquidity from MBO (new; closest to the discretionary "level survives")

Every absorption test used **displayed** size. MBO lets us detect the real
thing: executions at a price that exceed displayed size with the *same*
order_id reloading, or sustained executed-into ≫ visible with sub-tick
penetration over seconds. Build an iceberg detector (reload count, reload
size, elapsed), validate visually on 3 sessions, then: does an active iceberg
on the hold side at an MAT/CFADE trigger change outcomes? Treat as a
*feature on existing triggers*, not a standalone level fade (that family is
dead). Kill bar: δ ≥ 0.2 on train with CI90 excluding 0, else drop.

### 6.8 P7 — Execute on ES instead of NQ for thin edges (new)

ES tick = $12.50 on ~$300k notional vs NQ $5 on ~$450k; 1-tick slip is ~4×
smaller in return terms. Edges that die at 2-tick NQ slip (CFADE train) may
survive on ES. EXP-0046 showed ES *levels* fail, but the ES *tape* leg of CFADE
was mildly positive at @60s (DIG1). Test: same CFADE/MAT triggers, bracket on
ES in ES-scaled vol units. Kill bar: positive on train and May after ES costs.

### 6.9 P8 — Pre-open day-type prior (new; cheap)

ER60 is intraday and needs an hour. Overnight range, gap vs prior close,
Asia/London range, and prior-day ER are known at 14:30. Test as a *prior* for
the allocator in 6.2 (start the day on the leg the prior favours). Kill bar:
improves days+ of the combined book without reducing net.

## 7. Things we have not considered or have under-used

1. **Power and sample size** (section 5). No experiment has stated a target n
   or minimum detectable effect before running. Add both to the YAML template.
2. **The 290 unconverted recordings.** Research has treated "a month" as the
   unit of OOS when 14 months exist.
3. **Execution as research** (6.3). All P&L is marketable-at-1-tick; MBO is
   only used for feature cancels/adds.
4. **Aggressive-order counts and average aggressive size** (handoff §2.2–2.4).
   `aggressor_order_id` / `is_exec_start` are in the parquet; only
   `features/sweeps.py` and stop-run use them. "Few large aggressors vs many
   small" was a founding idea and has never been a feature in a surviving
   book.
5. **Iceberg / reload detection via order_id** (6.7).
6. **Label scale vs trading scale.** Hold/break labels use 30-pt impulse,
   30-pt reject/extension and a 120 s horizon; the trader scalps 10–20 pt.
   The AMBIGUOUS third of every universe is partly a label-scale artefact.
   Try a 15/15 label set alongside 30/30 and see which one the fade P&L tracks.
7. **Progress-based exits and scale-ins** on fade books (6.6). Only brackets
   and MAT-style trails have been tested.
8. **Second touch vs first touch.** Every level test fades the *first* touch.
   Discretionary reading often waits for the retest. EXP-0045/0046 retest arms
   were thin (n ≤ 21) — underpowered, not falsified.
9. **ES/NQ beta regime.** Correlation between ES and NQ 1-s returns drifts
   (sector rotation days). CFADE assumes the two should agree; a low-correlation
   regime should be a skip. Cheap rolling-correlation gate, pre-register.
10. **Loss clustering / daily stop design.** CFADE May: 2 days = all drawdown;
    4 days = 177 % of net. Both losing days had 9 trades at WR 0.22. A
    "stop after 3 consecutive losers" rule is a different object from the −150
    day kill and has not been tested.
11. **Calendar effects**: FOMC days, CPI/NFP 13:30 UK releases 1 h before our
    window, roll weeks, half days, month-end. Never stratified.
12. **ES as execution venue** (6.8).
13. **Day-type prior** (6.9).
14. **Multi-level consumption sequence** rather than one band: the StackConsume
    read is "was the first wall eaten in 2 s". A sweep *through* two walls vs a
    stall at the first is a different, tape-driven object (ties to EXP-0017).
15. **Evening session (17:00–21:00 UK)**: MAT ToD study showed weak close; CFADE
    and the fade books never looked outside 14:30–17:00. Rotational regimes
    are more common after the first hour — possibly CFADE's natural habitat.

## 8. Stop doing

- No more first-touch level fades under new level definitions (IRT, open book,
  swings, LVN). Seven constructions, zero survivors vs placebo.
- No second filters chosen on the holdout month, and no "Δavg < 1 SE"
  adoptions.
- No parameter sweeps on 14–23 day samples; the sample cannot rank neighbours.
- No new experiment without a stated target n / MDE and a kill bar in the YAML.

## 9. 30-day plan

| Week | Work |
|---|---|
| 1 | Bulk trades-only conversion (2025-07 →); walk-forward of frozen MAT, CFADE; combined-book correlation + allocators (6.1, 6.2). CFADE May side split and DIG3 strata read once. |
| 2 | CFADE execution model from MBO + microprice entry timer (6.3). Decide CFADE size (still 1 lot unless walk-forward clears). |
| 3 | Dig backlog 0014 → 0017 → 0033 on the full sample (6.4). Depth+MBO conversion for the stratified subset in background. |
| 4 | Cumulative tape divergence (6.5); StackConsume forward paper with ES-context gate and progress stop (6.6). Iceberg detector prototype if time (6.7). |

Deliverable at day 30: one portfolio spec (MAT + CFADE + allocator) with
walk-forward evidence across ≥ 10 months, an execution recommendation for the
fade leg, and a ranked, powered list of what to build next.
