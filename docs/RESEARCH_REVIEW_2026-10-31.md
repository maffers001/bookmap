# Research review and recommendations — 2026-10-31

> Scope: full programme through H82 / EXP-0091 — `docs/HYPOTHESES.md`,
> `docs/EXPERIMENT_LOG.md`, `docs/EDGE_RANKING.md` (2026-10-21),
> `docs/RESEARCH_REVIEW_2026-10-23.md`, `docs/PROFITABLE_STRATEGIES.md`,
> CFADE / MAT / LST / LVN / MBO synthesis artefacts under `results/`.
> Numbers are NQ points unless marked ES; UK 14:30–17:00; 1-tick slip +
> 0.25 pt/RT unless stated. Supersedes the 10-23 short answer for portfolio
> priority; does not replace experiment-level kill bars.

## 1. Short answer

**Convert Jul 2025–Feb 2026 and re-run every frozen book once before building
more signals.** Mar–Oct 2026 has been mined. The only virgin block left is
unconverted recordings plus any months you download next. CFADE remains the
core NQ tape book; ES open-cluster ∩ LVN is the only level construction with
honest placebo support — but its bracket was selected on the known sample.
EXP-0061 is a mechanism result, not a product. MAT belongs only as a gated
leg (`rot_fresh` / near-VWAP), not as a standalone.

What matters more than new hypotheses:

1. **Virgin power.** ~134 converted sessions (Mar–Oct 2026); ~141+ dense
   `.bmf` still unconverted (Jul 2025–Feb 2026) plus older sparse months.
   Every Tier-1/2 claim should face that block with frozen rules.
2. **Fill realism.** Sims fill TP at touch (`px >= tp`) to match Bookmap
   Replay. Live limits need queue clearance. Stress: one-tick-through TP and
   2-tick entry slip across CFADE, ES LVN, MAT075, LST — cheap, high EV.
3. **Organising principle.** ES is the levels market; NQ is the tape market.
   Survivors fit that split. Build dual ideas as *ES level + NQ tape*, not
   NQ level + NQ tape.

---

## 2. What we have (updated)

| Asset | State |
|---|---|
| Recordings | ~340+ `.bmf` under `C:\Bookmap\Feeds`; dense Jul 2025 → Oct 2026; sparse 2024 |
| Converted parquet | **~134 sessions** Mar–Oct 2026 (trades + depth + MBO both markets on most) |
| Unconverted virgin | **Jul 2025–Feb 2026** (~141 files) + Sep–Nov 2024 (~21) — primary next OOS |
| Live / indicators | MAT, CFADE (frozen DIG4), LST Mom + LST-PC (V1), HV / stop-run visuals |
| Survivors (watchlist) | See `docs/PROFITABLE_STRATEGIES.md` Tier 1–2 |
| Closed threads | DOP H69–H75; Tier A A1–A4; most MAT overlays; MBO standalones H80/H81; C4 H79 |

Infrastructure and methodology (causal stamps, twins, placebo levels, YAML
kill bars) are in good shape. Bottlenecks are virgin sample, fill model, and
calendar/roll hygiene — not missing feature plumbing.

---

## 3. Scorecard — what the evidence says now

### 3.1 Core books (same sign on expanded sample, caveats)

| Idea | Evidence | Read |
|---|---|---|
| **CFADE** against_m04 + ER≤0.30, vol RR 1:2 | 113d ~+1211; 131d ~+1100; May +335; Jun +568; **Mar −385** WR 0.27 | Best NQ book. March is a real left-tail / possible roll-week signal — not yet diagnosed. Fill quality still the ballgame. |
| **ES LVN outer** open-cluster ∩ LVN≤10, SL8/TP32, minOpen≥3 | Apr–Oct n=109 net **+353.6** ES pts, 6/7 months+, maxDD 69 | Tier 1 for retest. Bracket cell selected among many SL/TP/EOS variants on the same months. Conviction suite PASSes confluence vs far/shift. |
| **MAT + allocator (c)** | Always-on OOS −472 → (c) OOS +290 (EXP-0057); MAT alone OOS heavily negative | Portfolio rule, not a free lunch vs CFADE alone (see §4.3). |
| **MAT075** `rot_fresh_and_vwap_near` | EXP-0075 PASS OOS avg +2.76 n=132; Mar+Jun retest soft | Research-only until virgin months. Thin n. |
| **LST Momentum V1** | Holdout Gate1 PASS; mat_sim holdout +114; **LONG-only** stronger | Indicator shipped; autotrade after SIM / virgin. Shorts drag. |
| **LST-PC V1** | Event-study Gate2 PASS; mat_sim holdout **−60** | Watch / redefine (PC2 candidate); do not size. |

### 3.2 Soft / mechanism (do not size from current sample)

| Idea | Evidence | Caveat |
|---|---|---|
| **StackConsume-intact fade** (H34) | Oct/Sep/May same-sign; May ~+160 | Soft confirm; AMB/BREAK false fades; B2 ES-context + progress stop still OPEN |
| **EXP-0061** confirm-burst + ES-against exit | n=3055 net +815 OOS +577 | PF ~1.04; avg +0.23; commission ~half of gross — **not a product** |
| **`rot_fresh_0.90`** (H59) | OOS +212 | Needs future months; Sep–Oct already mined for MAT |
| **EXP-0091** paired ES/NQ MBO lifecycle on MAT | Count peek n=52 δ@60s +3.56 | Runner deferred to virgin months only |

### 3.3 Dead or closed (do not reopen same construction)

NQ first-touch level fades, dual LVN event studies, DOP thread (H69–H75),
Tier A gates on frozen MAT/CFADE, 100 ms dual absorption (H28), C4 MAT
absorption (H79), defended-retest / consumed-acceptance standalones (H80/H81
Stage 0 on DIG4∩MBO), most MAT overlays (CD, VIX keep, structural SL,
TP-extend path absorb, etc.). Full map: `HYPOTHESES.md` + `EDGE_RANKING.md`
Tier 4.

### 3.4 Visual / shadow only

HV extreme retest (parameter sign flips); stop-run fingerprint (≈ null
continuation). Shadow on fresh sessions; no autotrade.

---

## 4. Things weaker than the docs present them

### 4.1 Fill model is optimistic

`research/backtest/mat_sim.py` fills TP at working price on touch (`px >= tp`)
to match Bookmap Replay. Real passive/limit fills need the queue to clear; a
touch-and-reverse at TP is often an SL live. Impact is largest where WR is
low and TP is far:

- ES LVN: ~25 TPs ≈ +791 of net +354; 67 SLs ≈ −561.
- CFADE: ~210 TPs ≈ +7099 of net ~+1211.

**Action:** one-tick-through TP + 2-tick entry slip on all Tier-1/2 books.
Anything that dies here dies cheaply. Passive join / MBO queue model (proposed
10-23 as P2) was only partially attempted (EXP-0058 timer — reject on this
sample; re-test on future months only).

### 4.2 EXP-0061 is not a tradeable book

Mechanism holds (ES-against is a good *exit* for continuation; fade-at-stamp
twin is deeply negative). Product metrics do not: PF ≈ 1.039, avg +0.27,
commission eats ~half of gross. One extra tick of entry slip zeroes it.
Remove from Java shortlist as a standalone; keep as CFADE anatomy / exit
insight.

### 4.3 Allocator was benchmarked against the wrong yardstick

EXP-0057 shipped (c) = ER60 switch + MAT `skip_range175` because it beat
always-on (OOS −472 → +290). Against **CFADE alone** on the same table:
CFADE ~+1211 / maxDD ~363 vs (c) ~+1111 / maxDD ~559. Adding MAT cost net
and added drawdown in that framing. Under parity fills (`BEST_STRATEGY_EQUITY`)
MAT(c) contributes differently. Honest read: MAT is a **marginal** leg;
only `rot_fresh` / near-VWAP variants deserve virgin-month retest.

### 4.4 ES LVN headline is a selected cell

Many configurations on the same Apr–Oct months (SL/TP grids, inner/outer,
min-open, entry cut-offs, EOS 17:00 vs 21:00). Frozen SL8/TP32 is “best on
this sample” language in the summary itself. What *is* real:

- `LVN_CONVICTION`: confluence beats far-cluster (Δavg ~+5.5) and random
  shift (~+7.8); bootstrap 5th %ile > 0 on ES.
- Cluster-only is almost as good as confluence — this is largely an **ES
  open-book large resting first-touch fade**, not “LVN magic.”
- Same object failed pre-registered EXP-0046 (ES open standout T0 negative
  every month). Difference = bracket + hold to 21:00 + confluence cut.
  Reconcile in writing before sizing.

Trade-file nugget (descriptive, post-hoc): edge concentrated **14:30–15:00
UK** (~+359 of ~+354); later hours ~flat. Carry as a virgin-month arm, not
a silent retune.

### 4.5 Virgin months have been spent

Sep–Oct were the MAT holdout then mined by EXP-0067–0076. March was read
for CFADE, MAT075, and LST together — and was CFADE’s worst month (−385,
WR 0.27, n=52). That warning is noted in the retest log but not diagnosed
(roll week vs regime vs noise).

### 4.6 June 2026 carries the portfolio

Rough shares of net: MAT ~71% from June, 0061 ~42%, CFADE ~39%. Ex-June
curves are flatter. No experiment asked what June *was* (ATR, trend
persistence, ER distribution) even though `atr_expanded` passed and VIX
mid-tercile was descriptively strong vs hi-tercile (EXP-0074 — do not
promote mid post-hoc).

### 4.7 MBO lifecycle closed on an underpowered slice

Stage-0 counts / H80–H81 used DIG4∩MBO (~15–25 sessions) while ~131 depth
sessions exist. Iceberg proxy n=0 is more likely a detector failure than
proof icebergs do not exist (cf. EXP-0013 detector death). Keep lifecycle
as feature-on-trigger; re-count on full depth before accepting n=0;
EXP-0091 only on virgin months.

### 4.8 Multiplicity and kill-bar horizons

~90 hypotheses → several chance PASSes expected. Thin survivors (MAT075
n=132, `rot_fresh` OOS avg +0.25) sit in that noise. Power calc from 10-23
(n≈750 for +2 pt/trade at 80% power) still far above Stage-0 floors used
(n≥20 gates). Some kill bars used 300 s horizons for a 10–20 pt scalp
style (e.g. EXP-0088 missed @300s by ~0.11 pt while strong @60s).

### 4.9 No calendar awareness

No FOMC / CPI / NFP / roll / opex stratification in research runners.
March 2026 contains the NQM6 roll window; H34 already noted basis-shifted
NQU6 days. Split CFADE March roll-week vs rest before treating March as a
regime signal.

---

## 5. Nuggets for further research

1. **ES LVN ≈ opening half-hour.** Pre-register 14:30–15:00-only arm on
   virgin months; cluster-only vs confluence; dual arm = ES level + NQ entry
   on ES-confirmed tape.
2. **CFADE is multi-minute reversion with a 100 ms trigger.** Anatomy:
   holds ≤120 s lose OOS; 120–900 s win. Untested: re-enter once after first
   SL if ES still against; ES-against *dose* as size (H40 primary was too
   strict — neighbour had power; idea not falsified).
3. **Twin B localises CFADE’s edge.** NQ-only burst → ES-against is OOS
   negative; ES-confirmed burst → ES-against is the book. Information =
   “whole market burst, then half stopped.”
4. **ES levels / NQ tape.** Every NQ level construction failed; the level
   book with placebo support is on ES; every tape survivor is on NQ.
5. **EXP-0087 mechanism held** (support collapse @+2s → strongly worse
   path) though scratch book failed. Try skip/size-down or CFADE context,
   not exit-on-this-sample rescue.
6. **EXP-0088 breakthrough** strong short-horizon; failed 300 s bar
   narrowly — do not retune C4; note horizon mismatch for future designs.
7. **LST long-only + PC2.** Shorts drag every cut; PC2 (pullback ≥20 by
   +10s) holdout-strong but peeked — virgin only. Scale hand-tags (~60 →
   hundreds) and fit a simple classifier on *your labels* before “ML for
   regime.”
8. **StackConsume B2** (ES-context at +2s; progress time-stop) OPEN since
   10-23 — still not run.
9. **`rot_0.5_0.9` / fresh rotation** is the MAT context that reappears
   across 0067/0068/0075. If MAT lives, it is “MAT early in a rotation.”

---

## 6. Missed entirely (or barely touched)

| Gap | Why it matters |
|---|---|
| Fill / queue realism + passive MBO model | Monetises CFADE; stress-tests ES LVN / MAT |
| Calendar / roll / news days | March CFADE; Sep basis notes |
| Session structure as context | Overnight H/L, gap, London range, open type — DOP used only as breakout |
| Vol-regime sizing + June diagnosis | Portfolio concentration |
| Risk layer on full sample | −150 day kill designed on 23d; never re-checked on 131d |
| Late RTH / MOC | CFADE 17–18 UK already −382; 18–21 / MOC unused |
| Discretionary backlog without EXP | 30s pinbar V-reversal + trapped vol; size pulled at highs/lows; MOO/MOC; stops behind two HVNs (`MyIdeas.txt`) |

---

## 7. Recommended work order (devote time here)

Pre-register kill bars before reading virgin data. No threshold search on
Jul 2025–Feb 2026 or newly downloaded months.

| Priority | Work | Kill / promote |
|---|---|---|
| **P0** | Convert Jul 2025–Feb 2026 (trades-all; depth stratified ~1/3). Frozen retest: CFADE, ES LVN outer, MAT075 / `rot_fresh`, LST V1 + long-only, StackConsume, allocator (c). | CFADE: ≥60% months+, no month ≪ −400 pts; ES LVN: sign match + conviction-style ablations still lift; demote failures |
| **P1** | Fill realism pass (through-TP, 2-tick slip) on all Tier 1–2 | Drop any book that flips net− under both stresses |
| **P2** | ES open-cluster programme on virgin: SL8/TP32 frozen; 14:30–15:00 arm; cluster vs confluence; ES-level + NQ-tape dual; written EXP-0046 reconciliation | Must beat cluster-far / shift on virgin; dual optional |
| **P3** | CFADE anatomy: March roll/news split; SL re-entry; ES-against dose sizing; passive/MBO fill model on frozen events | Promote only if lift on virgin and retain ≥80% fills |
| **P4** | LST long-only V2 + PC2 on virgin; expand tags; optional tag classifier | No ship from holdout peeks |
| **P5** | MBO: full-depth lifecycle counts; iceberg detector visual QA; EXP-0091 virgin only | Stage-0 floors on full depth before claiming empty |
| **P6** | Descriptive regime study across books (June, ATR, day-type prior for allocator) | Strata only unless pre-registered gate |

### Deprioritise

- Further MAT overlays on the mined sample
- Any NQ first-touch level fade revival
- DOP thread
- Porting EXP-0061 as a standalone book
- Tier-A-style gates on frozen MAT/CFADE without a new construction + virgin data

---

## 8. Stop doing

- Retuning brackets / thresholds on Mar–Oct 2026 after seeing results
- Promoting thin OOS PASSes (n≪200, avg ≪1 pt) without virgin months
- Treating Replay touch-fills as live expectancy
- Closing MBO / iceberg ideas on DIG4-sized slices when 131 depth days exist
- Building NQ-level + NQ-tape duals; prefer ES-level + NQ-tape if dual at all
- Stacking MAT keep-gates on the current OOS

---

## 9. 30-day plan (revised)

| Week | Work |
|---|---|
| 1 | Bulk convert Jul 2025–Feb 2026 trades; start depth subset; draft freeze YAML for P0 retest pack |
| 2 | P0 frozen retests + P1 fill stress; update `PROFITABLE_STRATEGIES.md` retest log |
| 3 | P2 ES open-cluster virgin arms + EXP-0046 write-up; CFADE March diagnosis |
| 4 | P3 CFADE execution/anatomy; P4 LST SIM / virgin; P5 MBO recount if depth ready |

Deliverable at day 30: Tier-1 watchlist with virgin-month pass/fail rows,
fill-stress notes, and at most one new dual (ES level × NQ tape)
preregistered — not a new dig pile.

---

## 10. Related docs

- `docs/RESEARCH_REVIEW_2026-10-23.md` — earlier review (sample-starved era; many digs since completed)
- `docs/PROFITABLE_STRATEGIES.md` — Tier 1–2 watchlist + retest log
- `docs/EDGE_RANKING.md` — dig queue snapshot 2026-10-21
- `docs/HYPOTHESES.md` / `docs/EXPERIMENT_LOG.md` — claim ↔ EXP ↔ verdict
- `docs/MBO_LIFECYCLE_PLAN.md` — H79–H82 / EXP-0088–0091
- `docs/CORRELATED_IDEAS_2026-10-23.md` §8 — Tier A–D map (mostly closed)
- `docs/MyIdeas.txt` — discretionary backlog
- `results/CFADE_FAMILY_SYNTHESIS/`, `results/MAT_CONTEXT_SYNTHESIS/`,
  `results/MAT_BOOK_SYNTHESIS/`, `results/MOMENTUM_SLICE/STATUS.md`
