# KANKALAR GOLD AI
## Master Engineering & Quantitative Research Specification

**Document ID:** KGAI-MASTER-SPEC\
**Version:** 0.1 — Initial architecture baseline\
**Date:** 12 November 2026\
**Primary language:** Arabic user interface; English code, identifiers, logs, and technical documentation\
**Primary instrument:** Broker-provided gold/USD instrument, usually labelled `XAUUSD`, but the exact MT5 symbol must be discovered and validated\
**Platform:** MetaTrader 5 (MT5) as the primary broker-price/execution-reference interface\
**User interface:** Telegram alerts\
**Execution policy:** Research, analysis, and alerts only. The user makes every trading decision manually.

---

# 1. Executive mission

KANKALAR GOLD AI is a modular, evidence-driven quantitative research and decision-support system for intraday gold trading. Its job is not to promise a direction or produce a constant stream of trades. Its job is to:

1. Collect and validate market, volatility, positioning, and macroeconomic data.
2. Describe the current market regime and higher-timeframe context.
3. Run independent, versioned studies with explicit inputs, outputs, and limitations.
4. Test multiple strategy families under realistic transaction-cost assumptions.
5. Estimate the conditional probability and expected outcome of a proposed setup, only when enough out-of-sample evidence exists.
6. Calculate entry zones, invalidation levels, stop distances, target levels, and risk/reward using the broker's actual instrument specifications.
7. Reject weak, stale, contradictory, poorly calibrated, or expensive-to-execute opportunities.
8. Preserve every decision and later outcome so that the system can be audited and improved.
9. Send concise, explainable Telegram alerts. Never place or modify a real order.

**Core principle:** A no-trade decision is a valid and often preferable result. The system must never invent a setup to satisfy a schedule or user expectation.

**No performance promise:** A model score, backtest, win rate, or probability is not a guarantee of future results. A strategy becomes eligible for a paper-trading trial only after passing documented, chronological, out-of-sample tests.

# 2. Product boundaries and non-goals

## 2.1 In scope

- XAUUSD / broker gold CFD analysis.
- Optional comparison with COMEX Gold futures data (`GC`, and optionally Micro Gold `MGC`) when a properly licensed and sufficiently granular data source is available.
- Multi-timeframe intraday context.
- Technical, market-structure, volatility, liquidity, macro, and positioning studies.
- Statistical and machine-learning experiments.
- Backtesting, walk-forward evaluation, probability calibration, and signal lifecycle tracking.
- Telegram alerts and a read-only monitoring dashboard.
- Research-only paper-trading simulation.

## 2.2 Explicitly out of scope for v1

- Automatic order placement, modification, or cancellation.
- `mt5.order_send()` or any other live-trading function.
- Broker password storage or unattended account login unless a later, separately reviewed, read-only integration requires it.
- Copy trading, martingale, grid recovery, averaging down, or loss-recovery schemes.
- Claims that an AI model can reliably predict every market movement.
- Treating broker tick volume as consolidated global gold volume.
- Treating COMEX order book data as the complete global spot-gold order book.
- Using an LLM as the primary price-prediction engine.

The application must not import or call any MT5 trade-execution method in production code. Add an automated test that fails if prohibited trading calls appear in the application package.

# 3. Trading horizon and multi-timeframe plan

The selected style is **intraday**: setups may last from minutes to several hours. The initial timeframe map is:

| Timeframe | Role | Required use |
|---|---|---|
| D1 | Macro technical context | Broad trend, major levels, large volatility state |
| H4 | Primary regime | Main bullish / bearish / range / transition classification |
| H1 | Directional bias | Trend quality, structure, location, momentum |
| M15 | Setup discovery | Pullback, range rejection, breakout, liquidity event |
| M5 | Entry confirmation | Closed-bar confirmation and executable trigger definition |
| M1 | Optional diagnostics | Intrabar sequencing and finer execution simulation only when reliable data exists |

All bars must be timestamped in UTC. A signal may only use completed bars, unless a study explicitly documents that it is tick-based and records its exact event time. No signal may be based on a candle's final high, low, close, or volume before that candle has closed.

The system must support timeframe mapping as configuration, but the initial defaults above should be used for the first baseline experiment. Do not optimise timeframe combinations against the final holdout set.

# 4. Data architecture and source hierarchy

## 4.1 Source-of-truth hierarchy

1. **Broker MT5 feed:** authoritative for the exact instrument the user may later trade, including bid/ask, spread, trading sessions, contract properties, and available chart history.
2. **Exchange futures data:** optional contextual source for COMEX Gold futures price, volume, and (if licensed) market depth. It is a separate instrument, not a substitute for broker XAUUSD.
3. **Official macroeconomic sources:** FRED / ALFRED, Federal Reserve, BLS, BEA, Treasury, CFTC, and official event calendars.
4. **Research sources:** World Gold Council, LBMA publications, and peer-reviewed research. These may provide context, not fabricated intraday order flow.
5. **Commercial data vendor:** permitted only after verifying timestamp quality, coverage, API terms, costs, historical depth, and redistribution/storage rights.

Every data point must retain its source, instrument identifier, source timestamp, retrieval timestamp, timezone, units, and quality status.

## 4.2 MT5 connector requirements

Create an adapter around the official MetaTrader5 Python integration. It must support:

- Terminal connectivity and health checks.
- Symbol discovery; never hard-code `XAUUSD` as guaranteed to exist.
- `symbol_info`, `symbol_info_tick`, `copy_rates_range`, and `copy_ticks_range` where available.
- Bid, ask, tick timestamp, bar timestamp, spread, digits, point, tick size, tick value, contract size, volume minimum/maximum/step, trade mode, stops level, and relevant trading-session metadata.
- Historical data availability checks and explicit handling of truncated chart history.
- Connection retry with bounded exponential backoff, then a clear degraded/offline status.
- No call to `order_send`, `positions` mutation, or any trade execution API.

A production deployment must validate OS and package compatibility before setup. The current official PyPI wheels for the `MetaTrader5` package are Windows x86-64 builds, including a CPython 3.14 wheel; the official documentation describes Python-to-terminal interprocess communication. The system should therefore plan for a Windows PC/VPS running the MT5 terminal and adapter. Android/Termux should be treated as a monitoring/client environment, not the primary host for the official connector. Re-check compatibility at implementation time.

Use MT5 UTC handling correctly. MetaQuotes documents that MT5 bar/tick times are UTC and that Python datetimes supplied to history functions should be constructed in UTC. Never silently convert UTC-naive timestamps using the server's local timezone.

## 4.3 COMEX / futures context adapter

Support GC and optionally MGC only when a licensed data source is configured. Keep futures data separate from XAUUSD in storage and features. Required handling:

- Contract month and expiry metadata.
- Front-month selection and a documented roll rule.
- Continuous-series construction only for research, with raw contracts retained.
- Futures/spot basis and session differences.
- Volume and open-interest timestamps.
- Data-vendor and exchange licensing checks.

The CME product specification identifies the standard Gold futures contract as 100 troy ounces and Micro Gold as 10 troy ounces. These values must never be copied into the MT5 XAUUSD contract configuration; broker CFD specifications must be read directly from the connected terminal.

## 4.4 Macro and positioning data

Build optional, separately testable adapters for:

- FRED / ALFRED observations and vintages.
- US real yields and nominal Treasury yields.
- Dollar index data from a licensed or documented provider.
- Federal Reserve FOMC calendars, statements, and releases.
- BLS CPI, PPI, Employment Situation / NFP, and labor releases.
- BEA PCE and related releases.
- CFTC Commitments of Traders (COT), including the appropriate disaggregated metals report and the correct COMEX gold contract.
- Gold ETF flows and World Gold Council research data if the licensed/public source supports the needed frequency.
- VIX or other risk-sentiment proxies from a documented source.

**Point-in-time rule:** A historical backtest may use only information that was actually public at the signal timestamp. Store publication time separately from observation period. For revised macroeconomic series, use ALFRED vintages or another point-in-time archive. Do not backfill a revised value into a historical feature row as though it had been known then.

COT reports are low-frequency context, not intraday entry triggers. The CFTC states that reports are usually released Friday at 3:30 p.m. Eastern and generally describe positions from the previous Tuesday. Model availability by actual publication timestamp, including holiday delays.

## 4.5 Order-flow and liquidity data caveats

Potential inputs:

- MT5 tick stream and broker-provided depth, if available.
- COMEX traded volume, delta, and market depth, if licensed.
- VWAP and volume profile derived from a clearly identified venue/source.
- Cumulative volume delta, bid/ask aggressor proxies, and order-book imbalance only where the source's semantics justify them.

Never label broker tick volume as global traded volume. Never combine unlike venues without explicit normalization and source labels. A missing depth feed means `UNAVAILABLE`, not zero imbalance.

## 4.6 Data quality gate

Before analysis, validate:

- Timestamp monotonicity and timezone.
- Duplicate bars/ticks.
- Missing intervals and market closures.
- OHLC integrity (`low <= open/close <= high`).
- Non-finite, zero, or negative prices where invalid.
- Stale bid/ask and crossed/invalid quotes.
- Spread outliers and abnormal gaps.
- Symbol and contract metadata changes.
- Volume availability and meaning.
- Source outages and delayed publication.

A data-quality failure must block signal creation or downgrade the system to `DEGRADED`. Never silently substitute a different symbol, price source, or timeframe.

# 5. Research study catalogue

Each study must implement the common contract in Section 6 and have its own tests, version, and evaluation report. The initial study catalogue is intentionally broad; activation in the live signal ensemble is conditional on evidence.

## 5.1 Trend and momentum studies

- EMA / SMA slope and ordering (e.g. 20/50/200 as initial descriptive features, not universal trading rules).
- ADX and directional movement.
- RSI level, slope, and regime-aware interpretation.
- MACD level, signal relationship, histogram change, and zero-line context.
- Rate of Change / momentum over multiple horizons.
- Donchian channel position and breakout state.
- Supertrend or other ATR-based trend descriptors as candidates, not assumed superior.

## 5.2 Market structure and price action

- Confirmed swing highs/lows with non-repainting definitions.
- Break of Structure (BOS) and Change of Character (CHoCH), with exact event rules.
- Support/resistance zones based on repeatable swing/volume criteria.
- Range boundaries and failed breakouts.
- Opening range and session highs/lows.
- Gap and imbalance descriptors, including Fair Value Gap candidates with precise candle definitions.
- Liquidity sweep hypotheses around prior highs/lows and equal highs/lows.
- Candlestick patterns only where each pattern is formally defined and independently tested.

Terms such as “Smart Money”, “institutional footprint”, “liquidity grab”, or “order block” must not be used as evidence by themselves. Every concept needs an operational definition, a detection algorithm, and out-of-sample results.

## 5.3 Volatility studies

- True Range and ATR over multiple horizons.
- Realized volatility from intraday returns.
- EWMA volatility.
- GARCH(1,1) baseline.
- EGARCH / GJR-GARCH candidates for asymmetric volatility.
- HAR-RV candidate for multi-horizon realized-volatility dynamics.
- Volatility clustering and volatility percentile.
- Change-point detection for abrupt regime changes.
- Range expansion / compression and Bollinger bandwidth.

These models estimate volatility or risk distributions. They do not, by themselves, predict bullish or bearish direction.

## 5.4 Regime detection

Initial transparent baseline:

- Trend strength and slope.
- Directional persistence.
- Realized volatility percentile.
- Range efficiency / choppiness.
- Cross-timeframe disagreement.

Advanced candidates:

- Hidden Markov Model (HMM).
- Markov-switching models.
- Bayesian change-point detection.
- Clustering of volatility/return features.

Required regime labels:

- `BULLISH_TREND`
- `BEARISH_TREND`
- `SIDEWAYS`
- `HIGH_VOLATILITY`
- `TRANSITIONAL`
- `UNCERTAIN`

A regime model must expose posterior probabilities or uncertainty where possible. Do not force a crisp regime label when evidence is ambiguous.

## 5.5 Liquidity and volume studies

- Session VWAP and anchored VWAP with documented anchor rules.
- Volume profile / value area where true volume is available.
- Cumulative volume delta where aggressor-side data is trustworthy.
- Bid/ask spread percentile and spread shock detection.
- Order-book imbalance and depth depletion only when book data is available and stable.
- Relative volume by session and time of day.
- Volume/price divergence as a research feature.

## 5.6 Macro and positioning studies

- DXY return and trend state.
- US 10-year real yield and nominal yield changes.
- Yield-curve features, including 2Y/10Y changes where useful.
- Fed funds expectations only from time-stamped, licensed/documented data.
- CPI, PCE, PPI, NFP and unemployment surprises; compare actual with consensus only if archived consensus values are available with timestamps.
- FOMC event windows and event-risk classification.
- VIX changes and risk regime.
- COT net positioning, weekly changes, percentile/z-score, and crowded-positioning measures.
- ETF flows and official gold-market context when frequency and history support it.

Macro signals should be treated as conditional context. The system must not assume a constant inverse relationship between gold and the dollar, yields, or risk proxies.

# 6. Common study interface and evidence record

Every study must implement a consistent output, for example:

```json
{
  "study_id": "volatility.atr_regime",
  "version": "1.0.0",
  "instrument": "BROKER_GOLD_SYMBOL",
  "timeframe": "M15",
  "as_of_utc": "2026-11-12T12:00:00Z",
  "status": "AVAILABLE",
  "state": "EXPANDING",
  "value": 1.24,
  "normalized_value": 0.81,
  "direction": "NEUTRAL",
  "uncertainty": null,
  "inputs": {"atr_period": 14},
  "evidence": ["ATR percentile above the configured threshold"],
  "limitations": ["Broker tick volume is not consolidated market volume"],
  "data_snapshot_id": "..."
}
```

Allowed status values:

- `AVAILABLE`
- `INSUFFICIENT_DATA`
- `UNAVAILABLE`
- `STALE`
- `ERROR`

Allowed directional descriptors where applicable:

- `BULLISH`
- `BEARISH`
- `NEUTRAL`
- `NOT_APPLICABLE`

Rules:

- No hidden lookahead or repainting.
- No missing value silently converted to zero.
- Every output records input window, configuration, code version, and data snapshot.
- Each study is descriptive until its predictive contribution is separately validated.
- Correlated studies must be grouped or regularized; their agreement must not be counted as independent votes.

# 7. Strategy families

Build separate strategy modules. Each has a versioned specification and independent backtest.

1. **Trend pullback:** trend regime, retracement/location, resumed momentum, and structure-based invalidation.
2. **Range mean reversion:** confirmed range, edge location, rejection/mean-reversion evidence, and explicit range-failure stop.
3. **Volatility breakout:** compression, pre-defined breakout trigger, volatility confirmation, and false-breakout protection.
4. **Session/opening-range strategy:** session-aware opening range, documented session calendar, and time-of-day controls.
5. **Event-aware strategy filter:** restrict or suppress signals around scheduled and unscheduled high-impact events, based on measured spread/volatility behaviour.

Strategies must not be combined into a single ensemble until each has sufficient independent evidence. Strategy selection depends on regime posterior, evidence quality, execution costs, and out-of-sample performance.

# 8. Machine-learning and probabilistic layer

## 8.1 Model development order

Start with simple baselines and add complexity only when there is measurable out-of-sample improvement:

1. Logistic Regression / regularized linear baseline.
2. Random Forest baseline.
3. XGBoost and LightGBM candidates.
4. HMM or Markov-switching regime models.
5. Bayesian calibration / uncertainty candidates.
6. Deep sequence models (LSTM/Transformer) only if data volume, point-in-time quality, and out-of-sample evidence justify them.

## 8.2 Target definition

The model should answer a trading question, not merely predict the next close. Candidate target:

`P(TAKE_PROFIT_FIRST | setup, regime, costs, entry definition, horizon)`

Use triple-barrier labels with:

- Profit barrier.
- Stop/invalidation barrier.
- Maximum holding-time barrier.

A label must account for bid/ask execution side, spread, slippage assumptions, and ambiguous intrabar sequencing. Use tick data when available; otherwise use a pre-declared conservative OHLC rule. Mark ambiguous cases rather than inventing ordering.

## 8.3 Meta-labeling

A primary strategy proposes a setup. A secondary model estimates whether that setup should be accepted or rejected. It may use regime, spread, volatility, macro-event risk, structure quality, and calibrated primary-model outputs. It must be tested against the primary strategy alone.

## 8.4 Probability calibration and uncertainty

- Calibrate probability outputs on validation data only.
- Track Brier score, log loss, reliability diagrams, calibration slope/intercept, and expected calibration error where appropriate.
- Provide sample size and confidence intervals.
- Evaluate calibration by regime, strategy, session, and market period.
- Suppress numerical probability display when the relevant sample is too small or calibration has deteriorated.
- A technical evidence score is not a probability. A model probability is not a guarantee.

LLM usage is restricted to summarizing already-computed evidence, producing readable explanations, and answering questions about stored analysis. The LLM cannot modify prices, fabricate indicators, override risk gates, invent a probability, or autonomously approve a trade.

# 9. Decision engine

The decision engine consumes validated study outputs, regime probabilities, strategy proposals, model predictions, costs, and risk checks.

Possible states:

- `BUY_SETUP`
- `SELL_SETUP`
- `SETUP_WATCH`
- `WAIT_FOR_CONFIRMATION`
- `NO_TRADE`
- `DATA_DEGRADED`
- `SIGNAL_INVALIDATED`
- `SIGNAL_EXPIRED`

Required rejection gates:

- Stale or invalid market data.
- Missing critical study input.
- Spread above configured and validated threshold.
- Abnormal volatility / event risk outside tested limits.
- Conflicting regime evidence.
- No valid entry trigger.
- Stop distance structurally invalid or impractically wide.
- Expected reward after costs below the tested minimum.
- Insufficient sample or out-of-sample degradation.
- Existing duplicate or still-active signal for the same setup.

Do not use a naïve “number of bullish indicators” score. Group correlated features; use transparent rules or a trained/calibrated model with documented validation.

Every decision must include machine-readable reason codes and a human-readable explanation.

# 10. Entry, stop, targets, and risk engine

## 10.1 Entry model

Define entry as one of:

- Immediate marketable quote at a recorded bid/ask timestamp (for simulation only), or
- A price zone with an explicit trigger/confirmation condition.

A zone alone is not an executable signal. Telegram must clearly state whether the alert is `WATCH`, `WAITING_FOR_TRIGGER`, or `ENTRY_TRIGGERED`.

## 10.2 Stop-loss logic

Candidate stop methods:

- Structure invalidation beyond a swing/zone.
- ATR-based buffer adjusted for current volatility.
- Volatility distribution / expected adverse excursion.

Use the most defensible method for the tested strategy. The stop must invalidate the hypothesis, not simply fit a desired lot size. If the required stop makes reward/risk unattractive or risk too large, reject the trade.

## 10.3 Take-profit logic

Candidate targets:

- Next valid structural level.
- Range boundary / measured move.
- Volatility-scaled objective.
- Strategy-specific expected-value target.

Multiple targets may be reported, but each must be tied to a specific exit rule tested in backtesting. Avoid optimizing targets on the final holdout.

## 10.4 Broker-specific contract calculations

Read symbol metadata directly from MT5. Do not hard-code:

- Contract size.
- Tick size / point / digits.
- Tick value.
- Minimum, maximum, and step volume.
- Account currency conversion.
- Stop/freeze levels.
- Commission, swap, and margin assumptions.

Where appropriate, use MT5's calculation functions for hypothetical profit/margin estimates in the connected environment. These are calculations only; never send an order. Independently unit-test calculations against known examples and broker specifications.

## 10.5 Risk gates

Risk limits are configuration parameters, not hidden constants. The initial system must operate in research/paper mode using a virtual risk budget. Do not propose a personalized risk percentage or lot size until the user has explicitly provided account context and the broker's actual contract data has been validated.

Include configurable controls for:

- Maximum simulated risk per setup.
- Maximum aggregate open simulated risk.
- Daily loss stop.
- Drawdown stop.
- Consecutive-loss cooldown.
- Correlated/exposure caps if multiple instruments are added later.
- Spread and slippage limits.
- News/event blackout windows.

# 11. Backtesting and validation protocol

Backtesting is a research engine, not a marketing report.

## 11.1 Execution realism

Simulate:

- Buy entries at ask and sell entries at bid, with exit on the opposite quote.
- Variable spread and spread widening.
- Commission.
- Slippage, including stress scenarios.
- Swap/financing for trades crossing rollover.
- Trading hours, weekend gaps, holidays, outages, and broker-specific sessions.
- Stop/target ordering and gaps through stops.
- Rejected or unfilled limit entries where data supports it.

If historical bid/ask or tick data is unavailable, clearly downgrade the precision of the backtest and use conservative assumptions. Never present candle-close-only results as execution-grade simulation.

## 11.2 Chronological splits

Use strict time ordering:

- Development / training period.
- Validation period for model and parameter selection.
- Untouched final holdout.
- Subsequent paper-trading period.

Use rolling or anchored walk-forward testing. Fit scalers, imputers, feature selection, calibration, and thresholds on training/validation data only. Prevent overlap leakage with purging and embargo where labels span time. Never use random K-fold as the primary evaluation for time-dependent financial labels.

Combinatorially Symmetric Cross-Validation / Probability of Backtest Overfitting may be used as an additional multiple-testing diagnostic; it does not replace chronological walk-forward and untouched holdout testing.

## 11.3 Required metrics

- Net expectancy per trade and per unit of risk (R).
- Win rate with confidence interval.
- Profit factor.
- Maximum drawdown and duration.
- Sharpe and Sortino, with documented return sampling.
- Average win/loss and payoff ratio.
- Trade frequency and time in market.
- MAE / MFE.
- Tail loss / expected shortfall where suitable.
- Brier score / log loss / calibration.
- Results by regime, strategy, session, volatility bucket, and macro-event window.
- Sensitivity to spread, slippage, and delayed entry.
- Sample size and effective sample size.

Always report gross and net-of-cost results separately. Do not use a single metric to declare success.

## 11.4 Promotion gates

A candidate strategy can be promoted only when:

1. Data-quality checks pass.
2. No known lookahead or data leakage remains.
3. It beats a transparent baseline on relevant out-of-sample net metrics, not only training results.
4. The result is not driven by a few outliers or a single regime.
5. It survives cost and slippage stress tests.
6. Probability calibration and uncertainty are acceptable where probabilities are shown.
7. It passes unit, integration, and regression tests.
8. The report states limitations and sample size.

No arbitrary “must have 70% win rate” rule. Thresholds must be pre-registered based on strategy purpose, costs, sample uncertainty, and user risk constraints.

# 12. Signal lifecycle and Telegram

## 12.1 Signal lifecycle

Each signal receives a persistent ID and lifecycle state:

`CREATED -> WATCHING -> TRIGGERED -> ACTIVE_SIMULATION -> TP1 / TP2 / STOP / TIME_EXIT / INVALIDATED / EXPIRED`

The system must distinguish a setup alert from an executed entry. If the user manually enters a trade, the system should not assume execution unless the user explicitly records it through a Telegram command or dashboard action.

## 12.2 Telegram message requirements

Every alert should include:

- Instrument and broker symbol.
- Signal ID and creation time (UTC plus readable local display).
- Direction and lifecycle state.
- Market regime and timeframe roles.
- Entry zone and exact trigger condition.
- Stop-loss and invalidation reason.
- TP1 / TP2 and their exit assumptions.
- Estimated reward/risk after costs.
- Probability only if calibrated and sample-qualified.
- Evidence summary (3–5 strongest independent reasons).
- Important conflicting evidence / risks.
- Relevant event-risk window.
- Spread and data-quality status.
- Expiry and cancellation conditions.
- Model, strategy, and study versions.

Commands to consider:

- `/status` — current system and data health.
- `/gold` — latest market summary without a trade recommendation.
- `/signals` — current active/watch signals.
- `/history` — recent signal outcomes.
- `/why <signal_id>` — full explainability record.
- `/performance` — validated performance report.
- `/pause_alerts` and `/resume_alerts` — pause/resume notifications, never market data collection unless configured separately.

Store signal results before sending the Telegram message. Record Telegram send success/failure, retry safely, and prevent duplicate notifications. Never leak API tokens or credentials into logs.

# 13. Database and auditability

Use SQLite for the first single-host version, with a clear migration path to PostgreSQL. Suggested entities:

- `instruments`
- `symbol_specs_snapshots`
- `market_bars`
- `market_ticks` (optional/high-volume retention policy)
- `macro_observations`
- `macro_release_events`
- `cot_observations`
- `data_quality_events`
- `feature_snapshots`
- `study_results`
- `regime_predictions`
- `model_versions`
- `model_predictions`
- `strategy_runs`
- `signals`
- `signal_events`
- `simulated_trades`
- `signal_evaluations`
- `backtest_runs`
- `performance_reports`
- `telegram_delivery_log`
- `system_health`

Use UTC ISO-8601 timestamps. Preserve raw/source references and code/config versions. Avoid destructive updates to historical signal evidence. Store immutable snapshots or append-only event records for material decision changes. Add database migrations, indexes, backups, integrity checks, and retention policies.

The GOLD project must use its own root directory, environment variables, database, and bot token. Do not reuse or modify the KANKALAR LOSS AI database or running processes.

# 14. Reliability, security, and operations

- Use `.env` or OS secret storage; commit only `.env.example`.
- Redact credentials from logs and exception traces.
- Apply timeouts, bounded retries, and circuit breakers for external sources.
- Respect source API limits and licensing terms.
- Detect clock drift and timestamp inconsistencies.
- Add a heartbeat and health status for MT5, macro providers, database, and Telegram.
- Make scheduled jobs idempotent.
- Use a process lock to prevent duplicate scanner instances.
- Ensure a restart does not duplicate alerts or corrupt signal states.
- Provide graceful shutdown and database transactions.
- Keep alert delivery separate from analysis so Telegram outages do not stop market-data collection.
- Include a `SAFE_MODE` default that disables any trading functions and an explicit static test proving the restriction.

# 15. Suggested repository layout

```text
KANKALAR-GOLD-AI/
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore
├── docs/
│   ├── MASTER_SPEC.md
│   ├── DATA_DICTIONARY.md
│   ├── STUDY_CATALOGUE.md
│   ├── MODEL_CARD_TEMPLATE.md
│   ├── BACKTEST_PROTOCOL.md
│   ├── OPERATIONS.md
│   └── DECISIONS.md
├── config/
│   ├── defaults.yaml
│   ├── instruments.yaml
│   └── studies.yaml
├── src/kankalar_gold_ai/
│   ├── core/
│   ├── adapters/
│   │   ├── mt5/
│   │   ├── comex/
│   │   ├── macro/
│   │   └── telegram/
│   ├── data/
│   ├── studies/
│   ├── regimes/
│   ├── strategies/
│   ├── models/
│   ├── decision/
│   ├── risk/
│   ├── evaluation/
│   ├── signals/
│   ├── storage/
│   ├── notifications/
│   └── observability/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── regression/
│   └── fixtures/
├── scripts/
└── data/                  # runtime data; never commit private data
```

# 16. Implementation phases and acceptance criteria

## Phase 0 — Repository scaffold and engineering controls

Deliver:
- Project skeleton, configuration, logging, typed interfaces, README, architecture decisions.
- A test that proves there is no trading execution functionality.
- A mock data provider and synthetic fixtures.

Acceptance:
- Clean install and test command documented.
- No secrets or live account data required.
- Lint/type/test baseline passes.

## Phase 1 — MT5 read-only market data and quality

Deliver:
- MT5 health, symbol discovery, OHLC/tick retrieval, broker specification snapshots.
- UTC normalization, gap/stale/spread checks.
- Historical data export for research.

Acceptance:
- Works against a demo/read-only environment without sending orders.
- Detects missing symbol, stale tick, invalid OHLC, and limited chart history.
- Contract calculations use observed broker metadata.

## Phase 2 — Technical and structure studies

Deliver:
- Initial trend/momentum, market-structure, session, and volatility studies.
- Common study interface and snapshot persistence.

Acceptance:
- Unit tests include boundary cases, warm-up windows, missing bars, and no-lookahead checks.
- Each study has documentation and version.

## Phase 3 — Regime engine and strategy baselines

Deliver:
- Transparent regime baseline.
- Trend-pullback, range-reversion, and breakout strategies.
- Strategy registry and `NO_TRADE` gates.

Acceptance:
- Reproducible backtest on a fixed dataset.
- Every proposed setup is explainable and traceable.

## Phase 4 — Execution-aware backtesting and evaluation

Deliver:
- Bid/ask-aware simulator where data permits.
- Costs, slippage, swap, stop/target sequencing, walk-forward tests.
- Performance and robustness reports.

Acceptance:
- Tests prove no future data is used.
- Missing tick data triggers a documented conservative fallback.
- Gross/net metrics and costs are reported separately.

## Phase 5 — Macro, futures, and positioning context

Deliver:
- FRED/ALFRED, FOMC/BLS/BEA calendars, CFTC COT adapters.
- Optional COMEX data adapter behind configuration and licensing check.

Acceptance:
- Historical features respect actual publication time and vintage.
- Missing/late sources degrade gracefully.
- COT does not become a false intraday trigger.

## Phase 6 — Probabilistic ML and meta-labeling

Deliver:
- Baseline models, tree models, calibration, triple-barrier labels, meta-labeling experiments.
- Model registry and model cards.

Acceptance:
- Models beat simple baselines on untouched data to earn promotion.
- Probability is hidden when sample/calibration criteria fail.
- Feature transformations are fit only on training data.

## Phase 7 — Risk and signal lifecycle

Deliver:
- Entry/stop/target calculation, risk gates, expiry/invalidation, simulation trade tracking.

Acceptance:
- Broker-specific contract calculations validated.
- No invalid or stale signal can progress to triggered state.

## Phase 8 — Telegram and read-only dashboard

Deliver:
- Telegram commands and lifecycle alerts.
- Signal explanation and performance report.

Acceptance:
- Duplicate delivery tests pass.
- Notification failure does not interrupt scanning.
- Telegram interface cannot execute trades.

## Phase 9 — Paper-trading shadow run

Deliver:
- Continuous shadow operation with frozen strategy/model versions.
- Weekly evidence report and incident log.

Acceptance:
- Sufficient predeclared sample and duration across relevant regimes.
- Results remain consistent with out-of-sample expectations.
- Any material degradation blocks promotion and triggers review.

# 17. Development protocol for GitHub Copilot

Copilot must not generate the whole system in one uncontrolled pass.

For every phase:

1. Read this specification and the existing architecture before editing.
2. Propose a concise implementation plan and list files to change.
3. Implement only the current phase.
4. Add or update tests before claiming completion.
5. Run relevant tests and report exact results.
6. Run formatting, linting, type checks, and `git diff --check` where configured.
7. Summarize changed files, design choices, test evidence, known limitations, and next phase.
8. Do not proceed to the next phase until the current phase is reviewed.

General rules:

- Do not invent test results, market data, performance figures, or API availability.
- Do not use live network calls in unit tests; use fixtures and mocks.
- Never run a scanner against a live trading account to test signal generation.
- Never modify or reuse KANKALAR LOSS AI data, processes, configuration, or tokens.
- Do not remove tests merely to make the suite pass.
- Do not silently change a baseline strategy; version every change.
- Do not optimize on the final holdout set.
- Do not call an untested setup “high probability”.
- Do not add an automatic trade route or `order_send()`.
- If data requirements cannot be met, mark the relevant study `UNAVAILABLE` and explain the dependency.

# 18. Definition of a valid signal

A signal is valid only if all of the following are true:

- The broker symbol is verified and market data is fresh.
- Required bars are closed and complete.
- Data-quality gates pass.
- The market regime is known or uncertainty is explicitly accepted by the tested strategy.
- The strategy's setup and entry trigger are satisfied.
- Stop and targets are defined from tested rules.
- Spread and estimated execution costs are within validated limits.
- The risk engine approves the simulated risk.
- Probability output, if shown, is calibrated and sample-qualified.
- The signal has explicit expiry and invalidation logic.
- The signal snapshot and decision reasons are persisted successfully.

Otherwise return `WAIT`, `SETUP_WATCH`, `NO_TRADE`, or `DATA_DEGRADED` with reason codes.

# 19. Initial default configuration (research only)

```yaml
app:
  name: KANKALAR GOLD AI
  environment: research
  safe_mode: true
  timezone_storage: UTC
  language: ar

instrument:
  logical_name: GOLD_USD
  broker_symbol: null            # discover and confirm from MT5
  source: mt5

timeframes:
  context: [D1, H4]
  bias: [H1]
  setup: [M15]
  confirmation: [M5]
  diagnostics: [M1]

signals:
  allow_live_trading: false
  require_closed_bars: true
  allow_probability_display: false  # enable only after calibration/sample gates pass
  duplicate_suppression: true
  default_no_trade_on_uncertainty: true

risk:
  mode: virtual
  max_risk_per_trade: null         # define only after research and user review
  max_daily_loss: null
  max_drawdown: null

models:
  baseline: logistic_regression
  tree_candidates: [random_forest, xgboost, lightgbm]
  advanced_regime_candidates: [hmm, markov_switching]
  deep_learning_enabled: false

notifications:
  telegram_enabled: false          # enable only after alert tests pass
```

The configuration is illustrative. Copilot must validate it with a schema and configuration tests before runtime use.

# 20. Research references and implementation sources

The following are primary or high-authority sources to guide adapters and research. Re-check URLs, API terms, version compatibility, and availability during implementation.

1. MetaQuotes, Python integration overview: https://www.mql5.com/en/docs/python_metatrader5
2. MetaQuotes, `copy_rates_range` and UTC handling: https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesrange_py
3. MetaQuotes, symbol metadata: https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfo_py
4. MetaQuotes, hypothetical profit calculation: https://www.mql5.com/en/docs/python_metatrader5/mt5ordercalcprofit_py
5. PyPI, official `metatrader5` release wheels/platform compatibility: https://pypi.org/project/metatrader5/
6. CME Group, Gold futures contract specifications: https://www.cmegroup.com/markets/metals/precious/gold-futures.contractSpecs.html
7. CFTC, Commitments of Traders: https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm
8. CFTC, COT release schedule: https://www.cftc.gov/MarketReports/CommitmentsofTraders/ReleaseSchedule/index.htm
9. FRED API documentation: https://fred.stlouisfed.org/docs/api/fred/
10. FRED real-time periods / ALFRED vintage concepts: https://fred.stlouisfed.org/docs/api/fred/realtime_period.html
11. Federal Reserve FOMC calendars: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
12. BLS release calendar: https://www.bls.gov/schedule/2026/
13. World Gold Council, gold market size and structure: https://www.gold.org/goldhub/research/market-primer/gold-market-primer-market-size-and-structure
14. World Gold Council, gold trading volumes and methodology: https://www.gold.org/goldhub/data/gold-trading-volumes
15. Bailey et al., The Probability of Backtest Overfitting: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253

---

## Final product rule

**KANKALAR GOLD AI is a research and decision-support system. It is allowed to say “I do not know”, “the data is insufficient”, or “NO TRADE”. It is never allowed to manufacture certainty.**
