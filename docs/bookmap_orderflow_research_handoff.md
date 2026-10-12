# Bookmap Order-Flow Research Project --- Conversation Handoff for Cursor

**Purpose:** This document captures the full technical context, trading
ideas, research architecture, and proposed implementation discussed with
ChatGPT. It is intended to be imported into Cursor as the starting
context for an ongoing Bookmap/order-flow research project.

**Primary instruments:** CME E-mini S&P 500 (ES) and CME Nasdaq-100 (NQ)
futures\
**Primary trading platform:** Bookmap\
**Market data:** Rithmic / CME market data, including MBO where
available\
**Developer background:** Experienced software engineer;
Java/Kotlin/Python experience\
**AI/dev tooling available:** Cursor subscription, OpenClaw, ability to
create other AI environments\
**Overall objective:** Quantify the trader's discretionary order-flow
observations, develop useful Bookmap indicators, automatically research
and backtest them on recorded data, and---only if robust evidence
supports it---potentially develop automated trading strategies.

------------------------------------------------------------------------

# 1. Starting Question: Can Bookmap Be Extended?

The initial question was whether Bookmap allows custom coded extensions.

The conclusion was **yes**. Bookmap provides APIs for custom indicators,
strategies, data processing, visualization, and trading.

The main API discussed was the **Bookmap Layer 1 (L1) API**.

Relevant capabilities include:

-   Receive executed trades.
-   Receive bid/ask depth changes.
-   Receive BBO and other market information.
-   Work with Bookmap historical/replay data.
-   Create custom indicators.
-   Plot indicators over the heatmap or in lower panels.
-   Create custom GUI controls.
-   Draw arbitrary graphics using Bookmap's painter APIs.
-   Receive order/trading events.
-   Submit and modify orders for automated strategies.
-   Consume Market-by-Order (MBO) information where the feed supports
    it.
-   Monitor multiple instruments from a single add-on.

Bookmap has both a Simplified API and lower-level/core APIs. The
proposed approach is to start with the Simplified API wherever possible
and use the lower-level API only where custom rendering or deeper
control is required.

Bookmap add-ons can be developed as Java/Gradle projects, compiled to
JARs, and loaded into Bookmap through the API plugin configuration.

The Layer 0 API is primarily relevant to connectivity/custom data feeds
and possibly custom replay pipelines; it is not expected to be the main
API for the indicator work.

------------------------------------------------------------------------

# 2. Initial Indicator Idea: Volume and Orders Per Second

The first concrete indicator requested was to measure:

-   Buy volume transacted per second.
-   Sell volume transacted per second.
-   Buy orders/executions per second.
-   Sell orders/executions per second.

## 2.1 Buy/Sell Volume

Each executed trade can be classified according to the aggressor side.

Conceptually:

``` text
Buy Volume/sec
    = sum(size of buyer-aggressed executions during interval)

Sell Volume/sec
    = sum(size of seller-aggressed executions during interval)
```

For a one-second interval, counters are accumulated from trade events
and emitted/reset every second.

## 2.2 Trades vs Orders

An important distinction was identified.

Counting every trade callback gives **trade prints/executions**, but one
aggressive marketable order may generate multiple trade events as it
interacts with multiple passive orders or price levels.

Therefore, useful metrics include all of:

``` text
Buy Volume/sec
Sell Volume/sec

Buy Trades/sec
Sell Trades/sec

Buy Aggressive Orders/sec
Sell Aggressive Orders/sec
```

Where supported by Bookmap trade metadata, execution-chain information
and/or aggressor order IDs can be used to estimate actual aggressive
orders rather than merely count prints.

## 2.3 Average Aggressive Order Size

Another useful metric follows naturally:

``` text
Average Buy Aggressive Order Size
    = Buy Volume / Buy Orders

Average Sell Aggressive Order Size
    = Sell Volume / Sell Orders
```

Example:

``` text
Buy volume/sec   = 410
Buy orders/sec   = 8
Average buy size = 51.25 contracts/order

Sell volume/sec   = 92
Sell orders/sec   = 21
Average sell size = 4.38 contracts/order
```

This distinguishes many small aggressive participants from a smaller
number of unusually large aggressive orders.

## 2.4 Delta Metrics

Additional useful outputs:

``` text
Volume Delta/sec
    = Buy Volume - Sell Volume

Order Delta/sec
    = Buy Orders - Sell Orders
```

## 2.5 Configurable Aggregation

Do not hard-code one second as the only possible window.

Potential intervals:

``` text
100 ms
250 ms
500 ms
1 second
2 seconds
5 seconds
10 seconds
```

Raw one-second values may be noisy, so optional smoothing such as a
short EMA could also be useful.

------------------------------------------------------------------------

# 3. Displaying the Indicator in Bookmap

Bookmap can plot custom indicators directly inside the application,
synchronized with Bookmap's time axis and heatmap.

The initial recommended display was:

## Panel 1 --- Aggressive Volume

-   Buy volume/sec as positive.
-   Sell volume/sec as negative.
-   Optional volume delta.

Conceptually:

``` text
        BUY
         ↑
 +400 ┤       ╭─╮
 +300 ┤  ╭╮  ╭╯ ╰╮
 +200 ┤──╯╰──╯   ╰─
    0 ┼──────────────────
 -200 ┤    ╰──╮
 -300 ┤       ╰──
         ↓
        SELL
```

## Panel 2 --- Aggressive Orders / Executions

-   Buy orders/sec as positive.
-   Sell orders/sec as negative.
-   Optional order delta.

## Current-Value Widgets

A compact live display could show:

``` text
FLOW

BUY VOL/s       327
SELL VOL/s      118
VOL DELTA       +209

BUY ORD/s        54
SELL ORD/s       31
ORDER DELTA      +23

AVG BUY SIZE     6.1
AVG SELL SIZE    3.8
```

Bookmap's normal indicator API is most naturally suited to line-series
plots. If histogram/bar-style rendering is preferred, Bookmap's
lower-level painting facilities can be used to create custom graphics.

The initial implementation should favor Bookmap-native line plots for
simplicity and robustness, with custom rendering added later if it
materially improves trading usability.

------------------------------------------------------------------------

# 4. Trader's Actual Trading Style

The central goal is **short-term reversal timing through subtle changes
in order flow**.

The trader is not primarily looking for conventional technical
indicators. The trader visually interprets how aggressive trading,
resting liquidity, and price interact.

Important observations include:

1.  The market is moving directionally.
2.  It begins to consolidate or lose progress.
3.  It fails to consume large resting liquidity required to continue the
    move.
4.  Large resting orders begin trading against the direction of the
    move.
5.  Large orders appear close to price.
6.  The trader observes whether those orders attract price or repel
    price.
7.  Trend-side aggression may remain high, but its ability to move price
    decreases.
8.  Counter-direction aggression begins to increase.
9.  ES and NQ order-flow behavior may confirm or diverge from each
    other.

The core research objective is therefore **not simply to measure order
flow**, but to measure **the response of price and liquidity to order
flow**.

------------------------------------------------------------------------

# 5. Indicator / Feature Ideas

## 5.1 Effort vs Result / Failure-to-Progress

This was considered one of the highest-value ideas.

Measure how much aggressive trading "effort" is required to produce
price movement.

Example during a falling NQ market:

``` text
                  Sell Volume     Downward Progress
T-4 sec               220              5 ticks
T-3 sec               310              4 ticks
T-2 sec               390              2 ticks
T-1 sec               460              1 tick
NOW                   510              0 ticks
```

Although selling is increasing, its ability to move price is collapsing.

Possible metrics:

``` text
Sell Effort/Result
    = Aggressive Sell Volume / Downward Price Progress

Buy Effort/Result
    = Aggressive Buy Volume / Upward Price Progress
```

Care is required around zero price progress; use bounded
transformations, epsilon handling, categorical states, or another robust
formulation.

Potential signal:

``` text
Trend-side aggressive volume ↑
Trend-side executions ↑
Trend-side price progress ↓
=> possible absorption/exhaustion/reversal condition
```

The first derivative / acceleration of efficiency may be more useful
than the absolute level.

------------------------------------------------------------------------

# 5.2 Liquidity Reaction: Attract or Repel

This directly quantifies an existing discretionary observation.

When sufficiently large resting liquidity appears near current price,
create an event and track it.

Before contact:

-   Distance from price.
-   Size.
-   Relative size compared with recent depth.
-   Order age.
-   Persistence.
-   Whether size increases/decreases.
-   Whether price accelerates toward it.
-   Whether price moves away from it.
-   Queue/order composition if MBO is available.

At contact:

-   Volume executed into it.
-   Number of executions.
-   Percentage of displayed liquidity consumed.
-   Whether liquidity replenishes.
-   Whether price penetrates the level.
-   Maximum penetration.
-   Time spent interacting with the level.

After contact:

-   Price displacement after 100 ms.
-   250 ms.
-   500 ms.
-   1 second.
-   2 seconds.
-   5 seconds.
-   MFE and MAE.

This could allow the system to learn empirically when large liquidity
tends to:

-   attract price before contact;
-   repel price after contact;
-   disappear before contact;
-   absorb aggressive flow;
-   fail and allow continuation.

------------------------------------------------------------------------

# 5.3 Order-Flow Inflection

The trader specifically wants to detect **small changes before price
visibly reverses**.

Absolute values may be less informative than changes in flow.

Example:

``` text
Sell Volume/sec:
420 → 390 → 310 → 220

Buy Volume/sec:
80 → 95 → 140 → 210
```

Price may not have reversed yet, but control may be changing.

Measure:

-   Buy volume/sec.
-   Sell volume/sec.
-   Buy/sell executions/sec.
-   Buy/sell aggressive orders/sec.
-   Average aggressive order size.
-   First derivative.
-   Second derivative / acceleration.
-   Short-vs-long rolling averages.
-   Normalized z-scores versus recent activity.

Potential goal:

> Detect the turn in order flow before the turn in price.

------------------------------------------------------------------------

# 5.4 Near-Price Liquidity Pressure

Generic DOM imbalance may be too crude.

Instead, weight liquidity according to:

-   Distance from current price.
-   Size relative to normal size at that distance.
-   Persistence.
-   Whether liquidity is new or longstanding.
-   Whether it is increasing/decreasing.
-   Whether it survives price approaching.

Example conceptual score:

``` text
Liquidity Pressure
    = Σ(size × distance_weight × persistence_weight)
```

Calculate separately for bids and asks and perhaps across:

``` text
nearest 1 tick
nearest 3 ticks
nearest 5 ticks
nearest 10 ticks
```

Also distinguish:

-   newly added size;
-   cancelled size;
-   modified size;
-   persistent size.

------------------------------------------------------------------------

# 5.5 Liquidity Persistence / Withdrawal

Track large orders as price approaches.

Example:

``` text
Initial bid = 500

5 ticks away: 500
4 ticks away: 510
3 ticks away: 530
2 ticks away: 540
1 tick away: 550
```

versus:

``` text
Initial bid = 500

5 ticks away: 500
4 ticks away: 480
3 ticks away: 350
2 ticks away: 220
1 tick away: 0
```

Do **not** automatically label disappearing orders as spoofing. The
indicator should objectively describe observable behavior:

-   persistence;
-   withdrawal;
-   replenishment;
-   cancellation velocity;
-   survival ratio.

MBO can potentially distinguish a single genuinely large order from many
smaller orders at the same price.

------------------------------------------------------------------------

# 5.6 Absorption / Replenishment Velocity

Example:

``` text
Visible bid initially:         250
Aggressive sell volume into it: 700
Visible/replenished bid:        200
Price penetration:              minimal
```

The important information is not merely "there is a 200 bid."

It is that much more than the displayed size traded into the level and
yet the level survived / replenished.

Potential features:

``` text
executed_volume / initial_displayed_size
executed_volume / average_displayed_size
replenishment_volume
replenishment_rate
number_of_replenishments
time_at_level
penetration_ticks
post-contact_price_response
```

------------------------------------------------------------------------

# 5.7 ES/NQ Order-Flow Divergence

A Bookmap add-on can potentially observe multiple instruments, allowing
ES and NQ to be analyzed in the same signal engine.

Do not compare raw ES and NQ contract counts directly because their
activity distributions differ.

Normalize each market using measures such as:

-   z-score of aggressive volume;
-   percentile of recent volume;
-   normalized execution rate;
-   normalized order size;
-   normalized effort/result;
-   normalized absorption;
-   normalized near-price liquidity.

Possible observations:

-   NQ makes another low while ES does not.
-   NQ sell aggression remains high but loses price impact.
-   ES selling weakens first.
-   ES buy aggression begins increasing.
-   ES absorption appears before NQ.
-   ES and NQ liquidity states diverge.

This may provide a stronger reversal condition than price correlation
alone.

------------------------------------------------------------------------

# 5.8 ES/NQ Lead-Lag

Do not assume ES and NQ must confirm simultaneously.

Research rolling lead/lag relationships at:

``` text
100 ms
250 ms
500 ms
1 second
2 seconds
```

Questions:

-   Does ES order flow lead NQ during some regimes?
-   Does NQ lead ES during others?
-   Does lead/lag change by time of day?
-   Is order-flow lead/lag more useful than price lead/lag?
-   Does ES flow inflection preceding NQ flow inflection improve
    reversal prediction?

------------------------------------------------------------------------

# 6. Reversal Flow Engine

Rather than creating many unrelated indicators, the proposed long-term
architecture is a **Reversal Flow Engine**.

Conceptual state progression:

``` text
DIRECTIONAL MOVE
       ↓
CONSOLIDATION / LOSS OF PROGRESS
       ↓
TEST OF LIQUIDITY
       ↓
Does liquidity disappear?
       │
       ├── Yes → continuation remains possible
       │
       └── No
            ↓
       ABSORPTION
            ↓
Trend-side aggression loses efficiency
            ↓
Counter-direction flow begins increasing
            ↓
Nearby opposing liquidity changes
            ↓
ES/NQ confirmation or divergence
            ↓
REVERSAL CONDITIONS
```

Potential internal measurements:

``` text
TREND EFFICIENCY
Sell effort/result

FLOW CHANGE
Seller acceleration
Buyer acceleration

LIQUIDITY
Bid persistence
Ask persistence
Near-price pressure

ABSORPTION
Bid absorption
Ask absorption

CROSS-MARKET
ES flow state
NQ flow state
ES/NQ divergence
Lead/lag state
```

The aim is not initially to output a magical "BUY" or "SELL" score.
First expose and validate the underlying measurements.

------------------------------------------------------------------------

# 7. Automated Trading in Bookmap

Bookmap APIs can be used to submit and modify orders, so a Bookmap
add-on can potentially become an automated strategy.

However, automated execution should be the **last stage**, not the
first.

Proposed architecture:

``` text
                    REVERSAL ENGINE
                           │
          ┌────────────────┼────────────────┐
          │                │                │
      Indicator        Backtester       Auto Trader
          │                │                │
    Bookmap plots     Historical data    Rithmic /
      + alerts         + statistics      broker orders
```

The same underlying signal logic should be reused across visualization,
historical testing, and live execution wherever practical. Avoid
implementing materially different versions of the same signal in
research and production.

Progression:

``` text
Historical research
        ↓
Bookmap replay / simulation
        ↓
Broker/data-feed demo environment
        ↓
Very small live size
        ↓
Compare real fills/slippage with simulation
        ↓
Scale only if justified
```

Local execution simulations can have unrealistically favorable
latency/fills, so real/demo execution behavior must be measured before
relying on simulated performance.

------------------------------------------------------------------------

# 8. Backtesting Philosophy

Manual replay of every signal would be too time-consuming.

Instead, the strategy engine should automatically generate a structured
event whenever candidate conditions occur.

Example event record:

``` text
timestamp
instrument
direction

trend_strength
trend_duration
consolidation_duration

buy_volume_100ms
sell_volume_100ms
buy_volume_1s
sell_volume_1s
buy_volume_3s
sell_volume_3s

buy_orders_1s
sell_orders_1s
buy_avg_order_size
sell_avg_order_size

price_progress
trend_efficiency
effort_result_ratio

near_bid_liquidity
near_ask_liquidity
liquidity_distance
liquidity_relative_size
liquidity_age
liquidity_persistence
liquidity_withdrawal_rate

executed_volume_at_level
absorption_ratio
replenishment_volume
replenishment_rate

ES_flow_state
NQ_flow_state
ES_NQ_divergence
lead_lag_state

signal_timestamp
reference_price
```

Then automatically label what happened after the event.

Example:

``` text
100 ms       +1 tick
250 ms       +2 ticks
500 ms       +5 ticks
1 second     +7 ticks
2 seconds   +11 ticks
5 seconds   +18 ticks
10 seconds  +13 ticks

MFE          +21 ticks
MAE           -3 ticks

+5 target hit?      YES
+10 target hit?     YES
-5 stop first?      NO

Time to +10 ticks:  1.73 sec
```

This transforms historical recordings into a research dataset.

------------------------------------------------------------------------

# 9. Questions the Research System Should Answer

Examples:

-   Does failure-to-progress predict reversal?
-   Does aggressive volume remain predictive after controlling for
    volatility/activity?
-   Is absolute resting size useful, or should "large" always be
    relative to recent liquidity?
-   Does absorption ratio improve reversal probability?
-   Does replenishment matter independently of absorption?
-   How much volume normally needs to trade into a level before it
    becomes meaningful?
-   Is counter-flow acceleration more useful than counter-flow absolute
    size?
-   Does average aggressive order size matter?
-   Do large individual MBO orders behave differently from aggregated
    size?
-   Does liquidity close to price attract price before contact?
-   Under what conditions does liquidity repel price after contact?
-   Does withdrawal of large liquidity predict continuation?
-   Does ES confirmation improve NQ reversal signals?
-   Does ES tend to lead NQ order flow?
-   Does the relationship vary by time of day?
-   Are signals stronger around the US cash open?
-   How does performance differ in high- and low-volatility regimes?
-   What is the typical delay between absorption and reversal?
-   What stop distance best reflects actual MAE rather than arbitrary
    optimization?
-   How stable are effects across weeks/months?

------------------------------------------------------------------------

# 10. Research Methodology: Avoid Curve-Fitting

This is critical.

Do **not** instruct an AI to simply "find a profitable strategy." Given
enough parameter combinations, it can discover historical artifacts.

Start with explicit hypotheses.

Example:

## Hypothesis A

> After a directional down move, unusually high aggressive selling
> combined with unusually low downward price progress predicts positive
> short-term future returns.

Test this first.

## Hypothesis B

> The effect is stronger when persistent bid liquidity absorbs the
> selling.

## Hypothesis C

> The effect becomes stronger when aggressive buying begins
> accelerating.

## Hypothesis D

> ES order-flow confirmation improves NQ reversal outcomes.

Measure the incremental contribution of each component.

Illustrative output only:

``` text
Condition                            Events    Avg MFE    Avg MAE

Failure to progress                   2,840      7.2       5.9
+ absorption                          1,410     10.1       4.7
+ counter-flow acceleration             812     13.8       3.9
+ ES confirmation                       497     17.2       3.4
```

These numbers are illustrative and not actual research results.

------------------------------------------------------------------------

# 11. Out-of-Sample Testing

Always separate research data.

Possible simple split:

``` text
Development:   Jan-Jun
Validation:    Jul-Aug
Final holdout: Sep-Oct
```

Prefer rolling/walk-forward analysis when enough data exists.

Rules:

-   Never optimize directly against final holdout data.
-   Never repeatedly inspect holdout results while developing.
-   Record every experiment, including failures.
-   Prevent future information leaking into features.
-   Include realistic transaction costs.
-   Eventually include realistic latency and slippage.
-   Segment results by session/regime.
-   Report sample sizes.
-   Analyze parameter stability rather than only the optimum.
-   Avoid thresholds with a narrow "magic" profitable value.

------------------------------------------------------------------------

# 12. Recorded Bookmap Data

The trader has a large amount of recorded historical data.

If the recordings are native Bookmap `.bmf` files, the exact
extraction/batch-processing workflow needs to be established.

A likely initial architecture:

``` text
Bookmap .bmf recordings
          ↓
Bookmap replay
          ↓
Custom Bookmap extraction add-on
          ↓
Raw normalized event files
          ↓
Parquet
          ↓
Python / DuckDB research system
```

Do not repeatedly replay Bookmap for every parameter variation if it can
be avoided.

Instead:

1.  Extract raw events once.
2.  Generate reusable features.
3.  Store them efficiently.
4.  Run thousands of research queries against local structured data.

Potential raw event types:

``` text
TRADE
DEPTH
MBO_ADD
MBO_REPLACE
MBO_CANCEL
BBO
INSTRUMENT
TIMESTAMP
```

Need to investigate exactly what information is retained in the trader's
existing recordings and what Bookmap exposes during replay.

------------------------------------------------------------------------

# 13. Proposed Technology Stack

## Production / Bookmap Side

**Java** is the preferred initial language for the Bookmap production
add-on.

Responsibilities:

-   Live Bookmap integration.
-   Trade callbacks.
-   MBO/depth callbacks.
-   Multi-instrument ES/NQ state.
-   Indicator visualization.
-   Alerts.
-   Signal generation.
-   Eventually live trading.
-   Order/fill handling.
-   Risk controls.

## Research Side

**Python** for:

-   Historical analysis.
-   Feature engineering.
-   Statistics.
-   Parameter sensitivity.
-   Walk-forward analysis.
-   Monte Carlo analysis.
-   Visualization.
-   Machine learning later, only if justified.

Suggested tools:

-   Python
-   NumPy
-   Pandas and/or Polars
-   SciPy
-   DuckDB
-   Parquet
-   Matplotlib / Plotly as appropriate
-   potentially MLflow later
-   potentially Optuna later

## Storage

Prefer **Parquet + DuckDB** for large event/feature datasets rather than
CSV.

Conceptually:

``` text
Bookmap recordings
       ↓
Extractor
       ↓
Parquet event data
       ↓
DuckDB
       ↓
Feature generation
       ↓
Signal/event datasets
       ↓
Backtests/statistics
```

------------------------------------------------------------------------

# 14. AI-Agent Research Architecture

The trader has:

-   Cursor subscription.
-   OpenClaw installed.
-   Ability to create additional AI environments if needed.

The recommendation was **not to add another environment initially**.

Use:

-   **OpenClaw** as research coordinator/orchestrator.
-   **Cursor** as coding/engineering agent.
-   **Git repository** as persistent project memory/source of truth.
-   Local Java/Python tooling for research.
-   Local storage for large Bookmap data.

Conceptual architecture:

``` text
                         HUMAN
                           │
                           ▼
                 OpenClaw Coordinator
                           │
          ┌────────────────┼────────────────┐
          │                │                │
          ▼                ▼                ▼
   Microstructure       Quant          Skeptical
     Researcher       Researcher        Reviewer
          │                │                │
          └──────────┬─────┴────────────────┘
                     │
                     ▼
              Cursor Coding Agent
                     │
              ┌──────┴───────┐
              ▼              ▼
       Java Bookmap      Python Research
          Add-on             Engine
              │              │
              └──────┬───────┘
                     ▼
             Historical Data
                     │
                     ▼
              Experiment DB
                     │
                     ▼
              Research Reports
```

------------------------------------------------------------------------

# 15. Proposed Repository Structure

Create a repository such as:

``` text
orderflow-research/
│
├── AGENTS.md
├── RESEARCH_BRIEF.md
├── RESEARCH_RULES.md
├── HYPOTHESES.md
├── EXPERIMENT_LOG.md
├── README.md
│
├── docs/
│   ├── bookmap-api/
│   ├── market-microstructure/
│   └── strategy-notes/
│
├── bookmap-addon/
│   ├── build.gradle
│   └── src/
│
├── extractor/
│   └── ...
│
├── research/
│   ├── features/
│   ├── signals/
│   ├── backtests/
│   ├── statistics/
│   └── notebooks/
│
├── experiments/
│   ├── EXP-0001-effort-result/
│   ├── EXP-0002-absorption/
│   ├── EXP-0003-flow-inflection/
│   └── ...
│
├── data/
│   ├── raw/
│   ├── parquet/
│   └── features/
│
├── results/
│   ├── plots/
│   ├── reports/
│   └── experiment-db/
│
└── scripts/
    └── ...
```

Large/raw market data should generally not be committed to Git.

------------------------------------------------------------------------

# 16. Proposed AGENTS.md Rules

Cursor/OpenClaw should receive persistent instructions similar to:

``` text
You are working on a futures order-flow research system.

Primary instruments:
- CME E-mini S&P 500 (ES)
- CME Nasdaq-100 (NQ)

Primary data:
- Bookmap
- Rithmic
- CME MBO where available

Research objective:
Identify statistically robust precursors to short-term reversals based on
order-flow, liquidity behavior, price response, and ES/NQ relationships.

Never:
- optimize directly against the final test dataset;
- choose parameters solely because they maximize P&L;
- use future information in feature calculations;
- silently discard unsuccessful experiments;
- modify or overwrite historical results;
- describe an effect as robust without appropriate validation;
- enable live automated trading without explicit human approval.

Every strategy experiment must:
- state its hypothesis before running;
- identify the exact dataset/version;
- specify parameters before analyzing results;
- record source-code Git commit;
- record sample size;
- report MFE and MAE;
- report future returns at standard horizons;
- report results by trading session where useful;
- report transaction-cost assumptions;
- separate development, validation, and holdout results;
- save negative as well as positive findings;
- check for look-ahead bias;
- assess parameter sensitivity;
- document all methodological changes.
```

The exact rules should evolve as the research framework matures.

------------------------------------------------------------------------

# 17. Suggested AI Agent Roles

## Coordinator

Responsibilities:

-   Maintain roadmap.
-   Maintain hypothesis queue.
-   Select approved next experiment.
-   Ensure experiment prerequisites exist.
-   Coordinate specialist agents.
-   Produce concise human summaries.

The coordinator should not be allowed to repeatedly mine holdout data.

## Market Microstructure Researcher

Responsibilities:

-   Research Bookmap API capabilities.
-   Research Rithmic/CME MBO semantics.
-   Research queue imbalance.
-   Research absorption/exhaustion literature.
-   Research market microstructure papers.
-   Propose testable hypotheses.
-   Link every proposal to observable data.

## Quant Researcher

Responsibilities:

-   Convert hypotheses into precise experiments.
-   Define features without look-ahead.
-   Define labels/outcomes.
-   Select appropriate statistical tests.
-   Analyze robustness.
-   Perform walk-forward analysis.
-   Detect overfitting risks.

## Bookmap Engineer

Responsibilities:

-   Java/Bookmap integration.
-   Replay extraction.
-   MBO state reconstruction.
-   Multi-instrument synchronization.
-   Indicators.
-   Production signal engine.
-   Later, order execution/risk controls.

Cursor can act as the principal implementation agent for this role.

## Skeptical Reviewer

Responsibilities:

-   Attempt to invalidate findings.
-   Look for data leakage.
-   Look for selection bias.
-   Look for survivorship bias.
-   Look for timestamp/synchronization problems.
-   Challenge arbitrary thresholds.
-   Check transaction-cost assumptions.
-   Check whether a result persists out-of-sample.
-   Check whether sample sizes are adequate.

This role is deliberately adversarial.

------------------------------------------------------------------------

# 18. OpenClaw + Cursor Workflow

OpenClaw should orchestrate the research process while Cursor handles
repository-level implementation.

Conceptually:

``` text
OpenClaw Quant Agent:

"Implement EXP-0042 feature specification.
Do not modify holdout data.
Run unit tests.
Run only approved development/validation datasets.
Record results."

       ↓

Cursor coding session

       ↓

Cursor:
- reads repository rules;
- implements code;
- adds tests;
- runs approved tests;
- updates documentation/results.

       ↓

Quant Agent:
- analyzes experiment output.

       ↓

Reviewer:
- audits methodology and findings.

       ↓

Coordinator:
- summarizes for human approval.
```

Where available, OpenClaw/ACP integration can be used to launch Cursor
as an external coding agent rather than manually copying prompts between
systems.

------------------------------------------------------------------------

# 19. Automated Experiment Definitions

Experiments should be machine-readable.

Example:

``` yaml
experiment: EXP-0043

title: Persistent bid absorption after downward move

hypothesis: >
  After a downward price move, aggressive sell volume interacting with
  persistent bid liquidity predicts positive subsequent return when
  downward price progress is small relative to sell volume.

instruments:
  - NQ

features:
  - sell_volume_1s
  - sell_volume_3s
  - sell_orders_1s
  - downward_price_progress
  - sell_effort_result
  - displayed_bid_size
  - bid_relative_size
  - executed_volume_at_bid
  - absorption_ratio
  - replenishment_volume
  - bid_persistence_ms

outcomes:
  - return_100ms
  - return_250ms
  - return_500ms
  - return_1s
  - return_2s
  - return_5s
  - mfe_5s
  - mae_5s

datasets:
  development: <dataset>
  validation: <dataset>
  holdout: <locked dataset>

status: proposed
```

A human or approved coordinator process should move experiments through
states such as:

``` text
IDEA
PROPOSED
APPROVED
IMPLEMENTED
RUN
REVIEWED
VALIDATED
REJECTED
HOLDOUT_APPROVED
```

------------------------------------------------------------------------

# 20. Research Ledger

Every experiment should receive an immutable ID.

Initial suggested sequence:

``` text
EXP-0001  Buy/sell volume and executions per second
EXP-0002  Effort vs result / failure to progress
EXP-0003  Order-flow inflection
EXP-0004  Near-price liquidity pressure
EXP-0005  Liquidity persistence / withdrawal
EXP-0006  Absorption ratio
EXP-0007  Replenishment velocity
EXP-0008  Liquidity attraction/repulsion
EXP-0009  ES/NQ normalized flow divergence
EXP-0010  ES/NQ order-flow lead/lag
```

Do not delete failed experiments.

The experiment ledger protects against the AI/human tendency to try many
ideas and remember only successful ones.

------------------------------------------------------------------------

# 21. Scheduled Automated Research

Once the pipeline is stable, OpenClaw can manage repetitive jobs.

Potential overnight process:

``` text
01:00
Detect/process new Bookmap recordings

01:30
Convert/extract new market events

02:00
Update feature datasets

02:30
Run currently approved experiments

03:30
Generate statistics and plots

04:00
Skeptical reviewer audits new results

07:00
Produce daily research summary
```

Example human summary:

``` text
3 experiments completed.

EXP-0031
No useful relationship detected.

EXP-0032
Weak effect, unstable across sessions.

EXP-0033
Potentially interesting.
ES seller-efficiency deterioration 250-750 ms before NQ
was associated with improved NQ reversal outcomes.

Status:
Needs validation on independent period.
```

The system must not automatically promote a finding to live trading.

------------------------------------------------------------------------

# 22. Web / Academic Research Agent

The research agent can also monitor:

-   Bookmap API documentation.
-   Bookmap release/API changes.
-   CME MBO documentation.
-   Rithmic documentation.
-   Academic market-microstructure research.
-   Queue imbalance research.
-   Order-book resiliency.
-   Price impact.
-   Liquidity replenishment.
-   Cross-instrument lead/lag research.

When an external idea is found, store it as an **idea**, not as a proven
feature.

Example:

``` text
IDEA-0088

Concept:
Queue imbalance may predict very short-term price changes.

Relationship to current project:
Potential enhancement to near-price liquidity pressure.

Proposed experiment:
Test whether normalized queue imbalance improves
failure-to-progress reversal events.

Status:
Unapproved.
```

------------------------------------------------------------------------

# 23. Local vs Cloud

Large Bookmap recordings should preferably remain local.

Suggested arrangement:

``` text
Raw Bookmap data     → local PC / large local disk / NAS
Extraction           → local
DuckDB/Parquet       → local
Python research      → local
OpenClaw             → local where practical
Cursor local agent   → data-heavy coding/research
Git                  → source code + configs + reports
Cloud coding agents  → isolated code/refactor/test tasks
```

Avoid unnecessarily uploading large proprietary trading datasets to
remote coding environments.

------------------------------------------------------------------------

# 24. Machine Learning

Do not start with ML.

First determine whether human-understandable features have predictive
value.

Initial progression:

``` text
Market hypothesis
       ↓
Deterministic feature
       ↓
Statistical test
       ↓
Out-of-sample validation
       ↓
Feature combinations
       ↓
Only then consider ML
```

If ML is introduced later, candidate features could include:

-   flow acceleration;
-   effort/result;
-   absorption;
-   replenishment;
-   persistence;
-   queue imbalance;
-   order-size distributions;
-   liquidity distance;
-   ES/NQ divergence;
-   lead/lag state;
-   volatility;
-   time of day.

Potential models should initially be interpretable/simple before moving
to more complex models.

Avoid giving an unconstrained model thousands of raw inputs and
optimizing directly for historical P&L.

------------------------------------------------------------------------

# 25. Optuna / Parameter Optimization

Optuna or similar optimization frameworks may be useful later.

Do **not** start by running thousands of combinations looking for the
most profitable settings.

Correct order:

``` text
Hypothesis
    ↓
Evidence feature matters
    ↓
Robustness
    ↓
Reasonable parameter range
    ↓
Sensitivity analysis
    ↓
Optimization if warranted
```

A robust effect should normally exist across a sensible parameter region
rather than at one narrow optimum.

------------------------------------------------------------------------

# 26. Experiment Tracking

Initially, simple Git + Markdown + DuckDB/SQLite may be sufficient.

Every experiment should store:

``` text
experiment ID
date
hypothesis
Git commit
data version
instrument
session filters
feature definitions
parameters
sample size
results
MFE
MAE
future returns
transaction costs
plots
validation results
review comments
status
```

MLflow could be introduced later when the experiment count and parameter
space justify it.

------------------------------------------------------------------------

# 27. Key Design Principle: Event Research Before Strategy P&L

Initially, do not reduce every experiment to "would this strategy make
money?"

First determine whether an event has predictive information.

Example:

``` text
EVENT:
High sell aggression + low downward progress + persistent bid absorption

QUESTION:
What is the distribution of subsequent NQ returns?
```

Measure:

``` text
E[return 100ms]
E[return 250ms]
E[return 500ms]
E[return 1s]
E[return 2s]
E[return 5s]

median returns
win/loss distributions
MFE
MAE
tail behavior
time to MFE
time to MAE
```

Only after finding robust predictive effects should entry/exit rules be
optimized.

This separates **market behavior research** from **strategy
engineering**.

------------------------------------------------------------------------

# 28. Initial Development Roadmap

## Phase 0 --- Repository / Research Governance

Create:

-   `README.md`
-   `AGENTS.md`
-   `RESEARCH_BRIEF.md`
-   `RESEARCH_RULES.md`
-   `HYPOTHESES.md`
-   `EXPERIMENT_LOG.md`

Set up Git.

## Phase 1 --- Inspect Historical Data

Determine:

-   exact Bookmap recording format;
-   number/size/date range of files;
-   whether both ES and NQ are recorded;
-   whether MBO is retained;
-   timestamp resolution;
-   whether simultaneous ES/NQ recordings can be synchronized;
-   replay speed/batch capabilities;
-   extraction options.

This is a prerequisite for designing the backtester.

## Phase 2 --- Minimal Bookmap Add-on

Implement:

-   trade callback;
-   timestamp handling;
-   buy/sell classification;
-   buy/sell volume;
-   trades/executions;
-   one-second and sub-second aggregation;
-   Bookmap lower-panel visualization;
-   structured logging.

## Phase 3 --- Historical Extraction Pipeline

Build a Bookmap replay/extraction process producing normalized event
data.

Store in Parquet.

Validate extraction against Bookmap visually for selected periods.

## Phase 4 --- Event Research Framework

Implement:

-   feature API;
-   event detector API;
-   outcome labeler;
-   MFE/MAE;
-   future-return horizons;
-   experiment definitions;
-   reporting.

## Phase 5 --- First Core Features

Implement and test:

1.  effort/result;
2.  flow inflection;
3.  liquidity persistence;
4.  absorption;
5.  replenishment;
6.  liquidity attraction/repulsion.

## Phase 6 --- ES/NQ

Add:

-   synchronized instrument state;
-   normalized flow;
-   divergence;
-   lead/lag;
-   cross-market confirmation.

## Phase 7 --- Reversal State Machine

Combine validated components into a state-based reversal detector.

## Phase 8 --- Replay / Simulation Strategy

Use Bookmap replay/simulation to confirm that production implementation
behaves identically to research logic.

## Phase 9 --- Demo Execution

Implement:

-   order entry;
-   stop management;
-   target management;
-   max loss;
-   max position;
-   kill switch;
-   logging;
-   reconciliation.

## Phase 10 --- Live Evaluation

Only after extensive validation and explicit human approval.

------------------------------------------------------------------------

# 29. Suggested First Experiments

## EXP-0001 --- Flow Measurement Validation

Objective:

Validate accurate calculation of:

-   buy/sell volume;
-   buy/sell executions;
-   aggressive order estimates;
-   average aggressive size;
-   delta.

Compare visually against Bookmap.

No profitability research yet.

## EXP-0002 --- Failure to Progress

Hypothesis:

> Trend-side aggressive volume has less ability to move price
> immediately before some reversals.

Measure forward return/MFE/MAE across different normalized efficiency
levels.

## EXP-0003 --- Flow Inflection

Hypothesis:

> Deceleration of trend-side aggression combined with acceleration of
> counter-flow improves reversal outcomes.

## EXP-0004 --- Persistent Liquidity

Hypothesis:

> Large liquidity that persists as price approaches has different
> subsequent price behavior from large liquidity that withdraws.

## EXP-0005 --- Absorption

Hypothesis:

> High executed volume relative to displayed/resting liquidity with low
> penetration predicts reversal away from the level.

## EXP-0006 --- Replenishment

Hypothesis:

> Repeated replenishment at an absorbing level adds predictive
> information beyond executed-volume/displayed-size ratio alone.

## EXP-0007 --- Attract / Repel

Hypothesis:

> Some categories of unusually large near-price liquidity attract price
> before interaction and repel price after successful absorption.

## EXP-0008 --- ES Confirmation

Hypothesis:

> ES flow inflection/absorption state improves NQ reversal-event
> outcomes.

------------------------------------------------------------------------

# 30. Definition of Success

The goal is **not** to create an indicator that looks convincing on a
chart.

Success would mean identifying market behaviors that:

-   have a clear microstructure rationale;
-   can be objectively measured;
-   occur often enough to analyze;
-   show statistically meaningful forward behavior;
-   remain present across independent periods;
-   survive reasonable parameter variation;
-   remain useful after costs/slippage;
-   can be calculated in real time without future information;
-   can be implemented consistently in Bookmap.

Only then should the project attempt to convert them into an automated
trading strategy.

------------------------------------------------------------------------

# 31. Immediate Next Task for Cursor

Cursor should **not immediately write an auto-trader**.

The first task should be to initialize the repository and
inspect/validate the data/API assumptions.

Suggested Cursor prompt:

``` text
Read this document in full and treat it as the initial research brief for
the project.

Create the repository structure described here, but do not implement a
trading strategy yet.

First:

1. Create AGENTS.md, RESEARCH_RULES.md, HYPOTHESES.md and EXPERIMENT_LOG.md
   from the requirements in this brief.

2. Create a technical design for the Bookmap add-on and historical research
   pipeline.

3. Identify every assumption about the Bookmap API, Rithmic MBO, replay and
   .bmf extraction that must be verified before implementation.

4. Propose a minimal Phase-1 Bookmap Java add-on that records:
   - timestamps;
   - aggressive buy/sell trades;
   - volume;
   - execution counts;
   - execution-chain/order information where available;
   - MBO add/replace/cancel events where available.

5. Design a normalized Parquet event schema suitable for subsequent
   Python/DuckDB research.

6. Do not invent undocumented Bookmap behavior. Mark uncertain API details
   explicitly and verify them against current Bookmap documentation before
   coding against them.

7. Preserve the research rules: no holdout optimization, no look-ahead,
   no silent deletion of negative experiments, and no live trading without
   explicit human approval.

Return the proposed architecture and implementation plan before making
large-scale code changes.
```

------------------------------------------------------------------------

# 32. Important Context for Future AI Agents

The trader already has substantial discretionary experience reading
Bookmap. The purpose of this project is **not to replace that knowledge
with generic indicators**.

The core opportunity is to convert observations currently made visually
into measurable variables:

``` text
"What happened when price reached that large order?"

"Why did 500 contracts of selling fail to move price?"

"Did the bid replenish?"

"Did the order remain when price approached?"

"Did selling become less efficient?"

"Did buyers begin becoming more aggressive?"

"Did ES show the same change before NQ?"

"Did price become attracted to or repelled from nearby liquidity?"
```

The research system should remain centered on those questions.

The desired end state is an evidence-driven framework that can:

1.  quantify the trader's discretionary observations;
2.  discover which observations genuinely contain predictive
    information;
3.  display the useful measurements in Bookmap;
4.  automatically analyze large amounts of recorded data;
5.  combine validated signals into reversal models;
6.  eventually test automated execution without prematurely assuming
    profitability.

------------------------------------------------------------------------

# 33. Summary

The project should be approached as a **market-microstructure research
programme**, not simply as "write a trading bot."

Core stack:

``` text
Bookmap + Rithmic/CME MBO
        ↓
Java Bookmap integration
        ↓
Normalized historical event data
        ↓
Parquet + DuckDB
        ↓
Python research engine
        ↓
Hypothesis-driven experiments
        ↓
Walk-forward / holdout validation
        ↓
Validated reversal features
        ↓
Bookmap indicators
        ↓
Reversal state machine
        ↓
Simulation/demo execution
        ↓
Potential live automation
```

AI stack:

``` text
OpenClaw
    = coordinator / scheduled research / specialist agents

Cursor
    = primary coding and repository engineering agent

Git repository
    = persistent source of truth

Human trader
    = research direction, domain expertise, approval of hypotheses,
      interpretation, and final control over any live trading.
```

The first priority is to **preserve the trader's actual order-flow
knowledge in explicit hypotheses and build the data/research
infrastructure needed to test it at scale**.
