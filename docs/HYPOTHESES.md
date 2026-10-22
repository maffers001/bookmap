# Hypotheses

## H1 — Failure to progress (effort/result)

When trend-side aggressive volume rises while price progress in that direction collapses, short-horizon reversal (or mean-reversion) in NQ is more likely than baseline.

**Test:** EXP-0002

## H2 — Absorption / replenishment

High executed volume into a large resting level with low penetration and/or replenishment predicts price moving away from that level.

**Test:** EXP-0005 / EXP-0006

## H3 — Attract / repel / dwell

Unusually large near-price liquidity that attracts then absorbs (or dwells without trading then supports) has distinct forward return distributions from liquidity that withdraws.

**Test:** EXP-0004 / EXP-0007

## H4 — ES/NQ confirmation / momentum

NQ reversal/momentum precursors perform better when ES shows the same flow state nearby in time, vs NQ-only. Discretionary momentum: large orders taken on both ES and NQ with fast tape.

**Test:** EXP-0008 (tape burst + ES sync). First result on 5 days: confirmed hit+10 ≈82% vs null ≈49% / NQ-only ≈67%. Needs more days + depth-confirmed large takes.

## H5 — Composite reversal

Exhaustion + absorb + ES agree jointly improves NQ short-horizon outcomes vs any single component.

**Test:** EXP-0010

## H6 — CD with-the-flow gate

Restricting events to CD MA direction (22-tick range CD with 100 SMA / Bollinger) improves expectancy. Discretionary use: trend as filter, time entries on CD pullback to MA.

**Test:** EXP-0009 (post-filter on existing events). First result: with-flow hurts H2 absorb-fade; against-trend looks better. Pullback∩OF rare — next design should emit CD pullbacks as primary events.

**Follow-up EXP-0014:** Pre-fix primary weak on hit; ×mom path strong but hit loses to 0008 → demoted to gate. Points re-run (causal): n=49 hit10 **0.55** / @60s **+7.8**. MAT *gate* new cohort fails (−182) — do not ship as filter. **Reopen dig:** standalone CD×mom entries under MAT vol-k=1 P&L (not vs 0008 hit%).

## H7 — Dual-market LVN rejection

When NQ and ES both reach a same-side LVN zone within a short sync window, short-horizon NQ reversal away from that zone beats (a) random RTH timestamps and (b) NQ-only LVN touches.

**Test:** EXP-0011. **Result (5 days):** falsified on weak LVN def — dual hit+10 ≈39% ≈ NQ-only ≈40% ≈ null ≈38%. No Bookmap ship. See **H7b** for tightened retest.

## H7b — Dual LVN with HVN sandwich + min volume

Same dual-touch fade as H7, but LVNs must: (1) sit in a lookback profile with
**min traded volume**, (2) have an **HVN above and below** (sandwich; valley
low vs both peaks), (3) **not** sit within ``edge_margin_ticks`` of the
profile high/low (avoids high-of-day / edge artifacts). One dig on lookback
(20/30/45 min) and ``lvn_frac_of_peak`` (0.25/0.35/0.40).

**Test:** EXP-0023. **Result:** Dual almost empty under sandwich gates; NQ-only hit+10 ~31–44% ≈ null across lookback/frac dig. Tightening the LVN definition does not rescue H7 on these days. No ship.

## H8 — Liquidity flip at LVN

Given an H7 touch, outcomes improve when: (1) large resting size just beyond the LVN has low execute-into, and (2) soon after, large resting size on the reverse side of price begins trading (support → fuel).

**Test:** EXP-0012. **Result:** gated set empty (flip=0); beyond-only n=3 one-session cluster — not validated.

## H9 — Correlated dwell / repel

Large near-BBO size that dwells without trading on both ES and NQ predicts a move away; withdraw-without-trade predicts continuation.

## H10 — CD Bollinger-width regime

Wide CD Bollinger bands (high rolling width percentile) mark a **momentum** regime; narrow bands mark a **reversal** regime. Momentum precursors (EXP-0008) should lift in wide regimes; LVN / large-resting challenge-without-trade fades should lift in narrow regimes.

**Test:** EXP-0015 / EXP-0016. **Result:** H10 as momentum filter falsified (wide≈narrow≈all ~85% on lag&lt;250). Narrow∩dual-challenge empty; dual LVN×narrow n=8 too small.

## H11 — Dual-market challenge-without-trade

Large resting size near BBO on **both** ES and NQ is challenged (price approaches) but barely trades (`executed_into / size` low). Fade away from the level. Optionally co-located with an LVN. Stronger in narrow CD-BB regimes (H10).

**Test:** EXP-0016. **Result:** dual fade hit+10 21% (n=14) — fails; continuation flip also fails. No ship.

## H12 — Same-aggressor sweep momentum

A single `aggressor_order_id` that prints ≥min_prints with ≥min_vol and price span ≥min_span_ticks is a “without hesitation” sweep. When NQ and ES both produce a same-side sweep within a short sync window (prefer |ES lag| &lt; 250ms), short-horizon NQ continuation beats random null, NQ-only sweeps, and EXP-0008 100ms bar bursts.

**Test:** EXP-0017. **Result (pre-fix):** dual hit+10 52% vs null 43% vs look-ahead 0008 85% — killed for ≪0008 while beating null. **Reopen:** points event study then conditional MAT P&L; 0008 is a continuation overlay bar, not an auto-reject when null is beaten.

## H13 — Correlated near-BBO MBO vacuum

In a short window, MBO cancel count ≫ send count within tick_radius of BBO on the same side (ask for longs / bid for shorts), on both ES and NQ, while same-side aggression is active, predicts continuation into that vacuum.

**Test:** EXP-0018. **Result:** dual hit+10 40% ≈ null 41% (n=5245). Falsified. No ship.

## H14 — Leave value (VPOC / HVN departure)

Port from trade-processor 22-tick research (**S20 VPOC departure**, **S4 HVN break-hold**): price was at developing fair value (POC/HVN), then holds away with clearance and a large CD impulse in the departure direction — initiative leaving value, not a fade back to the magnet. Optional CD-MA regime + price confirm. Continuation on NQ; optionally tighten with ES+NQ tape sync.

**Test:** EXP-0019. **Result:** S20 hit+10 40% / S4 36% ≤ null ≈46% ≪ EXP-0008 85% on 5-day scalp horizons. Trade-processor edge does not transfer to this MFE/hit+10 yardstick. No ship.

## H15 — Near-book depth imbalance

Within X ticks of mid, compare resting ask size (above) vs bid size (below):
`imbalance = (ask_near − bid_near) / (ask_near + bid_near)`. Extreme imbalance
(|imb| ≥ thr) predicts short-horizon NQ moves. **Pressure:** ask-heavy → short /
bid-heavy → long. **Magnet (alt):** fade into the heavy side. Dual ES+NQ same-side
confirm should lift vs NQ-only and vs null.

**Test:** EXP-0020. **Result:** NQ-only **pressure** hit+10 ≈0.43 ≈ null — falsified. Points dual_magnet n=20 hit10 **0.70** / @10s **+6.2** — tiny, never MAT P&L'd; one-shot dig only (low priority).

## H16 — Dual micro-regime (attract→take + opposite hold)

Discretionary composite from project notes: micro bullish when **large offers
attract and get traded into** (aggression through ASK) **and** **large bids
dwell/hold with little trade** — same read on ES and NQ within a short sync.
Bearish is the mirror (BID taken through + ASK hold). Distinct from H2 fade
absorption and from H11 challenge-alone.

**Test:** EXP-0021. **Result:** Dual composite empty (n=0) even after one soft-gate dig. Dual take-only ≈ null; dual hold-only weak. Empty/falsified on these days. No ship.

## H17 — Dual ES+NQ CD trend / BB correlation filter

Build 22-tick CD (+100 SMA, BB) on **both** ES and NQ. Correlated CD action
should mark cleaner continuation: (1) **dual trend** — both MA trends align
with trade side; (2) **dual wide BB** — both width percentiles in wide regime;
(3) **dual same regime**. Apply as filters on EXP-0008 lag&lt;250 momentum.
Material lift vs unfiltered 0008 required to ship; NQ-only CD already weak
(EXP-0015 / EXP-0009).

**Test:** EXP-0022. **Result:** On matched 22/100 CD, ES is flat at momentum events so dual filters are empty/unusable; NQ-only trend ≈ unfiltered 85%. With denser ES CD (one dig), dual wide/trend give small hit bumps but path ≤ unfiltered. Falsified as a ship filter. No ship.

## H18 — ES leads NQ (timing filter on momentum)

Among EXP-0008 lag&lt;250 bursts, those where the ES burst timestamp is **before**
the NQ burst (ES leads) continue better than NQ-first or simultaneous.

**Test:** EXP-0024. **Result:** ES-leads hit+10 81% / @10s +7.1 &lt; unfiltered lag&lt;250 85% / +9.8. Falsified. No ship.

## H19 — Swing-break momentum

EXP-0008 lag&lt;250 burst that **breaks** a causal swing high/low in the trade
direction continues better than the same bursts that do not break a swing.

**Test:** EXP-0025. **Result:** 3m break n=69 hit+10 88% / @10s +11.2 vs all 85% / +9.8; 5m dig stronger (92% / +12.9). Promising overlay on these days — **no ship** without holdout.

**Reopen (significant pullaway, both sides):** EXP-0039 — same idea with **20pt pullaway** pivots (not 20s time), longs vs HIGH / shorts vs LOW, stratify frozen MAT (vol k=1) by through/at/none; May OOS. **Result (trend+VWAP):** `through_5` @60s train avg −1.51 vs `none` +1.36 (n=53/570); May agrees through is worse. **Result (no gates, EXP-0039_NOGATE):** train through_5 +0.43 vs none −0.32 (n=58) but May −6.3 vs −1.1 — flip. **Reject / park** — do not gate MAT to swing breaks.

## H20 — Failed swing break (stop-run fade)

Price pierces beyond a causal swing by ≥N ticks then reclaims inside within
2s — fade the failed break. Dual ES+NQ same-side confirm. Distinct from LVN
touch (H7) and sweep continuation (H12).

**Test:** EXP-0026. **Result:** Dual n=7 &lt;15 — empty stop. NQ-only ≈ null. No ship.

## H21 — Flush into resting liquidity + hold (sweep reverse)

Fast flush tags a large resting wall that **holds** (limited further
penetration); price reclaims through the wall → bounce continuation
(BID hold→long / ASK hold→short). Optional LVN crossed on the flush path.
Distinct from H2 fade-the-level, H7 LVN-touch fade, and H20 failed-swing reclaim.

**Test:** EXP-0027. **Result:** Chart-size flushes empty in UK RTH window; soft gates n=29 hit+10 ≈ null; dual=0. Empty/falsified on these days. No ship. (Example screenshot was ~18:25 UK — outside primary analysis window.)

## H22 — ES impulse, NQ quiet (catch-up)

Correlation as lead-lag, not as two bursts that already happened. ES moves ≥N ticks in a short window while NQ is still quiet (|move| ≤ M). Trade NQ in the ES direction. Stratum: recent 60s sign-agreement of 1s returns is high (markets are correlated) vs not.

**Test:** EXP-0028. **Result:** Default n=12 hit+10 42% ≈ null; dig windows stay small or ≈ null on hit+10. Fat mean path is n-noise. Not a second strategy.

## H23 — Same-window co-move

The event *is* correlated price action: both ES and NQ returns over the same window are same-sign and each at least min ticks. Continuation. Distinct from EXP-0008 tape-imbalance bursts.

**Test:** EXP-0029. **Result:** n=206 hit+10 50% ≈ null 46%; median @10s −1; MAE ~18 vs EXP-0008 MAE ~9 / hit 85%. Correlated *price* moves are not the tape-burst edge. Falsified.

## H24 — Average rotation length gate

Markets rotate a characteristic distance (swing low↔high). Measure causal **average completed rotation length** from confirmed swings; **current leg extension** from the open-leg origin (last LOW for up / last HIGH for down; breakouts still measure from origin).

- **Momentum:** skip if extension ≥ mult × avg (already past a typical rotation — don't chase). Prefer with-leg tape.
- **Reversal / fade:** enter only if extension ≥ mult × avg (stretched — fade more plausible).

**Test:** EXP-0030 (+ dig avg_n / mult / swing lookback). **Result:** Falsified as specified. Fresh momentum (keep) hit+10 **83%** / @10s **+6.6** &lt; all **85%** / **+9.8**; extended (skip) **90%** / **+13**. Absorb extended fade n=7 hit **29%** worse than all 40%. Mult dig: extended beats fresh at every threshold. No ship.

## H25 — Trapped aggressors at liquidity peak

**Above-average volume** at a local extreme that sits in a **moderate LVN band
(~50 prices)**, then a **≥20pt pullback within ~20–30s** with **no large volume**
and **no reclaim** of the peak → fade (short after high-peak).

Causal gates (H25b): (1) 250ms cluster vol ≥ mult × rolling avg; (2) 50-price
band around peak has lower volume than neighboring shoulder bands; (3) pullback
≥20pts in 5–30s; (4) post-peak clusters ≤ ~45% of peak cluster; (5) no reclaim.

Distinct from H2 (immediate absorb) and H21 (flush-hold bounce).

**Test:** EXP-0031 (+ dig). **Result (H25b):** Fires (n=26; shorts 11 hit+10 **27%** / @10s **−3.3** ≪ null 44% / 0008 85%). Dig: earlier 15pt pullback shorts n=29 hit **55%** still flat path. Identification ok; continuation edge absent on these days. No ship.

## H26 — Range-bar aggressor imbalance vs prior ranges

As price completes a fixed **tick-range** bar (try 5 / 10 / 22 / 50), measure
**buy aggressor vol, sell aggressor vol, total vol, and duration**. Compare each
to a rolling average of prior completed ranges (causal). Continuation with the
imbalance side when |imbalance| is large and the **opposite** side is
**below** its prior average. Optional gates: speed above/below average (duration
inverse), total volume above/below average, and require bar direction aligned
with imbalance.

**Test:** EXP-0032 (+ dig min_imbalance / avg_n / range × mode). **Result:** Falsified.
Primary r22 opp_low n=789 hit+10 **44%** / @10s **−1.8** ≈ null **46%** ≪ EXP-0008 **85%**.
All range×mode cells ~44–49% hit; path mean ≤0 (often negative). Dig mi/avg_n no lift
(best large-n ~47% still path −1). No ship.

## H27 — Simultaneous ES+NQ aggressor flow (same clock)

Correlated *behaviour* at the same wall-clock instant: both ES and NQ show
same-side aggressor imbalance in the **same** tape bar (100/250/500ms), zero lag
by construction. Optional: also require same-sign price progress in that bar.
Secondary: dual H26 range-imbalance closes within tight sync (≤250–500ms).

Distinct from EXP-0008 (±sync burst confirm, can be sequential) and H23
(price co-move without requiring aggressor imbalance).

**Test:** EXP-0033 (+ dig bar_ms / imbalance / vol / progress / dual-range).
**Result:** Base same-clock flow beats null but loses to 0008 (pre-fix ticks).
High-vol+price (nq≥40) was strongest in-sample and parked for lack of holdout —
**reopen** as 23d MAT overlay / alternate-entry P&L dig (not auto-dead for ≪0008).
Dual range stay closed. Lead remains 0008; |lag|&lt;50 already captures “same time.”
*(Pre-fix numbers: ticks + look-ahead comparator — see EXPERIMENT_LOG reset.)*

## H28 — Correlated absorption (both tapes hit a wall at once)

Heavy one-sided aggression that fails to move price = absorption (inverse of the
0008 progress gate). If ES **and** NQ both absorb on the same aggressor side
within 250 ms, the wall is market-wide, not one resting order → fade the
aggressor on NQ. ES and NQ are different tapes (ES 100 ms volume ~3–4× NQ,
trade count ~2×, ES |net| ≤ 1 tick on ~99% of bars), so thresholds are
**per-market causal rolling quantiles** (trailing 30 completed minutes) with
per-market flat rules (NQ ≤1 tick, ES unchanged) and absolute floors.

**Test:** EXP-0035 (`research/features/corr_absorb.py`). 9 rule variants
(q90/95/98, flat0, range cap, rel-vol, 250 ms bars, sync 500, imb ≥0.70);
comparators null, corrected 0008, and **NQ-only absorption from the same
rule**; strata depth-level contact (15 sessions), ES-first, side; fade P&L.
**Result (23d): reject.** Dual fade hit10 **0.32–0.36** ≈ NQ-only **0.30–0.35**
≪ 0008 0.45 and ≤ the large-n null (0.40); @60s +0.3…+1.0; MFE≈MAE≈9 pts
(these bars sit in quiet tape). ES confirmation adds nothing to NQ-only
absorption. Level-contact stratum ≈ no lift (old 0.36 vs 0.28, n=150/95).
P&L: train negative on vol k=1 for every variant; only `imb70` fixed 10/10
is positive on all cohorts (+237, n=166) — 1 of 36 cells, hit10 0.33, not a
signal. Closed; don't revisit as a 100 ms bar construction.

## H29 — Correlated exhaustion (NQ runs alone, ES has stopped)

An ES+NQ confirmed burst stays healthy while both markets keep pushing. The
first 100 ms bar after entry where NQ aggressor imbalance is still with the
trade (≥0.55, NQ still bursting: vol ≥25, n ≥6) but ES has stopped — ES
imbalance ≤0 on a bar with prints, or ES volume <30% of its confirming burst —
marks NQ running alone. Two readings: (a) exit the MAT trade at that bar close;
(b) fade the burst at that bar close. Causal: only bars starting at/after entry.

**Test:** EXP-0036 (`research/features/corr_exhaust.py`, `research/correlated/`).
Exit overlay on vol k=1 and 12.5/30 × causal/confirm; 8 rule variants; fade
event study in points vs null and corrected 0008; fade P&L via events override.
**Result (23d):** **Exit: reject** (unchanged). Fires on ~65% of trades and
gives back winners (−981 on matched fired trades vs bracket +1840).
**Fade:** P&L yardstick (EXP-0008 is continuation, not the bar). Ungated vol
k=1 m04: n=114 net **+479** (train −4 / hold +144 / new +339), days+ 16/23,
top3 share **0.42**, LOO never negative (`EXP-0036_DIG2`/`_DIG3`). Soft ES vol
OK; hard ≥10 kill the edge. DIG3: against-trend gates hurt; shorts dominate
(stratum only); additive to MAT (+1259 vs MAT +779).
**DIG4 freeze:** `against_m04` + `skip_er0.3` clears ship bar
(n=91 net **+355**, train +6 / hold +123 / new +226, days+ 16, top3 0.45,
LOO_neg=0); neighbour `skip_er0.25` also clears (+311). `skip_er0.35` fails
train. 2-tick slip still +332 but train −1 — live fills matter.
**Status:** exit closed; fade **FROZEN for TEST_AUTOTRADE** — see
`results/EXP-0036_DIG4/TEST_AUTOTRADE_SPEC.md`. No further param search on
these 23d. Not in MAT JAR yet (separate fade module).

## H31 — MAT bounce at significant S/R

Frozen MAT performs better when it fires as a **push away from support/resistance**
after a recent hold-side touch of a 20pt-pullaway swing: long off swing LOW,
short off swing HIGH. Distinct from H19 breakout-through (EXP-0039) and H20
failed-break fade (pierce then reclaim).

**Test:** EXP-0040 (+ `_NOGATE` without trend/VWAP). **Result:** Still thin —
gated bounce_5 n=2; ungated n=5 train avg +5.5 but May n=5 avg −20 (flip).
Dig lookback/pullaway did not reach n≥30. Design B touch→MAT within sync_ms
stays empty. **Reject / park.** S/R×MAT needs a longer post-touch confirm window
(or dual ES+NQ S/R arm), not trend/VWAP.

## H32 — Stop-run fingerprint (inferred)

CME never tags stop orders. Infer a stop run when price pierces a causal
reference level (20pt pullaway swing HIGH/LOW, locked session H/L after
pullaway, overnight H/L) by ≥`pierce_ticks`, then a short same-side aggression
window shows enough of: side vol, imbalance, multi-level same-aggressor
sweeps, distinct aggressor count. Optional MBO far-side pull and ES same-side
burst raise confidence (score). Post-hoc stall (reclaim inside level) vs
continue is a stratum only — never part of the signal. Distinct from H20
(pierce+reclaim fade, pre-fix) and H19 (MAT×swing break).

**Test:** EXP-0041. **Result (49d UK 14:30–17:00):** n=44 events; continuation
hit10 **0.41** ≈ null **0.39** ≪ corrected 0008 **0.45**. Fade flip worse.
`outcome_continue` stratum hit10 0.58 / @60s +6.0 (n=24) — identification of
true follow-through is OK but not a ship filter. Gated MAT P&L continuation
n=33 net +62 (train +21 / May +41) but event-study bar fails and train
`loo_neg=4`. **Freeze thresholds for visual Stop-Run Bookmap module; no
auto-trade.** Defaults: pierce=2, window=2000ms, min_vol=25, min_imb=0.60,
min_sweeps=1, min_aggr=3, min_score=2.

## H33 — IRT dual-level correlated S/R

When NQ and ES both approach any Investor/RT established level (LVN2/3,
`HW_MT_DOP_NY` daily open, developing POC / 2nd `dVPOC`) within a short sync
window, short-horizon **hold / reject** on NQ beats null and NQ-only. Level
kinds need not match (e.g. NQ DOP × ES LVN). Distinct from H7 (Bookmap causal
session LVNs). Phase 1: diagnostic pierce hold/fail + reclaim-fade P&L; CD MA
and orderflow deferred.

**Test:** EXP-0042. ~~First result (Oct shallow&lt;8 vol_k1 +612, Sep +128)~~ was
**look-ahead biased**: the shallow filter used the full-horizon pierce and the
reclaim stamp was only kept for eventual holds. **Causal re-run:** Oct
reclaim n=110 net **+9**, shallow n=90 **−100**; Sep reclaim +2, shallow
**−59**; shallow dig all-negative OOS. **Status: reject / park** as a
standalone fade. Dual alignment may survive only as a context feature.
CD/orderflow deferred. May untouched.

## H34 — IRT level hold vs break orderflow

At NQ IRT key levels (LVN2/3, DOP, 2nd dVPOC), after a ≥30 pt approach
impulse in UK 14:30–17:00, **orderflow in the ±2s around first touch** differs
between path holds (≥30 pt reject, pierce &lt;15) and breaks (≥15 pierce + ≥30
extension). Pre-registered scores: Take, Stack, Tape, Attract/Repel — oriented
beyond vs hold-side so resistance and support share one scale. Descriptive
only; not a P&amp;L book.

**Test:** EXP-0043. ~~First pass (±2 s window): Take δ 0.30 / Tape δ 0.20 in
Oct; Sep flip.~~ That window included the first 2 s of the reaction.
**Post-review re-run (pre-touch `[-2s,0)` only, robust RTH baselines,
BBO-relative stack baseline, session-relative large size, DOP open exclusion,
real clustering, session-bootstrap CIs):** Oct 13d, HOLD 64 / BREAK 50 / AMB 78.
`TakeScore_pre` **δ 0.17** (CI90 ∋ 0), `TapeScore_pre` **δ 0.01**, `StackScore`
**δ −0.01**, AttractRepel ≡ 0. The `_at` mechanism scores still separate
(Take_at δ 0.38, CI90 [0.24, 0.54]) — orderflow *confirms* the break within 2 s
but does not *predict* it. Sep: BREAK n=5 (baseline_ok 2) — uninformative.
**Completed scoring + follow-ups (14d Oct, 15d Sep incl. basis-shifted NQU6
days; MBO cancels resolved; `MboScore_pre`; ±40 pt placebo):** every pre-touch
score δ ≤ 0.16 with CI90 ∋ 0 and **placebo levels give the same values** —
pre-touch H34 is null and not level-specific. `Take_at` is also ≈ placebo
(generic 2-s momentum). `StackConsume` is the one level-specific signal (real δ
0.40 / 0.47 vs placebo 0.20 / −0.08). Confirmation test at +2 s: **fade when
the beyond stack is intact (`StackConsume ≤ −0.25`)** is positive in both
months on every bracket (tp30/sl15 +422 Oct n=122 / +158 Sep n=60; maxDD
≈ 90). Plain reclaim fade is flat/negative.
**Status: pre-touch null; dig candidate = StackConsume-intact fade at +2 s
(one gate of three picked post hoc; needs May / forward).** See
`results/EXP-0043/`, `results/EXP-0043_202609/`.