# Correlated ES/NQ orderflow ideas — candidate list (2026-10-23)

> Companion to `RESEARCH_REVIEW_2026-10-23.md` §6.5 and `EDGE_RANKING.md` §D.
> Purpose: enumerate the cross-market constructions we have **not** tested, so
> the next correlated experiment is chosen from a full menu rather than the
> first idea to hand. Each idea has a construction, the null it must beat, the
> data it needs and a kill bar. Nothing here is a result.

## 0. What the correlated family has already told us

| Done | Construction | Verdict | Lesson |
|---|---|---|---|
| H4 / MAT (EXP-0008) | NQ tape burst + ES same-side burst within a sync window | **Works** (regime-dependent; May −264) | Simultaneous *aggressor* agreement over ~100 ms–1 s is informative for continuation. |
| H30 / CFADE (EXP-0036) | NQ burst, then one ES bar against while NQ still pushing | **Works** (May +335) | Short-horizon *disagreement* after agreement is a fade. Thin ES on the burst is the best stratum. |
| H18 (EXP-0024) | Signed ES→NQ lag < 250 ms as a filter on MAT | Null in points | Who-prints-first at the 100 ms scale carries nothing once units are points. |
| H22 (EXP-0028) | ES impulse while NQ quiet → NQ catch-up | Null | Price lead/lag at tick scale does not survive. |
| H23 (EXP-0029) | Same-window co-move of ES and NQ *price* | Falsified | Correlated **price** is not the edge; correlated **tape** is. |
| H28 (EXP-0035) | Correlated absorption on 100/250 ms bars at NQ large levels | Reject | Single-bar absorption with ES confirming ≈ NQ-only ≈ null. |
| H31 (EXP-0038 exits) | ES going quiet/against → early MAT exit | Reject | Disagreement is an *entry* signal (CFADE), not an exit signal for a continuation book. |
| H35 Arm B (EXP-0044) | NQ-or-ES large take toward hold side as dwell trigger | Thin / not selected | ES large prints alone, at IRT levels, too rare to read. |
| H37 (EXP-0046) | ES open-book standout levels, traded on ES | Reject | ES resting-order *structure* is as dead as NQ's. |

Three structural facts the new ideas should respect:

1. **Everything that survived used aggressor tape on both legs, at 100 ms–2 s
   horizons, with NQ as the traded leg.** Book-shape and price-lead constructions
   have failed every time.
2. **We have never used the ES book.** `depth` and `mbo` parquet carry the ES
   role on all 52 sessions; the only ES features so far are tape bars and p99
   prints. The whole "correlated book change" family in §1 is new ground.
3. **Disagreement has been tested at one bar only.** CFADE's ES-against is a
   single 100 ms bar. Multi-second cumulative disagreement (§2.1) is the
   obvious dose-response extension and is still untested.

## 1. Correlated **book** change (the user's example: both books lean the same way before an imbalance)

The hypothesis class: when liquidity *moves* in both books in the same
direction at the same time, it is more likely to be informed (or a shared
algo) than when one book moves alone. Spoofing is usually single-market.

### 1.1 Co-pull — simultaneous near-touch cancellation on the same side

- **Event:** within a 500 ms–2 s window, NQ and ES both lose ≥ X % of resting
  size within k ticks of the BBO on the *same* side (bid pulls in both), with
  no executions explaining the drop (MBO cancel-resolved, not consumed).
- **Claim:** price moves toward the pulled side over 10–60 s more often than
  single-market pulls or placebo windows.
- **Null:** NQ-only pull of the same magnitude; random windows matched on time
  of day and vol.
- **Data:** NQ + ES MBO (have). Baseline per market, per minute-of-day
  (`robust session baselines` plumbing).
- **Kill:** dual-pull hit10 must exceed NQ-only pull by ≥ 5 pp on train with
  n ≥ 200, else drop.

### 1.2 Co-stack — simultaneous same-side adds that are not immediately hit

- Mirror of 1.1: both books add ≥ X % to one side within the window and the
  adds survive ≥ 2 s. Claim: *support/resistance by agreement* — price does not
  trade through that side within 30–60 s. This is the book-side analogue of the
  StackConsume-intact read, but defined by **co-ordinated arrival** rather than
  by an IRT level, so the dead level machinery is not involved.
- Null: NQ-only stack of the same size.

### 1.3 Correlated BBO-imbalance regime

- Rolling 1–5 s signed BBO/top-3 imbalance per market, normalised per market.
  States: agree-bid, agree-ask, disagree, neutral.
- Claim A (filter): MAT / CFADE outcomes stratified by state at trigger.
  Imbalance *alone* failed as a 10-pt predictor (H2); as a **two-market
  agreement state** on an existing trigger it has not been read.
- Claim B (execution): imbalance agreement predicts the next tick well enough
  to time CFADE entries (see REVIEW §6.3). This is the most likely payoff.

### 1.4 Cross-market book-pressure divergence

- NQ book leaning bid (imbalance > +θ) while ES book leans ask (< −θ) for
  ≥ 2 s, both normalised. Claim: the NQ lean is a spoof/trap — fade it. Null:
  NQ lean with ES neutral.
- Reason to test: single-market book leans are routinely faked; a fake is
  cheaper to run in one book than two.

### 1.5 Correlated iceberg / reload

- Iceberg detector per market from MBO reloads (REVIEW §6.7). Claim: an active
  iceberg on the same side in *both* markets at the same price-equivalent
  (basis-adjusted, §3.3) is a much stronger hold than one. Rare; count first.

### 1.6 Correlated large-order lifecycle

- Extend `LargeLevelTracker` to ES. For each NQ large resting order: does an ES
  large order appear/pull/trade within ±N s and on the same side? Build a
  "paired large order" event. Claim: paired orders behave (hold / get hit) more
  predictably than unpaired. Null: unpaired NQ large orders of the same size
  percentile.

## 2. Correlated **tape** (extensions of the two things that work)

### 2.1 Cumulative divergence over 5–30 s (dose-response CFADE)

- Rolling signed aggressor imbalance per market, z-scored per market per
  minute-of-day. Divergence D(t) = z_NQ − z_ES.
- Claim: P(fade works) increases monotonically with |D| held over ≥ T s while
  NQ price keeps making progress. CFADE is the T ≈ 0.1 s, one-bar corner.
- Null: same |D| but NQ price *not* progressing (no trap to fade).
- Kill: monotone in |D| and T on train, and ≥ 50 % of CFADE avg before any
  OOS read.

### 2.2 Effort/result divergence across markets

- H1 (NQ effort/result at 100 ms) died. Cross-market version: over 5–30 s, ES
  aggressor volume toward direction d is high and ES price moves; NQ aggressor
  volume toward d is also high but NQ price does **not** move (absorbed).
  Claim: NQ is being absorbed against a correlated push → fade NQ once ES
  stalls, or trade NQ catch-up if ES keeps going (test both; pre-register the
  split by ES continuation).

### 2.3 Aggressor-count vs aggressor-size agreement

- Per market, per 1 s: number of distinct aggressive orders
  (`aggressor_order_id`) and mean aggressive size. Four states: few-large,
  many-small, per market → 16 cells.
- Claim: "few-large in both" is institutional and continues; "many-small NQ,
  nothing ES" is retail/algo chop and fades. Founding idea in the handoff,
  never a feature.

### 2.4 Correlated sweep (multi-level take) — EXP-0017 made dual

- Sweep detector exists for NQ. Add ES. Event: NQ sweeps ≥ L levels and ES
  sweeps ≥ L' levels same side within 1 s. Claim: dual sweeps continue; single
  sweeps revert. Null: NQ-only sweep. Cheap — plumbing exists.

### 2.5 Correlated absorption over seconds, not bars (H28 redo)

- H28 used 100/250 ms bars and was rejected with the explicit note that a
  multi-second construction was the only sensible revisit. Event: ≥ 3 s of
  NQ aggressor volume into one price band with ≤ 1 tick of progress **and** ES
  aggressor volume in the same direction also stalls. Trade the reversal.
  Null: NQ-only absorption of the same duration.

### 2.6 Flow inflection synchrony (derivative of cumulative delta)

- Smooth per-market cumulative delta (EMA 2–5 s); event when the slope changes
  sign. Claim: a sign change that happens in both markets within 1 s is a turn;
  in one only it is noise. Measure continuation of the new slope over 30–60 s.
  Null: single-market inflections.

### 2.7 Volume-ratio shock

- Rolling 10 s ratio of NQ aggressor volume to ES aggressor volume, z-scored
  by minute-of-day. Spikes = one market is being traded idiosyncratically.
  Claim: an NQ volume spike with a flat ratio (ES also spiking) is a market-wide
  event and continues; a spike with ratio ≫ 1 is NQ-specific and reverts.
  Relates to CFADE's "low burst ES vol" stratum (+427 vs −78), which is the
  first hint this ratio matters.

## 3. Cross-market **price/basis** microstructure (new axis)

### 3.1 Synthetic basis (NQ − β·ES) dislocation

- β from rolling 30–60 min regression of 1 s returns. Residual
  r(t) = ΔNQ − β·ΔES over 1–5 s, z-scored.
- Claim: a large residual that is **not** accompanied by NQ aggressor flow in
  that direction mean-reverts (price moved without tape → liquidity pull, not
  buying). A large residual **with** flow continues (idiosyncratic information).
  This separates "NQ moved because someone bought" from "NQ moved because the
  book thinned", using ES as the control. Null: unconditional residual
  reversion.
- This is the only idea on the list that would also give a *continuous* signal
  for the live add-on rather than discrete events.

### 3.2 Correlation-regime gate

- Rolling 60 s / 5 min correlation of 1 s returns. CFADE and MAT both assume
  the markets are coupled. Claim: both books are better when ρ > 0.6 and
  should skip when ρ < 0.3 (sector-rotation days). Pre-register the thresholds
  on Oct/Sep; read May once. Trivial cost; applies to every cross-market idea.

### 3.3 Price-equivalence mapping for level ideas

- Any "same level in both markets" idea (1.5, 1.6) needs a map from ES price
  to NQ price. Use the rolling β and intercept from 3.1, not a fixed ratio.
  Record the mapping error distribution before using it.

## 4. Correlated **state** / regime ideas

### 4.1 Which market is "in charge" today

- Daily or hourly: Granger-style sign test of whether ES 1 s imbalance predicts
  NQ 1 s return better than the reverse. H18 showed millisecond lead/lag is
  noise; a *slow* lead state (which market the big players are using today) is
  a different question. Use as an allocator feature: MAT with ES-confirm vs
  NQ-first depending on state.

### 4.2 Divergence *persistence*

- How long does a cumulative divergence (2.1) usually last before resolving,
  and which market gives in? If ES typically wins, the resolution is a known
  trade; if it is 50/50, divergence is a volatility signal, not a direction.
  Pure descriptive study; informs 2.1's exit design.

### 4.3 Cross-market large-print clustering

- Count p99 prints in both markets per 5 s. Bursts of large prints in both →
  institutional participation; one only → single-market. Stratify MAT/CFADE
  outcomes by the two-market large-print count. Extends the Arm B idea from
  EXP-0044 off the dead level machinery.

## 5. Suggested order

Ranked by (prior from what has worked) × (cheapness given plumbing) ÷ (how
many prior failures the construction resembles).

| # | Idea | Why first | Needs |
|---|---|---|---|
| 1 | 2.1 cumulative divergence | Direct extension of CFADE; trades-only; high n | ES+NQ trades (have) |
| 2 | 3.2 correlation-regime gate | Cheap filter on both live books; applies everywhere | trades |
| 3 | 1.3 correlated BBO-imbalance as **execution timer** for CFADE | Monetises an existing edge; imbalance is good at next-tick | NQ+ES depth |
| 4 | 2.7 volume-ratio shock | CFADE's best stratum already points here | trades |
| 5 | 1.1 / 1.2 co-pull / co-stack | The user's hypothesis; first use of ES MBO; spoof-resistance argument is sound | ES+NQ MBO |
| 6 | 2.3 aggressor count/size cells | Founding idea, columns exist | trades |
| 7 | 2.4 dual sweep | EXP-0017 plumbing exists | trades |
| 8 | 3.1 basis dislocation ± flow | New axis; continuous signal | trades |
| 9 | 2.5 multi-second correlated absorption | H28's stated revisit | depth+trades |
| 10 | 1.6 / 1.5 paired large orders / icebergs | Rare, needs 3.3; count before building | MBO |

Rules for all of them (per `RESEARCH_RULES.md`): points not ticks; confirm
stamp for research, causal for anything headed to the add-on; **NQ-only
version of the same event is the mandatory null** — a correlated idea earns
its keep only by beating its own single-market twin; state target n and
minimum detectable effect in the YAML; freeze on Oct+Sep, read May once.
