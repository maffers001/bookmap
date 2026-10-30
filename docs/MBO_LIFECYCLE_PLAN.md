# MBO lifecycle edge programme

> Pre-registered 2026-10-30. Scope: phased programme; discover on existing
> depth/MBO sessions, freeze, validate once on virgin months. No Java /
> autotrade before a virgin-month pass. Rules: `docs/RESEARCH_RULES.md`.

## Premise corrections

- **"Single trader"** is unobservable. Unit of study = resting `order_id`
  and its **dominance** (largest order ÷ displayed size at that price).
- **p95 prints are too common.** Causal trailing-30-minute **p99** resting
  size per market; whole-session percentiles forbidden.
- **Passive fills are inferred.** `mbo.parquet` has send/cancel/replace
  only; trades carry aggressor IDs. FIFO queue-ahead vs depth-drop−traded
  accounting; disagreement → `fill_uncertain=1`. Seeded orders excluded.

## Phases

| Phase | ID | Deliverable |
|---|---|---|
| 0 | EXP-0088 / H79 | MAT burst absorption C4 one-shot (already built) |
| 1 | `mbo_lifecycle` | Causal order lifecycle layer + Stage-0 counts |
| 2 | EXP-0089 / H80 | Defended-retest fade (Q1) |
| 3 | EXP-0090 / H81 | Consumed acceptance continuation / breach fade (Q2) |
| 4 | counts | Paired ES/NQ lifecycle + iceberg proxy on frozen triggers |
| 5 | freeze | Virgin-month validation + docs |

## Terminal states (`research/features/mbo_lifecycle.py`)

| State | Rule |
|---|---|
| defended | executed ≥ 50% initial, pen ≤ 2 ticks, reload **or** stack ≥ 50% |
| completed | executed ≥ min, no reload, stack < 25%, pen ≤ 2 |
| consumed | pierce ≥ 4 ticks ∧ consume_ratio ≥ 1.0, no reload within 2 s |
| pulled | ≥ 70% cancelled before contact near BBO |

## EXP-0089 defended-retest (frozen)

See `research/experiments/EXP-0089.yaml`. Primary bar: defended − completed
≥ +1.0 pt @60 s and defended > ±40 pt placebo. Fail → reject/park, no retune.

## EXP-0090 consumed acceptance (frozen)

See `research/experiments/EXP-0090.yaml`. Primary bar: left_behind − tape_only
≥ +1.0 @60 s and ≥ +2.0 @300 s. Shape informed by EXP-0088 verdict.

## Floors (Stage 0)

- defended ≥ 30, consumed ≥ 30, primary (large∧dominant∧¬seeded) ≥ 20
- EXP-0089: defended-retest arm ≥ 30 and completed ≥ 30
- EXP-0090: left_behind ≥ 30 and tape_only ≥ 30

If floors fail: park standalone; keep lifecycle as feature-on-trigger only.

## Out of scope

Single-trader identity; whole-session percentiles; H13 vacuum revival;
H2 unconditional absorption; Java before virgin pass; threshold retunes
on samples already read.
